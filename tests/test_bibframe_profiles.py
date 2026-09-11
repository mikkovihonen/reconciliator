"""Tests for BIBFRAME and BFFI declarative schema profile loading, expansion, and detection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reconciliator.loaders.bibframe.profiles import (
    BIBFRAMESchemaProfile,
    detect_profile,
    get_profile,
)


def test_builtin_profiles_exist_and_validate() -> None:
    for name in ("bffi_1", "bibframe_3", "bibframe_2"):
        profile = get_profile(name)
        assert isinstance(profile, BIBFRAMESchemaProfile)
        assert profile.name == name
        assert len(profile.namespaces) > 0
        assert len(profile.work_classes) > 0
        assert len(profile.instance_classes) > 0


def test_profile_aliases() -> None:
    p_bffi = get_profile("bffi")
    assert p_bffi.name == "bffi_1"

    p_bf3 = get_profile("bf3")
    assert p_bf3.name == "bibframe_3"

    p_bf2 = get_profile("bf2")
    assert p_bf2.name == "bibframe_2"

    p_default = get_profile()
    assert p_default.name == "bibframe_3"


def test_custom_profile_yaml_loading(tmp_path: Path) -> None:
    custom_yaml = {
        "name": "custom_profile",
        "description": "Test profile for custom library",
        "version": "0.1.0",
        "namespaces": {
            "custom": "http://example.org/custom#",
            "bf": "http://id.loc.gov/ontologies/bibframe/",
        },
        "work_classes": ["custom:Work", "bf:Work"],
        "instance_classes": ["custom:Instance"],
        "instance_to_work_predicates": ["custom:hasWork"],
        "title_predicates": ["custom:title"],
        "main_title_predicates": ["custom:name"],
        "contribution_predicates": ["custom:creator"],
        "agent_predicates": ["custom:agent"],
        "role_predicates": ["custom:role"],
        "identifier_predicates": ["custom:id"],
        "subject_predicates": ["custom:topic"],
        "provision_activity_predicates": ["custom:published"],
        "language_predicates": ["custom:lang"],
    }
    profile_path = tmp_path / "custom.yaml"
    profile_path.write_text(yaml.safe_dump(custom_yaml), encoding="utf-8")

    loaded = get_profile(str(profile_path))
    assert loaded.name == "custom_profile"
    assert "http://example.org/custom#Work" in loaded.expanded_work_classes
    assert loaded.expand_curie("custom:Work") == "http://example.org/custom#Work"
    assert loaded.compact_uri("http://example.org/custom#Work") == "custom:Work"


def test_curie_expansion_and_compaction() -> None:
    profile = get_profile("bffi_1")
    assert profile.expand_curie("bffi:Work") == "http://urn.fi/URN:NBN:fi:schema:bffi:Work"
    assert profile.expand_curie("bf:Work") == "http://id.loc.gov/ontologies/bibframe/Work"
    assert (
        profile.compact_uri("http://urn.fi/URN:NBN:fi:schema:bffi:Instance")
        == "bffi:Instance"
    )
    # Unknown URI remains unchanged
    assert profile.compact_uri("http://unknown.org/foo") == "http://unknown.org/foo"


def test_detect_profile() -> None:
    # 1. Detection via BFFI namespace
    p_bffi = detect_profile(namespaces={"bffi": "http://urn.fi/URN:NBN:fi:schema:bffi/"})
    assert p_bffi.name == "bffi_1"

    # 2. Detection via BFFI class/predicate
    p_bffi_pred = detect_profile(classes_or_predicates={"bffi:Expression"})
    assert p_bffi_pred.name == "bffi_1"

    # 3. Detection via BIBFRAME 3.0 PMO predicate
    p_bf3 = detect_profile(classes_or_predicates={"bf:mediumOfPerformance"})
    assert p_bf3.name == "bibframe_3"

    # 4. Default fallback
    p_def = detect_profile(namespaces={"bf": "http://id.loc.gov/ontologies/bibframe/"})
    assert p_def.name == "bibframe_3"


def test_unknown_profile_raises() -> None:
    with pytest.raises(ValueError, match="Unknown BIBFRAME schema profile"):
        get_profile("non_existent_profile_xyz")

