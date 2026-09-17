# Cold-chain demo. `make seed` then `make test` are the two that matter.
#
# Everything runs out of .venv; the Quickstart in README.md creates it. Override
# with `make PY=python3 ...` if you would rather use a system interpreter.

PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: seed
seed:  ## Write the demo dataset to the FHIR sandbox (idempotent, safe to re-run)
	$(PY) -m app.fhir.seed

.PHONY: verify
verify:  ## Check the dataset is present and findable by identifier; write nothing
	$(PY) -m app.fhir.seed --verify

.PHONY: purge
purge:  ## Delete the demo resources from the sandbox
	$(PY) -m app.fhir.seed --purge

.PHONY: run
run:  ## Start the API (uvicorn --reload)
	$(PY) -m uvicorn app.main:app --reload --port $(PORT)

.PHONY: run-offline
run-offline:  ## Start the API with FHIR served from memory: no sandbox, no writes
	FHIR_MODE=dry-run $(PY) -m uvicorn app.main:app --reload --port $(PORT)

.PHONY: smoke
smoke:  ## POST one fixture to a server that is already running (see: make run-offline)
	@curl -sS -m 5 -o /dev/null "http://127.0.0.1:$(PORT)/health" 2>/dev/null \
		|| { echo "Nothing answered on port $(PORT)."; \
		     echo "Start one first:  make run-offline    # FHIR from memory, no writes"; \
		     exit 1; }
	@curl -sS -m 5 -X POST "http://127.0.0.1:$(PORT)/indent/demo/clean" | $(PY) -m json.tool

.PHONY: test
test:  ## Run the test suite (offline; no network)
	$(PY) -m pytest

.PHONY: integration
integration:  ## Run the tests that hit the live sandbox and RxNav
	$(PY) -m pytest -m integration

.PHONY: record-rxnav
record-rxnav:  ## Re-record the RxNav responses the offline tests replay (adds what is missing)
	$(PY) scripts/record_rxnav.py

.PHONY: demo
demo: seed  ## Seed, then walk every HL7 fixture through the pipeline
	$(PY) -m app.pipeline --demo

.PHONY: ui-data
ui-data:  ## Re-record the runs the demo page falls back to when no server is running
	$(PY) scripts/build_ui_demo.py

.PHONY: deploy
deploy: test  ## Publish the hosted demo (api/index.py) to production
	@# `test` first, and not as a formality: this pushes a public URL, and the entry point it
	@# deploys is the one that pins both dependencies offline. Deploying a red tree to a
	@# production alias is the one mistake here that other people can see.
	vercel deploy --prod --yes

.PHONY: clean
clean:  ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
