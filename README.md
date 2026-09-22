# SageBrain TAP

TAP (Targeted Augmentation Pipeline) is SageBrain’s shared framework for bringing in valuable external and generated knowledge.

This lets SageBrain "tap" selected sources to augment its capability, from ontologies and resources like Reactome and PubChem.

> Our graph goes to 11.

## Sources

| Source | Named graph | Status |
|---|---|---|
| [Reactome](reactome/README.md) | `urn:sagebrain:reactome:v97` | Human pathways, `sagebrain:participates_in` gene edges with reified associations behind them, and GO links; V97 acceptance report available |

Start with the [Reactome quick start](reactome/README.md#quick-start).

## Layout

Each source owns its Python modules and manifests. Reactome uses this layout:

```text
reactome/
  *.py                 Download, transform, load and query commands
  input/v97/           Downloaded sources and release.json (ignored)
  v97/data/
    rdf/               Generated Turtle (ignored)
    interim/           Cross-step files (ignored)
    reports/           Unmapped accessions and other diagnostics (ignored)
    store/             Local Oxigraph database (ignored)
  manifests/           Committed checksums and validation reports
shared/rdf.py          Turtle writer, literals and namespace registry
shared/oxigraph.py     Graph loading and local/HTTP query access
schema/                Source-specific LinkML definitions
slides/                Stakeholder presentations
```

Commands run from the repository root as `python -m <source>.<module>`.
Reactome supports local Oxigraph and the optional HTTP service.

## Conventions

Only commit manifests, not downloaded data or generated RDF. Checksums and provenance
identify the inputs; reproducing an ingest still requires those source files.
Reactome records its Zenodo version DOI and the HGNC mapping checksum.

Each source release has a named graph. Reloading replaces that release's data
while retaining other releases. Dataset metadata lives in the default graph;
retired Reactome pathways have a separate cumulative graph.

The shared model is defined in
[sagebrain-model](https://github.com/Sage-Bionetworks/sagebrain-model).
`sagebrain:` identifies model terms. There is no second namespace for
ingest-specific ones; a term an ingest needs before the model has ratified it is
minted under `sagebrain:` and reported as undefined by each acceptance suite,
so it stays visible until the model catches up.

That check reads the model published on
[sagebrain-model](https://github.com/Sage-Bionetworks/sagebrain-model)'s default
branch, because what counts as ratified is what is pushed, not what a local clone
happens to contain. `--model-ttl` takes a path instead when editing the model or
running offline; an unreachable model warns.
Species filters, evidence mappings and source layouts stay in their source module.
