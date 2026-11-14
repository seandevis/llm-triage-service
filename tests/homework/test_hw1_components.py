"""Homework 1 -- a component vocabulary.

Red at checkout: `component` is a free string, so the model can name it
anything. Asked about a sign-in outage, a real Claude answered
"authentication", not "auth"; our golden set only accepts "auth", so a right
answer scored as wrong. A schema checks the shape, not the words.

The fix: give the model the list of allowed components, as an enum in the
schema it receives, and reject anything outside it. HOMEWORK.md has the list.
"""

from __future__ import annotations

import pydantic
import pytest

from app.providers.base import PermanentProviderError
from app.schema import TriageResult, triage_result_json_schema
from tests.conftest import SAMPLES_DIR
from tests.support.goldenio import load_golden

EXPECTED = {
    "auth", "payments", "database", "api", "ui",
    "search", "notifications", "storage", "unknown",
}


def _components() -> tuple[str, ...]:
    try:
        from app.schema import COMPONENTS
    except ImportError:
        pytest.fail(
            "homework 1: declare COMPONENTS in app/schema.py -- the tuple of "
            "component names the model may use (HOMEWORK.md lists them)"
        )
    return tuple(COMPONENTS)


def test_the_allowed_components_are_declared():
    assert set(_components()) == EXPECTED


def test_a_component_outside_the_list_is_rejected():
    with pytest.raises(pydantic.ValidationError):
        TriageResult(severity="critical", component="authentication", confidence=0.9)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_allowed_component_is_accepted(name):
    assert TriageResult(severity="low", component=name, confidence=0.5).component == name


def test_the_schema_sent_to_the_model_lists_them():
    """The model only knows what the schema tells it. Without the list there,
    rejecting its answer afterwards just turns a wrong word into an error."""
    component = triage_result_json_schema()["properties"]["component"]
    assert set(component.get("enum", [])) == EXPECTED, (
        f"the component in the wire schema is {component}; it should carry an "
        f"enum of the allowed names"
    )


def test_the_golden_set_only_uses_known_components():
    golden = load_golden(SAMPLES_DIR / "golden.yaml")
    allowed = set(_components())
    for case in golden["cases"]:
        unknown = set(case["accept"]["component"]) - allowed
        assert not unknown, f"golden case {case['id']} accepts {unknown}, not in COMPONENTS"


async def test_a_reply_outside_the_list_is_a_permanent_error(make_service, mock_server, env):
    """What happens when a model ignores the enum anyway: a clear, permanent
    error after one call. Retrying would get the same word back."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="3")
    service = make_service()
    mock_server.reset_stats()
    original = service.provider.client.messages.create

    async def free_wording(**kwargs):
        message = await original(**kwargs)
        message.content[0].text = (
            '{"severity": "critical", "component": "authentication", '
            '"duplicate_of": null, "steps": [], "confidence": 0.95}'
        )
        return message

    service.provider.client.messages.create = free_wording

    with pytest.raises(PermanentProviderError):
        await service.triage("Login returns 500 for all users since the 4.2.1 deploy")
    assert mock_server.stats()["calls"] == 1, "a reply outside the vocabulary must not be retried"
