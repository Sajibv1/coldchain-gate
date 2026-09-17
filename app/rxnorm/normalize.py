"""Reduce an RxNorm concept to the three values the safety gate actually compares.

The brief asks us to "confirm that the requested drug formulation matches the
prescription". String comparison of drug names cannot do that — ``3 ML insulin glargine
100 UNT/ML Pen Injector [Basaglar]`` and ``insulin glargine 100 UNT/ML Pen Injector`` are
the same formulation, while ``300 UNT/ML`` differs from ``100 UNT/ML`` in a way that
matters clinically. So both sides of the comparison are normalized to an
(ingredient, strength, dose form) triple and compared on that.

Dose form and strength come from structured RxNorm relations, not from parsing the display
name:

    related?tty=IN           -> ingredient
    related?tty=DF           -> dose form
    allProperties ATTRIBUTES -> AVAILABLE_STRENGTH ("100 UNT/ML")

Anything that cannot be normalized into all three dimensions is dropped rather than
guessed; the caller turns an empty candidate list into a hold.
"""

from __future__ import annotations

import re

from app.models import NormalizedDrug
from app.rxnorm.client import JSON, RxNavClient

#: Unit spellings that mean the same thing, mapped to one canonical form. Deliberately
#: small: an unrecognised unit is reported as *unknown* rather than passed through, so a
#: unit we cannot reason about becomes a hold instead of a silent match.
UNIT_SYNONYMS: dict[str, str] = {
    "UNT/ML": "UNT/ML",
    "U/ML": "UNT/ML",
    "UNIT/ML": "UNT/ML",
    "UNITS/ML": "UNT/ML",
    "UNT/ML.": "UNT/ML",
    "MG/ML": "MG/ML",
    "MG/ML.": "MG/ML",
    "MCG/ML": "UG/ML",
    "UG/ML": "UG/ML",
    "G/ML": "G/ML",
    "%": "%",
    "MG": "MG",
    "MCG": "UG",
    "UG": "UG",
    "G": "G",
    "UNT": "UNT",
    "U": "UNT",
    "UNIT": "UNT",
    "UNITS": "UNT",
    "ML": "ML",
}

_STRENGTH_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[^\d\s].*?)\s*$")

#: Approximate-match candidates are kept only within this many points of the rank-1 hit.
#: RxNav scores are relative rather than normalised, so this is a delta, not a threshold.
SCORE_BAND = 1.0

#: Tokens that mean the same thing when comparing a request against an RxNorm name.
TOKEN_SYNONYMS: dict[str, str] = {
    "units": "unt",
    "unit": "unt",
    "u": "unt",
    "iu": "unt",
    "millilitre": "ml",
    "milliliter": "ml",
    "injection": "injectable",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _score(candidate: JSON) -> float:
    try:
        return float(candidate.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _tokenize(text: str) -> set[str]:
    return {TOKEN_SYNONYMS.get(t, t) for t in _TOKEN_RE.findall(text.lower())}


def dose_form_tokens(text: str | None) -> frozenset[str]:
    """Comparison form of a dose form, e.g. ``"Pen Injector"`` -> ``{"pen", "injector"}``.

    Public because the safety gate compares an HL7 ``RXO-5`` value against an RxNorm ``DF``
    name, and both spellings of the same form vary in case and in unit-ish words
    (``"Injectable Solution"`` vs ``"injection"``). Sharing the tokenizer with the
    candidate matcher keeps the two from drifting into different notions of "same form".
    """
    return frozenset(_tokenize(text or ""))


def _covers(request: str, candidate_name: str) -> bool:
    """True when every token of ``request`` appears in ``candidate_name``.

    One-directional on purpose. An RxNorm concept name routinely carries tokens the
    request omits — the pack size, a brand in brackets — but never the reverse: a request
    naming a strength or form the candidate lacks is a different formulation.
    """
    wanted = _tokenize(request)
    return bool(wanted) and wanted <= _tokenize(candidate_name)


def canonical_unit(raw: str | None) -> tuple[str | None, bool]:
    """Return ``(canonical_unit, is_known)``.

    ``is_known`` is False for a spelling we have no mapping for. The caller uses that to
    distinguish "these strengths differ" from "we cannot tell whether these are even the
    same unit" — a distinction that matters when failing closed.
    """
    if not raw:
        return None, False
    # The micro sign (U+00B5) and the Greek mu (U+03BC) are both used for "micro", and they
    # must be folded to "u" *before* uppercasing: `"µ".upper()` is Greek capital Mu (U+039C),
    # so replacing afterwards matches nothing and a perfectly ordinary "µg/mL" is reported as
    # an unknown unit — which the gate turns into a hold.
    key = raw.strip().replace("µ", "u").replace("μ", "u")
    key = key.upper().replace(" ", "")
    if key in UNIT_SYNONYMS:
        return UNIT_SYNONYMS[key], True
    return key, False


def parse_strength(raw: str | None) -> tuple[float, str] | None:
    """``"100 UNT/ML"`` -> ``(100.0, "UNT/ML")``. ``None`` when unparseable."""
    match = _STRENGTH_RE.match(raw or "")
    if not match:
        return None
    try:
        value = float(match.group("value"))
    except ValueError:
        return None
    unit = match.group("unit")
    return value, unit


def _first_name(concepts: list[JSON]) -> JSON | None:
    return concepts[0] if concepts else None


def _resolve_ingredient(client: RxNavClient, rxcui: str, tty: str | None, name: str) -> tuple[str | None, str | None]:
    """The ingredient, rolling a clinical/branded drug up to its base ingredient."""
    if tty == "IN":
        return name, rxcui
    ingredient = _first_name(client.related(rxcui, "IN"))
    if ingredient:
        return ingredient.get("name"), ingredient.get("rxcui")
    return None, None


def _resolve_dose_form(client: RxNavClient, rxcui: str, tty: str | None) -> str | None:
    """Dose form via DF, falling back through SCD for pack concepts (GPCK/BPCK)."""
    direct = _first_name(client.related(rxcui, "DF"))
    if direct:
        return direct.get("name")
    if tty in {"GPCK", "BPCK"}:
        scd = _first_name(client.related(rxcui, "SCD"))
        if scd and scd.get("rxcui"):
            via_scd = _first_name(client.related(scd["rxcui"], "DF"))
            if via_scd:
                return via_scd.get("name")
    return None


def normalize_rxcui(client: RxNavClient, rxcui: str) -> NormalizedDrug | None:
    """Normalize one RxCUI, or ``None`` if the concept is unusable.

    ``None`` is returned both for an unknown concept and for one RxNorm lists but the REST
    API cannot expand (it answers ``200 {}``). Callers skip these; they are not errors.
    """
    props = client.properties(rxcui)
    if not props:
        return None

    tty = props.get("tty")
    name = props.get("name") or ""

    ingredient_name, ingredient_rxcui = _resolve_ingredient(client, rxcui, tty, name)
    dose_form = _resolve_dose_form(client, rxcui, tty)

    strength = parse_strength(client.attributes(rxcui).get("AVAILABLE_STRENGTH"))
    strength_value, strength_unit = strength if strength else (None, None)

    return NormalizedDrug(
        ingredient_name=ingredient_name,
        ingredient_rxcui=ingredient_rxcui,
        strength_value=strength_value,
        strength_unit=strength_unit,
        dose_form=dose_form,
        source_rxcui=rxcui,
        source_tty=tty,
        source_name=name,
    )


def candidate_rxcuis(client: RxNavClient, display: str) -> list[str]:
    """RxCUIs plausibly denoting ``display``.

    Exact matches win outright when they exist, and for a full RxNorm-style name they do
    (``insulin glargine 100 UNT/ML Injectable Solution`` resolves directly). A floor indent
    is not spelled that way: ``insulin glargine 100 UNT/ML Pen Injector`` has no literal
    match, because RxNorm's canonical name for that concept begins with the pack size
    (``3 ML insulin glargine 100 UNT/ML Pen Injector``). So the approximate path is the one
    that matters, and it needs two filters — both measured against the live API, both
    load-bearing:

    1. **Score band, relative to the rank-1 candidate.** ``approximateTerm`` pads its results
       with loosely related concepts. Searching for ``insulin glargine 300 UNT/ML Pen
       Injector`` also returns ``3 ML insulin glargine 100 UNT/ML Pen Injector``
       (rxcui 847230) — which in this demo is the *prescribed* drug. That concept expands
       cleanly, so a band that let it through would make a wrong-strength indent report as
       ``terminology_ambiguous`` — "we cannot tell which formulation you meant" — when the
       truth is "you ordered the wrong strength". Anchoring the band to rank 1 rather than to
       the best *usable* candidate is the other half of this: rank 1 is frequently a concept
       the REST API cannot expand (that same search returns ``200 {}`` for rank 1), so
       anchoring to the best usable candidate would widen the band and readmit 847230.
    2. **Token coverage.** Fuzzy matching returns *something* for any drug-shaped string.
       ``insulin glargine 500 UNT/ML Pen Injector`` names a strength no product has; RxNav
       answers with the 100 and 300 UNT/ML pens, and this filter drops every one of them.
       Without it the request would resolve to the 100 UNT/ML pen and be reported as a
       strength mismatch — a clinical claim reached by string matching, which is the
       anti-pattern this design exists to avoid. A candidate is kept only when every token in
       the request appears in its name.

    A string with no drug in it at all is a third case needing neither filter: for
    ``qqqxyz wobble plinth`` RxNav returns no candidates, so the list is empty on arrival.
    The band cannot empty the list by itself — rank 1 is always within its own band — so an
    empty result means either "no candidates" or "coverage rejected all of them", and the
    caller reports both as unresolvable terminology.
    """
    exact = client.exact_rxcuis(display)
    if exact:
        return list(dict.fromkeys(exact))

    candidates = client.approximate(display)
    if not candidates:
        return []

    top_score = max(_score(c) for c in candidates)
    kept = [c for c in candidates if top_score - _score(c) <= SCORE_BAND]
    kept = [c for c in kept if _covers(display, c.get("name") or "")]
    return [c["rxcui"] for c in kept if c.get("rxcui")]


def candidates_for_display(client: RxNavClient, display: str) -> list[NormalizedDrug]:
    """Every *distinct, complete* normalization of ``display``.

    The caller treats 0 as unresolved and >1 as ambiguous — both are holds. Distinctness
    is by (ingredient, strength, dose form), so the several brand variants of one
    formulation collapse to a single candidate rather than reading as ambiguity.
    """
    distinct: dict[tuple, NormalizedDrug] = {}
    for rxcui in candidate_rxcuis(client, display):
        drug = normalize_rxcui(client, rxcui)
        if drug and drug.is_complete:
            distinct.setdefault(drug.comparable(), drug)
    return list(distinct.values())
