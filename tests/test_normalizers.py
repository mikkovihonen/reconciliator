"""Unit tests for identifier, agent, and full record normalizers."""

from reconciliator.normalizers.agent import normalize_agent_name
from reconciliator.normalizers.identifiers import (
    isbn_10_to_13,
    normalize_isbn,
    normalize_issn,
    normalize_source_id,
    normalize_urn,
)
from reconciliator.normalizers.record import (
    clean_publisher,
    clean_title,
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


def test_oai_source_id_normalization() -> None:
    # Melinda OAI IDs
    assert (
        normalize_source_id("(FI-MELINDA)000123456", prefix="(FI-MELINDA)", digits=9)
        == "(FI-MELINDA)000123456"
    )
    assert (
        normalize_source_id("FI-MELINDA:123456", prefix="(FI-MELINDA)", digits=9)
        == "(FI-MELINDA)000123456"
    )
    # Other OAI sources (Libris, DNB, custom OAI URIs)
    assert (
        normalize_source_id("SE-LIBRIS:12345", prefix="(SE-LIBRIS)", digits=8)
        == "(SE-LIBRIS)00012345"
    )
    assert (
        normalize_source_id(
            "oai:libris.kb.se:bib/12345", pattern=r"bib/(\d+)", prefix="(SE-LIBRIS)", digits=8
        )
        == "(SE-LIBRIS)00012345"
    )
    assert normalize_source_id("(DE-101) 98765", prefix="(DE-101)", digits=9) == "(DE-101)000098765"


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
    assert extract_year("c2001") == 2001
    assert extract_year("p2010") == 2010
    assert extract_year("cop. 1986") == 1986
    assert extract_year("[1995]") == 1995
    assert extract_year("2003, c2001") == 2003
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

    # Dict-based classifications (from Wikidata ingestion)
    raw_dicts = [
        {"code": "82-3", "scheme": "udc", "is_primary": True},
        {"code": "813.54", "scheme": "ddc", "is_primary": False},
    ]
    classes_dict = normalize_classifications(raw_dicts)
    assert len(classes_dict) == 2
    assert classes_dict[0].code == "82-3" and classes_dict[0].is_primary
    assert classes_dict[1].code == "813.54" and not classes_dict[1].is_primary

    # Mixed inputs (tuple + dict)
    raw_mixed = [
        ("82-3", "udc"),
        {"code": "84.2", "scheme": "ykl", "is_primary": False},
    ]
    classes_mixed = normalize_classifications(raw_mixed)
    assert len(classes_mixed) == 2
    # ykl prioritized over udc when no explicit primary is set
    assert classes_mixed[0].code == "82-3" and not classes_mixed[0].is_primary
    assert classes_mixed[1].code == "84.2" and classes_mixed[1].is_primary


def test_fallback_agents_no_duplicates() -> None:
    from reconciliator.normalizers.record import RecordNormalizer

    normalizer = RecordNormalizer()
    raw = {
        "source_id": "test1",
        "title": "Test Title",
        "statement_of_responsibility": "kirjoittanut Mika Waltari ; kuvittanut Rudolf Koivu",
        "agents": [],
    }
    rec = normalizer.normalize(raw)
    assert len(rec.agents) == 2
    names = [a.name for a in rec.agents]
    assert names == ["Waltari, Mika", "Koivu, Rudolf"]


def test_relator_term_to_code() -> None:
    from reconciliator.normalizers.record import RecordNormalizer
    from reconciliator.normalizers.relators import RelatorRegistry

    registry = RelatorRegistry()
    assert registry.term_to_code("kirjoittaja") == "aut"
    assert registry.term_to_code("kääntäjä") == "trl"
    assert registry.term_to_code("författare") == "aut"
    assert registry.term_to_code("editor") == "edt"
    assert registry.term_to_code("unknown_role_xyz") is None

    # Verify RecordNormalizer maps role terms to codes
    normalizer = RecordNormalizer(relator_registry=registry)
    raw = {
        "source_id": "test2",
        "title": "Test Title 2",
        "agents": [
            {"name": "Mika Waltari", "role": "kirjoittaja", "is_personal": True},
            {"name": "Rudolf Koivu", "role": "kuvittaja", "is_personal": True},
            {"name": "Some Editor", "role": "edt", "is_personal": True},  # Already 3-letter code
        ],
    }
    rec = normalizer.normalize(raw)
    assert len(rec.agents) == 3
    # role_counts no longer exists on Agent model (roles are in contributions_table)


def test_normalize_doi() -> None:
    from reconciliator.normalizers.identifiers import normalize_doi

    assert normalize_doi("https://doi.org/10.1000/182") == "10.1000/182"
    assert normalize_doi("http://dx.doi.org/10.1000/182") == "10.1000/182"
    assert normalize_doi("doi:10.1000/182") == "10.1000/182"
    assert normalize_doi("  10.1000/182  ") == "10.1000/182"
    assert normalize_doi("invalid doi string") is None
    assert normalize_doi("") is None


def test_record_normalizer_standard_identifiers() -> None:
    from reconciliator.normalizers.record import RecordNormalizer

    normalizer = RecordNormalizer()
    raw = {
        "source_id": "test_ids",
        "title": "Title with IDs",
        "identifiers": {
            "isbn": ["978-951-0-12345-6", "951-0-12345-X (nid.)"],
            "issn": ["12345679"],
            "urn": ["URN:NBN:fi:jyu-20210101"],
            "doi": ["https://doi.org/10.1234/test.5678", "doi:10.9999/other"],
        },
    }
    rec = normalizer.normalize(raw)
    assert "10.1234/test.5678" in rec.identifiers.doi
    assert "10.9999/other" in rec.identifiers.doi
    assert "urn:nbn:fi:jyu-20210101" in rec.identifiers.urn
