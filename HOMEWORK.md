# Homework

Three small tasks on top of the finished service from the session. Each one
starts as red tests and is done when they are green. Plan for an hour or two
per task.

This branch already has all seven stages done, the same code you saw in the
demo. If you would rather build those yourself first, start from `main`
instead; the README walks you through it.

## Getting started

```bash
git clone https://github.com/seandevis/llm-triage-service.git
cd llm-triage-service
git checkout homework
cp .env.example .env
make venv
make homework        # only the homework tests: 13 red to start with
make test-fast       # everything else, which must stay green
```

No API key needed: everything runs against the local mock, as in the session.

## Task 1 · A component vocabulary (do this one first)

**Why.** In the session, a real Claude was asked about a sign-in outage and
answered `"component": "authentication"`. Our golden set only accepts `auth`,
so a right answer scored as wrong. The schema says `component` is a string, so
the model chose its own word. A schema checks the shape, not the words.

**What to do.**
- Declare the allowed components in `app/schema.py` as `COMPONENTS`:
  `auth`, `payments`, `database`, `api`, `ui`, `search`, `notifications`,
  `storage`, `unknown`.
- Make `TriageResult.component` accept only those.
- Make sure the JSON schema the model receives lists them, so the model can
  pick from the list instead of inventing a word.

**Done when** `tests/homework/test_hw1_components.py` is green, and
`make test-fast` still is.

**Hint.** Look at how `severity` is declared.

## Task 2 · A second vendor as a fallback

**Why.** In episode six the circuit breaker stopped us from hammering a dead
provider, but users still got a 503. In episode seven OpenAI dropped in as one
file. Put the two together: when the primary provider is down, answer from the
second one.

**What to do.**
- `TriageService` accepts an optional `fallback` provider and exposes it as
  `.fallback`.
- A new setting, `FALLBACK_PROVIDER`, chooses it (`openai`, or empty for none).
- When the primary is down, the request is answered by the fallback: after the
  primary's retries run out, or straight away if its breaker is already open.
- Only for outages. A refusal or an answer that will not validate is the
  request's problem, not the provider's, and must not switch vendors.
- Everything above still applies to a fallback answer: the cache, the spend
  ceiling, the limits.

**Done when** `tests/homework/test_hw2_fallback.py` is green, and
`make test-fast` still is.

**Hint.** Start in `app/service.py`. The fallback deserves its own breaker.

## Task 3 · Streams inside the spend ceiling

**Why.** `triage_stream` never touches the spend ledger, so a client that only
streams has no spending limit. README's "Known gaps" lists it.

**What to do.**
- Reserve the worst-case cost before a stream starts; refuse with
  `SpendCeilingExceeded` if it does not fit, before anything reaches the
  provider.
- Settle the real cost when the stream ends; release the reservation if it
  fails.
- The real cost is only known at the end of the stream, so the adapter has to
  report the token usage somehow. How is up to you.

**Done when** `tests/homework/test_hw3_stream_budget.py` is green, and
`make test-fast` still is.

**Hint.** The unary path in `app/service.py` does exactly this. Look at what
the Anthropic SDK's stream gives you at the end.

## Bonus · Your golden set on a real model

With your own API key, run the golden set against a real model. Two things to
know: the service reaches the real API with `ALLOW_REAL_API=true`,
`PROVIDER_BASE_URL=https://api.anthropic.com` and your key in
`ANTHROPIC_API_KEY`; and `scripts/score_golden.py` always starts the local
mock, so you will need to change it to call the real API instead. Write in
`REPORT.md` what the real answers did differently from the mock. This one has
no tests; it costs a few cents.

## Sending it in

1. Fork this repository on GitHub.
2. Do the tasks on a branch in your fork, starting from `homework`.
3. Open a pull request into `homework` on this repository. The checks run on
   their own; fill in the checklist in the PR description.

Pull requests are reviewed, not merged: everyone's solution stays in their own
PR. There is no deadline.

## Rules

The same as for the stages: never weaken a test to make it pass, and nothing in
`app/` may send `x-mock-fault-*` headers. You may change `app/schema.py` and
`app/config.py` for these tasks; that is part of the work.
