"""Unit tests verifying multi-candidate authority URI aggregation."""

from reconciliator.agent_transfer import AgentAligner
from reconciliator.models import Agent, BibliographicRecord


def test_multi_candidate_uri_aggregation() -> None:
    target_agent = Agent(name="Waltari, Mika", surname="Waltari", role="aut")
    target_rec = BibliographicRecord(
        source_id="T001",
        source="target",
        title="Sinuhe egyptiläinen",
        agents=[target_agent],
    )

    kanto_uri = "https://urn.fi/URN:NBN:fi:au:pn:000123456"
    isni_uri = "http://isni.org/isni/0000000121477983"
    wikidata_uri = "http://www.wikidata.org/entity/Q273115"

    # Candidate 1: Melinda record with Asteri URN
    cand1 = BibliographicRecord(
        source_id="MEL001",
        source="melinda",
        title="Sinuhe egyptiläinen",
        agents=[
            Agent(
                name="Waltari, Mika",
                surname="Waltari",
                role="aut",
                uri_subfield_0=[kanto_uri],
            )
        ],
    )

    # Candidate 2: Wikidata record with ISNI and Wikidata QID
    cand2 = BibliographicRecord(
        source_id="Q273115",
        source="wikidata",
        title="Sinuhe the Egyptian",
        agents=[
            Agent(
                name="Mika Waltari",
                surname="Waltari",
                role="aut",
                uri_subfield_0=[isni_uri],
                uri_subfield_1=[wikidata_uri],
            )
        ],
    )

    aligner = AgentAligner(similarity_threshold=85.0)
    discovered = aligner.align_target_agents(target_rec.agents, [cand1, cand2])

    agent_uris = discovered[0]
    uri_strings = {u.uri for u in agent_uris}

    assert kanto_uri in uri_strings
    assert isni_uri in uri_strings
    assert wikidata_uri in uri_strings
    assert len(agent_uris) == 3
