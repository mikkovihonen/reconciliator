"""Unit and regression tests for core data validators and sanitizers."""

import numpy as np

from reconciliator.indexer.usearch_engine import USearchVectorEngine
from reconciliator.models import Agent, AgentEntity
from reconciliator.reconciliation.consolidation import WorkConsolidator, WorkSignature
from reconciliator.utils.strings import sanitize_control_characters


def test_sanitize_control_characters() -> None:
    """Verify NUL byte and XML 1.0 illegal control characters are stripped."""
    raw = "Valid Title\x00 with\x07 control\x0b chars\t\nand newline"
    cleaned = sanitize_control_characters(raw, allow_newlines_tabs=True)
    assert "\x00" not in cleaned
    assert "\x07" not in cleaned
    assert "\x0b" not in cleaned
    assert "\t" in cleaned
    assert "\n" in cleaned
    assert cleaned == "Valid Title with control chars\t\nand newline"

    # None handling
    assert sanitize_control_characters(None) is None


def test_wikidata_bce_year_regex() -> None:
    """Verify negative BCE years in Wikidata timestamps are correctly parsed."""
    import re

    # ISO 8601 BCE timestamp for Plato (circa -0428)
    bce_val = "-0428-00-00T00:00:00Z"
    m_b = re.search(r"([+-]?\d{4})", bce_val)
    assert m_b is not None
    year = int(m_b.group(1))
    assert year == -428

    # Positive AD year
    ad_val = "+1920-05-12T00:00:00Z"
    m_ad = re.search(r"([+-]?\d{4})", ad_val)
    assert m_ad is not None
    assert int(m_ad.group(1)) == 1920


def test_inverted_lifespan_sanitization() -> None:
    """Verify cataloging typos where birth_year > death_year are swapped rather than raising ValueError."""
    # Agent model
    agent = Agent(name="Test Author", birth_year=1980, death_year=1908)
    assert agent.birth_year == 1908
    assert agent.death_year == 1980

    # AgentEntity model
    agent_entity = AgentEntity(
        name="Test Entity",
        name_normalized="test entity",
        source="src",
        source_id="1",
        birth_year=1975,
        death_year=1920,
    )
    assert agent_entity.birth_year == 1920
    assert agent_entity.death_year == 1975


def test_consolidation_isbn_unification() -> None:
    """Verify ISBN-10 and ISBN-13 unify to canonical 13-digit EAN in WorkConsolidator._add_identifier."""
    sig1 = WorkSignature(
        work_id=1,
        work_curie="work:1",
        source="src1",
        source_id="1",
        title_normalized="test book",
        work_type="monograph",
    )
    sig2 = WorkSignature(
        work_id=2,
        work_curie="work:2",
        source="src2",
        source_id="2",
        title_normalized="test book",
        work_type="monograph",
    )

    # ISBN-10 for 'The Old Man and the Sea': 0684801221
    # Canonical EAN-13: 9780684801223
    WorkConsolidator._add_identifier(sig1, "isbn", "0684801221")
    WorkConsolidator._add_identifier(sig2, "isbn", "978-0-684-80122-3")

    assert "isbn:9780684801223" in sig1.identifiers
    assert "isbn:9780684801223" in sig2.identifiers
    # They share the canonical identifier!
    assert sig1.identifiers.intersection(sig2.identifiers) == {"isbn:9780684801223"}


def test_vector_nan_sanitization(tmp_path) -> None:
    """Verify non-finite vectors (NaN, Inf) are sanitized in USearchVectorEngine.add_vectors."""
    engine = USearchVectorEngine(dimension=4, index_path=tmp_path / "usearch.index")
    # Matrix with NaN and Inf
    vectors = np.array(
        [
            [1.0, 0.0, np.nan, 0.0],
            [0.0, np.inf, 0.0, -np.inf],
        ],
        dtype=np.float32,
    )
    ids = engine.add_vectors(vectors, ids=[101, 102])
    assert ids == [101, 102]
    assert engine.total_vectors == 2


def test_wemi_foreign_key_quarantine(tmp_path) -> None:
    """Verify expressions, manifestations, and items with missing parents are quarantined instead of crashing."""
    from reconciliator.database import WEMIRepository
    from reconciliator.indexer import TorchVectorEngine
    from reconciliator.models import ExpressionEntity, ItemEntity, ManifestationEntity, WorkEntity

    db_path = tmp_path / "authority.db"
    w_idx = TorchVectorEngine(tmp_path / "work.pt", dimension=4)
    m_idx = TorchVectorEngine(tmp_path / "manif.pt", dimension=4)
    repo = WEMIRepository(
        db_path=db_path,
        dimension=4,
        work_indexer=w_idx,
        manifestation_indexer=m_idx,
    )

    work = WorkEntity(
        source="src", source_id="w1", title="Valid Work", title_normalized="valid work"
    )

    # Valid manifestation referencing work
    valid_manif = ManifestationEntity(
        source="src",
        source_id="m1",
        title="Valid Manif",
        title_normalized="valid manif",
        work_curie="src:w1",
    )
    # Orphan manifestation referencing non-existent work
    orphan_manif = ManifestationEntity(
        source="src",
        source_id="m_orphan",
        title="Orphan Manif",
        title_normalized="orphan manif",
        work_curie="src:non_existent",
    )

    # Valid expression referencing work
    valid_exp = ExpressionEntity(
        source="src",
        source_id="e1",
        work_id=0,
        language="fin",
        work_curie="src:w1",
    )
    # Orphan expression referencing non-existent work
    orphan_exp = ExpressionEntity(
        source="src",
        source_id="e_orphan",
        work_id=0,
        language="fin",
        work_curie="src:non_existent",
    )

    # Valid item referencing valid_manif
    valid_item = ItemEntity(
        source="src",
        source_id="m1",  # Matches (src, m1)
        manifestation_id=0,
        holding_organization="Lib",
    )
    # Orphan item referencing non-existent manifestation
    orphan_item = ItemEntity(
        source="other_src",
        source_id="i_orphan",
        manifestation_id=0,
        holding_organization="Lib",
    )

    work2 = WorkEntity(source="src", source_id="w2", title="Work Two", title_normalized="work two")

    # Must insert successfully without raising IntegrityError!
    repo.insert_wemi_batch(
        works=[work, work2],
        expressions=[valid_exp, orphan_exp],
        manifestations=[valid_manif, orphan_manif],
        items=[valid_item, orphan_item],
    )

    # Valid records were inserted
    assert repo.get_work_by_curie("src:w1") is not None
    assert repo.get_manifestation_by_curie("src:m1") is not None
    # Orphan records were quarantined (not inserted with broken null FK)
    assert repo.get_manifestation_by_curie("src:m_orphan") is None
