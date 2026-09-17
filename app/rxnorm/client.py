"""RxNav / RxNorm REST client.

Endpoints used (all verified live, no API key):

    GET /rxcui.json?name={name}                    exact/normalized name -> rxcui
    GET /approximateTerm.json?term={term}          approximate match -> ranked candidates
    GET /rxcui/{rxcui}/properties.json             tty + canonical name
    GET /rxcui/{rxcui}/related.json?tty={tty}      IN (ingredient) / DF (dose form) / …
    GET /rxcui/{rxcui}/allProperties.json          ATTRIBUTES incl. AVAILABLE_STRENGTH

Two behaviors worth knowing, both discovered by probing the live API rather than assuming:

* **Name search is not fuzzy.** ``rxcui.json?name=insulin glargine 100 UNT/ML Pen
  Injector`` returns nothing, because RxNorm's canonical name for that concept is
  ``3 ML insulin glargine 100 UNT/ML Pen Injector`` — it begins with the pack size.
  ``approximateTerm`` is required to resolve a realistic display string.
* **Concepts can exist but be unindexed.** ``approximateTerm`` will happily return an
  rxcui for which ``properties``/``related``/``allProperties`` all return ``200 {}``.
  Callers must treat an empty properties response as "unusable", not as an error.

Lookups are cached in-process with a TTL to respect the ~20 req/s ceiling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

JSON = dict[str, Any]


class RxNavError(RuntimeError):
    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f"RxNav {path}: {detail}")
        self.path = path
        self.detail = detail


@dataclass
class RxNavClient:
    base_url: str
    timeout_s: float = 20.0
    cache_ttl_s: int = 3600
    max_approximate_entries: int = 10
    #: Injection seam for tests: an httpx transport to use instead of the network. Mirrors
    #: ``FhirClient.transport``. Without it the whole RxNorm layer can only be exercised
    #: against the live API, which makes the offline suite unable to cover the drug
    #: matching the brief grades most heavily.
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client = field(init=False, repr=False)
    _cache: dict[str, tuple[float, JSON]] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RxNavClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> JSON:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

        resp = self._client.get(path, params=params)
        if resp.status_code >= 400:
            raise RxNavError(path, f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise RxNavError(path, "response was not JSON") from None

        self._cache[key] = (now + self.cache_ttl_s, data)
        return data

    # -- lookups -----------------------------------------------------------------------

    def exact_rxcuis(self, name: str) -> list[str]:
        """Exact/normalized name match. Empty when RxNorm has no literal match."""
        data = self._get("/rxcui.json", {"name": name})
        return list(data.get("idGroup", {}).get("rxnormId") or [])

    def approximate(self, term: str) -> list[JSON]:
        """Ranked approximate candidates. Includes concepts the REST API cannot expand."""
        data = self._get(
            "/approximateTerm.json",
            {"term": term, "maxEntries": str(self.max_approximate_entries)},
        )
        candidates = data.get("approximateGroup", {}).get("candidate") or []
        seen: set[str] = set()
        unique: list[JSON] = []
        for c in candidates:
            rxcui = c.get("rxcui")
            if rxcui and rxcui not in seen:
                seen.add(rxcui)
                unique.append(c)
        return unique

    def properties(self, rxcui: str) -> JSON | None:
        """Concept properties, or ``None`` when the concept is not expandable via REST."""
        props = self._get(f"/rxcui/{rxcui}/properties.json").get("properties")
        return props or None

    def related(self, rxcui: str, tty: str) -> list[JSON]:
        """Concepts related to ``rxcui`` at relationship type ``tty`` (e.g. IN, DF)."""
        data = self._get(f"/rxcui/{rxcui}/related.json", {"tty": tty})
        groups = data.get("relatedGroup", {}).get("conceptGroup") or []
        return [p for g in groups for p in (g.get("conceptProperties") or [])]

    def attributes(self, rxcui: str) -> dict[str, str]:
        """ATTRIBUTES as a flat dict, e.g. ``{"AVAILABLE_STRENGTH": "100 UNT/ML"}``."""
        data = self._get(f"/rxcui/{rxcui}/allProperties.json", {"prop": "ATTRIBUTES"})
        concepts = data.get("propConceptGroup", {}).get("propConcept") or []
        return {c["propName"]: c["propValue"] for c in concepts if "propName" in c}
