"""Jev 的窄适配层：只做可枚举选项之间的软决策。

本模块不把 Jev 当作聊天模型，也不让它生成价格、路线或行程事实。请求遵循
TypeSafe System One 的 ``model/state/questions`` 结构；密钥仅从服务端环境读取，
并且所有失败都会返回可审计结果给 Python fallback，而非抛出异常中断规划。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config import TravelPlanConfig, current_config


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(slots=True)
class JevResult:
    tag: str
    status: str
    choice: str | None = None
    confidence: float | None = None
    duration_ms: int | None = None
    model: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    quota: dict[str, Any] | None = None
    attempted: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_audit(self) -> dict[str, Any]:
        """不含 request state，也不含任何认证信息。"""
        return asdict(self)


class JevClient:
    """每个 run 一份的调用预算与 circuit breaker。"""

    def __init__(
        self,
        config: TravelPlanConfig | None = None,
        *,
        api_key: str | None = None,
        http_client: _HttpClient | None = None,
    ) -> None:
        self.config = config or current_config()
        self.api_key = api_key if api_key is not None else (
            os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY") or ""
        )
        self._client = http_client or httpx.Client()
        self.calls: list[JevResult] = []
        self._circuit_open = False

    def choose(
        self,
        *,
        tag: str,
        state: dict[str, Any],
        instructions: str,
        criteria: dict[str, str],
    ) -> JevResult:
        """调用单个 Choice 问题，并验证返回的选择与置信度。

        业务代码只能在 ``result.ok`` 时使用 choice；任何其它状态都必须走自己的
        确定性策略。这里不自动重试，避免 timeout 后不确定是否已经产生收费调用。
        """
        if not self.config.jev_enabled:
            return self._record(JevResult(tag=tag, status="SKIPPED", error="JEV_ENABLED=false"))
        if not self.api_key:
            return self._record(JevResult(tag=tag, status="SKIPPED", error="未配置 JEV_API_KEY"))
        if self._circuit_open:
            return self._record(JevResult(tag=tag, status="SKIPPED", error="Jev circuit breaker 已打开"))
        attempted = sum(1 for call in self.calls if call.attempted)
        if attempted >= self.config.jev_max_calls_per_run:
            return self._record(JevResult(tag=tag, status="BUDGET_EXCEEDED", error="达到本次 run 的 Jev 调用上限"))
        if len(criteria) < 2:
            return self._record(JevResult(tag=tag, status="INVALID_REQUEST", error="Jev Choice 至少需要两个选项"))

        started = time.perf_counter()
        try:
            response = self._client.post(
                self.config.jev_base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.config.jev_model,
                    "state": state,
                    "questions": {
                        tag: {
                            "type": "choice",
                            "instructions": instructions,
                            "criteria": criteria,
                        }
                    },
                },
                timeout=max(0.1, self.config.jev_timeout_ms / 1000),
            )
        except httpx.TimeoutException as exc:
            self._circuit_open = True
            return self._record(self._failure(tag, "TIMEOUT", started, exc))
        except httpx.HTTPError as exc:
            self._circuit_open = True
            return self._record(self._failure(tag, "UNAVAILABLE", started, exc))

        duration_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            if response.status_code in {401, 403, 429} or response.status_code >= 500:
                self._circuit_open = True
            return self._record(
                JevResult(
                    tag=tag,
                    status=f"HTTP_{response.status_code}",
                    duration_ms=duration_ms,
                    error="Jev 返回非成功 HTTP 状态（响应体已省略，避免日志泄露）",
                    attempted=True,
                )
            )
        try:
            payload = response.json()
        except ValueError as exc:
            self._circuit_open = True
            return self._record(self._failure(tag, "INVALID_RESPONSE", started, exc))

        answer = (payload.get("answers") or {}).get(tag) if isinstance(payload, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        confidence = answer.get("confidence") if isinstance(answer, dict) else None
        if choice not in criteria or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            self._circuit_open = True
            return self._record(
                JevResult(
                    tag=tag,
                    status="INVALID_RESPONSE",
                    duration_ms=duration_ms,
                    error="Jev 返回缺少有效 choice/confidence 的响应",
                    attempted=True,
                )
            )
        status = "OK" if confidence >= self.config.jev_min_confidence else "LOW_CONFIDENCE"
        return self._record(
            JevResult(
                tag=tag,
                status=status,
                choice=choice,
                confidence=float(confidence),
                duration_ms=duration_ms,
                model=str(payload.get("model") or self.config.jev_model),
                usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
                quota=payload.get("quota") if isinstance(payload.get("quota"), dict) else None,
                attempted=True,
            )
        )

    def _failure(self, tag: str, status: str, started: float, exc: Exception) -> JevResult:
        return JevResult(
            tag=tag,
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:400],
            attempted=True,
        )

    def _record(self, result: JevResult) -> JevResult:
        self.calls.append(result)
        return result
