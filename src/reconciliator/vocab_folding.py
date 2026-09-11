"""Subject heading reconciliation and legacy thesauri folding engine."""

from __future__ import annotations

from typing import Optional

from reconciliator.config import VocabNormalizationConfig
from reconciliator.normalizers.linguistic import build_biographical_date_pattern, fold_label
from reconciliator.vocab_store import VocabStore


def format_target_vocab(
    target_vocab: str,
    source_vocab: Optional[str] = None,
    lang: str = "fi",
    config: Optional[VocabNormalizationConfig] = None,
) -> str:
    """Format vocabulary code with compliant language suffix (e.g. yso/fin, yso/swe)."""
    raw_source = (source_vocab or "").strip().lower()
    base_vocab = target_vocab.split("/")[0].strip()

    # Subcode vocabularies
    subcode_vocabs = (
        set(config.language_subcode_vocabularies.keys())
        if config
        else {"yso", "yso-paikat", "kauno", "muso", "slm"}
    )
    if not subcode_vocabs:
        subcode_vocabs = {"yso", "yso-paikat", "kauno", "muso", "slm"}
    else:
        subcode_vocabs.update({"yso", "yso-paikat", "kauno", "muso", "slm"})

    if base_vocab not in subcode_vocabs:
        return base_vocab

    # 1. Preserve explicit suffix from source if present (e.g. slm/swe -> slm/swe)
    if "/" in raw_source and not raw_source.startswith("http"):
        _, suffix = raw_source.split("/", 1)
        suffix = suffix.strip()
        if suffix:
            return f"{base_vocab}/{suffix}"

    # 2. Known legacy sources
    thes_langs = (
        config.thesaurus_languages
        if config
        else {
            "ysa": "fi",
            "allars": "sv",
            "kaunokki": "fi",
            "bella": "sv",
            "musa": "fi",
            "cilla": "sv",
        }
    )
    if raw_source in thes_langs:
        mapped_l = thes_langs[raw_source]
        return f"{base_vocab}/{'swe' if mapped_l in ('sv', 'swe') else 'fin'}"

    # 3. For standard vocabularies, attach language suffix
    lang_suffix = "swe" if lang in ("sv", "swe") else "fin"
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

    def fold_subject_heading(
        self,
        heading: str,
        source_vocab: Optional[str] = None,
        source_field: str = "650",
        source_uri: Optional[str] = None,
        lang: str = "fi",
    ) -> tuple[str, str, str, str, str, Optional[str]]:
        """
        Reconcile a subject heading to canonical modern ontology.

        Returns:
            (target_field, target_vocab, target_uri, target_pref_label, mapping_type, wikidata_qid)
        """
        clean_heading = heading.strip().rstrip(".,; ")
        raw_vocab = (source_vocab or "").strip().lower()
        # Strip language suffix like /fin, /swe, /eng (e.g. yso/fin -> yso, slm/fin -> slm)
        norm_vocab = raw_vocab.split("/")[0].strip()

        # If already canonical modern ontology URI/code and no vocab_store
        if self.vocab_store is None:
            # Fallback heuristic if vocab_store is not populated
            if norm_vocab and norm_vocab in self.config.legacy_to_modern:
                target_vocab = self.config.legacy_to_modern[norm_vocab]
            elif source_field in self.config.default_field_vocabularies:
                target_vocab = self.config.default_field_vocabularies[source_field]
            else:
                target_vocab = norm_vocab or "yso"

            target_field = source_field
            if target_vocab == "slm":
                target_field = "655"
            elif target_vocab == "seko":
                target_field = "382"

            formatted_vocab = format_target_vocab(
                target_vocab, source_vocab, lang=lang, config=self.config
            )
            return target_field, formatted_vocab, source_uri or "", clean_heading, "none", None

        # 1. Check legacy URI in concept_mappings or canonical URI in concepts
        if source_uri:
            mapping = self.vocab_store.find_legacy_mapping_by_uri(source_uri)
            if mapping:
                concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
                pref_label = clean_heading
                if concept:
                    pref_label = (
                        (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                        or concept.pref_label_fi
                        or concept.pref_label_en
                        or clean_heading
                    )
                target_field = mapping.target_field or source_field
                # Enforce: 655 is SLM
                if target_field == "655" and mapping.target_vocab == "kauno":
                    mapping.target_vocab = "slm"
                target_vocab = format_target_vocab(
                    mapping.target_vocab, source_vocab, lang=lang, config=self.config
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
                pref_label = (
                    (concept.pref_label_sv if lang in ("sv", "swe") else concept.pref_label_fi)
                    or concept.pref_label_fi
                    or concept.pref_label_en
                    or clean_heading
                )
                target_field = concept.target_field or source_field
                if concept.vocabulary == "slm" and target_field == "650":
                    target_field = "655"
                target_vocab = format_target_vocab(
                    concept.vocabulary, source_vocab, lang=lang, config=self.config
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
        legacy_sources = set(self.config.legacy_to_modern.keys()) | {
            "ysa",
            "allars",
            "kaunokki",
            "bella",
            "musa",
            "cilla",
        }
        if norm_vocab in legacy_sources:
            mapping = self.vocab_store.find_legacy_mapping_by_label(
                clean_heading, legacy_vocab=norm_vocab, lang=lang
            )
            if mapping:
                concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
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
                target_vocab = format_target_vocab(
                    mapping.target_vocab, source_vocab, lang=lang, config=self.config
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

            target_vocab = format_target_vocab(
                concept.vocabulary, source_vocab, lang=lang, config=self.config
            )
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
            clean_heading, legacy_vocab=norm_vocab or None, lang=lang
        )
        if mapping:
            concept = self.vocab_store.find_concept_by_uri(mapping.target_uri)
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
            target_vocab = format_target_vocab(
                mapping.target_vocab, source_vocab, lang=lang, config=self.config
            )
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
            if source_field == "655":
                candidate_vocabs = ["slm"]
            elif source_field == "382":
                candidate_vocabs = ["seko"]
            elif source_field == "651":
                candidate_vocabs = ["yso-paikat", "yso"]
            else:
                candidate_vocabs = ["yso", "kauno", "muso"]

        mapped_target = self.config.legacy_to_modern.get(norm_vocab)
        if mapped_target and mapped_target in candidate_vocabs:
            candidate_vocabs.remove(mapped_target)
            candidate_vocabs.insert(0, mapped_target)

        for v in candidate_vocabs:
            concept = self.vocab_store.find_concept_by_label(clean_heading, vocabulary=v, lang=lang)
            if concept:
                pref_label = (
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

                target_vocab = format_target_vocab(
                    concept.vocabulary, source_vocab, lang=lang, config=self.config
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
        if norm_vocab and norm_vocab in self.config.legacy_to_modern:
            default_target_vocab = self.config.legacy_to_modern[norm_vocab]
        elif source_field in self.config.default_field_vocabularies:
            default_target_vocab = self.config.default_field_vocabularies[source_field]
        else:
            default_target_vocab = norm_vocab or "yso"

        formatted_default = format_target_vocab(
            default_target_vocab, source_vocab, lang=lang, config=self.config
        )
        return source_field, formatted_default, source_uri or "", clean_heading, "none", None
