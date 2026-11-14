"""The OpenAI adapter: the same Protocol, a different dialect.

Stage 7. The only file besides anthropic_p.py that imports a vendor SDK, and
the only file that should need to exist for the port -- everything above it is
written against providers.base.Provider, so if the reliability layers had to
change, the abstraction was leaking.

Five places the two SDKs genuinely diverge, which is the lesson the port
exists to teach:

1. Structured output. Anthropic takes output_config={"format": {...}}; OpenAI
   takes response_format={"type": "json_schema", "json_schema": {...}} with the
   schema under a named wrapper. Same intent, incompatible spelling.

2. The stream event model. Anthropic emits named typed events -- message_start,
   content_block_delta, message_stop -- so "the object is finished" is a fact
   the wire tells you. OpenAI emits opaque chunks carrying choices[].delta, and
   completion is inferred from finish_reason plus a literal `data: [DONE]`
   sentinel. A truncated OpenAI stream therefore looks exactly like a slow one
   until the connection drops, which is why the error path matters more here.

3. Rate limits. Both answer 429 with Retry-After, but the error envelopes
   differ and so do the exception classes. The service layer must not see
   either: this file translates both into TransientProviderError.

4. Retry defaults. Both clients ship max_retries=2. The trap is identical in
   both SDKs, and porting the adapter without porting the max_retries=0 puts
   the double-retry bug back for the second dialect.

5. Who owns the "/v1" in base_url. anthropic appends /v1/messages, so it wants
   http://host:8081; openai appends only chat/completions, so it wants
   http://host:8081/v1. Aim either at the other's base URL and you get a bare
   404. It is in neither vendor's documentation -- each is internally consistent
   and has no occasion to mention the other -- so it surfaces only when both
   clients point at one server. The mock serves both spellings; the real APIs
   do not.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx2
import openai
import pydantic

from app.config import Settings, get_settings
from app.providers.base import PermanentProviderError, TransientProviderError
from app.schema import StreamEvent, TriageResult, triage_result_json_schema

SYSTEM_PROMPT = (
    "You are a bug triage assistant. Read the report and return a single JSON "
    "object matching the provided schema. Judge severity by user impact, name "
    "the one component most likely at fault, and give the repro steps you can "
    "actually see in the text. Set confidence to how sure you are; a vague or "
    "noisy report should score low. Return the JSON object and nothing else."
)


def _response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "triage_result",
            "strict": True,
            "schema": triage_result_json_schema(),
        },
    }


def _retry_after(exc: openai.APIStatusError) -> float | None:
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
    """Map an openai exception onto the adapter-neutral vocabulary.

    Deliberately the same shape as anthropic_p._translate. The service layer
    makes one decision -- retry or do not -- and must make it identically
    whichever dialect is configured.
    """
    if isinstance(exc, openai.RateLimitError):
        return TransientProviderError(f"rate limited: {exc}", retry_after=_retry_after(exc))
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return TransientProviderError(f"transport failure: {exc}")
    if isinstance(exc, openai.InternalServerError):
        return TransientProviderError(f"upstream error: {exc}", retry_after=_retry_after(exc))
    if isinstance(exc, openai.LengthFinishReasonError):
        return PermanentProviderError(
            "provider hit its token cap before finishing the object; raise MAX_TOKENS"
        )
    if isinstance(exc, openai.ContentFilterFinishReasonError):
        return PermanentProviderError("provider refused to answer this input")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return TransientProviderError(f"upstream {exc.status_code}: {exc}",
                                          retry_after=_retry_after(exc))
        return PermanentProviderError(f"upstream {exc.status_code}: {exc}")
    if isinstance(exc, openai.OpenAIError):
        return PermanentProviderError(f"provider error: {exc}")
    if isinstance(exc, httpx2.TransportError):
        # A connection that drops mid-stream surfaces raw from the transport;
        # the SDK only wraps failures that happen before the response starts.
        # A severed stream is the textbook transient failure.
        return TransientProviderError(f"connection lost mid-response: {exc}")
    return exc


def _parse(text: str) -> TriageResult:
    try:
        return TriageResult.model_validate_json(text)
    except pydantic.ValidationError as exc:
        raise PermanentProviderError(f"provider output did not match schema: {exc}") from exc
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise PermanentProviderError(f"provider output was not JSON: {exc}") from exc


def _text_of(completion: Any) -> str:
    """Pull the assistant text out of a ChatCompletion, or fail permanently.

    finish_reason carries what Anthropic puts in stop_reason, with different
    spellings: "length" is a truncated object, "content_filter" is a refusal.
    Both are permanent -- the same call fails the same way.
    """
    if not completion.choices:
        raise PermanentProviderError("provider returned no choices")
    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise PermanentProviderError(
            "provider hit its token cap before finishing the object; raise MAX_TOKENS"
        )
    if choice.finish_reason == "content_filter":
        raise PermanentProviderError("provider refused to answer this input")
    if getattr(choice.message, "refusal", None):
        raise PermanentProviderError(f"provider refused: {choice.message.refusal}")
    content = choice.message.content
    if not content:
        raise PermanentProviderError(
            f"provider returned no content (finish_reason={choice.finish_reason})"
        )
    return content


class OpenAIProvider:
    """Provider implementation driving the real openai SDK."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        kwargs: dict[str, Any] = {
            "api_key": self.settings.openai_api_key,
            # Divergence 4: this client ships max_retries=2 too. reliability/
            # retry.py owns retrying for both dialects.
            "max_retries": 0,
            "timeout": self.settings.request_timeout_s,
        }
        if self.settings.provider_base_url:
            kwargs["base_url"] = self.settings.provider_base_url
        self.client = openai.AsyncOpenAI(**kwargs)

    async def aclose(self) -> None:
        await self.client.close()

    async def _create(self, text: str, idem_key: str | None) -> Any:
        extra_headers = {"idempotency-key": idem_key} if idem_key else None
        try:
            return await self.client.chat.completions.create(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                response_format=_response_format(),
                extra_headers=extra_headers,
            )
        except Exception as exc:
            raise _translate(exc) from exc

    async def extract(self, text: str, *, idem_key: str | None = None) -> TriageResult:
        result, _, _ = await self.extract_with_usage(text, idem_key=idem_key)
        return result

    async def extract_with_usage(
        self, text: str, *, idem_key: str | None = None
    ) -> tuple[TriageResult, int, int]:
        completion = await self._create(text, idem_key)
        result = _parse(_text_of(completion))
        usage = completion.usage
        return result, usage.prompt_tokens, usage.completion_tokens

    async def stream(self, text: str) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents as the object is produced.

        Divergence 2 in practice: there is no typed "the object is done" event,
        so completion comes from the SDK's assembled final completion. A stream
        that dies mid-object yields an error and never a done.
        """
        yield StreamEvent(type="start")
        try:
            async with self.client.chat.completions.stream(
                model=self.settings.model,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                response_format=_response_format(),
                # Divergence 4b: without this the stream carries no usage at
                # all, and a ported spend ledger silently reads zero.
                stream_options={"include_usage": True},
            ) as stream:
                async for event in stream:
                    if event.type == "content.delta":
                        yield StreamEvent(type="delta", text=event.delta)
                final = await stream.get_final_completion()
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
            yield StreamEvent(type="error", error=str(exc), transient=False)
            return
        yield StreamEvent(type="done", result=result)
