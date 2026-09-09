"""Unit tests for VocabularyManager, namespace detection, allowlist filtering, and link prioritization."""

from reconciliator.vocabularies import VocabularyManager, detect_vocabulary


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

