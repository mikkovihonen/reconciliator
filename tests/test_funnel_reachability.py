"""Can a real MARC record reach each funnel at all?

`test_every_funnel_is_reachable_by_a_record_of_its_own` in test_funnels.py builds
each probe from the spec's own `when`, so it proves a spec claims its own probe —
not that records the loader actually produces reach it. That circularity hid a
dead funnel: every MARC music record carries a `music_work` key, so funnel D
keyed on that key claimed sound recordings too and funnel B was unreachable.

This audit closes the loop from the other end. It reads what the loader can
actually emit — which work types `determine_work_type` returns, which content
types `determine_content_type` returns, which keys `derive_domain_keys` adds,
which identifier schemes `parse_marc_record_elem` extracts — and checks each
funnel has at least one `when` alternative those capabilities can satisfy.

A funnel failing this is not necessarily a bug: several exist for record types
the MARC loader does not classify yet, and the ingest path may grow to produce
them. It is a gap, and `KNOWN_UNREACHABLE_FROM_MARC` names it explicitly so the
set cannot drift in either direction unnoticed.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from reconciliator.funnels.builtin import BUILTIN_SPECS
from reconciliator.funnels.spec import FunnelWhen

LOADER = pathlib.Path(__file__).resolve().parents[1] / "src/reconciliator/loaders/marcxml.py"

# Funnels no MARC record can currently reach, each because the loader does not
# classify the record type they key on. Fixing one means teaching
# `determine_work_type` or `derive_domain_keys` to recognise its signal - a
# bibliographic decision, not a refactor - and then removing it from this list.
KNOWN_UNREACHABLE_FROM_MARC = {
    "funnel_dd_sammelband",            # no 'sammelband' key: bound-with is not parsed
    "funnel_ee_web_archive",           # no 'web_harvest' work type
    "funnel_g_anthology",              # no 'anthology' work type
    "funnel_gg_broadcast",             # no 'broadcast_program' type, no 'broadcast' key
    "funnel_ii_preservation_surrogate",  # no 'preservation_surrogate' type (533 unparsed)
    "funnel_jj_medieval_fragment",     # no 'codex_fragment' work type
    "funnel_nn_geodata",               # no 'geodata_service' work type
    "funnel_t_legal",                  # no 'legal' work type, no 'statute' key
    "funnel_z_tactile",                # no 'tactile' work type
}


def _function_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(LOADER.read_text())
    return next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _returned_literals(name: str) -> set[str]:
    """String literals a function returns, i.e. the classifications it can produce."""
    out: set[str] = set()
    for node in ast.walk(_function_body(name)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out.add(node.value.value)
    return out


def _added_key_types() -> set[str]:
    """First argument of every `_add(...)` call — AST, not regex, so multi-line
    calls are not silently missed the way an earlier hand audit missed one."""
    out: set[str] = set()
    for node in ast.walk(_function_body("derive_domain_keys")):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_add"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.add(node.args[0].value)
    return out


def _extracted_identifier_schemes() -> set[str]:
    """Keys of the `identifiers` dict `parse_marc_record_elem` returns."""
    for node in ast.walk(_function_body("parse_marc_record_elem")):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key, value in zip(node.value.keys, node.value.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "identifiers"
                    and isinstance(value, ast.Dict)
                ):
                    return {
                        k.value
                        for k in value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
    return set()


@pytest.fixture(scope="module")
def capabilities() -> dict[str, set[str]]:
    caps = {
        "work_types": _returned_literals("determine_work_type"),
        "content_types": _returned_literals("determine_content_type"),
        "keys": _added_key_types(),
        "identifiers": _extracted_identifier_schemes(),
    }
    # Guard the audit itself: an extraction returning nothing would make every
    # funnel look unreachable and the assertions below meaningless.
    for name, values in caps.items():
        assert values, f"capability extraction for {name} found nothing"
    return caps


def _satisfiable(when: FunnelWhen, caps: dict[str, set[str]]) -> bool:
    if when.work_type and not (set(when.work_type) & caps["work_types"]):
        return False
    if when.content_type and not (set(when.content_type) & caps["content_types"]):
        return False
    if any(k not in caps["keys"] for k in (*when.has_key, *when.key_matches)):
        return False
    if any(
        s not in caps["identifiers"]
        for s in (*when.has_identifier, *when.identifier_matches)
    ):
        return False
    return True


def _unreachable(caps: dict[str, set[str]]) -> set[str]:
    return {
        spec.id
        for spec in BUILTIN_SPECS
        if spec.when
        and not spec.default
        and not any(_satisfiable(w, caps) for w in spec.when)
    }


def test_the_set_of_marc_unreachable_funnels_is_exactly_as_documented(capabilities):
    """Fails in both directions, on purpose.

    Growing means a change made a funnel unreachable - the regression that hid
    funnel B. Shrinking means a gap was closed and this list is now stale.
    """
    assert _unreachable(capabilities) == KNOWN_UNREACHABLE_FROM_MARC


def test_most_funnels_are_reachable(capabilities):
    """A blunt floor, so a broad breakage cannot hide inside the exact-set test."""
    reachable = len(BUILTIN_SPECS) - len(_unreachable(capabilities))
    assert reachable >= 32, f"only {reachable} of {len(BUILTIN_SPECS)} funnels reachable"


def test_translators_signature_is_emitted(capabilities):
    """It was declared on MatchRecord and read by two consumers, but never written.

    The clusterer's pairwise scoring and funnel HH's dispatch both consulted it,
    so both were reading a value that was always absent.
    """
    assert "translators_signature" in capabilities["keys"]


def test_a_meeting_main_entry_classifies_as_a_conference(capabilities):
    """111 was parsed as a corporate agent but never drove the work type."""
    assert "conference" in capabilities["work_types"]


@pytest.mark.parametrize("work_type", sorted({"music", "cartography", "thesis", "serial"}))
def test_core_work_types_remain_producible(capabilities, work_type: str):
    assert work_type in capabilities["work_types"]


# --- the BibFrame path ----------------------------------------------------
#
# The same question from the other ingest path. BibFrame decomposition set
# work_type under exactly two graph conditions (C-3 hub, C-5 music) and defaulted
# everything else to "text", so a BibFrame cartographic resource, thesis or
# dataset all arrived as text and reached the monograph funnel: only 5 of 42
# funnels were reachable from a BibFrame record.
#
# Unlike the nine MARC gaps, this was a lookup rather than a heuristic - the
# BIBFRAME vocabulary names its Work subclasses explicitly.

BF_TTL = """
@prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
<http://example.org/w1> a bf:Work, bf:{cls} ;
    bf:title [ a bf:Title ; bf:mainTitle "{title}" ] .
<http://example.org/i1> a bf:Instance ;
    bf:instanceOf <http://example.org/w1> ;
    bf:title [ a bf:Title ; bf:mainTitle "{title}" ] .
"""


def _bibframe_record(cls: str, title: str = "Teos"):  # noqa: ANN202
    from rdflib import Graph

    from reconciliator.loaders.bibframe import build_target_record_from_bibframe

    graph = Graph().parse(data=BF_TTL.format(cls=cls, title=title), format="turtle")
    return build_target_record_from_bibframe(graph, source="bf")


def _bibframe_funnel(cls: str) -> str:
    from reconciliator.matcher import extract_identifiers_from_target

    record = _bibframe_record(cls)
    identifiers = extract_identifiers_from_target(record)
    fallback = None
    for spec in BUILTIN_SPECS:
        if spec.default:
            fallback = spec
            continue
        if spec.claims(record, identifiers):
            return spec.id
    return fallback.id


@pytest.mark.parametrize(
    ("bf_class", "expected_funnel"),
    [
        ("Text", "funnel_a_monograph"),
        ("Cartography", "funnel_l_cartography"),
        ("Dataset", "funnel_n_dataset"),
        ("MovingImage", "funnel_f_moving_image"),
        ("StillImage", "funnel_s_still_image"),
        ("Object", "funnel_q_realia"),
        ("Manuscript", "funnel_r_archival"),
    ],
)
def test_a_bibframe_class_reaches_its_own_funnel(bf_class: str, expected_funnel: str):
    assert _bibframe_funnel(bf_class) == expected_funnel


def test_bibframe_music_classes_split_the_way_marc_does():
    """Both are work_type "music"; only content_type separates them.

    Without a content type from the class, every BibFrame music record reached
    the recorded-sound funnel - scores included.
    """
    assert _bibframe_funnel("NotatedMusic") == "funnel_d_notated_music"
    assert _bibframe_funnel("Audio") == "funnel_b_music_sound"


def test_bibframe_content_classifies_a_generic_work():
    """bf:content is the fallback when the Work carries only the generic class."""
    from rdflib import Graph

    from reconciliator.loaders.bibframe import build_target_record_from_bibframe

    graph = Graph().parse(
        data="""
        @prefix bf: <http://id.loc.gov/ontologies/bibframe/> .
        <http://example.org/w2> a bf:Work ;
            bf:content <http://id.loc.gov/vocabulary/contentTypes/cartographic-image> ;
            bf:title [ a bf:Title ; bf:mainTitle "Kartta" ] .
        <http://example.org/i2> a bf:Instance ; bf:instanceOf <http://example.org/w2> ;
            bf:title [ a bf:Title ; bf:mainTitle "Kartta" ] .
        """,
        format="turtle",
    )
    assert build_target_record_from_bibframe(graph, source="bf").work_type == "cartography"


def test_an_unrecognised_bibframe_class_still_defaults_to_text():
    """Classification must not fail closed on a class it does not know."""
    assert _bibframe_record("SomethingUnmapped").work_type == "text"


def test_bibframe_reaches_substantially_more_funnels_than_before():
    """A floor on the improvement, so a regression to 'everything is text' shows.

    5 of 42 before the class mapping. The remaining gap is largely the same nine
    MARC gaps plus types BIBFRAME does not model as distinct classes.
    """
    from reconciliator.loaders.bibframe.decomposition import (
        BIBFRAME_CONTENT_TYPES,
        BIBFRAME_CONTENT_WORK_TYPES,
        BIBFRAME_WORK_TYPES,
    )

    work_types = (
        set(BIBFRAME_WORK_TYPES.values())
        | set(BIBFRAME_CONTENT_WORK_TYPES.values())
        | {"text", "hub", "music"}
    )
    content_types = set(BIBFRAME_CONTENT_TYPES.values()) | {"text", "audio_music"}
    caps = {
        "work_types": work_types,
        "content_types": content_types,
        "keys": {"author", "title_stem", "work", "music_work", "standard"},
        "identifiers": {"isbn", "issn", "urn", "doi", "other"},
    }
    reachable = len(BUILTIN_SPECS) - len(_unreachable(caps))
    assert reachable >= 17, f"only {reachable} funnels reachable from BibFrame"
