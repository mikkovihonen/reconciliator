"""End-to-end tests for Tier 3 Standalone Agent Fallback and Coupled Work Matching synthesis."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pytest

from reconciliator.config import ReconcileConfig, VectorEngineConfig
from reconciliator.indexer import create_vector_engine
from reconciliator.matcher import ReconciliationEngine
from reconciliator.models import (
    Agent,
    BibliographicRecord,
    RecordIdentifiers,
    SubjectsContainer,
)
from reconciliator.repository import AuthorityStore


class MockDeterministicEmbedder:
    """Mock embedder generating deterministic normalized embeddings from text content."""

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension
        self.template_hash = "mock_template_hash"
        self.batch_size = 32

    def _word_vec(self, word: str) -> np.ndarray:
        seed = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dimension).astype(np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    def _hash_to_vec(self, text: str) -> np.ndarray:
        raw_tokens = [t.strip(":,|[]()").lower() for t in text.split()]
        stop_words = {"nimi", "sukunimi", "tarkenne", "vuodet"}
        tokens = [t for t in raw_tokens if t and t not in stop_words]
        if not tokens:
            tokens = ["empty"]

        vec = np.zeros(self.dimension, dtype=np.float32)
        for t in tokens:
            weight = 2.0 if t in {"waltari", "mika", "virtanen", "matti"} else 1.0
            vec += weight * self._word_vec(t)
        norm = np.linalg.norm(vec)
        if norm > 1e-9:
            vec /= norm
        return vec

    def embed_texts(self, texts: list[str], batch_size: Optional[int] = None) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.array([self._hash_to_vec(t) for t in texts], dtype=np.float32)

    def embed_records(
        self, records: list[BibliographicRecord], batch_size: Optional[int] = None
    ) -> np.ndarray:
        texts = [f"{r.title} {' '.join(a.name for a in r.agents)}" for r in records]
        return self.embed_texts(texts)


@pytest.fixture
def setup_authority_system(tmp_path: Path):
    db_path = tmp_path / "authority.db"
    work_idx_path = tmp_path / "authority.index"
    agent_idx_path = tmp_path / "agent.index"

    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = AuthorityStore(
        db_path=db_path,
        index_path=work_idx_path,
        dimension=64,
        vector_engine_config=vec_cfg,
    )
    embedder = MockDeterministicEmbedder(dimension=64)

    # 1. Insert authority records (coupled Work level)
    auth_rec1 = BibliographicRecord(
        source_id="melinda-001",
        source="melinda",
        title="Sinuhe egyptiläinen",
        publication_year=1945,
        identifiers=RecordIdentifiers(isbn=["9789510000001"]),
        agents=[
            Agent(
                name="Waltari, Mika",
                surname="Waltari",
                birth_year=1908,
                death_year=1979,
                dates="1908-1979",
                qualifier_counts={"kirjailija": 1},
                uri_subfield_0=["http://urn.fi/URN:NBN:fi:au:pn:000028241"],
                uri_subfield_1=["http://www.wikidata.org/entity/Q12345"],
            )
        ],
        subjects=SubjectsContainer(),
    )
    # Authority record 2: Virtanen, Matti (doctor)
    auth_rec2 = BibliographicRecord(
        source_id="melinda-002",
        source="melinda",
        title="Sisätautioppi ja diagnostiikka",
        publication_year=1960,
        agents=[
            Agent(
                name="Virtanen, Matti",
                surname="Virtanen",
                birth_year=1915,
                death_year=1980,
                dates="1915-1980",
                qualifier_counts={"lääkäri": 1},
                uri_subfield_0=["http://urn.fi/URN:NBN:fi:au:pn:00099901"],
            )
        ],
        subjects=SubjectsContainer(),
    )
    # Authority record 3: Virtanen, Matti (artist homonym)
    auth_rec3 = BibliographicRecord(
        source_id="melinda-003",
        source="melinda",
        title="Moderni grafiikka",
        publication_year=1975,
        agents=[
            Agent(
                name="Virtanen, Matti",
                surname="Virtanen",
                birth_year=1940,
                death_year=2010,
                dates="1940-2010",
                qualifier_counts={"kuvataiteilija": 1},
                uri_subfield_0=["http://urn.fi/URN:NBN:fi:au:pn:00099902"],
            )
        ],
        subjects=SubjectsContainer(),
    )

    records = [auth_rec1, auth_rec2, auth_rec3]
    vecs = embedder.embed_records(records)
    store.insert_records_batch(records, vectors=vecs)
    store.save_index()

    # 2. Build agent.index for fallback
    agent_indexer = create_vector_engine(agent_idx_path, dimension=64, config=vec_cfg)
    indexed_count = store.build_agent_index(agent_indexer, embedder)
    assert indexed_count >= 3

    return store, embedder, agent_indexer


def test_scenario1_coupled_match_transfers_agent_uris(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        threshold=0.80,
        agent_reports={"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        subject_reports={"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        fallback_agent_reconciliation=True,
    )
    matcher = ReconciliationEngine(
        authority_store=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Target book matches coupled work "Sinuhe egyptiläinen" by Mika Waltari
    target = BibliographicRecord(
        source_id="target-001",
        source="target",
        title="Sinuhe egyptiläinen",
        publication_year=1945,
        agents=[Agent(name="Waltari, Mika", surname="Waltari")],
        subjects=SubjectsContainer(),
    )

    res = matcher.reconcile_record(target)
    assert len(res.accepted_candidates) == 1
    assert res.accepted_candidates[0].source_id == "melinda-001"
    # Agent URI transferred via Tier 2 coupled matching
    uris = res.discovered_agent_uris.get(0, [])
    assert any("000028241" in u.uri for u in uris)


def test_scenario2_fallback_success_with_dates(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        threshold=0.85,
        agent_fallback_threshold=0.80,
        agent_reports={"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        subject_reports={"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        fallback_agent_reconciliation=True,
    )
    matcher = ReconciliationEngine(
        authority_store=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Target is a rare uncataloged publication: "Harvinaiset kirjeet", missing from Melinda
    # But author has life dates 1908-1979 matching Mika Waltari
    target = BibliographicRecord(
        source_id="target-002",
        source="target",
        title="Harvinaiset kirjeet",  # Completely different title -> coupled work fails
        publication_year=1955,
        agents=[
            Agent(
                name="Waltari, Mika",
                surname="Waltari",
                dates="1908-1979",
                birth_year=1908,
                death_year=1979,
            )
        ],
        subjects=SubjectsContainer(),
    )

    res = matcher.reconcile_record(target)
    # Coupled work matching did not match the book
    assert len(res.accepted_candidates) == 0

    # BUT Tier 3 Guarded Agent Fallback enriched the author!
    uris = res.discovered_agent_uris.get(0, [])
    assert len(uris) > 0
    assert any("000028241" in u.uri for u in uris)
    assert any("tier3_agent_fallback" == m.match_tier for m in res.matches)


def test_scenario3_fallback_success_with_qualifier(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        threshold=0.85,
        agent_fallback_threshold=0.80,
        agent_reports={"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        subject_reports={"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        fallback_agent_reconciliation=True,
    )
    matcher = ReconciliationEngine(
        authority_store=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Target book missing from Melinda, author is "Matti Virtanen" with qualifier "lääkäri"
    target = BibliographicRecord(
        source_id="target-003",
        source="target",
        title="Uusia lääketieteellisiä havaintoja",
        publication_year=1965,
        agents=[Agent(name="Virtanen, Matti", surname="Virtanen", qualifier_counts={"lääkäri": 1})],
        subjects=SubjectsContainer(),
    )

    res = matcher.reconcile_record(target)
    assert len(res.accepted_candidates) == 0

    # Fallback resolves to the doctor Virtanen, Matti (00099901), not the artist!
    uris = res.discovered_agent_uris.get(0, [])
    assert len(uris) > 0
    assert any("00099901" in u.uri for u in uris)
    assert not any("00099902" in u.uri for u in uris)


def test_scenario4_fallback_contradiction_rejected_by_date_guard(
    setup_authority_system,
) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        threshold=0.85,
        agent_fallback_threshold=0.80,
        agent_reports={"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        subject_reports={"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        fallback_agent_reconciliation=True,
    )
    matcher = ReconciliationEngine(
        authority_store=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Author named "Mika Waltari", but born in 1980 (different person!)
    target = BibliographicRecord(
        source_id="target-004",
        source="target",
        title="Tuntematon runoteos",
        publication_year=2015,
        agents=[
            Agent(
                name="Waltari, Mika",
                surname="Waltari",
                birth_year=1980,  # Contradicts authority (1908)!
                dates="1980-",
            )
        ],
        subjects=SubjectsContainer(),
    )

    res = matcher.reconcile_record(target)
    uris = res.discovered_agent_uris.get(0, [])
    # Date Guard must reject the match!
    assert len(uris) == 0


def test_scenario5_fallback_ambiguity_rejected_by_homonym_guard(
    setup_authority_system,
) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = ReconcileConfig(
        target_file=Path("dummy.xml"),
        threshold=0.85,
        agent_fallback_threshold=0.80,
        strict_homonym_guard=True,
        agent_reports={"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        subject_reports={"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        fallback_agent_reconciliation=True,
    )
    matcher = ReconciliationEngine(
        authority_store=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Target has only "Virtanen, Matti" with NO dates, NO qualifiers, and NO book match
    # There are multiple Matti Virtanens in authority DB -> homonym guard must reject!
    target = BibliographicRecord(
        source_id="target-005",
        source="target",
        title="Yleisiä ajatuksia",
        publication_year=1970,
        agents=[Agent(name="Virtanen, Matti", surname="Virtanen")],
        subjects=SubjectsContainer(),
    )

    res = matcher.reconcile_record(target)
    uris = res.discovered_agent_uris.get(0, [])
    # Rejection: cannot guess which Matti Virtanen it is!
    assert len(uris) == 0
