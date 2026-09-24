# Aggregate & Constituent Work Architecture

This document specifies the architectural model, ingestion pipeline, bottom-up multiplex clustering, and multi-format enrichment for **aggregates** (songbooks, musical anthologies, multi-track audio recordings, and composite publications) in Reconciliator.

---

## 1. Conceptual Model: Pure Graph Containment

In library metadata, aggregates consist of publications that embody multiple distinct constituent works (e.g. songs in a songbook, tracks on an album, stories in an anthology).

### Principles of the Architecture
1. **Zero Boolean Flags**: There is no `is_aggregate` boolean column. Whether a Work is an aggregate is queried dynamically from graph topology:
   ```sql
   SELECT 1 FROM work_relationships 
   WHERE source_work_id = :work_id AND relationship_type = 'has_part'
   LIMIT 1;
   ```
2. **Pure Graph Whole-Part Containment**: Outgoing `has_part` edges on Works in `work_relationships` and `corpus_work_relationships` define containment, with a 1-based `sequence_number` preserving track or ordinal position.
3. **Multi-Format Parity**: Standardized handling across MARCXML, LC BIBFRAME 2.0/3.0, and Finnish BFFI 1.0 (LKD), strictly avoiding namespace bleed (e.g. **no `bflc:`** in BFFI).
4. **Bottom-Up Multiplex Clustering**: Constituent works are clustered *first*, and the resulting constituent cluster identities inform and dramatically enhance container clustering accuracy.

---

## 2. Multi-Format Constituent Extraction Profiles

Constituent extraction is entirely profile-driven, mapping native syntax into unified `Constituent` structures:

```
                      ┌─────────────────────────────────────────┐
                      │ Source Record                           │
                      │ • MARCXML (730, 700, 740, 505)          │
                      │ • BFFI RDF (bffi:hasPart)               │
                      │ • LC BIBFRAME (bf:hasPart)              │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │ Profile-Driven Extractor                │
                      │ • marc_enhanced.yaml                    │
                      │ • bffi_haspart.yaml (No bflc:)          │
                      │ • bibframe_haspart.yaml                 │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │ Normalized Constituent Models           │
                      │ (title, creator, ordinal, identity_ref) │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
                      ┌─────────────────────────────────────────┐
                      │ Embodied WEMI Entities & Edges          │
                      │ • Constituent WorkEntity                │
                      │ • Constituent ExpressionEntity          │
                      │ • WorkRelationship (has_part, seq_num)  │
                      └─────────────────────────────────────────┘
```

### 2.1 MARCXML Profile ([`marc_enhanced.yaml`](../src/reconciliator/defaults/constituent_profiles/marc_enhanced.yaml))
Extracts analytical entries from:
- **`730` (Uniform Title Added Entry)**: Common in Finnish songbooks (e.g. `$aOi muistatko vielä sen virren$gMerikanto, Oskar`).
- **`700` (Name-Title Added Entry)**: Analytical composer/author entries (`$aMerikanto, Oskar,$d1868-1924.$tLaula, tyttö!`).
- **`740` (Uncontrolled Analytical Title)**: Secondary title added entries with indicator `ind2: "2"`.
- **`505` (Enhanced Contents Note)**: Tracklists with `$t` (title) and `$r` (statement of responsibility).

### 2.2 Finnish BFFI Profile ([`bffi_haspart.yaml`](../src/reconciliator/defaults/constituent_profiles/bffi_haspart.yaml))
Strictly adheres to the National Library of Finland LKD schema (`https://schema.finto.fi/bffi/1-0-0/lkd.rdf`):
- Uses `bffi:hasPart` and `bf:hasPart`.
- Extracts titles from `bffi:title` and `bffi:mainTitle`.
- Extracts sequence numbers from `bffi:sequenceNumber` or `bffi:sequence` literals.
- Extracts provenance components and author hints from `bffi:marcKey`.
- **Strict Constraint**: Zero references to or generation of `bflc:` triples.

### 2.3 LC BIBFRAME Profile ([`bibframe_haspart.yaml`](../src/reconciliator/defaults/constituent_profiles/bibframe_haspart.yaml))
Standard Library of Congress profile:
- Reads `bf:hasPart`.
- Reads `bf:title` / `bf:mainTitle` and `bf:contribution`.
- Reads `bflc:sequenceNumber` and `bflc:sequence`.

---

## 3. Ingestion & Staging

In [`src/reconciliator/cli.py`](../src/reconciliator/cli.py) (`_ingest_corpus_as_wemi`):
1. **Profile Resolution**: Automatically resolves the profile from configuration:
   - MARCXML $\rightarrow$ `config.constituents.profile` (default: `marc_enhanced`)
   - BFFI 1.0 $\rightarrow$ `config.constituents.bffi_profile` (default: `bffi_haspart`)
   - LC BIBFRAME $\rightarrow$ `config.constituents.bibframe_profile` (default: `bibframe_haspart`)
2. **Entity Generation**: Calls `build_embodied_constituents` to generate:
   - Constituent `WorkEntity` rows with content-derived or URI identities.
   - Constituent `ExpressionEntity` rows.
   - `ManifestationExpressionAssociation` connecting the container manifestation to constituent expressions.
   - Outgoing `WorkRelationship` edges with `relationship_type = 'has_part'` and `sequence_number = ordinal`.
3. **Database Staging**: Batches all entities into PostgreSQL via `insert_wemi_batch`.

---

## 4. Bottom-Up Multiplex Clustering

Clustering operates bottom-up in [`src/reconciliator/clustering/corpus.py`](../src/reconciliator/clustering/corpus.py):

### Step A: Constituent Micro-Clustering First
Before clustering container publications, all constituent works in the corpus are clustered:
1. Load constituent works from database (identified via incoming `has_part` edges or `c:` content hashes).
2. Generate deterministic blocking keys: `compute_author_key(creator) + "::" + compute_title_stem(title)`.
3. Partition constituent works into clusters with deterministic IDs: `va_const_work_{sha256}`.
4. Persist constituent clusters to `corpus_work_clusters`.

### Step B: Container Fingerprinting
Each container `MatchRecord` is decorated with its ordered list of constituent cluster IDs:
```python
container_record.constituent_cluster_ids = [
    c_link.constituent_work_cluster_id for c_link in constituents_for_record
]
```

### Step C: Cross-Format Macro-Clustering (Pairwise Scoring)
When comparing two container publications $A$ and $B$, pairwise scoring evaluates their constituent sets $C(A)$ and $C(B)$:
$$J(A, B) = \frac{|C(A) \cap C(B)|}{|C(A) \cup C(B)|}$$

#### Scoring Rules (`cluster.aggregates`):
1. **Container-Part Anti-Collision Guard**:
   If one record is a container ($|C| \ge 2$) and the other is an individual constituent component (`is_component = True`), matching is hard-vetoed:
   $$\text{Score} = 0.0, \quad \text{Evidence} = \text{"container\_part\_collision"}$$
2. **Content Mismatch Hard-Veto**:
   If both records have $\ge 2$ constituents but $J(A, B) \le 0.30$ (configurable via `jaccard_veto_threshold`), matching is hard-vetoed even if container titles are identical:
   $$\text{Score} = 0.0, \quad \text{Evidence} = \text{"aggregate\_content\_mismatch"}$$
3. **High-Confidence Merge Bonus**:
   If $J(A, B) \ge 0.80$ (configurable via `jaccard_merge_threshold`):
   - **Sequence Alignment** (`sequence_alignment: true`): Computes Longest Common Subsequence (LCS) ratio:
     $$\text{LCS Ratio} = \frac{2 \cdot \text{len}(LCS)}{|C(A)| + |C(B)|}$$
     If $\text{LCS Ratio} \ge 0.70$, returns `(0.95, "aggregate_jaccard_lcs_merge")`.
   - If sequence alignment is disabled, returns `(0.95, "aggregate_jaccard_merge")`.

### Step D: Graph Linkage Persistence
After macro-clustering completes:
1. Container record linkages are written to `corpus_linkages`.
2. Constituent record linkages are written to `corpus_constituent_linkages`.
3. Inter-cluster whole-part edges are written to `corpus_work_relationships`.

---

## 5. Multi-Format Output Enrichment

Enrichment translates resolved constituent authority URIs back into native source formats:

### 5.1 MARCXML Enrichment ([`MARCXMLEnricher`](../src/reconciliator/xml_enricher.py))
- Reads `corpus_constituent_linkages` by `record_id`.
- Locates matching `730`, `700`, or `740` analytical fields by title matching.
- Injects `$0 <final_work_uri>` directly into the matched field.

### 5.2 BFFI RDF Enrichment ([`enrich_bibframe_graph`](../src/reconciliator/loaders/bibframe/enricher.py))
- Resolves dialect title predicates (`p.expanded_title_predicates` and `p.expanded_main_title_predicates`).
- Matches constituent nodes under `bffi:hasPart` / `bf:hasPart`.
- Injects `(part_node, owl:sameAs, final_work_uri)`.
- If the node has `bffi:marcKey`, appends `$0<final_work_uri>` to the literal string.
- Strictly avoids introducing any `bflc:` triples into the graph.

---

## 6. Configuration Reference

In `config.yaml` or [`defaults.yaml`](../src/reconciliator/defaults/defaults.yaml):

```yaml
constituents:
  profile: "marc_enhanced"         # Default profile for MARCXML
  bibframe_profile: "bibframe_haspart" # Default profile for LC BIBFRAME
  bffi_profile: "bffi_haspart"     # Default profile for Finnish BFFI (LKD)
  mode: "entities"                 # off | keys_only | entities
  index_works: false               # Keep constituent works out of primary work ANN index

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
      blocking_family: "constituent_work" # Blocking key family for container indexing
```

