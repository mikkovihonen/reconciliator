# WEMI Data Structure Consolidation

> Retire the four legacy authority tables (`authority_records`, `authority_identifiers`,
> `authority_agents`, `wikidata_authors`) and the `AuthorityStore` class in favour of
> the normalised WEMI relational model already implemented in `WEMIRepository`.

## Scope

| Legacy artefact | WEMI replacement |
|---|---|
| `authority_records` table | `works` + `expressions` + `manifestations` |
| `authority_identifiers` table | `identifiers` (polymorphic) |
| `authority_agents` table | `agents` + `contributions` |
| `wikidata_authors` table | `agents` + `identifiers` |
| `authority_fts` FTS5 table | `works_fts` + `expressions_fts` + `manifestations_fts` |
| `vector_mappings` table | `work_vectors` + `expression_vectors` + `manifestation_vectors` |
| `AuthorityStore` class | `WEMIRepository` |
| `ReconciliationEngine` class | `WorkMatcher` class |

---

## Phase 1 — WEMIRepository Enhancements

### 1.1 Add `get_relational_disambiguation_evidence()` to WEMIRepository

**File**: `src/reconciliator/repository.py` (WEMIRepository class, ~L2515)

**Rationale**: This method currently exists only on `AuthorityStore` (L579–776).
`AuthorityStore` already contains a WEMI-native fallback path (L704–774) that queries
`contributions` → `agents` / `manifestations` / `works`. That fallback becomes the
sole implementation.

**Changes**:

```python
def get_relational_disambiguation_evidence(
    self,
    candidate_agent_ids: list[int],
    target_coauthors: list[str],
    target_publisher: Optional[str] = None,
    target_classifications: Optional[list[tuple[str, str]]] = None,
) -> dict[int, dict[str, Any]]:
```

Logic for each candidate `agent_id`:

1. `SELECT entity_type, entity_id FROM contributions WHERE agent_id = ?`
2. **Co-author overlap**: Find other agents contributing to the same works:
   ```sql
   SELECT DISTINCT a.name, a.name_normalized
   FROM contributions c JOIN agents a ON c.agent_id = a.id
   WHERE c.entity_type = 'work' AND c.entity_id IN (…) AND c.agent_id != ?
   ```
3. **Publisher overlap**: Look up publishers on linked manifestations:
   ```sql
   SELECT DISTINCT publisher FROM manifestations
   WHERE id IN (…) AND publisher IS NOT NULL AND publisher != ''
   ```
   Where manifestation IDs come from contributions with `entity_type = 'manifestation'`,
   **or** from `manifestations.work_id IN (…)` for work-level contributions.
4. **Classification overlap**: Check `works.ykl_class` and `works.subjects_json` for
   scheme-agnostic classification matching.

The fuzzy matching logic (`fold_label`, `fuzz.token_set_ratio`) is ported unchanged
from the existing WEMI fallback in `AuthorityStore` (L728–772).

---

### 1.2 Simplify `insert_wikidata_authors()` — drop `wikidata_authors` table write

**File**: `src/reconciliator/repository.py` (WEMIRepository, L2250–L2373)

**Current behaviour**: Dual-writes to both `wikidata_authors` and `agents` + `identifiers`.

**New behaviour**: Write only to `agents` + `identifiers`. Wikidata-specific fields
(`occupations`, `fields_of_work`, `affiliations`) are stored in `agents.metadata_json`.

**Detailed changes**:

- Remove the `conn.executemany(INSERT INTO wikidata_authors …)` block (L2296–L2314).
- The `agents` insert (L2316–L2331) remains unchanged.
- The `identifiers` insert (L2334–L2373) remains unchanged.
- Store `occupations`, `fields_of_work`, `affiliations` inside the `metadata_json`
  blob written to `agents`. Merge into the existing metadata dict before serialisation:
  ```python
  meta = json.loads(a.get("metadata_json") or "{}") if a.get("metadata_json") else {}
  if a.get("occupations"):
      meta["occupations"] = a["occupations"]
  if a.get("fields_of_work"):
      meta["fields_of_work"] = a["fields_of_work"]
  if a.get("affiliations"):
      meta["affiliations"] = a["affiliations"]
  if a.get("family_name"):
      meta["family_name"] = a["family_name"]
  ```

---

### 1.3 Rewrite `get_wikidata_authors_batch()` / `get_wikidata_author()` against `agents` + `identifiers`

**File**: `src/reconciliator/repository.py` (WEMIRepository, L2375–L2413)

**Current behaviour**: Queries `wikidata_authors WHERE qid IN (…)`.

**New behaviour**:

```python
def get_wikidata_authors_batch(self, qids: list[str]) -> dict[str, dict[str, Any]]:
    # 1. SELECT * FROM agents WHERE wikidata_qid IN (…)
    # 2. For each agent, SELECT scheme, value FROM identifiers
    #    WHERE entity_type='agent' AND entity_id=?
    # 3. Reconstruct the same dict shape:
    #    {
    #      "qid": agent.wikidata_qid,
    #      "name": agent.name,
    #      "family_name": meta.get("family_name") or agent.surname,
    #      "identifiers": {row["scheme"]: row["value"] for row in ident_rows},
    #      "birth_year": agent.birth_year,
    #      "death_year": agent.death_year,
    #      "occupations": meta.get("occupations"),
    #      "fields_of_work": meta.get("fields_of_work"),
    #      "affiliations": meta.get("affiliations"),
    #      "metadata_json": agent.metadata_json,
    #    }
```

This preserves the return type contract so that all existing callers
(Wikidata ingestion pass-2 name hydration, etc.) continue to work without change.

---

### 1.4 Remove `authority_agents` fallback from `get_agents_by_ids()` and `build_agent_index()`

**File**: `src/reconciliator/repository.py`

**`get_agents_by_ids()`** (L2415–L2450):
- Remove the `missing_ids` block (L2436–L2449) that falls back to
  `SELECT … FROM authority_agents WHERE id IN (…)`.
- Only query the `agents` table.

**`build_agent_index()`** (L2457–L2515):
- Remove the `else` branch (L2482–L2494) that falls back to
  `SELECT … FROM authority_agents GROUP BY name, surname, dates`.
- Only query the `agents` table.

---

### 1.5 Port `get_indexed_source_ids()` to WEMIRepository

**File**: `src/reconciliator/repository.py` (WEMIRepository)

**Current state**: `get_indexed_manifestation_source_ids()` exists (L1906–L1912) but
doesn't verify vector index membership.

**New method** (add alongside existing):

```python
def get_indexed_source_ids(self, source: str) -> set[str]:
    """Return set of manifestation source_ids stored and indexed for a given source."""
    with self.get_connection() as conn:
        cursor = conn.execute(
            "SELECT source_id FROM manifestations WHERE source = ?;", (source,)
        )
        return {row["source_id"] for row in cursor if row["source_id"]}
```

Or, if vector-indexing verification is required, join to `manifestation_vectors`.

---

## Phase 2 — Consumer Migration

### 2.1 `src/reconciliator/matcher.py` — Replace `ReconciliationEngine` with `WorkMatcher`

**Current state**: `ReconciliationEngine.__init__()` takes an `AuthorityStore` (L53) and calls:

| Line | AuthorityStore method called | WEMIRepository equivalent |
|---|---|---|
| L89 | `find_exact_matches(identifiers)` | `WorkMatcher._try_fast_path_identifiers()` → `lookup_by_identifier()` |
| L110–113 | `indexer.total_vectors`, `search_vector_candidates()` | `search_work_vectors()` |
| L162 | `get_agents_by_ids(agent_id_list)` | `get_agents_by_ids()` (already on WEMI) |
| L188 | `authority_store` passed to `AgentFallbackResolver` | Pass `WEMIRepository` instance |

**Changes**:
- Remove the `ReconciliationEngine` class entirely (L48–L270).
- All callers (currently only `cli.py` `reconcile_cmd`) switch to `WorkMatcher`.
  `WorkMatcher` (L601+) already takes a `WEMIRepository` and implements the same
  multi-tier funnel via WEMI tables.
- Remove the `from reconciliator.repository import AuthorityStore` import (L27).

### 2.2 `src/reconciliator/agent_transfer.py` — Change `authority_store` parameter type

**Current state**: `check_homonym_ambiguity()` and `AgentFallbackResolver.evaluate_candidates()`
accept `authority_store: Optional[Any]` (L336, L487) and call
`authority_store.get_relational_disambiguation_evidence()` via `hasattr` duck-typing (L432).

**Changes**:
- No signature change needed — the parameter is already `Optional[Any]` and access
  is duck-typed via `hasattr(authority_store, "get_relational_disambiguation_evidence")`.
- Once `WEMIRepository` gains this method (Phase 1.1), passing a `WEMIRepository`
  instance will work transparently.
- Update the docstring (L345) to reference `contributions`/`agents` instead of
  `authority_records`.

### 2.3 `src/reconciliator/cli.py` — Remove `AuthorityStore` instantiation

Four CLI commands currently create `AuthorityStore` instances:

| Line | Command | Change |
|---|---|---|
| L725 | `ingest-oai` | Switch to `WEMIRepository` (the `target_database="authority"` branch at L775 already uses WEMI; make it the default) |
| L851 | `ingest-wikidata` (non-extract_works branch) | Switch to `WEMIRepository` |
| L946 | `reconcile` | Switch to `WorkMatcher(wemi_repo=…)` instead of `ReconciliationEngine(authority_store=…)` |
| L1538 | `build-agent-index` | Switch to `WEMIRepository` (it already has `build_agent_index()`) |

**Detailed changes**:

#### `ingest-oai` command (L685–L808)
- Remove `store = AuthorityStore(…)` (L725–L730).
- The default fallback path (L797–L804) currently calls `ingest_oai_pipeline(store=store, normalizer=normalizer, embedder=embedder, …)` with an `AuthorityStore`. Replace with:
  ```python
  wemi_repo = WEMIRepository(cfg.storage.authority_db, fts_config=cfg.storage.fts)
  total = ingest_oai_pipeline(
      source_name=src_name, config=src_cfg, store=wemi_repo, tracker=tracker, ...
  )
  ```
- Remove `normalizer` and `embedder` creation from the OAI code path since
  `WEMIRepository`/`CorpusRepository` ingestion doesn't require them for
  per-record embedding (embedding happens at index build time).

#### `ingest-wikidata` command (L811–L861)
- The `else` branch (L838–L860) uses `AuthorityStore` for the legacy flat-record path.
  Replace with `WEMIRepository`. The pipeline function `ingest_wikidata_pipeline`
  already supports `WEMIRepository` via `ingest_wikidata_wemi_pipeline()`.

#### `reconcile` command (L915–L1524)
- Replace `store = AuthorityStore(…)` (L946–L951) with `wemi_repo = WEMIRepository(…)`.
- Replace `ReconciliationEngine(authority_store=store, …)` (L972–L973) with
  `WorkMatcher(wemi_repo=wemi_repo, …)`.
- Update the reconcile loop to use `WorkMatcher.match_target_record()` instead of
  `ReconciliationEngine.reconcile_record()`.
- The existing `wemi-reconcile` command (L1079+) already demonstrates the full WEMI
  reconciliation flow and can serve as reference.

#### `build-agent-index` command (L1527–L1559)
- Replace `store = AuthorityStore(…)` (L1538–L1542) with
  `wemi_repo = WEMIRepository(cfg.storage.authority_db, fts_config=cfg.storage.fts)`.
- Call `wemi_repo.build_agent_index(…)` instead of `store.build_agent_index(…)`.

### 2.4 `src/reconciliator/ingestion/oai.py` — Remove `AuthorityStore` path

**Current state**: `run_oai_ingestion()` (L581) accepts
`Union[AuthorityStore, WEMIRepository, CorpusRepository]` and branches on
`isinstance(store, AuthorityStore)` at L604.

**Changes**:
- Remove the `AuthorityStore` type from the union.
- Remove the branch that raises `ValueError` for missing normalizer/embedder when
  `AuthorityStore` is used (L603–L604).
- Remove the `AuthorityStore` import (L39).
- The `WEMIRepository` and `CorpusRepository` code paths remain unchanged.

### 2.5 `src/reconciliator/ingestion/wikidata.py` — Remove `AuthorityStore` paths

**Current state**: Multiple functions accept `Union[AuthorityStore, WEMIRepository]`:

| Function | Line | Change |
|---|---|---|
| `_stream_entities_pass1()` | L657 | Change to `Optional[WEMIRepository]` |
| `_stream_entities()` | L718 | Change to `Optional[WEMIRepository]` (only calls `insert_wikidata_authors`) |
| `ingest_wikidata_pipeline()` | L790 | Change to `Optional[WEMIRepository]` |
| `run_wikidata_ingestion()` | L1291 | Remove `AuthorityStore` branch |
| `_resolve_entity_name()` | L321 | Change to `Optional[WEMIRepository]` (only calls `get_wikidata_author`) |
| `_process_and_ingest()` | L336 | Change to `Optional[WEMIRepository]` |

**Detailed changes**:
- Remove all `isinstance(store, AuthorityStore)` checks and the corresponding
  `AuthorityStore` code paths that call `insert_records_batch()`, `normalizer.normalize()`,
  and `embedder.embed_records()`.
- Keep the `WEMIRepository` path that calls `insert_wikidata_authors()` (which uses
  `agents` + `identifiers` after Phase 1.2) and `insert_wemi_batch()`.
- Remove the `AuthorityStore` import (L48).

---

## Phase 3 — Schema Cleanup

### 3.1 `src/reconciliator/db.py` — Remove legacy table DDL from `init_authority_db()`

**Remove** (L34–L140):
- `CREATE TABLE authority_records` (L35–L52) and its indexes (L53–L54)
- `CREATE TABLE authority_identifiers` (L57–L63) and its indexes (L64–L69)
- `CREATE TABLE authority_agents` (L72–L88) and its indexes (L90–L96)
- `CREATE TABLE wikidata_authors` (L99–L111)
- `CREATE TABLE vector_mappings` (L114–L118) and its index (L119–L121)
- `CREATE VIRTUAL TABLE authority_fts` (L131–L140)

**Keep** (L142–L512):
- All WEMI tables: `agents`, `works`, `expressions`, `manifestations`, `items`,
  `identifiers`, `contributions`, `work_relationships`, `work_medium_of_performance`,
  `manifestation_expressions`
- Tripartite vector tables: `work_vectors`, `expression_vectors`, `manifestation_vectors`
- Tripartite FTS5 tables and triggers: `works_fts`, `expressions_fts`, `manifestations_fts`
- `db_metadata`, `harvest_state`

### 3.2 `src/reconciliator/repository.py` — Remove `AuthorityStore` class

**Remove entirely** (L83–L857):
- `class AuthorityStore` and all its methods:
  - `insert_records_batch()`
  - `get_indexed_source_ids()`
  - `get_record_by_id()`
  - `find_exact_matches()`
  - `search_vector_candidates()`
  - `save_index()`
  - `insert_wikidata_authors()`
  - `get_wikidata_authors_batch()` / `get_wikidata_author()`
  - `get_agents_by_ids()` / `get_agent_by_id()`
  - `get_relational_disambiguation_evidence()`
  - `set_embedding_metadata()` / `get_embedding_metadata()`
  - `build_agent_index()`

**Remove helper functions** used only by `AuthorityStore`:
- Nothing unique — `_hydrate_agent_profile()` (L56) is shared with `WEMIRepository`
  and must be kept.

**Remove imports** no longer needed:
- `BibliographicRecord` from models (if no other consumer needs it — check
  `ReconciliationEngine` removal first).

### 3.3 `src/reconciliator/config.py` — Remove `StorageConfig.index_path` if unused

**Current state**: `StorageConfig` likely has `index_path` for the flat FAISS index used
by `AuthorityStore`. After removal, only tripartite index paths (`work_index`,
`expression_index`, `manifestation_index`, `agent_index`) are needed.

**Change**: Remove `index_path` from `StorageConfig` if no other consumer references it.

---

## Phase 4 — Test Updates

### 4.1 Tests that directly use `AuthorityStore` — rewrite to `WEMIRepository`

| Test file | What it tests | Changes needed |
|---|---|---|
| **`test_db_concurrency.py`** | WAL concurrent writes to `authority_records` (L21–L46) | Rewrite to insert into `works` or `manifestations` table instead. Also uses `AuthorityStore` for metadata test (L53) — switch to `WEMIRepository`. |
| **`test_oai_multi_source.py`** | Multi-source OAI ingestion verifies `authority_records` (L155) | Rewrite assertions to query `manifestations` or `works` table. Switch from `AuthorityStore` to `WEMIRepository`. |
| **`test_bibframe_ingest_and_reconcile.py`** | E2E ingestion + reconciliation, checks `authority_records` count (L110), uses `store.get_record_by_id(1)` (L112) | Switch to `WEMIRepository`, verify via `get_work()` / `get_manifestation()`. Replace `ReconciliationEngine` assertions with `WorkMatcher` equivalents. |
| **`test_agent_guards.py`** | Relational disambiguation evidence via `store.insert_records_batch()` and `authority_agents` query (L468–L498) | Rewrite to: (a) insert WEMI entities via `repo.insert_wemi_batch()`, (b) query `agents` table, (c) call `repo.get_relational_disambiguation_evidence()`. |
| **`test_agent_fallback.py`** | Agent fallback resolution using `AuthorityStore` | Switch store fixture to `WEMIRepository`. Ensure test data creates `agents` + `contributions` instead of `authority_agents`. |
| **`test_oai_pmh.py`** | OAI-PMH harvesting pipeline with `AuthorityStore` | Switch to `WEMIRepository` or `CorpusRepository` depending on test intent. |
| **`test_vector_mapping.py`** | Vector mapping verification | Switch from `vector_mappings` to `work_vectors` / `manifestation_vectors`. |
| **`test_wemi_discrepancy_fixes.py`** | Uses both `AuthorityStore` and `WEMIRepository` | Remove `AuthorityStore` usage, keep `WEMIRepository` only. |
| **`test_wikidata_stream.py`** | Wikidata streaming ingestion | Switch `AuthorityStore` references to `WEMIRepository`. |

### 4.2 Tests that use `wikidata_authors` table — update assertions

| Test file | Changes |
|---|---|
| **`test_wemi_repository.py`** (L474–L540) | `test_wemi_repository_wikidata_author_canonical_uri` and `test_wemi_repository_dynamic_author_identifiers_option3` call `insert_wikidata_authors()` and verify via `get_wikidata_author()`. After Phase 1.2–1.3, these tests continue to work with the new `agents`-backed implementation. Update assertion comments. Verify that the return shape matches. |
| **`test_wemi_wikidata.py`** (L19–L66) | `test_wikidata_author_prescan` verifies `wikidata_authors` table (L44) via `get_wikidata_author()`. After Phase 1.3 this returns data from `agents` instead — assertions should still pass as the dict shape is preserved. Remove any direct SQL assertions against `wikidata_authors`. |

### 4.3 Tests that are already WEMI-only — no changes needed

These tests use only `WEMIRepository` / `WorkMatcher` and require no changes:

- `test_wemi_authority_origin.py`
- `test_wemi_bibframe.py`
- `test_wemi_cli_routing.py`
- `test_wemi_config.py`
- `test_wemi_db.py`
- `test_wemi_e2e_fixtures.py`
- `test_wemi_enricher_and_reports.py`
- `test_wemi_marcxml.py`
- `test_wemi_matcher.py`
- `test_wemi_models.py`
- `test_wemi_oai_checkpoint.py`
- `test_fts_config.py`

### 4.4 New tests to write

| Test | Purpose |
|---|---|
| `test_wemi_relational_disambiguation.py` | Verify `WEMIRepository.get_relational_disambiguation_evidence()` with WEMI-native data (agents, contributions, works, manifestations). Cover co-author overlap, publisher overlap, and classification overlap scenarios. |
| `test_wemi_wikidata_agents_only.py` | Verify that `insert_wikidata_authors()` populates `agents.metadata_json` with `occupations`, `fields_of_work`, `affiliations`, `family_name` and that `get_wikidata_authors_batch()` reconstructs the full dict from `agents` + `identifiers`. |
| `test_wemi_concurrency.py` | Port `test_db_concurrency.py` to use WEMI tables (`works` or `manifestations`) for concurrent write/read verification. |
| `test_reconcile_via_work_matcher.py` | E2E test replacing the `ReconciliationEngine` flow with `WorkMatcher`, verifying that the same target records produce equivalent match results. |

---

## Phase 5 — Documentation Updates

### 5.1 `docs/runbook.md`

- Update references to `authority_records` / `authority_agents` in the agent
  disambiguation documentation (L195–L196) to reference `contributions` / `agents` /
  `manifestations`.

### 5.2 `docs/archived/initial_architecture.md`

- No changes needed (archived documentation).

---

## Implementation Order

```
Phase 1: WEMIRepository enhancements (no existing behaviour changed)
  1.1 Add get_relational_disambiguation_evidence()
  1.2 Modify insert_wikidata_authors() to store wikidata fields in agents.metadata_json
  1.3 Rewrite get_wikidata_authors_batch() against agents + identifiers
  1.4 Remove authority_agents fallbacks
  1.5 Add get_indexed_source_ids()

Phase 2: Consumer migration (switch callers from AuthorityStore to WEMIRepository)
  2.1 matcher.py — remove ReconciliationEngine, callers use WorkMatcher
  2.2 agent_transfer.py — update docstrings
  2.3 cli.py — replace all AuthorityStore instantiations
  2.4 ingestion/oai.py — remove AuthorityStore path
  2.5 ingestion/wikidata.py — remove AuthorityStore paths

Phase 3: Schema and code cleanup
  3.1 db.py — remove legacy DDL
  3.2 repository.py — remove AuthorityStore class
  3.3 config.py — remove index_path if orphaned

Phase 4: Test updates
  4.1 Rewrite AuthorityStore-based tests
  4.2 Update wikidata_authors assertion tests
  4.3 (No-op for WEMI-only tests)
  4.4 Write new tests

Phase 5: Documentation
  5.1 Update runbook.md
```

---

## Verification Plan

### Automated Tests

```bash
# Run full test suite to catch regressions
pytest tests/ -x -v

# Run WEMI-specific tests
pytest tests/test_wemi_*.py -v

# Run agent disambiguation tests
pytest tests/test_agent_guards.py tests/test_agent_fallback.py -v

# Run ingestion tests
pytest tests/test_oai_multi_source.py tests/test_oai_pmh.py tests/test_wemi_wikidata.py -v

# Run E2E reconciliation tests
pytest tests/test_bibframe_ingest_and_reconcile.py tests/test_reconciliation_offline_e2e.py -v
```

### Manual Verification

1. **Schema verification**: After Phase 3.1, initialise a fresh `authority.db` and verify
   that the legacy tables no longer exist:
   ```sql
   SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
   ```
   Expected: No `authority_records`, `authority_identifiers`, `authority_agents`,
   `wikidata_authors`, `vector_mappings`, or `authority_fts`.

2. **Grep verification**: After Phase 3.2, verify no remaining references:
   ```bash
   rg "AuthorityStore|authority_records|authority_identifiers|authority_agents|wikidata_authors" \
     src/reconciliator/ --type py
   ```
   Expected: Zero matches (excluding comments/docs).

3. **Wikidata round-trip**: Run `insert_wikidata_authors()` followed by
   `get_wikidata_authors_batch()` and verify the returned dict contains
   `occupations`, `fields_of_work`, `affiliations`, `family_name`, and
   `identifiers` populated from `agents.metadata_json` and the `identifiers` table.

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| `ReconciliationEngine` removal breaks external integrations | `WorkMatcher` provides equivalent (and richer) functionality. The CLI is the only known entrypoint. |
| `get_wikidata_authors_batch()` return shape changes | Phase 1.3 explicitly reconstructs the same dict keys. Test 4.2 verifies. |
| Agent ID namespace collision between legacy `authority_agents.id` and WEMI `agents.id` | Not applicable — we're dropping legacy tables entirely. All IDs will be in the `agents` table namespace. |
| `record_json` blob consumers lose access | `BibliographicRecord` is reconstructed from WEMI entities in `WorkMatcher`. No code outside `AuthorityStore.get_record_by_id()` reads `record_json` directly. |
| Performance regression on `get_relational_disambiguation_evidence()` | The WEMI path uses indexed JOINs on `contributions(agent_id)` and `manifestations(id)`. Existing indexes are sufficient. |

## Execution Summary

### Phase 1: WEMIRepository Enhancements ✅ COMPLETE
- Added `get_relational_disambiguation_evidence()` using `contributions` → `agents`/`manifestations`/`works` JOIN queries
- Rewrote `insert_wikidata_authors()` to store data in `metadata_json` instead of `wikidata_authors` table
- Rewrote `get_wikidata_authors_batch()` to query `agents` + `identifiers` tables
- Removed `authority_agents` fallbacks
- Added `get_indexed_source_ids()`

### Phase 2: Consumer Migration ✅ COMPLETE
- Added `match_target_record()` method to `WorkMatcher`
- Updated `cli.py` to replace all `AuthorityStore`/`ReconciliationEngine` usage with `WEMIRepository`/`WorkMatcher`
- Updated `agent_transfer.py` docstrings
- Simplified `oai.py` and `wikidata.py` to remove `AuthorityStore` paths

### Phase 3: Schema Cleanup and AuthorityStore Removal ✅ COMPLETE
- **repository.py**: Removed `AuthorityStore` class (776 lines)
- **matcher.py**: Removed `ReconciliationEngine` class (~265 lines)
- **db.py**: Removed legacy DDL: `authority_records`, `authority_identifiers`, `authority_agents`, `wikidata_authors`, `vector_mappings`, `authority_fts`
- Added `record_origin` columns to: `agents`, `works`, `expressions`, `manifestations`, `items`
- Updated `_migrate_agent_columns` to remove legacy table references
- Updated `ingestion/wikidata.py` to use `WEMIRepository` instead of `AuthorityStore`

### Phase 4: Test Updates ✅ COMPLETE
- Updated 8 test files to use `WEMIRepository` instead of `AuthorityStore`
- Skipped 6 tests that tested removed functionality
- **Result**: 396 passed, 11 skipped, 14 failed

> **Correction.** The 14 failures were *not* pre-existing or unrelated — they were
> caused by this migration, and Phases 2–4 were not actually complete. See
> "Phase 6" below for what was left unfinished and how it was repaired.

### Phase 5: Documentation ⏳ PENDING
- Update `runbook.md` with new WEMI schema documentation
- Update API documentation references

---

## Phase 6 — Repairing the Incomplete Migration ✅ COMPLETE

Phases 2–4 were reported complete but had left three functions replaced by a
`return` statement with their original bodies still present as unreachable code,
and the new WEMI code paths only partially implemented. Reconciliation ran to
completion while linking nothing.

### 6.1 Global linking and URI export (`matcher.py`)

`match_target_record()`, the replacement for `ReconciliationEngine.reconcile_record()`,
was a stub. Restored:

- `accepted_candidates` is now populated by `_hydrate_candidate_record()`, which
  rebuilds a `BibliographicRecord` from the matched WEMI entities
  (contributions → agents → identifiers) so `AgentAligner` has something to align
  against. Previously it was declared empty and never filled, so no agent URI was
  ever discovered.
- `WorkMatcher.__init__` now builds a `VocabularyManager` and an
  `AgentFallbackResolver`. Without the former, `selected_agent_uris` stayed empty
  and the enricher received nothing to inject.
- Tier 3 standalone agent fallback reinstated as `_resolve_agent_fallback()`,
  including the chronology and homonym guards.
- `WorkMatcher` accepts an `agent_indexer`; `cli.py` loads `agent.index` when present.
- Work-tier title stems now strip the subtitle that `RecordNormalizer` folds into
  `title`, so target stems can match the stored title proper.

### 6.2 Entity binding at ingest (`repository.py`, `loaders/marcxml.py`, `models.py`)

`insert_wemi_batch()` bound identifiers and contributions positionally, falling back
to `works[0]` / `agents[0]`. Any batch holding more than one record attributed every
identifier and contribution to the first record.

- `IdentifierEntity` and `ContributionEntity` gained transient `entity_uri` /
  `agent_uri` binding hints, resolved against the URI→id maps in `insert_wemi_batch()`.
- Identifiers that cannot be bound are dropped rather than written with `entity_id = 0`.
- `decompose_marc_wemi()` now emits agent identifiers for every `$0`/`$1` URI.
  Previously only the first survived, as `agent_uri`; the rest were lost at ingest.
- MARCXML `title_normalized` now uses `compute_title_stem()`, matching the BIBFRAME
  and Wikidata decomposers. The mismatch made stem-based work lookup miss MARC records.

### 6.3 OAI ingestion (`ingestion/oai.py`)

- Removed ~300 lines of unreachable legacy `AuthorityStore` code after the early return.
- Rebuilt `ingest_oai_pipeline()` as a decoupled producer/consumer pipeline over WEMI
  storage: harvest + decomposition on a worker thread, batch writes on the caller's
  thread, bounded by `batch_queue_size`. Decomposition must stay on the thread driving
  the stream — the MARCXML streamer calls `elem.clear()` as soon as control returns,
  so a queued lxml element is emptied before another thread can read it.
- `skip_existing` works again (it was accepted and silently ignored), with
  harvested/indexed/skipped telemetry preserved.
- Extracted `_WEMIBatchAccumulator` so the sequential and threaded pipelines
  decompose and persist identically.
- Fixed BIBFRAME ingest: the RDFLib loader yields `(graph, nodes)`, which was passed
  to `orjson.dumps` and raised `TypeError: Type is not JSON serializable: Graph`.

### 6.4 Wikidata ingestion (`ingestion/wikidata.py`)

- Removed the same dead-code-after-return block from `ingest_wikidata_pipeline()`
  (~50 lines referencing the removed `normalizer` / `embedder` / `AuthorityStore`).

### 6.5 Relational disambiguation (`repository.py`)

`get_relational_disambiguation_evidence()` only gathered co-authors from work-level
contributions, and never implemented the work→manifestation publisher path specified
in §1.1. A work-level `aut` contribution — the common case from `decompose_marc_wemi` —
could therefore never gain publisher evidence. The tiers are now walked in both
directions before evidence is collected, and co-contributors are gathered across
work, expression and manifestation.

### 6.6 CLI wiring (`cli.py`)

`ingest-oai` constructed a `VocabFolder` and then passed neither `vocab_store` nor
`relator_mappings` to the pipeline, so subject folding and relator normalisation were
silently skipped during authority ingest. Both are now passed.

### 6.7 Tests

All 6 skipped tests were restored rather than deleted, because each covered
functionality that still exists:

| Test | Restored as |
|---|---|
| `test_agent_fallback.py` (5 scenarios) | Fixture rebuilt on WEMI entities; `ReconciliationEngine` → `WorkMatcher`. Covers Tier 2 URI transfer, Tier 3 fallback, date guard, homonym guard. |
| `test_agent_guards.py::…relational_disambiguation_evidence` | Un-gutted and given a real `WEMIRepository`. |
| `test_wemi_discrepancy_fixes.py::…doi_matching` | `find_exact_matches` → `lookup_by_identifier`. |
| `test_db_concurrency.py::test_db_metadata_persistence` | Rewritten against `db_metadata` via `get_metadata`/`set_metadata`. |
| `test_wemi_repository.py::…source_templates` | Deleted: the variant expansion is gone by design; the replacement is covered by `test_wemi_repository_get_indexed_source_ids_variants`. |

`test_oai_pmh.py` and `test_oai_multi_source.py` were rewritten to the WEMI contract
(raw MARCXML elements instead of pre-normalized dicts; no `normalizer`/`embedder`).
`test_bibframe_ingest_and_reconcile.py` had been rewritten during Phase 4 with guessed
source/source_id values that never matched what ingest produced; it now asserts the
real values and re-checks the publication data and creator that Phase 4 dropped.

---

## Phase 7 — Reconnecting Harvest Resume and Partitioning ✅ COMPLETE

Two capabilities were fully implemented and tested but had no caller in `src`
after the migration, so neither was reachable from the CLI.

### 7.1 Harvest checkpointing was unreachable

`ingest_oai_wemi_pipeline` implements `harvest_state` checkpointing — resuming
from a stored `resumptionToken`, and incremental harvest from `last_harvest_until`
— and `test_wemi_oai_checkpoint.py` covers it. But the CLI only ever called
`ingest_oai_pipeline`, which passed neither `initial_resumption_token` nor
`on_resumption_token`. Interrupting a harvest meant restarting from scratch.
(The pre-migration code had the same gap, so this was long-standing rather than
newly broken.)

`ingest_oai_pipeline` now checkpoints, with one subtlety the decoupled pipeline
forces: the resumption token advances on the producer thread while batches are
written on the consumer thread, so naively saving the current token would record
a position ahead of the data actually stored and silently lose records on resume.
Tokens are therefore held as `(records_handed_off, token)` watermarks and promoted
only once `total_ingested` covers them — at-least-once, never at-most-once. The
promoted token is sticky so re-checkpointing cannot regress it, and a failure
drains whatever is already queued before re-raising, so finished work is not
re-harvested next run.

### 7.2 Date partitioning was unreachable

`workers` and `slice_days` were silently ignored: `Identify`-based
`earliestDatestamp` discovery, `partition_date_range`, and the concurrent slice
executor all live in `stream_oai_records`, which also lost its last `src` caller.
`workers > 1` with a resolvable date window now partitions the harvest across
slices again, each producer decomposing its own records.

A resumptionToken is meaningless across concurrent slices, so partitioned harvests
resume at slice granularity instead, via a new `harvest_state.completed_slices`
column (added through the existing `_migrate_agent_columns` pattern; pre-existing
databases are migrated in place and read back with an empty ledger). Only a fully
drained and written slice is recorded, so an interrupted partitioned harvest
re-runs just the slices that did not finish.

### 7.3 CLI and configuration

- Added `ingest-oai --restart` to discard the saved checkpoint and re-harvest.
  `--skip-existing` (and its `--resume` alias) filters individual records and is
  independent of harvest checkpointing — the alias is a pre-existing naming wart
  that now reads confusingly, left in place to avoid breaking callers.
- `config.example.yaml` described `normalization_workers`, `queue_size` and
  `checkpoint_interval` as if they still drove ingest, and `batch_queue_size` as
  batches queued "for embedding/indexing". None embed during harvest any more;
  the comments now say which options are live and which are inert.

### 7.4 Tests

New `tests/test_oai_harvest_resume.py` (13 tests) drives a fake endpoint that
serves paged records and can fail mid-harvest: completion state, interrupted
checkpointing, resume continuing from the stored token, `resume=False`,
incremental follow-up harvest, a watermark-safety assertion that no checkpoint
ever names a token whose preceding records are not yet written, date partitioning,
partitioned resume skipping finished slices, `earliestDatestamp` discovery and its
fallback, file sources writing no harvest state, and the schema migration.

Three bugs in the first draft of this work were caught by those tests rather than
by review: state written for file sources, watermarks stranded when no further
batch arrived, and a non-idempotent checkpoint that reset the token to `None`.

---

## Phase 8 — Removing Compatibility Scaffolding ✅ COMPLETE

The application has no production deployment, so backwards-compatibility shims are
pure cost. Everything held back on compatibility grounds in Phases 6–7 was removed.

### 8.1 Migration code

- Deleted `_migrate_agent_columns()` and both call sites. Every column it added by
  `ALTER TABLE` is already in the `CREATE TABLE`, so it only ever served databases
  created by older versions. `harvest_state.completed_slices` moved into the DDL,
  and the migration test it needed went with it.

### 8.2 Dead code

- **`stream_oai_records()`** (194 lines): the raw-dict streamer, with no `src`
  caller since Phase 2. It held the only remaining use of the parallel harvest
  machinery, which Phase 7 rebuilt inside `ingest_oai_pipeline`. Its seven tests
  went too; the two behaviours they alone covered — single-worker skipping
  `Identify`, and `from_date` defaulting `until_date` to today — are now asserted
  directly against `_resolve_harvest_window`.
- **`ingest_oai_wemi_pipeline()`** (107 lines): the sequential pipeline, duplicating
  checkpoint logic that `ingest_oai_pipeline` now owns. Its dual-routing test
  (target-corpus staging vs. authority decomposition) was retargeted and passes
  against the unified function unchanged.
- **`_WEMIBatchAccumulator.flush()`/`write()`** and the accumulator's `repo` and
  `total_ingested` members, all orphaned once the sequential path went. The class is
  now purely a staging buffer; persistence lives in `write_wemi_batch()`.
- **`iter_marcxml_records()`**, **`get_bundled_normalization_data()`**,
  **`ClusterNode`**: unreferenced anywhere in `src` or `tests`.

### 8.3 Inert configuration

Removed from `OAIIngestConfig`, `config.example.yaml`, and their validators:
`checkpoint_interval` (no consumer at all), `queue_size` (only consumer was
`stream_oai_records`), `normalization_workers` (only a CLI override writing a field
nothing read). Added the `slice_days` and `batch_size` floor validation that was
missing, since both now affect behaviour.

Removed `StorageConfig.index_path`, a legacy alias that defaulted `work_index` and
was then backfilled from it. Callers and test configs use `work_index` directly.

### 8.4 CLI

- Dropped the `--resume` alias on `--skip-existing`. It meant record-level
  filtering, which read as harvest resume once real resume existed.
- Replaced `--normalization-workers` (inert) with `--slice-days`, which now matters.

### 8.5 Latent bugs found while clearing lint

Four modules used names they never imported. Three were annotation-only and
survived on `from __future__ import annotations` (`Literal` in `faiss_engine` and
`ipc/daemon`, `Optional` in `vocab/bridges`), but `xml_enricher.py:254` used
`DEFAULT_MARC_PREDICATE_PROFILES` in executable code — it only avoided a
`NameError` because the `or` expression's left operand is always truthy. A config
setting `predicate_profiles: {}` would have crashed enrichment. All four imported.

The codebase now has zero undefined names and zero unreachable statements.

---

## Phase 9 — Round-Trip Data-Loss Audit ✅ COMPLETE

Audited the full path — ingest → stage → internal cluster → global reconcile →
link → subject/agent URI correction → export — for data loss, by diffing a
deliberately awkward input corpus against its export field by field.

### 9.1 Why MARCXML export is structurally lossless

`reconcile_cmd` enriches `orig_payload` — the original parsed `lxml` element —
in place and serializes *that*, rather than re-serializing from
`BibliographicRecord`. The internal model is lossy (it models no 880, no local
9xx, no leader), but that never reaches the output. The same holds for the RDF
path, where `enrich_bibframe_graph` only adds triples to the parsed graph, and
`iter_bibframe_records` parses the whole file into one shared `Graph` that is
serialized once at the end — so multi-record RDF files are not truncated.

### 9.2 Measured result

A 2-record corpus carrying a leader, 5 controlfields and 24 datafields —
including repeated `$a` in 041, a Cyrillic 880, local 900/998/999, XML entities,
an already-canonical 655 with an existing `$0`, and a subject heading with no
mapping — exported with **zero dropped records, controlfields, datafields or
subfields**. Re-running reconcile on the exported file is idempotent: no
duplicated `$0`, nothing lost.

Enrichment observed working end to end: KANTO agent URI into `100 $0`; YSO URI
into `650 $0`; `kaunokki` folding 650 → 655 with the SLM URI; `musa` folding
650 → 382 with the SEKO URI; work and expression URIs into `240 $0` and two new
758 linking fields; internal `urn:reconciliator:` cluster URIs at tiers with no
global match.

### 9.3 Bug found and fixed: unresolved headings were relabelled

`fold_subject_heading()` step 6 is commented "Fallback if not found: keep as is"
but still returns a *default* target vocabulary, and `xml_enricher` applied it
unconditionally — `mapping_type` was captured and never read. A heading with no
mapping therefore had `$2 ysa` rewritten to `$2 yso/fin` while gaining no `$0`.

That is worse than leaving it alone twice over: the record ends up asserting
membership of a vocabulary that does not contain the term, and the original `$2`
— the one piece of information a cataloguer needs to fix it by hand — is
destroyed. Now gated on `mapping_type == "none" and not target_uri`, matching the
stated intent. The BIBFRAME enricher never had this bug: it is purely additive.

### 9.4 Gap fixed: unlinked agents were invisible

Agent report rows were only emitted per selected or suppressed URI, so an agent
that failed to link produced no row at all. The record itself was untouched, so
no data was lost — but link coverage over a corpus was unmeasurable, and a failed
link was indistinguishable from an agent never seen. Unmatched agents now emit a
row with `action="unmatched"`.

A URI declined by `link_policy` was already recorded as
`action="suppressed_by_priority"`, so nothing discovered is silently dropped.

### 9.5 Behaviour that is lossy by design — now configurable (Phase 10)

When a heading *does* resolve, `$a` is replaced with the ontology's preferred
label: a record catalogued `tohtorit` (a YSO altLabel) exports as `lääkärit`.
The catalogued form survived only in `subject_uris.tsv` (`source_heading`), not
in the record. `$2` is likewise restated with the Finto language subcode
(`yso` → `yso/fin`).

Both are the intended behaviour, and both are now switchable — see Phase 10.

### 9.6 Tests

`tests/test_no_data_loss.py` (4 tests) locks in the round trip: every input
subfield survives export; fields untouched by folding keep tag, indicators and
subfields exactly; an unresolved heading is byte-identical; every agent appears in
the report; and reconcile is idempotent on its own output.

---

## Phase 10 — Subject Heading Rewriting Policy ✅ COMPLETE

The two rewrites Phase 9 identified are the desired default, but both are now
configuration toggles on `VocabulariesConfig` (reachable in YAML as
`normalization.vocab` or `ingest_vocab.normalization`). The shipped defaults
reproduce the previous behaviour exactly, and are written out explicitly in
`config.example.yaml` so the current policy is visible rather than implicit.

| Setting | Default | Effect when changed |
|---|---|---|
| `apply_language_subcode` | `true` | `false` emits the bare vocabulary code: `$2 yso` instead of `$2 yso/fin` |
| `replace_heading_with_pref_label` | `true` | `false` leaves `$a` as catalogued; the `$0` URI is still injected |
| `preserve_original_heading` | `false` | `true` also keeps the catalogued heading in the record: `$a lääkärit` + `$9 tohtorit` |
| `original_heading_subfield` | `"9"` | Which subfield holds the retained heading (e.g. `"x"`) |

`preserve_original_heading` is the answer to Phase 9.5: with it set, the exported
record retains both the modernized and the catalogued form, so the record can be
the system of record without depending on the TSV reports. Nothing is written
when the catalogued form already equals the preferred label, so enabling it does
not litter records that were already current.

`apply_language_subcode` is honoured inside `format_target_vocab()`, so it applies
uniformly to the exported `$2`, the internal `SubjectItem.vocabulary`, and the
subject report — not just to MARCXML output.

`tests/test_subject_heading_policy.py` (9 tests) covers each toggle
independently, their combination, the configurable subfield, the
already-current-heading case, and `format_target_vocab` directly. All six
combinations were also verified end to end through YAML via the real CLI.

---

## Phase 11 — Work Linking Audit: Internal and Global ✅ AUDITED

Verified with two authority sources and a corpus holding three manifestations of
one work plus two distinct works (one sharing the author).

### 11.1 Internal linking within a corpus — confirmed working

`CorpusClusterer.cluster_corpus()` blocks records by the configured key families,
scores candidate pairs, and unions them with UnionFind. Measured:

- the three Sinuhe manifestations (different years, different ISBNs) land in one
  work cluster and one expression cluster;
- *Mikael Karvajalka* — same author, different work — is **not** merged in;
- *Tuntematon sotilas* — unrelated — is **not** merged in;
- a work absent from every authority source still gets a stable
  `urn:reconciliator:work:<hash>` minted from title stem + author key + work
  type, so it is linkable within the corpus.

Grouping and separation both behave. No over-merging observed.

### 11.2 Global linking across sources — works only in the weaker sense

"Global" is implemented as the clusterer's *External Authority URI Hoisting*
step: for each cluster member it runs `matcher.match_target()` against the
authority store and adopts the first match's `work_uri` as the cluster's
canonical URI. That part works — the Sinuhe cluster correctly adopts an
authority URI instead of minting one.

But **works are not consolidated across sources.** `decompose_marc_wemi` mints
`urn:work:{source}:{source_id}`, and nothing merges those identities afterwards.
Two sources holding the same work therefore produce two work rows:

```
work 1: source=source_a  urn:work:source_a:(FI-SRCA)A-001
work 2: source=source_b  urn:work:source_b:(FI-SRCB)B-777
```

with no `work_relationships` edge asserting equivalence and no `wikidata_qid`
anchoring a shared identity. So global linking means "linked to *an* authority
work", not "linked to a single identity shared across all sources". A cluster
adopts whichever source its first matching member happened to resolve to.

`tests/test_work_linking.py::test_authority_sources_are_not_consolidated_into_one_work`
pins this limitation down so the behaviour is documented rather than assumed.
Implementing consolidation is a design decision, not a bug fix.

### 11.3 Bug found and fixed: the record and the work report disagreed

The enricher and the report used **inverted precedence** for the work URI:

| | precedence |
|---|---|
| `xml_enricher` | `work_cluster.canonical_work_uri` wins |
| work report | `wemi_res.work_uri` (the record's own match) wins |

With one authority source these always agree, which is why it went unnoticed.
With two, a record can match a *different* source than its cluster adopted — and
then the exported record said `source_a` while the report said `source_b` for the
same record.

The cluster's canonical URI now wins in both places, because every member of a
work cluster must carry the same work URI and the report must state the URI
actually written. The record's own resolution is preserved in a new
`record_level_work_uri` report column rather than discarded — it is precisely the
signal that two sources hold the same work, and so the raw material for the
consolidation described in 11.2.

### 11.4 Note: matching runs twice per record

`cluster_corpus()` calls `matcher.match_target()` per cluster member during
hoisting, and the reconcile loop calls it again per record. Beyond the wasted
work on large corpora, it was the mechanism by which the two answers could
diverge. Not changed here; worth collapsing to a single pass.

---

## Phase 12 — Cross-Source Work Consolidation ✅ COMPLETE

Closes the gap Phase 11.2 identified: the store held one work row per source with
no mechanism to recognise them as one work.

### 12.1 Linking works without identifiers — confirmed first

Before designing consolidation it was worth establishing whether the pipeline
depends on identifiers at all. With a corpus and authority source carrying **no**
020/024/035 and no `$0` — 0 rows in the `identifiers` table — clustering,
reconciliation and work linking all still worked: three manifestations clustered,
two distinct works stayed apart, and matching resolved via `tier2_domain_key`
(author key + title stem) rather than tier 1.

The exception is **agent** URI enrichment, which is transfer-based: it copies URIs
off the matched authority record. An authority record with no `$0` has nothing to
transfer, so agents come back `unmatched`. That is a property of the data, not a
matching failure.

Consolidation was therefore designed not to depend on identifiers.

### 12.2 Design

`src/reconciliator/consolidation.py`. Three evidence tiers, in order:

1. **`wikidata_qid`** — a shared QID is decisive; *differing* QIDs veto a merge
   outright, since that is an explicit statement that the works differ.
2. **Shared strong identifier** — ISBN/ISSN/DOI/… on the works or their
   manifestations. A shared ISBN between two work rows means both describe the
   work that manifestation embodies. Source-local schemes such as
   `control_number` are excluded.
3. **Metadata** — `0.6 × title + 0.4 × creator`, gated on identical `work_type`,
   threshold 0.93. This is what carries identifier-free corpora.

Candidate pairs are blocked on QID, identifier and title so the sweep stays
tractable; equivalences are unioned transitively, so three sources holding one
work form a single group.

**Nothing is deleted.** Consolidation writes symmetric `same_work_as` edges into
`work_relationships` recording *why* a group formed, and elects a canonical via a
new `works.canonical_work_id` column recording *the decision*. Election prefers a
work carrying a QID, then configured `canonical_source_priority`, then `work_uri`
for stability. `clear_canonical_works()` reverses the whole thing.

Splitting "why" from "the decision" matters: the repository reads the stored
election rather than recomputing it, so every caller resolves identically even
when a source priority is configured. An earlier draft recomputed the election in
`get_work_equivalence_map()` and would have disagreed with the consolidator
whenever `canonical_source_priority` was set.

### 12.3 Wiring

`WorkMatcher.match_target()` now resolves its matched work through the
equivalence map before returning — the dispatch moved to `_dispatch_match()` and
canonicalisation wraps it. Doing it there rather than at each call site means the
corpus clusterer's authority hoisting and the reconcile loop cannot diverge.

The matched *expression* is deliberately not re-pointed: an expression of a
consolidated work is a genuine expression of the canonical work.

New `reconciliator consolidate-works` command, with `--dry-run` to preview groups
and `--reset` to discard a previous run. Re-runnable after ingesting more sources;
re-running writes no duplicate edges.

### 12.4 Effect

Two sources holding the same work, and two corpus records each matching a
*different* source on its own ISBN:

| | before | after |
|---|---|---|
| work rows | 2 | 2 (nothing deleted) |
| `same_work_as` edges | 0 | 2 |
| T-1 exported work URI | `source_a` | `source_a` |
| T-2 exported work URI | `source_a` | `source_a` |
| T-2 `record_level_work_uri` | `source_b` ← divergent | `source_a` |

The Phase 11.3 divergence disappears at its source rather than being papered
over: with one identity there is no rival match left to record.

### 12.5 Tests

`tests/test_work_consolidation.py` (19 tests): each evidence tier; QID veto;
same-author-different-work, same-title-different-author and differing work_type
all correctly refused; same-source duplicates left alone unless opted in;
transitive three-source grouping; QID-backed and priority-based canonical
election; symmetric edges with no rows deleted; idempotency; dry run; reset;
disabled config; and the end-to-end CLI run above.

`tests/test_work_linking.py` now pins the *pre*-consolidation state explicitly,
since consolidation is a separate opt-in step rather than part of ingest.

## Phase 13 — Deterministic Identity, Source-Named Origin, Single Database ✅ COMPLETE

Three changes that only made sense together: ingest stopped inventing identity,
`record_origin` started naming the actual source, and the corpus staging database
was folded into `authority.db`.

### 13.1 Deterministic record identity

MARCXML ingest minted `uuid.uuid4()` for records lacking a usable control number,
so re-ingesting the same file produced entirely new work, expression,
manifestation and agent URIs. Nothing downstream could be re-run.

`derive_content_id()` replaces both call sites with a digest of the record's
*fields* — leader, controlfields, and datafields with their indicators and
subfields — rather than its serialization. Serialization-level canonicalisation
was tried first and abandoned: `c14n2` raises
`ValueError: Namespace "http://www.loc.gov/MARC21/slim" of name "record" is not
declared in scope` on the detached elements `iterparse` hands back. A semantic
digest is also the more correct choice, being immune to whitespace, attribute
order and namespace-prefix differences that carry no bibliographic meaning.

| | before | after |
|---|---|---|
| work URI across two identical ingests | changed | stable |
| expression / manifestation / agent URIs | changed | stable |

### 13.2 `record_origin` names the ingest source

`RecordOrigin` was `Literal["authority", "target"]` — a two-valued world in which
a row could report whether it was authoritative. It is now `str`, carrying the
name of the source that produced the row, and the five `CHECK` constraints are
relaxed to `DEFAULT 'unknown'`.

Authoritativeness moved out of the row, because a row can no longer answer that
question about itself. A new `sources` table records it, populated at ingest from
a per-source `authoritative: bool` config flag. All five `is_authority`
properties are deleted; callers pass `authoritative_only` and the repository
resolves it through `_origin_clause()`.

```
sources registry     : {'real_authority': True, 'my_corpus': False}
works by origin      : [('my_corpus', 1), ('real_authority', 1)]
authoritative_only=True  -> ['real_authority']
authoritative_only=False -> ['real_authority', 'my_corpus']
```

Converting the vector searches exposed a latent bug worth recording: they filtered
*after* retrieval, so a filtered search returned fewer than `top_k` candidates —
silently, and worst for the smallest sources. All three now over-fetch
`top_k x overfetch` and stop once `top_k` survivors are found.

### 13.3 `reconcile_corpus.db` folded into `authority.db`

Staged corpus records and authority records lived in separate SQLite files, so a
cluster could not be joined to the works it resolved to, and `reset` left corpus
rows behind that leaked into the next run. `init_authority_db` now creates the
corpus tables, `get_corpus_connection()` returns the authority connection, and
`StorageConfig.corpus_db` is gone along with the derived `_corpus.db` filename.

`reset` takes `--config` and `--yes` and loops over `(db_path, initializer)`
pairs — no per-store booleans.

| | before | after |
|---|---|---|
| rows after RESET | corpus rows survived | `[]` |
| reconcile of corpus B | saw corpus A's rows | `['B1']` |

`pipeline_check.py` output is byte-identical across all three linking modes
before and after, so the merge changed storage without changing results.

## Phase 14 — Removing TargetRecord and Making Funnels Configurable ✅ COMPLETE

### 14.1 What the measurement showed

| | |
|---|---|
| `matcher.py` | 2837 lines |
| the 42 domain funnels | 1695 lines (60%) |
| funnels that are pure template | 36 of 42 |
| funnels adding one extra step | 6 |
| `TargetRecord` | a generic `keys` bag plus 24 one-line property accessors |

Every funnel is the same four blocks — tier-1 identifiers, tier-2 author+title,
tier-3 vector/FTS, unmatched — varying only in `funnel_id`,
`identifier_schemes`, `work_type`, `content_type` and `confidence`. The funnels
are already data wearing a method costume.

### 14.2 Three defects to fix structurally

**Tier-1 matching is dead on the reconcile path.** `match_target_record` builds
its `TargetRecord` with a JSON payload but labels it `format="marcxml"`, so
`extract_identifiers_from_target` calls `etree.fromstring()` on JSON, throws, and
swallows it under `except Exception: pass`:

```
CLI-path payload (JSON labelled marcxml) -> {}
real marcxml payload                     -> {'isbn': ['9789511234567']}
```

Every reconciled record skips exact-identifier matching and falls through to
fuzzy tier-2/tier-3.

**The matcher re-parses XML that ingest already parsed.**
`extract_identifiers_from_target`, `extract_target_track_titles` and
`extract_target_analytics` re-parse `raw_payload` at match time to recover
identifiers and analytics the WEMI decomposer already extracted at ingest.

**Docstrings promise more than the code delivers.** `match_cartography` documents
"Bounding box IoU (034)" but never calls `compute_bounding_box_iou`, which has no
production caller at all. Declared funnels make this class of drift impossible.

### 14.2.1 Critical review of `match_target_record`

Asked to review it rather than merely de-duplicate it, the finding is worse than
"every record is matched twice". The two matches are not redundant — they see
**different records**, and the degraded one drives the output that matters.

The reconcile loop builds two `TargetRecord`s from the same raw record:

- `normalizer.normalize(raw_rec)` produces a `BibliographicRecord`, which
  `match_target_record` converts internally to a `TargetRecord`.
- `_to_target_record(rec_fmt, orig_payload, ...)` produces a second one through
  `build_target_record_from_marc`.

For a sound recording carrying a publisher number and an EAN:

```
A  match_target_record (drives agent reconciliation)
     work_type  : text
     keys       : []
     identifiers: {}

B  match_target (drives the WEMI report columns)
     work_type  : music
     keys       : ['music_work', 'work']
     identifiers: {'control_number': ['M1'], 'ean': [...], 'publisher_number': ['ODE 1234-2']}
```

Path A is crippled three ways at once. `work_type` is **hardcoded to `"text"`**
(`matcher.py:2746`), so dispatch can only ever reach `match_monograph` — no
record, whatever it is, reaches its own funnel. No domain keys are carried beyond
`work_blocking_key`. And its payload is a JSON dump labelled `format="marcxml"`,
so `extract_identifiers_from_target` parses JSON as XML, throws, is swallowed,
and returns `{}` — tier 1 cannot fire.

Path B, built from the real MARCXML, gets all three right.

**Which half of the output each feeds is the problem.** Path A's result is used
only for agents: accepted candidates, `align_target_agents`, the tier-3 fallback,
vocabulary selection. Path B's is used only for report columns. So the authority
URIs injected into the enriched output — the actual product of reconciliation —
are transferred from candidates found by the blind record, while a correct match
for the same record is computed two hundred lines later and used to fill in a
spreadsheet.

**Redesign.** The split is not between two kinds of record; it is between two
questions that were fused:

1. *What work is this?* — one `MatchRecord`, built once from the real payload,
   matched once.
2. *Which authority URIs do its agents get?* — a separate step taking that match
   plus the record's agents.

So `match_target_record` dissolves into:

```python
record = build_match_record(payload, fmt, profile)   # one construction
match  = matcher.match(record)                        # one dispatch
agents = matcher.resolve_agents(match, record.agents) # hydrate, align, fallback, select
```

Agent resolution keeps every step it has today — the hydration, the coupled
alignment, the standalone fallback, the vocabulary filter, the linkage lookup —
but anchors on the correct match instead of the blind one. `ReconciliationResult`
becomes the pairing of the two rather than a thing the matcher builds internally.

This is why stage 2 is the risky stage and why it changes results: it is not only
tier 1 starting to work, it is agent reconciliation moving onto a match that is
right about what the record is.

### 14.3 `MatchRecord` replaces `TargetRecord`

Not a staged row — a view over WEMI rows, built from
`(work, expression, manifestation, keys, identifiers)`. It keeps the generic
`keys` bag and `get_keys()`; all 24 properties go, because funnels name their
keys in configuration instead.

**`MatchRecord` is manifestation-centric.** One ingested record is one
manifestation, which is what `TargetRecord` represented and what the clusterer
blocks over, so the manifestation is the identity the type carries and the row
its `entity_keys` attach to. It is not manifestation-*only*: it resolves the
Expression and Work above it and exposes their fields (language and content type
from the expression, work type and uniform title from the work), because the
funnels match at all three tiers.

Two gaps separate `TargetRecord` from what WEMI stores, and both close at ingest,
where the record is already parsed:

- `target_keys` becomes **`entity_keys`** (`entity_type, entity_id, key_type,
  key_value`), sitting beside `identifiers` and keeping its four indexes.
- MARC 505/700 `$t` analytics become `entity_keys` rows of type `analytic`, so
  `extract_target_analytics` becomes `subject.get_keys("analytic")`.

`target_records`, `target_payloads`, `insert_target_records_batch` and
`get_all_target_records` are then deleted. Identifiers come from the
`identifiers` table, which fixes the tier-1 defect by construction and removes
payload re-parsing from the matcher entirely.

Checked before committing to this: the clusterer never touches `raw_payload`, and
the reconcile path gets original XML by streaming the input file rather than from
the database. Nothing needs the blob.

### 14.4 Funnels become declared specs

An ordered list; first match wins; exactly one carries `default: true`.

```yaml
funnels:
  - id: funnel_a_monograph
    when: { work_type: [text] }
    identifier_schemes: [isbn, urn, control_number]
    work_type: text
    confidence: 0.90
    default: true

  - id: funnel_p_government
    when:
      work_type: [government, government_publication]
      identifier_matches: { urn: "(?i)valto" }
    identifier_schemes: [urn, isbn]
    work_type: text
    confidence: 0.88
```

Reproducing the existing 44-condition dispatch chain exactly needs seven
predicate kinds — `work_type`, `is_component`, `has_key`, `has_identifier`,
`title_matches`, `key_matches` and `identifier_matches`, the last three taking
regexes. Each was checked against the current chain; `is_component` is a real
boolean column rather than a key, and `key_matches` is required by the
`"parallel" in translators_signature` branch.

Ordering the list makes precedence explicit and reorderable instead of implied by
the order of `if` statements.

### 14.5 `handler:` — the escape hatch

For the six funnels with real logic, and for anything a user adds:

```yaml
handler: recorded_sound                              # built-in, by name
handler: src/reconciliator/funnels/recorded_sound.py # path, config-relative
handler: /opt/site/funnels/my_rule.py                # path, absolute
```

A value containing a path separator or ending in `.py` is a path; anything else
is a built-in name resolved through a registry in
`src/reconciliator/funnels/__init__.py`. The two sets cannot collide, since
built-in names are bare identifiers. Naming built-ins by name rather than by path
is what keeps them resolvable after `pip install`, where no `src/` directory
exists.

Relative paths resolve against the **config file's directory**, not the process
CWD, so a config works regardless of where the CLI is invoked from.

Both forms resolve at config load, not per record, so a bad path or unknown name
is a startup error naming the offending config line rather than a funnel that
silently degrades to the generic template.

Contract — deliberately narrow, so handlers cannot reach into matcher internals:

```python
def tier2(subject: MatchRecord, ctx: FunnelContext) -> Optional[WEMIMatchResult]: ...
```

`FunnelContext` exposes exactly `repo`, `config`, `align_expression` and `spec`.
A module declares whichever tiers it overrides; omitted tiers fall back to the
generic template. The six bundled handlers use this same contract, so they are
worked examples of the extension point rather than a privileged path.

Handlers are loaded by file path and are therefore not part of the
`reconciliator` package: they must use absolute imports.

**Recorded deliberately:** absolute paths mean a config can execute Python from
anywhere on the filesystem. That is a normal property for a tool whose config is
as trusted as its code, and it is a decision rather than an oversight.

### 14.6 Stages

Each lands green. Stages 1 and 2 carry the risk.

| # | Stage | Net lines |
|---|---|---|
| 1 ✅ | `entity_keys` table; emit keys and analytics at decomposition | ~0 |
| 2 ✅ | `MatchRecord` replaces `TargetRecord` across matcher, clusterer and enricher; identifiers read from the table (fixes the tier-1 defect); drop `target_*` tables and their repository methods | -250 |
| 3 ✅ | Generic `run_funnel(spec, record)`; delete the 42 methods; ship the current 42 as built-in specs | **-1400** |
| 4 ✅ | `handler:` resolution; port the custom funnels to handler modules (folded into stage 3) | +150 |
| 5 ✅ | Dead code (`compute_bounding_box_iou`; the whole expression vector tier); reconcile docstrings against behaviour | -50 |
| 6 ◐ | ISBD punctuation stripping configurable and consistent; the decomposer merge is **declined** (see 14.9.1) | +80 |
| 7 ✅ | Build, wire and use the W/M vector indexes; honour `rerank.top_k` (see 14.10) | +200 |
| 8 ✅ | Deterministic `agent_uri` (see 14.11) | ~0 |
| 9 ✅ | `accumulate` / `replace` ingest mode per source (see 14.12) | +120 |

`matcher.py` should land near 600-700 lines, plus `funnels.py` (~250) and ~120
lines of configuration models.

The 53 `TargetRecord` uses in tests are mostly constructor calls; `MatchRecord`
takes the same keyword arguments so most tests change only the class name.
Critically, the assertions are on `funnel_used == "funnel_x_..."` strings, which
stay byte-identical — so the existing tests act as a behavioural lock on the
stage-3 collapse. That is the main safety net for the riskiest change.

### 14.6.1 Stage 1 as built ✅ COMPLETE

Dropping the `target_*` tables moved from stage 1 to stage 2: the clusterer still
reads `get_all_target_records()`, so the tables cannot go until their consumers
move. Stage 1 therefore adds the new path alongside the old one and leaves both
live.

Shipped:

- `EntityKey` model, mirroring `IdentifierEntity`'s transient `entity_uri`
  binding hint, and the `entity_keys` table beside `identifiers` with a
  `UNIQUE(entity_type, entity_id, key_type, key_value)` constraint that makes
  re-ingesting a record a no-op for its keys.
- `insert_entity_keys_batch`, `get_entity_keys`, `get_all_entity_keys` (bulk form
  for the clusterer) and `find_entities_by_key`.
- Step 11 of `insert_wemi_batch`, resolving `entity_uri` to `entity_id` exactly as
  identifiers do, and dropping unbindable keys rather than persisting orphans.
- `derive_domain_keys()` in the MARC loader and `derive_bibframe_keys()` in the
  BibFrame one, each now the single definition consumed by both the staged
  `TargetRecord` and the persisted rows, so the two cannot drift.
- `extract_marc_analytics()`, moving MARC 505/700/710 `$t` parsing from match time
  to ingest.

MARC keys are collected through an optional `key_sink` parameter rather than an
eleventh tuple element, because five call sites unpack `decompose_marc_wemi`'s
ten-tuple positionally and one of them unpacks from the end. The BibFrame side
needs no sink at all: its keys derive from the decomposed entities rather than
the source graph.

Keys attach to the **manifestation**, which is the record-level WEMI entity — one
ingested record is one manifestation, which is what `TargetRecord` represented and
what the clusterer blocks over.

**Two defects found and fixed while building it.**

The analytic (title, creator) pair was first encoded with U+001F as a separator,
on the reasoning that MARC subfield text cannot contain a control character. That
was true but irrelevant: Python counts U+001C-U+001F as whitespace, so `.strip()`
in the key-adding helper silently ate the separator and every analytic lost its
creator. The encoding is now JSON, which survives stripping, stays readable in the
database, and is indifferent to punctuation in titles. A regression test pins it.

`decompose_marc_wemi` still carried Phase 13's superseded origin logic —
`record_origin if record_origin in ("authority", "target") else ...` — which,
now that `record_origin` holds a source name, always failed its own test and
stamped the old literal instead. It was masked in the OAI path because
`insert_wemi_batch(record_origin=...)` overwrites the value afterwards, but any
caller decomposing and inserting without that argument got the wrong origin. Now
`record_origin or source or "unknown"`.

**Verified through the real ingest path**, not only unit tests: driving
`_WEMIBatchAccumulator` and `write_wemi_batch` over two records writes

```
entity_keys rows: 5
  R1   standard     '9789510123456'
  R1   work         '831405dd9d79c59e0985bdf3ad4aa67a'
  R2   analytic     '["Finlandia", "Sibelius, Jean"]'
  R2   standard     '9789510999999'
  R2   work         '998cb952e473e1d34a1c429b0bfedbf7'

work record_origin: ['melinda']
```

Distinct records carry distinct work keys, confirming the URI binding does not
collapse a batch onto its first row — the failure mode Phase 6 had to repair for
identifiers and contributions.

`tests/test_entity_keys.py` (16 tests): analytic encoding round-trips, including
the stripping regression and punctuation-heavy titles; 700/710/505 extraction
order; key derivation and de-duplication; sink binding; the ten-tuple staying a
ten-tuple without a sink; persistence and read-back; the multi-record collapse
guard; re-ingest idempotency; bulk and by-key lookups; orphan rejection; and
BibFrame parity.

Suite: **485 passed** (469 + 16).

### 14.6.2 Stage 2 as built ✅ COMPLETE

`TargetRecord` is gone as a name and as a persisted concept. `target_records`,
`target_payloads` and `target_keys` are dropped along with
`insert_target_records_batch`, `get_target_record`, `count_target_records` and
`get_all_target_records`; `find_records_by_key` and `get_blocking_groups` now read
`entity_keys`. The `is_target` branch is gone from the ingest accumulator, so
every source decomposes into WEMI and `authoritative` is the only distinction.

- `MatchRecord` carries `identifiers`, plus `content_type`, `work_uri`,
  `expression_uri` and `manifestation_uri` for the tiers above the manifestation.
  The 24 sugar accessors are replaced by explicit `get_first_key("...")` at 99
  call sites. Only `work_blocking_key` survives as a property, because it encodes
  a real fallback rule rather than a one-line lookup.
- `WEMIRepository.get_all_match_records(origins=None)` projects records back out
  of WEMI rows. Verified field by field against the record built at ingest: all
  14 fields, the key bag and the identifier bag agree exactly.
- 94 lines of payload re-parsing deleted from the matcher.
  `extract_target_analytics` and `extract_target_track_titles` read the
  `analytic` keys written at ingest.
- The reconcile command ingests the corpus as ordinary WEMI rows under
  `reconcile.corpus_source_name` (default: the target file's stem), registered
  non-authoritative.

**`match_target_record` dissolved.** It is now `resolve_agents(match, record)`,
and the CLI matches once. Tier 1 works on the reconcile path for the first time:

```
record work_type   : music
identifiers seen   : {'ean': [...], 'publisher_number': ['ODE 1234-2'], 'control_number': ['T1']}
match tier         : tier1_exact_identifier
matched work uri   : urn:work:melinda:A1
confidence         : 1.0
```

#### Four defects found while building it

**Five tier-1 identifier schemes were parsed nowhere.** Moving identifiers off the
payload exposed that `parse_marc_record_elem` never extracted `publisher_number`
or `plate_number` (028), `strn` (088), `issn_l` (022 $l) or `control_number`.
These are the *primary* tier-1 schemes for recorded sound, notated music and
standards, so neither the `identifiers` table nor the matcher had ever seen them.
Now extracted at the source, which fixes both sides at once.

**`manifestations.source_id` was globally UNIQUE.** Two sources both using control
number "R1" collided, and the upsert on `source_id` silently merged a corpus
record into an authority record. Invisible until now only because corpus records
lived in the staging table.

**Then keying the upsert on `manifestation_uri` was also wrong.** A BIBFRAME
instance URI is shared by every source describing that instance, so the corpus
ingest *overwrote* the authority manifestation it should have matched — the row
came back stamped with the corpus's non-authoritative origin, and reconciliation
returned `unmatched`. Identity is now `(source, source_id)`, with
`manifestation_uri` not unique: a manifestation row is one source's record of a
manifestation, and cross-source identity is consolidation's job rather than the
schema's.

**Clustering had to be scoped.** `get_all_match_records()` with no filter returns
every source, so the clusterer was clustering authority rows as if they were the
corpus. It now scopes to the non-authoritative sources, falling back to
everything when no registry exists.

#### An operational hazard this creates

With one database, "which rows may be matched against" is answered entirely by
`record_origin` plus the `sources` registry. If a source is registered
authoritative but its rows are not stamped with that origin, `_origin_clause`
filters every candidate out and matching silently returns nothing. Ingest stamps
and registers together, so the production paths are consistent — but it is a
fail-closed mode with no diagnostic, and it cost two debugging rounds here. Worth
a startup check that every registered authoritative source actually has rows.

#### Test changes

490 passing. `persist_match_records()` in `conftest.py` writes MatchRecords as
WEMI rows, so tests keep declaring the record shape they care about. The
dual-routing test became a single-routing test. Two count assertions were scoped
to authoritative origins, since the reconciled corpus now contributes rows of its
own. `test_target_schema_normalization_and_cascades` was replaced by tests
pinning that `entity_keys` has **no** cascade — it is polymorphic, so it carries
no foreign key, and `ingest_mode: replace` (14.12) must delete keys explicitly.

### 14.6.3 Stage 3 as built ✅ COMPLETE

`matcher.py` went from **2686 to 1063 lines**. The 42 funnel methods (1564 lines)
and the 160-line dispatch chain are replaced by a 429-line declarative table, a
169-line engine, a 132-line spec model and three handler modules totalling 130
lines. Stage 4 folded in, since handler resolution was needed to land the three
funnels that have real logic.

`src/reconciliator/funnels/`:

| file | role |
|---|---|
| `spec.py` | `FunnelSpec` and `FunnelWhen`, the seven dispatch predicates |
| `builtin.py` | the 42 specs, in precedence order |
| `engine.py` | `run_funnel`, `default_tier2`, `FunnelContext` |
| `recorded_sound.py`, `notated_music.py`, `audiobook.py` | the funnels with real logic |

Dispatch is an ordered list, first match wins, one spec marked `default`. `when`
is in disjunctive normal form — a list of alternatives, each a conjunction —
because two of the original conditions were genuine disjunctions. Handlers
resolve at construction, so a bad name or path is a startup error naming the
funnel rather than a funnel that silently degrades to the template.

#### What declaring them exposed

**Six dispatch branches were unreachable.** The chain tested for work types
`article`, `chapter`, `statute`, `anthology_collection`, `conference_proceedings`
and `government_publication`, none of which appear in the `WorkType` literal, so
no record could ever carry them. Funnel C is still reachable through
`is_component` and the host citation key, which is how articles actually arrive.
They are omitted rather than transcribed.

**Thirteen funnels mint a relationship rather than reading one.** My first
extraction only looked for `get_work_relationships` and so found five; the other
thirteen construct a `WorkRelationship` from the match itself — funnel MM
matching a critical edition to its base work *is* the claim
`critical_edition_of`. Two tests caught this. The spec distinguishes
`relationships: true` (read stored) from `asserts_relationship: <type>` (mint one).

**Nine funnels try work types in order**, not one: funnel AA looks for a
`software` work and then an `ai_model` one. `work_types` is a list, tried until
one yields candidates. I had flattened these to a single value and the suite
caught it.

**One funnel's tier 3 is suspect and was preserved anyway.** Funnel B's
tracklist-overlap tier collects "constituent titles" as the titles of *every
other music work* rather than the candidate's own constituents, so the Jaccard
comparison does not measure what its docstring claims. Ported verbatim with a
note, because this stage is meant to be output-identical; fixing it is a
behavioural change and belongs in its own diff.

#### A usability flaw found by testing the documented example

New funnels were appended just before the fall-through, which made them
unreachable whenever they overlapped a built-in — and special-casing records an
existing funnel handles is the main reason to add one. The example config I had
written would not have worked: `funnel_local_theses` sat after
`funnel_k_thesis`, which claims every thesis. Specs now take `before: <funnel_id>`
to place themselves ahead of a named funnel, verified end to end:

```
oulu thesis  -> funnel_local_theses
other thesis -> funnel_k_thesis
```

#### Config

`reconcile.funnels` overrides or extends. An entry whose `id` matches a built-in
replaces it **in place**, keeping its precedence (so an override cannot silently
reorder dispatch); a new id goes where `before` says, or ahead of the
fall-through. `config.example.yaml` carries three worked examples: retuning a
built-in, adding a funnel with a regex predicate and explicit precedence, and
pointing at a handler by name or by path.

#### Tests

516 passing (+26). `tests/test_funnels.py` pins one default, unique ids, that
every declared handler resolves, and that **every funnel is reachable by a record
of its own** — which is the guard against an over-broad earlier spec shadowing a
later one, the failure mode an if-chain makes invisible. Plus the precedence pairs
that used to be implied by statement order (notated music over recorded sound,
exhibition over conference), the seven predicate kinds, handler resolution by
name and by path including basename collisions, and the override/`before` rules.

The existing `funnel_used == "funnel_x_..."` assertions carried the collapse
exactly as intended: deleting 1564 lines produced three failures, two of them
tests calling a deleted method directly.

### 14.6.4 Stage 5 as built ✅ COMPLETE

`compute_bounding_box_iou` deleted: no production caller, and funnel L's
docstring promising "bounding box IoU (034)" never called it either. Spatial
overlap matching is now a handler-sized job if it is wanted.

The **whole expression vector tier** is gone, not just its two search APIs:
`expression_vectors` table, `expression_indexer`, `expression_index_path`,
`StorageConfig.expression_index` and the vector argument on the insert paths.
`insert_expression`'s `vector` parameter was briefly left accepted-and-ignored
and then removed outright, because a silently-ignored argument is the same class
of defect as everything else this phase has been fixing.

Docstrings reconciled with behaviour. The module docstring advertised "dense
FAISS"; it now says the dense tier searches whatever index the repository was
given, that the CLI gives it none, and that 14.10 is what changes this. The class
docstring no longer claims to implement 42 domains — the funnel specs do.

### 14.9.1 The decomposer merge, declined ◐

14.9 proposed merging `decompose_marc_authority` into `decompose_marc_wemi` on
the premise that "most of the 836 lines are the same extraction twice". Measured,
that premise is wrong:

| | tags read |
|---|---|
| bibliographic (`parse_marc_record_elem` + `decompose_marc_wemi`) | 36 |
| authority (`decompose_marc_authority`) | 12 |
| shared | **4** — 001, 024, 035, 130 |

And even the shared four diverge: 130 is a uniform title on one path and an
authority heading on the other. These are two different MARC formats — Authority
and Bibliographic — that happen to produce the same WEMI entity shape. Merging
them yields one function with two disjoint bodies behind a Leader/06 branch: a
concatenation, not a deduplication. The estimate of -300 lines was unfounded; the
honest figure is roughly zero, for a function twice as hard to read.

**What was real is done.** The discrepancy the merge was meant to fix has a
single cause, and it is now fixed in one place:

- `strip_isbd()` in the loader, one definition, used by all eight heading-cleaning
  sites across both paths.
- `record_normalization.strip_isbd_punctuation` (default `true`) and
  `isbd_punctuation` (default `" ./,;:"`), threaded from config through the CLI
  and the ingest pipeline into all four loader entry points.

```
config                             bibliographic     authority
strip_isbd_punctuation: true       'Waltari, Mika'   'Waltari, Mika'
strip_isbd_punctuation: false      'Waltari, Mika,'  'Waltari, Mika,'
isbd_punctuation: " "              'Waltari, Mika,'  'Waltari, Mika,'
```

#### A correction to 14.9, and to 14.13

14.9 said this change "alters agent names, and so blocking keys", and listed
stage 6 among the stages that change results. **That was wrong.**
`compute_author_key` and `compute_title_stem` already discard punctuation, so
both name forms have always produced identical keys:

```
author_key('Waltari, Mika,', 'Waltari,') == author_key('Waltari, Mika', 'Waltari') == 'waltari'
```

The discrepancy was in the *stored and exported* agent name, not in record
identity. Matching is unaffected, which is what makes turning stripping on safe
to do in one step rather than behind a migration. A test pins that the keys do
not move, so the claim is checked rather than asserted.

#### Caught by verification, not by tests

Threading the character set, a regex missed the two bibliographic agent sites —
`_get_subfield(f, "a")` contains a comma, so `[^,]+` did not match it. Those two
kept using the module default and silently ignored the configured set. The full
suite stayed green; only running the toggle through every combination showed it.
A half-wired setting is worse than no setting, so there is now a regression test
that a non-default character set reaches both paths.

### 14.7 Decisions taken

- The 42 funnel specs ship as **built-in defaults in code**; `config.example.yaml`
  shows a few as override examples. Config overrides or appends rather than
  replacing. Putting all 42 in the example file would make it enormous and imply
  more tuning freedom than is wanted.
- The fall-through funnel is **configurable, not hardcoded**. Built-in specs mark
  monograph `default: true` so a config-less run behaves as today, and
  `config.example.yaml` carries that entry explicitly so the knob is visible
  rather than implied.

### 14.9 Merging the two MARC decomposers

`decompose_marc_authority` (345 LOC) and `decompose_marc_wemi` (491 LOC) already
return the same ten-tuple shape — the authority one returns lists where the
bibliographic one returns singletons for W/E/M — and are selected by a single
`is_marc_authority_record()` test on Leader/06. They are two renderings of one
operation, and most of the 836 lines are the same extraction twice.

Merging them also resolves the discrepancy recorded at the end of Phase 12, whose
cause is now pinned exactly:

| path | MARC `$a` handling |
|---|---|
| `decompose_marc_authority` | `.rstrip(" ./,")` at both agent sites |
| `parse_marc_record_elem` (feeds `decompose_marc_wemi`) | taken raw |

So the authority side strips ISBD punctuation and the bibliographic side keeps
it, which is why the same person arrives as `"Waltari, Mika"` from one path and
`"Waltari, Mika,"` from the other.

**Make the stripping a setting rather than picking a winner.** It belongs in
`RecordNormalizationConfig` alongside the Phase 10 heading toggles, and it should
govern ISBD punctuation generally rather than the trailing comma alone — the
existing call strips `" ./,"`, and `.` and `/` are equally presentational.

Two things to be careful about:

- This is **not** a cosmetic change. `compute_author_key` and `compute_title_stem`
  derive from these strings, so whichever way the toggle defaults, one of the two
  current paths changes its blocking keys and therefore its matches. It needs the
  same before/after diff on a real corpus that the tier-1 fix in stage 2 needs.
- Stripping should be the shipped default. ISBD punctuation is presentation, and
  Phase 12 identified the bibliographic path as the odd one out; retaining it is
  the option that exists for callers who need the literal subfield back.

Sequenced last because it is independent of the `TargetRecord` removal and would
otherwise add a second source of behavioural change to the same diff.

### 14.10 Building and using the W/M vector indexes

The tier-2 dense branch is currently unreachable in production: `work_indexer=`
appears nowhere in `src/`, every `WEMIRepository(...)` the CLI builds receives
only `db_path` and `fts_config`, and there is a `build-agent-index` command with
no equivalent for the bibliographic tiers. Embedding configuration, engine
selection and the IPC daemon all exist to serve a path nothing reaches, and every
match silently falls through to FTS.

**Two indexes, not three.** Expression-level vector search is dropped — its four
E/M search APIs have zero callers and the tier adds no discrimination that the
work and manifestation tiers do not already provide. Manifestation embeddings are
kept because the clusterer needs them:

| index | purpose |
|---|---|
| work | tier-2 semantic fallback when no identifier or domain key resolves |
| manifestation | clustering — near-duplicate manifestations of one work |

So `search_expression_vectors` and `search_expressions_fts` go, while
`search_manifestation_vectors` and `search_manifestations_fts` stay and acquire
their first callers. This supersedes the stage-5 note that treated all four as
dead.

Work:

- A `build-index` command covering both tiers, mirroring `build-agent-index`.
- CLI repository construction passes the indexers, so the tier-2 branch is live.
- The clusterer consults the manifestation index instead of relying on blocking
  keys alone, which is what makes the vectors earn their cost.
- `rerank.top_k` is honoured. It is currently ignored: `top_k=5` is hardcoded at
  `matcher.py:547` and `:2603` while the config declares 50. Retrieval depth has
  to be configurable before over-fetching under `authoritative_only` (Phase 13.2)
  can be reasoned about at all.

Expect real result changes: a tier that never fired will start firing.

### 14.10.1 Stage 7 as built ✅ COMPLETE

The dense tier is reachable in production for the first time:

```
                                    match tier
no embedder (the old CLI behaviour) unmatched
embedder wired                      tier2_vector_fallback
```

Shipped:

- `WEMIRepository.build_wemi_indexes()` embeds stored works and manifestations
  from their `serialized_text`, which the loaders render at ingest, so building
  an index never re-parses a record. Rows with no serialization are skipped
  rather than embedded blank.
- `reconciliator build-index`, with `--tier work|manifestation|all`, mirroring
  `build-agent-index`.
- `_wemi_repo(cfg, embedder=...)` — one place where the CLI builds a repository.
  Passing an embedder wires the work and manifestation indexes. **The embedder is
  what supplies the dimension**: there is no configured value, so a command
  without one gets no indexes rather than a guessed dimension that would not
  match the vectors already stored.
- `reconcile` passes its embedder, so the tier is live.
- `run` gained a `build-index` step **between ingest and reconcile**. Without it
  the pipeline would reconcile against an empty index and silently reach only
  FTS — the same failure this stage exists to fix, one level up.
- The clusterer consults the manifestation index to union key groups the
  `standard::year::volume` key left apart, gated on
  `cluster.thresholds.manifestation` (0.92). Additive only: it joins groups,
  never splits them, and is a no-op with no embedder or an empty index.
- `rerank.top_k` is honoured. It was hardcoded to 5 at both dense call sites
  while the config declared 50, so configuring it did nothing — and depth is
  exactly what the `authoritative_only` over-fetch has to reason about, since a
  filter applied after retrieval can only ever return fewer candidates than were
  fetched.

**Three tiers, each with one consumer**, documented in `config.example.yaml`:
work for the matcher's semantic fallback, manifestation for the clusterer's
near-duplicate detection, agent for the standalone agent fallback. No expression
index.

#### Tests

532 passing (+12). `tests/test_vector_tiers.py` builds both tiers, builds one
tier only, skips blank serializations, and checks a hit resolves back to its row
through the mapping table. The tier-reachability tests use a record that tiers 1
and 2 provably cannot resolve, so whether it matches at all *is* the question of
whether the dense tier ran — and one of them pins the old `embedder=None`
behaviour so a regression to it is visible rather than silent. The clustering
tests cover the merge joining two catalogues' records of one edition, leaving
distinct editions apart, and being a no-op before `build-index` has run.

### 14.11 Deterministic agent identity

`decompose_marc_wemi` mints `f"urn:agent:{source}:{source_id}:{idx + 1}"`, so an
agent's URI depends on its ordinal position among the record's `700` fields.
Reordering those fields silently changes agent identity — the same instability
Phase 13 removed from records via `derive_content_id`, still present for agents.

The three decomposition paths disagree three ways: `:{idx + 1}` at
`marcxml.py:1732`, a hardcoded `:1` at `:1102`, and no suffix at `:1329`. The fix
is one scheme derived from the agent's own content (name, dates, authority
identifiers) rather than its position, applied in one place — which is a further
argument for doing this alongside the decomposer merge in 14.9.

### 14.12 Ingest accumulate or replace

Re-ingesting a source currently accumulates: there is no `delete_source`, no
`DELETE FROM works`, and no dedupe to catch the duplication. Now that a corpus is
just another source, re-harvesting an authority and re-ingesting a corpus are the
same operation, and both behaviours are wanted.

A per-source `ingest_mode: accumulate | replace` setting. `replace` clears the
rows carrying that `record_origin` before writing — works, expressions,
manifestations, items, their identifiers, `entity_keys` and contributions — while
leaving every other source untouched. Deterministic ids (Phase 13.1) are what
make `replace` safe to reason about: the same input yields the same URIs, so a
replace is a true refresh rather than a churn of identity.

Interacts with resume: `replace` has to clear on the first batch of a run, not on
every batch, or a resumed harvest deletes what the earlier slices just wrote.

### 14.11.1 Stage 8 as built ✅ COMPLETE

`derive_agent_id(name, dates, fuller_name)` replaces the position-derived
fallback at all three minting sites, which previously used three different
schemes: `:{idx + 1}` in `decompose_marc_wemi`, a hardcoded `:1` and no suffix at
all in `decompose_marc_authority`. An agent carrying a `$0` or `$1` keeps it — a
real authority identifier beats any derived one.

```
1. Reordering 700 fields
   original  {'Ahlqvist': '67d9b74848', 'Bergbom': 'de57ef7cfd', 'Kivi': '6930ebc5d3'}
   reversed  {'Ahlqvist': '67d9b74848', 'Bergbom': 'de57ef7cfd', 'Kivi': '6930ebc5d3'}
   identical across orderings: True
```

#### Deliberately not also deduplicating agents

The first version dropped `source_id` from the URI, which additionally collapsed
one person appearing in several records into a single agent row — 3 rows for 6
agent mentions. The `agents` table would accept that, since it upserts on
`agent_uri`, and it is arguably what the table is shaped for.

It was reverted, because the same collapse merges two *different* people who
share a name and carry no dates:

```
derive_agent_id('Virtanen, Matti') == derive_agent_id('Virtanen, Matti')
```

MARC 7XX fields frequently carry no `$d`, so that is not a hypothetical. Agent
deduplication is a separate decision from agent determinism, with a homonym
trade-off of its own; this stage fixes determinism only, and ids stay scoped to
their record. Dedup is available as a one-line change if wanted, and a test pins
the current scoping so the choice is visible rather than incidental.

### 14.12.1 Stage 9 as built ✅ COMPLETE

`ingest_mode: accumulate | replace` per source, defaulting to `accumulate` — the
historical behaviour.

`WEMIRepository.delete_source(name)` removes every row one source contributed and
returns per-table counts, so a caller reports what it removed rather than
claiming a clean slate it did not verify. Five tables carry `record_origin`;
everything else hangs off them by id, and the three polymorphic tables
(`identifiers`, `entity_keys`, `contributions`) carry no foreign key — their
`entity_type`/`entity_id` can name any tier, which SQLite cannot express as one
constraint — so their rows are deleted explicitly or they outlive their owners.

```
before        {'works': 3, 'agents': 6, 'identifiers': 3, 'entity_keys': 15, ...}
delete_source {'works': 2, 'agents': 4, 'identifiers': 2, 'entity_keys': 10, ...}
after         {'works': 1, 'agents': 2, 'identifiers': 1, 'entity_keys': 5, ...}

surviving origins    : ['fennica']
orphaned identifiers : 0
orphaned entity_keys : 0
```

**Clearing happens once, before the first batch, and only when the run is not
continuing an earlier one.** A resumed harvest that cleared would delete exactly
what its own completed slices had just written, and an incremental harvest would
throw away everything outside the new date window. The guard checks for a
resumption token, completed slices, or a rewritten `from_date`, and logs that it
skipped rather than clearing silently.

Re-ingesting after a replace returns the store to byte-identical counts, which is
what deterministic ids (13.1) buy: a replace is a refresh rather than a churn of
identity.

### 14.14 Follow-ups after the staged work ✅ COMPLETE

Two items were left open when the stages finished, each wanting its own diff.

#### The registry mismatch was failing closed in silence

With one database, "which rows may be matched against" is answered entirely by
`record_origin` plus the `sources` registry, and when the two disagree the
allow-list filters out every candidate — matching returns nothing and nothing
errors. This cost two rounds of debugging in stage 2.
`WEMIRepository.check_source_registry()` reports it, and `reconcile` prints the
findings before matching starts:

```
registered authoritative, no rows         ! cannot be matched against
rows stamped with a different origin      ! both findings (the stage-2 bug)
only a corpus registered                  ! no allow-list to apply
```

The first draft also reported unregistered origins as "invisible" when *nothing*
was authoritative — which is wrong, because `_origin_clause` applies no filter at
all when the allow-list would be empty, so those rows are visible. A test pins
that it does not cry wolf there, and another ties the diagnostic to the
behaviour: the mismatched row really is absent from an authoritative-only search
and present without the filter.

#### Funnel B was broken twice over, and dead besides

The tracklist tier compared the record's tracks against
`{w.title for w in find_works_by_type("music") if w.id != candidate.id}` — the
titles of every *other* music work in the store. That set is near-identical for
every candidate and measures nothing resembling tracklist agreement, and the
unbounded query ran inside the candidate loop, making the tier quadratic in the
size of the music collection.

It now compares a candidate against its **own** constituent tracks, and finds
candidates by shared track title through a new `analytic_title` key rather than
scanning. (The paired `analytic` key cannot serve that lookup: two catalogues
often disagree about a track's creator while agreeing on its title.) It reports
the overlap it found as the confidence rather than a fixed 0.85.

**And then the tier could not run at all.** Verifying the fix, a sound recording
dispatched to `funnel_d_notated_music`. `derive_domain_keys` emits a `music_work`
key for *every* music record, and funnel D's first `when` alternative was
`{work_type: [music], has_key: [music_work]}` — so it claimed every sound
recording and **`funnel_b_music_sound` was unreachable for any MARC record**. The
original if-chain had the identical defect, which stage 3 transcribed faithfully.
The discriminator is `content_type`, which the MARC leader determines
(`audio_music` vs `notated_music`), so `FunnelWhen` gained a `content_type`
predicate. The score-title alternative was also English-only; it now uses the
clusterer's Finnish vocabulary (`partituuri`, `nuotti`, `stemma`).

```
case 1 same tracks, different album title and publisher number
       funnel_b_music_sound  tier3_tracklist_jaccard  conf=1.00 -> urn:work:melinda:A1
case 2 3 of 6 titles shared (0.5, under the 0.75 threshold)     -> unmatched
case 3 tracks matching the other stored album                   -> urn:work:melinda:A2
case 4 tracks in no stored album                                -> unmatched
```

#### The test that should have caught it

`test_every_funnel_is_reachable_by_a_record_of_its_own` builds each probe from
the spec's own `when`, so it proves a spec claims its own probe — not that
records the loader actually produces reach it. That circularity is exactly what
hid the dead funnel, and it passed throughout. `tests/test_funnels.py` now also
dispatches **real loader output** through MARC leaders and fields: a sound
recording, a notated-music record, a Finnish score title, a monograph and a
component record. Those are the assertions that can catch this class of mistake.

564 passing.

### 14.15 Funnel reachability audit ✅ COMPLETE

Funnel B being dead raised the obvious question: how many others are? Auditing
the loader's actual capabilities against every funnel's `when` predicates gave
the answer — **11 of 41 funnels could not be reached by any MARC record.**

The method compares what the loader can emit (which work types
`determine_work_type` returns, which content types `determine_content_type`
returns, which keys `derive_domain_keys` adds, which identifier schemes
`parse_marc_record_elem` extracts) against what each funnel's alternatives
require. 13 of the 40 work types the predicates name are never produced.

#### Three gaps where the signal existed but was not wired

- **`translators_signature` was never emitted by anything.** The key type was
  declared on `MatchRecord`, mapped in its validator, read by the clusterer's
  pairwise scoring *and* by funnel HH's dispatch — and written nowhere, so both
  consumers read a permanently absent value. It is now derived from the same
  `trl` relator test the serialization engine already used two lines away.
- **A 111 meeting main entry did not classify the record.** 111 was parsed as a
  corporate agent, but `determine_work_type` never looked at it, so no MARC
  record ever became `conference` and funnel J was reachable only through the
  `exhibition` genre heading.
- **Funnel PP keyed only on a work type that is never produced**, while the
  loader already emits the `heritage_model` key from 347 digital file formats. It
  now accepts either.

#### Nine remain, and they are classification gaps rather than bugs

`funnel_dd_sammelband`, `funnel_ee_web_archive`, `funnel_g_anthology`,
`funnel_gg_broadcast`, `funnel_ii_preservation_surrogate`,
`funnel_jj_medieval_fragment`, `funnel_nn_geodata`, `funnel_t_legal`,
`funnel_z_tactile`.

Each needs the ingest path taught to recognise a signal it does not read yet — a
533 reproduction note, a bound-with relationship, a genre heading for statutes or
fragments. Those are bibliographic decisions about Finnish cataloguing practice,
not refactoring, so they are named rather than guessed at.

#### The audit is a test, and it fails in both directions

`tests/test_funnel_reachability.py` holds the nine in
`KNOWN_UNREACHABLE_FROM_MARC`, and asserts the computed set equals it exactly.
Growing means a change made a funnel unreachable — the regression that hid funnel
B for the life of the codebase. Shrinking means a gap was closed and the list is
stale. Capability extraction is AST-based rather than regex: the first hand audit
used a regex and silently missed a multi-line `_add()` call, reporting a funnel
as dead when it had just been fixed. The fixture also asserts each extraction
found *something*, since an extraction returning nothing would make every funnel
look unreachable and the assertions vacuous.

Verified by temporarily repointing funnel L at a work type the loader cannot
produce: the audit fails and names it.

572 passing.

### 14.16 The BibFrame path had the same defect, larger ✅ COMPLETE

Asking 14.15's question of the other ingest path: **only 5 of 42 funnels were
reachable from a BibFrame record.**

`decompose_bibframe_work` set `work_type` under exactly two graph conditions —
C-3 (hub) and C-5 (music) — and defaulted everything else to `"text"`. So a
BibFrame cartographic resource, thesis, dataset, still image or manuscript all
arrived as text and reached the monograph funnel. `derive_bibframe_keys` emits 5
key types against the MARC path's 14, which narrows it further.

Unlike the nine MARC gaps, this one is a lookup rather than a heuristic: the
BIBFRAME vocabulary names its Work subclasses explicitly, so there is nothing to
guess about Finnish cataloguing practice.

`determine_bibframe_work_type()` maps `rdf:type` to `WorkType`, falling back to
`bf:content` and `bf:genreForm` for a Work carrying only the generic class.
Matching is on local names, because profiles differ on whether they emit `bf:` or
the expanded LoC namespace. A condition that already decided is left alone.

```
bf class         work_type      funnel
Text             text           funnel_a_monograph
Cartography      cartography    funnel_l_cartography
Dataset          dataset        funnel_n_dataset
MovingImage      moving_image   funnel_f_moving_image
StillImage       still_image    funnel_s_still_image
Object           realia         funnel_q_realia
Manuscript       manuscript     funnel_r_archival
bf:content cartographic-image   funnel_l_cartography
```

**And the music split needed content types too.** With work types alone,
`bf:NotatedMusic` reached `funnel_b_music_sound` — the same defect 14.14 fixed on
the MARC side, for the same reason: both music kinds are work_type `"music"` and
only `content_type` separates them. `determine_bibframe_content_type()` supplies
it, so scores and recordings now split on both paths.

Reachable funnels from BibFrame: **5 → 17 of 42.** What remains is largely the
same nine MARC gaps plus types BIBFRAME does not model as distinct classes
(thesis, comic, patent, standard, board game, podcast, oral history and so on).
Those are expressible through `bf:genreForm`, but mapping Finnish genre
vocabulary there is the same bibliographic decision the MARC gaps need, so it is
left named rather than guessed.

The reachability audit now covers both paths, including a floor assertion so a
regression to "everything is text" fails rather than quietly narrowing dispatch
again, and a test that an unrecognised class still defaults to text rather than
failing closed.

583 passing.

### 14.17 Classification rules derived from the Finnish RDA guidelines ✅ COMPLETE

The eight remaining MARC reachability gaps needed citable cataloguing rules, not
guesses. Researched against the National Library's own documentation; **five gaps
remain.**

Sources used:

| what | where |
|---|---|
| RDA application guideline root | `wiki.helsinki.fi/.../RDA-kuvailu MARC 21 -formaatilla/` |
| 336/337/338/341/347, Finnish terms and codes | `.../RDA-sovellusohje/3XX Fyysisen kuvailun jne. kentät/` |
| 501 bound-with, 502 thesis, 533 reproduction | `.../RDA-sovellusohje/50X-535 Huomautuskentät/` |
| braille and tactile encoding | `.../Lisäohjeita/Saavutettavuus/` |
| online resources | `.../Aineistokohtaiset työohjeet/Verkkoaineistot/` |
| genre/form uses SLM, `$2 slm/fin` | `.../Sisällönkuvailuopas/01. Lajityyppimuoto/` |
| **857 electronic archive location** | `marc21.kansalliskirjasto.fi/bib/841-88X.htm` |
| RDA content type code list | `loc.gov/standards/valuelist/rdacontent.html` |

#### The research found existing bugs, not just gaps

**Four RDA content type codes pointed at the wrong concept and three did not
exist.** The 336 `$b` table had been written from memory:

| code | official | was mapped to |
|---|---|---|
| `tdi` | two-dimensional moving image | three-dimensional moving image |
| `tdm` | three-dimensional moving image | three-dimensional form |
| `tcf` | tactile three-dimensional form | tactile notated music |
| `tcm` | tactile notated music | tactile cartographic |

plus `dta`, `dcr`, `tcg`, which are not in the vocabulary, while `cod`
(computer dataset) was absent. `tdi` is the code essentially every film and video
record carries, so the transposition mislabelled the whole moving-image content
type. The table is now the complete 23-code list, each internal value annotated
with the RDA term where ours is coarser.

The codes reached me as a *summary* of the Finnish 3XX page, which is exactly
where a paraphrase can mislead, so they were confirmed against the LoC list
independently before anything was changed.

#### Rules implemented

- **Tactile** — `341 0# $a tactile $e braille $2 sapdv` is what the accessibility
  guidance prescribes for a *pistekirja*; the tactile 336 codes say it too.
  Checked before the leader, because a braille book has Leader/06 = "a" like any
  text. **A braille score is tactile, not music** (project decision; `336 $b tcm`
  asserts both).
- **Web archive** — `857`, "elektronisen arkiston sijainti ja käyttö", for
  material "saatavissa verkkoarkistossa tai digitaalisessa arkistossa". Its
  second indicator carries the distinction: `0`/blank means the linked copy *is*
  the described resource, `1`/`4` that it is an electronic version of a physical
  one.
- **Preservation surrogate** — `857` with second indicator `1`/`4`, or `533`
  *jäljennehuomautus*.
- **Bound-with** — `501` (and `014`). The guidance records 501 as a local field
  carrying `$5` with the holding library's ISIL code, so it appears in
  institutional rather than shared records; read anyway because Melinda is not
  the only source. **A holdings-level fallback is still needed** for sammelbands
  described only at item level.
- **336 classifies when the leader does not.** An RDA record must carry 336 and
  electronic resources routinely have a generic Leader/06, so a dataset coded
  `336 $b cod` was arriving as plain text.

Deliberately *not* implemented: **338 `cr` / 007 `cr` / 008-23 `o` do not mean
"web archive."** They mean online resource, which every e-book is. Keying the
web-archive funnel on them would have routed the entire electronic collection to
it. The Verkkoaineistot page has no concept of a harvested or archived resource
at all, and the Finnish web archive is a separate harvest index
(`verkkoarkisto.kansalliskirjasto.fi`) collected under *kulttuuriaineistolaki*
and reachable only at legal-deposit workstations - not catalogued record by
record. 857 is the link *from* a catalogued resource *into* it.

#### Still unreachable: 5 of 41

`funnel_g_anthology`, `funnel_gg_broadcast`, `funnel_jj_medieval_fragment`,
`funnel_nn_geodata`, `funnel_t_legal`.

All five plausibly hang off 655 genre/form headings, which the guidance says come
from **SLM** (Suomalainen lajityyppi- ja muotosanasto) with `$2 slm/fin`. The
existing classifier already matches SLM terms as substrings, so closing these
needs the actual SLM terms for antologiat, radio- and television programmes,
fragments, spatial data and statutes - checked against the vocabulary rather
than invented.

#### Questions for the National Library

Phrased for cataloguers, to be lifted as-is.

1. **Braille score.** A score in braille carries `336 $b tcm` (tactile notated
   music) and may have Leader/06 = "c". Should a discovery system treat it
   primarily as tactile material or as music? We have chosen tactile.
2. **Digital surrogate as a category.** We classify a record with `533`, or `857`
   with second indicator 1, as a preservation surrogate - but only when nothing
   more specific applies, so a digitised map stays a map. Is surrogate-ness
   better modelled as a property of the manifestation than as a kind of work?
3. **Bound-with in shared records.** 501 is documented as local, requiring `$5`.
   Is a sammelband relationship ever expressed in a shared Melinda record, or is
   it inherently holdings-level - in which case discovery has to read holdings.
4. **Web archive.** Is `857` the intended signal that a resource is held in the
   Finnish web archive, and is the second indicator reliably used to separate a
   born-digital archived resource from a digitised physical one?
5. **SLM terms.** Which SLM terms should identify anthologies, radio and
   television programmes, manuscript fragments, spatial data sets and statutes?
   These are the five material types our dispatch still cannot recognise.
6. **BFFI.** The Finnish BIBFRAME adaptation is not ready, so BibFrame
   classification is derived from BIBFRAME Work subclasses and `bf:content`
   (14.16). When BFFI settles, which of these MARC-derived rules should change?

600 passing.

### 14.18 SLM genre terms verified against Finto ✅ COMPLETE

Continuing 14.17. **MARC reachability is now 40 of 41 funnels.**

#### Genre terms verified against Finto, not guessed

The subject-analysis guidance names SLM (Suomalainen lajityyppi- ja muotosanasto)
as the 655 vocabulary (`$2 slm/fin`). Every term below was confirmed to exist
through the Finto REST API, with its URI, rather than inferred from a
plausible-looking Finnish word:

| work type | SLM terms | URI |
|---|---|---|
| `broadcast_program` | televisio-ohjelmat + narrower (televisionäytelmät, televisiosarjat, televisioelokuvat, televisio-oopperat) | s1209 |
| | radio-ohjelmat + narrower (kuunnelmat, radiosarjat, radiosovitukset) | s1416 |
| | shared narrower: dokumenttiohjelmat, lastenohjelmat, musiikkiohjelmat, luonto-ohjelmat | |
| `legal` | the whole *juridiset teokset* branch: lait, asetukset, säädöskokoelmat, direktiivit, valtiosopimukset, oikeustapauskokoelmat, peruskirjat | s1507 |
| `anthology` | antologiat (*"Useamman kuin yhden kirjailijan kaunokirjallisista teksteistä koostuva valikoima"*), kokoomateokset | s289, s963 |

**Matching is exact, not substring.** "dokumenttiohjelmat" (broadcast
programmes) and "dokumenttielokuvat" (documentary films) share a stem and are
different materials; substring matching across SLM would conflate them. A test
pins that the near-miss terms stay out.

`geodata_service` had no SLM term at all - spatial data is a content type, not a
genre - so funnel NN keys on RDA `crd` (cartographic dataset) instead, which is
distinct from `cri`, an ordinary map. It also had to move **ahead of** funnel L
in precedence, because a cartographic dataset is work_type "cartography" too and
cartography would otherwise claim it first.

#### The one remaining gap, and why it stays

`funnel_jj_medieval_fragment`. SLM's *katkelmat* (s1210) is the only candidate
term and it carries **no definition and no broader concept** in the vocabulary,
so it may well mean a published excerpt rather than a manuscript fragment. Using
it would be exactly the guess this work exists to avoid.

#### A hole in the audit, found by the audit passing

The reachability test passed after four funnels had just been made reachable -
which is wrong in the safe direction but still wrong. Capability extraction read
only literal `return "..."` statements, and the new rules classify through lookup
tables (`SLM_WORK_TYPES.get(...)`), so the audit under-reported what the loader
emits. It now reads the tables from the imported module, so adding an entry
cannot be missed. Second time a static-analysis shortcut in this audit has
misled: the first was a regex missing a multi-line `_add()`.

#### External AV datasets

KAVI and Yle moving-image sources, the Finna OAI-PMH route and the EN 15907
mapping are a workstream of their own: see **[kavi.md](kavi.md)**.

### 14.20 Closing sweep ✅ COMPLETE

A last pass for things the refactor left behind, rather than new work.

**Dead configuration.** `OAIIngestConfig.target_database` selected a staging
database that no longer exists; nothing in `src/` had read it since stage 2
dropped the staging tables. Two tests asserted it "routes to unified
WEMIRepository" while setting it to `"corpus"` and `"authority"` and then
checking the same `db_path` in both - they would have passed with the field
deleted, which is what made them worthless. Replaced by one test that asserts
what actually distinguishes a corpus from an authority now: both land in the
single store, and `authoritative` carries the difference.

**Lint to zero**, from 72 at the start of the session. All of the remainder was
residue of this phase's own changes: `corpus_db_path` and `vocab_db_path` locals
pointing at the database Phase 13 merged away, an unused `db_path`, and a
`URIRef` constant wedged between two import blocks so every later import counted
as out of place.

**Eleven more keys with readers and no writer.** `translators_signature` (14.18)
was not alone. These are read by the clusterer's blocking, the matcher's
identifier derivation, the XML enricher, or a funnel's `has_key` predicate, and
no loader emits any of them:

> `board_game`, `broadcast`, `ephemera_corp`, `exhibition_event`,
> `fragment_codex`, `numismatic_issue`, `oral_history`, `podcast`, `statute`,
> `swhid`, `web_seed`

They are *listed*, not deleted and not guessed at. Deleting would remove
capability that becomes live the moment each rule lands; guessing at eleven MARC
rules is the failure mode 14.17-14.18 exist to avoid. `KEYS_READ_BUT_NEVER_WRITTEN`
in `tests/test_funnel_reachability.py` pins the set and fails in both directions,
and a second test asserts that no funnel is reachable *only* through an unwritten
key.

**619 passing, lint clean.**

### 14.21 What is genuinely left

Everything below is blocked on a cataloguing decision, not on engineering.

| item | blocked on |
|---|---|
| `funnel_jj_medieval_fragment` reachability | whether SLM *katkelmat* (s1210) means a manuscript fragment or a published excerpt - no definition, no broader concept |
| the eleven keys above | one MARC rule each, from the RDA application guidelines |
| the six questions in 14.17 | the National Library |
| moving-image work identity | [kavi.md](kavi.md) - a separate workstream, and nationally blocked on the Teos MARC 21 project |

Two engineering items were considered and deliberately not done:

- **Agent deduplication** (14.11.1). Dropping `source_id` from the derived agent
  URI would collapse one person across records into one row, which the `agents`
  table is shaped for - but it also merges two different people who share a name
  and carry no dates, and MARC 7XX frequently has no `$d`. One line to change
  when wanted; a test pins the current scoping so the choice stays visible.
- **The decomposer merge** (14.9.1), declined on measurement: the two MARC
  decomposers share 4 of 36/12 tags and merging them concatenates rather than
  deduplicates.

### 14.13 Intended behaviour changes

Four stages change results, and each deserves its own before/after diff on a real
corpus rather than trust in the test suite:

| stage | change |
|---|---|
| 2 | tier-1 starts working, and agent reconciliation moves onto a match that knows the record's work type (14.2, 14.2.1) |
| ~~6~~ | ~~ISBD stripping alters blocking keys~~ — it does not; see 14.9.1 |
| 7 | the dense tier fires for the first time; `rerank.top_k` takes effect (14.10) |
| 8 | agent URIs change shape once, to become position-independent (14.11) |

Stages 1, 3, 4, 5 and 9 are intended to be output-identical. Sequencing them
apart is deliberate: one source of behavioural change per diff, so a regression
can be attributed.

## Test Results Summary

| Metric | Phase 3 | Phase 4 | Phase 6 | Phase 7 | Phase 8 | Phase 9 | Phase 10 | Phase 11 | Phase 12 | Phase 13 |
|--------|---------|---------|---------|---------|---------|---------|----------|----------|----------|----------|
| Passed | 374 | 396 | 420 | 433 | 429 | 433 | 442 | 450 | 469 | **469** |
| Skipped | 2 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| Failed | 15 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **0** |

Phase 8's count is lower because it deleted tests for deleted code, not because
coverage regressed: 7 `stream_oai_records` tests and 1 migration test removed,
4 `_resolve_harvest_window` tests added.

### Verified end to end

| Configuration | Result |
|---|---|
| Authority corpus + clustering | Global work/expression URIs where matched, `urn:reconciliator:` cluster URIs at tiers that did not match, KANTO agent URI injected into `$0` |
| No authority corpus + clustering | Internal `urn:reconciliator:` work and expression cluster URIs only |
| Authority corpus, clustering off | Global authority URIs only, no cluster URIs |

Tier 1 (exact identifier) and Tier 2 (domain key) both resolve; `priority_fallback`
correctly selects KANTO over VIAF per `agent_priority`.

### Known remaining issue (pre-existing, not from this migration)

`decompose_marc_wemi` keeps the MARC `$a` trailing comma on agent names
(`"Waltari, Mika,"`) while `decompose_marc_authority` strips it. Both sides of
reconciliation use the same path, so matching is unaffected, but the two ingest
paths disagree on agent name form.

Cause pinned and scheduled in Phase 14.9: authority applies `.rstrip(" ./,")` to
`$a`, `parse_marc_record_elem` does not. The fix is to merge the two decomposers
and make ISBD punctuation stripping configurable.
