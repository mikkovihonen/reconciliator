"""Agent authority URI enrichment and contributor role normalization."""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from reconciliator.config import EnrichmentConfig
from reconciliator.loaders.marcxml import (
    _get_datafields,
    _get_subfields,
)
from reconciliator.normalizers.agent import normalize_agent_name
from reconciliator.normalizers.relators import RelatorRegistry
from reconciliator.reconciliation import ReconciliationResult
from reconciliator.utils.curie import (
    parse_curie,
    to_actionable_uri,
    to_curie,
)


class AgentEnricher:
    """Handles 1XX/7XX creator & contributor authority URI injection ($0, $1) and role normalization ($e, $j, $4)."""

    def __init__(self, relator_registry: Optional[RelatorRegistry] = None) -> None:
        self.relator_registry = relator_registry or RelatorRegistry()

    def enrich(
        self,
        record_elem: etree._Element,
        result: Any,
        record_lang: str,
        ns: str,
        subfield_tag: str,
        datafield_tag: str,
        toggles: EnrichmentConfig,
        enrich: bool = True,
    ) -> None:
        """Enrich agent fields with authority URIs and normalized relator roles in-place."""
        if not enrich:
            return

        # 1. Authority Enrichment ($0 Asteri/ISNI/VIAF and $1 Wikidata QID)
        if isinstance(result, ReconciliationResult) and result.target_record:
            agent_fields: list[etree._Element] = []
            for tag in ("100", "700", "110", "710", "111", "711"):
                agent_fields.extend(_get_datafields(record_elem, tag))

            for agent_idx, target_agent in enumerate(result.target_record.agents):
                selected_uris = result.selected_agent_uris.get(agent_idx, [])
                if not selected_uris:
                    continue

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
                            if toggles.inject_same_as and uri not in existing_u1:
                                sub = etree.SubElement(matched_field, subfield_tag, code="1")
                                sub.text = uri
                                existing_u1.add(uri)
                        else:
                            actionable_uri = to_actionable_uri(uri)
                            uri_src, uri_sid = parse_curie(actionable_uri)
                            uri_curie = (
                                to_curie(uri_src, uri_sid)
                                if uri_src != "unknown"
                                else actionable_uri
                            )

                            matched_existing_sub = None
                            for sub_elem in matched_field.findall(
                                f".//{subfield_tag}[@code='0']"
                            ) or matched_field.findall(".//subfield[@code='0']"):
                                s_text = (sub_elem.text or "").strip()
                                s_src, s_sid = parse_curie(s_text)
                                s_curie = to_curie(s_src, s_sid) if s_src != "unknown" else s_text
                                if s_text == actionable_uri or (
                                    uri_src != "unknown" and s_curie == uri_curie
                                ):
                                    matched_existing_sub = sub_elem
                                    break

                            if matched_existing_sub is not None:
                                matched_existing_sub.text = actionable_uri
                            elif actionable_uri not in existing_u0:
                                sub = etree.SubElement(matched_field, subfield_tag, code="0")
                                sub.text = actionable_uri
                                existing_u0.add(actionable_uri)

        # 2. Contributor Role Normalization & Injection ($e/$j and $4)
        if (
            toggles.inject_role_codes
            or toggles.normalize_role_terms
            or toggles.inject_missing_role_terms
        ):
            for tag in ("100", "700", "110", "710", "111", "711"):
                term_subfield_code = "j" if tag in ("111", "711") else "e"
                for field in _get_datafields(record_elem, tag):
                    term_elements = field.findall(
                        f".//{subfield_tag}[@code='{term_subfield_code}']"
                    ) or field.findall(f".//subfield[@code='{term_subfield_code}']")
                    f4_elements = field.findall(f".//{subfield_tag}[@code='4']") or field.findall(
                        ".//subfield[@code='4']"
                    )

                    if term_elements:
                        for term_elem in term_elements:
                            raw_term = (term_elem.text or "").strip()
                            if not raw_term:
                                continue
                            canonical_label, code = (
                                self.relator_registry.normalize_term_to_canonical(
                                    raw_term,
                                    lang=record_lang,
                                    role_format=toggles.role_format,
                                )
                            )
                            if toggles.normalize_role_terms and canonical_label:
                                term_elem.text = canonical_label

                            if toggles.inject_role_codes and code:
                                target_id = self.relator_registry.resolve_relator_identifier(
                                    code, format_mode=toggles.role_format
                                )
                                if target_id and not any(
                                    self.relator_registry.normalize_role((f4.text or "").strip())
                                    == code
                                    for f4 in f4_elements
                                ):
                                    sub_4 = etree.SubElement(field, subfield_tag, code="4")
                                    sub_4.text = target_id
                                    f4_elements.append(sub_4)

                    if not term_elements and f4_elements and toggles.inject_missing_role_terms:
                        for f4_elem in f4_elements:
                            f4_text = (f4_elem.text or "").strip()
                            code = self.relator_registry.normalize_role(f4_text)
                            if code and len(code) == 3:
                                canonical_label = self.relator_registry.get_canonical_term(
                                    code,
                                    lang=record_lang,
                                    role_format=toggles.role_format,
                                )
                                if canonical_label:
                                    sub_term = etree.SubElement(
                                        field, subfield_tag, code=term_subfield_code
                                    )
                                    sub_term.text = canonical_label
                                    term_elements.append(sub_term)

                    # Modernize any existing $4 if inject_role_codes is True
                    if toggles.inject_role_codes and f4_elements:
                        for f4_elem in f4_elements:
                            f4_text = (f4_elem.text or "").strip()
                            code = self.relator_registry.normalize_role(f4_text)
                            if code and len(code) == 3:
                                target_id = self.relator_registry.resolve_relator_identifier(
                                    code, format_mode=toggles.role_format
                                )
                                if target_id and f4_text != target_id:
                                    f4_elem.text = target_id
