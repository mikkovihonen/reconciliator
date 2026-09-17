# Plan: an abstract loader interface

## Why

`src/reconciliator/loaders/` holds three formats that are meant to produce the
same thing — WEMI rows fit for the datastore — through three unrelated function
signatures. Measured, they do not produce the same thing, and nothing says so.

| | `decompose_marc_wemi` | `decompose_marc_authority` | `decompose_bibframe_work` |
|---|---|---|---|
| parameters | 13 | 7 | 5 |
| returns W/E/M as | singletons | **lists** | singletons |
| `record_origin` | yes | yes | **no parameter** |
| `relator_mappings` | yes | no | no (own mechanism) |
| `vocab_store` (subject folding) | yes | no | **no** |
| `strip_isbd_punctuation` | yes | yes | **no** |
| `legacy_to_modern` | yes | no | **no** |
| `supported_subject_vocabularies` | yes | no | **no** |
| `key_sink` (entity keys) | yes | no | separate function |
| deterministic ids | `derive_content_id`, `derive_agent_id` | partial | **none** |
| key types emitted | 14 | 0 | 5 |
| work types assignable | 29 + SLM + RDA content | n/a | 16 |

So a record ingested as BibFrame is not origin-stamped at decomposition, gets no
subject-vocabulary folding, no ISBD normalisation, no legacy heading folding, and
no deterministic identity. Two records describing the same thing, arriving in the
two formats, land in the store normalised differently — and every one of those
gaps is silent.

### The severe one: BibFrame identity is not stable

Three parses of the *same* BibFrame input, where the Work and Instance are blank
nodes (common in real exports):

```
run 1: work_uri=n93338243e01e43288030d7d010615c7cb1
run 2: work_uri=n33889813c8ce492aa7776ad9d8569709b1
run 3: work_uri=n00b893c8b05c43ddbcc31b7d2412e0bbb1
identity stable across parses: False
```

rdflib mints a fresh label per parse, and the decomposer falls back to it. This is
exactly the defect Phase 13.1 removed from MARC with `derive_content_id`, still
live on the BibFrame path. Consequences: re-ingesting one file duplicates every
row, and `ingest_mode: replace` cannot help, because the ids differ each run.

### `bibra` is not a peer

`bibra.py` exposes `bibra_to_marcxml_elem` and feeds the MARC path. It is a
**format adapter**, not a loader, and the plan should keep that distinction rather
than force it into the same interface.

## The shape

### 1. One return type instead of three shapes

```python
@dataclass
class DecomposedRecord:
    works: list[WorkEntity]
    expressions: list[ExpressionEntity]
    manifestations: list[ManifestationEntity]
    items: list[ItemEntity]
    agents: list[AgentEntity]
    relationships: list[WorkRelationship]
    identifiers: list[IdentifierEntity]
    associations: list[ManifestationExpressionAssociation]
    mediums: list[MediumOfPerformance]
    contributions: list[ContributionEntity]
    entity_keys: list[EntityKey]
```

Always lists, so the singleton/list split disappears. This also retires two
awkward things: the ten-tuple that five call sites unpack positionally (one of
them counting from the end), and the `key_sink` out-parameter, which exists only
because adding an eleventh tuple element would have broken those call sites.

### 2. One options object instead of thirteen parameters

```python
@dataclass
class LoadContext:
    source: str
    record_origin: str
    id_source: str = "auto"
    include_items: bool = False
    serialization_engine: Optional[WEMISerializationEngine] = None
    # normalisation, uniform across formats
    strip_isbd_punctuation: bool = True
    isbd_punctuation: str = DEFAULT_ISBD_PUNCTUATION
    relator_mappings: Optional[dict[str, str]] = None
    vocab_store: Optional[Any] = None
    supported_subject_vocabularies: Optional[Iterable[str]] = None
    legacy_to_modern: Optional[dict[str, str]] = None
    profile: Optional[BIBFRAMESchemaProfile] = None
```

Every loader receives the whole context. A loader that cannot honour part of it
says so in its capabilities rather than omitting the parameter — which is how the
current gaps became invisible.

### 3. Capabilities, declared

This is the part the requirement turns on: *semantically similar outputs, or an
explicit statement that the output is not produced.*

```python
class LoaderCapabilities(BaseModel):
    formats: list[str]  # metadata prefixes / format names accepted
    work_types: set[str]  # classifications it can assign
    content_types: set[str]
    key_types: set[str]  # entity_key types it emits
    identifier_schemes: set[str]

    # normalisation guarantees, one flag per LoadContext concern
    deterministic_ids: bool
    stamps_record_origin: bool
    normalises_isbd: bool
    folds_subject_vocabularies: bool
    normalises_relators: bool

    # Everything the loader does NOT produce, each with the reason. A gap with no
    # entry here is a bug; a gap with an entry is a documented boundary.
    not_produced: dict[str, str]
```

So BibFrame would declare, today:

```python
not_produced = {
    "citation": "BIBFRAME has no host-item concept equivalent to MARC 773",
    "analytic": "constituent works are modelled as related Works, not 505/700 $t",
    "sammelband": "bound-with is a holdings-level relationship, absent from BIBFRAME",
    "deterministic_ids": "identity comes from graph node URIs; blank nodes are "
    "not stable across parses (see plan §Why)",
}
```

A reviewer can then see at a glance which differences are decisions and which are
oversights. Today they are indistinguishable.

### 4. The base class owns "normalized to suit the datastore"

```python
class RecordLoader(ABC):
    capabilities: ClassVar[LoaderCapabilities]

    @abstractmethod
    def accepts(self, item: Any) -> bool: ...

    @abstractmethod
    def _decompose(self, item: Any, ctx: LoadContext) -> DecomposedRecord: ...

    def load(self, item: Any, ctx: LoadContext) -> DecomposedRecord:
        record = self._decompose(item, ctx)
        return self._finalise(record, ctx)
```

`_finalise` is where the datastore contract is enforced, once, for every format:

- stamp `record_origin` on works, expressions, manifestations, items and agents —
  removing the current reliance on `insert_wemi_batch(record_origin=...)`
  overwriting whatever the loader guessed
- bind the transient `entity_uri` hints on identifiers, keys and contributions
- assert the invariants the repository depends on: every entity has a non-empty
  `source` and `source_id`; every identifier and key resolves to an owner;
  `manifestation_uri` is present
- under test, check declared capabilities against what was actually produced — a
  loader claiming `deterministic_ids` whose output contains an rdflib blank-node
  label fails loudly

### 5. Two MARC loaders, not one merged function

`MarcBibliographicLoader` and `MarcAuthorityLoader` become separate
implementations distinguished by `accepts()` (Leader/06 = `z`). This is what
14.9.1 in the WEMI plan declined to do by *merging* — the two read 4 shared tags
of 36 and 12, so one function with two disjoint bodies was concatenation. A shared
interface with separate implementations gets the uniformity without the
concatenation, and is the honest resolution of that item.

Registry replaces the dispatch chain in `_WEMIBatchAccumulator.add()`:

```python
LOADERS = [MarcAuthorityLoader(), MarcBibliographicLoader(), BibframeLoader()]
loader = next(l for l in LOADERS if l.accepts(item))
```

`BibraAdapter` stays outside the registry, implementing a narrower
`FormatAdapter` that yields MARCXML elements into the MARC loader.

## Staging

Each stage keeps the suite green.

| # | stage | risk |
|---|---|---|
| 1 | `DecomposedRecord`, `LoadContext`, `RecordLoader`, `LoaderCapabilities`; thin adapters wrapping the existing functions; no behaviour change | low |
| 2 | Move ingest and CLI dispatch onto the registry; delete the isinstance / `is_marc_authority_record` chain; update the five positional unpack sites | medium — touches ingest |
| 3 | `_finalise` takes over origin stamping and URI binding; declare all three capability sets **honestly**, gaps and all | medium |
| 4 | Point `tests/test_funnel_reachability.py` at declared capabilities instead of AST scanning | low, high value |
| 5 | Close declared gaps one at a time, each flipping one flag and deleting one `not_produced` entry | each its own diff |

### Stage 4 is worth more than it looks

The reachability audit currently reads the loader's lookup tables by parsing the
source. That has already failed twice: once to a regex that missed a multi-line
`_add()` call, and once to literal-only extraction that missed classification via
dictionary lookup — both times reporting a funnel unreachable when it was not, or
passing while four funnels had just become reachable. Declared capabilities remove
the inference: the audit compares funnel predicates against what loaders *say*
they emit, and `_finalise`'s test-mode check keeps the declaration honest.

### Suggested order for stage 5

1. **BibFrame deterministic ids** — the only correctness bug in the list; a
   content digest over the graph's stable triples, mirroring `derive_content_id`
2. **BibFrame `record_origin`** — removes reliance on the insert overwriting it
3. **Uniform ISBD normalisation** — the toggle exists and reaches only MARC
4. **Uniform subject folding and relator normalisation** — the largest, and the
   one most likely to change matching results, so last and on its own diff

## What this does not do

It does not unify the *extraction* logic, because the formats genuinely differ —
that was measured and declined in WEMI 14.9.1. It unifies the contract, the
return shape, the normalisation applied afterwards, and the statement of what each
format can and cannot yield.


---

# Implementation log

## Stages 1-4 ✅ COMPLETE

`loaders/base.py` — `DecomposedRecord`, `LoadContext`, `LoaderCapabilities`,
`RecordLoader`, `FormatAdapter`. `loaders/registry.py` — the three loaders and
the dispatch. Stage 1 was verified output-identical to the direct decomposers
before any call site moved.

Ingest now dispatches through `load_record()`; the isinstance/leader-test chain
in `_WEMIBatchAccumulator.add()` is gone, and every loader receives the full
`LoadContext` whether or not it honours all of it.

`tests/test_funnel_reachability.py` reads `LoaderCapabilities` instead of parsing
loader source — the inference that had twice returned the wrong answer.

### `check_output` earned its keep immediately

The first run reported that MARC emits `kanto` and `viaf` identifier schemes the
declaration omitted — agent `$0`/`$1` authority URIs become identifiers whose
scheme is whichever vocabulary `detect_vocabulary` recognises. The declaration now
reads that set from the vocabulary patterns rather than listing it by hand.

### The base class closed one gap on contact

BibFrame works went from `record_origin='unknown'` to the source name, because
`finalise` stamps every tier. Previously only `insert_wemi_batch(record_origin=)`
overwriting it afterwards made this right, so any caller inserting without that
argument stored the wrong origin — and origin plus the `sources` registry is what
decides which rows may be matched against.

## Stage 5.1: BibFrame identity ✅ COMPLETE

Fixed, and the root cause was not what the plan assumed.

`str(BNode())` returns a **bare** label — `"Nc5b5385e576141b8b9b3bbf7f40d717f"` —
with no `_:` prefix; only `.n3()` adds one. So **all eleven**
`startswith("_:")` guards in the decomposer were dead code that had never fired.
They were written to stop a blank node becoming a URI, and that branch never
executed, which is precisely how parse-local labels ended up as work, expression
and manifestation URIs.

- `is_blank_node()` tests the type instead, and the eleven guards now use it
- `derive_bibframe_node_id()` digests the node's own predicate/object pairs, with
  a blank-node object replaced by a digest of *its* pairs — one level of
  recursion, which BIBFRAME titles require, since
  `bf:title [ a bf:Title ; bf:mainTitle "X" ]` puts the title behind a blank node
- a derived id mints a URN on the MARC scheme rather than leaving a bare label

```
work : urn:work:bf:sha1:c8a6dc3fc3a51c54
expr : urn:exp:bf:sha1:c8a6dc3fc3a51c54#expr-1
manif: urn:manif:bf:sha1:fd96b1bc2bd8e09c
stable across 3 parses: True
different content -> different id: True
```

`BibframeLoader` now declares `deterministic_ids=True`, and the contract test that
asserted the gap failed on the flip — which is the mechanism working: a
declaration that changes has to be re-stated deliberately.

660 passing, lint clean, all three pipeline modes unchanged.

## Stage 5.2: BibFrame `record_origin` ✅ COMPLETE

Closed incidentally by `finalise`, as recorded above.

## Stage 5.3: uniform ISBD normalisation ✅ COMPLETE

The strip moved into `RecordLoader.finalise`, so it is a property of the
interface: a loader cannot forget it, and a new format gets it without writing
any code. `ISBD_TEXT_FIELDS` names the fields — titles, names, and the imprint
statements — and deliberately omits `*_normalized` (the normalisers already
discard punctuation) and the enumeration fields `volume_designation` and
`series_numbering`, which feed `check_volume_match` through `normalize_volume`.

Two findings while doing it:

- **The MARC path was itself inconsistent.** The authority decomposer stripped
  its headings and the bibliographic one did not strip the 245, so `w.title` came
  out "Testikirja" from one path and "Testikirja :" from the other — three test
  assertions had that asymmetry written down as expected behaviour. The strip now
  happens once, where 245 is read.
- **The target path passes through no loader at all.** `build_target_record_from_marc`
  builds a `MatchRecord` and no entities, so `finalise` never sees it; a work
  report printing the target title beside the matched corpus title showed
  "Testikirja :" against "Testikirja" for one and the same string. Stripping at
  the extraction point in `parse_marc_record_elem` keeps both sides comparable,
  and `finalise` remains the guarantee for any format that leaves something
  behind.

`check_output` takes the `ctx` now: `normalises_isbd` means the loader *honours*
`strip_isbd_punctuation`, not that it strips unconditionally.

## Stage 5.4: uniform relator normalisation ✅ COMPLETE

The profile resolves roles first — so a deployment can still override any of
them — and whatever it does not resolve now goes to the shared relator registry
instead of being returned as it stands.

That last step was the whole gap, and it was Finnish-shaped: the profile's
`role_label_mappings` cover the English labels, and BFFI — the Finnish BIBFRAME
profile — writes Finnish relator *terms*. Measured across a dozen terms, every
one of them disagreed:

| label | BibFrame, before | MARC |
|---|---|---|
| kääntäjä | kääntäjä | trl |
| kirjoittaja | kirjoittaja | aut |
| säveltäjä | säveltäjä | cmp |
| sanoittaja | sanoittaja | lyr |
| sovittaja | sovittaja | arr |

So one agent's `role_counts` merged across a Melinda source and a BFFI source
read `{"trl": 2, "kääntäjä": 3}`: the same role counted twice under two names.

Three further defects surfaced, each larger than the declared gap:

1. **A blank-node role was never read.** `bf:role [ rdfs:label "kääntäjä" ]` is
   the normal shape, and the decomposer passed `str(role_obj)` — rdflib's
   parse-local label — to the normaliser. The stored role was that label: not a
   role, and different on every parse. `role_label_predicates` is now a profile
   field, declared in all three shipped profiles.
2. **BibFrame agents had no `role_counts` at all**, though `enricher.py` reads
   `next(iter(agent.role_counts))` for the export role.
3. **MARC contributions had no real role.** `role = a.get("role") or "aut"` read
   a key nothing ever set, so every contribution row came out
   `aut`/`is_primary`/`work`: a translator was filed against the work as its
   author, and the expression tier held *no contributions whatsoever* — which is
   why the matcher's expression-level contributor lookup (matcher.py:361) never
   found a name to compare.

Plus one convention decision: a 1XX main entry with no `$e`/`$4` now counts as
`aut`, matching what the BibFrame path records from the primary contribution and
what the target path already assumes for a 245 `$c` name.

`check_output` enforces the claim as "the shared normaliser has nothing left to
change", which distinguishes "kääntäjä" from a genuinely unknown role, and
rejects a role that looks like a blank-node label.

## Stage 5.5: uniform subject folding ✅ COMPLETE

`loaders/subjects.py` is the folding logic in one place: subjects in as
already-extracted mappings (extraction is format-specific, folding is not),
`FoldedSubject` out, carrying heading, vocabulary, concept id and the
`vocab:concept` key the agent footprint counts. The MARC path was refactored onto
it and proven output-identical on every fixture record first
(`subject_parity.py`: PASS), then BibFrame was wired to the same call.

What the gap actually was:

- **The BibFrame decomposer read no `bf:subject` whatsoever.** The target path
  read subjects; the ingest path did not. A BIBFRAME work reached the datastore
  with no subjects, and its agents with an empty footprint. The extractor was
  private to `rdflib_loader`; it is now `extract_bibframe_subjects` in
  `decomposition.py` and both paths call it.
- **`works.subjects_json` held bare heading strings, which neither consumer
  could read.** `work_serialization.j2` renders
  `subjects | map(attribute='pref_label')` and so rendered *nothing* — the work's
  serialised text, which BM25 and the dense index score, carried no subject terms
  at all — while `find_classification_evidence` looks for `code`/`scheme` per
  entry and failed silently inside its own try/except. The stored shape is now
  the folded dict, and both consumers work.
- **A legacy vocabulary could not be recognised from a URI.** The URI pattern
  table lists only the modern vocabularies, because a MARC record declares ysa,
  allars, musa or kaunokki in `$2`. A BIBFRAME subject is a URI and nothing else,
  so a legacy heading arrived with no vocabulary and was dropped from the
  footprint. `vocabulary_hint_from_uri` reads the vocabulary out of the Finto URI
  path, which lets `legacy_to_modern` do its job on either format.

A 4XX variant title on a work authority still rides in `subjects_json` — there is
no variant-title column and that list is what the serialisation renders — but it
is tagged `kind: variant_title` so it cannot be mistaken for a subject.

MARC and BIBFRAME now produce identical concept sets, identical agent footprints
and identical subject terms in the serialised text for equivalent records.

## Stage 5.6: `translators_signature` from BIBFRAME ✅ COMPLETE

Declared impossible here because roles were not normalised, so no BibFrame agent
ever carried "trl". They do now, so the key that funnel HH dispatches on and the
clusterer scores with is derived from BIBFRAME by the same computation as MARC.

## Where stage 5 ended

Every declared *normalisation* gap is closed. What is left in `not_produced` is
structural, and each entry says why:

| loader | remaining | why |
|---|---|---|
| marc_bibliographic | — | none |
| marc_authority | entity_keys, subject folding, relators | an authority record has no manifestation to key, carries its own headings rather than subject access points, and no relator codes |
| bibframe | citation, analytic, sammelband | no host-item concept, constituent works modelled as related Works, bound-with is holdings-level |

660 → **680 passing**, lint clean, and all three linking modes unchanged through
every step: TARGET-001 at `tier1_exact_identifier`, TARGET-002 at
`tier2_domain_key` (0.8067), one work cluster, one expression cluster, two
manifestation clusters.
