"""The Anthropic adapter: the real SDK, pointed wherever PROVIDER_BASE_URL says.

The only file in app/ that imports anthropic. Everything above it is written
against providers.base.Provider.

Two things here are load-bearing. The SDK's own retries are disabled
(max_retries=0) because reliability/retry.py owns retrying -- see that module for
the other half of the argument. And errors are translated at this boundary, so
the service layer decides what to retry from TransientProviderError vs
PermanentProviderError and never sees an anthropic.* type, which is the same
decision it makes for the stage-7 OpenAI adapter.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import anthropic
import httpx2
import pydantic

from app.config import Settings, get_settings
from app.providers.base import (
    PermanentProviderError,
    TransientProviderError,
)
from app.schema import StreamEvent, TriageResult, triage_result_json_schema

SYSTEM_PROMPT = (
    "You are a bug triage assistant. Read the report and return a single JSON "
    "object matching the provided schema. Judge severity by user impact, name "
    "the one component most likely at fault, and give the repro steps you can "
    "actually see in the text. Set confidence to how sure you are; a vague or "
    "noisy report should score low. Return the JSON object and nothing else."
)


def _output_config() -> dict[str, Any]:
    return {"format": {"type": "json_schema", "schema": triage_result_json_schema()}}


def _sampling_body(settings: Settings) -> dict[str, Any]:
    """Sampling parameters that are not in this SDK version's typed signature.

    `anthropic==1.1.0` dropped `temperature` from the typed `create()`
    params, but the wire API still takes it and the workshop turns it (the gate
    runs at TEMPERATURE=0.7). `extra_body` is the SDK's supported route for
    exactly this: the field lands in the request JSON, which is what the mock
    fingerprints and what a real endpoint would honour. Keeping it here rather
    than dropping it means TEMPERATURE in config still means something.
    """
    return {"temperature": settings.temperature}


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _translate(exc: Exception) -> Exception:
    """Map an SDK exception onto the adapter-neutral error vocabulary.

    Transient = worth another attempt: rate limits, timeouts, connection
    failures, 5xx, overload. Permanent = retrying cannot help: a malformed
    request, an auth failure, a refusal, output that will not validate.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return TransientProviderError(f"rate limited: {exc}", retry_after=_retry_after(exc))
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return TransientProviderError(f"transport failure: {exc}")
    if isinstance(
        exc,
        (
            anthropic.InternalServerError,
            anthropic.ServiceUnavailableError,
            anthropic.OverloadedError,
            anthropic.DeadlineExceededError,
        ),
    ):
        retry_after = _retry_after(exc) if isinstance(exc, anthropic.APIStatusError) else None
        return TransientProviderError(f"upstream error: {exc}", retry_after=retry_after)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return TransientProviderError(f"upstream {exc.status_code}: {exc}",
                                          retry_after=_retry_after(exc))
        return PermanentProviderError(f"upstream {exc.status_code}: {exc}")
    if isinstance(exc, anthropic.AnthropicError):
        return PermanentProviderError(f"provider error: {exc}")
    if isinstance(exc, httpx2.TransportError):
        # A connection that drops mid-stream surfaces raw from the transport;
        # the SDK only wraps failures that happen before the response starts.
        # A severed stream is the textbook transient failure.
        return TransientProviderError(f"connection lost mid-response: {exc}")
    return exc


def _parse(text: str) -> TriageResult:
    """Validate provider text into a TriageResult, or fail permanently.

    A schema violation is not retryable at this layer: the same prompt at the
    same temperature will not reliably produce different structure, and the
    caller needs a clear error rather than four identical failures.
    """
    try:
        return TriageResult.model_validate_json(text)
    except pydantic.ValidationError as exc:
        raise PermanentProviderError(f"provider output did not match schema: {exc}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover - pydantic handles most of these
        raise PermanentProviderError(f"provider output was not JSON: {exc}") from exc


def _text_of(message: anthropic.types.Message) -> str:
    """Concatenate the text blocks of a Message.

    `stop_reason == "max_tokens"` means the object is cut off mid-JSON. That
    is a real failure with a real cause (MAX_TOKENS too low), so it gets its own
    message rather than surfacing as a baffling JSON parse error.
    """
    if message.stop_reason == "max_tokens":
        raise PermanentProviderError(
            "provider hit max_tokens before finishing the object; raise MAX_TOKENS"
        )
    if message.stop_reason == "refusal":
        raise PermanentProviderError("provider refused to answer this input")
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    if not parts:
        raise PermanentProviderError(
            f"provider returned no text content (stop_reason={message.stop_reason})"
        )
    return "".join(parts)


class AnthropicProvider:
    """`Provider` implementation driving the real `anthropic` SDK.

    Holds no reliability logic of its own beyond error translation: one call in,
    one upstream request out. Retry, idempotency, concurrency and spend live in
    `app/reliability/` so they are shared by every future adapter.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        kwargs: dict[str, Any] = {
            "api_key": self.settings.api_key,
            # See the module docstring: our retry layer owns retrying. If you
            # ever raise this above 0, MAX_RETRIES stops meaning what it says
            # and /stats will show you the damage.
            "max_retries": 0,
            "timeout": self.settings.request_timeout_s,
        }
        if self.settings.provider_base_url:
            kwargs["base_url"] = self.settings.provider_base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self.client.close()

    # --- unary ------------------------------------------------------------

    async def _create(self, text: str, idem_key: str | None) -> anthropic.types.Message:
        """One upstream call, errors translated. The single place we call the SDK."""
        extra_headers = {"idempotency-key": idem_key} if idem_key else None
        try:
            return await self.client.messages.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_config=_output_config(),
                extra_headers=extra_headers,
                extra_body=_sampling_body(self.settings),
            )
        except Exception as exc:
            raise _translate(exc) from exc

    async def extract(self, text: str, *, idem_key: str | None = None) -> TriageResult:
        """One upstream call. No retrying, no caching -- those live above."""
        result, _, _ = await self.extract_with_usage(text, idem_key=idem_key)
        return result

    async def extract_with_usage(
        self, text: str, *, idem_key: str | None = None
    ) -> tuple[TriageResult, int, int]:
        """`extract` plus the token counts, so the spend ledger can charge it."""
        message = await self._create(text, idem_key)
        result = _parse(_text_of(message))
        return result, message.usage.input_tokens, message.usage.output_tokens

    # --- streaming --------------------------------------------------------

    async def stream(self, text: str) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents as the object is produced.

        A stream that dies mid-object yields an `error` event and never a
        `done`: a half-parsed TriageResult is worse than no result, because
        it looks like an answer.
        """
        yield StreamEvent(type="start")
        buffer: list[str] = []
        try:
            async with self.client.messages.stream(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
                output_config=_output_config(),
                extra_body=_sampling_body(self.settings),
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        buffer.append(event.text)
                        yield StreamEvent(type="delta", text=event.text)
                final = await stream.get_final_message()
        except Exception as exc:
            translated = _translate(exc)
            yield StreamEvent(
                type="error",
                error=str(translated),
                transient=isinstance(translated, TransientProviderError),
            )
            return

        try:
            result = _parse(_text_of(final))
        except Exception as exc:
            # The stream completed; what came back cannot be used and would not
            # change on a retry -- refusal, max_tokens, invalid output.
            yield StreamEvent(type="error", error=str(exc), transient=False)
            return
        yield StreamEvent(type="done", result=result, input_tokens=final.usage.input_tokens, output_tokens=final.usage.output_tokens)
