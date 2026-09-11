# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Configurable Candidate Reranking Engines**:
  - Abstract `RerankScorer` protocol (`src/reconciliator/rerank/`) decoupling text-pair similarity scoring from domain metadata discriminators.
  - Pluggable scoring backends:
    - `bm25`: Lexical matching with term-frequency saturation, length normalization, and mathematically sound non-negative IDF scoring.
    - `cross_encoder`: Deep transformer cross-attention reranking via `sentence_transformers.CrossEncoder` with hardware acceleration (`cuda`, `mps`, `cpu`) and batch inference.
    - `fuzzy`: Typo-tolerant lexical matching via RapidFuzz `token_set_ratio`, handling cataloging variances and dirty OCR.
    - `noop`: Pass-through mode relying purely on dense semantic retrieval and hard metadata filters.
  - Domain candidate evaluator (`CandidateEvaluator`) enforcing volume/part compatibility, author surname overlap, publication year penalties, high-confidence dense short-circuiting, and linear score fusion.
  - Cleaned configuration schema (`reconcile.rerank`, `rerank_weight`, `rerank_score` in models and TSV reports).
- **Configurable Vector Search Engines**:
  - Abstract `VectorEngine` interface supporting interchangeable dense vector search backends (`faiss` and `torch`).
  - Automatic hardware and backend resolution (`engine: "auto"` dynamically chooses between FAISS and PyTorch based on hardware availability).
  - PyTorch vector engine with Apple Silicon Metal Performance Shaders (MPS) hardware acceleration, CUDA support, and CPU fallback.
  - Native serialization for FAISS binary indices and SafeTensors/JSON metadata for PyTorch indices.
  - Idempotent authority vector indexing and deterministic embedding generation.
- **Skosmos Vocabulary Downloader & Ingestion**:
  - Automated vocabulary downloader (`reconciliator download-skosmos`) fetching concept schemes directly from Finto Skosmos REST API or pre-configured static dump endpoints.
  - Generic vocabulary ingestion CLI (`reconciliator ingest-vocab`) parsing SKOS/RDF Turtle dumps into SQLite concept and mapping stores (`finto.db`).
  - Pre-configured vocabulary source declarations in `config.yaml` for Finnish national ontologies (`yso`, `kauno`, `muso`, `slm`, `seko`, `yso-paikat`, `kanto`, etc.).
  - Bundled bridge vocabularies (e.g. `loc-languages-bridge.ttl`) for cross-vocabulary concept resolution.
- **Vocabulary Reconciliation & Legacy Thesauri Folding**:
  - Automatic legacy thesaurus folding during reconciliation (YSA $\rightarrow$ YSO, Allärs $\rightarrow$ YSO, Kaunokki $\rightarrow$ KAUNO / SLM, MUSA $\rightarrow$ MUSO / SEKO).
  - Fictional character safeguarding blocking authority transfer from fictional character headings to real human person authorities.
  - Dynamic bilingual (Finnish/Swedish) alias resolution and diacritic/punctuation folding.
  - Wikidata property P2347 (YSO ID) alias extraction and dual enrichment.
- **BIBFRAME 2.0 & 3.0 Ingestion and Reconciliation**:
  - High-performance RDF graph loader (`rdflib_loader.py`) supporting Turtle, RDF/XML, N-Triples, and N3 formats with Work, Expression, and Instance hierarchy traversal.
  - Constant-memory streaming JSON-LD parser (`jsonld_stream.py`) using `orjson` for NDJSON and `@graph` document structures.
  - Native in-place enrichment engine for RDF graphs (`enrich_bibframe_graph`) and JSON-LD documents (`enrich_bibframe_jsonld`) injecting `owl:sameAs` links without lossy cross-format conversion.
  - LC BIBFRAME 3.0 Performed Music Ontology (PMO) support (`bf:mediumOfPerformance`, `bf:instrument`, `bf:ensemble`).
- **Finnish BFFI (LKD) 1.0 Profile**:
  - Pure `bffi:` declarative profile (`bffi_1.yaml`) strictly aligned with the National Library of Finland LKD schema (`https://schema.finto.fi/bffi/1-0-0/lkd.rdf`).
  - Full support for the Finnish RDA WEMI split (`bffi:Manifestation`, `bffi:Expression`, `bffi:Work`).
  - Integration with Metatietosanasto (MTS) role concept groups (`m34`, `m153`, `m491`, `m1157`) with automatic mapping to MARC relators.
  - Support for BCP 47 and ISO 639 literal language codes on `bffi:languageOfExpression` matching `rdae:P20006`.
  - Provenance field tag extraction from `bffi:marcKey` (`owl:equivalentProperty bflc:marcKey`).
- **Declarative Schema Profile Registry**:
  - Source-tree profile configurations in `src/reconciliator/defaults/bibframe_profiles/` (`bibframe_2.yaml`, `bibframe_3.yaml`, `bffi_1.yaml`).
  - Extensible profile engine supporting custom external YAML configurations, CURIE expansion/compaction, and automatic signature-based profile detection.
- **ResourceSync (ANSI/NISO Z39.99) Harvester**:
  - Asynchronous crawler for ResourceSync capability lists, resource lists, and changelists with retry backoff and payload streaming.
- **Generic Multi-Source OAI-PMH Harvester**:
  - Multi-source OAI-PMH harvesting (`source_type: "oai" | "file" | "resourcesync"`) for Melinda, Koha, Libris, and local repositories.
  - Parallel date range partitioning with automatic chunking and resumption tokens.
  - Exponential retry backoff with HTTP `Retry-After` header parsing.
- **BIBRA Record Compatibility**:
  - Native reconciliation and in-place enrichment support for BIBRA JSON record collections without cross-format translation.
- **Observability, Progress Tracking & Interactive TUI**:
  - Interactive Rich terminal UI dashboard (`reconciliator status --watch --tui`) displaying active tasks, throughput, ETA, and resource utilization.
  - Resumable pipeline progress tracking stored in SQLite (`progress.db`) using Write-Ahead Logging (WAL).
  - Background telemetry heartbeats and database maintenance commands (`reconciliator prune`).
- **Unified Configuration Surface (`config.yaml`)**:
  - Unified configuration schema for storage engines, embedding models, vector index backends, OAI sources, Skosmos endpoints, and reconciliation policies.
  - Removed cross-format conversion options to preserve strict input/output format integrity.
- Initial implementation of Reconciliator multi-tier matching funnel:
  - **Tier 1**: Exact fast-path identifier matching (ISBN, ISSN, URN, system control numbers).
  - **Tier 2**: Dense vector semantic retrieval using Sentence Transformers.
  - **Tier 3**: Sparse BM25 lexical reranker with metadata discriminators (volume/part matching, surname match, publication year proximity penalty).
  - **Tier 4**: Multi-candidate agent alignment and URI aggregation.
- Ingestion pipelines:
  - **Wikidata Ingestion**: Constant-memory streaming of gzipped JSON dumps with bibliographic key pre-filtering.
- Deterministic text serialization using Jinja2 templates with SHA-256 fingerprint validation.
- Agent authority URI enrichment and prioritized fallback filtering (`$0` for KANTO, ISNI, VIAF; `$1` for Wikidata).
- Decoupled reporting suite generating detailed and summary TSV reports for agent and subject reconciliation.
- Comprehensive CLI built with Typer (`download-skosmos`, `ingest-vocab`, `ingest-oai`, `ingest-wikidata`, `reconcile`, `status`, `prune`, `reset`, `run`).
- Full test suite, GitHub Actions CI workflow, and Zensical documentation pipeline.
- Added MIT License and `.gitignore`.
