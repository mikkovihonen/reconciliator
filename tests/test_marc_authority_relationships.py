"""Tests for MARC Authority agent relationships, qualifiers, and affiliations."""

import pytest
from lxml import etree

from reconciliator.config import AppConfig
from reconciliator.loaders.marcxml import (
    decompose_marc_authority,
    extract_agent_qualifiers,
    resolve_marc_relationship_type,
)
from reconciliator.models import AgentRelationshipEntity


def test_extract_agent_qualifiers_parenthesized():
    clean, quals = extract_agent_qualifiers("Vince (ohjelmoija)")
    assert clean == "Vince"
    assert quals == ["ohjelmoija"]


def test_extract_agent_qualifiers_subfields():
    clean, quals = extract_agent_qualifiers("Smith, John", ["(musician)", "[composer]"])
    assert clean == "Smith, John"
    assert "musician" in quals
    assert "composer" in quals


def test_resolve_marc_relationship_type():
    # Test $w code mappings
    assert resolve_marc_relationship_type("a", "") == "predecessor_of"
    assert resolve_marc_relationship_type("b", "") == "successor_of"
    assert resolve_marc_relationship_type("t", "") == "hierarchical_superior_of"
    assert resolve_marc_relationship_type("n", "") == "related_corporate_body"

    # Test $i designation patterns
    assert resolve_marc_relationship_type("", "Edeltäjä:", "510") == "predecessor_of"
    assert resolve_marc_relationship_type("", "Seuraaja:", "510") == "successor_of"
    assert resolve_marc_relationship_type("", "Todellinen nimi:", "500") == "real_identity_of"
    assert resolve_marc_relationship_type("", "Salapoliisinimi:", "500") == "pseudonym_of"
    assert resolve_marc_relationship_type("", "Emoyhteisö:", "510") == "hierarchical_superior_of"

    # Fallbacks
    assert resolve_marc_relationship_type("", "", "510") == "related_corporate_body"
    assert resolve_marc_relationship_type("", "", "500") == "related_person"


def test_decompose_marc_authority_qualifiers_and_affiliations():
    xml = """
    <record xmlns="http://www.loc.gov/MARC21/slim">
        <leader>00000cz  a2200000n  4500</leader>
        <controlfield tag="001">000191839</controlfield>
        <datafield tag="100" ind1="1" ind2=" ">
            <subfield code="a">Vince (ohjelmoija)</subfield>
            <subfield code="c">[pelintekijä]</subfield>
            <subfield code="d">1980-</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000191839</subfield>
        </datafield>
        <datafield tag="373" ind1=" " ind2=" ">
            <subfield code="a">Assembly Organizing</subfield>
        </datafield>
        <datafield tag="373" ind1=" " ind2=" ">
            <subfield code="a">Future Crew</subfield>
        </datafield>
        <datafield tag="500" ind1="1" ind2=" ">
            <subfield code="a">Valasvaara, Petja</subfield>
            <subfield code="d">1980-</subfield>
            <subfield code="i">Todellinen nimi:</subfield>
            <subfield code="0">http://urn.fi/URN:NBN:fi:au:pn:000191840</subfield>
        </datafield>
    </record>
    """
    elem = etree.fromstring(xml.encode("utf-8"))
    agent_rels: list[AgentRelationshipEntity] = []

    res = decompose_marc_authority(
        elem,
        source="kanto",
        agent_relationship_sink=agent_rels,
    )
    agents = res[4]
    assert len(agents) == 1
    agent = agents[0]

    # Verify qualifiers and clean name
    assert agent.name == "Vince"
    assert "ohjelmoija" in agent.qualifiers
    assert "pelintekijä" in agent.qualifiers

    # Verify affiliations
    assert "Assembly Organizing" in agent.affiliations
    assert "Future Crew" in agent.affiliations

    # Verify agent relationships sink
    assert len(agent_rels) >= 3

    aff_rels = [r for r in agent_rels if r.relationship_type == "affiliation"]
    assert len(aff_rels) == 2
    assert {r.target_name for r in aff_rels} == {"Assembly Organizing", "Future Crew"}

    identity_rels = [r for r in agent_rels if r.relationship_type == "real_identity_of"]
    assert len(identity_rels) == 1
    assert identity_rels[0].target_name == "Valasvaara, Petja"
    assert identity_rels[0].target_agent_curie == "http://urn.fi/URN:NBN:fi:au:pn:000191840"

