# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial implementation of Reconciliator, a high-performance Finnish bibliographic authority reconciliation and agent URI enrichment engine.
- Multi-tier matching funnel:
  - **Tier 1**: Exact fast-path identifier matching (ISBN, ISSN, URN, system control numbers).
  - **Tier 2**: Dense vector semantic retrieval using Sentence Transformers and FAISS index with hardware acceleration (CUDA, Apple Silicon Metal MPS, CPU).
  - **Tier 3**: Sparse BM25 lexical reranker with metadata discriminators (hard volume/part matching, author surname validation, publication year proximity penalty).
  - **Tier 4**: Multi-candidate agent alignment and URI aggregation across accepted records.
- Ingestion pipelines:
  - **OAI-PMH Ingestion**: Generalized multi-source OAI-PMH harvest and local MARCXML parsing (Melinda, Koha, Libris, etc.) with parallel workers and chunked date range slicing.
  - **FINTO Ingestion**: SKOS / RDF / Turtle vocabulary ingestion into SQLite (`finto.db`) supporting legacy thesauri folding (YSA, Allärs, Kaunokki, MUSA -> YSO, KAUNO, MUSO, SLM, SEKO).
  - **Wikidata Ingestion**: Constant-memory streaming of gzipped JSON dumps with bibliographic key pre-filtering.
- Deterministic text serialization using Jinja2 templates with SHA-256 fingerprint validation.
- Agent authority URI enrichment and prioritized fallback filtering (`$0` for KANTO, ISNI, VIAF; `$1` for Wikidata).
- Decoupled reporting suite generating detailed and summary TSV reports for agent and subject reconciliation.
- Resumable pipeline progress tracking stored in SQLite (`progress.db`) with WAL mode.
- Comprehensive CLI built with Typer (`download-skosmos`, `ingest-vocab`, `ingest-oai`, `ingest-wikidata`, `reconcile`, `status`, `prune`, `reset`, `run`).
- Full test suite, GitHub Actions CI workflow, and Zensical documentation pipeline.
- Added MIT License and `.gitignore`.
