"""Stage 2 of plans/aggregates.md: constituent extraction as per-source config.

Two sources in the same format do not agree about constituents — Melinda writes
enhanced 505s, a sound-recording database keeps tracks in its own conventions,
an EN 15907 filmography has episodes, and plenty of sources have no aggregates
at all. So the fields to read belong to the source, and the code is one
profile-driven extractor rather than a chain of per-source special cases.

Nothing extracts anything yet; that is stage 3. This fixes the configuration
surface it will be written against.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reconciliator.config import AppConfig, OAIIngestConfig, load_config
from reconciliator.constituents import (
    BUILTIN_EXTRACTORS,
    ConstituentProfile,
    ConstituentProfileError,
    available_profiles,
    get_constituent_profile,
    resolve_extractor,
    resolve_for_source,
)
from reconciliator.constituents.spec import ConstituentField

# --- bundled profiles -----------------------------------------------------


def test_every_bundled_profile_loads_and_validates():
    names = available_profiles()
    assert {"none", "marc_enhanced", "bibframe_haspart"} <= set(names)
    for name in names:
        profile = get_constituent_profile(name)
        assert isinstance(profile, ConstituentProfile)
        assert profile.name == name


def test_the_default_profile_looks_for_nothing():
    """So adding constituent extraction changes nothing for a source nobody has
    described yet."""
    assert get_constituent_profile(None).is_empty
    assert get_constituent_profile("none").is_empty


@pytest.mark.parametrize("spelling", ["off", "none", "disabled", "no", "marc_none"])
def test_the_ways_of_saying_nothing_all_resolve(spelling: str):
    """A config author reaching for "this source has no constituents" should not
    have to guess which word the parser wants."""
    assert get_constituent_profile(spelling).is_empty


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("marc", "marc_enhanced"),
        ("marc21", "marc_enhanced"),
        ("MARC-Enhanced", "marc_enhanced"),
        ("bibframe", "bibframe_haspart"),
        ("bffi", "bibframe_haspart"),
    ],
)
def test_aliases_resolve_to_bundled_profiles(alias: str, expected: str):
    assert get_constituent_profile(alias).name == expected


def test_the_marc_profile_filters_on_indicators():
    """The substance of the profile, not decoration: a basic 505 (ind2 blank)
    holds one `$a` blob - "A -- B -- C" - which cannot be split into titles
    reliably. No constituents is right; three wrong works is not."""
    profile = get_constituent_profile("marc_enhanced")
    contents = profile.fields_for_tag("505")
    assert len(contents) == 1
    assert contents[0].indicators == {"ind1": "0", "ind2": "0"}
    assert contents[0].title == "t"
    assert contents[0].duration == "g"
    assert contents[0].ordinal == "position"


def test_the_marc_profile_yields_both_shapes():
    """One source can record embodied constituents in its contents note and be
    itself a component through its host-item entry."""
    profile = get_constituent_profile("marc_enhanced")
    shapes = {f.tag: profile.shape_of(f) for f in profile.fields}
    assert shapes["505"] == "embodied"
    assert shapes["700"] == "embodied"
    assert shapes["773"] == "component"
    host = profile.fields_for_tag("773")[0]
    assert host.container_key == "citation"
    assert host.component_type == "article"


def test_name_title_entries_do_not_contribute_to_the_container():
    """An anthology's story authors came out as its primary authors. A catalogue
    that means them as the volume's contributors can say true; Melinda practice,
    as far as the fixtures show, does not."""
    profile = get_constituent_profile("marc_enhanced")
    for tag in ("700", "710"):
        assert profile.fields_for_tag(tag)[0].contributes_to_container is False


def test_the_bibframe_profile_uses_predicates_not_tags():
    profile = get_constituent_profile("bibframe_haspart")
    field = profile.fields[0]
    assert field.predicate == "bf:hasPart"
    assert field.tag is None
    # Ordering is left open deliberately: BIBFRAME states sequence with
    # bflc:sequence or an RDF list, not by document order, and triple iteration
    # order is not stable.
    assert field.ordinal == "none"


# --- profiles from a file -------------------------------------------------


def _write_profile(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_source_can_bring_its_own_profile(tmp_path: Path):
    path = _write_profile(
        tmp_path / "fono.yaml",
        {
            "shape": "embodied",
            "dedup": "title_and_creator",
            "max_per_record": 40,
            "fields": [{"tag": "505", "title": "t", "duration": "g"}],
        },
    )
    profile = get_constituent_profile(str(path))
    assert profile.name == "fono"  # defaulted from the filename
    assert profile.dedup == "title_and_creator"
    assert profile.max_per_record == 40


def test_a_relative_profile_path_resolves_against_the_config_directory(tmp_path: Path):
    """Not the process working directory, so a config keeps working wherever the
    CLI is invoked from."""
    (tmp_path / "profiles").mkdir()
    _write_profile(
        tmp_path / "profiles" / "site.yaml",
        {"fields": [{"tag": "505", "title": "t"}]},
    )
    profile = get_constituent_profile("profiles/site.yaml", config_dir=tmp_path)
    assert profile.name == "site"


def test_an_unknown_profile_names_the_source_and_lists_alternatives():
    with pytest.raises(ConstituentProfileError) as err:
        get_constituent_profile("no_such_profile", subject="melinda")
    assert "melinda" in str(err.value)
    assert "marc_enhanced" in str(err.value)


def test_a_missing_profile_file_names_the_source(tmp_path: Path):
    with pytest.raises(ConstituentProfileError, match="not found"):
        get_constituent_profile("profiles/absent.yaml", config_dir=tmp_path, subject="fono")


def test_an_invalid_profile_names_the_file(tmp_path: Path):
    path = _write_profile(
        tmp_path / "broken.yaml",
        # Both a tag and a predicate: this profile describes two sources at once.
        {"fields": [{"tag": "505", "predicate": "bf:hasPart", "title": "t"}]},
    )
    with pytest.raises(ConstituentProfileError) as err:
        get_constituent_profile(str(path), subject="fono")
    assert "broken.yaml" in str(err.value)


def test_a_field_with_no_title_is_refused():
    """A constituent with no title cannot become a work."""
    with pytest.raises(ValueError, match="no `title`"):
        ConstituentField(tag="505", duration="g")


def test_a_field_with_neither_tag_nor_predicate_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        ConstituentField(title="t")


def test_an_unknown_ordinal_is_refused():
    with pytest.raises(ValueError, match="ordinal"):
        ConstituentField(tag="505", title="t", ordinal="alphabetical")


@pytest.mark.parametrize("ordinal", ["position", "none", "subfield:g", "predicate:seq"])
def test_the_supported_ordinals_are_accepted(ordinal: str):
    assert ConstituentField(tag="505", title="t", ordinal=ordinal).ordinal == ordinal


# --- extractors -----------------------------------------------------------


def test_an_extractor_path_resolves_like_a_funnel_handler(tmp_path: Path):
    """The same rule as a funnel's `handler`, deliberately: a user who has
    learned one has learned the other."""
    module_file = tmp_path / "fono_tracks.py"
    module_file.write_text("def extract(record, profile):\n    return []\n")
    profile = get_constituent_profile(
        str(
            _write_profile(
                tmp_path / "fono.yaml",
                {
                    "fields": [{"tag": "505", "title": "t"}],
                    "extractor": "fono_tracks.py",
                },
            )
        )
    )
    module = resolve_extractor(profile, subject="fono", config_dir=tmp_path)
    assert hasattr(module, "extract")


def test_an_unknown_builtin_extractor_names_the_source(tmp_path: Path):
    profile = get_constituent_profile(
        str(
            _write_profile(
                tmp_path / "p.yaml",
                {"fields": [{"tag": "505", "title": "t"}], "extractor": "nope"},
            )
        )
    )
    with pytest.raises(ConstituentProfileError) as err:
        resolve_extractor(profile, subject="fono")
    assert "fono" in str(err.value)


def test_a_profile_with_no_extractor_resolves_to_none():
    assert resolve_extractor(get_constituent_profile("marc_enhanced")) is None


def test_the_builtin_extractor_registry_is_empty_for_now():
    """Stage 2 ships the mechanism; the built-ins arrive with the sources that
    need them. An empty registry still has to produce a sensible error."""
    assert BUILTIN_EXTRACTORS == {}


# --- per-source resolution ------------------------------------------------


def test_a_source_that_says_nothing_inherits_the_global_default():
    resolved = resolve_for_source("melinda", None, None, default_profile="marc_enhanced")
    assert resolved.profile.name == "marc_enhanced"
    assert resolved.mode == "keys_only"


def test_a_source_overrides_the_global_default():
    resolved = resolve_for_source(
        "fono", "bibframe_haspart", "entities", default_profile="marc_enhanced"
    )
    assert resolved.profile.name == "bibframe_haspart"
    assert resolved.mode == "entities"
    assert resolved.writes_entities is True


def test_off_is_first_class_in_either_field():
    """`constituents: off` is the natural way to write it, and it means the mode
    rather than a profile named "off"."""
    for profile_ref, mode in (("off", None), ("marc_enhanced", "off")):
        resolved = resolve_for_source("kavi", profile_ref, mode)
        assert resolved.mode == "off"
        assert resolved.profile is None
        assert resolved.enabled is False


def test_an_empty_profile_reports_itself_as_off():
    """Whatever the mode says: there is nothing to look for, and a caller should
    not have to ask a second question."""
    resolved = resolve_for_source("plain", "none", "entities")
    assert resolved.mode == "off"
    assert resolved.writes_entities is False


def test_default_config_looks_for_nothing():
    config = AppConfig()
    assert config.constituents.profile is None
    assert config.constituents.mode == "keys_only"
    source = OAIIngestConfig(oai_url="http://example.org/oai")
    assert source.constituents is None
    assert source.constituents_mode is None
    assert resolve_for_source("s", source.constituents, source.constituents_mode).mode == "off"


# --- startup validation ---------------------------------------------------


def _config_file(tmp_path: Path, source: dict, global_block: dict | None = None) -> Path:
    data: dict = {
        "storage": {"data_dir": str(tmp_path / "data")},
        "ingest_oai": {"melinda": {"oai_url": "http://example.org/oai", **source}},
    }
    if global_block is not None:
        data["constituents"] = global_block
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_example_config_resolves(tmp_path: Path):
    config = load_config("config.example.yaml")
    assert config.config_dir == Path.cwd().resolve()
    melinda = config.ingest_oai["melinda"]
    resolved = resolve_for_source(
        "melinda",
        melinda.constituents,
        melinda.constituents_mode,
        default_profile=config.constituents.profile,
        default_mode=config.constituents.mode,
        config_dir=config.config_dir,
    )
    assert resolved.profile.name == "marc_enhanced"


def test_a_bad_profile_name_fails_when_the_config_loads(tmp_path: Path):
    """Rather than a source that quietly ingests no constituents for the rest of
    the corpus. Same reason `WorkMatcher` resolves funnel handlers in its
    constructor."""
    path = _config_file(tmp_path, {"constituents": "no_such_profile"})
    with pytest.raises(ConstituentProfileError) as err:
        load_config(path)
    assert "melinda" in str(err.value)


def test_a_missing_extractor_file_fails_when_the_config_loads(tmp_path: Path):
    _write_profile(
        tmp_path / "site.yaml",
        {"fields": [{"tag": "505", "title": "t"}], "extractor": "absent.py"},
    )
    path = _config_file(tmp_path, {"constituents": "site.yaml"})
    with pytest.raises(ConstituentProfileError, match="not found"):
        load_config(path)


def test_load_config_stamps_the_config_directory(tmp_path: Path):
    """`WorkMatcher` has read `config_dir` since funnel handlers gained paths,
    and nothing ever set it, so a relative handler path silently resolved
    against the process working directory."""
    path = _config_file(tmp_path, {})
    config = load_config(path)
    assert config.config_dir == tmp_path.resolve()
