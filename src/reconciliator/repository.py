"""AuthorityStore repository mediating access to SQLite authority.db and vector index."""

from __future__ import annotations

from collections import defaultdict
import json
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from rapidfuzz import fuzz

from reconciliator.config import get_bundled_record_defaults
from reconciliator.vocabularies import concept_id_to_uri, format_wikidata_uri
from reconciliator.normalizers.linguistic import fold_label

from reconciliator.db import (
    get_db_connection,
    get_metadata,
    init_authority_db,
    init_corpus_db,
    set_metadata,
)
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import (
    AgentEntity,
    BibliographicRecord,
    ContributionEntity,
    CorpusLinkage,
    EntityType,
    ExpressionCluster,
    ExpressionEntity,
    HarvestState,
    IdentifierEntity,
    ItemEntity,
    ManifestationCluster,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    MediumOfPerformance,
    RecordIdentifiers,
    SammelbandBinding,
    TargetRecord,
    WorkCluster,
    WorkEntity,
    WorkRelationship,
)

if TYPE_CHECKING:
    from reconciliator.config import RepositoryFTSConfig, VectorEngineConfig
    from reconciliator.embedder import SentenceEmbedder


def _hydrate_agent_profile(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Hydrate intrinsic count distributions, orcid, and variant names from metadata_json."""
    meta: dict[str, Any] = {}
    if row_dict.get("metadata_json"):
        try:
            meta = json.loads(row_dict["metadata_json"])
        except Exception:
            meta = {}
    row_dict["qualifier_counts"] = meta.get("qualifier_counts", {})
    row_dict["affiliation_counts"] = meta.get("affiliation_counts", {})
    row_dict["role_counts"] = meta.get("role_counts", {})
    row_dict["language_counts"] = meta.get("language_counts", {})
    row_dict["subject_counts"] = meta.get("subject_counts", {})
    row_dict["orcid"] = meta.get("orcid") or row_dict.get("orcid")
    row_dict["variant_names"] = meta.get("variant_names") or row_dict.get("variant_names") or []

    # Fallback to legacy columns if count dicts are empty
    if not row_dict["qualifier_counts"] and row_dict.get("qualifier"):
        row_dict["qualifier_counts"] = {str(row_dict["qualifier"]): 1}
    if not row_dict["affiliation_counts"] and row_dict.get("affiliation"):
        row_dict["affiliation_counts"] = {str(row_dict["affiliation"]): 1}
    if not row_dict["role_counts"] and row_dict.get("role"):
        row_dict["role_counts"] = {str(row_dict["role"]): 1}

    return row_dict


class AuthorityStore:
    """Encapsulates persistent authority records in SQLite and dense vectors in the vector index."""

    def __init__(
        self,
        db_path: Path | str,
        index_path: Path | str,
        dimension: int = 768,
        vector_engine_config: Optional[VectorEngineConfig] = None,
        indexer: Optional[VectorIndex] = None,
        source_identifier_templates: Optional[dict[str, str]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.index_path = Path(index_path)
        self.dimension = dimension

        if source_identifier_templates is not None:
            self.source_identifier_templates = {
                k.lower(): str(v) for k, v in source_identifier_templates.items()
            }
        else:
            rec_defaults = get_bundled_record_defaults()
            self.source_identifier_templates = {
                k.lower(): str(v)
                for k, v in rec_defaults.get("source_identifier_templates", {}).items()
            }

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            init_authority_db(conn)

        if indexer is not None:
            self.indexer = indexer
        else:
            self.indexer = create_vector_engine(
                self.index_path, dimension=self.dimension, config=vector_engine_config
            )

    def get_connection(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def insert_records_batch(
        self,
        records: list[BibliographicRecord],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert records, identifiers, agents, FTS entries, and dense vectors."""
        if not records:
            return []

        record_ids: list[int] = []
        all_id_rows: list[tuple[int, str, str]] = []
        all_agent_rows: list[tuple[Any, ...]] = []
        all_fts_rows: list[tuple[int, int, str, str, str, str, str]] = []

        with self.get_connection() as conn:
            for record in records:
                # Upsert authority_records
                cursor = conn.execute(
                    """
                    INSERT INTO authority_records (
                        source, source_id, title, title_normalized,
                        uniform_title, edition, series, volume_info,
                        year, publisher, language, subjects_json, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        title = excluded.title,
                        record_json = excluded.record_json
                    RETURNING id;
                    """,
                    (
                        record.source,
                        record.source_id,
                        record.title,
                        record.title.lower(),
                        record.uniform_title,
                        record.edition,
                        record.series_title,
                        record.volume_designation,
                        record.publication_year,
                        record.publisher,
                        record.language,
                        json.dumps(record.subjects.model_dump(), ensure_ascii=False),
                        json.dumps(record.model_dump(), ensure_ascii=False),
                    ),
                )
                rec_id = cursor.fetchone()["id"]
                record_ids.append(rec_id)

                # Prepare identifiers
                for isbn in record.identifiers.isbn:
                    all_id_rows.append((rec_id, "isbn", isbn))
                for issn in record.identifiers.issn:
                    all_id_rows.append((rec_id, "issn", issn))
                for urn in record.identifiers.urn:
                    all_id_rows.append((rec_id, "urn", urn))
                for doi in record.identifiers.doi:
                    all_id_rows.append((rec_id, "doi", doi))
                for oid in record.identifiers.other_ids:
                    all_id_rows.append((rec_id, "other", oid))
                id_type = "wikidata_qid" if record.source == "wikidata" else record.source
                all_id_rows.append((rec_id, id_type, record.source_id))

                # Prepare agents
                for a in record.agents:
                    agent_meta = {
                        "qualifier_counts": a.qualifier_counts,
                        "affiliation_counts": a.affiliation_counts,
                        "role_counts": a.role_counts,
                        "language_counts": a.language_counts,
                        "subject_counts": a.subject_counts,
                        "orcid": a.orcid,
                        "variant_names": a.variant_names,
                    }
                    all_agent_rows.append(
                        (
                            rec_id,
                            a.name,
                            a.name.lower(),
                            a.surname,
                            None,
                            None,
                            a.dates,
                            a.birth_year,
                            a.death_year,
                            a.fuller_name,
                            None,
                            json.dumps(agent_meta, ensure_ascii=False),
                            json.dumps(a.uri_subfield_0, ensure_ascii=False),
                            json.dumps(a.uri_subfield_1, ensure_ascii=False),
                        )
                    )

                # Prepare FTS
                agent_names_str = " ".join(a.name for a in record.agents)
                all_subjs = record.subjects.all_subjects()
                subj_terms = " ".join(s.pref_label for s in all_subjs if s.pref_label)
                all_fts_rows.append(
                    (
                        rec_id,
                        rec_id,
                        record.title,
                        record.uniform_title or "",
                        agent_names_str,
                        record.publisher or "",
                        subj_terms,
                    )
                )

            # Clean existing child rows in batch for any re-inserted / updated records (using fast rowid for FTS5)
            rec_id_tuples = [(rid,) for rid in record_ids]
            conn.executemany(
                "DELETE FROM authority_identifiers WHERE record_id = ?;", rec_id_tuples
            )
            conn.executemany("DELETE FROM authority_agents WHERE record_id = ?;", rec_id_tuples)
            conn.executemany("DELETE FROM authority_fts WHERE rowid = ?;", rec_id_tuples)
            conn.executemany("DELETE FROM vector_mappings WHERE record_id = ?;", rec_id_tuples)

            # Bulk insert children
            if all_id_rows:
                conn.executemany(
                    "INSERT INTO authority_identifiers (record_id, id_type, id_value) VALUES (?, ?, ?);",
                    all_id_rows,
                )
            if all_agent_rows:
                conn.executemany(
                    """
                    INSERT INTO authority_agents (
                        record_id, name, name_normalized, surname, role,
                        qualifier, dates, birth_year, death_year, fuller_name,
                        affiliation, metadata_json, uri_subfield_0, uri_subfield_1
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    all_agent_rows,
                )
            if all_fts_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO authority_fts (rowid, record_id, title, uniform_title, author_names, publisher, subjects)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    all_fts_rows,
                )

        # Add vectors to vector index if provided
        if vectors is not None and len(vectors) == len(records):
            vector_ids = self.indexer.add_vectors(vectors, ids=record_ids)
            with self.get_connection() as conn:
                mapping_rows = list(zip(vector_ids, record_ids))
                conn.executemany(
                    "INSERT OR REPLACE INTO vector_mappings (vector_id, record_id) VALUES (?, ?);",
                    mapping_rows,
                )

        return record_ids

    def get_indexed_source_ids(self, source: str) -> set[str]:
        """Return set of source_ids that are already stored in DB and active in the vector index."""
        if self.indexer.total_vectors == 0:
            return set()

        valid_vector_ids: Optional[set[int]] = None
        if hasattr(self.indexer, "_id_to_idx") and getattr(self.indexer, "_id_to_idx", None):
            valid_vector_ids = set(self.indexer._id_to_idx.keys())
        elif hasattr(self.indexer, "ids") and getattr(self.indexer, "ids", None) is not None:
            ids_t = self.indexer.ids
            if ids_t is not None and len(ids_t) > 0:
                valid_vector_ids = set(
                    ids_t.cpu().tolist() if hasattr(ids_t, "cpu") else list(ids_t)
                )

        indexed_ids: set[str] = set()
        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT r.id, r.source_id
                FROM authority_records r
                JOIN vector_mappings v ON r.id = v.record_id
                WHERE r.source = ?;
                """,
                (source,),
            )
            for row in cursor:
                if valid_vector_ids is not None and row["id"] not in valid_vector_ids:
                    continue
                sid = row["source_id"]
                if not sid:
                    continue
                indexed_ids.add(sid)

                # Generate unprefixed, unpadded, and OAI URI variations for robust matching
                core_id = sid
                if sid.startswith("(") and ")" in sid:
                    core_id = sid.split(")", 1)[1]
                    indexed_ids.add(core_id)
                unpadded = core_id.lstrip("0")
                if unpadded and unpadded != core_id:
                    indexed_ids.add(unpadded)
                tmpl = (
                    self.source_identifier_templates.get(source.lower())
                    if hasattr(self, "source_identifier_templates") and self.source_identifier_templates
                    else None
                )
                if tmpl and "{id}" in tmpl and core_id.isdigit():
                    indexed_ids.add(tmpl.format(id=core_id))
                    if unpadded:
                        indexed_ids.add(tmpl.format(id=unpadded))

        return indexed_ids

    def get_record_by_id(self, record_id: int) -> Optional[BibliographicRecord]:
        """Fetch full BibliographicRecord model from authority_records."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT record_json FROM authority_records WHERE id = ?;", (record_id,)
            ).fetchone()
            if not row or not row["record_json"]:
                return None
            data = json.loads(row["record_json"])
            return BibliographicRecord.model_validate(data)

    def find_exact_matches(self, identifiers: RecordIdentifiers) -> list[BibliographicRecord]:
        """
        Fast-path Tier 1 exact match query across ISBN, ISSN, URN, and control numbers.
        Bibliographic authority records are prioritized over Wikidata.
        """
        query_values: list[tuple[str, str]] = []
        for isbn in identifiers.isbn:
            query_values.append(("isbn", isbn))
        for issn in identifiers.issn:
            query_values.append(("issn", issn))
        for urn in identifiers.urn:
            query_values.append(("urn", urn))
        for doi in identifiers.doi:
            query_values.append(("doi", doi))

        matched_record_ids: list[int] = []
        with self.get_connection() as conn:
            for id_type, id_value in query_values:
                rows = conn.execute(
                    """
                    SELECT r.id, r.source
                    FROM authority_identifiers i
                    JOIN authority_records r ON i.record_id = r.id
                    WHERE i.id_type = ? AND i.id_value = ?
                    ORDER BY CASE WHEN r.source != 'wikidata' THEN 0 ELSE 1 END;
                    """,
                    (id_type, id_value),
                ).fetchall()
                for r in rows:
                    rec_id = r["id"]
                    if rec_id not in matched_record_ids:
                        matched_record_ids.append(rec_id)

            for oid in identifiers.other_ids:
                rows = conn.execute(
                    """
                    SELECT r.id, r.source
                    FROM authority_identifiers i
                    JOIN authority_records r ON i.record_id = r.id
                    WHERE i.id_type NOT IN ('isbn', 'issn', 'urn', 'doi') AND i.id_value = ?
                    ORDER BY CASE WHEN r.source != 'wikidata' THEN 0 ELSE 1 END;
                    """,
                    (oid,),
                ).fetchall()
                for r in rows:
                    rec_id = r["id"]
                    if rec_id not in matched_record_ids:
                        matched_record_ids.append(rec_id)

        results: list[BibliographicRecord] = []
        for rid in matched_record_ids:
            rec = self.get_record_by_id(rid)
            if rec:
                results.append(rec)
        return results

    def search_vector_candidates(
        self, query_vector: np.ndarray, top_k: int = 50
    ) -> list[tuple[BibliographicRecord, float]]:
        """
        Retrieve top-k candidate records from the vector index.
        Returns list of (BibliographicRecord, dense_cosine_similarity).
        """
        if self.indexer.total_vectors == 0:
            return []

        scores, indices = self.indexer.search(query_vector, top_k=top_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_rec_id: dict[int, int] = {}
        with self.get_connection() as conn:
            placeholders = ",".join("?" for _ in vector_indices)
            rows = conn.execute(
                f"SELECT vector_id, record_id FROM vector_mappings WHERE vector_id IN ({placeholders});",
                vector_indices,
            ).fetchall()
            for r in rows:
                vector_to_rec_id[r["vector_id"]] = r["record_id"]

        results: list[tuple[BibliographicRecord, float]] = []
        seen_record_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            rec_id = vector_to_rec_id.get(v_id, v_id if v_id >= 0 else None)
            if rec_id is not None and rec_id not in seen_record_ids:
                seen_record_ids.add(rec_id)
                rec = self.get_record_by_id(rec_id)
                if rec:
                    results.append((rec, float(score)))

        return results

    def save_index(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist vector index to disk with metadata."""
        self.indexer.save(metadata=metadata)

    def insert_wikidata_authors(self, authors: list[dict[str, Any]]) -> None:
        """Persist author entity metadata into wikidata_authors table."""
        if not authors:
            return
        rows = []
        for a in authors:
            if not a.get("qid"):
                continue
            idents = a.get("identifiers")
            if isinstance(idents, dict):
                idents_json = json.dumps(idents, ensure_ascii=False) if idents else None
            elif isinstance(idents, str):
                idents_json = idents
            else:
                idents_json = a.get("identifiers_json")

            rows.append(
                (
                    a["qid"],
                    a.get("name", ""),
                    a.get("family_name"),
                    idents_json,
                    a.get("birth_year"),
                    a.get("death_year"),
                    a.get("occupations"),
                    a.get("fields_of_work"),
                    a.get("affiliations"),
                    a.get("metadata_json"),
                )
            )
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO wikidata_authors (
                    qid, name, family_name, identifiers_json,
                    birth_year, death_year, occupations, fields_of_work, affiliations, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    name = excluded.name,
                    family_name = excluded.family_name,
                    identifiers_json = COALESCE(excluded.identifiers_json, wikidata_authors.identifiers_json),
                    birth_year = COALESCE(excluded.birth_year, wikidata_authors.birth_year),
                    death_year = COALESCE(excluded.death_year, wikidata_authors.death_year),
                    occupations = COALESCE(excluded.occupations, wikidata_authors.occupations),
                    fields_of_work = COALESCE(excluded.fields_of_work, wikidata_authors.fields_of_work),
                    affiliations = COALESCE(excluded.affiliations, wikidata_authors.affiliations),
                    metadata_json = COALESCE(excluded.metadata_json, wikidata_authors.metadata_json);
                """,
                rows,
            )

    def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch cached author entities from wikidata_authors by QIDs."""
        if not qids:
            return {}
        results: dict[str, dict[str, Any]] = {}
        with self.get_connection() as conn:
            placeholders = ",".join("?" for _ in qids)
            rows = conn.execute(
                f"""SELECT qid, name, family_name, identifiers_json,
                           birth_year, death_year, occupations, fields_of_work, affiliations, metadata_json
                    FROM wikidata_authors WHERE qid IN ({placeholders});""",
                qids,
            ).fetchall()
            for r in rows:
                raw_ident = r["identifiers_json"]
                identifiers: dict[str, str] = {}
                if raw_ident:
                    try:
                        identifiers = json.loads(raw_ident)
                    except Exception:
                        identifiers = {}
                results[r["qid"]] = {
                    "qid": r["qid"],
                    "name": r["name"],
                    "family_name": r["family_name"],
                    "identifiers": identifiers,
                    "birth_year": r["birth_year"],
                    "death_year": r["death_year"],
                    "occupations": r["occupations"],
                    "fields_of_work": r["fields_of_work"],
                    "affiliations": r["affiliations"],
                    "metadata_json": r["metadata_json"],
                }
        return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from wikidata_authors by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

    def get_agents_by_ids(self, agent_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch rich agent profiles by IDs from authority_agents or agents table."""
        if not agent_ids:
            return {}
        results: dict[int, dict[str, Any]] = {}
        placeholders = ",".join("?" for _ in agent_ids)
        with self.get_connection() as conn:
            # First attempt: WEMI agents table
            try:
                rows = conn.execute(
                    f"""
                    SELECT id, agent_uri, source, wikidata_qid, name, name_normalized, surname,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation, metadata_json
                    FROM agents WHERE id IN ({placeholders});
                    """,
                    agent_ids,
                ).fetchall()
                for r in rows:
                    results[r["id"]] = _hydrate_agent_profile(dict(r))
            except Exception:
                pass

            # Second attempt: authority_agents table for any remaining IDs
            missing_ids = [aid for aid in agent_ids if aid not in results]
            if missing_ids:
                m_placeholders = ",".join("?" for _ in missing_ids)
                rows = conn.execute(
                    f"""
                    SELECT id, record_id, name, name_normalized, surname, role,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation,
                           metadata_json, uri_subfield_0, uri_subfield_1
                    FROM authority_agents WHERE id IN ({m_placeholders});
                    """,
                    missing_ids,
                ).fetchall()
                for r in rows:
                    results[r["id"]] = _hydrate_agent_profile(dict(r))
        return results

    def get_agent_by_id(self, agent_id: int) -> dict[str, Any] | None:
        """Fetch a single rich agent profile by ID."""
        batch = self.get_agents_by_ids([agent_id])
        return batch.get(agent_id)

    def get_relational_disambiguation_evidence(
        self,
        candidate_agent_ids: list[int],
        target_coauthors: list[str],
        target_publisher: Optional[str] = None,
        target_classifications: Optional[list[tuple[str, str]]] = None,
    ) -> dict[int, dict[str, Any]]:
        """Execute targeted SQL queries on authority_records / authority_agents or WEMI contributions

        to check co-author graph overlap, publisher overlap, and scheme-agnostic classification overlap.
        """
        evidence: dict[int, dict[str, Any]] = {
            cid: {
                "has_coauthor_match": False,
                "matched_coauthors": [],
                "has_publisher_match": False,
                "matched_publishers": [],
                "has_classification_match": False,
                "matched_classifications": [],
            }
            for cid in candidate_agent_ids
        }
        if not candidate_agent_ids:
            return evidence

        norm_target_coauthors = {fold_label(ca) for ca in target_coauthors if ca and fold_label(ca)}
        norm_target_pub = fold_label(target_publisher) if target_publisher else ""
        norm_target_classes = {
            f"{scheme.strip().lower()}:{code.strip().lower()}"
            for code, scheme in (target_classifications or [])
            if code and scheme
        }

        with self.get_connection() as conn:
            for cid in candidate_agent_ids:
                cand_row = conn.execute(
                    "SELECT id, record_id, name_normalized, dates, uri_subfield_0 FROM authority_agents WHERE id = ?;",
                    (cid,),
                ).fetchone()

                if cand_row:
                    cand_name_norm = cand_row["name_normalized"]
                    cand_u0 = cand_row["uri_subfield_0"]
                    cand_dates = cand_row["dates"]

                    if cand_u0 and cand_u0 not in ("[]", ""):
                        rec_rows = conn.execute(
                            "SELECT DISTINCT record_id FROM authority_agents WHERE uri_subfield_0 = ?;",
                            (cand_u0,),
                        ).fetchall()
                        rec_ids = [r["record_id"] for r in rec_rows if r["record_id"]]
                    elif cand_dates:
                        rec_rows = conn.execute(
                            "SELECT DISTINCT record_id FROM authority_agents WHERE name_normalized = ? AND dates = ?;",
                            (cand_name_norm, cand_dates),
                        ).fetchall()
                        rec_ids = [r["record_id"] for r in rec_rows if r["record_id"]]
                    else:
                        rec_ids = [cand_row["record_id"]] if cand_row["record_id"] else []

                    if rec_ids:
                        placeholders = ",".join("?" for _ in rec_ids)

                        if norm_target_coauthors:
                            co_rows = conn.execute(
                                f"""
                                SELECT DISTINCT name, name_normalized
                                FROM authority_agents
                                WHERE record_id IN ({placeholders}) AND id != ?;
                                """,
                                rec_ids + [cid],
                            ).fetchall()
                            for co in co_rows:
                                c_norm = fold_label(co["name_normalized"] or co["name"])
                                for t_norm in norm_target_coauthors:
                                    if t_norm in c_norm or c_norm in t_norm or fuzz.token_set_ratio(t_norm, c_norm) >= 85.0:
                                        evidence[cid]["has_coauthor_match"] = True
                                        if co["name"] not in evidence[cid]["matched_coauthors"]:
                                            evidence[cid]["matched_coauthors"].append(co["name"])

                        if norm_target_pub:
                            pub_rows = conn.execute(
                                f"""
                                SELECT DISTINCT publisher
                                FROM authority_records
                                WHERE id IN ({placeholders}) AND publisher IS NOT NULL AND publisher != '';
                                """,
                                rec_ids,
                            ).fetchall()
                            for pb in pub_rows:
                                p_norm = fold_label(pb["publisher"])
                                if p_norm and (p_norm in norm_target_pub or norm_target_pub in p_norm or fuzz.token_set_ratio(norm_target_pub, p_norm) >= 80.0):
                                    evidence[cid]["has_publisher_match"] = True
                                    if pb["publisher"] not in evidence[cid]["matched_publishers"]:
                                        evidence[cid]["matched_publishers"].append(pb["publisher"])

                        if norm_target_classes:
                            json_rows = conn.execute(
                                f"""
                                SELECT DISTINCT record_json
                                FROM authority_records
                                WHERE id IN ({placeholders}) AND record_json IS NOT NULL;
                                """,
                                rec_ids,
                            ).fetchall()
                            for jr in json_rows:
                                try:
                                    rj = json.loads(jr["record_json"])
                                    r_classes = rj.get("classifications", [])
                                    for rc in r_classes:
                                        code = str(rc.get("code", "")).strip().lower()
                                        scheme = str(rc.get("scheme", "")).strip().lower()
                                        if not code:
                                            continue
                                        for tc in norm_target_classes:
                                            t_scheme, _, t_code = tc.partition(":")
                                            if t_scheme == scheme:
                                                if code.startswith(t_code) or t_code.startswith(code):
                                                    evidence[cid]["has_classification_match"] = True
                                                    matched_str = f"{scheme}:{code}"
                                                    if matched_str not in evidence[cid]["matched_classifications"]:
                                                        evidence[cid]["matched_classifications"].append(matched_str)
                                except Exception:
                                    pass

                else:
                    try:
                        wemi_agent = conn.execute(
                            "SELECT id, name_normalized FROM agents WHERE id = ?;", (cid,)
                        ).fetchone()
                        if wemi_agent:
                            contribs = conn.execute(
                                "SELECT entity_type, entity_id FROM contributions WHERE agent_id = ?;",
                                (cid,),
                            ).fetchall()
                            work_ids = [c["entity_id"] for c in contribs if c["entity_type"] == "work"]
                            manif_ids = [c["entity_id"] for c in contribs if c["entity_type"] == "manifestation"]

                            if work_ids and norm_target_coauthors:
                                w_placeholders = ",".join("?" for _ in work_ids)
                                co_agents = conn.execute(
                                    f"""
                                    SELECT DISTINCT a.name, a.name_normalized
                                    FROM contributions c
                                    JOIN agents a ON c.agent_id = a.id
                                    WHERE c.entity_type = 'work' AND c.entity_id IN ({w_placeholders}) AND c.agent_id != ?;
                                    """,
                                    work_ids + [cid],
                                ).fetchall()
                                for co in co_agents:
                                    c_norm = fold_label(co["name_normalized"] or co["name"])
                                    for t_norm in norm_target_coauthors:
                                        if t_norm in c_norm or c_norm in t_norm or fuzz.token_set_ratio(t_norm, c_norm) >= 85.0:
                                            evidence[cid]["has_coauthor_match"] = True
                                            if co["name"] not in evidence[cid]["matched_coauthors"]:
                                                evidence[cid]["matched_coauthors"].append(co["name"])

                            if manif_ids and norm_target_pub:
                                m_placeholders = ",".join("?" for _ in manif_ids)
                                pubs = conn.execute(
                                    f"""
                                    SELECT DISTINCT publisher
                                    FROM manifestations
                                    WHERE id IN ({m_placeholders}) AND publisher IS NOT NULL AND publisher != '';
                                    """,
                                    manif_ids,
                                ).fetchall()
                                for pb in pubs:
                                    p_norm = fold_label(pb["publisher"])
                                    if p_norm and (p_norm in norm_target_pub or norm_target_pub in p_norm or fuzz.token_set_ratio(norm_target_pub, p_norm) >= 80.0):
                                        evidence[cid]["has_publisher_match"] = True
                                        if pb["publisher"] not in evidence[cid]["matched_publishers"]:
                                            evidence[cid]["matched_publishers"].append(pb["publisher"])

                            if work_ids and norm_target_classes:
                                w_placeholders = ",".join("?" for _ in work_ids)
                                classes = conn.execute(
                                    f"""
                                    SELECT DISTINCT ykl_class
                                    FROM works
                                    WHERE id IN ({w_placeholders}) AND ykl_class IS NOT NULL AND ykl_class != '';
                                    """,
                                    work_ids,
                                ).fetchall()
                                for cl in classes:
                                    code = str(cl["ykl_class"]).strip().lower()
                                    for tc in norm_target_classes:
                                        t_scheme, _, t_code = tc.partition(":")
                                        if t_scheme in ("ykl", ""):
                                            if code.startswith(t_code) or t_code.startswith(code):
                                                evidence[cid]["has_classification_match"] = True
                                                matched_str = f"ykl:{code}"
                                                if matched_str not in evidence[cid]["matched_classifications"]:
                                                    evidence[cid]["matched_classifications"].append(matched_str)
                    except Exception:
                        pass

        return evidence

    def set_embedding_metadata(self, model_name: str, dimension: int, template_hash: str) -> None:
        """Persist embedding metadata keys to db_metadata table."""
        with self.get_connection() as conn:
            set_metadata(conn, "embedding_model_name", model_name)
            set_metadata(conn, "embedding_dimension", str(dimension))
            set_metadata(conn, "embedding_template_hash", template_hash)

    def get_embedding_metadata(self) -> dict[str, str]:
        """Fetch embedding metadata keys from db_metadata table."""
        with self.get_connection() as conn:
            return {
                "model_name": get_metadata(conn, "embedding_model_name") or "",
                "dimension": get_metadata(conn, "embedding_dimension") or "",
                "template_hash": get_metadata(conn, "embedding_template_hash") or "",
            }

    def build_agent_index(
        self,
        agent_indexer: VectorIndex,
        embedder: SentenceEmbedder,
        batch_size: int = 128,
        template_path: Optional[Path | str] = None,
    ) -> int:
        """Build dedicated agent vector index (agent.index) using concise essential serialization.

        Serializes canonical name, surname, fuller name, and dates into ~30-50 tokens.
        """
        from reconciliator.template import WEMISerializationEngine

        wemi_engine = WEMISerializationEngine(agent_template_path=template_path)

        with self.get_connection() as conn:
            # First check if WEMI agents table has records
            count_res = conn.execute("SELECT COUNT(*) AS c FROM agents;").fetchone()
            has_wemi_agents = count_res and count_res["c"] > 0

            if has_wemi_agents:
                cur = conn.execute(
                    """
                    SELECT id, agent_uri, source, wikidata_qid, name, name_normalized, surname,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation, metadata_json
                    FROM agents
                    ORDER BY id;
                    """
                )
            else:
                cur = conn.execute(
                    """
                    SELECT MIN(id) AS id, name, name_normalized, surname, role,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation,
                           metadata_json, uri_subfield_0, uri_subfield_1
                    FROM authority_agents
                    WHERE (uri_subfield_0 IS NOT NULL AND uri_subfield_0 != '[]' AND uri_subfield_0 != '')
                       OR (uri_subfield_1 IS NOT NULL AND uri_subfield_1 != '[]' AND uri_subfield_1 != '')
                    GROUP BY name, surname, dates
                    ORDER BY id;
                    """
                )

            total_indexed = 0
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                ids: list[int] = []
                texts: list[str] = []
                for r in rows:
                    agent_dict = dict(r)
                    text = wemi_engine.render_agent(agent_dict)
                    ids.append(agent_dict["id"])
                    texts.append(text)

                if texts:
                    vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    agent_indexer.add_vectors(vectors, ids=ids)
                    total_indexed += len(texts)

        agent_indexer.save()
        return total_indexed


# ---------------------------------------------------------------------------
# WEMI Entity Core Repository Helpers
# ---------------------------------------------------------------------------


def _row_to_agent(row: sqlite3.Row) -> AgentEntity:
    return AgentEntity.model_validate(_hydrate_agent_profile(dict(row)))


def _row_to_work(row: sqlite3.Row) -> WorkEntity:
    return WorkEntity.model_validate(dict(row))


def _row_to_expression(row: sqlite3.Row) -> ExpressionEntity:
    return ExpressionEntity.model_validate(dict(row))


def _row_to_manifestation(row: sqlite3.Row) -> ManifestationEntity:
    return ManifestationEntity.model_validate(dict(row))


def _row_to_item(row: sqlite3.Row) -> ItemEntity:
    return ItemEntity.model_validate(dict(row))


def _row_to_identifier(row: sqlite3.Row) -> IdentifierEntity:
    return IdentifierEntity.model_validate(dict(row))


def _row_to_contribution(row: sqlite3.Row) -> ContributionEntity:
    return ContributionEntity.model_validate(dict(row))


def _row_to_work_relationship(row: sqlite3.Row) -> WorkRelationship:
    return WorkRelationship.model_validate(dict(row))


def _row_to_harvest_state(row: sqlite3.Row) -> HarvestState:
    return HarvestState.model_validate(dict(row))


class WEMIRepository:
    """Repository managing access to WEMI relational tables in authority.db,

    FTS5 lexical search, and tripartite vector indices (Work, Expression, Manifestation).
    """

    def __init__(
        self,
        db_path: Path | str,
        work_index_path: Optional[Path | str] = None,
        expression_index_path: Optional[Path | str] = None,
        manifestation_index_path: Optional[Path | str] = None,
        dimension: int = 768,
        vector_engine_config: Optional[VectorEngineConfig] = None,
        fts_config: Optional["RepositoryFTSConfig"] = None,
        work_indexer: Optional[VectorIndex] = None,
        expression_indexer: Optional[VectorIndex] = None,
        manifestation_indexer: Optional[VectorIndex] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.dimension = dimension
        self.vector_engine_config = vector_engine_config
        if fts_config is not None:
            self.fts_config = fts_config
        else:
            from reconciliator.config import RepositoryFTSConfig

            self.fts_config = RepositoryFTSConfig()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            init_authority_db(conn)

        # Work indexer
        if work_indexer is not None:
            self.work_indexer = work_indexer
        elif work_index_path is not None:
            self.work_indexer = create_vector_engine(
                Path(work_index_path), dimension=self.dimension, config=vector_engine_config
            )
        else:
            self.work_indexer = None

        # Expression indexer
        if expression_indexer is not None:
            self.expression_indexer = expression_indexer
        elif expression_index_path is not None:
            self.expression_indexer = create_vector_engine(
                Path(expression_index_path), dimension=self.dimension, config=vector_engine_config
            )
        else:
            self.expression_indexer = None

        # Manifestation indexer
        if manifestation_indexer is not None:
            self.manifestation_indexer = manifestation_indexer
        elif manifestation_index_path is not None:
            self.manifestation_indexer = create_vector_engine(
                Path(manifestation_index_path),
                dimension=self.dimension,
                config=vector_engine_config,
            )
        else:
            self.manifestation_indexer = None

    def get_connection(self) -> sqlite3.Connection:
        """Obtain connection to authority.db with foreign keys enabled."""
        return get_db_connection(self.db_path)

    # -----------------------------------------------------------------------
    # Batch Insertions
    # -----------------------------------------------------------------------

    def insert_agents_batch(self, agents: list[AgentEntity]) -> list[int]:
        """Insert or update AgentEntity records in batch."""
        if not agents:
            return []
        agent_ids: list[int] = []
        with self.get_connection() as conn:
            for agent in agents:
                agent_meta: dict[str, Any] = {}
                if agent.metadata_json:
                    try:
                        agent_meta = json.loads(agent.metadata_json)
                    except Exception:
                        agent_meta = {}
                if agent.qualifier_counts:
                    agent_meta["qualifier_counts"] = agent.qualifier_counts
                if agent.affiliation_counts:
                    agent_meta["affiliation_counts"] = agent.affiliation_counts
                if agent.role_counts:
                    agent_meta["role_counts"] = agent.role_counts
                if agent.language_counts:
                    agent_meta["language_counts"] = agent.language_counts
                if agent.subject_counts:
                    agent_meta["subject_counts"] = agent.subject_counts
                if agent.orcid:
                    agent_meta["orcid"] = agent.orcid
                if agent.variant_names:
                    agent_meta["variant_names"] = agent.variant_names
                meta_str = json.dumps(agent_meta, ensure_ascii=False) if agent_meta else None

                cursor = conn.execute(
                    """
                    INSERT INTO agents (
                        agent_uri, source, wikidata_qid, name, name_normalized,
                        surname, qualifier, dates, birth_year, death_year,
                        fuller_name, affiliation, metadata_json, is_personal, is_fictional
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_uri) DO UPDATE SET
                        source = excluded.source,
                        wikidata_qid = COALESCE(excluded.wikidata_qid, agents.wikidata_qid),
                        name = excluded.name,
                        name_normalized = excluded.name_normalized,
                        surname = COALESCE(excluded.surname, agents.surname),
                        qualifier = COALESCE(excluded.qualifier, agents.qualifier),
                        dates = COALESCE(excluded.dates, agents.dates),
                        birth_year = COALESCE(excluded.birth_year, agents.birth_year),
                        death_year = COALESCE(excluded.death_year, agents.death_year),
                        fuller_name = COALESCE(excluded.fuller_name, agents.fuller_name),
                        affiliation = COALESCE(excluded.affiliation, agents.affiliation),
                        metadata_json = COALESCE(excluded.metadata_json, agents.metadata_json),
                        is_personal = excluded.is_personal,
                        is_fictional = excluded.is_fictional
                    RETURNING id;
                    """,
                    (
                        agent.agent_uri,
                        agent.source,
                        agent.wikidata_qid,
                        agent.name,
                        agent.name_normalized,
                        agent.surname,
                        None,
                        agent.dates,
                        agent.birth_year,
                        agent.death_year,
                        agent.fuller_name,
                        None,
                        meta_str,
                        1 if agent.is_personal else 0,
                        1 if agent.is_fictional else 0,
                    ),
                )
                agent_ids.append(cursor.fetchone()["id"])
        return agent_ids

    def insert_works_batch(
        self,
        works: list[WorkEntity],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert or update WorkEntity records and optional dense vectors in batch."""
        if not works:
            return []
        work_ids: list[int] = []
        with self.get_connection() as conn:
            for work in works:
                cursor = conn.execute(
                    """
                    INSERT INTO works (
                        work_uri, source, source_id, wikidata_qid, title,
                        title_normalized, original_language, primary_agent_id,
                        ykl_class, work_type, subjects_json, serialized_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(work_uri) DO UPDATE SET
                        source = excluded.source,
                        source_id = excluded.source_id,
                        wikidata_qid = COALESCE(excluded.wikidata_qid, works.wikidata_qid),
                        title = excluded.title,
                        title_normalized = excluded.title_normalized,
                        original_language = COALESCE(excluded.original_language, works.original_language),
                        primary_agent_id = COALESCE(excluded.primary_agent_id, works.primary_agent_id),
                        ykl_class = COALESCE(excluded.ykl_class, works.ykl_class),
                        work_type = excluded.work_type,
                        subjects_json = excluded.subjects_json,
                        serialized_text = excluded.serialized_text
                    RETURNING id;
                    """,
                    (
                        work.work_uri,
                        work.source,
                        work.source_id,
                        work.wikidata_qid,
                        work.title,
                        work.title_normalized,
                        work.original_language,
                        work.primary_agent_id,
                        work.ykl_class,
                        work.work_type,
                        json.dumps(work.subjects_json, ensure_ascii=False),
                        work.serialized_text,
                    ),
                )
                work_ids.append(cursor.fetchone()["id"])

        if vectors is not None and self.work_indexer is not None and len(vectors) == len(works):
            vector_ids = self.work_indexer.add_vectors(vectors, ids=work_ids)
            with self.get_connection() as conn:
                mapping_rows = list(zip(vector_ids, work_ids))
                conn.executemany(
                    "INSERT OR REPLACE INTO work_vectors (vector_id, work_id) VALUES (?, ?);",
                    mapping_rows,
                )

        return work_ids

    def insert_expressions_batch(
        self,
        expressions: list[ExpressionEntity],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert or update ExpressionEntity records and optional dense vectors in batch."""
        if not expressions:
            return []
        expression_ids: list[int] = []
        with self.get_connection() as conn:
            for exp in expressions:
                cursor = conn.execute(
                    """
                    INSERT INTO expressions (
                        work_id, expression_uri, source, source_id, wikidata_qid,
                        language, content_type, title, version_designation,
                        arrangement_note, medium_of_performance, serialized_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(expression_uri) DO UPDATE SET
                        work_id = excluded.work_id,
                        source = excluded.source,
                        source_id = excluded.source_id,
                        wikidata_qid = COALESCE(excluded.wikidata_qid, expressions.wikidata_qid),
                        language = excluded.language,
                        content_type = excluded.content_type,
                        title = excluded.title,
                        version_designation = excluded.version_designation,
                        arrangement_note = excluded.arrangement_note,
                        medium_of_performance = excluded.medium_of_performance,
                        serialized_text = excluded.serialized_text
                    RETURNING id;
                    """,
                    (
                        exp.work_id,
                        exp.expression_uri,
                        exp.source,
                        exp.source_id,
                        exp.wikidata_qid,
                        exp.language,
                        exp.content_type,
                        exp.title,
                        exp.version_designation,
                        exp.arrangement_note,
                        exp.medium_of_performance,
                        exp.serialized_text,
                    ),
                )
                expression_ids.append(cursor.fetchone()["id"])

        if (
            vectors is not None
            and self.expression_indexer is not None
            and len(vectors) == len(expressions)
        ):
            vector_ids = self.expression_indexer.add_vectors(vectors, ids=expression_ids)
            with self.get_connection() as conn:
                mapping_rows = list(zip(vector_ids, expression_ids))
                conn.executemany(
                    "INSERT OR REPLACE INTO expression_vectors (vector_id, expression_id) VALUES (?, ?);",
                    mapping_rows,
                )

        return expression_ids

    def insert_manifestations_batch(
        self,
        manifestations: list[ManifestationEntity],
        vectors: Optional[np.ndarray] = None,
    ) -> list[int]:
        """Insert or update ManifestationEntity records and optional dense vectors in batch."""
        if not manifestations:
            return []
        manif_ids: list[int] = []
        with self.get_connection() as conn:
            for manif in manifestations:
                cursor = conn.execute(
                    """
                    INSERT INTO manifestations (
                        work_id, container_id, is_component, component_type,
                        manifestation_uri, source, source_id, title,
                        title_normalized, subtitle, publication_year, publisher,
                        publication_place, edition_statement, edition_number,
                        series_title, series_numbering, volume_designation,
                        issue_enumeration, pagination, extent, carrier_type,
                        host_title, serialized_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        work_id = COALESCE(excluded.work_id, manifestations.work_id),
                        container_id = COALESCE(excluded.container_id, manifestations.container_id),
                        is_component = excluded.is_component,
                        component_type = excluded.component_type,
                        manifestation_uri = COALESCE(excluded.manifestation_uri, manifestations.manifestation_uri),
                        source = excluded.source,
                        title = excluded.title,
                        title_normalized = excluded.title_normalized,
                        subtitle = excluded.subtitle,
                        publication_year = excluded.publication_year,
                        publisher = excluded.publisher,
                        publication_place = excluded.publication_place,
                        edition_statement = excluded.edition_statement,
                        edition_number = excluded.edition_number,
                        series_title = excluded.series_title,
                        series_numbering = excluded.series_numbering,
                        volume_designation = excluded.volume_designation,
                        issue_enumeration = excluded.issue_enumeration,
                        pagination = excluded.pagination,
                        extent = excluded.extent,
                        carrier_type = excluded.carrier_type,
                        host_title = excluded.host_title,
                        serialized_text = excluded.serialized_text
                    RETURNING id;
                    """,
                    (
                        manif.work_id,
                        manif.container_id,
                        1 if manif.is_component else 0,
                        manif.component_type,
                        manif.manifestation_uri,
                        manif.source,
                        manif.source_id,
                        manif.title,
                        manif.title_normalized,
                        manif.subtitle,
                        manif.publication_year,
                        manif.publisher,
                        manif.publication_place,
                        manif.edition_statement,
                        manif.edition_number,
                        manif.series_title,
                        manif.series_numbering,
                        manif.volume_designation,
                        manif.issue_enumeration,
                        manif.pagination,
                        manif.extent,
                        manif.carrier_type,
                        manif.host_title,
                        manif.serialized_text,
                    ),
                )
                manif_ids.append(cursor.fetchone()["id"])

        if (
            vectors is not None
            and self.manifestation_indexer is not None
            and len(vectors) == len(manifestations)
        ):
            vector_ids = self.manifestation_indexer.add_vectors(vectors, ids=manif_ids)
            with self.get_connection() as conn:
                mapping_rows = list(zip(vector_ids, manif_ids))
                conn.executemany(
                    "INSERT OR REPLACE INTO manifestation_vectors (vector_id, manifestation_id) VALUES (?, ?);",
                    mapping_rows,
                )

        return manif_ids

    def insert_manifestation_expressions_batch(
        self, assocs: list[ManifestationExpressionAssociation]
    ) -> None:
        """Insert or replace junction associations between Manifestations and Expressions."""
        if not assocs:
            return
        rows = [
            (
                a.manifestation_id,
                a.expression_id,
                a.track_number,
                a.disc_number,
                a.duration,
                1 if a.is_primary else 0,
            )
            for a in assocs
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO manifestation_expressions (
                    manifestation_id, expression_id, track_number, disc_number, duration, is_primary
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def link_manifestation_expression(
        self, manifestation_id: int, expression_id: int, is_primary: bool = True
    ) -> None:
        """Convenience method to link a Manifestation to an Expression."""
        self.insert_manifestation_expressions_batch(
            [
                ManifestationExpressionAssociation(
                    manifestation_id=manifestation_id,
                    expression_id=expression_id,
                    is_primary=is_primary,
                )
            ]
        )

    def insert_work(self, work: WorkEntity, vector: Optional[np.ndarray] = None) -> int:
        """Convenience method to insert a single WorkEntity."""
        vectors = np.expand_dims(vector, axis=0) if vector is not None else None
        return self.insert_works_batch([work], vectors=vectors)[0]

    def insert_expression(
        self, expression: ExpressionEntity, vector: Optional[np.ndarray] = None
    ) -> int:
        """Convenience method to insert a single ExpressionEntity."""
        vectors = np.expand_dims(vector, axis=0) if vector is not None else None
        return self.insert_expressions_batch([expression], vectors=vectors)[0]

    def insert_manifestation(
        self, manifestation: ManifestationEntity, vector: Optional[np.ndarray] = None
    ) -> int:
        """Convenience method to insert a single ManifestationEntity."""
        vectors = np.expand_dims(vector, axis=0) if vector is not None else None
        return self.insert_manifestations_batch([manifestation], vectors=vectors)[0]

    def insert_items_batch(self, items: list[ItemEntity]) -> list[int]:
        """Insert ItemEntity records in batch."""
        if not items:
            return []
        item_ids: list[int] = []
        with self.get_connection() as conn:
            for item in items:
                cursor = conn.execute(
                    """
                    INSERT INTO items (
                        manifestation_id, item_uri, source, source_id,
                        holding_organization, shelf_mark, barcode, custodial_history, item_condition
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id;
                    """,
                    (
                        item.manifestation_id,
                        item.item_uri,
                        item.source,
                        item.source_id,
                        item.holding_organization,
                        item.shelf_mark,
                        item.barcode,
                        item.custodial_history,
                        item.item_condition,
                    ),
                )
                item_ids.append(cursor.fetchone()["id"])
        return item_ids

    def insert_identifiers_batch(self, identifiers: list[IdentifierEntity]) -> list[int]:
        """Insert polymorphic IdentifierEntity records in batch."""
        if not identifiers:
            return []
        id_ids: list[int] = []
        with self.get_connection() as conn:
            for ident in identifiers:
                cursor = conn.execute(
                    """
                    INSERT INTO identifiers (
                        entity_type, entity_id, scheme, value, value_normalized
                    ) VALUES (?, ?, ?, ?, ?)
                    RETURNING id;
                    """,
                    (
                        ident.entity_type,
                        ident.entity_id,
                        ident.scheme,
                        ident.value,
                        ident.value_normalized,
                    ),
                )
                id_ids.append(cursor.fetchone()["id"])
        return id_ids

    def insert_contributions_batch(self, contributions: list[ContributionEntity]) -> list[int]:
        """Insert ContributionEntity records in batch."""
        if not contributions:
            return []
        c_ids: list[int] = []
        with self.get_connection() as conn:
            for c in contributions:
                cursor = conn.execute(
                    """
                    INSERT INTO contributions (
                        entity_type, entity_id, agent_id, role, is_primary, field_tag
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    RETURNING id;
                    """,
                    (
                        c.entity_type,
                        c.entity_id,
                        c.agent_id,
                        c.role,
                        1 if c.is_primary else 0,
                        c.field_tag,
                    ),
                )
                c_ids.append(cursor.fetchone()["id"])
        return c_ids

    def insert_work_relationships_batch(self, rels: list[WorkRelationship]) -> list[int]:
        """Insert WorkRelationship records in batch."""
        if not rels:
            return []
        r_ids: list[int] = []
        with self.get_connection() as conn:
            for rel in rels:
                cursor = conn.execute(
                    """
                    INSERT INTO work_relationships (
                        source_work_id, target_work_id, relationship_type
                    ) VALUES (?, ?, ?)
                    RETURNING id;
                    """,
                    (
                        rel.source_work_id,
                        rel.target_work_id,
                        rel.relationship_type,
                    ),
                )
                r_ids.append(cursor.fetchone()["id"])
        return r_ids

    def insert_medium_of_performance_batch(self, mops: list[MediumOfPerformance]) -> None:
        """Insert or replace MediumOfPerformance records in batch."""
        if not mops:
            return
        rows = [
            (
                m.work_id,
                m.medium_uri,
                m.label,
                m.player_count,
                1 if m.is_soloist else 0,
            )
            for m in mops
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO work_medium_of_performance (
                    work_id, medium_uri, label, player_count, is_soloist
                ) VALUES (?, ?, ?, ?, ?);
                """,
                rows,
            )

    def insert_wemi_batch(
        self,
        works: list[WorkEntity],
        expressions: list[ExpressionEntity],
        manifestations: list[ManifestationEntity],
        items: Optional[list[ItemEntity]] = None,
        agents: Optional[list[AgentEntity]] = None,
        relationships: Optional[list[WorkRelationship]] = None,
        identifiers: Optional[list[IdentifierEntity]] = None,
        manifestation_expressions: Optional[list[ManifestationExpressionAssociation]] = None,
        mediums_of_performance: Optional[list[MediumOfPerformance]] = None,
        contributions: Optional[list[ContributionEntity]] = None,
        work_vectors: Optional[Any] = None,
        expression_vectors: Optional[Any] = None,
        manifestation_vectors: Optional[Any] = None,
    ) -> None:
        """Coordinated batch insertion of WEMI domain entities with foreign key binding."""
        # 1. Agents
        agent_key_to_id: dict[Any, int] = {}
        if agents:
            a_ids = self.insert_agents_batch(agents)
            for a, aid in zip(agents, a_ids):
                a.id = aid
                agent_key_to_id[a.agent_uri] = aid
                agent_key_to_id[a.name] = aid
                agent_key_to_id[a.name_normalized] = aid

        # 2. Works
        work_key_to_id: dict[Any, int] = {}
        if works:
            w_ids = self.insert_works_batch(works, vectors=work_vectors)
            for w, wid in zip(works, w_ids):
                w.id = wid
                work_key_to_id[w.work_uri] = wid
                work_key_to_id[(w.source, w.source_id)] = wid

        # 3. Expressions
        expr_key_to_id: dict[Any, int] = {}
        if expressions:
            for exp in expressions:
                if not exp.work_id or exp.work_id <= 0:
                    cand_id = work_key_to_id.get((exp.source, exp.source_id))
                    if cand_id is None and len(works) == 1:
                        cand_id = works[0].id
                    if cand_id:
                        exp.work_id = cand_id
            e_ids = self.insert_expressions_batch(expressions, vectors=expression_vectors)
            for exp, eid in zip(expressions, e_ids):
                exp.id = eid
                expr_key_to_id[exp.expression_uri] = eid
                expr_key_to_id[(exp.source, exp.source_id)] = eid

        # 4. Manifestations
        manif_key_to_id: dict[Any, int] = {}
        if manifestations:
            for m in manifestations:
                if not m.work_id or m.work_id <= 0:
                    cand_id = work_key_to_id.get((m.source, m.source_id))
                    if cand_id is None and len(works) == 1:
                        cand_id = works[0].id
                    if cand_id:
                        m.work_id = cand_id
            m_ids = self.insert_manifestations_batch(manifestations, vectors=manifestation_vectors)
            for m, mid in zip(manifestations, m_ids):
                m.id = mid
                manif_key_to_id[m.manifestation_uri] = mid
                manif_key_to_id[(m.source, m.source_id)] = mid

        # 5. Manifestation-Expression Associations
        if manifestation_expressions:
            for assoc in manifestation_expressions:
                if (not assoc.manifestation_id or assoc.manifestation_id <= 0) and manifestations:
                    assoc.manifestation_id = manifestations[0].id
                if (not assoc.expression_id or assoc.expression_id <= 0) and expressions:
                    assoc.expression_id = expressions[0].id
            self.insert_manifestation_expressions_batch(manifestation_expressions)

        # 6. Items
        if items:
            for item in items:
                if not item.manifestation_id or item.manifestation_id <= 0:
                    cand_id = manif_key_to_id.get((item.source, item.source_id))
                    if cand_id is None and len(manifestations) == 1:
                        cand_id = manifestations[0].id
                    if cand_id:
                        item.manifestation_id = cand_id
            self.insert_items_batch(items)

        # 7. Identifiers
        if identifiers:
            for ident in identifiers:
                if not ident.entity_id or ident.entity_id <= 0:
                    if ident.entity_type == "manifestation" and manifestations:
                        ident.entity_id = manifestations[0].id
                    elif ident.entity_type == "work" and works:
                        ident.entity_id = works[0].id
                    elif ident.entity_type == "expression" and expressions:
                        ident.entity_id = expressions[0].id
            self.insert_identifiers_batch(identifiers)

        # 8. Work Relationships
        if relationships:
            for rel in relationships:
                if (not rel.source_work_id or rel.source_work_id <= 0) and works:
                    rel.source_work_id = works[0].id
            self.insert_work_relationships_batch(relationships)

        # 9. Mediums of Performance
        if mediums_of_performance:
            for mop in mediums_of_performance:
                if (not mop.work_id or mop.work_id <= 0) and works:
                    mop.work_id = works[0].id
            self.insert_medium_of_performance_batch(mediums_of_performance)

        # 10. Contributions
        if contributions:
            valid_contribs: list[ContributionEntity] = []
            for c in contributions:
                if not c.entity_id or c.entity_id <= 0:
                    if c.entity_type == "work" and works:
                        c.entity_id = works[0].id
                    elif c.entity_type == "expression" and expressions:
                        c.entity_id = expressions[0].id
                    elif c.entity_type == "manifestation" and manifestations:
                        c.entity_id = manifestations[0].id
                if not c.agent_id or c.agent_id <= 0:
                    if agents:
                        c.agent_id = agents[0].id
                if c.agent_id and c.agent_id > 0 and c.entity_id and c.entity_id > 0:
                    valid_contribs.append(c)
            if valid_contribs:
                self.insert_contributions_batch(valid_contribs)

    # -----------------------------------------------------------------------
    # Lookups & Retrievals
    # -----------------------------------------------------------------------

    def get_agent(self, agent_id: int) -> Optional[AgentEntity]:
        """Fetch AgentEntity by ID."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?;", (agent_id,)).fetchone()
            return _row_to_agent(row) if row else None

    def get_agent_by_uri(self, agent_uri: str) -> Optional[AgentEntity]:
        """Fetch AgentEntity by agent_uri, with fallback to concept_id_to_uri."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_uri = ?;", (agent_uri,)).fetchone()
            if not row:
                expanded = concept_id_to_uri(agent_uri)
                if expanded and expanded != agent_uri:
                    row = conn.execute("SELECT * FROM agents WHERE agent_uri = ?;", (expanded,)).fetchone()
            return _row_to_agent(row) if row else None

    def get_work(self, work_id: int) -> Optional[WorkEntity]:
        """Fetch WorkEntity by ID."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM works WHERE id = ?;", (work_id,)).fetchone()
            return _row_to_work(row) if row else None

    def get_work_by_uri(self, work_uri: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by work_uri, with fallback to concept_id_to_uri."""
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM works WHERE work_uri = ?;", (work_uri,)).fetchone()
            if not row:
                expanded = concept_id_to_uri(work_uri)
                if expanded and expanded != work_uri:
                    row = conn.execute("SELECT * FROM works WHERE work_uri = ?;", (expanded,)).fetchone()
            return _row_to_work(row) if row else None

    def get_work_by_source(self, source: str, source_id: str) -> Optional[WorkEntity]:
        """Fetch WorkEntity by source and source_id."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM works WHERE source = ? AND source_id = ?;", (source, source_id)
            ).fetchone()
            return _row_to_work(row) if row else None

    def get_expression(self, expression_id: int) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM expressions WHERE id = ?;", (expression_id,)
            ).fetchone()
            return _row_to_expression(row) if row else None

    def get_expression_by_uri(self, expression_uri: str) -> Optional[ExpressionEntity]:
        """Fetch ExpressionEntity by expression_uri."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM expressions WHERE expression_uri = ?;", (expression_uri,)
            ).fetchone()
            return _row_to_expression(row) if row else None

    def get_manifestation(self, manifestation_id: int) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM manifestations WHERE id = ?;", (manifestation_id,)
            ).fetchone()
            return _row_to_manifestation(row) if row else None

    def get_manifestation_by_uri(self, manifestation_uri: str) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by manifestation_uri."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM manifestations WHERE manifestation_uri = ?;", (manifestation_uri,)
            ).fetchone()
            return _row_to_manifestation(row) if row else None

    def get_manifestation_by_source(
        self, source: str, source_id: str
    ) -> Optional[ManifestationEntity]:
        """Fetch ManifestationEntity by source and source_id."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM manifestations WHERE source = ? AND source_id = ?;",
                (source, source_id),
            ).fetchone()
            return _row_to_manifestation(row) if row else None

    def get_expressions_for_work(self, work_id: int) -> list[ExpressionEntity]:
        """Fetch all Expression entities linked to a given work_id."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM expressions WHERE work_id = ? ORDER BY id;", (work_id,)
            ).fetchall()
            return [_row_to_expression(r) for r in rows]

    def get_manifestations_for_work(self, work_id: int) -> list[ManifestationEntity]:
        """Fetch all Manifestation entities linked directly to a given work_id."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM manifestations WHERE work_id = ? ORDER BY publication_year ASC, id ASC;",
                (work_id,),
            ).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_expressions_for_manifestation(self, manifestation_id: int) -> list[ExpressionEntity]:
        """Fetch Expression entities associated with a manifestation via manifestation_expressions."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT e.*
                FROM manifestation_expressions me
                JOIN expressions e ON me.expression_id = e.id
                WHERE me.manifestation_id = ?
                ORDER BY me.disc_number ASC, me.track_number ASC, e.id ASC;
                """,
                (manifestation_id,),
            ).fetchall()
            return [_row_to_expression(r) for r in rows]

    def get_manifestations_for_expression(self, expression_id: int) -> list[ManifestationEntity]:
        """Fetch Manifestation entities associated with an expression via manifestation_expressions."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT m.*
                FROM manifestation_expressions me
                JOIN manifestations m ON me.manifestation_id = m.id
                WHERE me.expression_id = ?
                ORDER BY m.publication_year ASC, m.id ASC;
                """,
                (expression_id,),
            ).fetchall()
            return [_row_to_manifestation(r) for r in rows]

    def get_items_for_manifestation(self, manifestation_id: int) -> list[ItemEntity]:
        """Fetch all Item entities held under a given manifestation."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE manifestation_id = ? ORDER BY id;",
                (manifestation_id,),
            ).fetchall()
            return [_row_to_item(r) for r in rows]

    def get_work_relationships(self, work_id: int) -> list[WorkRelationship]:
        """Fetch all relationships where work_id is either source or target."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM work_relationships WHERE source_work_id = ? OR target_work_id = ? ORDER BY id;",
                (work_id, work_id),
            ).fetchall()
            return [_row_to_work_relationship(r) for r in rows]

    def get_identifiers_for_entity(
        self, entity_type: EntityType, entity_id: int
    ) -> list[IdentifierEntity]:
        """Fetch all polymorphic identifiers for a specific entity."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM identifiers WHERE entity_type = ? AND entity_id = ? ORDER BY id;",
                (entity_type, entity_id),
            ).fetchall()
            return [_row_to_identifier(r) for r in rows]

    def get_contributions_for_entity(
        self, entity_type: str, entity_id: int
    ) -> list[ContributionEntity]:
        """Fetch all contributions for a specific entity."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM contributions WHERE entity_type = ? AND entity_id = ? ORDER BY is_primary DESC, id ASC;",
                (entity_type, entity_id),
            ).fetchall()
            return [_row_to_contribution(r) for r in rows]

    def lookup_by_identifier(self, scheme: str, value: str) -> list[tuple[EntityType, int]]:
        """Look up entity types and IDs matching a scheme and identifier value."""
        norm_val = value.strip().lower()
        clean_val = re.sub(r"[^0-9a-zA-Z]", "", value).lower()
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT entity_type, entity_id
                FROM identifiers
                WHERE scheme = ? AND (value_normalized = ? OR value_normalized = ? OR value = ?);
                """,
                (scheme, norm_val, clean_val, value.strip()),
            ).fetchall()
            return [(r["entity_type"], r["entity_id"]) for r in rows]

    def get_mediums_of_performance(self, work_id: int) -> list[MediumOfPerformance]:
        """Fetch MediumOfPerformance records for a given work."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM work_medium_of_performance WHERE work_id = ?;", (work_id,)
            ).fetchall()
            return [
                MediumOfPerformance(
                    work_id=r["work_id"],
                    medium_uri=r["medium_uri"],
                    label=r["label"],
                    player_count=r["player_count"],
                    is_soloist=bool(r["is_soloist"]),
                )
                for r in rows
            ]

    def find_works_by_author_and_title(
        self, author_key: Optional[str], title_stem: str, work_type: Optional[str] = None
    ) -> list[WorkEntity]:
        """Look up works matching title stem and optional author key/surname."""
        clean_title = title_stem.strip().lower()
        with self.get_connection() as conn:
            query = """
                SELECT w.* FROM works w
                LEFT JOIN agents a ON w.primary_agent_id = a.id
                WHERE (w.title_normalized LIKE ? OR w.title_normalized = ?)
            """
            params: list[Any] = [f"%{clean_title}%", clean_title]
            if author_key:
                clean_author = author_key.strip().lower()
                query += " AND (w.primary_agent_id IS NULL OR a.surname LIKE ? OR a.name_normalized LIKE ?)"
                params.extend([f"%{clean_author}%", f"%{clean_author}%"])
            if work_type:
                query += " AND w.work_type = ?"
                params.append(work_type)
            query += " LIMIT 20;"
            rows = conn.execute(query, params).fetchall()
            return [_row_to_work(r) for r in rows]

    def find_works_by_type(self, work_type: str, limit: int = 50) -> list[WorkEntity]:
        """Fetch works filtered by work_type."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM works WHERE work_type = ? LIMIT ?;", (work_type, limit)
            ).fetchall()
            return [_row_to_work(r) for r in rows]

    def get_indexed_manifestation_source_ids(self, source: str) -> set[str]:
        """Return set of manifestation source_ids stored in authority.db for a given source."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT source_id FROM manifestations WHERE source = ?;", (source,)
            )
            return {row["source_id"] for row in cursor if row["source_id"]}

    # -----------------------------------------------------------------------
    # Database-Level SQLite FTS5 Lexical Search (Engine-Level BM25 Ranking)
    #
    # NOTE ON ARCHITECTURAL DISTINCTION:
    # This module provides database-level full-text search against SQLite FTS5
    # virtual tables (works_fts, expressions_fts, manifestations_fts). Ranking is
    # computed directly inside SQLite using its native C implementation of bm25()
    # (or configured ranking expression/weights from RepositoryFTSConfig in storage.fts).
    #
    # This is distinct from:
    # 1) ReconcileFTSFallbackConfig (reconcile.fts_fallback in matcher.py):
    #    Governs the reconciliation matcher's funnel fallback policy (activation toggle,
    #    candidate limit, and confidence score assigned to tier3_fts_fallback).
    # 2) RerankConfig (reconcile.rerank in reconciliator.rerank.bm25):
    #    In-memory candidate evaluation in Python using the rank-bm25 library for
    #    candidate pair scoring, dense/sparse score fusion, and metadata discriminators.
    # -----------------------------------------------------------------------

    def _build_fts_query_and_order(
        self,
        query: str,
        table_name: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
    ) -> tuple[str, str, int]:
        """Format MATCH query string, ORDER BY clause, and limit based on config or explicit parameters."""
        eff_limit = limit if limit is not None else (self.fts_config.default_limit if self.fts_config else 20)
        eff_rank = ranking_function or (self.fts_config.ranking_function if self.fts_config else "bm25")
        eff_weights = weights if weights is not None else (self.fts_config.column_weights if self.fts_config else None)
        eff_prefix = prefix_search if prefix_search is not None else (self.fts_config.prefix_search if self.fts_config else False)
        clean_query = query.strip()
        if eff_prefix and clean_query:
            tokens = clean_query.split()
            processed_tokens: list[str] = []
            for t in tokens:
                token_clean = t.rstrip("*")
                if not token_clean:
                    continue
                if (
                    token_clean.upper() in {"OR", "AND", "NOT"}
                    or token_clean.startswith('"')
                    or token_clean.endswith('"')
                ):
                    processed_tokens.append(token_clean)
                else:
                    processed_tokens.append(f"{token_clean}*")
            clean_query = " ".join(processed_tokens)

        if eff_weights:
            weights_str = ", ".join(str(float(w)) for w in eff_weights)
            order_clause = f"bm25({table_name}, {weights_str})"
        elif eff_rank and eff_rank.lower() != "bm25":
            order_clause = eff_rank
        else:
            order_clause = "f.rank"

        return clean_query, order_clause, eff_limit

    def search_works_fts(
        self,
        query: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
    ) -> list[tuple[WorkEntity, float]]:
        """Search works via SQLite FTS5. Returns list of (WorkEntity, bm25_rank)."""
        if not query or not query.strip():
            return []
        match_query, order_clause, eff_limit = self._build_fts_query_and_order(
            query, "works_fts", limit, ranking_function, weights, prefix_search
        )
        with self.get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT w.*, {order_clause} AS rank
                FROM works_fts f
                JOIN works w ON f.rowid = w.id
                WHERE works_fts MATCH ?
                ORDER BY rank
                LIMIT ?;
                """,
                (match_query, eff_limit),
            )
            return [(_row_to_work(row), float(row["rank"])) for row in cursor]

    def search_expressions_fts(
        self,
        query: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
    ) -> list[tuple[ExpressionEntity, float]]:
        """Search expressions via SQLite FTS5. Returns list of (ExpressionEntity, bm25_rank)."""
        if not query or not query.strip():
            return []
        match_query, order_clause, eff_limit = self._build_fts_query_and_order(
            query, "expressions_fts", limit, ranking_function, weights, prefix_search
        )
        with self.get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT e.*, {order_clause} AS rank
                FROM expressions_fts f
                JOIN expressions e ON f.rowid = e.id
                WHERE expressions_fts MATCH ?
                ORDER BY rank
                LIMIT ?;
                """,
                (match_query, eff_limit),
            )
            return [(_row_to_expression(row), float(row["rank"])) for row in cursor]

    def search_manifestations_fts(
        self,
        query: str,
        limit: Optional[int] = None,
        ranking_function: Optional[str] = None,
        weights: Optional[list[float]] = None,
        prefix_search: Optional[bool] = None,
    ) -> list[tuple[ManifestationEntity, float]]:
        """Search manifestations via SQLite FTS5. Returns list of (ManifestationEntity, bm25_rank)."""
        if not query or not query.strip():
            return []
        match_query, order_clause, eff_limit = self._build_fts_query_and_order(
            query, "manifestations_fts", limit, ranking_function, weights, prefix_search
        )
        with self.get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT m.*, {order_clause} AS rank
                FROM manifestations_fts f
                JOIN manifestations m ON f.rowid = m.id
                WHERE manifestations_fts MATCH ?
                ORDER BY rank
                LIMIT ?;
                """,
                (match_query, eff_limit),
            )
            return [(_row_to_manifestation(row), float(row["rank"])) for row in cursor]

    # -----------------------------------------------------------------------
    # Dense Vector Search
    # -----------------------------------------------------------------------

    def search_work_vectors(
        self, query_vector: np.ndarray, top_k: int = 20
    ) -> list[tuple[WorkEntity, float]]:
        """Retrieve top-k candidate works from the work vector index."""
        if self.work_indexer is None or self.work_indexer.total_vectors == 0:
            return []

        scores, indices = self.work_indexer.search(query_vector, top_k=top_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_work_id: dict[int, int] = {}
        with self.get_connection() as conn:
            placeholders = ",".join("?" for _ in vector_indices)
            rows = conn.execute(
                f"SELECT vector_id, work_id FROM work_vectors WHERE vector_id IN ({placeholders});",
                vector_indices,
            ).fetchall()
            for r in rows:
                vector_to_work_id[r["vector_id"]] = r["work_id"]

        results: list[tuple[WorkEntity, float]] = []
        seen_work_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            work_id = vector_to_work_id.get(v_id, v_id if v_id >= 0 else None)
            if work_id is not None and work_id not in seen_work_ids:
                seen_work_ids.add(work_id)
                work = self.get_work(work_id)
                if work:
                    results.append((work, float(score)))

        return results

    def search_expression_vectors(
        self, query_vector: np.ndarray, top_k: int = 20
    ) -> list[tuple[ExpressionEntity, float]]:
        """Retrieve top-k candidate expressions from the expression vector index."""
        if self.expression_indexer is None or self.expression_indexer.total_vectors == 0:
            return []

        scores, indices = self.expression_indexer.search(query_vector, top_k=top_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_exp_id: dict[int, int] = {}
        with self.get_connection() as conn:
            placeholders = ",".join("?" for _ in vector_indices)
            rows = conn.execute(
                f"SELECT vector_id, expression_id FROM expression_vectors WHERE vector_id IN ({placeholders});",
                vector_indices,
            ).fetchall()
            for r in rows:
                vector_to_exp_id[r["vector_id"]] = r["expression_id"]

        results: list[tuple[ExpressionEntity, float]] = []
        seen_exp_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            exp_id = vector_to_exp_id.get(v_id, v_id if v_id >= 0 else None)
            if exp_id is not None and exp_id not in seen_exp_ids:
                seen_exp_ids.add(exp_id)
                expression = self.get_expression(exp_id)
                if expression:
                    results.append((expression, float(score)))

        return results

    def search_manifestation_vectors(
        self, query_vector: np.ndarray, top_k: int = 20
    ) -> list[tuple[ManifestationEntity, float]]:
        """Retrieve top-k candidate manifestations from the manifestation vector index."""
        if self.manifestation_indexer is None or self.manifestation_indexer.total_vectors == 0:
            return []

        scores, indices = self.manifestation_indexer.search(query_vector, top_k=top_k)
        if len(scores) == 0 or len(indices) == 0:
            return []

        vector_indices = indices[0].tolist()
        dense_scores = scores[0].tolist()

        vector_to_manif_id: dict[int, int] = {}
        with self.get_connection() as conn:
            placeholders = ",".join("?" for _ in vector_indices)
            rows = conn.execute(
                f"SELECT vector_id, manifestation_id FROM manifestation_vectors WHERE vector_id IN ({placeholders});",
                vector_indices,
            ).fetchall()
            for r in rows:
                vector_to_manif_id[r["vector_id"]] = r["manifestation_id"]

        results: list[tuple[ManifestationEntity, float]] = []
        seen_manif_ids: set[int] = set()
        for v_id, score in zip(vector_indices, dense_scores):
            manif_id = vector_to_manif_id.get(v_id, v_id if v_id >= 0 else None)
            if manif_id is not None and manif_id not in seen_manif_ids:
                seen_manif_ids.add(manif_id)
                manif = self.get_manifestation(manif_id)
                if manif:
                    results.append((manif, float(score)))

        return results

    # -----------------------------------------------------------------------
    # Harvest State & Checkpoints
    # -----------------------------------------------------------------------

    def get_harvest_state(self, source_id: str) -> Optional[HarvestState]:
        """Fetch harvest checkpoint from harvest_state."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM harvest_state WHERE source_id = ?;", (source_id,)
            ).fetchone()
            return _row_to_harvest_state(row) if row else None

    def save_harvest_state(self, state: HarvestState) -> None:
        """Upsert a harvest checkpoint in harvest_state."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO harvest_state (
                    source_id, source_type, metadata_prefix, set_spec,
                    last_harvest_from, last_harvest_until, resumption_token,
                    records_harvested, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_type = excluded.source_type,
                    metadata_prefix = excluded.metadata_prefix,
                    set_spec = excluded.set_spec,
                    last_harvest_from = excluded.last_harvest_from,
                    last_harvest_until = excluded.last_harvest_until,
                    resumption_token = excluded.resumption_token,
                    records_harvested = excluded.records_harvested,
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    state.source_id,
                    state.source_type,
                    state.metadata_prefix,
                    state.set_spec,
                    state.last_harvest_from,
                    state.last_harvest_until,
                    state.resumption_token,
                    state.records_harvested,
                    state.status,
                ),
            )

    def insert_wikidata_authors(self, authors: list[dict[str, Any]]) -> None:
        """Persist author entity metadata into wikidata_authors table and agents table."""
        if not authors:
            return
        rows = []
        for a in authors:
            if not a.get("qid"):
                continue
            idents = a.get("identifiers")
            if isinstance(idents, dict):
                idents_json = json.dumps(idents, ensure_ascii=False) if idents else None
            elif isinstance(idents, str):
                idents_json = idents
            else:
                idents_json = a.get("identifiers_json")

            rows.append(
                (
                    a["qid"],
                    a.get("name", ""),
                    a.get("family_name"),
                    idents_json,
                    a.get("birth_year"),
                    a.get("death_year"),
                    a.get("occupations"),
                    a.get("fields_of_work"),
                    a.get("affiliations"),
                    a.get("metadata_json"),
                )
            )
        agent_rows = [
            (
                format_wikidata_uri(a["qid"]),
                "wikidata",
                a["qid"],
                a.get("name") or a["qid"],
                (a.get("name") or a["qid"]).strip().lower(),
                a.get("family_name"),
                a.get("birth_year"),
                a.get("death_year"),
                a.get("metadata_json"),
            )
            for a in authors
            if a.get("qid")
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO wikidata_authors (
                    qid, name, family_name, identifiers_json,
                    birth_year, death_year, occupations, fields_of_work, affiliations, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    name = excluded.name,
                    family_name = excluded.family_name,
                    identifiers_json = COALESCE(excluded.identifiers_json, wikidata_authors.identifiers_json),
                    birth_year = COALESCE(excluded.birth_year, wikidata_authors.birth_year),
                    death_year = COALESCE(excluded.death_year, wikidata_authors.death_year),
                    occupations = COALESCE(excluded.occupations, wikidata_authors.occupations),
                    fields_of_work = COALESCE(excluded.fields_of_work, wikidata_authors.fields_of_work),
                    affiliations = COALESCE(excluded.affiliations, wikidata_authors.affiliations),
                    metadata_json = COALESCE(excluded.metadata_json, wikidata_authors.metadata_json);
                """,
                rows,
            )
            conn.executemany(
                """
                INSERT INTO agents (
                    agent_uri, source, wikidata_qid, name, name_normalized, surname,
                    birth_year, death_year, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_uri) DO UPDATE SET
                    name = excluded.name,
                    name_normalized = excluded.name_normalized,
                    surname = COALESCE(excluded.surname, agents.surname),
                    wikidata_qid = COALESCE(excluded.wikidata_qid, agents.wikidata_qid),
                    birth_year = COALESCE(excluded.birth_year, agents.birth_year),
                    death_year = COALESCE(excluded.death_year, agents.death_year),
                    metadata_json = COALESCE(excluded.metadata_json, agents.metadata_json);
                """,
                agent_rows,
            )

            # Option 3: Persist relational author identifiers into identifiers table (entity_type = 'agent')
            qids_with_idents = [a["qid"] for a in authors if a.get("qid") and a.get("identifiers")]
            if qids_with_idents:
                placeholders = ",".join("?" for _ in qids_with_idents)
                agent_records = conn.execute(
                    f"SELECT id, wikidata_qid FROM agents WHERE wikidata_qid IN ({placeholders});",
                    qids_with_idents,
                ).fetchall()
                qid_to_agent_id = {r["wikidata_qid"]: r["id"] for r in agent_records}

                ident_params = []
                for a in authors:
                    qid = a.get("qid")
                    if not qid or qid not in qid_to_agent_id:
                        continue
                    agent_id = qid_to_agent_id[qid]
                    idents = a.get("identifiers")
                    if isinstance(idents, dict):
                        for scheme, val in idents.items():
                            if val is None:
                                continue
                            val_str = str(val).strip()
                            if not val_str:
                                continue
                            norm_val = re.sub(r"[^0-9a-zA-Z]", "", val_str).lower() or val_str.lower()
                            ident_params.append(
                                ("agent", agent_id, scheme.lower(), val_str, norm_val, agent_id, scheme.lower(), val_str)
                            )
                if ident_params:
                    conn.executemany(
                        """
                        INSERT INTO identifiers (entity_type, entity_id, scheme, value, value_normalized)
                        SELECT ?, ?, ?, ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM identifiers
                            WHERE entity_type = 'agent' AND entity_id = ? AND scheme = ? AND value = ?
                        );
                        """,
                        ident_params,
                    )

    def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch cached author entities from wikidata_authors by QIDs."""
        if not qids:
            return {}
        placeholders = ",".join("?" for _ in qids)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""SELECT qid, name, family_name, identifiers_json,
                           birth_year, death_year, occupations, fields_of_work, affiliations, metadata_json
                    FROM wikidata_authors WHERE qid IN ({placeholders});""",
                qids,
            ).fetchall()
            results: dict[str, dict[str, Any]] = {}
            for row in rows:
                raw_ident = row["identifiers_json"]
                identifiers: dict[str, str] = {}
                if raw_ident:
                    try:
                        identifiers = json.loads(raw_ident)
                    except Exception:
                        identifiers = {}
                results[row["qid"]] = {
                    "qid": row["qid"],
                    "name": row["name"],
                    "family_name": row["family_name"],
                    "identifiers": identifiers,
                    "birth_year": row["birth_year"],
                    "death_year": row["death_year"],
                    "occupations": row["occupations"],
                    "fields_of_work": row["fields_of_work"],
                    "affiliations": row["affiliations"],
                    "metadata_json": row["metadata_json"],
                }
            return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from wikidata_authors by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

    def get_agents_by_ids(self, agent_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch rich agent profiles by IDs from agents or authority_agents table."""
        if not agent_ids:
            return {}
        results: dict[int, dict[str, Any]] = {}
        placeholders = ",".join("?" for _ in agent_ids)
        with self.get_connection() as conn:
            try:
                rows = conn.execute(
                    f"""
                    SELECT id, agent_uri, source, wikidata_qid, name, name_normalized, surname,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation, metadata_json
                    FROM agents WHERE id IN ({placeholders});
                    """,
                    agent_ids,
                ).fetchall()
                for r in rows:
                    results[r["id"]] = _hydrate_agent_profile(dict(r))
            except Exception:
                pass

            missing_ids = [aid for aid in agent_ids if aid not in results]
            if missing_ids:
                m_placeholders = ",".join("?" for _ in missing_ids)
                rows = conn.execute(
                    f"""
                    SELECT id, record_id, name, name_normalized, surname, role,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation,
                           metadata_json, uri_subfield_0, uri_subfield_1
                    FROM authority_agents WHERE id IN ({m_placeholders});
                    """,
                    missing_ids,
                ).fetchall()
                for r in rows:
                    results[r["id"]] = _hydrate_agent_profile(dict(r))
        return results

    def get_agent_by_id(self, agent_id: int) -> dict[str, Any] | None:
        """Fetch a single rich agent profile by ID."""
        batch = self.get_agents_by_ids([agent_id])
        return batch.get(agent_id)

    def build_agent_index(
        self,
        agent_indexer: VectorIndex,
        embedder: SentenceEmbedder,
        batch_size: int = 128,
        template_path: Optional[Path | str] = None,
    ) -> int:
        """Build dedicated agent vector index (agent.index) using concise essential serialization."""
        from reconciliator.template import WEMISerializationEngine

        wemi_engine = WEMISerializationEngine(agent_template_path=template_path)

        with self.get_connection() as conn:
            count_res = conn.execute("SELECT COUNT(*) AS c FROM agents;").fetchone()
            has_wemi_agents = count_res and count_res["c"] > 0

            if has_wemi_agents:
                cur = conn.execute(
                    """
                    SELECT id, agent_uri, source, wikidata_qid, name, name_normalized, surname,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation, metadata_json
                    FROM agents
                    ORDER BY id;
                    """
                )
            else:
                cur = conn.execute(
                    """
                    SELECT MIN(id) AS id, name, name_normalized, surname, role,
                           qualifier, dates, birth_year, death_year, fuller_name, affiliation,
                           metadata_json, uri_subfield_0, uri_subfield_1
                    FROM authority_agents
                    WHERE (uri_subfield_0 IS NOT NULL AND uri_subfield_0 != '[]' AND uri_subfield_0 != '')
                       OR (uri_subfield_1 IS NOT NULL AND uri_subfield_1 != '[]' AND uri_subfield_1 != '')
                    GROUP BY name, surname, dates
                    ORDER BY id;
                    """
                )

            total_indexed = 0
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                ids: list[int] = []
                texts: list[str] = []
                for r in rows:
                    agent_dict = dict(r)
                    text = wemi_engine.render_agent(agent_dict)
                    ids.append(agent_dict["id"])
                    texts.append(text)

                if texts:
                    vectors = embedder.embed_texts(texts, batch_size=len(texts))
                    agent_indexer.add_vectors(vectors, ids=ids)
                    total_indexed += len(texts)

        agent_indexer.save()
        return total_indexed

    # -----------------------------------------------------------------------
    # Index Persistence
    # -----------------------------------------------------------------------

    def save_indices(self, metadata: Optional[dict[str, Any]] = None) -> None:
        """Persist all active vector indices to disk."""
        if self.work_indexer is not None:
            self.work_indexer.save(metadata=metadata)
        if self.expression_indexer is not None:
            self.expression_indexer.save(metadata=metadata)
        if self.manifestation_indexer is not None:
            self.manifestation_indexer.save(metadata=metadata)
        if hasattr(self, "agent_indexer") and self.agent_indexer is not None:
            self.agent_indexer.save(metadata=metadata)


class CorpusRepository:
    """Repository managing staged target records, discovered clusters, and linkages

    in reconcile_corpus.db.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            init_corpus_db(conn)

    def get_connection(self) -> sqlite3.Connection:
        """Obtain connection to reconcile_corpus.db."""
        return get_db_connection(self.db_path)

    def insert_target_records_batch(self, records: list[TargetRecord]) -> None:
        """Batch upsert staged target records, normalized keys, and detached payloads."""
        if not records:
            return
        meta_rows = [
            (
                r.record_id,
                r.format,
                r.work_type,
                r.title,
                r.uniform_title,
                r.language,
                r.author_key,
                r.title_stem,
                r.year,
                r.volume_designation,
                1 if r.is_component else 0,
            )
            for r in records
        ]
        payload_rows = [
            (
                r.record_id,
                r.raw_payload,
                r.serialized_work,
                r.serialized_expression,
                r.serialized_manifestation,
            )
            for r in records
        ]
        rec_ids = [(r.record_id,) for r in records]
        key_rows = [
            (r.record_id, k_type, k_val.strip())
            for r in records
            for k_type, k_vals in r.keys.items()
            for k_val in k_vals
            if k_val and k_val.strip()
        ]

        with self.get_connection() as conn:
            # 1. Upsert core metadata
            conn.executemany(
                """
                INSERT INTO target_records (
                    record_id, format, work_type, title, uniform_title, language,
                    author_key, title_stem, year, volume_designation, is_component
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    format = excluded.format,
                    work_type = excluded.work_type,
                    title = excluded.title,
                    uniform_title = excluded.uniform_title,
                    language = excluded.language,
                    author_key = excluded.author_key,
                    title_stem = excluded.title_stem,
                    year = excluded.year,
                    volume_designation = excluded.volume_designation,
                    is_component = excluded.is_component;
                """,
                meta_rows,
            )

            # 2. Upsert payloads
            conn.executemany(
                """
                INSERT INTO target_payloads (
                    record_id, raw_payload, serialized_work, serialized_expression,
                    serialized_manifestation
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    raw_payload = excluded.raw_payload,
                    serialized_work = excluded.serialized_work,
                    serialized_expression = excluded.serialized_expression,
                    serialized_manifestation = excluded.serialized_manifestation;
                """,
                payload_rows,
            )

            # 3. Replace keys
            conn.executemany("DELETE FROM target_keys WHERE record_id = ?;", rec_ids)
            if key_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO target_keys (record_id, key_type, key_value)
                    VALUES (?, ?, ?);
                    """,
                    key_rows,
                )

    def get_target_record(self, record_id: str) -> Optional[TargetRecord]:
        """Fetch a single staged target record by its ID, reconstructing keys and payloads."""
        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT r.*, p.raw_payload, p.serialized_work, p.serialized_expression, p.serialized_manifestation
                FROM target_records r
                LEFT JOIN target_payloads p ON r.record_id = p.record_id
                WHERE r.record_id = ?;
                """,
                (record_id,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            key_rows = conn.execute(
                "SELECT key_type, key_value FROM target_keys WHERE record_id = ? ORDER BY id;",
                (record_id,),
            ).fetchall()
            keys: dict[str, list[str]] = {}
            for kr in key_rows:
                kt, kv = kr["key_type"], kr["key_value"]
                if kt not in keys:
                    keys[kt] = []
                keys[kt].append(kv)
            d["keys"] = keys
            return TargetRecord.model_validate(d)

    def count_target_records(self) -> int:
        """Count total target records staged in corpus."""
        with self.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM target_records;").fetchone()[0]

    def insert_manifestation_clusters_batch(self, clusters: list[ManifestationCluster]) -> None:
        """Batch insert or replace carrier duplicate clusters."""
        if not clusters:
            return
        rows = [
            (
                c.cluster_id,
                c.canonical_record_id,
                c.publisher,
                c.publication_year,
                c.edition_statement,
                c.extent,
                c.carrier_type,
                c.member_record_count,
            )
            for c in clusters
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO corpus_manifestation_clusters (
                    cluster_id, canonical_record_id, publisher, publication_year,
                    edition_statement, extent, carrier_type, member_record_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def insert_work_clusters_batch(self, clusters: list[WorkCluster]) -> None:
        """Batch insert or replace work family clusters."""
        if not clusters:
            return
        rows = [
            (
                c.cluster_id,
                c.work_type,
                c.canonical_work_uri,
                c.wikidata_qid,
                c.canonical_title,
                c.canonical_author,
            )
            for c in clusters
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO corpus_work_clusters (
                    cluster_id, work_type, canonical_work_uri, wikidata_qid,
                    canonical_title, canonical_author
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def insert_expression_clusters_batch(self, clusters: list[ExpressionCluster]) -> None:
        """Batch insert or replace expression realization clusters."""
        if not clusters:
            return
        rows = [
            (
                c.cluster_id,
                c.work_cluster_id,
                c.canonical_expression_uri,
                c.content_type,
                c.language,
                c.version_designation,
                json.dumps(c.translators_json, ensure_ascii=False),
                json.dumps(c.performers_json, ensure_ascii=False),
            )
            for c in clusters
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO corpus_expression_clusters (
                    cluster_id, work_cluster_id, canonical_expression_uri,
                    content_type, language, version_designation,
                    translators_json, performers_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def insert_corpus_linkages_batch(self, linkages: list[CorpusLinkage]) -> None:
        """Batch insert or replace final record-to-cluster linkages."""
        if not linkages:
            return
        rows = [
            (
                lnk.record_id,
                lnk.manifestation_cluster_id,
                lnk.work_cluster_id,
                lnk.expression_cluster_id,
                lnk.final_work_uri,
                lnk.final_expression_uri,
                lnk.sibling_manifestation_ids_json,
            )
            for lnk in linkages
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO corpus_linkages (
                    record_id, manifestation_cluster_id, work_cluster_id,
                    expression_cluster_id, final_work_uri, final_expression_uri,
                    sibling_manifestation_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

    def insert_sammelband_bindings_batch(self, bindings: list[SammelbandBinding]) -> None:
        """Batch insert or replace sammelband item-level composite bindings."""
        if not bindings:
            return
        rows = [
            (
                b.binding_id,
                b.record_id,
                b.sequence_in_binding,
                b.binding_notes,
            )
            for b in bindings
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO sammelband_bindings (
                    binding_id, record_id, sequence_in_binding, binding_notes
                ) VALUES (?, ?, ?, ?);
                """,
                rows,
            )

    def get_harvest_state(self, source_id: str) -> Optional[HarvestState]:
        """Fetch harvest checkpoint from harvest_state."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM harvest_state WHERE source_id = ?;", (source_id,)
            ).fetchone()
            return _row_to_harvest_state(row) if row else None

    def save_harvest_state(self, state: HarvestState) -> None:
        """Upsert a harvest checkpoint in harvest_state."""
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO harvest_state (
                    source_id, source_type, metadata_prefix, set_spec,
                    last_harvest_from, last_harvest_until, resumption_token,
                    records_harvested, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_type = excluded.source_type,
                    metadata_prefix = excluded.metadata_prefix,
                    set_spec = excluded.set_spec,
                    last_harvest_from = excluded.last_harvest_from,
                    last_harvest_until = excluded.last_harvest_until,
                    resumption_token = excluded.resumption_token,
                    records_harvested = excluded.records_harvested,
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    state.source_id,
                    state.source_type,
                    state.metadata_prefix,
                    state.set_spec,
                    state.last_harvest_from,
                    state.last_harvest_until,
                    state.resumption_token,
                    state.records_harvested,
                    state.status,
                ),
            )

    def get_all_target_records(self) -> list[TargetRecord]:
        """Fetch all staged target records with their keys and payloads."""
        with self.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.*, p.raw_payload, p.serialized_work, p.serialized_expression, p.serialized_manifestation
                FROM target_records r
                LEFT JOIN target_payloads p ON r.record_id = p.record_id
                ORDER BY r.record_id;
                """
            ).fetchall()
            if not rows:
                return []

            key_rows = conn.execute(
                "SELECT record_id, key_type, key_value FROM target_keys ORDER BY record_id, id;"
            ).fetchall()
            keys_by_record: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
            for kr in key_rows:
                keys_by_record[kr["record_id"]][kr["key_type"]].append(kr["key_value"])

            records = []
            for r in rows:
                d = dict(r)
                d["keys"] = dict(keys_by_record.get(d["record_id"], {}))
                records.append(TargetRecord.model_validate(d))
            return records

    def find_records_by_key(self, key_type: str, key_value: str) -> list[str]:
        """Find record IDs matching a specific key type and value."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT record_id FROM target_keys WHERE key_type = ? AND key_value = ?;",
                (key_type, key_value),
            ).fetchall()
            return [r[0] for r in rows]

    def get_blocking_groups(self, key_types: Optional[list[str]] = None) -> dict[tuple[str, str], list[str]]:
        """Find all blocking key collision groups directly in SQL."""
        with self.get_connection() as conn:
            if key_types:
                placeholders = ",".join("?" for _ in key_types)
                rows = conn.execute(
                    f"""
                    SELECT key_type, key_value, GROUP_CONCAT(record_id)
                    FROM target_keys
                    WHERE key_type IN ({placeholders})
                    GROUP BY key_type, key_value
                    HAVING COUNT(*) > 1;
                    """,
                    key_types,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT key_type, key_value, GROUP_CONCAT(record_id)
                    FROM target_keys
                    GROUP BY key_type, key_value
                    HAVING COUNT(*) > 1;
                    """
                ).fetchall()
            groups = {}
            for r in rows:
                groups[(r[0], r[1])] = r[2].split(",")
            return groups

    def get_work_cluster(self, cluster_id: str) -> Optional[WorkCluster]:
        """Fetch a work cluster by ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM corpus_work_clusters WHERE cluster_id = ?;", (cluster_id,)
            ).fetchone()
            return WorkCluster.model_validate(dict(row)) if row else None

    def get_all_work_clusters(self) -> list[WorkCluster]:
        """Fetch all work clusters."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_work_clusters ORDER BY cluster_id;"
            ).fetchall()
            return [WorkCluster.model_validate(dict(r)) for r in rows]

    def get_expression_cluster(self, cluster_id: str) -> Optional[ExpressionCluster]:
        """Fetch an expression cluster by ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM corpus_expression_clusters WHERE cluster_id = ?;", (cluster_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["translators_json"] = json.loads(d.get("translators_json") or "[]")
            d["performers_json"] = json.loads(d.get("performers_json") or "[]")
            return ExpressionCluster.model_validate(d)

    def get_expression_clusters_for_work(self, work_cluster_id: str) -> list[ExpressionCluster]:
        """Fetch all expression clusters under a work cluster."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_expression_clusters WHERE work_cluster_id = ? ORDER BY cluster_id;",
                (work_cluster_id,),
            ).fetchall()
            clusters = []
            for r in rows:
                d = dict(r)
                d["translators_json"] = json.loads(d.get("translators_json") or "[]")
                d["performers_json"] = json.loads(d.get("performers_json") or "[]")
                clusters.append(ExpressionCluster.model_validate(d))
            return clusters

    def get_all_expression_clusters(self) -> list[ExpressionCluster]:
        """Fetch all expression clusters."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_expression_clusters ORDER BY cluster_id;"
            ).fetchall()
            clusters = []
            for r in rows:
                d = dict(r)
                d["translators_json"] = json.loads(d.get("translators_json") or "[]")
                d["performers_json"] = json.loads(d.get("performers_json") or "[]")
                clusters.append(ExpressionCluster.model_validate(d))
            return clusters

    def get_manifestation_cluster(self, cluster_id: str) -> Optional[ManifestationCluster]:
        """Fetch a manifestation cluster by ID."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM corpus_manifestation_clusters WHERE cluster_id = ?;", (cluster_id,)
            ).fetchone()
            return ManifestationCluster.model_validate(dict(row)) if row else None

    def get_all_manifestation_clusters(self) -> list[ManifestationCluster]:
        """Fetch all manifestation clusters."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM corpus_manifestation_clusters ORDER BY cluster_id;"
            ).fetchall()
            return [ManifestationCluster.model_validate(dict(r)) for r in rows]

    def get_corpus_linkage(self, record_id: str) -> Optional[CorpusLinkage]:
        """Fetch linkage record for a staged target record."""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM corpus_linkages WHERE record_id = ?;", (record_id,)
            ).fetchone()
            return CorpusLinkage.model_validate(dict(row)) if row else None

    def get_all_corpus_linkages(self) -> list[CorpusLinkage]:
        """Fetch all corpus linkage records."""
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM corpus_linkages ORDER BY record_id;").fetchall()
            return [CorpusLinkage.model_validate(dict(r)) for r in rows]

    def get_sammelband_bindings(self, binding_id: str) -> list[SammelbandBinding]:
        """Fetch all constituent items in a composite binding."""
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sammelband_bindings WHERE binding_id = ? ORDER BY sequence_in_binding;",
                (binding_id,),
            ).fetchall()
            return [SammelbandBinding.model_validate(dict(r)) for r in rows]
