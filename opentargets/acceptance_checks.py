"""Structural acceptance checks for a loaded Open Targets release graph.

Reactome's contract, adapted: structural errors fail; the model-term review is a
warning. Each check is a SPARQL query against the loaded graph rather than a
re-read of the Turtle, so what is verified is what a consumer will actually query.

Checks 9 and 12 are this ingest's equivalent of Reactome's "NF1 across hierarchy
levels": a small set of facts a correct release must contain, chosen because they
are the ones downstream work depends on. If selumetinib stops being APPROVED for
plexiform neurofibroma, or NCT02407405 stops being a phase-2 selumetinib trial in
it, either the release changed something real or this ingest broke, and both are
worth stopping for.

Usage:
    python -m opentargets.acceptance_checks --release 26.06
    python -m opentargets.acceptance_checks --release 26.06 --endpoint http://localhost:7011/query
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from shared import model_terms
from shared.oxigraph import GraphClient

from .common import (
    ACTION_TYPES,
    CLINICAL_STAGES,
    DEFAULT_RELEASE,
    DRUG_TYPES,
    REPORT_QUALITY_CONTROLS,
    TARGET_TYPES,
    TRIAL_OVERALL_STATUSES,
    TRIAL_STOP_REASON_CATEGORIES,
    TRIAL_STOPPED_STATUSES,
    log,
    release_graph,
    trial_start_window,
)

PREFIXES = """
PREFIX biolink: <https://w3id.org/biolink/vocab/>
PREFIX sagebrain: <https://w3id.org/synapse/sagebrain#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX void: <http://rdfs.org/ns/void#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

#: Facts a correct 26.06 ingest must contain (check 9). Compound label, the
#: mechanism target it must reach, and an indication it must carry.
DEMO_ANCHORS = [
    ("trametinib", "MAP2K1", None, None),
    ("trametinib", "MAP2K2", "EFO_0000658", "PHASE_2"),
    ("tno155", "PTPN11", None, None),
    ("ribociclib", "CDK4", None, None),
    ("ribociclib", "CDK6", None, None),
    ("selumetinib", "MAP2K1", "EFO_0000658", "APPROVAL"),
    ("mirdametinib", "MAP2K2", "EFO_0000658", "APPROVAL"),
    ("selumetinib", "MAP2K2", "MONDO_0017827", "PHASE_2"),
]

#: Facts the trial layer must carry (check 12). Trial accession, its stage, overall
#: status, the month it started, a drug it tests and a disease it studies -- and for
#: the second, the stop-reason categories, because a maximum clinical stage is
#: exactly the thing that cannot record that a phase-1 trial halted on toxicity.
TRIAL_ANCHORS = [
    ("NCT02407405", "PHASE_2", "ACTIVE_NOT_RECRUITING", "2016-01", "CHEMBL1614701",
     "EFO_0000658", ()),
    ("NCT01160926", "PHASE_1", "TERMINATED", "2010-07", "CHEMBL1614701",
     "MONDO_0006519", ("Negative", "Safety_Sideeffects")),
]

#: Roughly half to twice the 26.06 release, which is 2.45M triples: 1.0M for the
#: molecule/mechanism/indication layer and 1.45M for trials. The lower bound is
#: what catches a truncated trials.ttl, which load_graph cannot catch for itself
#: -- it verifies the parts exist, not that they are whole.
TRIPLE_RANGE = (1_500_000, 5_000_000)


@dataclass
class Check:
    number: int
    name: str
    passed: bool
    detail: str
    fatal: bool = True
    lines: list[str] = field(default_factory=list)


def _one(client: GraphClient, query: str) -> dict:
    rows = client.select(PREFIXES + query)["results"]["bindings"]
    return rows[0] if rows else {}


def _values(client: GraphClient, query: str, variable: str) -> list[str]:
    return [row[variable]["value"]
            for row in client.select(PREFIXES + query)["results"]["bindings"]
            if variable in row]


def _count(client: GraphClient, graph: str, where: str) -> int:
    row = _one(client, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ {where} }} }}")
    return int(row["n"]["value"]) if row else 0


def _distinct(client: GraphClient, graph: str, variable: str, where: str) -> int:
    row = _one(client, f"SELECT (COUNT(DISTINCT ?{variable}) AS ?n) "
                       f"WHERE {{ GRAPH <{graph}> {{ {where} }} }}")
    return int(row["n"]["value"]) if row else 0


def model_term_iris(client: GraphClient, graph: str) -> list[tuple[str, str]]:
    """Every IRI in the graph that could be a model term, with its count.

    Both positions such a term can occupy, matching Reactome's equivalent check.
    Scanning only predicates would leave a class -- ``?s a sagebrain:Foo`` --
    unreviewed, and that is the one place an undefined term could be emitted
    forever without appearing in the report whose whole job is to name it. No
    ``sagebrain:`` class is emitted at 26.06; the point is that one would be
    caught the run it appeared.

    Returns every IRI, not only ``sagebrain:`` ones -- ``counts_from_iris``
    applies the namespace filter, and duplicating it here would be a second
    place for the two to disagree.

    Split out of ``run_checks`` so the query itself is testable: the bug this
    replaced was invisible in the report, because a check that never looks at a
    position reports the same PASS whether or not anything is wrong there.
    """
    return [(row["term"]["value"], row["n"]["value"]) for row in client.select(
        PREFIXES + f"""SELECT ?term (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{
          {{ ?s ?term ?o }} UNION {{ ?s a ?term }}
        }} }} GROUP BY ?term""")["results"]["bindings"]]


def run_checks(client: GraphClient, graph: str, release: str,
               default_client: GraphClient | None = None) -> list[Check]:
    checks: list[Check] = []

    # 1 -- every compound is typed and labelled.
    unlabelled = _count(client, graph, """
        ?c sagebrain:drug_type ?t . FILTER NOT EXISTS { ?c rdfs:label ?l }""")
    untyped = _count(client, graph, """
        ?c sagebrain:drug_type ?t . FILTER NOT EXISTS { ?c a ?class }""")
    compounds = _distinct(client, graph, "c", "?c sagebrain:drug_type ?t")
    checks.append(Check(1, "Compounds typed and labelled",
                        unlabelled == 0 and untyped == 0,
                        f"{compounds:,} compounds; {unlabelled} unlabelled, {untyped} untyped"))

    # 2 -- no mechanism edge points at a gene the ingest did not assert as a node.
    dangling = _count(client, graph, """
        ?a a biolink:ChemicalToGeneAssociation ; biolink:object ?g .
        FILTER NOT EXISTS { ?g a biolink:Gene }""")
    edges = _count(client, graph, "?a a biolink:ChemicalToGeneAssociation")
    checks.append(Check(2, "Mechanism edges resolve to gene nodes", dangling == 0,
                        f"{edges:,} mechanism edges, {dangling} with no typed gene node"))

    # 3 -- same for the disease side.
    dangling_disease = _count(client, graph, """
        ?a a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation ; biolink:object ?d .
        FILTER NOT EXISTS {
          { ?d a biolink:Disease } UNION { ?d a biolink:PhenotypicFeature }
          UNION { ?d a biolink:DiseaseOrPhenotypicFeature } }""")
    indications = _count(
        client, graph, "?a a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation")
    checks.append(Check(3, "Indication edges resolve to disease/phenotype nodes",
                        dangling_disease == 0,
                        f"{indications:,} indication edges, {dangling_disease} with no "
                        f"typed node"))

    # 4-6 -- controlled vocabularies, verified in the GRAPH rather than the source.
    for number, name, predicate, allowed, constant in (
        (4, "Clinical stages", "sagebrain:max_clinical_stage",
         CLINICAL_STAGES, "CLINICAL_STAGES"),
        (5, "Action types", "sagebrain:action_type", ACTION_TYPES, "ACTION_TYPES"),
        (6, "Target types", "sagebrain:target_type", TARGET_TYPES, "TARGET_TYPES"),
    ):
        observed = set(_values(client, f"""
            SELECT DISTINCT ?v WHERE {{ GRAPH <{graph}> {{ ?s {predicate} ?v }} }}""", "v"))
        unknown = sorted(observed - allowed)
        checks.append(Check(number, name, not unknown,
                            f"{len(observed)} value(s) in the graph, all in {constant}"
                            if not unknown else
                            f"not in {constant}: {', '.join(unknown)}"))

    # 7 -- genes are HGNC-keyed and carry exactly one taxon.
    non_hgnc = _count(client, graph, """
        ?g a biolink:Gene .
        FILTER(!STRSTARTS(STR(?g), "https://identifiers.org/hgnc:"))""")
    multi_taxon = len(_values(client, f"""
        SELECT ?g WHERE {{ GRAPH <{graph}> {{ ?g a biolink:Gene ; biolink:in_taxon ?t }} }}
        GROUP BY ?g HAVING(COUNT(DISTINCT ?t) != 1)""", "g"))
    genes = _distinct(client, graph, "g", "?g a biolink:Gene")
    checks.append(Check(7, "Genes HGNC-keyed with one taxon",
                        non_hgnc == 0 and multi_taxon == 0,
                        f"{genes:,} gene nodes; {non_hgnc} not HGNC-keyed, "
                        f"{multi_taxon} without exactly one taxon"))

    # 8 -- graph size, and the loader's own void:triples assertion agrees with it.
    total = _count(client, graph, "?s ?p ?o")
    # VoID is in the DEFAULT graph, while `client` is scoped to the release graph,
    # so asking `client` for it silently returns nothing and the comparison below
    # degrades to "not checked" while still reporting PASS. Use an unscoped client.
    void_client = default_client or client
    declared_rows = void_client.select(PREFIXES + f"""
        SELECT ?n WHERE {{ <{graph}> void:triples ?n }}""")["results"]["bindings"]
    declared = int(declared_rows[0]["n"]["value"]) if declared_rows else None
    low, high = TRIPLE_RANGE
    size_ok = low <= total <= high
    # The counts are compared but a mismatch is not fatal: void:triples is counted
    # from the Turtle by line, the graph count is after RDF deduplication, so they
    # can differ legitimately when a transform emits the same triple twice.
    agrees = declared is None or abs(declared - total) <= total * 0.01
    checks.append(Check(8, "Release size", size_ok and agrees,
                        f"{total:,} triples (expected {low:,}-{high:,}); "
                        f"void:triples asserts {declared:,}"
                        f"{'' if agrees else ' -- DIFFERS by more than 1%'}"
                        if declared is not None else f"{total:,} triples, no void:triples"))

    # 9 -- the facts downstream work depends on.
    missing: list[str] = []
    for label, symbol, disease, stage in DEMO_ANCHORS:
        found = _count(client, graph, f"""
            ?c skos:altLabel|rdfs:label ?l . FILTER(LCASE(STR(?l)) = "{label}")
            ?m a biolink:ChemicalToGeneAssociation ;
               biolink:subject ?c ; biolink:object ?g .
            ?g rdfs:label "{symbol}" .""")
        if not found:
            missing.append(f"{label} -> {symbol} (mechanism)")
        if disease:
            iri = ("http://www.ebi.ac.uk/efo/" if disease.startswith("EFO")
                   else "http://purl.obolibrary.org/obo/") + disease
            found = _count(client, graph, f"""
                ?c skos:altLabel|rdfs:label ?l . FILTER(LCASE(STR(?l)) = "{label}")
                ?i biolink:subject ?c ; biolink:object <{iri}> ;
                   sagebrain:max_clinical_stage "{stage}" .""")
            if not found:
                missing.append(f"{label} -> {disease} at {stage} (indication)")
    checks.append(Check(9, "Known facts present", not missing,
                        f"all {len(DEMO_ANCHORS)} anchors present" if not missing
                        else f"{len(missing)} missing", lines=missing))

    # 10 -- the THREE clinical-stage slots must never land on the same subject.
    # They are different facts -- one per drug-disease edge, one per molecule, one
    # per trial -- and keeping them apart is the whole reason they are named
    # differently. A subject carrying two would mean a transform started
    # conflating them, which is the failure mode that produces a plausible wrong
    # answer rather than an obvious one.
    STAGE_SLOTS = (
        ("sagebrain:max_clinical_stage", "an indication edge",
         "?s a biolink:ChemicalToDiseaseOrPhenotypicFeatureAssociation"),
        ("sagebrain:overall_clinical_stage", "a compound", "?s sagebrain:drug_type ?d"),
        ("sagebrain:trial_clinical_stage", "a trial", "?s a biolink:ClinicalTrial"),
    )
    shared_subjects = 0
    for index, (slot, _, _) in enumerate(STAGE_SLOTS):
        for other, _, _ in STAGE_SLOTS[index + 1:]:
            shared_subjects += _count(client, graph,
                                      f"?s {slot} ?a ; {other} ?b .")
    misplaced = []
    for slot, belongs_on, typing in STAGE_SLOTS:
        stray = _count(client, graph,
                       f"?s {slot} ?v . FILTER NOT EXISTS {{ {typing} }}")
        if stray:
            misplaced.append(f"{stray:,} {slot} value(s) off {belongs_on}")
    checks.append(Check(10, "Clinical-stage slots kept apart",
                        shared_subjects == 0 and not misplaced,
                        f"{shared_subjects} subject(s) carry more than one of the "
                        f"three slots; " + ("; ".join(misplaced) if misplaced
                                            else "each slot only on its own subjects")))

    # 11 -- trial nodes are registry-keyed, typed and staged.
    #
    # Keyed on the accession rather than on anything this ingest mints, for the
    # same reason genes are HGNC-keyed: a trial node has to be the same node next
    # release, and an NCT number is the only identifier in clinical_report that is.
    trials = _distinct(client, graph, "t", "?t a biolink:ClinicalTrial")
    non_registry = _count(client, graph, """
        ?t a biolink:ClinicalTrial .
        FILTER(!STRSTARTS(STR(?t), "https://identifiers.org/clinicaltrials:NCT"))""")
    unstaged = _count(client, graph, """
        ?t a biolink:ClinicalTrial .
        FILTER NOT EXISTS { ?t sagebrain:trial_clinical_stage ?s }""")
    # Reported, not required: 3,282 in-scope trials carry no registry title, and
    # the transform refuses to substitute the release's generated stand-in.
    unlabelled_trials = _count(client, graph, """
        ?t a biolink:ClinicalTrial . FILTER NOT EXISTS { ?t rdfs:label ?l }""")
    checks.append(Check(11, "Trials registry-keyed and staged",
                        non_registry == 0 and unstaged == 0,
                        f"{trials:,} trial nodes; {non_registry} not NCT-keyed, "
                        f"{unstaged} without a stage, {unlabelled_trials:,} without a "
                        f"registry title (expected -- not substituted)"))

    # 12 -- the trial facts downstream work depends on, the counterpart of check 9.
    missing_trials: list[str] = []
    for accession, stage, status, start, drug, disease, categories in TRIAL_ANCHORS:
        trial = f"<https://identifiers.org/clinicaltrials:{accession}>"
        disease_iri = ("http://www.ebi.ac.uk/efo/" if disease.startswith("EFO")
                       else "http://purl.obolibrary.org/obo/") + disease
        required = [
            ("stage", f'{trial} sagebrain:trial_clinical_stage "{stage}" .'),
            ("status",
             f'{trial} biolink:clinical_trial_overall_status "{status}" .'),
            ("start", f'{trial} sagebrain:trial_start_date '
                      f'"{start}"^^xsd:gYearMonth .'),
            ("drug", f'{trial} sagebrain:trial_drug '
                     f'<https://identifiers.org/chembl:{drug}> .'),
            ("disease", f'{trial} biolink:clinical_trial_conditions <{disease_iri}> .'),
        ]
        required += [(f"stop reason {category}",
                      f'{trial} sagebrain:trial_stop_reason_category "{category}" .')
                     for category in categories]
        for what, where in required:
            if not _count(client, graph, where):
                missing_trials.append(f"{accession}: {what}")
    checks.append(Check(12, "Known trial facts present", not missing_trials,
                        f"all {len(TRIAL_ANCHORS)} trial anchors complete"
                        if not missing_trials else f"{len(missing_trials)} missing",
                        lines=missing_trials))

    # 13 -- a trial's drug and disease links must reach nodes this ingest asserted.
    # This is what verifies the disease pass really widened: 310 terms reach the
    # graph only because a trial names them, and indications.ttl emits none of them.
    dangling_drugs = _count(client, graph, """
        ?t sagebrain:trial_drug ?c . FILTER NOT EXISTS { ?c sagebrain:drug_type ?d }""")
    dangling_conditions = _count(client, graph, """
        ?t biolink:clinical_trial_conditions ?d .
        FILTER NOT EXISTS {
          { ?d a biolink:Disease } UNION { ?d a biolink:PhenotypicFeature }
          UNION { ?d a biolink:DiseaseOrPhenotypicFeature } }""")
    drug_links = _count(client, graph, "?t sagebrain:trial_drug ?c")
    condition_links = _count(client, graph, "?t biolink:clinical_trial_conditions ?d")
    checks.append(Check(13, "Trial links resolve to compound and disease nodes",
                        dangling_drugs == 0 and dangling_conditions == 0,
                        f"{drug_links:,} trial-drug and {condition_links:,} "
                        f"trial-condition links; {dangling_drugs} and "
                        f"{dangling_conditions} dangling"))

    # 14 -- the trial layer's own vocabularies, verified in the graph.
    for number, name, predicate, allowed, constant in (
        (14, "Trial stages", "sagebrain:trial_clinical_stage",
         CLINICAL_STAGES, "CLINICAL_STAGES"),
        (15, "Trial stop-reason categories", "sagebrain:trial_stop_reason_category",
         TRIAL_STOP_REASON_CATEGORIES, "TRIAL_STOP_REASON_CATEGORIES"),
        (16, "Report quality controls", "sagebrain:trial_quality_control",
         REPORT_QUALITY_CONTROLS, "REPORT_QUALITY_CONTROLS"),
        (17, "Trial overall statuses", "biolink:clinical_trial_overall_status",
         TRIAL_OVERALL_STATUSES, "TRIAL_OVERALL_STATUSES"),
    ):
        observed = set(_values(client, f"""
            SELECT DISTINCT ?v WHERE {{ GRAPH <{graph}> {{ ?s {predicate} ?v }} }}""", "v"))
        unknown = sorted(observed - allowed)
        checks.append(Check(number, name, not unknown,
                            f"{len(observed)} value(s) in the graph, all in {constant}"
                            if not unknown else
                            f"not in {constant}: {', '.join(unknown)}"))

    # 18 -- start dates carry month precision and a plausible year.
    #
    # Two separate claims, checked separately because they fail separately. The
    # datatype is the precision claim: an xsd:date here would assert a day the
    # release manufactured. The year window is the range claim: it isolates
    # placeholders like 2099-01-01 without discarding real planned starts.
    earliest, latest = trial_start_window(release)
    wrong_datatype = _count(client, graph, """
        ?t sagebrain:trial_start_date ?d .
        FILTER(DATATYPE(?d) != xsd:gYearMonth)""")
    out_of_range = _count(client, graph, f"""
        ?t sagebrain:trial_start_date ?d .
        BIND(xsd:integer(SUBSTR(STR(?d), 1, 4)) AS ?year)
        FILTER(?year < {earliest} || ?year > {latest})""")
    dated = _count(client, graph, "?t sagebrain:trial_start_date ?d")
    checks.append(Check(18, "Trial start dates are gYearMonth in range",
                        wrong_datatype == 0 and out_of_range == 0,
                        f"{dated:,} start dates; {wrong_datatype} not gYearMonth, "
                        f"{out_of_range} outside {earliest}-{latest}"))

    # 19 -- a stop reason only makes sense on a trial that stopped.
    #
    # Status and stop reason are separate columns, and this is the claim that
    # ties them: at 26.06 all 21,876 stop reasons sit on TERMINATED, WITHDRAWN or
    # SUSPENDED. A stop reason on a COMPLETED trial would mean the two had come
    # apart upstream and the free text was describing something other than a stop,
    # which nothing downstream would notice on its own.
    allowed = ", ".join(f'"{status}"' for status in sorted(TRIAL_STOPPED_STATUSES))
    stray_reasons = _count(client, graph, f"""
        ?t sagebrain:trial_stop_reason ?r ;
           biolink:clinical_trial_overall_status ?status .
        FILTER(?status NOT IN ({allowed}))""")
    # Every trial carries exactly one status; more than one would mean a row was
    # emitted twice under the same accession.
    multi_status = len(_values(client, f"""
        SELECT ?t WHERE {{ GRAPH <{graph}> {{
          ?t a biolink:ClinicalTrial ; biolink:clinical_trial_overall_status ?s }} }}
        GROUP BY ?t HAVING(COUNT(DISTINCT ?s) != 1)""", "t"))
    stopped = _count(client, graph, "?t sagebrain:trial_stop_reason ?r")
    checks.append(Check(19, "Stop reasons only on stopped trials",
                        stray_reasons == 0 and multi_status == 0,
                        f"{stopped:,} stop reasons; {stray_reasons} on a status "
                        f"outside {'/'.join(sorted(TRIAL_STOPPED_STATUSES))}, "
                        f"{multi_status} trial(s) without exactly one status"))

    # 20 -- model terms. A warning: the model repo and this ingest move at
    # different speeds, and so does the network.
    review = model_terms.review(model_terms.counts_from_iris(
        model_term_iris(client, graph)))
    checks.append(Check(20, "Model terms defined in sagebrain-model", review.passed,
                        review.detail, fatal=False, lines=review.lines()))
    return checks


def render(release: str, checks: list[Check]) -> str:
    failures = [c for c in checks if not c.passed and c.fatal]
    lines = [f"# Open Targets {release} acceptance report", "",
             "Generated by `python -m opentargets.acceptance_checks`.", "",
             "| # | Check | Result | Detail |", "| --- | --- | --- | --- |"]
    for check in checks:
        mark = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        lines.append(f"| {check.number} | {check.name} | {mark} | {check.detail} |")
    lines.append("")
    for check in checks:
        if check.lines:
            lines += [f"### {check.number}. {check.name}", ""]
            lines += [f"- {line}" for line in check.lines]
            lines.append("")
    lines += ["## Result", "",
              "**FAILED**" if failures else "**PASSED** — no structural check failed.", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=DEFAULT_RELEASE)
    parser.add_argument("--store", type=Path, default=None,
                        help="Local Oxigraph store (default: opentargets/<release>/data/store)")
    parser.add_argument("--endpoint", default=None, help="SPARQL query URL instead")
    parser.add_argument("--manifest-dir", type=Path, default=Path("opentargets/manifests"))
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    graph = release_graph(args.release)
    store = args.store or Path("opentargets") / args.release / "data" / "store"
    client = GraphClient(store=None if args.endpoint else store,
                         endpoint=args.endpoint, graphs=[graph])
    # Unscoped, for the default graph where VoID lives.
    default_client = GraphClient(store=None if args.endpoint else store,
                                 endpoint=args.endpoint)

    log(f"Acceptance checks for <{graph}>")
    checks = run_checks(client, graph, args.release, default_client)
    for check in checks:
        mark = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        log(f"  {check.number:>2}. [{mark}] {check.name}: {check.detail}")
        for line in check.lines:
            log(f"        {line}")

    report_path = args.report or args.manifest_dir / f"{args.release}-acceptance.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render(args.release, checks), encoding="utf-8")
    log(f"Wrote {report_path}")

    failures = [c for c in checks if not c.passed and c.fatal]
    if failures:
        log(f"\n{len(failures)} structural check(s) FAILED.")
        return 1
    log("\nAcceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
