"""AuthorityStore repository mediating access to SQLite authority.db and vector index."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from reconciliator.db import get_db_connection, get_metadata, init_authority_db, set_metadata
from reconciliator.indexer import VectorIndex, create_vector_engine
from reconciliator.models import BibliographicRecord, RecordIdentifiers

if TYPE_CHECKING:
    from reconciliator.config import VectorEngineConfig


class AuthorityStore:
    """Encapsulates persistent authority records in SQLite and dense vectors in the vector index."""

    def __init__(
        self,
        db_path: Path | str,
        index_path: Path | str,
        dimension: int = 768,
        vector_engine_config: Optional[VectorEngineConfig] = None,
        indexer: Optional[VectorIndex] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.index_path = Path(index_path)
        self.dimension = dimension

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
        all_agent_rows: list[tuple[int, str, str, Optional[str], Optional[str], str, str]] = []
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
                for oid in record.identifiers.other_ids:
                    all_id_rows.append((rec_id, "other", oid))
                id_type = "wikidata_qid" if record.source == "wikidata" else record.source
                all_id_rows.append((rec_id, id_type, record.source_id))

                # Prepare agents
                for a in record.agents:
                    all_agent_rows.append(
                        (
                            rec_id,
                            a.name,
                            a.name.lower(),
                            a.surname,
                            a.role,
                            json.dumps(a.uri_subfield_0, ensure_ascii=False),
                            json.dumps(a.uri_subfield_1, ensure_ascii=False),
                        )
                    )

                # Prepare FTS
                agent_names_str = " ".join(a.name for a in record.agents)
                all_subjs = (
                    record.subjects.topical
                    + record.subjects.genre_form
                    + record.subjects.geographic
                    + record.subjects.medium_of_performance
                )
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
                        record_id, name, name_normalized, surname, role, uri_subfield_0, uri_subfield_1
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
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
                if source == "melinda" and core_id.isdigit():
                    indexed_ids.add(f"oai:melinda.kansalliskirjasto.fi/bib/{core_id}")
                    if unpadded:
                        indexed_ids.add(f"oai:melinda.kansalliskirjasto.fi/bib/{unpadded}")

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
                    WHERE i.id_type NOT IN ('isbn', 'issn', 'urn') AND i.id_value = ?
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
        rows = [
            (
                a["qid"],
                a.get("name", ""),
                a.get("family_name"),
                a.get("asteri_id"),
                a.get("isni"),
                a.get("viaf"),
            )
            for a in authors
            if a.get("qid")
        ]
        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO wikidata_authors (qid, name, family_name, asteri_id, isni, viaf)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(qid) DO UPDATE SET
                    name = excluded.name,
                    family_name = excluded.family_name,
                    asteri_id = excluded.asteri_id,
                    isni = excluded.isni,
                    viaf = excluded.viaf;
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
                f"SELECT qid, name, family_name, asteri_id, isni, viaf FROM wikidata_authors WHERE qid IN ({placeholders});",
                qids,
            ).fetchall()
            for r in rows:
                results[r["qid"]] = {
                    "name": r["name"],
                    "family_name": r["family_name"],
                    "asteri_id": r["asteri_id"],
                    "isni": r["isni"],
                    "viaf": r["viaf"],
                }
        return results

    def get_wikidata_author(self, qid: str) -> dict[str, Any] | None:
        """Fetch a single cached author entity from wikidata_authors by QID."""
        result = self.get_wikidata_authors_batch([qid])
        return result.get(qid)

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
