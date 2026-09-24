use hashbrown::HashMap;

/// Fast Disjoint-Set Union (Union-Find) with path compression and union by rank.
pub struct IntUnionFind {
    parent: Vec<usize>,
    rank: Vec<u8>,
}

impl IntUnionFind {
    pub fn new(size: usize) -> Self {
        Self {
            parent: (0..size).collect(),
            rank: vec![0; size],
        }
    }

    pub fn find(&mut self, mut i: usize) -> usize {
        let mut root = i;
        while root != self.parent[root] {
            root = self.parent[root];
        }
        // Path compression
        while i != root {
            let next = self.parent[i];
            self.parent[i] = root;
            i = next;
        }
        root
    }

    pub fn union(&mut self, i: usize, j: usize) -> bool {
        let root_i = self.find(i);
        let root_j = self.find(j);
        if root_i == root_j {
            return false;
        }

        if self.rank[root_i] < self.rank[root_j] {
            self.parent[root_i] = root_j;
        } else if self.rank[root_i] > self.rank[root_j] {
            self.parent[root_j] = root_i;
        } else {
            self.parent[root_j] = root_i;
            self.rank[root_i] = self.rank[root_i].saturating_add(1);
        }
        true
    }

    pub fn get_components(&mut self) -> HashMap<usize, Vec<usize>> {
        let mut components: HashMap<usize, Vec<usize>> = HashMap::new();
        for i in 0..self.parent.len() {
            let root = self.find(i);
            components.entry(root).or_default().push(i);
        }
        components
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_int_union_find() {
        let mut uf = IntUnionFind::new(5);
        assert_eq!(uf.find(0), 0);
        assert_eq!(uf.find(1), 1);

        assert!(uf.union(0, 1));
        assert_eq!(uf.find(0), uf.find(1));

        assert!(uf.union(2, 3));
        assert_eq!(uf.find(2), uf.find(3));
        assert_ne!(uf.find(0), uf.find(2));

        assert!(uf.union(1, 3));
        assert_eq!(uf.find(0), uf.find(2));

        // Already connected
        assert!(!uf.union(0, 2));

        let components = uf.get_components();
        assert_eq!(components.len(), 2); // {0, 1, 2, 3} and {4}
    }
}
