"""Idempotent demo seeder for the public HAPI FHIR sandbox.

`make seed` has to be safe to run repeatedly and recoverable after the sandbox is purged.
Both properties come from one decision: every resource is written with a **client-assigned,
namespaced id** via ``PUT`` (create-or-replace) rather than ``POST``. Running this twice
leaves the server in the same state; running it after a purge recreates everything.

Verified against the live sandbox before this was built on: ``PUT`` to a fresh id returns
201, a second ``PUT`` to the same id returns 200, and an ``identifier=`` search returns
exactly the one seeded resource.

The namespace matters. The public sandbox is shared, so identifiers from two candidates
could collide and make an order-correlation step match a stranger's record. ``DEMO_NAMESPACE``
is prefixed onto every id, and :func:`verify` fails loudly if a search returns more than one
hit — which is what a collision looks like.

Timestamps are fixed constants, not ``now()``. A seeder whose output changes on every run
is not idempotent in any useful sense: it churns version ids and makes "did my write land?"
impossible to answer by comparing state.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from app import demo
from app.config import Settings, get_settings
from app.fhir import build
from app.fhir.client import FhirClient, FhirError

JSON = dict[str, Any]

#: Fixed so that re-seeding is a no-op. Only the prescription is dated; the dispense is
#: written by the pipeline at run time.
AUTHORED_ON = "2026-09-16T08:00:00Z"


@dataclass(frozen=True)
class SeededDataset:
    """The demo dataset, with ids already namespaced by the caller's settings."""

    patient: JSON
    prescriber: JSON
    courier: JSON
    ward: JSON
    service: JSON
    request: JSON

    def resources(self) -> list[JSON]:
        """In dependency order: referents before referrers.

        HAPI resolves references leniently, so a wrong order would probably still write —
        which is exactly why the order is explicit here rather than left to chance.
        """
        return [
            self.patient,
            self.prescriber,
            self.courier,
            self.ward,
            self.service,
            self.request,
        ]


def build_dataset(
    settings: Settings,
    *,
    token: str | None = None,
) -> SeededDataset:
    """Construct the demo dataset. Pure; no I/O.

    With ``token=None`` this is the demo dataset, and its identifiers are exactly the ones
    in the HL7 fixtures — the order numbers are the correlation keys, so they cannot be
    decorated.

    ``token`` exists because resource-id namespacing alone does not make two datasets
    coexistable. HAPI rejects a write that duplicates an existing resource (``HAPI-2840``)
    and it decides "duplicate" from *content*, not from the id — and not only from
    ``identifier`` fields. Confirmed on the live sandbox: a second ``Patient`` holding the
    same MRN is rejected, a second ``Practitioner`` with the same staff id is rejected, and
    a second ``Location`` with the same ``name`` is rejected even though ``Location`` here
    carries no identifier at all.

    So a token is applied to every content field HAPI might key on: the MRN, both order
    numbers, both staff ids, the ward name, and the patient's name. Tests pass a per-run
    token so they cannot collide with a seeded demo, with each other, or with a previous
    run's leftovers.
    """
    ns = settings.ns_id
    sfx = f"-{token}" if token else ""

    patient = build.patient(
        resource_id=ns("patient-1"),
        mrn=f"{demo.MRN}{sfx}",
        family=f"{demo.PATIENT_FAMILY}{sfx}",
        given=demo.PATIENT_GIVEN,
        gender="male",
        birth_date="1974-03-02",
    )
    prescriber = build.practitioner(
        resource_id=ns("practitioner-prescriber"),
        family=demo.PRESCRIBER_FAMILY,
        given=demo.PRESCRIBER_GIVEN,
        staff_id=f"P-{settings.demo_namespace}{sfx}",
    )
    courier = build.practitioner(
        resource_id=ns("practitioner-courier"),
        family="OKAFOR",
        given="M.",
        staff_id=f"C-{settings.demo_namespace}{sfx}",
    )
    ward = build.location(resource_id=ns("location-ward-3w"), name=f"{demo.WARD_NAME}{sfx}")
    # The name is tokenized for the same reason the ward's is: HAPI's duplicate check keys on
    # content, not on id, so two namespaces seeding "Cold-chain dispensing service" would
    # collide (HAPI-2840) exactly as two wards called "3 West" do.
    service = build.organization(
        resource_id=ns("organization-dispensing-service"),
        name=f"{demo.DISPENSING_SERVICE_NAME}{sfx}",
    )

    request = build.medication_request(
        resource_id=ns("medreq-1001"),
        placer_order_number=f"{demo.PLACER_ORDER_NUMBER}{sfx}",
        filler_order_number=f"{demo.FILLER_ORDER_NUMBER}{sfx}",
        medication=build.medication_codeable(
            rxcui=demo.DRUG_RXCUI,
            display=demo.DRUG_DISPLAY,
            ingredient_rxcui=None,  # resolved at runtime by the normalizer, not hardcoded
        ),
        subject_id=patient["id"],
        requester_id=prescriber["id"],
        authored_on=AUTHORED_ON,
        dose_value=100,
        dose_unit="UNT/ML",
        route_text="Subcutaneous",
        quantity_value=1,
        quantity_unit="pen",
    )

    return SeededDataset(
        patient=patient,
        prescriber=prescriber,
        courier=courier,
        ward=ward,
        service=service,
        request=request,
    )


def write_all(client: FhirClient, dataset: SeededDataset) -> None:
    """Write every resource in dependency order.

    ``FhirClient.update`` is a PUT with a client-assigned id, so this is create-or-replace:
    a 201 on first run, a 200 on every subsequent one, with identical end state either way.
    """
    for resource in dataset.resources():
        client.update(resource)
        print(f"  wrote {resource['resourceType']}/{resource['id']}")


def order_identifiers(dataset: SeededDataset) -> tuple[str, str]:
    """``(placer, filler)`` as carried on the seeded request.

    Read off the resource rather than from ``app.demo``, so a tokenized dataset verifies
    against its own identifiers instead of the demo's.
    """
    by_system = {i["system"]: i["value"] for i in dataset.request["identifier"]}
    return by_system[build.SYSTEM_PLACER_ORDER], by_system[build.SYSTEM_FILLER_ORDER]


def verify(client: FhirClient, dataset: SeededDataset) -> list[str]:
    """Read back through the *search* path the pipeline will actually use.

    Deliberately not a ``read`` by id: the pipeline correlates an HL7 order to a
    prescription by identifier, so that is the lookup that has to work. A read-by-id
    would pass even if the identifier were indexed wrongly.

    Returns a list of human-readable problems; empty means healthy.
    """
    problems: list[str] = []
    request_id = dataset.request["id"]
    placer, filler = order_identifiers(dataset)

    hits = client.search_by_identifier("MedicationRequest", build.SYSTEM_PLACER_ORDER, placer)
    if len(hits) != 1:
        problems.append(
            f"placer-order search returned {len(hits)} results, expected exactly 1. "
            "More than one usually means DEMO_NAMESPACE is not unique to you — pick "
            "another one in .env and re-seed."
        )
    elif hits[0].get("id") != request_id:
        problems.append(f"placer-order search matched {hits[0].get('id')}, expected {request_id}")

    filler_hits = client.search_by_identifier(
        "MedicationRequest", build.SYSTEM_FILLER_ORDER, filler
    )
    if len(filler_hits) != 1:
        problems.append(f"filler-order search returned {len(filler_hits)} results, expected 1")

    if client.read("Patient", dataset.patient["id"]) is None:
        problems.append("seeded Patient is not readable by id")
    if client.read("MedicationRequest", request_id) is None:
        problems.append("seeded MedicationRequest is not readable by id")

    return problems


def purge(client: FhirClient, dataset: SeededDataset, *, raise_on_error: bool = True) -> list[str]:
    """Delete the demo resources (reverse dependency order). 404s and 410s are ignored.

    ``raise_on_error=False`` makes this best-effort and returns the ids that could not be
    deleted. The public sandbox intermittently returns a 5xx or times out mid-purge, and a
    *teardown* that fails turns a passing test into an error — which reads as a defect in
    this code when it is a property of a shared free server. The default stays strict
    because ``make purge`` is a deliberate human action where a silent partial failure is
    the worse outcome: the caller is told what is left behind.
    """
    failures: list[str] = []
    for resource in reversed(dataset.resources()):
        try:
            client.delete(resource["resourceType"], resource["id"])
        except FhirError as exc:
            if raise_on_error:
                raise
            failures.append(f"{resource['resourceType']}/{resource['id']} ({exc})")
    return failures


def explain_conflict(exc: FhirError, settings: Settings) -> str:
    """Turn HAPI's terse 412 into something actionable.

    HAPI enforces uniqueness on ``Patient.identifier`` and ``MedicationRequest.identifier``,
    so namespacing the resource id is not sufficient on its own: a Patient holding
    ``MRN12345`` blocks a second one regardless of id. The failure mode this most often
    shows up as is changing ``DEMO_NAMESPACE`` and re-seeding — the new namespace creates
    fresh ids, but the old Patient still owns the MRN.
    """
    return (
        f"{exc}\n"
        "  HAPI will not create a second resource carrying the same identifier\n"
        f"  (HAPI-2840). Something already holds this MRN / order number under\n"
        "  'urn:dna:coldchain:*' — usually a previous run under a different namespace.\n"
        "  Either purge that namespace:\n"
        "      DEMO_NAMESPACE=<old-namespace> python -m app.fhir.seed --purge\n"
        f"  or keep '{settings.demo_namespace}' and change MRN / PLACER_ORDER_NUMBER /\n"
        "  FILLER_ORDER_NUMBER in app/demo.py, updating the HL7 fixtures' PID-3 and\n"
        "  ORC-2/ORC-3 to match."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the cold-chain demo dataset.")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="only check that the dataset is present and searchable; write nothing",
    )
    parser.add_argument(
        "--purge", action="store_true", help="delete the demo resources instead of writing"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    dataset = build_dataset(settings)

    with FhirClient(settings.fhir_root, timeout_s=settings.fhir_timeout_s) as client:
        if args.purge:
            purge(client, dataset)
            print(f"purged namespace {settings.demo_namespace}")
            return 0

        if not args.verify:
            # Ids only. Not the patient name: this is a logging surface, and the same
            # discipline the alert scrubber enforces applies to our own output.
            try:
                write_all(client, dataset)
            except FhirError as exc:
                if exc.status == 412:
                    print(explain_conflict(exc, settings), file=sys.stderr)
                    return 1
                raise
            print(f"seeded namespace {settings.demo_namespace}")

        problems = verify(client, dataset)
        if problems:
            for problem in problems:
                print(f"  FAIL {problem}", file=sys.stderr)
            return 1

        placer, filler = order_identifiers(dataset)
        print(
            f"  OK   MedicationRequest {dataset.request['id']} found by placer "
            f"({placer}) and filler ({filler}) identifier"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
