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
# `determine_work_type` or `derive_domain_keys` to recognise its signal, which
# needs a citable rule from the Finnish RDA application guidelines rather than a
# guess - see docs/plans/wemi_data_structure.md 14.17 for the ones researched so
# far and the open questions to put to the National Library.
KNOWN_UNREACHABLE_FROM_MARC = {
    # SLM's "katkelmat" (s1210) is the only candidate term, and it carries no
    # definition and no broader concept in the vocabulary, so it may well mean a
    # published excerpt rather than a manuscript fragment. Using it would be the
    # kind of guess this table exists to prevent; it is a question for the
    # National Library instead. See 14.18 in docs/plans/wemi_data_structure.md.
    "funnel_jj_medieval_fragment",
}


def _function_body(name: str) -> ast.FunctionDef:
    tree = ast.parse(LOADER.read_text())
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


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
    """What the MARC loader can emit, read from its own declaration.

    This used to infer the answer by parsing the loader's source, and got it
    wrong twice: a regex missed a multi-line `_add()` call, and literal-only
    extraction missed classification through lookup tables - the second time
    passing while four funnels it claimed were unreachable had just become
    reachable. `LoaderCapabilities` states it instead, and
    `RecordLoader.check_output` (exercised in tests/test_loader_contract.py)
    keeps the statement honest against what the loader actually produces.
    """
    from reconciliator.loaders.registry import MarcBibliographicLoader

    declared = MarcBibliographicLoader.capabilities
    caps = {
        "work_types": set(declared.work_types),
        "content_types": set(declared.content_types),
        "keys": set(declared.key_types),
        "identifiers": set(declared.identifier_schemes),
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
    if any(s not in caps["identifiers"] for s in (*when.has_identifier, *when.identifier_matches)):
        return False
    return True


def _unreachable(caps: dict[str, set[str]]) -> set[str]:
    return {
        spec.id
        for spec in BUILTIN_SPECS
        if spec.when and not spec.default and not any(_satisfiable(w, caps) for w in spec.when)
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
    from reconciliator.utils import extract_identifiers_from_target

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


# --- RDA content types (336 $b, $2 rdacontent) ----------------------------
#
# Codes checked against the LoC "Term and Code List for RDA Content Types". The
# previous mapping was written from memory and had four codes pointing at the
# wrong concept and three that do not exist in the vocabulary at all.
#
# Sources:
#   https://loc.gov/standards/valuelist/rdacontent.html
#   https://wiki.helsinki.fi/xwiki/bin/view/rdasovellusohje/
#     RDA-kuvailu MARC 21 -formaatilla/RDA-sovellusohje/3XX Fyysisen kuvailun jne. kentät/

RDA_CONTENT_TYPE_STANDARD = {
    "crd": "cartographic dataset",
    "cri": "cartographic image",
    "crm": "cartographic moving image",
    "crt": "cartographic tactile image",
    "crn": "cartographic tactile three-dimensional form",
    "crf": "cartographic three-dimensional form",
    "cod": "computer dataset",
    "cop": "computer program",
    "ntv": "notated movement",
    "ntm": "notated music",
    "prm": "performed music",
    "snd": "sounds",
    "spw": "spoken word",
    "sti": "still image",
    "tci": "tactile image",
    "tcm": "tactile notated music",
    "tcn": "tactile notated movement",
    "tct": "tactile text",
    "tcf": "tactile three-dimensional form",
    "txt": "text",
    "tdf": "three-dimensional form",
    "tdm": "three-dimensional moving image",
    "tdi": "two-dimensional moving image",
}


def test_every_mapped_content_code_is_a_real_rda_code():
    """The loader once mapped `dta`, `dcr` and `tcg`, none of which exist."""
    from reconciliator.loaders.marcxml import RDA_CONTENT_TYPES

    invented = set(RDA_CONTENT_TYPES) - set(RDA_CONTENT_TYPE_STANDARD)
    assert invented == set(), f"not RDA content codes: {sorted(invented)}"


def test_the_moving_image_codes_are_not_transposed():
    """`tdi` is two-dimensional - ordinary film and video - and was mapped to 3-D.

    This is the code the overwhelming majority of moving-image records carry, so
    the transposition mislabelled essentially every film in a corpus.
    """
    from reconciliator.loaders.marcxml import RDA_CONTENT_TYPES

    assert RDA_CONTENT_TYPES["tdi"] == "moving_image"
    assert RDA_CONTENT_TYPES["tdm"] == "three_dimensional_moving_image"


def test_the_tactile_codes_are_not_swapped():
    """`tcf` is a tactile three-dimensional form, `tcm` tactile notated music."""
    from reconciliator.loaders.marcxml import RDA_CONTENT_TYPES

    assert RDA_CONTENT_TYPES["tcm"] == "tactile_notated_music"
    assert RDA_CONTENT_TYPES["tcf"] != "tactile_notated_music"


def test_computer_dataset_is_mapped():
    """`cod` is the real code; the loader had mapped a non-existent `dta`."""
    from reconciliator.loaders.marcxml import RDA_CONTENT_TYPES

    assert RDA_CONTENT_TYPES["cod"] == "dataset"


@pytest.mark.parametrize(
    ("code", "expected_work_type"),
    [
        ("tdi", "moving_image"),
        ("cod", "dataset"),
        ("cri", "cartography"),
        ("tct", "tactile"),
        ("tcm", "tactile"),
    ],
)
def test_a_content_type_classifies_a_record_with_a_generic_leader(
    code: str, expected_work_type: str
):
    """An RDA record must carry 336, and electronic resources often have a
    generic Leader/06, so the content type has to be able to classify."""
    from lxml import etree

    from reconciliator.loaders.marcxml import build_target_record_from_marc

    ns = "http://www.loc.gov/MARC21/slim"
    record = etree.fromstring(
        f"""<record xmlns="{ns}">
            <leader>00000cam a2200000 i 4500</leader>
            <controlfield tag="001">X1</controlfield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Teos</subfield></datafield>
            <datafield tag="336" ind1=" " ind2=" "><subfield code="b">{code}</subfield><subfield code="2">rdacontent</subfield></datafield>
        </record>""".encode()
    )
    assert build_target_record_from_marc(record, source="m").work_type == expected_work_type


def test_the_accessibility_field_marks_a_braille_book_as_tactile():
    """`341 0# $a tactile $e braille $2 sapdv` is what the Finnish accessibility
    guidance tells cataloguers to record for a pistekirja (braille book)."""
    from lxml import etree

    from reconciliator.loaders.marcxml import build_target_record_from_marc

    ns = "http://www.loc.gov/MARC21/slim"
    record = etree.fromstring(
        f"""<record xmlns="{ns}">
            <leader>00000cam a2200000 i 4500</leader>
            <controlfield tag="001">B1</controlfield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Pistekirja</subfield></datafield>
            <datafield tag="341" ind1="0" ind2=" ">
                <subfield code="a">tactile</subfield>
                <subfield code="e">braille</subfield>
                <subfield code="2">sapdv</subfield>
            </datafield>
        </record>""".encode()
    )
    assert build_target_record_from_marc(record, source="m").work_type == "tactile"


# --- rules derived from the Finnish RDA application guidelines -------------
#
# Sources:
#   857  https://marc21.kansalliskirjasto.fi/bib/841-88X.htm
#        "ELEKTRONISEN ARKISTON SIJAINTI JA KÄYTTÖ ... aineisto tai sen osa on
#         saatavissa verkkoarkistossa tai digitaalisessa arkistossa"
#   501/533  .../RDA-sovellusohje/50X-535 Huomautuskentät/
#   341/532  .../Lisäohjeita/Saavutettavuus/


def _marc_with(extra: str, leader: str = "00000cam a2200000 i 4500"):  # noqa: ANN202
    from lxml import etree

    ns = "http://www.loc.gov/MARC21/slim"
    return etree.fromstring(
        f"""<record xmlns="{ns}">
            <leader>{leader}</leader>
            <controlfield tag="001">X1</controlfield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Teos</subfield></datafield>
            {extra}
        </record>""".encode()
    )


def _work_type_of(extra: str, leader: str = "00000cam a2200000 i 4500") -> str:
    from reconciliator.loaders.marcxml import build_target_record_from_marc

    return build_target_record_from_marc(_marc_with(extra, leader), source="m").work_type


def test_857_marks_a_resource_held_in_a_web_archive():
    """Second indicator 0 or blank: the linked copy is the described resource."""
    for ind2 in ("0", " "):
        extra = (
            f'<datafield tag="857" ind1="4" ind2="{ind2}">'
            '<subfield code="u">https://verkkoarkisto.kansalliskirjasto.fi/va/1</subfield>'
            "</datafield>"
        )
        assert _work_type_of(extra) == "web_harvest"


def test_857_second_indicator_1_marks_a_digital_surrogate():
    """ "Aineiston versio": the record describes a physical item and the archived
    copy is its electronic version - a surrogate, not a born-digital resource."""
    extra = (
        '<datafield tag="857" ind1="4" ind2="1">'
        '<subfield code="u">https://digi.kansalliskirjasto.fi/x</subfield></datafield>'
    )
    assert _work_type_of(extra) == "preservation_surrogate"


def test_533_reproduction_note_marks_a_surrogate():
    extra = (
        '<datafield tag="533" ind1=" " ind2=" ">'
        '<subfield code="a">Digitoitu näköispainos</subfield></datafield>'
    )
    assert _work_type_of(extra) == "preservation_surrogate"


def test_an_ordinary_856_online_resource_is_not_a_web_archive():
    """856 is "elektronisen aineiston sijainti" - every e-book has one.

    Keying the web-archive funnel on an online resource would route the entire
    electronic collection to it. Only 857 means an archive.
    """
    extra = (
        '<datafield tag="856" ind1="4" ind2="0">'
        '<subfield code="u">https://example.org/ebook</subfield></datafield>'
    )
    assert _work_type_of(extra) == "text"


def test_a_digitised_map_stays_a_map():
    """Surrogate-ness is checked after the leader, so it cannot mask the material.

    Whether "preservation_surrogate" should be a work type at all is an open
    question - being a surrogate is a property of the manifestation.
    """
    extra = (
        '<datafield tag="533" ind1=" " ind2=" "><subfield code="a">Digitoitu</subfield></datafield>'
    )
    assert _work_type_of(extra, leader="00000cem a2200000 i 4500") == "cartography"


def test_501_bound_with_becomes_a_sammelband_key():
    """501 is a local field carrying $5 with the holding library's ISIL code.

    Read anyway because Melinda is not the only source a corpus comes from; a
    holdings-level fallback is still needed for sammelbands described only at
    item level.
    """
    from reconciliator.loaders.marcxml import build_target_record_from_marc

    extra = (
        '<datafield tag="501" ind1=" " ind2=" ">'
        '<subfield code="a">Yhteissidos: Toinen teos</subfield>'
        '<subfield code="5">FI-Hul</subfield></datafield>'
    )
    record = build_target_record_from_marc(_marc_with(extra), source="m")
    assert record.get_keys("sammelband") == ["Yhteissidos: Toinen teos"]
    assert record.get_keys("sammelband_holder") == ["FI-Hul"]


def test_a_braille_score_is_tactile_not_music():
    """Project decision: tactile wins over the material it carries.

    `336 $b tcm` is literally "tactile notated music", so the record asserts
    both. The tactile check runs before the leader, so a braille score - whose
    Leader/06 may well be "c" - classifies as tactile.
    """
    extra = (
        '<datafield tag="336" ind1=" " ind2=" "><subfield code="b">tcm</subfield>'
        '<subfield code="2">rdacontent</subfield></datafield>'
    )
    assert _work_type_of(extra, leader="00000ccm a2200000 i 4500") == "tactile"


# --- SLM genre/form terms (655 $2 slm/fin) --------------------------------
#
# Every term confirmed to exist in SLM through the Finto API, with its URI, not
# inferred from a plausible-looking Finnish word.
#
#   televisio-ohjelmat  s1209   radio-ohjelmat  s1416
#   juridiset teokset   s1507   antologiat      s289 (broader: kokoomateokset s963)
#
# Source: https://finto.fi/slm/  |  https://api.finto.fi/rest/v1/slm/


def _work_type_for_genre(term: str) -> str:
    from reconciliator.loaders.marcxml import build_target_record_from_marc

    extra = (
        '<datafield tag="655" ind1=" " ind2="7">'
        f'<subfield code="a">{term}</subfield>'
        '<subfield code="2">slm/fin</subfield></datafield>'
    )
    return build_target_record_from_marc(_marc_with(extra), source="m").work_type


@pytest.mark.parametrize(
    "term",
    [
        "televisio-ohjelmat",
        "televisiosarjat",
        "televisionäytelmät",
        "radio-ohjelmat",
        "radiosarjat",
        "kuunnelmat",
        "dokumenttiohjelmat",
    ],
)
def test_slm_broadcast_terms_classify_as_broadcast(term: str):
    assert _work_type_for_genre(term) == "broadcast_program"


@pytest.mark.parametrize(
    "term", ["lait", "asetukset", "säädöskokoelmat", "direktiivit", "valtiosopimukset"]
)
def test_slm_juridical_terms_classify_as_legal(term: str):
    """The whole "juridiset teokset" (s1507) branch."""
    assert _work_type_for_genre(term) == "legal"


@pytest.mark.parametrize("term", ["antologiat", "kokoomateokset"])
def test_slm_anthology_terms_classify_as_anthology(term: str):
    assert _work_type_for_genre(term) == "anthology"


@pytest.mark.parametrize(
    ("term", "not_broadcast"),
    [
        ("dokumenttielokuvat", "documentary films, not programmes"),
        ("elämäkerrat", "biographies"),
        ("romaanit", "novels"),
    ],
)
def test_similar_slm_terms_are_not_swept_into_broadcast(term: str, not_broadcast: str):
    """Why the table is exact-match rather than substring.

    "dokumenttiohjelmat" and "dokumenttielokuvat" share a stem and are different
    materials; substring matching across SLM would conflate them.
    """
    assert _work_type_for_genre(term) != "broadcast_program", not_broadcast


def test_a_cartographic_dataset_is_spatial_data_not_a_map():
    """`crd` is the only real signal for spatial data: SLM has no term for it.

    The geodata spec must also sit ahead of cartography, since a cartographic
    dataset is work_type "cartography" too and would otherwise be claimed first.
    """
    from reconciliator.loaders.marcxml import build_target_record_from_marc
    from reconciliator.utils import extract_identifiers_from_target

    def funnel_for(code: str) -> str:
        extra = (
            '<datafield tag="336" ind1=" " ind2=" ">'
            f'<subfield code="b">{code}</subfield>'
            '<subfield code="2">rdacontent</subfield></datafield>'
        )
        record = build_target_record_from_marc(_marc_with(extra), source="m")
        identifiers = extract_identifiers_from_target(record)
        fallback = None
        for spec in BUILTIN_SPECS:
            if spec.default:
                fallback = spec
                continue
            if spec.claims(record, identifiers):
                return spec.id
        return fallback.id

    assert funnel_for("crd") == "funnel_nn_geodata"
    assert funnel_for("cri") == "funnel_l_cartography"


# --- domain keys with readers but no writer -------------------------------
#
# `translators_signature` was declared on MatchRecord, consulted by the
# clusterer's pairwise scoring and by funnel HH's dispatch, and written by
# nothing - so both consumers read a permanently absent value (fixed in 14.18).
# It was not alone. These key types are read by the clusterer, the matcher's
# identifier derivation, the XML enricher or a funnel's `has_key` predicate, and
# no loader emits them.
#
# Each needs a MARC rule grounded in the Finnish RDA guidelines, which is the
# same blocked work as the medieval-fragment gap. They are listed rather than
# deleted because each becomes live the moment its rule lands, and rather than
# guessed at because inventing bibliographic rules is the failure mode this file
# exists to prevent.
KEYS_READ_BUT_NEVER_WRITTEN = {
    "board_game",
    "broadcast",
    "ephemera_corp",
    "exhibition_event",
    "fragment_codex",
    "numismatic_issue",
    "oral_history",
    "podcast",
    "statute",
    "swhid",
    "web_seed",
}


def _key_types_written() -> set[str]:
    """First argument of every `_add(...)` in either loader's key derivation."""
    written: set[str] = set()
    for module, func in (
        ("src/reconciliator/loaders/marcxml.py", "derive_domain_keys"),
        ("src/reconciliator/loaders/bibframe/decomposition.py", "derive_bibframe_keys"),
    ):
        tree = ast.parse(pathlib.Path(module).read_text())
        fn = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func),
            None,
        )
        if fn is None:
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_add"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                written.add(node.args[0].value)
    return written


def test_the_keys_with_no_writer_are_exactly_as_documented():
    """Fails in both directions.

    Growing means a consumer started reading a key nothing produces - the defect
    itself. Shrinking means a loader rule landed and this list is stale.
    """
    written = _key_types_written()
    assert written, "key-writing extraction found nothing"
    still_unwritten = {k for k in KEYS_READ_BUT_NEVER_WRITTEN if k not in written}
    assert still_unwritten == KEYS_READ_BUT_NEVER_WRITTEN


def test_the_keys_funnel_dispatch_depends_on_are_written():
    """A `has_key` predicate on an unwritten key is a dead dispatch alternative.

    Weaker than the reachability test above, which allows a funnel to be reached
    through any one alternative: this checks that a funnel reachable *only*
    through `has_key` has that key actually produced.
    """
    written = _key_types_written()
    broken = []
    for spec in BUILTIN_SPECS:
        if spec.default or not spec.when:
            continue
        # alternatives that rest solely on a key, with no work_type to fall back on
        for when in spec.when:
            if when.has_key and not when.work_type and not when.content_type:
                missing = [k for k in when.has_key if k not in written]
                if missing and len(spec.when) == 1:
                    broken.append((spec.id, missing))
    assert broken == [], f"funnels reachable only through an unwritten key: {broken}"
