"""WEMI graph, work/expression links, and specialized domain linkages enrichment."""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from reconciliator.config import (
    DEFAULT_MARC_PREDICATE_PROFILES,
    EnrichmentConfig,
    get_category_default_field_map,
)
from reconciliator.loaders.marcxml import (
    _get_datafields,
    _get_subfields,
)
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    WorkCluster,
)
from reconciliator.reconciliation import WEMIMatchResult
from reconciliator.utils.curie import (
    extract_wikidata_qid,
    to_actionable_uri,
)
from reconciliator.vocab import VocabFolder, concept_id_to_uri, format_wikidata_uri


class LinkageEnricher:
    """Handles WEMI 758 work/expression links, uniform title 240, component 773, sibling 775/776,
    and specialized domain MARC link fields (013, 024, 026, 084, 501, 532, 655, 710, 770, 772, 777, 780, 785, 830).
    """

    def __init__(self, vocab_folder: Optional[VocabFolder] = None) -> None:
        self.vocab_folder = vocab_folder

    def enrich(
        self,
        record_elem: etree._Element,
        result: Any,
        wemi_result: Optional[WEMIMatchResult],
        linkage: Optional[CorpusLinkage],
        work_cluster: Optional[WorkCluster],
        expression_cluster: Optional[ExpressionCluster],
        manifestation_cluster: Optional[ManifestationCluster],
        target_record: Optional[MatchRecord],
        sibling_ids: Optional[list[str]],
        constituent_linkages: Optional[list[Any]],
        record_lang: str,
        ns: str,
        subfield_tag: str,
        datafield_tag: str,
        toggles: EnrichmentConfig,
        enrich: bool = True,
    ) -> None:
        """Enrich WEMI graph and specialized domain linkages in-place."""
        # Extract WEMI resolution properties
        work_uri: Optional[str] = None
        work_title: Optional[str] = None
        wikidata_qid: Optional[str] = None
        expression_uri: Optional[str] = None
        expression_title: Optional[str] = None
        sibling_ids_resolved: list[str] = list(sibling_ids or [])

        if isinstance(result, WEMIMatchResult):
            wemi_res = result
        else:
            wemi_res = wemi_result

        if wemi_res:
            work_uri = wemi_res.work_uri
            expression_uri = wemi_res.expression_uri
            if wemi_res.matched_work:
                work_title = wemi_res.matched_work.title
                if wemi_res.matched_work.source == "wikidata":
                    wikidata_qid = wemi_res.matched_work.source_id
                elif getattr(wemi_res.matched_work, "wikidata_qid", None):
                    wikidata_qid = wemi_res.matched_work.wikidata_qid
                elif wemi_res.matched_work.curie:
                    wikidata_qid = extract_wikidata_qid(wemi_res.matched_work.curie)
            if wemi_res.matched_expression:
                expression_title = wemi_res.matched_expression.title

        if isinstance(result, CorpusLinkage):
            cor_link = result
        else:
            cor_link = linkage

        if cor_link:
            work_uri = cor_link.final_work_uri or work_uri
            expression_uri = cor_link.final_expression_uri or expression_uri

        if work_cluster:
            work_uri = work_cluster.canonical_work_uri or work_uri
            work_title = work_cluster.canonical_title or work_title
            if not wikidata_qid and work_cluster.canonical_work_curie:
                wikidata_qid = extract_wikidata_qid(work_cluster.canonical_work_curie)

        if not wikidata_qid and work_uri:
            wikidata_qid = extract_wikidata_qid(work_uri)

        if expression_cluster:
            expression_uri = expression_cluster.canonical_expression_uri or expression_uri

        if not enrich:
            return

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
        profiles = [p for p in toggles.active_profiles if p in predicate_profiles]
        if not profiles:
            profiles = (
                ["bibframe"]
                if "bibframe" in predicate_profiles
                else list(predicate_profiles.keys())[:1]
            )

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
                        si.text = toggles.get_relationship_label(
                            "work_manifested", lang=record_lang
                        )
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
                        si.text = toggles.get_relationship_label(
                            "expression_manifested", lang=record_lang
                        )
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
                    seen_preds = set()
                    for p in profiles:
                        pred_uri, _ = predicate_profiles[p]["expression"]
                        if pred_uri not in seen_preds:
                            s4 = etree.SubElement(df758, subfield_tag, code="4")
                            s4.text = pred_uri
                            seen_preds.add(pred_uri)
                    si = etree.SubElement(df758, subfield_tag, code="i")
                    si.text = toggles.get_relationship_label(
                        "expression_manifested", lang=record_lang
                    )
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
            and (target_record.is_component or target_record.get_first_key("citation"))
        ):
            f773_list = _get_datafields(record_elem, "773")
            for f773 in f773_list:
                existing_w = _get_subfields(f773, "w")
                if (
                    target_record.get_first_key("citation")
                    and target_record.get_first_key("citation") not in existing_w
                ):
                    sw = etree.SubElement(f773, subfield_tag, code="w")
                    sw.text = target_record.get_first_key("citation")
            if not f773_list and target_record.get_first_key("citation"):
                df773 = etree.SubElement(record_elem, datafield_tag, tag="773", ind1="0", ind2="8")
                si = etree.SubElement(df773, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("host_item", lang=record_lang)
                sw = etree.SubElement(df773, subfield_tag, code="w")
                sw.text = target_record.get_first_key("citation")

        # 3.5 Enrich 775 Sibling Manifestation & 776 Reproduction / Digital Twin
        for sib_id in sibling_ids_resolved:
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
            or any(
                kw in (target_record.title_stem or "").lower()
                for kw in toggles.surrogate_title_keywords
            )
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
        if target_record and target_record.get_first_key("series"):
            for f780 in _get_datafields(record_elem, "780"):
                if not _get_subfields(f780, "0") and work_uri:
                    s0 = etree.SubElement(f780, subfield_tag, code="0")
                    s0.text = work_uri
            for f785 in _get_datafields(record_elem, "785"):
                if not _get_subfields(f785, "0") and work_uri:
                    s0 = etree.SubElement(f785, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.7 Enrich 830 Series Super-Work Entry
        if target_record and target_record.get_first_key("series"):
            for f830 in _get_datafields(record_elem, "830"):
                if not _get_subfields(f830, "0"):
                    s0 = etree.SubElement(f830, subfield_tag, code="0")
                    s0.text = concept_id_to_uri(f"series:{target_record.get_first_key('series')}")

        # 3.8 Enrich 770 / 772 Board Game Dependencies
        if target_record and target_record.get_first_key("board_game"):
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
        if target_record and target_record.get_first_key("fingerprint"):
            has_fp = any(
                c in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "026")
                for c in fp_scheme.accepted
            )
            if not has_fp:
                df026 = etree.SubElement(record_elem, datafield_tag, tag="026", ind1=" ", ind2=" ")
                se = etree.SubElement(df026, subfield_tag, code="e")
                se.text = target_record.get_first_key("fingerprint")
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
        if target_record and target_record.get_first_key("sammelband"):
            if not _get_datafields(record_elem, "777"):
                df777 = etree.SubElement(record_elem, datafield_tag, tag="777", ind1="0", ind2="8")
                si = etree.SubElement(df777, subfield_tag, code="i")
                si.text = toggles.get_relationship_label("bound_with", lang=record_lang)
                so = etree.SubElement(df777, subfield_tag, code="o")
                so.text = target_record.get_first_key("sammelband")

        # 3.11 Enrich 710 $4 dgg Degree Granting Institution (Theses)
        if target_record and target_record.work_type == "thesis":
            thesis_relator_code = toggles.thesis_relator_code
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
        if target_record and target_record.get_first_key("patent"):
            f013_list = _get_datafields(record_elem, "013")
            if not f013_list:
                df013 = etree.SubElement(record_elem, datafield_tag, tag="013", ind1=" ", ind2=" ")
                sa = etree.SubElement(df013, subfield_tag, code="a")
                sa.text = target_record.get_first_key("patent")
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
        if target_record and target_record.get_first_key("web_seed"):
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in web_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.get_first_key("web_seed")
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = web_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.15 Enrich Medieval Manuscript Fragment 024 7
        frag_scheme = toggles.get_scheme("manuscript_fragment")
        if target_record and target_record.get_first_key("fragment_codex"):
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in frag_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.get_first_key("fragment_codex")
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = frag_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.16 Enrich Ephemera Corporate Authority 655 #7
        if target_record and target_record.get_first_key("ephemera_corp"):
            ephemera_terms: set[str] = set(toggles.ephemera_terms)
            if self.vocab_folder and self.vocab_folder.vocab_store:
                e_concept_id = toggles.domain_concepts.get("ephemera")
                if e_concept_id:
                    c = self.vocab_folder.vocab_store.find_concept_by_uri(e_concept_id)
                    if c:
                        for lbl in c.pref_labels.values():
                            ephemera_terms.add(lbl.lower().strip())
                        for alt_list in c.alt_labels.values():
                            for alt in alt_list:
                                ephemera_terms.add(str(alt).lower().strip())

            genre_field = get_category_default_field_map()["genre_form"]
            for f655 in _get_datafields(record_elem, genre_field):
                if any(
                    any(term in a.lower() for term in ephemera_terms)
                    for a in _get_subfields(f655, "a")
                ):
                    if work_uri and work_uri not in _get_subfields(f655, "0"):
                        s0 = etree.SubElement(f655, subfield_tag, code="0")
                        s0.text = work_uri

        # 3.17 Enrich Exhibition Events 111 / 711
        if target_record and target_record.get_first_key("exhibition_event"):
            for f111 in _get_datafields(record_elem, "111") + _get_datafields(record_elem, "711"):
                if work_uri and work_uri not in _get_subfields(f111, "0"):
                    s0 = etree.SubElement(f111, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.18 Enrich Geodata 084 $2 epsg
        geo_scheme = toggles.get_scheme("geodata_spatial")
        if target_record and target_record.get_first_key("geodata_spatial"):
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "084")
                for k in geo_scheme.accepted
            ):
                df084 = etree.SubElement(record_elem, datafield_tag, tag="084", ind1=" ", ind2=" ")
                sa = etree.SubElement(df084, subfield_tag, code="a")
                sa.text = target_record.get_first_key("geodata_spatial")
                s2 = etree.SubElement(df084, subfield_tag, code="2")
                s2.text = geo_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df084, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.19 Enrich Numismatics 024 7 $2 km
        numis_scheme = toggles.get_scheme("numismatics")
        if target_record and target_record.get_first_key("numismatic_issue"):
            if not any(
                k in _get_subfields(f, "2")
                for f in _get_datafields(record_elem, "024")
                for k in numis_scheme.accepted
            ):
                df024 = etree.SubElement(record_elem, datafield_tag, tag="024", ind1="7", ind2=" ")
                sa = etree.SubElement(df024, subfield_tag, code="a")
                sa.text = target_record.get_first_key("numismatic_issue")
                s2 = etree.SubElement(df024, subfield_tag, code="2")
                s2.text = numis_scheme.default
                if work_uri:
                    s0 = etree.SubElement(df024, subfield_tag, code="0")
                    s0.text = work_uri

        # 3.20 Enrich Constituent Works from Aggregate Linkages (730, 700, 740)
        if constituent_linkages:
            for c_link in constituent_linkages:
                uri = getattr(c_link, "final_work_uri", None) or (
                    f"reconciliator:{c_link.constituent_work_cluster_id}"
                    if getattr(c_link, "constituent_work_cluster_id", None)
                    else None
                )
                if not uri:
                    continue
                actionable_uri = to_actionable_uri(uri)
                target_tags = (
                    [c_link.field_tag]
                    if getattr(c_link, "field_tag", None) and str(c_link.field_tag).isdigit()
                    else ["730", "700", "740"]
                )
                matched_field = None
                for tag in target_tags:
                    for field in _get_datafields(record_elem, tag):
                        a_elems = field.findall(f".//{subfield_tag}[@code='a']") or field.findall(
                            ".//subfield[@code='a']"
                        )
                        t_elems = field.findall(f".//{subfield_tag}[@code='t']") or field.findall(
                            ".//subfield[@code='t']"
                        )
                        field_title = (
                            (t_elems[0].text or "").strip()
                            if t_elems and t_elems[0].text
                            else (
                                (a_elems[0].text or "").strip()
                                if a_elems and a_elems[0].text
                                else ""
                            )
                        )
                        if field_title and (
                            field_title.lower() in c_link.title.lower()
                            or c_link.title.lower() in field_title.lower()
                        ):
                            matched_field = field
                            break
                    if matched_field is not None:
                        break

                if matched_field is not None:
                    existing_0 = {
                        (sub.text or "").strip()
                        for sub in (
                            matched_field.findall(f".//{subfield_tag}[@code='0']")
                            or matched_field.findall(".//subfield[@code='0']")
                        )
                    }
                    if actionable_uri not in existing_0:
                        sub = etree.SubElement(matched_field, subfield_tag, code="0")
                        sub.text = actionable_uri
