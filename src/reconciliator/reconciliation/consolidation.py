"""Cross-source work identity consolidation.

Every contributing source mints its own `urn:work:{source}:{source_id}`, so one
intellectual creation held by three catalogues arrives as three work rows. This
module finds those rows, records `same_work_as` edges between them, and elects one
canonical work per equivalence group.

Nothing is deleted or rewritten: consolidation is expressed as relationships, so
it is inspectable, re-runnable, and reversible. Matching resolves a matched work
through the equivalence map, which is what makes every record of a work carry the
same URI regardless of which source it happened to match.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from rapidfuzz import fuzz
from sqlalchemy import and_, func, select

from reconciliator.config import WorkConsolidationConfig
from reconciliator.utils.curie import to_curie
from reconciliator.database import (
    WEMIRepository,
    agents_table,
    contributions_table,
    identifiers_table,
    manifestations_table,
    work_relationships_table,
    works_table,
)
from reconciliator.models import WorkRelationship
from reconciliator.utils import UnionFind, clean_alphanumeric

logger = logging.getLogger(__name__)

SAME_WORK = "same_work_as"

# Synthetic URIs minted per record carry no cross-source identity.
_LOCAL_URI_PREFIXES = ("urn:work:", "urn:reconciliator:", "urn:agent:", "urn:exp:", "urn:manif:")


@dataclass
class WorkSignature:
    """Everything consolidation needs to decide whether two work rows are one work."""

    work_id: int
    work_curie: str
    source: str
    source_id: str
    title_normalized: str
    work_type: str
    author_keys: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)

    @property
    def work_uri(self) -> str:
        return self.work_curie

    @property
    def wikidata_qid(self) -> Optional[str]:
        for ident in self.identifiers:
            if ident.startswith("wikidata:"):
                return ident.split(":", 1)[1].upper()
        return None


@dataclass
class ConsolidationResult:
    """Outcome of a consolidation run."""

    groups: list[list[int]] = field(default_factory=list)
    canonical_by_work: dict[int, int] = field(default_factory=dict)
    edges_written: int = 0
    evidence: dict[tuple[int, int], str] = field(default_factory=dict)

    @property
    def consolidated_works(self) -> int:
        """Work rows that were folded into some other row's identity."""
        return sum(len(g) - 1 for g in self.groups)


class WorkConsolidator:
    """Finds and records equivalent work rows across contributing sources."""

    def __init__(
        self,
        repo: WEMIRepository,
        config: Optional[WorkConsolidationConfig] = None,
    ) -> None:
        self.repo = repo
        self.config = config or WorkConsolidationConfig()

    # ------------------------------------------------------------------
    # Signature collection
    # ------------------------------------------------------------------

    def load_signatures(self) -> dict[int, WorkSignature]:
        """Build a comparison signature for every authority work."""
        signatures: dict[int, WorkSignature] = {}
        # Only authoritative sources take part: consolidating a corpus row into an
        # authority work would give the corpus record the authority's identity.
        auth_sources = self.repo.authoritative_sources()
        stmt_works = select(
            works_table.c.id,
            works_table.c.source,
            works_table.c.source_id,
            works_table.c.title_normalized,
            works_table.c.work_type,
        )
        if auth_sources:
            stmt_works = stmt_works.where(works_table.c.record_origin.in_(auth_sources))

        with self.repo.get_connection() as conn:
            for row in conn.execute(stmt_works).mappings():
                work_curie = to_curie(row["source"] or "", row["source_id"] or "")
                sig = WorkSignature(
                    work_id=row["id"],
                    work_curie=work_curie,
                    source=row["source"] or "",
                    source_id=row["source_id"] or "",
                    title_normalized=(row["title_normalized"] or "").strip().lower(),
                    work_type=row["work_type"] or "text",
                )
                if sig.source.lower() == "wikidata" and sig.source_id:
                    sig.identifiers.add(f"wikidata:{sig.source_id.upper()}")
                signatures[row["id"]] = sig
            if not signatures:
                return signatures

            # Creators, for metadata comparison. Roles are not filtered: any agent
            # shared between two same-titled works is evidence of the same work.
            stmt_creators = (
                select(
                    contributions_table.c.entity_id.label("work_id"),
                    agents_table.c.name_normalized,
                    agents_table.c.surname,
                )
                .join(agents_table, contributions_table.c.agent_id == agents_table.c.id)
                .where(contributions_table.c.entity_type == "work")
            )
            for row in conn.execute(stmt_creators).mappings():
                sig = signatures.get(row["work_id"])
                if sig is None:
                    continue
                for value in (row["name_normalized"], row["surname"]):
                    if value:
                        sig.author_keys.add(re.sub(r"[^\w]", "", str(value).lower()))

            schemes = {s.lower() for s in self.config.strong_identifier_schemes}
            sources = {("wikidata" if s == "wikidata_qid" else s) for s in schemes}
            if self.config.use_wikidata_qid:
                sources.add("wikidata")
            if sources:
                sources_list = [s.lower() for s in sources]
                # Identifiers attached to the work itself.
                stmt_work_idents = select(
                    identifiers_table.c.entity_id.label("work_id"),
                    identifiers_table.c.source,
                    identifiers_table.c.source_id,
                ).where(
                    identifiers_table.c.entity_type == "work",
                    func.lower(identifiers_table.c.source).in_(sources_list),
                )
                for row in conn.execute(stmt_work_idents).mappings():
                    sig = signatures.get(row["work_id"])
                    if sig is not None:
                        self._add_identifier(sig, row["source"], row["source_id"])

                # Identifiers on the work's manifestations. An ISBN identifies a
                # manifestation, but a shared ISBN between two work rows means both
                # describe the work that manifestation embodies.
                stmt_manif_idents = (
                    select(
                        manifestations_table.c.work_id.label("work_id"),
                        identifiers_table.c.source,
                        identifiers_table.c.source_id,
                    )
                    .join(
                        manifestations_table,
                        and_(
                            identifiers_table.c.entity_type == "manifestation",
                            identifiers_table.c.entity_id == manifestations_table.c.id,
                        ),
                    )
                    .where(
                        manifestations_table.c.work_id.is_not(None),
                        func.lower(identifiers_table.c.source).in_(sources_list),
                    )
                )
                for row in conn.execute(stmt_manif_idents).mappings():
                    sig = signatures.get(row["work_id"])
                    if sig is not None:
                        self._add_identifier(sig, row["source"], row["source_id"])

        return signatures

    @staticmethod
    def _add_identifier(sig: WorkSignature, scheme: Any, value: Any) -> None:
        norm = clean_alphanumeric(str(value or ""))
        if not norm:
            return
        scheme_l = str(scheme or "").strip().lower()
        if scheme_l == "wikidata_qid":
            scheme_l = "wikidata"
        # A bare source-minted URI is not shared identity; skip it.
        if scheme_l in ("urn", "uri") and str(value).startswith(_LOCAL_URI_PREFIXES):
            return
        sig.identifiers.add(f"{scheme_l}:{norm}")

    # ------------------------------------------------------------------
    # Equivalence
    # ------------------------------------------------------------------

    def metadata_score(self, a: WorkSignature, b: WorkSignature) -> float:
        """Similarity of two works on title, creator and work type alone.

        Used where no identifier or QID is shared, which is the normal case for
        corpora catalogued without identifiers.
        """
        if not a.title_normalized or not b.title_normalized:
            return 0.0
        if a.work_type != b.work_type:
            return 0.0

        title_sim = fuzz.token_set_ratio(a.title_normalized, b.title_normalized) / 100.0

        if a.author_keys and b.author_keys:
            if a.author_keys & b.author_keys:
                author_sim = 1.0
            else:
                author_sim = (
                    max(fuzz.ratio(x, y) for x in a.author_keys for y in b.author_keys) / 100.0
                )
        elif not a.author_keys and not b.author_keys:
            # Anonymous works: the title has to carry the decision alone.
            author_sim = 0.75
        else:
            # One side names a creator and the other does not; treat as weak.
            author_sim = 0.50

        return round(0.6 * title_sim + 0.4 * author_sim, 4)

    def compare(self, a: WorkSignature, b: WorkSignature) -> Optional[str]:
        """Return the evidence tier joining two works, or None if they differ."""
        cfg = self.config
        if not cfg.include_same_source and a.source == b.source:
            return None

        if cfg.use_wikidata_qid and a.wikidata_qid and b.wikidata_qid:
            if a.wikidata_qid == b.wikidata_qid:
                return "wikidata_qid"
            # Distinct QIDs are an explicit statement that these differ.
            return None

        if cfg.use_shared_identifiers and (a.identifiers & b.identifiers):
            shared = sorted(a.identifiers & b.identifiers)[0]
            return f"identifier:{shared.split(':', 1)[0]}"

        if cfg.use_metadata:
            score = self.metadata_score(a, b)
            if score >= cfg.metadata_threshold:
                return f"metadata:{score}"

        return None

    def find_equivalences(
        self,
        signatures: Optional[dict[int, WorkSignature]] = None,
    ) -> list[tuple[int, int, str]]:
        """Find equivalent work pairs, blocking so the comparison stays tractable."""
        sigs = signatures if signatures is not None else self.load_signatures()
        if len(sigs) < 2:
            return []

        # Block on anything that could make two works equal, so the O(n^2) sweep
        # is confined to plausible pairs rather than the whole store.
        blocks: dict[str, list[int]] = {}

        def _block(key: str, work_id: int) -> None:
            blocks.setdefault(key, []).append(work_id)

        for sig in sigs.values():
            if sig.wikidata_qid:
                _block(f"qid:{sig.wikidata_qid}", sig.work_id)
            for ident in sig.identifiers:
                _block(f"id:{ident}", sig.work_id)
            if sig.title_normalized:
                _block(f"title:{sig.title_normalized}", sig.work_id)

        seen: set[tuple[int, int]] = set()
        pairs: list[tuple[int, int, str]] = []
        for members in blocks.values():
            if len(members) < 2:
                continue
            unique = sorted(set(members))
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    key = (unique[i], unique[j])
                    if key in seen:
                        continue
                    seen.add(key)
                    evidence = self.compare(sigs[unique[i]], sigs[unique[j]])
                    if evidence:
                        pairs.append((unique[i], unique[j], evidence))
        return pairs

    # ------------------------------------------------------------------
    # Canonical election and persistence
    # ------------------------------------------------------------------

    def elect_canonical(self, members: list[int], sigs: dict[int, WorkSignature]) -> int:
        """Pick the representative work for an equivalence group, deterministically."""
        priority = [s.lower() for s in self.config.canonical_source_priority]

        def rank(work_id: int) -> tuple[int, int, str]:
            sig = sigs[work_id]
            has_qid = 0 if sig.wikidata_qid else 1
            try:
                source_rank = priority.index(sig.source.lower())
            except ValueError:
                source_rank = len(priority)
            return (has_qid, source_rank, sig.work_curie)

        return min(members, key=rank)

    def consolidate(self, dry_run: bool = False) -> ConsolidationResult:
        """Find equivalent works, persist same_work_as edges, elect canonicals."""
        result = ConsolidationResult()
        if not self.config.enabled:
            return result

        sigs = self.load_signatures()
        pairs = self.find_equivalences(sigs)
        if not pairs:
            return result

        uf = UnionFind(sigs.keys())
        for a, b, evidence in pairs:
            uf.union(a, b)
            result.evidence[(a, b)] = evidence

        for members in uf.groups().values():
            if len(members) < 2:
                continue
            members = sorted(members)
            canonical = self.elect_canonical(members, sigs)
            result.groups.append(members)
            for work_id in members:
                result.canonical_by_work[work_id] = canonical

        if dry_run or not result.groups:
            return result

        # The edges record why a group was formed; canonical_work_id records the
        # decision, so every caller resolves identically without re-electing.
        self.repo.set_canonical_works(result.canonical_by_work)

        existing = self._existing_edges()
        new_edges: list[WorkRelationship] = []
        for a, b, _evidence in pairs:
            for src, tgt in ((a, b), (b, a)):  # symmetric
                if (src, tgt) not in existing:
                    new_edges.append(
                        WorkRelationship(
                            source_work_id=src,
                            target_work_id=tgt,
                            relationship_type=SAME_WORK,  # type: ignore[arg-type]
                        )
                    )
                    existing.add((src, tgt))
        if new_edges:
            self.repo.insert_work_relationships_batch(new_edges)
            result.edges_written = len(new_edges)

        logger.info(
            "Consolidated %d work rows into %d canonical works (%d edges written).",
            sum(len(g) for g in result.groups),
            len(result.groups),
            result.edges_written,
        )
        return result

    def _existing_edges(self) -> set[tuple[int, int]]:
        stmt = select(
            work_relationships_table.c.source_work_id,
            work_relationships_table.c.target_work_id,
        ).where(work_relationships_table.c.relationship_type == SAME_WORK)
        with self.repo.get_connection() as conn:
            return {
                (r["source_work_id"], r["target_work_id"]) for r in conn.execute(stmt).mappings()
            }

