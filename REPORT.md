# REPORT — triage-service under a bad provider

Every number below comes from the mock's `/stats` or the service's own ledger,
produced by `make demo S=<scenario>` on this commit. Mock: SEED=1337,
TEMPERATURE=0.7.

## What one request costs under chaos

`make demo S=chaos`: 30 distinct requests, FAULT_RATE=0.4, MAX_RETRIES=3.

| | |
|---|---|
| answered | 30 of 30 |
| upstream calls | 38 (1.27 per request) |
| outcomes | ok 25, bad_content 5, timeout 4, server_error 3, rate_limit 1 |
| cost | $0.006554 by the mock, $0.006554 by our ledger |

Retry turned 40% faults into 100% answers for 27% more calls. The ledger and
the mock agree to the cent, so no attempt went uncharged.

**Five of the thirty answers were confidently wrong** (`bad_content`), and the
service returned 200 for every one. Nothing in the request path can see that;
only the golden set can, which is why it runs on a schedule, not just in CI.

## peak_concurrency

The mock read **5** with MAX_CONCURRENCY=4. This is not work escaping the
semaphore: four `timeout` faults stalled, the client gave up at
REQUEST_TIMEOUT_S and released its permit, and the mock was still holding the
abandoned connection when the next call arrived. With no faults
(`make demo S=limits`) the mock reads exactly 4, and 10 requests at 0.8s each
take 2.5s: three batches, not ten in series.

## Did we double-retry?

No. `make demo S=budget`, against an upstream that always answers 429:

| MAX_RETRIES | upstream calls |
|---|---|
| 0 | 1 |
| 1 | 2 |
| 2 | 3 |
| 3 | 4 |

Exactly 1 + MAX_RETRIES. The SDK's own retries are off (`max_retries=0`); left
on, every row would be three times larger.

## Idempotency and the ceiling

- `make demo S=replay`: 8 identical concurrent requests with one key made **1**
  upstream call; 7 were served from the cache, all byte-identical.
- `make demo S=ceiling`: 40 requests against a $0.01 ceiling. 18 answered, **22
  refused before sending**, $0.003953 spent. Each call reserves the worst case
  ($0.006120) up front, so refusal starts while real spend is still well under
  the ceiling: the price of never overshooting.
- `make demo S=rate`: a burst of 10 against a provider limit of 4 per second
  took 2.0s and drew **0** throttled responses.

## What the breaker saved

`make demo S=outage`: the provider is down, six requests in a row.

| request | answer | upstream calls so far |
|---|---|---|
| 1 | 503, retries exhausted | 4 |
| 2 | 503, circuit open (tripped by the 5th failure) | 5 |
| 3-6 | 503, circuit open | 5 |

5 calls instead of 24. Two things to watch: the breaker counts attempts, not
requests, so one request with a large retry budget can open it for everyone;
and it lives in one process, so N workers let N×5 failures through.

## Known gap

`/triage/stream` never touches the ledger: streams are not charged against the
ceiling. Closing it needs the stream to report its token usage (a contract
change). Until then, a client that only streams has no spend limit.

## What I would watch in production

- upstream calls per request, alerting above 1 + MAX_RETRIES
- `calls_by_outcome.timeout`, as an early latency signal
- a scheduled golden-set score, because `bad_content` is invisible to
  everything else
