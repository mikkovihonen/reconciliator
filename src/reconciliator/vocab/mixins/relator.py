"""Relator terms and role codes storage and resolution mixin for VocabStore."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Optional, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from reconciliator.database import calculate_batch_size, relator_terms_table
from reconciliator.normalizers.linguistic import fold_label

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from reconciliator.config import VocabulariesConfig

logger = logging.getLogger(__name__)


class RelatorStoreProtocol(Protocol):
    """Protocol for relator store dependencies."""

    engine: Engine
    config: VocabulariesConfig

    def clear_caches(self) -> None: ...


class RelatorStoreMixin:
    """Manages MARC relator terms, role codes, and canonical role label lookups."""

    def insert_relators(
        self: RelatorStoreProtocol,
        terms: Sequence[dict[str, Any] | tuple[Any, ...] | str],
    ) -> None:
        """Bulk insert or replace relator terms in relator_terms."""
        if not terms:
            return
        rows: list[dict[str, Any]] = []
        default_lang = getattr(self.config, "default_language", "und")
        for t in terms:
            if isinstance(t, dict):
                raw = str(t.get("raw_term", ""))
                folded = str(t.get("folded_term") or fold_label(raw))
                lang = str(t.get("lang", default_lang))
                code = str(t.get("code") or "")
                source_uri = t.get("source_uri")
                bridged_uri = t.get("bridged_uri") or t.get("uri")
                is_preferred = bool(t.get("is_preferred", True))
            elif isinstance(t, (tuple, list)):
                raw = str(t[0])
                folded = fold_label(raw)
                lang = str(t[1]) if len(t) > 1 else default_lang
                code = str(t[2]) if len(t) > 2 and t[2] else ""
                source_uri = t[3] if len(t) > 3 else None
                bridged_uri = t[4] if len(t) > 4 else None
                is_preferred = bool(t[5]) if len(t) > 5 else True
            else:
                raw = str(t)
                folded = fold_label(raw)
                lang = default_lang
                code = ""
                source_uri = None
                bridged_uri = None
                is_preferred = True
            if not folded or not code:
                continue
            rows.append(
                {
                    "folded_term": folded,
                    "raw_term": raw,
                    "lang": lang,
                    "code": code,
                    "source_uri": source_uri,
                    "bridged_uri": bridged_uri,
                    "is_preferred": is_preferred,
                }
            )

        seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["folded_term"], r["code"], r["lang"])
            if key not in seen or r["is_preferred"]:
                seen[key] = r
        unique_rows = list(seen.values())
        if not unique_rows:
            return

        relator_batch_size = calculate_batch_size(num_columns=len(relator_terms_table.c))
        with self.engine.begin() as conn:
            for i in range(0, len(unique_rows), relator_batch_size):
                chunk = unique_rows[i : i + relator_batch_size]
                stmt = pg_insert(relator_terms_table).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_relator_folded_code_lang",
                    set_={
                        "raw_term": stmt.excluded.raw_term,
                        "source_uri": stmt.excluded.source_uri,
                        "bridged_uri": stmt.excluded.bridged_uri,
                        "is_preferred": stmt.excluded.is_preferred,
                    },
                )
                conn.execute(stmt)

        self.clear_caches()

    @functools.lru_cache(maxsize=8192)
    def is_known_relator(self: RelatorStoreProtocol, folded_term: str) -> bool:
        """Check whether a folded role term exists in relator_terms."""
        with self.engine.connect() as conn:
            stmt = (
                select(1)
                .where(relator_terms_table.c.folded_term == folded_term.strip().casefold())
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return bool(row)

    @functools.lru_cache(maxsize=8192)
    def get_relator_code(self: RelatorStoreProtocol, folded_term: str) -> Optional[str]:
        """Look up standard MARC code for a folded role term in relator_terms."""
        with self.engine.connect() as conn:
            stmt = (
                select(relator_terms_table.c.code)
                .where(
                    relator_terms_table.c.folded_term == folded_term.strip().casefold(),
                    relator_terms_table.c.code.isnot(None),
                )
                .order_by(relator_terms_table.c.is_preferred.desc())
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return str(row[0]) if row and row[0] else None

    @functools.lru_cache(maxsize=8192)
    def get_canonical_relator_term(
        self: RelatorStoreProtocol, code: str, lang: str = "fi"
    ) -> Optional[str]:
        """Look up canonical preferred role term for a code in target language, falling back to fi then en."""
        if not code:
            return None
        clean_code = code.strip().lower()
        clean_lang = lang.strip().lower() if lang else "fi"
        with self.engine.connect() as conn:
            # 1. Try exact requested lang
            stmt = (
                select(relator_terms_table.c.raw_term)
                .where(
                    relator_terms_table.c.code == clean_code,
                    relator_terms_table.c.lang == clean_lang,
                    relator_terms_table.c.is_preferred.is_(True),
                )
                .limit(1)
            )
            row = conn.execute(stmt).first()
            if row and row[0]:
                return str(row[0])

            # 2. Try Finnish fallback if not requested lang
            if clean_lang != "fi":
                stmt_fi = (
                    select(relator_terms_table.c.raw_term)
                    .where(
                        relator_terms_table.c.code == clean_code,
                        relator_terms_table.c.lang == "fi",
                        relator_terms_table.c.is_preferred.is_(True),
                    )
                    .limit(1)
                )
                row_fi = conn.execute(stmt_fi).first()
                if row_fi and row_fi[0]:
                    return str(row_fi[0])

            # 3. Try English fallback
            if clean_lang != "en":
                stmt_en = (
                    select(relator_terms_table.c.raw_term)
                    .where(
                        relator_terms_table.c.code == clean_code,
                        relator_terms_table.c.lang == "en",
                        relator_terms_table.c.is_preferred.is_(True),
                    )
                    .limit(1)
                )
                row_en = conn.execute(stmt_en).first()
                if row_en and row_en[0]:
                    return str(row_en[0])

            # 4. Any preferred term for this code
            stmt_any = (
                select(relator_terms_table.c.raw_term)
                .where(
                    relator_terms_table.c.code == clean_code,
                    relator_terms_table.c.is_preferred.is_(True),
                )
                .limit(1)
            )
            row_any = conn.execute(stmt_any).first()
            return str(row_any[0]) if row_any and row_any[0] else None

    @functools.lru_cache(maxsize=8192)
    def get_relator_source_uri(self: RelatorStoreProtocol, code: str) -> Optional[str]:
        """Look up source URI (e.g. LoC URI) for a MARC relator code."""
        if not code:
            return None
        clean_code = code.strip().lower()
        with self.engine.connect() as conn:
            stmt = (
                select(relator_terms_table.c.source_uri)
                .where(
                    relator_terms_table.c.code == clean_code,
                    relator_terms_table.c.source_uri.isnot(None),
                )
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return str(row[0]) if row and row[0] else None

    @functools.lru_cache(maxsize=8192)
    def get_relator_bridged_uri(self: RelatorStoreProtocol, code: str) -> Optional[str]:
        """Look up bridged URI (e.g. target ontology URI) for a MARC relator code."""
        if not code:
            return None
        clean_code = code.strip().lower()
        with self.engine.connect() as conn:
            stmt = (
                select(relator_terms_table.c.bridged_uri)
                .where(
                    relator_terms_table.c.code == clean_code,
                    relator_terms_table.c.bridged_uri.isnot(None),
                )
                .limit(1)
            )
            row = conn.execute(stmt).first()
            return str(row[0]) if row and row[0] else None
