"""Integration test suite for Milestone 5: WEMI MARCXML and BIBFRAME Enrichers and Dual Decoupled TSV Reports.

Tests 758 Work/Expression link injection across 42 specialized domains, uniform titles,
sibling/lineage links, accessibility/preservation links, constituent analytical links,
BIBFRAME PMO graph enrichment, and TSV report generation.
"""

import tempfile
from pathlib import Path
from typing import Optional

from lxml import etree
from rdflib import RDF, Graph, IdentifiedNode, URIRef

from reconciliator.config import AppConfig, EnrichmentConfig
from reconciliator.loaders.bibframe.enricher import (
    enrich_bibframe_graph,
    enrich_bibframe_jsonld,
)
from reconciliator.models import (
    ExpressionEntity,
    MatchRecord,
    WorkEntity,
)
from reconciliator.reconciliation import ConstituentWorkMatch, WEMIMatchResult
from reconciliator.reports import (
    EXPRESSION_TSV_COLUMNS,
    WORK_TSV_COLUMNS,
    ExpressionReportRow,
    ReportGenerator,
    WorkReportRow,
)
from reconciliator.xml_enricher import MARCXMLEnricher

OWL_SAME_AS = URIRef("http://www.w3.org/2002/07/owl#sameAs")
BF_INSTANCE_OF = URIRef("http://id.loc.gov/ontologies/bibframe/instanceOf")
BF_EXPRESSION_OF = URIRef("http://id.loc.gov/ontologies/bibframe/expressionOf")
BF_HAS_PART = URIRef("http://id.loc.gov/ontologies/bibframe/hasPart")

MARC_SAMPLE = """<record xmlns="http://www.loc.gov/MARC21/slim">
  <leader>01234cam a22003254i 4500</leader>
  <controlfield tag="001">rec_001</controlfield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Waltari, Mika,</subfield>
  </datafield>
  <datafield tag="240" ind1="1" ind2="0">
    <subfield code="a">Sinuhe egyptiläinen</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Sinuhe egyptiläinen /</subfield>
    <subfield code="c">Mika Waltari.</subfield>
  </datafield>
</record>"""


def test_marcxml_enricher_758_and_240():
    """Verify injection of 758 Work/Expression and 240 $0/$1 URIs into MARCXML tree."""
    root = etree.fromstring(MARC_SAMPLE.encode("utf-8"))
    enricher = MARCXMLEnricher()

    wemi_res = WEMIMatchResult(
        target_record_id="rec_001",
        matched_work=WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001",
            source="melinda",
            source_id="sinuhe_work",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            wikidata_qid="Q1198642",
        ),
        matched_expression=ExpressionEntity(
            work_id=1,
            expression_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001#fin",
            source="melinda",
            source_id="sinuhe_fin",
            language="fin",
            content_type="text",
            title="Sinuhe egyptiläinen",
        ),
        work_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001",
        expression_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001#fin",
        match_tier="tier1_exact_identifier",
        confidence_score=1.0,
    )

    enriched = enricher.enrich_wemi_record_tree(record_elem=root, wemi_result=wemi_res)

    # Verify 758 fields
    fields_758 = enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    assert len(fields_758) == 2

    # Work 758
    work_758 = [
        f
        for f in fields_758
        if any(
            "instanceOf" in (s.text or "")
            for s in f.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='4']")
        )
    ][0]
    assert any(
        s.text == "http://urn.fi/URN:NBN:fi:au:pn:000000001"
        for s in work_758.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
    )
    assert any(
        "Q1198642" in (s.text or "")
        for s in work_758.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='1']")
    )

    # Expression 758
    exp_758 = [
        f
        for f in fields_758
        if any(
            "expressionOf" in (s.text or "")
            for s in f.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='4']")
        )
    ][0]
    assert any(
        s.text == "http://urn.fi/URN:NBN:fi:au:pn:000000001#fin"
        for s in exp_758.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
    )

    # Verify 240 $0 and $1
    f240 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='240']")
    assert f240 is not None
    assert any(
        s.text == "http://urn.fi/URN:NBN:fi:au:pn:000000001"
        for s in f240.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
    )
    assert any(
        "Q1198642" in (s.text or "")
        for s in f240.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='1']")
    )


def test_marcxml_enricher_specialized_domain_fields():
    """Verify enrichment across specialized material domains (patent, tactile, sammelband, geodata)."""
    raw_xml = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>01234cam a22003254i 4500</leader>
      <controlfield tag="001">special_001</controlfield>
      <datafield tag="245" ind1="1" ind2="0">
        <subfield code="a">Specialized Artifact</subfield>
      </datafield>
      <datafield tag="700" ind1="1" ind2="2">
        <subfield code="a">Beethoven, Ludwig van</subfield>
        <subfield code="t">Sinfonia nro 5</subfield>
      </datafield>
    </record>"""

    root = etree.fromstring(raw_xml.encode("utf-8"))
    enricher = MARCXMLEnricher()

    target = MatchRecord(
        record_id="special_001",
        format="marcxml",
        work_type="tactile",
        title="Specialized Artifact",
        title_stem="specialized artifact",
        work_blocking_key="artifact::key",
        patent_key="FI12345B",
        sammelband_item_id="BINDING_99",
        geodata_spatial_key="EPSG:3067",
        fingerprint_key="e-te e-re r-e- thal 1544R",
        web_seed_key="https://kansalliskirjasto.fi",
        fragment_codex_key="F.IV.1",
        ephemera_corp_key="STOCKMANN_CAT_1930",
        numismatic_issue_key="KM:42",
        raw_payload=b"",
    )

    wemi_res = WEMIMatchResult(
        target_record_id="special_001",
        work_uri="http://example.org/work/special",
        expression_uri="http://example.org/exp/special",
        constituent_matches=[
            ConstituentWorkMatch(
                title="Sinfonia nro 5",
                work_uri="http://example.org/work/beethoven_sym5",
            )
        ],
    )

    enriched = enricher.enrich_wemi_record_tree(
        record_elem=root,
        wemi_result=wemi_res,
        target_record=target,
    )

    # 1. Accessibility 341 & 532
    f341 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='341']")
    assert f341 is not None
    assert any(
        s.text == "braille"
        for s in f341.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='b']")
    )
    f532 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='532']")
    assert f532 is not None

    # 2. Early print fingerprint 026
    f026 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='026']")
    assert f026 is not None
    assert any(
        "feigb" in (s.text or "")
        for s in f026.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='2']")
    )

    # 3. Sammelband composite 777
    f777 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='777']")
    assert f777 is not None
    assert any(
        "BINDING_99" in (s.text or "")
        for s in f777.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='o']")
    )

    # 4. Geodata 084 $2 epsg
    f084 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='084']")
    assert f084 is not None
    assert any(
        "EPSG:3067" in (s.text or "")
        for s in f084.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='a']")
    )

    # 5. Numismatics 024 $2 km
    f024_km = [
        f
        for f in enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='024']")
        if any(
            s.text == "km"
            for s in f.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='2']")
        )
    ]
    assert len(f024_km) > 0

    # 6. Patent 013
    f013 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='013']")
    assert f013 is not None
    assert any(
        "FI12345B" in (s.text or "")
        for s in f013.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='a']")
    )

    # 7. Analytical 700 $t constituent work
    f700 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']")
    assert f700 is not None
    assert any(
        s.text == "http://example.org/work/beethoven_sym5"
        for s in f700.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")
    )


def test_bibframe_enricher_wemi_rdf_graph():
    """Verify RDF graph in-place enrichment with owl:sameAs, PMO types, and WEMI relationships."""
    g = Graph()
    work_node = URIRef("http://example.org/work_local_1")
    exp_node = URIRef("http://example.org/exp_local_1")
    inst_node = URIRef("http://example.org/inst_local_1")

    nodes: tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]] = (
        inst_node,
        exp_node,
        work_node,
    )

    wemi_res = WEMIMatchResult(
        target_record_id="rec_bffi_01",
        matched_work=WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia",
            source="melinda",
            source_id="finlandia_w",
            title="Finlandia",
            title_normalized="finlandia",
            work_type="music",
            wikidata_qid="Q827419",
        ),
        matched_expression=ExpressionEntity(
            work_id=1,
            expression_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia#score",
            source="melinda",
            source_id="finlandia_e",
            content_type="notated_music",
            language="fin",
            title="Finlandia Score",
        ),
        work_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia",
        expression_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia#score",
        manifestation_uri="http://example.org/manif/finlandia_score",
        constituent_matches=[
            ConstituentWorkMatch(
                title="Andante Festivo",
                work_uri="http://urn.fi/URN:NBN:fi:au:pn:music_andante",
            )
        ],
    )

    enrich_bibframe_graph(g=g, nodes=nodes, wemi_result=wemi_res)

    # Verify Work owl:sameAs
    assert (
        work_node,
        OWL_SAME_AS,
        URIRef("http://urn.fi/URN:NBN:fi:au:pn:music_finlandia"),
    ) in g
    assert (
        work_node,
        OWL_SAME_AS,
        URIRef("http://www.wikidata.org/entity/Q827419"),
    ) in g

    # Verify Expression owl:sameAs & expressionOf
    assert (
        exp_node,
        OWL_SAME_AS,
        URIRef("http://urn.fi/URN:NBN:fi:au:pn:music_finlandia#score"),
    ) in g
    assert (exp_node, BF_EXPRESSION_OF, work_node) in g

    # Verify Manifestation owl:sameAs & instanceOf
    assert (
        inst_node,
        OWL_SAME_AS,
        URIRef("http://example.org/manif/finlandia_score"),
    ) in g
    assert (inst_node, BF_INSTANCE_OF, work_node) in g

    # Verify MusicNotation PMO RDF class
    assert (
        work_node,
        RDF.type,
        URIRef("http://id.loc.gov/ontologies/bibframe/MusicNotation"),
    ) in g

    # Verify Constituent Part bf:hasPart
    assert (
        work_node,
        BF_HAS_PART,
        URIRef("http://urn.fi/URN:NBN:fi:au:pn:music_andante"),
    ) in g


def test_bibframe_enricher_jsonld_wemi():
    """Verify JSON-LD dictionary enrichment with wemi_reconciliation block."""
    doc = {"@id": "http://example.org/inst_1", "title": "Finlandia"}
    wemi_res = WEMIMatchResult(
        target_record_id="target_01",
        work_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia",
        expression_uri="http://urn.fi/URN:NBN:fi:au:pn:music_finlandia#score",
        manifestation_uri="http://example.org/manif/finlandia",
        match_tier="tier1_exact_identifier",
        funnel_used="funnel_d_notated_music",
        confidence_score=1.0,
    )

    enriched_doc = enrich_bibframe_jsonld(doc=doc, wemi_result=wemi_res)
    assert "wemi_reconciliation" in enriched_doc
    recon = enriched_doc["wemi_reconciliation"]
    assert recon["work_uri"] == "http://urn.fi/URN:NBN:fi:au:pn:music_finlandia"
    assert recon["match_tier"] == "tier1_exact_identifier"
    assert recon["funnel_used"] == "funnel_d_notated_music"


def test_tsv_reports_work_and_expression():
    """Verify ReportGenerator generates valid TSV files for Work, Expression, and WEMI summary."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        cfg = AppConfig()
        rep_gen = ReportGenerator(config=cfg)

        # Add sample work report row
        w_row = WorkReportRow(
            target_record_id="rec_001",
            work_type="text",
            target_title="Sinuhe egyptiläinen",
            target_author="Waltari, Mika",
            target_year=1945,
            work_blocking_key="waltari::sinuhe",
            matched_work_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001",
            wikidata_qid="Q1198642",
            match_tier="tier1_exact_identifier",
            funnel_used="funnel_a_monograph",
            confidence_score=1.0,
            is_clustered=True,
            cluster_id="wc_sinuhe",
            action="injected",
        )
        rep_gen.add_work_row(w_row)

        # Add sample expression report row
        e_row = ExpressionReportRow(
            target_record_id="rec_001",
            work_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001",
            expression_uri="http://urn.fi/URN:NBN:fi:au:pn:000000001#fin",
            content_type="text",
            language="fin",
            version_designation=None,
            translators=None,
            performers=None,
            match_tier="tier1_exact_identifier",
            confidence_score=1.0,
            cluster_id="ec_sinuhe_fin",
            action="injected",
        )
        rep_gen.add_expression_row(e_row)

        work_tsv = tmp_path / "work_reconciliation.tsv"
        exp_tsv = tmp_path / "expression_reconciliation.tsv"
        summary_tsv = tmp_path / "wemi_reconciliation_summary.tsv"

        rep_gen.write_work_report(work_tsv)
        rep_gen.write_expression_report(exp_tsv)
        rep_gen.write_wemi_summary_report(summary_tsv)

        # Verify file existence
        assert work_tsv.exists()
        assert exp_tsv.exists()
        assert summary_tsv.exists()

        # Check work report headers
        with work_tsv.open("r", encoding="utf-8") as f:
            lines = [line.strip().split("\t") for line in f if line.strip()]
            assert lines[0] == WORK_TSV_COLUMNS
            assert len(lines) == 2
            assert lines[1][0] == "rec_001"
            assert lines[1][1] == "text"
            assert lines[1][6] == "http://urn.fi/URN:NBN:fi:au:pn:000000001"

        # Check expression report headers
        with exp_tsv.open("r", encoding="utf-8") as f:
            lines = [line.strip().split("\t") for line in f if line.strip()]
            assert lines[0] == EXPRESSION_TSV_COLUMNS
            assert len(lines) == 2
            assert lines[1][0] == "rec_001"
            assert lines[1][3] == "text"

        # Check WEMI summary report
        with summary_tsv.open("r", encoding="utf-8") as f:
            lines = [line.strip().split("\t") for line in f if line.strip()]
            assert len(lines) >= 4
            sections = {row[0] for row in lines[1:]}
            assert "overview" in sections
            assert "work_types" in sections
            assert "content_types" in sections


def test_marcxml_enricher_predicate_profiles_and_modes():
    """Verify stacked vs distinct MARC 758 emission modes and predicate profiles."""
    enricher = MARCXMLEnricher()
    wemi_res = WEMIMatchResult(
        target_record_id="rec_001",
        matched_work=WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
            source="melinda",
            source_id="sinuhe_work",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            wikidata_qid="Q212267",
        ),
        matched_expression=ExpressionEntity(
            work_id=1,
            expression_uri="urn:reconciliator:exp:e000100201",
            source="melinda",
            source_id="sinuhe_exp_fin",
            title="The Egyptian",
            title_normalized="the egyptian",
            language="fin",
            content_type="text",
        ),
        work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
        expression_uri="urn:reconciliator:exp:e000100201",
        confidence_score=1.0,
    )

    # 1. Stacked mode with ["bffi", "rda"]
    root_stacked = etree.fromstring(MARC_SAMPLE.encode("utf-8"))
    cfg_stacked = EnrichmentConfig(
        active_profiles=["bffi", "rda"],
        marc_predicate_mode="stacked",
    )
    enriched_stacked = enricher.enrich_wemi_record_tree(
        record_elem=root_stacked,
        wemi_result=wemi_res,
        enrichment_config=cfg_stacked,
    )
    fields_758_stacked = enriched_stacked.findall(
        ".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']"
    )
    assert len(fields_758_stacked) == 2  # 1 work, 1 expression

    work_758 = fields_758_stacked[0]
    work_preds = [
        s.text for s in work_758.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='4']")
    ]
    assert "http://urn.fi/URN:NBN:fi:schema:bffi:workManifested" in work_preds
    assert "http://rdaregistry.info/Elements/m/P30135" in work_preds
    s2 = work_758.find(".//{http://www.loc.gov/MARC21/slim}subfield[@code='2']")
    assert s2 is not None and s2.text == "bffi"

    # 2. Distinct mode with ["bffi", "rda"]
    root_distinct = etree.fromstring(MARC_SAMPLE.encode("utf-8"))
    cfg_distinct = EnrichmentConfig(
        active_profiles=["bffi", "rda"],
        marc_predicate_mode="distinct",
    )
    enriched_distinct = enricher.enrich_wemi_record_tree(
        record_elem=root_distinct,
        wemi_result=wemi_res,
        enrichment_config=cfg_distinct,
    )
    fields_758_distinct = enriched_distinct.findall(
        ".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']"
    )
    # 2 profiles x 2 entities (work + expression) = 4 fields
    assert len(fields_758_distinct) == 4
    schemes = [
        s.text
        for f in fields_758_distinct
        for s in f.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='2']")
    ]
    assert "bffi" in schemes
    assert "rda" in schemes


def test_marcxml_enricher_fine_grained_toggles():
    """Verify selective omission when toggles are disabled."""
    enricher = MARCXMLEnricher()
    wemi_res = WEMIMatchResult(
        target_record_id="rec_001",
        matched_work=WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
            source="melinda",
            source_id="sinuhe_work",
            title="Sinuhe egyptiläinen",
            title_normalized="sinuhe egyptilainen",
            wikidata_qid="Q212267",
        ),
        matched_expression=ExpressionEntity(
            work_id=1,
            expression_uri="urn:reconciliator:exp:e000100201",
            source="melinda",
            source_id="sinuhe_exp_fin",
            title="The Egyptian",
            title_normalized="the egyptian",
            language="fin",
            content_type="text",
        ),
        work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
        expression_uri="urn:reconciliator:exp:e000100201",
        confidence_score=1.0,
    )
    target_rec = MatchRecord(
        record_id="rec_001",
        format="marcxml",
        work_type="text",
        title="Sinuhe egyptiläinen",
        uniform_title="Sinuhe egyptiläinen",
        title_stem="sinuhe",
        work_blocking_key="sinuhe",
        raw_payload=b"",
        host_citation_key="HOST-999",
    )

    # Disable work links, same_as, and component links
    cfg = EnrichmentConfig(
        inject_work_links=False,
        inject_expression_links=True,
        inject_same_as=False,
        inject_component_links=False,
    )
    root = etree.fromstring(MARC_SAMPLE.encode("utf-8"))
    enriched = enricher.enrich_wemi_record_tree(
        record_elem=root,
        wemi_result=wemi_res,
        target_record=target_rec,
        enrichment_config=cfg,
    )

    fields_758 = enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='758']")
    # Only expression 758 should be present
    assert len(fields_758) == 1
    assert any(
        "expressionOf" in (s.text or "")
        or "expressionManifested" in (s.text or "")
        or "P30139" in (s.text or "")
        for s in fields_758[0].findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='4']")
    )

    # 240 should NOT have work_uri injected because inject_work_links is False
    f240 = enriched.find(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='240']")
    assert f240 is not None
    assert not f240.findall(".//{http://www.loc.gov/MARC21/slim}subfield[@code='0']")

    # 773 should NOT be present because inject_component_links is False
    f773 = enriched.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='773']")
    assert len(f773) == 0


def test_bibframe_enricher_fine_grained_toggles():
    """Verify BIBFRAME graph enrichment respects fine-grained toggles."""
    g = Graph()
    inst_node = URIRef("http://example.org/instance/1")
    exp_node = URIRef("http://example.org/expression/1")
    work_node = URIRef("http://example.org/work/1")
    nodes: tuple[IdentifiedNode, Optional[IdentifiedNode], Optional[IdentifiedNode]] = (
        inst_node,
        exp_node,
        work_node,
    )

    wemi_res = WEMIMatchResult(
        target_record_id="bf_001",
        matched_work=WorkEntity(
            work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
            source="melinda",
            source_id="sinuhe_work",
            title="Sinuhe",
            title_normalized="sinuhe",
            wikidata_qid="Q212267",
        ),
        matched_expression=ExpressionEntity(
            work_id=1,
            expression_uri="urn:reconciliator:exp:e000100201",
            source="melinda",
            source_id="sinuhe_exp",
            title="Sinuhe",
            title_normalized="sinuhe",
            language="fin",
            content_type="text",
        ),
        work_uri="http://urn.fi/URN:NBN:fi:au:wa:000100200",
        expression_uri="urn:reconciliator:exp:e000100201",
        manifestation_uri="http://urn.fi/URN:NBN:fi:bib:me:000100200",
        constituent_matches=[
            ConstituentWorkMatch(
                title="part1",
                work_uri="http://example.org/work/part1",
                expression_uri=None,
                relationship_type=None,
                confidence_score=1.0,
            )
        ],
        confidence_score=1.0,
    )

    # Disable same_as and component links
    cfg = EnrichmentConfig(
        inject_work_links=True,
        inject_expression_links=True,
        inject_same_as=False,
        inject_component_links=False,
    )
    enriched_g = enrich_bibframe_graph(
        g,
        nodes,
        wemi_result=wemi_res,
        enrichment_config=cfg,
    )

    # owl:sameAs should NOT be injected because inject_same_as is False
    assert (
        work_node,
        OWL_SAME_AS,
        URIRef("http://urn.fi/URN:NBN:fi:au:wa:000100200"),
    ) not in enriched_g
    assert (
        work_node,
        OWL_SAME_AS,
        URIRef("http://www.wikidata.org/entity/Q212267"),
    ) not in enriched_g
    assert (exp_node, OWL_SAME_AS, URIRef("urn:reconciliator:exp:e000100201")) not in enriched_g

    # bf:expressionOf and bf:instanceOf SHOULD be injected
    assert (exp_node, BF_EXPRESSION_OF, work_node) in enriched_g
    assert (inst_node, BF_INSTANCE_OF, work_node) in enriched_g

    # bf:hasPart should NOT be injected because inject_component_links is False
    assert (work_node, BF_HAS_PART, URIRef("http://example.org/work/part1")) not in enriched_g
