# Open Targets design decisions

Implemented for the drug layer: molecule identity, mechanism of action and clinical
indication. Run instructions are in [README.md](README.md); recorded results are in
[manifests/](manifests/). This document records the model and validation contract.

## 1. Scope

In: `drug_molecule`, `drug_mechanism_of_action`, `clinical_indication`, and `disease`
for labels. Pinned and layout-gated but **not projected**: `clinical_report`. Out:
target–disease association scores, the 3.2 GB `variant` index, `target_essentiality`,
and everything else in the release.

`clinical_report` is deliberately deferred rather than dropped. Indication edges carry
a report *count*, which is enough to tell a stage backed by 14 reports from one backed
by a single record. The reports themselves — trial status, stop reasons, expert review,
countries — are a layer with its own modelling questions, and pinning them now means a
later pass starts from bytes that are already identified.

Association scores are excluded on principle, not for size. They are composites over
heterogeneous evidence; useful for ranking, wrong as evidence, and a graph that carries
them invites exactly the misuse where a number is quoted without the evidence behind it.

## 2. Sources and provenance

Upstream publishes `release_data_integrity`, a SHA-1 per file for the whole release,
with `release_data_integrity.sha1` alongside. So:

1. Fetch the `.sha1`, fetch the manifest, check the manifest against it. An unverified
   manifest cannot be used to verify anything else.
2. Check every downloaded part against the manifest.
3. Commit `manifests/<release>-sources.tsv` with upstream's SHA-1, our SHA-256, bytes
   and row counts; record `sha1(release_data_integrity)` as the release anchor in
   `release.json` and in VoID.

The anchor plays the role Reactome's Zenodo version DOI plays: one digest that
identifies the whole release.

Dataset **file names are discovered, not hardcoded**. Some datasets are Spark output
whose part names embed a per-release UUID, others a single `<name>.parquet`. The listing
is read from FTP and the bytes are then pinned by digest, which is what actually
identifies them.

`croissant.json` supplies the publication date and licence (CC0-1.0). `manifest.json`
is 21 MB of build logs, so only its top-level `result` is read, by streaming the first
chunk — see the trap in README.

## 3. Verified layouts

`verify_schemas` gates every release on the columns this ingest reads, not on every
column the dataset has: a column upstream adds is not a problem, one it removes is.
It also checks each controlled vocabulary against the release's actual values and the
disease-id prefixes against the IRI map.

This gate matters more here than for Reactome because **Open Targets reorganises
datasets between releases** — `clinical_indication` and `clinical_report` are a recent
split, and 26.06 still ships a `clinical_target` next to them. A run that discovers
such a change by producing an empty graph has wasted the run and can be mistaken for a
source with no data.

## 4. Target model

Biolink supplies the classes and predicates. [shared/rdf.py](../shared/rdf.py) defines
namespace bases. `sagebrain:` is the only local namespace; the 11 terms this ingest
mints are listed by acceptance check 11 until sagebrain-model ratifies them.

```turtle
CHEMBL:CHEMBL2103875
    a biolink:SmallMolecule ;
    rdfs:label "TRAMETINIB" ;
    sagebrain:drug_type "Small molecule" ;
    sagebrain:inchi_key "LIRYPHYGHXZJBZ-UHFFFAOYSA-N" ;
    sagebrain:overall_clinical_stage "APPROVAL" ;
    skos:altLabel "GSK1120212" , "MEKINIST" .

HGNC:6840  a biolink:Gene ; rdfs:label "MAP2K1" ; biolink:in_taxon NCBITaxon:9606 .

[] a biolink:ChemicalToGeneAssociation ;
   biolink:subject CHEMBL:CHEMBL2103875 ;
   biolink:object HGNC:6840 ;
   biolink:predicate biolink:affects ;
   sagebrain:action_type "INHIBITOR" ;
   sagebrain:mechanism_of_action "Dual specificity mitogen-activated protein kinase kinase 1 inhibitor" ;
   sagebrain:target_type "single protein" ;
   biolink:original_object "ENSEMBL:ENSG00000169032" ;
   biolink:primary_knowledge_source infores:open-targets .

[] a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation ;
   biolink:subject CHEMBL:CHEMBL1614701 ;
   biolink:object EFO:EFO_0000658 ;
   biolink:predicate biolink:treats_or_applied_or_studied_to_treat ;
   sagebrain:max_clinical_stage "APPROVAL" ;
   sagebrain:clinical_report_count 14 ;
   biolink:primary_knowledge_source infores:open-targets .
```

Both endpoints of every association are typed nodes, following Reactome: a consumer can
label a gene or a disease without reading the association table, and a query that joins
on one cannot silently match an identifier the ingest never asserted.

## 5. Modelling rules

1. **Genes resolve to HGNC.** Open Targets keys targets on Ensembl; Reactome and the
   NF-OSI graph key genes on HGNC. Resolving on the way in means one kind of gene node
   across the repo, and the source id is kept as `biolink:original_object` — the same
   treatment Reactome gives the UniProt accession. Measured at 26.06: 1,548 of 1,550
   distinct target ids resolve (99.9%). An Ensembl id mapping to several HGNC ids fans
   out. Above 10% unresolved the transform fails, matching Reactome's threshold.
2. **The indication predicate is the weak one.** `treats_or_applied_or_studied_to_treat`,
   because 87% of rows are below APPROVAL. Using `biolink:treats` would turn every
   abandoned trial into a therapeutic claim.
3. **Clinical stage lives on the edge.** `maxClinicalStage` is a maximum over one
   drug–disease pair's reports, so it is a property of that pair. The molecule-level
   maximum is emitted under a deliberately *different* name, `maximumClinicalStage`, so
   the two cannot be confused in a query.
4. **`targetType` rides on every mechanism edge.** ChEMBL targets are sometimes groups,
   and the member edges derived from a group are not separately measured interactions.
   Group edges outnumber single-protein edges roughly two to one, so a consumer that
   does not filter will over-count.
5. **Molecule classes are a shallow split.** `Small molecule` → `biolink:SmallMolecule`;
   the other ten modalities → `biolink:ChemicalEntity`, with `drugType` kept verbatim.
   Mapping antibodies, gene and cell therapies each onto a Biolink class would assert
   distinctions this ingest cannot check. `ChemicalEntity` is a loose fit for the 67
   `Cell` entries and the drugType says so.
6. **Disease nodes are typed by id prefix.** `clinical_indication` mixes ontologies:
   8,591 rows point at HP phenotype terms and 430 at MP mouse-phenotype terms, which are
   not diseases. GO and OBA terms take the union class rather than being forced either
   way. An unmapped prefix raises: a disease id turned into a guessed IRI would join to
   nothing and read as absent data.
7. **Nodes only for referenced terms.** The release describes 47,080 disease terms; the
   3,749 an indication points at get nodes. The rest are another source's job.
8. **Labels are the payload.** Every synonym and trade name becomes `skos:altLabel`, so
   name resolution is queryable, and the same index is exported as TSV for consumers
   that would rather join a table. `parentMolecule` edges are kept because a salt form
   and its parent share most labels.

## 6. Duplicates and ambiguity

`drug_mechanism_of_action` keys on a **list** of ChEMBL ids — 6,500 rows cover 5,820
molecules — and the release additionally contains genuinely duplicate rows (ribociclib's
CDK4 mechanism appears twice). Rows are fanned out per drug and edges deduplicated on
the full tuple; 689 duplicates were collapsed at 26.06 and the count is reported rather
than left to inflate the graph.

Label ambiguity is **recorded, not resolved**. 2,607 folded labels name more than one
molecule. The exported index gives one row per candidate with `ambiguous=yes`; the RDF
gives each molecule its own labels. Neither picks a winner, because the right resolution
depends on the consumer's field.

## 7. Acceptance criteria

Eleven checks: compounds typed and labelled; mechanism edges resolve to typed gene nodes;
indication edges resolve to typed disease or phenotype nodes; the three controlled
vocabularies as they appear *in the graph*; genes HGNC-keyed with exactly one taxon;
release size in range and in agreement with the `void:triples` the loader asserted;
a set of known facts; that the two clinical-stage slots never land on the same subject;
and the `sagebrain:` model-term review.

The known-facts check is this ingest's equivalent of Reactome's NF1 membership check —
eight anchors covering the mechanism and indication hops that downstream work depends
on, including selumetinib and mirdametinib at APPROVAL for plexiform neurofibroma. If
one disappears, either the release changed something real or this ingest broke, and both
are worth stopping for.

The model-term review is a warning, never a failure: the model repo and this ingest move
at different speeds, and so does the network.

## 8. Failure handling

Reject an unverifiable integrity manifest, a digest mismatch, a missing required column,
an unknown value in any controlled vocabulary, an unmapped disease-id prefix, an
unpinnable file present in the FTP listing but absent from the manifest, and target
resolution below the threshold. Keep reports and exports outside `rdf/`, so the loader
cannot pick them up.

## 9. Graphs and refresh

Oxigraph stores each release in `urn:sagebrain:opentargets:<release>`. Reloading replaces
only that graph. VoID lives in the default graph and carries the source URL, the release
anchor digest, the licence, the citation, the triple count and — as separate
`rdfs:comment` statements rather than one wall of text — the four caveats a consumer
cannot recover from the triples.

## 10. Open decisions

- **`clinical_report` projection.** Pinned and gated; the modelling is not done. Trial
  stop reasons are the interesting part: of the NF trials with a stated reason, the
  recurring one is slow accrual rather than toxicity or futility.
- **Structure-based resolution.** 18,697 molecules carry InChIKey and SMILES. A consumer
  with structures can resolve without agreeing on a name, which is the only route for the
  long tail of catalogue compounds whose stereochemistry and salt prefixes ChEMBL spells
  differently. Not built.
- **Cross-references.** `crossReferences` (PubChem, DrugBank, …) is read by nothing yet
  and so is deliberately absent from the layout gate's required columns.
