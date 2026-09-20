"""LLM 调用层（PRD §16 / §25）。

为什么单独一层，而不是在每个节点里 new 一个 ChatOpenAI
--------------------------------------------------
整条固定流程里有 5 处要调模型（意图解析、检索词扩写、地点抽取、Critic、成文）。
集中在一处才能统一三件必须一致的事：

1. **JSON 容错**：模型经常把 JSON 包在 ```json 里，或者前后带一句解释。这里统一
   走 `models.parse_envelope` 的容错解析，各节点拿到的是 dict/list，不需要各自 try。
2. **失败降级**：铁律是 "Evidence First, LLM Second"。模型不可用不是一个异常，
   而是一个状态 —— 所有方法都不抛异常，返回空结果 + 状态，由调用方决定怎么降级。
   一次 run 不允许因为模型超时就整份失败。
3. **调用留痕**：每次调用（含失败）都进 `self.calls`，最后写进 audit_report.json。
   审计要能回答"这次模型到底被调了几次、都让它干什么了、返回了什么"。

Secret 只从环境变量读，且**不写入日志/输出文件**：`calls` 里只记 model 名与耗时。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models import coerce_str, parse_envelope

try:  # 事件名只从 SuperHarness 取，不在本地抄一份字符串（抄了迟早会跟上游漂移）
    from superharness.observability import EventType
except Exception:  # noqa: BLE001 —— 没装 superharness 时仍允许 import 本模块并降级

    class EventType:  # type: ignore[no-redef]
        """superharness 缺席时的占位。名字与上游一致，事件发不出去而已。"""

        LLM_STARTED = "llm.started"
        LLM_FINISHED = "llm.finished"

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 单次模型调用的默认超时（秒）。比 Provider 的 60s 长：模型侧排队比数据源更常见。
#: 120s 实测偏紧：kimi-k3 抽地点有一次 115.8s 才返回，再慢一点就被我们判成超时。
#: 但也不能无限放宽 —— 模型侧真卡住时（实测超过 5 分钟），放宽等于让整条 run 干等。
DEFAULT_TIMEOUT_SECONDS = 180.0

#: 覆盖单次模型调用超时的环境变量名（秒）。做成可调是因为"多慢算慢"取决于模型：
#: 推理模型的思考 token 不算在首 token 延迟里，同一条 prompt 在不同模型上能差好几倍。
TIMEOUT_ENV_VAR = "TRAVELPLAN_LLM_TIMEOUT_SECONDS"

#: 状态语义与 providers.PROVIDER_STATUSES 对齐，前端不需要学第二套词表。
STATUS_OK = "OK"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_INVALID_RESPONSE = "INVALID_RESPONSE"
STATUS_TIMEOUT = "TIMEOUT"


@dataclass(slots=True)
class LLMResult:
    """一次模型调用的结果 + 留痕。`value` 只在 JSON 模式下有意义。"""

    status: str = STATUS_UNAVAILABLE
    text: str = ""
    value: Any = None
    error: str | None = None
    model: str = ""
    tag: str = ""
    duration_ms: int | None = None
    #: 模型侧回报的 token 用量。取不到就是 None —— 宁可让上游不显示，也不填 0 冒充。
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def degraded_reason(self) -> str:
        """给用户/审计看的一句话，说明这次为什么没有模型输出。"""
        if self.ok:
            return ""
        return f"模型调用 {self.tag or '未命名'} 未成功（{self.status}{'：' + self.error if self.error else ''}）"

    def to_audit(self) -> dict[str, Any]:
        """audit_report.json 的 llm_calls 条目。刻意不含 prompt 全文与任何 Secret。"""
        return {
            "tag": self.tag,
            "model": self.model,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "chars": len(self.text or ""),
        }


def _usage_of(response: Any) -> dict[str, Any] | None:
    """从模型的返回里取 token 用量，取不到返回 None。

    不同 provider 把用量放在不同字段（`usage_metadata` / `response_metadata.token_usage`），
    而"取不到"和"用量为 0"是两回事：前者必须在日志里留白，后者才是真的 0。
    """
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        meta = getattr(response, "response_metadata", None)
        usage = meta.get("token_usage") if isinstance(meta, dict) else None
    if not isinstance(usage, dict):
        return None

    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    details = usage.get("input_token_details")
    cached = details.get("cache_read") if isinstance(details, dict) else None

    payload: dict[str, Any] = {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
    }
    if cached is not None:
        payload["cached_tokens"] = int(cached or 0)
    return payload


def _timeout_from_env(default: float | None = None) -> float:
    """读 `TRAVELPLAN_LLM_TIMEOUT_SECONDS`；没设或不成数就退回默认值。

    刻意不因为环境变量写错就抛异常：配错一个超时不该让整条 run 起不来，
    退回默认值最多是这次慢一点。
    """
    base = DEFAULT_TIMEOUT_SECONDS if default is None else default
    raw = coerce_str(os.environ.get(TIMEOUT_ENV_VAR)).strip()
    if not raw:
        return base
    try:
        value = float(raw)
    except ValueError:
        return base
    return value if value > 0 else base


def degraded_note(result: LLMResult, fallback: str) -> str:
    """把「模型这次为什么没用上」拼成一句话，保证不会出现没有前因的「；...」。

    调用成功（status OK）但产出不可用（空文本 / 空列表）时 ``degraded_reason`` 是空串，
    直接拼接会得到「；检索词退化为规则组合」这种读起来像有前因、实际什么都没说的句子。
    审计里的降级说明必须能自证原因，否则读者无从判断该不该重跑。
    """
    reason = coerce_str(result.degraded_reason).strip().strip("；; ")
    return f"{reason}；{fallback}" if reason else fallback


def _as_text(content: Any) -> str:
    """把 AIMessage.content 归一成字符串。

    langchain 的 content 可能是 str，也可能是 [{"type":"text","text":...}] 这种
    内容块列表（多模态/带 reasoning 的模型都会这么返回），这里统一摊平。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


class LLM:
    """模型句柄。`available=False` 时所有调用立刻返回降级结果（不联网、不报错）。"""

    def __init__(
        self,
        *,
        model: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        emit: Any | None = None,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._emit_hook = emit
        self._model_name = ""
        self.calls: list[LLMResult] = []
        if model is not None:
            self._model_name = str(
                getattr(model, "model_name", None) or getattr(model, "model", "") or ""
            )

    def _emit(self, event_type: str, data: dict[str, Any], *, status: str | None = None) -> None:
        """把这次模型调用报给 SuperHarness 的原生 Observability（START.md §9：不要重造）。

        上报失败绝不能影响规划：模型调用的结果照常返回，观测缺席只是少一行日志。
        """
        hook = self._emit_hook
        if hook is None:
            return
        try:
            hook(event_type, "llm", data=data, status=status)
        except Exception:  # noqa: BLE001 —— 日志消费者出错不能把主流程搞死
            pass

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, model: Any | None = None, emit: Any | None = None) -> "LLM":
        """从环境变量构造。模型不可用（缺 key/base_url）时返回一个 available=False 的句柄。

        刻意不抛异常：`create_model()` 在缺配置时抛 ValueError，而"缺模型"应该是
        一次可降级的 run，不是一个起不来的进程。
        """
        timeout = _timeout_from_env()
        if model is not None:
            return cls(model=model, timeout=timeout, emit=emit)
        try:
            from superharness import create_model
        except Exception:  # noqa: BLE001 —— superharness 没装时也允许降级
            return cls()
        try:
            return cls(model=create_model(), timeout=timeout, emit=emit)
        except Exception:  # noqa: BLE001
            return cls(emit=emit)

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._model_name

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    def _call_model(self, messages: list[Any]) -> tuple[str, dict[str, Any] | None]:
        """真正发请求，返回（文本，token 用量）。

        刻意不在这里写 `self.calls`：超时后被我们放弃的那次调用如果还往账本里写一条，
        审计就会多出一条「成功」记录，而实际结果是没等到。
        """
        response = self._model.invoke(messages)
        return _as_text(getattr(response, "content", response)), _usage_of(response)

    def invoke(self, system: str, user: str, *, tag: str = "") -> LLMResult:
        """发一次对话；永远不抛异常。失败时 status != OK 且 error 有值。

        这里只做观测上报，真正的调用在 `_invoke` —— 上报要有统一的收口点，
        否则 4 个 return 分支里漏一个就会出现「有 started 没有 finished」的残迹。
        """
        self._emit(
            EventType.LLM_STARTED,
            {"model": self._model_name, "tag": tag},
        )
        result = self._invoke(system, user, tag=tag)
        self._emit(
            EventType.LLM_FINISHED,
            {
                "tag": tag,
                "model": result.model,
                "duration_ms": result.duration_ms,
                "decision": f"{result.status}" + (f" · {result.error}" if result.error else ""),
                "output": result.text[:4000] if result.text else "",
                **(result.usage or {}),
            },
            status=result.status,
        )
        return result

    def _invoke(self, system: str, user: str, *, tag: str = "") -> LLMResult:
        """发一次对话；永远不抛异常。失败时 status != OK 且 error 有值。

        超时必须由我们自己兜：模型句柄是外部注入的，langchain 的 timeout 只在构造期
        生效，而 openai 客户端默认要等 10 分钟才超时 —— 一旦模型侧卡住，整条 run 就
        吊死在这里（实测卡了 5 分钟以上，前端 120s 早就断了）。所以用线程 + 墙钟兜底，
        超时就当作一次失败调用降级，让流程按 "Evidence First, LLM Second" 继续跑完。
        """
        if self._model is None:
            result = LLMResult(
                status=STATUS_UNAVAILABLE,
                model=self._model_name,
                tag=tag,
                error="未配置可用模型（MODEL_NAME / MODEL_BASE_URL / MODEL_API_KEY）",
            )
            self.calls.append(result)
            return result

        started = time.monotonic()
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            messages: list[Any] = [SystemMessage(content=system), HumanMessage(content=user)]
        except Exception as exc:  # noqa: BLE001 —— 缺依赖也只是一个降级状态
            result = LLMResult(
                status=STATUS_UNAVAILABLE,
                model=self._model_name,
                tag=tag,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.calls.append(result)
            return result

        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["text"], box["usage"] = self._call_model(messages)
            except BaseException as exc:  # noqa: BLE001 —— 线程里任何异常都要带回主线程
                box["error"] = exc

        # daemon 线程：即使模型一直不返回，也不该拖住进程退出。
        thread = threading.Thread(target=worker, daemon=True, name=f"llm-{tag or 'call'}")
        thread.start()
        thread.join(self._timeout)
        duration_ms = int((time.monotonic() - started) * 1000)

        if thread.is_alive():
            result = LLMResult(
                status=STATUS_TIMEOUT,
                model=self._model_name,
                tag=tag,
                error=f"模型调用超过 {self._timeout:g}s 未返回，已放弃等待并按失败降级",
                duration_ms=duration_ms,
            )
            self.calls.append(result)
            return result

        error = box.get("error")
        if error is not None:
            message = f"{type(error).__name__}: {error}"
            lowered = message.lower()
            # httpx/openai 的超时消息是 "Request timed out."，只有 "timeout" 一个词会漏判，
            # 把超时错报成 UNAVAILABLE —— 审计里这两者必须分得清。
            is_timeout = "timeout" in lowered or "timed out" in lowered
            result = LLMResult(
                status=STATUS_TIMEOUT if is_timeout else STATUS_UNAVAILABLE,
                model=self._model_name,
                tag=tag,
                error=message[:400],
                duration_ms=duration_ms,
            )
            self.calls.append(result)
            return result

        result = LLMResult(
            status=STATUS_OK,
            text=coerce_str(box.get("text")),
            model=self._model_name,
            tag=tag,
            duration_ms=duration_ms,
            usage=box.get("usage"),
        )
        self.calls.append(result)
        return result

    def invoke_json(self, system: str, user: str, *, tag: str = "") -> LLMResult:
        """发一次"只输出 JSON"的对话，并把返回解析进 `result.value`。

        解析失败时 status=INVALID_RESPONSE，但 `text` 仍然保留 —— 审计需要看到
        "模型到底回了什么"，而不是只知道它没解析成功。
        """
        result = self.invoke(system, user, tag=tag)
        if not result.ok:
            return result

        parsed = _parse_json_loose(result.text)
        if parsed is None:
            result.status = STATUS_INVALID_RESPONSE
            result.error = "模型输出不是可解析的 JSON"
            return result
        result.value = parsed
        return result

    def audit_entries(self) -> list[dict[str, Any]]:
        return [call.to_audit() for call in self.calls]


def _parse_json_loose(text: str) -> Any | None:
    """从模型输出里尽力取出 JSON。

    比 `parse_envelope` 多的两件事：剥掉 ```json 代码块围栏；顶层数组也当合法结果
    （检索词扩写 prompt 要求的就是一个数组）。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        # 去掉 ``` 或 ```json 起止围栏，保留中间内容原样。
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
        raw = raw.strip()
    if not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    payload = parse_envelope(raw)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and payload:
        # parse_envelope 把裸数组包成了 {"status": "OK", "items": [...]}，
        # 这个形状是我们自己造的，还原回数组，调用方不必知道这层包装。
        if payload.get("status") == "OK" and set(payload) == {"status", "items"}:
            return payload["items"]
        return payload
    return None
