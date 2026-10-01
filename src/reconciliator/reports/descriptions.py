"""Dynamic descriptions and quality bracket derivation for reporting."""

from __future__ import annotations

from typing import Any, Optional

from reconciliator.config import ReconcileConfig
from reconciliator.reports.models import QualityBracket
from reconciliator.vocab import strip_vocabulary_subcode


def _get_model_field_default(model_cls: type, field_name: str) -> list[str]:
    """Extract default list from a Pydantic model field definition if available."""
    field_info = getattr(model_cls, "model_fields", {}).get(field_name)
    if field_info is not None:
        val = field_info.get_default(call_default_factory=True)
        if isinstance(val, (list, tuple)):
            return [str(x) for x in val]
    return []


def describe_agent_vocabulary(vocab: str) -> str:
    """Dynamically format human-readable description for an agent vocabulary."""
    if not vocab:
        return "Unspecified agent vocabulary"
    v_clean = vocab.strip().upper()
    if v_clean in ("OTHER", "UNCLASSIFIED", "UNKNOWN"):
        return "Unclassified or external agent identifier URIs"
    if v_clean in ("WIKIDATA", "WD"):
        return "Wikidata entity QID URIs ($1)"
    return f"{v_clean} agent identifier URIs ($0)"


def describe_source_thesaurus(thesaurus: str) -> str:
    """Dynamically format human-readable description for a source thesaurus."""
    if not thesaurus:
        return "Legacy terms with no $2 or local tags"
    norm = thesaurus.strip().lower()
    if norm in ("unspecified_legacy", "unspecified", "none", "legacy"):
        return "Legacy terms with no $2 or local tags"

    if norm == "allars":
        label = "Allärs"
    elif len(norm) <= 4:
        label = norm.upper()
    else:
        label = norm.capitalize()

    return f"Terms originated from {label}"


def describe_target_ontology(ontology: str) -> str:
    """Dynamically format human-readable description for a target ontology."""
    if not ontology:
        return "Unspecified target ontology"
    norm = ontology.strip().lower()
    if norm in ("wikidata", "wd"):
        return "Mapped to Wikidata Entity URIs"

    if "-" in norm:
        prefix, *rest = norm.split("-")
        ont_label = f"{prefix.upper()}-{'-'.join(rest)}"
    else:
        ont_label = norm.upper()

    return f"Mapped to {ont_label}"


def describe_tag_migration(key: str) -> str:
    """Dynamically generate human-readable description based on what happened in what field."""
    parts = key.split("_to_")
    if len(parts) != 2:
        return f"Subject migration/folding {key}"

    left_tokens = parts[0].split("_")
    right_tokens = parts[1].split("_")

    src_tag = left_tokens[0]
    src_v = left_tokens[1].upper() if len(left_tokens) > 1 else None

    if len(right_tokens) == 2:
        tgt_tag, tgt_v = right_tokens[0], right_tokens[1].upper()
    elif len(right_tokens) == 1:
        if right_tokens[0].isdigit():
            tgt_tag, tgt_v = right_tokens[0], None
        else:
            tgt_tag, tgt_v = src_tag, right_tokens[0].upper()
    else:
        return f"Subject migration/folding {key}"

    if src_tag == tgt_tag and src_v and tgt_v:
        return f"In {src_tag}, {src_v} folded to {tgt_v}"

    if src_tag != tgt_tag and src_v and tgt_v:
        return f"Terms in {src_tag} ({src_v}) retagged to {tgt_tag} {tgt_v}"

    if tgt_v:
        return f"Terms in {src_tag} retagged to {tgt_tag} {tgt_v}"
    return f"Terms in {src_tag} retagged to {tgt_tag}"


class _DynamicDescriptions(dict[str, str]):
    """Dynamic dict-like mapping that resolves keys via a generator function."""

    def __init__(self, generator: Any) -> None:
        super().__init__()
        self._generator = generator

    def __getitem__(self, key: str) -> str:
        return str(self._generator(key))

    def __contains__(self, key: object) -> bool:
        return True

    def get(self, key: str, default: Any = None) -> str:
        return str(self._generator(key))


DEFAULT_AGENT_VOCAB_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(describe_agent_vocabulary)
DEFAULT_SOURCE_THESAURUS_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(
    describe_source_thesaurus
)
DEFAULT_TARGET_ONTOLOGY_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(
    describe_target_ontology
)
DEFAULT_TAG_MIGRATION_DESCRIPTIONS: dict[str, str] = _DynamicDescriptions(describe_tag_migration)


def normalize_vocab_code(vocab: Optional[str]) -> str:
    """Normalize vocabulary code by stripping language subcodes (e.g. yso/fin -> yso)."""
    if not vocab:
        return ""
    clean = vocab.strip()
    if not clean.startswith("http"):
        clean = strip_vocabulary_subcode(clean)
    return clean.lower()


def derive_quality_brackets(
    reconcile_cfg: Optional[ReconcileConfig],
) -> list[QualityBracket]:
    """Derive quality score brackets based on application thresholds or custom brackets.

    Returns a list of QualityBracket objects ordered from highest to lowest score.
    """
    if reconcile_cfg and reconcile_cfg.quality_brackets:
        cutoffs = sorted(reconcile_cfg.quality_brackets)
        brackets: list[QualityBracket] = []
        top_cut = cutoffs[-1]
        brackets.append(
            QualityBracket(
                metric=f"score_{top_cut:.2f}_to_1.00",
                low=top_cut,
                high=1.0,
                is_top=True,
                description=f"Candidate matches with score in [{top_cut:.2f}, 1.00]",
            )
        )
        for i in range(len(cutoffs) - 1, 0, -1):
            low = cutoffs[i - 1]
            high = cutoffs[i]
            brackets.append(
                QualityBracket(
                    metric=f"score_{low:.2f}_to_{high:.2f}",
                    low=low,
                    high=high,
                    is_top=False,
                    description=f"Candidate matches with score in [{low:.2f}, {high:.2f})",
                )
            )
        lowest = cutoffs[0]
        if lowest > 0.0:
            brackets.append(
                QualityBracket(
                    metric=f"score_below_{lowest:.2f}",
                    low=None,
                    high=lowest,
                    is_top=False,
                    description=f"Candidate matches with score below {lowest:.2f}",
                )
            )
        return brackets

    t_accept = round(reconcile_cfg.threshold, 2) if reconcile_cfg else 0.85
    if (
        reconcile_cfg
        and reconcile_cfg.rerank
        and reconcile_cfg.rerank.short_circuit
        and reconcile_cfg.rerank.short_circuit.enabled
    ):
        t_short = round(reconcile_cfg.rerank.short_circuit.threshold, 2)
    elif reconcile_cfg is None:
        t_short = 0.98
    else:
        t_short = 0.95

    t_short = min(1.0, max(0.0, t_short))
    t_accept = min(1.0, max(0.0, t_accept))

    brackets = []
    if t_short > t_accept:
        t_mid = round((t_accept + t_short) / 2, 2)
        if t_mid > t_accept and t_mid < t_short:
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_short:.2f}_to_1.00",
                    low=t_short,
                    high=1.0,
                    is_top=True,
                    description=f"Candidate matches with score in [{t_short:.2f}, 1.00] (high-confidence / short-circuit)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_mid:.2f}_to_{t_short:.2f}",
                    low=t_mid,
                    high=t_short,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_mid:.2f}, {t_short:.2f}) (robust hybrid reranked)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_accept:.2f}_to_{t_mid:.2f}",
                    low=t_accept,
                    high=t_mid,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_accept:.2f}, {t_mid:.2f}) (meeting acceptance threshold)",
                )
            )
        else:
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_short:.2f}_to_1.00",
                    low=t_short,
                    high=1.0,
                    is_top=True,
                    description=f"Candidate matches with score in [{t_short:.2f}, 1.00] (high-confidence)",
                )
            )
            brackets.append(
                QualityBracket(
                    metric=f"score_{t_accept:.2f}_to_{t_short:.2f}",
                    low=t_accept,
                    high=t_short,
                    is_top=False,
                    description=f"Candidate matches with score in [{t_accept:.2f}, {t_short:.2f}) (meeting acceptance threshold)",
                )
            )
    else:
        brackets.append(
            QualityBracket(
                metric=f"score_{t_accept:.2f}_to_1.00",
                low=t_accept,
                high=1.0,
                is_top=True,
                description=f"Candidate matches with score in [{t_accept:.2f}, 1.00] (meeting acceptance threshold)",
            )
        )

    if t_accept > 0.0:
        brackets.append(
            QualityBracket(
                metric=f"score_below_{t_accept:.2f}",
                low=None,
                high=t_accept,
                is_top=False,
                description=f"Candidate matches with score below {t_accept:.2f} (below acceptance threshold)",
            )
        )

    return brackets
