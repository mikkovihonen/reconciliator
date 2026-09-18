# Operational Runbook: Reconciliator

`reconciliator` is a high-performance, offline-first bibliographic authority reconciliation and URI enrichment engine designed for Finnish cataloging ecosystems (**Melinda**, **Fennica**, **BIBRA**, **FINTO**).

This runbook provides complete operational guidance for system administrators and cataloging engineers: data prerequisites, configuration reference, CLI commands with realistic examples, deployment workflows, and troubleshooting procedures.

---

## Table of Contents

1. [Architecture & Storage Prerequisites](#1-architecture--storage-prerequisites)
   - [Storage Components](#storage-components)
   - [Hardware & Acceleration](#hardware--acceleration)
   - [Vector Index IPC Architecture & Singleton Daemon](#vector-index-ipc-architecture--singleton-daemon)
   - [Architectural Synthesis: Coupled Work Matching & Guarded Agent Fallback](#architectural-synthesis-coupled-work-matching--guarded-agent-fallback)
   - [External Data Dumps & Upstream Sources](#external-data-dumps--upstream-sources)
2. [Complete Configuration Surface (`config.yaml`)](#2-complete-configuration-surface-configyaml)
   - [Storage Section (`storage`)](#storage-section-storage)
   - [Embedding Section (`embedding`)](#embedding-section-embedding)
   - [Skosmos Downloader Section (`download_skosmos`)](#skosmos-downloader-section-download_skosmos)
   - [Value Vocabularies Ingestion Section (`ingest_vocab`)](#value-vocabularies-ingestion-section-ingest_vocab)
   - [OAI-PMH, ResourceSync & File Ingestion Section (`ingest_oai`)](#oai-pmh-resourcesync--file-ingestion-section-ingest_oai)
   - [Wikidata Ingestion Section (`ingest_wikidata`)](#wikidata-ingestion-section-ingest_wikidata)
   - [Reconciliation Section (`reconcile`)](#reconciliation-section-reconcile)
   - [Telemetry Section (`telemetry`)](#telemetry-section-telemetry)
   - [Normalization Section (`normalization`)](#normalization-section-normalization)
   - [Modular Source Configuration Files](#modular-source-configuration-files)
   - [Production Example Configurations](#production-example-configurations)
3. [CLI Command Reference](#3-cli-command-reference)
   - [`reconciliator download-skosmos`](#reconciliator-download-skosmos)
   - [`reconciliator ingest-vocab`](#reconciliator-ingest-vocab)
   - [`reconciliator ingest-oai`](#reconciliator-ingest-oai)
   - [`reconciliator ingest-wikidata`](#reconciliator-ingest-wikidata)
   - [`reconciliator build-agent-index`](#reconciliator-build-agent-index)
   - [`reconciliator reconcile`](#reconciliator-reconcile)
   - [`reconciliator run`](#reconciliator-run)
   - [`reconciliator status`](#reconciliator-status)
   - [`reconciliator daemon`](#reconciliator-daemon)
   - [`reconciliator prune`](#reconciliator-prune)
   - [`reconciliator reset`](#reconciliator-reset)
4. [Standard Operational Workflows](#4-standard-operational-workflows)
   - [Workflow A: Cold-Start Bootstrapping](#workflow-a-cold-start-bootstrapping)
   - [Workflow B: Batch Production Reconciliation](#workflow-b-batch-production-reconciliation)
   - [Workflow C: Air-Gapped / Zero-Network Execution](#workflow-c-air-gapped--zero-network-execution)
   - [Workflow D: Quality Audit & Discrepancy Review](#workflow-d-quality-audit--discrepancy-review)
   - [Workflow E: Telemetry Maintenance & Monitoring Database Pruning](#workflow-e-telemetry-maintenance--monitoring-database-pruning)
5. [Diagnostics & Troubleshooting](#5-diagnostics--troubleshooting)

---

## 1. Architecture & Storage Prerequisites

### Storage Components

`reconciliator` operates strictly offline during reconciliation, persisting relational metadata, WEMI graphs, and clusters in **PostgreSQL** (with `pg_trgm` and full-text search extensions), alongside local memory-mapped vector index files:

| Component | Storage Engine | Purpose | Typical Size |
|---|---|---|---|
| PostgreSQL Authority DB | PostgreSQL 15+ | Normalized authority records (Melinda / Asteri, KANTO, Wikidata, ISNI, VIAF), 4-tier WEMI repository (`works`, `expressions`, `manifestations`, `items`, `work_relationships`, `work_consolidations`), polymorphic linkages (`identifiers`, `entity_keys`, `contributions`), and value vocabularies (`concepts`, `concept_labels`, `relator_terms`, `entity_subjects`). | 1 GB – 20 GB |
| PostgreSQL Corpus Subsystem | PostgreSQL 15+ | Staging tables for target records, multiplex intra-corpus clusters (`corpus_work_clusters`, `corpus_expression_clusters`, `corpus_manifestation_clusters`), cross-layer linkages (`corpus_linkages`), composite bindings (`sammelband_bindings`), and `harvest_state`. | 500 MB – 10 GB |
| `data/work.index` | USearch / FAISS / Torch | Dense vector index storing conceptual Work-level embedding vectors for title, author, and subject matching. | 100 MB – 2 GB |
| `data/manifestation.index`| USearch / FAISS / Torch | Dense vector index storing Manifestation-level embedding vectors (publication, publisher, carrier, edition). | 100 MB – 2 GB |
| `data/agent.index` | USearch / FAISS / Torch | Dense vector index storing essential agent representations for Tier 3 guarded standalone agent fallback. | 50 MB – 500 MB |
| Telemetry & State | PostgreSQL / Memory | Real-time task progress, stages, processed byte/record counters, and heartbeat telemetry (`task_progress`, `db_metadata`). | < 10 MB |

### Hardware & Acceleration

- **CPU**: Modern multi-core x86_64 or ARM64 processor (8+ cores recommended for multi-worker ingestion and reconciliation).
- **RAM**: Minimum 8 GB (16 GB or 32 GB recommended when handling full national bibliography indexes).
- **Disk**: NVMe SSD strongly recommended for PostgreSQL WAL, vector indexing, and memory-mapped file throughput.
- **Hardware Acceleration (`device`)**:
  - `auto`: Automatically picks CUDA if available, then Apple Silicon MPS (Metal Performance Shaders), otherwise CPU.
  - `cuda`: NVIDIA GPU with PyTorch CUDA runtime.
  - `mps`: Apple Silicon GPU (M1/M2/M3/M4) via Metal.
  - `cpu`: Pure CPU execution.

### Vector Index IPC Architecture & Singleton Daemon

In high-throughput ingestion and multi-worker reconciliation pipelines, multiple processes need access to vector search indices (`authority.index`, `work.index`, `expression.index`, `manifestation.index`). However, direct concurrent multi-process writes to FAISS or Torch binary files can cause index corruption or lock contention. Furthermore, on macOS (Apple Silicon MPS), initializing separate PyTorch GPU contexts in multiple parallel processes consumes excessive unified memory and can lead to Metal context allocation failures.

To address these challenges, `reconciliator` provides a **Singleton Vector Daemon** architecture:

```
┌─────────────────────────┐     ┌─────────────────────────┐
│  Ingestion Worker (CLI) │     │ Reconciliation Pipeline │
└────────────┬────────────┘     └────────────┬────────────┘
             │                               │
             │   AF_UNIX Domain Socket IPC   │
             │   (Zero-Copy Binary Protocol) │
             ▼                               ▼
┌─────────────────────────────────────────────────────────┐
│               reconciliator vector daemon               │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │ IndexPoolManager (Per-Index Threading Locks)       │  │
│  │  • authority.index    • work.index                │  │
│  │  • expression.index   • manifestation.index       │  │
│  └───────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────┐  │
│  │ Single Vector Engine Context (USearch / MPS Torch │  │
│  │ / FAISS Shared Safely Across All Clients)         │  │
│  └───────────────────────────────────────────────────┘  │
│  ┌─────────────────────────┐ ┌────────────────────────┐ │
│  │ Auto-Save Flusher (60s) │ │ Idle Watchdog (30 min) │ │
│  └─────────────────────────┘ └────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

#### Key Architecture Guarantees:
1. **Concurrency Serialization**: All index mutations (`add_vectors`, `remove_ids`, `save`, `load`) and searches are routed through `VectorDaemonServer` over a local Unix Domain Socket (`AF_UNIX`). An internal `IndexPoolManager` manages multiple indices concurrently with per-index threading locks, ensuring thread-safe reads and strictly serialized writes.
2. **macOS & Apple Silicon MPS Optimization**: Only the singleton daemon instantiates the PyTorch MPS acceleration context. Client CLI processes (such as `ingest-oai`, `ingest-wikidata`, or `reconcile`) remain lightweight and connect over the socket, eliminating multi-process GPU context duplication.
3. **macOS Socket Path Length Safety**: Darwin strictly limits `sockaddr_un.sun_path` to 104 bytes. Long repository paths can easily trigger `AF_UNIX path too long`. `reconciliator` safely hashes the workspace directory path with SHA-256 to a deterministic, short socket path (`/tmp/rec_vectord_<hash>.sock`, ~36 characters).
4. **Zero-Copy High-Performance Binary Protocol**: The IPC protocol uses a lightweight 13-byte header (`RVCD` magic byte + opcode + message ID + payload length). Vector payloads and ID arrays are streamed directly via raw memory buffers (`ndarray.tobytes()` and `np.frombuffer().copy()`), achieving sub-millisecond query latencies and tens of thousands of vector insertions per second.
5. **Zero-Race Atomic Autostart**: When configured with `auto_start: true`, clients verify daemon connectivity on demand. If the daemon is not running, the client acquires an atomic advisory lock (`fcntl.flock` on `.lock`) and spawns the daemon process in the background. Other concurrent processes block briefly on the lock until the daemon socket is active, eliminating startup races.
6. **Hybrid Persistence & Periodic Flush**: The daemon runs an asynchronous flusher thread that automatically saves dirty indices to disk at configurable intervals (default: every 60 seconds), while also supporting immediate synchronous disk flushes on explicit client `save()` requests.
7. **Zero-Copy Disk-Mapped Vector Search (USearch)**: When configured with `backend: "usearch"` and `mmap: true`, the index operates via OS memory mapping (`view=True`). Only actively traversed graph nodes and vector candidates are paged from NVMe SSD into RAM on demand. This keeps resident process RAM under 100 MB even when searching multi-million entity datasets (such as Melinda 3M and Wikidata), eliminating memory exhaustion without sacrificing search throughput.
8. **Resource Reclamation via Idle Watchdog**: To prevent unmanaged resource consumption on local developer workstations or shared servers, the daemon monitors client inactivity. After a configurable idle period (default: 30 minutes / `1800s` without client requests), the daemon automatically executes a clean flush, removes the socket file, and terminates cleanly.

### Architectural Synthesis: Coupled Work Matching & Guarded Agent Fallback

A foundational challenge in bibliographic authority reconciliation is balancing **false positive prevention** with **recall across rare and uncataloged works**:
- Pure agent-only vector matching suffers from severe false positive risks caused by homonyms (e.g. common Finnish names like *"Matti Virtanen"* or *"Juha Korhonen"*).
- Pure work-level matching guarantees zero false positives for cataloged publications, but fails to enrich authors on rare pamphlets, self-published titles, uncataloged periodicals, or newly published books absent from the national bibliography.

`reconciliator` synthesizes both paradigms through a 3-tier matching funnel:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Incoming Target Record                          │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                     ┌─────────────▼─────────────┐
                     │ Tier 1: Exact ID Lookup   ├─► Exact Match Found (ISBN/ISSN/URN)
                     │ (AuthorityStore/FTS5)     │   Transfer Authority URIs directly
                     └─────────────┬─────────────┘
                                   │ (No exact ID match)
                     ┌─────────────▼─────────────┐
                     │ Tier 2: Coupled Work Match├─► Dense Semantic Match Found
                     │ Title + Author + Subjects │   Align target agents & transfer URIs
                     │ in authority.index / work │
                     └─────────────┬─────────────┘
                                   │ (Unmatched book OR unlinked agents)
                     ┌─────────────▼─────────────┐
                     │ Tier 3: Standalone Agent  │
                     │ Fallback (agent.index)    │
                     └─────────────┬─────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   Dense Vector Candidate Retrieval           PostgreSQL Rich Metadata Hydration
   (Top-k via agent.index, cosine ≥ 0.90)    (Full dates, occupations, affiliations)
              │                                         │
              └────────────────────┬────────────────────┘
                                   │
                                   ▼
              ┌─────────────────────────────────────────┐
              │       Dynamic Selective Guards          │
              │  1. Date Guard (conflicting life dates) │
              │  2. Chronological Sanity Window (age/post)
              │  3. Qualifier Guard (occupation match)  │
              │  4. Strict Homonym Ambiguity Guard      │
              └────────────────    ┬────────────────────┘
                                   │
                     ┌─────────────▼─────────────┐
                     │ Resolved Candidate Agent  ├─► Enrich Agent ($0/$1 or owl:sameAs)
                     │ (All guards satisfied)    │
                     └───────────────────────────┘
```

#### "Store Everything in DB, Embed Essentials" (Two-Tier Hybrid Architecture)

Dense vector models experience severe semantic dilution when embedding exhaustive biographical text (such as long lists of Wikipedia categories, corporate affiliations, and multiple career phases). Furthermore, prolific authors can have dozens of occupations and affiliations across hundreds of publications.

To achieve optimal vector discriminatory power without losing rich contextual data, Reconciliator implements a **Two-Tier Hybrid Architecture**:

1. **Dense Index Embedding (Essentials Only, ~30–50 tokens)**:
   The `agent_serialization.j2` template serializes only core distinguishing biographical fields:
   `Nimi: {{ name }} [Sukunimi: {{ surname }}] [Tarkenne: {{ fuller_name }}] | Vuodet: {{ dates }}`.
   This guarantees compact, high-dimensional vector separation without candidate retrieval drop-off caused by volatile career qualifiers.
2. **Tier 1: Intrinsic Agent Footprint (In-Memory, Decoupled Relational Entities)**:
   Stored in relational tables (`agent_attributes`, `entity_subjects`, `contributions`, `identifiers`) and hydrated on demand:
   - **`qualifiers`**: Titles, occupations, and professions from MARC repeatable `$c` and Wikidata `P106` stored in `agent_attributes`.
   - **`affiliations`**: Universities, research bodies, and corporate employers from MARC repeatable `$u` and Wikidata `P108/P1416` in `agent_relationships`.
   - **`roles`**: Contributor functions from MARC repeatable `$e` and `$4` normalized to canonical 3-letter MARC relator codes in `contributions`.
   - **`languages`**: Language footprints from MARC `008` (chars 35-37), `041$a/$h`, and Wikidata `P1412`.
   - **`subjects`**: Canonical subject and genre concept IDs matching supported vocabularies configured in YAML, linked via `entity_subjects`.
   - **`orcid`: `Optional[str]`**: Persistent academic identifier from MARC `$1` (`https://orcid.org/...`) or Wikidata `P496` in `identifiers`.
   - **`variant_names: list[str]`**: Alternative spellings, pseudonyms, and maiden names in `agent_attributes` (`attribute_type="variant_name"`).
   - **Dates**: Structured `birth_year`, `death_year`, and natural language `dates`.
3. **Tier 2: Relational Deep Disambiguation (On-Demand PostgreSQL Graph Queries)**:
   Queried from relational tables in PostgreSQL via `WEMIRepository` *only when 2+ candidates match with identical names and close dense scores*:
   - **Co-author Graph Overlap**: Checks if any candidate co-authored works in `contributions` with other creators on the target record.
   - **Publisher Overlap**: Checks if candidate's publications in `manifestations` match the target record's publisher (`260/264$b`).
   - **Scheme-Agnostic Classification Overlap**: Checks if candidate's publications share classification notations across any scheme (UDC `080`, DDC `082`, YKL/SAB `084`, LCC `050`), formatted as `"{scheme}:{code}"` with hierarchical prefix matching via `entity_subjects`.

#### Dynamic Selective Guards

During Tier 3 standalone agent fallback, candidate agents retrieved from `agent.index` are evaluated through selective dynamic guards before acceptance:

1. **ORCID Guard (`verify_orcid`)**:
   Verifies persistent academic identifiers. If both target and candidate specify an ORCID, an exact match confirms the candidate immediately, while a mismatch immediately disqualifies the candidate.
2. **Life Dates Guard (`verify_life_dates`)**:
   Verifies chronological consistency (birth and death years). Flags and disqualifies impossible life spans ($Y_{\text{death}} - Y_{\text{birth}} > 120$), chronological inversions ($Y_{\text{birth}} > Y_{\text{death}}$), or conflicts between target and candidate years.
3. **Publication Chronology Guard (`verify_publication_chronology`)**:
   - **Minimum Author Age**: Enforces $Y_{\text{pub}} \ge Y_{\text{birth}} + \text{chronology\_min\_author\_age}$ (default: 12). Disqualifies candidates if the target work was published before the author reached writing age.
   - **Maximum Posthumous Publication Limit**: Enforces $Y_{\text{pub}} - Y_{\text{death}} \le \text{chronology\_max\_posthumous\_years}$ (default: 70). Rejects works published centuries after an author's death unless cataloged as posthumous editions.
4. **Qualifier Distribution Guard (`verify_qualifiers`)**:
   Verifies overlap between empirical qualifier distributions (e.g. `{"professori": 5, "lääkäri": 1}` vs `{"lääkäri": 10}`). Evaluates exact and fuzzy similarity ($\ge 80\%$). If both sides have qualifiers and their distributions are completely disjoint, the candidate is disqualified.
5. **Affiliation Distribution Guard (`verify_affiliations`)**:
   Verifies overlap between institutional affiliation distributions (e.g. `{"Helsingin yliopisto": 3}`). Disqualifies candidates with contradictory, disjoint affiliations when both sides provide data.
6. **Subject Overlap Guard (`verify_subjects`)**:
   Verifies overlap between empirical subject/genre concept ID distributions (e.g. `{"yso:p2591": 5}` vs `{"yso:p2591": 2}`). Evaluates exact concept ID keys, cross-vocabulary concept ID tokens (e.g. `p2591` across `kauno:p2591` and `yso:p2591`), and legacy mapped concept URIs via `vocab_store`. If both sides provide subjects and their distributions are completely disjoint with zero semantic overlap, the candidate is disqualified.
7. **Strict Homonym Ambiguity Guard (`check_homonym_ambiguity`)**:
   Detects if 2+ candidate agents fall within a narrow score margin (`score_margin: 0.03`) of the top candidate.
   - **Tier 1 Resolution**: Evaluates distinguishing dates, ORCID, qualifier counts, affiliation counts, language counts, and subject counts in-memory.
   - **Tier 2 Resolution**: If still tied, queries PostgreSQL for co-author graph overlap, publisher overlap, and scheme-agnostic classification overlap.
   - If ambiguity cannot be resolved, the match is rejected as an ambiguous homonym to prevent false guesses.

### External Data Dumps & Upstream Sources

To operate the solution, the following data inputs can be provided or downloaded:

#### 1. FINTO Value Vocabularies & Ontologies
- **Formats**: SKOS Turtle (`.ttl`) or RDF/XML.
- **Online Sources**: Finto Skosmos REST API (`https://api.finto.fi/rest/v1`) or canonical static dumps (`https://api.finto.fi/download/...`).
- **Required Vocabularies**:
  - `yso`: General Finnish Ontology (Yleinen suomalainen ontologia)
  - `yso-paikat`: Geographic places ontology (maps to MARC 651)
  - `slm`: Genre and form ontology (Suomalaiset lajityyppi- ja muotosanasto, maps to MARC 655)
  - `kauno`: Fiction genre ontology
  - `muso`: Music ontology
  - `seko`: Ensemble medium of performance vocabulary (maps to MARC 382)
  - Legacy thesauri: `ysa`, `allars`, `kaunokki`, `musa`
- **Acquisition**: Can be fetched automatically via `reconciliator download-skosmos` (from Finto or any Skosmos instance).

#### 2. Melinda / Fennica Authority Records
- **Formats**: MARCXML collections (`.xml`).
- **Online Source**: Kansalliskirjasto OAI-PMH API (`https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib`), metadataPrefix `marc21`.
- **Offline Source**: Pre-harvested MARCXML export files containing Asteri / KANTO authority records.

#### 3. Wikidata Entity Dumps (Optional)
- **Formats**: Compressed JSON lines dump (`latest-all.json.gz`).
- **Online Source**: Wikimedia Dumps (`https://dumps.wikimedia.org/wikidatawiki/entities/latest-all.json.gz`).
- **Entities Filtered**: Entities with Finnish library relevance, such as claims with property `P2347` (YSO ID), `P214` (VIAF), `P227` (GND), `P213` (ISNI).

#### 4. Sentence Transformer Embedding Model
- **Default**: `TurkuNLP/sbert-cased-finnish-paraphrase` (128-dimensional dense representation fine-tuned for Finnish paraphrasing and semantic similarity).
- **Acquisition**: Cached automatically by `sentence-transformers` on first run in `~/.cache/huggingface/hub`. In air-gapped environments, copy the local model directory and set `model_name: "/path/to/local/model"`.

---

## 2. Complete Configuration Surface (`config.yaml`)

`reconciliator` is configured via a single declarative YAML file. All relative file paths are automatically resolved relative to the directory containing the configuration file.

### Storage Section (`storage`)

Defines storage settings for PostgreSQL and the vector indexing engine.

```yaml
storage:
  data_dir: "data"                           # Base directory for vector index files
  database_url: null                         # Explicit PostgreSQL URL (defaults to postgres config)
  postgres:
    host: "localhost"
    port: 5432
    database: "reconciliator"
    user: "postgres"
    password: "password"
  work_index: "data/work.index"              # Work-level vector index path
  manifestation_index: "data/manifestation.index" # Manifestation-level vector index path
  agent_index: "data/agent.index"            # Standalone agent vector index path (Tier 3 fallback)
  vector_engine:
    mode: "daemon"                           # "daemon" (safe multi-process singleton via IPC) | "embedded" (direct in-process)
    backend: "auto"                          # "auto" | "faiss" | "torch" | "usearch" ("auto" selects torch on MPS/CUDA, faiss on CPU)
    device: "auto"                           # "auto" | "mps" | "cuda" | "cpu" (for torch backend)
    threads: null                            # Thread count override for FAISS OpenMP (defaults to 1 on macOS)
    daemon:
      enabled: true                          # Toggle singleton background service
      socket_path: null                      # Socket path override (null defaults to /tmp/rec_vectord_<hash>.sock)
      auto_start: true                       # Automatically spawn daemon process if not running
      idle_timeout_seconds: 1800             # Auto-shutdown after 30 mins idle (null = run forever)
      auto_save_interval_seconds: 60         # Periodic background flush interval for dirty indices
      heartbeat_interval_seconds: 2.0        # Heartbeat frequency for telemetry
      max_batch_bytes: 67108864              # Max batch byte size before IPC frame chunking (64 MB)
    usearch:
      dtype: "f16"                           # "f32" | "f16" | "i8" | "b1" ("i8" cuts RAM/disk by 75%, "f16" by 50%)
      metric: "cos"                          # "cos" | "ip" | "l2"
      connectivity: 16                       # HNSW graph connectivity M (higher = better recall, slightly larger file)
      expansion_add: 128                     # HNSW construction search depth (efConstruction)
      expansion_search: 64                   # HNSW query search depth (efSearch)
      mmap: true                             # Zero-copy memory mapping via OS page cache for search (near-zero resident RAM)
    torch:
      dtype: "float32"                       # "float32" | "float16" | "bfloat16" ("float16" cuts RAM by 50% on MPS/CUDA)
      mmap: false                            # Zero-copy memory mapping via safetensors for search/reconciliation
      chunk_size: 50000                      # Batch size for matrix multiplication on GPU/MPS
      initial_capacity: 10000                # Pre-allocated memory slots to prevent repeated buffer resizing
    faiss:
      index_type: "flat"                     # "flat" (exact inner product) | "sq8" (8-bit scalar quantization)
      mmap: false                            # Memory-map FAISS index directly from disk via OS page cache
      nprobe: 16                             # Voronoi cells searched per query (for IVF indexes)
      nlist: 2048                            # Number of Voronoi partitions (for IVF indexes)
```

| `data_dir` | `Path` | `data` | Base directory for vector index files and daemon sockets. |
| `database_url` | `Optional[str]` | `None` | PostgreSQL database connection URL. |
| `postgres` | `PostgresConfig` | default | PostgreSQL connection settings (host, port, dbname, user, password). |
| `work_index` | `Path` | `data/work.index` | Path to Work-level dense vector index. |
| `manifestation_index`| `Path` | `data/manifestation.index` | Path to Manifestation-level dense vector index. |
| `agent_index` | `Path` | `data/agent.index` | Path to standalone Agent vector index (Tier 3 fallback). |
| `vector_engine.mode` | `Literal` | `daemon` | Execution mode: `daemon` (concurrent-safe singleton via IPC socket) or `embedded` (direct in-process). |
| `vector_engine.backend` | `Literal` | `auto` | Vector search engine backend: `auto` (detects hardware acceleration), `faiss`, `torch`, or `usearch`. |
| `vector_engine.device` | `Literal` | `auto` | Computation device: `auto`, `cpu`, `cuda`, or `mps` (Apple Silicon Metal). |
| `vector_engine.threads` | `Optional[int]` | `None` | Max threads for vector operations (`None` defaults to system core count or 1 on macOS). |
| `vector_engine.daemon.enabled` | `bool` | `true` | When true and mode is `daemon`, enables the singleton vector daemon. |
| `vector_engine.daemon.socket_path` | `Optional[Path]` | `None` | Custom path to Unix domain socket. If `None`, defaults to `/tmp/rec_vectord_<hash>.sock`. |
| `vector_engine.daemon.auto_start` | `bool` | `true` | Automatically spawn background daemon on demand if not running. |
| `vector_engine.daemon.idle_timeout_seconds` | `Optional[int]` | `1800` | Auto-shutdown watchdog after idle period (seconds; 1800 = 30 min; `None` disables). |
| `vector_engine.daemon.auto_save_interval_seconds` | `int` | `60` | Periodic background persistence interval for dirty indices (seconds). |
| `vector_engine.daemon.heartbeat_interval_seconds` | `float` | `2.0` | Heartbeat interval for recording daemon telemetry in `progress.db`. |
| `vector_engine.daemon.max_batch_bytes` | `int` | `67108864` | IPC frame streaming limit before chunking (bytes; 64 MB). |
| `vector_engine.usearch.dtype` | `Literal` | `f16` | USearch scalar precision: `f32`, `f16` (50% size reduction), `i8` (75% size reduction), or `b1`. |
| `vector_engine.usearch.metric` | `Literal` | `cos` | Distance metric: `cos` (cosine similarity), `ip` (inner product), or `l2` (Euclidean). |
| `vector_engine.usearch.connectivity` | `int` | `16` | HNSW graph connectivity degree M (higher = better recall, slightly larger file). |
| `vector_engine.usearch.expansion_add` | `int` | `128` | Search depth during graph construction (`efConstruction`). |
| `vector_engine.usearch.expansion_search` | `int` | `64` | Search depth during nearest neighbor query (`efSearch`). |
| `vector_engine.usearch.mmap` | `bool` | `true` | Zero-copy memory mapping (`view=True`) directly from SSD via OS page cache (resident RAM < 100 MB). |
| `vector_engine.torch.dtype` | `Literal` | `float32` | Torch tensor precision: `float32`, `float16` (50% RAM savings), or `bfloat16`. |
| `vector_engine.torch.mmap` | `bool` | `false` | Memory-map tensor vectors via safetensors for search/reconciliation. |
| `vector_engine.torch.chunk_size` | `int` | `50000` | Chunk size for batched matrix cosine similarity computation. |
| `vector_engine.torch.initial_capacity` | `int` | `10000` | Initial buffer capacity allocation for dynamic vector arrays. |
| `vector_engine.faiss.index_type` | `Literal` | `flat` | FAISS index structure: `flat` (IndexFlatIP) or `sq8` (IndexScalarQuantizer SQ8). |
| `vector_engine.faiss.mmap` | `bool` | `false` | Memory-map FAISS index directly from disk via OS page cache. |
| `vector_engine.faiss.nprobe` | `int` | `16` | Voronoi cells searched per query (for IVF indexes). |
| `vector_engine.faiss.nlist` | `int` | `2048` | Number of Voronoi centroids (for IVF indexes). |

---

### Embedding Section (`embedding`)

Configures sentence transformer models, serialization templates, and hardware accelerators.

```yaml
embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  template_path: "reconciliator/templates/default_serialization.j2"
  device: "auto"        # "auto" | "cuda" | "mps" | "cpu"
  batch_size: 64        # Inference batch size (> 0)
  force_template: false # When true, permits reconciliation even if template hash differs
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model_name` | `str` | `TurkuNLP/sbert-cased-finnish-paraphrase` | Hugging Face model ID or local directory path. |
| `template_path` | `Path` | Bundled Jinja2 template | Path to Jinja2 template serializing records into dense prompts. |
| `device` | `Literal` | `auto` | Acceleration device: `auto`, `cuda`, `mps`, or `cpu`. |
| `batch_size` | `int` | `64` | Embedding inference batch size. Must be greater than 0. |
| `force_template` | `bool` | `false` | Bypasses template integrity check when index metadata differs. |

---

### Skosmos Downloader Section (`download_skosmos`)

Configures automated multi-source downloading of value vocabularies and ontologies from Skosmos instances (e.g. Finto, AGROVOC, UNESCO).

```yaml
download_skosmos:
  finto:
    enabled: true
    api_url: "https://api.finto.fi/rest/v1"
    output_dir: "data/finto"
    vocabularies:
      - "yso"
      - "yso-paikat"
      - "slm"
      - "kauno"
      - "muso"
      - "seko"
    dump_urls: {}             # Optional custom map of vocab ID to static archive URL
    format: "text/turtle"
    timeout_seconds: 120.0
    overwrite: false
    normalization: null       # Optional VocabNormalizationConfig override
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Whether this Skosmos source is active during batch downloads (`--all`). |
| `api_url` | `str` | `https://api.finto.fi/rest/v1` | Base URL of the Skosmos REST API v1 endpoint. |
| `output_dir` | `Path` | `data/finto` | Target directory where downloaded `.ttl` dumps are stored. |
| `vocabularies` | `list[str]` | `['yso', 'yso-paikat', 'slm', 'kauno', 'muso', 'seko']` | Default list of vocabulary IDs to download. |
| `dump_urls` | `dict[str, str]` | `{}` | Direct archive URLs bypassing Skosmos REST endpoint for large ontologies. |
| `format` | `str` | `text/turtle` | Dump format mime-type. |
| `timeout_seconds`| `float`| `120.0` | HTTP request stream timeout in seconds. |
| `overwrite` | `bool` | `false` | When false, skips files that already exist on disk. |
| `normalization` | `Optional[VocabNormalizationConfig]` | `None` | Local vocabulary normalization overrides for this source. |

---

### Value Vocabularies Ingestion Section (`ingest_vocab`)

Specifies input files or directories for parsing value vocabularies into `vocab.db`.

```yaml
ingest_vocab:
  input_path: "data/finto"  # Single .ttl file or directory containing multiple .ttl files
  vocabularies:
    - "yso"
    - "yso-paikat"
    - "kauno"
    - "muso"
    - "slm"
    - "seko"
  legacy_thesauri:
    - "ysa"
    - "allars"
    - "kaunokki"
    - "musa"
  pref_label_languages: null  # null, "*", or "all" to ingest all languages without explicit lists; or an explicit list (e.g. ["fi", "sv", "en"])
  alt_label_languages: null   # null, "*", or "all" to ingest all alternative labels; or an explicit list
  normalization: null         # Optional VocabNormalizationConfig override
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `input_path` | `Path` | `data/finto` | File path to a single `.ttl` dump or directory of `.ttl` files. |
| `vocabularies` | `list[str]` | `['yso', 'yso-paikat', ...]` | Target modern ontologies to parse. |
| `legacy_thesauri` | `list[str]` | `['ysa', 'allars', ...]` | Legacy thesauri with mapping tables (`skos:exactMatch`). |
| `pref_label_languages` | `Optional[list[str] \| str]` | `None` | Preferred label languages to ingest (`null`, `"*"`, `"all"` for all languages without explicit lists; or an explicit list like `["fi", "sv", "en"]`). |
| `alt_label_languages` | `Optional[list[str] \| str]` | `None` | Alternative label languages to ingest (`null`, `"*"`, `"all"` for all languages; or an explicit list). |
| `normalization` | `Optional[VocabNormalizationConfig]` | `None` | Optional vocabulary normalization overrides during ingestion. |

---

### OAI-PMH, ResourceSync & File Ingestion Section (`ingest_oai`)

Configures harvesting or reading authority records from one or multiple OAI-PMH endpoints, ResourceSync (ANSI/NISO Z39.99) repositories, or local MARCXML/BIBFRAME files, routing to either the WEMI authority repository or the target staging corpus.

```yaml
ingest_oai:
  melinda:
    enabled: true
    source_type: "oai"        # "oai" (OAI-PMH harvest), "resourcesync", or "file" (local file)
    format: "marcxml"         # "marcxml" | "bibframe"
    schema_profile: null      # Optional BIBFRAME profile (e.g. "bffi_1")
    input_path: null          # Path to local file if source_type is "file"
    oai_url: "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib"
    resourcesync_url: null    # Endpoint URL if source_type is "resourcesync"
    metadata_prefix: "marc21"
    set_spec: null            # Optional OAI set specifier
    id_source: "marc_001"     # "marc_001" | "oai_identifier" | "auto"
    include_items: false      # Extract and link physical item-level holdings
    target_database: "authority" # "authority" (WEMI authority.db), "corpus" (reconcile_corpus.db), or custom Path
    workers: 8                # Number of parallel workers for chunk harvesting (recommended: 8-16)
    normalization_workers: 4  # Parallel worker threads for normalization & vocab folding (default: 4)
    queue_size: null          # Max capacity for raw harvested record queue (null = auto: max(batch_size * workers * 2, 2000))
    batch_queue_size: 4       # Max capacity for pre-normalized batches queued for embedding/indexing (default: 4)
    checkpoint_interval: 50000 # Save vector index snapshot every N indexed records (default: 50000)
    request_delay: 0.1        # Rate limit delay between requests per worker in seconds
    from_date: null           # "YYYY-MM-DD" selective harvest start (auto-discovered via Identify if null)
    until_date: null          # "YYYY-MM-DD" selective harvest end (defaults to current date if null)
    slice_days: 30            # Temporal slice window per worker in days
    batch_size: 500           # Authority database insertion batch size
    timeout: 60.0
    normalization: null       # Optional source-specific RecordNormalizationConfig
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `source_type` | `Literal` | `oai` | Ingestion source: `oai` for live OAI-PMH, `resourcesync` for ResourceSync, or `file` for local files. |
| `format` | `Literal` | `marcxml` | Ingestion record format: `marcxml` or `bibframe`. |
| `schema_profile` | `Optional[str]` | `None` | Declarative BIBFRAME profile name (e.g. `bffi_1`) if `format: "bibframe"`. |
| `input_path` | `Path` | `None` | Path to local MARCXML/authority file if `source_type` is `file`. |
| `oai_url` | `str` | `https://.../bib` | Endpoint URL for OAI-PMH service. |
| `resourcesync_url` | `str` | `None` | Endpoint URL for ResourceSync capability list or resource list. |
| `metadata_prefix`| `str` | `marc21` | Metadata format prefix. |
| `set_spec` | `str` | `None` | Optional OAI-PMH set filter. |
| `id_source` | `Literal` | `marc_001` | Record ID derivation: `marc_001`, `oai_identifier`, or `auto`. |
| `include_items` | `bool` | `false` | Whether to extract and index item holdings. |
| `target_database`| `Union[str, Path]` | `authority` | Target storage routing: `"authority"` (WEMIRepository), `"corpus"` (CorpusRepository), or custom DB path. |
| `workers` | `int` | `4` | Concurrency for chunked OAI-PMH harvesting. |
| `normalization_workers` | `int` | `4` | Worker threads for parallel record normalization and vocab folding. |
| `queue_size` | `Optional[int]` | `None` | Raw harvested record queue capacity (auto-sized to `max(batch_size * workers * 2, 2000)` if null). |
| `batch_queue_size` | `int` | `4` | Max ready-to-embed batches buffered in memory between producer and consumer threads. |
| `checkpoint_interval` | `int` | `50000` | Frequency (in indexed records) of saving intermediate vector index snapshots. |
| `request_delay` | `float` | `0.1` | Rate limit pause per worker thread between consecutive requests. |
| `from_date` | `str` | `None` | ISO date `YYYY-MM-DD` (auto-queries `Identify` endpoint if null). |
| `until_date` | `str` | `None` | ISO date `YYYY-MM-DD` (defaults to current UTC date if null). |
| `slice_days` | `int` | `30` | Temporal chunk partition size in days per worker. |
| `batch_size` | `int` | `500` | Database insert batch size. |
| `timeout` | `float` | `60.0` | HTTP request network timeout in seconds. |
| `normalization` | `Optional[RecordNormalizationConfig]` | `None` | Source-specific record normalization overrides. |

---

### Wikidata Ingestion Section (`ingest_wikidata`)

Configures streaming from official Wikidata JSON gzip dumps (`latest-all.json.gz`). Operates in true $O(1)$ memory using two sequential streaming passes:
1. **Pass 1 (Author Discovery)**: Scans the compressed dump and indexes author entities (humans, organizations, places) with their QID, family name, ISNI, VIAF, and Asteri ID into the PostgreSQL `agents`, `identifiers`, and `agent_attributes` tables.
2. **Pass 2 (Bibliographic Streaming & Hydration)**: Streams bibliographic records, hydra-populates author names and identifiers directly from PostgreSQL, encodes dense embeddings, and inserts them into WEMI tables (`works`, `expressions`, `manifestations`) when `extract_works: true`.

```yaml
ingest_wikidata:
  dump_path: "data/wikidata/latest-all.json.gz"
  batch_size: 1000
  extract_works: true         # True: streams entities directly into WEMI tables (works, expressions, manifestations)
  pass1_only: false           # True: scans author entities only (populates agents, identifiers, and attributes)
  normalization: null         # Optional RecordNormalizationConfig override
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dump_path` | `Path` | `data/wikidata/latest-all.json.gz` | Path to `.json.gz` or `.json` Wikidata entity dump. |
| `batch_size` | `int` | `1000` | Number of entities batched for embedding and indexing. |
| `extract_works` | `bool` | `true` | When true, populates 4-tier WEMI tables (`works`, `expressions`, `manifestations`) in PostgreSQL. |
| `pass1_only` | `bool` | `false` | When true, executes only author discovery pass 1 and halts. |
| `normalization` | `Optional[RecordNormalizationConfig]` | `None` | Optional record normalization rules for Wikidata entity processing. |

---

### Reconciliation Section (`reconcile`)

Defines target records, formats, declarative schema profiles, pluggable reranking, and report destinations.

```yaml
reconcile:
  target_file: "data/target_records.xml"
  format: "auto"               # "auto" | "marcxml" | "bibra" | "bibframe"
  schema_profile: "bffi_1"     # Optional BIBFRAME profile: "bffi_1", "bibframe_2", "bibframe_3", or custom YAML
  output_file: "data/enriched_records.xml"
  enrich: true                 # true: inject novel $0/$1 URIs or owl:sameAs; false: audit & report only
  fold_legacy: true            # true: modernize legacy thesauri headings via vocab.db
  threshold: 0.85              # Minimum acceptance score [0.0, 1.0]
  quality_brackets: null       # Optional custom brackets (e.g. [0.70, 0.85, 0.95]); if null, derived from thresholds

  agent_reports:
    detailed_file: "data/reports/agent_reconciliation_uris.tsv"
    summary_file: "data/reports/agent_reconciliation_summary.tsv"

  subject_reports:
    detailed_file: "data/reports/subject_reconciliation_uris.tsv"
    summary_file: "data/reports/subject_reconciliation_summary.tsv"

  work_reports:
    detailed_file: "data/reports/work_reconciliation.tsv"
    summary_file: "data/reports/wemi_summary.tsv"

  expression_reports:
    detailed_file: "data/reports/expression_reconciliation.tsv"
    summary_file: "data/reports/wemi_summary.tsv"

  templates:
    work: "reconciliator/templates/work_serialization.j2"
    expression: "reconciliator/templates/expression_serialization.j2"
    manifestation: "reconciliator/templates/manifestation_serialization.j2"
    agent: "reconciliator/templates/agent_serialization.j2"

  fallback_agent_reconciliation: true # Tier 3 standalone agent fallback for unlinked agents
  agent_fallback_threshold: 0.90      # Cosine similarity threshold for standalone agent fallback [0.0, 1.0]
  verify_chronology: true             # Validate publication chronology against author life dates
  chronology_min_author_age: 12       # Minimum author age at publication year (sanity lower bound)
  chronology_max_posthumous_years: 70 # Max allowable years between author death and publication
  strict_homonym_guard: true          # Reject ambiguous matches when 2+ candidates match without distinguishing dates/qualifiers

  cluster:
    enabled: true              # Intra-corpus multiplex graph clustering
    similarity_threshold: 0.88 # Global cosine similarity fallback
    max_cluster_size: 500      # Max nodes per cluster
    blocking_families:         # Candidate blocking keys
      - "work_blocking_key"
      - "series_key"
      - "music_work_key"
      - "standard_key"
    thresholds:
      work: 0.85               # Work-layer similarity threshold
      expression: 0.88         # Expression-layer similarity threshold
      manifestation: 0.92      # Manifestation-layer similarity threshold
    message_passing: true      # Cross-layer graph message passing
    reconcile_items: false     # Cluster physical items
    scoring:
      with_author:
        title: 0.45            # Title similarity weight when author is present
        author: 0.35           # Author similarity weight
        year: 0.10             # Publication year proximity weight
        type: 0.10             # Work/content type consistency weight
      without_author:
        title: 0.70            # Title weight when author is absent
        year: 0.15             # Year proximity weight
        type: 0.15             # Type consistency weight
      author_missing_similarity: 0.60    # Imputed similarity when author is missing
      year_missing_similarity: 0.80      # Imputed similarity when year is missing
      year_max_difference: 20.0          # Max year gap before similarity drops to 0.0
      cross_format_type_similarity: 0.95 # Similarity across audio/text adaptations
      default_type_similarity: 0.50      # Default similarity for mismatched formats
    music_score:
      terms: ["score", "partituuri", "nuotti", "part", "stemma"]
      standard_key_terms: ["ismn", "plate"]

  agent_clustering:
    enabled: true
    similarity_threshold: 0.85
    date_tolerance_years: 1
    major_date_discrepancy_veto_years: 2
    require_corroboration_for_date_discrepancy: true
    min_career_overlap_tolerance_years: 40
    batch_size: 1000
    token_matching_algorithm: "token_set_ratio"
    affiliation_fuzzy_threshold: 80.0
    affiliation_fuzzy_score: 0.85
    source_authority_precedence:
      - "asteri"
      - "wikidata"
      - "melinda"

  funnels:
    default_match_tier: "tier2_domain_key"
    default_confidence: 0.90
    audiobook:
      default_language: "fin"
      title_suffix: " (äänikirja)"
      undefined_language: "und"
      source_id_suffix: "_audio"
      expression_uri_prefix: "#spoken_word_"
      content_type: "spoken_word"
    recorded_sound:
      jaccard_threshold: 0.75
      max_candidates: 50
      match_tier: "tier3_tracklist_jaccard"
    notated_music:
      fallback_match_tier: "tier2_domain_key"
    patterns:
      notated_music_title: "(?i)score|partituuri|nuotti|stemma"
      podcast_title: "podcast"
      parallel_translators_signature: "(?i)parallel"
      government_valto_urn: "(?i)valto"
      exhibition_title: "näyttely"

  enrichment_toggles:
    inject_work_links: true       # Inject 758 / Work URIs
    inject_expression_links: true # Inject Expression URIs
    inject_hub_links: true        # Inject BIBFRAME Hub URIs
    inject_same_as: true          # Inject owl:sameAs (BIBFRAME)
    inject_component_links: true  # Inject 773 / host item component links
    marc_predicate_profiles:      # Target predicate formats
      - "bibframe"
      - "bffi"
      - "rda"
    marc_predicate_mode: "stacked" # "stacked" (multiple $4) or "distinct" (separate fields)

  rerank:
    enabled: true              # Toggle reranking on/off
    engine: "bm25"             # Reranking engine: "bm25", "cross_encoder", "fuzzy", or "noop"
    model_name: null           # HuggingFace model path (required when engine is "cross_encoder")
    device: "auto"             # Device for neural rerankers: "auto", "cpu", "cuda", or "mps"
    batch_size: 16             # Batch inference size for cross-encoder rerankers
    dense_weight: 0.6          # Dense semantic cosine weight [0.0, 1.0]
    rerank_weight: 0.4         # Reranking weight [0.0, 1.0] (must sum to 1.0)
    top_k: 50                  # Number of dense candidates retrieved
    short_circuit:
      enabled: true            # Fast-path shortcut for near-identical matches
      threshold: 0.98          # Score threshold bypassing reranking
    require_volume_match: true # Enforce volume/part numbering consistency ("osa 1" vs "osa 2")
    require_surname_match: true# Require at least one matching author surname
    max_year_difference: 5     # Maximum allowable publication year gap
    year_penalty_threshold: 1  # Year gap threshold before linear proximity penalties apply

  link_policy: "priority_fallback"  # "priority_fallback" | "all"
  vocabularies:
    - "KANTO"
    - "YSO"
    - "MUSO"
    - "KAUNO"
    - "SLM"
    - "SEKO"
    - "VIAF"
    - "ISNI"
    - "WIKIDATA"
  agent_priority:
    - "KANTO"
    - "ISNI"
    - "VIAF"
    - "WIKIDATA"
  subject_priority:
    - "YSO"
    - "KAUNO"
    - "MUSO"
    - "SLM"
    - "SEKO"
    - "WIKIDATA"
  supported_subject_vocabularies:
    - "yso"
    - "yso-paikat"
    - "kauno"
    - "muso"
    - "slm"
    - "seko"
    - "koko"
    - "wikidata"
  entity_vocabularies:
    - "WIKIDATA"
  vocabulary_patterns:
    KANTO: ["urn.fi/urn:nbn:fi:au:pn:", "(fi-asteri-n)", "(fi-kanto)", "asteri"]
    YSO: ["yso.fi/onto/yso/", "(fi-yso)"]
    MUSO: ["yso.fi/onto/muso/", "(fi-muso)"]
    KAUNO: ["yso.fi/onto/kauno/"]
    SLM: ["urn.fi/urn:nbn:fi:au:slm:"]
    SEKO: ["urn.fi/urn:nbn:fi:au:seko:"]
    VIAF: ["viaf.org/viaf/", "(viaf)"]
    ISNI: ["isni.org/isni/", "(isni)"]
    WIKIDATA: ["wikidata.org/entity/", "wikidata.org/wiki/"]
  normalization: null          # Optional RecordNormalizationConfig override
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `target_file` | `Path` | *Required* | Path to target records (MARCXML, BIBRA JSON/JSONL, or BIBFRAME RDF/JSON-LD). |
| `format` | `Literal` | `auto` | Bibliographic format: `auto` (extension-based), `marcxml`, `bibra`, or `bibframe`. |
| `schema_profile` | `Optional[str]` | `None` | Declarative BIBFRAME/BFFI profile name (`bffi_1`, `bibframe_2`, `bibframe_3`) or path to custom YAML. |
| `output_file` | `Path` | `None` | Optional path to write in-place enriched records (same format as input). |
| `enrich` | `bool` | `true` | When true, injects missing authority URIs (`$0`/`$1` in MARC/BIBRA, `owl:sameAs` in BIBFRAME). |
| `fold_legacy` | `bool` | `true` | When true, modernizes legacy subject thesauri (YSA, Allars, etc.) to modern ontologies. |
| `threshold` | `float` | `0.85` | Acceptance confidence threshold [0.0, 1.0]. |
| `quality_brackets` | `Optional[list[float]]` | `None` | Optional custom score cutoffs for agent summary quality report. When null, brackets are dynamically derived from `threshold` and `rerank.short_circuit.threshold`. |
| `agent_reports` | `ReportSet`| *Required* | File paths for agent detailed and summary TSV reports. |
| `subject_reports`| `ReportSet`| *Required* | File paths for subject detailed and summary TSV reports. |
| `work_reports` | `Optional[ReportSet]`| `None` | Optional file paths for WEMI Work detailed and summary TSV reports. |
| `expression_reports`| `Optional[ReportSet]`| `None` | Optional file paths for WEMI Expression detailed and summary TSV reports. |
| `templates.work` | `Path` | `work_serialization.j2` | Jinja2 template path for Work-level dense vector prompts. |
| `templates.expression` | `Path` | `expression_serialization.j2` | Jinja2 template path for Expression-level dense vector prompts. |
| `templates.manifestation` | `Path` | `manifestation_serialization.j2`| Jinja2 template path for Manifestation-level dense vector prompts. |
| `templates.agent` | `Path` | `agent_serialization.j2` | Jinja2 template path for essential Agent dense vector prompts (Tier 3 fallback). |
| `fallback_agent_reconciliation` | `bool` | `true` | Enables Tier 3 Standalone Agent Fallback for unlinked target agents using `agent.index`. |
| `agent_fallback_threshold` | `float` | `0.90` | Minimum acceptance cosine similarity threshold [0.0, 1.0] for standalone agent fallback. |
| `verify_chronology` | `bool` | `true` | Enforces publication chronology sanity checks against author birth/death years. |
| `chronology_min_author_age` | `int` | `12` | Minimum author age ($Y_{\text{pub}} - Y_{\text{birth}}$) required for publication. |
| `chronology_max_posthumous_years`| `int` | `70` | Maximum allowable posthumous years ($Y_{\text{pub}} - Y_{\text{death}}$) before rejection. |
| `strict_homonym_guard` | `bool` | `true` | When true, rejects close multi-candidate ties that lack distinguishing dates or qualifiers. |
| `cluster.enabled`| `bool` | `true` | Enables intra-corpus multiplex graph clustering prior to reconciliation. |
| `cluster.similarity_threshold` | `float` | `0.88` | Base cosine similarity threshold for clustering graph edges. |
| `cluster.max_cluster_size` | `int` | `500` | Hard threshold bounding cluster expansion. |
| `cluster.blocking_families` | `list[str]` | Standard keys | Key generators for candidate blocking partitions (`work_blocking_key`, `series_key`, etc.). |
| `cluster.thresholds.work` | `float` | `0.85` | Work-layer multiplex cosine threshold. |
| `cluster.thresholds.expression` | `float` | `0.88` | Expression-layer multiplex cosine threshold. |
| `cluster.thresholds.manifestation` | `float` | `0.92` | Manifestation-layer multiplex cosine threshold. |
| `cluster.message_passing` | `bool` | `true` | Enables hierarchical cross-layer graph message passing. |
| `cluster.reconcile_items` | `bool` | `false` | Enables physical item/holding resolution. |
| `cluster.scoring.with_author` | `dict` | standard weights | Feature weights (title, author, year, type) when author metadata is available. |
| `cluster.scoring.without_author` | `dict` | standard weights | Feature weights when author metadata is absent. |
| `cluster.scoring.year_max_difference` | `float` | `20.0` | Maximum publication year difference before similarity drops to 0.0. |
| `cluster.scoring.cross_format_type_similarity` | `float` | `0.95` | Type similarity for cross-format adaptations (audiobooks, text). |
| `cluster.music_score.terms` | `list[str]` | standard terms | Sheet music and score designation terms for expression keying. |
| `agent_clustering.enabled` | `bool` | `true` | Enables intra-corpus agent identity graph clustering. |
| `agent_clustering.similarity_threshold` | `float` | `0.85` | Graph edge acceptance threshold for agent clustering. |
| `agent_clustering.affiliation_fuzzy_threshold` | `float` | `80.0` | RapidFuzz token matching threshold for institutional affiliations. |
| `agent_clustering.affiliation_fuzzy_score` | `float` | `0.85` | Score awarded when affiliation fuzzy similarity meets the threshold. |
| `agent_clustering.source_authority_precedence` | `list[str]` | `['asteri', 'wikidata', 'melinda']` | Authority source priority for canonical identity election. |
| `funnels.default_match_tier` | `str` | `"tier2_domain_key"` | Fallback match tier label when not overridden by a spec. |
| `funnels.default_confidence` | `float` | `0.90` | Default match confidence score for domain funnels. |
| `funnels.recorded_sound.jaccard_threshold` | `float` | `0.75` | Minimum tracklist Jaccard overlap for recorded sound matching. |
| `funnels.recorded_sound.max_candidates` | `int` | `50` | Maximum candidate manifestations evaluated by tracklist index lookup. |
| `funnels.recorded_sound.match_tier` | `str` | `"tier3_tracklist_jaccard"` | Match tier label for recorded sound tracklist overlap match. |
| `funnels.audiobook.default_language` | `str` | `"fin"` | Default language code for synthesized spoken word expressions. |
| `funnels.audiobook.title_suffix` | `str` | `" (äänikirja)"` | Title suffix appended to synthesized audiobook expressions. |
| `funnels.audiobook.source_id_suffix` | `str` | `"_audio"` | Source ID suffix for synthesized audiobook expressions. |
| `funnels.audiobook.expression_uri_prefix` | `str` | `"#spoken_word_"` | Fragment prefix for synthesized audiobook expression URIs. |
| `funnels.audiobook.content_type` | `str` | `"spoken_word"` | Content type assigned to synthesized audiobook expressions. |
| `funnels.notated_music.fallback_match_tier` | `str` | `"tier2_domain_key"` | Match tier label when medium of performance is absent. |
| `funnels.patterns.*` | `str` | standard regexes | Configurable regexes for domain funnel dispatch (music, podcast, valto, exhibition). |
| `enrichment_toggles.inject_work_links` | `bool` | `true` | Injects MARC 758 / BIBFRAME Work URIs into enriched records. |
| `enrichment_toggles.inject_expression_links` | `bool` | `true` | Injects MARC 758 / BIBFRAME Expression URIs. |
| `enrichment_toggles.inject_hub_links` | `bool` | `true` | Injects Hub URIs into enriched BIBFRAME graphs. |
| `enrichment_toggles.inject_same_as` | `bool` | `true` | Injects `owl:sameAs` links into RDF records. |
| `enrichment_toggles.inject_component_links` | `bool` | `true` | Injects MARC 773 / host publication component links. |
| `enrichment_toggles.marc_predicate_profiles`| `list[str]` | `['bibframe', 'bffi', 'rda']` | Controlled relationship ontologies for MARC predicate mappings. |
| `enrichment_toggles.marc_predicate_mode` | `Literal` | `stacked` | `stacked` combines relationship codes into repeated `$4`; `distinct` creates separate fields. |
| `enrichment_toggles.thesis_relator_code` | `str` | `"dgg"` | MARC relator code in 710 $4 matching degree granting institutions. |
| `enrichment_toggles.ephemera_terms` | `list[str]` | Standard Finnish/Swedish/English terms | Controlled terms triggering 655 corporate authority linking for ephemera. |
| `rerank.enabled` | `bool` | `true` | When true, applies reranking and hard metadata discriminators to candidates. |
| `rerank.engine` | `Literal` | `bm25` | Reranker engine backend: `bm25`, `cross_encoder`, `fuzzy`, or `noop`. |
| `rerank.model_name` | `Optional[str]` | `None` | Transformer model name when `engine: "cross_encoder"` (e.g. `cross-encoder/ms-marco-MiniLM-L-6-v2`). |
| `rerank.device` | `Literal` | `auto` | Device for cross-encoder inference (`auto`, `cpu`, `cuda`, `mps`). |
| `rerank.dense_weight` | `float` | `0.6` | Weight of dense semantic cosine similarity [0.0, 1.0]. |
| `rerank.rerank_weight` | `float` | `0.4` | Weight of reranker score [0.0, 1.0] (must sum to 1.0 with `dense_weight`). |
| `rerank.top_k` | `int` | `50` | Number of dense vector nearest neighbors retrieved for reranking. |
| `link_policy` | `Literal` | `priority_fallback` | `priority_fallback` links top-priority vocabulary; `all` links all verified URIs. |
| `vocabularies` | `list[str]` | Standard vocabularies | Recognized vocabulary authorities for linking. |
| `agent_priority` | `list[str]` | `['KANTO', 'ISNI', 'VIAF', 'WIKIDATA']` | Order of precedence for resolving agent identity URIs. |
| `subject_priority` | `list[str]` | `['YSO', 'KAUNO', 'MUSO', 'SLM', 'SEKO', 'WIKIDATA']` | Order of precedence for resolving subject concept URIs. |
| `supported_subject_vocabularies` | `list[str]` | Standard subject vocabularies | List of supported subject/genre vocabularies retained during agent subject harvesting and reconciliation. |
| `entity_vocabularies` | `list[str]` | `['WIKIDATA']` | Controlled entity ontologies mapped to `$1` (real-world object entities) rather than `$0` (authority headings). |
| `vocabulary_patterns` | `dict[str, list[str]]` | Standard URI namespace patterns | Substring patterns mapped to vocabulary codes for URI namespace detection. |
| `normalization` | `Optional[RecordNormalizationConfig]` | `None` | Record normalization overrides specific to target reconciliation. |

---

### Telemetry Section (`telemetry`)

Controls progress monitoring heartbeat, watch interval polling, and dashboard panel toggles.

```yaml
telemetry:
  heartbeat_interval_seconds: 1.0
  watch_interval_seconds: 1.0
  show_vector_daemon: true          # Render dedicated Vector Daemon status panel in telemetry TUI
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `heartbeat_interval_seconds` | `float` | `1.0` | Telemetry heartbeat update interval in seconds during pipeline execution. |
| `watch_interval_seconds` | `float` | `1.0` | Live screen refresh and poll interval for `reconciliator status --watch`. |
| `show_vector_daemon` | `bool` | `true` | When true, renders a dedicated live Vector Daemon status panel in the TUI. |

---

### Normalization Section (`normalization`)

`reconciliator` features a generalized, fully configurable normalization surface decoupled from hardcoded logic. Normalization rules can be defined globally in `normalization` (under `record` and `vocab`), or overridden per-source (in `ingest_oai.<name>.normalization`, `download_skosmos.<name>.normalization`, `ingest_wikidata.normalization`, or `reconcile.normalization`).

```yaml
normalization:
  record:
    non_filing_articles:
      fi: ["yksi ", "eräs "]
      sv: ["en ", "ett ", "den ", "det ", "de "]
      en: ["the ", "a ", "an "]
      de: ["der ", "die ", "das ", "ein ", "eine "]
      fr: ["le ", "la ", "les ", "un ", "une ", "l'"]
    publisher_suffixes:
      - "oy"
      - "oyj"
      - "ab"
      - "abp"
      - "ltd"
      - "inc"
      - "corp"
      - "gmbh"
      - "kustannus"
      - "kustannusosakeyhtiö"
    volume_prefixes:
      - "osa"
      - "del"
      - "vol"
      - "volume"
      - "bd"
      - "band"
      - "tome"
      - "t"
      - "v"
      - "nro"
      - "no"
    volume_suffixes:
      - "osa"
      - "del"
      - "vol"
      - "kokoelma"
      - "painos"
    volume_format_template: "osa {num}"
    fictional_qualifiers:
      - "fiktiivinen"
      - "fiktiv"
      - "fictitious"
    responsibility_prefixes:
      - "kirjoittanut"
      - "toimittanut"
      - "toim."
      - "kirj."
      - "tekijä"
      - "författare"
      - "av"
      - "by"
      - "edited by"
    responsibility_conjunctions:
      - "ja"
      - "sekä"
      - "och"
      - "and"
      - "und"
      - "et"
    classification_priority:
      - "ykl"
      - "udk"
      - "dewey"
      - "lc"
    default_classification_scheme: "ykl"
    control_number_prefix: null             # e.g. "(FI-MELINDA)"
    control_number_pattern: null
    control_number_padding_digits: null     # e.g. 9
    urn_pattern: "(urn:nbn:[^\\s]+)"
    preserved_diacritics: "åäöÅÄÖ"
    custom_relator_terms: {}
    default_language: "fi"

  vocab:
    legacy_to_modern:
      ysa: "yso"
      allars: "yso"
      kaunokki: "kauno"
      bella: "kauno"
      musa: "muso"
      cilla: "seko"
    thesaurus_languages:
      ysa: "fi"
      allars: "sv"
      kaunokki: "fi"
      bella: "sv"
      musa: "fi"
      cilla: "sv"
    language_subcode_vocabularies:
      yso: "fin"
      yso-paikat: "fin"
      kauno: "fin"
      muso: "fin"
      slm: "fin"
    language_subcode_suffixes:
      fi: "fin"
      fin: "fin"
      sv: "swe"
      swe: "swe"
      en: "eng"
      eng: "eng"
    preferred_label_languages:
      - "fi"
      - "sv"
      - "en"
    language_code_map:
      fi: "fi"
      fin: "fi"
      sv: "sv"
      swe: "sv"
      en: "en"
      eng: "en"
    ingest_pref_label_languages: null # null, "*", or "all" to ingest all languages without explicit lists; or an explicit list (e.g. ["fi", "sv", "en"])
    ingest_alt_label_languages: null  # null, "*", or "all" to ingest all alternative labels; or an explicit list
    vocab_target_fields:
      slm: "655"
      seko: "382"
    field_to_vocabularies:
      "650": ["yso", "kauno", "muso"]
      "651": ["yso-paikat"]
      "655": ["slm", "seko"]
      "382": ["seko"]
    supported_subject_vocabularies:
      - "yso"
      - "yso-paikat"
      - "kauno"
      - "muso"
      - "slm"
      - "seko"
      - "koko"
      - "wikidata"
    vocabularies:
      - "KANTO"
      - "YSO"
      - "MUSO"
      - "KAUNO"
      - "SLM"
      - "SEKO"
      - "VIAF"
      - "ISNI"
      - "WIKIDATA"
    agent_priority:
      - "KANTO"
      - "ISNI"
      - "VIAF"
      - "WIKIDATA"
    subject_priority:
      - "YSO"
      - "KAUNO"
      - "MUSO"
      - "SLM"
      - "SEKO"
      - "WIKIDATA"
    entity_vocabularies:
      - "WIKIDATA"
    vocabulary_patterns:
      KANTO: ["urn.fi/urn:nbn:fi:au:pn:", "(fi-asteri-n)", "(fi-kanto)", "asteri"]
      YSO: ["yso.fi/onto/yso/", "(fi-yso)"]
      MUSO: ["yso.fi/onto/muso/", "(fi-muso)"]
      KAUNO: ["yso.fi/onto/kauno/"]
      SLM: ["urn.fi/urn:nbn:fi:au:slm:"]
      SEKO: ["urn.fi/urn:nbn:fi:au:seko:"]
      VIAF: ["viaf.org/viaf/", "(viaf)"]
      ISNI: ["isni.org/isni/", "(isni)"]
      WIKIDATA: ["wikidata.org/entity/", "wikidata.org/wiki/"]
    default_field_vocabularies:
      "650": "yso"
      "651": "yso-paikat"
      "655": "slm"
      "382": "seko"
    preserved_diacritics: "åäöÅÄÖ"
    default_language: "fi"
```

#### Record Normalization Parameters (`RecordNormalizationConfig`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `non_filing_articles` | `dict[str, list[str]]` | Map of ISO languages | Leading grammatical articles stripped during title normalization. |
| `publisher_suffixes` | `list[str]` | Corporate suffixes | Legal and publishing suffixes stripped from publisher literals. |
| `volume_prefixes` | `list[str]` | `['osa', 'vol', ...]` | Keywords preceding volume/part numbers. |
| `volume_suffixes` | `list[str]` | `['osa', 'del', ...]` | Keywords succeeding volume/part numbers. |
| `volume_format_template` | `str` | `osa {num}` | Canonical output template for normalized volumes. |
| `fictional_qualifiers` | `list[str]` | `['fiktiivinen', ...]` | Substrings marking fictional entity qualifiers in agent names. |
| `responsibility_prefixes` | `list[str]` | Lead-in phrases | Lead-in phrases stripped when parsing 245$c authors. |
| `responsibility_conjunctions`| `list[str]` | Conjunctions | Coordinating conjunctions splitting 245$c statements. |
| `classification_priority` | `list[str]` | `['ykl', 'udk', ...]` | Scheme hierarchy determining primary classification flag. |
| `default_classification_scheme` | `str` | `ykl` | Default scheme when unspecified in input record. |
| `control_number_prefix` | `str` | `None` | Prefix applied/expected for system control numbers (e.g. `(FI-MELINDA)`). |
| `control_number_padding_digits`| `int` | `None` | Zero-padding width for numeric system control numbers (e.g. `9`). |
| `control_number_strip_prefixes`| `list[str]`| `None` | Optional list of prefix strings to strip prior to normalization. |
| `urn_pattern` | `str` | `(urn:nbn:[^\s]+)` | Regex pattern extracting persistent URN identifiers. |
| `preserved_diacritics` | `str` | `åäöÅÄÖ` | Specific Unicode characters preserved during selective diacritic folding. |
| `biographical_date_prefixes`| `list[str]` | `['ca.', 's.', 'k.', ...]` | Prefix tokens indicating birth/death/flourished date qualifiers. |
| `biographical_era_bc` | `list[str]` | `['eaa.', 'bc', ...]` | Era designations indicating Before Common Era (BC). |
| `biographical_era_ad` | `list[str]` | `['jaa.', 'ad', ...]` | Era designations indicating Anno Domini / Common Era (AD). |
| `biographical_century_suffixes`| `list[str]` | `['luku', 'talet', ...]` | Suffixes designating century/period notations (e.g. `1800-luku`). |
| `biographical_date_pattern` | `str` | `None` | Optional raw regex pattern overriding modular biographical date parsing. |
| `roman_numerals` | `dict[str, int]` | Roman numeral map (`i: 1`, `ii: 2`, ...) | Roman numeral mappings used when normalizing volume designations. |
| `relator_mappings` | `dict[str, str]` | Multilingual term map | Natural-language relator word to MARC code mappings (`term -> code`). |
| `custom_relator_terms` | `dict[str, str]` | `{}` | Additional relator term mappings (`code -> term`). |
| `default_language` | `str` | `fi` | Fallback language code when not specified on record. |

#### Vocabulary Normalization Parameters (`VocabNormalizationConfig`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `legacy_to_modern` | `dict[str, str]` | Thesaurus mappings | Target modern vocabulary mapped from legacy thesaurus code. |
| `thesaurus_languages` | `dict[str, str]` | Language mappings | Canonical language associated with legacy thesaurus (`ysa: fi`, `allars: sv`). |
| `language_subcode_vocabularies`| `dict[str, str]`| Subcode mappings | Modern vocabularies requiring language subcode designations. |
| `language_subcode_suffixes`| `dict[str, str]`| ISO language code to suffix map | Maps 2-letter and 3-letter language codes to ontology subcode designations. |
| `preferred_label_languages`| `list[str]` | `['fi', 'sv', 'en']` | Fallback priority order when resolving concept preferred labels. |
| `language_code_map` | `dict[str, str]` | Alias to canonical language | Normalizes 3-letter MARC codes to canonical 2-letter label keys. |
| `ingest_pref_label_languages`| `Optional[list[str]]`| `None` | Languages retained during ingestion for preferred labels (`null`, `"*"`, `"all"` for all languages; or e.g. `["fi", "sv", "en"]`). |
| `ingest_alt_label_languages` | `Optional[list[str]]`| `None` | Languages retained during ingestion for alternative labels (`null`, `"*"`, `"all"` for all languages; or e.g. `["fi", "sv", "en"]`). |
| `vocab_target_fields` | `dict[str, str]` | `{'slm': '655', 'seko': '382'}` | Target MARC field assigned based on modern vocabulary. |
| `field_to_vocabularies` | `dict[str, list[str]]`| MARC field candidate routing | Candidate ontologies queried for each subject field (`650`, `651`, `655`, `382`). |
| `supported_subject_vocabularies` | `list[str]` | `['yso', 'yso-paikat', 'kauno', 'muso', 'slm', 'seko', 'koko', 'wikidata']` | Controlled ontologies accepted during subject harvesting; unlisted vocabularies are discarded. |
| `vocabularies` | `list[str]` | Standard vocabularies | Recognized vocabulary authorities for linking. |
| `agent_priority` | `list[str]` | `['KANTO', 'ISNI', 'VIAF', 'WIKIDATA']` | Order of precedence for resolving agent identity URIs. |
| `subject_priority` | `list[str]` | `['YSO', 'KAUNO', 'MUSO', 'SLM', 'SEKO', 'WIKIDATA']` | Order of precedence for resolving subject concept URIs. |
| `entity_vocabularies` | `list[str]` | `['WIKIDATA']` | Controlled entity ontologies mapped to `$1` (real-world object entities) rather than `$0` (authority headings). |
| `vocabulary_patterns` | `dict[str, list[str]]` | Standard URI namespace patterns | Substring patterns mapped to vocabulary codes for URI namespace detection. |
| `default_field_vocabularies` | `dict[str, str]` | Default field fallbacks | Fallback ontology assigned per MARC field when lookup fails. |
| `preserved_diacritics` | `str` | `åäöÅÄÖ` | Characters preserved during subject label folding. |
| `biographical_date_prefixes`| `list[str]` | `['ca.', 's.', 'k.', ...]` | Prefix tokens indicating birth/death/flourished date qualifiers. |
| `biographical_era_bc` | `list[str]` | `['eaa.', 'bc', ...]` | Era designations indicating Before Common Era (BC). |
| `biographical_era_ad` | `list[str]` | `['jaa.', 'ad', ...]` | Era designations indicating Anno Domini / Common Era (AD). |
| `biographical_century_suffixes`| `list[str]` | `['luku', 'talet', ...]` | Suffixes designating century/period notations. |
| `biographical_date_pattern` | `str` | `None` | Optional raw regex pattern overriding modular date parsing. |
| `default_language` | `str` | `fi` | Default lookup language when not specified. |

---

### Modular Source Configuration Files

To keep `config.yaml` concise and manageable, `download_skosmos` and `ingest_oai` sections support modular external file referencing:

```yaml
# Direct file reference:
download_skosmos:
  finto: "sources/skosmos/finto.example.yaml"

# Modular file or directory glob reference:
ingest_oai:
  melinda: "sources/oai/melinda.example.yaml"
  # Or load all *.yaml files in a directory:
  # ingest_oai: "sources/oai/"
```

Each source file can contain source-specific configurations and its own localized `normalization` overrides. All relative paths within external source configuration files are resolved seamlessly relative to the root configuration file.

---

#### Full Production Profile (`config.prod.yaml`)

```yaml
storage:
  data_dir: "/var/data/reconciliator"
  database_url: "postgresql+psycopg://postgres:secret@postgres.internal:5432/reconciliator"
  work_index: "/var/data/reconciliator/work.index"
  manifestation_index: "/var/data/reconciliator/manifestation.index"
  agent_index: "/var/data/reconciliator/agent.index"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "auto"
  batch_size: 128

download_skosmos:
  finto:
    enabled: true
    api_url: "https://api.finto.fi/rest/v1"
    output_dir: "/var/data/reconciliator/finto"
    vocabularies: ["yso", "yso-paikat", "slm", "kauno", "muso", "seko"]
    overwrite: false

ingest_vocab:
  input_path: "/var/data/reconciliator/finto"
  vocabularies: ["yso", "yso-paikat", "kauno", "muso", "slm", "seko"]
  legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]

ingest_oai:
  melinda:
    enabled: true
    source_type: "oai"
    format: "marcxml"
    target_database: "authority"
    oai_url: "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib"
    metadata_prefix: "marc21"
    workers: 8
    slice_days: 15
    batch_size: 1000

reconcile:
  target_file: "/var/data/incoming/batch_2026_09.xml"
  format: "auto"
  output_file: "/var/data/enriched/batch_2026_09_enriched.xml"
  enrich: true
  fold_legacy: true
  threshold: 0.88
  fallback_agent_reconciliation: true
  agent_fallback_threshold: 0.90
  verify_chronology: true
  strict_homonym_guard: true
  agent_reports:
    detailed_file: "/var/reports/agent_detailed.tsv"
    summary_file: "/var/reports/agent_summary.tsv"
  subject_reports:
    detailed_file: "/var/reports/subject_detailed.tsv"
    summary_file: "/var/reports/subject_summary.tsv"
  work_reports:
    detailed_file: "/var/reports/work_detailed.tsv"
    summary_file: "/var/reports/wemi_summary.tsv"
  expression_reports:
    detailed_file: "/var/reports/expression_detailed.tsv"
    summary_file: "/var/reports/wemi_summary.tsv"
  cluster:
    enabled: true
    similarity_threshold: 0.88
    max_cluster_size: 500
    thresholds:
      work: 0.85
      expression: 0.88
      manifestation: 0.92
    message_passing: true
  enrichment_toggles:
    inject_work_links: true
    inject_expression_links: true
    inject_hub_links: true
    inject_same_as: true
    inject_component_links: true
    marc_predicate_profiles: ["bibframe", "bffi", "rda"]
    marc_predicate_mode: "stacked"
  rerank:
    enabled: true
    engine: "bm25"
    dense_weight: 0.65
    rerank_weight: 0.35
    top_k: 40
    short_circuit:
      enabled: true
      threshold: 0.985
    require_volume_match: true
    require_surname_match: true
    max_year_difference: 3
    year_penalty_threshold: 1
```

---

## 3. CLI Command Reference

All commands accept `--config / -c` pointing to the YAML configuration file. If omitted, `config.yaml` in the current working directory is loaded by default.

### `reconciliator download-skosmos`

Downloads value vocabularies and ontologies in SKOS Turtle format from any configured Skosmos REST API instance (defaulting to Finto, with support for multi-source instances like AGROVOC, UNESCO, etc.). Automatically utilizes fast static distribution dumps for recognized Finto vocabularies.

```bash
# 1. Discover all available vocabularies on default/specified Skosmos instance and print a table
reconciliator download-skosmos --list
reconciliator download-skosmos -s finto --list

# 2. Download default vocabularies for a specific source to data/finto
reconciliator download-skosmos -c config.yaml -s finto

# 3. Download all enabled Skosmos sources in sequence
reconciliator download-skosmos -c config.yaml --all

# 4. Download specific vocabularies (e.g. YSO, YSO-paikat, and SLM)
reconciliator download-skosmos -c config.yaml -s finto -v yso -v yso-paikat -v slm

# 5. Download into a custom output directory and force overwrite existing files
reconciliator download-skosmos -c config.yaml -o /tmp/finto_dumps -v seko --force

# 6. Discover and download ALL published vocabularies on the selected Skosmos instance
reconciliator download-skosmos -c config.yaml -s finto --all-vocabs

# 7. Download and immediately chain ingestion into vocab.db
reconciliator download-skosmos -c config.yaml -s finto -v yso -v slm --ingest
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-s, --source TEXT`: Name of configured Skosmos source to download (defaults to `finto`).
- `-a, --all`: Download vocabularies for all enabled Skosmos sources.
- `-v, --vocab TEXT`: Specific vocabulary ID to download (repeatable).
- `-o, --output-dir PATH`: Directory where `.ttl` files will be saved.
- `--api-url TEXT`: Override Skosmos REST API endpoint URL.
- `--all-vocabs`: Query the API and download every vocabulary published on the Skosmos instance.
- `--list`: Print a formatted table of available vocabularies and exit.
- `--force`: Overwrite existing files on disk.
- `--ingest`: Automatically trigger `ingest-vocab` on the downloaded directory upon completion.

---

### `reconciliator ingest-vocab`

Parses SKOS Turtle dumps, extracts concepts, preferred and alternate labels, and legacy migration tables (`skos:exactMatch`, `dcterms:isReplacedBy`). Ingests bundled Library of Congress bridge files, populates `relator_terms`, and indexes `concept_aliases`.

```bash
# Ingest ontologies from directory or file configured in config.yaml
reconciliator ingest-vocab -c config.yaml
```

#### What happens during ingestion:
1. Parses all `.ttl` dumps in `input_path` (e.g. `yso.ttl`, `yso-paikat.ttl`, `slm.ttl`, `kauno.ttl`, `muso.ttl`, `seko.ttl`).
2. Auto-ingests bundled bridge files from `src/reconciliator/vocab/bridges/` (mapping LoC countries, languages, and issuance modes to YSO concepts).
3. Ingests Metatietosanasto (MTS) relator terms into `relator_terms`.
4. Rebuilds `concept_aliases` table indexing Finnish, Swedish, and English preferred and alternate labels for bilingual resolution (e.g. `Malax` $\to$ `Maalahti`).

---

### `reconciliator ingest-oai`

Harvests or reads authority records from configured OAI-PMH or MARCXML sources (e.g. Melinda, Koha, Libris), normalizes agent names, generates dense embeddings using `SentenceEmbedder`, and builds the FAISS vector index with direct ID mapping and idempotent updates.

```bash
# Ingest single configured source (e.g. melinda)
reconciliator ingest-oai -c config.yaml -s melinda

# Ingest all enabled sources in sequence
reconciliator ingest-oai -c config.yaml --all

# Resume harvest without re-embedding already indexed records
reconciliator ingest-oai -c config.yaml -s melinda --resume

# Override harvest date filters on CLI
reconciliator ingest-oai -c config.yaml -s melinda --from 2024-01-01 --until 2024-06-01
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-s, --source TEXT`: Name of specific configured OAI source to ingest.
- `-a, --all`: Ingest all enabled sources declared under `ingest_oai`.
- `--from TEXT`: Selective harvest start date (`YYYY-MM-DD`).
- `--until TEXT`: Selective harvest end date (`YYYY-MM-DD`).
- `-w, --workers INTEGER`: Override number of parallel harvest worker threads.
- `--normalization-workers INTEGER`: Override number of parallel normalization workers.
- `--skip-existing, --resume`: Skip normalization and embedding for records already indexed in the authority database and vector index. Instant bypass for previously harvested records.

---

### `reconciliator ingest-wikidata`

Streams a compressed Wikidata JSON dump (`latest-all.json.gz`), filters entities relevant to Finnish bibliographic authority work (agents with VIAF, GND, ISNI, and YSO P2347 claims), generates dense embeddings, and appends them to `authority.db` and `authority.faiss`.

```bash
# Stream and index Wikidata dump configured in config.yaml
reconciliator ingest-wikidata -c config.yaml
```

---

### `reconciliator build-agent-index`

Extracts agent identities from `authority.db` (`agents` table or distinct `authority_agents` entries), renders their essential textual representations (~30–50 tokens using `agent_serialization.j2`), embeds them using `SentenceEmbedder`, and builds or updates the dedicated standalone agent vector index (`data/agent.index`).

This index serves as the dense candidate retrieval engine for **Tier 3 Standalone Agent Fallback**, resolving unlinked authors on rare publications or items absent from Melinda/Fennica.

```bash
# 1. Build agent index with default configuration (batch size 128)
reconciliator build-agent-index -c config.yaml

# 2. Build agent index with larger batch size on GPU/MPS
reconciliator build-agent-index -c config.yaml -b 512
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-b, --batch-size INTEGER`: Encoding batch size (default: 128).

---

### `reconciliator reconcile`

Performs offline reconciliation on a target bibliographic file (MARCXML, BIBRA JSON/JSONL, or BIBFRAME RDF/JSON-LD with declarative profiles such as Finnish BFFI 1.0).

#### Operational Pipeline:
1. **Target Staging & Intra-Corpus Clustering**: Stages incoming records into `corpus_db` (`reconcile_corpus.db`) and executes 3-layer multiplex graph clustering (`CorpusClusterer`) across Works, Expressions, and Manifestations using blocking partitions.
2. **Vocabulary Modernization**: Folds legacy subject thesauri headings (YSA, Allärs, Kaunokki, MUSA) to contemporary ontologies (YSO, YSO-paikat, SLM, KAUNO, MUSO, SEKO) with language indicators.
3. **Multi-Tier Matching Funnel**:
   - **Tier 1: Exact Identifier Lookup**: Matches standard identifiers (ISBN, ISSN, URN, Asteri ID) directly against authority indices for instant 100% confidence transfer.
   - **Tier 2: Coupled Work Semantic Matching**: Evaluates Title + Author + Subjects jointly via `authority.index` / `work.index` and rerankers (`bm25`, `cross_encoder`, `fuzzy`). Aligns agents on matched publications with near-zero false positive risk.
   - **Tier 3: Guarded Standalone Agent Fallback**: For target agents remaining unlinked (e.g. on rare, uncataloged, or missing publications), queries `agent.index` and filters candidates through dynamic selective guards:
     - *Life Dates Guard*: Discards candidates with contradictory birth/death years.
     - *Chronological Sanity Window*: Enforces author minimum age (`chronology_min_author_age`) and posthumous publication limit (`chronology_max_posthumous_years`).
     - *Qualifier Guard*: Validates role/profession/affiliation distribution compatibility.
     - *Subject Overlap Guard*: Disqualifies candidates with completely disjoint subject/genre profiles when both sides have data.
     - *Strict Homonym Guard*: Rejects ambiguous candidates within score margin (`0.03`) that lack distinguishing dates, qualifiers, or subject profiles.
4. **WEMI Conceptual Resolution**: Resolves Work and Expression entities against `authority.db` using multi-tier fallback funnels and semantic vector matching.
5. **In-Place Enrichment**: Injects authorized URIs into target records without format conversion (`$0`/`$1` and `758`/`773` in MARCXML, `owl:sameAs` in BIBFRAME).
6. **Comprehensive TSV Reporting**: Emits detailed records and aggregate summary TSVs for agents, subjects, works, and expressions.

```bash
# 1. Standard reconciliation with enrichment and report generation
reconciliator reconcile -c config.yaml

# 2. Audit-only mode (reports generated, but target file unmodified)
# Set 'enrich: false' in config.yaml, then run:
reconciliator reconcile -c config.audit.yaml
```

#### Generated Reports:
1. **Agent Detailed TSV** (`agent_reports.detailed_file`):
   - Exact 21 tab-separated columns:
     `target_record_id`, `target_title`, `target_year`, `target_isbn`, `field_tag`, `field_occurrence`, `subfield_code`, `target_heading`, `target_role`, `uri`, `vocabulary`, `authority_label`, `authority_record_id`, `authority_source`, `match_tier`, `match_score`, `dense_score`, `rerank_score`, `agent_name_score`, `action`, `is_novel`.
2. **Agent Summary TSV** (`agent_reports.summary_file`):
   - Tidy 5-column schema (`section`, `metric`, `value`, `percentage`, `description`) tracking KPIs across:
     - `execution`: Timestamps, target/output file paths, enrichment mode, runtime, link policy, and full operational threshold auditability (`threshold`, `short_circuit_threshold`, `rerank_engine`, `dense_weight`, `rerank_weight`).
     - `records`, `fields`, `uris`, `tiers`, `vocabularies`, `sources`. Vocabulary descriptions and priorities are dynamically generated via semantic formatters (`describe_agent_vocabulary`) and derived from `ReconcileConfig.agent_priority` without hardcoded static tables.
     - `quality`: Dynamic alignment quality brackets derived directly from active application thresholds ($T_{\text{accept}}$, $T_{\text{short}}$, and midpoint $T_{\text{mid}}$) or custom `quality_brackets`, completely accounting for 100% of candidate rows (including below-threshold candidates).
3. **Subject Detailed TSV** (`subject_reports.detailed_file`):
   - Exact 16 tab-separated columns:
     `target_record_id`, `target_title`, `source_field_tag`, `field_occurrence`, `source_heading`, `source_vocab`, `source_uri`, `language`, `target_field_tag`, `target_vocab`, `target_uri`, `target_pref_label`, `mapping_type`, `action`, `is_novel`, `tag_changed`.
4. **Subject Summary TSV** (`subject_reports.summary_file`):
   - Tidy 5-column schema tracking legacy source thesauri folded, canonical target ontologies, dynamic tag migrations, and intra-field foldings without hardcoded tables. Vocabulary descriptions (`describe_source_thesaurus`, `describe_target_ontology`, `describe_tag_migration`) and priorities are dynamically synthesized from configuration schemas and data.
5. **Work Detailed TSV** (`work_reports.detailed_file`):
   - Exact 14 tab-separated columns:
     `target_record_id`, `work_type`, `target_title`, `target_author`, `target_year`, `work_blocking_key`, `matched_work_uri`, `wikidata_qid`, `match_tier`, `funnel_used`, `confidence_score`, `is_clustered`, `cluster_id`, `action`.
6. **Expression Detailed TSV** (`expression_reports.detailed_file`):
   - Exact 12 tab-separated columns:
     `target_record_id`, `work_uri`, `expression_uri`, `content_type`, `language`, `version_designation`, `translators`, `performers`, `match_tier`, `confidence_score`, `cluster_id`, `action`.
7. **WEMI Summary TSV** (`work_reports.summary_file` or `expression_reports.summary_file`):
   - Tidy 5-column schema tracking KPIs across works resolved, expressions linked, clusters formed, hoisted vs minted URIs, and match tiers.

---

### `reconciliator run`

Executes the entire end-to-end pipeline in sequence as configured in your YAML file:
1. `ingest-vocab` (if `ingest_vocab` section is present)
2. `ingest-oai` (if `ingest_oai` section is present)
3. `ingest-wikidata` (if `ingest_wikidata` section is present)
4. `reconcile` (if `reconcile` section is present)

```bash
# Execute end-to-end batch pipeline
reconciliator run -c config.yaml
```

---

### `reconciliator status`

Queries task execution state, processed byte counts, records/sec throughput, and error telemetry recorded in `progress.db`.

When `storage.vector_engine.mode` is set to `"daemon"` and `telemetry.show_vector_daemon: true`, both the snapshot table and the interactive TUI include a dedicated **Vector Daemon Status Panel** displaying daemon health (`● ACTIVE` / `○ OFFLINE`), process PID, socket connectivity, uptime and idle watchdog countdown, loaded indices count and names (`authority`, `work`, `expression`, `manifestation`), total indexed vector count across all indices, pending dirty indices awaiting auto-flush, and active device backend (e.g. PyTorch MPS or FAISS).

In `--watch` mode, `reconciliator status` launches an interactive, full-screen Terminal User Interface (TUI) utilizing Rich's alternate screen buffer. The dashboard displays live task stages, color-coded status badges (`● RUNNING`, `✔ COMPLETED`, `✖ FAILED`), progress percentages, items and megabytes processed/total, throughput rates (items/s), last heartbeat timestamps, aggregate processing rates, and an active diagnostics panel if any background pipeline encounters an error.

```bash
# 1. Print snapshot table of current tasks and vector daemon status
reconciliator status -c config.yaml

# 2. Live full-screen TUI monitor (updates every second in terminal; press Ctrl+C to exit)
reconciliator status -c config.yaml --watch

# 3. Live monitor with custom polling frequency (every 0.5s)
reconciliator status -c config.yaml -w -i 0.5

# 4. Emit machine-readable JSON status for CI/CD or dashboard integration
reconciliator status -c config.yaml --json

# 5. Query a specific task ID
reconciliator status -c config.yaml --task-id reconciliation-1725969600

# 6. Display status and immediately prune completed tasks from progress.db
reconciliator status -c config.yaml --prune
```

#### Terminal Dashboard Table Example:
```text
╭────────────────────────────── Vector Index Daemon ──────────────────────────────╮
│ Status: ● ACTIVE (PID 48210)                 Uptime: 00:14:22                   │
│ Indices Loaded: 4 (authority, work, expr...) Total Vectors: 842,105             │
│ Dirty Indices: 0                             Backend: torch (mps)               │
│ Socket: /tmp/rec_vectord_7a9f1b2c.sock       Idle Shutdown: in 00:25:38         │
╰─────────────────────────────────────────────────────────────────────────────────╯
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Task ID                      ┃ Type           ┃ Status    ┃ Stage            ┃ Progress   ┃ Elapsed       ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ vocab-download-172596950     │ vocab_ingest   │ completed │ done             │ 6/6 vocabs │ 00:00:14      │
│ oai-ingest-melinda-172596955 │ oai_pmh_harves │ completed │ indexing         │ 100.0%     │ 00:03:42      │
│ reconciliation-172596960     │ reconciliation │ running   │ reconciling      │ 78.4%      │ 00:01:15      │
└──────────────────────────────┴────────────────┴───────────┴──────────────────┴────────────┴───────────────┘
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `--task-id TEXT`: Query status of a specific task ID.
- `-w, --watch`: Launch continuous live full-screen TUI monitor (exits automatically when all tasks reach completed or failed state, or upon Ctrl+C).
- `-i, --interval FLOAT`: Watch polling and screen refresh interval in seconds (defaults to `telemetry.watch_interval_seconds` from `config.yaml`).
- `--json`: Emit output as raw JSON lines / objects.
- `--prune, --prune-completed`: Prune completed tasks from `progress.db` after displaying status.

---

### `reconciliator daemon`

Manages the background singleton vector indexing service (`VectorDaemonServer`). The daemon manages all persistent vector indices, serializes read/write access across multiple concurrent CLI pipelines, shares a single GPU context (MPS / CUDA), periodically flushes updates to disk, and automatically shuts down after prolonged inactivity.

```bash
# 1. Inspect vector daemon health, loaded indices, uptime, and socket connectivity
reconciliator daemon status -c config.yaml

# 2. Emit daemon status in machine-readable JSON format
reconciliator daemon status -c config.yaml --json

# 3. Explicitly start the daemon in the background (if not already running)
reconciliator daemon start -c config.yaml

# 4. Gracefully save all dirty indices to disk and shut down the daemon
reconciliator daemon stop -c config.yaml

# 5. Stop and restart the daemon
reconciliator daemon restart -c config.yaml
```

#### Subcommands:
- `reconciliator daemon status`: Connects to the local socket, queries daemon ping/status telemetry, and prints the formatted status panel (or JSON structure).
- `reconciliator daemon start`: Spawns the daemon process in the background using `DaemonLifecycleManager` and verifies socket connectivity. (Note: when `auto_start: true`, pipelines will spawn the daemon automatically on demand if it is not already running).
- `reconciliator daemon stop`: Sends an explicit `OP_SHUTDOWN` command to the daemon over the IPC socket. The daemon flushes all dirty indices to disk, closes sockets, unlinks the socket file, and exits cleanly.
- `reconciliator daemon restart`: Executes a graceful stop, waits 0.5 seconds, and starts a fresh daemon process.

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `--json`: (Available on `daemon status`) Output full daemon status dictionary as JSON.

---

### `reconciliator prune`

Prunes task telemetry records and pipeline run history from the task telemetry store while safeguarding active running tasks.

```bash
# 1. Prune all completed tasks (prompts for interactive confirmation [y/N])
reconciliator prune -c config.yaml --completed

# 2. Prune all completed tasks non-interactively (ideal for cron or scripts)
reconciliator prune -c config.yaml --completed -y

# 3. Prune failed tasks
reconciliator prune -c config.yaml --failed -y

# 4. Prune tasks older than 7 days (supports 7d, 24h, 30m, 60s, or bare numbers)
reconciliator prune -c config.yaml --older-than 7d -y

# 5. Keep the latest 10 tasks and purge all older history
reconciliator prune -c config.yaml --keep-last 10 -y

# 6. Prune all finished tasks (both completed and failed), safely leaving running tasks untouched
reconciliator prune -c config.yaml --all -y

# 7. Prune a specific task by its task ID
reconciliator prune -c config.yaml --task-id oai_pmh_harvest-172596955 -y

# 8. Override active-task safety guard to include running tasks (use with caution)
reconciliator prune -c config.yaml --all --include-running -y
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `--task-id TEXT`: Prune a specific task ID.
- `--completed`: Prune tasks with status `completed`.
- `--failed`: Prune tasks with status `failed`.
- `--older-than TEXT`: Prune tasks older than human-readable duration (e.g. `7d`, `24h`, `30m`, `60s`, or bare numeric days).
- `--keep-last INTEGER`: Keep the most recent $N$ tasks, purging older entries.
- `-a, --all`: Prune all finished tasks (`completed` and `failed`).
- `--include-running`: Allow deletion of tasks currently in `running` state (defaults to `false` to safeguard ongoing harvests/reconciliations).
- `-y, --yes`: Bypass interactive confirmation prompt.


---

### `reconciliator reset`

Resets and cleans PostgreSQL database tables, vector indices, and daemon state.

```bash
# 1. Interactively reset all configured PostgreSQL tables and vector indices (prompts [y/N])
reconciliator reset -c config.yaml

# 2. Reset all storage non-interactively (ideal for test setups, CI, and batch scripts)
reconciliator reset -c config.yaml --yes
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-y, --yes`: Bypass interactive confirmation prompt.

---

## 4. Standard Operational Workflows

### Workflow A: Cold-Start Bootstrapping

Follow this workflow when setting up a new production instance:

```bash
# Step 1: Create storage directories
mkdir -p data/finto data/reports

# Step 2: Download canonical FINTO vocabularies and ingest them into vocab.db
reconciliator download-skosmos -c config.yaml --all --ingest

# Step 3: Ingest configured authority records and build primary WEMI vector indices
reconciliator ingest-oai -c config.yaml --all

# Step 4: Build standalone agent vector index for Tier 3 fallback
reconciliator build-agent-index -c config.yaml

# Step 5: Run reconciliation on target records (stages into corpus_db, clusters, matches WEMI & agents, enriches)
reconciliator reconcile -c config.yaml

# Step 6: Check output file and all generated TSV reports
ls -lh data/enriched_records.xml data/reports/*.tsv
```

---

### Workflow B: Batch Production Reconciliation

When authority files are already ingested and new target catalogs arrive daily or monthly:

```bash
# 1. Point config.yaml to incoming target file
# (e.g. target_file: "incoming/records_2026_09_10.xml")

# 2. Launch reconciliation in the background
nohup reconciliator reconcile -c config.yaml > reconcile.log 2>&1 &

# 3. Monitor live progress from another terminal
reconciliator status -c config.yaml --watch
```

---

### Workflow C: Air-Gapped / Zero-Network Execution

In secure, offline cataloging environments where external internet access is prohibited:

1. **Pre-packaging (on connected workstation)**:
   - Run `reconciliator download-skosmos` to download `.ttl` dumps.
   - Cache HuggingFace embedding model:
     ```python
     from sentence_transformers import SentenceTransformer

     model = SentenceTransformer("TurkuNLP/sbert-cased-finnish-paraphrase")
     model.save("/path/to/local/model_dir")
     ```
2. **Transfer to Offline Host**:
   - Transfer `vocab.db`, `authority.db`, vector index files (`authority.index`, `work.index`, `expression.index`, `manifestation.index`, `agent.index`), and `/path/to/local/model_dir`.
3. **Configuration**:
   - Set `model_name: "/path/to/local/model_dir"`.
   - Set `source_type: "file"` for configured OAI/MARCXML sources.
4. **Run Reconciliation**:
   - Execute `reconciliator reconcile -c config.offline.yaml`.
   - **Zero network requests** are made during execution.

---

### Workflow D: Quality Audit & Discrepancy Review

To run an audit report without modifying the original records:

1. In `config.yaml`, set `enrich: false`.
2. Run `reconciliator reconcile -c config.yaml`.
3. Inspect summary reports:
   ```bash
   cat data/reports/agent_reconciliation_summary.tsv
   cat data/reports/subject_reconciliation_summary.tsv
   cat data/reports/wemi_summary.tsv
   ```
4. Filter detailed report for ambiguous or borderline agent matches (score between acceptance threshold 0.85 and midpoint 0.92):
   ```bash
   awk -F'\t' '$16 >= 0.85 && $16 < 0.92 {print $1, $10, $15, $16}' data/reports/agent_reconciliation_uris.tsv
   ```
5. Inspect unlinked or newly minted conceptual works:
   ```bash
   awk -F'\t' '$14 == "minted" || $14 == "unmatched" {print $1, $2, $3, $4, $14}' data/reports/work_reconciliation.tsv
   ```

---

### Workflow E: Telemetry Maintenance & Monitoring Database Pruning

Over time, long-running ingestion harvests and batch reconciliation jobs accumulate telemetry task rows in `data/progress.db`. To maintain lean storage and fast status queries:

1. **Check Current Status and Task History**:
   ```bash
   reconciliator status -c config.yaml
   ```

2. **Automated Maintenance via Periodic Job (Cron)**:
   Add a periodic pruning job to retain the last 30 days of telemetry, automatically purging completed and failed runs without interrupting active pipelines:
   ```bash
   # Crontab entry: run every Sunday at 02:00 AM
   0 2 * * 0 /opt/reconciliator/.venv/bin/reconciliator prune -c /opt/reconciliator/config.yaml --older-than 30d -y >> /var/log/reconciliator-prune.log 2>&1
   ```

3. **Post-Batch Cleanup**:
   After verifying batch outputs and reports, prune finished tasks while retaining the 5 most recent runs:
   ```bash
   reconciliator prune -c config.yaml --keep-last 5 -y
   ```

4. **Quick Status & Inline Cleanup**:
   Check pipeline status and immediately purge all completed tasks in one step:
   ```bash
   reconciliator status -c config.yaml --prune
   ```

5. **Troubleshooting Failed Pipeline State**:
   If a pipeline failed due to a misconfiguration and has been re-run successfully, purge old error entries:
   ```bash
   reconciliator prune -c config.yaml --failed -y
   ```

---

## 5. Diagnostics & Troubleshooting

### Issue 1: "Target MARCXML file not found"
- **Symptom**: `reconcile` exits immediately with exit code 1.
- **Remedy**: Verify `reconcile.target_file` exists and is readable. Check whether the path in `config.yaml` is relative to the configuration file location.

### Issue 2: Vector Index Metadata Mismatch (USearch / FAISS / PyTorch)
- **Symptom**: `ValueError: Index metadata mismatch: expected model ... but found ...`.
- **Cause**: The vector index was generated using an embedding model or serialization template different from the one currently configured.
- **Remedy**:
  - Re-run `ingest-oai` or `ingest-wikidata` to regenerate index vectors with the new model.
  - Or, if only the Jinja2 template was modified without affecting vector semantics, set `force_template: true` in the `embedding` config section.

### Issue 3: High Memory Consumption or Out of Memory (OOM) on Large Datasets (Melinda / Wikidata)
- **Symptom**: High host memory usage (20–40+ GB) or process killed by OS during dense embedding generation or vector indexing on multi-million record datasets.
- **Remedy**:
  - **Switch to USearch with Zero-Copy Memory Mapping**: Set `storage.vector_engine.backend: "usearch"`, `storage.vector_engine.usearch.dtype: "f16"` (50% size reduction) or `"i8"` (75% size reduction), and `storage.vector_engine.usearch.mmap: true`. USearch uses native OS page cache memory mapping (`view=True`), keeping resident search RAM under 100 MB regardless of dataset size (even across 10M+ records).
  - **Or use FAISS SQ8**: Set `storage.vector_engine.backend: "faiss"` and `storage.vector_engine.faiss.index_type: "sq8"` (8-bit quantization, 75% memory reduction).
  - **Reduce embedding batch size**: Lower `embedding.batch_size` (e.g. from `64` to `32` or `16`).
  - **Adjust harvester workers**: If running on a low-memory instance, lower `workers` in `ingest_oai` (e.g. from `8` to `2` or `4`).

### Issue 4: PostgreSQL Connection or Lock Contention
- **Symptom**: Operational error or connection timeout during high-concurrency ingestion.
- **Remedy**:
  - Verify PostgreSQL service is healthy and accepting connections on configured host/port (`storage.postgres` or `storage.database_url`).
  - Ensure `max_connections` in `postgresql.conf` is sized appropriately for configured worker counts (`workers` in `ingest_oai`).
  - Batch insertions in `reconciliator` use idempotent `ON CONFLICT DO NOTHING` / `DO UPDATE` semantics with connection pooling.

### Issue 5: Missing Language Suffixes on Modern Ontologies
- **Symptom**: Cataloging rules require `yso/fin` or `yso/swe` in `$2`, but only `yso` is emitted.
- **Remedy**:
  - Ensure `reconcile.fold_legacy: true`.
  - The folding engine automatically checks the source vocabulary (`ysa` $\to$ `yso/fin`, `allars` $\to$ `yso/swe`, `kaunokki` $\to$ `slm/fin`). If the source record does not carry a legacy `$2`, ensure the MARC record has language indicator in controlfield `008` or field `041` so the document language is detected.

### Issue 6: Vector Daemon Socket Unreachable or Stale Lockfile
- **Symptom**: CLI pipelines hang or report `ConnectionRefusedError` when attempting to communicate with vector daemon.
- **Cause**: An abrupt machine reboot or ungraceful termination (`kill -9`) may leave an orphaned `.sock` or `.lock` file in `/tmp`.
- **Remedy**:
  - Check daemon status: `reconciliator daemon status -c config.yaml`.
  - Restart the daemon cleanly: `reconciliator daemon restart -c config.yaml`.
  - If a stale socket file remains without an active PID, remove the socket: `rm /tmp/rec_vectord_*.sock /tmp/rec_vectord_*.lock`.

### Issue 7: Vector Daemon Process Shuts Down Unexpectedly
- **Symptom**: `reconciliator daemon status` reports `○ OFFLINE` after running earlier.
- **Cause**: This is normal, intended behavior governed by the idle watchdog (`idle_timeout_seconds: 1800`). When no queries or indexing commands are received for 30 minutes, the daemon flushes indices to disk, cleans up its socket, and terminates to free GPU VRAM and system memory.
- **Remedy**:
  - With `auto_start: true`, running any pipeline (`reconcile`, `ingest-oai`, etc.) will re-spawn the daemon automatically without manual intervention.
  - To keep the daemon running indefinitely on dedicated servers, set `idle_timeout_seconds: null` in `config.yaml` or manage it as a system service.

### Issue 8: macOS Socket Path Too Long (`AF_UNIX path too long`)
- **Symptom**: `OSError: AF_UNIX path too long` on macOS during daemon initialization.
- **Cause**: macOS Darwin strictly caps Unix Domain Socket paths at 104 characters (`sizeof(sun_path) == 104`). Setting a long nested path in `storage.vector_engine.daemon.socket_path` exceeds this operating system limit.
- **Remedy**:
  - Leave `socket_path: null` in `config.yaml`. `reconciliator` automatically computes a deterministic SHA-256 workspace hash and binds to `/tmp/rec_vectord_<hash>.sock` (under 40 characters), fully compliant with Darwin limits.

### Issue 9: Target Author Rejected as Ambiguous Homonym
- **Symptom**: Author candidate achieves high dense vector similarity ($\ge 0.90$) during Tier 3 standalone fallback, but the reconciliation report emits `action: none` with match note `ambiguous_homonyms_detected`.
- **Cause**: Two or more distinct candidate agents in `agent.index` match within the score margin (`score_margin: 0.03`) of the top candidate, and neither candidate could be unambiguously distinguished in-memory or via PostgreSQL relational evidence.
- **Remedy**:
  - Check whether target MARC records carry life dates in 100/700 `$d` or persistent academic identifier (ORCID in `$1`); matching dates or ORCID resolves candidates immediately.
  - Provide occupational titles in repeatable `$c` (e.g. `professori`, `lääkäri`) or institutional affiliations in `$u` (e.g. `Helsingin yliopisto`); candidate distributions will be evaluated via `verify_qualifiers` and `verify_affiliations`.
  - Ensure subject headings in MARC fields 600–655 match supported vocabularies configured in `supported_subject_vocabularies`; the `verify_subjects` guard will evaluate subject overlap to resolve ambiguity in Tier 1.


