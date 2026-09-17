"""Disjoint Set Union (UnionFind) data structure."""

from __future__ import annotations

from collections import defaultdict
from typing import Generic, Iterable, TypeVar

T = TypeVar("T")


class UnionFind(Generic[T]):
    """Disjoint Set Union (DSU) data structure with path compression and union by rank."""

    def __init__(self, elements: Iterable[T]) -> None:
        self.parent: dict[T, T] = {e: e for e in elements}
        self.rank: dict[T, int] = {e: 0 for e in elements}

    def find(self, item: T) -> T:
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, item1: T, item2: T) -> None:
        root1 = self.find(item1)
        root2 = self.find(item2)
        if root1 != root2:
            if self.rank[root1] < self.rank[root2]:
                self.parent[root1] = root2
            elif self.rank[root1] > self.rank[root2]:
                self.parent[root2] = root1
            else:
                self.parent[root2] = root1
                self.rank[root1] += 1

    def get_components(self) -> dict[T, list[T]]:
        """Return connected components grouped by root representative."""
        components: dict[T, list[T]] = defaultdict(list)
        for item in self.parent:
            root = self.find(item)
            components[root].append(item)
        return dict(components)

    groups = get_components

