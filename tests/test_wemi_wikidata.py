"""Unit tests for two-pass Wikidata WEMI decomposition and streaming ingestion."""

import gzip
from pathlib import Path

import orjson

from reconciliator.config import WikidataIngestConfig
from reconciliator.ingestion.wikidata import (
    _prescan_authors,
    decompose_wikidata_entity,
    ingest_wikidata_wemi_pipeline,
)
from reconciliator.database import WEMIRepository


def test_wikidata_author_prescan(tmp_path: Path):
    """Verify Pass 1 prescan populates wikidata_authors and agents tables in WEMIRepository."""
    author_entity = {
        "id": "Q43210",
        "labels": {"fi": {"value": "Mika Waltari"}, "en": {"value": "Mika Waltari"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P3595": [{"mainsnak": {"datavalue": {"value": "000039234"}}}],
            "P213": [{"mainsnak": {"datavalue": {"value": "0000 0001 2147 888X"}}}],
        },
    }

    dump_path = tmp_path / "authors_test.json.gz"
    with gzip.open(dump_path, "wb") as f:
        line = orjson.dumps(author_entity)
        f.write(line + b"\n")

    auth_db = tmp_path / "auth.db"
    w_repo = WEMIRepository(db_path=auth_db)
    persisted_count = _prescan_authors(dump_path, w_repo, tracker=None)
    assert persisted_count == 1

    # Verify wikidata_authors table
    author = w_repo.get_wikidata_author("Q43210")
    assert author is not None
    assert author["name"] == "Mika Waltari"
    assert author["identifiers"]["kanto"] == "000039234"
    assert author["identifiers"]["isni"] == "0000 0001 2147 888X"

    # Verify agents table
    agent = w_repo.get_agent_by_curie("http://www.wikidata.org/entity/Q43210")
    assert agent is not None
    assert agent.name == "Mika Waltari"
    assert agent.source == "wikidata"
    assert agent.source_id == "Q43210"
    assert agent.curie == "wikidata:Q43210"

    # Verify Option 3: relational identifiers table populated for agent
    matches = w_repo.lookup_by_identifier("kanto", "000039234")
    assert len(matches) == 1
    assert matches[0][0] == "agent"
    assert matches[0][1] == agent.id

    isni_matches = w_repo.lookup_by_identifier("isni", "0000 0001 2147 888X")
    assert len(isni_matches) == 1
    assert isni_matches[0][0] == "agent"
    assert isni_matches[0][1] == agent.id


def test_decompose_wikidata_creative_work(tmp_path: Path):
    """Verify decomposition of a primary creative work entity."""
    auth_db = tmp_path / "auth.db"
    w_repo = WEMIRepository(db_path=auth_db)

    # Seed author in repository
    w_repo.insert_wikidata_authors(
        [
            {
                "qid": "Q43210",
                "name": "Mika Waltari",
                "identifiers": {"kanto": "000039234"},
            }
        ]
    )

    work_entity = {
        "id": "Q12345",
        "labels": {"fi": {"value": "Sinuhe egyptiläinen"}, "en": {"value": "The Egyptian"}},
        "claims": {
            "P31": [
                {"mainsnak": {"datavalue": {"value": {"id": "Q7725634"}}}}  # literary work
            ],
            "P50": [  # Author
                {"mainsnak": {"datavalue": {"value": {"id": "Q43210"}}}}
            ],
            "P407": [  # Language of work: Finnish (Q1412)
                {"mainsnak": {"datavalue": {"value": {"id": "Q1412"}}}}
            ],
            "P577": [  # Publication date: 1945
                {"mainsnak": {"datavalue": {"value": {"time": "+1945-01-01T00:00:00Z"}}}}
            ],
            "P212": [  # ISBN-13
                {"mainsnak": {"datavalue": {"value": "978-951-0-12345-6"}}}
            ],
            "P144": [  # Based on / adaptation of
                {"mainsnak": {"datavalue": {"value": {"id": "Q99999"}}}}
            ],
        },
    }

    decomp = decompose_wikidata_entity(work_entity, store=w_repo)
    assert decomp is not None
    (
        work,
        expr,
        manif,
        items,
        agents,
        relationships,
        identifiers,
        associations,
        contributions,
    ) = decomp

    assert work.title == "Sinuhe egyptiläinen"
    assert work.work_type == "text"
    assert work.original_language == "fin"
    assert work.source == "wikidata"
    assert work.source_id == "Q12345"
    assert work.curie == "wikidata:Q12345"

    assert expr.language == "fin"
    assert expr.content_type == "text"

    assert manif.publication_year == 1945
    assert len(associations) == 1

    assert len(agents) == 1
    assert agents[0].name == "Mika Waltari"

    assert len(contributions) == 1
    assert contributions[0].role == "aut"
    assert contributions[0].is_primary is True

    # Identifiers
    isbns = [i for i in identifiers if i.scheme == "isbn"]
    assert len(isbns) == 1
    assert isbns[0].value == "978-951-0-12345-6"

    # Relationships
    assert len(relationships) == 1
    assert relationships[0].relationship_type == "adaptation_of"


def test_decompose_wikidata_edition_with_parent_work(tmp_path: Path):
    """Verify decomposition of an edition entity with P629 pointing to parent work."""
    auth_db = tmp_path / "auth.db"
    w_repo = WEMIRepository(db_path=auth_db)

    edition_entity = {
        "id": "Q55555",
        "labels": {"fi": {"value": "Sinuhe egyptiläinen (Taskukirja)"}},
        "claims": {
            "P31": [
                {"mainsnak": {"datavalue": {"value": {"id": "Q3331189"}}}}  # edition
            ],
            "P629": [  # Edition or translation of Work Q12345
                {"mainsnak": {"datavalue": {"value": {"id": "Q12345"}}}}
            ],
            "P407": [{"mainsnak": {"datavalue": {"value": {"id": "Q1412"}}}}],
        },
    }

    decomp = decompose_wikidata_entity(edition_entity, store=w_repo)
    assert decomp is not None
    work = decomp[0]
    manif = decomp[2]

    # Work QID correctly reflects parent work Q12345
    assert work.source == "wikidata"
    assert work.source_id == "Q12345"
    assert work.curie == "wikidata:Q12345"
    assert manif.source_id == "Q55555"


def test_ingest_wikidata_wemi_pipeline_e2e(tmp_path: Path):
    """Verify end-to-end two-pass Wikidata streaming ingestion into WEMIRepository."""
    author_entity = {
        "id": "Q700",
        "labels": {"fi": {"value": "Aleksis Kivi"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
            "P3595": [{"mainsnak": {"datavalue": {"value": "000012345"}}}],
        },
    }
    work_entity = {
        "id": "Q701",
        "labels": {"fi": {"value": "Seitsemän veljestä"}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q7725634"}}}}],
            "P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q700"}}}}],
            "P407": [{"mainsnak": {"datavalue": {"value": {"id": "Q1412"}}}}],
            "P577": [{"mainsnak": {"datavalue": {"value": {"time": "+1870-01-01T00:00:00Z"}}}}],
            "P212": [{"mainsnak": {"datavalue": {"value": "978-951-0-77777-7"}}}],
        },
    }

    dump_path = tmp_path / "wikidata_test.json.gz"
    with gzip.open(dump_path, "wb") as f:
        f.write(orjson.dumps(author_entity) + b"\n")
        f.write(orjson.dumps(work_entity) + b"\n")

    auth_db = tmp_path / "authority.db"
    w_repo = WEMIRepository(db_path=auth_db)

    config = WikidataIngestConfig(
        dump_path=str(dump_path),
        batch_size=1,
    )

    total = ingest_wikidata_wemi_pipeline(config, w_repo)
    assert total == 1

    # Verify author in database
    author = w_repo.get_wikidata_author("Q700")
    assert author is not None
    assert author["name"] == "Aleksis Kivi"

    # Verify work in database
    work = w_repo.get_work_by_source("wikidata", "Q701")
    assert work is not None
    assert work.title == "Seitsemän veljestä"
    assert work.work_type == "text"
    assert work.original_language == "fin"

    # Verify manifestation in database
    manif = w_repo.get_manifestation_by_source("wikidata", "Q701")
    assert manif is not None
    assert manif.publication_year == 1870

    # Verify identifier was indexed
    matches = w_repo.lookup_by_identifier("isbn", "978-951-0-77777-7")
    assert len(matches) == 1
    assert matches[0][0] == "manifestation"
    assert matches[0][1] == manif.id
