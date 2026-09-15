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

The source field decides the shape:

| source | shape | today |
|---|---|---|
| MARC 505 `$t` (enhanced contents) | A | `analytic_title` key only |
| MARC 700/710 `$t` (analytic added entry) | A | `analytic` key, **plus a spurious contributor — see below** |
| MARC 773 (host item entry, on the component's own record) | B | `is_component=True`, container never linked |
| MARC 774 (constituent unit entry) | B | not read at all |
| BIBFRAME `bf:hasPart` on a Work | A | **mis-mapped to `has_series_member`** |

Shape B for 773 is the half-built path: the component knows its host through the
`citation` key, and `container_id` is never resolved, so the container does not
know its parts and the hierarchy cannot be walked in either direction from SQL.

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

Follow the shape the loader contract settled on: **format-specific extraction,
shared assembly.**

- Extend `extract_marc_analytics` into an ordered `Constituent` record:
  `title`, `creator`, `ordinal`, `disc`, `duration`, `pagination`, `source_tag`,
  `role`. It currently returns `(title, creator)` and discards 505 `$g`
  (duration), 505 `$r` (responsibility) and 700 `$g`.
- **Dedup 505 against 700.** The album above emitted `analytic` for
  "Cantus arcticus" twice — once from the 700 `$t` with a creator, once from the
  505 `$t` without one. Match on normalised title, keep the richer entry.
- New `loaders/aggregates.py`, beside `loaders/subjects.py`: a `Constituent` list
  in, and `(works, expressions, associations, relationships, entity_keys)` out,
  for both shapes. Both loaders call it, so the two formats cannot disagree about
  what a constituent becomes.
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

```yaml
ingest:
  constituents: entities      # off | keys_only | entities
  max_constituents_per_record: 100
```

`keys_only` is today's behaviour, kept so the change can be bisected. Default
`entities`, because there is nothing in production to migrate; the cap exists
because a real 505 can list a 500-track box set, and one malformed record should
not be able to write 500 works.

## Stages

Each is its own diff with its own verification, in this order:

1. **Repository binding and the relationship pair.** The four blockers above,
   plus `has_constituent` / `constituent_of`. No loader change. Verified with a
   hand-built batch: twenty ordered associations survive as twenty rows, in
   order, and a re-inserted batch adds no duplicate edges.
2. **`Constituent` extraction from MARC 505/700/710**, ordered, with duration and
   505-vs-700 dedup. No entities emitted yet; asserted against fixtures.
3. **Shape A emission** behind the config flag, through `loaders/aggregates.py`.
   Verified: an album yields 1 + N works and N ordered M-E rows; two albums
   sharing a track converge on one constituent work row; `delete_source` still
   removes everything.
4. **The contributor fix** — a 7XX with `$t` stops contributing to the container —
   with a measured before/after on matching, alone in its diff.
5. **Shape B**: `container_id` resolution for 773-bearing records, 774 if Melinda
   uses it, `component_type` derived from the container's work type.
6. **BIBFRAME `bf:hasPart`** via new profile fields, including the series
   mis-mapping fix.
7. **Rewire the consumers**: funnel G and recorded sound read stored rows;
   capability declarations, funnel reachability, `pipeline_check`.
8. **Decide indexing and clustering treatment**, with a measurement rather than a
   guess.

## Open questions — cataloguing, not code

These gate stages 2 and 5 and need real records, not reasoning:

1. **Melinda's 505 practice.** Enhanced (`ind2=0`, one `$t` per constituent) or
   basic (a single `$a` blob with " -- " separators)? The basic form cannot be
   split reliably, and a wrong split writes wrong works.
2. **Is 774 used at all** in Melinda or Fono? If not, stage 5 is 773-only.
3. **Durations**: does Fono or Yle give track durations, and in which subfield?
4. **Does a 700 `$t` always carry `ind2=2`** in Finnish practice, or is `ind2`
   blank also used for analytics? The contributor fix in stage 4 depends on being
   able to tell an analytic entry from a plain added entry.
5. **Should a constituent work inherit the container's subjects?** It changes
   agent footprints, since the subject histogram is stamped per record.
6. **Do anthology constituents belong in the target-side `MatchRecord`** as
   keys, or only in the store?
