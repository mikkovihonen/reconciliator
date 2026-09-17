"""Subject heading reconciliation and legacy thesauri folding engine."""

from __future__ import annotations

from typing import Optional

from reconciliator.config import VocabNormalizationConfig
from reconciliator.models import VocabConcept
from reconciliator.normalizers.linguistic import build_biographical_date_pattern, fold_label
from reconciliator.vocab_store import VocabStore
from reconciliator.vocabularies import strip_vocabulary_subcode


def format_target_vocab(
    target_vocab: str,
    source_vocab: Optional[str] = None,
    lang: Optional[str] = None,
    config: Optional[VocabNormalizationConfig] = None,
) -> str:
    """Format vocabulary code with compliant language suffix (e.g. yso/fin, yso/swe).

    Returns the bare vocabulary code when ``apply_language_subcode`` is disabled,
    or when the vocabulary is not one that carries a subcode.
    """
    cfg = config or VocabNormalizationConfig()
    raw_source = (source_vocab or "").strip().lower()
    base_vocab = strip_vocabulary_subcode(target_vocab)

    if not cfg.apply_language_subcode:
        return base_vocab

    # Subcode vocabularies from configuration
    subcode_vocabs = set(cfg.language_subcode_vocabularies.keys())
    if base_vocab not in subcode_vocabs:
        return base_vocab

    # 1. Preserve explicit suffix from source if present (e.g. slm/swe -> slm/swe)
    if "/" in raw_source and not raw_source.startswith("http"):
        _, suffix = raw_source.split("/", 1)
        suffix = suffix.strip()
        if suffix:
            return f"{base_vocab}/{suffix}"

    default_subcode = cfg.language_subcode_vocabularies.get(base_vocab, "fin")

    # 2. Known legacy sources from configuration
    thes_langs = cfg.thesaurus_languages
    if raw_source in thes_langs:
        mapped_l = thes_langs[raw_source].strip().lower()
        subcode = cfg.language_subcode_suffixes.get(mapped_l, default_subcode)
        return f"{base_vocab}/{subcode}"

    # 3. For standard vocabularies, attach language suffix based on record language
    effective_lang = (lang or cfg.default_language).strip().lower()
    lang_suffix = cfg.language_subcode_suffixes.get(effective_lang, default_subcode)
    return f"{base_vocab}/{lang_suffix}"


class VocabFolder:
    """Reconciles legacy subject thesauri headings to canonical modern ontologies."""

    def __init__(
        self,
        vocab_store: Optional[VocabStore] = None,
        config: Optional[VocabNormalizationConfig] = None,
    ) -> None:
        self.vocab_store = vocab_store
        self.config = config or VocabNormalizationConfig()
        self.date_pattern = build_biographical_date_pattern(
            prefixes=self.config.biographical_date_prefixes,
            era_bc=self.config.biographical_era_bc,
            era_ad=self.config.biographical_era_ad,
            century_suffixes=self.config.biographical_century_suffixes,
            custom_pattern=self.config.biographical_date_pattern,
        )

    def _resolve_target_field(
        self,
        vocab: str,
        current_field: str,
        concept_target_field: Optional[str] = None,
    ) -> str:
        """Determine target MARC field based on concept metadata or configured vocab_target_fields."""
        if concept_target_field:
            return concept_target_field
        return self.config.vocab_target_fields.get(vocab, current_field)

    def _enforce_valid_field_vocab(self, target_field: str, target_vocab: str) -> str:
        """Ensure target_vocab is allowed in target_field, falling back to field default if prohibited."""
        if target_field == self.config.default_vocab_target_field:
            return target_vocab
        allowed = self.config.field_to_vocabularies.get(target_field)
        if allowed and target_vocab not in allowed:
            return self.config.default_field_vocabularies.get(target_field, target_vocab)
        return target_vocab

    def _get_default_target_vocab(self, source_field: str, norm_vocab: str) -> str:
        """Get default target vocabulary from legacy mapping, field defaults, or source vocab."""
        if norm_vocab and norm_vocab in self.config.legacy_to_modern:
            return self.config.legacy_to_modern[norm_vocab]
        if source_field in self.config.default_field_vocabularies:
            return self.config.default_field_vocabularies[source_field]
        default_topical = self.config.default_vocab_target_field
        if default_topical in self.config.default_field_vocabularies:
            return norm_vocab or self.config.default_field_vocabularies[default_topical]
        if self.config.default_field_vocabularies:
            return norm_vocab or next(iter(self.config.default_field_vocabularies.values()))
        return norm_vocab

    def _select_pref_label(
        self,
        concept: VocabConcept,
        lang: Optional[str],
        fallback_heading: str,
    ) -> str:
        """Select preferred label from concept based on requested language and configured priority."""
        req_lang = (lang or self.config.default_language).strip().lower()
        norm_req = self.config.language_code_map.get(req_lang, req_lang)

        priority: list[str] = [norm_req]
        for pref in self.config.preferred_label_languages:
            norm_pref = self.config.language_code_map.get(
                pref.strip().lower(), pref.strip().lower()
            )
            if norm_pref not in priority:
                priority.append(norm_pref)

        for lang_code in priority:
            label = concept.pref_labels.get(lang_code)
            if not label:
                label = getattr(concept, f"pref_label_{lang_code}", None)
            if label and isinstance(label, str):
                return label

        for label in concept.pref_labels.values():
            if label and isinstance(label, str):
                return label

        return fallback_heading

    def fold_subject_heading(
        self,
        heading: str,
        source_vocab: Optional[str] = None,
        source_field: Optional[str] = None,
        source_uri: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> tuple[str, str, str, str, str, Optional[str]]:
        """
        Reconcile a subject heading to canonical modern ontology.

        Returns:
            (target_field, target_vocab, target_uri, target_pref_label, mapping_type, wikidata_qid)
        """
        source_field = source_field or self.config.default_vocab_target_field
        clean_heading = heading.strip().rstrip(".,; ")
        effective_lang = (lang or self.config.default_language).strip().lower()
        raw_vocab = (source_vocab or "").strip().lower()
        # Strip language suffix like /fin, /swe, /eng (e.g. yso/fin -> yso, slm/fin -> slm)
        norm_vocab = strip_vocabulary_subcode(raw_vocab)

        # If already canonical modern ontology URI/code and no vocab_store
        if self.vocab_store is None:
            # Fallback heuristic if vocab_store is not populated
            target_vocab = self._get_default_target_vocab(source_field, norm_vocab)
            target_field = self._resolve_target_field(target_vocab, source_field)
            target_vocab = self._enforce_valid_field_vocab(target_field, target_vocab)

            formatted_vocab = format_target_vocab(
                target_vocab, source_vocab, lang=lang, config=self.config
            )
            return target_field, formatted_vocab, source_uri or "", clean_heading, "none", None

        # 1. Check legacy URI in concept_mappings or canonical URI in concepts
        if source_uri:
            mapping = self.vocab_store.find_legacy_mapping_by_uri(source_uri)
            if mapping:
                concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
                pref_label = (
                    self._select_pref_label(concept, lang, clean_heading)
                    if concept
                    else clean_heading
                )
                target_field = self._resolve_target_field(
                    mapping.target_vocab, source_field, mapping.target_field
                )
                tgt_v = self._enforce_valid_field_vocab(target_field, mapping.target_vocab)
                target_vocab = format_target_vocab(
                    tgt_v, source_vocab, lang=lang, config=self.config
                )
                return (
                    target_field,
                    target_vocab,
                    mapping.target_uri,
                    pref_label,
                    mapping.mapping_type,
                    None,
                )

            # Check if source_uri is already a modern canonical concept URI
            concept = self.vocab_store.find_concept_by_uri(source_uri)
            if concept:
                pref_label = self._select_pref_label(concept, lang, clean_heading)
                target_field = self._resolve_target_field(
                    concept.vocabulary, source_field, concept.target_field
                )
                tgt_v = self._enforce_valid_field_vocab(target_field, concept.vocabulary)
                target_vocab = format_target_vocab(
                    tgt_v, source_vocab, lang=lang, config=self.config
                )
                return (
                    target_field,
                    target_vocab,
                    concept.uri,
                    pref_label,
                    "existing_canonical",
                    None,
                )

        # 2. Check legacy label in concept_mappings if source_vocab is an explicit legacy thesaurus
        legacy_sources = set(self.config.legacy_to_modern.keys())
        if norm_vocab in legacy_sources:
            mapping = self.vocab_store.find_legacy_mapping_by_label(
                clean_heading, legacy_vocab=norm_vocab, lang=effective_lang
            )
            if mapping:
                concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
                pref_label = (
                    self._select_pref_label(concept, lang, clean_heading)
                    if concept
                    else clean_heading
                )
                target_field = self._resolve_target_field(
                    mapping.target_vocab, source_field, mapping.target_field
                )
                tgt_v = self._enforce_valid_field_vocab(target_field, mapping.target_vocab)
                target_vocab = format_target_vocab(
                    tgt_v, source_vocab, lang=lang, config=self.config
                )
                return (
                    target_field,
                    target_vocab,
                    mapping.target_uri,
                    pref_label,
                    mapping.mapping_type,
                    None,
                )

        # 3. Dynamic alias lookup in concept_aliases (including multilingual, Wikidata P2347, and bridges)
        folded = fold_label(
            clean_heading,
            preserved_diacritics=self.config.preserved_diacritics,
            date_pattern=self.date_pattern,
        )
        concept, qid = self.vocab_store.find_concept_by_alias(folded, target_field=source_field)
        if concept:
            pref_label = self._select_pref_label(concept, lang, clean_heading)
            target_field = self._resolve_target_field(
                concept.vocabulary, source_field, concept.target_field
            )
            tgt_v = self._enforce_valid_field_vocab(target_field, concept.vocabulary)
            target_vocab = format_target_vocab(tgt_v, source_vocab, lang=lang, config=self.config)
            return (
                target_field,
                target_vocab,
                concept.uri,
                pref_label,
                "alias_match",
                qid,
            )

        # 4. Fallback legacy mapping lookup without legacy_vocab filter
        mapping = self.vocab_store.find_legacy_mapping_by_label(
            clean_heading, legacy_vocab=norm_vocab or None, lang=effective_lang
        )
        if mapping:
            concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
            pref_label = (
                self._select_pref_label(concept, lang, clean_heading) if concept else clean_heading
            )
            target_field = self._resolve_target_field(
                mapping.target_vocab, source_field, mapping.target_field
            )
            tgt_v = self._enforce_valid_field_vocab(target_field, mapping.target_vocab)
            target_vocab = format_target_vocab(tgt_v, source_vocab, lang=lang, config=self.config)
            return (
                target_field,
                target_vocab,
                mapping.target_uri,
                pref_label,
                mapping.mapping_type,
                None,
            )

        # 5. Direct lookup in concepts
        candidate_vocabs = list(self.config.field_to_vocabularies.get(source_field, []))
        if not candidate_vocabs:
            if source_field in self.config.default_field_vocabularies:
                candidate_vocabs = [self.config.default_field_vocabularies[source_field]]
            else:
                candidate_vocabs = list(
                    dict.fromkeys(self.config.default_field_vocabularies.values())
                )

        mapped_target = self.config.legacy_to_modern.get(norm_vocab)
        if mapped_target and mapped_target in candidate_vocabs:
            candidate_vocabs.remove(mapped_target)
            candidate_vocabs.insert(0, mapped_target)

        for v in candidate_vocabs:
            concept = self.vocab_store.find_concept_by_label(
                clean_heading, vocabulary=v, lang=effective_lang
            )
            if concept:
                pref_label = self._select_pref_label(concept, lang, clean_heading)
                target_field = self._resolve_target_field(
                    concept.vocabulary, source_field, concept.target_field
                )
                tgt_v = self._enforce_valid_field_vocab(target_field, concept.vocabulary)
                target_vocab = format_target_vocab(
                    tgt_v, source_vocab, lang=lang, config=self.config
                )
                return (
                    target_field,
                    target_vocab,
                    concept.uri,
                    pref_label,
                    "pref_label_lookup",
                    None,
                )

        # 6. Fallback if not found: keep as is
        default_target_vocab = self._get_default_target_vocab(source_field, norm_vocab)
        target_field = self._resolve_target_field(default_target_vocab, source_field)
        default_target_vocab = self._enforce_valid_field_vocab(target_field, default_target_vocab)
        formatted_default = format_target_vocab(
            default_target_vocab, source_vocab, lang=lang, config=self.config
        )
        return source_field, formatted_default, source_uri or "", clean_heading, "none", None
