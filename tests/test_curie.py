"""Unit tests for CURIE utility module."""

from reconciliator.utils.curie import (
    extract_wikidata_qid,
    format_wikidata_uri,
    parse_curie,
    to_curie,
)


def test_to_curie() -> None:
    assert to_curie("wikidata", "Q42") == "wikidata:Q42"
    assert to_curie("MELINDA", "000123") == "melinda:000123"
    assert to_curie("kanto", "000012345") == "kanto:000012345"


def test_parse_curie_standard() -> None:
    assert parse_curie("wikidata:Q12344") == ("wikidata", "Q12344")
    assert parse_curie("isni:0000000121484501") == ("isni", "0000000121484501")
    assert parse_curie("melinda:000123456") == ("melinda", "000123456")


def test_parse_curie_from_uris() -> None:
    assert parse_curie("https://www.wikidata.org/entity/Q12344") == ("wikidata", "Q12344")
    assert parse_curie("http://wikidata.org/wiki/Q42") == ("wikidata", "Q42")
    assert parse_curie("https://isni.org/isni/0000000121484501") == ("isni", "0000000121484501")
    assert parse_curie("http://viaf.org/viaf/12345") == ("viaf", "12345")
    assert parse_curie("http://urn.fi/URN:NBN:fi:au:pn:000012345") == ("kanto", "000012345")
    assert parse_curie("https://orcid.org/0000-0002-1825-0097") == ("orcid", "0000-0002-1825-0097")


def test_parse_curie_from_marc_prefix() -> None:
    assert parse_curie("(FI-ASTERI-N)000012345") == ("kanto", "000012345")
    assert parse_curie("(FI-MELINDA)000123456") == ("melinda", "000123456")


def test_parse_curie_bare_qid() -> None:
    assert parse_curie("Q12344") == ("wikidata", "Q12344")


def test_extract_wikidata_qid() -> None:
    assert extract_wikidata_qid("wikidata:Q12344") == "Q12344"
    assert extract_wikidata_qid("https://www.wikidata.org/entity/Q42") == "Q42"
    assert extract_wikidata_qid("Q999") == "Q999"
    assert extract_wikidata_qid("isni:0000000121484501") is None


def test_format_wikidata_uri() -> None:
    assert format_wikidata_uri("wikidata:Q42") == "http://www.wikidata.org/entity/Q42"
    assert format_wikidata_uri("Q42") == "http://www.wikidata.org/entity/Q42"


def test_utils_reexports() -> None:
    from reconciliator.utils import (
        clear_curie_cache,
        extract_source_identifier,
        extract_wikidata_qid,
        format_source_uri,
        format_wikidata_uri,
        parse_curie,
        to_curie,
    )

    assert to_curie("wikidata", "Q42") == "wikidata:Q42"
    assert parse_curie("wikidata:Q42") == ("wikidata", "Q42")
    assert extract_wikidata_qid("Q42") == "Q42"

