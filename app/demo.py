"""Single source of truth for the demo dataset.

The HL7 v2 fixtures, the FHIR seeder, and the tests all have to agree on the same order
numbers and the same patient, or the order-correlation step matches nothing and the demo
silently degrades into "everything is a hold". Keeping the values here means a change to
the fixture cannot drift away from the seeded records.

Two deliberate properties:

* **The patient name is real-looking.** The PHI tests are only meaningful if there is
  something that genuinely must be stripped. A scrubbing test against a placeholder like
  "Test Patient" proves nothing.
* **The order numbers are the ones in the HL7 fixture** (ORC-2 placer / ORC-3 filler).
  Those are the correlation keys the pipeline searches on.
"""

from __future__ import annotations

# -- patient (HL7 PID-3 / PID-5) -------------------------------------------------------

MRN = "MRN12345"
PATIENT_FAMILY = "DOE"
PATIENT_GIVEN = "JOHN"

# -- order identifiers (HL7 ORC-2 / ORC-3) ---------------------------------------------

PLACER_ORDER_NUMBER = "INDENT-1001"
FILLER_ORDER_NUMBER = "RX-1001"

# -- workforce (not PHI; see app/safety/phi.py) ----------------------------------------

PRESCRIBER_FAMILY = "RAHMAN"
PRESCRIBER_GIVEN = "AZIZ"
COURIER_NAME = "M. OKAFOR"

# -- logistics -------------------------------------------------------------------------

WARD_NAME = "3 West"

#: The dispensing service as an auditable party — the ``AuditEvent.agent.who`` for events
#: recorded before a prescriber has been correlated. Workforce/organisational identity, so
#: it is not PHI, and it is the subject of a seeded ``Organization`` so the reference
#: resolves on write.
DISPENSING_SERVICE_NAME = "Cold-chain dispensing service"

# -- prescription ----------------------------------------------------------------------
# SCD 847230 = "3 ML insulin glargine 100 UNT/ML Pen Injector". The two mismatch fixtures
# are built against the same ingredient so that only the differing axis is in play:
#   300 UNT/ML Pen Injector    (2002419) -> STRENGTH_MISMATCH
#   Injectable Solution        (311041)  -> DOSE_FORM_MISMATCH

DRUG_DISPLAY = "insulin glargine 100 UNT/ML Pen Injector"
DRUG_RXCUI = "847230"
DRUG_INGREDIENT = "insulin glargine"

#: Wrong-strength indent, used by the strength-mismatch fixture.
DRUG_WRONG_STRENGTH_DISPLAY = "insulin glargine 300 UNT/ML Pen Injector"

#: Wrong-formulation indent, used by the dose-form-mismatch fixture.
DRUG_WRONG_FORM_DISPLAY = "insulin glargine 100 UNT/ML Injectable Solution"

#: Matches nothing in RxNorm.
DRUG_UNRESOLVABLE_DISPLAY = "qqqxyz wobble plinth"

# -- RxNorm-layer probes -----------------------------------------------------------------
# Not HL7 fixtures: these exist to pin the *matcher*, not the gate. Both are realistic
# floor indents that RxNorm resolves to the wrong thing unless the filters in
# `app/rxnorm/normalize.py` do their job, and each was measured against the live API:
#
#   a strength no product has -> RxNav returns candidates (the 100 and 300 UNT/ML pens),
#       all of them sharing only the ingredient. Without the token-coverage filter this
#       resolves to the 100 UNT/ML pen and is reported as a *strength mismatch* — a
#       clinical claim arrived at by fuzzy string matching, which is the anti-pattern.
#   a vial presentation -> RxNav's nearest concept is "insulin glargine 100 UNT/ML",
#       which has no dose form at all. Without the coverage filter the request for a vial
#       would silently attach itself to a pen-injector prescription.
#
# Both are recorded in `tests/data/rxnav_responses.json`; see `scripts/record_rxnav.py`.

#: A strength that does not exist — every candidate must be dropped by token coverage.
DRUG_NONEXISTENT_STRENGTH_DISPLAY = "insulin glargine 500 UNT/ML Pen Injector"

#: A different presentation of the same strength — the "vial" token is the whole test.
DRUG_VIAL_DISPLAY = "insulin glargine 100 UNT/ML Vial"
