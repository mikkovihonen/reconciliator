"""Centralized controlled vocabularies, subject headings, and hierarchy package."""

from reconciliator.vocab.detection import (
    concept_id_to_uri,
    detect_vocabulary,
    extract_concept_id,
    format_wikidata_uri,
    get_concept_id_rules,
    get_vocabulary_patterns,
    normalize_concept_id,
    strip_vocabulary_subcode,
)
from reconciliator.vocab.folding import (
    VocabFolder,
    format_target_vocab,
)
from reconciliator.vocab.hierarchy import (
    ConceptGraph,
    HierarchyService,
)
from reconciliator.vocab.manager import (
    VocabularyManager,
    normalize_subject_vocabulary,
    resolve_subject_concept_id,
)
from reconciliator.vocab.mixins.concept import is_language_allowed
from reconciliator.vocab.store import (
    VocabStore,
    init_vocab_db,
)
from reconciliator.vocab.subjects import (
    FoldedSubject,
    fold_subjects,
    subject_footprint,
    vocabulary_hint_from_uri,
)

__all__ = [
    # Detection & Extraction
    "detect_vocabulary",
    "extract_concept_id",
    "normalize_concept_id",
    "concept_id_to_uri",
    "format_wikidata_uri",
    "strip_vocabulary_subcode",
    "get_vocabulary_patterns",
    "get_concept_id_rules",
    # Manager & Policy
    "VocabularyManager",
    "normalize_subject_vocabulary",
    "resolve_subject_concept_id",
    # Store
    "VocabStore",
    "init_vocab_db",
    "is_language_allowed",
    # Folding
    "VocabFolder",
    "format_target_vocab",
    # Subjects
    "FoldedSubject",
    "vocabulary_hint_from_uri",
    "fold_subjects",
    "subject_footprint",
    # Hierarchy
    "HierarchyService",
    "ConceptGraph",
]
