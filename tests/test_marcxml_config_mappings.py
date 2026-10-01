"""Tests for configurable MARCXML mappings and heuristics."""

from lxml import etree

from reconciliator.loaders.marcxml import (
    CARRIER_TYPE_MAPPINGS,
    CONTENT_TYPE_WORK_TYPES,
    GENRE_PATTERNS,
    GENRE_WORK_TYPES,
    RDA_CONTENT_TYPES,
    TACTILE_CONTENT_CODES,
    decompose_marc_wemi,
    determine_carrier_type,
    determine_content_type,
    determine_work_type,
)


def test_default_mappings_loaded_from_configuration() -> None:
    # 1. Verify GENRE_WORK_TYPES has SLM defaults
    assert "slm" in GENRE_WORK_TYPES
    assert GENRE_WORK_TYPES["slm"]["televisio-ohjelmat"] == "broadcast_program"
    assert GENRE_WORK_TYPES["slm"]["juridiset teokset"] == "legal"
    assert GENRE_WORK_TYPES["slm"]["antologiat"] == "anthology"

    # 2. Verify GENRE_PATTERNS has default heuristics
    assert "sarjakuv" in GENRE_PATTERNS["comic"]
    assert "manga" in GENRE_PATTERNS["comic"]
    assert "lautapel" in GENRE_PATTERNS["board_game"]

    # 3. Verify RDA_CONTENT_TYPES
    assert RDA_CONTENT_TYPES["txt"] == "text"
    assert RDA_CONTENT_TYPES["ntm"] == "notated_music"
    assert RDA_CONTENT_TYPES["cod"] == "dataset"

    # 4. Verify CONTENT_TYPE_WORK_TYPES
    assert CONTENT_TYPE_WORK_TYPES["cod"] == "dataset"
    assert CONTENT_TYPE_WORK_TYPES["cop"] == "software"
    assert CONTENT_TYPE_WORK_TYPES["ntm"] == "music"

    # 5. Verify TACTILE_CONTENT_CODES
    assert "tct" in TACTILE_CONTENT_CODES
    assert "tcm" in TACTILE_CONTENT_CODES

    # 6. Verify CARRIER_TYPE_MAPPINGS
    assert CARRIER_TYPE_MAPPINGS["nc"] == "volume"
    assert CARRIER_TYPE_MAPPINGS["sd"] == "audio_disc"
    assert CARRIER_TYPE_MAPPINGS["cr"] == "online_resource"


def test_custom_genre_vocabulary_mapping() -> None:
    # Test that 655 with another vocabulary (e.g. LCGFT) can be resolved to WorkType
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nam a22000004i 4500</leader>
      <datafield tag="655" ind1=" " ind2="7">
        <subfield code="a">Graphic novels</subfield>
        <subfield code="2">lcgft</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">A Sample Graphic Novel</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    custom_cfg = {
        "record_normalization": {
            "genre_work_types": {
                "lcgft": {
                    "graphic novels": "comic",
                }
            }
        }
    }

    work_type = determine_work_type(elem, config=custom_cfg)
    assert work_type == "comic"

    # Also test via decompose_marc_wemi
    work = decompose_marc_wemi(elem, config=custom_cfg).work
    assert work.work_type == "comic"


def test_custom_genre_pattern_heuristics() -> None:
    # Test configuring genre patterns for another language (e.g. Swedish "serier" -> comic)
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nam a22000004i 4500</leader>
      <datafield tag="655" ind1=" " ind2="7">
        <subfield code="a">Tecknade serier</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">En svensk seriebok</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    custom_cfg = {
        "record_normalization": {
            "genre_patterns": {
                "comic": ["serier"],
            }
        }
    }

    work_type = determine_work_type(elem, config=custom_cfg)
    assert work_type == "comic"


def test_custom_content_and_carrier_type_mappings() -> None:
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000nam a22000004i 4500</leader>
      <datafield tag="336" ind1=" " ind2=" ">
        <subfield code="b">custom_code</subfield>
      </datafield>
      <datafield tag="338" ind1=" " ind2=" ">
        <subfield code="b">custom_carrier</subfield>
      </datafield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Custom Mappings Test</subfield>
      </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))

    custom_cfg = {
        "record_normalization": {
            "rda_content_types": {
                "custom_code": "dataset",
            },
            "content_type_work_types": {
                "custom_code": "dataset",
            },
            "carrier_type_mappings": {
                "custom_carrier": "online_resource",
            },
        }
    }

    assert determine_content_type(elem, config=custom_cfg) == "dataset"
    assert determine_work_type(elem, config=custom_cfg) == "dataset"
    assert determine_carrier_type(elem, config=custom_cfg) == "online_resource"
