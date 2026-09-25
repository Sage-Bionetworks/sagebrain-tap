# Source dataset reference

These pages describe the pinned Open Targets release 26.06 inputs: column types,
coverage, controlled vocabularies, RDF mappings, and data limitations. Counts are
measured from the pinned files. Source row counts and emitted node or association counts
describe different stages of the pipeline.

| Dataset | Source rows | Projection | Notes |
|---|---:|---|---|
| [`drug_molecule`](drug_molecule.md) | 22,407 | Molecule nodes and label index | Some parent references have no corresponding molecule record |
| [`drug_mechanism_of_action`](drug_mechanism_of_action.md) | 6,500 | Mechanism associations and gene nodes | Source rows expand across drugs and target members |
| [`clinical_indication`](clinical_indication.md) | 86,468 | Indication associations | Unique drug–disease pairs with stage and report count |
| [`disease`](disease.md) | 47,080 | Referenced disease and phenotype nodes | Labels and synonyms for terms used by indications or trials |
| [`clinical_report`](clinical_report.md) | 289,955 | Clinical trial nodes | Trials naming a ChEMBL-resolved drug; start dates use month precision |

See [DESIGN.md](../DESIGN.md) for dataset selection and modeling decisions, and
[TRIALS.md](../TRIALS.md) for trial scope and interpretation.
