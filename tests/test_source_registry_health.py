"""The `sources` registry and `record_origin` have to agree.

With one database, "which rows may be matched against" is answered entirely by
`record_origin` plus the registry. When the two disagree the failure is silent
and total - the allow-list filters out every candidate, so matching returns
nothing and nothing errors. This cost two rounds of debugging during stage 2, so
it is reported rather than left to be inferred from empty output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reconciliator.database import WEMIRepository
from reconciliator.models import WorkEntity


@pytest.fixture
def store(tmp_path: Path) -> WEMIRepository:
    return WEMIRepository(db_path=tmp_path / "authority.db")


def _add_work(store: WEMIRepository, origin: str, source_id: str = "W1") -> None:
    store.insert_works_batch(
        [
            WorkEntity(
                work_uri=f"urn:work:{origin}:{source_id}",
                source=origin,
                source_id=source_id,
                record_origin=origin,
                title="T",
                title_normalized="t",
            )
        ]
    )


def test_a_consistent_store_reports_nothing(store: WEMIRepository):
    store.register_source("melinda", is_authoritative=True)
    _add_work(store, "melinda")
    assert store.check_source_registry() == []


def test_an_authoritative_source_with_no_rows_is_reported(store: WEMIRepository):
    """Its allow-list entry matches nothing, so it cannot be matched against."""
    store.register_source("melinda", is_authoritative=True)
    findings = store.check_source_registry()
    assert any("registered authoritative but no rows" in f for f in findings)


def test_rows_stamped_with_a_different_origin_are_reported(store: WEMIRepository):
    """The exact stage-2 failure: registered as one name, stamped as another.

    `authoritative_sources()` returns ['melinda'], the rows carry 'authority',
    so every candidate is filtered out and matching silently finds nothing.
    """
    store.register_source("melinda", is_authoritative=True)
    _add_work(store, "authority")
    findings = store.check_source_registry()
    assert any("registered authoritative but no rows" in f for f in findings)
    assert any("'authority'" in f and "invisible" in f for f in findings)


def test_no_authoritative_source_is_reported_as_an_unfiltered_search(store: WEMIRepository):
    """Corpus-only registration means the allow-list is empty, so no filter applies."""
    store.register_source("corpus", is_authoritative=False)
    _add_work(store, "corpus")
    findings = store.check_source_registry()
    assert any("no allow-list to apply" in f for f in findings)


def test_an_unregistered_origin_is_not_called_invisible_when_nothing_is_authoritative(
    store: WEMIRepository,
):
    """Guards against crying wolf.

    `_origin_clause` applies no filter at all when the allow-list would be
    empty, so with nothing authoritative every row stays visible. Reporting
    invisibility there would be wrong.
    """
    _add_work(store, "melinda")
    findings = store.check_source_registry()
    assert not any("invisible" in f for f in findings)
    assert any("no allow-list to apply" in f for f in findings)


def test_an_empty_store_reports_nothing(store: WEMIRepository):
    assert store.check_source_registry() == []


def test_the_finding_matches_what_matching_actually_does(store: WEMIRepository):
    """Ties the diagnostic to the behaviour it is warning about."""
    store.register_source("melinda", is_authoritative=True)
    _add_work(store, "authority", "A1")

    assert store.check_source_registry(), "the mismatch must be reported"
    # ...and the mismatch really does hide the row from authoritative search.
    assert store.find_works_by_author_and_title(None, "t", authoritative_only=True) == []
    assert store.find_works_by_author_and_title(None, "t", authoritative_only=False) != []
