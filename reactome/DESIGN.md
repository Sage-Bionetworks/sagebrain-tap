# Reactome design decisions

Implemented for human Tier 1 pathways. Run instructions are in [README.md](README.md);
recorded results are in [manifests/](manifests/). This document records the model
and validation contract.

## 1. Scope

Support gene membership, pathway hierarchy, GO crosswalks and curated/inferred
evidence. Exclude non-human species and reaction-level BioPAX mechanisms.
Future mechanism data can attach to the same pathway identifiers.

## 2. Sources and provenance

Use numbered Reactome download URLs for individual files and the Zenodo version
DOI as the provenance anchor. The full Zenodo archive is an alternative input;
its central directory can be enumerated through HTTP range requests.
Record source checksums, DOI and HGNC mapping checksum. Check downloaded content:
some Reactome URLs return HTML errors with HTTP 200.

## 3. Verified layouts

| File | Tab-separated columns | Header |
|---|---|---|
| `ReactomePathways.txt` | stable ID, name, species | No |
| `ReactomePathwaysRelation.txt` | parent ID, child ID | No |
| `UniProt2Reactome_All_Levels.txt` | accession, pathway ID, URL, name, evidence, species | No |
| `Pathways2GoTerms_human.txt` | Identifier, Name, GO_Term | Yes |

`verify_columns` gates every release. HGNC's complete set provides `hgnc_id`,
`symbol` and `uniprot_ids` for identifier resolution.

## 4. Target model

Biolink 4.4.4 supplies the classes and predicates used here.
[schema/reactome.yaml](../schema/reactome.yaml) records the LinkML definitions;
[shared/rdf.py](../shared/rdf.py) defines namespace bases.

```turtle
REACT:R-HSA-5673001
    a biolink:Pathway ;
    rdfs:label "RAF/MAP kinase cascade" ;
    biolink:in_taxon NCBITaxon:9606 ;
    biolink:part_of REACT:R-HSA-5683057 .

HGNC:7765
    a biolink:Gene ;
    rdfs:label "NF1" ;
    biolink:in_taxon NCBITaxon:9606 .

HGNC:7765 sagebrain:participates_in REACT:R-HSA-5673001 .

[] a biolink:GeneToPathwayAssociation ;
   biolink:subject HGNC:7765 ;
   biolink:object REACT:R-HSA-5673001 ;
   biolink:predicate biolink:participates_in ;
   biolink:has_evidence ECO:0000304 ;
   biolink:primary_knowledge_source infores:reactome ;
   biolink:original_subject "UNIPROT:P21359" .
```

Gene membership is asserted in two shapes, written from one set in one pass.
`sagebrain:participates_in` carries the distinct (gene, pathway) pairs as plain
edges; the reified `biolink:GeneToPathwayAssociation` carries every
(gene, pathway, evidence, accession) row. Neither is derivable from the other:
RDF does not entail the edge from the association, and the association cannot be
reconstructed from the edge. Both are needed, and the agreement between them is
an invariant this ingest maintains and acceptance check 9 verifies.

The edge exists because a relation nothing names cannot be traversed.
`?gene sagebrain:participates_in/biolink:part_of* ?ancestor` is one property
path; the reified equivalent is a two-triple join through a blank node per hop,
and a consumer that reads the graph as a graph -- a property-graph export, an
embedding, a neighbourhood expansion -- sees no gene-pathway relation at all
without it. Matching on `biolink:subject`/`biolink:object` instead is not a
substitute: it names no relation, so it silently widens to any association that
happens to have a gene in subject position.

The term is `sagebrain:participates_in`, ratified in the shared model as an
`owl:ObjectProperty` with `rdfs:domain biolink:Gene`, `rdfs:range
biolink:Pathway` and `rdfs:subPropertyOf biolink:participates_in`. This is the
first place a Reactome release satisfies one of the shared model's own
connections rather than being Biolink-shaped data that happens to sit in a
SageBrain graph. The `biolink:` superproperty is not materialised: nothing in
this stack reasons over `rdfs:subPropertyOf`, and doubling the edges to serve a
query the model-aligned term already serves buys nothing.

What the edge cannot say is the cost. It is a union over evidence codes and
over source accessions: one pair can come from several accessions and carry TAS
and IEA at once, and 11% of the pairs at V97 are IEA-only -- orthology
projections, not curation. The association stays authoritative for evidence,
for the originating accession and for the knowledge source, and the two queries
that report evidence join it rather than the edge. Source attribution for the
edge itself rests on the named graph, which is per release and therefore per
source; a cross-graph union query must bind the graph to keep it.

Both endpoints are typed nodes. Every gene that
survives UniProt→HGNC resolution gets a `biolink:Gene` node carrying its symbol
and taxon, written by `transform_associations.py` alongside the associations
themselves. A consumer can therefore enumerate the genes in a release, or label
one, without reading the association table; and a query that joins on a gene
cannot silently match an identifier the ingest never asserted.

External entities use identifiers.org URIs (Reactome, HGNC, UniProt) or OBO URIs
(taxa, GO, ECO). Model terms use `sagebrain:`, the only local namespace: a term
this ingest needs before sagebrain-model ratifies it is minted there anyway and
listed by acceptance check 12 until it is defined. The release graph uses one,
`sagebrain:participates_in`, which the model ratified in v0.2;
`release_diff.py` mints two undefined ones for the separate retired graph.

The UniProt accession each association came from is kept as
`biolink:original_subject`, Biolink's own slot for what a source called the
subject before normalization. It is an `owl:DatatypeProperty`, so the value is a
string and cannot be followed. That is deliberate decision for now. When a source
that makes statements about UniProt entities is ingested, those entities will be typed
as `biolink:Protein` (`biolink:ProteinIsoform` for the suffixed ones) and linked
from the gene with `biolink:has_gene_product`.

## 5. Modelling rules

1. Filter species by name, not stable-ID infix. The hierarchy file has no species
   column; both endpoints must belong to the filtered pathway set.
2. Use `biolink:part_of` for containment and `skos:closeMatch` for GO crosswalks.
   Neither subclassing nor equivalence describes these relationships.
3. Resolve UniProt to HGNC, stripping isoform suffixes only for lookup. Keep the
   full accession on associations. Multiple HGNC matches produce multiple facts.
4. `_All_Levels` is already transitively closed. Do not propagate again.
   Counts across pathways are not independent; VoID records this limitation.
5. Preserve `TAS` → `ECO:0000304` and `IEA` → `ECO:0000501`. Unknown codes fail.
6. Write the plain `sagebrain:participates_in` edge and the reified association
   from the same in-memory set, in the same run. Two derivations of one fact
   drift; one derivation cannot. The check is still run against the loaded
   graph, because that is where a partial reload would show up.

Unmapped accessions are reported, with a default failure threshold of 10% of
in-scope rows. The recorded V97 run lost 3.01% after isoform normalization.

## 6. Graphs and refresh

Oxigraph stores each release in `urn:sagebrain:reactome:v<N>`. Reloading replaces
only that graph. VoID lives in the default graph and includes the Zenodo DOI,
license, release, triple count, species and ingest activity.

`release_diff` compares pathway lists. Retired IDs keep their label and release
history in `urn:sagebrain:reactome:retired`, appended cumulatively. The flat files
do not identify authoritative successors; exact-name inference is optional.

## 7. Acceptance criteria

Check human taxa first, then human stable IDs, exactly one taxon per entity,
pathway count, acyclic hierarchy, resolved hierarchy endpoints, evidence codes,
NF1 membership across hierarchy levels, participation-edge agreement and total
triple count (1–4 million). Use an explicit expected pathway count for the
release-count comparison.

The participation check runs both directions. An edge with no association behind
it is a membership claim with no evidence and no knowledge source, which this
graph is not allowed to contain. An association pair with no edge is a fact no
property path can reach: every query still runs and quietly returns less, which
is the failure mode a triple count would never catch.

Then check that every `sagebrain:` term in the graph is defined in
sagebrain-model, read from its default branch rather than a local clone. A
warning, never a failure: the model repo and this ingest move at different
speeds, and so does the network.

Optionally compare sampled pathway accessions with the live Content Service.
Differences or network failures are warnings because the live service and HGNC
mapping can differ from release inputs. Structural failures stop the pipeline.

## 8. Failure handling

Reject unknown species, unknown evidence, changed TSV layouts and excessive
unmapped accessions. Require the `_All_Levels` filename to avoid accidentally
loading leaf-only associations. Keep intermediates and reports outside `rdf/`.

## 9. Execution

Download → verify → pathways → associations → GO → load → acceptance checks.
The association step writes both `associations.ttl` and `participation.ttl`.
Pathways writes the ID set consumed by the next two transforms. Release diffing
is separate because it needs two releases. Local and HTTP queries select the
same named graph.
