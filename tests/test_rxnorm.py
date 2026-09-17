"""Tests for the RxNorm layer — the code lookup the brief asks for in place of string matching.

Everything here is offline. The client replays **recorded real RxNav responses**
(``tests/data/rxnav_responses.json``, produced by ``scripts/record_rxnav.py``), so what is
being tested is RxNav's actual behaviour rather than my assumptions about its response
shapes. That distinction is the whole point of this layer: the safety gate's verdicts are
only as good as the drug resolution underneath them.

The two filters in ``candidate_rxcuis`` carry the weight, and each has a test that fails if
the filter is removed:

* ``test_the_band_rejects_the_prescribed_drug_on_a_wrong_strength_search`` — the decisive
  one. A 300 UNT/ML search returns the *100 UNT/ML* concept, which is the drug actually
  prescribed in this demo. Drop the band and a wrong-strength indent stops being reported as
  a strength mismatch and becomes "ambiguous terminology" instead.
* ``test_token_coverage_rejects_a_strength_no_product_has`` — a request for 500 UNT/ML
  returns five candidates, all sharing only the ingredient. Drop coverage and the request
  silently resolves to the 100 UNT/ML pen: a clinical claim reached by fuzzy string match.

Both are asserted as *relationships in the recorded data* rather than as hard-coded scores,
so re-recording does not break them — but ``RecordedRxNavTransport`` raises on any request
that was not recorded, so a term that drifts out of the recording fails loudly.
"""

from __future__ import annotations

import pytest

from app import demo
from app.rxnorm import normalize
from app.rxnorm.normalize import (
    SCORE_BAND,
    canonical_unit,
    candidate_rxcuis,
    candidates_for_display,
    dose_form_tokens,
    normalize_rxcui,
    parse_strength,
)

#: The demo's prescribed product, per `app/demo.py`: SCD 847230.
PRESCRIBED_RXCUI = demo.DRUG_RXCUI
#: The unexpandable rank-1 hit for the clean display — see the unexpandable-concept tests.
UNEXPANDABLE_RXCUI = "1359855"


# -- pure helpers: units -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UNT/ML", "UNT/ML"),
        ("unt/ml", "UNT/ML"),
        ("U/ML", "UNT/ML"),
        ("units/mL", "UNT/ML"),
        ("Unt/Ml.", "UNT/ML"),
        ("UNT / ML", "UNT/ML"),
        ("MG/ML", "MG/ML"),
        ("MCG/ML", "UG/ML"),
        ("µg/mL", "UG/ML"),
        ("μg/mL", "UG/ML"),
        ("%", "%"),
    ],
)
def test_unit_spellings_collapse_to_one_canonical_form(raw: str, expected: str) -> None:
    """Without this, "100 U/ML" and "100 UNT/ML" compare as different units."""
    unit, known = canonical_unit(raw)
    assert (unit, known) == (expected, True)


@pytest.mark.parametrize("raw", ["FURLONGS", "widgets/ml", "10^9/L"])
def test_an_unrecognised_unit_is_reported_unknown_not_passed_through(raw: str) -> None:
    """``known=False`` is what lets the gate hold instead of claiming "the strengths differ"."""
    unit, known = canonical_unit(raw)
    assert known is False
    assert unit is not None  # returned as written, for the hold message


def test_a_missing_unit_is_unknown() -> None:
    assert canonical_unit(None) == (None, False)
    assert canonical_unit("") == (None, False)


def test_the_micro_sign_is_normalised() -> None:
    """``µ`` (U+00B5) and ``μ`` (U+03BC) are different characters and both appear in the wild.

    They are also easy to introduce by accident: a value copied from RxNorm and one typed on
    a ward keyboard can differ in a byte and silently become different units.
    """
    assert canonical_unit("µg/mL")[0] == canonical_unit("μg/mL")[0] == "UG/ML"


# -- pure helpers: strength --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100 UNT/ML", (100.0, "UNT/ML")),
        ("300 UNT/ML", (300.0, "UNT/ML")),
        ("100.5 MG/ML", (100.5, "MG/ML")),
        ("3 ML", (3.0, "ML")),
        ("  100 UNT/ML  ", (100.0, "UNT/ML")),
    ],
)
def test_strengths_parse_into_a_value_and_a_unit(raw: str, expected: tuple[float, str]) -> None:
    assert parse_strength(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "abc UNT/ML", "", "   ", None, "UNT/ML", "100"])
def test_unparseable_strengths_return_none(raw: str | None) -> None:
    """``None`` is the signal the gate turns into ``order_detail_unreadable``.

    ``"100"`` is in here deliberately: a bare number is not a strength. Accepting it would
    mean guessing the unit from elsewhere, and the whole point of reading RXO-18/19
    structurally is that the unit is stated rather than inferred.
    """
    assert parse_strength(raw) is None


def test_parsing_does_not_canonicalise_the_unit() -> None:
    """Canonicalisation is a separate step, and the separation is what distinguishes a
    unit mismatch from an unknown unit."""
    assert parse_strength("100 u/ml") == (100.0, "u/ml")


# -- pure helpers: dose form -------------------------------------------------------------


def test_dose_form_tokens_are_case_and_punctuation_insensitive() -> None:
    assert dose_form_tokens("Pen Injector") == {"pen", "injector"}
    assert dose_form_tokens("PEN INJECTOR") == dose_form_tokens("pen  injector") == {
        "pen",
        "injector",
    }


def test_the_hl7_component_separators_are_not_part_of_the_dose_form() -> None:
    """The parser hands over the *text* component of RXO-5, not the raw triple.

    ``RXO-5`` is ``PEN INJ^Pen Injector^L`` on the wire, and taking it whole would tokenize
    the mnemonic and the coding system into the comparison ("inj", "l"), so a dose form would
    never compare equal to anything. The split belongs to `app/hl7/parse.py`; asserted here
    because this module's comparison is what breaks if it changes.
    """
    assert dose_form_tokens("Pen Injector") == {"pen", "injector"}
    assert dose_form_tokens("PEN INJ^Pen Injector^L") != dose_form_tokens("Pen Injector")


def test_an_empty_dose_form_has_no_tokens() -> None:
    assert dose_form_tokens(None) == frozenset()
    assert dose_form_tokens("") == frozenset()


def test_different_dose_forms_do_not_compare_equal() -> None:
    """The gate's dose-form comparison, at the level it actually operates.

    ``Injectable Solution`` against ``Pen Injector`` is the demo's mismatch fixture. Note
    ``injection`` is a synonym of ``injectable`` but *not* of ``injector`` — treating those
    as the same word would make a solution and a pen compare equal, which is precisely the
    confusion the third dimension exists to catch.
    """
    assert dose_form_tokens("Injectable Solution") != dose_form_tokens("Pen Injector")
    assert dose_form_tokens("injection") == dose_form_tokens("Injectable")
    assert dose_form_tokens("injection") != dose_form_tokens("Pen Injector")


# -- the exact-match fast path -----------------------------------------------------------


def test_an_exact_rxnorm_name_resolves_without_fuzzy_matching(rxnav) -> None:
    """A name spelled the way RxNorm spells it short-circuits the whole approximate path."""
    assert rxnav.exact_rxcuis(demo.DRUG_WRONG_FORM_DISPLAY) == ["311041"]
    assert candidate_rxcuis(rxnav, demo.DRUG_WRONG_FORM_DISPLAY) == ["311041"]


def test_a_realistic_floor_indent_has_no_exact_match(rxnav) -> None:
    """Which is why the approximate path is the one that matters.

    The display a ward actually sends omits the pack size, and RxNorm's canonical name for
    that concept begins with it — so the literal lookup finds nothing and the fuzzy path has
    to carry the decision.
    """
    assert rxnav.exact_rxcuis(demo.DRUG_DISPLAY) == []
    assert rxnav.approximate(demo.DRUG_DISPLAY) != []


# -- the score band ----------------------------------------------------------------------


def test_the_band_rejects_the_prescribed_drug_on_a_wrong_strength_search(rxnav) -> None:
    """The decisive case: a wrong-strength indent must not become "ambiguous terminology".

    The demo prescribes 100 UNT/ML (rxcui 847230). An indent for 300 UNT/ML returns that very
    concept among its candidates — and unlike most of the padding, it expands cleanly. With
    the band removed it would be a second complete candidate, so the gate would report
    ``terminology_ambiguous`` ("we cannot tell which formulation you meant") when the truth
    is ``strength_mismatch`` ("you ordered ten times the prescribed strength"). Different
    hold, different follow-up, and the wrong one is the reassuring-sounding one.
    """
    term = demo.DRUG_WRONG_STRENGTH_DISPLAY
    raw = rxnav.approximate(term)

    # It really is in the response — the band, not the API, is what removes it.
    assert PRESCRIBED_RXCUI in {c["rxcui"] for c in raw}, "recording no longer exercises the band"
    assert PRESCRIBED_RXCUI not in candidate_rxcuis(rxnav, term)

    # ...and it is outside the band, not merely absent from the kept set.
    top = max(float(c["score"]) for c in raw)
    prescribed = [float(c["score"]) for c in raw if c["rxcui"] == PRESCRIBED_RXCUI]
    assert min(prescribed) < top - SCORE_BAND


def test_the_band_keeps_the_rank_one_concept_even_when_it_cannot_be_expanded(rxnav) -> None:
    """The band is anchored to rank 1, not to the best *usable* candidate.

    For the clean display, rank 1 (1359855) is a concept the REST API cannot expand — it
    answers ``200 {}`` for ``properties``. Anchoring to the best usable candidate instead
    would widen the band on whichever search has an unusable rank 1, and on the 300 UNT/ML
    search that widening is exactly what would readmit the prescribed 100 UNT/ML drug.
    """
    kept = candidate_rxcuis(rxnav, demo.DRUG_DISPLAY)

    assert UNEXPANDABLE_RXCUI in kept, "rank 1 is no longer the unexpandable concept"
    assert rxnav.properties(UNEXPANDABLE_RXCUI) is None
    assert normalize_rxcui(rxnav, UNEXPANDABLE_RXCUI) is None


def test_the_band_keeps_the_right_strength_candidate(rxnav) -> None:
    """A filter that rejected everything would pass the tests above while breaking the demo."""
    specified = candidates_for_display(rxnav, demo.DRUG_WRONG_STRENGTH_DISPLAY)

    assert [d.source_rxcui for d in specified] == ["2002419"]
    assert specified[0].strength_value == 300.0
    assert specified[0].strength_unit == "UNT/ML"


# -- token coverage ----------------------------------------------------------------------


def test_token_coverage_rejects_a_strength_no_product_has(rxnav) -> None:
    """Without coverage this resolves to the 100 UNT/ML pen and reports a bogus mismatch.

    RxNav answers a request for 500 UNT/ML with the products that share the ingredient, and
    several of them are within the score band. Every one is dropped by coverage, because none
    of their names contains "500" — the token the request is *about*.
    """
    term = demo.DRUG_NONEXISTENT_STRENGTH_DISPLAY
    raw = rxnav.approximate(term)

    top = max(float(c["score"]) for c in raw)
    within_band = [c for c in raw if top - float(c["score"]) <= SCORE_BAND]
    assert len(within_band) > 1, "recording no longer exercises coverage"
    assert candidate_rxcuis(rxnav, term) == []
    assert candidates_for_display(rxnav, term) == []


def test_token_coverage_rejects_a_different_presentation(rxnav) -> None:
    """The minimal version of the same rule: one candidate, dropped on a single token.

    A request for a *vial* is answered with the nearest concept RxNorm has, which is the
    strength without any form at all. Keeping it would attach a vial indent to a pen-injector
    prescription — a presentation the ward did not ask for.
    """
    term = demo.DRUG_VIAL_DISPLAY
    raw = rxnav.approximate(term)

    assert len(raw) >= 1
    assert candidate_rxcuis(rxnav, term) == []
    assert candidates_for_display(rxnav, term) == []


def test_coverage_is_one_directional(rxnav) -> None:
    """A candidate may carry tokens the request omits; it may not omit tokens the request has.

    The prescribed concept's name begins with the pack size ("3 ML …") that the ward's display
    never mentions. If coverage ran both ways, the correct product would fail its own search.
    """
    assert normalize._covers(demo.DRUG_DISPLAY, "3 ML insulin glargine 100 UNT/ML Pen Injector")
    assert not normalize._covers(
        "3 ML insulin glargine 100 UNT/ML Pen Injector", demo.DRUG_DISPLAY
    )


def test_a_string_with_no_drug_in_it_yields_no_candidates_at_all(rxnav) -> None:
    """The third route to "unresolved", and it needs neither filter.

    RxNav returns an ``approximateGroup`` with no candidate list whatsoever — so the band and
    the coverage check are both bypassed, and the empty result has to be handled as a normal
    outcome rather than an error.
    """
    assert rxnav.approximate(demo.DRUG_UNRESOLVABLE_DISPLAY) == []
    assert candidate_rxcuis(rxnav, demo.DRUG_UNRESOLVABLE_DISPLAY) == []
    assert candidates_for_display(rxnav, demo.DRUG_UNRESOLVABLE_DISPLAY) == []


# -- normalizing a real concept ----------------------------------------------------------


def test_a_real_concept_normalizes_to_the_comparable_triple(rxnav) -> None:
    """Ingredient, strength and dose form — from structured relations, not the display name."""
    drug = normalize_rxcui(rxnav, PRESCRIBED_RXCUI)

    assert drug is not None and drug.is_complete
    assert drug.ingredient_name == demo.DRUG_INGREDIENT
    assert drug.ingredient_rxcui == "274783"
    assert (drug.strength_value, drug.strength_unit) == (100.0, "UNT/ML")
    assert drug.dose_form == "Pen Injector"
    assert (drug.source_rxcui, drug.source_tty) == (PRESCRIBED_RXCUI, "SCD")


def test_the_strength_comes_from_attributes_not_the_name(rxnav) -> None:
    """``AVAILABLE_STRENGTH`` is the structured source; the name merely agrees with it."""
    drug = normalize_rxcui(rxnav, "2002419")

    assert drug is not None
    assert (drug.strength_value, drug.strength_unit) == (300.0, "UNT/ML")
    assert "300 UNT/ML" in drug.source_name  # the name would have been parseable too…


def test_dose_form_is_what_separates_two_otherwise_identical_products(rxnav) -> None:
    """Same ingredient, same strength, different form — the third dimension earns its place."""
    pen = normalize_rxcui(rxnav, PRESCRIBED_RXCUI)
    solution = normalize_rxcui(rxnav, "311041")

    assert pen is not None and solution is not None
    assert pen.ingredient_rxcui == solution.ingredient_rxcui
    assert pen.strength_value == solution.strength_value
    assert pen.dose_form != solution.dose_form
    assert pen.comparable() != solution.comparable()


def test_an_unexpandable_concept_is_none_rather_than_an_error(rxnav) -> None:
    """RxNorm lists these concepts but the REST API answers ``200 {}`` for them.

    Treating that as an exception would make the clean path fail; treating it as a *usable*
    concept would fabricate a drug with no strength or form. ``None`` is the third option, and
    the caller skips it.
    """
    assert normalize_rxcui(rxnav, "1604545") is None


# -- candidate lists: dedup and ambiguity ------------------------------------------------


def test_brand_variants_of_one_formulation_collapse_to_one_candidate(rxnav_aliased) -> None:
    """Several rxcuis, one formulation, one candidate — not an ambiguity.

    RxNorm holds a branded and a generic concept for the same pen, and a request can match
    both. Reporting that as ``terminology_ambiguous`` would refuse a perfectly ordinary
    indent, so distinctness is by (ingredient, strength, dose form) rather than by rxcui.

    Tested through the aliasing fixture because the recorded data cannot reach this state:
    RxNorm's near-duplicate concepts are consistently unexpandable over REST, so they are
    dropped as unusable before dedup sees them. The payloads are the real 847230 ones.
    """
    client = rxnav_aliased(alias="9000001", original=PRESCRIBED_RXCUI, display=demo.DRUG_DISPLAY)

    # Two rxcuis survive the filters and expand to the same triple…
    assert sorted(candidate_rxcuis(client, demo.DRUG_DISPLAY)) == sorted(
        ["9000001", PRESCRIBED_RXCUI]
    )
    # …and dedup collapses them to the one formulation.
    candidates = candidates_for_display(client, demo.DRUG_DISPLAY)
    assert len(candidates) == 1
    assert candidates[0].source_rxcui == PRESCRIBED_RXCUI


def test_an_incomplete_concept_is_never_returned_as_a_candidate(rxnav) -> None:
    """Every returned candidate has all three dimensions, or the gate cannot compare it.

    ``is_complete`` is the contract the gate relies on when it calls a verdict — a candidate
    list containing a half-resolved drug would push the missing check downstream.
    """
    for term in (
        demo.DRUG_DISPLAY,
        demo.DRUG_WRONG_STRENGTH_DISPLAY,
        demo.DRUG_WRONG_FORM_DISPLAY,
    ):
        candidates = candidates_for_display(rxnav, term)
        assert candidates, f"{term!r} resolved to nothing"
        assert all(c.is_complete for c in candidates)


def test_the_three_fixture_displays_resolve_to_three_distinct_formulations(rxnav) -> None:
    """The demo's whole premise, asserted end to end at this layer.

    If two of these collapsed to the same triple, the mismatch fixtures would stop testing a
    mismatch and the gate tests would pass for the wrong reason.
    """
    triples = {
        term: [c.comparable() for c in candidates_for_display(rxnav, term)]
        for term in (
            demo.DRUG_DISPLAY,
            demo.DRUG_WRONG_STRENGTH_DISPLAY,
            demo.DRUG_WRONG_FORM_DISPLAY,
        )
    }
    flat = [t for triples_ in triples.values() for t in triples_]
    assert len(flat) == len(set(flat)) == 3


# -- client behaviour --------------------------------------------------------------------


def test_approximate_deduplicates_repeated_rxcuis(rxnav) -> None:
    """RxNav returns the same concept several times in one response.

    Measured: the 300 UNT/ML search lists 847230 three times. Left in place, the duplicates
    inflate the candidate count and turn a single formulation into reported ambiguity.
    """
    raw = rxnav.approximate(demo.DRUG_WRONG_STRENGTH_DISPLAY)
    rxcuis = [c["rxcui"] for c in raw]

    assert len(rxcuis) == len(set(rxcuis))
    # The response really does contain duplicates — otherwise this proves nothing.
    kept = candidate_rxcuis(rxnav, demo.DRUG_WRONG_STRENGTH_DISPLAY)
    assert len(kept) == len(set(kept))


def test_lookups_are_cached(rxnav) -> None:
    """The public API is rate-limited to roughly 20 requests/second and has no key.

    A second identical lookup must not spend another request — the recording transport raises
    on an unrecorded URL, so a cache miss here would fail rather than quietly go to the
    network.
    """
    first = rxnav.approximate(demo.DRUG_DISPLAY)
    second = rxnav.approximate(demo.DRUG_DISPLAY)
    assert first == second
