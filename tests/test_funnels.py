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
from reconciliator.matcher import _merge_funnel_specs
from reconciliator.models import MatchRecord


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


def test_notated_music_takes_precedence_over_recorded_sound():
    """A music record with a music work key is a score, not a recording."""
    assert _select(_record(work_type="music", keys={"music_work": ["x"]})).id == (
        "funnel_d_notated_music"
    )
    assert _select(_record(work_type="music")).id == "funnel_b_music_sound"


def test_exhibition_takes_precedence_over_conference_for_events():
    assert _select(
        _record(work_type="event", keys={"exhibition_event": ["x"]})
    ).id == "funnel_ll_exhibition"
    # Events that are not exhibitions fall through to the conference funnel,
    # which is what the old chain did after its exhibition test failed.
    assert _select(_record(work_type="event")).id == "funnel_j_conference"


def test_component_records_reach_the_article_funnel():
    assert _select(_record(is_component=True)).id == "funnel_c_article"
    assert _select(_record(keys={"citation": ["issn::2020::11"]})).id == "funnel_c_article"


def test_government_is_claimed_by_work_type_or_by_a_valto_urn():
    assert _select(_record(work_type="government")).id == "funnel_p_government"
    assert _select(
        _record(work_type="text"), {"urn": ["URN:ISBN:valto-123"]}
    ).id == "funnel_p_government"


def test_broadcast_claims_both_audio_and_moving_image_with_a_timecode():
    for work_type in ("audio_nonmusic", "moving_image"):
        assert _select(
            _record(work_type=work_type, keys={"broadcast": ["t"]})
        ).id == "funnel_gg_broadcast"


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
    module = resolve_handler(
        "src/reconciliator/funnels/recorded_sound.py", "f", Path.cwd()
    )
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
    merged = _merge_funnel_specs(
        [FunnelSpec(id="funnel_custom", when={"work_type": ["hub"]})]
    )
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
