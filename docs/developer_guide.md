# Developer Guide: Reconciliator

Welcome to the `reconciliator` developer documentation. This guide provides complete onboarding and reference material for developing, testing, extending, and maintaining the platform.

`reconciliator` is built as a **hybrid high-performance architecture**:
- **Python (3.10+)**: High-level orchestration, CLI workflows, PostgreSQL relational mappings, Pydantic validation, MLX / PyTorch vector embeddings, and LLM judge pipelines.
- **Rust (2021 edition)**: Compute-intensive core modules (`reconciliator-core`) exposed via **PyO3** and packaged via **Maturin**, powering zero-copy Unicode normalization, high-speed sequence alignment, flat-array disjoint-set graph clustering, and SAX MARCXML streaming parsers.

---

## 1. Prerequisites & Toolchain Setup

### 1.1 Core Tools

| Tool | Recommended Version | Purpose | Installation |
|---|---|---|---|
| **Python** | 3.10 – 3.14 | Runtime & orchestration | Managed via `uv` or system Python |
| **uv** | Latest | Python package & venv manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` or `brew install uv` |
| **rustup** | Latest | Rust toolchain manager | `brew install rustup` or `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| **rustc & cargo** | 1.80+ (tested on 1.98+) | Rust compiler & package manager | Installed automatically by `rustup` |
| **maturin** | 1.15.0+ | PEP 517 build backend for PyO3 | Installed in virtualenv via `uv sync` / `uv pip` |
| **pigz** *(optional)* | Latest | Parallel gzip for fast MARCXML ingestion | `brew install pigz` or `apt install pigz` |

### 1.2 Initial Workspace Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/NatLibFi/reconciliator.git
   cd reconciliator
   ```

2. **Ensure Rust is in your PATH**:
   If using Homebrew on macOS Apple Silicon:
   ```bash
   export PATH="/opt/homebrew/opt/rustup/bin:$PATH"
   ```
   Or standard `rustup` path:
   ```bash
   source "$HOME/.cargo/env"
   ```
   Verify versions:
   ```bash
   rustc --version
   cargo --version
   ```

3. **Set up Python Virtual Environment**:
   ```bash
   uv venv
   source .venv/bin/activate
   uv sync
   ```

4. **Compile the Native Rust Extension in Place**:
   ```bash
   maturin develop
   ```
   Or for optimized release mode (recommended for benchmarking & production):
   ```bash
   maturin develop --release
   ```

5. **Verify the Installation**:
   ```bash
   python -c "from reconciliator import _core; print(_core.rust_ping())"
   # Output: pong from rust
   ```

---

## 2. Repository Layout

```
reconciliator/
├── Cargo.toml                         # Root Cargo workspace manifest
├── Cargo.lock                         # Locked Rust dependencies
├── pyproject.toml                     # Python packaging (maturin build backend)
│
├── crates/
│   └── reconciliator_core/            # Native Rust crate (reconciliator-core)
│       ├── Cargo.toml                 # Crate definition (crate-type = ["cdylib", "rlib"])
│       └── src/
│           ├── lib.rs                 # PyO3 root module & sys.modules registration
│           ├── normalizers/           # Phase 1: Pure-logic text normalizers
│           │   ├── mod.rs             # Submodule registration & PyO3 wrappers
│           │   ├── linguistic.rs      # fold_diacritics, strip_biographical_dates, fold_label
│           │   ├── blocking.rs        # SHA-256 keys, work keys, series & citation keys
│           │   └── identifiers.rs     # ISBN-10 to ISBN-13, ISBN validation, clean_alphanumeric
│           ├── clustering/            # Phase 2: Parallel clustering & alignment
│           │   ├── mod.rs             # Submodule registration & PyO3 wrappers
│           │   ├── lcs.rs             # LCS sequence alignment, Jaccard, aggregate overlap
│           │   └── union_find.rs      # Flat-array IntUnionFind with path compression
│           └── parsers/               # Phase 3: High-throughput streaming parsers
│               ├── mod.rs             # Submodule registration & PyO3 wrappers
│               ├── marcxml.rs         # Zero-copy quick-xml SAX parser with pigz support
│               └── turtle.rs          # Fast streaming SKOS Turtle parser with URN & legacy detection
│
├── src/
│   └── reconciliator/                 # Main Python package
│       ├── _core.pyi                  # Complete type stub signatures for the Rust extension
│       ├── cli.py                     # CLI entrypoints (Rich console, commands)
│       ├── config/                    # Pydantic configuration hierarchy
│       ├── database/                  # SQLAlchemy / PostgreSQL schemas and migrations
│       ├── ingestion/                 # Ontology and vocabulary ingestion
│       │   └── vocab.py               # parse_turtle_file / parse_turtle_simple calling _core.parsers
│       ├── vocab/                     # Vocabulary stores & graph caches
│       │   └── store.py               # Streamlined bulk loading with RETURNING & batched inserts
│       ├── normalizers/               # Normalization facades (Rust fast-path + Python fallback)
│       │   ├── linguistic.py          # Delegates to _core.normalizers
│       │   ├── blocking.py            # Delegates to _core.normalizers
│       │   └── identifiers.py         # Delegates to _core.normalizers
│       ├── clustering/                # WEMI multiplex graph clusterer
│       │   └── corpus.py              # CorpusClusterer calling _core.clustering / cluster_edges
│       ├── loaders/                   # Bibliographic ingest loaders
│       │   └── marcxml.py             # MARCXML loaders & fast-paths (parse_marc_file_fast)
│       ├── utils/
│       │   └── union_find.py          # cluster_edges bridging Python lists to IntUnionFind
│       └── ...
│
├── tests/                             # Test suite
│   ├── test_rust_core.py              # Verification suite for PyO3 Rust extension
│   ├── test_normalizers.py            # Tests for linguistic, blocking, and identifier rules
│   ├── test_cross_format_aggregate_clustering.py
│   ├── test_marcxml_indexing.py
│   └── ...
└── docs/                              # Technical & architecture documentation
    ├── developer_guide.md             # This document
    ├── datamodel.md                   # WEMI entity and schema definitions
    └── runbook.md                     # Operational runbook
```

---

## 3. Python / Rust Interoperability Architecture

### 3.1 Build Mechanics (`pyproject.toml` + `Cargo.toml`)

`reconciliator` utilizes **PEP 517** via `maturin`:
- In `pyproject.toml`, `[build-system]` specifies `build-backend = "maturin"`.
- `[tool.maturin]` configures:
  - `python-source = "src"`: Python files live in `src/reconciliator`.
  - `module-name = "reconciliator._core"`: The compiled shared library (`.so` / `.dylib`) is placed inside `src/reconciliator/_core.*.so`.
- Root `Cargo.toml` points to `crates/reconciliator_core` which compiles as a `cdylib`.

### 3.2 Dual-Import & `sys.modules` Registration

In PyO3, submodules added via `parent_module.add_submodule(&submod)` are attributes on the parent module. To allow both direct submodule access and package-style imports in Python:
```python
# Both import patterns are fully supported:
from reconciliator._core import normalizers
from reconciliator._core.normalizers import py_fold_diacritics
```

This is achieved in each Rust submodule's registration hook:
```rust
parent_module.add_submodule(&submod)?;
py.import("sys")?
    .getattr("modules")?
    .set_item("reconciliator._core.<submodule>", &submod)?;
```

### 3.3 Direct Delegation Architecture

To eliminate code duplication, maintenance overhead, and divergent behavior between language boundaries, operations with native Rust implementations delegate directly to `reconciliator._core`:

```python
from reconciliator._core.normalizers import (
    py_fold_diacritics as _rust_fold_diacritics,
    py_strip_biographical_dates as _rust_strip_biographical_dates,
)

def fold_diacritics(s: str, preserved_diacritics: Optional[Container[str] | str] = None) -> str:
    """Selectively fold Latin diacritics; preserve specified diacritics (default: Finnish / Swedish åäöÅÄÖ)."""
    if preserved_diacritics is None or isinstance(preserved_diacritics, str):
        return _rust_fold_diacritics(s, preserved_diacritics)
    return _rust_fold_diacritics(s, "".join(preserved_diacritics))
```

This ensures the Rust core is the single source of truth for normalizers, graph algorithms, and parsing logic.

### 3.4 Static Typing with `_core.pyi`

All native functions, classes, and submodules are declared in [`src/reconciliator/_core.pyi`](../src/reconciliator/_core.pyi). This ensures IDE autocompletion, type hinting, and `mypy` static type checking work seamlessly without having to inspect C extensions.

When adding or altering a PyO3 function, **always update `_core.pyi` simultaneously**.

---

## 4. Daily Developer Workflows

### 4.1 Modifying Rust Code

Whenever you change files in `crates/reconciliator_core/`:

1. **Recompile into Virtualenv**:
   ```bash
   # Debug mode (fast compilation)
   maturin develop

   # Release mode (full compiler optimizations: inlining, LTO, SIMD)
   maturin develop --release
   ```

2. **Offline Rebuilds (e.g., inside restricted sandboxes or air-gapped CI)**:
   ```bash
   maturin develop --release --skip-install --offline
   ```

### 4.2 Running Tests

#### Rust Unit Tests
Fast, zero-dependency unit tests running natively:
```bash
cargo test
```

#### Targeted Python Integration Tests
Verify the PyO3 extension, normalizers, and clustering without requiring a live PostgreSQL instance:
```bash
pytest tests/test_rust_core.py tests/test_normalizers.py tests/test_cross_format_aggregate_clustering.py tests/test_marcxml_indexing.py
```

#### Full End-to-End Suite (requires local PostgreSQL)
```bash
pytest
```

### 4.3 Code Quality, Linting & Formatting

#### Rust
- **Formatting**:
  ```bash
  cargo fmt --check    # Check
  cargo fmt            # Apply auto-formatting
  ```
- **Linting & Diagnostics**:
  ```bash
  cargo clippy -- -D warnings
  ```

#### Python
- **Formatting**:
  ```bash
  ruff format --check  # Check
  ruff format          # Apply auto-formatting
  ```
- **Linting**:
  ```bash
  ruff check
  ```
- **Type Checking**:
  ```bash
  mypy src/
  ```

---

## 5. Performance Benchmarks & High-ROI Subsystems

The native Rust components target hot loops where Python interpreter overhead, regex churn, and memory allocations are highest:

| Subsystem | Pure Python | Native Rust | Speedup | Notes |
|---|---|---|---|---|
| **Diacritic Folding** (`fold_diacritics`) | 143,451 ops/sec | **2,110,729 ops/sec** | **14.7×** | Zero-copy NFC walk, fast-path ASCII bypass, Nordic `åäöÅÄÖ` preservation |
| **Deterministic Key Hash** (`SHA-256`) | 1,707,089 ops/sec | **2,731,469 ops/sec** | **1.6×** | In-register SHA-256 computation |
| **Sequence Alignment** (`lcs_length`) | 60,203 ops/sec | **646,410 ops/sec** | **10.7×** | Single-vector DP buffer allocation for track sequence validation |
| **Graph Clustering** (`IntUnionFind`) | 0.765s / 10k nodes | **0.028s / 10k nodes** | **27.7×** | Flat-array contiguous memory, path compression, union-by-rank |
| **Streaming MARCXML SAX Parser** | 109,317 recs/sec | **267,515 recs/sec** | **2.4×** | Event-driven SAX (`quick-xml`), ~70% lower memory footprint than `lxml` DOM |

---

## 6. Extending the Codebase

### 6.1 Adding a New Rust Normalizer

1. **Implement Core Logic**:
   Write a pure Rust function and unit tests in `crates/reconciliator_core/src/normalizers/<module>.rs`.
2. **Export to PyO3**:
   Annotate the function with `#[pyfunction]` in `crates/reconciliator_core/src/normalizers/mod.rs` and register it in `register_submodule`.
3. **Add Type Stubs**:
   Declare the function signature in `src/reconciliator/_core.pyi` under `class normalizers:`.
4. **Wire Python Module**:
   Import and delegate directly to it in `src/reconciliator/normalizers/`.
5. **Add Integration Tests**:
   Add test assertions in `tests/test_rust_core.py` and `tests/test_normalizers.py`.

### 6.2 Working with the LLM Judge & Reranking Architecture

When working with or extending the candidate evaluation and reranking pipeline (`src/reconciliator/rerank/` and `src/reconciliator/judge/`):
- **Consult the Architectural Specifications**: Review [`docs/two_stage_llm_judge_architecture.md`](two_stage_llm_judge_architecture.md) (which details the unified two-stage resolution pipeline, target-record micro-batching, and KV-branching judge) and [`docs/cascading_dual_route_reranker_architecture.md`](cascading_dual_route_reranker_architecture.md).
- **1D Dense Vectors vs. KV Cache Suffixes**:
  - The **1D dense vectors** stored in the database index (FAISS/USearch) are used *only* for coarse vector retrieval in Stage 1.
  - The **LLM judge** evaluates candidates via text tokens appended to the frozen target prompt KV cache.
  - *Never attempt to pass 1D dense vectors directly as candidate suffixes into the LLM judge*. A single 1D vector loses token-level distinctions (volumes, years, editions) and causes distribution collapse in causal attention layers. See Section 1.2 of [`docs/two_stage_llm_judge_architecture.md`](two_stage_llm_judge_architecture.md) for full details.

### 6.3 Contributing Checklist

Before submitting a Pull Request:

- [ ] `cargo test` passes cleanly with 0 failures.
- [ ] `cargo clippy -- -D warnings` reports 0 warnings.
- [ ] `cargo fmt --check` passes.
- [ ] `maturin develop --release` builds cleanly.
- [ ] `pytest tests/test_rust_core.py tests/test_normalizers.py` passes.
- [ ] `ruff check` and `ruff format --check` pass on modified Python files.
- [ ] Any new PyO3 functions or classes have matching signatures in `src/reconciliator/_core.pyi`.


