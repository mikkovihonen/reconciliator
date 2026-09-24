"""Declared funnel specs: dispatch, precedence, handlers and config overrides.

The 42 domain funnels were 1564 lines of near-identical method bodies dispatched
by a 44-condition if-chain. They are an ordered list of specs now, so these tests
pin the two properties the old code could not state: that every funnel is
reachable, and that precedence is explicit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reconciliator.funnels import (
    BUILTIN_HANDLERS,
    BUILTIN_SPECS,
    FunnelHandlerError,
    resolve_handler,
    resolve_specs,
)
from reconciliator.funnels.spec import FunnelSpec, FunnelWhen
from reconciliator.models import MatchRecord
from reconciliator.reconciliation import _merge_funnel_specs


def _record(**kw) -> MatchRecord:
    base = dict(record_id="r1", format="marcxml", title="T", title_stem="t")
    base.update(kw)
    return MatchRecord(**base)


def _select(record: MatchRecord, identifiers: dict | None = None, specs=None) -> FunnelSpec:
    """Mirror of WorkMatcher._select_funnel over a bare spec list."""
    specs = specs or BUILTIN_SPECS
    identifiers = identifiers or {}
    fallback = None
    for spec in specs:
        if spec.default:
            fallback = spec
            continue
        if spec.claims(record, identifiers):
            return spec
    return fallback


# --- shape ---------------------------------------------------------------


def test_there_is_exactly_one_default_funnel():
    defaults = [s.id for s in BUILTIN_SPECS if s.default]
    assert defaults == ["funnel_a_monograph"]


def test_funnel_ids_are_unique():
    ids = [s.id for s in BUILTIN_SPECS]
    assert len(ids) == len(set(ids))


def test_every_declared_handler_resolves():
    """A bad handler must fail at construction, not mid-corpus."""
    assert len(resolve_specs(BUILTIN_SPECS)) == len(BUILTIN_SPECS)


def test_every_funnel_is_reachable_by_a_record_of_its_own():
    """Guards the ordering: an over-broad earlier spec would shadow a later one."""
    unreachable = []
    for spec in BUILTIN_SPECS:
        if spec.default:
            continue
        when = spec.when[0]
        kw: dict = {}
        identifiers: dict = {}
        if when.work_type:
            kw["work_type"] = when.work_type[0]
        if when.content_type:
            kw["content_type"] = when.content_type[0]
        if when.is_component is not None:
            kw["is_component"] = when.is_component
        keys = {k: ["probe"] for k in when.has_key}
        for key_type in when.key_matches:
            keys[key_type] = ["parallel"]
        if keys:
            kw["keys"] = keys
        if when.title_matches:
            kw["title_stem"] = when.title_matches
        for scheme in when.has_identifier:
            identifiers[scheme] = ["probe"]
        for scheme in when.identifier_matches:
            identifiers[scheme] = ["URN:VALTO:1"]
        if _select(_record(**kw), identifiers).id != spec.id:
            unreachable.append(spec.id)
    assert unreachable == []


def test_the_default_funnel_claims_a_plain_text_record():
    assert _select(_record(work_type="text")).id == "funnel_a_monograph"


# --- precedence ----------------------------------------------------------


def test_the_music_split_keys_on_content_type_not_on_the_music_work_key():
    """Every music record carries a music_work key, score or recording alike.

    Dispatch keyed on that key claimed both, so funnel_b_music_sound was
    unreachable for any MARC record. content_type comes from the leader and is
    the real discriminator.
    """
    assert _select(_record(work_type="music", content_type="notated_music")).id == (
        "funnel_d_notated_music"
    )
    assert _select(_record(work_type="music", content_type="audio_music")).id == (
        "funnel_b_music_sound"
    )
    # A music_work key on a sound recording must not divert it.
    assert (
        _select(
            _record(work_type="music", content_type="audio_music", keys={"music_work": ["x"]})
        ).id
        == "funnel_b_music_sound"
    )


def test_exhibition_takes_precedence_over_conference_for_events():
    assert (
        _select(_record(work_type="event", keys={"exhibition_event": ["x"]})).id
        == "funnel_ll_exhibition"
    )
    # Events that are not exhibitions fall through to the conference funnel,
    # which is what the old chain did after its exhibition test failed.
    assert _select(_record(work_type="event")).id == "funnel_j_conference"


def test_component_records_reach_the_article_funnel():
    assert _select(_record(is_component=True)).id == "funnel_c_article"
    assert _select(_record(keys={"citation": ["issn::2020::11"]})).id == "funnel_c_article"


def test_government_is_claimed_by_work_type_or_by_a_valto_urn():
    assert _select(_record(work_type="government")).id == "funnel_p_government"
    assert (
        _select(_record(work_type="text"), {"urn": ["URN:ISBN:valto-123"]}).id
        == "funnel_p_government"
    )


def test_broadcast_claims_both_audio_and_moving_image_with_a_timecode():
    for work_type in ("audio_nonmusic", "moving_image"):
        assert (
            _select(_record(work_type=work_type, keys={"broadcast": ["t"]})).id
            == "funnel_gg_broadcast"
        )


# --- predicates ----------------------------------------------------------


def test_an_empty_when_matches_everything():
    assert FunnelWhen().matches(_record(), {})


def test_when_alternatives_are_disjunctive_and_fields_conjunctive():
    spec = FunnelSpec(id="f", when=[{"work_type": ["music"], "has_key": ["music_work"]}])
    assert not spec.claims(_record(work_type="music"), {})
    assert spec.claims(_record(work_type="music", keys={"music_work": ["x"]}), {})

    either = FunnelSpec(id="f", when=[{"work_type": ["music"]}, {"has_key": ["music_work"]}])
    assert either.claims(_record(work_type="music"), {})
    assert either.claims(_record(work_type="text", keys={"music_work": ["x"]}), {})


def test_regex_predicates_apply_to_titles_keys_and_identifiers():
    assert FunnelWhen(title_matches="(?i)näyttely").matches(
        _record(title_stem="kevään näyttely"), {}
    )
    assert FunnelWhen(key_matches={"translators_signature": "(?i)parallel"}).matches(
        _record(keys={"translators_signature": ["Parallel text"]}), {}
    )
    assert FunnelWhen(identifier_matches={"urn": "(?i)valto"}).matches(
        _record(), {"urn": ["urn:VALTO:9"]}
    )
    assert not FunnelWhen(identifier_matches={"urn": "(?i)valto"}).matches(
        _record(), {"urn": ["urn:nbn:fi:1"]}
    )


def test_single_when_and_single_work_type_accept_shorthand():
    spec = FunnelSpec(id="f", when={"work_type": ["music"]}, work_types="music")
    assert len(spec.when) == 1
    assert spec.work_types == ["music"]


# --- handlers ------------------------------------------------------------


def test_handler_resolves_by_builtin_name():
    for name in BUILTIN_HANDLERS:
        assert resolve_handler(name, "f") is not None


def test_handler_resolves_by_relative_path_against_the_config_directory():
    module = resolve_handler("src/reconciliator/funnels/recorded_sound.py", "f", Path.cwd())
    assert hasattr(module, "tier3")


def test_unknown_builtin_handler_names_the_funnel_and_lists_alternatives():
    with pytest.raises(FunnelHandlerError) as err:
        resolve_handler("no_such_handler", "funnel_x")
    assert "funnel_x" in str(err.value)
    assert "recorded_sound" in str(err.value)


def test_missing_handler_file_is_refused_at_resolution_time():
    with pytest.raises(FunnelHandlerError, match="not found"):
        resolve_handler("nowhere/absent.py", "funnel_x", Path.cwd())


def test_handler_paths_with_the_same_basename_do_not_collide(tmp_path: Path):
    """Two funnels may point at different files that happen to share a name."""
    a, b = tmp_path / "a" / "rule.py", tmp_path / "b" / "rule.py"
    for path, marker in ((a, "A"), (b, "B")):
        path.parent.mkdir(parents=True)
        path.write_text(f"MARKER = {marker!r}\n")
    mod_a = resolve_handler(str(a), "funnel_one")
    mod_b = resolve_handler(str(b), "funnel_two")
    assert (mod_a.MARKER, mod_b.MARKER) == ("A", "B")


# --- config overrides ----------------------------------------------------


def test_no_overrides_leaves_the_builtins_untouched():
    assert [s.id for s in _merge_funnel_specs(None)] == [s.id for s in BUILTIN_SPECS]


def test_an_override_replaces_a_builtin_in_place_keeping_its_precedence():
    original = [s.id for s in BUILTIN_SPECS]
    position = original.index("funnel_l_cartography")
    merged = _merge_funnel_specs(
        [FunnelSpec(id="funnel_l_cartography", when={"work_type": ["cartography"]}, confidence=0.5)]
    )
    assert [s.id for s in merged] == original
    assert merged[position].confidence == 0.5


def test_a_new_funnel_is_inserted_before_the_fall_through():
    merged = _merge_funnel_specs([FunnelSpec(id="funnel_custom", when={"work_type": ["hub"]})])
    ids = [s.id for s in merged]
    assert ids[-1] == "funnel_a_monograph", "the default must stay last"
    assert ids[-2] == "funnel_custom"


def test_a_configured_funnel_can_claim_records_the_builtins_would_not():
    merged = _merge_funnel_specs(
        [FunnelSpec(id="funnel_custom", when={"work_type": ["hub"]}, work_types=["hub"])]
    )
    assert _select(_record(work_type="hub"), specs=merged).id == "funnel_custom"
    # ...without disturbing anything else.
    assert _select(_record(work_type="text"), specs=merged).id == "funnel_a_monograph"


def test_before_places_a_new_funnel_ahead_of_a_builtin():
    """Without this a new funnel can only claim what nothing else wanted.

    The usual reason to add a funnel is to special-case records a built-in
    already handles, and precedence is first-match-wins, so a new funnel that
    overlaps a built-in is unreachable unless it can be placed ahead of it.
    """
    merged = _merge_funnel_specs(
        [
            FunnelSpec(
                id="funnel_local_theses",
                before="funnel_k_thesis",
                when=[{"work_type": ["thesis"], "identifier_matches": {"urn": "(?i)oulu"}}],
                work_types=["thesis"],
            )
        ]
    )
    ids = [s.id for s in merged]
    assert ids.index("funnel_local_theses") < ids.index("funnel_k_thesis")

    record = _record(work_type="thesis")
    assert _select(record, {"urn": ["URN:NBN:fi:oulu-1"]}, merged).id == "funnel_local_theses"
    assert _select(record, {"urn": ["URN:NBN:fi:jyu-9"]}, merged).id == "funnel_k_thesis"


def test_before_naming_an_unknown_funnel_is_refused():
    with pytest.raises(ValueError, match="names no known funnel"):
        _merge_funnel_specs([FunnelSpec(id="funnel_x", before="funnel_does_not_exist")])


def test_overriding_a_builtin_ignores_before_and_keeps_its_position():
    original = [s.id for s in BUILTIN_SPECS]
    merged = _merge_funnel_specs(
        [FunnelSpec(id="funnel_l_cartography", before="funnel_c_article", confidence=0.5)]
    )
    assert [s.id for s in merged] == original


# --- dispatch on real loader output ---------------------------------------
#
# The reachability test above builds each probe from the spec's own `when`, which
# proves a spec claims its own probe - not that records the loader actually
# produces reach it. That circularity hid a dead funnel: every MARC music record
# carries a `music_work` key, so keying funnel D on that key claimed sound
# recordings too and funnel B was unreachable. These tests dispatch loader
# output, which is the only thing that can catch that class of mistake.


def _marc(leader: str, title: str, extra: str = "", control_007: str = "") -> object:
    from lxml import etree

    ns = "http://www.loc.gov/MARC21/slim"
    cf007 = f'<controlfield tag="007">{control_007}</controlfield>' if control_007 else ""
    return etree.fromstring(
        f"""<record xmlns="{ns}">
            <leader>{leader}</leader>
            <controlfield tag="001">X1</controlfield>
            {cf007}
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Sibelius, Jean</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
            {extra}
        </record>""".encode()
    )


def _funnel_for(marc_record) -> str:  # noqa: ANN001
    from reconciliator.loaders.marcxml import build_target_record_from_marc
    from reconciliator.utils import extract_identifiers_from_target

    record = build_target_record_from_marc(marc_record, source="melinda")
    return _select(record, extract_identifiers_from_target(record)).id


def test_a_real_sound_recording_reaches_the_recorded_sound_funnel():
    """The regression this whole section exists for."""
    assert (
        _funnel_for(
            _marc(
                "00000cjm a2200000 i 4500",
                "Finlandia",
                extra='<datafield tag="028" ind1="0" ind2="2"><subfield code="a">ODE 1234-2</subfield></datafield>',
                control_007="sd f||g|||||",
            )
        )
        == "funnel_b_music_sound"
    )


def test_a_real_notated_music_record_reaches_the_notated_music_funnel():
    assert (
        _funnel_for(_marc("00000ccm a2200000 i 4500", "Karelia-sarja, op. 11"))
        == "funnel_d_notated_music"
    )


def test_a_finnish_score_title_reaches_notated_music_even_from_a_recording_leader():
    """The score vocabulary is the clusterer's, not English-only."""
    assert (
        _funnel_for(_marc("00000cjm a2200000 i 4500", "Karelia-sarja partituuri"))
        == "funnel_d_notated_music"
    )


def test_a_real_monograph_reaches_the_fall_through_funnel():
    assert (
        _funnel_for(_marc("00000cam a2200000 i 4500", "Sinuhe egyptilainen"))
        == "funnel_a_monograph"
    )


def test_a_real_component_record_reaches_the_article_funnel():
    assert (
        _funnel_for(
            _marc(
                "00000cam a2200000 i 4500",
                "Tutkimus jostakin",
                extra='<datafield tag="773" ind1="0" ind2=" "><subfield code="t">Host Journal</subfield><subfield code="q">45</subfield></datafield>',
            )
        )
        == "funnel_c_article"
    )


# --- relator terms with their diacritics folded away ----------------------


def test_an_ascii_folded_relator_term_resolves():
    """One unfolded term silently disabled cross-source work consolidation:
    `saveltaja` resolved to nothing, so the role was not a work role, so the
    contribution was filed against the *expression*, so
    `WorkConsolidator.load_signatures` - which reads contributions on works -
    found no creators, so the metadata score fell from clearing 0.93 to 0.90 and
    nothing consolidated at all. Found while measuring aggregate linking. See
    docs/aggregates.md."""
    from reconciliator.normalizers.relators import normalize_role

    for folded, proper in (
        ("saveltaja", "säveltäjä"),
        ("kaantaja", "kääntäjä"),
        ("esittaja", "esittäjä"),
        ("KAANTAJA", "kääntäjä"),
    ):
        assert normalize_role(folded) == normalize_role(proper) != folded


def test_an_exact_term_still_resolves_the_same_way():
    """Every standard role term resolves to its expected MARC code."""
    from reconciliator.normalizers.relators import normalize_role

    for term, code in [
        ("kirjoittaja", "aut"),
        ("toimittaja", "edt"),
        ("kuvittaja", "ill"),
        ("kääntäjä", "trl"),
        ("säveltäjä", "cmp"),
        ("näyttelijä", "act"),
        ("author", "aut"),
        ("editor", "edt"),
        ("författare", "aut"),
        ("redaktör", "edt"),
    ]:
        assert normalize_role(term) == code, term


def test_an_unknown_role_is_still_returned_as_itself():
    from reconciliator.normalizers.relators import normalize_role

    assert normalize_role("tuntematon-rooli") == "tuntematon-rooli"


def test_a_configured_mapping_folds_too():
    from reconciliator.normalizers.relators import RelatorRegistry

    registry = RelatorRegistry()
    registry.register("trl", "tulkitsijä")
    assert registry.normalize_role("tulkitsija") == "trl"


def test_a_folded_term_is_recognised_when_stripping_a_name():
    """`strip_relator` tests the same vocabulary, so "Jean Sibelius
    (saveltaja)" must lose its role marker like the properly spelled form."""
    from reconciliator.normalizers.relators import RelatorRegistry

    registry = RelatorRegistry()
    assert registry.strip_relator("Sibelius, Jean (saveltaja)") == "Sibelius, Jean"
    assert registry.strip_relator("Sibelius, Jean (säveltäjä)") == "Sibelius, Jean"


def test_names_are_not_folded_by_this():
    """`fold_label` deliberately preserves Finnish diacritics because it folds
    *names*, where å and a are different people. Only the relator lookup key is
    folded, and only a relator term is a closed vocabulary."""
    from reconciliator.normalizers.linguistic import fold_label

    assert fold_label("Åström, Ann", preserved_diacritics="åäöÅÄÖ").startswith("å")


def test_build_builtin_specs_custom_patterns():
    from reconciliator.funnels.builtin import build_builtin_specs

    custom = build_builtin_specs(
        patterns={
            "podcast_title": "custom_podcast_regex",
            "exhibition_title": "custom_exhibition_regex",
        }
    )
    podcast_spec = next(s for s in custom if s.id == "funnel_cc_podcast")
    assert any(
        getattr(w, "title_matches", None) == "custom_podcast_regex" for w in podcast_spec.when
    )

    exhibition_spec = next(s for s in custom if s.id == "funnel_ll_exhibition")
    assert any(
        getattr(w, "title_matches", None) == "custom_exhibition_regex" for w in exhibition_spec.when
    )


def test_audiobook_custom_config_fields():
    from unittest.mock import MagicMock

    from reconciliator.funnels.audiobook import tier2
    from reconciliator.funnels.engine import FunnelContext
    from reconciliator.funnels.spec import FunnelSpec
    from reconciliator.models import WorkEntity
    from reconciliator.reconciliation import WEMIMatchResult

    spec = FunnelSpec(
        id="funnel_e_audiobook",
        extra={
            "default_language": "swe",
            "title_suffix": " (talbok)",
            "source_id_suffix": "_tal",
            "expression_uri_prefix": "#talbok_",
        },
    )
    repo = MagicMock()
    work = WorkEntity(
        id=42,
        work_uri="http://example.org/work/42",
        source="melinda",
        source_id="src42",
        title="Test Title",
        title_normalized="test title",
        work_type="text",
    )
    repo.find_works_by_author_and_title.return_value = [work]
    repo.get_expressions_for_work.return_value = []

    ctx = FunnelContext(
        spec=spec,
        repo=repo,
        config=None,
        identifiers={},
        align_expression=MagicMock(),
        try_identifiers=MagicMock(),
        vector_or_fts=MagicMock(),
        rank_candidates=MagicMock(),
        result_cls=WEMIMatchResult,
        constituent_cls=MagicMock(),
    )
    record = MagicMock()
    record.record_id = "rec1"
    record.author_key = "author"
    record.title_stem = "title"
    record.language = None

    res = tier2(record, ctx)
    assert res is not None
    assert res.matched_expression.language == "swe"
    assert res.matched_expression.title == "Test Title (talbok)"
    assert res.matched_expression.source_id == "src42_tal"
    assert res.matched_expression.expression_uri == "melinda:src42_tal"
