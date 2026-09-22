"""Unit tests for MARC 21 Authority and SKOS / Linked Data serializers."""

from __future__ import annotations

from lxml import etree
import pytest

from reconciliator.authority.serializers.marc21 import MARC21AuthoritySerializer
from reconciliator.authority.serializers.skos import SKOSAuthoritySerializer
from reconciliator.config import VirtualAuthorityConfig
from reconciliator.models import (
    AgentCluster,
    AgentClusterAttribute,
    AgentClusterMember,
    WorkCluster,
)

MARC_NS = "http://www.loc.gov/MARC21/slim"


@pytest.fixture
def sample_agent_cluster() -> AgentCluster:
    return AgentCluster(
        cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
        canonical_name="Waltari, Mika",
        canonical_surname="Waltari",
        canonical_uri="urn:reconciliator:agent:va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
        birth_year=1908,
        death_year=1979,
        member_count=3,
        work_count=45,
        authority_status="established",
        canonical_access_point="Waltari, Mika, 1908-1979",
        members=[
            AgentClusterMember(
                agent_id=1,
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                source="asteri",
                source_id="00003456",
                link_method="exact_identifier",
                confidence=1.0,
            ),
            AgentClusterMember(
                agent_id=2,
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                source="wikidata",
                source_id="Q212885",
                link_method="wikidata_qid",
                confidence=1.0,
            ),
            AgentClusterMember(
                agent_id=3,
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                source="isni",
                source_id="0000000121171887",
                link_method="isni_uri",
                confidence=1.0,
            ),
        ],
        attributes=[
            AgentClusterAttribute(
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                attribute_type="variant_name",
                value="Nauticus",
                frequency=2,
                is_canonical=False,
            ),
            AgentClusterAttribute(
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                attribute_type="variant_name",
                value="Leo Arne",
                frequency=1,
                is_canonical=False,
            ),
            AgentClusterAttribute(
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                attribute_type="occupation",
                value="kirjailijat",
                frequency=5,
                is_canonical=True,
            ),
            AgentClusterAttribute(
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                attribute_type="affiliation",
                value="Suomen Kirjailijaliitto",
                frequency=3,
                is_canonical=True,
            ),
            AgentClusterAttribute(
                cluster_id="va_agent_018e4720-4a88-7f91-9233-8a3d13264101",
                attribute_type="place",
                value="Helsinki",
                frequency=4,
                is_canonical=True,
            ),
        ],
    )


@pytest.fixture
def sample_work_cluster() -> WorkCluster:
    return WorkCluster(
        cluster_id="va_work_018e4720-4a88-7f91-9233-8a3d13264102",
        canonical_title="Sinuhe egyptiläinen",
        canonical_author="Waltari, Mika",
        work_type="text",
        canonical_work_curie="urn:reconciliator:work:va_work_018e4720-4a88-7f91-9233-8a3d13264102",
        canonical_work_source="asteri",
        canonical_work_source_id="w_009988",
        authority_status="established",
        canonical_access_point="Waltari, Mika. Sinuhe egyptiläinen",
    )


def test_marc21_agent_cluster_serialization(sample_agent_cluster: AgentCluster):
    """Verify that MARC21AuthoritySerializer produces valid MARC 21 Authority XML with 100/400/024 fields."""
    serializer = MARC21AuthoritySerializer()
    xml_output = serializer.serialize_agent_cluster(sample_agent_cluster)

    root = etree.fromstring(xml_output.encode("utf-8"))
    assert root.tag == f"{{{MARC_NS}}}record"
    assert root.get("type") == "Authority"

    # Control fields 001 and 003
    f001 = root.find(f"./{{{MARC_NS}}}controlfield[@tag='001']")
    assert f001 is not None and f001.text == sample_agent_cluster.cluster_id
    f003 = root.find(f"./{{{MARC_NS}}}controlfield[@tag='003']")
    assert f003 is not None and f003.text == "FI-NL"

    # 100 1# authorized heading
    f100 = root.find(f"./{{{MARC_NS}}}datafield[@tag='100']")
    assert f100 is not None
    sf_a = f100.find(f"./{{{MARC_NS}}}subfield[@code='a']")
    assert sf_a is not None and sf_a.text == "Waltari, Mika"
    sf_d = f100.find(f"./{{{MARC_NS}}}subfield[@code='d']")
    assert sf_d is not None and sf_d.text == "1908-1979"

    # 400 1# variant headings
    f400_list = root.findall(f"./{{{MARC_NS}}}datafield[@tag='400']")
    variants = [df.find(f"./{{{MARC_NS}}}subfield[@code='a']").text for df in f400_list]
    assert "Nauticus" in variants
    assert "Leo Arne" in variants

    # 024 standard identifiers
    f024_list = root.findall(f"./{{{MARC_NS}}}datafield[@tag='024']")
    assert len(f024_list) >= 3

    # 374 occupation and 373 affiliation
    f374 = root.find(f"./{{{MARC_NS}}}datafield[@tag='374']")
    assert f374 is not None
    assert f374.find(f"./{{{MARC_NS}}}subfield[@code='a']").text == "kirjailijat"

    f373 = root.find(f"./{{{MARC_NS}}}datafield[@tag='373']")
    assert f373 is not None
    assert f373.find(f"./{{{MARC_NS}}}subfield[@code='a']").text == "Suomen Kirjailijaliitto"

    # 678 biographical summary / status
    f678 = root.find(f"./{{{MARC_NS}}}datafield[@tag='678']")
    assert f678 is not None
    assert "ESTABLISHED" in f678.find(f"./{{{MARC_NS}}}subfield[@code='a']").text


def test_marc21_work_cluster_serialization(sample_work_cluster: WorkCluster):
    """Verify that MARC21AuthoritySerializer produces valid uniform title authority records."""
    serializer = MARC21AuthoritySerializer()
    xml_output = serializer.serialize_work_cluster(sample_work_cluster)

    root = etree.fromstring(xml_output.encode("utf-8"))
    assert root.tag == f"{{{MARC_NS}}}record"

    f100 = root.find(f"./{{{MARC_NS}}}datafield[@tag='100']")
    assert f100 is not None
    assert f100.find(f"./{{{MARC_NS}}}subfield[@code='a']").text == "Waltari, Mika"
    assert f100.find(f"./{{{MARC_NS}}}subfield[@code='t']").text == "Sinuhe egyptiläinen"


def test_skos_agent_cluster_serialization(sample_agent_cluster: AgentCluster):
    """Verify SKOS JSON-LD and Turtle exports."""
    serializer = SKOSAuthoritySerializer()

    # JSON-LD
    jsonld = serializer.serialize_agent_cluster_jsonld(sample_agent_cluster)
    assert jsonld["@type"] == ["skos:Concept", "schema:Person"]
    assert jsonld["skos:prefLabel"] == "Waltari, Mika, 1908-1979"
    assert "Nauticus" in jsonld["skos:altLabel"]
    assert "Leo Arne" in jsonld["skos:altLabel"]
    assert jsonld["schema:birthDate"] == "1908"
    assert jsonld["reconciliator:authorityStatus"] == "established"

    # Turtle
    turtle = serializer.serialize_agent_cluster_turtle(sample_agent_cluster)
    assert "@prefix skos:" in turtle
    assert 'skos:prefLabel "Waltari, Mika, 1908-1979"' in turtle
    assert 'skos:altLabel "Nauticus"' in turtle
    assert 'schema:birthDate "1908"' in turtle


def test_skos_work_cluster_serialization(sample_work_cluster: WorkCluster):
    """Verify SKOS Work cluster JSON-LD and Turtle serialization."""
    serializer = SKOSAuthoritySerializer()

    jsonld = serializer.serialize_work_cluster_jsonld(sample_work_cluster)
    assert jsonld["@type"] == ["skos:Concept", "schema:CreativeWork"]
    assert jsonld["schema:name"] == "Sinuhe egyptiläinen"
    assert jsonld["schema:author"]["schema:name"] == "Waltari, Mika"

    turtle = serializer.serialize_work_cluster_turtle(sample_work_cluster)
    assert 'schema:name "Sinuhe egyptiläinen"' in turtle
    assert 'schema:creator "Waltari, Mika"' in turtle

