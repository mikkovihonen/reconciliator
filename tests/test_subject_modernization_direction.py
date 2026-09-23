"""Unit tests verifying subject modernization direction, canonical preservation, alias priority, and single $0 update."""

from lxml import etree

from reconciliator.ingestion.vocab import parse_turtle_simple
from reconciliator.models import BibliographicRecord, ConceptMapping, VocabConcept
from reconciliator.vocab import VocabFolder, VocabStore
from reconciliator.xml_enricher import MARCXMLEnricher


def test_parse_turtle_simple_inverts_legacy_thesaurus_mappings(tmp_path) -> None:
    ttl_content = """
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix yso: <http://www.yso.fi/onto/yso/> .
@prefix allars: <http://uri.suomi.fi/datamodel/ns/allars#> .
@prefix ysa: <http://uri.suomi.fi/datamodel/ns/ysa#> .

yso:p1780 a skos:Concept ;
    skos:prefLabel "historia"@fi, "historia"@sv ;
    skos:exactMatch allars:Y19503, ysa:Y95390, <http://id.loc.gov/authorities/genreForms/gf2011026604> .
"""
    ttl_file = tmp_path / "test_yso.ttl"
    ttl_file.write_text(ttl_content, encoding="utf-8")

    concepts, mappings, hierarchy = parse_turtle_simple(
        ttl_content,
        vocab_target_fields={"yso": "650"},
        legacy_thesauri={"allars", "ysa"},
    )

    assert len(concepts) == 1
    c = concepts[0]
    assert c.vocabulary == "yso"
    assert c.concept_id == "p1780"

    # Mappings must map FROM legacy TO yso (inversion)
    assert len(mappings) == 2  # allars and ysa, NOT loc.gov gf...

    mapping_targets = {m.legacy_id: (m.target_id, m.legacy_vocab, m.target_vocab) for m in mappings}
    assert "Y19503" in mapping_targets
    target_id, legacy_vocab, target_vocab = mapping_targets["Y19503"]
    assert target_id == "p1780"
    assert legacy_vocab == "allars"
    assert target_vocab == "yso"

    assert "Y95390" in mapping_targets
    target_id, legacy_vocab, target_vocab = mapping_targets["Y95390"]
    assert target_id == "p1780"
    assert legacy_vocab == "ysa"
    assert target_vocab == "yso"

    # Verify no loc.gov external matches leaked into concept_mappings
    for m in mappings:
        assert "id.loc.gov" not in m.legacy_id
        assert "gf2011026604" not in m.legacy_vocab


def test_fold_subject_heading_preserves_existing_canonical(tmp_path) -> None:
    store = VocabStore(db_path=tmp_path / "test_vocab.db")

    # Insert a canonical concept
    concept = VocabConcept(
        vocabulary="yso",
        concept_id="p1780",
        pref_labels={"fi": "historia", "sv": "historia"},
        target_field="650",
    )
    store.insert_concepts([concept])

    # Insert a legacy mapping that points from allars to yso
    mapping = ConceptMapping(
        legacy_vocab="allars",
        legacy_id="Y19503",
        target_vocab="yso",
        target_id="p1780",
        target_field="650",
    )
    store.insert_mappings([mapping])

    folder = VocabFolder(store)

    # Test that a heading already carrying canonical YSO URI is retained as canonical
    target_field, target_vocab, target_uri, target_pref_label, mapping_type, qid = (
        folder.fold_subject_heading(
            heading="historia",
            source_vocab="yso/fin",
            source_uri="http://www.yso.fi/onto/yso/p1780",
            source_field="650",
        )
    )
    assert mapping_type == "existing_canonical"
    assert target_uri == "http://www.yso.fi/onto/yso/p1780"
    assert target_vocab == "yso/fin"


def test_find_concept_by_alias_field_priority(tmp_path) -> None:
    store = VocabStore(db_path=tmp_path / "test_vocab.db")

    yso_concept = VocabConcept(
        vocabulary="yso",
        concept_id="p1234",
        pref_labels={"fi": "kosto"},
        target_field="650",
    )
    kauno_concept = VocabConcept(
        vocabulary="kauno",
        concept_id="k5678",
        pref_labels={"fi": "kosto"},
        target_field="650",
    )
    store.insert_concepts([yso_concept, kauno_concept])

    aliases = [
        {
            "folded_label": "kosto",
            "raw_label": "kosto",
            "lang": "fi",
            "concept_id": "k5678",
            "vocabulary": "kauno",
            "target_field": "650",
            "is_preferred": 1,
        },
        {
            "folded_label": "kosto",
            "raw_label": "kosto",
            "lang": "fi",
            "concept_id": "p1234",
            "vocabulary": "yso",
            "target_field": "650",
            "is_preferred": 1,
        },
    ]
    store.insert_aliases(aliases)

    # For 650, default is yso and subject_priority has yso before kauno
    found, _ = store.find_concept_by_alias("kosto", target_field="650")
    assert found is not None
    assert found.vocabulary == "yso"
    assert found.concept_id == "p1234"


def test_xml_enricher_updates_existing_legacy_subfield_zero(tmp_path) -> None:
    store = VocabStore(db_path=tmp_path / "test_vocab.db")

    concept = VocabConcept(
        vocabulary="yso",
        concept_id="p1780",
        pref_labels={"fi": "historia"},
        target_field="650",
    )
    store.insert_concepts([concept])
    mapping = ConceptMapping(
        legacy_vocab="allars",
        legacy_id="Y19503",
        target_vocab="yso",
        target_id="p1780",
        target_field="650",
    )
    store.insert_mappings([mapping])

    xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <datafield tag="650" ind1=" " ind2="7">
        <subfield code="a">historia</subfield>
        <subfield code="0">allars:Y19503</subfield>
        <subfield code="2">allars</subfield>
      </datafield>
    </record>"""
    elem = etree.fromstring(xml.encode("utf-8"))

    folder = VocabFolder(store)
    enricher = MARCXMLEnricher(vocab_folder=folder)

    mock_rec = BibliographicRecord(source_id="TEST-001", source="target", title="Test")
    from reconciliator.reconciliation import ReconciliationResult

    result = ReconciliationResult(target_record=mock_rec)

    enriched = enricher.enrich_record_tree(elem, result, enrich=True, fold_legacy=True)

    f650 = enriched.findall(
        ".//{http://www.loc.gov/MARC21/slim}datafield[@tag='650']"
    ) or enriched.findall(".//datafield[@tag='650']")
    assert len(f650) == 1
    subfields_0 = [
        sf.text
        for sf in (
            f650[0].findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
            or f650[0].findall(".//subfield[@code='0']")
        )
    ]
    # Must have exactly 1 $0, updated to the canonical target URI
    assert len(subfields_0) == 1
    assert subfields_0[0] == "http://www.yso.fi/onto/yso/p1780"
