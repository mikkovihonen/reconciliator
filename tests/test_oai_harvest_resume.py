"""Tests for OAI-PMH harvest checkpointing, resume, and date-partitioned harvesting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from reconciliator.config import OAIIngestConfig, VectorEngineConfig
from reconciliator.db import works_table
from reconciliator.ingestion.oai import _resolve_harvest_window, ingest_oai_pipeline
from reconciliator.repository import WEMIRepository
from tests.test_oai_pmh import make_marc_record


def make_repo(tmp_path: Path) -> WEMIRepository:
    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    return WEMIRepository(
        db_path=tmp_path / "authority.db",
        work_index_path=tmp_path / "work.index",
        dimension=8,
        vector_engine_config=vec_cfg,
    )


def oai_config(**overrides: Any) -> OAIIngestConfig:
    base: dict[str, Any] = {
        "source_type": "oai",
        "oai_url": "https://example.org/oai",
        "batch_size": 2,
        "workers": 1,
        "checkpoint_interval_records": 1,
    }
    base.update(overrides)
    return OAIIngestConfig(**base)


def make_oai_page(rec_ids: list[str], resumption_token: Optional[str] = None) -> bytes:
    """Build a synthetic OAI-PMH ListRecords page XML containing MARCXML records."""
    records_xml = "".join(
        f"""
        <record>
          <metadata>
            <record xmlns="http://www.loc.gov/MARC21/slim">
              <controlfield tag="001">{rid}</controlfield>
              <datafield tag="245" ind1="1" ind2="0">
                <subfield code="a">Title {rid}</subfield>
              </datafield>
            </record>
          </metadata>
        </record>
        """
        for rid in rec_ids
    )
    token_xml = f"<resumptionToken>{resumption_token}</resumptionToken>" if resumption_token else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <ListRecords>
        {records_xml}
        {token_xml}
      </ListRecords>
    </OAI-PMH>
    """.encode("utf-8")


class FakeEndpoint:
    """Stands in for OAIPMHHarvester.harvest_partition.

    Serves records in fixed-size pages as raw XML bytes, yielding (page_bytes, next_token)
    the way the real async harvester does, and can fail after a given number of records
    to simulate an interrupted harvest.
    """

    def __init__(
        self,
        pages: list[list[str]],
        fail_after: Optional[int] = None,
    ) -> None:
        self.pages = pages
        self.fail_after = fail_after
        self.calls: list[dict[str, Any]] = []
        self.served = 0

    async def __call__(
        self,
        from_date: Optional[str] = None,
        until_date: Optional[str] = None,
        resumption_token: Optional[str] = None,
        semaphore: Optional[Any] = None,
    ) -> AsyncIterator[tuple[bytes, Optional[str]]]:
        self.calls.append(
            {
                "from_date": from_date,
                "until_date": until_date,
                "resumption_token": resumption_token,
            }
        )

        start_page = 0
        if resumption_token:
            start_page = int(str(resumption_token).removeprefix("page-"))

        for page_idx in range(start_page, len(self.pages)):
            rec_ids = self.pages[page_idx]
            if self.fail_after is not None and self.served >= self.fail_after:
                raise RuntimeError("endpoint died mid-harvest")
            self.served += len(rec_ids)
            is_last = page_idx == len(self.pages) - 1
            next_token = None if is_last else f"page-{page_idx + 1}"
            yield make_oai_page(rec_ids, next_token), next_token


def stored_ids(repo: WEMIRepository) -> list[str]:
    with repo.get_connection() as conn:
        return sorted(conn.execute(select(works_table.c.source_id)).scalars())


# ---------------------------------------------------------------------------
# Single continuous stream
# ---------------------------------------------------------------------------


def test_completed_harvest_records_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    endpoint = FakeEndpoint([["r1", "r2"], ["r3", "r4"]])

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
        total = asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    assert total == 4
    assert stored_ids(repo) == ["r1", "r2", "r3", "r4"]

    state = repo.get_harvest_state("melinda")
    assert state is not None
    assert state.status == "completed"
    # A finished harvest carries no token, so the next run is incremental.
    assert state.resumption_token is None
    assert state.records_harvested == 4
    assert state.last_harvest_until is not None


def test_interrupted_harvest_checkpoints_a_safe_token(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    # Serve two full pages, then die before or on the third.
    endpoint = FakeEndpoint([["r1", "r2"], ["r3", "r4"], ["r5", "r6"]], fail_after=4)

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
        with pytest.raises(RuntimeError, match="endpoint died mid-harvest"):
            asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    state = repo.get_harvest_state("melinda")
    assert state is not None
    assert state.status == "in_progress"
    # page-2 is only safe once pages 0 and 1 are durably written; it must never
    # point past what actually landed.
    assert state.resumption_token == "page-2"
    assert set(stored_ids(repo)) >= {"r1", "r2", "r3", "r4"}
    assert state.records_harvested >= 4


def test_resume_continues_from_checkpoint(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pages = [["r1", "r2"], ["r3", "r4"], ["r5", "r6"]]

    first = FakeEndpoint(pages, fail_after=4)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", first):
        with pytest.raises(RuntimeError):
            asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    token = repo.get_harvest_state("melinda").resumption_token
    assert token == "page-2"

    # Second run resumes and must request the checkpointed token, not page 0.
    second = FakeEndpoint(pages)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", second):
        total = asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    assert second.calls[0]["resumption_token"] == "page-2"
    # Only the unharvested tail was fetched.
    assert total == 2
    assert stored_ids(repo) == ["r1", "r2", "r3", "r4", "r5", "r6"]

    state = repo.get_harvest_state("melinda")
    assert state.status == "completed"
    assert state.resumption_token is None


def test_resume_false_forces_full_reharvest(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    pages = [["r1", "r2"], ["r3", "r4"], ["r5", "r6"]]

    first = FakeEndpoint(pages, fail_after=4)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", first):
        with pytest.raises(RuntimeError):
            asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    second = FakeEndpoint(pages)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", second):
        asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo, resume=False))

    assert second.calls[0]["resumption_token"] is None


def test_completed_harvest_goes_incremental_next_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    first = FakeEndpoint([["r1", "r2"]])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", first):
        asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    until = repo.get_harvest_state("melinda").last_harvest_until
    assert until is not None

    second = FakeEndpoint([["r3"]])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", second):
        asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    # The follow-up run asks only for records changed since the last completion.
    assert second.calls[0]["from_date"] == until


def test_checkpoint_never_outruns_written_records(tmp_path: Path) -> None:
    """The token is only advanced once the records before it are durably stored."""
    repo = make_repo(tmp_path)
    pages = [["r1", "r2"], ["r3", "r4"], ["r5", "r6"]]
    endpoint = FakeEndpoint(pages)

    observed: list[tuple[Optional[str], int]] = []
    real_save = WEMIRepository.save_harvest_state

    def spy(self: WEMIRepository, state: Any) -> None:
        with self.get_connection() as conn:
            written = conn.exec_driver_sql("SELECT COUNT(*) FROM works;").scalar()
        observed.append((state.resumption_token, written))
        real_save(self, state)

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
        with patch.object(WEMIRepository, "save_harvest_state", spy):
            asyncio.run(ingest_oai_pipeline("melinda", oai_config(), repo))

    # Each page holds 2 records, so "page-N" implies 2*N records already written.
    for token, written in observed:
        if token and token.startswith("page-"):
            assert written >= 2 * int(token.removeprefix("page-")), (
                f"checkpointed {token} with only {written} records written"
            )


# ---------------------------------------------------------------------------
# Date-partitioned harvesting
# ---------------------------------------------------------------------------


def test_workers_partition_the_date_range(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = oai_config(
        workers=4,
        slice_days=10,
        from_date="2024-01-01",
        until_date="2024-02-10",
        batch_size=1,
    )

    class PerSliceEndpoint(FakeEndpoint):
        async def __call__(self, from_date=None, until_date=None, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append({"from_date": from_date, "until_date": until_date})
            rec_id = f"rec-{from_date}"
            yield make_oai_page([rec_id]), None

    endpoint = PerSliceEndpoint([])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
        total = asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    # 2024-01-01..2024-02-10 in 10-day slices is more than one partition, and each
    # is fetched with its own bounded window.
    assert len(endpoint.calls) > 1
    assert total == len(endpoint.calls)
    windows = {(c["from_date"], c["until_date"]) for c in endpoint.calls}
    assert len(windows) == len(endpoint.calls)
    assert all(f and u for f, u in windows)

    state = repo.get_harvest_state("melinda")
    assert state.status == "completed"
    # A finished partitioned harvest clears the ledger.
    assert state.completed_slices == []


def test_partitioned_resume_skips_completed_slices(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = oai_config(
        workers=2,
        slice_days=10,
        from_date="2024-01-01",
        until_date="2024-02-10",
        batch_size=1,
    )

    class FlakySliceEndpoint:
        def __init__(self, fail_for: Optional[str]) -> None:
            self.fail_for = fail_for
            self.calls: list[tuple[Optional[str], Optional[str]]] = []

        async def __call__(self, from_date=None, until_date=None, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append((from_date, until_date))
            if from_date == self.fail_for:
                raise RuntimeError(f"slice {from_date} failed")
            rec_id = f"rec-{from_date}"
            yield make_oai_page([rec_id]), None

    # Fail the last slice so the earlier ones are recorded as complete.
    from reconciliator.harvest.oai_pmh import partition_date_range

    slices = partition_date_range("2024-01-01", "2024-02-10", 10)
    doomed = slices[-1][0]

    first = FlakySliceEndpoint(fail_for=doomed)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", first):
        with pytest.raises(RuntimeError):
            asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    state = repo.get_harvest_state("melinda")
    assert state.status == "in_progress"
    done = {tuple(s) for s in state.completed_slices}
    assert done, "at least one slice should have been recorded complete"
    assert tuple(slices[-1]) not in done

    # Resume: only the slices that never finished are re-requested.
    second = FlakySliceEndpoint(fail_for=None)
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", second):
        asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    requested = {(f, u) for f, u in second.calls}
    assert requested.isdisjoint(done)
    assert repo.get_harvest_state("melinda").status == "completed"


def test_single_worker_does_not_partition(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = oai_config(workers=1, from_date="2024-01-01", until_date="2024-12-31")
    endpoint = FakeEndpoint([["r1"]])

    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
        asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    assert len(endpoint.calls) == 1
    assert endpoint.calls[0]["from_date"] == "2024-01-01"
    assert endpoint.calls[0]["until_date"] == "2024-12-31"


def test_multiworker_without_from_date_discovers_earliest_datestamp(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = oai_config(workers=3, slice_days=30, batch_size=1)
    endpoint = FakeEndpoint([["r1"]])

    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        new_callable=AsyncMock,
        return_value="2023-01-01",
    ) as discover:
        with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
            asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    discover.assert_called_once()
    # Discovery supplies the lower bound, so the range can be partitioned.
    assert len(endpoint.calls) > 1


def test_multiworker_falls_back_when_discovery_fails(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = oai_config(workers=3, batch_size=1)
    endpoint = FakeEndpoint([["r1", "r2"]])

    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", endpoint):
            total = asyncio.run(ingest_oai_pipeline("melinda", cfg, repo))

    # No window to slice: one continuous stream, still fully harvested.
    assert len(endpoint.calls) == 1
    assert endpoint.calls[0]["from_date"] is None
    assert total == 2


# ---------------------------------------------------------------------------
# File sources are unaffected
# ---------------------------------------------------------------------------


def test_file_source_writes_no_harvest_state(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    cfg = OAIIngestConfig(source_type="file", input_path=Path("dummy.xml"), batch_size=2)
    records = [make_marc_record("f1", "File One"), make_marc_record("f2", "File Two")]

    with patch("reconciliator.ingestion.oai.stream_file_raw_elements", return_value=records):
        total = asyncio.run(ingest_oai_pipeline("localfile", cfg, repo))

    assert total == 2
    # Resumption is an OAI concept; a file source has nothing to check point.
    assert repo.get_harvest_state("localfile") is None


# ---------------------------------------------------------------------------
# Harvest window resolution
# ---------------------------------------------------------------------------


def test_single_worker_never_queries_identify() -> None:
    """Identify costs a round trip and is only needed to bound a partitioned range."""
    cfg = oai_config(workers=1, from_date=None, until_date=None)
    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        new_callable=AsyncMock,
    ) as identify:
        assert asyncio.run(_resolve_harvest_window("melinda", cfg)) == (None, None)
    identify.assert_not_called()


def test_from_date_without_until_defaults_to_today() -> None:
    from datetime import datetime, timezone

    cfg = oai_config(workers=2, from_date="2026-08-01", until_date=None, slice_days=10)
    resolved_from, resolved_until = asyncio.run(_resolve_harvest_window("melinda", cfg))

    assert resolved_from == "2026-08-01"
    assert resolved_until == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_explicit_window_is_left_alone() -> None:
    cfg = oai_config(workers=4, from_date="2024-01-01", until_date="2024-06-30")
    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        new_callable=AsyncMock,
    ) as identify:
        assert asyncio.run(_resolve_harvest_window("melinda", cfg)) == ("2024-01-01", "2024-06-30")
    identify.assert_not_called()


def test_file_source_never_queries_identify() -> None:
    cfg = OAIIngestConfig(source_type="file", input_path=Path("dummy.xml"), workers=8)
    with patch(
        "reconciliator.harvest.oai_pmh.OAIPMHHarvester.get_earliest_datestamp",
        new_callable=AsyncMock,
    ) as identify:
        assert asyncio.run(_resolve_harvest_window("localfile", cfg)) == (None, None)
    identify.assert_not_called()
