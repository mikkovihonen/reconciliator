"""Unit tests for VocabularyManager, namespace detection, allowlist filtering, and link prioritization."""

from reconciliator.vocab import VocabularyManager, detect_vocabulary


def test_detect_vocabulary() -> None:
    assert detect_vocabulary("https://urn.fi/URN:NBN:fi:au:pn:000123456") == "KANTO"
    assert detect_vocabulary("(FI-ASTERI-N)000123456") == "KANTO"
    assert detect_vocabulary("http://www.yso.fi/onto/yso/p2591") == "YSO"
    assert detect_vocabulary("http://urn.fi/URN:NBN:fi:au:slm:s123") == "SLM"
    assert detect_vocabulary("http://urn.fi/URN:NBN:fi:au:seko:s789") == "SEKO"
    assert detect_vocabulary("http://www.yso.fi/onto/kauno/k123") == "KAUNO"
    assert detect_vocabulary("http://www.yso.fi/onto/muso/p100") == "MUSO"
    assert detect_vocabulary("http://viaf.org/viaf/111079447") == "VIAF"
    assert detect_vocabulary("http://isni.org/isni/0000000121477983") == "ISNI"
    assert detect_vocabulary("http://www.wikidata.org/entity/Q273115") == "WIKIDATA"


def test_priority_fallback() -> None:
    vm = VocabularyManager(
        allowed_vocabularies=["KANTO", "ISNI", "VIAF", "WIKIDATA"],
        agent_priority=["KANTO", "ISNI", "VIAF", "WIKIDATA"],
        link_policy="priority_fallback",
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    viaf_uri = "http://viaf.org/viaf/111079447"

    # Both present -> KANTO chosen, VIAF suppressed
    selected, suppressed = vm.select_agent_uris([kanto_uri, viaf_uri])
    assert selected == [kanto_uri]
    assert suppressed == [viaf_uri]

    # Orthogonal $0 (KANTO, VIAF) and $1 (WIKIDATA)
    wikidata_uri = "http://www.wikidata.org/entity/Q273115"
    selected_ortho, suppressed_ortho = vm.select_agent_uris([kanto_uri, viaf_uri, wikidata_uri])
    assert kanto_uri in selected_ortho
    assert wikidata_uri in selected_ortho
    assert viaf_uri in suppressed_ortho
    assert len(selected_ortho) == 2
    assert len(suppressed_ortho) == 1

    # Only VIAF present -> VIAF chosen
    selected_viaf, suppressed_viaf = vm.select_agent_uris([viaf_uri])
    assert selected_viaf == [viaf_uri]
    assert suppressed_viaf == []


def test_all_links_policy() -> None:
    vm = VocabularyManager(
        allowed_vocabularies=["KANTO", "VIAF"],
        link_policy="all",
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    viaf_uri = "http://viaf.org/viaf/111079447"

    selected, suppressed = vm.select_agent_uris([kanto_uri, viaf_uri])
    assert set(selected) == {kanto_uri, viaf_uri}
    assert suppressed == []


def test_vocabulary_manager_yaml_defaults() -> None:
    """Test that VocabularyManager cleanly defaults all parameters from YAML defaults without hardcoded fallbacks."""
    from reconciliator.config import get_bundled_vocab_defaults
    from reconciliator.vocab import get_vocabulary_patterns

    defaults = get_bundled_vocab_defaults()
    vm = VocabularyManager()

    assert vm.allowed_vocabularies == {v.upper() for v in defaults["vocabularies"]}
    assert vm.agent_priority == [v.upper() for v in defaults["agent_priority"]]
    assert vm.subject_priority == [v.upper() for v in defaults["subject_priority"]]
    assert vm.entity_vocabularies == {v.upper() for v in defaults["entity_vocabularies"]}
    assert vm.vocabulary_patterns == get_vocabulary_patterns()


def test_detect_vocabulary_custom_patterns() -> None:
    """Test detect_vocabulary with runtime configured patterns."""
    custom_patterns = {
        "CUSTOM_VOCAB": ["custom.org/term/"],
        "SPECIAL": ["special:id:"],
    }
    assert (
        detect_vocabulary("http://custom.org/term/123", patterns=custom_patterns) == "CUSTOM_VOCAB"
    )
    assert detect_vocabulary("urn:special:id:999", patterns=custom_patterns) == "SPECIAL"
    assert detect_vocabulary("http://custom.org/unknown", patterns=custom_patterns) is None


def test_vocabulary_manager_custom_patterns_and_priorities() -> None:
    """Test VocabularyManager with custom patterns and priorities."""
    custom_patterns = {
        "MY_AUTH": ["myauth.fi/id/"],
        "OTHER_AUTH": ["otherauth.org/"],
    }
    vm = VocabularyManager(
        allowed_vocabularies=["MY_AUTH", "OTHER_AUTH"],
        agent_priority=["MY_AUTH", "OTHER_AUTH"],
        vocabulary_patterns=custom_patterns,
    )

    my_uri = "https://myauth.fi/id/100"
    other_uri = "https://otherauth.org/200"

    assert vm.is_allowed(my_uri)
    assert vm.is_allowed(other_uri)
    assert not vm.is_allowed(
        "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    )  # KANTO not in custom allowed

    selected, suppressed = vm.select_agent_uris([other_uri, my_uri])
    assert selected == [my_uri]
    assert suppressed == [other_uri]


def test_normalize_concept_id_and_expand() -> None:
    """Test normalize_concept_id across various URI, control number, and CURIE variants."""
    from reconciliator.vocab import concept_id_to_uri, normalize_concept_id

    # YSO: URL, HTTPS, trailing slash, MARC control number, CURIE
    assert normalize_concept_id("http://www.yso.fi/onto/yso/p2591") == "yso:p2591"
    assert normalize_concept_id("https://yso.fi/onto/yso/p2591/") == "yso:p2591"
    assert normalize_concept_id("(FI-YSO)p2591") == "yso:p2591"
    assert normalize_concept_id("yso:p2591") == "yso:p2591"

    # SLM & SEKO
    assert normalize_concept_id("https://urn.fi/URN:NBN:fi:au:slm:s123") == "slm:s123"
    assert normalize_concept_id("(fi-slm)s123") == "slm:s123"
    assert normalize_concept_id("http://urn.fi/URN:NBN:fi:au:seko:s789") == "seko:s789"

    # KANTO: Asteri URN, control numbers
    assert normalize_concept_id("https://urn.fi/URN:NBN:fi:au:pn:000123456") == "kanto:000123456"
    assert normalize_concept_id("(FI-ASTERI-N)000123456") == "kanto:000123456"
    assert normalize_concept_id("(fi-kanto)000123456") == "kanto:000123456"

    # WIKIDATA: upper case QID
    assert normalize_concept_id("http://www.wikidata.org/entity/q42") == "wikidata:Q42"
    assert normalize_concept_id("https://wikidata.org/wiki/Q42") == "wikidata:Q42"

    # VIAF & ISNI
    assert normalize_concept_id("http://viaf.org/viaf/111079447") == "viaf:111079447"
    assert normalize_concept_id("http://isni.org/isni/0000000121477983") == "isni:0000000121477983"

    # Expand back to canonical URI
    assert concept_id_to_uri("yso:p2591") == "http://www.yso.fi/onto/yso/p2591"
    assert concept_id_to_uri("slm:s123") == "http://urn.fi/URN:NBN:fi:au:slm:s123"
    assert concept_id_to_uri("kanto:000123456") == "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    assert concept_id_to_uri("wikidata:Q42") == "http://www.wikidata.org/entity/Q42"


def test_concept_id_multiple_id_patterns_and_case_normalization() -> None:
    """Test custom configuration with multiple ordered id_patterns and case normalization."""
    from reconciliator.vocab import normalize_concept_id

    custom_rules = {
        "TEST_ONT": {
            "prefix": "testont",
            "uri_template": "http://test.org/{id}",
            "id_patterns": [
                r"test\.org/alpha/(A\d+)",
                r"test\.org/beta/(B\d+)",
                r"^(\w\d+)$",
            ],
            "canonical_id_case": "upper",
        }
    }
    custom_patterns = {
        "TEST_ONT": ["test.org/"],
    }

    assert (
        normalize_concept_id(
            "http://test.org/alpha/a123",
            rules=custom_rules,
            patterns=custom_patterns,
        )
        == "testont:A123"
    )
    assert (
        normalize_concept_id(
            "http://test.org/beta/b456",
            rules=custom_rules,
            patterns=custom_patterns,
        )
        == "testont:B456"
    )


def test_strip_vocabulary_subcode() -> None:
    """Test strip_vocabulary_subcode helper."""
    from reconciliator.vocab import strip_vocabulary_subcode

    assert strip_vocabulary_subcode("yso/fin") == "yso"
    assert strip_vocabulary_subcode("slm/swe") == "slm"
    assert strip_vocabulary_subcode("yso") == "yso"
    assert strip_vocabulary_subcode(None) == ""
    assert strip_vocabulary_subcode("  kauno/eng  ") == "kauno"


def test_select_subject_uris() -> None:
    """Test VocabularyManager.select_subject_uris priority fallback and entity preservation."""
    vm = VocabularyManager(
        allowed_vocabularies=["YSO", "SLM", "SEKO", "WIKIDATA"],
        subject_priority=["YSO", "SLM", "SEKO", "WIKIDATA"],
        link_policy="priority_fallback",
    )

    yso_uri = "http://www.yso.fi/onto/yso/p2591"
    slm_uri = "http://urn.fi/URN:NBN:fi:au:slm:s123"
    wikidata_uri = "http://www.wikidata.org/entity/Q42"

    # Both YSO and SLM present -> YSO chosen, SLM suppressed
    selected, suppressed = vm.select_subject_uris([slm_uri, yso_uri])
    assert selected == [yso_uri]
    assert suppressed == [slm_uri]

    # Orthogonal Wikidata entity preserved alongside preferred subject
    selected_ortho, suppressed_ortho = vm.select_subject_uris([slm_uri, yso_uri, wikidata_uri])
    assert yso_uri in selected_ortho
    assert wikidata_uri in selected_ortho
    assert slm_uri in suppressed_ortho
    assert len(selected_ortho) == 2
    assert len(suppressed_ortho) == 1


def test_format_wikidata_uri() -> None:
    """Test format_wikidata_uri function and VocabularyManager integration."""
    from reconciliator.vocab import format_wikidata_uri

    # Standard QID
    assert format_wikidata_uri("Q273115") == "http://www.wikidata.org/entity/Q273115"
    # Lowercase qid normalized to uppercase
    assert format_wikidata_uri("q273115") == "http://www.wikidata.org/entity/Q273115"
    # Prefixed concept ID
    assert format_wikidata_uri("wikidata:Q273115") == "http://www.wikidata.org/entity/Q273115"
    assert format_wikidata_uri("wikidata:q273115") == "http://www.wikidata.org/entity/Q273115"
    # Full URI preserved (and trailing slash stripped)
    assert (
        format_wikidata_uri("http://www.wikidata.org/entity/Q273115")
        == "http://www.wikidata.org/entity/Q273115"
    )
    assert (
        format_wikidata_uri("http://www.wikidata.org/entity/Q273115/")
        == "http://www.wikidata.org/entity/Q273115"
    )
    assert (
        format_wikidata_uri("https://www.wikidata.org/wiki/Q273115")
        == "https://www.wikidata.org/wiki/Q273115"
    )

    # None and empty
    assert format_wikidata_uri(None) is None
    assert format_wikidata_uri("") is None
    assert format_wikidata_uri("   ") is None

    # VocabularyManager method
    vm = VocabularyManager()
    assert vm.format_wikidata_uri("Q42") == "http://www.wikidata.org/entity/Q42"
    assert vm.format_wikidata_uri("q42") == "http://www.wikidata.org/entity/Q42"
    assert vm.format_wikidata_uri(None) is None
