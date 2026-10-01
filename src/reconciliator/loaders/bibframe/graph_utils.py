"""RDF graph traversal and node identity utilities for BIBFRAME."""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Optional

from rdflib import RDFS, SKOS, BNode, Graph, IdentifiedNode, Literal, URIRef

from reconciliator.models import IdentifierEntity

if TYPE_CHECKING:
    from reconciliator.loaders.bibframe.profiles import BIBFRAMESchemaProfile

def extract_marc_key_components(
    marc_key: str, profile: Optional[BIBFRAMESchemaProfile] = None
) -> Optional[tuple[str, str, str]]:
    """Extract (tag, indicators, subfields) from a provenance literal string.

    Example:
        '1001 $aWaltari, Mika,$d1908-1979,$ewriter' -> ('100', '1 ', '$aWaltari, Mika,$d1908-1979,$ewriter')
    """
    from reconciliator.loaders.bibframe.profiles import get_profile

    p = profile or get_profile()
    m = re.match(p.provenance_regex, marc_key.strip())
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None

def _get_literals(g: Graph, subject: IdentifiedNode, predicates: list[str] | set[str]) -> list[str]:
    """Retrieve unique string values for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, Literal):
                val = str(obj).strip()
                if val and val not in results:
                    results.append(val)
            elif isinstance(obj, IdentifiedNode):
                for label in g.objects(obj, RDFS.label):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
                for label in g.objects(obj, SKOS.prefLabel):
                    val = str(label).strip()
                    if val and val not in results:
                        results.append(val)
    return results

def _get_uris(g: Graph, subject: IdentifiedNode, predicates: list[str] | set[str]) -> list[str]:
    """Retrieve unique URIRef strings for given predicates on a subject."""
    results: list[str] = []
    for pred in predicates:
        pred_ref = URIRef(pred) if not isinstance(pred, URIRef) else pred
        for obj in g.objects(subject, pred_ref):
            if isinstance(obj, URIRef):
                u = str(obj).strip()
                if u and u not in results:
                    results.append(u)
    return results

def _identifiers_as_bag(identifiers: list[IdentifierEntity]) -> dict[str, list[str]]:
    """Shape IdentifierEntity rows as the scheme -> values bag MatchRecord carries."""
    bag: dict[str, list[str]] = {}
    for ident in identifiers:
        bucket = bag.setdefault(ident.scheme.lower().strip(), [])
        if ident.value not in bucket:
            bucket.append(ident.value)
    return bag
def is_blank_node(node: Any) -> bool:
    """Whether an RDF term is a blank node.

    `str(BNode())` returns a *bare* label - "Nc5b5385e..." - with no "_:"
    prefix; only `.n3()` adds one. Every `startswith("_:")` guard in this module
    was therefore dead code that never fired, which is how parse-local labels
    came to be used as work, expression and manifestation URIs.
    """
    return isinstance(node, BNode)

def derive_bibframe_node_id(g: Graph, node: Optional[IdentifiedNode]) -> str:
    """A stable identifier for a BIBFRAME node.

    A node with a real URI is identified by it. A blank node is not: rdflib mints
    a fresh label on every parse, so identity taken from the label changes each
    time the same file is ingested - new work, expression and manifestation URIs,
    no upsert match, duplicate rows, and an `ingest_mode: replace` that cannot
    help because the ids differ between runs.

    This mirrors `derive_content_id` on the MARC side: identity from content, not
    from position or parse order. The digest covers the node's own
    predicate/object pairs, with a blank-node object replaced by a digest of
    *its* pairs - one level of recursion, which is what BIBFRAME titles need,
    since `bf:title [ a bf:Title ; bf:mainTitle "X" ]` puts the title itself
    behind a blank node.
    """
    if node is None:
        return ""
    if not is_blank_node(node):
        return str(node)

    def pairs(subject: Any, depth: int) -> list[str]:
        out: list[str] = []
        for predicate, obj in sorted(g.predicate_objects(subject), key=lambda po: str(po[0])):
            obj_text = str(obj)
            if is_blank_node(obj):
                if depth <= 0:
                    continue
                inner = hashlib.sha1("\n".join(pairs(obj, depth - 1)).encode("utf-8")).hexdigest()[
                    :16
                ]
                obj_text = f"_:{inner}"
            out.append(f"{predicate}\t{obj_text}")
        return out

    digest = hashlib.sha1("\n".join(pairs(node, 1)).encode("utf-8")).hexdigest()[:16]
    return f"sha1:{digest}"

def bibframe_work_uri(g: Graph, node: Optional[IdentifiedNode], source: str) -> str:
    """The work URI (CURIE) a node will be stored under.

    Mirrors how `decompose_bibframe_work` builds it: every work is stored under
    the canonical CURIE `source:node_id`.
    """
    if node is None:
        return ""
    node_id = derive_bibframe_node_id(g, node)
    return f"{source}:{node_id}"

