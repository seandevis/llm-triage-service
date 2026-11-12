# triage-service — workshop repo
#
# Native path (no Docker required):  make dev / make test / make stats
# The native path is the supported fallback and must always work.

VENV    ?= .venv
# Windows venvs put executables in Scripts/, POSIX ones in bin/.
ifeq ($(OS),Windows_NT)
VENV_BIN ?= $(VENV)/Scripts
else
VENV_BIN ?= $(VENV)/bin
endif
PY      ?= $(VENV_BIN)/python
PIP     ?= $(VENV_BIN)/pip
PYTEST  ?= $(VENV_BIN)/pytest
HOST    ?= 127.0.0.1
PORT    ?= 8080
MOCK_PORT ?= 8081

.DEFAULT_GOAL := help
.PHONY: help venv dev mock test test-fast test-stages test-graded homework golden demo gate stats clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv and install pinned dependencies
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install -q -r requirements.txt

# .env when it exists, the way compose reads it. A variable already set in the
# shell wins, so `FAULT_RATE=0.4 make mock` does what it says.
ENV_FILE = $(if $(wildcard .env),--env-file .env,)

dev: ## Run the API on $(HOST):$(PORT) with reload
	$(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload $(ENV_FILE)

mock: ## Run the Anthropic-dialect mock on $(HOST):$(MOCK_PORT)   [stage 2]
	$(PY) -m uvicorn mock.server:app --host $(HOST) --port $(MOCK_PORT) $(ENV_FILE)

test-fast: ## The inner loop: no chaos, no timing tests. Run this while working.
	FAULT_RATE=0.0 $(PYTEST) -m "not chaos and not latency" --ignore=tests/homework

test-stages: ## Only the participant-facing tests (tests/stages/)
	$(PYTEST) tests/stages

test: ## The full suite at the gate's chaos level. This decides if a stage is done.
	FAULT_RATE=0.4 TEMPERATURE=0.7 $(PYTEST) --ignore=tests/homework

test-graded: ## The full suite with the mock refusing per-request fault headers
	MOCK_ALLOW_HEADER_OVERRIDE=false FAULT_RATE=0.4 TEMPERATURE=0.7 $(PYTEST) --ignore=tests/homework

homework: ## The homework tasks only (HOMEWORK.md)
	FAULT_RATE=0.4 TEMPERATURE=0.7 $(PYTEST) tests/homework

golden: ## Score the golden set across seeds (stage 4). SEEDS=20 MODE=clean|bad_content
	$(PY) scripts/score_golden.py --seeds $(or $(SEEDS),20) --mode $(or $(MODE),clean)

demo: ## Demo scenario for the recorded session: make demo S=chaos (no S lists them)
	$(PY) scripts/demo.py $(S)

gate: ## Ten consecutive green runs at FAULT_RATE=0.4 + the graded run
	bash scripts/check_suite_stable.sh 10

stats: ## Print the mock's /stats counters                        [stage 2]
	@curl -sS http://$(HOST):$(MOCK_PORT)/stats | $(PY) -m json.tool

clean: ## Remove caches and build droppings
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache
