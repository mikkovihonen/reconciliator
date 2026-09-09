# Reconciliator

**High-Performance Finnish Bibliographic Authority Reconciliation & Agent URI Enrichment Engine**

`reconciliator` is an automated, offline reconciliation and enrichment system designed specifically for Finnish library bibliographic records. It reconciles target MARCXML records against authoritative Finnish catalog sources (Melinda) and international knowledge bases (Wikidata), enriching bibliographic agents with canonical authority identifiers (KANTO, ISNI, VIAF, Wikidata) and modernizing legacy subject headings.

> </br> 
> This repository currently contains only the solution architecture.
> </br>
> See the Architecture Specification for implementation details. </br>
> </br>

---

## Overview & Core Capabilities

### Multi-Source Authority Grounding
Ingests authoritative records from the Finnish national union catalogue (**Melinda**) and **Wikidata** to form a comprehensive, local knowledge base.

### Offline Multi-Tier Matching Funnel
Combines fast-path exact identifier matching, dense vector semantic retrieval with configurable short-circuiting, and sparse lexical reranking with strict metadata discriminators:

- **Tier 1 (Exact Identifier Lookup)**: Fast-path matching on normalized ISBN, ISSN, URN, and system control numbers.
- **Tier 2 (Dense Vector Semantic Retrieval)**: High-recall candidate search using sentence embedding models to match records on semantic content even when standard identifiers are missing or titles vary.
- **Tier 3 (Sparse Lexical Reranker & Metadata Discriminators)**: Evaluates candidates by fusing dense and BM25 lexical similarity while enforcing hard constraints (volume/part designations like `osa 1` vs `osa 2`, author surname token match, and publication year proximity).
- **Tier 4 (Agent Alignment & URI Aggregation)**: Fuzzy name alignment across all accepted candidates to aggregate authority URIs (KANTO, ISNI, VIAF, Wikidata).

#### Embedding Models & Semantic Retrieval
- **Sentence Transformer Encoders**: Dense representations are generated using configurable sentence transformer models (defaulting to Finnish SBERT `TurkuNLP/sbert-cased-finnish-paraphrase`, supporting any Hugging Face model or local weights). The engine automatically detects and utilizes hardware acceleration (NVIDIA CUDA, Apple Silicon Metal `mps`, or optimized CPU execution).
- **Deterministic Text Serialization**: Bibliographic records are serialized into rich descriptive strings via configurable Jinja2 templates (incorporating titles, authors, publishers, publication year, series, classifications, and modern canonical subjects). SHA-256 template fingerprinting guarantees strict mathematical symmetry between offline authority indexing and target record reconciliation.
- **Dense FAISS Vector Indexing**: Normalized $L_2$ embeddings are indexed in FAISS for high-throughput inner-product (cosine similarity) nearest-neighbor queries, retrieving top-$k$ semantic candidates in milliseconds.
- **High-Confidence Short-Circuit**: Candidates exceeding a configurable dense cosine similarity threshold ($S_{\text{dense}} \ge \text{threshold}$, configured via `short_circuit.threshold`, default 0.98) can bypass lexical BM25 reranking directly into acceptance (with hard volume compatibility strictly enforced).
- **Hybrid Score Fusion**: In Tier 3, dense cosine similarity ($S_{\text{dense}}$) is fused with BM25 lexical similarity ($S_{\text{sparse}}$) via configurable weights ($w_{\text{dense}} \cdot S_{\text{dense}} + w_{\text{sparse}} \cdot S_{\text{sparse}}$) to produce the composite match score.

### Multi-Candidate Agent Alignment & URI Aggregation
Authority identifiers are frequently dispersed across separate catalog entries—for example, a Melinda record may hold a **KANTO** (Asteri) URN in `$0`, an electronic edition may contain an **ISNI** in `$0`, and a Wikidata entry may provide a **Wikidata** QID in `$1`.

- **Multi-Candidate Aggregation**: The engine aligns target agents against agents across **all** accepted candidate records exceeding the match threshold, extracting and merging distinct authority identifiers rather than restricting transfer to a single top hit.
- **Supported Agent Vocabularies**:
  - **`$0`**: **KANTO** (Finnish National Agent Authority Asteri URNs `https://urn.fi/URN:NBN:fi:au:pn:*`), **ISNI** (`http://isni.org/*`), and **VIAF** (`http://viaf.org/*`).
  - **`$1`**: **Wikidata** entity QIDs (`http://www.wikidata.org/entity/Q*`).
- **Policy Filtering & Prioritization**: Aggregated URIs are filtered against allowed vocabularies and prioritized according to `agent_priority` (default `["KANTO", "ISNI", "VIAF", "WIKIDATA"]`), ensuring national authority links (**KANTO**) take precedence while providing rich international fallback identifiers.

---

## Documentation

- [Architecture Specification](docs/initial_architecture.md): Detailed technical architecture, data model schemas, matching algorithms, configuration reference, and test verification plans.

