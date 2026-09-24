"""Tests for pure BFFI aggregate and constituent extraction."""

from __future__ import annotations

import pytest
from rdflib import RDF, RDFS, Graph, Literal, Namespace, URIRef

from reconciliator.constituents import get_constituent_profile
from reconciliator.loaders.bibframe.decomposition import (
    decompose_bibframe_work,
    extract_bibframe_constituents,
)
from reconciliator.loaders.bibframe.profiles import get_profile

BFFI = Namespace("http://urn.fi/URN:NBN:fi:schema:bffi:")
BF = Namespace("http://id.loc.gov/ontologies/bibframe/")


def test_bffi_extraction_strictly_without_bflc():
    """Verify that pure BFFI records extract constituents without bflc: namespace."""
    bffi_schema_profile = get_profile("bffi_1")
    const_profile = get_constituent_profile("bffi_haspart")

    # Ensure profile does not mention bflc
    assert "bflc" not in const_profile.name
    for f in const_profile.fields:
        if f.predicate:
            assert "bflc" not in f.predicate
        for sp in f.sequence_predicates:
            assert "bflc" not in sp

    # Create synthetic BFFI Graph
    g = Graph()
    g.bind("bffi", BFFI)
    g.bind("bf", BF)

    manifestation_node = URIRef("http://urn.fi/URN:NBN:fi:bib:manif:10002388")
    work_node = URIRef("http://urn.fi/URN:NBN:fi:bib:work:10002388")
    expr_node = URIRef("http://urn.fi/URN:NBN:fi:bib:expr:10002388")

    g.add((manifestation_node, RDF.type, BFFI.CollectionManifestation))
    g.add((manifestation_node, BF.instanceOf, work_node))
    g.add((work_node, RDF.type, BFFI.AggregatingWork))
    g.add((manifestation_node, BFFI.hasExpression, expr_node))
    g.add((expr_node, RDF.type, BFFI.Expression))
    g.add((expr_node, BF.expressionOf, work_node))

    # Add constituent parts under work_node using pure bffi:hasPart
    part1 = URIRef("http://urn.fi/URN:NBN:fi:bib:work:song_1")
    title1_node = URIRef("http://urn.fi/URN:NBN:fi:bib:title:song_1")
    g.add((work_node, BFFI.hasPart, part1))
    g.add((part1, RDF.type, BFFI.Work))
    g.add((part1, BF.title, title1_node))
    g.add((title1_node, RDF.type, BF.Title))
    g.add((title1_node, BF.mainTitle, Literal("Laula, tyttö! /")))
    g.add((part1, BFFI.sequenceNumber, Literal(1)))
    g.add(
        (
            part1,
            BFFI.marcKey,
            Literal("7300 $aLaula, tyttö!$gMerikanto, Oskar"),
        )
    )

    part2 = URIRef("http://urn.fi/URN:NBN:fi:bib:work:song_2")
    title2_node = URIRef("http://urn.fi/URN:NBN:fi:bib:title:song_2")
    g.add((work_node, BFFI.hasPart, part2))
    g.add((part2, RDF.type, BFFI.Work))
    g.add((part2, BF.title, title2_node))
    g.add((title2_node, RDF.type, BF.Title))
    g.add((title2_node, BF.mainTitle, Literal("Oi muistatko vielä sen virren")))
    g.add((part2, BFFI.sequence, Literal(2)))
    g.add(
        (
            part2,
            BFFI.marcKey,
            Literal("7300 $aOi muistatko vielä sen virren$gMerikanto, Oskar"),
        )
    )

    # Verify no bflc triple in graph
    for s, p, o in g:
        assert "bflc" not in str(p)
        assert "bflc" not in str(o)

    constituents = extract_bibframe_constituents(
        g=g,
        work_node=work_node,
        constituent_profile=const_profile,
        profile=bffi_schema_profile,
        source="natlibfi",
        record_id="b10002388",
    )

    assert len(constituents) == 2
    assert constituents[0].title == "Laula, tyttö!"
    assert constituents[0].creator == "Merikanto, Oskar"
    assert constituents[0].ordinal == 1
    assert constituents[0].identity_ref == "http://urn.fi/URN:NBN:fi:bib:work:song_1"

    assert constituents[1].title == "Oi muistatko vielä sen virren"
    assert constituents[1].creator == "Merikanto, Oskar"
    assert constituents[1].ordinal == 2
    assert constituents[1].identity_ref == "http://urn.fi/URN:NBN:fi:bib:work:song_2"


def test_bffi_decomposition_with_constituent_sink():
    """Verify that decompose_bibframe_work populates constituent_sink using bffi profile."""
    bffi_schema_profile = get_profile("bffi_1")
    const_profile = get_constituent_profile("bffi_haspart")

    g = Graph()
    g.bind("bffi", BFFI)

    manifestation_node = URIRef("http://urn.fi/URN:NBN:fi:bib:manif:test1")
    work_node = URIRef("http://urn.fi/URN:NBN:fi:bib:work:test1")
    g.add((manifestation_node, RDF.type, BFFI.CollectionManifestation))
    g.add((manifestation_node, BFFI.workManifested, work_node))
    g.add((work_node, RDF.type, BFFI.AggregatingWork))

    title_node = URIRef("http://urn.fi/URN:NBN:fi:bib:title:test1")
    g.add((work_node, BFFI.title, title_node))
    g.add((title_node, BFFI.mainTitle, Literal("113 kitaralaulua")))

    part = URIRef("http://urn.fi/URN:NBN:fi:bib:work:p1")
    g.add((work_node, BFFI.hasPart, part))
    t_node = URIRef("http://urn.fi/URN:NBN:fi:bib:title:p1")
    g.add((part, BFFI.title, t_node))
    g.add((t_node, BFFI.mainTitle, Literal("Kalliolle kukkulalle")))
    g.add((part, BFFI.sequenceNumber, Literal(1)))

    sink = []
    w, e, m, itms, agts, rels, idents, assoc, contribs = decompose_bibframe_work(
        g=g,
        instance_node=manifestation_node,
        profile=bffi_schema_profile,
        source="natlibfi",
        constituent_profile=const_profile,
        constituent_sink=sink,
    )

    assert len(sink) == 1
    assert sink[0].title == "Kalliolle kukkulalle"
    assert sink[0].ordinal == 1

