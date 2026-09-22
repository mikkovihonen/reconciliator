"""MARC 21 Authority XML serializer for Agent and Work clusters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from lxml import etree

from reconciliator.config import AppConfig, MARC21AuthoritySerializationConfig
from reconciliator.models import AgentCluster, WorkCluster

MARC_NS = "http://www.loc.gov/MARC21/slim"
NSMAP = {None: MARC_NS}


class MARC21AuthoritySerializer:
    """Serializes canonical clusters into standard MARC 21 Authority XML records."""

    def __init__(self, config: Optional[MARC21AuthoritySerializationConfig] = None) -> None:
        if config is not None:
            self.config = config
        else:
            self.config = AppConfig().virtual_authority.serialization.marc21

    def _create_controlfield(self, parent: etree._Element, tag: str, text: str) -> etree._Element:
        elem = etree.SubElement(parent, f"{{{MARC_NS}}}controlfield")
        elem.set("tag", tag)
        elem.text = text
        return elem

    def _create_datafield(
        self, parent: etree._Element, tag: str, ind1: str = " ", ind2: str = " "
    ) -> etree._Element:
        elem = etree.SubElement(parent, f"{{{MARC_NS}}}datafield")
        elem.set("tag", tag)
        elem.set("ind1", ind1)
        elem.set("ind2", ind2)
        return elem

    def _create_subfield(self, parent: etree._Element, code: str, text: str) -> etree._Element:
        elem = etree.SubElement(parent, f"{{{MARC_NS}}}subfield")
        elem.set("code", code)
        elem.text = text
        return elem

    def serialize_agent_cluster(self, cluster: AgentCluster) -> str:
        """Serialize an AgentCluster into a standard MARC 21 Authority XML document."""
        root = etree.Element(f"{{{MARC_NS}}}record", nsmap=NSMAP)
        root.set("type", "Authority")

        # Leader: Authority standard leader
        leader = etree.SubElement(root, f"{{{MARC_NS}}}leader")
        leader.text = "00000nz  a2200000n  4500"

        # 001: Control number (cluster ID)
        self._create_controlfield(root, "001", cluster.cluster_id)

        # 003: Control number identifier (organization code)
        self._create_controlfield(root, "003", self.config.organization_code)

        # 005: Date and time of latest transaction
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S.0")
        self._create_controlfield(root, "005", now_str)

        # 008: Fixed-Length Data Elements (40 characters)
        date_entered = datetime.now(timezone.utc).strftime("%y%m%d")
        f008_val = f"{date_entered}n| azannaabn          |a aaa      "
        self._create_controlfield(root, "008", f008_val[:40])

        # 024 7: Standard Identifiers & Cross-references
        seen_identifiers: set[str] = set()
        if cluster.canonical_uri:
            seen_identifiers.add(cluster.canonical_uri)
            df024 = self._create_datafield(root, "024", "7", " ")
            self._create_subfield(df024, "a", cluster.canonical_uri)
            self._create_subfield(df024, "2", "uri")

        for m in cluster.members:
            src_clean = m.source.lower()
            code_2 = self.config.standard_id_source_codes.get(src_clean, src_clean)
            val = m.source_id
            ident_key = f"{code_2}:{val}"
            if ident_key not in seen_identifiers:
                seen_identifiers.add(ident_key)
                df024 = self._create_datafield(root, "024", "7", " ")
                self._create_subfield(df024, "a", val)
                self._create_subfield(df024, "2", code_2)

        # 046: Special Coded Dates
        if cluster.birth_year or cluster.death_year:
            df046 = self._create_datafield(root, "046", " ", " ")
            if cluster.birth_year:
                self._create_subfield(df046, "f", str(cluster.birth_year))
            if cluster.death_year:
                self._create_subfield(df046, "g", str(cluster.death_year))

        # 100 1# (or 110 2# if corporate): Authorized Access Point
        # Default to 100 1# (personal name)
        df100 = self._create_datafield(root, "100", self.config.default_indicator_1, " ")
        self._create_subfield(df100, "a", cluster.canonical_name)
        if cluster.birth_year or cluster.death_year:
            b_str = str(cluster.birth_year or "")
            d_str = str(cluster.death_year or "")
            dates_str = f"{b_str}-{d_str}" if (b_str or d_str) else ""
            if dates_str:
                self._create_subfield(df100, "d", dates_str)

        # 400 1#: Variant Access Points (from attributes)
        variant_names = [
            a.value
            for a in cluster.attributes
            if a.attribute_type == "variant_name" and a.value != cluster.canonical_name
        ]
        for v_name in variant_names:
            df400 = self._create_datafield(root, "400", self.config.default_indicator_1, " ")
            self._create_subfield(df400, "a", v_name)
            if cluster.birth_year or cluster.death_year:
                b_str = str(cluster.birth_year or "")
                d_str = str(cluster.death_year or "")
                dates_str = f"{b_str}-{d_str}" if (b_str or d_str) else ""
                if dates_str:
                    self._create_subfield(df400, "d", dates_str)

        # 370: Associated Place
        places = [a.value for a in cluster.attributes if a.attribute_type == "place"]
        for pl in places:
            df370 = self._create_datafield(root, "370", " ", " ")
            self._create_subfield(df370, "c", pl)

        # 373: Associated Group / Affiliation
        affiliations = [a.value for a in cluster.attributes if a.attribute_type == "affiliation"]
        for aff in affiliations:
            df373 = self._create_datafield(root, "373", " ", " ")
            self._create_subfield(df373, "a", aff)

        # 374: Occupation
        occupations = [a.value for a in cluster.attributes if a.attribute_type == "occupation"]
        for occ in occupations:
            df374 = self._create_datafield(root, "374", " ", " ")
            self._create_subfield(df374, "a", occ)
            self._create_subfield(df374, "2", self.config.subject_system_code)

        # 670: Source Data Found Citations
        for m in cluster.members:
            df670 = self._create_datafield(root, "670", " ", " ")
            self._create_subfield(df670, "a", f"{m.source.upper()}: {m.source_id}")
            self._create_subfield(
                df670, "b", f"Link method: {m.link_method}; confidence: {m.confidence:.2f}"
            )

        # 678: Biographical text / Status summary
        df678 = self._create_datafield(root, "678", "0", " ")
        status_text = (
            f"Authority Status: {cluster.authority_status.upper()}; "
            f"Consolidated members: {cluster.member_count}; "
            f"Works: {cluster.work_count}"
        )
        self._create_subfield(df678, "a", status_text)

        return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8").decode("utf-8")

    def serialize_work_cluster(self, cluster: WorkCluster) -> str:
        """Serialize a WorkCluster into a standard MARC 21 Authority XML document."""
        root = etree.Element(f"{{{MARC_NS}}}record", nsmap=NSMAP)
        root.set("type", "Authority")

        # Leader
        leader = etree.SubElement(root, f"{{{MARC_NS}}}leader")
        leader.text = "00000nz  a2200000n  4500"

        # 001
        self._create_controlfield(root, "001", cluster.cluster_id)

        # 003
        self._create_controlfield(root, "003", self.config.organization_code)

        # 005
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S.0")
        self._create_controlfield(root, "005", now_str)

        # 008
        date_entered = datetime.now(timezone.utc).strftime("%y%m%d")
        f008_val = f"{date_entered}n| azannaabn          |a aaa      "
        self._create_controlfield(root, "008", f008_val[:40])

        # 024 7: Standard Identifier
        if cluster.canonical_work_curie:
            df024 = self._create_datafield(root, "024", "7", " ")
            self._create_subfield(df024, "a", cluster.canonical_work_curie)
            self._create_subfield(df024, "2", "uri")

        # 130 #0 (or 100 1# with $t title if author is present): Authorized Uniform Title Heading
        if cluster.canonical_author:
            df100 = self._create_datafield(root, "100", "1", " ")
            self._create_subfield(df100, "a", cluster.canonical_author)
            self._create_subfield(df100, "t", cluster.canonical_title)
        else:
            df130 = self._create_datafield(root, "130", " ", "0")
            self._create_subfield(df130, "a", cluster.canonical_title)

        # 380: Form of Work
        if cluster.work_type:
            df380 = self._create_datafield(root, "380", " ", " ")
            self._create_subfield(df380, "a", cluster.work_type)

        # 670: Source Citation
        if cluster.canonical_work_source:
            df670 = self._create_datafield(root, "670", " ", " ")
            self._create_subfield(
                df670, "a", f"{cluster.canonical_work_source.upper()}: {cluster.canonical_work_source_id or ''}"
            )
            self._create_subfield(df670, "b", f"Authorized work title: {cluster.canonical_title}")

        return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8").decode("utf-8")
