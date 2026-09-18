"""End-to-end tests for Tier 3 Standalone Agent Fallback and Coupled Work Matching synthesis."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pytest

from reconciliator.config import ReconcileConfig, VectorEngineConfig
from reconciliator.indexer import create_vector_engine
from reconciliator.matcher import WorkMatcher
from reconciliator.models import (
    Agent,
    AgentEntity,
    BibliographicRecord,
    ContributionEntity,
    ExpressionEntity,
    IdentifierEntity,
    ManifestationEntity,
    SubjectsContainer,
    WorkEntity,
)
from reconciliator.normalizers.blocking import compute_title_stem
from reconciliator.repository import WEMIRepository


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


def _authority_record(
    source_id: str,
    title: str,
    publication_year: int,
    agent: AgentEntity,
    isbn: Optional[str] = None,
) -> dict[str, Any]:
    """Build one authority record as a WEMI split bound together by URIs."""
    work_uri = f"urn:work:melinda:{source_id}"
    expression_uri = f"urn:exp:melinda:{source_id}:fin"
    manifestation_uri = f"urn:manif:melinda:{source_id}"

    work = WorkEntity(
        work_uri=work_uri,
        source="melinda",
        source_id=source_id,
        record_origin="authority",
        title=title,
        title_normalized=compute_title_stem(title),
        work_type="text",
    )
    expression = ExpressionEntity(
        work_id=0,
        expression_uri=expression_uri,
        source="melinda",
        source_id=source_id,
        record_origin="authority",
        language="fin",
        content_type="text",
        title=title,
    )
    manifestation = ManifestationEntity(
        work_id=0,
        manifestation_uri=manifestation_uri,
        source="melinda",
        source_id=source_id,
        record_origin="authority",
        title=title,
        title_normalized=compute_title_stem(title),
        publication_year=publication_year,
        carrier_type="volume",
    )
    identifiers: list[IdentifierEntity] = []
    if isbn:
        identifiers.append(
            IdentifierEntity(
                entity_type="manifestation",
                entity_id=0,
                entity_uri=manifestation_uri,
                scheme="isbn",
                value=isbn,
                value_normalized=isbn,
            )
        )
    contribution = ContributionEntity(
        entity_type="work",
        entity_id=0,
        agent_id=0,
        entity_uri=work_uri,
        agent_uri=agent.agent_uri,
        role="aut",
        is_primary=True,
    )
    return {
        "works": [work],
        "expressions": [expression],
        "manifestations": [manifestation],
        "agents": [agent],
        "identifiers": identifiers,
        "contributions": [contribution],
    }


@pytest.fixture
def setup_authority_system(tmp_path: Path):
    db_path = tmp_path / "authority.db"
    work_idx_path = tmp_path / "authority.index"
    agent_idx_path = tmp_path / "agent.index"

    vec_cfg = VectorEngineConfig(backend="usearch", device="cpu", mode="embedded")
    store = WEMIRepository(
        db_path=db_path,
        work_index_path=work_idx_path,
        dimension=64,
        vector_engine_config=vec_cfg,
    )
    embedder = MockDeterministicEmbedder(dimension=64)

    waltari = AgentEntity(
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:000028241",
        source="melinda",
        record_origin="authority",
        wikidata_qid="Q12345",
        name="Waltari, Mika",
        name_normalized="waltari, mika",
        surname="Waltari",
        birth_year=1908,
        death_year=1979,
        dates="1908-1979",
        qualifiers=["kirjailija"],
    )
    # Two Matti Virtanens: a doctor and an artist, to exercise the homonym guard.
    virtanen_doctor = AgentEntity(
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:00099901",
        source="melinda",
        record_origin="authority",
        name="Virtanen, Matti",
        name_normalized="virtanen, matti",
        surname="Virtanen",
        birth_year=1915,
        death_year=1980,
        dates="1915-1980",
        qualifiers=["lääkäri"],
    )
    virtanen_artist = AgentEntity(
        agent_uri="http://urn.fi/URN:NBN:fi:au:pn:00099902",
        source="melinda",
        record_origin="authority",
        name="Virtanen, Matti",
        name_normalized="virtanen, matti",
        surname="Virtanen",
        birth_year=1940,
        death_year=2010,
        dates="1940-2010",
        qualifiers=["kuvataiteilija"],
    )


    for parts in (
        _authority_record(
            "melinda-001",
            "Sinuhe egyptiläinen",
            1945,
            waltari,
            isbn="9789510000001",
        ),
        _authority_record("melinda-002", "Sisätautioppi ja diagnostiikka", 1960, virtanen_doctor),
        _authority_record("melinda-003", "Moderni grafiikka", 1975, virtanen_artist),
    ):
        store.insert_wemi_batch(record_origin="authority", **parts)

    # Build agent.index so the Tier 3 standalone fallback has something to search.
    agent_indexer = create_vector_engine(agent_idx_path, dimension=64, config=vec_cfg)
    indexed_count = store.build_agent_index(agent_indexer, embedder)
    assert indexed_count >= 3

    return store, embedder, agent_indexer


def _reconcile_config(**overrides: Any) -> ReconcileConfig:
    base: dict[str, Any] = {
        "target_file": Path("dummy.xml"),
        "threshold": 0.85,
        "agent_reports": {"detailed_file": Path("d.tsv"), "summary_file": Path("s.tsv")},
        "subject_reports": {"detailed_file": Path("d2.tsv"), "summary_file": Path("s2.tsv")},
        "fallback_agent_reconciliation": True,
    }
    base.update(overrides)
    return ReconcileConfig(**base)


def _reconcile(matcher, bib: BibliographicRecord):
    """Match a record, then resolve its agents against that match.

    The conversion is spelled out here rather than hidden inside the matcher: a
    BibliographicRecord carries no work type and no domain keys, so a MatchRecord
    built from one is a deliberately impoverished input. The former
    `match_target_record` did this silently and matched on the result, which is
    why agent reconciliation never reached any funnel but the monograph one.
    """
    from reconciliator.models import MatchRecord
    from reconciliator.normalizers.blocking import (
        compute_author_key,
        compute_title_stem,
        compute_work_blocking_key,
    )

    author_key = compute_author_key(
        bib.agents[0].name if bib.agents else None,
        bib.agents[0].surname if bib.agents else None,
    )
    title_stem = compute_title_stem(bib.uniform_title or bib.title or "")
    record = MatchRecord(
        record_id=bib.source_id,
        format="bibra",
        title=bib.title or "",
        uniform_title=bib.uniform_title,
        language=bib.language,
        author_key=author_key,
        title_stem=title_stem,
        work_blocking_key=compute_work_blocking_key(author_key, title_stem),
        identifiers={"isbn": list(bib.identifiers.isbn)} if bib.identifiers.isbn else {},
        year=bib.publication_year,
    )
    return matcher.resolve_agents(matcher.match_target(record), bib)


def test_scenario1_coupled_match_transfers_agent_uris(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = _reconcile_config(threshold=0.80)
    matcher = WorkMatcher(
        wemi_repo=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
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

    res = _reconcile(matcher, target)
    assert len(res.accepted_candidates) == 1
    assert res.accepted_candidates[0].source_id == "melinda-001"
    # Agent URI transferred via Tier 2 coupled matching
    uris = res.discovered_agent_uris.get(0, [])
    assert any("000028241" in u.uri for u in uris)


def test_scenario2_fallback_success_with_dates(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = _reconcile_config(agent_fallback_threshold=0.80)
    matcher = WorkMatcher(
        wemi_repo=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
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

    res = _reconcile(matcher, target)
    # Coupled work matching did not match the book
    assert len(res.accepted_candidates) == 0

    # BUT Tier 3 Guarded Agent Fallback enriched the author!
    uris = res.discovered_agent_uris.get(0, [])
    assert len(uris) > 0
    assert any("000028241" in u.uri for u in uris)
    assert any("tier3_agent_fallback" == m.match_tier for m in res.matches)


def test_scenario3_fallback_success_with_qualifier(setup_authority_system) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = _reconcile_config(agent_fallback_threshold=0.80)
    matcher = WorkMatcher(
        wemi_repo=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
    )

    # Target book missing from Melinda, author is "Matti Virtanen" with qualifier "lääkäri"
    target = BibliographicRecord(
        source_id="target-003",
        source="target",
        title="Uusia lääketieteellisiä havaintoja",
        publication_year=1965,
        agents=[Agent(name="Virtanen, Matti", surname="Virtanen", qualifiers=["lääkäri"])],
        subjects=SubjectsContainer(),

    )

    res = _reconcile(matcher, target)
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
    r_cfg = _reconcile_config(agent_fallback_threshold=0.80)
    matcher = WorkMatcher(
        wemi_repo=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
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

    res = _reconcile(matcher, target)
    uris = res.discovered_agent_uris.get(0, [])
    # Date Guard must reject the match!
    assert len(uris) == 0


def test_scenario5_fallback_ambiguity_rejected_by_homonym_guard(
    setup_authority_system,
) -> None:
    store, embedder, agent_indexer = setup_authority_system
    r_cfg = _reconcile_config(agent_fallback_threshold=0.80, strict_homonym_guard=True)
    matcher = WorkMatcher(
        wemi_repo=store, embedder=embedder, config=r_cfg, agent_indexer=agent_indexer
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

    res = _reconcile(matcher, target)
    uris = res.discovered_agent_uris.get(0, [])
    # Rejection: cannot guess which Matti Virtanen it is!
    assert len(uris) == 0
