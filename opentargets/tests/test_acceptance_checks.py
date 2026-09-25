"""Checks that would report PASS whether or not they were looking.

A structural check failing loudly is self-evidencing; a check that never looks
at part of the graph reports exactly the same thing as one that looked and found
nothing. That class of bug is invisible in the acceptance report, so it is worth
a test against a real loaded graph rather than a mock.
"""

import tempfile
import unittest
from pathlib import Path

from shared.model_terms import counts_from_iris
from shared.oxigraph import GraphClient, load_oxigraph

from opentargets.acceptance_checks import model_term_iris
from opentargets.common import release_graph

TURTLE = """
@prefix biolink: <https://w3id.org/biolink/vocab/> .
@prefix sagebrain: <https://w3id.org/synapse/sagebrain#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<https://identifiers.org/clinicaltrials:NCT00000001>
    a biolink:ClinicalTrial ;
    rdfs:label "A trial" ;
    sagebrain:trial_clinical_stage "PHASE_2" .

<https://identifiers.org/chembl:CHEMBL1>
    a sagebrain:MintedClass ;
    sagebrain:drug_type "Small molecule" .

[] biolink:predicate sagebrain:minted_value .
"""

VOID = """
@prefix void: <http://rdfs.org/ns/void#> .
<urn:sagebrain:opentargets:test> a void:Dataset .
"""


class ModelTermScanTests(unittest.TestCase):
    """The scan must cover all three positions a model term can occupy."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        part, void = root / "part.ttl", root / "void.ttl"
        part.write_text(TURTLE)
        void.write_text(VOID)
        cls.graph = release_graph("test")
        load_oxigraph(root / "store", cls.graph, [part], void)
        cls.client = GraphClient(store=root / "store", graphs=[cls.graph])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def terms(self):
        return counts_from_iris(model_term_iris(self.client, self.graph))

    def test_a_term_in_predicate_position_is_reviewed(self):
        self.assertEqual(self.terms()["trial_clinical_stage"], 1)
        self.assertEqual(self.terms()["drug_type"], 1)

    def test_a_term_in_class_position_is_reviewed(self):
        """The gap this test exists for. Scanning only ?s ?p ?o would leave an
        undefined sagebrain: class emitted forever without ever reaching the
        report whose job is to name it -- and the report would read PASS.

        Counted once, not twice: class position is object position, since `a` is
        rdf:type, so a query that unions them separately inflates every class."""
        self.assertIn("MintedClass", self.terms())
        self.assertEqual(self.terms()["MintedClass"], 1)

    def test_a_term_in_object_position_is_reviewed(self):
        """Not hypothetical: biolink:affects and
        biolink:treats_or_applied_or_studied_to_treat occur in the real graph
        only as the object of biolink:predicate, so a scan of predicate and
        class position alone misses them."""
        self.assertEqual(self.terms()["minted_value"], 1)

    def test_terms_from_other_namespaces_are_not_reported(self):
        """biolink: is not ours to mint, so it is not this check's business.
        The namespace filter lives in counts_from_iris, not in the query."""
        self.assertNotIn("ClinicalTrial", self.terms())
        self.assertNotIn("label", self.terms())


if __name__ == "__main__":
    unittest.main()
