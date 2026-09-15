# Plan: aggregates — constituent works, ordered tracklists, component manifestations

## Why

The schema was built for containment and nothing fills it. Measured by ingesting
one album record and one anthology record through `load_record`:

| | in the schema | written by any loader |
|---|---|---|
| `manifestations.container_id` (self-FK, `ON DELETE CASCADE`) | yes | **never** |
| `manifestations.is_component` | yes | only from 773, i.e. the *component's* view |
| `manifestations.component_type` (`track`, `article`, `chapter`, `episode`, …) | yes | **never** |
| `manifestation_expressions.track_number` | yes, indexed | **never** |
| `manifestation_expressions.disc_number` | yes | **never** |
| `manifestation_expressions.duration` | yes | **never** |
| `work_type: 'anthology'` | yes | yes, from SLM *antologiat* |
| `entity_keys: analytic`, `analytic_title` | yes | yes |

```
===== ALBUM (Rautavaara: 3 × 505 $t, 1 × 700 $t, 1 × 028)
  works 1  expressions 1  manifestations 1
      is_component=False component_type=None container_id=None
  M-E associations 1 -> [(track=None, disc=1, duration=None, primary=True)]
  key analytic_title   ['Cantus arcticus', 'Angel of light', 'Isle of bliss']

===== ANTHOLOGY (2 × 700 $t)
  works 1 ('anthology')  expressions 1  manifestations 1
  key analytic_title   ['Kuun maisema', 'Agnes']
```

So an aggregate is stored as a *bag of constituent title strings*. That is enough
for the matching tiers that exist — recorded sound's tier 3 does tracklist
Jaccard over `analytic_title`, and funnel G resolves anthology constituents by
author and title — and not enough to answer "what does this album contain" or
"which releases carry this track" from the datastore. A tracklist is also
unordered in that form: track 1 and track 9 are indistinguishable.

The read side is already wired.
`RecordRepository.get_expressions_for_manifestation` orders by
`disc_number, track_number` and has a caller in `matcher.py`. It has simply never
been given more than one row.

## The model: two shapes, not one

The schema carries two mechanisms because there are two cases, and conflating
them is what makes aggregate modelling go wrong.

**Shape A — embodied constituent.** An album track, an anthology story, a song in
a songbook. The constituent is a Work realised as an Expression, *embodied in the
container's manifestation*: it has no carrier, no publisher and no extent of its
own. So it gets a Work row and an Expression row, joined to the container's
existing manifestation through `manifestation_expressions` with
`track_number`, `disc_number` and `duration`. **No component manifestation row.**

**Shape B — component manifestation.** A journal article, a chapter with its own
pagination, one physical part of a multi-part set. The constituent has its own
extent within the container, so it gets its own Work, Expression *and*
Manifestation, the last carrying `container_id`, `is_component=True`,
`component_type` and its own `pagination`.

Which shape a constituent takes is **per-source configuration**, not a property
of the format — see the next section. For orientation, this is how the sources in
hand express containment today:

| source field | usual shape | today |
|---|---|---|
| MARC 505 `$t` (enhanced contents) | A | `analytic_title` key only |
| MARC 700/710 `$t` (analytic added entry) | A | `analytic` key, **plus a spurious contributor — see below** |
| MARC 773 (host item entry, on the component's own record) | B | `is_component=True`, container never linked |
| MARC 774 (constituent unit entry) | B | not read at all |
| BIBFRAME `bf:hasPart` on a Work | A | **mis-mapped to `has_series_member`** |

Shape B for 773 is the half-built path: the component knows its host through the
`citation` key, and `container_id` is never resolved, so the container does not
know its parts and the hierarchy cannot be walked in either direction from SQL.

## Extraction is per-source configuration

Two sources in the same format do not agree about constituents. Melinda writes
enhanced 505s; a sound-recording database keeps tracks in its own conventions
with durations; KAVI's EN 15907 has episodes of a series; Yle ships JSON; a BFFI
export uses `bf:hasPart`. Plenty of sources have no aggregates at all, and for
those, guessing is worse than doing nothing. So the fields to read, the shape
they produce and the ordering they carry are **configuration owned by the
source**, and the code is one profile-driven extractor rather than a chain of
per-source special cases.

This is the mechanism `schema_profile` and `normalization` already establish on
`OAIIngestConfig`: a global default, a bundled profile by name, and a per-source
override.

```yaml
ingest_oai:
  melinda:
    format: marcxml
    constituents: marc_enhanced        # bundled name, or a path to a YAML profile
    constituents_mode: entities        # off | keys_only | entities
  fono:
    format: marcxml
    constituents: profiles/fono.yaml   # this source's own conventions
  kavi:
    format: en15907
    constituents: off                  # first-class: silence must not mean "guess"
```

Bundled profiles live in `src/reconciliator/defaults/constituent_profiles/*.yaml`,
beside `defaults/bibframe_profiles/`, and resolve through
`get_constituent_profile(name_or_path)` with the same three steps as
`get_profile`: an existing file path wins, then an alias map, then a bundled
name, then an error that lists what is available.

A profile is declarative:

```yaml
name: marc_enhanced
shape: embodied                    # default shape for entries below
dedup: normalized_title            # none | normalized_title | title_and_creator
max_per_record: 100

fields:                            # in precedence order; richer entries win dedup
  - tag: "505"
    indicators: {ind1: "0", ind2: "0"}    # enhanced contents only
    title: "t"
    creator: "r"
    duration: "g"
    ordinal: position                     # position | subfield:<code> | none
  - tag: "700"
    indicators: {ind2: "2"}               # analytic added entry
    title: "t"
    creator: "a"
    duration: "g"
    contributes_to_container: false       # the agent is the constituent's, not the album's
  - tag: "710"
    indicators: {ind2: "2"}
    title: "t"
    creator: "a"
  - tag: "773"                            # this record is itself a part
    shape: component
    component_type: article
    container_key: citation               # resolve the container through this key

extractor: null                    # built-in name or path to .py, for the rest
```

For RDF sources the same profile shape carries predicates instead of tags
(`predicate: "bf:hasPart"`), so one mechanism covers MARC, BIBFRAME and anything
added later; the BIBFRAME *schema* profile keeps owning namespace and class
questions.

**The escape hatch is the funnel rule, verbatim.** `extractor` is either a
built-in name or a path to a Python file:

```yaml
extractor: fono_tracks                            # built-in, by name
extractor: src/reconciliator/constituents/fono.py # path, config-relative
extractor: /opt/site/constituents/kavi.py         # path, absolute
```

A value ending in `.py` or containing a path separator is a path; anything else
is a built-in name. Extractors resolve when configuration loads, so a bad path or
an unknown name is a startup error naming the offending *source* rather than a
source that silently ingests no constituents. This is `funnels.resolve_handler`
with a different registry, and it should share the resolution code rather than
copy it — the two differ only in the error message and the built-in table.

Three properties worth stating, because they are what makes this configuration
rather than a switch statement:

- **A profile describes one source's convention, not a format.** Two MARC sources
  can and will disagree; that is the point.
- **Shape is per field entry**, defaulting to the profile. One source can yield
  embodied constituents from its 505s *and* component manifestations from its
  773-bearing records.
- **`off` is first-class**, and is the default for any source that names no
  profile, so adding this feature changes nothing for a source nobody has
  described yet.

The loader's `LoaderCapabilities` should declare which profile it ran under and
whether constituents were produced, so `check_output` can verify that a profile
promising ordinals actually yielded ordered rows — the same declared-and-checked
contract the loader interface settled on.

## Identity

`works` is `UNIQUE(work_uri)` — not `(source, source_id)` — and the upsert
conflicts on `work_uri`. That gives convergence for free if the constituent's URI
is content-derived:

```
work_uri : urn:work:{source}:c:{sha1(author_key|title_stem|work_type)[:16]}
source_id: c:{same digest}          # not a harvested record id; documented as such
expr_uri : urn:exp:{source}:c:{digest}:{lang}
```

Two albums in the same source that share a track then converge on **one**
constituent work row, which is the entire point: it is one work. Cross-source
identity stays consolidation's job, as everywhere else — `WorkConsolidator` and
the `same_work_as` edges.

The expression must carry the same `(source, source_id)` pair as its work, because
`insert_wemi_batch` binds an expression to its work through
`work_key_to_id[(source, source_id)]`. That mechanism already exists and
generalises to N works per record without change.

Component manifestations (shape B) key off the container:
`source_id = {container_source_id}:p{n}`, `manifestation_uri =
urn:manif:{source}:{container_source_id}:p{n}`.

**Accepted risk.** A content-derived `work_uri` means two genuinely different
works with the same author key, title stem and work type merge into one row. This
is the same collision `compute_work_blocking_key` already tolerates for blocking,
but here it merges stored rows rather than grouping candidates. Including
`work_type` in the digest narrows it; including the container would defeat
convergence. A test should document the residual case rather than pretend it away.

## Blockers in the repository — stage 1, before any loader changes

Four of these make the feature impossible today, and all four are invisible until
a record produces more than one of something.

1. **Every M-E association binds to `manifestations[0]` / `expressions[0]`**
   (`insert_wemi_batch` step 5). `ManifestationExpressionAssociation` has no URI
   fields, unlike `IdentifierEntity` and `ContributionEntity`, which bind by
   `entity_uri`. Worse, the table's primary key is
   `(manifestation_id, expression_id)` and the insert is `INSERT OR REPLACE`, so
   twenty ordered associations would collapse into **one row**. Needs
   `manifestation_uri` / `expression_uri` on the model and URI binding in step 5.
2. **`container_id` is passed through but never resolved.** Needs a
   `container_uri` field bound through `manif_key_to_id`, which means
   manifestations must be inserted container-before-component within a batch.
3. **Relationships bind `source_work_id` to `works[0]`** (step 8). With N works
   per record, constituent edges need `source_work_uri` / `target_work_uri`
   binding.
4. **`work_relationships` has no unique constraint** and
   `insert_work_relationships_batch` is a plain `INSERT`, so every re-harvest
   duplicates every edge. With one edge per record today that is untidy; with
   twenty constituent edges per album it is a leak. Needs
   `UNIQUE(source_work_id, target_work_id, relationship_type)` and
   `ON CONFLICT DO NOTHING`.

## The relationship pair

Add to `WorkRelationship.relationship_type`:

```
has_constituent   # container -> part
constituent_of    # part -> container
```

Naming follows the existing `series_member_of` / `has_series_member` pair. The
enum has nothing for part/whole today: `series_member_of` is serial hierarchy and
`bound_with` is item-level sammelband. `funnels/engine.py` already invents
`relationship_type="constituent_of"` for its output-only `ConstituentWorkMatch`,
so the string is chosen; it simply cannot be stored.

**Fix the BIBFRAME mis-mapping at the same time.** All three shipped profiles map
`bf:hasPart` → `has_series_member` and `bf:partOf` → `series_member_of`. On a
`bf:Work` those predicates are the constituent relation, not series membership,
so BIBFRAME aggregates are currently recorded as series members. The mapping has
to become context-dependent — a `bf:partOf` whose object is a `bf:Series` is
series membership, otherwise it is containment — which means a profile field per
sense rather than one global predicate map.

## Extraction and assembly

The shape the loader contract settled on: **configuration decides what to read,
shared code decides what it becomes.**

- One profile-driven extractor per format family produces an ordered
  `Constituent`: `title`, `creator`, `ordinal`, `disc`, `duration`,
  `pagination`, `shape`, `component_type`, `source_field`,
  `contributes_to_container`. `extract_marc_analytics` becomes its MARC
  implementation; it currently returns `(title, creator)` and discards 505 `$g`
  (duration), 505 `$r` (responsibility) and 700 `$g`, and it reads *any* 505
  rather than the indicators a profile names.
- **Dedup is a profile setting.** The album above emitted `analytic` for
  "Cantus arcticus" twice — once from the 700 `$t` with a creator, once from the
  505 `$t` without one. `dedup: normalized_title` keeps the richer entry; a
  source that legitimately repeats a title can say `none`.
- New `loaders/aggregates.py`, beside `loaders/subjects.py`: a `Constituent` list
  in, and `(works, expressions, associations, relationships, entity_keys)` out,
  for both shapes. Every loader and every custom extractor funnels through it, so
  no source can invent its own idea of what a constituent becomes — which is the
  same division that stopped the three formats normalising differently.
- Keep emitting `analytic` and `analytic_title` keys. The tracklist tier's key
  lookup is cheap and works; the entity rows are additive, not a replacement.

## What this exposes, and must fix

**700 `$t` name-title entries are harvested as contributors of the container.**
The anthology yields:

```
contribution entity_type=work role=aut primary=True agent=Waltari, Mika
contribution entity_type=work role=aut primary=True agent=Canth, Minna
```

Waltari and Canth are recorded as primary authors of *"Suomalaisia novelleja"*.
They authored the constituent stories. A 7XX carrying `$t` is a name-title
analytic entry, and its agent is the constituent's creator — so once constituents
exist as entities, that agent belongs to the constituent work, not the container.
This is the largest behaviour change in the plan: it removes contributors from
aggregate works and changes their author keys, which moves matching results. It
needs its own diff and a before/after on the fixtures.

It is also the one place where a source could reasonably disagree, which is why
`contributes_to_container` is a per-field profile setting rather than a rule. A
catalogue that lists an anthology's authors in 700 `$t` *and* means them as the
volume's contributors can say so; Melinda practice, as far as the fixtures show,
does not.

**`_constituents` mode `"resolve"` hardcodes `work_type="text"`**
(`funnels/engine.py`), so album tracks could never resolve even if their works
were stored — and recorded sound uses `constituents="titles"`, so it never tries.
Both should read the stored `has_constituent` rows instead of doing a `LIKE`
lookup per title.

## Matching and indexing consequences

- **Tracklist tier** keeps its Jaccard over `analytic_title` for candidate
  generation, but an accepted candidate can now report ordered alignment. A
  sequence-aware score is a later, separate question.
- **Vector index.** `build_wemi_indexes` indexes every work. An album with 20
  tracks multiplies the work index, and a track work is a plausible *and*
  misleading neighbour for an album-level query. Decide deliberately: index
  constituents for track-level recall but exclude them from work-level candidate
  pools, or gate them behind a flag. Measure before choosing.
- **Clustering.** Constituent works clustering with each other across sources is
  desirable — the same track in two catalogues is one work — but `max_cluster_size
  = 500` and message passing were sized for record-level works. A popular song
  could exceed that.

## Config

Two levels, matching `normalization`: a global default and a per-source override.

```yaml
constituents:
  profile: null            # the empty profile: look for nothing
  mode: keys_only          # off | keys_only | entities

ingest_oai:
  melinda:
    constituents: marc_enhanced
    constituents_mode: entities
  fono:
    constituents: profiles/fono.yaml
  kavi:
    constituents: off
```

`keys_only` is today's behaviour, kept so the change can be bisected per source.
`max_per_record` lives in the profile rather than here, because the reason for a
cap is a source's own practice: a real 505 can list a 500-track box set, and one
malformed record should not be able to write 500 works.

## Stages

Each is its own diff with its own verification, in this order:

1. ✅ **Repository binding and the relationship pair.** *Done — see the log
   below.*
2. ✅ **The profile mechanism.** *Done — see the log below.*
3. ✅ **Profile-driven MARC extraction.** *Done — see the log below.*
4. ✅ **Shape A emission.** *Done — see the log below.*
5. ✅ **The contributor fix.** *Done — see the log below.*
6. ✅ **Shape B: component manifestations.** *Done — see the log below.*
7. ✅ **BIBFRAME `bf:hasPart`.** *Done — see the log below.*
8. ✅ **Rewire the consumers.** *Done — see the log below.*
9. ✅ **Indexing and clustering, measured.** *Done — see the log below.*

## Open questions

Making extraction configuration demotes most of these: they now decide *what a
profile says* for one source, not what the code does, so none of them blocks
stages 2-4 and a wrong answer is a config edit rather than a rewrite.

**Per source, answerable from records** — each one fills in a profile:

1. **Melinda's 505 practice.** Enhanced (`ind2=0`, one `$t` per constituent) or
   basic (a single `$a` blob with " -- " separators)? If both occur, the profile
   filters on indicators and the basic form yields nothing rather than a bad
   split — which is why the indicator filter is in the profile and not optional.
2. **Is 774 used at all** in Melinda or Fono? If not, no profile names it.
3. **Durations**: does Fono or Yle give track durations, and in which subfield?
4. **Does a 700 `$t` always carry `ind2=2`** in Finnish practice? This sets the
   indicator filter and `contributes_to_container` for stage 5.

**Genuinely open design questions**, which configuration does not answer:

5. **Should a constituent work inherit the container's subjects?** It changes
   agent footprints, since the subject histogram is stamped per record. Arguably
   a profile setting too, but the default matters more than the switch.
6. **Do anthology constituents belong in the target-side `MatchRecord`** as
   keys, or only in the store?
7. **Does a component manifestation belong to the container's work, or its own?**
   `manifestations.work_id` is nullable, so both are expressible; an article's
   manifestation pointing at the article's work is the FRBR-correct answer and
   the one that makes `get_manifestations_for_work` useless for the container.
8. **Should a constituent work carry its inherited creator as a contribution?**
   The container's main entry is inherited for *identity* but not recorded, so
   cross-source consolidation of constituents has only the title to compare and
   falls just under the metadata threshold. Recording it would let the same track
   from two catalogues consolidate — and would also give constituent works a
   creator in reports.

9. **Should an aggregate's `author_key` come from its first analytic entry?**
   Today an anthology's key is its first story author's surname, so two
   catalogues listing the same stories in a different order derive different
   keys and never block together. Changing it needs the target path to carry a
   constituent profile, because both sides share one derivation — see stage 5.

---

# Implementation log

## Stage 1 ✅ COMPLETE

`ManifestationExpressionAssociation` gained `manifestation_uri` /
`expression_uri`, `WorkRelationship` gained `source_work_uri` /
`target_work_uri`, and `ManifestationEntity` gained `container_uri` — the three
ways a row names another row in the same batch, before either has an id.
`insert_wemi_batch` binds by those first and keeps the positional fallbacks,
which are still load-bearing: the loaders emit an association with no URIs on it
until stage 3.

Manifestations now insert in two passes when the batch holds a component —
containers first, so a `container_uri` resolves — and only then, so the common
one-manifestation case pays nothing. The vector argument is sliced alongside the
rows, because it is positional against the manifestation list and slicing one
without the other would attach the wrong embedding.

`work_relationships` gained `UNIQUE(source_work_id, target_work_id,
relationship_type)` and the insert an `ON CONFLICT … DO UPDATE`, so re-harvesting
a record no longer appends another copy of each of its edges.

Unbindable rows are dropped with a warning rather than stored as a join of
nothing (the junction) or failing the whole batch on a foreign key (an edge). A
component whose container is absent is stored *unlinked* instead — it is still a
real publication, and dropping it would lose the only record that the source
described it.

Verified on a hand-built album — 1 container + 20 constituents + 1 shape-B
component:

```
expressions on the album manifestation : 21  (want 21)
junction rows                          : 21
  first three : [('ALBUM-1', None, 1, None), ('c:track01', 1, 1, '4:01'), ('c:track02', 2, 1, '5:02')]
  last two    : [('c:track19', 19, 2), ('c:track20', 20, 2)]
  ordered     : True discs=[1, 2]
has_constituent edges from the album   : 20  (want 20)
  distinct targets                     : 20
component container_id                 : 1 (album id 1) is_component=True type=part
after a second identical ingest        : junction=21 edges=20 works=21 manifs=2
```

`tests/test_aggregate_batch.py` holds twelve tests covering the same ground plus
batch order independence, a missing container, an unbindable association, the
no-URI single-expression path, and `delete_source` cascading the whole aggregate
(`PRAGMA foreign_keys` is on, so the junction and the container hierarchy are
real cascades). 680 → **692 passing**, lint clean, all three linking modes
unchanged.

### One caveat, by decision rather than oversight

`CREATE TABLE IF NOT EXISTS` cannot add a constraint to a table that already
exists, so a database created before this stage has no
`UNIQUE(source_work_id, target_work_id, relationship_type)` and will go on
duplicating edges. There is no migration code, on purpose — see
`plans/wemi_data_structure.md` and the standing decision that nothing is in
production. Delete `data/authority.db` (or `reconciliator reset`) and re-ingest;
the local dev database in this checkout is one of the affected ones.

## Stage 2 ✅ COMPLETE

`constituents/spec.py` holds `ConstituentField` and `ConstituentProfile`;
`constituents/__init__.py` holds the resolution: `get_constituent_profile`
(bundled name → alias → YAML path, in `get_profile`'s three steps),
`resolve_extractor`, and `resolve_for_source`, which folds a source's
configuration and the global default into one `SourceConstituents` answer.

Three profiles ship in `defaults/constituent_profiles/`, beside the BIBFRAME
schema profiles:

| profile | what it describes |
|---|---|
| `none` | nothing — the default, so an undescribed source behaves exactly as before. Reachable as `off`, `disabled`, `no`, `marc_none` |
| `marc_enhanced` | Melinda MARC: enhanced 505 (`ind1=0 ind2=0`), 700/710 `$t` with `ind2=2`, and 773 as a component |
| `bibframe_haspart` | `bf:hasPart`, unordered |

Config is two levels, matching `normalization`: a global `constituents` block
and `constituents` / `constituents_mode` per source on `OAIIngestConfig`.

### The reference rule is now shared, not copied

A funnel's `handler` and a profile's `extractor` mean the same thing — a
built-in name or a path to a Python file — so the rule moved into
`module_ref.resolve_module_ref` and `funnels.resolve_handler` became a
four-line delegation. All 31 funnel tests pass untouched, error messages
included.

### Two things this turned up

**`config_dir` was read and never set.** `WorkMatcher` has passed
`getattr(config, "config_dir", None)` to the funnel resolver since handlers
gained paths, and nothing ever assigned it — so a relative `handler:` path
resolved against the process working directory, not the config file's
directory, contrary to what `funnels/__init__.py` documents. `load_config` now
stamps it on `AppConfig` and on `ReconcileConfig`.

**Indicator filters are load-bearing, not decoration.** A basic 505 (`ind2`
blank) holds one `$a` blob — `"Cantus arcticus -- Angel of light -- Isle of
bliss"` — and `extract_marc_analytics` reads *any* 505 today. Filtering on
indicators is what lets a profile decline to guess: the enhanced form yields
constituents and the basic form yields none, which is why open question 1 stopped
being a blocker.

### Verified

```
--- BAD: exit 1
    ConstituentProfileError: source 'fono': unknown constituent profile
    'tracks_by_vibes'. Available: bibframe_haspart, marc_enhanced, none,
    or a path to a YAML profile.
--- GOOD: exit 0
    loaded: {'melinda': ('marc_enhanced', 'entities'), 'kavi': ('off', None)}
```

41 tests in `tests/test_constituent_profiles.py` — every bundled profile, every
spelling of "nothing", aliases, a source's own YAML profile, a relative path
against the config directory, both extractor forms, and the four validation
refusals (a field with no title, with neither tag nor predicate, with both, and
an unknown ordinal). 692 → **733 passing**, lint clean, pipeline unchanged.

## Stage 3 ✅ COMPLETE

`constituents/record.py` holds the extracted `Constituent` and `finalise`, which
deduplicates, caps and numbers — the same whatever the record was written in, so
no extractor gets its own opinion about it. `loaders/marcxml.py` holds
`extract_marc_constituents(record_elem, profile)`, because format-specific
reading stays with the format.

`extract_marc_analytics` is now a two-line call on the new **`marc_legacy`**
profile: every 505 and every 700/710 `$t`, unfiltered and undeduplicated, field
order 700 → 710 → 505. That is a faithful description of an imprecise
convention, and it means the `analytic` / `analytic_title` keys a source has
always derived stay byte-identical. Proven against the old implementation
inlined from git, on three constructed records and every fixture record:
`legacy analytic parity: PASS`.

What `marc_enhanced` reads from the same album, which the old reading could not:

```
#1 'Cantus arcticus'  creator='Rautavaara, Einojuhani'  dur='18:20'  tag=505
#2 'Angel of light'   creator=None                      dur='31:05'  tag=505
#3 'Isle of bliss'    creator=None                      dur=None     tag=505
```

Three constituents rather than four, in order, each with its own duration, and
the composer in its authority-controlled form.

### Three things the implementation decided

**Subfield grouping, not `findall`.** An enhanced 505 repeats its subfields in
groups — `$t` `$r` `$g`, again and again — so a track's duration is the `$g`
that follows *its* `$t`. Reading each code across the datafield gave every track
the first track's duration and the first track's composer. The first attempt
then over-corrected: falling back to the datafield's first occurrence of a code
reintroduced exactly that leak, so the fallback is now limited to the subfields
*before the first title*, which is what a 700 needs (`$a` precedes `$t`) and
nothing more.

**Merging is per attribute, by profile order.** One constituent described twice
is two descriptions of it, not two candidates: the note gives the duration and
the added entry gives the composer. Each attribute comes from the
higher-precedence entry that states one, precedence being the profile's field
order — so it is the source's decision, not the function's. An earlier attempt
ranked by "richness", which quietly preferred a transcribed name over a
controlled one.

**One stated exception: `creator_controlled`.** A 7XX `$a` is an
authority-controlled name and a contents note's `$r` is transcribed from the
item. "Rautavaara, Einojuhani" and "Rautavaara" are the same person and only the
first will match an authority record, so a controlled creator wins whichever
entry otherwise takes precedence. Declared per field entry, not inferred.

### Verified

24 tests in `tests/test_constituent_extraction.py`: ordered tracklists,
per-group subfields, a creator ahead of its title, both indicator-filter
outcomes (a basic 505 and a plain 700 yield nothing), blank indicators written
as a space or omitted, dedup in all three modes, the controlled-name exception,
stated ordinals, disc numbers, the cap logging the record id, a `component`
from 773, an RDF entry ignored, and the legacy path byte-for-byte.

733 → **757 passing**, lint clean, pipeline unchanged.

## Stage 4 ✅ COMPLETE

`loaders/aggregates.py` turns `Constituent` objects into rows — the shared half,
so two formats describing one album cannot store two shapes of the same fact.
Emission happens in the *loader*, not in `decompose_marc_wemi`: a record's
constituents are more works, and that function returns one of each tier, while
the loader holds a `DecomposedRecord`, which has held lists since the loader
contract landed.

`LoadContext` carries a resolved `SourceConstituents`; `ingest_oai_pipeline`
resolves it once before reading any record, so a bad profile name stops the
harvest at the start rather than after 200,000 records.
`LoaderCapabilities.emits_constituents` declares it, and the other two loaders
declare the gap: an authority record describes a work rather than a publication
with contents, and the BibFrame path reads no `bf:hasPart` yet.

Two albums, one shared track, ingested through the real loader path:

```
work ALB-1                'Cantus arcticus'        (container)
work c:36383d9b726368ed   'Cantus arcticus'        containers=2
work c:f77a6a61f9211418   'Angel of light'         containers=1
work ALB-2                'Rautavaara collection'  (container)
work c:42319cf2bb747398   'Isle of bliss'          containers=1

ALB-1 junction: [primary, None] [track 1, 18:20] [track 2, 31:05]
ALB-2 junction: [primary, None] [track 1, 12:00] [track 2, 18:25]

after delete_source: every table empty
```

Five work rows for two albums and three distinct pieces: the shared track is one
work with two containers, while each release keeps its own timing on its own
junction row — duration belongs to the embodiment, not to the work.

### Convergence needed a cataloguing rule, not a digest tweak

The first run produced *six* works: the shared track came out twice, because one
release's note names its composer in `$r` and the other's does not, so one digest
had an author key and the other had none. A constituent that states no creator
now inherits the container's **main entry** — which is the cataloguing rule, that
contents listed under a single main entry are that main entry's work — and
`compute_author_key` already folds "Rautavaara" and "Rautavaara, Einojuhani" to
the same key, so the two forms were never the problem. `marc_main_entry_name`
returns None for a record with no 1XX, which is precisely the various-artists
compilation where inheriting would attribute every track to one artist.

### And a guard that was dead, then wrong

A release whose note lists one work, itself, *is* that work, and the container row
already represents it. The first guard compared the constituent's URI with the
container's — a digest URI against a control-number URI, which can never be equal,
so it never fired at all. Fixing the comparison then made it too broad: an album
named after one of its several pieces lost that piece from its own tracklist. It
now applies only when the note lists exactly one work, which is the case where
the container and the constituent really are the same work.

That is the fourth guard this project has had that could not fire. The pattern
worth naming: a guard written against an identifier whose *shape* the author had
not checked.

### Verified

19 tests in `tests/test_aggregate_emission.py`: all three modes (and `keys_only`
reproducing today's rows exactly), identical blocking keys in every mode, ordered
junctions with durations, edges in both directions from one row, convergence
across releases, per-release timings, main-entry inheritance and its absence on a
compilation, the single-work release, re-ingest stability, `record_origin` and
ISBD stripping applied by `finalise` for free, serialisation, the capability
declarations, and a 773 skipped rather than half-written.

757 → **776 passing**, lint clean, legacy analytic parity still PASS, pipeline
unchanged.

## Stage 5 ✅ COMPLETE

A 7XX carrying `$t` is a name-title analytic entry: the agent wrote the
constituent, not the volume. Both were filed against the container, so an
anthology's story authors were stored as its own primary authors. Measured on the
same record, before and after:

```
mode=keys_only                                    mode=entities
  Waltari  -> 'Suomalaisia novelleja' primary       Waltari  -> 'Kuun maisema' primary
  Canth    -> 'Suomalaisia novelleja' primary       Canth    -> 'Agnes'        primary
  Virtanen -> expression, role=edt                  Virtanen -> expression, role=edt
blocking keys identical in both modes
```

The editor — a 700 with no `$t` — is untouched, which is the point of making it a
profile decision rather than a rule about 7XX.

### The agent rows stay

Waltari is a real agent and carries the `$0`/`$1` authority links. What was wrong
is the *tier the contribution named*, so the fix moves contributions and deletes
nothing.

### A composer's own album is a copy, not a move

`marc_enhanced` names the 1XX as protected, and the first implementation simply
skipped it — which left a composer's own piece with no creator at all on an album
listing his own works. The main-entry case now *adds* a contribution to the
constituent and leaves the container's intact, because he contributed both:

```
Rautavaara -> 'Teoksia'          primary   (container, kept)
Rautavaara -> 'Cantus arcticus'  primary   (constituent, added)
Sibelius   -> 'Finlandia'        primary   (moved off 'Teoksia')
```

### What this stage deliberately does *not* change

I said earlier that this fix would change aggregate works' author keys and move
matching results. It does not, and the reason matters: `author_key` comes from
`parsed["agents"][0]`, not from contributions, and `build_target_record_from_marc`
shares that derivation. The target path carries no constituent profile, so
changing the key on the ingest side alone would make the two sides disagree about
the same record and break tier-2 domain-key matching for exactly the records this
plan is about.

So the blocking keys are byte-identical in every mode — asserted — and "should an
aggregate's author key come from its first analytic entry at all?" is now open
question 8. It needs the target path to carry a profile, which is its own change.

### Verified

7 more tests (26 in `tests/test_aggregate_emission.py`): the defect in
`keys_only`, the fix in `entities`, the editor untouched, agent rows kept, the
main-entry copy, nothing moving in `keys_only`, and the blocking keys pinned.
776 → **783 passing**, lint clean, pipeline unchanged.

### One finding, filed rather than fixed

The relator registry does not fold diacritics: a source writing `säveltäjä`
resolves to `cmp`, and one writing the ASCII-folded `saveltaja` stores the raw
term. The project has `fold_label` and a `preserved_diacritics` setting, so the
machinery exists. Out of this stage's scope — it belongs with relator
normalisation, not with aggregates — but it is a live gap for any source that
ASCII-folds its Finnish.

## Stage 6 ✅ COMPLETE

Shape B turned out not to be "emit more rows". A host-item entry describes the
*container*, so what it yields is a fact about the record already in hand: this
manifestation is a component of that one. `apply_component_containment` sets
`is_component`, `component_type`, `host_title`, `pagination` and — the part that
never existed — `container_uri`.

An article and its journal issue, ingested in separate batches:

```
batch 1: the journal issue, on its own
  (FI-MELINDA)000900111  is_component=False container_uri=None
batch 2: two articles, a separate run
  (FI-MELINDA)000900222  is_component=True type=article
                         container_uri=urn:manif:melinda:(FI-MELINDA)000900111
  (FI-MELINDA)000900333  is_component=True type=article container_uri=None   (no $w)

issue manifestation id = 1
  000900222: container_id=1
  000900333: container_id=None

components of the issue (1): 000900222  type=article
manifestations left after deleting the issue: 1   (the cascade took the linked one)
```

### The link is `$w`, and the `citation` key cannot serve

The plan said the container would be found "through the profile's
`container_key`", naming `citation`. That does not work, and the reason is worth
recording: the citation key is `ISSN::year::page::author`, which identifies the
*article*. Nothing on the host's record derives anything equal to it. MARC 773
`$w` carries the host's record control number, and it is the only subfield in a
host-item entry that identifies a row rather than describing one — so
`container_source_id` is the profile field that matters, and `container_key`
stays in the spec for a source whose parts genuinely name their host by a key.

### Resolution has to reach the store, not just the batch

Stage 1 resolved `container_uri` within a batch, which is the rare case: a
journal issue is ingested long before the articles that cite it. `insert_wemi_batch`
now falls back to `get_manifestation_by_uri`, so containment works across batches
and across runs.

### One mapping corrected on the way

The first profile read pagination from 773 `$g`, which is MARC's "related parts"
— a composite like `"2020, s. 12-34"` — so a year and a volume would have been
stored in a column named for pages. `$q` is "enumeration and first page", and it
is what the existing `citation` key already reads. A source that really does
write its pages in `$g` says so in its own profile.

### Verified

7 more tests (33 in `tests/test_aggregate_emission.py`): the fact-not-rows shape,
resolution from an earlier batch, asking a container for its parts, an article
with no `$w`, a host absent from the store, the delete cascade now that rows
carry values, and a source with no profile keeping exactly its old behaviour.
783 → **790 passing**, lint clean, pipeline unchanged.

## Stage 7 ✅ COMPLETE

`bf:hasPart` on a Work is containment. All three shipped schema profiles mapped
it to `has_series_member`, so every BIBFRAME aggregate was recorded as a series.
Fixed in all three, with `series_classes` deciding the exception: the same
predicate pointing at a `bf:Series` *is* series membership, and BIBFRAME 2 states
that with `bf:hasSeries` anyway.

```
--- an anthology with two parts and a series
    series_member_of   -> http://example.org/series1
    has_constituent    -> http://example.org/p1
    has_constituent    -> http://example.org/p2
    works: ['Suomalaisia novelleja', 'Kuun maisema', 'Agnes']
```

A part keeps its own node URI as its identity — the graph stating it is better
evidence than a digest of its title — and only a blank-node part falls back to
the content digest. The constituent profile says *where* the parts are; the
schema profile says how this dialect writes a title and an agent. Neither
duplicates the other, which is why `bf:title` pointing at a Title node rather
than a string is handled without the constituent profile knowing about it.

Extraction reaches the work node through a `constituent_sink` out-parameter, the
same pattern and the same reason as `key_sink` on the MARC side: the node the
parts hang off is resolved by seventy lines of decision matrix inside
`decompose_bibframe_work`, and a caller has no way to find it again.

### Four defects, each invisible until a record produced two works

1. **Relationship edges named neither endpoint.** `source_work_id=0,
   target_work_id=0` and no URIs, so every edge violated the foreign key on its
   target — a graph stating any mapped predicate would have failed its batch.
   Since stage 1 they were dropped with a warning instead, which is how this
   surfaced quietly rather than as a crash.
2. **An endpoint has to use the URI the row is stored under.** A work with a real
   URI is stored under *that*, and only a blank node gets
   `urn:work:{source}:{digest}`. The first attempt minted
   `urn:work:bffi:http://example.org/w1` for a work stored as
   `http://example.org/w1`, so the edge named a row that does not exist.
   `bibframe_work_uri` now mirrors the storage rule in one place.
3. **The decomposer deduplicated edges on `(type, source_id, target_id)`** — all
   zero before binding — so two `bf:hasPart` objects collapsed into a single
   edge and one part was silently dropped. Keyed on URIs now.
4. **Expressions and manifestations named no work.** `insert_wemi_batch` matched
   them by `(source, source_id)`, which agrees for MARC and *disagrees* for
   BIBFRAME, where the work, the expression and the instance are three different
   nodes with three different ids. The only thing binding them was the fallback
   "the single work in this batch" — so the moment a record produced a second
   work, the insert failed its foreign key. `ExpressionEntity.work_uri` and
   `ManifestationEntity.work_uri` complete what stage 1 started for
   associations, relationships and containers.

That last one is the finding worth keeping: **every one of the batch binder's
heuristic fallbacks was load-bearing only because a record had never produced
more than one work.** Five row types needed an explicit URI; stage 1 found three
of them and this stage found the rest.

### Verified

16 tests in `tests/test_aggregate_bibframe.py`: the mapping in all three
profiles, containment vs series by the other end's type, both endpoints named,
endpoints matching stored URIs, two parts staying two edges through a real
insert, no fact stated twice, parts as works with their own URIs, a title read
from behind its Title node, a blank-node part digesting, embodiment in the
container's manifestation, and both `keys_only` and no-profile behaviour.

790 → **806 passing**, lint clean, legacy analytic parity still PASS, stage-4
convergence harness unchanged, pipeline unchanged.

## Stage 8 ✅ COMPLETE

```
matched via funnel_g_anthology at tier1_exact_identifier
work: urn:work:fennica:ANTH-1
constituent matches:
   'Kuun maisema'  -> urn:work:fennica:c:336d5fc518e71acc  constituent_of
   'Agnes'         -> urn:work:fennica:c:6ab8da0b7bf30d9d  constituent_of
```

`get_constituent_works(work_id)` answers a whole tracklist in one indexed
lookup, ordered by the junction's disc and track number. Funnel G's `resolve`
mode aligns the target's analytic titles against it, and the recorded-sound tier
reports the matched album's own tracks rather than the target's claimed ones.
The per-title lookup remains as a fallback — without the hardcoded
`work_type="text"` that made it useless for music — for a container ingested
under `keys_only`.

### The blocking query was a full table scan

Raised mid-stage, and worth the measurement. `find_works_by_author_and_title`
matched `title_normalized LIKE '%stem%'`, which `EXPLAIN QUERY PLAN` reports as
`SCAN w`. At 60,000 works:

| form | plan | cost |
|---|---|---|
| `LIKE '%stem%'` | **SCAN w** | **6.38 ms** |
| `= stem` | `SEARCH w USING INDEX idx_works_title_norm` | < 0.01 ms |

It is called at least once per target record, so the scan was the dominant cost
of tier 2 — and constituent works multiply the table it scans. Resolving one
20-track album cost **135 ms**.

The fix is a ladder — equality, then a prefix `LIKE 'stem%'` (both indexed),
then the substring scan — each rung tried only when the one before found
nothing, so recall is preserved exactly. **6.38 ms → 0.22 ms**, and the 20-track
album **135 ms → 4 ms**.

A vector index was considered and declined *for this lookup*: equality is
already a thousand times faster on an existing index, both sides compute the
same stem through `compute_title_stem`, and an approximate neighbour cannot
deliver the exact-blocking contract tier 2 promises. Tier 3 already does dense
retrieval and FTS for titles that do not match exactly, which is where such a
title is supposed to go. Stage 9 is where a constituent-specific index gets
measured rather than assumed.

One behaviour change falls out: when an exact stem exists, a work whose title
merely *contains* the probe is no longer a tier-2 candidate. It has a different
stem, which is what tier 2 blocks on. A test in `test_rerank_wiring.py` had been
leaning on exactly that noise for its precondition — it asserted that the
blocking query returned the *weaker* candidate first — so it now constructs two
candidates with the same stem, which is the case it meant: ranking beating "take
the first row".

### Constituents were reported from one tier out of three

`default_tier2` filled in `constituent_matches`; tier 1 and tier 3 build their
own results and did not. So an anthology matched on its **ISBN** — the strongest
match available — reported no constituents, while the same anthology matched by
author and title reported all of them. `run_funnel` now fills in what the funnel
reports over whichever tier answered: a tier decides *what matched*, the funnel
decides what to report about it. Stored relationships are filled the same way;
an *asserted* relationship is not, because it is a claim the matching tier makes
and a tier that did not make it should not have one attributed to it.

### Verified

7 tests in `tests/test_aggregate_consumers.py`: track-ordered constituents, a
work with none, only `has_constituent` edges followed, the index in the query
plan, recall preserved through the ladder, exact preferred over longer, and
constituents reported from a tier-1 match end to end through `WorkMatcher`.
806 → **813 passing**, lint clean, pipeline unchanged.

## Stage 9 ✅ COMPLETE

Measured on 200 albums of 12 tracks, not assumed.

| | keys_only | entities | |
|---|---|---|---|
| works | 200 | 1088 | 5.4× |
| expressions | 200 | 1088 | 5.4× |
| **manifestations** | 200 | 200 | **1.0×** |
| junction rows | 200 | 2600 | 13× |
| `has_constituent` edges | 0 | 2400 | — |
| entity_keys | 5600 | 5600 | 1.0× |

Two of those confirm the design: manifestations do not grow, because an embodied
constituent has no carrier; and entity_keys do not grow, because constituents
emit none. Works grow 5.4× rather than 13×, because convergence merged tracks
shared between releases — 888 work rows for 2400 track slots.

### The index decision: exclude, and the argument is the measurement

Constituents would be **82%** of the work index, and BM25 over the same
`serialized_text` the index embeds shows what that does:

```
query 'Finlandia ja muita teoksia 3'   ->  1. ALBUM 'Finlandia ja muita teoksia 3'
query 'Finlandia'                      ->  1. constituent 'Finlandia nro 4'
                                           2. constituent 'Finlandia nro 3'
                                           ... six tracks, no album at all
```

Tier 3 takes the top *k*, so for a target album with a short or common title the
real album candidates are pushed out of the window — a recall regression in album
matching caused by indexing things that are not albums. So
`build_wemi_indexes` excludes them, and the corpus above goes from 1088 indexed
works to exactly 200.

Nothing is lost: a constituent is reachable exactly by its `analytic_title` key
and from a matched container through `get_constituent_works`. And the opposite
case is real — a source whose *targets* are tracks rather than releases wants
them indexed — so it is `constituents.index_works`, a setting, not a rule.

The test for "only a constituent" is **having no manifestation of its own**,
which needs no naming convention: a work nobody published separately is not
something a record can match at the manifestation level, and a track released as
a single is a row with its own carrier and stays indexed.

This is also where the vector-index question raised during stage 8 lands. A
constituent-specific index would serve track-level *targets*, which no source in
scope ingests; the flag above is the cheaper version of the same capability, and
the exact key path already covers the retrieval that exists.

### Clustering: constituents are fine, and one finding is not

Consolidation loads every authoritative work, so constituents take part. On two
sources ingesting the same 60 albums it grouped **60 groups of exactly 2** with
120 symmetric `same_work_as` edges — the containers — and left the constituents
ungrouped. Nothing blew up: no group exceeded 2 members, nowhere near the 500
cap.

The constituents did not group because a 505-derived track has no *contribution*:
the main entry is inherited for **identity** but never recorded as a creator, so
consolidation has only the title to compare and scores 0.90 against a 0.93
threshold. That is open question 9.

### The finding that nearly became a false one

The first consolidation run reported **zero** groups, including for the
containers, and it looked like a pre-existing consolidation defect. It was my
fixture: it wrote `$e saveltaja` without diacritics. The chain is worth keeping,
because every link is silent:

1. the relator registry does not fold diacritics, so `saveltaja` does not
   normalise to `cmp` (the finding filed in stage 5);
2. a role that is not a work role files the contribution against the
   **expression** instead of the work;
3. `load_signatures` reads `contributions WHERE entity_type = 'work'`, so the
   work has no creators;
4. the metadata score drops from clearing 0.93 to 0.90, and **nothing
   consolidates at all**.

Spelled `säveltäjä`, the same corpus consolidates perfectly. So the diacritic
gap is not cosmetic: one unfolded relator term silently disables cross-source
work consolidation for every record that uses it. It is cheap to fix and belongs
with relator normalisation rather than here, but it should be fixed.

### Verified

6 tests in `tests/test_aggregate_indexing.py`: exclusion by default, the flag
restoring inclusion, a constituent that was also published staying indexed,
manifestations never filtered, the config default, and a constituent still
reachable from its container. 813 → **819 passing**, lint clean, pipeline
unchanged.
