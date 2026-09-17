"""Runtime configuration.

Every setting has a working default so a fresh clone runs without a .env file. The one
value worth changing is DEMO_NAMESPACE: all client-assigned FHIR ids are prefixed with it,
so a demo run cannot collide with another candidate's records on the shared public sandbox.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    fhir_base_url: str = "https://hapi.fhir.org/baseR4"
    fhir_timeout_s: float = 30.0

    rxnav_base_url: str = "https://rxnav.nlm.nih.gov/REST"
    rxnav_timeout_s: float = 20.0
    rxnav_cache_ttl_s: int = 3600

    fhir_mode: Literal["sandbox", "dry-run"] = Field(
        default="sandbox",
        description=(
            "Where the API reads and writes. 'dry-run' serves the seeded dataset from "
            "memory and keeps writes for the life of the process: no sandbox traffic and "
            "nothing written to a shared server. The RxNorm lookup is a separate service "
            "and still goes out — see .env.example."
        ),
    )

    demo_namespace: str = Field(
        # The demo dataset is already published to the public sandbox under this
        # namespace, and HAPI enforces uniqueness on Patient.identifier globally, so
        # MRN12345 has exactly one owner. Defaulting to anything else would make a
        # fresh clone fail on its first `make seed` with HAPI-2840 rather than finding
        # the records it expects. Changing this means changing app/demo.py too —
        # app.fhir.seed.explain_conflict spells that out.
        default="sajib-cc-58f1",
        pattern=r"^[A-Za-z0-9\-]{1,40}$",
        description="Prefix for every client-assigned FHIR id.",
    )

    @property
    def fhir_root(self) -> str:
        return self.fhir_base_url.rstrip("/")

    @property
    def rxnav_root(self) -> str:
        return self.rxnav_base_url.rstrip("/")

    def ns_id(self, local: str) -> str:
        """Mint a sandbox-safe, namespaced FHIR id.

        Namespacing is what keeps `make seed` idempotent *and* keeps this demo from
        writing over anyone else's records on the public server.
        """
        return f"{self.demo_namespace}-{local}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
