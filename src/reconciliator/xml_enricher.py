"""MARCXML tree mutator injecting non-present authority $0 and $1 subfields."""

from __future__ import annotations

from typing import Optional

from lxml import etree

from reconciliator.finto_folding import FintoFolder
from reconciliator.loaders.marcxml import MARC_NS, _get_datafields, _get_subfields
from reconciliator.matcher import ReconciliationResult
from reconciliator.normalizers.agent import normalize_agent_name


class MARCXMLEnricher:
    """Mutates MARCXML trees with novel authority links and folded modern ontology terms."""

    def __init__(self, finto_folder: Optional[FintoFolder] = None) -> None:
        self.finto_folder = finto_folder or FintoFolder()

    def enrich_record_tree(
        self,
        record_elem: etree._Element,
        result: ReconciliationResult,
        enrich: bool = True,
        fold_legacy: bool = True,
    ) -> etree._Element:
        """Enrich a MARCXML <record> element in-place with novel authority links and modernized subjects."""
        if not enrich and not fold_legacy:
            return record_elem

        # Determine XML namespace prefix if used
        if record_elem.tag.startswith("{"):
            ns = record_elem.tag.split("}")[0] + "}"
            subfield_tag = f"{ns}subfield"
        else:
            subfield_tag = "subfield"

        # 1. Subject Modernization & Folding (650, 651, 655, 382)
        if fold_legacy:
            for tag in ("650", "651", "655", "382"):
                for field in _get_datafields(record_elem, tag):
                    # Get $a
                    a_elems = field.findall("marc:subfield[@code='a']", MARC_NS) or field.findall(
                        "subfield[@code='a']"
                    )
                    heading = a_elems[0].text.strip() if a_elems and a_elems[0].text else ""
                    if not heading:
                        continue

                    # Get $2
                    f2_elems = field.findall("marc:subfield[@code='2']", MARC_NS) or field.findall(
                        "subfield[@code='2']"
                    )
                    vocab = f2_elems[0].text.strip() if f2_elems and f2_elems[0].text else None

                    # Get $0
                    f0_elems = field.findall("marc:subfield[@code='0']", MARC_NS) or field.findall(
                        "subfield[@code='0']"
                    )
                    existing_uris = [e.text.strip() for e in f0_elems if e.text]
                    source_uri = existing_uris[0] if existing_uris else None

                    target_field, target_vocab, target_uri, target_label, mapping_type = (
                        self.finto_folder.fold_subject_heading(
                            heading=heading,
                            source_vocab=vocab,
                            source_field=tag,
                            source_uri=source_uri,
                        )
                    )

                    # If tag changed (e.g. 650 -> 655 for SLM)
                    if target_field != tag:
                        field.set("tag", target_field)

                    # Update $a preferred label
                    if target_label and a_elems:
                        a_elems[0].text = target_label

                    # Update or add $2
                    if target_vocab:
                        if f2_elems:
                            f2_elems[0].text = target_vocab
                        else:
                            sub_2 = etree.SubElement(field, subfield_tag, code="2")
                            sub_2.text = target_vocab

                    # Inject modern $0 URI if enrich is active and not already present
                    if enrich and target_uri and target_uri not in existing_uris:
                        sub_0 = etree.SubElement(field, subfield_tag, code="0")
                        sub_0.text = target_uri

        # 2. Agent Enrichment ($0 Asteri/ISNI/VIAF and $1 Wikidata QID)
        if enrich:
            agent_fields: list[etree._Element] = []
            for tag in ("100", "700", "110", "710", "111", "711"):
                agent_fields.extend(_get_datafields(record_elem, tag))

            for agent_idx, target_agent in enumerate(result.target_record.agents):
                selected_uris = result.selected_agent_uris.get(agent_idx, [])
                if not selected_uris:
                    continue

                # Find corresponding field element
                matched_field: Optional[etree._Element] = None
                for af in agent_fields:
                    a_sub = _get_subfields(af, "a")
                    if a_sub:
                        raw_a = a_sub[0]
                        norm_a, _ = normalize_agent_name(raw_a)
                        if norm_a.lower() == target_agent.name.lower():
                            matched_field = af
                            break

                if matched_field is None and agent_idx < len(agent_fields):
                    matched_field = agent_fields[agent_idx]

                if matched_field is not None:
                    existing_u0 = set(_get_subfields(matched_field, "0"))
                    existing_u1 = set(_get_subfields(matched_field, "1"))

                    for uri in selected_uris:
                        if "wikidata.org" in uri.lower():
                            if uri not in existing_u1:
                                sub = etree.SubElement(matched_field, subfield_tag, code="1")
                                sub.text = uri
                                existing_u1.add(uri)
                        else:
                            if uri not in existing_u0:
                                sub = etree.SubElement(matched_field, subfield_tag, code="0")
                                sub.text = uri
                                existing_u0.add(uri)

        return record_elem
