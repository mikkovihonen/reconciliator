"""Unit tests for chronological subject headings (MARC 648) and yso-aika vocabulary integration."""

from pathlib import Path

from lxml import etree

from reconciliator.ingestion.vocab import parse_turtle_simple
from reconciliator.loaders.marcxml import MARC_NS, parse_marc_record_elem
from reconciliator.models import (
    ConceptMapping,
    SubjectItem,
    SubjectsContainer,
    VocabConcept,
)
from reconciliator.normalizers.record import RecordNormalizer
from reconciliator.vocab_folding import VocabFolder
from reconciliator.vocab_store import VocabStore
from reconciliator.xml_enricher import MARCXMLEnricher


def test_subjects_container_helpers() -> None:
    container = SubjectsContainer(
        topical=[
            SubjectItem(
                uri="http://example.org/1", pref_label="kissa", vocabulary="yso", target_field="650"
            )
        ],
        genre_form=[
            SubjectItem(
                uri="http://example.org/2", pref_label="runot", vocabulary="slm", target_field="655"
            )
        ],
        geographic=[
            SubjectItem(
                uri="http://example.org/3",
                pref_label="Helsinki",
                vocabulary="yso-paikat",
                target_field="651",
            )
        ],
        chronological=[
            SubjectItem(
                uri="http://example.org/4",
                pref_label="1800-luku",
                vocabulary="yso-aika",
                target_field="648",
            )
        ],
        medium_of_performance=[
            SubjectItem(
                uri="http://example.org/5",
                pref_label="viulu",
                vocabulary="seko",
                target_field="382",
            )
        ],
    )

    items = container.all_items()
    assert len(items) == 5
    tags = [t for t, _ in items]
    assert tags == ["650", "655", "651", "648", "382"]

    all_subs = container.all_subjects()
    assert len(all_subs) == 5
    assert [s.pref_label for s in all_subs] == ["kissa", "runot", "Helsinki", "1800-luku", "viulu"]


def test_marcxml_loader_parses_648() -> None:
    xml_str = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <controlfield tag="001">rec-648</controlfield>
        <datafield tag="245" ind1="0" ind2="0">
            <subfield code="a">Suomen historia</subfield>
        </datafield>
        <datafield tag="648" ind1=" " ind2="7">
            <subfield code="a">1800-luku</subfield>
            <subfield code="2">yso-aika</subfield>
            <subfield code="0">http://www.yso.fi/onto/yso-aika/p1234</subfield>
        </datafield>
        <datafield tag="650" ind1=" " ind2="7">
            <subfield code="a">historia</subfield>
            <subfield code="2">yso</subfield>
        </datafield>
    </record>
    """
    elem = etree.fromstring(xml_str.encode("utf-8"))
    parsed = parse_marc_record_elem(elem)
    assert parsed is not None
    subjects = parsed["subjects"]
    assert "chronological" in subjects
    assert len(subjects["chronological"]) == 1
    assert subjects["chronological"][0]["heading"] == "1800-luku"
    assert subjects["chronological"][0]["field"] == "648"
    assert subjects["chronological"][0]["vocabulary"] == "yso-aika"
    assert subjects["chronological"][0]["uri"] == "http://www.yso.fi/onto/yso-aika/p1234"


def test_record_normalizer_folds_and_retags_chronological(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab_648.db")
    # Setup yso-aika concept
    store.insert_concepts(
        [
            VocabConcept(
                uri="http://www.yso.fi/onto/yso-aika/p1800",
                vocabulary="yso-aika",
                pref_label_fi="1800-luku",
                pref_label_sv="1800-talet",
                target_field="648",
            ),
            VocabConcept(
                uri="http://www.yso.fi/onto/yso/p456",
                vocabulary="yso",
                pref_label_fi="historia",
                target_field="650",
            ),
        ]
    )
    # Setup mapping from a legacy temporal concept in 650 to yso-aika in 648
    store.insert_mappings(
        [
            ConceptMapping(
                legacy_uri="http://www.yso.fi/onto/ysa/Y9999",
                legacy_vocab="ysa",
                legacy_label_fi="1800-luku",
                target_uri="http://www.yso.fi/onto/yso-aika/p1800",
                target_vocab="yso-aika",
                mapping_type="exact_match",
                target_field="648",
            )
        ]
    )

    folder = VocabFolder(store)
    normalizer = RecordNormalizer(vocab_folder=folder)

    # Raw record with:
    # 1. 648 heading already in chronological
    # 2. 650 heading (mis-tagged 1800-luku in topical) that should be retagged to 648
    raw = {
        "source_id": "test-rec",
        "title": "Aikakaudet",
        "subjects": {
            "chronological": [
                {
                    "heading": "1800-luku",
                    "field": "648",
                    "vocabulary": "yso-aika",
                    "uri": "http://www.yso.fi/onto/yso-aika/p1800",
                }
            ],
            "topical": [
                {
                    "heading": "1800-luku",
                    "field": "650",
                    "vocabulary": "ysa",
                    "uri": "http://www.yso.fi/onto/ysa/Y9999",
                },
                {
                    "heading": "historia",
                    "field": "650",
                    "vocabulary": "yso",
                },
            ],
        },
    }

    rec = normalizer.normalize(raw)
    # Both 1800-luku should end up in chronological (since target_field is 648)
    assert len(rec.subjects.chronological) == 2
    for item in rec.subjects.chronological:
        assert item.target_field == "648"
        assert item.vocabulary == "yso-aika"
        assert item.pref_label == "1800-luku"

    # And topical should retain only 'historia'
    assert len(rec.subjects.topical) == 1
    assert rec.subjects.topical[0].pref_label == "historia"


def test_xml_enricher_modernizes_648(tmp_path: Path) -> None:
    store = VocabStore(tmp_path / "vocab_enrich_648.db")
    store.insert_concepts(
        [
            VocabConcept(
                uri="http://www.yso.fi/onto/yso-aika/p1800",
                vocabulary="yso-aika",
                pref_label_fi="1800-luku",
                target_field="648",
            )
        ]
    )
    store.insert_mappings(
        [
            ConceptMapping(
                legacy_uri="http://www.yso.fi/onto/ysa/Y1800",
                legacy_vocab="ysa",
                legacy_label_fi="1800-luku",
                target_uri="http://www.yso.fi/onto/yso-aika/p1800",
                target_vocab="yso-aika",
                mapping_type="exact_match",
                target_field="648",
            )
        ]
    )
    folder = VocabFolder(store)
    enricher = MARCXMLEnricher(vocab_folder=folder)

    xml_content = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <controlfield tag="001">rec1</controlfield>
        <datafield tag="648" ind1=" " ind2="7">
            <subfield code="a">1800-luku</subfield>
            <subfield code="2">ysa</subfield>
            <subfield code="0">http://www.yso.fi/onto/ysa/Y1800</subfield>
        </datafield>
    </record>
    """
    root = etree.fromstring(xml_content.encode("utf-8"))
    enricher.enrich_record_tree(
        root,
        fold_legacy=True,
        enrich=True,
    )

    f648 = root.findall(".//marc:datafield[@tag='648']", MARC_NS) or root.findall(
        ".//datafield[@tag='648']"
    )
    assert len(f648) == 1
    f = f648[0]
    sub2 = (f.findall("marc:subfield[@code='2']", MARC_NS) or f.findall("subfield[@code='2']"))[
        0
    ].text
    assert sub2 == "yso-aika"
    sub0 = [
        e.text
        for e in (
            f.findall("marc:subfield[@code='0']", MARC_NS) or f.findall("subfield[@code='0']")
        )
    ]
    assert "http://www.yso.fi/onto/yso-aika/p1800" in sub0


def test_parse_turtle_simple_detects_yso_aika() -> None:
    ttl = """
    @prefix yso-aika: <http://www.yso.fi/onto/yso-aika/> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

    yso-aika:p1800 a skos:Concept ;
        skos:prefLabel "1800-luku"@fi ;
        skos:prefLabel "1800-talet"@sv .
    """
    concepts, mappings = parse_turtle_simple(ttl)
    assert len(concepts) == 1
    c = concepts[0]
    assert c.vocabulary == "yso-aika"
    assert c.target_field == "648"
    assert c.pref_labels.get("fi") == "1800-luku"
    assert c.pref_labels.get("sv") == "1800-talet"


def test_bibframe_rdflib_temporal_subject() -> None:
    from rdflib import Graph

    from reconciliator.loaders.bibframe import get_profile, parse_bibframe_graph

    ttl = """
    @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://example.org/inst/1> a bffi:Manifestation ;
        rdfs:label "Historia" ;
        bffi:expressionManifested <http://example.org/exp/1> .

    <http://example.org/exp/1> a bffi:Expression ;
        bffi:expressionOf <http://example.org/work/1> .

    <http://example.org/work/1> a bffi:Work ;
        bffi:subject <http://www.yso.fi/onto/yso-aika/p1900> .

    <http://www.yso.fi/onto/yso-aika/p1900> a bffi:Temporal ;
        rdfs:label "1900-luku" .
    """
    g = Graph()
    g.parse(data=ttl, format="turtle")
    profile = get_profile("bffi_1")
    records = list(parse_bibframe_graph(g, profile=profile))
    assert len(records) == 1
    rec, _ = records[0]
    subjects = rec["subjects"]
    assert "chronological" in subjects
    assert len(subjects["chronological"]) == 1
    sub = subjects["chronological"][0]
    assert sub["heading"] == "1900-luku"
    assert sub["field"] == "648"
    assert sub["vocabulary"] == "yso-aika"


def test_bibframe_jsonld_temporal_subject(tmp_path: Path) -> None:
    import json

    from reconciliator.loaders.bibframe import get_profile, iter_jsonld_bibframe_records

    data = [
        {
            "@id": "http://example.org/inst/1",
            "@type": ["http://id.loc.gov/ontologies/bibframe/Instance"],
            "http://id.loc.gov/ontologies/bibframe/instanceOf": [
                {"@id": "http://example.org/work/1"}
            ],
        },
        {
            "@id": "http://example.org/work/1",
            "@type": ["http://id.loc.gov/ontologies/bibframe/Work"],
            "http://id.loc.gov/ontologies/bibframe/subject": [
                {
                    "@id": "http://www.yso.fi/onto/yso-aika/p1900",
                    "@type": ["http://id.loc.gov/ontologies/bibframe/Temporal"],
                    "rdfs:label": [{"@value": "1900-luku"}],
                }
            ],
        },
    ]
    file_path = tmp_path / "records.json"
    file_path.write_text(json.dumps(data), encoding="utf-8")

    profile = get_profile("bibframe_2")
    recs = list(iter_jsonld_bibframe_records(file_path, profile=profile))
    assert len(recs) == 1
    rec = recs[0][0]
    assert "chronological" in rec["subjects"]
    assert rec["subjects"]["chronological"][0]["heading"] == "1900-luku"
    assert rec["subjects"]["chronological"][0]["field"] == "648"
    assert rec["subjects"]["chronological"][0]["vocabulary"] == "yso-aika"


def test_configurable_subject_field_category_map() -> None:
    from reconciliator.config import (
        RecordNormalizationConfig,
        get_category_default_field_map,
        get_subject_field_category_map,
    )

    cat_map = get_subject_field_category_map()
    assert cat_map["650"] == "topical"
    assert cat_map["651"] == "geographic"
    assert cat_map["648"] == "chronological"
    assert cat_map["655"] == "genre_form"
    assert cat_map["382"] == "medium_of_performance"

    field_map = get_category_default_field_map()
    assert field_map["topical"] == "650"
    assert field_map["geographic"] == "651"
    assert field_map["chronological"] == "648"
    assert field_map["genre_form"] == "655"
    assert field_map["medium_of_performance"] == "382"

    cfg = RecordNormalizationConfig()
    assert cfg.subject_field_category_map == cat_map


def test_bibframe_custom_subject_type_fields() -> None:
    from rdflib import Graph

    from reconciliator.loaders.bibframe import get_profile, parse_bibframe_graph

    ttl = """
    @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix ex: <http://example.org/> .

    <http://example.org/inst/1> a bffi:Manifestation ;
        rdfs:label "Test" ;
        bffi:expressionManifested <http://example.org/exp/1> .

    <http://example.org/exp/1> a bffi:Expression ;
        bffi:expressionOf <http://example.org/work/1> .

    <http://example.org/work/1> a bffi:Work ;
        bffi:subject <http://example.org/custom_subj> .

    <http://example.org/custom_subj> a ex:CustomTemporalType ;
        rdfs:label "Rautakausi" .
    """
    g = Graph()
    g.parse(data=ttl, format="turtle")
    base_profile = get_profile("bffi_1")
    # Custom profile with custom type mapped to 648
    custom_profile = base_profile.model_copy(
        update={
            "subject_type_fields": {
                **base_profile.subject_type_fields,
                "CustomTemporalType": "648",
            }
        }
    )
    records = list(parse_bibframe_graph(g, profile=custom_profile))
    assert len(records) == 1
    rec, _ = records[0]
    assert len(rec["subjects"]["chronological"]) == 1
    assert rec["subjects"]["chronological"][0]["heading"] == "Rautakausi"
    assert rec["subjects"]["chronological"][0]["field"] == "648"


def test_bibframe_dynamic_default_subject_field() -> None:
    from rdflib import Graph

    from reconciliator.config import RecordNormalizationConfig, VocabNormalizationConfig
    from reconciliator.loaders.bibframe import get_profile, parse_bibframe_graph

    rec_cfg = RecordNormalizationConfig()
    assert rec_cfg.default_subject_field == "650"

    vocab_cfg = VocabNormalizationConfig()
    assert vocab_cfg.default_vocab_target_field == "650"

    ttl = """
    @prefix bffi: <http://urn.fi/URN:NBN:fi:schema:bffi:> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

    <http://example.org/inst/1> a bffi:Manifestation ;
        rdfs:label "Test" ;
        bffi:expressionManifested <http://example.org/exp/1> .

    <http://example.org/exp/1> a bffi:Expression ;
        bffi:expressionOf <http://example.org/work/1> .

    <http://example.org/work/1> a bffi:Work ;
        bffi:subject <http://example.org/untyped_subj> .

    <http://example.org/untyped_subj> rdfs:label "Musiikki" .
    """
    g = Graph()
    g.parse(data=ttl, format="turtle")

    base_profile = get_profile("bffi_1")
    assert base_profile.default_subject_field == "650"

    # Override default_subject_field to 382
    custom_profile = base_profile.model_copy(update={"default_subject_field": "382"})
    records = list(parse_bibframe_graph(g, profile=custom_profile))
    assert len(records) == 1
    rec, _ = records[0]
    assert len(rec["subjects"]["medium_of_performance"]) == 1
    assert rec["subjects"]["medium_of_performance"][0]["heading"] == "Musiikki"
    assert rec["subjects"]["medium_of_performance"][0]["field"] == "382"
