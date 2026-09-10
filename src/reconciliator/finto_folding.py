"""Finto subject heading reconciliation and legacy thesauri folding engine."""

from __future__ import annotations

from typing import Optional

from reconciliator.finto_store import FintoStore
from reconciliator.normalizers.linguistic import fold_label

LEGACY_TO_MODERN_DEFAULTS = {
    "ysa": "yso",
    "allars": "yso",
    "kaunokki": "kauno",
    "bella": "kauno",
    "musa": "muso",
    "cilla": "muso",
}


def format_target_vocab(
    target_vocab: str, source_vocab: Optional[str] = None, lang: str = "fi"
) -> str:
    """Format vocabulary code with compliant language suffix (e.g. yso/fin, yso/swe)."""
    raw_source = (source_vocab or "").strip().lower()
    base_vocab = target_vocab.split("/")[0].strip()

    # Only append language suffixes to vocabularies that require language subcodes
    if base_vocab not in ("yso", "yso-paikat", "kauno", "muso", "slm"):
        return base_vocab

    # 1. Preserve explicit suffix from source if present (e.g. slm/swe -> slm/swe)
    if "/" in raw_source and not raw_source.startswith("http"):
        _, suffix = raw_source.split("/", 1)
        suffix = suffix.strip()
        if suffix:
            return f"{base_vocab}/{suffix}"

    # 2. Known legacy sources
    if raw_source in ("allars", "bella", "cilla"):
        return f"{base_vocab}/swe"
    if raw_source in ("ysa", "kaunokki", "musa"):
        return f"{base_vocab}/fin"

    # 3. For standard Finto vocabularies, attach language suffix
    lang_suffix = "swe" if lang in ("sv", "swe") else "fin"
    return f"{base_vocab}/{lang_suffix}"


class FintoFolder:
    """Reconciles legacy subject thesauri headings to canonical modern FINTO ontologies."""

    def __init__(self, finto_store: Optional[FintoStore] = None) -> None:
        self.finto_store = finto_store

    def fold_subject_heading(
        self,
        heading: str,
        source_vocab: Optional[str] = None,
        source_field: str = "650",
        source_uri: Optional[str] = None,
        lang: str = "fi",
    ) -> tuple[str, str, str, str, str]:
    ) -> tuple[str, str, str, str, str, Optional[str]]:
        """
        Reconcile a subject heading to canonical modern ontology.

        Returns:
            (target_field, target_vocab, target_uri, target_pref_label, mapping_type)
            (target_field, target_vocab, target_uri, target_pref_label, mapping_type, wikidata_qid)
        """
        clean_heading = heading.strip().rstrip(".,; ")
        raw_vocab = (source_vocab or "").strip().lower()
        # Strip language suffix like /fin, /swe, /eng (e.g. yso/fin -> yso, slm/fin -> slm)
        norm_vocab = raw_vocab.split("/")[0].strip()

        # If already canonical modern ontology URI/code and no finto_store
        if self.finto_store is None:
            # Fallback heuristic if finto_store is not populated
            target_vocab = LEGACY_TO_MODERN_DEFAULTS.get(norm_vocab, norm_vocab or "yso")
            target_field = source_field
            if source_field == "655":
                target_vocab = "slm"
            elif source_field == "382":
                target_vocab = "seko"
            return target_field, target_vocab, source_uri or "", clean_heading, "none"
            formatted_vocab = format_target_vocab(target_vocab, source_vocab, lang=lang)
            return target_field, formatted_vocab, source_uri or "", clean_heading, "none", None

        # 1. Check legacy URI in finto_mappings or canonical URI in finto_concepts
        if source_uri:
            mapping = self.finto_store.find_legacy_mapping_by_uri(source_uri)
            if mapping:
                concept = self.finto_store.find_concept_by_uri(mapping.target_uri)
                pref_label = clean_heading
                if concept:
                    pref_label = (
                        (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                        (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                        or concept.pref_label_fi
                        or concept.pref_label_en
                        or clean_heading
                    )
                target_field = mapping.target_field or source_field
                # Enforce: 655 is SLM
                if target_field == "655" and mapping.target_vocab == "kauno":
                    mapping.target_vocab = "slm"
                target_vocab = format_target_vocab(mapping.target_vocab, source_vocab, lang=lang)
                return (
                    target_field,
                    mapping.target_vocab,
                    target_vocab,
                    mapping.target_uri,
                    pref_label,
                    mapping.mapping_type,
                    None,
                )

            # Check if source_uri is already a modern canonical concept URI
            concept = self.finto_store.find_concept_by_uri(source_uri)
            if concept:
                pref_label = (
                    (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                    (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or concept.pref_label_en
                    or clean_heading
                )
                target_field = concept.target_field or source_field
                if concept.vocabulary == "slm" and target_field == "650":
                    target_field = "655"
                target_vocab = format_target_vocab(concept.vocabulary, source_vocab, lang=lang)
                return (
                    target_field,
                    concept.vocabulary,
                    target_vocab,
                    concept.uri,
                    pref_label,
                    "existing_canonical",
                    None,
                )

        # 2. Check legacy label in finto_mappings
        # 2. Check legacy label in finto_mappings if source_vocab is an explicit legacy thesaurus
        if norm_vocab in ("ysa", "allars", "kaunokki", "bella", "musa", "cilla"):
            mapping = self.finto_store.find_legacy_mapping_by_label(
                clean_heading, legacy_vocab=norm_vocab, lang=lang
            )
            if mapping:
                concept = self.finto_store.find_concept_by_uri(mapping.target_uri)
                pref_label = clean_heading
                if concept:
                    pref_label = (
                        (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                        or concept.pref_label_fi
                        or clean_heading
                    )
                target_field = mapping.target_field or source_field
                if target_field == "655" and mapping.target_vocab == "kauno":
                    mapping.target_vocab = "slm"
                target_vocab = format_target_vocab(mapping.target_vocab, source_vocab, lang=lang)
                return (
                    target_field,
                    target_vocab,
                    mapping.target_uri,
                    pref_label,
                    mapping.mapping_type,
                    None,
                )

        # 3. Dynamic alias lookup in finto_aliases (including multilingual, Wikidata P2347, and bridges)
        folded = fold_label(clean_heading)
        concept, qid = self.finto_store.find_concept_by_alias(folded, target_field=source_field)
        if concept:
            pref_label = (
                (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                or concept.pref_label_fi
                or concept.pref_label_sv
                or clean_heading
            )
            target_field = concept.target_field or source_field
            if concept.vocabulary == "slm" and target_field == "650":
                target_field = "655"
            elif concept.vocabulary == "seko" and target_field == "650":
                target_field = "382"

            target_vocab = format_target_vocab(concept.vocabulary, source_vocab, lang=lang)
            return (
                target_field,
                target_vocab,
                concept.uri,
                pref_label,
                "alias_match",
                qid,
            )

        # 4. Fallback legacy mapping lookup without legacy_vocab filter
        mapping = self.finto_store.find_legacy_mapping_by_label(
            clean_heading, legacy_vocab=norm_vocab or None, lang=lang
        )
        if mapping:
            concept = self.finto_store.find_concept_by_uri(mapping.target_uri)
            pref_label = clean_heading
            if concept:
                pref_label = (
                    (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                    (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or clean_heading
                )
            target_field = mapping.target_field or source_field
            if target_field == "655" and mapping.target_vocab == "kauno":
                mapping.target_vocab = "slm"
            target_vocab = format_target_vocab(mapping.target_vocab, source_vocab, lang=lang)
            return (
                target_field,
                mapping.target_vocab,
                target_vocab,
                mapping.target_uri,
                pref_label,
                mapping.mapping_type,
                None,
            )

        # 3. Direct lookup in finto_concepts
        # 4. Direct lookup in finto_concepts
        candidate_vocabs: list[str] = []
        if source_field == "655":
            candidate_vocabs = ["slm"]
        elif source_field == "382":
            candidate_vocabs = ["seko"]
        elif source_field == "651":
            candidate_vocabs = ["yso"]
            candidate_vocabs = ["yso-paikat", "yso"]
        else:
            # 650
            if norm_vocab in ("kaunokki", "bella"):
                candidate_vocabs = ["kauno", "yso"]
            elif norm_vocab in ("musa", "cilla"):
                candidate_vocabs = ["muso", "yso"]
            elif norm_vocab in ("ysa", "allars", "yso"):
                candidate_vocabs = ["yso"]
            else:
                candidate_vocabs = ["yso", "kauno", "muso"]

        for v in candidate_vocabs:
            concept = self.finto_store.find_concept_by_label(clean_heading, vocabulary=v, lang=lang)
            if concept:
                pref_label = (
                    (concept.pref_label_sv if lang == "sv" else concept.pref_label_fi)
                    (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or clean_heading
                )
                target_field = source_field
                # If term has a designated target_field (e.g. SLM genre term discovered in 650)
                if concept.target_field:
                    target_field = concept.target_field
                elif concept.vocabulary == "slm":
                    target_field = "655"
                elif concept.vocabulary == "seko":
                    target_field = "382"

                target_vocab = format_target_vocab(concept.vocabulary, source_vocab, lang=lang)
                return (
                    target_field,
                    concept.vocabulary,
                    target_vocab,
                    concept.uri,
                    pref_label,
                    "pref_label_lookup",
                    None,
                )

        # 4. Fallback if not found: keep as is
        # 5. Fallback if not found: keep as is
        default_target_vocab = LEGACY_TO_MODERN_DEFAULTS.get(norm_vocab, norm_vocab)
        if source_field == "655":
            default_target_vocab = "slm"
        elif source_field == "382":
            default_target_vocab = "seko"

        return source_field, default_target_vocab, source_uri or "", clean_heading, "none"
        formatted_default = format_target_vocab(default_target_vocab, source_vocab, lang=lang)
        return source_field, formatted_default, source_uri or "", clean_heading, "none", None
