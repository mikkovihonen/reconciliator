"""`ingest_mode: accumulate | replace` — what a re-ingest does to earlier rows.

Re-ingesting a source used to always accumulate: there was no `delete_source`,
no dedupe to catch the duplication, and since a corpus is now stored like any
other source, "re-ingest my corpus" and "re-harvest an authority" are the same
operation. Both behaviours are wanted, so it is a per-source setting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from lxml import etree

from reconciliator.config import OAIIngestConfig
from reconciliator.ingestion.oai import ingest_oai_pipeline
from reconciliator.loaders.marcxml import decompose_marc_wemi
from reconciliator.models import HarvestState
from reconciliator.repository import WEMIRepository
from tests.test_oai_harvest_resume import FakeEndpoint, make_repo, oai_config, stored_ids

MARC_NS = "http://www.loc.gov/MARC21/slim"
COUNTED = [
    "works",
    "expressions",
    "manifestations",
    "agents",
    "identifiers",
    "entity_keys",
    "contributions",
]


def _record(control: str, title: str) -> etree._Element:
    return etree.fromstring(
        f"""<record xmlns="{MARC_NS}">
            <controlfield tag="001">{control}</controlfield>
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Kivi, Aleksis</subfield><subfield code="d">1834-1872</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
            <datafield tag="020" ind1=" " ind2=" "><subfield code="a">97895100000{control[-1]}</subfield></datafield>
            <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Sibelius, Jean</subfield><subfield code="t">Finlandia</subfield></datafield>
        </record>""".encode()
    )


def _ingest(store: WEMIRepository, source: str, control: str, title: str) -> None:
    keys: list[Any] = []
    w, e, m, itms, agts, rels, idents, assoc, mops, contribs = decompose_marc_wemi(
        _record(control, title), source=source, record_origin=source, key_sink=keys
    )
    store.insert_wemi_batch(
        works=[w],
        expressions=[e],
        manifestations=[m],
        items=itms,
        agents=agts,
        relationships=rels,
        identifiers=idents,
        manifestation_expressions=assoc,
        mediums_of_performance=mops,
        contributions=contribs,
        entity_keys=keys,
        record_origin=source,
    )


def _counts(store: WEMIRepository) -> dict[str, int]:
    with store.get_connection() as conn:
        return {t: conn.exec_driver_sql(f"SELECT COUNT(*) FROM {t};").scalar() for t in COUNTED}


@pytest.fixture
def store(tmp_path: Path) -> WEMIRepository:
    return WEMIRepository(db_path=tmp_path / "authority.db")


# --- delete_source --------------------------------------------------------


def test_delete_source_removes_only_that_source(store: WEMIRepository):
    _ingest(store, "melinda", "M1", "Seitseman veljesta")
    _ingest(store, "melinda", "M2", "Nummisuutarit")
    _ingest(store, "fennica", "F1", "Kanteletar")

    store.delete_source("melinda")

    with store.get_connection() as conn:
        origins = list(conn.exec_driver_sql("SELECT DISTINCT record_origin FROM works;").scalars())
    assert origins == ["fennica"]
    assert _counts(store)["works"] == 1


def test_delete_source_leaves_no_orphaned_polymorphic_rows(store: WEMIRepository):
    """`identifiers`, `entity_keys` and `contributions` carry no foreign key.

    entity_type/entity_id can name any tier, which SQLite cannot express as one
    constraint, so there is no cascade and these rows must be deleted explicitly
    or they outlive their owners.
    """
    _ingest(store, "melinda", "M1", "Seitseman veljesta")
    _ingest(store, "fennica", "F1", "Kanteletar")
    store.delete_source("melinda")

    with store.get_connection() as conn:
        orphan_identifiers = conn.exec_driver_sql(
            """
            SELECT COUNT(*) FROM identifiers
            WHERE (entity_type = 'work' AND entity_id NOT IN (SELECT id FROM works))
               OR (entity_type = 'manifestation' AND entity_id NOT IN (SELECT id FROM manifestations))
               OR (entity_type = 'agent' AND entity_id NOT IN (SELECT id FROM agents));
            """
        ).scalar()
        orphan_keys = conn.exec_driver_sql(
            """
            SELECT COUNT(*) FROM entity_keys
            WHERE entity_type = 'manifestation'
              AND entity_id NOT IN (SELECT id FROM manifestations);
            """
        ).scalar()
        orphan_contribs = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM contributions WHERE agent_id NOT IN (SELECT id FROM agents);"
        ).scalar()
    assert (orphan_identifiers, orphan_keys, orphan_contribs) == (0, 0, 0)


def test_replace_then_reingest_restores_the_identical_state(store: WEMIRepository):
    """Why deterministic ids matter: a replace is a refresh, not a churn."""
    for control, title in (("M1", "Seitseman veljesta"), ("M2", "Nummisuutarit")):
        _ingest(store, "melinda", control, title)
    before = _counts(store)

    store.delete_source("melinda")
    for control, title in (("M1", "Seitseman veljesta"), ("M2", "Nummisuutarit")):
        _ingest(store, "melinda", control, title)

    assert _counts(store) == before


def test_delete_source_reports_what_it_removed(store: WEMIRepository):
    _ingest(store, "melinda", "M1", "Seitseman veljesta")
    removed = store.delete_source("melinda")
    assert removed["works"] == 1
    assert removed["entity_keys"] > 0
    assert sum(removed.values()) > 0


def test_deleting_an_unknown_source_is_a_no_op(store: WEMIRepository):
    _ingest(store, "melinda", "M1", "Seitseman veljesta")
    before = _counts(store)
    assert sum(store.delete_source("never_ingested").values()) == 0
    assert _counts(store) == before


# --- config ---------------------------------------------------------------


def test_accumulate_is_the_default():
    assert OAIIngestConfig(oai_url="http://x", metadata_prefix="marc21").ingest_mode == (
        "accumulate"
    )


def test_unknown_ingest_mode_is_refused():
    with pytest.raises(Exception):
        OAIIngestConfig(oai_url="http://x", metadata_prefix="marc21", ingest_mode="wipe")


# --- pipeline behaviour ---------------------------------------------------


def test_replace_clears_before_a_fresh_harvest(tmp_path: Path):
    repo = make_repo(tmp_path)
    _ingest(repo, "src", "OLD1", "Stale record")
    assert "OLD1" in stored_ids(repo)

    endpoint = FakeEndpoint(pages=[["N1", "N2"]])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", new=endpoint):
        ingest_oai_pipeline(source_name="src", config=oai_config(ingest_mode="replace"), store=repo)

    ids = stored_ids(repo)
    assert "OLD1" not in ids, "replace must remove rows the source wrote before"
    assert {"N1", "N2"} <= set(ids)


def test_accumulate_keeps_earlier_rows(tmp_path: Path):
    repo = make_repo(tmp_path)
    _ingest(repo, "src", "OLD1", "Earlier record")

    endpoint = FakeEndpoint(pages=[["N1"]])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", new=endpoint):
        ingest_oai_pipeline(
            source_name="src", config=oai_config(ingest_mode="accumulate"), store=repo
        )

    assert {"OLD1", "N1"} <= set(stored_ids(repo))


def test_replace_does_not_clear_when_resuming_an_interrupted_harvest(tmp_path: Path):
    """The subtlety: a resumed run would delete what its own slices just wrote.

    Clearing happens once, before the first batch, and only when the run is not
    continuing an earlier one.
    """
    repo = make_repo(tmp_path)
    _ingest(repo, "src", "FROM_EARLIER_SLICE", "Written by the interrupted run")
    repo.save_harvest_state(
        HarvestState(
            source_id="src",
            source_type="oai",
            metadata_prefix="marc21",
            status="in_progress",
            resumption_token="page-1",
            records_harvested=2,
        )
    )

    endpoint = FakeEndpoint(pages=[["A1"], ["B1"]])
    with patch("reconciliator.harvest.oai_pmh.OAIPMHHarvester.harvest_partition", new=endpoint):
        ingest_oai_pipeline(
            source_name="src", config=oai_config(ingest_mode="replace"), store=repo, resume=True
        )

    assert "FROM_EARLIER_SLICE" in stored_ids(repo)
