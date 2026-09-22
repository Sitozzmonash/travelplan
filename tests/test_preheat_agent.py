"""`app/preheat_agent.py` 的离线单测：一座城市的预热 Agent（P6）。

全部离线：不联网、不真 key、不碰 `data/travelplan.db`（store 落在 tmp_path）。模型与 Hub 都是
替身，但**跑的是真的 Agent Loop**（`create_harness_agent` + LangChain 的工具调用循环），因为要
被验证的正是这段接线：Agent 能调工具、能入库、失败时不写脏数据。

每个用例断言的固定三件事：
1. 预热 Agent 的产物进的是**城市缓存四张表 + 实体层**（不是它自己造的一张小表）；
2. 入库内容来自 Provider 的**原文**，不是模型的转述（正文长度、坐标都要对得上）；
3. 跑不成的时候 `PreheatAgentError` / `places=0`，且缓存一行都没写。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app import city_cache, preheat_agent
from app.preheat_agent import PREHEAT_AGENT_NAME, PreheatAgentError, preheat_city
from app.preheat_tools import (
    CityKnowledgeSink,
    build_preheat_tools,
    clamp_top_n,
    split_mentions,
)
from app.store import TravelPlanStore
from tests.fakes import FakeHub, make_store

CITY = "成都"

#: 预热流程的 user prompt 里有这一句（见 app/preheat_agent._user_prompt）。模型用它区分
#: "这是预热调用"与"这是别的模型调用"（例如长期记忆 consolidation）。
PREHEAT_MARK = "请开始：先搜攻略"

#: 预热 Agent 应该拿到的工具集合 —— 这条清单本身就是"决策边界"的一部分：
#: 机酒火（价格与时刻表永远不会进城市缓存）**不在**里面。
EXPECTED_TOOL_NAMES = {
    "search_guides",
    "search_poi",
    "poi_detail",
    "save_city_evidence",
    "save_city_poi",
}


@pytest.fixture(autouse=True)
def isolated_harness_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """本模块的 Agent 装配不写长期记忆、不打 Console Trace。

    理由与 `tests/test_agent_runner.py` 的同名 fixture 相同：`MemoryMiddleware` 会往
    `data/memory.db`（开发库）写 rollout，攒够条数还会**再调一次模型**做 consolidation ——
    那一次会打乱剧本模型的动作序列，让用例"有时过有时不过"。
    """

    from superharness import agent as harness_agent
    from superharness.config import settings
    from superharness.middleware import ObservabilityMiddleware

    patched = dataclasses.replace(
        settings,
        memory_enabled=False,
        retrieval_enabled=False,
        log_enabled=False,
        trace_enabled=False,
    )
    monkeypatch.setattr(harness_agent, "settings", patched)
    monkeypatch.setattr(ObservabilityMiddleware.__init__, "__defaults__", (patched,))


# ======================================================================
# 替身：剧本模型
# ======================================================================


def _is_preheat_call(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(message, HumanMessage) and PREHEAT_MARK in str(message.content)
        for message in messages
    )


class ScriptedPreheatModel(BaseChatModel):
    """按剧本逐轮出招的离线模型：每一步要么调一个工具，要么直接回复结束。"""

    script: list[dict[str, Any]] = Field(default_factory=list)
    #: 每次模型调用时它看到的工具名（用来验证工具可见性与工具集边界）
    visible: list[list[str]] = Field(default_factory=list)
    #: 每次模型调用时它看到的工具返回文本（用来验证 UNKNOWN_ID 这类反馈真的回给了模型）
    tool_outputs: list[str] = Field(default_factory=list)
    turns: int = 0
    #: 第几轮开始抛异常（模拟"Agent 跑到一半模型炸了"）
    boom_at: int | None = None
    #: 每轮模型调用前的固定延迟（用来验证墙钟超时）
    delay_seconds: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "scripted-preheat"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedPreheatModel:
        self.visible.append(sorted(getattr(tool, "name", "") for tool in tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not _is_preheat_call(messages):
            return _chat(AIMessage(content="（非预热流程调用，跳过）"))

        for message in messages:
            if isinstance(message, ToolMessage):
                text = str(message.content)
                if text not in self.tool_outputs:
                    self.tool_outputs.append(text)

        if self.delay_seconds:
            import time

            time.sleep(self.delay_seconds)
        if self.boom_at is not None and self.turns >= self.boom_at:
            raise RuntimeError("剧本模型：这一步按剧本失败")

        step = self.script[min(self.turns, len(self.script) - 1)]
        self.turns += 1
        name = step.get("tool")
        if name:
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": step.get("args") or {}, "id": f"call-{self.turns}"}],
            )
        else:
            message = AIMessage(content=step.get("text") or "")
        return _chat(message)


def _chat(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


# ======================================================================
# 剧本片段
# ======================================================================


def _happy_script() -> list[dict[str, Any]]:
    """一段最小的"搜 → 抽 → 查高德 → 入库"剧本。

    入库时用 **name** 而不是 place_id：`save_city_poi` 两个键都认（模型少抄一次长 id），
    这条用例顺便把 name 那条查找路径钉住；place_id 那条由 `_realistic_script` 覆盖。
    """

    return [
        {"tool": "search_guides", "args": {"keyword": "成都 三日游", "platform": "xiaohongshu"}},
        {"tool": "save_city_evidence", "args": {"evidence_id": "g1", "place_mentions": "宽窄巷子、武侯祠"}},
        {"tool": "search_poi", "args": {"keywords": "宽窄巷子", "region": CITY}},
        {"tool": "poi_detail", "args": {"poi_id": "poi-宽窄巷子"}},
        {"tool": "save_city_poi", "args": {"place_id": "宽窄巷子", "query": "宽窄巷子"}},
        {"text": "已入库 1 条攻略、1 个地点。"},
    ]


def _realistic_script() -> list[dict[str, Any]]:
    """一段接近真实规模的剧本（15 个模型轮次）：3 条攻略 + 4 批 POI 查询与入库。

    它存在的意义是**钉住默认步数预算够用**：LangGraph 的 `recursion_limit` 里含固定的编排
    开销（可用轮次 ≈ limit − 11），把默认值调小到"看起来更省"就会让真实预热跑到一半被判超步数。
    """

    script: list[dict[str, Any]] = []
    for index in range(3):
        script.append(
            {"tool": "search_guides", "args": {"keyword": f"成都 攻略{index}", "platform": "xiaohongshu"}}
        )
        script.append(
            {"tool": "save_city_evidence", "args": {"evidence_id": f"g{index + 1}", "place_mentions": "宽窄巷子"}}
        )
    for index in range(4):
        script.append({"tool": "search_poi", "args": {"keywords": f"地点{index}", "region": CITY}})
        script.append(
            {"tool": "save_city_poi", "args": {"place_id": f"poi-地点{index}", "query": f"地点{index}"}}
        )
    script.append({"text": "完成。"})
    return script


def _env(tmp_path: Path) -> tuple[TravelPlanStore, FakeHub]:
    return make_store(tmp_path / "travelplan.db"), FakeHub(store=None)


#: 剧本用的步数预算。单测用的是短剧本，这个值要能覆盖它即可 —— 默认值够不够用由
#: `test_realistic_script_fits_the_default_step_budget` 单独钉住。
TEST_STEPS = 24


# ======================================================================
# 1. 正常路径：Agent 入库 → 城市缓存可读（复用路径照旧）
# ======================================================================


def test_agent_preheat_writes_city_cache_readable_by_reuse_path(tmp_path):
    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(script=_happy_script())

    summary = preheat_city(store, CITY, model=model, hub=hub, max_steps=TEST_STEPS)

    assert summary["places"] >= 1, summary
    assert summary["evidences"] == 1
    assert summary["social_evidences"] == 1, "小红书证据必须被认成社媒证据（降级判定靠它）"
    assert summary["queries"] >= 1, "真正发起过的检索词要写进 city_cache_meta"
    assert summary["agent"]["committed"] is True
    assert summary["agent"]["prompt_version"]

    # 复用路径：**新鲜**命中（不是 allow_stale 才拿到），正式规划走的就是这一条
    hit = city_cache.read_candidates(CITY, store=store)
    assert hit is not None, "预热写出来的行必须让 read_candidates 直接命中"
    assert [place.name for place in hit.places] == ["宽窄巷子"]
    assert hit.evidences and hit.evidences[0].text == FakeHub.GUIDE_TEXT, (
        "缓存里的正文必须是 Provider 的原文（模型只给 evidence_id，没法转述）"
    )
    assert hit.mentions, "攻略提及要挂到 POI 上（city_poi_mentions 不能是空表）"
    assert hit.social_served_queries, "检索词要落进 meta（下一个会话据此只补差集）"


def test_pois_go_through_the_resolver_not_around_it(tmp_path):
    """入库必须经过 `app/places.py`：实体层有 canonical / provider refs 才算走对了路。"""

    store, hub = _env(tmp_path)
    preheat_city(
        store, CITY, model=ScriptedPreheatModel(script=_happy_script()), hub=hub, max_steps=TEST_STEPS
    )

    assert store.count_canonical_places(CITY) >= 1, "没有实体层行 = 绕过了 PlaceResolver"
    refs = store.get_place_provider_refs(CITY)
    assert refs and refs[0]["provider"] == "amap"
    assert store.get_place_aliases(CITY), "别名索引要落库（下次同义检索才能免查高德）"
    assert store.get_poi_query_cache_rows(CITY), "查询缓存要落库（query 参数的意义在这里）"
    assert store.get_place_evidence_links(CITY), "证据要挂到 canonical 实体上"


def test_coordinates_come_from_the_hub_not_from_the_model(tmp_path):
    """坐标由录制代理从 Provider 结果里取，模型抄不进来 —— 也就不用担心它抄错。"""

    store, hub = _env(tmp_path)
    sink = CityKnowledgeSink(CITY, store=store, run_id="t")
    tools = {tool.name: tool for tool in build_preheat_tools(hub, sink)}

    # 模型"抄错"的坐标：save_city_poi 根本不接受坐标参数，抄错无处可抄。
    assert "lat" not in tools["save_city_poi"].args_schema.model_fields
    assert "lng" not in tools["save_city_poi"].args_schema.model_fields
    assert "text" not in tools["save_city_evidence"].args_schema.model_fields


# ======================================================================
# 2. 决策边界：工具集里没有机酒火
# ======================================================================


def test_toolset_excludes_transport_and_hotel_tools(tmp_path):
    """价格与时刻表永远不进城市缓存 —— 这条不变量由"工具根本不存在"来保证。"""

    store, hub = _env(tmp_path)
    sink = CityKnowledgeSink(CITY, store=store, run_id="t")

    names = {tool.name for tool in build_preheat_tools(hub, sink)}

    assert names == EXPECTED_TOOL_NAMES, sorted(names)


def test_all_tools_are_visible_to_the_model(tmp_path):
    """能力筛选（tool_top_k=3）不能把入库工具挡掉：Agent 得看得见才能用。"""

    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(script=[{"text": "什么都不做"}])

    preheat_city(store, CITY, model=model, hub=hub, max_steps=TEST_STEPS)

    assert model.visible, "模型一次都没有被调用"
    for names in model.visible:
        assert EXPECTED_TOOL_NAMES <= set(names), sorted(EXPECTED_TOOL_NAMES - set(names))


def test_realistic_script_fits_the_default_step_budget(tmp_path):
    """默认步数预算够一次真实规模的预热（15 个模型轮次）—— 这条防的是"把默认值调小"。

    背景：`recursion_limit` 里含固定的编排开销（实测可用模型轮次 ≈ limit − 11），
    而预热是逐条入库（一条攻略一次、一个地点一次）。默认值一旦低于真实规模，
    线上表现是"每座城市跑到一半被判超步数、什么都不写"，而单测全绿。
    """

    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(script=_realistic_script())

    # 不传 max_steps：用的就是 `config.preheat_agent_steps`
    summary = preheat_city(store, CITY, model=model, hub=hub)

    assert summary["agent"]["committed"] is True, summary
    assert summary["places"] == 4, summary
    assert summary["evidences"] == 3, summary


# ======================================================================
# 3. 失败路径：不写脏数据
# ======================================================================


def test_unknown_ids_are_rejected_and_nothing_is_written(tmp_path):
    """模型编的 id 一律拒绝：不写城市缓存、不建实体，并如实告诉它去哪个工具查。"""

    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(
        script=[
            {"tool": "save_city_evidence", "args": {"evidence_id": "g99", "place_mentions": "杜甫草堂"}},
            {"tool": "save_city_poi", "args": {"place_id": "poi-编造的景点", "query": "编造"}},
            {"text": "入库完成。"},
        ]
    )

    summary = preheat_city(store, CITY, model=model, hub=hub, max_steps=TEST_STEPS)

    assert summary["places"] == 0 and summary["evidences"] == 0
    assert summary["degradations"], "没有写入必须如实记降级"
    pois, mentions = store.get_city_cache_rows(CITY)
    assert pois == [] and mentions == []
    assert store.count_canonical_places(CITY) == 0, "被拒绝的 POI 不该建实体"
    feedback = "\n".join(model.tool_outputs)
    assert "UNKNOWN_ID" in feedback, "拒绝的原因要回到模型手上，它才知道换工具"
    assert "search_guides" in feedback and "search_poi" in feedback


def test_agent_failure_writes_nothing_even_after_a_partial_selection(tmp_path):
    """Agent 跑到一半崩了：已经选中的东西也**不提交**（不留半座城的脏数据）。"""

    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(script=_happy_script()[:3], boom_at=3)

    with pytest.raises(PreheatAgentError) as excinfo:
        preheat_city(store, CITY, model=model, hub=hub, max_steps=TEST_STEPS)

    assert "剧本模型" in str(excinfo.value), "失败原因要如实带出来"
    pois, mentions = store.get_city_cache_rows(CITY)
    assert pois == [] and mentions == []
    assert store.count_canonical_places(CITY) == 0
    assert city_cache.read_candidates(CITY, store=store, allow_stale=True) is None


def test_timeout_is_reported_and_writes_nothing(tmp_path):
    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(script=_happy_script(), delay_seconds=5)

    with pytest.raises(PreheatAgentError) as excinfo:
        preheat_city(store, CITY, model=model, hub=hub, timeout_seconds=1)

    assert "超时" in str(excinfo.value)
    pois, _ = store.get_city_cache_rows(CITY)
    assert pois == []


def test_step_limit_is_reported_with_its_own_message(tmp_path):
    """步数用尽要说"步数"，不能混成一句普通报错（两者要调的是不同配置）。"""

    store, hub = _env(tmp_path)
    model = ScriptedPreheatModel(
        script=[{"tool": "search_guides", "args": {"keyword": "成都 攻略", "platform": "web"}}]
    )

    with pytest.raises(PreheatAgentError) as excinfo:
        preheat_city(store, CITY, model=model, hub=hub, max_steps=4)

    assert "最大步数" in str(excinfo.value)
    pois, _ = store.get_city_cache_rows(CITY)
    assert pois == []


def test_missing_model_fails_loudly(tmp_path, monkeypatch):
    """一个模型都没配全：如实报失败，不静默跑出一条"空预热"。

    这里把"取模型"这一步替换掉，而不是去删环境变量：`.env` 可能配了 `MODEL_*_bk1/bk2`，
    删掉主模型之后备用模型会顶上，用例就变成"拿真模型跑真网络"了（开发机上真的会发生）。
    """

    store, hub = _env(tmp_path)
    monkeypatch.setattr(preheat_agent, "_model_and_fallbacks", lambda model: (None, []))

    with pytest.raises(PreheatAgentError) as excinfo:
        preheat_city(store, CITY, hub=hub)

    assert "MODEL_NAME" in str(excinfo.value)
    assert store.get_city_cache_rows(CITY) == ([], [])


# ======================================================================
# 4. 工具层的小纯函数（不依赖 Hub / 模型）
# ======================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("宽窄巷子、武侯祠", ["宽窄巷子", "武侯祠"]),
        ("宽窄巷子, 武侯祠；锦里\n杜甫草堂", ["宽窄巷子", "武侯祠", "锦里", "杜甫草堂"]),
        (["宽窄巷子, 武侯祠"][0], ["宽窄巷子", "武侯祠"]),
        ("", []),
        ("宽窄巷子、宽窄巷子", ["宽窄巷子"]),
    ],
)
def test_split_mentions(raw, expected):
    assert split_mentions(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [(0, 5), (-1, 5), ("x", 5), (100, 10), (3, 3)])
def test_clamp_top_n(raw, expected):
    assert clamp_top_n(raw) == expected


def test_agent_name_is_not_the_planner_name():
    assert PREHEAT_AGENT_NAME != "travelplan-planner"
