"""MARCXML tree mutator injecting authority $0 and $1 subfields across all 42 WEMI domains."""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from reconciliator.config import (
    DEFAULT_LANGUAGE,
    EnrichmentConfig,
    get_bundled_vocabularies_defaults,
)
from reconciliator.enrichment.handlers import (
    AgentEnricher,
    LinkageEnricher,
    SubjectEnricher,
)
from reconciliator.loaders.marcxml import (
    _get_controlfield,
    _get_datafields,
    _get_subfields,
)
from reconciliator.models import (
    CorpusLinkage,
    ExpressionCluster,
    ManifestationCluster,
    MatchRecord,
    WorkCluster,
)
from reconciliator.normalizers.relators import RelatorRegistry
from reconciliator.reconciliation import WEMIMatchResult
from reconciliator.vocab import VocabFolder

_LANG_CODE_MAP: dict[str, str] = dict(
    get_bundled_vocabularies_defaults().get("language_code_map") or {}
)


def _extract_record_language(record_elem: etree._Element) -> Optional[str]:
    """Extract 2-letter language code from 041 $a or 008 (bytes 35-37)."""
    for f041 in _get_datafields(record_elem, "041"):
        a_vals = _get_subfields(f041, "a")
        if a_vals:
            code = a_vals[0].strip().lower()
            mapped = _LANG_CODE_MAP.get(code)
            if mapped:
                return mapped
            return code[:2]
    f008 = _get_controlfield(record_elem, "008")
    if f008 and len(f008) >= 38:
        code = f008[35:38].strip().lower()
        mapped = _LANG_CODE_MAP.get(code)
        if mapped:
            return mapped
        return code[:2]
    return None


class MARCXMLEnricher:
    """Mutates MARCXML trees with novel authority links, folded modern terms, and WEMI relationships."""

    def __init__(
        self,
        vocab_folder: Optional[VocabFolder] = None,
        relator_registry: Optional[RelatorRegistry] = None,
    ) -> None:
        vf = vocab_folder or VocabFolder()
        rr = relator_registry or RelatorRegistry(
            vocab_store=getattr(vf, "vocab_store", None)
        )
        self.subject_enricher = SubjectEnricher(vocab_folder=vf)
        self.agent_enricher = AgentEnricher(relator_registry=rr)
        self.linkage_enricher = LinkageEnricher(vocab_folder=vf)

    @property
    def vocab_folder(self) -> VocabFolder:
        return self.subject_enricher.vocab_folder

    @vocab_folder.setter
    def vocab_folder(self, val: VocabFolder) -> None:
        self.subject_enricher.vocab_folder = val
        self.linkage_enricher.vocab_folder = val

    @property
    def relator_registry(self) -> RelatorRegistry:
        return self.agent_enricher.relator_registry

    @relator_registry.setter
    def relator_registry(self, val: RelatorRegistry) -> None:
        self.agent_enricher.relator_registry = val

    def enrich_record_tree(
        self,
        record_elem: etree._Element,
        result: Any = None,
        enrich: bool = True,
        fold_legacy: bool = True,
        wemi_result: Optional[WEMIMatchResult] = None,
        linkage: Optional[CorpusLinkage] = None,
        work_cluster: Optional[WorkCluster] = None,
        expression_cluster: Optional[ExpressionCluster] = None,
        manifestation_cluster: Optional[ManifestationCluster] = None,
        target_record: Optional[MatchRecord] = None,
        enrichment_config: Optional[EnrichmentConfig] = None,
        sibling_ids: Optional[list[str]] = None,
        constituent_linkages: Optional[list[Any]] = None,
    ) -> etree._Element:
        """Enrich a MARCXML <record> element in-place with novel authority links and modernized subjects."""
        if not enrich and not fold_legacy:
            return record_elem

        toggles = enrichment_config or EnrichmentConfig()
        record_lang = _extract_record_language(record_elem) or toggles.language or DEFAULT_LANGUAGE

        # Determine XML namespace prefix if used
        if record_elem.tag.startswith("{"):
            ns = record_elem.tag.split("}")[0] + "}"
            subfield_tag = f"{ns}subfield"
            datafield_tag = f"{ns}datafield"
        else:
            ns = ""
            subfield_tag = "subfield"
            datafield_tag = "datafield"

        # 1. Subject Modernization & Folding
        self.subject_enricher.enrich(
            record_elem=record_elem,
            record_lang=record_lang,
            ns=ns,
            subfield_tag=subfield_tag,
            datafield_tag=datafield_tag,
            toggles=toggles,
            enrich=enrich,
            fold_legacy=fold_legacy,
        )

        # 2. Agent Authority Enrichment & Relator Role Normalization
        self.agent_enricher.enrich(
            record_elem=record_elem,
            result=result,
            record_lang=record_lang,
            ns=ns,
            subfield_tag=subfield_tag,
            datafield_tag=datafield_tag,
            toggles=toggles,
            enrich=enrich,
        )

        # 3. WEMI Graph & Specialized Domain Linkages
        self.linkage_enricher.enrich(
            record_elem=record_elem,
            result=result,
            wemi_result=wemi_result,
            linkage=linkage,
            work_cluster=work_cluster,
            expression_cluster=expression_cluster,
            manifestation_cluster=manifestation_cluster,
            target_record=target_record,
            sibling_ids=sibling_ids,
            constituent_linkages=constituent_linkages,
            record_lang=record_lang,
            ns=ns,
            subfield_tag=subfield_tag,
            datafield_tag=datafield_tag,
            toggles=toggles,
            enrich=enrich,
        )

        return record_elem

    def enrich_wemi_record_tree(
        self,
        record_elem: etree._Element,
        wemi_result: Optional[WEMIMatchResult] = None,
        linkage: Optional[CorpusLinkage] = None,
        work_cluster: Optional[WorkCluster] = None,
        expression_cluster: Optional[ExpressionCluster] = None,
        manifestation_cluster: Optional[ManifestationCluster] = None,
        target_record: Optional[MatchRecord] = None,
        enrich: bool = True,
        fold_legacy: bool = True,
        enrichment_config: Optional[EnrichmentConfig] = None,
        constituent_linkages: Optional[list[Any]] = None,
    ) -> etree._Element:
        """Convenience method explicitly for WEMI graph and cluster enrichment."""
        return self.enrich_record_tree(
            record_elem=record_elem,
            result=wemi_result or linkage,
            enrich=enrich,
            fold_legacy=fold_legacy,
            wemi_result=wemi_result,
            linkage=linkage,
            work_cluster=work_cluster,
            expression_cluster=expression_cluster,
            manifestation_cluster=manifestation_cluster,
            target_record=target_record,
            enrichment_config=enrichment_config,
            constituent_linkages=constituent_linkages,
        )
