# Reactome

Human pathways, gene–pathway associations and GO crosswalks as RDF.
Reaction-level mechanisms and non-human species are outside current ingest scope.

## Quick start

Run from the repository root with Python 3.10+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r reactome/requirements.txt
python -m reactome.pipeline --version 97
```

Then run a test query:

```bash
python -m reactome.queries --canned gene-pathway-count --gene APP
```

The pipeline downloads sources into `reactome/input/v97/`, writes Turtle to
`reactome/v97/data/rdf/`, and loads `reactome/v97/data/store/`.
Use `--skip-download` to reuse sources, `--round-trip 0` to skip online Content
Service checks, or `--input-dir` and `--workdir` to choose other locations.
The named graph is `urn:sagebrain:reactome:v97`.

## Optional HTTP server for local/dev testing

The Docker service uses the same Oxigraph version as the Python dependency,
with its own persistent volume. No image build or RDF staging is needed.

```bash
docker compose -f reactome/compose.yaml up -d
python -m reactome.pipeline --version 97 --skip-download \
  --endpoint http://localhost:7010 --expected-pathways 2883
python -m reactome.queries --endpoint http://localhost:7010/query \
  --canned disease-gene-panel --panel als
```

Open <http://localhost:7010> for the SPARQL UI. The pipeline takes the server
**base URL**; individual commands take `/store` for loading and `/query` for
queries or acceptance checks, following the
[Oxigraph HTTP API](https://github.com/oxigraph/oxigraph/tree/main/cli#usage).
Set `OXIGRAPH_PORT` to change the host port (default: 7010).
`docker compose -f reactome/compose.yaml down` stops the server and retains data.

To load existing RDF without rerunning transforms:

```bash
python -m reactome.load_graph --version 97 \
  --ttl-dir reactome/v97/data/rdf \
  --release-json reactome/input/v97/release.json \
  --endpoint http://localhost:7010/store
```

## Queries

Canned queries select one release (`--version`, default `97`) in either backend.
Use `--store PATH` for a local database, or `--endpoint URL` for HTTP.
`OXIGRAPH_ENDPOINT` can supply the query URL. Output formats: `tsv`, `csv`, `json`.

```bash
python -m reactome.queries --canned gene-pathways --gene NF1
python -m reactome.queries --canned shared-pathways --genes APP,PSEN1
python -m reactome.queries --canned hub-genes --limit 10 --format json
```

Membership queries traverse `sagebrain:participates_in`, so they cannot be
answered by some other association that happens to have a gene in subject
position. `gene-pathways` and `evidence-breakdown` join the reified association
instead, because they project evidence and the plain edge does not carry it.

`--help` lists all queries. Disease panels keep genes absent from the graph as
zero-count rows; a zero means no association in this ingest, not no biological role.
Raw queries (argument or stdin) must select their own graph:

```bash
python -m reactome.queries \
  'SELECT (COUNT(*) AS ?n) WHERE { GRAPH <urn:sagebrain:reactome:v97> { ?s ?p ?o } }'
```

## Pipeline and model

| Module | Result |
|---|---|
| `download_sources` | Versioned Reactome files, HGNC mappings, DOI and checksums |
| `verify_columns` | TSV layout gate and committed report |
| `transform_pathways` | `pathways.ttl`; filtered IDs in `interim/` |
| `transform_associations` | `associations.ttl` and `participation.ttl`; unmapped accessions in `reports/` |
| `transform_go` | `go_crosswalk.ttl` |
| `load_graph` | Release graph and `void.ttl` metadata in the default graph |
| `acceptance_checks` | Species, hierarchy, evidence, NF1, edge/association agreement, release-size and model-term checks |
| `describe_data` | Release characteristics as JSON, in `manifests/` |

The pipeline runs these in order; `describe_data` follows the acceptance checks,
so a release that fails them leaves no characteristics manifest behind.

Every module supports `python -m reactome.<module> --help`.
See [schema](../schema/reactome.yaml), [design decisions](DESIGN.md) and
[manifests](manifests/).

- Filter by species name; filter hierarchy edges against the resulting pathway IDs.
- Hierarchy uses `biolink:part_of`; GO links use `skos:closeMatch`.
- `_All_Levels` already includes ancestor pathways. Do not propagate again or
  treat per-pathway gene counts as independent enrichment categories.
- Genes use HGNC IDs and are emitted as typed `biolink:Gene` nodes.
- Gene membership is written twice, from one set: `participation.ttl` carries the
  distinct pairs as plain `sagebrain:participates_in` edges for traversal,
  `associations.ttl` reifies each row so evidence, accession and knowledge source
  have somewhere to live. The edge is a union over evidence codes and accessions
  and cannot say whether a pair is curated or orthology-projected; join the
  association when that matters. Acceptance check 9 proves the two agree.
- Associations retain the original UniProt accession as `biolink:original_subject`,
  Biolink's own slot for a pre-normalization subject. It is string-valued, so the
  accession is recorded but not traversable. Isoform
  suffixes are kept. Multiple HGNC matches fan out; unmapped rows are reported and
  fail the transform above 10%.
- Evidence maps `TAS` to `ECO:0000304` and `IEA` to `ECO:0000501`.
  Unknown species and evidence codes fail the ingest.

## Provenance and validation

From the emitted Turtle, `describe_data` summarizes an ingested release
hierarchy shape, gene-set sizes, evidence mix, propagation and source checksums.
The pipeline produces this automatically, but to regenerate it alone:

```bash
python -m reactome.describe_data --version 97
python -m reactome.describe_data --version 97 --out -   # or stdout
```

The result is committed as
[`manifests/v97-characteristics.json`](manifests/v97-characteristics.json).
It counts the release graph's statements from the Turtle and checks that total
against the `void:triples` the loader asserted.

The [recorded V97 run](manifests/v97-acceptance.md) has 2,883 pathways,
11,485 genes, 161,818 associations over 142,146 distinct gene-pathway pairs, and
1,321,893 release-graph triples. Its provenance is `10.5281/zenodo.21383214`.

Acceptance checks fail on structural errors; Content Service differences are
warnings. Use `--expected-pathways` to enable comparison with a release's count.

Downloads use `download.reactome.org/<version>/`; the Zenodo version DOI is
recorded in VoID. `download_sources --from-zenodo` uses the full archive instead;
`--enumerate-remote` records its contents without downloading the entire archive.
HGNC is a changing upstream file: preserve the downloaded copy and its checksum
when exact reproduction matters.

## Release retirement

```bash
python -m reactome.release_diff \
  --previous reactome/input/v96/ReactomePathways.txt \
  --current reactome/input/v97/ReactomePathways.txt \
  --previous-version 96 --current-version 97 \
  --outdir reactome/v97/data/rdf \
  --report reactome/manifests/v96-to-v97-diff.md
curl --fail -X POST -H 'Content-Type: text/turtle' \
  --data-binary @reactome/v97/data/rdf/retired_v96_to_v97.ttl \
  'http://localhost:7010/store?graph=urn%3Asagebrain%3Areactome%3Aretired'
```

Retired pathways retain their label and last-seen release in the cumulative
`urn:sagebrain:reactome:retired` graph. Append with POST, don't replace it.
The regular loader loads only the four core files and VoID, so retirement
files are not accidentally mixed into a release. Successor inference by exact
name is available with `--infer-successor-by-name` and is off by default.

## Development

```bash
python -m unittest discover -s reactome/tests -v
```

Set `TEST_OXIGRAPH_URL` to a disposable server's base URL to run the same tests
over HTTP as well. The tests replace release graphs `v99996` and `v99997` there.
