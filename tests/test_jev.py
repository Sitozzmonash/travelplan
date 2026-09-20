"""Jev 适配层完全离线测试：不读取真实 Key，也不发 HTTP 请求。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx

from app.config import TravelPlanConfig
from app.decision.jev import JevClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttp:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _config() -> TravelPlanConfig:
    return replace(TravelPlanConfig(), jev_min_confidence=0.7, jev_max_calls_per_run=1)


def test_choice_uses_typed_question_and_returns_valid_answer() -> None:
    http = FakeHttp(
        FakeResponse(
            {
                "model": "jev-1.13.0",
                "answers": {"quality": {"choice": "KEEP", "confidence": 0.91}},
                "usage": {"input_tokens": 12, "output_tokens": 0},
            }
        )
    )
    client = JevClient(_config(), api_key="test-key", http_client=http)

    result = client.choose(
        tag="quality",
        state={"plan": "structured facts only"},
        instructions="keep or replan",
        criteria={"KEEP": "acceptable", "REPLAN": "needs change"},
    )

    assert result.ok
    assert result.choice == "KEEP"
    assert result.confidence == 0.91
    assert http.calls[0]["json"]["questions"]["quality"]["type"] == "choice"
    assert "test-key" not in result.to_audit().values()


def test_low_confidence_never_becomes_usable_choice() -> None:
    http = FakeHttp(FakeResponse({"answers": {"quality": {"choice": "KEEP", "confidence": 0.3}}}))
    client = JevClient(_config(), api_key="test-key", http_client=http)

    result = client.choose(
        tag="quality", state={}, instructions="keep or replan", criteria={"KEEP": "yes", "REPLAN": "no"}
    )

    assert result.status == "LOW_CONFIDENCE"
    assert result.ok is False


def test_timeout_opens_circuit_without_retrying() -> None:
    http = FakeHttp(httpx.TimeoutException("timed out"))
    client = JevClient(_config(), api_key="test-key", http_client=http)

    first = client.choose(
        tag="quality", state={}, instructions="keep or replan", criteria={"KEEP": "yes", "REPLAN": "no"}
    )
    second = client.choose(
        tag="quality_2", state={}, instructions="keep or replan", criteria={"KEEP": "yes", "REPLAN": "no"}
    )

    assert first.status == "TIMEOUT"
    assert second.status == "SKIPPED"
    assert len(http.calls) == 1
