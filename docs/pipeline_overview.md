# Pipeline overview

Written for an engineer new to this codebase.

Reconciliator ingests bibliographic corpora, decomposes them into a WEMI
(Work / Expression / Manifestation / Item) structure, links records to each other
and to authority data, and writes enriched records back out. This describes how
it actually works today, not how it is intended to work.

For the refactoring history see
[plans/wemi_data_structure.md](plans/wemi_data_structure.md); for the
moving-image research see [plans/kavi.md](plans/kavi.md).

---

## 1. The shape of it

```
                  ┌──────────────┐
  MARCXML ──────► │              │
  BIBFRAME ─────► │   ingest     │──► works / expressions / manifestations
  (OAI-PMH,       │  (decompose) │    + agents, identifiers, entity_keys,
   file,          └──────────────┘      contributions, relationships
   ResourceSync)                                    │
                                                    ▼
                                        ┌───────────────────────┐
                                        │  authority.db         │
                                        │  one SQLite database  │
                                        └───────────────────────┘
                                          │        │          │
                          ┌───────────────┘        │          └──────────────┐
                          ▼                        ▼                         ▼
                  ┌──────────────┐        ┌─────────────────┐       ┌────────────────┐
                  │ build-index  │        │ consolidate-    │       │   reconcile    │
                  │ (embeddings) │        │ works           │       │                │
                  └──────────────┘        │ (cross-source   │       │ cluster ─►     │
                          │               │  work identity) │       │ match ─►       │
                          └──────────────►└─────────────────┘──────►│ resolve agents │
                                                                    │ ─► enrich      │
                                                                    └────────────────┘
                                                                             │
                                                                             ▼
                                                                    enriched records
                                                                    + TSV reports
```

Everything lives in **one SQLite database** (`authority.db`, 36 tables). There
used to be a separate corpus-staging database; it was merged so a cluster can be
joined to the works it resolves to.

## 2. Commands

| command | what it does |
|---|---|
| `ingest-vocab` | Load controlled vocabularies (YSO, SLM, KANTO…) into `vocab.db` |
| `download-skosmos` | Fetch those vocabularies from Finto |
| `ingest-oai` | Harvest a source (OAI-PMH, file or ResourceSync) into WEMI rows |
| `ingest-wikidata` | Stream a Wikidata dump into the same structure |
| `build-index` | Embed stored works and manifestations into their vector indexes |
| `build-agent-index` | Embed agents into the agent vector index |
| `consolidate-works` | Elect one canonical work per cross-source equivalence group |
| `reconcile` | Cluster, match, resolve agents, enrich, report |
| `run` | ingest-vocab → ingest-oai → ingest-wikidata → **build-index** → reconcile |
| `status` | Task progress and telemetry from `progress.db` |
| `reset` / `prune` | Clear storage; drop completed tasks |

## 3. Ingest

Every source is ingested the same way. There is no separate path for "the corpus
being reconciled" — that distinction is a per-source flag, not a code path.

```yaml
ingest_oai:
  melinda:
    source_type: oai          # oai | file | resourcesync
    format: marcxml           # marcxml | bibframe
    authoritative: true       # may this source be matched *against*?
    ingest_mode: accumulate   # accumulate | replace
```

**Flow.** `ingest_oai_pipeline` runs a producer/consumer pair: harvester threads
yield records, a `_WEMIBatchAccumulator` decomposes each into entities, and a
writer thread persists batches through `insert_wemi_batch`. Harvest progress is
checkpointed to `harvest_state`, so an interrupted run resumes from its
resumptionToken and a completed run harvests incrementally next time. With
`workers > 1` and a resolvable date window the harvest is partitioned into
`slice_days` slices fetched concurrently.

**Decomposition** (`loaders/marcxml.py`, `loaders/bibframe/`, with
`loaders/bibra.py` adapting bibra JSON into MARCXML) produces, per record:

- one `WorkEntity`, one `ExpressionEntity`, one `ManifestationEntity`
- `AgentEntity` rows with `ContributionEntity` links carrying relator roles
- `IdentifierEntity` rows (ISBN, ISSN, ISMN, DOI, URN, publisher number, …)
- `EntityKey` rows — the *derived* blocking keys, see below
- `WorkRelationship` rows, mediums of performance, item rows when enabled

**Identity is deterministic.** A record with no usable control number gets an id
derived from a digest of its own fields — leader, controlfields, datafields with
indicators and subfields — so re-ingesting the same input yields the same work,
expression, manifestation and agent URIs. Agent URIs derive from the agent's
name and dates rather than its ordinal position among the `7XX` fields. The same
holds for RDF: a BIBFRAME blank node is identified by a digest of its own
predicate/object pairs, never by rdflib's parse-local label, which is regenerated
on every parse. Watch for that when working in `loaders/bibframe/`:
`str(BNode())` returns a *bare* label with no `_:` prefix — only `.n3()` adds
one — so a `startswith("_:")` test never fires. Use `is_blank_node()`.

**Subjects** are folded to a vocabulary and a concept id at ingest
(`loaders/subjects.py`), because that is the only form in which two catalogues'
headings can be compared: "lääkärit" under `$2 ysa` and the same heading under
`$2 yso/fin` are one subject once both read `yso:p2591`. A folded subject is
stored on the work as a dict — heading, `pref_label`, vocabulary, uri, `code`,
`scheme` — and the per-record `vocab:concept` histogram is stamped on every agent
the record names, as evidence that this person was named on a record about these
subjects. A heading that resolves to no supported vocabulary is kept but carries
no key: counting the raw string would make two spellings look like two subjects.

**`record_origin`** on every row is the name of the source that produced it. The
`sources` table records which of those names are authoritative. Together these
answer "which rows may be matched against", so both halves must agree — if a
source is registered authoritative but its rows carry a different origin, the
allow-list filters out every candidate and matching silently finds nothing.
`reconcile` warns about that mismatch before it starts.

## 4. The two key tables beside the entities

**`identifiers`** holds externally-meaningful standard numbers, polymorphically
(`entity_type` + `entity_id`).

**`entity_keys`** holds *derived* keys, in the same shape: the blocking keys the
clusterer groups on and the funnels dispatch on. Both are computed once at
ingest, where the record is already parsed, rather than by re-parsing a stored
payload at match time.

Key types the MARC loader emits: `work` (author + title stem), `author`,
`title_stem`, `uniform_title`, `music_work`, `series`, `standard`, `citation`,
`patent`, `fingerprint`, `geodata_spatial`, `heritage_model`,
`translators_signature`, `sammelband`, `analytic` and `analytic_title` (MARC
505/700/710 `$t` constituent works).

Neither table has a foreign key — `entity_type` can name any tier, which SQLite
cannot express as one constraint — so deleting an owning row does **not** cascade
and cleanup has to be explicit. `delete_source()` does that, which is what backs
`ingest_mode: replace`.

## 5. Classification

`determine_work_type()` decides what kind of thing a record is, in this order:

1. `502` → thesis
2. **655 genre/form headings**, matched exactly against SLM terms
   (*Suomalainen lajityyppi- ja muotosanasto*, `$2 slm/fin`) — broadcast
   programmes, juridical works, anthologies, comics, games and so on
3. `341 $a tactile` or a tactile `336 $b` code → tactile
4. `111` meeting main entry → conference
5. Leader/06 and /07, `007`, `008` → music, cartography, moving image, still
   image, dataset, software, manuscript, serial, integrating…
6. `857` electronic archive location → web harvest, or preservation surrogate
   when its second indicator says the archived copy is a version of a physical
   item; `533` reproduction note says the same
7. **RDA `336 $b` content type** — an RDA record must carry 336 and electronic
   resources often have a generic leader, so a dataset coded `cod` under
   `Leader/06 = a` still classifies
8. default → text

Rules 2, 3, 6 and 7 were derived from the Finnish RDA application guidelines and
verified against Finto and the LoC code lists. `determine_content_type()` maps
the full 23-code `rdacontent` vocabulary.

BibFrame classifies from BIBFRAME Work subclasses and `bf:content`.

## 6. Matching: funnels

A `MatchRecord` is the record-level view the matcher consumes:
manifestation-centric, resolving the Expression and Work above it, carrying the
key bag and the identifier bag.

Domain behaviour is **declared, not coded**. `funnels/builtin.py` holds 42
`FunnelSpec` entries in precedence order; the first whose `when` predicate claims
a record wins, and one is marked `default`.

```yaml
- id: funnel_l_cartography
  when: { work_type: [cartography] }
  identifier_schemes: [sheet_number, isbn]
  work_types: [cartography]
  content_type: cartographic_image
  align_expression: true
  confidence: 0.90
```

`when` is in disjunctive normal form — a list of alternatives, each a
conjunction — over seven predicate kinds: `work_type`, `content_type`,
`is_component`, `has_key`, `has_identifier`, and regexes in `title_matches`,
`key_matches` and `identifier_matches`.

Every funnel runs the same three tiers (`funnels/engine.py`):

| tier | how |
|---|---|
| 1 | exact standard identifier, trying the spec's `identifier_schemes` first |
| 2 | author key + title stem, filtered by the spec's `work_types` in order |
| 3 | dense vector search over the work index, then SQLite FTS5 |

Tiers 2 and 3 **rank** their candidates rather than taking the first. A blocking
query returns everything sharing an author key and title stem, which for a
prolific author under a common title is several genuinely different works, and
the first row is whichever the index happened to return.

`CandidateEvaluator` scores each candidate — fusing the dense score with a
pluggable reranker (`bm25`, `cross_encoder`, `fuzzy`, `noop`) by the configured
weights — and applies three vetoes: conflicting volume designation, no shared
author surname, publication years further apart than `max_year_difference`.
Volume designations are compared *normalised*, so "osa 2", "2. osa" and "Vol. II"
are one designation.

The two roles are kept apart. The vetoes reject at both tiers. The fused-score
threshold applies only at tier 3, where the score is a real similarity: a tier-2
candidate already agrees on author key *and* title stem exactly, so holding it to
a fuzzy bar would reject correct matches. Because volume and year live on the
manifestation and a work has several, each of a work's editions is evaluated and
the best verdict stands — the record matches the work if it matches any edition.

`rerank.enabled: false` bypasses the vetoes and the fusion, leaving the bare
dense threshold.

A spec may name a `handler` — a built-in name or a path to a Python file under
`funnels/` — whose module defines any of `tier1`/`tier2`/`tier3`. Omitted tiers
fall back to the generic template. Three exist: recorded sound (tracklist
overlap), notated music, audiobooks (cross-format resolution to the textual
work).

`reconcile.funnels` in config overrides or extends: an entry whose `id` matches a
built-in replaces it *in place*, keeping its precedence; a new id goes where its
`before:` says.

**Retrieval depth** comes from `rerank.top_k`. Where a filter will be applied the
vector searches over-fetch and stop once enough survivors are found, because a
filter applied after retrieval can only return fewer candidates than were
fetched.

## 7. Agent resolution

Matching a record and resolving its agents are two questions, so they are two
calls:

```python
record = _to_target_record(payload, fmt, profile)   # one construction
match  = matcher.match_target(record)               # one dispatch
result = matcher.resolve_agents(match, bib_record)  # hydrate, align, fallback
```

`resolve_agents` hydrates the matched WEMI entities so their agents' authority
URIs are available, aligns the record's agents against them (`AgentAligner`),
falls back to the standalone agent index for any still unlinked, then applies the
vocabulary filter and `link_policy` priority selection that decide which URIs get
injected.

## 8. Clustering

`CorpusClusterer` finds records that describe the same work, expression or
manifestation *within* the corpus. It reads `get_all_match_records()` scoped to
the non-authoritative sources — authority data lives in the same database and
must not be clustered as if it were the corpus.

- **blocking** over the key families, then pairwise similarity within blocks,
  unioned with `UnionFind`
- **expression** sub-clustering by content type, language and translator
- **manifestation** grouping by `standard::year::volume`, then merged further by
  manifestation-embedding similarity above `cluster.thresholds.manifestation` —
  records of one edition that share no standard number never meet in a key group
- **authority hoisting**: each cluster member is matched, and a cluster adopts the
  authority work URI of any member that resolved, so every member exports the
  same identity

Results go to `corpus_work_clusters`, `corpus_expression_clusters`,
`corpus_manifestation_clusters` and `corpus_linkages`.

## 9. Consolidation

Each source mints its own work URI, so one work held by several catalogues
arrives as several rows. `consolidate-works` elects one canonical row per
equivalence group on three tiers of evidence — shared Wikidata QID (a *differing*
QID vetoes a merge), shared strong identifier, then metadata agreement — and
writes symmetric `same_work_as` edges recording *why* plus a
`works.canonical_work_id` recording *the decision*. Nothing is deleted.

The matcher resolves matches through that election, so the corpus clusterer's
hoisting and the reconcile loop cannot disagree about a work's identity.

## 10. Enrichment and output

`MARCXMLEnricher` and the BibFrame enricher write resolved URIs back into the
records: agent `$0`/`$1`, subject URIs folded to their modern vocabulary,
work/expression links. Subject heading rewriting is configurable — whether to
replace a legacy heading with the ontology's preferred label or retain it, and
whether to append the `$2 yso/fin`-style language subcode.

`ReportGenerator` emits TSV report sets for agents, subjects, works and
expressions, each row naming the match tier, the funnel used and the confidence.

## 11. Vector indexes

Three indexes, each with exactly one consumer:

| index | consumer |
|---|---|
| `work_index` | the matcher's tier-3 semantic fallback |
| `manifestation_index` | the clusterer's near-duplicate merge |
| `agent_index` | the standalone agent fallback |

There is no expression index — that tier added no discrimination the other two do
not already provide, and nothing searched it.

The embedder supplies the dimension, so a command without one gets no indexes
rather than a guessed dimension that would not match the stored vectors. Both the
matcher and the clusterer guard on an empty index, so wiring them before
`build-index` has run is safe.

## 11a. The loader contract

Every format reaches the datastore through `RecordLoader`
(`loaders/base.py`), and the registry (`loaders/registry.py`) picks the loader
for a harvested item. Three things matter about it.

**One shape, one options object.** A loader returns a `DecomposedRecord` — always
lists, including the WEMI tiers — and receives a `LoadContext` carrying
everything any loader may need. A loader that cannot honour part of the context
*declares* that rather than omitting the parameter.

**Normalisation is the base class's job, not each decomposer's.**
`RecordLoader.finalise` stamps `record_origin` on every tier and strips trailing
ISBD punctuation from every stored name and title. A new format gets both without
writing any code, and an existing one cannot forget them. Format-specific
normalisation that needs the source record — MARC's subject folding, its relator
mapping — happens in the decomposer and is *shared code*
(`loaders/subjects.py`, `normalizers/relators.py`) so that two formats cannot
reach different answers about the same heading or the same role.

**Every absence is declared, with a reason.** `LoaderCapabilities` states what a
loader produces — work types, content types, key types, identifier schemes — and
one flag per normalisation concern; `not_produced` maps each missing output to
why it is missing. A silently omitted output is indistinguishable from an
impossible one, which is how the BibFrame path came to apply none of the MARC
path's normalisation without anything saying so. `check_output` compares a
produced record against its loader's declaration, and
`tests/test_funnel_reachability.py` reads the declaration rather than parsing
loader source — an inference that had twice returned the wrong answer.

What remains declared-and-absent is structural: a MARC authority record has no
manifestation to key and carries no relator codes; BIBFRAME has no host-item
concept (MARC 773), models constituent works as related Works rather than 505/700
`$t` subfields, and has no bound-with, which is holdings-level.

See [plans/loader_interface.md](plans/loader_interface.md) for the measured
divergences this closed, and for what each one turned out to be hiding.

## 12. Testing conventions

619 tests. Three patterns carry most of the weight:

- **Dispatch on loader output.** Tests that build a probe record from a funnel's
  own predicate prove only that the spec claims its own probe. The tests that
  catch real defects run actual MARC leaders and fields through the loader and
  assert *which funnel claimed the record*.
- **Sets that fail in both directions.** `KNOWN_UNREACHABLE_FROM_MARC` and
  `KEYS_READ_BUT_NEVER_WRITTEN` in `tests/test_funnel_reachability.py` assert
  equality, not membership. Growing means a regression; shrinking means a gap was
  closed and the list is stale.
- **Capability extraction over the real tables.** The reachability audit reads
  the loader's lookup tables from the imported module rather than parsing for
  string literals — two earlier static-analysis shortcuts silently under-reported
  what the loader emits.

---

## 13. Development ideas

Ten public methods had no caller anywhere. Each was examined for a place in the
architecture as it now stands; the verdicts differ, and are recorded here because
the methods themselves have been removed.

### Genuinely useful, and worth building deliberately

**`search_manifestations_fts` — a no-embedder fallback for clustering.**
The matcher degrades from vector search to FTS5 when no index is built. The
clusterer does not: its manifestation merge is a no-op without an embedder, so
records of one edition that share no standard number stay apart until
`build-index` has run. A lexical fallback would mirror the matcher's own shape
and make clustering useful before any embedding exists. This is the strongest of
the ten.

**Single-cluster fetches — a step toward not loading everything.**
`get_work_cluster`, `get_expression_cluster` and `get_manifestation_cluster` fetch
one cluster by id. The reconcile loop instead loads all three cluster tables into
in-memory dicts up front, because it visits every record. That is fine for
fixtures and a scalability ceiling for a real corpus: cluster state grows with
the corpus and is held entirely in memory. Per-record fetches behind a bounded
cache would remove that ceiling, and would also give an inspection or API path
something to call that is not "load the whole table".

**URI lookups — tier-1 matching on URI-valued identifiers.**
`get_expression_by_uri` and `get_manifestation_by_uri` had no caller, and their
work- and agent-level siblings are exercised only by tests. Meanwhile two funnel
specs (`funnel_hh_parallel_multilingual`, `funnel_mm_critical_edition`) list
`work_uri` among their `identifier_schemes`, which resolves through
`lookup_by_identifier` against the `identifiers` table — where a URI-valued
identifier is not what is stored. Records carrying an external work URI (MARC
`758 $0`, BIBFRAME `instanceOf`) currently dedupe only through the
`ON CONFLICT(work_uri)` upsert at write time. Resolving such a URI to an existing
entity at *match* time is a real capability that nothing presently has.

**`refresh_work_equivalences` — needed the moment `run` chains consolidation.**
The matcher caches the work-equivalence map for its lifetime. Nothing invalidates
it because nothing runs consolidation in the same process as matching. `run`
executes its steps as direct calls within one process, and does not currently
include `consolidate-works` — the moment it does, and it is sequenced before
`reconcile`, the matcher will hold a map built before consolidation ran. Whoever
adds that step needs to invalidate the cache, or build the matcher afterwards.

### Redundant: a second way to do something the code already does

**`resolve_canonical_work`** duplicates `WorkMatcher._canonicalize_match`, which
resolves through a cached equivalence map rather than a per-call query. Two
implementations of one rule is how the two came to disagree in Phase 12 — the
earlier draft recomputed the canonical election instead of reading the stored
one, and diverged whenever a source priority was configured. If a non-matcher
caller ever needs canonical resolution, it should share the matcher's path rather
than reintroduce a parallel one.

**`get_indexed_manifestation_source_ids`** is byte-identical to
`get_indexed_source_ids` — same SQL, same return type, different name. Ingest's
`skip_existing` uses the latter.

**`get_agent_by_id`** wraps `get_agents_by_ids([id])` for a single id. Callers use
the batch form, which is the right default for a pipeline that processes records
in batches.

### A second sweep, wider than the first

The first sweep covered four files. A full-tree scan found more, but most of what
it flagged was framework-invoked and invisible to a caller search: Typer commands
registered by decorator, and Pydantic field and model validators. Those are not
dead. What was:

**`vocab/bridges.py`** — shadowed entirely by `vocab/bridges/__init__.py`, since
Python prefers a package over a same-named module. It re-exported *from the
package that shadows it*, so importing it would have been a circular self-import.
It also created phantom references that made `get_bundled_bridge_dir` look
uncalled when the package uses it internally. Deleted.

**Four report-writer aliases** — `write_work_detailed_report`,
`write_work_summary_report`, `write_expression_detailed_report`,
`write_expression_summary_report`, each a one-line forward to a method the CLI
calls directly, and *two of them forwarding to the same one*. Naming symmetry
again, and the same risk: two names for one behaviour is two places it can
diverge.

**`MatchRecord.add_key` / `add_identifier`** — mutators for the key and
identifier bags. Both bags are populated at construction by the loaders, so
neither had a caller.

**`get_all_relators`, `is_supported_subject_vocab`, `load_custom_terms`** — each
a plausible API on a live class, none reached.

**`expanded_agent_personal_classes`** — and this one is worth more than a
deletion. Its sibling `expanded_agent_corporate_classes` has two callers: the
BibFrame loader asks whether an agent is *corporate* and never whether it is
*personal*, treating "not corporate" as personal by omission. So the profile
field `agent_personal_classes` is **declared, expanded and never read** — the
same defect as the eleven keys in `KEYS_READ_BUT_NEVER_WRITTEN`. The cached
property is gone; the config field is deliberately left, because the loader
arguably ought to consult it rather than infer from absence.

### Resolved since this chapter was written

The `rerank` package — `CandidateEvaluator`, the four scorers and twelve of
thirteen `RerankConfig` fields — was also connected to nothing: the evaluator was
constructed into `WorkMatcher.evaluator` and never read. It is now wired into
tiers 2 and 3, which is how the volume-normalisation defect in
`check_volume_match` came to light: raw string equality rejected every candidate
whose volume was merely spelled differently.

### A note on how these accumulated

None of the ten were wrong when written; they are the shape of an API built
outward from a data model rather than inward from its callers. The pattern worth
noticing is that the *redundant* three each duplicate something that already
existed — and in one case the duplicate had already caused a real divergence.
Symmetry in a repository API is cheap to add and quietly expensive: every
uncalled method is a second place a rule can be implemented differently.
