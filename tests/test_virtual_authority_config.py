"""Unit tests for Virtual Authority File configuration hierarchy and defaults."""

from __future__ import annotations

from reconciliator.config import (
    AppConfig,
    VirtualAuthorityConfig,
    get_bundled_virtual_authority_defaults,
)


def test_virtual_authority_bundled_defaults():
    """Verify that defaults.yaml exposes complete virtual_authority defaults."""
    defaults = get_bundled_virtual_authority_defaults()
    assert defaults["enabled"] is True
    assert defaults["base_uri"] == "urn:reconciliator:"
    assert defaults["pid_generator"] == "uuidv7"
    assert "agent" in defaults["uri_templates"]
    assert defaults["lifecycle"]["redirect_status_code"] == 308
    assert defaults["lifecycle"]["auto_redirect_on_merge"] is True
    assert "asteri" in defaults["status_policy"]["authoritative_origins"]
    assert defaults["serialization"]["marc21"]["organization_code"] == "FI-NL"
    assert defaults["serialization"]["marc21"]["rules_code"] == "rda"
    assert defaults["openrefine"]["service_name"] == "Reconciliator Virtual Authority File"
    assert defaults["oai_pmh_server"]["records_per_page"] == 100


def test_virtual_authority_pydantic_initialization():
    """Verify that VirtualAuthorityConfig instantiates cleanly with typed sub-models."""
    vaf = VirtualAuthorityConfig()
    assert vaf.enabled is True
    assert vaf.pid_generator == "uuidv7"
    assert vaf.lifecycle.redirect_status_code == 308
    assert vaf.status_policy.established_status_name == "established"
    assert vaf.serialization.marc21.rules_code == "rda"
    assert vaf.serialization.skos.default_namespace == "http://www.w3.org/2004/02/skos/core#"
    assert vaf.openrefine.preview_width == 430
    assert vaf.oai_pmh_server.admin_email == "admin@reconciliator.local"


def test_app_config_virtual_authority_mounting():
    """Verify that AppConfig mounts virtual_authority seamlessly."""
    app_cfg = AppConfig()
    assert hasattr(app_cfg, "virtual_authority")
    assert isinstance(app_cfg.virtual_authority, VirtualAuthorityConfig)
    assert app_cfg.virtual_authority.enabled is True


def test_virtual_authority_custom_overrides():
    """Verify that custom configuration dictionaries properly override defaults."""
    custom_dict = {
        "enabled": True,
        "base_uri": "https://data.example.org/authority/",
        "pid_generator": "monotonic_int",
        "lifecycle": {
            "auto_redirect_on_merge": False,
            "redirect_status_code": 301,
        },
        "status_policy": {
            "authoritative_origins": ["custom_authority"],
            "established_status_name": "verified",
        },
        "serialization": {
            "marc21": {
                "organization_code": "SE-LIBRIS",
                "rules_code": "koha",
            }
        },
        "openrefine": {
            "service_name": "Custom OpenRefine Service",
            "preview_width": 500,
        },
    }
    vaf = VirtualAuthorityConfig(**custom_dict)
    assert vaf.base_uri == "https://data.example.org/authority/"
    assert vaf.pid_generator == "monotonic_int"
    assert vaf.lifecycle.auto_redirect_on_merge is False
    assert vaf.lifecycle.redirect_status_code == 301
    assert vaf.status_policy.authoritative_origins == ["custom_authority"]
    assert vaf.status_policy.established_status_name == "verified"
    assert vaf.serialization.marc21.organization_code == "SE-LIBRIS"
    assert vaf.openrefine.service_name == "Custom OpenRefine Service"
    assert vaf.openrefine.preview_width == 500
