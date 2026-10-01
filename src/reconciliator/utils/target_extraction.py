"""Target MatchRecord payload and identifier extraction utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from reconciliator.models import MatchRecord


def extract_identifiers_from_target(target: MatchRecord) -> dict[str, list[str]]:
    """Extract standard identifiers from MatchRecord fields and raw payload."""
    ids: dict[str, list[str]] = {}

    def _add(scheme: str, val: Optional[str]) -> None:
        if val and val.strip():
            s = scheme.lower().strip()
            v = val.strip()
            if s not in ids:
                ids[s] = []
            if v not in ids[s]:
                ids[s].append(v)

    # From target blocking / domain keys
    if target.get_first_key("standard"):
        from reconciliator.normalizers.identifiers import classify_identifier_scheme

        sk = target.get_first_key("standard").strip()
        scheme, norm_val = classify_identifier_scheme(sk)
        _add(scheme, norm_val)
        if scheme == "standard_number" and target.work_type in ("music", "audio_nonmusic"):
            _add("publisher_number", sk)

    if (
        target.get_first_key("series")
        and "-" in target.get_first_key("series")
        and len(target.get_first_key("series")) == 9
    ):
        _add("issn", target.get_first_key("series"))

    domain_key_schemes = (
        ("patent", ("patent_number", "patent")),
        ("statute", ("sdk", "eli")),
        ("swhid", ("swhid",)),
        ("fingerprint", ("fingerprint",)),
        ("podcast", ("guid_rss",)),
        ("board_game", ("bgg", "ean")),
        ("fragment_codex", ("fragmenta",)),
        ("numismatic_issue", ("michel", "scott", "km")),
        ("geodata_spatial", ("epsg",)),
        ("web_seed", ("memento_uri",)),
        ("sammelband", ("barcode",)),
    )
    for dk, target_schemes in domain_key_schemes:
        dk_val = target.get_first_key(dk)
        if dk_val:
            for ts in target_schemes:
                _add(ts, dk_val)

    for scheme, values in (target.identifiers or {}).items():
        for value in values:
            _add(scheme, value)

    return ids


def extract_target_track_titles(target: MatchRecord) -> set[str]:
    """Constituent track titles, from the analytics extracted at ingest."""
    return {title for title, _creator in extract_target_analytics(target)}


def extract_target_analytics(target: MatchRecord) -> list[tuple[str, Optional[str]]]:
    """Constituent works (title, creator), from the analytics extracted at ingest.

    These come from MARC 700/710/505 `$t`, parsed once by the loader into
    `entity_keys` rather than re-parsed from the payload on every match attempt.
    """
    from reconciliator.normalizers.keys import decode_analytic

    return [decode_analytic(v) for v in target.get_keys("analytic")]
