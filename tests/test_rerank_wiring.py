"""Candidate reranking, as actually wired into the matcher.

The `rerank` package existed, was configured in `config.example.yaml`, and was
connected to nothing: `CandidateEvaluator` was constructed into
`WorkMatcher.evaluator` and never read, and no rerank engine was ever built. Of
thirteen `RerankConfig` fields only `top_k` did anything, and that fed vector
retrieval depth rather than reranking.

Consequently the matcher could not prefer a better candidate over a worse one -
tier 2 took `candidates[0]` from the blocking query and tier 3 took the first
candidate over a bare dense threshold - and the volume, surname and year
discriminators never ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reconciliator.config import ReconcileConfig, RerankConfig
from reconciliator.database import WEMIRepository
from reconciliator.models import (
    AgentEntity,
    ContributionEntity,
    ManifestationEntity,
    MatchRecord,
    WorkEntity,
)
from reconciliator.reconciliation import WorkMatcher


@pytest.fixture
def store(tmp_path: Path) -> WEMIRepository:
    repo = WEMIRepository(db_path=tmp_path / "authority.db")
    repo.register_source("melinda", is_authoritative=True)
    return repo


def _add_work(
    repo: WEMIRepository,
    source_id: str,
    title: str,
    author: str = "Waltari, Mika",
    editions: tuple[tuple[int | None, str | None], ...] = ((None, None),),
) -> None:
    """Insert one work with one manifestation per (year, volume) edition."""
    work_uri = f"urn:work:melinda:{source_id}"
    repo.insert_wemi_batch(
        works=[
            WorkEntity(
                work_uri=work_uri,
                source="melinda",
                source_id=source_id,
                record_origin="melinda",
                title=title,
                title_normalized=title.lower(),
                work_type="text",
            )
        ],
        expressions=[],
        manifestations=[
            ManifestationEntity(
                manifestation_uri=f"urn:manif:melinda:{source_id}:{i}",
                source="melinda",
                source_id=f"{source_id}:{i}",
                record_origin="melinda",
                title=title,
                title_normalized=title.lower(),
                publication_year=year,
                volume_designation=volume,
            )
            for i, (year, volume) in enumerate(editions)
        ],
        agents=[
            AgentEntity(
                agent_uri=f"urn:agent:melinda:{source_id}",
                source="melinda",
                name=author,
                name_normalized=author.lower(),
                surname=author.split(",")[0],
                record_origin="melinda",
            )
        ],
        contributions=[
            ContributionEntity(
                entity_type="work",
                entity_id=0,
                agent_id=0,
                entity_uri=work_uri,
                agent_uri=f"urn:agent:melinda:{source_id}",
                role="aut",
                is_primary=True,
            )
        ],
        record_origin="melinda",
    )


def _matcher(repo: WEMIRepository, **rerank_overrides) -> WorkMatcher:
    return WorkMatcher(
        wemi_repo=repo,
        config=ReconcileConfig(
            target_file=Path("x"), threshold=0.85, rerank=RerankConfig(**rerank_overrides)
        ),
    )


def _record(**overrides) -> MatchRecord:
    base = dict(
        record_id="T1",
        format="marcxml",
        title="Sinuhe egyptilainen",
        title_stem="sinuhe egyptilainen",
        author_key="waltari",
        work_type="text",
    )
    base.update(overrides)
    return MatchRecord(**base)


def _rank(matcher: WorkMatcher, repo: WEMIRepository, record: MatchRecord, **kw):
    candidates = repo.find_works_by_author_and_title(
        record.author_key, record.title_stem, work_type="text"
    )
    return matcher._rank_candidates(record, [(c, 0.90) for c in candidates], **kw)


# --- the core capability --------------------------------------------------


def test_the_better_candidate_wins_over_the_first_one(store: WEMIRepository):
    """The blocking query returns everything sharing an author key and title stem.

    For a prolific author under a common title that is several genuinely
    different works, and the first row is whichever the index happened to
    return. This is the capability the matcher previously did not have.

    Both candidates share the title stem, which is what tier 2 blocks on: the
    two are separated by the year discriminator and by nothing else. An earlier
    version of this test used a candidate whose title merely *contained* the
    stem, which stopped being returned when the blocking query learned to try
    equality before a substring scan - it was leaning on blocking noise for its
    precondition rather than on ranking.
    """
    _add_work(store, "A_FAR", "Sinuhe egyptilainen", editions=((1902, None),))
    _add_work(store, "B_NEAR", "Sinuhe egyptilainen", editions=((1945, None),))
    matcher = _matcher(store)
    record = _record(year=1945)

    blocking = store.find_works_by_author_and_title("waltari", "sinuhe egyptilainen")
    assert [w.source_id for w in blocking] == ["A_FAR", "B_NEAR"]
    assert blocking[0].source_id == "A_FAR", "precondition: the weaker candidate is first"

    ranked = _rank(matcher, store, record, require_threshold=False)
    assert ranked[0][0].source_id == "B_NEAR"
    assert ranked[0][1].match_score > ranked[1][1].match_score

    result = matcher.match_target(record)
    assert result.matched_work is not None
    assert result.matched_work.source_id == "B_NEAR"


def test_the_reported_confidence_is_the_fused_score(store: WEMIRepository):
    """A tier that compares candidates should report which one it found.

    Previously every tier-2 match reported its spec's flat `confidence`.
    """
    _add_work(store, "W1", "Sinuhe egyptilainen")
    result = _matcher(store).match_target(_record())
    assert result.match_tier == "tier2_domain_key"
    assert result.confidence_score not in (0.0, 0.90), result.confidence_score


# --- the vetoes -----------------------------------------------------------


def test_a_conflicting_volume_rejects_the_candidate(store: WEMIRepository):
    _add_work(store, "V1", "Sinuhe egyptilainen", editions=((None, "osa 1"),))
    matcher = _matcher(store)

    same = _rank(matcher, store, _record(volume_designation="osa 1"), require_threshold=False)
    other = _rank(matcher, store, _record(volume_designation="osa 2"), require_threshold=False)
    assert same[0][1].accepted is True
    assert other[0][1].accepted is False
    assert other[0][1].volume_match is False


def test_volume_designations_are_compared_normalised(store: WEMIRepository):
    """Raw string equality rejected every differently-spelled volume.

    "osa 2" against "2. osa" is the usual case across two catalogues, and both
    normalise to the same designation.
    """
    _add_work(store, "V2", "Sinuhe egyptilainen", editions=((None, "2. osa"),))
    matcher = _matcher(store)
    for spelling in ("osa 2", "2. osa", "Vol. II"):
        ranked = _rank(
            matcher, store, _record(volume_designation=spelling), require_threshold=False
        )
        assert ranked[0][1].volume_match is True, spelling


def test_a_year_beyond_the_maximum_difference_rejects(store: WEMIRepository):
    _add_work(store, "Y1", "Sinuhe egyptilainen", editions=((1945, None),))
    matcher = _matcher(store)

    near = _rank(matcher, store, _record(year=1946), require_threshold=False)
    far = _rank(matcher, store, _record(year=1975), require_threshold=False)
    assert (near[0][1].accepted, near[0][1].year_diff) == (True, 1)
    assert (far[0][1].accepted, far[0][1].year_diff) == (False, 30)


def test_a_surname_mismatch_rejects(store: WEMIRepository):
    _add_work(store, "S1", "Sinuhe egyptilainen", author="Waltari, Mika")
    matcher = _matcher(store)
    # The record's author identity is its normalised author_key, so candidate
    # surnames are normalised the same way before comparison.
    assert (
        _rank(matcher, store, _record(author_key="waltari"), require_threshold=False)[0][
            1
        ].surname_match
        is True
    )
    ranked = matcher._rank_candidates(
        _record(author_key="linna"),
        [(store.find_works_by_author_and_title("waltari", "sinuhe egyptilainen")[0], 0.9)],
        require_threshold=False,
    )
    assert ranked[0][1].surname_match is False
    assert ranked[0][1].accepted is False


def test_disabling_reranking_bypasses_the_vetoes(store: WEMIRepository):
    """`rerank.enabled: false` is documented as a toggle, so it must toggle."""
    _add_work(store, "Y1", "Sinuhe egyptilainen", editions=((1945, None),))
    ranked = _rank(
        _matcher(store, enabled=False), store, _record(year=1975), require_threshold=False
    )
    assert ranked[0][1].accepted is True


# --- vetoes versus the fuzzy threshold ------------------------------------


def test_tier_two_is_not_held_to_the_fuzzy_threshold(store: WEMIRepository):
    """An exact-key match must not be rejected for a low title similarity.

    Tier-2 candidates already agree on author key *and* title stem. With the
    default weights and a 0.85 threshold, also demanding the fused score clear
    that bar requires a rerank score of 0.775 from an exact-key match - which
    rejected correct matches and pushed them down to FTS. The score orders
    tier-2 candidates; only the vetoes reject them.
    """
    _add_work(store, "W1", "Sinuhe egyptilainen ja pitka erilainen jatko-osa")
    matcher = _matcher(store)

    with_threshold = _rank(matcher, store, _record(), require_threshold=True)
    without = _rank(matcher, store, _record(), require_threshold=False)
    assert with_threshold[0][1].accepted is False, "the fused score is below 0.85"
    assert without[0][1].accepted is True, "but the vetoes all pass"
    # And the funnel takes the veto-based view.
    assert matcher.match_target(_record()).match_tier == "tier2_domain_key"


def test_vetoes_still_reject_even_without_the_threshold(store: WEMIRepository):
    """`require_threshold=False` relaxes the score bar, not the discriminators."""
    _add_work(store, "V1", "Sinuhe egyptilainen", editions=((None, "osa 1"),))
    ranked = _rank(
        _matcher(store), store, _record(volume_designation="osa 2"), require_threshold=False
    )
    assert ranked[0][1].accepted is False


# --- multiple editions ----------------------------------------------------


def test_a_work_matches_if_any_of_its_editions_is_compatible(store: WEMIRepository):
    """Volume and year live on the manifestation, and a work has several.

    Hydrating a candidate from the work alone left both as None, which the
    checks read as "do not penalise" - so they silently never fired.
    """
    _add_work(
        store,
        "M1",
        "Sinuhe egyptilainen",
        editions=((1945, "osa 1"), (1946, "osa 2"), (1947, "osa 3")),
    )
    matcher = _matcher(store)
    for volume in ("osa 1", "osa 2", "osa 3"):
        ranked = _rank(matcher, store, _record(volume_designation=volume), require_threshold=False)
        assert ranked[0][1].accepted is True, volume
    ranked = _rank(matcher, store, _record(volume_designation="osa 9"), require_threshold=False)
    assert ranked[0][1].accepted is False, "no edition is volume 9"


def test_ranking_without_an_evaluator_preserves_the_previous_behaviour(store: WEMIRepository):
    """No rerank config means input order and everything accepted."""
    _add_work(store, "A", "Sinuhe egyptilainen")
    _add_work(store, "B", "Sinuhe egyptilainen toinen")
    matcher = WorkMatcher(wemi_repo=store)
    assert matcher.evaluator is None
    ranked = _rank(matcher, store, _record())
    assert [w.source_id for w, _ in ranked] == [
        c.source_id for c in store.find_works_by_author_and_title("waltari", "sinuhe egyptilainen")
    ]
    assert all(v.accepted for _, v in ranked)


def test_rank_candidates_forwards_target_encoding_to_evaluator(store: WEMIRepository):
    """When target_encoding is provided to _rank_candidates, it must be passed
    to evaluate_candidates_batch so LLM judge KV caches are reused.
    """
    _add_work(store, "W_KV", "Sinuhe egyptilainen", editions=((1945, None),))
    matcher = _matcher(store)

    passed_target_encoding = None

    class MockEvaluator:
        def evaluate_candidates_batch(
            self,
            target_record,
            candidates,
            acceptance_threshold=0.85,
            author_relationships=None,
            target_encoding=None,
        ):
            nonlocal passed_target_encoding
            passed_target_encoding = target_encoding
            from reconciliator.models import ReconciliationMatch

            return [
                ReconciliationMatch(
                    candidate_record_id=cand.source_id,
                    source=cand.source,
                    match_tier="tier2_llm_judge",
                    match_score=0.95,
                    dense_score=dense_score,
                    accepted=True,
                )
                for cand, dense_score in candidates
            ]

    matcher.evaluator = MockEvaluator()
    works = store.find_works_by_author_and_title("waltari", "sinuhe egyptilainen")
    fake_encoding = object()

    ranked = matcher._rank_candidates(
        _record(),
        [(w, 0.9) for w in works],
        target_encoding=fake_encoding,
    )
    assert passed_target_encoding is fake_encoding
    assert len(ranked) == 1
    assert ranked[0][1].accepted is True
    assert ranked[0][1].match_score == 0.95


def test_llm_judge_score_batch_reuses_target_encoding():
    from reconciliator.rerank.llm_judge import LLMJudgeScorer

    class MockEvalResult:
        confidence_score = 0.99

    class MockBackend:
        def __init__(self):
            self.eval_call_count = 0
            self.encoded_target = False
            self.config = type(
                "Config", (), {"symmetric_scoring": False, "candidate_suffix_template": "{text}"}
            )()

        def encode_target(self, text, retain_cache=True):
            self.encoded_target = True
            return type(
                "Encoding", (), {"kv_cache": object(), "base_seq_len": 10, "prior_bias": 0.0}
            )()

        def evaluate_candidate(self, base_cache, base_seq_len, candidate_suffix, prior_bias):
            self.eval_call_count += 1
            return MockEvalResult()

    mock_backend = MockBackend()
    scorer = LLMJudgeScorer(backend=mock_backend)

    # 1. Without target_encoding, it calls encode_target
    scores = scorer.score_batch("target query", ["cand 1", "cand 2"])
    assert scores == [0.99, 0.99]
    assert mock_backend.encoded_target is True

    # 2. With target_encoding supplied, it reuses it directly without calling encode_target
    mock_backend.encoded_target = False
    fake_enc = type("Encoding", (), {"kv_cache": object(), "base_seq_len": 10, "prior_bias": 0.0})()
    scores2 = scorer.score_batch("target query", ["cand 1"], target_encoding=fake_enc)
    assert scores2 == [0.99]
    assert mock_backend.encoded_target is False
