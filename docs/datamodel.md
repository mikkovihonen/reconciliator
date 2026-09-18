# Reconciliator Data Model Architecture

This document provides a comprehensive specification of the multi-layer data model in Reconciliator. It describes each layer's role, entities, and use cases, accompanied by Mermaid relationship diagrams.

---

## 1. Overview & Multi-Layer Architecture

Reconciliator organizes bibliographic and authority metadata into **five decoupled functional layers**:

1. **Ingestion & Raw Staging Layer**: Stores immutable, provenance-tracked, source-specific bibliographic and authority records. Ingestion is **100% idempotent** without heuristic merges or count aggregations.
2. **Controlled Vocabularies & Subject Ontologies Layer**: Standardizes concepts, subject headings, classification codes, and contributor relators across Finnish and international vocabularies (YSO, YKL, UDC, MeSH, RDA).
3. **Vector & Trigram Retrieval Index Layer**: Provides dual-index retrieval capabilities: dense semantic vector representations for multilingual similarity and fast sparse/trigram PostgreSQL indexes for lexical blocking.
4. **Cluster & Reconciliation Layer (Corpus Subsystem)**: Reconciles cross-source records into first-class **WEMI Intellectual Clusters** (Works, Expressions, Manifestations) and **Agent Identity Clusters** with empirical attribute frequency distributions and canonical representative hoisting.
5. **Pipeline State, Telemetry & Metadata Layer**: Manages resumable OAI-PMH harvesting checkpoints, worker thread telemetry, progress monitoring, and database version metadata.

### High-Level Architectural Graph

```mermaid
flowchart TD
    classDef leftAlign text-align:left;

    Sources["<b>External Metadata Sources</b><br/>• Asteri Authority (MARCXML)<br/>• Melinda Union Catalog (MARCXML)<br/>• Wikidata Graph (JSON / SPARQL)<br/>• Bibframe / RDF Sources"]:::leftAlign

    Layer1["<b>Layer 1: Ingestion & Raw Staging (Authority DB)</b><br/>• agents, agent_attributes (count-free)<br/>• works, expressions, manifestations, items<br/>• manifestation_expressions (M:N)<br/>• identifiers, entity_keys, contributions<br/>• agent_relationships, work_relationships, work_consolidations"]:::leftAlign

    Layer2["<b>Layer 2: Controlled Vocabularies & Ontologies</b><br/>• concepts, concept_labels (YSO, YKL, UDC, MeSH)<br/>• concept_mappings, concept_mapping_labels<br/>• relator_terms (RDA role codes)<br/>• entity_subjects (topic, genre, occupation, field_of_work)"]:::leftAlign

    Layer3["<b>Layer 3: Retrieval & Indexing</b><br/>• USearch ANN Vector Indices (work_vectors, manifestation_vectors)<br/>• PostgreSQL pg_trgm GIN Trigram Indexes (title, name)<br/>• PostgreSQL Full-Text Search tsvector Indexes"]:::leftAlign

    Layer4["<b>Layer 4: Cluster & Reconciliation (Corpus DB)</b><br/>• WEMI Clusters: corpus_work_clusters, corpus_expression_clusters,<br/>  corpus_manifestation_clusters, corpus_linkages, sammelband_bindings<br/>• Agent Clusters: corpus_agent_clusters, corpus_agent_cluster_members,<br/>  corpus_agent_cluster_attributes (empirical frequencies)"]:::leftAlign

    Layer5["<b>Layer 5: Pipeline State & Telemetry</b><br/>• harvest_state (OAI-PMH resumption checkpoints)<br/>• task_progress (worker rates & telemetry)<br/>• db_metadata (schema versions & configuration)"]:::leftAlign

    %% Flow connections
    Sources --> Layer1
    Layer1 --> Layer2
    Layer1 --> Layer3
    Layer1 & Layer2 --> Layer4
    Layer4 <-->|Candidate Retrieval & ANN Search| Layer3
    Layer5 -.-> Layer1
    Layer5 -.-> Layer4
```

### Core Design Principles
- **Zero Hardcoding**: All weights, tolerances, blocking keys, authority precedence, and vocabularies are configured dynamically via [`defaults.yaml`].
- **Decoupled Relational Entities**: No JSON blob packing for queryable entities; relationships, subjects, and attributes are modeled as first-class relational tables.
- **Idempotency**: Ingesting a batch twice produces identical database state without duplicate rows or unbounded counter increments.
- **Strict Separation of Raw and Cluster Domains**: Raw staging preserves exact source evidence; consensus values, canonical entities, and frequencies are computed exclusively in the cluster layer. Work consolidation decisions are isolated in `work_consolidations` rather than mutating immutable raw work rows.

---

## 2. Layer 1: Ingestion & Raw Staging Layer

### Purpose & Use Cases
The Raw Staging Layer stores the ingested bibliographic and authority records as close to their source representation as possible while normalizing them into the IFLA LRM / FRBR conceptual model:
- **Authority Grounding**: Stores person and corporate bodies (`agents`) without artificial counts or premature merges.
- **Multi-Expression Support**: Supports complex manifestations (e.g. musical anthologies, multi-work vinyl records, translated omnibus editions) via the `manifestation_expressions` junction table.
- **Structural Linkages & Consolidation**: Captures sequential and parent-child work hierarchies (`work_relationships`), physical item copies (`items`), agent affiliations or pseudonyms (`agent_relationships`), and records audited canonical work equivalence decisions and rationale in `work_consolidations` without altering raw work entities.
- **Deterministic Keying**: Extracts polymorphic identifiers (ISBN, ISSN, DOI, URN, ISNI, VIAF, ORCID) into `identifiers` and blocking signatures into `entity_keys`.

### Entity-Relationship Diagram

```mermaid
erDiagram
    agents ||--o{ agent_attributes : "has attributes (1:N)"
    agents ||--o{ agent_relationships : "source agent (1:N)"
    agents ||--o{ agent_relationships : "target agent (1:N)"
    agents ||--o{ contributions : "participates in (1:N)"
    agents ||--o{ works : "primary agent of (0:1)"

    works ||--|{ expressions : "realized through (1:N)"
    works ||--o{ manifestations : "primary work of (0:1)"
    works ||--o{ contributions : "credited on (1:N)"
    works ||--o{ work_relationships : "source work (1:N)"
    works ||--o{ work_relationships : "target work (1:N)"
    works ||--o{ work_consolidations : "consolidates (0:1)"
    works ||--o{ work_medium_of_performance : "scored for (1:N)"

    work_consolidations {
        int work_id PK, FK
        int canonical_work_id FK
        text evidence
        datetime created_at
    }

    expressions ||--|{ manifestation_expressions : "embodied in (1:N)"
    manifestations ||--|{ manifestation_expressions : "embodies (1:N)"
    manifestations ||--o{ items : "has exemplar copies (1:N)"
    manifestations ||--o{ manifestations : "contains component (1:N)"

    identifiers }o--|| agents : "identifies (polymorphic)"
    identifiers }o--|| works : "identifies (polymorphic)"
    identifiers }o--|| expressions : "identifies (polymorphic)"
    identifiers }o--|| manifestations : "identifies (polymorphic)"

    entity_keys }o--|| agents : "indexes (polymorphic)"
    entity_keys }o--|| works : "indexes (polymorphic)"
    entity_keys }o--|| manifestations : "indexes (polymorphic)"

    agents {
        int id PK
        string source
        string source_id
        string record_origin "authority | bibliographic"
        string name
        string name_normalized
        string surname
        string dates
        int birth_year
        int death_year
        bool is_personal
        bool is_fictional
    }

    agent_attributes {
        int id PK
        int agent_id FK
        string attribute_type "qualifier | variant_name"
        string value
    }

    agent_relationships {
        int id PK
        int source_agent_id FK
        int target_agent_id FK
        string target_name
        string target_uri
        string relationship_type "affiliation | pseudonym"
    }

    works {
        int id PK
        string source
        string source_id
        string record_origin
        string title
        string title_normalized
        string original_language
        int primary_agent_id FK
        string work_type "text | notated_music | audio | etc."
    }

    expressions {
        int id PK
        int work_id FK
        string source
        string source_id
        string language
        string content_type "text | spoken_word | etc."
        string version_designation
    }

    manifestations {
        int id PK
        int work_id FK
        int container_id FK
        bool is_component
        string source
        string source_id
        string title
        string title_normalized
        int publication_year
        string publisher
        string carrier_type
    }

    manifestation_expressions {
        int manifestation_id PK, FK
        int expression_id PK, FK
        int track_number
        int disc_number
        bool is_primary
    }

    items {
        int id PK
        int manifestation_id FK
        string source
        string source_id
        string holding_organization
        string shelf_mark
        string barcode
    }

    identifiers {
        int id PK
        string entity_type "work | expression | manifestation | item | agent"
        int entity_id
        string source "isbn | issn | doi | isni | viaf | orcid | etc."
        string source_id
        string source_id_normalized
    }

    entity_keys {
        int id PK
        string entity_type
        int entity_id
        string key_type "title_author | author_title | lcc | udc"
        string key_value
    }

    contributions {
        int id PK
        string entity_type "work | expression | manifestation"
        int entity_id
        int agent_id FK
        string role "aut | edt | trl | cmp | prf | etc."
        bool is_primary
    }
```

---

## 3. Layer 2: Controlled Vocabularies & Subject Ontologies Layer

### Purpose & Use Cases
The Vocabulary Layer standardizes semantic metadata across multiple vocabularies and languages:
- **Concept Harmonization**: Ingests concepts from YSO (General Finnish Ontology), YKL, UDC, and MeSH.
- **Multilingual Subject Resolution**: Resolves Finnish, Swedish, and English preferred and alternative labels (`concept_labels`) into unified concept IDs.
- **Vocabulary Mapping**: Maps legacy or specialized vocabularies into target authority ontologies (`concept_mappings`).
- **Entity Subject Grounding**: Links Works, Expressions, Manifestations, and Agents to controlled concepts (`entity_subjects`) with specific relationship types (`topic`, `genre`, `occupation`, `field_of_work`).
- **Relator Standardization**: Maps raw textual role strings (e.g. `"kirjoittaja"`, `"författare"`, `"author"`) to standardized MARC/RDA relator codes (`relator_terms`).

### Entity-Relationship Diagram

```mermaid
erDiagram
    concepts ||--|{ concept_labels : "labeled by (1:N)"
    concepts ||--o{ concept_mappings : "legacy concept (1:N)"
    concept_mappings ||--o{ concept_mapping_labels : "has mapping labels (1:N)"
    concepts ||--o{ entity_subjects : "referenced as concept (1:N)"

    entity_subjects }o--|| works : "describes topic/genre"
    entity_subjects }o--|| agents : "describes occupation/field"

    concepts {
        int id PK
        string vocabulary "yso | ykl | udc | mesh | etc."
        string concept_id "p4811"
        bool is_deprecated
        string target_field
    }

    concept_labels {
        int id PK
        int concept_row_id FK
        string lang "fi | sv | en"
        string label_type "prefLabel | altLabel"
        string label
    }

    concept_mappings {
        int id PK
        string legacy_vocab
        string legacy_id
        string target_vocab
        string target_id
        string mapping_type "exactMatch | closeMatch | broadMatch"
    }

    concept_mapping_labels {
        int id PK
        int mapping_id FK
        string lang
        string label
    }

    relator_terms {
        string folded_term PK
        string raw_term
        string lang
        string code "aut | trl | edt | ill"
        string uri
    }

    entity_subjects {
        int id PK
        string entity_type "work | expression | manifestation | agent"
        int entity_id
        string relation_type "topic | genre | occupation | field_of_work"
        string vocabulary
        string concept_id
        string raw_label
        bool is_resolved
        bool is_primary
    }
```

---

## 4. Layer 3: Vector & Trigram Retrieval Index Layer

### Purpose & Use Cases
The Retrieval Index Layer acts as the **computational retrieval engine for Layer 4 (Cluster & Reconciliation)**. Pairwise comparison across millions of works and agents is $O(N^2)$, making brute-force reconciliation computationally impossible. Layer 3 reduces this search space into a high-precision candidate set:
- **Fast Lexical Blocking**: Uses PostgreSQL `pg_trgm` (trigram) GIN indexes on `title_normalized` and `name_normalized` to generate candidate match pairs in milliseconds.
- **Full-Text Lexical Search (FTS)**: Evaluates `to_tsvector('simple', serialized_text)` for unstructured bibliographic text matching.
- **Dense Semantic Retrieval**: Employs USearch vector index engines (`work_vectors`, `manifestation_vectors`, `agent_index`) managed by an IPC daemon to retrieve semantic nearest neighbors when lexical variations, differing subtitles, or language translations prevent exact token matching.

### Vector Index Architecture & Relational Mapping
Reconciliator uses a hybrid vector architecture that couples relational PostgreSQL mapping tables with high-performance USearch HNSW (Hierarchical Navigable Small World) binary indexes on disk:

```mermaid
erDiagram
    works ||--o| work_vectors : "relational vector map (1:1)"
    manifestations ||--o| manifestation_vectors : "relational vector map (1:1)"
    work_vectors ||--|| USearch_Work_Index : "vector_id key bridge"
    manifestation_vectors ||--|| USearch_Manifestation_Index : "vector_id key bridge"

    works {
        int id PK
        string title
        string title_normalized
        string work_type
    }

    work_vectors {
        int vector_id PK "Key in USearch index"
        int work_id FK "Reference to works.id"
    }

    manifestations {
        int id PK
        string title
        string title_normalized
        string publisher
        int publication_year
    }

    manifestation_vectors {
        int vector_id PK "Key in USearch index"
        int manifestation_id FK "Reference to manifestations.id"
    }

    USearch_Work_Index {
        uint64 key "vector_id"
        float32_array embedding "768d / 384d vector"
        string binary_file "work_index.usearch"
        string meta_file "work_index.usearch.meta.json"
    }

    USearch_Manifestation_Index {
        uint64 key "vector_id"
        float32_array embedding "768d / 384d vector"
        string binary_file "manifestation_index.usearch"
        string meta_file "manifestation_index.usearch.meta.json"
    }
```

### Inverted Trigram & Full-Text Search (FTS) Indexes
In addition to dense vector embeddings, Layer 3 provisions PostgreSQL GIN trigram and lexical text search indexes directly on Layer 1 entity tables:

| Index Name | Table & Target Column | Index Type / Opclass | Use Case |
| :--- | :--- | :--- | :--- |
| `idx_works_title_trgm` | `works.title_normalized` | `GIN (gin_trgm_ops)` | Fast fuzzy title matching, handling typos and punctuation differences |
| `idx_agents_name_trgm` | `agents.name_normalized` | `GIN (gin_trgm_ops)` | Fast fuzzy agent name candidate blocking |
| `idx_manifestations_title_trgm` | `manifestations.title_normalized` | `GIN (gin_trgm_ops)` | Fast fuzzy manifestation title blocking |
| `idx_works_fts` | `works.serialized_text` | `GIN (to_tsvector('simple', ...))` | Lexical search on serialized MARC fields (titles, notes, classifications) |
| `idx_expressions_fts` | `expressions.serialized_text` | `GIN (to_tsvector('simple', ...))` | Full-text search on language, notes, and music instrumentation |
| `idx_manifestations_fts` | `manifestations.serialized_text` | `GIN (to_tsvector('simple', ...))` | Full-text search across complete manifestation metadata |

### How Layer 4 Uses Layer 3
The engines in Layer 4 (`WorkMatcher`, `Clusterer`, and `AgentClusterer`) actively query Layer 3 in a progressive multi-tier funnel:
1. **Tier 1 (Exact Identifier Lookup)**: Queries `identifiers` for ISBN, ISSN, DOI, URN, ISNI, or VIAF matches.
2. **Tier 2 (Blocking Key Matching)**: Queries `entity_keys` for standardized bibliographic signatures (`title_author`, `author_title`, `surname_decade`).
3. **Tier 3 (Trigram Title & Author Fuzzy Search)**: Queries `pg_trgm` indexes with threshold cutoffs to capture typographic errors and punctuation shifts.
4. **Tier 4 (Dense Vector Semantic Search)**: Queries the USearch ANN vector daemon using dual-encoder embeddings to discover translations, cross-format adaptations (audiobooks, radio plays), and radically altered title formulations.

### Index & Retrieval Flow Diagram

```mermaid
flowchart LR
    L4Engines["Layer 4 Engines<br/>(WorkMatcher / Clusterer / AgentClusterer)"] --> Pipeline{Multi-Tier Retrieval Pipeline}

    subgraph SparseTier ["Sparse & Lexical Retrieval (PostgreSQL)"]
        Pipeline -->|1. Standard Identifiers| IdentLookup["identifiers Index Lookup<br/>(ISBN, ISSN, DOI, URN)"]
        Pipeline -->|2. Blocking Keys| KeyLookup["entity_keys Index Lookup<br/>(title_author, surname_decade)"]
        Pipeline -->|3. Trigram Fuzzy Search| TrigramLookup["pg_trgm GIN Indexes<br/>(idx_works_title_trgm,<br/>idx_agents_name_trgm)"]
    end

    subgraph DenseTier ["Dense Semantic Retrieval (USearch Daemon)"]
        Pipeline -->|4. Dense Embedding Query| DenseLookup["ANN Vector Search<br/>(work_vectors, manifestation_vectors)"]
    end

    IdentLookup --> Candidates[Consolidated Candidate Pool]
    KeyLookup --> Candidates
    TrigramLookup --> Candidates
    DenseLookup --> Candidates

    Candidates --> L4Scoring["Layer 4 Feature Scoring & Disambiguation Guards<br/>(Cluster Partitioning)"]
```

---

## 5. Layer 4: Cluster & Reconciliation Layer (Corpus Subsystem)

### Purpose & Use Cases
The Cluster Layer stores the consolidated real-world entities produced by reconciliation:
- **WEMI Intellectual Clusters**:
  - `corpus_work_clusters`: Conceptual Work identity (e.g. *Sinuhe egyptiläinen*).
  - `corpus_expression_clusters`: Language, content type, and version designation of a work (e.g. Swedish translation by Ole Torvalds).
  - `corpus_manifestation_clusters`: Physical and digital publications, collapsing publisher and print run duplicates.
  - `corpus_linkages`: Record-level routing map from any source record ID to its exact Work, Expression, and Manifestation clusters.
  - `sammelband_bindings`: Models "bound-with" composite codices where multiple distinct bibliographic works are bound into a single physical binding.
- **Agent Identity Clusters**:
  - `corpus_agent_clusters`: Unified person or corporate entity head, electing a canonical record by configured source precedence (Asteri $\to$ Wikidata $\to$ Melinda).
  - `corpus_agent_cluster_members`: Complete provenance linking every member agent with its specific `link_method` (`identifier_isni`, `weighted_scoring`, etc.) and confidence score.
  - `corpus_agent_cluster_attributes`: Aggregated frequency counts for all variant names, qualifiers, affiliations, and occupations.

### Active Dependency on Layer 3 (Retrieval & Indexing)
Layer 4 does not perform passive storage; its clustering engines depend directly on Layer 3:
- **Work & Expression Clustering**: `Clusterer` and `WorkMatcher` query Layer 3 vector indices and trigram indexes to discover candidates across sources. Without Layer 3, cross-lingual work clustering (e.g. matching a Finnish original to its French edition with a completely translated title) would be impossible.
- **Agent Identity Clustering**: `AgentClusterer` queries Layer 3 multi-key blocking strategies (`name_normalized`, `surname_decade`, `folded_surname`, `variant_names`) to group candidate pairs before running tolerant scoring and Union-Find graph partitioning.
- **Target Record Routing**: Incoming records matched by `WorkMatcher` use Layer 3 to resolve against existing clusters, persisting the resulting coordinates into `corpus_linkages`.

### Entity-Relationship Diagram

```mermaid
erDiagram
    %% WEMI Intellectual Clusters
    corpus_work_clusters ||--|{ corpus_expression_clusters : "has expressions (1:N)"
    corpus_work_clusters ||--o{ corpus_linkages : "realized in (1:N)"
    corpus_expression_clusters ||--o{ corpus_linkages : "expressed in (1:N)"
    corpus_manifestation_clusters ||--o{ corpus_linkages : "embodies (1:N)"
    sammelband_bindings }o--|| corpus_linkages : "physically binds (N:M)"

    %% Agent Identity Clusters
    corpus_agent_clusters ||--|{ corpus_agent_cluster_members : "merges members (1:N)"
    corpus_agent_clusters ||--|{ corpus_agent_cluster_attributes : "compiles consensus (1:N)"

    %% Cross-Domain Intersections
    corpus_agent_clusters ||--o{ corpus_work_clusters : "canonical author of (1:N)"
    corpus_agent_clusters ||--o{ corpus_expression_clusters : "translator/performer in (1:N)"

    corpus_work_clusters {
        string cluster_id PK
        string work_type
        string canonical_title
        string canonical_author
        string canonical_work_source
        string canonical_work_source_id
    }

    corpus_expression_clusters {
        string cluster_id PK
        string work_cluster_id FK
        string language
        string content_type
        string version_designation
    }

    corpus_manifestation_clusters {
        string cluster_id PK
        string canonical_record_id
        string publisher
        int publication_year
        string carrier_type
        int member_record_count
    }

    corpus_linkages {
        string record_id PK
        string work_cluster_id FK
        string expression_cluster_id FK
        string manifestation_cluster_id FK
        string final_work_source
        string final_expression_source
    }

    sammelband_bindings {
        string binding_id PK
        string record_id PK, FK
        int sequence_in_binding
        string binding_notes
    }

    corpus_agent_clusters {
        string cluster_id PK
        int canonical_agent_id FK
        string canonical_name
        string canonical_uri
        int birth_year
        int death_year
        int member_count
        int work_count
        float confidence_score
    }

    corpus_agent_cluster_members {
        int agent_id PK, FK
        string cluster_id FK
        string source
        string source_id
        string link_method "identifier_isni | weighted_scoring"
        float confidence
    }

    corpus_agent_cluster_attributes {
        int id PK
        string cluster_id FK
        string attribute_type "variant_name | qualifier | affiliation | occupation"
        string value
        string concept_id
        int frequency
        bool is_canonical
    }
```

---

## 6. Layer 5: Pipeline State, Telemetry & Metadata Layer

### Purpose & Use Cases
The Pipeline State Layer provides operational reliability and monitoring:
- **Resumable OAI-PMH Harvesting**: Tracks source synchronization partitions, timestamps, and resumption tokens (`harvest_state`), enabling fault-tolerant incremental sync.
- **Asynchronous Task Telemetry**: Stores worker progress, transfer rates (records/sec, bytes/sec), percentages, and error states (`task_progress`) for live terminal UI monitoring.
- **System Metadata**: Maintains schema versions, indexing timestamps, and configuration hashes (`db_metadata`).

### Entity Diagram

```mermaid
erDiagram
    harvest_state {
        string source_id PK
        string source_type "oai_pmh | sparql | dump"
        string metadata_prefix
        string set_spec
        string last_harvest_from
        string last_harvest_until
        string resumption_token
        int records_harvested
        string status "in_progress | completed | failed"
    }

    task_progress {
        string task_id PK
        string task_type "harvest | index | cluster | match"
        string status "running | completed | failed"
        int total_records
        int processed_records
        float rate_per_sec
        float percentage
        string error_message
    }

    db_metadata {
        string key PK
        string value
        datetime updated_at
    }
```

---

## 7. End-to-End Lifecycle & Data Flow

The following sequence diagram illustrates how an incoming bibliographic record is processed through all five layers of the data model:

```mermaid
sequenceDiagram
    autonumber
    actor CLI as CLI / Worker
    participant L5 as Layer 5: Harvest State
    participant L1 as Layer 1: Ingestion & Raw Staging
    participant L2 as Layer 2: Vocabularies & Relators
    participant L3 as Layer 3: Retrieval & Indexing
    participant L4 as Layer 4: Cluster & Reconciliation
    actor Export as Enriched Output

    CLI->>L5: Checkpoint sync partition (harvest_state)
    CLI->>L1: Idempotent batch insert (works, manifestations, agents)
    L1->>L1: Extract identifiers & entity keys (ISBN, ISSN, author_title)
    
    CLI->>L2: Resolve subjects & occupations (entity_subjects, concepts)
    CLI->>L2: Normalize contributor roles (relator_terms)
    
    CLI->>L3: Generate & store embeddings (work_vectors, USearch daemon)
    
    CLI->>L4: Run AgentClusterer (ISNI override, blocking, tolerant scoring)
    L4->>L4: Populate corpus_agent_clusters & attribute frequencies
    
    CLI->>L4: Run WorkMatcher & WEMI Clusterer
    L4->>L4: Map to Work/Expression/Manifestation clusters & corpus_linkages
    
    CLI->>Export: Export enriched MARCXML (758 / 100 $0) & Bibframe RDF
```

---

## 8. Summary Table of Database Tables

| Table Name | Layer | Purpose | Primary Key |
| :--- | :---: | :--- | :--- |
| `agents` | 1 | Raw person and organization entities | `id` (autoincrement) |
| `agent_attributes` | 1 | Unweighted intrinsic qualifiers and variant names | `id` (autoincrement) |
| `agent_relationships` | 1 | Person-to-org affiliations and pseudonyms | `id` (autoincrement) |
| `works` | 1 | Distinct intellectual/artistic creations | `id` (autoincrement) |
| `expressions` | 1 | Linguistic/musical realizations of works | `id` (autoincrement) |
| `manifestations` | 1 | Physical/digital publication editions | `id` (autoincrement) |
| `manifestation_expressions` | 1 | M:N embodiment junction with track numbers | `(manifestation_id, expression_id)` |
| `items` | 1 | Physical copy holdings and shelf marks | `id` (autoincrement) |
| `identifiers` | 1 | Polymorphic external identifiers (ISBN, ISNI, etc.) | `id` (autoincrement) |
| `entity_keys` | 1 | Polymorphic blocking and lookup signatures | `id` (autoincrement) |
| `contributions` | 1 | Roles linking agents to works/expressions | `id` (autoincrement) |
| `work_relationships` | 1 | Work-to-work relationships (series, adaptations) | `id` (autoincrement) |
| `work_consolidations` | 1 | Reconciled work equivalences and evidence | `work_id` (FK to works.id) |
| `work_medium_of_performance`| 1 | Musical instrumentation and scoring details | `(work_id, medium_uri)` |
| `concepts` | 2 | Controlled vocabulary concept identifiers | `id` (autoincrement) |
| `concept_labels` | 2 | Multilingual preferred and alternative labels | `id` (autoincrement) |
| `concept_mappings` | 2 | Mappings across vocabulary systems | `id` (autoincrement) |
| `relator_terms` | 2 | Normalized contributor role term mappings | `folded_term` |
| `entity_subjects` | 2 | Subject/topic/genre/occupation linkages | `id` (autoincrement) |
| `work_vectors` | 3 | Work dense semantic embedding vectors | `vector_id` |
| `manifestation_vectors` | 3 | Manifestation dense embedding vectors | `vector_id` |
| `corpus_work_clusters` | 4 | Reconciled conceptual work clusters | `cluster_id` |
| `corpus_expression_clusters`| 4 | Reconciled expression clusters | `cluster_id` |
| `corpus_manifestation_clusters`| 4 | Reconciled manifestation clusters | `cluster_id` |
| `corpus_linkages` | 4 | Record-level routing map to WEMI clusters | `record_id` |
| `sammelband_bindings` | 4 | Composite bound-with volume constituent map | `(binding_id, record_id)` |
| `corpus_agent_clusters` | 4 | Reconciled agent identity clusters | `cluster_id` |
| `corpus_agent_cluster_members`| 4 | Member provenance and confidence scores | `agent_id` |
| `corpus_agent_cluster_attributes`| 4 | Empirical frequencies of consensus attributes | `id` (autoincrement) |
| `harvest_state` | 5 | OAI-PMH synchronization checkpoints | `source_id` |
| `task_progress` | 5 | Asynchronous worker telemetry and rates | `task_id` |
| `db_metadata` | 5 | Schema version and system configuration metadata | `key` |

