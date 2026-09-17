# Write-up — Case 1, pharmacy cold chain to IPD

**Try it first: <https://coldchain-gate.vercel.app>.** Seven buttons, one per indent, and the
six that get *refused* are the ones worth pressing. `README.md` says what each part of the page
is showing; this document is the reasoning behind the system it is showing.

## Approach

I treated the brief's "Common Mistakes" table as the real specification, since it names the
failure modes precisely. Four decisions follow from it directly.

**Drug validation is a code lookup, never a string comparison.** `app/rxnorm/normalize.py`
resolves the ordered product to an RxNorm concept and compares *ingredient, strength and
dose form as codes* against the prescription's. The interesting part is not the comparison
but the two filters that stop it degrading into fuzzy matching. A strength no product has
(`500 UNT/ML`) still returns candidates from RxNav — they share the ingredient and nothing
else; unfiltered, the request "resolves" to the 100 pen and the system reports a *strength
mismatch*, which is a clinical claim arrived at by substring matching. And a vial indent's
nearest concept carries no dose form at all, so unfiltered it silently attaches to a
pen-injector prescription. Token-coverage and dose-form-presence filters remove both. Each
is pinned by a test recorded against the live API.

**Order correlation requires agreement, not a fallback.** When an inbound OMP carries both
ORC-2 (placer) and ORC-3 (filler) identifiers, each must resolve to the same single
`MedicationRequest`; a match on one cannot silently override a contradiction in the other.
If only one identifier is populated, that one may correlate the order. The hold detail and
the `AuditEvent` deliberately omit both values because either is patient-linkable.

**The HL7 message is parsed with a library, not with `split`.** `app/hl7/parse.py` uses
`hl7apy` against the v2.5 grammar. Nothing in the codebase regexes a segment — including
the fixtures, which are generated through the same grammar so they are valid by
construction.

**The privacy boundary is an allowlist and a refusal, not a scrubber.** `ALERT_FIELDS` names
the only fields an outbound alert may carry, and `assert_alert_shaped` raises
`PrivacyViolation` immediately before send rather than sanitising and proceeding. A denylist
leaks the first field nobody thought of. The subtle case: the HL7 message control id and the
order number are not patient data in themselves, but `/health` publishes the namespace, so
`namespace + control id` reconstructs the audit-event id, and that walks to the Patient in
two hops. Both are excluded from anything outbound — a leak I found by asking what the
already-published values let you derive, not by reading the field list.

**The audit trail is tamper-*evident*, and says so.** The brief asks for "tamper-proof"; on a
server we do not control, nothing is. Each event hashes
`prev_hash | sequence | canonical(payload)`, and the payload is embedded verbatim in the
`AuditEvent`, so verification needs only the server and trusts neither this code nor this
process. That catches edits, deletions and reordering. It does **not** catch truncation of
the newest events — a shortened chain is still internally consistent. Detecting that needs
the head hash anchored somewhere the attacker cannot reach, which is why the module and the
README say "evident" throughout. The gap is pinned by a test so the claim cannot drift.

The response also distinguishes an observed handoff from a persisted audit trail. If the
FHIR server rejects the final audit writes after the dispense is recorded, the service does
not claim success for the trail: it returns `audit_recorded: false`. Retrying that audit work
durably is a production concern, not something an in-process demo can guarantee.

Every outcome files an event, including the ones that stop — a message that will not parse
is precisely what an auditor goes looking for. That branch was initially the exception: it
returned before the write, so an unreadable indent reached the response but left nothing on
the server. The fix needed an event id, and the usual stem (the HL7 message control id) does
not exist for a message that would not parse, so it is a truncated digest of the raw bytes.
That is not a disclosure — the ER7 *is* the PHI-bearing artifact, so anyone holding it
already holds everything the digest could lead back to — and it stays deterministic, so
re-processing the same bad message replaces its event rather than accumulating duplicates.

**The demo page exists because the brief's reader is not necessarily an engineer.** The brief
asks for five backend deliverables and never mentions a UI, so `ui/index.html` is an extra —
but the argument this system makes is carried by a *refusal*, and a refusal is invisible from
the outside: an indent that stops at the gate looks exactly like one that was never sent. The
page makes it visible. Seven buttons, one per fixture, each sending a real HL7 message through
the pipeline; press the wrong-strength one and the four-field allowlist column empties, the
phone says nothing was sent, and the audit chain shows the two events a hold writes instead of
the four a dispense does. Nothing on the page is typed in by hand — verdicts, reason codes and
digests all come from a live run, or from a recorded one when no server is answering.

Two constraints shaped it. The internal-record panel is deliberately *not* fetched: `Run.public()`
builds the wire view from an allowlist and only the CLI reaches `Run.internal()`, so a route that
put the patient on a browser would have undercut the boundary the page is there to demonstrate.
That panel is the seeded demo patient, labelled as such. And the recorded fallback is held to the
same boundary as the wire — `tests/test_ui.py` asserts it is PHI-free, that its chain actually
links, and that it still matches a fresh pipeline run, so it cannot quietly become a second
source of truth.

The page is also deployed, at <https://coldchain-gate.vercel.app>, because asking a reviewer to
clone a repository and read a terminal is a worse first impression than a link. `api/index.py`
runs the identical `create_app` with **nothing injected**: the click resolves the drug against
the live NIH RxNav service and writes the `MedicationDispense` and its `AuditEvent` chain to the
public HAPI sandbox. I built the pinned version first — FHIR in memory, RxNav replayed from the
recorded payloads — and it was the wrong call for this artefact. A recording makes the page a
screenshot with buttons: it cannot show the case where the pipeline refuses because a *lookup*
failed, and it puts a value on screen that the reader has no way to tell apart from a live one.
The page exists to make a refusal visible; a version of it that can only produce the refusals I
already knew about is not doing that job.

The cost is real and worth stating rather than discovering. A click now depends on two services
nobody here controls, so a slow minute at NLM becomes a `503` and reads as a broken demo — hence
`/health` reporting the upstream URLs it will actually call, and the client timeouts being set
shorter than the library defaults so the pipeline's own `503` is what a judge sees rather than
Vercel's `maxDuration` kill, which returns an opaque platform error page. Writing to a shared
server is the other cost, and it is bounded rather than waved away: the dataset is namespaced,
and resources go by `PUT` with client-assigned ids derived from the order number, so repeat
presses replace their own resources instead of accumulating. Pressing all seven buttons leaves
seven dispenses or holds and their trails.

One deployment detail is worth recording because it was a real near-miss, and because the fix
is the same line that makes the live version live. `app/main.py` ends with a module-level
`app = create_app()` so `uvicorn app.main:app` works, and Vercel's FastAPI detection finds that
object in preference to `api/index.py`. The first deploy therefore served an application whose
configuration *nobody had chosen* — read from whatever the environment happened to hold.
Nothing in the page or the responses revealed it; `/health` did, and only because it is the one
route that reports configuration rather than liveness. `framework: null` in `vercel.json` is
what makes the intended entry point the only one. That the accidental app was also writing to a
public sandbox is what made it alarming at the time — but the point is the general one, since
the next stray entry point could have pointed anywhere.

A second near-miss is worth recording for a different reason, because it marks a limit of the
test suite rather than of the code. The banner's sentence is *composed* — a lead clause, a colon,
then the pipeline's `detail` — and both halves were correct while the result was not: `detail` is
authored lowercase because the same string is embedded in audit events, and the page appended it
after a full stop, so the line a judge is meant to read began "…no phone was notified. prescribed
100 UNT/ML…". The banner's second line had the same shape of problem, printing the raw
`strength_mismatch` where a judge needed words. Neither is visible to `tests/test_ui.py`: every
string involved is individually correct, the defect exists only in the rendered sentence, and the
suite does not run JavaScript. I found them by executing the page's own script against a stub DOM
and reading the output for all seven fixtures — which is also how the "Correct drug" sentence
turned out to open with a lowercase drug name, since `insulin glargine` is an INN and is spelled
that way. The suite's guarantee stops at the edge of the rendered page, and saying so is better
than implying it covers more.

A third defect belongs to the same family — a value that is individually correct while meaning
something other than what it says — and it was found by reading what the deployed process
actually reports rather than what the code says it reports. `/health` published
`notifications_held` as `len(notifier.deliveries)`, which is every send *attempt*, the
successful ones included. It read `1` after a clean dispense: the one run in the demo where a
notification is certainly not held back, reported under a name saying it was. Nothing failed and
no test went red. It now reports delivered and failed as separate counters, and a test drives a
dispense and a hold through one service and pins both — including that a hold moves neither,
because a held indent never reaches the notifier at all.

## Assumptions
The brief invites these; each is a place I chose a reading and moved.

1. **No auth for Case 1.** The auth rubric row is generic across the three cases, and Case 1
   names five build requirements, none of them auth. Building an OAuth2/SMART layer against
   an open public sandbox would be security theatre. It is first in the list below.
2. **One order per message.** A message carrying two items is held
   (`multi_item_order_unsupported`), not split. Choosing among items on the ward's behalf is
   a clinical decision this system should not make.
3. **Only a new order is dispensable.** ORC-1 must be `NW`; `XO` is held. An order control
   the pipeline does not recognise is a hold, never a pass.
4. **Compound orders (`RXC`) are out of scope.** Component preparation is a different
   process with different safety properties; the honest answer is a hold.
5. **The courier's name is not PHI.** It is workforce identity, and the ward needs to know
   who is at the door. The patient's name, MRN and DOB are.
6. **`FHIR R4`, deliberately.** The obvious library, `fhir.resources`, ships R4B and STU3
   models but no R4 sub-package in any current release — and the brief links the R4 spec. So
   the payload builders are plain dicts with field names read off `hl7.org/fhir/R4`, which
   also makes the payload contracts assertable without a server.
7. **The ETA is a local extension.** R4 has `whenPrepared`, `whenHandedOver` and `performer`
   but no expected-delivery field, and the R4 extension registry has nothing suitable. The
   standards-native alternative is a separate `SupplyDelivery` resource, whose
   `occurrence[x]` means exactly this — I did not take it because it splits the answer the
   brief asks for across two resources. URL documented in the README.
8. **Dose form cannot come from HL7.** In v2.5 `RXO-5 REQUESTED_DOSAGE_FORM` is a CE with
   **no assigned HL7 table**; `HL70162` is *Route of Administration*, correct on RXR-1 and
   wrong here. A real deployment would source dose-form codes from NCI Thesaurus (or a
   terminology server); offline, the fixture cites a local code rather than inventing a
   table reference that looks plausible and is false.
9. **Public HAPI sandbox, not local Docker.** Docker is unavailable on this machine, so the
   demo targets `hapi.fhir.org/baseR4`. Nothing assumes the public server beyond
   `FHIR_BASE_URL`; production would be a local HAPI JPA starter.
10. **The chain is scoped per indent.** Each message gets its own chain from genesis. This
    keeps an indent's trail verifiable independently, at the cost that deleting an *entire*
    indent's events is invisible — see improvement 2.

## What I'd improve with more time

1. **OAuth2 + SMART on FHIR with PKCE, and mTLS to the device gateway.** The backend-services
   flow for the FHIR calls, and a per-device credential for the notification path, so a
   compromised ward tablet cannot post indents.
2. **Anchor the chain head externally.** Per-indent chains (assumption 10) mean whole-trail
   deletion is undetectable. A global chain, or a periodic head hash signed and published
   somewhere the writer cannot reach, closes both that and tail truncation.
3. **Make the notifier durable and idempotent across restarts.** Delivery memory is
   in-process, so a redeploy can re-notify. A unique constraint on the notification token in
   Postgres is the right shape.
4. **`SupplyDelivery` alongside `MedicationDispense`** for the ETA, plus a published
   `StructureDefinition` for the extension instead of a bare URL.
5. **Dose-form codes from a terminology server** (NCI Thesaurus via RxNav's `ndcproperties`
   or a SNOMED CT server), replacing assumption 8's local code.
6. **Push instead of poll.** `POST /indent` models an interface calling us; FHIR
   `Subscription` on `MedicationRequest` would let the gate react to the prescription
   instead.
7. **Production hardening:** multiple workers with the state moved out of process, rate
   limiting, structured audit shipping, and a real device gateway in place of the in-process
   notifier.
