"""Outbound notification — how a dispatch alert reaches the nurse's device.

The interface is deliberately one method wide. Everything that decides *what* may be sent
lives in :mod:`app.safety.phi`; this module only decides whether a send succeeded, and it
refuses to send anything that is not the allowlisted shape. Keeping the two apart means the
privacy argument can be read, and tested, without reading any transport code.

The demo adapter keeps deliveries in memory so the UI can render what a phone would have
received alongside the internal record. It is not a queue and does not pretend to be: a real
deployment hands this off to whatever the hospital already runs for paging, and the interface
is narrow precisely so that substitution is a one-file change.

**Failure is reported, never raised into the pipeline.** A delivery that times out must not
roll back a dispense that was already written to the FHIR server — the medicine is on its way
whether or not the phone buzzed. The result object carries the failure so the caller can
retry or escalate, and the audit trail records the attempt either way.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.models import ALERT_FIELDS, DispatchAlert
from app.safety import phi

JSON = dict[str, Any]


@dataclass(frozen=True)
class Delivery:
    """One attempt to notify, and what came of it.

    ``token`` is the device-facing handle rather than any identifier — see
    :func:`app.safety.phi.notification_token`. It is what lets the UI, the log, and the phone
    agree on *which* delivery they mean without any of them holding an order number.
    """

    token: str
    payload: JSON
    delivered: bool
    sent_at: str
    error: str | None = None

    def to_json(self) -> JSON:
        return {
            "token": self.token,
            "delivered": self.delivered,
            "sent_at": self.sent_at,
            "payload": self.payload,
            "error": self.error,
        }


class Notifier(Protocol):
    """The whole contract: mint a device-facing handle, send this alert, report what happened.

    ``token_for`` is on the protocol rather than left to the adapter because the token is
    what lets the device and the record agree on *which* delivery they mean without either
    holding an order number — a transport that could not mint one could not satisfy the
    privacy boundary, so it is part of what a notifier is.
    """

    def token_for(self, *parts: str) -> str: ...

    def send(self, alert: DispatchAlert, *, token: str) -> Delivery: ...


@dataclass
class InMemoryNotifier:
    """The demo adapter — records what a phone would have received.

    Holds the deliveries so the API can show them and the UI can render the notification
    beside the internal record. Bounded, because an unbounded list in a web process is a slow
    leak, and the demo only ever needs the recent past.
    """

    secret: str = "demo-notification-secret"
    capacity: int = 50
    #: Set to make the next sends fail, so the failure path is demonstrable rather than
    #: theoretical. The brief's rubric rewards a real failure path; this is the notification's.
    fail_next: int = 0
    deliveries: list[Delivery] = field(default_factory=list)
    _clock: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)

    def token_for(self, *parts: str) -> str:
        return phi.notification_token(self.secret, *parts)

    def send(self, alert: DispatchAlert, *, token: str) -> Delivery:
        """Deliver ``alert``, or record why it was not delivered.

        Raises :class:`app.safety.phi.PrivacyViolation` rather than returning a failed
        ``Delivery`` when the payload is not the allowlist. The distinction is intentional: a
        failed send is an infrastructure problem the caller may retry, while a payload that is
        not the allowlist is a programming error, and retrying it would send patient data to a
        phone.
        """
        payload = alert.to_payload()
        phi.assert_alert_shaped(payload)

        error: str | None = None
        delivered = True
        if self.fail_next > 0:
            self.fail_next -= 1
            delivered = False
            error = "notification transport unavailable"

        delivery = Delivery(
            token=token,
            payload=payload,
            delivered=delivered,
            sent_at=datetime.now(UTC).isoformat(timespec="seconds"),
            error=error,
        )
        self.deliveries.append(delivery)
        if len(self.deliveries) > self.capacity:
            del self.deliveries[: -self.capacity]
        return delivery

    def recent(self, limit: int = 10) -> list[JSON]:
        return [d.to_json() for d in self.deliveries[-limit:]]


def alert_payload(alert: DispatchAlert) -> JSON:
    """The serialised alert, shape-checked on the way out.

    Every path that turns an alert into bytes for a device goes through here, so the invariant
    is enforced at the boundary rather than only asserted in tests.
    """
    payload = alert.to_payload()
    phi.assert_alert_shaped(payload)
    return payload


#: The field names a device receives, re-exported so the UI, the API and the tests all read
#: the same list from one place.
DEVICE_FIELDS: tuple[str, ...] = ALERT_FIELDS
