"""Stage 3 of plans/aggregates.md: profile-driven MARC constituent extraction.

No entities are written yet; that is stage 4. What is settled here is *reading*:
which tags a profile claims, which indicator values qualify, which subfield
carries what, how one constituent described twice is reconciled, and how a
tracklist gets its order.

The reading `extract_marc_analytics` has always performed is now the
`marc_legacy` profile, so the blocking keys a source has always derived are
byte-identical while `marc_enhanced` offers the stricter reading.
"""

from __future__ import annotations

from lxml import etree

from reconciliator.constituents import get_constituent_profile
from reconciliator.constituents.record import Constituent
from reconciliator.constituents.record import finalise as finalise_constituents
from reconciliator.constituents.spec import ConstituentField, ConstituentProfile
from reconciliator.loaders.marcxml import (
    extract_marc_analytics,
    extract_marc_constituents,
)

ENHANCED = get_constituent_profile("marc_enhanced")
LEGACY = get_constituent_profile("marc_legacy")


def _record(*datafields: str, leader: str = "00000cjm a2200000 i 4500") -> etree._Element:
    return etree.fromstring(
        f"""<record xmlns="http://www.loc.gov/MARC21/slim">
            <leader>{leader}</leader>
            <controlfield tag="001">R1</controlfield>
            {"".join(datafields)}
        </record>""".encode()
    )


#: An enhanced contents note: subfields repeat in groups, one per track.
ALBUM_505 = (
    '<datafield tag="505" ind1="0" ind2="0">'
    '<subfield code="t">Cantus arcticus</subfield>'
    '<subfield code="r">Rautavaara</subfield>'
    '<subfield code="g">(18:20)</subfield>'
    '<subfield code="t">Angel of light</subfield>'
    '<subfield code="g">(31:05)</subfield>'
    '<subfield code="t">Isle of bliss</subfield>'
    "</datafield>"
)

#: A name-title analytic added entry: $a precedes $t.
ANALYTIC_700 = (
    '<datafield tag="700" ind1="1" ind2="2">'
    '<subfield code="a">Rautavaara, Einojuhani</subfield>'
    '<subfield code="t">Cantus arcticus</subfield>'
    "</datafield>"
)

#: A plain added entry: a performer, not a constituent.
PERFORMER_700 = (
    '<datafield tag="700" ind1="1" ind2=" ">'
    '<subfield code="a">Segerstam, Leif</subfield>'
    '<subfield code="e">kapellimestari</subfield>'
    "</datafield>"
)

#: A basic contents note: one $a blob, no $t to read.
BASIC_505 = (
    '<datafield tag="505" ind1="0" ind2=" ">'
    '<subfield code="a">Kuun maisema -- Agnes -- Hurskas kurjuus</subfield>'
    "</datafield>"
)


# --- the tracklist --------------------------------------------------------


def test_an_enhanced_contents_note_yields_ordered_constituents():
    got = extract_marc_constituents(_record(ALBUM_505), ENHANCED)
    assert [(c.ordinal, c.title) for c in got] == [
        (1, "Cantus arcticus"),
        (2, "Angel of light"),
        (3, "Isle of bliss"),
    ]


def test_each_constituent_keeps_its_own_subfields():
    """Subfields repeat in groups, so a track's duration is the `$g` that follows
    *its* `$t`. Reading each code with `findall` instead gave every track on the
    album the first track's duration and the first track's composer."""
    got = extract_marc_constituents(_record(ALBUM_505), ENHANCED)
    # Track 3 states no duration and track 2 states no creator; neither inherits
    # from the group before it.
    assert [c.duration for c in got] == ["18:20", "31:05", None]
    assert [c.creator for c in got] == ["Rautavaara", None, None]


def test_a_duration_loses_its_card_catalogue_parentheses():
    got = extract_marc_constituents(_record(ALBUM_505), ENHANCED)
    assert got[0].duration == "18:20"


def test_a_creator_before_the_title_is_still_found():
    """A 700 puts `$a` ahead of `$t`, so the creator is outside the group it
    belongs to; subfields before the first title are the fallback."""
    got = extract_marc_constituents(_record(ANALYTIC_700), ENHANCED)
    assert [(c.title, c.creator) for c in got] == [("Cantus arcticus", "Rautavaara, Einojuhani")]


# --- indicator filters ----------------------------------------------------


def test_a_basic_contents_note_yields_nothing():
    """Its single `$a` holds "A -- B -- C", which cannot be split into titles
    reliably. No constituents is right; three wrong works is not."""
    assert extract_marc_constituents(_record(BASIC_505), ENHANCED) == []


def test_a_plain_added_entry_is_not_a_constituent():
    """`ind2=2` marks an analytic entry. A conductor in a 700 with a blank ind2
    is a contributor to the album, not something the album contains."""
    got = extract_marc_constituents(_record(PERFORMER_700), ENHANCED)
    assert got == []


def test_a_blank_indicator_matches_whether_written_as_space_or_omitted():
    """Exporters disagree about which, and a profile should not have to."""
    profile = ConstituentProfile(
        name="t", fields=[ConstituentField(tag="505", title="t", indicators={"ind2": " "})]
    )
    with_space = _record(
        '<datafield tag="505" ind1="0" ind2=" "><subfield code="t">A</subfield></datafield>'
    )
    omitted = _record('<datafield tag="505" ind1="0"><subfield code="t">A</subfield></datafield>')
    assert len(extract_marc_constituents(with_space, profile)) == 1
    assert len(extract_marc_constituents(omitted, profile)) == 1


def test_an_entry_with_no_indicators_claims_every_instance():
    """Which is what the old unfiltered reading did, and why `marc_legacy`
    reproduces it without naming any."""
    got = extract_marc_constituents(_record(BASIC_505, ALBUM_505), LEGACY)
    assert [c.title for c in got] == ["Cantus arcticus", "Angel of light", "Isle of bliss"]


# --- one constituent described twice --------------------------------------


def test_a_track_in_both_the_note_and_an_added_entry_is_one_constituent():
    got = extract_marc_constituents(_record(ALBUM_505, ANALYTIC_700), ENHANCED)
    assert [c.title for c in got] == [
        "Cantus arcticus",
        "Angel of light",
        "Isle of bliss",
    ]


def test_the_two_descriptions_are_merged_attribute_by_attribute():
    """The note gives the duration, the added entry gives the composer. Two
    entries for one track are two descriptions of it, not two candidates."""
    got = extract_marc_constituents(_record(ALBUM_505, ANALYTIC_700), ENHANCED)
    cantus = got[0]
    assert cantus.duration == "18:20"  # from the 505
    assert cantus.creator == "Rautavaara, Einojuhani"  # from the 700
    assert cantus.ordinal == 1


def test_the_authority_controlled_name_wins_over_the_transcribed_one():
    """ "Rautavaara, Einojuhani" and "Rautavaara" are the same person, and only
    the first will match an authority record. The 505 takes precedence for every
    other attribute; `creator_controlled` is the one stated exception."""
    got = extract_marc_constituents(_record(ALBUM_505, ANALYTIC_700), ENHANCED)
    assert got[0].creator == "Rautavaara, Einojuhani"
    assert got[0].creator_controlled is True
    assert got[0].source_field == "505"  # precedence still the note's


def test_a_deduplicated_duplicate_does_not_consume_a_track_number():
    got = extract_marc_constituents(_record(ALBUM_505, ANALYTIC_700), ENHANCED)
    assert [c.ordinal for c in got] == [1, 2, 3]


def test_dedup_none_keeps_both_descriptions():
    """A source that legitimately repeats a title says so, and the legacy
    profile does, because that is what the old reading produced."""
    got = extract_marc_constituents(_record(ALBUM_505, ANALYTIC_700), LEGACY)
    assert [c.title for c in got].count("Cantus arcticus") == 2


def test_dedup_on_title_and_creator_keeps_different_creators_apart():
    profile = ConstituentProfile(
        name="t",
        dedup="title_and_creator",
        fields=[ConstituentField(tag="700", title="t", creator="a", indicators={"ind2": "2"})],
    )
    record = _record(
        ANALYTIC_700,
        '<datafield tag="700" ind1="1" ind2="2">'
        '<subfield code="a">Sibelius, Jean</subfield>'
        '<subfield code="t">Cantus arcticus</subfield>'
        "</datafield>",
    )
    got = extract_marc_constituents(record, profile)
    assert len(got) == 2


# --- shape and ordinals ---------------------------------------------------


def test_a_host_item_entry_yields_a_component_not_an_embodied_constituent():
    """One source can record embodied constituents in its contents note and be
    itself a component through its host-item entry."""
    record = _record(
        '<datafield tag="773" ind1="0" ind2=" ">'
        '<subfield code="t">Virittäjä</subfield>'
        '<subfield code="g">2020, s. 12-34</subfield>'
        '<subfield code="q">12</subfield>'
        '<subfield code="w">(FI-MELINDA)000900111</subfield>'
        "</datafield>",
        leader="00000cab a2200000 i 4500",
    )
    got = extract_marc_constituents(record, ENHANCED)
    assert len(got) == 1
    assert got[0].shape == "component"
    assert got[0].component_type == "article"
    # $q, "enumeration and first page", which is what the existing citation key
    # already reads. $g is "related parts" - here a year *and* a page range - so
    # reading it as pagination would put a year in a column named for pages.
    assert got[0].pagination == "12"
    # $w is the host's record control number - the only subfield in a host-item
    # entry that identifies a row rather than describing one.
    assert got[0].container_ref == "(FI-MELINDA)000900111"
    assert got[0].ordinal is None  # a component is not track n of anything


def test_a_stated_ordinal_is_read_rather_than_counted():
    profile = ConstituentProfile(
        name="t",
        fields=[ConstituentField(tag="505", title="t", ordinal="subfield:v")],
    )
    record = _record(
        '<datafield tag="505" ind1="0" ind2="0">'
        '<subfield code="t">Second</subfield><subfield code="v">2</subfield>'
        '<subfield code="t">Fourth</subfield><subfield code="v">4</subfield>'
        "</datafield>"
    )
    got = extract_marc_constituents(record, profile)
    assert [(c.ordinal, c.title) for c in got] == [(2, "Second"), (4, "Fourth")]


def test_an_unnumbered_profile_leaves_constituents_unordered():
    got = extract_marc_constituents(_record(ALBUM_505), LEGACY)
    assert all(c.ordinal is None for c in got)


def test_a_disc_number_is_read_as_an_integer():
    profile = ConstituentProfile(
        name="t", fields=[ConstituentField(tag="505", title="t", disc="n")]
    )
    record = _record(
        '<datafield tag="505" ind1="0" ind2="0">'
        '<subfield code="t">A</subfield><subfield code="n">disc 2</subfield>'
        "</datafield>"
    )
    assert extract_marc_constituents(record, profile)[0].disc == 2


# --- the cap and empty profiles -------------------------------------------


def test_the_cap_truncates_rather_than_writing_a_box_set(caplog):
    """A real contents note can list 500 tracks, and a malformed one anything at
    all. One record should not be able to write 500 works."""
    profile = ConstituentProfile(
        name="t", max_per_record=5, fields=[ConstituentField(tag="505", title="t")]
    )
    subfields = "".join(f'<subfield code="t">Track {n}</subfield>' for n in range(1, 21))
    record = _record(f'<datafield tag="505" ind1="0" ind2="0">{subfields}</datafield>')
    got = extract_marc_constituents(record, profile, record_id="BOXSET-1")
    assert len(got) == 5
    assert "BOXSET-1" in caplog.text


def test_a_profile_that_selects_nothing_yields_nothing():
    """`none` is a real answer for the many sources that have no aggregates."""
    assert extract_marc_constituents(_record(ALBUM_505), get_constituent_profile("none")) == []


def test_an_rdf_field_entry_is_ignored_by_the_marc_extractor():
    """A profile for another format is not this extractor's business, and must
    not raise on the way past."""
    profile = ConstituentProfile(
        name="t", fields=[ConstituentField(predicate="bf:hasPart", title="bf:title")]
    )
    assert extract_marc_constituents(_record(ALBUM_505), profile) == []


# --- the legacy path ------------------------------------------------------


def test_extract_marc_analytics_is_unchanged():
    """It is now the `marc_legacy` profile, so the `analytic` and
    `analytic_title` keys a source has always derived stay byte-identical: order,
    duplicates and all."""
    record = _record(ALBUM_505, ANALYTIC_700, PERFORMER_700)
    assert extract_marc_analytics(record) == [
        ("Cantus arcticus", "Rautavaara, Einojuhani"),
        ("Cantus arcticus", None),
        ("Angel of light", None),
        ("Isle of bliss", None),
    ]


def test_the_legacy_profile_reads_added_entries_before_the_contents_note():
    """Field order in a profile is precedence *and* output order, and the old
    function appended 700s, then 710s, then 505s."""
    assert [f.tag for f in LEGACY.fields] == ["700", "710", "505"]


# --- finalise, independent of format --------------------------------------


def test_finalise_numbers_only_what_the_profile_numbers():
    profile = ConstituentProfile(
        name="t",
        fields=[
            ConstituentField(tag="505", title="t", ordinal="position"),
            ConstituentField(tag="700", title="t", ordinal="none"),
        ],
    )
    got = finalise_constituents(
        [
            Constituent(title="A", source_field="505", precedence=0),
            Constituent(title="B", source_field="700", precedence=1),
            Constituent(title="C", source_field="505", precedence=0),
        ],
        profile,
    )
    assert [(c.title, c.ordinal) for c in got] == [("A", 1), ("B", None), ("C", 2)]


def test_730_uniform_title_and_740_analytical_title_extraction():
    """Music anthologies and songbooks catalog constituent songs in 730 and 740."""
    record = _record(
        '<datafield tag="730" ind1="0" ind2=" ">'
        '<subfield code="a">Laula, tyttö!, op30, nro 2 /</subfield>'
        '<subfield code="g">Merikanto, Oskar</subfield>'
        "</datafield>",
        '<datafield tag="730" ind1="0" ind2=" ">'
        '<subfield code="a">Sinfonia nro 9 /</subfield>'
        '<subfield code="g">Beethoven, Ludwig van</subfield>'
        "</datafield>",
        '<datafield tag="740" ind1="0" ind2="2">'
        '<subfield code="a">Kalliolle kukkulalle /</subfield>'
        "</datafield>",
    )
    got = extract_marc_constituents(record, ENHANCED)
    assert len(got) == 3
    assert got[0].title == "Laula, tyttö!, op30, nro 2"
    assert got[0].creator == "Merikanto, Oskar"
    assert got[0].ordinal == 1
    assert got[0].contributes_to_container is False

    assert got[1].title == "Sinfonia nro 9"
    assert got[1].creator == "Beethoven, Ludwig van"
    assert got[1].ordinal == 2

    assert got[2].title == "Kalliolle kukkulalle"
    assert got[2].creator is None
    assert got[2].ordinal == 3
