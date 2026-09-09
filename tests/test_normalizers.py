"""Unit tests for identifier, agent, and full record normalizers."""

from reconciliator.normalizers.agent import extract_surname, normalize_agent_name, strip_agent_dates
from reconciliator.normalizers.identifiers import (
    isbn_10_to_13,
    normalize_isbn,
    normalize_issn,
    normalize_melinda_id,
    normalize_urn,
    validate_isbn_13,
)
from reconciliator.normalizers.record import (
    clean_publisher,
    clean_title,
    extract_edition,
    extract_extent,
    extract_year,
    normalize_classifications,
    normalize_volume,
)


def test_isbn_normalization() -> None:
    # 13-digit with hyphens and qualifier
    raw = "978-951-0-00001-0 (nid.)"
    assert normalize_isbn(raw) == "9789510000010"

    # 10-digit ISBN conversion
    # 0-306-40615-2 -> 978-0-306-40615-7
    raw10 = "0-306-40615-2"
    conv13 = isbn_10_to_13("0306406152")
    assert conv13 == "9780306406157"
    assert normalize_isbn(raw10) == "9780306406157"


def test_issn_normalization() -> None:
    assert normalize_issn("00280836") == "0028-0836"
    assert normalize_issn("0028-0836") == "0028-0836"


def test_urn_normalization() -> None:
    raw = "http://urn.fi/URN:NBN:fi:bib:20210001"
    assert normalize_urn(raw) == "urn:nbn:fi:bib:20210001"


def test_melinda_id_normalization() -> None:
    assert normalize_melinda_id("(FI-MELINDA)000123456") == "(FI-MELINDA)000123456"
    assert normalize_melinda_id("FI-MELINDA:123456") == "(FI-MELINDA)000123456"


def test_agent_name_normalization() -> None:
    name, surname = normalize_agent_name("Waltari, Mika, 1908-1979", is_personal=True)
    assert name == "Waltari, Mika"
    assert surname == "Waltari"

    name_direct, surname_direct = normalize_agent_name("Mika Waltari", is_personal=True)
    assert name_direct == "Waltari, Mika"
    assert surname_direct == "Waltari"

    # Corporate name preserved
    corp, corp_surname = normalize_agent_name("Suomalaisen Kirjallisuuden Seura", is_personal=False)
    assert corp == "Suomalaisen Kirjallisuuden Seura"
    assert corp_surname is None


def test_title_cleaning() -> None:
    assert clean_title("The Great Gatsby /") == "Great Gatsby"
    assert clean_title("Sinuhe egyptiläinen :") == "Sinuhe egyptiläinen"
    assert clean_title("En herrgårdssägen.") == "herrgårdssägen"


def test_year_and_publisher_cleaning() -> None:
    assert extract_year("Helsinki, 1945.") == 1945
    assert clean_publisher("Kustannusosakeyhtiö Otava Oy") == "Otava"
    assert clean_publisher("WSOY Ab") == "WSOY"


def test_volume_normalization() -> None:
    assert normalize_volume("Sinuhe egyptiläinen 1. osa") == "osa 1"
    assert normalize_volume("Tuntematon sotilas, osa II") == "osa 2"
    assert normalize_volume("Vol. 3") == "osa 3"
    assert normalize_volume("del IV") == "osa 4"


def test_classifications_normalization() -> None:
    raw = [("82-3", "udc"), ("84.2", "ykl"), ("839.73", "ykl")]
    classes = normalize_classifications(raw)

    # First ykl must be flagged is_primary=True
    assert len(classes) == 3
    assert classes[0].code == "82-3" and not classes[0].is_primary
    assert classes[1].code == "84.2" and classes[1].is_primary
    assert classes[2].code == "839.73" and not classes[2].is_primary

