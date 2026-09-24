# Operational Runbook: Reconciliator

`reconciliator` is an automated, high-performance bibliographic authority reconciliation, WEMI multiplex clustering, and URI enrichment engine designed for Finnish and international cataloging ecosystems (**Melinda**, **Fennica**, **BIBRA**, **FINTO**).

This runbook provides complete operational guidance for system administrators, DevOps engineers, and cataloging teams: prerequisites, configuration reference, CLI commands with realistic examples, deployment workflows, and troubleshooting procedures.

---

## 1. Architecture & Storage Prerequisites

### Storage Architecture: Unified PostgreSQL Engine
`reconciliator` uses **PostgreSQL 15+** as its single, unified database engine. All 43 relational tables—including WEMI entities, authority records, vocabulary mappings, corpus clusters, constituent linkages, and task progress telemetry—reside in PostgreSQL.

| Component | Storage Engine | Purpose | Sizing Guidelines |
|---|---|---|---|
| PostgreSQL Authority DB | PostgreSQL 15+ (`pg_trgm`) | Ingested WEMI entities (`works`, `expressions`, `manifestations`, `items`), polymorphic linkages (`identifiers`, `entity_keys`, `contributions`), and controlled vocabularies (`concepts`, `concept_labels`, `relator_terms`). | 5 GB – 50 GB |
| PostgreSQL Corpus Subsystem | PostgreSQL 15+ | Intra-corpus WEMI clusters (`corpus_work_clusters`, `corpus_expression_clusters`, `corpus_manifestation_clusters`), record linkages (`corpus_linkages`), constituent links (`corpus_constituent_linkages`), and whole-part cluster edges (`corpus_work_relationships`). | 2 GB – 20 GB |
| Vector Indices | Local Mapped Files (FAISS / PyTorch) | Pre-computed dense embeddings for nearest-neighbor semantic candidate retrieval (`work_vectors`, `manifestation_vectors`, `agent_vectors`). | 500 MB – 10 GB |

### Hardware & Acceleration
- **Memory**: Minimum 16 GB RAM (32 GB+ recommended for large-scale multi-million record corpora).
- **CPU**: Multi-core processor (8+ cores recommended for parallel OAI-PMH harvesting and BM25 lexical reranking).
- **GPU / Accelerator (Optional)**:
  - Apple Silicon Metal (`mps`): Automatically detected and utilized on macOS for PyTorch dense vector embeddings and MLX LLM judges.
  - NVIDIA CUDA (`cuda`): Automatically detected on Linux servers for PyTorch vector acceleration and transformer Cross-Encoders.
  - CPU Fallback: Robust CPU fallback for containerized or air-gapped environments.

---

## 2. Configuration Surface (`config.yaml` / `defaults.yaml`)

Reconciliator's configuration system is driven by a single-source-of-truth YAML file matching [`src/reconciliator/defaults/defaults.yaml`](../src/reconciliator/defaults/defaults.yaml) (see full reference in [`config.example.yaml`](../config.example.yaml)).

All configuration models are strongly typed Pydantic models organized into domain modules under [`src/reconciliator/config/`](../src/reconciliator/config/) and exported canonically from [`reconciliator.config`](../src/reconciliator/config/__init__.py):

- **`infrastructure.py`**: Hardware compute (`ComputeConfig`), filesystem paths and PostgreSQL storage (`StorageConfig`, `PostgresConfig`), vector search engines (`VectorEngineConfig`, `TorchEngineConfig`, `FAISSEngineConfig`, `USearchEngineConfig`, `VectorDaemonConfig`), logging (`LoggingConfig`), telemetry (`TelemetryConfig`), and source registry entries (`SourceConfig`).
- **`semantics.py`**: Linguistic normalization and biographical qualifiers (`LinguisticConfig`, `BiographicalDatesConfig`), controlled vocabularies and hierarchy (`VocabulariesConfig`, `HierarchyConfig`), and format mutation/predicate policies (`EnrichmentConfig`).
- **`matching.py`**: WEMI multiplex graph clustering (`WEMIClusterConfig`, `WEMIScoringConfig`, `AggregateClusteringConfig`), agent clustering (`AgentClusterConfig`), cross-source work consolidation (`WorkConsolidationConfig`), candidate reranking and LLM judging (`RerankConfig`, `CascadeRouterConfig`, `LLMJudgeConfig`, `ShortCircuitConfig`), and domain-specific funnels (`FunnelsConfig`).
- **`normalization.py`**: Bibliographic record parsing, ISBD punctuation stripping, non-filing articles, volume parsing, roman numerals, and standard identifiers (`RecordNormalizationConfig`, with embedded `linguistic: LinguisticConfig`).
- **`ingestion.py`**: Universal ingest filters (`IngestFilterConfig`), field length truncation (`FieldTruncationConfig`), OAI-PMH harvesting (`OAIIngestConfig`, `OAIDefaultsConfig`), Skosmos downloads (`SkosmosDownloadConfig`), value vocabulary ingestion (`VocabIngestConfig`), and Wikidata streaming (`WikidataIngestConfig`).
- **`authority.py`**: Virtual authority generation, PID allocation, provenance tracking, and serialization (`VirtualAuthorityConfig`, `AgentAuthorityConfig`).
- **`app.py`**: Root [`AppConfig`](../src/reconciliator/config/app.py), offline reconcile configuration ([`ReconcileConfig`](../src/reconciliator/config/app.py)), template/report paths (`TemplateSetPathsConfig`, `ReportSetPathsConfig`), and cross-subsystem validation and propagation.
- **`loader.py`**: Safe YAML loading (`load_config`) and logging setup (`configure_logging`).

> [!NOTE]
> Reconciliator enforces strict configuration schemas without backward-compatibility shims, aliases, or hardcoded fallbacks. All default values derive directly from [`defaults.yaml`](../src/reconciliator/defaults/defaults.yaml).

### 2.1 Storage Configuration
```yaml
storage:
  postgres:
    database_url: "postgresql+psycopg://postgres:postgres@localhost:5432/reconciliator"
    pool_size: 20
    max_overflow: 10
    pool_timeout: 30
    auto_start_container: false  # Set false in production to connect to managed PostgreSQL
```

### 2.2 Embedding & Vector Engine
```yaml
storage:
  vector_engine:
    backend: "auto"     # auto | faiss | torch | usearch
    mode: "embedded"    # embedded | daemon

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "auto"        # auto | cuda | mps | cpu
  batch_size: 64
  template_path: "reconciliator/embedding/templates/default_serialization.j2"
```

### 2.3 Aggregate & Constituent Work Configuration
```yaml
constituents:
  profile: "marc_enhanced"         # Profile for MARCXML
  bibframe_profile: "bibframe_haspart" # Profile for LC BIBFRAME
  bffi_profile: "bffi_haspart"     # Profile for Finnish BFFI (LKD, no bflc:)
  mode: "entities"                 # off | keys_only | entities
  index_works: false               # Keep constituents out of primary work ANN index

reconcile:
  cluster:
    aggregates:
      enabled: true                  # Enable aggregate multiplex scoring
      jaccard_merge_threshold: 0.80  # Constituent overlap required for merge bonus
      jaccard_veto_threshold: 0.30   # Constituent overlap below which records are vetoed
      min_constituents_for_overlap: 3 # Minimum constituents to trigger overlap rules
      merge_bonus_score: 0.95        # Similarity score granted on constituent match
      sequence_alignment: true       # Require LCS track sequence alignment
      lcs_threshold: 0.70            # LCS ratio threshold
      blocking_family: "constituent_work"
```

### 2.4 Candidate Reranking & Cascading
```yaml
reconcile:
  rerank:
    enabled: true
    engine: "cascade"              # cascade | llm_judge | bm25 | fuzzy | cross_encoder | noop
    top_k: 20                      # Number of candidates retrieved from vector ANN
    dense_weight: 0.50             # Weight for dense embedding cosine similarity
    rerank_weight: 0.50            # Weight for reranker score (dense_weight + rerank_weight = 1.0)
    max_year_difference: 5         # Hard discriminator maximum publication year tolerance
    short_circuit:
      enabled: true
      threshold: 0.98
```

---

## 3. CLI Command Reference

All operations are invoked through `reconciliator` using the unified Typer CLI:

### 1. `reconciliator run`
Executes the complete pipeline end-to-end in automated sequence:
`ingest-vocab` $\rightarrow$ `ingest-oai` $\rightarrow$ `ingest-wikidata` $\rightarrow$ `build-index` $\rightarrow$ `reconcile`.
```bash
reconciliator run -c config.yaml
```

### 2. `reconciliator serve`
Launches the high-throughput FastAPI REST API service for online single-record and batch reconciliation.
```bash
reconciliator serve -c config.yaml --host 0.0.0.0 --port 8000
```
- Options:
  - `--host`, `-h`: Bind address (default: `127.0.0.1`).
  - `--port`, `-p`: Bind port (default: `8000`).
  - `--no-auth`: Disables Entra ID token authentication for local development.

### 3. `reconciliator reconcile`
Performs intra-corpus clustering, candidate matching, agent alignment, output enrichment, and TSV report generation.
```bash
reconciliator reconcile -c config.yaml --source koha_music --out-dir ./output/
```

### 4. `reconciliator consolidate-works`
Unifies cross-source works across Melinda, Asteri, and Wikidata into canonical work identities using Wikidata QIDs, strong identifiers, and metadata scoring.
```bash
reconciliator consolidate-works -c config.yaml
```

### 5. `reconciliator cluster-agents`
Clusters raw agent authority records into canonical agent identity clusters with consensus attribute frequencies.
```bash
reconciliator cluster-agents -c config.yaml
```

### 6. `reconciliator build-index` & `build-agent-index`
Generates dense vector embeddings and constructs FAISS / PyTorch ANN indices for stored works, manifestations, and agents.
```bash
reconciliator build-index -c config.yaml
reconciliator build-agent-index -c config.yaml
```

### 7. `reconciliator download-skosmos` & `ingest-vocab`
Downloads controlled vocabularies (YSO, SLM, KANTO, SEKO) from Finto Skosmos REST APIs and ingests them into PostgreSQL.
```bash
reconciliator download-skosmos -c config.yaml
reconciliator ingest-vocab -c config.yaml
```

### 8. `reconciliator ingest-oai`
Harvests bibliographic records from OAI-PMH, ResourceSync, or local file dumps, performing WEMI decomposition and staging.
```bash
# Harvest all configured sources
reconciliator ingest-oai --all -c config.yaml

# Harvest a specific named source incrementally
reconciliator ingest-oai melinda -c config.yaml
```

### 9. `reconciliator ingest-wikidata`
Streams Wikidata entity dumps, extracting authority persons, works, and persistent identifiers.
```bash
reconciliator ingest-wikidata -c config.yaml
```

### 10. `reconciliator status`
Displays active tasks, throughput, memory usage, and completion ETA from the database telemetry table.
```bash
reconciliator status -c config.yaml --watch
```

### 11. `reconciliator prune`
Removes completed or stale task records from `task_progress`.
```bash
reconciliator prune --completed --older-than 7d -y -c config.yaml
```

### 12. `reconciliator reset`
Drops and recreates all database tables. **Destructive.**
```bash
reconciliator reset -c config.yaml --yes
```

---

## 4. Standard Operational Workflows

### Workflow A: Cold-Start Bootstrapping
Initial setup from an empty database to fully indexed authority grounding:
```bash
# 1. Initialize database tables
reconciliator reset -c config.yaml --yes

# 2. Download and ingest Finto vocabularies
reconciliator download-skosmos -c config.yaml
reconciliator ingest-vocab -c config.yaml

# 3. Ingest national authority union catalogue (Melinda)
reconciliator ingest-oai melinda -c config.yaml

# 4. Consolidate cross-source works and cluster agents
reconciliator consolidate-works -c config.yaml
reconciliator cluster-agents -c config.yaml

# 5. Build vector ANN indices
reconciliator build-index -c config.yaml
reconciliator build-agent-index -c config.yaml
```

### Workflow B: Production Target Reconciliation
Reconciling and enriching an incoming library catalog harvest:
```bash
# 1. Harvest target catalog
reconciliator ingest-oai local_catalog -c config.yaml

# 2. Run reconciliation, clustering, and enrichment
reconciliator reconcile -c config.yaml --source local_catalog --out-dir ./enriched_output/
```

---

## 5. Troubleshooting & Diagnostics

### PostgreSQL Connection & Pool Exhaustion
- **Symptom**: `OperationalError: connection to server failed: Connection refused` or `TimeoutError: QueuePool limit exceeded`.
- **Remedy**:
  - Verify PostgreSQL service status: `systemctl status postgresql` or `pg_isready`.
  - Ensure `pg_trgm` extension is installed: `CREATE EXTENSION IF NOT EXISTS pg_trgm;`.
  - Increase `storage.postgres.pool_size` (e.g. from 20 to 50) in `config.yaml` when running high worker concurrency.

### OAI-PMH Harvest Interruption & Resumption
- **Symptom**: Network timeout during harvesting of large catalogues.
- **Remedy**: Reconciliator checkpoints progress automatically to `harvest_state`. Simply re-run `reconciliator ingest-oai <source> -c config.yaml`; the harvester reads the stored resumption token and resumes from the last successfully written slice.

### VRAM & Accelerator Optimization
- **Symptom**: PyTorch CUDA out-of-memory or Metal MPS allocation failure during vector indexing.
- **Remedy**: Decrease `embedding.batch_size` (e.g. from 128 to 32) in `config.yaml`, or set `embedding.device: "cpu"` to run in system RAM.
