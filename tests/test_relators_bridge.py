"""Tests for dynamic LoC Relators <-> Finto MTS bridge vocabulary and relator resolution."""

from __future__ import annotations

from pathlib import Path

from reconciliator.ingestion.vocab import ingest_bridge_files
from reconciliator.loaders.bibframe.base import normalize_role_uri_or_label
from reconciliator.normalizers.relators import (
    RelatorRegistry,
    get_default_relator_registry,
    normalize_role,
)
from reconciliator.vocab import VocabStore, concept_id_to_uri, normalize_concept_id
from reconciliator.vocab.bridges import (
    get_bridged_role_map,
    get_bundled_bridge_files,
    parse_bridge_turtle,
)


def test_loc_relators_bridge_file_exists_and_parses() -> None:
    bridge_files = get_bundled_bridge_files()
    relator_files = [bf for bf in bridge_files if "relators" in bf.name]
    assert len(relator_files) == 1
    rf = relator_files[0]

    mappings = parse_bridge_turtle(rf, bridge_vocab="loc-relators")
    assert len(mappings) >= 150

    aut_mapping = next(m for m in mappings if m["notation"] == "aut")
    assert aut_mapping["source_uri"] == "http://id.loc.gov/vocabulary/relators/aut"
    assert aut_mapping["target_uri"] == "http://urn.fi/URN:NBN:fi:au:mts:m552"
    assert aut_mapping["pref_label_fi"] == "kirjoittaja"
    assert aut_mapping["pref_label_sv"] == "författare"
    assert aut_mapping["pref_label_en"] == "author agent"


def test_get_bridged_role_map_dynamic() -> None:
    role_map = get_bridged_role_map()
    assert isinstance(role_map, dict)
    assert role_map.get("m552") == "aut"
    assert role_map.get("m23") == "trl"
    assert role_map.get("m2932") == "ill"
    assert role_map.get("m695") == "cmp"
    assert role_map.get("m1205") == "arr"
    assert role_map.get("m5656") == "edt"
    assert role_map.get("m3589") == "cnd"
    assert role_map.get("m4115") == "prf"
    assert role_map.get("m3157") == "pbl"


def test_relator_registry_resolves_mts_uris() -> None:
    registry = get_default_relator_registry()
    assert registry.normalize_role("http://urn.fi/URN:NBN:fi:au:mts:m552") == "aut"
    assert registry.normalize_role("urn:nbn:fi:au:mts:m23") == "trl"
    assert registry.normalize_role("https://finto.fi/mts/fi/page/m695") == "cmp"
    assert registry.normalize_role("http://urn.fi/URN:NBN:fi:au:mts:m4115") == "prf"
    assert normalize_role("http://urn.fi/URN:NBN:fi:au:mts:m5656") == "edt"


def test_bibframe_normalize_role_uri_or_label_with_mts() -> None:
    assert normalize_role_uri_or_label("http://urn.fi/URN:NBN:fi:au:mts:m552") == "aut"
    assert normalize_role_uri_or_label("urn:nbn:fi:au:mts:m1205") == "arr"
    assert normalize_role_uri_or_label("http://urn.fi/URN:NBN:fi:au:mts:m1042") == "pht"
    assert normalize_role_uri_or_label("http://id.loc.gov/vocabulary/relators/ill") == "ill"


def test_mts_concept_id_rules() -> None:
    assert normalize_concept_id("http://urn.fi/URN:NBN:fi:au:mts:m552") == "mts:m552"
    assert normalize_concept_id("urn:nbn:fi:au:mts:m4115") == "mts:m4115"
    assert concept_id_to_uri("mts:m552") == "https://urn.fi/URN:NBN:fi:au:mts:m552"
    assert concept_id_to_uri("mts:m4115") == "https://urn.fi/URN:NBN:fi:au:mts:m4115"


def test_ingest_bridge_files_populates_relator_terms(tmp_path: Path) -> None:
    db_path = tmp_path / "vocab_bridge_test.db"
    store = VocabStore(db_path)

    ingest_bridge_files(store)

    # Verify relator_terms has been populated with codes and multilingual labels from the bridge
    assert store.is_known_relator("kirjoittaja")
    assert store.is_known_relator("författare")
    assert store.is_known_relator("author agent")

    assert store.get_relator_code("kirjoittaja") == "aut"
    assert store.get_relator_code("författare") == "aut"
    assert store.get_relator_code("author agent") == "aut"

    assert store.get_relator_code("kääntäjä") == "trl"
    assert store.get_relator_code("översättare") == "trl"
    assert store.get_relator_code("translator agent") == "trl"

    # Verify RelatorRegistry with this store resolves both terms and URIs
    registry = RelatorRegistry(vocab_store=store)
    assert registry.normalize_role("författare") == "aut"
    assert registry.normalize_role("översättare") == "trl"
    assert registry.normalize_role("http://urn.fi/URN:NBN:fi:au:mts:m552") == "aut"
