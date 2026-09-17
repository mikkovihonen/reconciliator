"""Stage 8 of plans/aggregates.md: the consumers read the stored rows.

Funnel G resolved an anthology's constituents with one
`find_works_by_author_and_title` per title, hardcoded to `work_type="text"` — so
an album's tracks could never resolve however they were catalogued, and an
anthology's stories cost a query each. The recorded-sound tier reported the
*target's* track titles, which says what the target claimed rather than what it
matched.

The blocking query itself was the expensive part: `title_normalized LIKE
'%stem%'` is `SCAN w`, measured at 6.4 ms per call over 60,000 works, and it is
called at least once per target record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reconciliator.models import (
    AgentEntity,
    ExpressionEntity,
    ManifestationEntity,
    ManifestationExpressionAssociation,
    WorkEntity,
    WorkRelationship,
)
from reconciliator.repository import WEMIRepository

SRC = "fono"


@pytest.fixture
def store(tmp_path: Path) -> WEMIRepository:
    repo = WEMIRepository(db_path=tmp_path / "authority.db")
    repo.register_source(SRC, is_authoritative=True)
    return repo


def _album_with_tracks(repo: WEMIRepository, tracks: list[tuple[str, int]]) -> WorkEntity:
    """A container work with `tracks` constituents, junction rows and edges."""
    container = WorkEntity(
        work_uri=f"urn:work:{SRC}:ALB",
        source=SRC,
        source_id="ALB",
        record_origin=SRC,
        title="Kokoelma",
        title_normalized="kokoelma",
        work_type="music",
    )
    container_manifestation = ManifestationEntity(
        manifestation_uri=f"urn:manif:{SRC}:ALB",
        source=SRC,
        source_id="ALB",
        record_origin=SRC,
        title="Kokoelma",
        title_normalized="kokoelma",
        work_uri=container.work_uri,
        carrier_type="audio_disc",
    )
    works = [container]
    expressions: list[ExpressionEntity] = []
    associations: list[ManifestationExpressionAssociation] = []
    relationships: list[WorkRelationship] = []

    for title, track_number in tracks:
        work_uri = f"urn:work:{SRC}:c:{track_number}"
        expression_uri = f"urn:exp:{SRC}:c:{track_number}:fin"
        works.append(
            WorkEntity(
                work_uri=work_uri,
                source=SRC,
                source_id=f"c:{track_number}",
                record_origin=SRC,
                title=title,
                title_normalized=title.lower(),
                work_type="music",
            )
        )
        expressions.append(
            ExpressionEntity(
                work_id=0,
                work_uri=work_uri,
                expression_uri=expression_uri,
                source=SRC,
                source_id=f"c:{track_number}",
                record_origin=SRC,
                language="fin",
                content_type="audio_music",
                title=title,
            )
        )
        associations.append(
            ManifestationExpressionAssociation(
                manifestation_id=0,
                expression_id=0,
                manifestation_uri=container_manifestation.manifestation_uri,
                expression_uri=expression_uri,
                track_number=track_number,
                is_primary=False,
            )
        )
        relationships.append(
            WorkRelationship(
                source_work_id=0,
                target_work_id=0,
                source_work_uri=container.work_uri,
                target_work_uri=work_uri,
                relationship_type="has_constituent",
            )
        )

    repo.insert_wemi_batch(
        works=works,
        expressions=expressions,
        manifestations=[container_manifestation],
        relationships=relationships,
        manifestation_expressions=associations,
        agents=[
            AgentEntity(
                agent_uri=f"urn:agent:{SRC}:ALB",
                source=SRC,
                record_origin=SRC,
                name="Rautavaara, Einojuhani",
                name_normalized="rautavaara, einojuhani",
                surname="Rautavaara",
            )
        ],
        record_origin=SRC,
    )
    return repo.get_work_by_source(SRC, "ALB")


# --- the stored reader ----------------------------------------------------


def test_a_container_returns_its_constituents_in_track_order(store: WEMIRepository):
    """One indexed lookup for a whole tracklist, in place of a title query per
    constituent - and in track order, because a tracklist is ordered."""
    container = _album_with_tracks(store, [("Kolmas", 3), ("Ensimmainen", 1), ("Toinen", 2)])
    assert [w.title for w in store.get_constituent_works(container.id)] == [
        "Ensimmainen",
        "Toinen",
        "Kolmas",
    ]


def test_a_work_with_no_constituents_returns_nothing(store: WEMIRepository):
    store.insert_works_batch(
        [
            WorkEntity(
                work_uri=f"urn:work:{SRC}:SOLO",
                source=SRC,
                source_id="SOLO",
                record_origin=SRC,
                title="Yksi",
                title_normalized="yksi",
            )
        ]
    )
    solo = store.get_work_by_source(SRC, "SOLO")
    assert store.get_constituent_works(solo.id) == []


def test_only_constituent_edges_are_followed(store: WEMIRepository):
    """Not every relationship: a series member or a translation is not something
    the work contains."""
    container = _album_with_tracks(store, [("Ensimmainen", 1)])
    other = store.get_work_by_source(SRC, "c:1")
    store.insert_work_relationships_batch(
        [
            WorkRelationship(
                source_work_id=container.id,
                target_work_id=other.id,
                relationship_type="series_member_of",
            )
        ]
    )
    assert [w.source_id for w in store.get_constituent_works(container.id)] == ["c:1"]


# --- the blocking query ---------------------------------------------------


def test_an_exact_stem_uses_the_index(store: WEMIRepository):
    """`EXPLAIN` reports an Index Scan or Index Cond on idx_works_title_norm
    for equality. Tier 2 is called at least once per target record, so the
    difference is the dominant cost of it."""
    with store.get_connection() as conn:
        plan = [
            str(row[0])
            for row in conn.exec_driver_sql(
                "EXPLAIN SELECT w.* FROM works w WHERE w.title_normalized = 'x';"
            )
        ]
    plan_text = "\n".join(plan)
    assert "idx_works_title_norm" in plan_text or "Index" in plan_text, plan_text


def test_recall_is_preserved_through_the_ladder(store: WEMIRepository):
    """Each rung is tried only when the one before it found nothing, so a title
    that only a substring match can reach is still reached."""
    store.insert_works_batch(
        [
            WorkEntity(
                work_uri=f"urn:work:{SRC}:LONG",
                source=SRC,
                source_id="LONG",
                record_origin=SRC,
                title="Jotain ennen sinuhe egyptilainen ja jotain jalkeen",
                title_normalized="jotain ennen sinuhe egyptilainen ja jotain jalkeen",
                work_type="text",
            )
        ]
    )
    found = store.find_works_by_author_and_title(None, "sinuhe egyptilainen")
    assert [w.source_id for w in found] == ["LONG"]


def test_an_exact_match_is_preferred_over_a_longer_one(store: WEMIRepository):
    """The behaviour change this ladder makes: when an exact stem exists, a work
    whose title merely contains the probe is no longer in the candidate pool. It
    has a different stem, which is what tier 2 blocks on, and tier 3 searches on
    other evidence for the cases tier 2 should not claim."""
    store.insert_works_batch(
        [
            WorkEntity(
                work_uri=f"urn:work:{SRC}:LONG",
                source=SRC,
                source_id="LONG",
                record_origin=SRC,
                title="Sinuhe egyptilainen ja jotain aivan muuta",
                title_normalized="sinuhe egyptilainen ja jotain aivan muuta",
                work_type="text",
            ),
            WorkEntity(
                work_uri=f"urn:work:{SRC}:EXACT",
                source=SRC,
                source_id="EXACT",
                record_origin=SRC,
                title="Sinuhe egyptilainen",
                title_normalized="sinuhe egyptilainen",
                work_type="text",
            ),
        ]
    )
    found = store.find_works_by_author_and_title(None, "sinuhe egyptilainen")
    assert [w.source_id for w in found] == ["EXACT"]


# --- reporting, whichever tier matched ------------------------------------


def test_constituents_are_reported_from_every_tier(store: WEMIRepository, tmp_path: Path):
    """A tier decides what matched; the funnel decides what to report about it.
    Only `default_tier2` filled these in, so an anthology matched on its ISBN -
    the strongest match there is - reported no constituents, while the same
    anthology matched by author and title reported all of them."""
    from lxml import etree

    from reconciliator.config import ReconcileConfig, RerankConfig
    from reconciliator.constituents import resolve_for_source
    from reconciliator.loaders.base import LoadContext
    from reconciliator.loaders.marcxml import build_target_record_from_marc
    from reconciliator.loaders.registry import load_record
    from reconciliator.matcher import WorkMatcher
    from reconciliator.template import WEMISerializationEngine

    anthology = """<record xmlns="http://www.loc.gov/MARC21/slim">
      <leader>00000cam a2200000 i 4500</leader>
      <controlfield tag="001">ANTH-1</controlfield>
      <controlfield tag="008">200101s2020    fi |||||||||||||||fin d</controlfield>
      <datafield tag="020" ind1=" " ind2=" ">
        <subfield code="a">9789510012345</subfield></datafield>
      <datafield tag="245" ind1="0" ind2="0">
        <subfield code="a">Suomalaisia novelleja</subfield></datafield>
      <datafield tag="655" ind1=" " ind2="7"><subfield code="a">antologiat</subfield>
        <subfield code="2">slm/fin</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Waltari, Mika</subfield>
        <subfield code="t">Kuun maisema</subfield></datafield>
      <datafield tag="700" ind1="1" ind2="2"><subfield code="a">Canth, Minna</subfield>
        <subfield code="t">Agnes</subfield></datafield>
    </record>"""

    element = etree.fromstring(anthology.encode())
    record = load_record(
        element,
        LoadContext(
            source=SRC,
            record_origin=SRC,
            serialization_engine=WEMISerializationEngine(),
            constituents=resolve_for_source(SRC, "marc_enhanced", "entities"),
        ),
    )
    store.insert_wemi_batch(
        works=record.works,
        expressions=record.expressions,
        manifestations=record.manifestations,
        agents=record.agents,
        relationships=record.relationships,
        identifiers=record.identifiers,
        manifestation_expressions=record.associations,
        contributions=record.contributions,
        entity_keys=record.entity_keys,
        record_origin=SRC,
    )

    matcher = WorkMatcher(
        store,
        config=ReconcileConfig(target_file=tmp_path / "x.xml", rerank=RerankConfig()),
    )
    target = build_target_record_from_marc(element, source="target")
    result = matcher.match_target(target)

    # Tier 1, on the ISBN.
    assert result.match_tier == "tier1_exact_identifier"
    assert {(c.title, c.relationship_type) for c in result.constituent_matches} == {
        ("Kuun maisema", "constituent_of"),
        ("Agnes", "constituent_of"),
    }
    # And each one resolved to the stored constituent work, not to a title alone.
    assert all(c.work_uri for c in result.constituent_matches)
