"""SKOS / Linked Data RDF serializer for Agent and Work clusters."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import AppConfig, SKOSAuthoritySerializationConfig
from reconciliator.models import AgentCluster, WorkCluster


def _escape_turtle(val: str) -> str:
    """Escape string for Turtle literal syntax."""
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class SKOSAuthoritySerializer:
    """Serializes canonical clusters into SKOS and Schema.org Linked Data representations."""

    def __init__(self, config: Optional[SKOSAuthoritySerializationConfig] = None) -> None:
        if config is not None:
            self.config = config
        else:
            self.config = AppConfig().virtual_authority.serialization.skos

    def serialize_agent_cluster_jsonld(self, cluster: AgentCluster) -> dict[str, Any]:
        """Serialize an AgentCluster into a JSON-LD document."""
        entity_uri = cluster.canonical_uri or f"urn:reconciliator:agent:{cluster.cluster_id}"
        variant_names = [
            a.value
            for a in cluster.attributes
            if a.attribute_type == "variant_name" and a.value != cluster.canonical_name
        ]
        exact_matches = [
            f"{m.source}:{m.source_id}"
            for m in cluster.members
            if m.source.lower() in ("isni", "viaf", "wikidata", "kanto", "asteri")
        ]
        affiliations = [a.value for a in cluster.attributes if a.attribute_type == "affiliation"]
        occupations = [a.value for a in cluster.attributes if a.attribute_type == "occupation"]

        doc: dict[str, Any] = {
            "@context": {
                "skos": self.config.default_namespace,
                "schema": self.config.schema_namespace,
                "reconciliator": "urn:reconciliator:",
            },
            "@id": entity_uri,
            "@type": ["skos:Concept", "schema:Person"],
            "skos:prefLabel": cluster.canonical_access_point or cluster.canonical_name,
            "schema:name": cluster.canonical_name,
            "reconciliator:clusterId": cluster.cluster_id,
            "reconciliator:authorityStatus": cluster.authority_status,
            "reconciliator:memberCount": cluster.member_count,
            "reconciliator:workCount": cluster.work_count,
        }

        if cluster.canonical_surname:
            doc["schema:familyName"] = cluster.canonical_surname
        if cluster.birth_year:
            doc["schema:birthDate"] = str(cluster.birth_year)
        if cluster.death_year:
            doc["schema:deathDate"] = str(cluster.death_year)
        if variant_names:
            doc["skos:altLabel"] = variant_names
        if exact_matches:
            doc["skos:exactMatch"] = exact_matches
        if affiliations:
            doc["schema:affiliation"] = affiliations
        if occupations:
            doc["schema:hasOccupation"] = occupations

        return doc

    def serialize_agent_cluster_turtle(self, cluster: AgentCluster) -> str:
        """Serialize an AgentCluster into Turtle RDF format."""
        entity_uri = cluster.canonical_uri or f"urn:reconciliator:agent:{cluster.cluster_id}"
        pref_label = _escape_turtle(cluster.canonical_access_point or cluster.canonical_name)
        canon_name = _escape_turtle(cluster.canonical_name)

        lines = [
            f"@prefix skos: <{self.config.default_namespace}> .",
            f"@prefix schema: <{self.config.schema_namespace}> .",
            "@prefix reconciliator: <urn:reconciliator:> .",
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
            "",
            f"<{entity_uri}> a skos:Concept, schema:Person ;",
            f'    skos:prefLabel "{pref_label}" ;',
            f'    schema:name "{canon_name}" ;',
            f'    reconciliator:clusterId "{cluster.cluster_id}" ;',
            f'    reconciliator:authorityStatus "{cluster.authority_status}" ;',
            f"    reconciliator:memberCount {cluster.member_count} ;",
            f"    reconciliator:workCount {cluster.work_count} ",
        ]

        if cluster.birth_year:
            lines[-1] += ";"
            lines.append(f'    schema:birthDate "{cluster.birth_year}" ')
        if cluster.death_year:
            lines[-1] += ";"
            lines.append(f'    schema:deathDate "{cluster.death_year}" ')

        variant_names = [
            a.value
            for a in cluster.attributes
            if a.attribute_type == "variant_name" and a.value != cluster.canonical_name
        ]
        for vn in variant_names:
            escaped_vn = _escape_turtle(vn)
            lines[-1] += ";"
            lines.append(f'    skos:altLabel "{escaped_vn}" ')

        for m in cluster.members:
            if m.source.lower() in ("isni", "viaf", "wikidata", "kanto"):
                lines[-1] += ";"
                lines.append(f"    skos:exactMatch <{m.source}:{m.source_id}> ")

        lines[-1] += "."
        return "\n".join(lines)

    def serialize_work_cluster_jsonld(self, cluster: WorkCluster) -> dict[str, Any]:
        """Serialize a WorkCluster into a JSON-LD document."""
        entity_uri = cluster.canonical_work_curie or f"urn:reconciliator:work:{cluster.cluster_id}"

        doc: dict[str, Any] = {
            "@context": {
                "skos": self.config.default_namespace,
                "schema": self.config.schema_namespace,
                "reconciliator": "urn:reconciliator:",
            },
            "@id": entity_uri,
            "@type": ["skos:Concept", "schema:CreativeWork"],
            "skos:prefLabel": cluster.canonical_access_point or cluster.canonical_title,
            "schema:name": cluster.canonical_title,
            "reconciliator:clusterId": cluster.cluster_id,
            "reconciliator:authorityStatus": cluster.authority_status,
            "reconciliator:workType": cluster.work_type,
        }

        if cluster.canonical_author:
            doc["schema:author"] = {
                "@type": "schema:Person",
                "schema:name": cluster.canonical_author,
            }

        return doc

    def serialize_work_cluster_turtle(self, cluster: WorkCluster) -> str:
        """Serialize a WorkCluster into Turtle RDF format."""
        entity_uri = cluster.canonical_work_curie or f"urn:reconciliator:work:{cluster.cluster_id}"
        pref_label = _escape_turtle(cluster.canonical_access_point or cluster.canonical_title)
        canon_title = _escape_turtle(cluster.canonical_title)

        lines = [
            f"@prefix skos: <{self.config.default_namespace}> .",
            f"@prefix schema: <{self.config.schema_namespace}> .",
            "@prefix reconciliator: <urn:reconciliator:> .",
            "",
            f"<{entity_uri}> a skos:Concept, schema:CreativeWork ;",
            f'    skos:prefLabel "{pref_label}" ;',
            f'    schema:name "{canon_title}" ;',
            f'    reconciliator:clusterId "{cluster.cluster_id}" ;',
            f'    reconciliator:authorityStatus "{cluster.authority_status}" ;',
            f'    reconciliator:workType "{cluster.work_type}" ',
        ]

        if cluster.canonical_author:
            escaped_auth = _escape_turtle(cluster.canonical_author)
            lines[-1] += ";"
            lines.append(f'    schema:creator "{escaped_auth}" ')

        lines[-1] += "."
        return "\n".join(lines)
