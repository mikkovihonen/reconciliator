"""MARCXML tree mutator injecting authority $0 and $1 subfields across all 42 WEMI domains."""

from __future__ import annotations

import json
from typing import Any, Optional

from lxml import etree

from reconciliator.config import (
    DEFAULT_MARC_PREDICATE_PROFILES,
    EnrichmentTogglesConfig,
    get_category_default_field_map,
    get_subject_field_category_map,
)
from reconciliator.loaders.marcxml import MARC_NS, _get_controlfield, _get_datafields, _get_subfields
from reconciliator.matcher import ReconciliationResult, WEMIMatchResult
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    TargetRecord,
    WorkCluster,
)
from reconciliator.normalizers.agent import normalize_agent_name
from reconciliator.vocab_folding import VocabFolder
from reconciliator.vocabularies import concept_id_to_uri, format_wikidata_uri


def _extract_record_language(record_elem: etree._Element) -> Optional[str]:
    """Extract 2-letter language code from 041 $a or 008 (bytes 35-37)."""
    for f041 in _get_datafields(record_elem, "041"):
        a_vals = _get_subfields(f041, "a")
        if a_vals:
            code = a_vals[0].strip().lower()
            if code in ("fin", "fi"):
                return "fi"
            if code in ("swe", "sv"):
                return "sv"
            if code in ("eng", "en"):
                return "en"
    f008 = _get_controlfield(record_elem, "008")
    if f008 and len(f008) >= 38:
        code = f008[35:38].strip().lower()
        if code in ("fin", "fi"):
            return "fi"
        if code in ("swe", "sv"):
            return "sv"
        if code in ("eng", "en"):
            return "en"
    return None


class MARCXMLEnricher:
    """Mutates MARCXML trees with novel authority links, folded modern terms, and WEMI relationships."""

    def __init__(self, vocab_folder: Optional[VocabFolder] = None) -> None:
        self.vocab_folder = vocab_folder or VocabFolder()

    def enrich_record_tree(
        self,
        record_elem: etree._Element,
        result: Any = None,
        enrich: bool = True,
        fold_legacy: bool = True,
        wemi_result: Optional[WEMIMatchResult] = None,
        linkage: Optional[CorpusLinkage] = None,
        work_cluster: Optional[WorkCluster] = None,
        expression_cluster: Optional[ExpressionCluster] = None,
        manifestation_cluster: Optional[ManifestationCluster] = None,
        target_record: Optional[TargetRecord] = None,
        enrichment_config: Optional[EnrichmentTogglesConfig] = None,
    ) -> etree._Element:
        """Enrich a MARCXML <record> element in-place with novel authority links and modernized subjects."""
        if not enrich and not fold_legacy:
            return record_elem

        toggles = enrichment_config or EnrichmentTogglesConfig()
        record_lang = _extract_record_language(record_elem) or toggles.language or "fi"

        # Determine XML namespace prefix if used
        if record_elem.tag.startswith("{"):
            ns = record_elem.tag.split("}")[0] + "}"
            subfield_tag = f"{ns}subfield"
            datafield_tag = f"{ns}datafield"
        else:
            ns = ""
            subfield_tag = "subfield"
            datafield_tag = "datafield"

        # 1. Subject Modernization & Folding
        vocab_cfg = self.vocab_folder.config
        if fold_legacy:
            tags_to_modernize = list(vocab_cfg.field_to_vocabularies.keys())
            if not tags_to_modernize:
                tags_to_modernize = list(get_subject_field_category_map().keys())
            for tag in tags_to_modernize:
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
                    existing_uris = _get_subfields(field, "0")
                    source_uri = existing_uris[0] if existing_uris else None

                    target_field, target_vocab, target_uri, target_label, mapping_type, qid = (
                        self.vocab_folder.fold_subject_heading(
                            heading=heading,
                            source_vocab=vocab,
                            source_field=tag,
                            source_uri=source_uri,
                        )
                    )

                    # An unresolved heading must be left exactly as catalogued.
                    # fold_subject_heading still returns a default target vocabulary
                    # in that case, and stamping it would assert membership of a
                    # vocabulary that does not contain the term, while destroying
                    # the original $2 a cataloguer needs to correct it by hand.
                    if mapping_type == "none" and not target_uri:
                        continue

                    # If tag changed (e.g. 650 -> 655 for SLM)
                    if target_field != tag:
                        field.set("tag", target_field)

                    # Replace $a with the ontology preferred label, optionally
                    # keeping the heading as catalogued alongside it so the record
                    # retains it rather than only the subject report.
                    if target_label and a_elems:
                        original_heading = a_elems[0].text
                        if vocab_cfg.replace_heading_with_pref_label:
                            if (
                                vocab_cfg.preserve_original_heading
                                and original_heading
                                and original_heading.strip() != target_label
                            ):
                                keep_code = vocab_cfg.original_heading_subfield
                                if original_heading.strip() not in _get_subfields(
                                    field, keep_code
                                ):
                                    sub_keep = etree.SubElement(
                                        field, subfield_tag, code=keep_code
                                    )
                                    sub_keep.text = original_heading.strip()
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

                    # Inject $1 Wikidata URI if enrich is active and qid is present
                    if enrich and qid:
                        existing_subj_u1 = _get_subfields(field, "1")
                        wikidata_uri = format_wikidata_uri(qid)
                        if wikidata_uri and wikidata_uri not in existing_subj_u1 and not any(
                            e.endswith(qid) for e in existing_subj_u1
                        ):
                            sub_1 = etree.SubElement(field, subfield_tag, code="1")
                            sub_1.text = wikidata_uri

        # 2. Legacy Agent Enrichment ($0 Asteri/ISNI/VIAF and $1 Wikidata QID)
        if enrich and isinstance(result, ReconciliationResult) and result.target_record:
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
                            if toggles.inject_same_as and uri not in existing_u1:
                                sub = etree.SubElement(matched_field, subfield_tag, code="1")
                                sub.text = uri
                                existing_u1.add(uri)
                        else:
                            if uri not in existing_u0:
                                sub = etree.SubElement(matched_field, subfield_tag, code="0")
                                sub.text = uri
                                existing_u0.add(uri)

        # 3. WEMI Graph & Specialized Domain Enrichment
        # Extract WEMI resolution properties
        work_uri: Optional[str] = None
        work_title: Optional[str] = None
        wikidata_qid: Optional[str] = None
        expression_uri: Optional[str] = None
        expression_title: Optional[str] = None
        sibling_ids: list[str] = []

        if isinstance(result, WEMIMatchResult):
            wemi_res = result
        else:
            wemi_res = wemi_result

        if wemi_res:
            work_uri = wemi_res.work_uri
            expression_uri = wemi_res.expression_uri
            if wemi_res.matched_work:
                work_title = wemi_res.matched_work.title
                wikidata_qid = wemi_res.matched_work.wikidata_qid
            if wemi_res.matched_expression:
                expression_title = wemi_res.matched_expression.title

        if isinstance(result, CorpusLinkage):
            cor_link = result
        else:
            cor_link = linkage

        if cor_link:
            work_uri = cor_link.final_work_uri or work_uri
            expression_uri = cor_link.final_expression_uri or expression_uri
            if cor_link.sibling_manifestation_ids_json:
                try:
                    sibling_ids = json.loads(cor_link.sibling_manifestation_ids_json)
                except Exception:
                    pass

        if work_cluster:
            work_uri = work_cluster.canonical_work_uri or work_uri
            work_title = work_cluster.canonical_title or work_title
            wikidata_qid = work_cluster.wikidata_qid or wikidata_qid

        if expression_cluster:
            expression_uri = expression_cluster.canonical_expression_uri or expression_uri

        if not enrich:
            return record_elem

        # Title fallback from 245
        if not work_title:
            for f245 in _get_datafields(record_elem, "245"):
                a_vals = _get_subfields(f245, "a")
                if a_vals:
                    work_title = a_vals[0].strip().rstrip(" /:;.,")
                    break
            work_title = work_title or "Work"

        # Resolved predicate profiles
        predicate_profiles = toggles.predicate_profiles or DEFAULT_MARC_PREDICATE_PROFILES
        profiles = [p for p in toggles.marc_predicate_profiles if p in predicate_profiles]
        if not profiles:
            profiles = ["bibframe"] if "bibframe" in predicate_profiles else list(predicate_profiles.keys())[:1]

        # 3.1 Inject 758 Work Link
        if work_uri and toggles.inject_work_links:
            work_pred_uris = [predicate_profiles[p]["work"][0] for p in profiles]
            has_work_758 = False
            for f758 in _get_datafields(record_elem, "758"):
                u0_vals = _get_subfields(f758, "0")
                u4_vals = _get_subfields(f758, "4")
                if work_uri in u0_vals or any(pred in u4_vals for pred in work_pred_uris):
                    has_work_758 = True
                    if work_uri not in u0_vals:
                        s0 = etree.SubElement(f758, subfield_tag, code="0")
                        s0.text = work_uri
                    if (
                        toggles.inject_same_as
                        and wikidata_qid
                        and not any(wikidata_qid in u for u in _get_subfields(f758, "1"))
                    ):
                        s1 = etree.SubElement(f758, subfield_tag, code="1")
                        s1.text = format_wikidata_uri(wikidata_qid)
                    break

            if not has_work_758:
                if toggles.marc_predicate_mode == "distinct":
                    for p in profiles:
                        pred_uri, scheme = predicate_profiles[p]["work"]
                        df758 = etree.SubElement(
                            record_elem, datafield_tag, tag="758", ind1=" ", ind2=" "
                        )
                        s4 = etree.SubElement(df758, subfield_tag, code="4")
                        s4.text = pred_uri
                        si = etree.SubElement(df758, subfield_tag, code="i")
                        si.text = toggles.get_relationship_label("work_manifested", lang=record_lang)
                        sa = etree.SubElement(df758, subfield_tag, code="a")
                        sa.text = work_title
                        s0 = etree.SubElement(df758, subfield_tag, code="0")
                        s0.text = work_uri
                        if toggles.inject_same_as and wikidata_qid:
                            s1 = etree.SubElement(df758, subfield_tag, code="1")
                            s1.text = format_wikidata_uri(wikidata_qid)
                        s2 = etree.SubElement(df758, subfield_tag, code="2")
                        s2.text = scheme
                else:  # stacked
                    df758 = etree.SubElement(
                        record_elem, datafield_tag, tag="758", ind1=" ", ind2=" "
                    )
                    seen_preds: set[str] = set()
                    for p in profiles:
                        pred_uri, _ = predicate_profiles[p]["work"]
                        if pred_uri not in seen_preds:
                            s4 = etree.SubElement(df758, subfield_tag, code="4")
                            s4.text = pred_uri
                            seen_preds.add(pred_uri)
                    si = etree.SubElement(df758, subfield_tag, code="i")
                    si.text = toggles.get_relationship_label("work_manifested", lang=record_lang)
                    sa = etree.SubElement(df758, subfield_tag, code="a")
                    sa.text = work_title
                    s0 = etree.SubElement(df758, subfield_tag, code="0")
                    s0.text = work_uri
                    if toggles.inject_same_as and wikidata_qid:
                        s1 = etree.SubElement(df758, subfield_tag, code="1")
                        s1.text = format_wikidata_uri(wikidata_qid)
                    s2 = etree.SubElement(df758, subfield_tag, code="2")
                    s2.text = predicate_profiles[profiles[0]]["work"][1]

        # 3.2 Inject 758 Expression Link
        if expression_uri and toggles.inject_expression_links:
            exp_pred_uris = [predicate_profiles[p]["expression"][0] for p in profiles]
            has_exp_758 = False
            for f758 in _get_datafields(record_elem, "758"):
                u0_vals = _get_subfields(f758, "0")
                u4_vals = _get_subfields(f758, "4")
                if expression_uri in u0_vals or any(pred in u4_vals for pred in exp_pred_uris):
                    has_exp_758 = True
                    if expression_uri not in u0_vals:
                        s0 = etree.SubElement(f758, subfield_tag, code="0")
                        s0.text = expression_uri
                    break

            if not has_exp_758:
                if toggles.marc_predicate_mode == "distinct":
                    for p in profiles:
                        pred_uri, scheme = predicate_profiles[p]["expression"]
                        df758 = etree.SubElement(
                            record_elem, datafield_tag, tag="758", ind1=" ", ind2=" "
                        )
                        s4 = etree.SubElement(df758, subfield_tag, code="4")
                        s4.text = pred_uri
                        si = etree.SubElement(df758, subfield_tag, code="i")
                        si.text = toggles.get_relationship_label("expression_manifested", lang=record_lang)
                        sa = etree.SubElement(df758, subfield_tag, code="a")
                        sa.text = expression_title or work_title
                        s0 = etree.SubElement(df758, subfield_tag, code="0")
                        s0.text = expression_uri
                        s2 = etree.SubElement(df758, subfield_tag, code="2")
                        s2.text = scheme
                else:  # stacked
                    df758 = etree.SubElement(
                        record_elem, datafield_tag, tag="758", ind1=" ", ind2=" "
                    )
                    seen_preds: set[str] = set()
                    for p in profiles:
                        pred_uri, _ = predicate_profiles[p]["expression"]
                        if pred_uri not in seen_preds:
                            s4 = etree.SubElement(df758, subfield_tag, code="4")
                            s4.text = pred_uri
                            seen_preds.add(pred_uri)
                    si = etree.SubElement(df758, subfield_tag, code="i")
                    si.text = toggles.get_relationship_label("expression_manifested", lang=record_lang)
                    sa = etree.SubElement(df758, subfield_tag, code="a")
                    sa.text = expression_title or work_title
                    s0 = etree.SubElement(df758, subfield_tag, code="0")
                    s0.text = expression_uri
                    s2 = etree.SubElement(df758, subfield_tag, code="2")
                    s2.text = predicate_profiles[profiles[0]]["expression"][1]

        # 3.3 Enrich 240 Uniform Title
        if work_uri and toggles.inject_work_links:
            f240_list = _get_datafields(record_elem, "240")
            for f240 in f240_list:
                existing_u0 = _get_subfields(f240, "0")
                if work_uri not in existing_u0:
                    s0 = etree.SubElement(f240, subfield_tag, code="0")
                    s0.text = work_uri
                if (
                    toggles.inject_same_as
                    and wikidata_qid
                    and not any(wikidata_qid in u for u in _get_subfields(f240, "1"))
                ):
                    s1 = etree.SubElement(f240, subfield_tag, code="1")
                    s1.text = format_wikidata_uri(wikidata_qid)

            if not f240_list and target_record and target_record.uniform_title:
                df240 = etree.SubElement(record_elem, datafield_tag, tag="240", ind1="1", ind2="0")
                sa = etree.SubElement(df240, subfield_tag, code="a")
                sa.text = target_record.uniform_title
                s0 = etree.SubElement(df240, subfield_tag, code="0")
                s0.text = work_uri
                if toggles.inject_same_as and wikidata_qid:
                    s1 = etree.SubElement(df240, subfield_tag, code="1")
                    s1.text = format_wikidata_uri(wikidata_qid)

        # 3.4 Enrich 773 Host Item / Citation Linking
        if (
            toggles.inject_component_links
            and target_record
            and (target_record.is_component or target_record.host_citation_key)
        ):
            f773_list = _get_datafields(record_elem, "773")
            for f773 in f773_list:
                existing_w = _get_subfields(f773, "w")
                if (
                    target_record.host_citation_key
                    and target_record.host_citation_key not in existing_w
                ):
                    sw = etree.SubElement(f773, subfield_tag, code="w")
                    sw.text = target_record.host_citation_key
            if not f773_list and target_record.host_citation_key:
                df773 = etree.SubElement(record_elem, datafield_tag, tag="773", ind1="0", ind2="8")
                si = etree.SubElement(df773, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("host_item", lang=record_lang)
                sw = etree.SubElement(df773, subfield_tag, code="w")
                sw.text = target_record.host_citation_key

        # 3.5 Enrich 775 Sibling Manifestation & 776 Reproduction / Digital Twin
        for sib_id in sibling_ids:
            has_sib = any(
                sib_id in _get_subfields(f, "w") for f in _get_datafields(record_elem, "775")
            )
            if not has_sib:
                df775 = etree.SubElement(record_elem, datafield_tag, tag="775", ind1="0", ind2="8")
                si = etree.SubElement(df775, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("other_manifestation", lang=record_lang)
                sw = etree.SubElement(df775, subfield_tag, code="w")
                sw.text = sib_id

        if target_record and (
            target_record.work_type in ("tactile", "heritage_model")
            or any(kw in (target_record.title_stem or "").lower() for kw in toggles.surrogate_title_keywords)
        ):
            if work_uri and not any(
                work_uri in _get_subfields(f, "w") for f in _get_datafields(record_elem, "776")
            ):
                df776 = etree.SubElement(record_elem, datafield_tag, tag="776", ind1="0", ind2="8")
                si = etree.SubElement(df776, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("reproduction", lang=record_lang)
                sw = etree.SubElement(df776, subfield_tag, code="w")
                sw.text = work_uri
                s0 = etree.SubElement(df776, subfield_tag, code="0")
                s0.text = work_uri

        # 3.6 Enrich 780 / 785 Serial & Standard Lineage
        if target_record and target_record.series_key:
            for f780 in _get_datafields(record_elem, "780"):
                if not _get_subfields(f780, "0") and work_uri:
                    s0 = etree.SubElement(f780, subfield_tag, code="0")
                    s0.text = work_uri
            for f785 in _get_datafields(record_elem, "785"):
                if not _get_subfields(f785, "0") and work_uri:
                    s0 = etree.SubElement(f785, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.7 Enrich 830 Series Super-Work Entry
        if target_record and target_record.series_key:
            for f830 in _get_datafields(record_elem, "830"):
                if not _get_subfields(f830, "0"):
                    s0 = etree.SubElement(f830, subfield_tag, code="0")
                    s0.text = concept_id_to_uri(f"series:{target_record.series_key}")

        # 3.8 Enrich 770 / 772 Board Game Dependencies
        if target_record and target_record.board_game_key:
            for f770 in _get_datafields(record_elem, "770"):
                if not _get_subfields(f770, "0") and work_uri:
                    s0 = etree.SubElement(f770, subfield_tag, code="0")
                    s0.text = work_uri
            for f772 in _get_datafields(record_elem, "772"):
                if not _get_subfields(f772, "0") and work_uri:
                    s0 = etree.SubElement(f772, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.9 Enrich 026 Early Print Fingerprint & 341/532 Accessibility
        fp_scheme = toggles.get_scheme("fingerprint")
        if target_record and target_record.fingerprint_key:
            has_fp = any(
                c in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "026")
                for c in fp_scheme.accepted
            )
            if not has_fp:
                df026 = etree.SubElement(record_elem, datafield_tag, tag="026", ind1=" ", ind2=" ")
                se = etree.SubElement(df026, subfield_tag, code="e")
                se.text = target_record.fingerprint_key
                s2 = etree.SubElement(df026, subfield_tag, code="2")
                s2.text = fp_scheme.default

        acc_tactile = toggles.get_accessibility("tactile", lang=record_lang)
        if target_record and target_record.work_type == "tactile":
            if not _get_datafields(record_elem, "341"):
                df341 = etree.SubElement(record_elem, datafield_tag, tag="341", ind1="0", ind2=" ")
                sa = etree.SubElement(df341, subfield_tag, code="a")
                sa.text = acc_tactile["content_summary"]
                sb = etree.SubElement(df341, subfield_tag, code="b")
                sb.text = acc_tactile["mode"]
            if not _get_datafields(record_elem, "532"):
                df532 = etree.SubElement(record_elem, datafield_tag, tag="532", ind1="1", ind2=" ")
                sa = etree.SubElement(df532, subfield_tag, code="a")
                sa.text = acc_tactile["note"]

        # 3.10 Enrich 777 / 501 Sammelband Composite Volume Links
        if target_record and target_record.sammelband_item_id:
            if not _get_datafields(record_elem, "777"):
                df777 = etree.SubElement(record_elem, datafield_tag, tag="777", ind1="0", ind2="8")
                si = etree.SubElement(df777, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("bound_with", lang=record_lang)
                so = etree.SubElement(df777, subfield_tag, code="o")
                so.text = target_record.sammelband_item_id

        # 3.11 Enrich 710 $4 dgg Degree Granting Institution (Theses)
        if target_record and target_record.work_type == "thesis":
            thesis_relator_code = "dgg"
            for f710 in _get_datafields(record_elem, "710"):
                if thesis_relator_code in _get_subfields(f710, "4"):
                    if not _get_subfields(f710, "0"):
                        s0 = etree.SubElement(f710, subfield_tag, code="0")
                        s0.text = toggles.thesis_institution_uri

        # 3.12 Enrich Analytical 700 $t / 711 $t Constituent Works
        if wemi_res and wemi_res.constituent_matches:
            for match in wemi_res.constituent_matches:
                for f700 in _get_datafields(record_elem, "700") + _get_datafields(
                    record_elem, "711"
                ):
                    t_vals = _get_subfields(f700, "t")
                    if any(match.title.lower() in t.lower() for t in t_vals):
                        existing_u0 = _get_subfields(f700, "0")
                        if match.work_uri and match.work_uri not in existing_u0:
                            s0 = etree.SubElement(f700, subfield_tag, code="0")
                            s0.text = match.work_uri

        # 3.13 Enrich Patent 013
        if target_record and target_record.patent_key:
            f013_list = _get_datafields(record_elem, "013")
            if not f013_list:
                df013 = etree.SubElement(record_elem, datafield_tag, tag="013", ind1=" ", ind2=" ")
                sa = etree.SubElement(df013, subfield_tag, code="a")
                sa.text = target_record.patent_key
                if work_uri:
                    s0 = etree.SubElement(df013, subfield_tag, code="0")
                    s0.text = work_uri
            else:
                for f013 in f013_list:
                    if work_uri and work_uri not in _get_subfields(f013, "0"):
                        s0 = etree.SubElement(f013, subfield_tag, code="0")
                        s0.text = work_uri

        # 3.14 Enrich Web Harvest 024 7
        web_scheme = toggles.get_scheme("web_harvest")
        if target_record and target_record.web_seed_key:
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in web_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.web_seed_key
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = web_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.15 Enrich Medieval Manuscript Fragment 024 7
        frag_scheme = toggles.get_scheme("manuscript_fragment")
        if target_record and target_record.fragment_codex_key:
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in frag_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.fragment_codex_key
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = frag_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.16 Enrich Ephemera Corporate Authority 655 #7
        if target_record and target_record.ephemera_corp_key:
            ephemera_terms: set[str] = {"pienpainatteet", "småtryck", "accidenstryck", "tilapäispainatteet", "ephemera"}
            if self.vocab_folder and self.vocab_folder.vocab_store:
                e_concept_id = toggles.domain_concepts.get("ephemera", "slm:s1492")
                c = self.vocab_folder.vocab_store.find_concept_by_uri(e_concept_id)
                if c:
                    for lbl in c.pref_labels.values():
                        ephemera_terms.add(lbl.lower().strip())
                    for alt_list in c.alt_labels.values():
                        for alt in alt_list:
                            ephemera_terms.add(str(alt).lower().strip())

            genre_field = get_category_default_field_map()["genre_form"]
            for f655 in _get_datafields(record_elem, genre_field):
                if any(any(term in a.lower() for term in ephemera_terms) for a in _get_subfields(f655, "a")):
                    if work_uri and work_uri not in _get_subfields(f655, "0"):
                        s0 = etree.SubElement(f655, subfield_tag, code="0")
                        s0.text = work_uri

        # 3.17 Enrich Exhibition Events 111 / 711
        if target_record and target_record.exhibition_event_key:
            for f111 in _get_datafields(record_elem, "111") + _get_datafields(record_elem, "711"):
                if work_uri and work_uri not in _get_subfields(f111, "0"):
                    s0 = etree.SubElement(f111, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.18 Enrich Geodata 084 $2 epsg
        geo_scheme = toggles.get_scheme("geodata_spatial")
        if target_record and target_record.geodata_spatial_key:
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "084")
                for k in geo_scheme.accepted
            ):
                df084 = etree.SubElement(record_elem, datafield_tag, tag="084", ind1=" ", ind2=" ")
                sa = etree.SubElement(df084, subfield_tag, code="a")
                sa.text = target_record.geodata_spatial_key
                s2 = etree.SubElement(df084, subfield_tag, code="2")
                s2.text = geo_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df084, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.19 Enrich Numismatics 024 7 $2 km
        numis_scheme = toggles.get_scheme("numismatics")
        if target_record and target_record.numismatic_issue_key:
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in numis_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.numismatic_issue_key
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = numis_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        return record_elem

    def enrich_wemi_record_tree(
        self,
        record_elem: etree._Element,
        wemi_result: Optional[WEMIMatchResult] = None,
        linkage: Optional[CorpusLinkage] = None,
        work_cluster: Optional[WorkCluster] = None,
        expression_cluster: Optional[ExpressionCluster] = None,
        manifestation_cluster: Optional[ManifestationCluster] = None,
        target_record: Optional[TargetRecord] = None,
        enrich: bool = True,
        fold_legacy: bool = True,
        enrichment_config: Optional[EnrichmentTogglesConfig] = None,
    ) -> etree._Element:
        """Convenience method explicitly for WEMI graph and cluster enrichment."""
        return self.enrich_record_tree(
            record_elem=record_elem,
            result=wemi_result or linkage,
            enrich=enrich,
            fold_legacy=fold_legacy,
            wemi_result=wemi_result,
            linkage=linkage,
            work_cluster=work_cluster,
            expression_cluster=expression_cluster,
            manifestation_cluster=manifestation_cluster,
            target_record=target_record,
            enrichment_config=enrichment_config,
        )
