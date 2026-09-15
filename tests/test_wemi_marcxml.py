"""Unit tests for MARCXML WEMI decomposition and MatchRecord generation (Milestone 2)."""

from lxml import etree

from reconciliator.loaders.marcxml import (
    build_target_record_from_marc,
    decompose_marc_wemi,
    determine_carrier_type,
    determine_content_type,
    determine_work_type,
    parse_marc_items,
)


def _build_marc_record(
    leader: str = "00000nam a2200000 i 4500",
    controlfields: dict[str, str] | None = None,
    datafields: list[tuple[str, str, str, list[tuple[str, str]]]] | None = None,
) -> etree._Element:
    """Helper to create an etree MARCXML <record>."""
    rec = etree.Element("{http://www.loc.gov/MARC21/slim}record")
    ldr = etree.SubElement(rec, "{http://www.loc.gov/MARC21/slim}leader")
    ldr.text = leader

    cf = controlfields or {}
    for tag, val in cf.items():
        c = etree.SubElement(rec, "{http://www.loc.gov/MARC21/slim}controlfield", tag=tag)
        c.text = val

    df = datafields or []
    for tag, ind1, ind2, subfields in df:
        d = etree.SubElement(
            rec, "{http://www.loc.gov/MARC21/slim}datafield", tag=tag, ind1=ind1, ind2=ind2
        )
        for code, text in subfields:
            s = etree.SubElement(d, "{http://www.loc.gov/MARC21/slim}subfield", code=code)
            s.text = text

    return rec


def test_determine_work_type():
    # Music (Leader/06 = c or d)
    rec_score = _build_marc_record(leader="00000cdm a2200000 i 4500")
    assert determine_work_type(rec_score) == "music"

    # Recorded sound (Leader/06 = j)
    rec_audio = _build_marc_record(leader="00000cjm a2200000 i 4500")
    assert determine_work_type(rec_audio) == "music"

    # Cartographic (Leader/06 = e)
    rec_map = _build_marc_record(leader="00000cem a2200000 i 4500")
    assert determine_work_type(rec_map) == "cartography"

    # Moving Image (Leader/06 = g)
    rec_film = _build_marc_record(leader="00000cgm a2200000 i 4500")
    assert determine_work_type(rec_film) == "moving_image"

    # Serial (Leader/07 = s)
    rec_serial = _build_marc_record(leader="00000cas a2200000 i 4500")
    assert determine_work_type(rec_serial) == "serial"

    # Monograph (Leader/06 = a, Leader/07 = m)
    rec_mono = _build_marc_record(leader="00000cam a2200000 i 4500")
    assert determine_work_type(rec_mono) == "text"


def test_determine_content_and_carrier_type():
    rec = _build_marc_record(
        leader="00000cam a2200000 i 4500",
        datafields=[
            ("336", "#", "#", [("a", "teksti"), ("b", "txt"), ("2", "rdacontent")]),
            ("338", "#", "#", [("a", "nide"), ("b", "nc"), ("2", "rdacarrier")]),
        ],
    )
    assert determine_content_type(rec) == "text"
    assert determine_carrier_type(rec) == "volume"


def test_decompose_monograph_wemi():
    rec = _build_marc_record(
        leader="00000cam a2200000 i 4500",
        controlfields={
            "001": "FI-MELINDA-123456",
            "008": "950101s1945    fi |||||||||||||||||fin|d",
        },
        datafields=[
            ("020", "#", "#", [("a", "978-951-0-12345-6")]),
            (
                "100",
                "1",
                "#",
                [("a", "Waltari, Mika,"), ("d", "1908-1979,"), ("e", "kirjoittaja."), ("4", "aut")],
            ),
            ("240", "1", "0", [("a", "Sinuhe egyptiläinen")]),
            (
                "245",
                "1",
                "0",
                [
                    ("a", "Sinuhe egyptiläinen :"),
                    ("b", "viisitoista kirjaa lääkäri Sinuhen elämästä"),
                ],
            ),
            ("264", "#", "1", [("a", "Helsinki :"), ("b", "WSOY,"), ("c", "1945")]),
            ("300", "#", "#", [("a", "950 s. ;"), ("c", "21 cm")]),
            ("490", "1", "#", [("a", "WSOY:n kirjasto"), ("v", "12")]),
            ("830", "#", "0", [("a", "WSOY:n kirjasto ;"), ("v", "12")]),
            (
                "650",
                "#",
                "7",
                [("a", "lääkärit"), ("2", "yso/fin"), ("0", "http://www.yso.fi/onto/yso/p1234")],
            ),
        ],
    )

    (
        work,
        expr,
        manif,
        items,
        agents,
        relationships,
        identifiers,
        assoc,
        mops,
        contributions,
    ) = decompose_marc_wemi(rec, source="melinda", include_items=False)

    assert work.title == "Sinuhe egyptiläinen"
    assert work.work_type == "text"
    assert work.original_language == "fin"
    assert "Sinuhe egyptiläinen" in work.serialized_text

    assert expr.title == "Sinuhe egyptiläinen :"
    assert expr.language == "fin"
    assert expr.content_type == "text"

    assert manif.title == "Sinuhe egyptiläinen :"
    assert manif.publisher == "WSOY,"
    assert manif.publication_year == 1945
    assert manif.carrier_type == "volume"
    assert manif.series_title == "WSOY:n kirjasto"
    assert manif.series_numbering == "12"

    assert len(assoc) == 1

    # Identifiers
    isbn_idents = [i for i in identifiers if i.scheme == "isbn"]
    assert len(isbn_idents) == 1
    assert isbn_idents[0].value == "978-951-0-12345-6"
    assert isbn_idents[0].value_normalized == "9789510123456"

    # Contributions
    assert len(contributions) == 1
    assert contributions[0].role == "aut"


def test_decompose_music_wemi():
    rec = _build_marc_record(
        leader="00000cdm a2200000 i 4500",
        controlfields={"001": "MUSIC-999"},
        datafields=[
            ("024", "2", "#", [("a", "979-0-004-18200-5")]),
            ("028", "2", "0", [("a", "EB 8548"), ("b", "Breitkopf & Härtel")]),
            ("100", "1", "#", [("a", "Sibelius, Jean,"), ("d", "1865-1957,"), ("4", "cmp")]),
            ("240", "1", "0", [("a", "Finlandia,"), ("n", "op. 26,"), ("m", "orkesteri")]),
            ("245", "1", "0", [("a", "Finlandia :"), ("b", "sävelruno orkesterille : op. 26")]),
            ("382", "0", "1", [("a", "orkesteri"), ("2", "lcmpt")]),
            ("384", "0", "#", [("a", "As-duuri")]),
        ],
    )

    (
        work,
        expr,
        manif,
        items,
        agents,
        relationships,
        identifiers,
        assoc,
        mops,
        contributions,
    ) = decompose_marc_wemi(rec, source="fennica")

    assert work.work_type == "music"
    assert work.title == "Finlandia,"
    assert len(mops) == 1
    assert mops[0].label == "orkesteri"

    ismn_idents = [i for i in identifiers if i.scheme == "ismn"]
    assert len(ismn_idents) == 1
    assert ismn_idents[0].value == "979-0-004-18200-5"


def test_decompose_with_items():
    rec = _build_marc_record(
        leader="00000cam a2200000 i 4500",
        controlfields={"001": "101"},
        datafields=[
            ("245", "0", "0", [("a", "Harvinainen kirja")]),
            (
                "852",
                "#",
                "#",
                [("a", "HYK"), ("b", "KANS"), ("c", "Signum A.12"), ("p", "3123456789")],
            ),
            (
                "852",
                "#",
                "#",
                [("a", "TYK"), ("b", "PAA"), ("c", "Vapaakappale B.44"), ("p", "3987654321")],
            ),
        ],
    )

    items = parse_marc_items(rec, manifestation_id=101, source="fennica")
    assert len(items) == 2
    assert items[0].holding_organization == "HYK"
    assert items[0].shelf_mark == "Signum A.12"
    assert items[0].barcode == "3123456789"

    assert items[1].holding_organization == "TYK"
    assert items[1].shelf_mark == "Vapaakappale B.44"
    assert items[1].barcode == "3987654321"

    _, _, _, decomp_items, *rest = decompose_marc_wemi(rec, source="fennica", include_items=True)
    assert len(decomp_items) == 2


def test_build_target_record_from_marc():
    rec = _build_marc_record(
        leader="00000cam a2200000 i 4500",
        controlfields={"001": "TARGET-100", "008": "950101s1945    fi |||||||||||||||||fin|d"},
        datafields=[
            ("020", "#", "#", [("a", "978-951-0-12345-6")]),
            ("100", "1", "#", [("a", "Waltari, Mika,"), ("4", "aut")]),
            ("245", "1", "0", [("a", "Sinuhe egyptiläinen")]),
        ],
    )

    target = build_target_record_from_marc(rec, source="target")
    assert target.record_id == "TARGET-100"
    assert target.format == "marcxml"
    assert target.work_type == "text"
    assert target.get_first_key("standard") == "978-951-0-12345-6"
    assert target.author_key is not None
    assert target.title_stem is not None
    assert target.work_blocking_key is not None
    assert len(target.raw_payload) > 0
    assert "Sinuhe egyptiläinen" in target.serialized_work


def test_decompose_marc_wemi_missing_id(caplog):
    rec = _build_marc_record(
        leader="00000cam a2200000 i 4500",
        controlfields={},
        datafields=[
            ("245", "1", "0", [("a", "Nimetön kirja")]),
        ],
    )
    with caplog.at_level("WARNING"):
        work, expr, manif, *_ = decompose_marc_wemi(rec, source="fennica")

    # Derived from the record's own fields, so re-ingesting is idempotent.
    assert work.source_id.startswith("sha1:")
    assert expr.source_id == work.source_id
    assert manif.source_id == work.source_id
    assert any("carries no control number" in record.message for record in caplog.records)

    again, *_ = decompose_marc_wemi(
        _build_marc_record(
            leader="00000cam a2200000 i 4500",
            controlfields={},
            datafields=[("245", "1", "0", [("a", "Nimetön kirja")])],
        ),
        source="fennica",
    )
    assert again.source_id == work.source_id, "the same record must keep the same id"



def test_derive_content_id_is_stable_and_content_sensitive() -> None:
    """Records without a control number get an id derived from their own fields.

    A uuid4 fallback made the same record a different work on every ingest, so no
    URI-derived key could survive a rebuild.
    """
    from lxml import etree

    from reconciliator.loaders.marcxml import derive_content_id

    MARC = "http://www.loc.gov/MARC21/slim"

    def record(title: str, author: str = "Waltari, Mika,") -> etree._Element:
        return etree.fromstring(
            f"""<record xmlns="{MARC}">
              <datafield tag="100" ind1="1" ind2=" "><subfield code="a">{author}</subfield></datafield>
              <datafield tag="245" ind1="1" ind2="0"><subfield code="a">{title}</subfield></datafield>
            </record>""".encode("utf-8")
        )

    first = derive_content_id(record("Sinuhe egyptiläinen"))
    again = derive_content_id(record("Sinuhe egyptiläinen"))
    assert first == again, "same content must yield the same id"

    assert first != derive_content_id(record("Mikael Karvajalka")), "title must matter"
    assert first != derive_content_id(record("Sinuhe egyptiläinen", "Linna, Väinö,")), (
        "creator must matter"
    )

    # Serialization differences must not change the identifier.
    unprefixed = etree.fromstring(
        """<record>
          <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Waltari, Mika,</subfield></datafield>
          <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Sinuhe egyptiläinen</subfield></datafield>
        </record>""".encode("utf-8")
    )
    assert derive_content_id(unprefixed) == first, "namespace prefix must not affect the id"

    assert first.startswith("sha1:")
    assert derive_content_id(record("X"), prefix="auth").startswith("auth:")


def test_parse_extracts_publisher_plate_strn_issnl_and_control_number():
    """These five tier-1 schemes reached neither the identifiers table nor the matcher.

    028 publisher/plate numbers are the primary tier-1 schemes for recorded sound
    and notated music, 088 for standards, and 022 $l identifies a serial across
    medium changes. All were parsed nowhere.
    """
    from lxml import etree

    from reconciliator.loaders.marcxml import parse_marc_record_elem

    ns = "http://www.loc.gov/MARC21/slim"
    rec = etree.fromstring(
        f"""<record xmlns="{ns}">
            <controlfield tag="001">A1</controlfield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Finlandia</subfield></datafield>
            <datafield tag="022" ind1=" " ind2=" "><subfield code="a">0042-6806</subfield><subfield code="l">0042-6807</subfield></datafield>
            <datafield tag="028" ind1="0" ind2="2"><subfield code="a">ODE 1234-2</subfield></datafield>
            <datafield tag="028" ind1="2" ind2="0"><subfield code="a">B&amp;H 1890</subfield></datafield>
            <datafield tag="088" ind1=" " ind2=" "><subfield code="a">STRN-9</subfield></datafield>
        </record>""".encode()
    )
    ids = parse_marc_record_elem(rec, source="melinda")["identifiers"]
    assert ids["publisher_number"] == ["ODE 1234-2"]
    assert ids["plate_number"] == ["B&H 1890"]
    assert ids["strn"] == ["STRN-9"]
    assert ids["issn_l"] == ["0042-6807"]
    assert ids["control_number"] == ["A1"]


def test_match_record_carries_identifiers_and_wemi_context():
    """The record the matcher consumes needs identifiers and the tiers above it."""
    from lxml import etree

    from reconciliator.loaders.marcxml import build_target_record_from_marc

    ns = "http://www.loc.gov/MARC21/slim"
    rec = etree.fromstring(
        f"""<record xmlns="{ns}">
            <controlfield tag="001">A1</controlfield>
            <datafield tag="100" ind1="1" ind2=" "><subfield code="a">Sibelius, Jean</subfield></datafield>
            <datafield tag="245" ind1="1" ind2="0"><subfield code="a">Finlandia</subfield></datafield>
            <datafield tag="028" ind1="0" ind2="2"><subfield code="a">ODE 1234-2</subfield></datafield>
        </record>""".encode()
    )
    record = build_target_record_from_marc(rec, source="melinda")
    assert record.get_identifiers("publisher_number") == ["ODE 1234-2"]
    assert record.work_uri and record.expression_uri and record.manifestation_uri
    assert record.content_type
