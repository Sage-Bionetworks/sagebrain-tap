# Source dataset notes

One page per Open Targets dataset this ingest downloads, describing the source as it
actually is at release 26.06: column layout and fill, controlled vocabularies, what
the transforms read and emit, and the quirks worth knowing before trusting a column.

Counts are measured against the pinned inputs, not quoted from upstream docs.

| Dataset | Rows | Projected | Notes |
|---|---:|---|---|
| [`drug_molecule`](drug_molecule.md) | 22,407 | yes | Molecule nodes and the label index; 211 parent references dangle |
| [`drug_mechanism_of_action`](drug_mechanism_of_action.md) | 6,500 | yes | No key column; group targets outnumber single-protein 9,506 to 5,202 |
| [`clinical_indication`](clinical_indication.md) | 86,468 | yes | The cleanest dataset here — every column filled, every pair unique |
| [`disease`](disease.md) | 47,080 | partly | Ontology dump; only 3,749 terms (8%) reach the graph, labels only |
| [`clinical_report`](clinical_report.md) | 289,955 | no | Gated, not projected; four record kinds, manufactured date precision |

For why the other 51 release directories are excluded, see the scope table in
[DESIGN.md](../DESIGN.md). For the decided-but-unbuilt trial layer over
`clinical_report`, see [TRIALS.md](../TRIALS.md).
