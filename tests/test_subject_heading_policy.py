"""Configuration toggles for subject heading rewriting and $2 language subcodes.

The defaults reproduce the shipped behaviour: a resolved heading's $a is replaced
with the ontology preferred label, and the $2 vocabulary code carries the Finto
language subcode. Both are switchable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from reconciliator.config import VocabulariesConfig
from reconciliator.vocab import VocabFolder, VocabStore, format_target_vocab
from reconciliator.xml_enricher import MARCXMLEnricher

FIXTURES = Path(__file__).parent / "fixtures"
MARC_NS = "http://www.loc.gov/MARC21/slim"

# "tohtorit" is a YSO altLabel whose preferred label is "lääkärit", so folding it
# exercises the replace/retain decision.
RECORD = f"""<record xmlns="{MARC_NS}">
  <controlfield tag="001">POLICY-001</controlfield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Testiteos</subfield>
  </datafield>
  <datafield tag="650" ind1=" " ind2=" ">
    <subfield code="a">tohtorit</subfield>
    <subfield code="2">ysa</subfield>
  </datafield>
</record>
"""


@pytest.fixture
def vocab_store(tmp_path: Path) -> VocabStore:
    from reconciliator.config import VocabIngestConfig
    from reconciliator.ingestion.vocab import ingest_vocab_file

    store = VocabStore(tmp_path / "vocab.db")
    ingest_vocab_file(
        FIXTURES / "sample_finto.ttl",
        store,
        config=VocabIngestConfig(
            input_path=FIXTURES / "sample_finto.ttl",
            vocabularies=["yso", "kauno", "muso", "slm", "seko"],
            legacy_thesauri=["ysa", "allars", "kaunokki", "musa"],
        ),
    )
    return store


def _enrich(vocab_store: VocabStore, **cfg_overrides: object) -> etree._Element:
    cfg = VocabulariesConfig(**cfg_overrides)  # type: ignore[arg-type]
    enricher = MARCXMLEnricher(vocab_folder=VocabFolder(vocab_store, config=cfg))
    record = etree.fromstring(RECORD.encode("utf-8"))
    enricher.enrich_record_tree(record, enrich=True, fold_legacy=True)
    return record


def _subject_field(record: etree._Element) -> dict[str, list[str]]:
    for df in record.findall(f"{{{MARC_NS}}}datafield"):
        if df.get("tag") in ("650", "655"):
            out: dict[str, list[str]] = {}
            for sf in df.findall(f"{{{MARC_NS}}}subfield"):
                out.setdefault(sf.get("code"), []).append((sf.text or "").strip())
            return out
    raise AssertionError("no subject field in enriched record")


# ---------------------------------------------------------------------------
# Heading replacement
# ---------------------------------------------------------------------------


def test_default_replaces_heading_with_pref_label(vocab_store: VocabStore) -> None:
    subs = _subject_field(_enrich(vocab_store))
    assert subs["a"] == ["lääkärit"], "default should modernize the heading"
    assert subs["0"] == ["http://www.yso.fi/onto/yso/p2591"]
    assert "9" not in subs, "default keeps no copy of the catalogued heading"


def test_preserve_original_heading_keeps_catalogued_form(vocab_store: VocabStore) -> None:
    subs = _subject_field(_enrich(vocab_store, preserve_original_heading=True))
    assert subs["a"] == ["lääkärit"], "still modernized"
    assert subs["9"] == ["tohtorit"], "catalogued heading retained in the record"
    assert subs["0"] == ["http://www.yso.fi/onto/yso/p2591"]


def test_preserved_heading_subfield_is_configurable(vocab_store: VocabStore) -> None:
    subs = _subject_field(
        _enrich(
            vocab_store,
            preserve_original_heading=True,
            original_heading_subfield="x",
        )
    )
    assert subs["a"] == ["lääkärit"]
    assert subs["x"] == ["tohtorit"]
    assert "9" not in subs


def test_replacement_can_be_disabled(vocab_store: VocabStore) -> None:
    """The URI is still injected, but the catalogued heading stays in $a."""
    subs = _subject_field(_enrich(vocab_store, replace_heading_with_pref_label=False))
    assert subs["a"] == ["tohtorit"], "heading must be left as catalogued"
    assert subs["0"] == ["http://www.yso.fi/onto/yso/p2591"], "URI still corrected"


def test_no_duplicate_when_heading_already_matches_pref_label(
    vocab_store: VocabStore,
) -> None:
    """Nothing is retained when the catalogued form already is the preferred label."""
    cfg = VocabulariesConfig(preserve_original_heading=True)
    enricher = MARCXMLEnricher(vocab_folder=VocabFolder(vocab_store, config=cfg))
    record = etree.fromstring(RECORD.replace(">tohtorit<", ">lääkärit<").encode("utf-8"))
    enricher.enrich_record_tree(record, enrich=True, fold_legacy=True)
    subs = _subject_field(record)
    assert subs["a"] == ["lääkärit"]
    assert "9" not in subs


# ---------------------------------------------------------------------------
# $2 language subcode
# ---------------------------------------------------------------------------


def test_default_applies_language_subcode(vocab_store: VocabStore) -> None:
    subs = _subject_field(_enrich(vocab_store))
    assert subs["2"] == ["yso/fin"], "Finto convention carries the language subcode"


def test_language_subcode_can_be_disabled(vocab_store: VocabStore) -> None:
    subs = _subject_field(_enrich(vocab_store, apply_language_subcode=False))
    assert subs["2"] == ["yso"], "bare vocabulary code when the subcode is off"
    assert subs["0"] == ["http://www.yso.fi/onto/yso/p2591"], "URI unaffected"


def test_format_target_vocab_honours_the_toggle() -> None:
    on = VocabulariesConfig()
    off = VocabulariesConfig(apply_language_subcode=False)

    assert format_target_vocab("yso", "ysa", lang="fi", config=on) == "yso/fin"
    assert format_target_vocab("yso", "ysa", lang="fi", config=off) == "yso"

    # An explicit source suffix is still dropped when the toggle is off.
    assert format_target_vocab("slm", "slm/swe", config=on) == "slm/swe"
    assert format_target_vocab("slm", "slm/swe", config=off) == "slm"

    # Vocabularies that never carry a subcode are unaffected either way.
    assert format_target_vocab("seko", "musa", config=on) == "seko"
    assert format_target_vocab("seko", "musa", config=off) == "seko"


def test_toggles_are_independent(vocab_store: VocabStore) -> None:
    subs = _subject_field(
        _enrich(
            vocab_store,
            apply_language_subcode=False,
            preserve_original_heading=True,
        )
    )
    assert subs["a"] == ["lääkärit"]
    assert subs["9"] == ["tohtorit"]
    assert subs["2"] == ["yso"]
    assert subs["0"] == ["http://www.yso.fi/onto/yso/p2591"]
