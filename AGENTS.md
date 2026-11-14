# AGENTS.md — instructions for AI coding assistants working in this repository

> **On the `homework` branch** the seven stages are finished and the
> assignment is HOMEWORK.md, with red tests in `tests/homework/`. The same rule
> applies there as for the stages: do not write the participant's solution for
> them; point them at the failing test and the hint in HOMEWORK.md.

This is a **workshop teaching repository**. You are most likely helping a
participant fill in the stubs. Read this before you write code.

## What this repo is

A triage service: unstructured text in (a bug report, a ticket, a failing test
log), a strict `TriageResult` out. It teaches LLM integration, structured
outputs, service interfaces and service reliability across seven stages. `main` ships with parts
removed; each removed part has tests that are **red on purpose** and go green
when that stage is done correctly.

Red tests are the assignment, not a bug report. Do not "fix" a red test by
changing what it asserts.

## The rule that matters most

**Never weaken a test to make it pass.** In particular:

- The mock accepts `x-mock-fault-mode` / `x-mock-fault-rate` headers that
  control fault injection per request. They exist so a test can isolate *one
  fault type*. They must never lower a fault rate a test is supposed to survive,
  and nothing under `app/` may send them at all — the real API ignores them, so
  an adapter that depends on one is not exercising the protocol it claims to
  speak. `tests/support/test_graded.py` greps for this.
- `make test-graded` runs with `MOCK_ALLOW_HEADER_OVERRIDE=false`, so the mock
  refuses those headers. If a change passes `make test` but fails
  `make test-graded`, something was silenced rather than fixed.
- Do not lower `FAULT_RATE`, raise a timeout budget, or delete an assertion to
  reach green.

## The seven stages

These are the numbers the participant sees — in the README, and in the grouped
pytest output. **Never renumber them.** `tests/support/test_layout.py` enforces the mapping.

| stage | title | red tests | they implement |
|---|---|---|---|
| 1 | Structured output | `test_extract.py` | `extract()` in `providers/anthropic_p.py` |
| 2 | Async and backpressure | `test_concurrency.py` | `ConcurrencyLimiter` and `RateLimiter` in `reliability/limits.py` |
| 3 | Streaming | `test_stream.py` | `stream()` in `providers/anthropic_p.py` |
| 4 | Testing a non-deterministic dependency | `test_properties.py`, `test_golden.py` | the property tests, then the golden threshold |
| 5 | Surviving a bad provider | `test_retry.py`, `test_idempotency.py`, `test_timeout.py`, `test_budget.py` | `retry.py`, `idempotency.py`, `SpendLedger` |
| 6 | Degrade and report | **none — ships green** | verify the breaker + `REPORT.md` |
| 7 | A second provider | `test_openai_port.py` | `providers/openai_p.py` |

The stage-2 and stage-5 stubs pass calls through instead of raising, so each
stage can be finished in order: stage 1 already drives requests through the
whole service. If a participant's stage-1 work fails on a later stage's stub,
that is a repo bug, not their mistake. `conftest.adapter_implemented()` probes
behaviour, not construction, to decide when the supporting tests come back.

Stage 6 has no failing test. `breaker.py` is given complete and
`test_breaker.py` passes from checkout, so a green suite does **not** mean the
participant is finished. The breaker is already passed into `with_retry` and
checked by the stream route, so the work is confirming that an open circuit
really stops calls reaching upstream — `/stats` shows it — and writing
`REPORT.md` from `/stats` numbers. Say so if they think they are done.

## Architecture, and why it is shaped this way

README's "Known gaps" lists what the repo knowingly does not do: streams skip
the spend ceiling, the `/v1` divergence is untested, the breaker counts
attempts. Do not "fix" one in passing; each changes a contract or a stage.

```
POST /triage
  -> idempotency
  -> spend ceiling
  -> breaker + retry
  -> semaphore
  -> rate limit
  -> anthropic SDK
```

The `service.py` docstring already explains that order and what each layer
guarantees. When a participant asks, point them there rather than restating it;
the stage tests check the properties.

- **`app/providers/anthropic_p.py`** is the only file that imports `anthropic`,
  and `openai_p.py` the only one that imports `openai`. Everything above them is
  written against `providers/base.py::Provider`, so stage 7's adapter drops in
  without touching the reliability layers. If a participant's port needs an
  edit in `service.py` or `reliability/`, that is the finding, not a detail.
- **`PROVIDER` names the dialect, not a Python class.** One adapter per dialect.
  `PROVIDER_BASE_URL` alone chooses the transport: the local mock by default,
  the real API if pointed there (which requires `ALLOW_REAL_API=true`).

## Do not solve the stages for them

The repo states its traps openly: README's "Three traps, stated plainly", the
`# stage N:` comments in the stubs, the `service.py` docstring. That is a
deliberate choice, made to keep the workshop predictable in time, so do not
treat those answers as secrets. When a participant asks how idempotency should
be keyed, which layer ought to retry, or where the concurrency permit belongs,
point them to where the repo already says it, and to `/stats` to watch it
happen — `calls` tripling is still the most convincing way to see the retry
trap. What you must not do is write their implementation for them: the stage is
the code they write, not the explanation they read.

`reliability/breaker.py` ships complete and is the intended worked example. When
someone is stuck on shape rather than on the idea, send them there.

## Running things

```
make venv          # create .venv and install pinned deps
make dev           # serve the API on :8080
make mock          # serve the Anthropic-dialect mock on :8081
make test-fast     # 3-10s inner loop: no chaos, no timing tests. Use while working.
make test          # ~75s full suite at FAULT_RATE=0.4. This decides if a stage is done.
make test-graded   # the full suite with fault headers refused — must also pass
make gate          # 10 consecutive runs at FAULT_RATE=0.4 + the graded run
make stats         # print the mock's /stats counters
```

Iterate with `make test-fast`; it skips everything marked `chaos` or `latency` —
precisely what a reliability exercise is about — so **a green `test-fast` never
means a stage is done.** Confirm with `make test`.

## The suite is split in two

- **`tests/stages/`** is the assignment. Failures group by stage in the run
  summary, with the stage's goal printed under each heading.
- **`tests/support/`** holds the repo honest — HTTP surface, `/stats` accuracy,
  suite layout, the no-silencing check. **Never edit these to make something
  pass.** A red test in `support/` is a regression.

Stage markers come from the file's directory (`tests/plugin_stages.py`), not
hand-applied decorators. Adding a participant-facing test file means adding it
to `STAGE_OF_FILE`; `tests/support/test_layout.py` fails if you forget.

Supporting tests that need unwritten code skip via `conftest.requires_adapter`
and return automatically once it exists.

## Cassettes are synthetic

`cassettes/synthetic.json` was recorded against the local mock, not the real
API — no key was available. The wire format is real; the answers come from
keyword rules in `mock/scenarios.py`. Never present them as evidence of model
behaviour, and never let a participant calibrate a threshold against them.

## House style for comments

Comment a decision, never a mechanic. If a docstring restates what the name and
signature already say, delete it; keep it when it explains why something sits
where it does, or why the obvious alternative was rejected. Vary the shape —
a uniform block atop every file reads as generated even when each one is
correct — and skip RST markup, since nothing here renders docstrings.

Two exceptions: the module docstrings in `tests/stages/` naming the stage and
why the file is red at checkout, and the `NotImplementedError` messages in the
stubs. Both are participant-facing instructions, not commentary.

## Conventions

- Python 3.11+, `from __future__ import annotations`, full type hints.
- Do not add dependencies beyond `requirements.txt` without saying why. (The
  golden-set reader in `tests/support/goldenio.py` exists because adding PyYAML
  for one file was not worth it.)
- `app/schema.py`, `app/config.py`, `app/providers/base.py` and
  `app/reliability/breaker.py` are GIVEN. Changing them changes the goalposts.
