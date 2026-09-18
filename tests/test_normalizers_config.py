"""Tests for generalized normalizer configuration and modular source loading."""

from pathlib import Path

from reconciliator.config import (
    RecordNormalizationConfig,
    VocabNormalizationConfig,
    load_config,
)
from reconciliator.normalizers.agent import normalize_agent_name, parse_245c_agents
from reconciliator.normalizers.identifiers import (
    normalize_source_id,
    normalize_urn,
)
from reconciliator.normalizers.linguistic import fold_diacritics, fold_label
from reconciliator.normalizers.record import (
    RecordNormalizer,
    clean_publisher,
    clean_title,
    normalize_volume,
)
from reconciliator.vocab_folding import VocabFolder, format_target_vocab


def test_linguistic_preserved_diacritics_customization() -> None:
    # Default preserves åäö, folds others
    assert fold_diacritics("Hämeenlinna") == "Hämeenlinna"
    assert fold_diacritics("Müller") == "Muller"
    assert fold_diacritics("Café") == "Cafe"

    # Custom preserved diacritics: preserve ü, fold ä
    assert fold_diacritics("Müller", preserved_diacritics="üÜ") == "Müller"
    assert fold_diacritics("Hämeenlinna", preserved_diacritics="üÜ") == "Hameenlinna"

    # fold_label with custom diacritics
    assert fold_label("Müller (1900-1975)", preserved_diacritics="üÜ") == "müller"


def test_control_number_and_urn_normalization() -> None:
    # Generic source ID with prefix and zero-padding
    assert normalize_source_id("12345", prefix="(FI-TEST)", digits=8) == "(FI-TEST)00012345"
    # Generic OAI source ID with prefix and zero-padding
    assert (
        normalize_source_id("000123456", prefix="(FI-MELINDA)", digits=9) == "(FI-MELINDA)000123456"
    )
    assert (
        normalize_source_id("(FI-MELINDA) 987", prefix="(FI-MELINDA)", digits=9)
        == "(FI-MELINDA)000000987"
    )
    assert (
        normalize_source_id("LIBRIS: 54321", prefix="(SE-LIBRIS)", digits=8)
        == "(SE-LIBRIS)00054321"
    )

    # Source ID with regex pattern
    assert normalize_source_id("SYS:998877", pattern=r"SYS:(\d+)") == "998877"

    # URN default
    assert normalize_urn("http://urn.fi/URN:NBN:fi-fe20200101") == "urn:nbn:fi-fe20200101"
    # Custom URN pattern
    assert (
        normalize_urn("ark:/12148/bpt6k12345", pattern=r"(ark:/[^\s]+)") == "ark:/12148/bpt6k12345"
    )


def test_agent_245c_and_fictional_config() -> None:
    # Custom responsibility prefixes and conjunctions
    responsibility = "geschrieben von Johann Wolfgang Goethe und Friedrich Schiller"
    agents = parse_245c_agents(
        responsibility,
        prefixes=["geschrieben von"],
        conjunctions=["und"],
    )
    assert len(agents) == 2
    assert agents[0] == "Goethe, Johann Wolfgang"
    assert agents[1] == "Schiller, Friedrich"

    # Custom fictional qualifiers
    name = "Bond, James (geheimer Agent)"
    norm_name, surname = normalize_agent_name(name)
    assert norm_name == "Bond, James (geheimer Agent)"
    assert surname == "Bond"


def test_record_normalizer_custom_config() -> None:
    cfg = RecordNormalizationConfig(
        volume_prefixes=["volumen", "tomo"],
        volume_format_template="vol. {num}",
        publisher_suffixes=["s.a.", "s.l."],
        control_number_prefix="(ES-BNE)",
        control_number_padding_digits=10,
        classification_priority=["dewey", "ykl"],
        default_classification_scheme="dewey",
        custom_relator_terms={"aut": "kirjoittaja-erikois"},
    )
    normalizer = RecordNormalizer(config=cfg)

    raw_data = {
        "source_id": "123456",
        "title": "Don Quijote : Tomo II",
        "publisher": "Editorial Cervantes S.A.",
        "classifications": [
            {"code": "840", "scheme": "ykl"},
            {"code": "863", "scheme": "dewey"},
        ],
        "agents": [
            {"name": "Cervantes, Miguel de", "role": "kirjoittaja-erikois"},
        ],
    }

    rec = normalizer.normalize(raw_data)
    assert rec.source_id == "(ES-BNE)0000123456"
    assert rec.volume_designation == "vol. 2"
    assert rec.publisher == "Editorial Cervantes"
    # Dewey should be flagged primary due to custom priority
    prim_class = [c for c in rec.classifications if c.is_primary]
    assert len(prim_class) == 1
    assert prim_class[0].scheme == "dewey"
    assert prim_class[0].code == "863"
    # Custom relator term mapped to code
    assert normalizer.relator_registry.normalize_role("kirjoittaja-erikois") == "aut"


def test_clean_title_and_volume_options() -> None:
    # Custom non-filing articles
    custom_articles = {"de": ["der ", "die ", "das "]}
    assert (
        clean_title("Die Leiden des jungen Werthers", articles=custom_articles, lang="de")
        == "Leiden des jungen Werthers"
    )

    # Custom publisher suffixes
    assert (
        clean_publisher("Suomalaisen Kirjallisuuden Seura SKS", suffixes=["SKS", "r.y."])
        == "Suomalaisen Kirjallisuuden Seura"
    )

    # Custom volume template
    assert normalize_volume("Band 3", prefixes=["band"], format_template="Bd. {num}") == "Bd. 3"


def test_vocab_normalization_config_and_folding() -> None:
    cfg = VocabNormalizationConfig(
        legacy_to_modern={"legacy_thes": "modern_ont"},
        thesaurus_languages={"legacy_thes": "sv"},
        default_field_vocabularies={"650": "custom_yso"},
    )
    # Format target vocab with custom config
    formatted = format_target_vocab("modern_ont", source_vocab="legacy_thes", config=cfg)
    assert formatted == "modern_ont"  # Not a subcode vocab

    folder = VocabFolder(config=cfg)
    t_field, t_vocab, t_uri, t_label, m_type, qid = folder.fold_subject_heading(
        heading="Testitermi",
        source_vocab="legacy_thes",
        source_field="650",
    )
    assert t_vocab == "modern_ont"


def test_modular_config_loading_and_resolution(tmp_path: Path) -> None:
    # Create an external source file
    sources_dir = tmp_path / "sources" / "oai"
    sources_dir.mkdir(parents=True)
    oai_file = sources_dir / "custom_oai.yaml"
    oai_file.write_text(
        """
enabled: true
source_type: file
input_path: data/test.xml
normalization:
  control_number_prefix: "(EXT-SOURCE)"
  control_number_padding_digits: 6
""",
        encoding="utf-8",
    )

    # Root config referencing the external source file
    root_cfg_file = tmp_path / "config.yaml"
    root_cfg_file.write_text(
        f"""
storage:
  data_dir: "data"
ingest_oai:
  my_source: "{oai_file.as_posix()}"
normalization:
  record:
    default_language: "fi"
""",
        encoding="utf-8",
    )

    loaded = load_config(root_cfg_file)
    assert "my_source" in loaded.ingest_oai
    src_cfg = loaded.ingest_oai["my_source"]
    assert src_cfg.source_type == "file"
    assert src_cfg.normalization is not None
    assert src_cfg.normalization.control_number_prefix == "(EXT-SOURCE)"
    assert src_cfg.normalization.control_number_padding_digits == 6
    # Relative data_dir resolved against tmp_path
    assert loaded.storage.data_dir == (tmp_path / "data").resolve()


def test_custom_roman_numerals_and_relator_mappings() -> None:
    # Custom roman numerals
    custom_roman = {"xiii": 13, "xiv": 14, "xv": 15}
    vol = normalize_volume("osa XIV", roman_numerals=custom_roman)
    assert vol == "osa 14"

    # Custom relator mappings in RecordNormalizationConfig
    cfg = RecordNormalizationConfig(
        roman_numerals={"xx": 20},
        relator_mappings={"redaktör-ledare": "edt", "huvudförfattare": "aut"},
    )
    normalizer = RecordNormalizer(config=cfg)

    raw_data = {
        "source_id": "999",
        "title": "Samlade Skrifter : Del XX",
        "agents": [
            {"name": "Lindgren, Astrid", "role": "huvudförfattare"},
            {"name": "Andersson, Sven", "role": "redaktör-ledare"},
        ],
    }
    rec = normalizer.normalize(raw_data)
    assert rec.volume_designation == "osa 20"
    assert normalizer.relator_registry.normalize_role("huvudförfattare") == "aut"
    assert normalizer.relator_registry.normalize_role("redaktör-ledare") == "edt"


def test_clean_publisher_default_suffixes() -> None:
    # Verify default publisher suffixes correctly clean standard entities without regex hardcoding
    assert clean_publisher("Otava Oy") == "Otava"
    assert clean_publisher("Schildts Förlag Ab") == "Schildts"
    assert clean_publisher("Springer Publishing Ltd.") == "Springer"


def test_biographical_date_qualifiers_customization() -> None:
    from reconciliator.normalizers.linguistic import (
        build_biographical_date_pattern,
        strip_biographical_dates,
    )

    # Custom prefix "geb." (German "geboren") and custom era "v.Chr."
    custom_pattern = build_biographical_date_pattern(
        prefixes=["geb.", "gest."],
        era_bc=["v.chr."],
    )
    # Verify standard pattern doesn't know "geb." by default
    default_stripped = strip_biographical_dates("Goethe, Johann Wolfgang, geb. 1749")
    assert "geb." in default_stripped

    # Custom pattern strips it cleanly
    custom_stripped = strip_biographical_dates(
        "Goethe, Johann Wolfgang, geb. 1749", date_pattern=custom_pattern
    )
    assert custom_stripped == "Goethe, Johann Wolfgang"

    # Custom era BC
    caesar_stripped = strip_biographical_dates(
        "Caesar, Gaius Julius, 100 v.Chr. - 44 v.Chr.", date_pattern=custom_pattern
    )
    assert caesar_stripped == "Caesar, Gaius Julius"

    # RecordNormalizer configured with custom biographical qualifier parts
    cfg = RecordNormalizationConfig(
        biographical_date_prefixes=["geb.", "gest."],
        biographical_era_bc=["v.chr."],
    )
    normalizer = RecordNormalizer(config=cfg)
    raw_data = {
        "source_id": "test-1",
        "title": "Faust",
        "agents": [
            {"name": "Goethe, Johann Wolfgang von, geb. 1749, gest. 1832", "role": "aut"},
        ],
    }
    rec = normalizer.normalize(raw_data)
    assert rec.agents[0].name == "Goethe, Johann Wolfgang von"
    assert rec.agents[0].surname == "Goethe"


def test_load_config_with_external_and_omitted_normalization(tmp_path: Path) -> None:
    # 1. Test load_config on the canonical config.example.yaml with external global normalization
    root_example = Path(__file__).parent.parent / "config.example.yaml"
    cfg = load_config(root_example)
    assert cfg.normalization is not None
    assert cfg.normalization.record.default_language == "fi"
    assert cfg.normalization.record.classification_priority == ["ykl", "udk", "dewey", "lc"]
    assert cfg.normalization.vocab.default_field_vocabularies["650"] == "yso"

    # 2. Test config with normalization completely omitted -> defaults apply
    cfg_file = tmp_path / "test_no_norm.yaml"
    cfg_file.write_text(
        """
storage:
  data_dir: "data"
""",
        encoding="utf-8",
    )
    loaded = load_config(cfg_file)
    assert loaded.normalization is not None
    assert loaded.normalization.record.default_language == "fi"
    assert "kirjoittaja" in loaded.normalization.record.relator_mappings
