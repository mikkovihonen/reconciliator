"""Subject heading modernization, vocabulary folding, and authority URI enrichment."""

from __future__ import annotations

from typing import Optional

from lxml import etree

from reconciliator.config import (
    EnrichmentConfig,
    get_subject_field_category_map,
)
from reconciliator.loaders.marcxml import (
    MARC_NS,
    _get_datafields,
    _get_subfields,
)
from reconciliator.utils.curie import to_actionable_uri
from reconciliator.vocab import VocabFolder, format_wikidata_uri


class SubjectEnricher:
    """Handles subject heading modernization, ontology folding, and $0/$1 authority URI injection."""

    def __init__(self, vocab_folder: Optional[VocabFolder] = None) -> None:
        self.vocab_folder = vocab_folder or VocabFolder()

    def enrich(
        self,
        record_elem: etree._Element,
        record_lang: str,
        ns: str,
        subfield_tag: str,
        datafield_tag: str,
        toggles: EnrichmentConfig,
        enrich: bool = True,
        fold_legacy: bool = True,
    ) -> None:
        """Modernize subject headings in-place across 6XX fields."""
        if not fold_legacy:
            return

        vocab_cfg = self.vocab_folder.config
        tags_to_modernize = list(vocab_cfg.field_to_vocabularies.keys())
        if not tags_to_modernize:
            tags_to_modernize = list(get_subject_field_category_map().keys())

        for tag in tags_to_modernize:
            for field in _get_datafields(record_elem, tag):
                # Get $a
                a_elems = field.findall("marc:subfield[@code='a']", MARC_NS) or field.findall(
                    "subfield[@code='a']"
                )
                heading = a_elems[0].text.strip() if a_elems and a_elems[0].text else ""
                if not heading:
                    continue

                # Get $2
                f2_elems = field.findall("marc:subfield[@code='2']", MARC_NS) or field.findall(
                    "subfield[@code='2']"
                )
                vocab = f2_elems[0].text.strip() if f2_elems and f2_elems[0].text else None

                # Get $0
                existing_uris = _get_subfields(field, "0")
                source_uri = existing_uris[0] if existing_uris else None

                target_field, target_vocab, target_uri, target_label, mapping_type, qid = (
                    self.vocab_folder.fold_subject_heading(
                        heading=heading,
                        source_vocab=vocab,
                        source_field=tag,
                        source_uri=source_uri,
                        lang=record_lang,
                    )
                )

                # An unresolved heading must be left exactly as catalogued.
                if mapping_type == "none" and not target_uri:
                    continue

                # If tag changed (e.g. 650 -> 655 for SLM)
                if target_field != tag:
                    field.set("tag", target_field)

                # Replace $a with the ontology preferred label, optionally keeping original
                if target_label and a_elems:
                    original_heading = a_elems[0].text
                    if vocab_cfg.replace_heading_with_pref_label:
                        if (
                            vocab_cfg.preserve_original_heading
                            and original_heading
                            and original_heading.strip() != target_label
                        ):
                            keep_code = vocab_cfg.original_heading_subfield
                            if original_heading.strip() not in _get_subfields(field, keep_code):
                                sub_keep = etree.SubElement(field, subfield_tag, code=keep_code)
                                sub_keep.text = original_heading.strip()
                        a_elems[0].text = target_label

                # Update or add $2
                if target_vocab:
                    if f2_elems:
                        f2_elems[0].text = target_vocab
                    else:
                        sub_2 = etree.SubElement(field, subfield_tag, code="2")
                        sub_2.text = target_vocab

                # Inject or update modern $0 URI if enrich is active
                if enrich and target_uri:
                    actionable_target_uri = to_actionable_uri(target_uri)
                    if source_uri and source_uri in existing_uris:
                        f0_elems = field.findall(
                            "marc:subfield[@code='0']", MARC_NS
                        ) or field.findall("subfield[@code='0']")
                        for elem in f0_elems:
                            if elem.text and elem.text.strip() == source_uri:
                                elem.text = actionable_target_uri
                                break
                    elif actionable_target_uri not in existing_uris:
                        sub_0 = etree.SubElement(field, subfield_tag, code="0")
                        sub_0.text = actionable_target_uri

                # Inject $1 Wikidata URI if enrich is active and qid is present
                if enrich and qid:
                    existing_subj_u1 = _get_subfields(field, "1")
                    wikidata_uri = format_wikidata_uri(qid)
                    if (
                        wikidata_uri
                        and wikidata_uri not in existing_subj_u1
                        and not any(e.endswith(qid) for e in existing_subj_u1)
                    ):
                        sub_1 = etree.SubElement(field, subfield_tag, code="1")
                        sub_1.text = wikidata_uri
