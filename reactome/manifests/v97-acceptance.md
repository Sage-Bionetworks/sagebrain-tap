# Reactome v97 -- acceptance checks

Graph: `<urn:sagebrain:reactome:v97>`

| Check | Status | Detail |
|---|---|---|
| 1. Species assertion (taxon == 9606) | **PASS** | zero rows, as required |
| 2. No non-R-HSA stable IDs | **PASS** | zero rows, as required |
| 3. Exactly one in_taxon per node | **PASS** | every entity node has exactly one |
| 4. Pathway count | **PASS** | 2,883 human pathways (no --expected-pathways given to compare against; check reactome.org/about/news for the release figure) |
| 5. Hierarchy is acyclic | **PASS** | 2,899 part_of edges, no cycles |
| 6. No dangling hierarchy endpoints | **PASS** | every part_of endpoint resolves to a pathway node |
| 7. Evidence codes are the mapped set | **PASS** | only IEA (ECO_0000501), TAS (ECO_0000304) |
| 8. NF1 spot check | **PASS** | 9 pathways, 7 parent/child pairs among them, RAF/MAP kinase cascade present; RAS/MAPK-related: 6 |
| 9. Content Service round-trip | **PASS** | R-HSA-3270619: 13 accessions, exact match; R-HSA-72731: 8 accessions, exact match; R-HSA-5678771: 1 accessions, exact match; R-HSA-156902: 90 accessions, exact match; R-HSA-9674404: 1 accessions, exact match |
| 10. Triple count in range | **PASS** | 1,179,747 triples |
| 11. Model terms are defined in sagebrain-model | **PASS** | no sagebrain: terms in the output |
