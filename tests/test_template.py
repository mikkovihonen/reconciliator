"""Unit tests for RecordTemplateEngine and custom bibliographic Jinja2 filters."""

from pathlib import Path

import pytest

from reconciliator.embedding.template import (
    RecordTemplateEngine,
    collapse_ws,
    join_classifications,
    join_labels,
    join_names,
    join_surnames,
    primary_code,
)
from reconciliator.models import (
    Agent,
    BibliographicRecord,
    ClassificationItem,
    RecordIdentifiers,
    SubjectItem,
    SubjectsContainer,
)


@pytest.fixture
def sample_record() -> BibliographicRecord:
    return BibliographicRecord(
        source_id="REC001",
        source="melinda",
        title="Sinuhe egyptiläinen",
        subtitle="viisitoista kirjaa lääkäri Sinuhen elämästä",
        uniform_title="Sinuhe",
        publication_year=1945,
        publisher="WSOY",
        edition=1,
        volume_designation="osa 1",
        classifications=[
            ClassificationItem(code="84.2", scheme="ykl", is_primary=True),
            ClassificationItem(code="82-3", scheme="udc", is_primary=False),
        ],
        identifiers=RecordIdentifiers(isbn=["9789510000010"]),
        agents=[
            Agent(name="Waltari, Mika", surname="Waltari", role="aut"),
            Agent(name="Virtanen, Jorma", surname="Virtanen", role="edt"),
        ],
        subjects=SubjectsContainer(
            topical=[
                SubjectItem(
                    uri="http://www.yso.fi/onto/yso/p2591", pref_label="lääkärit", vocabulary="yso"
                ),
            ],
            genre_form=[
                SubjectItem(
                    uri="http://urn.fi/URN:NBN:fi:au:slm:s123",
                    pref_label="historialliset romaanit",
                    vocabulary="slm",
                ),
            ],
        ),
    )


def test_default_template_rendering(sample_record: BibliographicRecord) -> None:
    engine = RecordTemplateEngine()
    rendered = engine.render(sample_record)

    assert "Nimeke: Sinuhe egyptiläinen : viisitoista kirjaa lääkäri Sinuhen elämästä" in rendered
    assert "[Yhtenäistetty: Sinuhe]" in rendered
    assert "Tekijät: Waltari, Mika, Virtanen, Jorma" in rendered
    assert "Julkaisija: WSOY" in rendered
    assert "Vuosi: 1945" in rendered
    assert "osa 1" in rendered
    assert "Luokitus: 84.2" in rendered
    assert "Aiheet: lääkärit" in rendered
    assert "Lajityyppi: historialliset romaanit" in rendered


def test_custom_template_rendering(sample_record: BibliographicRecord) -> None:
    custom_path = Path(__file__).parent / "fixtures" / "sample_custom_template.j2"
    engine = RecordTemplateEngine(custom_path)
    rendered = engine.render(sample_record)

    assert "Title: Sinuhe egyptiläinen - viisitoista kirjaa lääkäri Sinuhen elämästä" in rendered
    assert "Authors: Waltari, Mika, Virtanen, Jorma" in rendered
    assert "Surnames: Waltari, Virtanen" in rendered
    assert "Main Classification: 84.2" in rendered
    assert "All Classifications: 84.2, 82-3" in rendered
    assert "YKL: 84.2" in rendered
    assert "Topics: lääkärit" in rendered


def test_missing_template_file_raises_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        RecordTemplateEngine(Path("non_existent_template.j2"))


def test_template_hashing() -> None:
    engine = RecordTemplateEngine()
    h = engine.compute_template_hash()
    assert isinstance(h, str)
    assert len(h) == 64


def test_custom_filters() -> None:
    agents = [
        Agent(name="Waltari, Mika", surname="Waltari", role="aut"),
        Agent(name="Kivi, Aleksis", surname=None, role="aut"),
    ]
    assert join_names(agents) == "Waltari, Mika, Kivi, Aleksis"
    assert join_surnames(agents) == "Waltari, Kivi, Aleksis"

    subjects = [
        SubjectItem(uri="http://ex.org/1", pref_label="historia", vocabulary="yso"),
        SubjectItem(uri="http://ex.org/2", pref_label="arkeologia", vocabulary="yso"),
    ]
    assert join_labels(subjects, delimiter="; ") == "historia; arkeologia"

    classes = [
        ClassificationItem(code="84.2", scheme="ykl", is_primary=False),
        ClassificationItem(code="82-3", scheme="udc", is_primary=True),
    ]
    assert primary_code(classes) == "82-3"
    assert join_classifications(classes, scheme="ykl") == "84.2"
    assert collapse_ws("  Hello   \n\t world!  ") == "Hello world!"


def test_wemi_serialization_engine() -> None:
    from reconciliator.embedding.template import WEMISerializationEngine
    from reconciliator.models import ExpressionEntity, ManifestationEntity, WorkEntity

    engine = WEMISerializationEngine()

    # 1. Test Work rendering
    work = WorkEntity(
        work_uri="urn:work:1",
        source="melinda",
        source_id="w1",
        title="Sinuhe egyptiläinen",
        title_normalized="sinuhe egyptilainen",
        original_language="fin",
        work_type="text",
        classification="84.2",
    )
    work_str = engine.render_work(
        work,
        authors=[{"name": "Waltari, Mika"}],
        subjects=["muinainen egypti", "historia"],
    )
    assert "Nimeke: Sinuhe egyptiläinen" in work_str
    assert "Tekijät: Waltari, Mika" in work_str
    assert "Tyyppi: text" in work_str
    assert "Kieli: fin" in work_str
    assert "Aiheet: muinainen egypti, historia" in work_str
    assert "Luokitus: 84.2" in work_str

    # 2. Test Expression rendering
    exp = ExpressionEntity(
        work_id=1,
        expression_uri="urn:exp:1",
        source="melinda",
        source_id="e1",
        language="swe",
        content_type="text",
        title="Sinuhe egyptiern",
        version_designation="2. rev. uppl.",
    )
    exp_str = engine.render_expression(
        exp,
        work_title="Sinuhe egyptiläinen",
        translators=[{"name": "Enckell, Thomas"}],
    )
    assert "Teos: Sinuhe egyptiläinen" in exp_str
    assert "Ilmiasu: Sinuhe egyptiern [Kieli: swe]" in exp_str
    assert "Kääntäjä: Enckell, Thomas" in exp_str
    assert "Versio: 2. rev. uppl." in exp_str

    # 3. Test Manifestation rendering
    manif = ManifestationEntity(
        work_id=1,
        source="melinda",
        source_id="m1",
        title="Sinuhe egyptiern",
        title_normalized="sinuhe egyptiern",
        publisher="Schildts",
        publication_place="Helsingfors",
        publication_year=1946,
        edition_statement="2. uppl.",
        carrier_type="volume",
        extent="680 s.",
    )
    manif_str = engine.render_manifestation(manif)
    assert "Nimeke: Sinuhe egyptiern" in manif_str
    assert "Julkaisija: Schildts (Helsingfors)" in manif_str
    assert "Vuosi: 1946" in manif_str
    assert "Painos: 2. uppl." in manif_str
    assert "Muoto: volume" in manif_str
    assert "Laajuus: 680 s." in manif_str
