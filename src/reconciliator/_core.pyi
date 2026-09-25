"""Type stubs for reconciliator._core native Rust extension."""

from typing import Dict, List, Optional, Sequence, Tuple

def rust_ping() -> str: ...

class normalizers:
    @staticmethod
    def py_fold_diacritics(s: str, preserved_diacritics: Optional[str] = None) -> str: ...
    @staticmethod
    def py_strip_biographical_dates(s: str) -> str: ...
    @staticmethod
    def py_fold_label(s: str, preserved_diacritics: Optional[str] = None) -> str: ...
    @staticmethod
    def py_isbn_10_to_13(isbn10: str) -> Optional[str]: ...
    @staticmethod
    def py_validate_isbn_13(isbn13: str) -> bool: ...
    @staticmethod
    def py_clean_alphanumeric(s: str) -> str: ...
    @staticmethod
    def py_deterministic_key_hash(key_content: str) -> str: ...
    @staticmethod
    def py_compute_work_blocking_key(author_key: str, title_stem: str) -> str: ...
    @staticmethod
    def py_compute_series_key(
        series_title_stem: Optional[str] = None,
        series_issn: Optional[str] = None,
    ) -> Optional[str]: ...
    @staticmethod
    def py_compute_host_citation_key(
        issn: Optional[str] = None,
        year: Optional[str] = None,
        start_page: Optional[str] = None,
        author_surname: Optional[str] = None,
    ) -> Optional[str]: ...

class rerank:
    @staticmethod
    def py_bm25_score_batch(
        query: str,
        documents: Sequence[str],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> List[float]: ...
    @staticmethod
    def py_normalize_volume(
        text: str,
        format_template: str = "osa {num}",
    ) -> Optional[str]: ...
    @staticmethod
    def py_check_numeric_title_tokens_compatible(
        target_title: str,
        candidate_title: str,
        target_year: Optional[int] = None,
        candidate_year: Optional[int] = None,
    ) -> bool: ...

class clustering:
    @staticmethod
    def py_lcs_length(seq1: Sequence[str], seq2: Sequence[str]) -> int: ...
    @staticmethod
    def py_jaccard_similarity(c1: Sequence[str], c2: Sequence[str]) -> float: ...
    @staticmethod
    def py_evaluate_aggregate_overlap(
        c1: Sequence[str],
        c2: Sequence[str],
        is_component1: bool = False,
        is_component2: bool = False,
        min_constituents: int = 2,
        jaccard_veto_threshold: float = 0.30,
        jaccard_merge_threshold: float = 0.80,
        sequence_alignment: bool = True,
        lcs_threshold: float = 0.70,
        merge_bonus_score: float = 0.95,
    ) -> Optional[Tuple[float, str]]: ...
    @staticmethod
    def py_cluster_connected_components(
        num_nodes: int,
        edges: Sequence[Tuple[int, int]],
    ) -> Dict[int, List[int]]: ...

class MarcSubfield:
    code: str
    value: str
    def __init__(self, code: str, value: str) -> None: ...

class MarcDatafield:
    tag: str
    ind1: str
    ind2: str
    subfields: List[MarcSubfield]
    def __init__(self, tag: str, ind1: str, ind2: str, subfields: List[MarcSubfield]) -> None: ...
    def get_subfield(self, code: str) -> Optional[str]: ...
    def get_subfields(self, code: str) -> List[str]: ...
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]: ...

class MarcRecordData:
    leader: Optional[str]
    controlfields: List[Tuple[str, str]]
    datafields: List[MarcDatafield]
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]: ...
    def get_controlfield(self, tag: str) -> Optional[str]: ...
    def get_datafields(self, tag: str) -> List[MarcDatafield]: ...
    def get_subfield(self, tag: str, code: str) -> Optional[str]: ...
    def get_subfields(self, tag: str, code: str) -> List[str]: ...
    def add_subfield(self, tag: str, code: str, value: str) -> bool: ...
    def add_datafield(
        self,
        tag: str,
        ind1: str = " ",
        ind2: str = " ",
        subfields: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> None: ...
    def to_xml(self) -> str: ...
    def to_xml_bytes(self) -> bytes: ...

class RawVocabConcept:
    uri: str
    vocabulary: str
    concept_id: str
    pref_labels: Dict[str, str]
    alt_labels: Dict[str, List[str]]
    is_deprecated: bool
    target_field: Optional[str]

class RawConceptMapping:
    legacy_uri: str
    legacy_vocab: str
    legacy_id: str
    target_uri: str
    target_vocab: str
    target_id: str
    mapping_type: str
    target_field: str
    legacy_labels: Dict[str, str]

class RawHierarchyEdge:
    vocabulary: str
    concept_id: str
    broader_concept_id: str
    relation_type: str

class parsers:
    MarcSubfield = MarcSubfield
    MarcDatafield = MarcDatafield
    MarcRecordData = MarcRecordData
    RawVocabConcept = RawVocabConcept
    RawConceptMapping = RawConceptMapping
    RawHierarchyEdge = RawHierarchyEdge

    @staticmethod
    def py_parse_marc_xml_str(xml: str) -> List[MarcRecordData]: ...
    @staticmethod
    def py_parse_marc_file(
        file_path: str,
        use_pigz: bool = True,
        pigz_threads: int = 4,
        pigz_path: Optional[str] = None,
    ) -> List[MarcRecordData]: ...
    @staticmethod
    def py_parse_turtle_content(
        content: str,
        default_vocab: Optional[str] = None,
        legacy_thesauri: Optional[List[str]] = None,
        vocab_target_fields: Optional[Dict[str, str]] = None,
        default_target_field: Optional[str] = None,
        pref_langs: Optional[List[str]] = None,
        alt_langs: Optional[List[str]] = None,
        custom_patterns: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[List[RawVocabConcept], List[RawConceptMapping], List[RawHierarchyEdge]]: ...
    @staticmethod
    def py_parse_turtle_file(
        file_path: str,
        default_vocab: Optional[str] = None,
        legacy_thesauri: Optional[List[str]] = None,
        vocab_target_fields: Optional[Dict[str, str]] = None,
        default_target_field: Optional[str] = None,
        pref_langs: Optional[List[str]] = None,
        alt_langs: Optional[List[str]] = None,
        custom_patterns: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[List[RawVocabConcept], List[RawConceptMapping], List[RawHierarchyEdge]]: ...
