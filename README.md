# triage-service

> **This is the `homework` branch.** All seven stages are done here; the tasks
> are in [HOMEWORK.md](HOMEWORK.md). To build the stages yourself, use `main`.

A small service that turns unstructured text — a bug report, a ticket, a
failing test log — into a strict `TriageResult`. It is the workshop repository
for a module on **LLM integration, structured outputs and service reliability**.

You get it with parts removed. Seven stages put them back. Each stage has tests
that ship **red** and go green when you have done it correctly.

```
POST /triage        {"text": "..."}  ->  TriageResult
POST /triage/stream {"text": "..."}  ->  server-sent events
GET  /health                          ->  liveness
GET  /limits                          ->  what the reliability layers think
```

## Quick start

Needs **Python 3.11 or newer** and nothing else; `make venv` installs the pinned
dependencies into a local `.venv`.

```bash
cp .env.example .env
make venv
make dev            # http://127.0.0.1:8080/health
```

In another terminal:

```bash
make mock           # the Anthropic-dialect mock on :8081
make test-fast      # the inner loop, ~3s
```

Nothing here needs an API key or a network. The default `PROVIDER_BASE_URL`
points at the local mock, and the test suite starts its own copy in-process.

**Docker path**, verified with Docker 29.6.1 and Compose v5.3.0:

```bash
cp .env.example .env      # compose reads it; it will not start without it
docker compose up -d      # api :8080, mock :8081, redis :6379
curl localhost:8080/health
```

All three services come up and `/health` answers 200 with every stub still in
place. The native path (`make dev` / `make test`) remains the documented
fallback and is what the test suite uses — it needs no daemon.

---

## The seven stages

Run `make test` and the failures are grouped by stage, with the next thing to do
printed under each heading. Work down the list.

Every stage can be finished in order. A request goes through the whole service
from stage 1 on, so the stubs for later stages do not raise: they pass the call
straight through and do nothing useful. The semaphore bounds nothing, retry
tries once, the cache never hits, the ledger never refuses. Each stage's tests
are red until you replace that stage's pass-through, and they say why in terms
of what reached the mock. Comments marked `# stage N:` in the stubs say what to
write.

### Stage 1 — Structured output
**Red:** `tests/stages/test_extract.py`
**You write:** `extract()` and `extract_with_usage()` in `app/providers/anthropic_p.py`

Text in, a validated `TriageResult` out. Ask the provider for conforming output
with `output_config`, then validate what comes back anyway — the schema is a
request, not a guarantee.

The interesting part is the failures. `stop_reason` is not always `end_turn`:
`max_tokens` means the object is cut off mid-JSON, and `refusal` means the model
declined. Both are permanent — reported clearly, never retried, never surfaced
as a baffling JSON parse error.

### Stage 2 — Async and backpressure
**Red:** `tests/stages/test_concurrency.py`
**You write:** `ConcurrencyLimiter` and `RateLimiter` in `app/reliability/limits.py`

Bound in-flight calls to `MAX_CONCURRENCY`. Hold the permit around the upstream
call **only** — a permit held during work that is not the scarce resource is a
permit nobody else can use.

The test does not take your word for it. It reads `peak_concurrency` from the
mock's `/stats`: the highest number the mock ever had in flight at once. It also
checks you did not simply serialise everything — a semaphore is a limit, not a
queue of one.

Then the rate limit. Concurrency bounds how many calls are open at once; a
provider also bounds how many may *start* per window, and answers 429 past it.
`RateLimiter` enforces that limit on our side: at most `RATE_LIMIT_REQUESTS`
calls start in any `RATE_LIMIT_WINDOW_S` seconds, and the excess waits instead
of being sent. The retry layer would recover from the 429s eventually, so a
request succeeding proves nothing. The tests switch on the mock's own limit
(`MOCK_RATE_LIMIT_REQUESTS`) and read `calls_by_outcome.throttled` from
`/stats` — every throttled call is one the provider had to refuse for you.

### Stage 3 — Streaming
**Red:** `tests/stages/test_stream.py`
**You write:** `stream()` in `app/providers/anthropic_p.py`

First byte fast, and **never a partial object**. A stream that dies mid-object
must yield an `error` event, not a `done` carrying whatever parsed. Half a
`TriageResult` has the same shape as a whole one — the caller cannot tell, which
is what makes silent partial data worse than a loud failure.

Mark every `error` event `transient=True` or `False`. A severed stream may well
succeed next time; a refusal never will. The breaker counts only the first
kind — a bad input says nothing about whether upstream is healthy, and tripping
on one would let a single request take the service down for everyone.

### Stage 4 — Testing a non-deterministic dependency
**Red:** `tests/stages/test_properties.py`, `tests/stages/test_golden.py`
**You write:** the property tests, then a threshold in `samples/golden.yaml`

Four of the seven properties describe stage 5's behaviour: the contract under
chaos, idempotency, the retry budget and the spend ceiling. Write them now
anyway. With the pass-through stubs they fail, correctly, and they go green
when stage 5 does -- which is the point of writing a property before the code
it constrains. Write each one so that a stub could not pass it: "at most N
calls" is true of a retry that never retries, and "spend never went down" is
true of a ledger that records nothing.

The only stage where you write tests rather than code, and the reason it exists:
everything you have written so far checks *examples* — this input gives that
output. Examples pin down one point each. A property holds for every input.

When the vendor ships a new checkpoint next quarter, your example tests go red
and tell you almost nothing. "A stream ends in exactly one terminal event" and
"upstream calls never exceed `1 + MAX_RETRIES`" are still exactly as true and
exactly as load-bearing.

Then the golden set. Set its threshold **by measuring**: `make golden` scores
the set on 20 seeds, `make golden MODE=bad_content` does the same for
confidently-wrong output. Pick a bar the worst clean seed clears and the best
`bad_content` seed does not. If your threshold survives `bad_content`, it is
measuring nothing.

Why seeds: at `TEMPERATURE=0.7` the mock sometimes reads a hedged report
differently, the way a sampled model would, so one run is one draw from a
spread. At `TEMPERATURE=0` every seed scores the same, which is exactly why a
threshold measured there would not survive the temperature you deploy at.

### Stage 5 — Surviving a bad provider
**Red:** `test_retry.py`, `test_idempotency.py`, `test_timeout.py`, `test_budget.py`
**You write:** `reliability/retry.py`, `reliability/idempotency.py`, `SpendLedger` in `reliability/limits.py`

The big one. Three traps, each with its own section below. This stage also
runs stage 1's ten samples again at `FAULT_RATE=0.4`: stage 1 checked
extraction without faults, because surviving them is this stage's job.

### Stage 6 — Degrade and report
**Red:** *nothing.* `tests/stages/test_breaker.py` **ships green.**
**You write:** `REPORT.md`, after checking the breaker does its job

`reliability/breaker.py` is given complete, and its tests pass before you have
written a line of stage 1. **This stage has no failing test to chase**, which
means a green suite does not mean you are finished. The work is:

1. Check that the breaker does its job. `service.py` already passes it into
   `with_retry` and the stream route asks it before sending anything, so once
   your stage-5 retry honours it, an open circuit should stop calls reaching
   upstream and the API should answer 503 with `Retry-After`. Prove it with
   `/stats` against a failing mock (`FAULT_RATE=1.0 FAULT_MODE=rate_limit`).
2. Write `REPORT.md`: what you measured, using `/stats` numbers rather than
   impressions. What did one request cost under chaos? What did `peak_concurrency`
   read? Did you ever double-retry, and how did you find out?

   For a live `peak_concurrency` reading, start the mock with some latency
   (`MOCK_LATENCY_S=0.5 make mock`): at the default of 0 a request is over
   almost before the mock can count it as in flight.

Read `breaker.py` as the worked example — it is the shape the modules you wrote
in stages 1–5 should have ended up with.

### Stage 7 — A second provider
**Red:** `tests/stages/test_openai_port.py`
**You write:** `app/providers/openai_p.py`

The tests switch `PROVIDER=openai` and `MODEL=gpt-4o-mini` themselves. To run
the service on this dialect, set the same two in `.env`. The same mock
serves both dialects on the same port; the only other setting is the
credential, which is `OPENAI_API_KEY`, never the Anthropic one.

The exercise is not "write another adapter". It is to find out how much of what
you built in stages 1–5 was really about Anthropic. If the port costs you one
new file, the abstraction held; if you find yourself editing `service.py` or
anything under `reliability/`, it did not, and the interesting question is why.

**The five places the SDKs diverge.** This list is the point of the stage — the
code is the excuse.

1. **The structured-output parameter.** Anthropic takes
   `output_config={"format": {"type": "json_schema", "schema": ...}}`. OpenAI
   takes `response_format={"type": "json_schema", "json_schema": {"name": ...,
   "schema": ...}}` — the schema sits under a *named* wrapper. Same intent,
   incompatible spelling, and no amount of shared code hides it.

2. **The stream event model.** Anthropic emits named typed events
   (`message_start`, `content_block_delta`, `message_stop`), so "the object is
   finished" is a fact the wire states. OpenAI emits opaque chunks carrying
   `choices[].delta.content`, and completion is *inferred* from `finish_reason`
   plus a literal `data: [DONE]` sentinel. Consequence: a truncated OpenAI
   stream looks exactly like a slow one until the connection drops. There is no
   missing `message_stop` to notice, so your error path carries more weight.

3. **How rate limits are counted.** Both answer HTTP 429 with `Retry-After`, but
   the error envelopes differ (`{"type": "error", "error": {...}}` vs
   `{"error": {"message", "type", "param", "code"}}`) and so do the exception
   classes — `anthropic.RateLimitError` vs `openai.RateLimitError`. Your service
   layer must see neither; both translate to `TransientProviderError` at the
   adapter boundary, or the retry logic starts branching on vendor.

4. **The clients' own retry defaults.** Both SDKs ship `max_retries=2`. The trap
   you disarmed in stage 5 is waiting, unchanged, in the second SDK — porting
   the adapter without porting `max_retries=0` puts the double-retry bug straight
   back. Watch `make stats`.

5. **Who owns the `/v1` in `base_url`.** `anthropic` appends `/v1/messages` to
   whatever you give it, so it wants `http://host:8081`. `openai` appends only
   `chat/completions`, so it wants `http://host:8081/v1`. Point either client at
   the other's base URL and you get a bare 404 that names nothing.

   This is the most instructive item here, and the reason it is worth its own
   line: it appears in neither vendor's documentation, because each is internally
   consistent and neither has occasion to mention the other. It only becomes
   visible when both clients are aimed at the same server — which is exactly what
   this stage does, and exactly what a real multi-provider service does. The mock
   answers both spellings so one `PROVIDER_BASE_URL` works either way; against
   the real APIs it will not.

---

## Three traps, stated plainly

**1. Exactly one layer may retry.** The `anthropic` SDK ships `max_retries=2`.
Write a retry loop and leave that alone and one request becomes
`(1 + MAX_RETRIES) × 3` upstream calls, while `MAX_RETRIES` appears to do
nothing. Decide who retries, disable the other, make `MAX_RETRIES` control
whichever is live. The bug does not change the *outcome* — the request still
succeeds, just three times more expensively — so `test_retry.py` asserts on the
mock's call count, and its `MAX_RETRIES=0` case is the one that catches you.

**2. Idempotency caches the result, not the call.** At `TEMPERATURE=0.7` the
same prompt twice gives two different valid answers. A caller retrying a request
whose response it never received must get back the answer it already paid for,
byte for byte. Deduplicating the side effect is not enough. And concurrent
duplicates need a per-key lock, or they all miss the empty cache together and
all call upstream — which is exactly when duplicates arrive.

**3. The spend ceiling raises before the call, not after.** Discovering you are
over budget once the money is gone is a report, not a limit. A call's true cost
is unknown until it returns, so reserve the worst case up front and reconcile
afterwards.

---

## Running the tests

| command | time | what it is for |
|---|---|---|
| `make test-fast` | 3–10s | **Run this while working.** No fault injection, no timing tests. |
| `make test` | ~75s | **This decides whether a stage is done.** Full chaos at `FAULT_RATE=0.4`. |
| `make test-graded` | ~75s | The full suite with the mock refusing fault headers. Must also pass. |
| `make test-stages` | ~50s | Only the participant-facing tests. |
| `make gate` | ~13min | Ten consecutive full runs plus the graded run. |
| `make golden` | ~2s | Stage 4: the golden set's score on 20 seeds. `MODE=bad_content` for the other edge. |

The times are for a repository with every stage done. At checkout most tests
fail within milliseconds, so every command finishes in a few seconds, and
`test-fast` grows from ~3s to ~10s as the stages go green. Most of the full
suite's time is spent waiting -- on mock latency, `Retry-After` and rate-limit
windows -- not computing, so a faster machine barely changes it. Measured on a
laptop with other work running; an idle machine may come in a little under.

`make test-fast` skips everything marked `chaos` or `latency`. Those are exactly
the tests a reliability exercise turns on, so a green `test-fast` means "I have
not broken the shape of it", not "this stage is done".

### Reading the output

```
  stage 2 (Async and backpressure): 7 failing
    -> Implement ConcurrencyLimiter and RateLimiter in app/reliability/limits.py
       test_concurrency.py: 7
```

The suite is split in two:

- **`tests/stages/`** — the assignment. You read these to understand what each
  stage wants, and they go green as you implement it.
- **`tests/support/`** — the HTTP surface, `/stats` accuracy, the suite's own
  layout, and the check that nobody silenced the fault injector. These hold the
  repository honest. **You never need to edit one.** A red test here is a
  regression, not homework.

Supporting tests that need code you have not written yet skip themselves and
come back automatically once it exists.

---

## The mock

`mock/server.py` speaks enough of the Anthropic wire protocol that the **real
`anthropic` SDK**, pointed at it with `base_url`, works without complaint — real
SSE event names, real payload shapes, a plausible `usage` block, typed errors.
You are exercising the genuine client library, not a stub of it.

This is the check that cannot be eyeballed, and the suite does it for you:
the genuine `anthropic` client is pointed at the mock throughout, so if the
wire format were wrong, the tests could not pass at all.

It injects faults on purpose, controlled by `FAULT_RATE` (0–1) and `FAULT_MODE`:

| mode | what it does |
|---|---|
| `rate_limit` | HTTP 429 with `Retry-After` |
| `timeout` | holds the connection open past any sane client timeout |
| `truncated_stream` | stops emitting mid-object, no `message_stop` |
| `bad_content` | schema-valid output naming the **wrong** component, confidently |
| `refusal` | `stop_reason: refusal`, a permanent failure. Only when named: never part of `all`, and Anthropic dialect only |

Faults are deterministic in `SEED` + a hash of the request body **+ the attempt
number**. That last term matters: keyed on the body alone every fault would be
permanent, and no correct retry implementation could ever recover — which is not
how upstreams behave. A 429 or a stall is transient by nature, so attempt 1 may
fail and attempt 2 succeed, identically on every run.

`bad_content` is the one to think hardest about. It is schema-valid and wrong.
No amount of output validation catches it; that is what the golden set is for.

### /stats is a teaching instrument

```bash
make stats
```

```json
{
  "calls": 4,
  "calls_by_outcome": {"rate_limit": 3, "ok": 1},
  "retries_observed": 3,
  "peak_concurrency": 2,
  "input_tokens": 118, "output_tokens": 74,
  "estimated_cost_usd": 0.000488
}
```

Watch `calls` while you work. If one logical request costs more upstream calls
than `1 + MAX_RETRIES`, something is retrying that you did not ask to retry.
This is how you find your own double-retry bug.

`peak_concurrency` can exceed `MAX_CONCURRENCY` under `timeout` faults without
anything being wrong: a request the client abandoned at `REQUEST_TIMEOUT_S`
still occupies the mock until its stall ends. With no faults injected, a peak
above the limit is a real bug.

## Do not silence the chaos

The mock accepts `x-mock-fault-mode` / `x-mock-fault-rate` headers that override
fault injection per request. They are a **mock-only extension** — the real API
ignores them, and nothing under `app/` may send one.

Use them in a test only to **isolate one fault type**, never to lower a rate the
test is supposed to survive. Setting `x-mock-fault-mode: none` on a failing
reliability test is not passing it, it is deleting it.

```bash
make test-graded    # MOCK_ALLOW_HEADER_OVERRIDE=false — the mock refuses them
```

Your work must pass **both** `make test` and `make test-graded`. If it passes
one and not the other, you silenced something instead of fixing it.

## Known gaps

Things the repo does not do, on purpose or not yet. They are listed so nobody
mistakes them for something they broke, and each makes a good paragraph in
`REPORT.md`.

- **Streams bypass the spend ceiling.** `triage_stream` takes a permit and a
  rate slot but never reserves or settles against the `SpendLedger`, so
  `/triage/stream` can spend without limit, and no test notices. Closing it
  needs the stream to report the tokens it used, which `Provider.stream` and
  `StreamEvent` do not carry today: a contract change, not a one-line fix.
- **Divergence 5 is not tested.** The mock answers both `/v1/chat/completions`
  and `/chat/completions`, so an OpenAI adapter that gets the `/v1` wrong still
  passes every test. Against the real API it would fail with a bare 404.
- **The breaker counts attempts, not requests.** With `MAX_RETRIES` of 4 or
  more, one request can produce the five consecutive failures that open the
  circuit for every caller. And it lives in one process, so N workers let N×5
  failures through before all of them stop.

## Configuration

Everything comes from the environment via `app/config.py`; nothing else reads
`os.environ`. See `.env.example` for the full list with defaults.

`make dev` and `make mock` load `.env` when it exists, as compose does. A
variable already set in your shell wins over the file, so
`FAULT_RATE=0.4 make mock` overrides whatever `.env` says. The test suite does
not read `.env` at all: each test sets exactly the environment it needs.

The ones you will turn: `FAULT_RATE`, `FAULT_MODE`, `SEED`, `MAX_RETRIES`,
`MAX_CONCURRENCY`, `SPEND_CEILING_USD`, `REQUEST_TIMEOUT_S`, `TEMPERATURE`.

`PROVIDER` names the *dialect*, not a Python class — one adapter per dialect,
and `PROVIDER_BASE_URL` alone chooses the transport, which is how either dialect can
be pointed at the local mock. Pointing the base URL at
`api.anthropic.com` sends real billable traffic and is refused unless you set
`ALLOW_REAL_API=true`. Nothing in this workshop needs that.

## Samples and the golden set

`samples/` holds ten bug reports: **three unambiguous, four borderline, three
noisy**.

| | files | what they are for |
|---|---|---|
| unambiguous | `01`–`03` | must always be right; a threshold spent here means extraction is broken |
| borderline | `04`–`07` | two competent triagers would disagree; where the threshold actually lives |
| noisy | `08`–`10` | chatty, hedged, a bare traceback, four words — signal under noise |

`samples/golden.yaml` lists, for each, the answers a careful reader could
defend — a *set*, not a single answer. Two competent triagers disagree about the
borderline ones, and a golden set that admits only one of them measures
agreement with its author rather than quality. Severity is graded by distance on
the ordered scale: calling a `high` bug `critical` is a much smaller error than
calling it `trivial`.

Sample `10` caps confidence instead of requiring a floor. It is four words with
no repro and no component; the right answer is a guess held loosely, and high
confidence there is the failure.

The threshold is committed to that file rather than to a test, so moving the bar
is a reviewable change instead of a number quietly edited until the suite goes
green.

## Cassettes

`scripts/record_cassette.py` records request/response pairs for offline replay.

```bash
make mock                                          # in one terminal
.venv/bin/python scripts/record_cassette.py        # in another
```

> **The cassettes in `cassettes/synthetic.json` are SYNTHETIC.** No API key was
> available when this repository was built, so they were recorded against the
> local mock, not the real API. The **wire format is real** — the same JSON the
> SDK parses — so they are useful for exercising a client, a parser or a replay
> harness. The **answers are not**: they come from keyword rules in
> `mock/scenarios.py`. Nothing about model behaviour — how it words a component,
> how it calibrates confidence, what it does with an ambiguous report — can be
> learned from them.

Every cassette records `meta.synthetic`, so a consumer never has to guess. With
a real key, `--real` records genuinely (`ALLOW_REAL_API=true` and a real
`ANTHROPIC_API_KEY`; it costs money and refuses to overwrite a real recording
with a synthetic one).

## Layout

```
app/
  main.py                FastAPI routes; maps failures to status codes
  schema.py              TriageResult                              GIVEN
  config.py              settings from env                         GIVEN
  service.py             composition root — the layer order
  providers/
    base.py              Protocol                                  GIVEN
    anthropic_p.py       the only file that imports `anthropic`    stages 1, 3
    openai_p.py          the OpenAI dialect                        stage 7
  reliability/
    retry.py             backoff + jitter; the ONLY retry layer    stage 5
    idempotency.py       result cache with a per-key lock          stage 5
    limits.py            semaphore + rate limit + spend ceiling    stages 2, 5
    breaker.py           circuit breaker                  GIVEN, complete
mock/
  server.py              Anthropic-dialect HTTP mock
  scenarios.py           deterministic content + fault decisions
samples/                 10 reports (3 unambiguous, 4 borderline, 3 noisy)
  golden.yaml            accepted answers + the threshold you commit
cassettes/
  synthetic.json         SYNTHETIC — real wire format, invented answers
scripts/
  record_cassette.py     records cassettes (synthetic by default)
  check_suite_stable.sh  10 consecutive runs + the graded run
tests/
  stages/                THE ASSIGNMENT — you read and fix these
  support/               holds the repo honest — you never edit these
  conftest.py            the in-process mock server, fixtures, paths
  plugin_stages.py       stage markers + the grouped failure summary
```
