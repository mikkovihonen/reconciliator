"""Generic hierarchical vocabulary reasoning, semantic similarity, and DAG traversal."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Optional

from reconciliator.config import HierarchyConfig
from reconciliator.vocab.detection import extract_concept_id


class ConceptGraph:
    """Directed Acyclic Graph (DAG) representing concept broader/narrower relationships."""

    def __init__(self) -> None:
        self.parents: dict[str, set[str]] = {}
        self.children: dict[str, set[str]] = {}

    def add_edge(self, concept_id: str, broader_id: str) -> None:
        """Add a directed edge concept_id -> broader_id."""
        cid = concept_id.strip()
        bid = broader_id.strip()
        if not cid or not bid or cid == bid:
            return
        self.parents.setdefault(cid, set()).add(bid)
        self.children.setdefault(bid, set()).add(cid)

    def get_parents(self, concept_id: str) -> set[str]:
        """Direct broader concepts."""
        return set(self.parents.get(concept_id.strip(), ()))

    def get_children(self, concept_id: str) -> set[str]:
        """Direct narrower concepts."""
        return set(self.children.get(concept_id.strip(), ()))

    def get_ancestors(self, concept_id: str, max_depth: int = 10) -> dict[str, int]:
        """Traverse upwards to find all broader ancestors and their shortest depths."""
        cid = concept_id.strip()
        if not cid:
            return {}
        visited: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(cid, 0)])

        while queue:
            curr, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for p in self.parents.get(curr, ()):
                next_depth = depth + 1
                if p not in visited or next_depth < visited[p]:
                    visited[p] = next_depth
                    queue.append((p, next_depth))

        return visited

    def get_descendants(self, concept_id: str, max_depth: int = 10) -> dict[str, int]:
        """Traverse downwards to find all narrower descendants and their shortest depths."""
        cid = concept_id.strip()
        if not cid:
            return {}
        visited: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque([(cid, 0)])

        while queue:
            curr, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for c in self.children.get(curr, ()):
                next_depth = depth + 1
                if c not in visited or next_depth < visited[c]:
                    visited[c] = next_depth
                    queue.append((c, next_depth))

        return visited

    def find_lowest_common_ancestor(
        self, id_a: str, id_b: str, max_depth: int = 10
    ) -> tuple[Optional[str], int, int]:
        """Find the Lowest Common Ancestor (LCA) between two concepts.

        Returns (lca_id, depth_from_a, depth_from_b).
        If a == b, returns (a, 0, 0).
        If no common ancestor exists, returns (None, -1, -1).
        """
        a = id_a.strip()
        b = id_b.strip()
        if not a or not b:
            return None, -1, -1
        if a == b:
            return a, 0, 0

        anc_a = self.get_ancestors(a, max_depth=max_depth)
        anc_a[a] = 0

        anc_b = self.get_ancestors(b, max_depth=max_depth)
        anc_b[b] = 0

        common = set(anc_a.keys()) & set(anc_b.keys())
        if not common:
            return None, -1, -1

        # Select LCA minimizing sum of distances
        best_lca: Optional[str] = None
        min_total_dist = float("inf")
        best_da = -1
        best_db = -1

        for c in common:
            total_dist = anc_a[c] + anc_b[c]
            if total_dist < min_total_dist:
                min_total_dist = total_dist
                best_lca = c
                best_da = anc_a[c]
                best_db = anc_b[c]

        return best_lca, best_da, best_db

    def compute_distance(self, id_a: str, id_b: str, max_depth: int = 10) -> Optional[int]:
        """Compute shortest distance (hops) between two concepts in the taxonomy."""
        _, da, db = self.find_lowest_common_ancestor(id_a, id_b, max_depth=max_depth)
        if da == -1 or db == -1:
            return None
        return da + db

    def compute_path_decay_similarity(
        self, id_a: str, id_b: str, decay_factor: float = 0.85, max_depth: int = 10
    ) -> float:
        """Compute semantic similarity using exponential path decay: Sim = decay_factor^(distance)."""
        a = id_a.strip()
        b = id_b.strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        dist = self.compute_distance(a, b, max_depth=max_depth)
        if dist is None:
            return 0.0
        return float(decay_factor**dist)

    def compute_wu_palmer_similarity(self, id_a: str, id_b: str, max_depth: int = 10) -> float:
        """Compute Wu-Palmer similarity: 2 * depth(LCA) / (depth(a) + depth(b))."""
        a = id_a.strip()
        b = id_b.strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        lca, da, db = self.find_lowest_common_ancestor(a, b, max_depth=max_depth)
        if not lca or da == -1 or db == -1:
            return 0.0

        # Depth from root: maximum depth of LCA from any top-level root concept
        lca_anc = self.get_ancestors(lca, max_depth=max_depth)
        depth_lca = max(lca_anc.values(), default=0) + 1
        depth_a = depth_lca + da
        depth_b = depth_lca + db

        denominator = depth_a + depth_b
        if denominator == 0:
            return 0.0
        return (2.0 * depth_lca) / float(denominator)


class HierarchyService:
    """High-level service for hierarchical vocabulary operations, similarity, and expansion."""

    def __init__(self, vocab_store: Any, config: Optional[HierarchyConfig] = None) -> None:
        self.vocab_store = vocab_store
        self.config = config or getattr(vocab_store.config, "hierarchy", HierarchyConfig())
        self._graphs: dict[str, ConceptGraph] = {}

    def get_graph(self, vocabulary: str) -> ConceptGraph:
        """Get or load in-memory ConceptGraph for a vocabulary."""
        clean_voc = str(vocabulary).strip().lower()
        if clean_voc in self._graphs:
            return self._graphs[clean_voc]

        graph = ConceptGraph()
        edges = self.vocab_store.load_hierarchy_edges(clean_voc)
        for cid, pcid in edges:
            graph.add_edge(cid, pcid)

        if self.config.cache_graphs:
            self._graphs[clean_voc] = graph
        return graph

    def get_broader(
        self, vocabulary: str, concept_id: str, max_depth: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """Retrieve broader concepts (ancestors) with depths."""
        depth = max_depth or self.config.max_traversal_depth
        graph = self.get_graph(vocabulary)
        anc = graph.get_ancestors(concept_id, max_depth=depth)
        return sorted(anc.items(), key=lambda x: x[1])

    def get_narrower(
        self, vocabulary: str, concept_id: str, max_depth: Optional[int] = None
    ) -> list[tuple[str, int]]:
        """Retrieve narrower concepts (descendants) with depths."""
        depth = max_depth or self.config.max_traversal_depth
        graph = self.get_graph(vocabulary)
        desc = graph.get_descendants(concept_id, max_depth=depth)
        return sorted(desc.items(), key=lambda x: x[1])

    def find_lowest_common_ancestor(
        self, vocabulary: str, concept_a: str, concept_b: str
    ) -> tuple[Optional[str], int, int]:
        """Find LCA between concept_a and concept_b."""
        graph = self.get_graph(vocabulary)
        return graph.find_lowest_common_ancestor(
            concept_a, concept_b, max_depth=self.config.max_traversal_depth
        )

    def compute_distance(self, vocabulary: str, concept_a: str, concept_b: str) -> Optional[int]:
        """Compute shortest path distance in the taxonomy."""
        graph = self.get_graph(vocabulary)
        return graph.compute_distance(
            concept_a, concept_b, max_depth=self.config.max_traversal_depth
        )

    def compute_similarity(self, vocabulary: str, concept_a: str, concept_b: str) -> float:
        """Compute semantic similarity using the configured algorithm."""
        if not self.config.enabled:
            return 1.0 if concept_a.strip() == concept_b.strip() else 0.0

        graph = self.get_graph(vocabulary)
        if self.config.similarity_algorithm == "wu_palmer":
            sim = graph.compute_wu_palmer_similarity(
                concept_a, concept_b, max_depth=self.config.max_traversal_depth
            )
        else:
            sim = graph.compute_path_decay_similarity(
                concept_a,
                concept_b,
                decay_factor=self.config.path_decay_factor,
                max_depth=self.config.max_traversal_depth,
            )

        return sim if sim >= self.config.min_similarity_threshold else 0.0

    def expand_concepts(
        self, vocabulary: str, concept_ids: Iterable[str], max_depth: Optional[int] = None
    ) -> set[str]:
        """Expand a set of concept IDs to include their broader ancestors."""
        depth = max_depth if max_depth is not None else self.config.max_expansion_depth
        if depth <= 0:
            return {c.strip() for c in concept_ids if c and c.strip()}

        graph = self.get_graph(vocabulary)
        expanded: set[str] = set()
        for c in concept_ids:
            clean_c = c.strip()
            if not clean_c:
                continue
            expanded.add(clean_c)
            for anc, _ in graph.get_ancestors(clean_c, max_depth=depth).items():
                expanded.add(anc)

        return expanded

    def concepts_similarity(self, set_a: set[str], set_b: set[str]) -> float:
        """Compute semantic overlap between two sets of concepts (e.g. Agent concept footprints).

        Concepts may be formatted as 'vocab:id' or bare 'id'.
        If exact match exists, similarity is 1.0.
        Otherwise, calculates maximum bipartite hierarchical similarity across concepts of matching vocabularies.
        """
        if not set_a or not set_b:
            return 0.0

        # Exact match shortcut
        if set_a & set_b:
            return 1.0

        if not self.config.enabled:
            return 0.0

        # Parse concepts into (vocab, id) pairs
        parsed_a: list[tuple[str, str]] = []
        for item in set_a:
            item_str = str(item).strip()
            if ":" in item_str and not item_str.startswith("http"):
                v, _, cid = item_str.partition(":")
                parsed_a.append((v.lower(), cid))
            else:
                cid = extract_concept_id(item_str) or item_str
                parsed_a.append(("yso", cid))

        parsed_b: list[tuple[str, str]] = []
        for item in set_b:
            item_str = str(item).strip()
            if ":" in item_str and not item_str.startswith("http"):
                v, _, cid = item_str.partition(":")
                parsed_b.append((v.lower(), cid))
            else:
                cid = extract_concept_id(item_str) or item_str
                parsed_b.append(("yso", cid))

        # Compare concepts sharing the same vocabulary
        max_sim = 0.0
        for v_a, id_a in parsed_a:
            for v_b, id_b in parsed_b:
                if v_a == v_b:
                    sim = self.compute_similarity(v_a, id_a, id_b)
                    if sim > max_sim:
                        max_sim = sim
                        if max_sim >= 1.0:
                            return 1.0

        return max_sim
