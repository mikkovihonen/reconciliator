"""Offline multi-tier reconciliation engine combining exact lookup, dense FAISS, and sparse reranking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from lxml import etree
from rdflib import RDF, Graph

from reconciliator.agent_transfer import AgentAligner, AgentFallbackResolver
from reconciliator.config import ReconcileConfig, ReconcileFTSFallbackConfig
from reconciliator.embedder import SentenceEmbedder
from reconciliator.indexer.base import VectorIndex
from reconciliator.models import (
    AgentURI,
    BibliographicRecord,
    ExpressionEntity,
    ManifestationEntity,
    ReconciliationMatch,
    TargetRecord,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.repository import AuthorityStore, WEMIRepository
from reconciliator.rerank import CandidateEvaluator
from reconciliator.vocabularies import (
    VocabularyManager,
    format_wikidata_uri,
    normalize_subject_vocabulary,
)


@dataclass
class ReconciliationResult:
    """Consolidated reconciliation result for a single target record."""

    target_record: BibliographicRecord
    matches: list[ReconciliationMatch] = field(default_factory=list)
    accepted_candidates: list[BibliographicRecord] = field(default_factory=list)
    discovered_agent_uris: dict[int, list[AgentURI]] = field(default_factory=dict)
    selected_agent_uris: dict[int, list[str]] = field(default_factory=dict)
    suppressed_agent_uris: dict[int, list[str]] = field(default_factory=dict)


class ReconciliationEngine:
    """Orchestrates multi-tier offline reconciliation against local authority storage."""

    def __init__(
        self,
        authority_store: AuthorityStore,
        embedder: SentenceEmbedder,
        config: ReconcileConfig,
        agent_indexer: Optional[VectorIndex] = None,
    ) -> None:
        self.authority_store = authority_store
        self.embedder = embedder
        self.config = config
        self.agent_indexer = agent_indexer

        self.evaluator = CandidateEvaluator(config.rerank)
        self.vocab_manager = VocabularyManager(
            allowed_vocabularies=config.vocabularies,
            agent_priority=config.agent_priority,
            subject_priority=config.subject_priority,
            link_policy=config.link_policy,
            vocabulary_patterns=config.vocabulary_patterns,
            entity_vocabularies=config.entity_vocabularies,
            supported_subject_vocabularies=config.supported_subject_vocabularies,
        )
        self.agent_aligner = AgentAligner()
        self.agent_fallback_resolver = AgentFallbackResolver(
            similarity_threshold=config.agent_fallback_threshold,
            verify_chronology=config.verify_chronology,
            min_author_age=config.chronology_min_author_age,
            max_posthumous_years=config.chronology_max_posthumous_years,
            strict_homonym_guard=config.strict_homonym_guard,
        )

    def reconcile_record(self, target_record: BibliographicRecord) -> ReconciliationResult:
        """Run complete multi-tier matching funnel on target record."""
        matches: list[ReconciliationMatch] = []
        accepted_candidates: list[BibliographicRecord] = []
        seen_cand_ids: set[str] = set()

        # Step 2.2: Tier 1 Exact ID Lookup
        exact_candidates = self.authority_store.find_exact_matches(target_record.identifiers)
        for cand in exact_candidates:
            if cand.source_id not in seen_cand_ids:
                seen_cand_ids.add(cand.source_id)
                match = ReconciliationMatch(
                    candidate_record_id=cand.source_id,
                    source=cand.source,
                    match_tier="tier1_exact_id",
                    match_score=1.0,
                    dense_score=None,
                    rerank_score=None,
                    volume_match=True,
                    surname_match=True,
                    year_diff=None,
                    accepted=True,
                )
                matches.append(match)
                accepted_candidates.append(cand)

        # Step 2.3: Tier 2 Dense Retrieval & Step 2.4: Pluggable Reranking
        dense_candidates = []
        if self.authority_store.indexer.total_vectors > 0:
            query_vector = self.embedder.embed_records([target_record])
            dense_candidates = self.authority_store.search_vector_candidates(
                query_vector, top_k=self.config.rerank.top_k
            )

        for cand_record, dense_score in dense_candidates:
            if cand_record.source_id in seen_cand_ids:
                continue
            seen_cand_ids.add(cand_record.source_id)

            match = self.evaluator.evaluate_candidate(
                target_record=target_record,
                candidate_record=cand_record,
                dense_score=dense_score,
                acceptance_threshold=self.config.threshold,
            )
            matches.append(match)
            if match.accepted:
                accepted_candidates.append(cand_record)

        # Step 2.5: Coupled Work Matching Agent Alignment & Multi-Candidate URI Aggregation
        discovered_uris = self.agent_aligner.align_target_agents(
            target_record.agents, accepted_candidates
        )

        # Step 2.55: Standalone Agent Fallback (Tier 3) for unlinked target agents
        if self.config.fallback_agent_reconciliation and self.agent_indexer is not None:
            import json
            from reconciliator.template import WEMISerializationEngine
            from reconciliator.vocabularies import detect_vocabulary

            wemi_engine = WEMISerializationEngine()
            for idx, target_agent in enumerate(target_record.agents):
                # Skip if already linked via Tier 1/2 coupled matching or fictional
                if discovered_uris.get(idx) or target_agent.is_fictional:
                    continue

                # Essential representation for dense query vector (~30-50 tokens)
                agent_text = wemi_engine.render_agent(target_agent)
                if not agent_text or not agent_text.strip():
                    continue

                q_vec = self.embedder.embed_texts([agent_text])
                cand_scores, cand_ids = self.agent_indexer.search(q_vec, top_k=5)
                if len(cand_ids) == 0 or len(cand_ids[0]) == 0:
                    continue

                agent_id_list = [int(i) for i in cand_ids[0]]
                agent_score_list = [float(s) for s in cand_scores[0]]

                # Hydrate rich candidate profiles from SQLite
                cand_profiles = self.authority_store.get_agents_by_ids(agent_id_list)
                evaluated_cands: list[dict[str, Any]] = []
                for cid, cscore in zip(agent_id_list, agent_score_list):
                    if cid in cand_profiles:
                        profile = dict(cand_profiles[cid])
                        profile["score"] = cscore
                        evaluated_cands.append(profile)

                target_coauthors = [a.name for a in target_record.agents if a != target_agent]
                target_classifications = [(c.code, c.scheme) for c in target_record.classifications]
                target_subjects = dict(target_agent.subject_counts)
                if not target_subjects and target_record.subjects:
                    for cat in target_record.subjects.all_subjects():
                        if cat.vocabulary and cat.pref_label:
                            norm_v = normalize_subject_vocabulary(
                                cat.vocabulary,
                                uri=cat.uri,
                                supported_vocabularies=self.config.supported_subject_vocabularies,
                            )
                            if norm_v:
                                target_subjects[f"{norm_v}:{cat.pref_label.strip().lower()}"] = 1

                best_cand, reason = self.agent_fallback_resolver.evaluate_candidates(
                    target_agent=target_agent,
                    target_pub_year=target_record.publication_year,
                    candidates=evaluated_cands,
                    authority_store=self.authority_store,
                    target_coauthors=target_coauthors,
                    target_publisher=target_record.publisher,
                    target_classifications=target_classifications,
                    target_subjects=target_subjects,
                )

                if best_cand:
                    best_score = best_cand.get("score", self.config.agent_fallback_threshold)
                    existing_uris = {u.uri for u in discovered_uris[idx]}

                    # Extract primary agent URI if present
                    if best_cand.get("agent_uri"):
                        u = str(best_cand["agent_uri"])
                        if u not in existing_uris:
                            vocab = detect_vocabulary(u) or ("WIKIDATA" if "wikidata.org" in u else "OTHER")
                            subfield = "1" if "wikidata.org" in u.lower() else "0"
                            discovered_uris[idx].append(
                                AgentURI(
                                    uri=u,
                                    vocabulary=vocab,
                                    subfield_code=subfield,
                                    source_record_id=str(best_cand.get("source_id") or best_cand.get("id")),
                                    source=str(best_cand.get("source") or "agent_fallback"),
                                    alignment_score=float(best_score * 100.0),
                                )
                            )
                            existing_uris.add(u)

                    # Extract uri_subfield_0 (KANTO/ISNI/VIAF)
                    u0_raw = best_cand.get("uri_subfield_0")
                    if u0_raw:
                        try:
                            parsed_u0 = json.loads(u0_raw) if u0_raw.startswith("[") else [u0_raw]
                            for item in parsed_u0:
                                if item and item not in existing_uris:
                                    vocab = detect_vocabulary(item) or "OTHER"
                                    discovered_uris[idx].append(
                                        AgentURI(
                                            uri=item,
                                            vocabulary=vocab,
                                            subfield_code="0",
                                            source_record_id=str(best_cand.get("id")),
                                            source=str(best_cand.get("source") or "agent_fallback"),
                                            alignment_score=float(best_score * 100.0),
                                        )
                                    )
                                    existing_uris.add(item)
                        except Exception:
                            pass

                    # Extract uri_subfield_1 (Wikidata QIDs)
                    u1_raw = best_cand.get("uri_subfield_1")
                    if u1_raw:
                        try:
                            parsed_u1 = json.loads(u1_raw) if u1_raw.startswith("[") else [u1_raw]
                            for item in parsed_u1:
                                if item and item not in existing_uris:
                                    vocab = detect_vocabulary(item) or "WIKIDATA"
                                    discovered_uris[idx].append(
                                        AgentURI(
                                            uri=item,
                                            vocabulary=vocab,
                                            subfield_code="1",
                                            source_record_id=str(best_cand.get("id")),
                                            source=str(best_cand.get("source") or "agent_fallback"),
                                            alignment_score=float(best_score * 100.0),
                                        )
                                    )
                                    existing_uris.add(item)
                        except Exception:
                            pass

                    # Extract wikidata_qid
                    if best_cand.get("wikidata_qid"):
                        qid = best_cand["wikidata_qid"]
                        wiki_uri = format_wikidata_uri(qid)
                        if wiki_uri and wiki_uri not in existing_uris:
                            discovered_uris[idx].append(
                                AgentURI(
                                    uri=wiki_uri,
                                    vocabulary="WIKIDATA",
                                    subfield_code="1",
                                    source_record_id=str(best_cand.get("id")),
                                    source="wikidata",
                                    alignment_score=float(best_score * 100.0),
                                )
                            )
                            existing_uris.add(wiki_uri)

                    matches.append(
                        ReconciliationMatch(
                            candidate_record_id=str(best_cand.get("agent_uri") or best_cand.get("id")),
                            source=str(best_cand.get("source") or "agent_fallback"),
                            match_tier="tier3_agent_fallback",
                            match_score=float(best_score),
                            dense_score=float(best_score),
                            rerank_score=None,
                            volume_match=True,
                            surname_match=True,
                            year_diff=None,
                            accepted=True,
                        )
                    )

        # Step 2.6: Vocabulary Filter & Priority Selection
        selected_uris: dict[int, list[str]] = {}
        suppressed_uris: dict[int, list[str]] = {}

        for idx, uri_items in discovered_uris.items():
            raw_uri_strs = [item.uri for item in uri_items]
            sel, sup = self.vocab_manager.select_agent_uris(raw_uri_strs)
            selected_uris[idx] = sel
            suppressed_uris[idx] = sup

        return ReconciliationResult(
            target_record=target_record,
            matches=matches,
            accepted_candidates=accepted_candidates,
            discovered_agent_uris=discovered_uris,
            selected_agent_uris=selected_uris,
            suppressed_agent_uris=suppressed_uris,
        )


@dataclass
class ConstituentWorkMatch:
    """Represents a matched constituent work within an aggregate or collection."""

    title: str
    matched_work: Optional[WorkEntity] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    relationship_type: Optional[str] = None
    confidence_score: float = 1.0


@dataclass
class WEMIMatchResult:
    """Consolidated match result for a target record resolved by WorkMatcher."""

    target_record_id: str
    matched_work: Optional[WorkEntity] = None
    matched_expression: Optional[ExpressionEntity] = None
    matched_manifestation: Optional[ManifestationEntity] = None
    work_uri: Optional[str] = None
    expression_uri: Optional[str] = None
    manifestation_uri: Optional[str] = None
    match_tier: str = "unmatched"
    funnel_used: str = "unknown"
    confidence_score: float = 0.0
    constituent_matches: list[ConstituentWorkMatch] = field(default_factory=list)
    relationships: list[WorkRelationship] = field(default_factory=list)


def compute_bounding_box_iou(
    bbox1: tuple[float, float, float, float],
    bbox2: tuple[float, float, float, float],
) -> float:
    """Compute Intersection over Union (IoU) of two bounding boxes (west, east, north, south)."""
    w1, e1, n1, s1 = bbox1
    w2, e2, n2, s2 = bbox2

    w1, e1 = min(w1, e1), max(w1, e1)
    w2, e2 = min(w2, e2), max(w2, e2)
    s1, n1 = min(s1, n1), max(s1, n1)
    s2, n2 = min(s2, n2), max(s2, n2)

    inter_w = max(w1, w2)
    inter_e = min(e1, e2)
    inter_s = max(s1, s2)
    inter_n = min(n1, n2)

    if inter_w < inter_e and inter_s < inter_n:
        inter_area = (inter_e - inter_w) * (inter_n - inter_s)
    else:
        inter_area = 0.0

    area1 = (e1 - w1) * (n1 - s1)
    area2 = (e2 - w2) * (n2 - s2)
    union_area = area1 + area2 - inter_area

    if union_area <= 0.0:
        return 0.0
    return inter_area / union_area


def compute_jaccard_overlap(set1: set[str], set2: set[str]) -> float:
    """Compute Jaccard similarity between two sets of string tokens/titles."""
    if not set1 or not set2:
        return 0.0
    norm1 = {re.sub(r"[^0-9a-zA-Z]", "", s).lower() for s in set1 if s}
    norm2 = {re.sub(r"[^0-9a-zA-Z]", "", s).lower() for s in set2 if s}
    norm1.discard("")
    norm2.discard("")
    if not norm1 or not norm2:
        return 0.0
    intersection = len(norm1 & norm2)
    union = len(norm1 | norm2)
    return intersection / union if union > 0 else 0.0


def extract_identifiers_from_target(target: TargetRecord) -> dict[str, list[str]]:
    """Extract standard identifiers from TargetRecord fields and raw payload."""
    ids: dict[str, list[str]] = {}

    def _add(scheme: str, val: Optional[str]) -> None:
        if val and val.strip():
            s = scheme.lower().strip()
            v = val.strip()
            if s not in ids:
                ids[s] = []
            if v not in ids[s]:
                ids[s].append(v)

    # From target blocking / domain keys
    if target.standard_key:
        sk = target.standard_key.strip()
        sk_clean = re.sub(r"[^0-9a-zA-Z]", "", sk)
        if sk_clean.startswith(("978", "979")) or len(sk_clean) in (10, 13):
            _add("isbn", sk)
        elif "-" in sk and len(sk) == 9:
            _add("issn", sk)
        elif sk.lower().startswith("10."):
            _add("doi", sk)
        elif sk.lower().startswith("urn:"):
            _add("urn", sk)
        elif sk.lower().startswith(("m", "9790")):
            _add("ismn", sk)
        else:
            _add("standard_number", sk)
            if target.work_type in ("music", "audio_nonmusic"):
                _add("publisher_number", sk)

    if target.series_key and "-" in target.series_key and len(target.series_key) == 9:
        _add("issn", target.series_key)

    if target.patent_key:
        _add("patent_number", target.patent_key)
        _add("patent", target.patent_key)

    if target.statute_key:
        _add("sdk", target.statute_key)
        _add("eli", target.statute_key)

    if target.swhid_key:
        _add("swhid", target.swhid_key)

    if target.fingerprint_key:
        _add("fingerprint", target.fingerprint_key)

    if target.podcast_key:
        _add("guid_rss", target.podcast_key)

    if target.board_game_key:
        _add("bgg", target.board_game_key)
        _add("ean", target.board_game_key)

    if target.fragment_codex_key:
        _add("fragmenta", target.fragment_codex_key)

    if target.numismatic_issue_key:
        _add("michel", target.numismatic_issue_key)
        _add("scott", target.numismatic_issue_key)
        _add("km", target.numismatic_issue_key)

    if target.geodata_spatial_key:
        _add("epsg", target.geodata_spatial_key)

    if target.web_seed_key:
        _add("memento_uri", target.web_seed_key)

    if target.sammelband_item_id:
        _add("barcode", target.sammelband_item_id)

    # From raw_payload if available
    if target.raw_payload:
        if target.format == "marcxml":
            try:
                root = etree.fromstring(target.raw_payload)
                for f in root.findall(".//{http://www.loc.gov/MARC21/slim}controlfield"):
                    if f.get("tag") == "001" and f.text:
                        _add("control_number", f.text)
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='020']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='a']"):
                        if sf.text:
                            _add("isbn", sf.text.split()[0])
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='022']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='a']"):
                        if sf.text:
                            _add("issn", sf.text.strip())
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='l']"):
                        if sf.text:
                            _add("issn_l", sf.text.strip())
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='024']"):
                    ind1 = df.get("ind1", "")
                    val_sf = df.find("{http://www.loc.gov/MARC21/slim}subfield[@code='a']")
                    val = val_sf.text.strip() if val_sf is not None and val_sf.text else None
                    src_sf = df.find("{http://www.loc.gov/MARC21/slim}subfield[@code='2']")
                    src = src_sf.text.strip().lower() if src_sf is not None and src_sf.text else ""
                    if val:
                        if ind1 == "0":
                            _add("isrc", val)
                        elif ind1 == "2":
                            _add("ismn", val)
                        elif ind1 == "3":
                            _add("ean", val)
                        elif src:
                            _add(src, val)
                        elif val.startswith("10."):
                            _add("doi", val)
                        elif val.lower().startswith("urn:"):
                            _add("urn", val)
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='028']"):
                    ind1 = df.get("ind1", "")
                    val_sf = df.find("{http://www.loc.gov/MARC21/slim}subfield[@code='a']")
                    val = val_sf.text.strip() if val_sf is not None and val_sf.text else None
                    if val:
                        if ind1 == "2":
                            _add("plate_number", val)
                        else:
                            _add("publisher_number", val)
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='027']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='a']"):
                        if sf.text:
                            _add("strn", sf.text.strip())
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='013']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='a']"):
                        if sf.text:
                            _add("patent_number", sf.text.strip())
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='026']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='e']"):
                        if sf.text:
                            _add("fingerprint", sf.text.strip())
                for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='776']"):
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='w']"):
                        if sf.text:
                            _add("control_number", sf.text.strip())
                    for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='z']"):
                        if sf.text:
                            _add("isbn", sf.text.split()[0])
            except Exception:
                pass
        elif target.format in ("bibframe", "turtle", "rdf", "jsonld"):
            try:
                g = Graph()
                fmt = "turtle" if target.format == "turtle" else "xml"
                g.parse(data=target.raw_payload, format=fmt)
                for _, p, o in g:
                    p_str = str(p)
                    if "identifiedBy" in p_str or "identifier" in p_str:
                        for val_node in g.objects(o, RDF.value):
                            v_str = str(val_node).strip()
                            for t in g.objects(o, RDF.type):
                                t_local = str(t).split("/")[-1].split("#")[-1].lower()
                                for cand in (
                                    "isbn",
                                    "issn",
                                    "ismn",
                                    "isrc",
                                    "doi",
                                    "urn",
                                    "barcode",
                                ):
                                    if cand in t_local:
                                        _add(cand, v_str)
            except Exception:
                pass

    return ids


def extract_target_track_titles(target: TargetRecord) -> set[str]:
    """Extract constituent track titles from TargetRecord fields or payload."""
    tracks: set[str] = set()
    if target.raw_payload and target.format == "marcxml":
        try:
            root = etree.fromstring(target.raw_payload)
            for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='505']"):
                for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='t']"):
                    if sf.text and sf.text.strip():
                        tracks.add(sf.text.strip())
            for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']"):
                for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='t']"):
                    if sf.text and sf.text.strip():
                        tracks.add(sf.text.strip())
        except Exception:
            pass
    return tracks


def extract_target_analytics(target: TargetRecord) -> list[tuple[str, Optional[str]]]:
    """Extract constituent works (title, creator) from 700 $t / 710 $t / 505."""
    analytics: list[tuple[str, Optional[str]]] = []
    if target.raw_payload and target.format == "marcxml":
        try:
            root = etree.fromstring(target.raw_payload)
            for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='700']"):
                t_sf = df.find("{http://www.loc.gov/MARC21/slim}subfield[@code='t']")
                a_sf = df.find("{http://www.loc.gov/MARC21/slim}subfield[@code='a']")
                if t_sf is not None and t_sf.text and t_sf.text.strip():
                    title = t_sf.text.strip()
                    author = a_sf.text.strip() if a_sf is not None and a_sf.text else None
                    analytics.append((title, author))
            for df in root.findall(".//{http://www.loc.gov/MARC21/slim}datafield[@tag='505']"):
                for sf in df.findall("{http://www.loc.gov/MARC21/slim}subfield[@code='t']"):
                    if sf.text and sf.text.strip():
                        analytics.append((sf.text.strip(), None))
        except Exception:
            pass
    return analytics


class WorkMatcher:
    """Multi-tier WEMI resolution and authority linking engine across 42 specialized domains."""

    def __init__(
        self,
        wemi_repo: WEMIRepository,
        embedder: Optional[SentenceEmbedder] = None,
        config: Optional[ReconcileConfig] = None,
    ) -> None:
        self.repo = wemi_repo
        self.embedder = embedder
        self.config = config
        self.agent_aligner = AgentAligner()
        self.evaluator = (
            CandidateEvaluator(self.config.rerank)
            if (self.config and hasattr(self.config, "rerank"))
            else None
        )

    # -----------------------------------------------------------------------
    # Helper Methods
    # -----------------------------------------------------------------------

    def _align_expression(
        self,
        exps: list[ExpressionEntity],
        target_language: Optional[str] = None,
        content_type: Optional[str] = None,
        translator_signature: Optional[str] = None,
    ) -> Optional[ExpressionEntity]:
        """Align expressions by language, optional content_type, and translator matching via AgentAligner."""
        if not exps:
            return None
        filtered = exps
        if content_type:
            ct_filtered = [e for e in filtered if e.content_type == content_type]
            if ct_filtered:
                filtered = ct_filtered
        if target_language:
            t_lang = target_language.strip().lower()
            lang_filtered = [
                e for e in filtered if e.language and e.language.strip().lower() == t_lang
            ]
            if lang_filtered:
                filtered = lang_filtered

        # Translator alignment via AgentAligner if translator_signature is present
        if translator_signature and len(filtered) > 1:
            norm_sig = translator_signature.strip().lower()
            best_match: Optional[ExpressionEntity] = None
            best_score = 0.0
            for e in filtered:
                candidate_names: list[str] = []
                if e.title:
                    candidate_names.append(e.title)
                if e.id and hasattr(self.repo, "get_contributions_for_entity"):
                    contribs = self.repo.get_contributions_for_entity("expression", e.id)
                    for c in contribs:
                        if hasattr(self.repo, "get_agent"):
                            agent = self.repo.get_agent(c.agent_id)
                            if agent:
                                if agent.name:
                                    candidate_names.append(agent.name)
                                if agent.surname:
                                    candidate_names.append(agent.surname)
                for c_name in candidate_names:
                    score = self.agent_aligner.align_names(norm_sig, c_name)
                    if score > best_score:
                        best_score = score
                        best_match = e
            if best_match and best_score >= 0.70:
                return best_match

        return filtered[0]

    def _try_fast_path_identifiers(
        self,
        target: TargetRecord,
        ids: dict[str, list[str]],
        funnel_name: str,
        preferred_schemes: Optional[list[str]] = None,
    ) -> Optional[WEMIMatchResult]:
        """Attempt Tier 1 standard identifier resolution."""
        schemes_order: list[str] = []
        if preferred_schemes:
            schemes_order.extend([s.lower() for s in preferred_schemes])
        for s in ids.keys():
            s_low = s.lower()
            if s_low not in schemes_order:
                schemes_order.append(s_low)

        for scheme in schemes_order:
            values = ids.get(scheme, [])
            for val in values:
                matches = self.repo.lookup_by_identifier(scheme, val)
                if not matches:
                    continue

                entity_type, entity_id = matches[0]
                if entity_type == "manifestation":
                    manif = self.repo.get_manifestation(entity_id)
                    if manif:
                        work = self.repo.get_work(manif.work_id) if manif.work_id else None
                        exps = self.repo.get_expressions_for_manifestation(manif.id)
                        if not exps and work:
                            exps = self.repo.get_expressions_for_work(work.id)
                        exp = self._align_expression(
                            exps, target.language, translator_signature=target.translators_signature
                        )
                        if not work and exp and exp.work_id:
                            work = self.repo.get_work(exp.work_id)
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            matched_manifestation=manif,
                            work_uri=work.work_uri if work else None,
                            expression_uri=exp.expression_uri if exp else None,
                            manifestation_uri=manif.manifestation_uri,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

                elif entity_type == "expression":
                    exp = self.repo.get_expression(entity_id)
                    if exp:
                        work = self.repo.get_work(exp.work_id)
                        manifs = self.repo.get_manifestations_for_expression(exp.id)
                        manif = manifs[0] if manifs else None
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            matched_manifestation=manif,
                            work_uri=work.work_uri if work else None,
                            expression_uri=exp.expression_uri,
                            manifestation_uri=manif.manifestation_uri if manif else None,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

                elif entity_type == "work":
                    work = self.repo.get_work(entity_id)
                    if work:
                        exps = self.repo.get_expressions_for_work(work.id)
                        exp = self._align_expression(
                            exps, target.language, translator_signature=target.translators_signature
                        )
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=work,
                            matched_expression=exp,
                            work_uri=work.work_uri,
                            expression_uri=exp.expression_uri if exp else None,
                            match_tier="tier1_exact_identifier",
                            funnel_used=funnel_name,
                            confidence_score=1.0,
                        )

        return None

    def _vector_or_fts_fallback(
        self,
        target: TargetRecord,
        work_type: Optional[str] = None,
        funnel_name: str = "fallback",
    ) -> Optional[WEMIMatchResult]:
        """Perform semantic vector search fallback or FTS5 lexical search."""
        # 1. Dense vector search
        if (
            self.embedder is not None
            and self.repo.work_indexer is not None
            and self.repo.work_indexer.total_vectors > 0
        ):
            query_text = target.serialized_work or f"{target.title} {target.author_key or ''}"
            query_vec = self.embedder.embed_texts([query_text])
            results = self.repo.search_work_vectors(query_vec, top_k=5)
            thresh = (
                self.config.threshold
                if hasattr(self.config, "threshold") and self.config.threshold is not None
                else 0.70
            )
            for cand_work, score in results:
                if work_type and cand_work.work_type != work_type:
                    continue
                if score >= thresh:
                    exps = self.repo.get_expressions_for_work(cand_work.id)
                    exp = self._align_expression(
                        exps, target.language, translator_signature=target.translators_signature
                    )
                    return WEMIMatchResult(
                        target_record_id=target.record_id,
                        matched_work=cand_work,
                        matched_expression=exp,
                        work_uri=cand_work.work_uri,
                        expression_uri=exp.expression_uri if exp else None,
                        match_tier="tier2_vector_fallback",
                        funnel_used=funnel_name,
                        confidence_score=float(score),
                    )

        # 2. FTS5 Lexical Search (Tier 3 Fallback)
        # Architectural distinction:
        # This fallback performs database-level candidate retrieval against SQLite FTS5 virtual tables
        # using SQLite's native C-level BM25 ranking function. It is completely independent of the
        # in-memory sparse reranking (rank-bm25 / CandidateEvaluator) performed during scoring.
        fts_cfg = (
            self.config.fts_fallback
            if (self.config and hasattr(self.config, "fts_fallback"))
            else ReconcileFTSFallbackConfig()
        )
        if fts_cfg.enabled:
            fts_query = target.title_stem or target.title
            if fts_query:
                try:
                    fts_results = self.repo.search_works_fts(fts_query, limit=fts_cfg.limit)
                    for cand_work, _rank in fts_results:
                        if work_type and cand_work.work_type != work_type:
                            continue
                        exps = self.repo.get_expressions_for_work(cand_work.id)
                        exp = self._align_expression(
                            exps, target.language, translator_signature=target.translators_signature
                        )
                        return WEMIMatchResult(
                            target_record_id=target.record_id,
                            matched_work=cand_work,
                            matched_expression=exp,
                            work_uri=cand_work.work_uri,
                            expression_uri=exp.expression_uri if exp else None,
                            match_tier="tier3_fts_fallback",
                            funnel_used=funnel_name,
                            confidence_score=fts_cfg.confidence_score,
                        )
                except Exception:
                    pass

        return None

    # -----------------------------------------------------------------------
    # Domain Funnels (A through PP)
    # -----------------------------------------------------------------------

    def match_monograph(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel A (Monographs): ISBN/URN exact -> Author+Title stem -> Vector fallback -> Expression alignment."""
        # Tier 1
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_a_monograph", ["isbn", "urn", "control_number"]
        )
        if fast:
            return fast

        # Tier 2: Author + Title stem
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(
                exps, target.language, translator_signature=target.translators_signature
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_a_monograph",
                confidence_score=0.90,
            )

        # Tier 3: Work vector fallback / FTS5
        fb = self._vector_or_fts_fallback(target, "text", "funnel_a_monograph")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_a_monograph",
        )

    def match_recorded_sound(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel B (Recorded Sound): Publisher number/ISRC/EAN exact -> Album title + performer -> Jaccard tracklist."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_b_music_sound", ["publisher_number", "isrc", "ean", "upc"]
        )
        if fast:
            return fast

        # Tier 2: Album Title + Performer Alignment
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="music"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="audio_music")
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_performer_alignment",
                funnel_used="funnel_b_music_sound",
                confidence_score=0.88,
            )

        # Tier 3: Tracklist Jaccard Overlap
        target_tracks = extract_target_track_titles(target)
        if target_tracks:
            music_works = self.repo.find_works_by_type("music", limit=50)
            for mw in music_works:
                constituent_titles = {
                    r.title for r in self.repo.find_works_by_type("music") if r.id != mw.id
                }
                if compute_jaccard_overlap(target_tracks, constituent_titles) >= 0.75:
                    exps = self.repo.get_expressions_for_work(mw.id)
                    exp = self._align_expression(exps, target.language, content_type="audio_music")
                    cm = [ConstituentWorkMatch(title=t) for t in target_tracks]
                    return WEMIMatchResult(
                        target_record_id=target.record_id,
                        matched_work=mw,
                        matched_expression=exp,
                        work_uri=mw.work_uri,
                        expression_uri=exp.expression_uri if exp else None,
                        match_tier="tier3_tracklist_jaccard",
                        funnel_used="funnel_b_music_sound",
                        confidence_score=0.85,
                        constituent_matches=cm,
                    )

        fb = self._vector_or_fts_fallback(target, "music", "funnel_b_music_sound")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_b_music_sound",
        )

    def match_article(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel C (Articles / Serial Components): DOI/URN exact -> Host Citation Key -> Host-restricted search."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_c_article", ["doi", "urn"])
        if fast:
            return fast

        # Tier 2: Host Citation Key
        if target.host_citation_key:
            cand_works = self.repo.find_works_by_author_and_title(
                target.author_key, target.title_stem
            )
            if cand_works:
                work = cand_works[0]
                exps = self.repo.get_expressions_for_work(work.id)
                exp = self._align_expression(exps, target.language)
                return WEMIMatchResult(
                    target_record_id=target.record_id,
                    matched_work=work,
                    matched_expression=exp,
                    work_uri=work.work_uri,
                    expression_uri=exp.expression_uri if exp else None,
                    match_tier="tier2_host_citation_key",
                    funnel_used="funnel_c_article",
                    confidence_score=0.95,
                )

        fb = self._vector_or_fts_fallback(target, None, "funnel_c_article")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_c_article",
        )

    def match_notated_music(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel D (Notated Music): ISMN/Plate number exact -> Music Work Key + Medium -> Score format discrimination."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_d_notated_music", ["ismn", "plate_number"]
        )
        if fast:
            return fast

        # Tier 2: Composer + Uniform Title / Music Work Key
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="music"
        )
        if cand_works:
            work = cand_works[0]
            # Medium of performance check
            mops = self.repo.get_mediums_of_performance(work.id)
            # Tier 3: Score format discrimination
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="notated_music")
            # Tier 4: Collection score analytics
            analytics = extract_target_analytics(target)
            cms = [ConstituentWorkMatch(title=t) for t, _a in analytics]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_music_signature" if mops else "tier2_domain_key",
                funnel_used="funnel_d_notated_music",
                confidence_score=0.92,
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, "music", "funnel_d_notated_music")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_d_notated_music",
        )

    def match_audiobook(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel E (Audiobooks): ISBN/Publisher number exact -> Cross-format Work resolution (same Work as print) -> Narrator alignment."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_e_audiobook", ["isbn", "publisher_number"]
        )
        if fast:
            return fast

        # Tier 2: Cross-Format Work Resolution to underlying text work
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            spoken_exps = [e for e in exps if e.content_type == "spoken_word"]
            exp = (
                self._align_expression(spoken_exps, target.language, content_type="spoken_word")
                if spoken_exps
                else None
            )
            if not exp:
                exp = ExpressionEntity(
                    id=0,
                    work_id=work.id or 0,
                    expression_uri=f"{work.work_uri}#spoken_word_{target.language or 'und'}",
                    source=work.source,
                    source_id=f"{work.source_id}_audio",
                    language=target.language or "fin",
                    content_type="spoken_word",
                    title=f"{work.title} (äänikirja)",
                )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="cross_format",
                funnel_used="funnel_e_audiobook",
                confidence_score=0.92,
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_e_audiobook")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_e_audiobook",
        )

    def match_moving_image(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel F (Moving Image): EAN/UPC exact -> Director + Title + Year -> Compilation analytics."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_f_moving_image", ["ean", "upc", "publisher_number"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="moving_image"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="moving_image")
            analytics = extract_target_analytics(target)
            cms = [ConstituentWorkMatch(title=t) for t, _a in analytics]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_f_moving_image",
                confidence_score=0.90,
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, "moving_image", "funnel_f_moving_image")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_f_moving_image",
        )

    def match_anthology(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel G (Anthologies): ISBN exact -> Editor/Compiler + Collection title -> Constituent work analytics."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_g_anthology", ["isbn"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            analytics = extract_target_analytics(target)
            cms = []
            for t, a in analytics:
                cw = self.repo.find_works_by_author_and_title(a, t, work_type="text")
                if cw:
                    cms.append(
                        ConstituentWorkMatch(
                            title=t,
                            matched_work=cw[0],
                            work_uri=cw[0].work_uri,
                            relationship_type="constituent_of",
                        )
                    )
                else:
                    cms.append(ConstituentWorkMatch(title=t, relationship_type="constituent_of"))

            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_g_anthology",
                confidence_score=0.88,
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_g_anthology")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_g_anthology",
        )

    def match_serial(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel H (Serials): ISSN/ISSN-L exact -> Title succession chain traversal (780/785) -> Corporate publisher."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_h_serial", ["issn", "issn_l"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="serial"
        )
        if cand_works:
            work = cand_works[0]
            rels = self.repo.get_work_relationships(work.id)
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_succession_chain" if rels else "tier2_domain_key",
                funnel_used="funnel_h_serial",
                confidence_score=0.90,
                relationships=rels,
            )

        fb = self._vector_or_fts_fallback(target, "serial", "funnel_h_serial")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_h_serial",
        )

    def match_series(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel I (Multi-Part Series): Series uniform title (830) / series ISSN -> Volume anchor ($v) + constituent work."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_i_series", ["issn", "series_number"]
        )
        if fast:
            return fast

        # Super-Work match
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="serial"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="series_member_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_series_anchor",
                funnel_used="funnel_i_series",
                confidence_score=0.88,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, None, "funnel_i_series")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_i_series",
        )

    def match_conference(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel J (Conference Proceedings): ISBN/DOI exact -> Meeting name/year/place (111/711) -> Constituent paper DOIs."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_j_conference", ["isbn", "doi"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="event"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            analytics = extract_target_analytics(target)
            cms = [ConstituentWorkMatch(title=t) for t, _a in analytics]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_meeting_triad",
                funnel_used="funnel_j_conference",
                confidence_score=0.88,
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, None, "funnel_j_conference")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_j_conference",
        )

    def match_thesis(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel K (Theses & Dissertations): Repository URN/handle -> Author + Title + Dissertation note (502) -> Deduplication."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_k_thesis", ["urn", "handle", "doi"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="thesis"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_dissertation_dedup",
                funnel_used="funnel_k_thesis",
                confidence_score=0.92,
            )

        fb = self._vector_or_fts_fallback(target, "thesis", "funnel_k_thesis")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_k_thesis",
        )

    def match_cartography(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel L (Cartography): Sheet index / map ISBN -> Bounding box IoU (034) -> Area + scale ratio (255)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_l_cartography", ["sheet_number", "isbn"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="cartography"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="cartographic_image")
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_l_cartography",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "cartography", "funnel_l_cartography")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_l_cartography",
        )

    def match_comic(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel M (Comics & Manga): ISBN/Series ISSN -> Series + Volume + Writer/Artist pair -> Translated expression."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_m_comic", ["isbn", "issn"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="comic"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_m_comic",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "comic", "funnel_m_comic")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_m_comic",
        )

    def match_dataset(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel N (Datasets): DataCite DOI / Handle -> Creator research group + title -> Version expression discrimination."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_n_dataset", ["doi", "handle"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="dataset"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="dataset")
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_n_dataset",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "dataset", "funnel_n_dataset")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_n_dataset",
        )

    def match_standard(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel O (Standards & Reports): Standard/STRN number exact -> Replacement tracking (780/785) -> Body + ICS code."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_o_standard", ["strn", "standard_number", "standard"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="standard"
        )
        if cand_works:
            work = cand_works[0]
            rels = self.repo.get_work_relationships(work.id)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_o_standard",
                confidence_score=0.90,
                relationships=rels,
            )

        fb = self._vector_or_fts_fallback(target, "standard", "funnel_o_standard")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_o_standard",
        )

    def match_government(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel P (Government Publications): Valto URN:ISBN -> Ministry corporate hierarchy + report number."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_p_government", ["urn", "isbn"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_p_government",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_p_government")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_p_government",
        )

    def match_realia(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel Q (3D Objects / Realia): Barcode / product code -> Title + Physical carrier description (300/338)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_q_realia", ["ean", "barcode", "isbn"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="realia"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_q_realia",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "realia", "funnel_q_realia")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_q_realia",
        )

    def match_archival(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel R (Archival Manuscripts): Collection signum -> Fonds anchor (cre) + 773 -> Draft-to-published link (draft_of)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_r_archival", ["signum", "call_number", "handle"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="archive_fonds"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="manuscript"
        )
        if cand_works:
            work = cand_works[0]
            # Detect draft-to-published literary work
            published_works = self.repo.find_works_by_author_and_title(
                target.author_key, target.title_stem, work_type="text"
            )
            rels = []
            if published_works and published_works[0].id != work.id:
                rels.append(
                    WorkRelationship(
                        source_work_id=work.id or 0,
                        target_work_id=published_works[0].id or 0,
                        relationship_type="draft_of",
                    )
                )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_r_archival",
                confidence_score=0.90,
                relationships=rels,
            )

        fb = self._vector_or_fts_fallback(target, "archive_fonds", "funnel_r_archival")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_r_archival",
        )

    def match_still_image(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel S (Still Images & Ephemera): Archive photo ID -> Artist/Photographer (art/pht) + Depicted place (751) -> Medium (340)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_s_still_image", ["accession_number", "photo_id"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="still_image"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="still_image")
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_s_still_image",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "still_image", "funnel_s_still_image")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_s_still_image",
        )

    def match_legal(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel T (Legal Norms & Statutes): Säädösnumero (SDK) / ELI / CELEX / ECLI exact -> Uniform title + enactment date -> Amendment chain (amends)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_t_legal", ["sdk", "eli", "celex", "ecli"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="legal"
        )
        if cand_works:
            work = cand_works[0]
            rels = self.repo.get_work_relationships(work.id)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_t_legal",
                confidence_score=0.92,
                relationships=rels,
            )

        fb = self._vector_or_fts_fallback(target, "legal", "funnel_t_legal")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_t_legal",
        )

    def match_patent(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel U (Patents & IP): Patent number (013) exact -> Priority application family -> Inventor + Assignee + IPC (084)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_u_patent", ["patent_number", "patent"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="patent"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_u_patent",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "patent", "funnel_u_patent")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_u_patent",
        )

    def match_game(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel V (Video Games): EAN / platform SKU exact -> Studio/Designer (dsr/cre) + title stem -> Platform expression (538)."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_v_game", ["ean", "upc", "sku"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="interactive_game"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language, content_type="computer_program")
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_domain_key",
                funnel_used="funnel_v_game",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "interactive_game", "funnel_v_game")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_v_game",
        )

    def match_integrating(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel W (Integrating Resources): ISSN / loose-leaf control ID -> Title + publisher -> Successive iteration (310/362)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_w_integrating", ["issn", "loose_leaf_id"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="integrating"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_w_integrating",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "integrating", "funnel_w_integrating")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_w_integrating",
        )

    def match_board_game(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel X (Board Games): EAN / publisher item code / BGG ID exact -> Game Designer (dsr) + Title stem -> Expansion hierarchy (expansion_of)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_x_board_game", ["ean", "publisher_code", "bgg"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="board_game"
        )
        if cand_works:
            work = cand_works[0]
            rels = self.repo.get_work_relationships(work.id)
            analytics = extract_target_analytics(target)
            cms = [ConstituentWorkMatch(title=t) for t, _a in analytics]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_x_board_game",
                confidence_score=0.90,
                relationships=rels,
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, "board_game", "funnel_x_board_game")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_x_board_game",
        )

    def match_early_print(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel Y (Early Printed Books & Incunabula): Fingerprint formula (026) / ISTC / GW exact -> Colophon printer + city + year window."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_y_early_print", ["fingerprint", "istc", "gw"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="early_print"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_y_early_print",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "early_print", "funnel_y_early_print")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_y_early_print",
        )

    def match_tactile(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel Z (Tactile & Accessible Media): Source ISBN / control in 776 -> Base canonical Work resolution -> Accessibility discrimination."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_z_tactile", ["isbn", "control_number"]
        )
        if fast:
            return fast

        # Base Work resolution
        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="accessible_reproduction_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_accessible_repro",
                funnel_used="funnel_z_tactile",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_z_tactile")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_z_tactile",
        )

    def match_software_ai(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel AA (Software & Trained AI Models): SWHID / Hugging Face ID exact -> Git commit SHA -> Lineage (fine_tuned_from)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_aa_software_ai", ["swhid", "huggingface"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="software"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="ai_model"
        )
        if cand_works:
            work = cand_works[0]
            rels = self.repo.get_work_relationships(work.id)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_aa_software_ai",
                confidence_score=0.90,
                relationships=rels,
            )

        fb = self._vector_or_fts_fallback(target, "software", "funnel_aa_software_ai")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_aa_software_ai",
        )

    def match_drama(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel BB (Dramatic Works & Choreography): Theatrical uniform title (240 10) + Playwright/Choreographer -> Staging resolution (staging_of)."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_bb_drama", ["play_id", "urn"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="drama"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="choreography"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="staging_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_bb_drama",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "drama", "funnel_bb_drama")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_bb_drama",
        )

    def match_podcast(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel CC (Broadcast Podcasts & Episodic Media): RSS enclosure GUID / Apple/Spotify URI exact -> Show series title -> Host + timestamp."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_cc_podcast", ["guid_rss", "spotify_uri"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="podcast"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_cc_podcast",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "podcast", "funnel_cc_podcast")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_cc_podcast",
        )

    def match_sammelband(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel DD (Bound-With / Sammelbände): Shared copy barcode & shelfmark -> Bound-with note (501/777) -> Boundary isolation."""
        # Boundaries remain strictly isolated; match underlying manifestation/work independently
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_dd_sammelband", ["barcode", "shelfmark"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(target.author_key, target.title_stem)
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_sammelband_isolated",
                funnel_used="funnel_dd_sammelband",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, None, "funnel_dd_sammelband")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_dd_sammelband",
        )

    def match_web_archive(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel EE (Web Archives & Harvested Crawls): Memento URI / WARC UUID exact -> Seed domain triad -> harvest_of."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_ee_web_archive", ["memento_uri", "warc_id"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="web_harvest"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="harvest_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_ee_web_archive",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "web_harvest", "funnel_ee_web_archive")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_ee_web_archive",
        )

    def match_oral_history(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel FF (Oral Histories): Archival handle / signum exact -> Informant (ive) + interviewer (ivr) + session (518)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_ff_oral_history", ["handle", "signum"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="oral_history"
        )
        if cand_works:
            work = cand_works[0]
            exps = self.repo.get_expressions_for_work(work.id)
            exp = self._align_expression(exps, target.language)
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                matched_expression=exp,
                work_uri=work.work_uri,
                expression_uri=exp.expression_uri if exp else None,
                match_tier="tier2_session_triad",
                funnel_used="funnel_ff_oral_history",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "oral_history", "funnel_ff_oral_history")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_ff_oral_history",
        )

    def match_broadcast(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel GG (Broadcast TV & Radio Flow): EIDR URI / ISAN exact -> Station + air date + timecode -> broadcast_of."""
        fast = self._try_fast_path_identifiers(target, ids, "funnel_gg_broadcast", ["eidr", "isan"])
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="broadcast_program"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="broadcast_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_transmission_triad",
                funnel_used="funnel_gg_broadcast",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "broadcast_program", "funnel_gg_broadcast")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_gg_broadcast",
        )

    def match_parallel_multilingual(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel HH (Parallel Multilingual Editions): Base Work ID -> Language matrix (041 1#) -> parallel_expression_in."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_hh_parallel_multilingual", ["work_uri", "isbn"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=work.id or 0,
                target_work_id=work.id or 0,
                relationship_type="parallel_expression_in",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_parallel_matrix",
                funnel_used="funnel_hh_parallel_multilingual",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_hh_parallel_multilingual")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_hh_parallel_multilingual",
        )

    def match_preservation_surrogate(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel II (Preservation Surrogates & Microforms): Source print control no (776 $w) -> reproduction_of."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_ii_preservation_surrogate", ["control_number", "isbn"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="reproduction_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_reproduction_note",
                funnel_used="funnel_ii_preservation_surrogate",
                confidence_score=0.92,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "text", "funnel_ii_preservation_surrogate")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_ii_preservation_surrogate",
        )

    def match_medieval_fragment(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel JJ (Medieval Manuscript Fragments): Fragment ID (fragmenta) exact -> Bailiff account binding -> fragment_of_codex."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_jj_medieval_fragment", ["fragmenta"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="codex_fragment"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="fragment_of_codex",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_virtual_codex",
                funnel_used="funnel_jj_medieval_fragment",
                confidence_score=0.92,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "codex_fragment", "funnel_jj_medieval_fragment")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_jj_medieval_fragment",
        )

    def match_ephemera(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel KK (Ephemera & Job Printing): Corporate body (110/710) + Genre heading (655 #7 Pienpainatteet) -> Seasonal window (362)."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_kk_ephemera", ["corporate_authority"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="ephemera"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_corporate_ephemera",
                funnel_used="funnel_kk_ephemera",
                confidence_score=0.88,
            )

        fb = self._vector_or_fts_fallback(target, "ephemera", "funnel_kk_ephemera")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_kk_ephemera",
        )

    def match_exhibition(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel LL (Exhibition Catalogues & Events): Monograph ISBN / Event triad (111/711) -> Dual Work resolution -> exhibits_work."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_ll_exhibition", ["isbn", "event_id"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="exhibition"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="event"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="exhibits_work",
            )
            analytics = extract_target_analytics(target)
            cms = [
                ConstituentWorkMatch(title=t, relationship_type="exhibits_work")
                for t, _a in analytics
            ]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_event_triad",
                funnel_used="funnel_ll_exhibition",
                confidence_score=0.90,
                relationships=[rel],
                constituent_matches=cms,
            )

        fb = self._vector_or_fts_fallback(target, "exhibition", "funnel_ll_exhibition")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_ll_exhibition",
        )

    def match_critical_edition(
        self, target: TargetRecord, ids: dict[str, list[str]]
    ) -> WEMIMatchResult:
        """Funnel MM (Scholarly Critical Editions): Canonical base Work ID (240 10 $0 / 758) -> Critical editor -> critical_edition_of."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_mm_critical_edition", ["work_uri", "isbn"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="critical_edition"
        ) or self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="text"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="critical_edition_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_critical_scholarship",
                funnel_used="funnel_mm_critical_edition",
                confidence_score=0.92,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "critical_edition", "funnel_mm_critical_edition")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_mm_critical_edition",
        )

    def match_geodata(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel NN (Spatial Geodata & GIS Web Services): EPSG code + service endpoint -> Bounding polygon IoU -> spatial_layer_of."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_nn_geodata", ["epsg", "service_endpoint"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="geodata_service"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="spatial_layer_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_spatial_polygon",
                funnel_used="funnel_nn_geodata",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "geodata_service", "funnel_nn_geodata")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_nn_geodata",
        )

    def match_numismatic(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel OO (Numismatics & Historical Seals): Michel / Scott / KM catalog no exact -> Issuing state + denomination + year."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_oo_numismatic", ["michel", "scott", "km"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="numismatic"
        )
        if cand_works:
            work = cand_works[0]
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_oo_numismatic",
                confidence_score=0.90,
            )

        fb = self._vector_or_fts_fallback(target, "numismatic", "funnel_oo_numismatic")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_oo_numismatic",
        )

    def match_heritage_3d(self, target: TargetRecord, ids: dict[str, list[str]]) -> WEMIMatchResult:
        """Funnel PP (3D Cultural Heritage Digital Twins): 3D asset hash / URI -> Monument stem + time layer -> digital_twin_of."""
        fast = self._try_fast_path_identifiers(
            target, ids, "funnel_pp_heritage_3d", ["asset_hash", "model_uri"]
        )
        if fast:
            return fast

        cand_works = self.repo.find_works_by_author_and_title(
            target.author_key, target.title_stem, work_type="heritage_model"
        )
        if cand_works:
            work = cand_works[0]
            rel = WorkRelationship(
                source_work_id=0,
                target_work_id=work.id or 0,
                relationship_type="digital_twin_of",
            )
            return WEMIMatchResult(
                target_record_id=target.record_id,
                matched_work=work,
                work_uri=work.work_uri,
                match_tier="tier2_domain_key",
                funnel_used="funnel_pp_heritage_3d",
                confidence_score=0.90,
                relationships=[rel],
            )

        fb = self._vector_or_fts_fallback(target, "heritage_model", "funnel_pp_heritage_3d")
        if fb:
            return fb

        return WEMIMatchResult(
            target_record_id=target.record_id,
            match_tier="unmatched",
            funnel_used="funnel_pp_heritage_3d",
        )

    # -----------------------------------------------------------------------
    # Master Dispatcher
    # -----------------------------------------------------------------------

    def match_target(
        self,
        target: TargetRecord,
        identifiers: Optional[dict[str, list[str]]] = None,
    ) -> WEMIMatchResult:
        """Resolve a staged TargetRecord using specialized multi-tier domain funnels (A through PP)."""
        # 1. Extract and merge standard identifiers
        extracted_ids = extract_identifiers_from_target(target)
        if identifiers:
            for k, v in identifiers.items():
                k_low = k.lower().strip()
                if k_low not in extracted_ids:
                    extracted_ids[k_low] = []
                for item in v:
                    if item not in extracted_ids[k_low]:
                        extracted_ids[k_low].append(item)

        # 2. Domain Funnel Dispatch
        wt = target.work_type

        if target.is_component or target.host_citation_key or wt in ("article", "chapter"):
            return self.match_article(target, extracted_ids)

        if wt == "music":
            if (
                target.music_work_key
                or "score" in (target.title_stem or "").lower()
                or any(k in extracted_ids for k in ("ismn", "plate_number"))
            ):
                return self.match_notated_music(target, extracted_ids)
            return self.match_recorded_sound(target, extracted_ids)

        if wt == "audio_nonmusic":
            if target.podcast_key or "podcast" in (target.title_stem or "").lower():
                return self.match_podcast(target, extracted_ids)
            if target.oral_history_key:
                return self.match_oral_history(target, extracted_ids)
            if target.broadcast_timecode_key:
                return self.match_broadcast(target, extracted_ids)
            return self.match_audiobook(target, extracted_ids)

        if wt == "moving_image":
            if target.broadcast_timecode_key:
                return self.match_broadcast(target, extracted_ids)
            return self.match_moving_image(target, extracted_ids)

        if wt == "serial":
            return self.match_serial(target, extracted_ids)

        if wt == "integrating":
            return self.match_integrating(target, extracted_ids)

        if wt == "thesis":
            return self.match_thesis(target, extracted_ids)

        if wt == "cartography":
            return self.match_cartography(target, extracted_ids)

        if wt == "comic":
            return self.match_comic(target, extracted_ids)

        if wt == "dataset":
            return self.match_dataset(target, extracted_ids)

        if wt == "standard":
            return self.match_standard(target, extracted_ids)

        if wt in ("legal", "statute"):
            return self.match_legal(target, extracted_ids)

        if wt == "patent":
            return self.match_patent(target, extracted_ids)

        if wt == "interactive_game":
            return self.match_game(target, extracted_ids)

        if wt == "board_game":
            return self.match_board_game(target, extracted_ids)

        if wt == "early_print":
            return self.match_early_print(target, extracted_ids)

        if wt == "tactile":
            return self.match_tactile(target, extracted_ids)

        if wt in ("software", "ai_model"):
            return self.match_software_ai(target, extracted_ids)

        if wt in ("drama", "choreography"):
            return self.match_drama(target, extracted_ids)

        if wt == "podcast":
            return self.match_podcast(target, extracted_ids)

        if target.sammelband_item_id:
            return self.match_sammelband(target, extracted_ids)

        if wt == "web_harvest":
            return self.match_web_archive(target, extracted_ids)

        if wt == "oral_history":
            return self.match_oral_history(target, extracted_ids)

        if wt == "broadcast_program":
            return self.match_broadcast(target, extracted_ids)

        if target.translators_signature and "parallel" in target.translators_signature.lower():
            return self.match_parallel_multilingual(target, extracted_ids)

        if wt == "preservation_surrogate":
            return self.match_preservation_surrogate(target, extracted_ids)

        if wt == "codex_fragment":
            return self.match_medieval_fragment(target, extracted_ids)

        if wt == "ephemera":
            return self.match_ephemera(target, extracted_ids)

        if wt in ("anthology", "anthology_collection"):
            return self.match_anthology(target, extracted_ids)

        if wt in ("conference", "conference_proceedings"):
            return self.match_conference(target, extracted_ids)

        if wt in ("government", "government_publication") or any(
            "valto" in u.lower() for u in extracted_ids.get("urn", [])
        ):
            return self.match_government(target, extracted_ids)

        if wt in ("exhibition", "event"):
            if target.exhibition_event_key or "näyttely" in (target.title_stem or "").lower():
                return self.match_exhibition(target, extracted_ids)
            return self.match_conference(target, extracted_ids)

        if wt == "critical_edition":
            return self.match_critical_edition(target, extracted_ids)

        if wt == "geodata_service":
            return self.match_geodata(target, extracted_ids)

        if wt == "numismatic":
            return self.match_numismatic(target, extracted_ids)

        if wt == "heritage_model":
            return self.match_heritage_3d(target, extracted_ids)

        if wt == "realia":
            return self.match_realia(target, extracted_ids)

        if wt in ("archive_fonds", "manuscript"):
            return self.match_archival(target, extracted_ids)

        if wt == "still_image":
            return self.match_still_image(target, extracted_ids)

        if target.series_key:
            return self.match_series(target, extracted_ids)

        # Default: Monographic Work matching
        return self.match_monograph(target, extracted_ids)
