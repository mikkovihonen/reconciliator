# Operational Runbook: Reconciliator

`reconciliator` is a high-performance, offline-first bibliographic authority reconciliation and URI enrichment engine designed for Finnish cataloging ecosystems (**Melinda**, **Fennica**, **BIBRA**, **FINTO**).

This runbook provides complete operational guidance for system administrators and cataloging engineers: data prerequisites, configuration reference, CLI commands with realistic examples, deployment workflows, and troubleshooting procedures.

---

## Table of Contents

1. [Architecture & Storage Prerequisites](#1-architecture--storage-prerequisites)
   - [Storage Components](#storage-components)
   - [Hardware & Acceleration](#hardware--acceleration)
   - [External Data Dumps & Upstream Sources](#external-data-dumps--upstream-sources)
2. [Complete Configuration Surface (`config.yaml`)](#2-complete-configuration-surface-configyaml)
   - [Storage Section (`storage`)](#storage-section-storage)
   - [Embedding Section (`embedding`)](#embedding-section-embedding)
   - [FINTO Downloader Section (`download_finto`)](#finto-downloader-section-download_finto)
   - [FINTO Ingestion Section (`ingest_finto`)](#finto-ingestion-section-ingest_finto)
   - [OAI-PMH & MARCXML Ingestion Section (`ingest_oai`)](#oai-pmh--marcxml-ingestion-section-ingest_oai)
   - [Wikidata Ingestion Section (`ingest_wikidata`)](#wikidata-ingestion-section-ingest_wikidata)
   - [Reconciliation Section (`reconcile`)](#reconciliation-section-reconcile)
   - [Telemetry Section (`telemetry`)](#telemetry-section-telemetry)
   - [Production Example Configurations](#production-example-configurations)
3. [CLI Command Reference](#3-cli-command-reference)
   - [`reconciliator download-finto`](#reconciliator-download-finto)
   - [`reconciliator ingest-finto`](#reconciliator-ingest-finto)
   - [`reconciliator ingest-melinda`](#reconciliator-ingest-melinda)
   - [`reconciliator ingest-wikidata`](#reconciliator-ingest-wikidata)
   - [`reconciliator reconcile`](#reconciliator-reconcile)
   - [`reconciliator run`](#reconciliator-run)
   - [`reconciliator status`](#reconciliator-status)
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

`reconciliator` operates strictly offline during reconciliation, storing all authority graphs and vector indices in local SQLite databases and FAISS files:

| File | Type | Purpose | Typical Size |
|---|---|---|---|
| `data/authority.db` | SQLite 3 (WAL mode) | Normalized authority records (Melinda / Asteri, KANTO, Wikidata, ISNI, VIAF), agent literals, identifiers (ISBN, ISSN, URN), and serialized prompt strings. | 500 MB – 8 GB |
| `data/authority.faiss` | FAISS IndexFlatIP | Dense vector index storing normalized embedding vectors for semantic similarity matching. | 200 MB – 3 GB |
| `data/finto.db` | SQLite 3 (WAL mode) | FINTO ontologies (YSO, YSO-paikat, KAUNO, MUSO, SLM, SEKO), legacy migration tables (YSA, Allars, Kaunokki, MUSA), dynamic relator terms (`relator_terms`), and multilingual / bridge aliases (`finto_aliases`). | 100 MB – 500 MB |
| `data/progress.db` | SQLite 3 (WAL mode) | Real-time task progress, stages, processed byte/record counters, and heartbeat telemetry. | < 5 MB |

### Hardware & Acceleration

- **CPU**: Modern multi-core x86_64 or ARM64 processor (8+ cores recommended for multi-worker ingestion and reconciliation).
- **RAM**: Minimum 8 GB (16 GB or 32 GB recommended when handling full national bibliography indexes).
- **Disk**: NVMe SSD strongly recommended for SQLite WAL and vector indexing throughput.
- **Hardware Acceleration (`device`)**:
  - `auto`: Automatically picks CUDA if available, then Apple Silicon MPS (Metal Performance Shaders), otherwise CPU.
  - `cuda`: NVIDIA GPU with PyTorch CUDA runtime.
  - `mps`: Apple Silicon GPU (M1/M2/M3/M4) via Metal.
  - `cpu`: Pure CPU execution.

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
- **Acquisition**: Can be fetched automatically via `reconciliator download-finto`.

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

Defines file paths for persistent SQLite databases and the FAISS vector index.

```yaml
storage:
  authority_db: "data/authority.db"      # Authority records and textual metadata
  finto_db: "data/finto.db"              # FINTO concepts, migration tables, and aliases
  progress_db: "data/progress.db"        # Real-time task progress and telemetry
  index_path: "data/authority.faiss"     # FAISS IndexFlatIP dense vector index
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `authority_db` | `Path` | `data/authority.db` | Path to SQLite authority database. |
| `finto_db` | `Path` | `data/finto.db` | Path to dedicated FINTO ontologies database. |
| `progress_db` | `Path` | `data/progress.db` | Path to progress and heartbeat tracking database. |
| `index_path` | `Path` | `data/authority.faiss` | Path to serialized FAISS dense vector index. |

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

### FINTO Downloader Section (`download_finto`)

Configures the automated download of FINTO vocabularies and ontologies.

```yaml
download_finto:
  api_url: "https://api.finto.fi/rest/v1"
  output_dir: "data/finto"
  vocabularies:
    - "yso"
    - "yso-paikat"
    - "slm"
    - "kauno"
    - "muso"
    - "seko"
  format: "text/turtle"
  timeout_seconds: 120.0
  overwrite: false
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_url` | `str` | `https://api.finto.fi/rest/v1` | Base URL of Finto's Skosmos REST API. |
| `output_dir` | `Path` | `data/finto` | Target directory where downloaded `.ttl` dumps are stored. |
| `vocabularies` | `list[str]` | `['yso', 'yso-paikat', 'slm', 'kauno', 'muso', 'seko']` | Default list of vocabulary IDs to download. |
| `format` | `str` | `text/turtle` | Dump format mime-type. |
| `timeout_seconds`| `float`| `120.0` | HTTP request stream timeout in seconds. |
| `overwrite` | `bool` | `false` | When false, skips files that already exist on disk. |

---

### FINTO Ingestion Section (`ingest_finto`)

Specifies input files or directories for parsing FINTO ontologies into `finto.db`.

```yaml
ingest_finto:
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
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `input_path` | `Path` | `data/finto` | File path to a single `.ttl` dump or directory of `.ttl` files. |
| `vocabularies` | `list[str]` | `['yso', 'yso-paikat', ...]` | Target modern ontologies to parse. |
| `legacy_thesauri` | `list[str]` | `['ysa', 'allars', ...]` | Legacy thesauri with mapping tables (`skos:exactMatch`). |

---

### OAI-PMH & MARCXML Ingestion Section (`ingest_oai`)

Configures harvesting or reading authority records from one or multiple OAI-PMH endpoints or local MARCXML files, generating dense FAISS embeddings with idempotent updates.

```yaml
ingest_oai:
  melinda:
    enabled: true
    source_type: "oai"        # "oai" (OAI-PMH harvest) or "file" (local MARCXML)
    input_path: null          # Path to local MARCXML file if source_type is "file"
    oai_url: "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib"
    metadata_prefix: "marc21"
    set_spec: null            # Optional OAI set specifier
    id_source: "marc_001"     # "marc_001" | "oai_identifier" | "auto"
    workers: 8                # Number of parallel workers for chunk harvesting (recommended: 8-16)
    request_delay: 0.1        # Rate limit delay between requests per worker in seconds
    from_date: null           # "YYYY-MM-DD" selective harvest start (auto-discovered via Identify if null)
    until_date: null          # "YYYY-MM-DD" selective harvest end (defaults to current date if null)
    slice_days: 30            # Temporal slice window per worker in days
    batch_size: 500           # Authority database insertion batch size
    timeout: 60.0
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `source_type` | `Literal` | `oai` | `oai` for live OAI-PMH harvest, `file` for local XML file. |
| `input_path` | `Path` | `None` | Path to local MARCXML authority file if `source_type` is `file`. |
| `oai_url` | `str` | `https://.../bib` | Endpoint URL for OAI-PMH service. |
| `metadata_prefix`| `str` | `marc21` | OAI-PMH metadata format prefix. |
| `set_spec` | `str` | `None` | Optional OAI-PMH set filter. |
| `workers` | `int` | `8` | Concurrency for chunked OAI-PMH downloads. |
| `request_delay` | `float` | `0.1` | Rate limit pause per worker thread between consecutive requests. |
| `from_date` | `str` | `None` | ISO date `YYYY-MM-DD` (auto-queries `Identify` endpoint if null). |
| `until_date` | `str` | `None` | ISO date `YYYY-MM-DD` (defaults to current UTC date if null). |
| `slice_days` | `int` | `30` | Temporal chunk partition size in days per worker. |
| `batch_size` | `int` | `500` | Database insert batch size. |

---

### Wikidata Ingestion Section (`ingest_wikidata`)

Configures streaming from official Wikidata JSON gzip dumps (`latest-all.json.gz`). Operates in true $O(1)$ memory using two sequential streaming passes:
1. **Pass 1 (Author Discovery)**: Scans the compressed dump and indexes author entities (humans, organizations, places) with their QID, family name, ISNI, VIAF, and Asteri ID into the local SQLite `wikidata_authors` table.
2. **Pass 2 (Bibliographic Streaming & Hydration)**: Streams bibliographic records, hydra-populates author names and identifiers directly from SQLite, encodes dense embeddings, and inserts them into `authority.db` and FAISS.

```yaml
ingest_wikidata:
  dump_path: "data/wikidata/latest-all.json.gz"
  batch_size: 1000
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dump_path` | `Path` | `data/wikidata/latest-all.json.gz` | Path to `.json.gz` or `.json` Wikidata entity dump. |
| `batch_size` | `int` | `1000` | Number of entities batched for embedding and indexing. |

---

### Reconciliation Section (`reconcile`)

Defines target records, output formats, hybrid scoring, and report destinations.

```yaml
reconcile:
  target_file: "data/target_records.xml"
  target_format: "auto"        # "auto" | "marcxml" | "bibra"
  output_file: "data/enriched_records.xml"
  output_format: "auto"        # "auto" | "marcxml" | "bibra"
  enrich: true                 # true: inject novel $0/$1 URIs; false: audit & report only
  fold_legacy: true            # true: modernize legacy thesauri headings via finto.db
  threshold: 0.85              # Minimum acceptance score [0.0, 1.0]

  agent_reports:
    detailed_file: "data/reports/agent_reconciliation_uris.tsv"
    summary_file: "data/reports/agent_reconciliation_summary.tsv"

  subject_reports:
    detailed_file: "data/reports/subject_reconciliation_uris.tsv"
    summary_file: "data/reports/subject_reconciliation_summary.tsv"

  sparse_rerank:
    enabled: true              # Toggle BM25 sparse reranking on/off
    dense_weight: 0.6          # Dense semantic cosine weight [0.0, 1.0]
    sparse_weight: 0.4         # Sparse BM25 lexical weight [0.0, 1.0] (must sum to 1.0)
    top_k: 50                  # Number of dense candidates retrieved from FAISS
    short_circuit:
      enabled: true            # Fast-path shortcut for near-identical matches
      threshold: 0.98          # Score threshold bypassing sparse reranking
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
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `target_file` | `Path` | *Required* | Path to target records (MARCXML or BIBRA JSON/JSONL). |
| `target_format` | `Literal` | `auto` | Format: `auto` (extension-based), `marcxml`, or `bibra`. |
| `output_file` | `Path` | `None` | Optional path to write enriched records. |
| `output_format` | `Literal` | `auto` | Output format: `auto`, `marcxml`, or `bibra`. |
| `enrich` | `bool` | `true` | When true, injects missing authority `$0` and `$1` subfields. |
| `fold_legacy` | `bool` | `true` | When true, modernizes legacy subject thesauri (YSA, Allars, etc.). |
| `threshold` | `float` | `0.85` | Acceptance confidence threshold [0.0, 1.0]. |
| `agent_reports` | `ReportSet`| *Required* | File paths for agent detailed and summary TSV reports. |
| `subject_reports`| `ReportSet`| *Required* | File paths for subject detailed and summary TSV reports. |
| `link_policy` | `Literal` | `priority_fallback` | `priority_fallback` links top-priority vocabulary; `all` links all verified URIs. |

---

### Telemetry Section (`telemetry`)

Controls progress monitoring heartbeat and watch interval polling.

```yaml
telemetry:
  heartbeat_interval_seconds: 1.0
  watch_interval_seconds: 1.0
```

---

### Production Example Configurations

#### Full Production Profile (`config.prod.yaml`)

```yaml
storage:
  authority_db: "/var/data/reconciliator/authority.db"
  finto_db: "/var/data/reconciliator/finto.db"
  progress_db: "/var/data/reconciliator/progress.db"
  index_path: "/var/data/reconciliator/authority.faiss"

embedding:
  model_name: "TurkuNLP/sbert-cased-finnish-paraphrase"
  device: "auto"
  batch_size: 128

download_finto:
  api_url: "https://api.finto.fi/rest/v1"
  output_dir: "/var/data/reconciliator/finto"
  vocabularies: ["yso", "yso-paikat", "slm", "kauno", "muso", "seko"]
  overwrite: false

ingest_finto:
  input_path: "/var/data/reconciliator/finto"
  vocabularies: ["yso", "yso-paikat", "kauno", "muso", "slm", "seko"]
  legacy_thesauri: ["ysa", "allars", "kaunokki", "musa"]

ingest_melinda:
  source_type: "oai"
  oai_url: "https://oai-pmh.api.melinda.kansalliskirjasto.fi/bib"
  metadata_prefix: "marc21"
  workers: 8
  slice_days: 15
  batch_size: 1000

reconcile:
  target_file: "/var/data/incoming/batch_2026_09.xml"
  output_file: "/var/data/enriched/batch_2026_09_enriched.xml"
  enrich: true
  fold_legacy: true
  threshold: 0.88
  agent_reports:
    detailed_file: "/var/reports/agent_detailed.tsv"
    summary_file: "/var/reports/agent_summary.tsv"
  subject_reports:
    detailed_file: "/var/reports/subject_detailed.tsv"
    summary_file: "/var/reports/subject_summary.tsv"
  sparse_rerank:
    enabled: true
    dense_weight: 0.65
    sparse_weight: 0.35
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

### `reconciliator download-finto`

Downloads FINTO value vocabularies and ontologies in SKOS Turtle format from Finto's Skosmos API or fast static distribution dumps.

```bash
# 1. Discover all available vocabularies on Finto and print a table
reconciliator download-finto --list

# 2. Download default vocabularies defined in config.yaml to data/finto
reconciliator download-finto -c config.yaml

# 3. Download specific vocabularies (e.g. YSO, YSO-paikat, and SLM)
reconciliator download-finto -c config.yaml -v yso -v yso-paikat -v slm

# 4. Download into a custom output directory and force overwrite existing files
reconciliator download-finto -c config.yaml -o /tmp/finto_dumps -v seko --force

# 5. Discover and download ALL published vocabularies on Finto
reconciliator download-finto -c config.yaml --all

# 6. Download and immediately chain ingestion into finto.db
reconciliator download-finto -c config.yaml -v yso -v slm --ingest
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-v, --vocab TEXT`: Specific vocabulary ID to download (repeatable).
- `-o, --output-dir PATH`: Directory where `.ttl` files will be saved.
- `--all`: Query the API and download every vocabulary published by Finto.
- `--list`: Print a formatted table of all available Finto vocabularies and exit.
- `--force`: Overwrite existing files on disk.
- `--ingest`: Automatically trigger `ingest-finto` on the downloaded directory upon completion.

---

### `reconciliator ingest-finto`

Parses SKOS Turtle dumps, extracts concepts, preferred and alternate labels, and legacy migration tables (`skos:exactMatch`, `dcterms:isReplacedBy`). Ingests bundled Library of Congress bridge files, populates `relator_terms`, and indexes `finto_aliases`.

```bash
# Ingest FINTO ontologies from directory or file configured in config.yaml
reconciliator ingest-finto -c config.yaml
```

#### What happens during ingestion:
1. Parses all `.ttl` dumps in `input_path` (e.g. `yso.ttl`, `yso-paikat.ttl`, `slm.ttl`, `kauno.ttl`, `muso.ttl`, `seko.ttl`).
2. Auto-ingests bundled bridge files from `src/reconciliator/vocab/bridges/` (mapping LoC countries, languages, and issuance modes to YSO concepts).
3. Ingests Metatietosanasto (MTS) relator terms into `relator_terms`.
4. Rebuilds `finto_aliases` table indexing Finnish, Swedish, and English preferred and alternate labels for bilingual resolution (e.g. `Malax` $\to$ `Maalahti`).

---

### `reconciliator ingest-oai`

Harvests or reads authority records from configured OAI-PMH or MARCXML sources (e.g. Melinda, Koha, Libris), normalizes agent names, generates dense embeddings using `SentenceEmbedder`, and builds the FAISS vector index with direct ID mapping and idempotent updates.

```bash
# Ingest single configured source (e.g. melinda)
reconciliator ingest-oai -c config.yaml -s melinda

# Ingest all enabled sources in sequence
reconciliator ingest-oai -c config.yaml --all

# Override harvest date filters on CLI
reconciliator ingest-oai -c config.yaml -s melinda --from 2024-01-01 --until 2024-06-01
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `-s, --source TEXT`: Name of specific configured OAI source to ingest.
- `-a, --all`: Ingest all enabled sources declared under `ingest_oai`.
- `--from TEXT`: Selective harvest start date (`YYYY-MM-DD`).
- `--until TEXT`: Selective harvest end date (`YYYY-MM-DD`).

---

### `reconciliator ingest-wikidata`

Streams a compressed Wikidata JSON dump (`latest-all.json.gz`), filters entities relevant to Finnish bibliographic authority work (agents with VIAF, GND, ISNI, and YSO P2347 claims), generates dense embeddings, and appends them to `authority.db` and `authority.faiss`.

```bash
# Stream and index Wikidata dump configured in config.yaml
reconciliator ingest-wikidata -c config.yaml
```

---

### `reconciliator reconcile`

Performs offline reconciliation on a target bibliographic file (MARCXML or BIBRA JSON). Modernizes legacy subject headings, aligns agents, matches candidates via hybrid dense FAISS + sparse BM25 scoring, enriches records with `$0` and `$1` subfields, and writes comprehensive TSV reports.

```bash
# 1. Standard reconciliation with enrichment and report generation
reconciliator reconcile -c config.yaml

# 2. Audit-only mode (reports generated, but target XML unmodified)
# Set 'enrich: false' in config.yaml, then run:
reconciliator reconcile -c config.audit.yaml
```

#### Generated Reports:
1. **Agent Detailed TSV** (`agent_reports.detailed_file`):
   - Row per agent occurrence: target record ID, field tag (100/700/110/710), raw name, normalized surname, role, matched candidate ID, candidate source, confidence score, injected `$0` / `$1` URIs, action (`injected`, `existing`, `unmatched`).
2. **Agent Summary TSV** (`agent_reports.summary_file`):
   - High-level KPIs: total records, total agents, matched percentage, break-down by vocabulary (KANTO, ISNI, VIAF, Wikidata).
3. **Subject Detailed TSV** (`subject_reports.detailed_file`):
   - Row per subject occurrence: source heading, source vocab (`ysa`, `allars`, `kaunokki`), target modern label, target vocab (`yso/fin`, `slm/fin`), target URI, mapping type (`exact_match`, `alias_match`, `existing_canonical`), tag change (`650` $\to$ `655`).
4. **Subject Summary TSV** (`subject_reports.summary_file`):
   - Modernization KPIs: legacy headings folded, canonical terms retained, unmapped rate.

---

### `reconciliator run`

Executes the entire end-to-end pipeline in sequence as configured in your YAML file:
1. `ingest-finto` (if `ingest_finto` section is present)
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

In `--watch` mode, `reconciliator status` launches an interactive, full-screen Terminal User Interface (TUI) utilizing Rich's alternate screen buffer. The dashboard displays live task stages, color-coded status badges (`● RUNNING`, `✔ COMPLETED`, `✖ FAILED`), progress percentages, items and megabytes processed/total, throughput rates (items/s), last heartbeat timestamps, aggregate processing rates, and an active diagnostics panel if any background pipeline encounters an error.

```bash
# 1. Print snapshot table of current tasks
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
┏━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Task ID                  ┃ Type           ┃ Status    ┃ Stage            ┃ Progress   ┃ Elapsed       ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ finto-download-172596950 │ finto_ingest   │ completed │ done             │ 6/6 vocabs │ 00:00:14      │
│ melinda-ingest-172596955 │ oai_pmh_harves │ completed │ indexing         │ 100.0%     │ 00:03:42      │
│ reconciliation-172596960 │ reconciliation │ running   │ reconciling      │ 78.4%      │ 00:01:15      │
└──────────────────────────┴────────────────┴───────────┴──────────────────┴────────────┴───────────────┘
```

#### Options:
- `-c, --config PATH`: Path to YAML configuration file (default: `config.yaml`).
- `--task-id TEXT`: Query status of a specific task ID.
- `-w, --watch`: Launch continuous live full-screen TUI monitor (exits automatically when all tasks reach completed or failed state, or upon Ctrl+C).
- `-i, --interval FLOAT`: Watch polling and screen refresh interval in seconds (defaults to `telemetry.watch_interval_seconds` from `config.yaml`).
- `--json`: Emit output as raw JSON lines / objects.
- `--prune, --prune-completed`: Prune completed tasks from `progress.db` after displaying status.

---

### `reconciliator prune`

Prunes task telemetry records and pipeline run history from `progress.db` while safeguarding active running tasks. Automatically executes SQLite `VACUUM` when rows are deleted to reclaim disk space.

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

> [!NOTE]
> `reconciliator prune-telemetry` is available as a hidden CLI alias providing identical functionality.

---

### `reconciliator reset`

Resets and cleans SQLite databases and vector index files.

```bash
# 1. Interactively reset all configured databases and vector indices (prompts [y/N])
reconciliator reset -c config.yaml

# 2. Reset all storage non-interactively (ideal for test setups and scripts)
reconciliator reset -c config.yaml --all --yes

# 3. Reset authority database and FAISS index only (leaves finto.db intact)
reconciliator reset -c config.yaml --authority -y

# 4. Reset finto database only
reconciliator reset -c config.yaml --finto -y

# 5. Reset progress and telemetry database only
reconciliator reset -c config.yaml --progress -y
```

#### Options:
- `-a, --all`: Reset all storage components (`authority.db`, `finto.db`, `progress.db`, `authority.faiss`).
- `--authority`: Reset `authority.db` and FAISS vector index only.
- `--finto`: Reset `finto.db` only.
- `--progress`: Reset `progress.db` task telemetry only.
- `-y, --yes`: Bypass interactive confirmation prompt.

---

## 4. Standard Operational Workflows

### Workflow A: Cold-Start Bootstrapping

Follow this workflow when setting up a new production instance:

```bash
# Step 1: Create storage directory
mkdir -p data/finto data/reports

# Step 2: Download canonical FINTO vocabularies and ingest them into finto.db
reconciliator download-finto -c config.yaml -v yso -v yso-paikat -v slm -v kauno -v muso -v seko --ingest

# Step 3: Ingest Melinda authority records and build FAISS vector index
reconciliator ingest-melinda -c config.yaml

# Step 4: Run reconciliation on target records
reconciliator reconcile -c config.yaml

# Step 5: Check output file and reports
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
   - Run `reconciliator download-finto` to download `.ttl` dumps.
   - Cache HuggingFace embedding model:
     ```python
     from sentence_transformers import SentenceTransformer

     model = SentenceTransformer("TurkuNLP/sbert-cased-finnish-paraphrase")
     model.save("/path/to/local/model_dir")
     ```
2. **Transfer to Offline Host**:
   - Transfer `finto.db`, `authority.db`, `authority.faiss`, and `/path/to/local/model_dir`.
3. **Configuration**:
   - Set `model_name: "/path/to/local/model_dir"`.
   - Set `source_type: "file"` for Melinda ingestion.
4. **Run Reconciliation**:
   - Execute `reconciliator reconcile -c config.offline.yaml`.
   - **Zero network requests** are made during execution.

---

### Workflow D: Quality Audit & Discrepancy Review

To run an audit report without modifying the original records:

1. In `config.yaml`, set `enrich: false`.
2. Run `reconciliator reconcile -c config.yaml`.
3. Inspect `data/reports/agent_reconciliation_summary.tsv`:
   ```bash
   cat data/reports/agent_reconciliation_summary.tsv
   ```
4. Filter detailed report for ambiguous or borderline matches (score between 0.85 and 0.90):
   ```bash
   awk -F'\t' '$8 >= 0.85 && $8 < 0.90 {print $1, $3, $7, $8}' data/reports/agent_reconciliation_uris.tsv
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

### Issue 2: FAISS Index Metadata Mismatch
- **Symptom**: `ValueError: Index metadata mismatch: expected model ... but found ...`.
- **Cause**: The FAISS index was generated using an embedding model or serialization template different from the one currently configured.
- **Remedy**:
  - Re-run `ingest-oai` to regenerate index vectors with the new model.
  - Or, if only the Jinja2 template was modified without affecting vector semantics, set `force_template: true` in the `embedding` config section.

### Issue 3: Out of Memory (OOM) During Ingestion
- **Symptom**: Process killed by OS during dense embedding generation.
- **Remedy**:
  - Lower `embedding.batch_size` (e.g. from `64` to `32` or `16`).
  - If running on CPU or low-memory instance, lower `workers` in `ingest_oai` (e.g. from `8` to `1` or `2`).

### Issue 4: SQLite `database is locked`
- **Symptom**: SQLite operational error during high-concurrency ingestion.
- **Remedy**:
  - `reconciliator` automatically configures SQLite WAL (`Write-Ahead Logging`) with 60-second busy timeouts.
  - Ensure databases are stored on local SSD filesystems rather than network-attached mounts (NFS/SMB) that do not support reliable SQLite file locking.

### Issue 5: Missing Language Suffixes on Modern Ontologies
- **Symptom**: Cataloging rules require `yso/fin` or `yso/swe` in `$2`, but only `yso` is emitted.
- **Remedy**:
  - Ensure `reconcile.fold_legacy: true`.
  - The folding engine automatically checks the source vocabulary (`ysa` $\to$ `yso/fin`, `allars` $\to$ `yso/swe`, `kaunokki` $\to$ `slm/fin`). If the source record does not carry a legacy `$2`, ensure the MARC record has language indicator in controlfield `008` or field `041` so the document language is detected.

