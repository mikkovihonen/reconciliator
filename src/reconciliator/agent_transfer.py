"""Fuzzy agent name alignment and multi-candidate authority URI aggregation."""

from __future__ import annotations

from rapidfuzz import fuzz

from reconciliator.models import Agent, AgentURI, BibliographicRecord
from reconciliator.vocabularies import detect_vocabulary


class AgentAligner:
    """Aligns target agents with candidate records and aggregates discovered authority URIs."""

    def __init__(self, similarity_threshold: float = 85.0) -> None:
        self.similarity_threshold = similarity_threshold

    def align_target_agents(
        self,
        target_agents: list[Agent],
        candidate_records: list[BibliographicRecord],
    ) -> dict[int, list[AgentURI]]:
        """
        Align each target agent against agents across all accepted candidate records.
        Returns a mapping of target_agent_index -> list of discovered AgentURIs.
        """
        discovered: dict[int, list[AgentURI]] = {i: [] for i in range(len(target_agents))}

        for idx, target_agent in enumerate(target_agents):
            target_name = target_agent.name.lower()
            seen_uris: set[str] = set()

            for cand_rec in candidate_records:
                for cand_agent in cand_rec.agents:
                    cand_name = cand_agent.name.lower()
                    score = fuzz.token_sort_ratio(target_name, cand_name)

                    if score >= self.similarity_threshold:
                        # Extract $0 URIs (Asteri, ISNI, VIAF)
                        for u0 in cand_agent.uri_subfield_0:
                            if u0 and u0 not in seen_uris:
                                seen_uris.add(u0)
                                vocab = detect_vocabulary(u0) or "OTHER"
                                discovered[idx].append(
                                    AgentURI(
                                        uri=u0,
                                        vocabulary=vocab,
                                        subfield_code="0",
                                        source_record_id=cand_rec.source_id,
                                        source=cand_rec.source,
                                        alignment_score=float(score),
                                    )
                                )

                        # Extract $1 URIs (Wikidata QIDs)
                        for u1 in cand_agent.uri_subfield_1:
                            if u1 and u1 not in seen_uris:
                                seen_uris.add(u1)
                                vocab = detect_vocabulary(u1) or "WIKIDATA"
                                discovered[idx].append(
                                    AgentURI(
                                        uri=u1,
                                        vocabulary=vocab,
                                        subfield_code="1",
                                        source_record_id=cand_rec.source_id,
                                        source=cand_rec.source,
                                        alignment_score=float(score),
                                    )
                                )

        return discovered
