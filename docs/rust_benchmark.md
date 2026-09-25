# Rust vs. Python Implementation Benchmarks

This document records the empirical benchmarking results comparing native Rust implementations (`reconciliator._core`) against their pure Python / external library counterparts across the Reconciliator codebase.

All benchmarks were executed on an Apple Silicon host running Python 3.14 (`aarch64-apple-darwin`), with release-optimized Rust extensions compiled via `maturin` (`opt-level = 3`, LTO enabled).

---

## 1. Benchmark Results Matrix

| Module / Operation | Rust Implementation | Python / Baseline Implementation | Benchmark Workload | Rust Time | Python Time | Speedup Factor | Architectural Decision |
|---|---|---|---|---|---|---|---|
| **Turtle Parsing (`slm.ttl`)** | `reconciliator._core.parsers.py_parse_turtle_file` | `rdflib.Graph` | 13,000 triples | **0.0060s** | 0.2964s | **49.6× faster** | Adopted Rust; removed `rdflib` dependency and fallback |
| **Turtle Parsing (`yso-paikat.ttl`)** | `reconciliator._core.parsers.py_parse_turtle_file` | `rdflib.Graph` | 165,000 triples | **0.0653s** | 2.9723s | **45.5× faster** | Adopted Rust; removed `rdflib` dependency and fallback |
| **BM25 Lexical Reranker** | `reconciliator._core.rerank.py_bm25_score_batch` | `rank_bm25.BM25Okapi` | 2,000 documents | **0.0026s** | 0.1174s | **45.1× faster** | Adopted Rust (0.000 diff); removed `rank-bm25` |
| **Diacritic Folding** | `reconciliator._core.normalizers.py_fold_diacritics` | `unicodedata.normalize` loop | 50,000 strings | **0.0182s** | 0.2701s | **14.8× faster** | Retained Rust; direct delegation |
| **Longest Common Subsequence** | `reconciliator._core.clustering.py_lcs_length` | Python DP matrix | 10,000 string pairs | **0.0080s** | 0.1033s | **12.9× faster** | Retained Rust; direct delegation |
| **Volume Normalization** | `reconciliator._core.rerank.py_normalize_volume` | Python regex tokenizer | 10,000 strings | **0.0194s** | 0.1530s | **7.9× faster** | Retained Rust; removed Python regex fallback |
| **Numeric Title Tokens Check** | `reconciliator._core.rerank.py_check_numeric_title_tokens_compatible` | Python regex set intersection | 10,000 comparisons | **0.0265s** | 0.0756s | **2.9× faster** | Retained Rust; removed Python fallback |
| **Union-Find Graph Clustering** | `reconciliator.utils.cluster_edges` (`IntUnionFind`) | Python `UnionFind` class | 10k nodes, 5k edges | **0.0201s** | 0.0586s | **2.9× faster** | Retained Rust; direct delegation |
| **MARCXML Parsing** | `reconciliator._core.parsers.py_parse_marc_file` | `lxml.etree.iterparse` | 1,000 records | **0.0302s** | 0.0639s | **2.1× faster** | Retained Rust SAX parser |
| **Fuzzy String Similarity** | `py_fuzz_ratio` / `py_fuzz_token_sort_ratio` | `rapidfuzz.fuzz` | 10,000 author/title pairs | 0.0082s | **0.0022s** | **3.7× slower** | **Removed Rust `similarity.rs`**; standardizing on Rapidfuzz |

---

## 2. Key Findings & Architectural Outcomes

Per the architectural policy (*"No backwards compatibility, no aliases, no shims, no hardcoding. If Rust is faster, remove the Python fallback; if Rust is slower, remove the Rust implementation"*):

### A. Removed Slower Rust Implementations
- **Fuzzy String Similarity**:
  - `rapidfuzz` (C++ SIMD bit-parallel Myers algorithm with AVX2/NEON optimizations) proved **2.1× to 3.7× faster** than scalar Rust similarity functions.
  - **Action**: Completely removed `crates/reconciliator_core/src/similarity.rs` and its Python bindings.
  - Standardized on `rapidfuzz.fuzz` across `reconciliator.reconciliation.agents`, achieving ~1.53 million author/agent comparisons per second.

### B. Removed Python Fallbacks Where Rust is Superior
1. **BM25 Lexical Scoring**:
   - Rust implementation with Rayon parallelization and `OnceLock` term document frequency caching achieved a **45.1× speedup** over `rank-bm25` with 0.000000 maximum score divergence.
   - **Action**: Dropped `rank-bm25` dependency from `pyproject.toml`. Removed `_HAVE_RUST_BM25` shims and the pure-Python `rank_bm25` fallback code in `src/reconciliator/rerank/bm25.py`.
2. **SKOS Turtle (`.ttl`) Ingestion**:
   - Streaming native Rust parser (`rio_turtle`) achieved **45.5×–49.6× speedups** over `rdflib.Graph` and parsed large vocabularies (e.g. `yso-paikat.ttl` with 165k triples) in **65ms** vs. **2.97s**.
   - **Action**: Dropped `rdflib` fallback from `src/reconciliator/ingestion/vocab.py`, removing ~170 lines of dead fallback logic and monkey patches.
3. **Rerank Evaluator Discriminators**:
   - Native Rust string volume normalization and numeric title token checks achieved **7.9×** and **2.9×** speedups respectively.
   - **Action**: Removed `_HAVE_RUST_DISCRIMINATORS` and Python regex fallback paths from `src/reconciliator/rerank/evaluator.py`.

### C. Elimination of Shims & Feature Flags
- Removed all `_HAVE_RUST_*` global boolean flags across the entire codebase.
- Removed all `try...except ImportError` fallback blocks surrounding `reconciliator._core` imports.
- Native Rust extensions are now first-class, mandatory runtime components of the project.

