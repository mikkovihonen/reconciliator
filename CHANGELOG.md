# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **IFLA LRM / WEMI Relational Data Store**: 4-tier entity model decomposing records into Works, Expressions, Manifestations, and Items within a PostgreSQL 15+ relational schema, featuring polymorphic indexing, trigram similarity, and pure-graph whole-part containment (`has_part`).
- **Multi-Protocol Harvesting & Multi-Format Ingestion**: Ingestion engine supporting OAI-PMH, ResourceSync, MARCXML, LC BIBFRAME 2.0/3.0, Finnish BFFI 1.0 (LKD), BIBRA JSON, and Wikidata dumps without forced cross-format conversions.
- **Domain-Aware Entity Resolution**: Precedence-ordered matching pipeline supporting 42 specialized bibliographic material categories (monographs, sound recordings, music, serials, cartography, theses, etc.) with domain-specific metadata discriminators and blocking keys.
- **Multiplex Entity Clustering & Consolidation**: Cross-source canonical work consolidation and intra-corpus entity clustering, including bottom-up micro-clustering for constituent works and macro-clustering for containers.
- **Pluggable Retrieval & Cascading Verification**: Dense vector search (FAISS, PyTorch with Apple Silicon MPS/CUDA acceleration) paired with pluggable lexical rerankers (BM25, fuzzy, cross-encoders) and an adaptive KV-branching LLM judge for ambiguous candidate sets.
- **Controlled Vocabulary Reconciliation**: Finto/Skosmos ontology harvester and offline cache supporting Finnish national vocabularies (YSO, KAUNO, SLM, SEKO, KANTO), legacy thesaurus and ontology folding (including MUSA and MUSO into YSO), bilingual alias resolution, configurable vocabulary code mapping (unifying YSO topical, place, and time headings under `yso/fin` and canonical `http://www.yso.fi/onto/yso/` URIs), and configurable label language resolution supporting record-language driven label selection and per-vocabulary overrides in native enrichment.
- **LLM Judge Cross-Encoder Parity & Calibration**: Comprehensive enhancement bringing the autoregressive KV-branching LLM Judge to parity with bidirectional cross-encoders:
  - Multi-token logit marginalization (`torch.logsumexp` / `mx.logsumexp`) across synonym positive (`YES`, `Yes`, `True`) and negative (`NO`, `No`, `False`) token clusters to eliminate tokenizer boundary sensitivity.
  - Context-free prior calibration evaluating neutral candidate prompts (`Candidate: N/A...`) during single-pass target encoding to eliminate baseline model skew.
  - Prefix-LM attention masking enabling bidirectional query prefix attention without causal degradation while keeping KV-cache views 100% static and zero-copy truncatable.
  - Symmetric dual-direction scoring ($A \rightarrow B$ and $B \rightarrow A$) for ambiguous contenders within `symmetric_margin_threshold` to guarantee order invariance.
  - Model architecture suitability evaluation guide in [`docs/two_stage_llm_judge_architecture.md`](docs/two_stage_llm_judge_architecture.md), establishing linear contiguous KV-cache requirements and evaluating Qwen 2.5 (7B/14B/32B), Mistral NeMo 12B, Llama 3.1/3.3, and sliding-window incompatibilities (Gemma 2/3).
  - Configurable configuration surface (`defaults.yaml`, `config.example.yaml`, `LLMJudgeConfig`) with zero aliases or backwards-compatibility shims.
- **In-Place Multi-Format Catalog Enrichment**: In-place enrichment injecting canonical authority identifiers ($0/$1 in MARCXML, `owl:sameAs` and `bffi:marcKey` in BIBFRAME/BFFI) directly into native catalog formats without lossy serialization.
- **Operational Service & Command-Line Interfaces**: High-concurrency FastAPI HTTP service for real-time reconciliation alongside a comprehensive Typer CLI with an interactive Rich terminal dashboard.
