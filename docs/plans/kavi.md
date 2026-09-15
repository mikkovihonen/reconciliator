# KAVI and Finnish moving-image metadata

Moving-image works are the weakest tier in this system: no Finnish library data
carries work-level identity for films or television programmes, so the AV funnels
have never had anything to match against. This document is the research into what
external sources exist, what is usable, and what it would take to ingest.

It is a workstream of its own, separate from
[wemi_data_structure.md](wemi_data_structure.md), which covers the WEMI refactor
itself. The funnel-reachability work that led here is Phase 14.15-14.18 there.

## Why this matters: no work-level AV linking exists yet

The National Library's memo *"Elonet-Teka-konversion tarpeellisuudesta osana
kansallista Teos Marc 21:ssä -hanketta"* (JT, 11.9.2023) states the problem
directly:

> Finnassa, Melindassa tai muussakaan kotimaisessa kirjastojärjestelmässä ei
> näitä tietoja kuitenkaan ole linkitetty RDA:n vaatimalla tavalla eli teoksen
> auktorisoidun hakutiedon kautta.

KAVI has published Elonet in Finna, Finnish libraries hold the same works, and
Melinda catalogues literature *about* those films - but none of it is linked
through an authorised work access point. Producing exactly that linkage, as work
authority records in **Teka** (Kansalliset teos- ja ekspressiotiedot) within
Asteri and later Finto, is a headline goal of the national *Teos MARC 21:ssä*
project. The conversion plan is field-by-field complete, and **SLM has been
harmonised with KAVI's Tenho vocabulary** to enable it.

Scale quoted in the memo: ~240 000 film titles and ~17 600 film reviews in Finna,
plus substantial books and articles about named films.

So the gap the AV funnels hit is not an oversight in this codebase - it is a
known national gap with a project underway to close it. Until Teka exists, an
external source is the only way to give AV works identity.

## The two candidate datasets

**KAVI, "Kotimaiset tv-sarjat KAVIn tietokannoissa"** (CC-BY 4.0). Ritva as
MPEG-7 XML (sarja-id, pysyvä tunniste, sarjan nimi, vaihtoehtoinen nimi, kuvaus,
from 1.1.2009) and Tenho as CSV. **Both distribution URLs 404.** They are Drupal
`/sites/default/files/` paths and kavi.fi has migrated to WordPress; the
avoindata.fi record still advertises the dead links, and KAVI has no open-data
landing page at any obvious path. Worth reporting to KAVI - the dataset is
catalogued but not downloadable.

**Yle Elävä arkisto / Arkivet** (CC0, explicitly experimental - *"palvelu saattaa
olla nurin tai päättyä varoituksetta"*). Live and downloadable:

| file | content |
|---|---|
| `program.csv` | 23 831 entities: `PID`, `PARTOF`, `TYPE` (TVSeries/TVProgram), titles fi/sv, `EPISODE_NUMBER`, `FIRSTRUN`, `DURATION`, classification, keywords |
| `media.csv` | 34 816 media items with contributors and actors |
| `tvlahetysrekisteri1985-1999.zip` | 227 483 TV transmission records |
| `radionlahetysrekisteri1997-2005.zip` | 3 018 032 radio transmission records |

`program.csv` is a WEMI hierarchy already: `TYPE=TVSeries` is the container,
`TYPE=TVProgram` the episode, `PARTOF` the link between them, `PID` a persistent
identifier, `FIRSTRUN` the first transmission. That is precisely the authority
data funnel GG has never had - the Elonet-Teka memo confirms no such work-level
linking exists in Finnish library data yet
(*"ei näitä tietoja kuitenkaan ole linkitetty RDA:n vaatimalla tavalla"*), which
is the whole point of the Teos MARC 21 project.

Caveats to weigh before ingesting: the series structure is *"automaattisesti
päätelty ja sisältää virheitä"* by Yle's own admission, the files were last
updated in 2018, and the service disclaims its own continuity.

**This needs a decision, not a default.** Ingesting it means a third loader
(CSV/MPEG-7 alongside MARCXML and BibFrame) and a new source type - a real
feature, not a refactor. The architecture takes it cleanly: it would register as
a source with `authoritative: true`, `PID` becoming the work `source_id`, and
`PARTOF` a `series_member_of` relationship.

618 passing.

## Harvesting: Finna OAI-PMH in EN 15907

**Answer to "is there an open OAI-PMH": yes, but not KAVI's own.**

`kavi.verkkokirjasto.fi` is Axiell Arena on Liferay 7.0.6 and exposes no OAI-PMH
at any of fifteen probed paths. That OPAC is the library catalogue whose
publications use the FIAF Classification Scheme for Literature on Film and
Television - literature *about* film, not the films.

The films are in **Finna**, which does have one:

```
endpoint   https://api.finna.fi/OAI/Server
formats    oai_dc  marc21  oai_vufind_json  oai_ead  oai_ead3
           oai_forward  oai_lido  oai_qdc  oai_finna_json
sets       non_dedup   (only - there is no per-organisation set)
```

### The format is itself the filter

`ListIdentifiers&metadataPrefix=oai_forward` returns **only** `kavi.*`
identifiers, `completeListSize=138149` - exactly the count the REST API reports
for `datasource_str_mv:"kavi"`. No other Finna datasource offers that format, so
requesting it is an effective KAVI-only harvest, with standard resumptionToken
paging. `set=kavi` is rejected as `badArgument`; the filtering happens through
the format instead.

`oai_forward` is **EN 15907** (`forward.cineca.it/schema/EN15907-forward-v1.0.xsd`),
the European standard for moving-image metadata - consistent with the FIAF
classification the OPAC mentions.

**KAVI film records have no MARC representation**: `metadataPrefix=marc21` on a
`kavi.elonet_elokuva_*` identifier returns `cannotDisseminateFormat`. So this
cannot be ingested through the existing MARCXML path.

### Scale is tractable

| | |
|---|---|
| works | 138 149 |
| page size | 1 000 records / 3.5 MB |
| record size | median 2.8 kB (max seen 218 kB, a fully-credited feature) |
| full harvest | ~0.4 GB uncompressed, 139 pages |
| agent links | ~6 per work, so roughly 830 000 contributions |

### EN 15907 maps onto WEMI almost directly

```xml
<CinematographicWork>
  <Identifier scheme="KAVI" IDTypeName="elonet_elokuva">100001</Identifier>
  <IdentifyingTitle>Aaltoska orkaniseeraa</IdentifyingTitle>
  <Title><TitleText>Aaltoska orkaniseeraa</TitleText>
         <TitleRelationship>original</TitleRelationship></Title>
  <Title><TitleText lang="swe">Aaltoska i sitt ässe</TitleText>
         <TitleRelationship>translated</TitleRelationship></Title>
  <Title><TitleText>Musta veljeskunta</TitleText>
         <TitleRelationship>working</TitleRelationship></Title>
  <YearOfReference>1949</YearOfReference>
  <HasAgent>
    <Activity tehtava="ohjaus" finna-activity-code="D02">D02</Activity>
    <AgentName>Edvin Laine</AgentName>
    <AgentIdentifier><IDTypeName>elonet_henkilo</IDTypeName>
                     <IDValue>100002</IDValue></AgentIdentifier>
  </HasAgent>
</CinematographicWork>
```

| EN 15907 | WEMI |
|---|---|
| `CinematographicWork` | `WorkEntity`, work_type `moving_image` |
| `Identifier` (`elonet_elokuva`) | `source_id` plus an `IdentifierEntity` |
| `IdentifyingTitle` | work title |
| `Title` + `TitleRelationship=original` | work title / uniform title |
| `Title` + `translated` with `lang` | expression-level titles, one per language |
| `Title` + `working` | variant title |
| `YearOfReference` | production year |
| `HasAgent` / `Activity` / `AgentName` | `ContributionEntity` with a role |
| `AgentIdentifier` | **persistent agent identifier** |

Three persistent agent id types: `elonet_henkilo` (person),
`elonet_kokoonpano` (ensemble), `elonet_yhtio` (company).

**That last row is the significant one.** The project's agent reconciliation
exists to attach authority URIs to bare names; this source supplies name, role
and a persistent identifier together, for ~830 000 contributions. It is an
authority source for film people, not only for films.

### What it would take

The existing OAI-PMH harvester needs no changes - resume, checkpointing, date
partitioning and the batch writer all work against any `metadataPrefix`. What is
missing is one decomposer: EN 15907 -> WEMI, alongside `marcxml` and `bibframe`.
Roles arrive as a mix of MARC relator codes (`aus`) and Finna/EN 15907 codes
(`D02`, `A99` with `finna-activity-text`), so the relator normaliser needs a
second code list.

Still the same decision as 14.18, now much better specified: a third loader is a
feature, not a refactor. The difference from the Yle dataset is that this one is
live, paged, standards-based, 138 149 works deep, and carries persistent
identifiers for both works and people - where Yle's is a 2018 CSV snapshot whose
series structure Yle itself calls automatically inferred and error-prone.

