"""Report which emitted `biolink:` terms the pinned Biolink release defines.

The mirror of `model_terms.py`, and deliberately the opposite verdict. A
`sagebrain:` term an ingest needs before the model has ratified it is a to-do
for the model repo, so that check warns. A `biolink:` term is not ours to mint:
Biolink is a published vocabulary on a version this repo pins, so a term it does
not define is a typo or a term that moved between releases, and the graph is
wrong now. That fails.

Not failing on the network, though. Biolink unreachable is a warning for the
same reason an unreachable sagebrain-model is: better than passing quietly on no
evidence, and not a reason to call a graph broken.

What "defined" means here is every class, slot, enum and type name the schema
declares, converted to the local name LinkML would generate -- CamelCase for
classes, enums and types, underscores for slots. Names are taken from the
schema rather than from a generated artifact because the generated OWL is
lossy in both directions: it drops the `deprecated` flags this check reports,
and it carries terms the schema does not. `slot_uri` is deliberately ignored --
`subject` declares `slot_uri: rdf:subject` and LinkML still emits
`biolink:subject` as a property, so honouring it would fail a term Biolink has.

Read from the pinned release tag on GitHub, and the pin is read from the
ingest's own LinkML schema (`settings.biolink_version`) rather than restated
here. There is one pin per ingest and this is a check that it holds; a second
copy of the number could drift from the one the schema advertises, and then the
check would be verifying a claim nobody made. Point ``--biolink-yaml`` at a file
to check against a working copy, when editing the model or running offline.

Usage is through `review`, which takes counted terms the way `model_terms` does,
so the caller decides whether they came from a loaded graph or from Turtle."""

from __future__ import annotations

import difflib
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .rdf import NAMESPACES

#: Raw file base for a release tag. `v` is prepended to the pinned number.
BIOLINK_REPO = "https://raw.githubusercontent.com/biolink/biolink-model"

#: The schema root. Its `imports:` are followed for anything unprefixed --
#: `attributes` supplies 29 slots (`biolink:stringdb_combined_score` and
#: friends) that live in a sibling file, and treating them as undefined would
#: fail a graph for using a term Biolink has.
ROOT_SCHEMA = "biolink-model.yaml"

BIOLINK_TIMEOUT = 30

BIOLINK_BASE = NAMESPACES["biolink"]

#: Sections whose names become terms, and how LinkML renders each one's name.
#: `types` are in here for the reason a fatal check should always take the
#: permissive side of a judgement call: nothing in this repo emits
#: `biolink:LabelType`, but failing a release over one would be worse than
#: missing it.
SECTIONS = {"classes": "class", "enums": "enum", "types": "type", "slots": "slot"}


class BiolinkUnavailable(RuntimeError):
    """Biolink could not be read. Always a warning, never an ingest failure."""


def camelcase(name: str) -> str:
    """`chemical entity` -> `ChemicalEntity`, as LinkML generates it.

    Reproduced rather than imported: linkml-runtime pulls in a large dependency
    tree for two string functions, and this repo's other schema tooling is
    stdlib. Verified to agree with `linkml_runtime.utils.formatutils` on all 930
    names in Biolink 4.4.4, including the ones that make a naive `.title()`
    wrong -- `microRNA` -> `MicroRNA`, `RNA product` -> `RNAProduct`.
    """
    words = re.sub(r"_+", " ", name.strip().replace(",", "")).split()
    return "".join(word[0].upper() + word[1:] for word in words)


def underscore(name: str) -> str:
    """`treats or applied or studied to treat` -> the slot's local name."""
    return re.sub(r"\s+", "_", name.strip()).replace(",", "").replace("-", "_")


@dataclass
class BiolinkModel:
    #: Where it was read from: the release-tag URL, or the path of a working copy.
    source: str
    #: The version the schema declares, which need not be the one asked for.
    declared_version: str | None
    #: Local name -> the section it came from.
    defined: dict[str, str] = field(default_factory=dict)
    #: Local name -> whatever the schema put in `deprecated:`.
    deprecated: dict[str, str] = field(default_factory=dict)

    def suggest(self, term: str) -> list[str]:
        """Near names, restricted to the ones shaped like what was emitted.

        A misspelled class is never a slot, and offering `treats` for
        `ChemicalToGeneAssociation` would be noise in the one place the report
        has to be actionable.
        """
        wanted = "slot" if term[:1].islower() else "class"
        pool = [name for name, kind in self.defined.items()
                if (kind == "slot") == (wanted == "slot")]
        return difflib.get_close_matches(term, pool, n=3, cutoff=0.6)


def pinned_version(schema_path: Path | str) -> str:
    """`settings.biolink_version` from an ingest's LinkML schema.

    An unreadable or unpinned schema raises BiolinkUnavailable like any other
    read failure, so a caller that cannot find the pin gets a warning naming
    the file rather than a traceback out of the middle of a check run.
    """
    try:
        text = Path(schema_path).read_text(encoding="utf-8")
    except OSError as error:
        raise BiolinkUnavailable(f"cannot read {schema_path}: {error}") from error
    document = _parse(text, str(schema_path))
    version = (document.get("settings") or {}).get("biolink_version")
    if not version:
        raise BiolinkUnavailable(f"no settings.biolink_version in {schema_path}")
    return str(version)


def schema_url(version: str, name: str = ROOT_SCHEMA) -> str:
    return f"{BIOLINK_REPO}/v{version.lstrip('v')}/{name}"


def _parse(text: str, where: str) -> dict:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover
        raise BiolinkUnavailable(
            "PyYAML is required to read the Biolink schema -- pip install PyYAML"
        ) from error
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise BiolinkUnavailable(f"{where} is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        # A 404 page from raw.githubusercontent is the literal string
        # "404: Not Found", which parses as a str and would otherwise read as a
        # schema defining nothing -- indistinguishable from a real empty model.
        raise BiolinkUnavailable(f"{where} did not parse as a YAML mapping")
    return document


def _read(location: str, timeout: int) -> str:
    if not location.startswith(("http://", "https://")):
        path = Path(location)
        if not path.exists():
            raise BiolinkUnavailable(f"no such file: {path}")
        return path.read_text(encoding="utf-8")
    try:
        request = urllib.request.Request(
            location, headers={"User-Agent": "sagebrain-tap-acceptance-checks"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, OSError) as error:
        raise BiolinkUnavailable(f"{type(error).__name__}: {error}") from error


def _sibling(location: str, name: str) -> str:
    """Where an unprefixed import lives, relative to the document importing it."""
    if location.startswith(("http://", "https://")):
        return location.rsplit("/", 1)[0] + f"/{name}.yaml"
    return str(Path(location).parent / f"{name}.yaml")


def _collect(document: dict, model: BiolinkModel) -> None:
    for section, kind in SECTIONS.items():
        for name, body in (document.get(section) or {}).items():
            term = underscore(name) if kind == "slot" else camelcase(name)
            model.defined[term] = kind
            if not isinstance(body, dict):
                continue
            flag = body.get("deprecated")
            # LinkML writes the flag as the string "true"; some schemas put a
            # migration note there instead, and that note is worth carrying.
            if flag is not None and str(flag).strip().lower() not in ("", "false"):
                model.deprecated[term] = str(flag)


def load_model(version: str | None = None, schema_yaml: str | Path | None = None,
               timeout: int = BIOLINK_TIMEOUT) -> BiolinkModel:
    """Read the pinned Biolink schema, following its unprefixed imports.

    `schema_yaml` overrides the version entirely: it names a file to read, for a
    working copy or an offline run. Prefixed imports (`linkml:types`) are left
    alone -- they define LinkML's own types, none of which are biolink: terms.
    """
    if schema_yaml is None and not version:
        raise BiolinkUnavailable("no Biolink version to check against")
    root = str(schema_yaml) if schema_yaml is not None else schema_url(version)

    model = BiolinkModel(source=root, declared_version=None)
    pending, seen = [root], set()
    while pending:
        location = pending.pop(0)
        if location in seen:
            continue
        seen.add(location)
        document = _parse(_read(location, timeout), location)
        if location == root:
            if not (document.get("classes") or {}):
                # attributes.yaml is `default_prefix: biolink` too, and pointing
                # --biolink-yaml at it would yield 29 slots and fail every class
                # in the graph. The root of the Biolink schema defines classes.
                raise BiolinkUnavailable(
                    f"{root} declares no classes -- is it the Biolink schema root?")
            declared = document.get("version")
            model.declared_version = str(declared) if declared else None
        _collect(document, model)
        for name in document.get("imports") or []:
            if ":" not in str(name):
                pending.append(_sibling(location, str(name)))

    if not model.defined:
        raise BiolinkUnavailable(f"{root} declares no classes, slots, enums or types")
    return model


#: Body of the scan that finds every Biolink term a graph actually uses.
#:
#: Object position is not optional, and it is the reason this is a constant
#: rather than a line each caller writes. These ingests reify: the predicate of
#: an association is the OBJECT of `biolink:predicate`, so a predicate-and-
#: rdf:type scan misses `biolink:affects` and
#: `biolink:treats_or_applied_or_studied_to_treat` entirely -- the two terms in
#: the Open Targets graph whose choice is most argued over, and so the two least
#: affordable to leave unchecked. It costs about 2s more on a 2.6M-triple
#: graph, which is the cheapest 2s in the suite.
#:
#: Subject position is left out deliberately: it adds another 1.8s and finds
#: nothing, because an ingest emits statements USING the vocabulary, not
#: statements ABOUT it.
#:
#: `?t` is the term. Wrap in `GRAPH <...> { ... }` and group by it.
TERM_SCAN = "{ ?s ?t ?o } UNION { ?s ?p ?t }"


def term_from_iri(value: str) -> str | None:
    """Local name of a Biolink IRI, or None for anything else."""
    value = value.strip("<>")
    if value.startswith(BIOLINK_BASE) and len(value) > len(BIOLINK_BASE):
        return value[len(BIOLINK_BASE):]
    return None


def counts_from_iris(pairs) -> Counter:
    """``(iri, count)`` pairs -> counts keyed by Biolink local name."""
    found: Counter = Counter()
    for value, count in pairs:
        term = term_from_iri(value)
        if term:
            found[term] += int(count)
    return found


@dataclass
class BiolinkTermReview:
    #: The version the check was asked for, from the ingest's schema pin.
    version: str | None
    source: str
    model_available: bool
    model: BiolinkModel | None = None
    emitted: Counter = field(default_factory=Counter)
    error: str | None = None

    @property
    def undefined(self) -> list[str]:
        if self.model is None:
            return []
        return sorted(term for term in self.emitted if term not in self.model.defined)

    @property
    def deprecated_used(self) -> list[str]:
        if self.model is None:
            return []
        return sorted(term for term in self.emitted if term in self.model.deprecated)

    @property
    def version_mismatch(self) -> str | None:
        """Set when the schema read declares a version other than the pin."""
        if self.model is None or self.version is None:
            return None
        declared = self.model.declared_version
        if declared and declared.lstrip("v") != self.version.lstrip("v"):
            return declared
        return None

    @property
    def passed(self) -> bool:
        return self.model_available and not self.undefined

    @property
    def fatal(self) -> bool:
        """An undefined term fails; not having read Biolink cannot.

        The caller hands this straight to its check, so an unreachable Biolink
        comes out as a warning and a real typo comes out as a failure, without
        the caller having to know the difference.
        """
        return self.model_available

    @property
    def detail(self) -> str:
        if not self.model_available:
            hint = (" -- point --biolink-yaml at a working copy to check offline"
                    if self.source.startswith("http") else "")
            return f"skipped -- could not read Biolink from {self.source} ({self.error}){hint}"
        where = f"Biolink {self.version}" if self.version else self.source
        notes = []
        if self.version_mismatch:
            notes.append(f"schema read declares {self.version_mismatch}, not {self.version}")
        if self.deprecated_used:
            notes.append(f"{len(self.deprecated_used)} deprecated")
        suffix = f" ({'; '.join(notes)})" if notes else ""
        if not self.emitted:
            return f"no biolink: terms in the output{suffix}"
        if not self.undefined:
            return (f"all {len(self.emitted)} biolink: terms are defined "
                    f"in {where}{suffix}")
        return (f"{len(self.undefined)} of {len(self.emitted)} biolink: terms are not "
                f"defined in {where}: {', '.join(self.undefined)}{suffix}")

    def lines(self) -> list[str]:
        """Per-term counts and the nearest real names, so the report is actionable."""
        out = []
        for term in sorted(self.undefined, key=lambda t: (-self.emitted[t], t)):
            near = self.model.suggest(term) if self.model else []
            hint = f" -- did you mean {', '.join('biolink:' + n for n in near)}?" if near else ""
            out.append(f"biolink:{term} -- {self.emitted[term]:,} statements, "
                       f"not in Biolink {self.version}{hint}")
        for term in sorted(self.deprecated_used, key=lambda t: (-self.emitted[t], t)):
            note = self.model.deprecated[term] if self.model else ""
            because = "" if note.strip().lower() == "true" else f" ({note})"
            out.append(f"biolink:{term} -- {self.emitted[term]:,} statements, "
                       f"deprecated in Biolink {self.version}{because}")
        return out


def review(emitted: Counter, pin_from: Path | str | None = None,
           version: str | None = None, schema_yaml: str | Path | None = None,
           timeout: int = BIOLINK_TIMEOUT) -> BiolinkTermReview:
    """Compare already-counted biolink: terms against the pinned release.

    `pin_from` is the ingest's LinkML schema, whose `settings.biolink_version`
    is the pin. `version` overrides it from the command line; `schema_yaml`
    replaces the download with a file.
    """
    def where() -> str:
        if schema_yaml is not None:
            return str(schema_yaml)
        if version:
            return schema_url(version)
        return f"the Biolink version pinned in {pin_from}"

    source = where()
    try:
        if version is None and pin_from is not None:
            version = pinned_version(pin_from)
            source = where()
        model = load_model(version, schema_yaml, timeout)
    except BiolinkUnavailable as error:
        return BiolinkTermReview(version, source, model_available=False,
                                 emitted=emitted, error=str(error))
    return BiolinkTermReview(version, model.source, model_available=True,
                             model=model, emitted=emitted)
