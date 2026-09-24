"""Unit tests for VocabularyManager, namespace detection, allowlist filtering, and link prioritization."""

from reconciliator.vocab import VocabularyManager, detect_vocabulary


def test_detect_vocabulary() -> None:
    assert detect_vocabulary("https://urn.fi/URN:NBN:fi:au:pn:000123456") == "KANTO_PN"
    assert detect_vocabulary("(FI-ASTERI-N)000123456") == "KANTO_PN"
    assert detect_vocabulary("https://urn.fi/URN:NBN:fi:au:cn:000000252") == "KANTO_CN"
    assert detect_vocabulary("(FI-ASTERI-C)000000252") == "KANTO_CN"
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
        allowed_vocabularies=["KANTO_PN", "KANTO_CN", "ISNI", "VIAF", "WIKIDATA"],
        agent_priority=["KANTO_PN", "KANTO_CN", "ISNI", "VIAF", "WIKIDATA"],
        link_policy="priority_fallback",
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    viaf_uri = "http://viaf.org/viaf/111079447"

    # Both present -> KANTO_PN chosen, VIAF suppressed
    selected, suppressed = vm.select_agent_uris([kanto_uri, viaf_uri])
    assert selected == [kanto_uri]
    assert suppressed == [viaf_uri]

    # Orthogonal $0 (KANTO_PN, VIAF) and $1 (WIKIDATA)
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
        allowed_vocabularies=["KANTO_PN", "VIAF"],
        link_policy="all",
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    viaf_uri = "http://viaf.org/viaf/111079447"

    selected, suppressed = vm.select_agent_uris([kanto_uri, viaf_uri])
    assert set(selected) == {kanto_uri, viaf_uri}
    assert suppressed == []


def test_vocabulary_manager_yaml_defaults() -> None:
    """Test that VocabularyManager cleanly defaults all parameters from YAML defaults without hardcoded fallbacks."""
    from reconciliator.config import get_bundled_vocabularies_defaults
    from reconciliator.vocab import get_vocabulary_patterns

    defaults = get_bundled_vocabularies_defaults()
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
    assert normalize_concept_id("https://urn.fi/URN:NBN:fi:au:pn:000123456") == "kanto_pn:000123456"
    assert normalize_concept_id("(FI-ASTERI-N)000123456") == "kanto_pn:000123456"
    assert normalize_concept_id("https://urn.fi/URN:NBN:fi:au:cn:000000252") == "kanto_cn:000000252"
    assert normalize_concept_id("(FI-ASTERI-C)000000252") == "kanto_cn:000000252"

    # WIKIDATA: upper case QID
    assert normalize_concept_id("http://www.wikidata.org/entity/q42") == "wikidata:Q42"
    assert normalize_concept_id("https://wikidata.org/wiki/Q42") == "wikidata:Q42"

    # VIAF & ISNI
    assert normalize_concept_id("http://viaf.org/viaf/111079447") == "viaf:111079447"
    assert normalize_concept_id("http://isni.org/isni/0000000121477983") == "isni:0000000121477983"

    # Expand back to canonical URI
    assert concept_id_to_uri("yso:p2591") == "http://www.yso.fi/onto/yso/p2591"
    assert concept_id_to_uri("slm:s123") == "https://urn.fi/URN:NBN:fi:au:slm:s123"
    assert concept_id_to_uri("kanto_pn:000123456") == "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    assert concept_id_to_uri("kanto_cn:000000252") == "https://urn.fi/URN:NBN:fi:au:cn:000000252"
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


def test_vocabulary_code_map_unification() -> None:
    """Test that yso, yso-aika, and yso-paikat output unified yso/fin and yso/swe vocabulary codes."""
    from reconciliator.config import VocabulariesConfig
    from reconciliator.vocab import format_target_vocab

    cfg = VocabulariesConfig()
    # Default language (fi -> fin)
    assert format_target_vocab("yso", config=cfg) == "yso/fin"
    assert format_target_vocab("yso-paikat", config=cfg) == "yso/fin"
    assert format_target_vocab("yso-aika", config=cfg) == "yso/fin"

    # Swedish language (sv -> swe)
    assert format_target_vocab("yso", lang="sv", config=cfg) == "yso/swe"
    assert format_target_vocab("yso-paikat", lang="sv", config=cfg) == "yso/swe"
    assert format_target_vocab("yso-aika", lang="sv", config=cfg) == "yso/swe"

    # Preserved source suffix
    assert format_target_vocab("yso-paikat", source_vocab="yso/swe", config=cfg) == "yso/swe"
    assert format_target_vocab("yso-aika", source_vocab="yso/swe", config=cfg) == "yso/swe"

    # Subcodes disabled
    cfg_no_sub = VocabulariesConfig(apply_language_subcode=False)
    assert format_target_vocab("yso", config=cfg_no_sub) == "yso"
    assert format_target_vocab("yso-paikat", config=cfg_no_sub) == "yso"
    assert format_target_vocab("yso-aika", config=cfg_no_sub) == "yso"


def test_yso_paikat_and_aika_actionable_uris() -> None:
    """Test that yso-paikat and yso-aika identifiers resolve to canonical http://www.yso.fi/onto/yso/ URIs."""
    from reconciliator.utils.curie import to_actionable_uri

    # CURIEs
    assert to_actionable_uri("yso-paikat:p104990") == "http://www.yso.fi/onto/yso/p104990"
    assert to_actionable_uri("yso-aika:p6197061979") == "http://www.yso.fi/onto/yso/p6197061979"
    assert to_actionable_uri("yso:p2591") == "http://www.yso.fi/onto/yso/p2591"

    # Legacy/divergent URLs canonicalized
    assert to_actionable_uri("http://www.yso.fi/onto/yso-paikat/p104990") == "http://www.yso.fi/onto/yso/p104990"
    assert to_actionable_uri("http://www.yso.fi/onto/yso-aika/p6197061979") == "http://www.yso.fi/onto/yso/p6197061979"


def test_resolve_target_language() -> None:
    """Test resolving target label language via record policy, global default, and per-vocab overrides."""
    from reconciliator.config import VocabulariesConfig
    from reconciliator.vocab import VocabFolder

    # 1. Default config: default_label_language = "record"
    cfg = VocabulariesConfig()
    folder = VocabFolder(config=cfg)

    # 3-letter MARC code maps to 2-letter
    assert folder.resolve_target_language("yso", record_lang="swe") == "sv"
    assert folder.resolve_target_language("yso", record_lang="fin") == "fi"
    assert folder.resolve_target_language("yso", record_lang="eng") == "en"
    # Unified code mapping
    assert folder.resolve_target_language("yso-paikat", record_lang="swe") == "sv"
    assert folder.resolve_target_language("yso-aika", record_lang="swe") == "sv"
    # When record_lang is None -> falls back to default_language ("fi")
    assert folder.resolve_target_language("yso", record_lang=None) == "fi"

    # 2. Per-vocabulary overrides
    cfg_overrides = VocabulariesConfig(
        default_label_language="record",
        vocabulary_label_languages={"kauno": "fi", "slm": "record"},
    )
    folder_overrides = VocabFolder(config=cfg_overrides)
    # kauno pinned to "fi" even on Swedish record
    assert folder_overrides.resolve_target_language("kauno", record_lang="swe") == "fi"
    # slm follows record
    assert folder_overrides.resolve_target_language("slm", record_lang="swe") == "sv"
    # unmentioned vocabulary (yso) uses default_label_language ("record")
    assert folder_overrides.resolve_target_language("yso", record_lang="swe") == "sv"

    # 3. Global fixed default_label_language
    cfg_fixed = VocabulariesConfig(
        default_label_language="fi",
        vocabulary_label_languages={"slm": "sv"},
    )
    folder_fixed = VocabFolder(config=cfg_fixed)
    assert folder_fixed.resolve_target_language("yso", record_lang="swe") == "fi"
    assert folder_fixed.resolve_target_language("kauno", record_lang="swe") == "fi"
    # override takes precedence
    assert folder_fixed.resolve_target_language("slm", record_lang="fin") == "sv"


def test_fold_subject_heading_multilingual_and_subcode() -> None:
    """Test fold_subject_heading multilingual label selection and accurate $2 subcode formatting."""
    from unittest.mock import MagicMock
    from reconciliator.config import VocabulariesConfig
    from reconciliator.models import VocabConcept
    from reconciliator.vocab import VocabFolder

    store = MagicMock()
    # Concept with fi and sv labels
    concept_yso = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p2591",
        vocabulary="yso",
        concept_id="p2591",
        pref_labels={"fi": "lääkärit", "sv": "läkare", "en": "physicians"},
        target_field="650",
    )
    # Concept with fi only
    concept_fi_only = VocabConcept(
        uri="http://www.yso.fi/onto/yso/p999",
        vocabulary="yso",
        concept_id="p999",
        pref_labels={"fi": "erikoislääkärit"},
        target_field="650",
    )

    def find_by_uri(uri):
        if uri == "http://www.yso.fi/onto/yso/p2591":
            return concept_yso
        if uri == "http://www.yso.fi/onto/yso/p999":
            return concept_fi_only
        return None

    store.find_concept_by_uri.side_effect = find_by_uri

    cfg = VocabulariesConfig(default_label_language="record")
    folder = VocabFolder(vocab_store=store, config=cfg)

    # 1. Swedish record gets Swedish label and yso/swe
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="lääkärit",
        source_vocab="yso/fin",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p2591",
        lang="swe",
    )
    assert t_label == "läkare"
    assert t_vocab == "yso/swe"

    # 2. Finnish record gets Finnish label and yso/fin
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="läkare",
        source_vocab="yso/swe",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p2591",
        lang="fin",
    )
    assert t_label == "lääkärit"
    assert t_vocab == "yso/fin"

    # 3. Swedish record where concept only has Finnish label:
    # Falls back to Finnish label, and formats $2 as yso/fin (reflecting the actual label language)
    t_field, t_vocab, t_uri, t_label, _, _ = folder.fold_subject_heading(
        heading="erikoislääkärit",
        source_vocab="yso",
        source_field="650",
        source_uri="http://www.yso.fi/onto/yso/p999",
        lang="swe",
    )
    assert t_label == "erikoislääkärit"
    assert t_vocab == "yso/fin"

