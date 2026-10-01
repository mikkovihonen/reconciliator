"""In-memory accumulator for batching decomposed WEMI entities before database ingestion."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.loaders.base import DecomposedRecord, LoadContext
from reconciliator.loaders.registry import load_record


class WEMIBatchAccumulator:
    """Stages decomposed WEMI entities until a batch is ready to hand to the writer."""

    def __init__(
        self,
        source_name: str,
        id_source: str = "auto",
        include_items: bool = False,
        vocab_store: Optional[Any] = None,
        strip_isbd_punctuation: bool = True,
        isbd_punctuation: str = " ./,;:",
        constituents: Optional[Any] = None,
        filter_config: Optional[Any] = None,
    ) -> None:
        self.source_name = source_name
        self.id_source = id_source
        self.include_items = include_items
        self.strip_isbd_punctuation = strip_isbd_punctuation
        self.isbd_punctuation = isbd_punctuation
        self.vocab_store = vocab_store
        self.constituents = constituents
        self.filter_config = filter_config
        self.staged_records = 0

        self.works: list[Any] = []
        self.expressions: list[Any] = []
        self.manifestations: list[Any] = []
        self.items: list[Any] = []
        self.agents: list[Any] = []
        self.contributions: list[Any] = []
        self.relationships: list[Any] = []
        self.agent_relationships: list[Any] = []
        self.identifiers: list[Any] = []
        self.associations: list[Any] = []
        self.entity_keys: list[Any] = []
        self.entity_subjects: list[Any] = []

    @property
    def pending(self) -> int:
        return self.staged_records

    def add_record(self, record: DecomposedRecord) -> bool:
        if self.filter_config is not None and record.works:
            expr_lang = record.expressions[0].language if record.expressions else None
            pub_year = record.manifestations[0].publication_year if record.manifestations else None
            accepted = any(
                self.filter_config.accepts(
                    work_type=w.work_type,
                    language=w.original_language or expr_lang,
                    year=pub_year,
                )
                for w in record.works
            )
            if not accepted:
                return False

        self.staged_records += 1
        self.works.extend(record.works)
        self.expressions.extend(record.expressions)
        self.manifestations.extend(record.manifestations)
        self.items.extend(record.items)
        self.agents.extend(record.agents)
        self.contributions.extend(record.contributions)
        self.relationships.extend(record.relationships)
        self.agent_relationships.extend(record.agent_relationships)
        self.identifiers.extend(record.identifiers)
        self.associations.extend(record.associations)
        self.entity_keys.extend(record.entity_keys)
        self.entity_subjects.extend(record.entity_subjects)
        return True

    def add(self, item: Any) -> bool:
        ctx = LoadContext(
            source=self.source_name,
            record_origin=self.source_name,
            id_source=self.id_source,
            include_items=self.include_items,
            vocab_store=self.vocab_store,
            strip_isbd_punctuation=self.strip_isbd_punctuation,
            isbd_punctuation=self.isbd_punctuation,
            constituents=self.constituents,
        )
        record = load_record(item, ctx)
        return self.add_record(record)

    def merge_payload(self, payload: dict[str, Any]) -> None:
        """Merge an existing batch payload dictionary into this accumulator."""
        count = int(payload.get("record_count") or 0)
        self.staged_records += count
        for key in (
            "works",
            "expressions",
            "manifestations",
            "items",
            "agents",
            "contributions",
            "relationships",
            "agent_relationships",
            "identifiers",
            "associations",
            "entity_keys",
            "entity_subjects",
        ):
            items = payload.get(key)
            if items:
                getattr(self, key).extend(items)

    def take(self) -> dict[str, Any]:
        record_count = self.staged_records
        self.staged_records = 0

        payload = {
            "works": self.works,
            "expressions": self.expressions,
            "manifestations": self.manifestations,
            "items": self.items,
            "agents": self.agents,
            "relationships": self.relationships,
            "agent_relationships": self.agent_relationships,
            "identifiers": self.identifiers,
            "associations": self.associations,
            "contributions": self.contributions,
            "entity_keys": self.entity_keys,
            "entity_subjects": self.entity_subjects,
            "record_count": record_count,
            "record_origin": self.source_name,
        }
        self.works = []
        self.expressions = []
        self.manifestations = []
        self.items = []
        self.agents = []
        self.contributions = []
        self.relationships = []
        self.agent_relationships = []
        self.identifiers = []
        self.associations = []
        self.entity_keys = []
        self.entity_subjects = []
        return payload
