"""The 42 built-in domain funnels, in dispatch order.

Order is precedence: the first spec whose `when` claims a record wins, and the
one marked `default` claims whatever nothing else did. This replaces a
44-condition if-chain in which precedence was implied by statement order and
could only be read by tracing the whole function.

Six work types the old chain tested for - article, chapter, statute,
anthology_collection, conference_proceedings, government_publication - are absent
from the `WorkType` literal, so no record could ever carry them and those
branches were dead. They are omitted rather than transcribed. Funnel C is still
reachable through `is_component` and the host citation key, which is how articles
actually arrive.
"""

from __future__ import annotations

from typing import Optional

from reconciliator.config import get_bundled_section_defaults
from reconciliator.funnels.spec import FunnelSpec

_FUNNELS_DEFAULTS = get_bundled_section_defaults("funnels")
_PATTERNS = dict(_FUNNELS_DEFAULTS.get("patterns") or {})

__all__ = [
    "build_builtin_specs",
    "BUILTIN_SPECS",
]


def build_builtin_specs(patterns: Optional[dict[str, str]] = None) -> list[FunnelSpec]:
    """The 42 built-in domain funnels, in dispatch order."""
    p = {**_PATTERNS, **(patterns or {})}
    return [
        # --- components and articles -------------------------------------------
        FunnelSpec(
            id="funnel_c_article",
            when=[{"is_component": True}, {"has_key": ["citation"]}],
            identifier_schemes=["doi", "urn"],
            match_tier="tier2_host_citation_key",
            confidence=0.95,
            align_expression=True,
        ),
        # --- music: notated before recorded ------------------------------------
        FunnelSpec(
            id="funnel_d_notated_music",
            # Keyed on content_type, not on the music_work key. The original chain
            # tested `music_work_key or "score" in stem or ismn/plate_number`, and
            # derive_domain_keys emits a music_work key for EVERY music record - so
            # that first alternative claimed every sound recording too, and
            # funnel_b_music_sound was unreachable in practice. content_type comes
            # from the MARC leader and is what actually distinguishes the two.
            when=[
                {"work_type": ["music"], "content_type": ["notated_music"]},
                # The same score vocabulary the clusterer uses. "score" alone is
                # English-only, and this is a Finnish-language catalogue.
                {
                    "work_type": ["music"],
                    "title_matches": p["notated_music_title"],
                },
                {"work_type": ["music"], "has_identifier": ["ismn"]},
            {"work_type": ["music"], "has_identifier": ["plate_number"]},
        ],
        identifier_schemes=["ismn", "plate_number"],
        work_types=["music"],
        fallback_work_type="music",
        match_tier="tier2_music_signature",
        confidence=0.92,
        content_type="notated_music",
        align_expression=True,
        constituents="titles",
        handler="notated_music",
    ),
    FunnelSpec(
        id="funnel_b_music_sound",
        when={"work_type": ["music"]},
        identifier_schemes=["publisher_number", "isrc", "ean", "upc"],
        work_types=["music"],
        fallback_work_type="music",
        match_tier="tier2_performer_alignment",
        confidence=0.88,
        content_type="audio_music",
        align_expression=True,
        handler="recorded_sound",
    ),
    # --- spoken audio ------------------------------------------------------
    FunnelSpec(
        id="funnel_cc_podcast",
        when=[
            {"work_type": ["audio_nonmusic"], "has_key": ["podcast"]},
            {"work_type": ["audio_nonmusic"], "title_matches": p["podcast_title"]},
            {"work_type": ["podcast"]},
        ],
        identifier_schemes=["guid_rss", "spotify_uri"],
        work_types=["podcast"],
        fallback_work_type="podcast",
        confidence=0.88,
    ),
    FunnelSpec(
        id="funnel_ff_oral_history",
        when=[
            {"work_type": ["audio_nonmusic"], "has_key": ["oral_history"]},
            {"work_type": ["oral_history"]},
        ],
        identifier_schemes=["handle", "signum"],
        work_types=["oral_history"],
        fallback_work_type="oral_history",
        match_tier="tier2_session_triad",
        confidence=0.88,
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_gg_broadcast",
        when=[
            {"work_type": ["audio_nonmusic"], "has_key": ["broadcast"]},
            {"work_type": ["moving_image"], "has_key": ["broadcast"]},
            {"work_type": ["broadcast_program"]},
        ],
        identifier_schemes=["eidr", "isan"],
        asserts_relationship="broadcast_of",
        work_types=["broadcast_program"],
        fallback_work_type="broadcast_program",
        match_tier="tier2_transmission_triad",
    ),
    FunnelSpec(
        id="funnel_e_audiobook",
        when={"work_type": ["audio_nonmusic"]},
        identifier_schemes=["isbn", "publisher_number"],
        work_types=["text"],
        fallback_work_type="text",
        match_tier="cross_format",
        confidence=0.92,
        content_type="spoken_word",
        align_expression=True,
        handler="audiobook",
    ),
    # --- moving image ------------------------------------------------------
    FunnelSpec(
        id="funnel_f_moving_image",
        when={"work_type": ["moving_image"]},
        identifier_schemes=["ean", "upc", "publisher_number"],
        work_types=["moving_image"],
        fallback_work_type="moving_image",
        content_type="moving_image",
        align_expression=True,
        constituents="titles",
    ),
    # --- continuing resources ----------------------------------------------
    FunnelSpec(
        id="funnel_h_serial",
        when={"work_type": ["serial"]},
        identifier_schemes=["issn", "issn_l"],
        work_types=["serial"],
        fallback_work_type="serial",
        match_tier="tier2_succession_chain",
        align_expression=True,
        relationships=True,
    ),
    FunnelSpec(
        id="funnel_w_integrating",
        when={"work_type": ["integrating"]},
        identifier_schemes=["issn", "loose_leaf_id"],
        work_types=["integrating"],
        fallback_work_type="integrating",
        confidence=0.88,
    ),
    # --- single work types -------------------------------------------------
    FunnelSpec(
        id="funnel_k_thesis",
        when={"work_type": ["thesis"]},
        identifier_schemes=["urn", "handle", "doi"],
        work_types=["thesis", "text"],
        fallback_work_type="thesis",
        match_tier="tier2_dissertation_dedup",
        confidence=0.92,
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_nn_geodata",
        # Must precede funnel_l_cartography: a cartographic dataset is also
        # work_type "cartography", so cartography would otherwise claim it first.
        # Safe to put earlier because this `when` requires the dataset content
        # type, which an ordinary map does not carry.
        # SLM has no genre term for spatial data, and the Finnish guidance
        # documents no convention for a geodata service, so work_type alone made
        # this unreachable. The RDA content type `crd` (cartographic dataset) is
        # the real signal: a cartographic *dataset* is spatial data, as distinct
        # from `cri`, a cartographic image, which is an ordinary map.
        when=[
            {"work_type": ["geodata_service"]},
            {"content_type": ["cartographic_dataset"]},
        ],
        identifier_schemes=["epsg", "service_endpoint"],
        asserts_relationship="spatial_layer_of",
        work_types=["geodata_service"],
        fallback_work_type="geodata_service",
        match_tier="tier2_spatial_polygon",
    ),
    FunnelSpec(
        id="funnel_l_cartography",
        when={"work_type": ["cartography"]},
        identifier_schemes=["sheet_number", "isbn"],
        work_types=["cartography"],
        fallback_work_type="cartography",
        content_type="cartographic_image",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_m_comic",
        when={"work_type": ["comic"]},
        identifier_schemes=["isbn", "issn"],
        work_types=["comic"],
        fallback_work_type="comic",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_n_dataset",
        when={"work_type": ["dataset"]},
        identifier_schemes=["doi", "handle"],
        work_types=["dataset"],
        fallback_work_type="dataset",
        content_type="dataset",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_o_standard",
        when={"work_type": ["standard"]},
        identifier_schemes=["strn", "standard_number", "standard"],
        work_types=["standard"],
        fallback_work_type="standard",
        relationships=True,
    ),
    FunnelSpec(
        id="funnel_t_legal",
        when=[{"work_type": ["legal"]}, {"has_key": ["statute"]}],
        identifier_schemes=["sdk", "eli", "celex", "ecli"],
        work_types=["legal"],
        fallback_work_type="legal",
        confidence=0.92,
        relationships=True,
    ),
    FunnelSpec(
        id="funnel_u_patent",
        when={"work_type": ["patent"]},
        identifier_schemes=["patent_number", "patent"],
        work_types=["patent"],
        fallback_work_type="patent",
    ),
    FunnelSpec(
        id="funnel_v_game",
        when={"work_type": ["interactive_game"]},
        identifier_schemes=["ean", "upc", "sku"],
        work_types=["interactive_game"],
        fallback_work_type="interactive_game",
        content_type="computer_program",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_x_board_game",
        when={"work_type": ["board_game"]},
        identifier_schemes=["ean", "publisher_code", "bgg"],
        work_types=["board_game"],
        fallback_work_type="board_game",
        relationships=True,
        constituents="titles",
    ),
    FunnelSpec(
        id="funnel_y_early_print",
        when={"work_type": ["early_print"]},
        identifier_schemes=["fingerprint", "istc", "gw"],
        work_types=["early_print"],
        fallback_work_type="early_print",
        confidence=0.88,
    ),
    FunnelSpec(
        id="funnel_z_tactile",
        when={"work_type": ["tactile"]},
        identifier_schemes=["isbn", "control_number"],
        asserts_relationship="accessible_reproduction_of",
        work_types=["text"],
        fallback_work_type="text",
        match_tier="tier2_accessible_repro",
    ),
    FunnelSpec(
        id="funnel_aa_software_ai",
        when={"work_type": ["software", "ai_model"]},
        identifier_schemes=["swhid", "huggingface"],
        work_types=["software", "ai_model"],
        fallback_work_type="software",
        relationships=True,
    ),
    FunnelSpec(
        id="funnel_bb_drama",
        when={"work_type": ["drama", "choreography"]},
        identifier_schemes=["play_id", "urn"],
        asserts_relationship="staging_of",
        work_types=["drama", "choreography"],
        fallback_work_type="drama",
    ),
    FunnelSpec(
        id="funnel_dd_sammelband",
        when={"has_key": ["sammelband"]},
        identifier_schemes=["barcode", "shelfmark"],
        match_tier="tier2_sammelband_isolated",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_ee_web_archive",
        when={"work_type": ["web_harvest"]},
        identifier_schemes=["memento_uri", "warc_id"],
        asserts_relationship="harvest_of",
        work_types=["web_harvest"],
        fallback_work_type="web_harvest",
    ),
    FunnelSpec(
        id="funnel_hh_parallel_multilingual",
        when={
            "key_matches": {
                "translators_signature": p["parallel_translators_signature"]
            }
        },
        identifier_schemes=["work_uri", "isbn"],
        asserts_relationship="parallel_expression_in",
        work_types=["text"],
        fallback_work_type="text",
        match_tier="tier2_parallel_matrix",
    ),
    FunnelSpec(
        id="funnel_ii_preservation_surrogate",
        when={"work_type": ["preservation_surrogate"]},
        identifier_schemes=["control_number", "isbn"],
        asserts_relationship="reproduction_of",
        work_types=["text"],
        fallback_work_type="text",
        match_tier="tier2_reproduction_note",
        confidence=0.92,
    ),
    FunnelSpec(
        id="funnel_jj_medieval_fragment",
        when={"work_type": ["codex_fragment"]},
        identifier_schemes=["fragmenta"],
        asserts_relationship="fragment_of_codex",
        work_types=["codex_fragment"],
        fallback_work_type="codex_fragment",
        match_tier="tier2_virtual_codex",
        confidence=0.92,
    ),
    FunnelSpec(
        id="funnel_kk_ephemera",
        when={"work_type": ["ephemera"]},
        identifier_schemes=["corporate_authority"],
        work_types=["ephemera"],
        fallback_work_type="ephemera",
        match_tier="tier2_corporate_ephemera",
        confidence=0.88,
    ),
    FunnelSpec(
        id="funnel_g_anthology",
        when={"work_type": ["anthology"]},
        identifier_schemes=["isbn"],
        work_types=["text"],
        fallback_work_type="text",
        confidence=0.88,
        align_expression=True,
        constituents="resolve",
    ),
    FunnelSpec(
        id="funnel_p_government",
        when=[
            {"work_type": ["government"]},
            {
                "identifier_matches": {
                    "urn": p["government_valto_urn"]
                }
            },
        ],
        identifier_schemes=["urn", "isbn"],
        work_types=["text"],
        fallback_work_type="text",
        confidence=0.88,
    ),
    FunnelSpec(
        id="funnel_ll_exhibition",
        when=[
            {"work_type": ["exhibition", "event"], "has_key": ["exhibition_event"]},
            {
                "work_type": ["exhibition", "event"],
                "title_matches": p["exhibition_title"],
            },
        ],
        identifier_schemes=["isbn", "event_id"],
        asserts_relationship="exhibits_work",
        work_types=["exhibition", "event"],
        fallback_work_type="exhibition",
        match_tier="tier2_event_triad",
        constituents="titles",
    ),
    # Conference claims the remaining events, which is what the old chain did
    # after the exhibition test failed.
    FunnelSpec(
        id="funnel_j_conference",
        when=[{"work_type": ["conference"]}, {"work_type": ["exhibition", "event"]}],
        identifier_schemes=["isbn", "doi"],
        work_types=["event", "text"],
        match_tier="tier2_meeting_triad",
        confidence=0.88,
        constituents="titles",
    ),
    FunnelSpec(
        id="funnel_mm_critical_edition",
        when={"work_type": ["critical_edition"]},
        identifier_schemes=["work_uri", "isbn"],
        asserts_relationship="critical_edition_of",
        work_types=["critical_edition", "text"],
        fallback_work_type="critical_edition",
        match_tier="tier2_critical_scholarship",
        confidence=0.92,
    ),
    FunnelSpec(
        id="funnel_oo_numismatic",
        when={"work_type": ["numismatic"]},
        identifier_schemes=["michel", "scott", "km"],
        work_types=["numismatic"],
        fallback_work_type="numismatic",
    ),
    FunnelSpec(
        id="funnel_pp_heritage_3d",
        # Also on the key, which the loader emits from 347 digital file formats.
        # `determine_work_type` never returns "heritage_model", so the work_type
        # alternative alone made this funnel unreachable from MARC.
        when=[{"work_type": ["heritage_model"]}, {"has_key": ["heritage_model"]}],
        identifier_schemes=["asset_hash", "model_uri"],
        asserts_relationship="digital_twin_of",
        work_types=["heritage_model"],
        fallback_work_type="heritage_model",
    ),
    FunnelSpec(
        id="funnel_q_realia",
        when={"work_type": ["realia"]},
        identifier_schemes=["ean", "barcode", "isbn"],
        work_types=["realia"],
        fallback_work_type="realia",
        confidence=0.88,
    ),
    FunnelSpec(
        id="funnel_r_archival",
        when={"work_type": ["archive_fonds", "manuscript"]},
        identifier_schemes=["signum", "call_number", "handle"],
        asserts_relationship="draft_of",
        work_types=["archive_fonds", "manuscript", "text"],
        fallback_work_type="archive_fonds",
    ),
    FunnelSpec(
        id="funnel_s_still_image",
        when={"work_type": ["still_image"]},
        identifier_schemes=["accession_number", "photo_id"],
        work_types=["still_image"],
        fallback_work_type="still_image",
        content_type="still_image",
        align_expression=True,
    ),
    FunnelSpec(
        id="funnel_i_series",
        when={"has_key": ["series"]},
        identifier_schemes=["issn", "series_number"],
        asserts_relationship="series_member_of",
        work_types=["serial", "text"],
        match_tier="tier2_series_anchor",
        confidence=0.88,
    ),
    # --- fall-through ------------------------------------------------------
    FunnelSpec(
        id="funnel_a_monograph",
        identifier_schemes=["isbn", "urn", "control_number"],
        work_types=["text"],
        fallback_work_type="text",
        align_expression=True,
        default=True,
    ),
]


BUILTIN_SPECS: list[FunnelSpec] = build_builtin_specs()
