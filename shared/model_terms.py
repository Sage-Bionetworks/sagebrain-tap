"""Report which emitted `sagebrain:` terms the shared model actually defines.

There is one model namespace, so an ingest that needs a term the model has not
ratified yet mints it under `sagebrain:` anyway and this module names it. That is
the whole point: a second "ingest extensions" namespace made undefined terms easy
to emit and easy to forget, because nothing ever listed them. Here they show up
in the acceptance report every run, as a to-do for the model repo.

A WARNING, never a failure. The model repo and an ingest move at different
speeds, and a graph that is useful before the term is ratified is still useful --
what is not acceptable is not knowing. An unreachable model is a warning for the
same reason: better than passing quietly on no evidence.

Read from the published model on GitHub by default, not from a checkout. What
counts as ratified is what is on the default branch, and a local clone is
whatever someone last pulled -- checking against it can report a term as defined
because of an unpushed edit, which is the one answer nobody wants. Point
``--model-ttl`` at a file to check a working copy on purpose, when editing the
model or running offline."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .rdf import NAMESPACES

#: The model as published, on the default branch.
MODEL_URL = ("https://raw.githubusercontent.com/Sage-Bionetworks/sagebrain-model"
             "/main/ontology/main/sagebrain.ttl")

MODEL_TIMEOUT = 30

MODEL_BASE = NAMESPACES["sagebrain"]

#: A term written out in full, e.g. ``<https://w3id.org/synapse/sagebrain#has_sample>``.
IRI_TERM = re.compile(rf"<{re.escape(MODEL_BASE)}([A-Za-z_][A-Za-z_0-9]*)>")


def _prefixed_term(label: str) -> re.Pattern:
    """``label:local_name``, but not the label inside a URN.

    The lookbehind keeps ``urn:sagebrain:reactome:v97`` from reading as a term
    named ``reactome``.
    """
    return re.compile(rf"(?<![:\w#-]){re.escape(label)}:([A-Za-z_][A-Za-z_0-9]*)")


class ModelUnavailable(RuntimeError):
    """The model could not be read. Always a warning, never an ingest failure."""


@dataclass
class ModelTermReview:
    #: Where the model was read from: the URL, or the path of a working copy.
    source: str
    model_available: bool
    defined: set[str] = field(default_factory=set)
    emitted: Counter = field(default_factory=Counter)
    error: str | None = None

    @property
    def undefined(self) -> list[str]:
        return sorted(term for term in self.emitted if term not in self.defined)

    @property
    def detail(self) -> str:
        if not self.model_available:
            hint = (" -- point --model-ttl at a working copy to check offline"
                    if self.source.startswith("http") else "")
            return f"skipped -- could not read the model from {self.source} ({self.error}){hint}"
        where = "the published model" if self.source == MODEL_URL else self.source
        if not self.emitted:
            return "no sagebrain: terms in the output"
        if not self.undefined:
            return (f"all {len(self.emitted)} sagebrain: terms are defined in {where}")
        return (f"{len(self.undefined)} of {len(self.emitted)} sagebrain: terms are not "
                f"defined in {where}: {', '.join(self.undefined)}")

    @property
    def passed(self) -> bool:
        return self.model_available and not self.undefined

    def lines(self) -> list[str]:
        """Per-term counts, so the model repo can triage by how much rides on each."""
        if not self.undefined:
            return []
        return [f"sagebrain:{term} -- {self.emitted[term]:,} statements"
                for term in sorted(self.undefined, key=lambda t: (-self.emitted[t], t))]


def read_model(model_ttl: Path | str | None = MODEL_URL,
               timeout: int = MODEL_TIMEOUT) -> tuple[str, str]:
    """Return ``(source, turtle)`` for the model to check against.

    A URL is fetched, anything else is read from disk. Remote by default because
    "defined" means defined on the default branch. Any failure raises
    ModelUnavailable rather than falling back to a checkout: a silent fallback
    would answer a different question than the one asked, and the caller would
    have no way to tell which question was answered.
    """
    # None reaches here from a caller that forwards an unset CLI default; it
    # means "wherever the model lives", not a file called "None".
    source = str(model_ttl or MODEL_URL)
    if not source.startswith(("http://", "https://")):
        path = Path(source)
        if not path.exists():
            raise ModelUnavailable(f"no such file: {path}")
        return str(path), path.read_text(encoding="utf-8")

    try:
        request = urllib.request.Request(
            source, headers={"User-Agent": "sagebrain-tap-acceptance-checks"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, OSError) as error:
        raise ModelUnavailable(f"{type(error).__name__}: {error}") from error

    if MODEL_BASE not in body:
        # A 404 page or a redirect to HTML parses as zero definitions, which
        # would read as "the model defines nothing" -- indistinguishable from a
        # real emptied model, and far more likely.
        raise ModelUnavailable(f"response does not declare {MODEL_BASE}")
    return source, body


def defined_terms(turtle: str) -> set[str]:
    """Local names the model declares as subjects.

    A term counts as defined when the model makes a statement *about* it, which
    is why only subject position is read. Terms the model merely mentions --
    ``rdfs:range sagebrain:ElementCategory`` on some other term -- are not
    definitions, and treating them as such would let a typo validate itself.
    """
    defined: set[str] = set()
    for line in turtle.splitlines():
        if not line or line[0].isspace() or line.startswith(("#", "@")):
            continue
        subject = line.split()[0]
        if subject.startswith("sagebrain:"):
            defined.add(subject.split(":", 1)[1])
        elif subject.startswith(f"<{MODEL_BASE}"):
            defined.add(subject[1:-1].split("#", 1)[1])
    return defined


def emitted_terms(ttl_paths) -> Counter:
    """Count model-namespace terms in emitted Turtle.

    Resolved against each file's own ``@prefix`` lines, never by matching the
    token ``sagebrain:``. A file written before the two namespaces were merged
    binds that same label to a different base, so a label-matching scan would
    report its terms as model terms that need defining when they are nothing of
    the kind. The prefix a file uses is a fact about that file.
    """
    found: Counter = Counter()
    for path in sorted(Path(p) for p in ttl_paths):
        prefixed = None
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("@prefix"):
                    parts = stripped.split()
                    if len(parts) >= 3 and parts[2].strip("<>") == MODEL_BASE:
                        prefixed = _prefixed_term(parts[1].rstrip(":"))
                    continue
                if stripped.startswith("#"):
                    continue
                if prefixed is not None:
                    found.update(prefixed.findall(stripped))
                found.update(IRI_TERM.findall(stripped))
    return found


def term_from_iri(value: str) -> str | None:
    """Local name of a model IRI, or None for anything else.

    Takes full IRIs because that is what a SPARQL binding returns; checking the
    loaded graph rather than the Turtle on disk means the check covers what a
    consumer will actually query.
    """
    value = value.strip("<>")
    if value.startswith(MODEL_BASE) and len(value) > len(MODEL_BASE):
        return value[len(MODEL_BASE):]
    return None


def counts_from_iris(pairs) -> Counter:
    """``(iri, count)`` pairs -> counts keyed by model-term local name."""
    found: Counter = Counter()
    for value, count in pairs:
        term = term_from_iri(value)
        if term:
            found[term] += int(count)
    return found


def review(emitted: Counter,
           model_ttl: Path | str | None = MODEL_URL) -> ModelTermReview:
    """Compare already-counted terms against the model.

    Takes counts rather than a source so the caller decides where the output
    came from -- Turtle on disk (emitted_terms) or a loaded graph
    (counts_from_iris) -- without this module growing a SPARQL client.
    """
    try:
        source, turtle = read_model(model_ttl)
    except ModelUnavailable as error:
        return ModelTermReview(str(model_ttl or MODEL_URL), model_available=False,
                               emitted=emitted, error=str(error))
    return ModelTermReview(source, model_available=True,
                           defined=defined_terms(turtle), emitted=emitted)


def review_files(ttl_paths,
                 model_ttl: Path | str | None = MODEL_URL) -> ModelTermReview:
    return review(emitted_terms(ttl_paths), model_ttl)
