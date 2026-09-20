"""tikhub_social Plugin 的单元测试。

**不联网**：所有 HTTP 都被替身拦住（把模块里的 ``httpx`` 换成 FakeHttpx），
测试只验证「配额闸门怎么判、参数怎么拼、返回怎么归一化、失败怎么映射、
token 有没有泄漏」。

模块加载方式与 PluginLoader 完全一致（``spec_from_file_location`` 按文件路径加载），
所以这里也按路径加载 —— 顺便验证「tool 模块不能 import 同 Plugin 其它模块」这条约束成立。

关于 fixture 的真实性（如实说明）
--------------------------------
* ``USER_INFO_*`` 是**实测样本**（2026-09-18 curl 真实响应，token 与邮箱已替换）：
  free_credit / balance 在 ``user_data`` 下。
* ``XHS_*`` 是**构造样本**：该路由在当前账号上实测返回 402（免费额度为 0、
  且路由不接受免费额度），拿不到成功响应体。结构按 openapi.json 的说明 +
  ``note_card`` 包裹 / 平铺两种已知写法构造，**不代表实测字段名**。
* ``DOUYIN_*`` 同为构造样本，字段名取自 openapi.json 对该端点的返回说明
  （aweme_id / desc / create_time / author / statistics / share_url）。
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = (
    REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins" / "tikhub_social"
)
MODULE_PATH = PLUGIN_DIR / "tools" / "search.py"

EXPECTED_TOOL_NAMES = {"get_tikhub_quota", "search_xiaohongshu", "search_douyin"}

#: 任何"充值 / 加额度 / 关掉免费约束"的 tool 都不允许存在。
FORBIDDEN_TOOL_FRAGMENTS = (
    "recharge", "topup", "top_up", "add_credit", "purchase", "buy",
    "disable", "free_only", "freeonly", "switch", "upgrade", "pay",
)

FAKE_TOKEN = "tikhub-test-token-11112222deadbeef"
TOKEN_ENV = "TIKHUB_API_TOKEN"

USER_INFO_PATH_KEY = "user/get_user_info"
XHS_PATH_KEY = "xiaohongshu/app_v2/search_notes"
DOUYIN_PATH_KEY = "douyin/search/fetch_general_search_v3"


# ==================================================
# 加载 / 替身
# ==================================================

def _load_module():
    """按文件路径加载 plugin 内的 tool 模块（与 PluginLoader 的做法一致）。"""
    spec = importlib.util.spec_from_file_location("tikhub_social_search", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeHttpx:
    """``httpx`` 的替身：按 URL 子串分派预置响应，并记录每一次调用。

    ``raises`` 里的 key 命中时直接抛异常，用于覆盖传输层失败路径。

    刻意做成"整个模块对象"的替身（而不是 patch 全局 ``httpx.get``）：
    这样不会污染同进程里其它测试或其它 Plugin 对 httpx 的使用。
    """

    TimeoutException = httpx.TimeoutException
    HTTPError = httpx.HTTPError

    def __init__(self, routes: dict | None = None, raises: dict | None = None):
        self.routes = routes or {}
        self.raises = raises or {}
        self.calls: list[dict] = []

    # ---- 内部 ----
    def _respond(self, method: str, url: str, params=None, body=None, headers=None) -> FakeResponse:
        self.calls.append({
            "method": method,
            "url": url,
            "params": dict(params or {}),
            "body": dict(body or {}),
            "headers": dict(headers or {}),
        })

        for key, exc in self.raises.items():
            if key in url:
                raise exc

        for key, response in self.routes.items():
            if key in url:
                return response

        raise AssertionError(f"未预期的请求（说明有 tool 调了不该调的端点）: {method} {url}")

    # ---- httpx 的接口面 ----
    def get(self, url, headers=None, params=None, timeout=None, **kwargs):
        return self._respond("GET", url, params=params, headers=headers)

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):  # noqa: A002 - 对齐 httpx 形参名
        return self._respond("POST", url, body=json, headers=headers)

    # ---- 断言辅助 ----
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]

    def calls_to(self, key: str) -> list[dict]:
        return [call for call in self.calls if key in call["url"]]


def _user_info(free_credit, balance, *, include_balance: bool = True) -> dict:
    """实测形状的账户响应（字段在 user_data 下）。"""
    user_data = {
        "email": "traveller@example.com",
        "free_credit": free_credit,
        "email_verified": True,
        "account_disabled": False,
        "is_active": True,
    }
    if include_balance:
        user_data["balance"] = balance

    return {
        "code": 200,
        "router": "/api/v1/tikhub/user/get_user_info",
        "api_key_data": {"api_key_name": "旅行规划", "api_key_status": 1},
        "user_data": user_data,
    }


def _ok(*payloads) -> FakeResponse:
    return FakeResponse(200, payloads[0])


XHS_PAYLOAD = {
    "code": 200,
    "router": "/api/v1/xiaohongshu/app_v2/search_notes",
    "data": {
        "items": [
            {
                "id": "66f0123456789abc000001",
                "xsec_token": "ABxyz",
                "note_card": {
                    "note_id": "66f0123456789abc000001",
                    "display_title": "成都三天两夜攻略",
                    "desc": "第一天宽窄巷子，第二天熊猫基地，第三天都江堰。",
                    "user": {"user_id": "u1001", "nickname": "小吃货阿May"},
                    "interact_info": {
                        "liked_count": "2385",
                        "collected_count": "815",
                        "comment_count": "120",
                    },
                    "time": 1694102400000,
                },
            },
            {"type": "ads", "ad_info": {"position": 1}},  # 广告卡没有 note_id → 应被丢弃
        ],
        "pagination": {"page": 1, "has_more": True},
    },
}

DOUYIN_PAYLOAD = {
    "code": 200,
    "router": "/api/v1/douyin/search/fetch_general_search_v3",
    "data": {
        "items": [
            {
                "aweme_id": "7300000000000000001",
                "desc": "成都美食合集｜本地人带路\n第二行不该出现在标题里",
                "create_time": 1694102400,
                "author": {"uid": "123456", "sec_uid": "SEC123", "nickname": "成都吃喝玩乐"},
                "statistics": {
                    "digg_count": 12345,
                    "comment_count": 678,
                    "share_count": 90,
                    "play_count": 100000,
                },
                "share_url": "https://www.douyin.com/video/7300000000000000001",
            },
            {"type": "related_words", "words": ["成都火锅", "成都串串"]},  # 非视频卡片 → 丢弃
        ],
        "pagination": {"has_more": 0, "next_page": None},
    },
}

SUBMIT_PAYLOAD = {
    "detail": {
        "code": 402,
        "request_id": "req-123",
        "message": "Insufficient balance, this endpoint requires payment and does not accept free credit",
        "message_zh": "免费额度以及余额不足，此路由需要付费，并且不接受使用免费额度请求",
    }
}


@pytest.fixture
def module():
    return _load_module()


@pytest.fixture
def env_token(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, FAKE_TOKEN)
    return FAKE_TOKEN


def _install(monkeypatch, module, fake: FakeHttpx) -> FakeHttpx:
    monkeypatch.setattr(module, "httpx", fake)
    return fake


# ==================================================
# 闸门：不该花钱的时候，一个内容请求都不许发
# ==================================================

class TestQuotaGate:
    def test_free_credit_zero_never_touches_content_endpoint(self, module, monkeypatch, env_token):
        """free_credit = 0（实测的当前状态）→ 直接 FREE_CREDIT_EXHAUSTED，不发内容请求。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(0.0, 0.0)),
        }))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "FREE_CREDIT_EXHAUSTED"
        assert envelope["items"] == []
        assert envelope["count"] == 0
        assert envelope["provider"] == "tikhub"
        assert envelope["platform"] == "xhs"
        assert envelope["quota"] == {"free_credit": 0.0, "balance": 0.0}
        assert "免费额度" in envelope["error"]

        # 核心断言：内容端点从未被请求过（一次都没有）。
        assert fake.calls_to(XHS_PATH_KEY) == []
        assert fake.calls_to(DOUYIN_PATH_KEY) == []
        assert len(fake.calls) == 1
        assert USER_INFO_PATH_KEY in fake.urls()[0]

    def test_safe_mode_blocks_when_balance_also_positive(self, module, monkeypatch, env_token):
        """free_credit > 0 且 balance > 0 → 无法证明先扣免费额度 → SAFE_MODE_BLOCK。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(5.0, 2.0)),
        }))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "SAFE_MODE_BLOCK"
        assert envelope["items"] == []
        assert envelope["quota"] == {"free_credit": 5.0, "balance": 2.0}
        assert "SAFE_MODE" in envelope["error"]
        assert fake.calls_to(XHS_PATH_KEY) == []

    def test_safe_mode_blocks_when_balance_unreadable(self, module, monkeypatch, env_token):
        """balance 读不到（未知 != 0）→ 同样保守拒绝。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(5.0, None, include_balance=False)),
        }))

        envelope = json.loads(module.search_douyin.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "SAFE_MODE_BLOCK"
        assert envelope["items"] == []
        assert fake.calls_to(DOUYIN_PATH_KEY) == []

    def test_quota_in_api_key_data_is_still_recognized(self, module, monkeypatch, env_token):
        """文档把 free_credit 写在 api_key_data 下，实测在 user_data 下 —— 两处都要认。"""
        payload = {
            "code": 200,
            "api_key_data": {"api_key_name": "旅行规划", "free_credit": 0.0, "balance": 0.0},
        }
        _install(monkeypatch, module, FakeHttpx({USER_INFO_PATH_KEY: _ok(payload)}))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "FREE_CREDIT_EXHAUSTED"
        assert envelope["quota"] == {"free_credit": 0.0, "balance": 0.0}

    def test_missing_quota_fields_is_invalid_response(self, module, monkeypatch, env_token):
        """账户响应里既没有 free_credit 也没有 balance → 判不出来就不许调内容接口。"""
        _install(monkeypatch, module, FakeHttpx({USER_INFO_PATH_KEY: _ok({"code": 200, "data": {}})}))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "INVALID_RESPONSE"
        assert envelope["quota"] == {"free_credit": None, "balance": None}

    def test_account_endpoint_rate_limit_maps_to_rate_limit(self, module, monkeypatch, env_token):
        """实测：连续调用账户接口会 429。限流就是 RATE_LIMIT，不是含糊的 UNAVAILABLE。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: FakeResponse(429, {"detail": "too many requests"}),
        }))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "RATE_LIMIT"
        assert envelope["items"] == []
        assert fake.calls_to(XHS_PATH_KEY) == []


# ==================================================
# 402：额度耗尽，不重试、不回落现金余额
# ==================================================

class TestPaymentRequired:
    def test_402_maps_to_free_credit_exhausted_and_does_not_retry(self, module, monkeypatch, env_token):
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(1.0, 0.0)),
            XHS_PATH_KEY: FakeResponse(402, SUBMIT_PAYLOAD),
        }))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "FREE_CREDIT_EXHAUSTED"
        assert envelope["items"] == []
        assert envelope["error"] and "402" in envelope["error"]

        # 不重试：内容端点恰好被请求一次。
        assert len(fake.calls_to(XHS_PATH_KEY)) == 1

    def test_business_code_402_in_http_200_body_also_maps(self, module, monkeypatch, env_token):
        """有的路由 HTTP 200、业务码放在 body 里，同样必须识别为额度耗尽。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(1.0, 0.0)),
            DOUYIN_PATH_KEY: _ok({"code": 402, "detail": {"code": 402, "message": "Insufficient balance"}}),
        }))

        envelope = json.loads(module.search_douyin.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "FREE_CREDIT_EXHAUSTED"
        assert len(fake.calls_to(DOUYIN_PATH_KEY)) == 1

    def test_rate_limit_and_auth_error_are_distinguished(self, module, monkeypatch, env_token):
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(1.0, 0.0)),
            XHS_PATH_KEY: FakeResponse(429, {"detail": "too many requests"}),
        }))
        assert json.loads(module.search_xiaohongshu.invoke({"keyword": "成都"}))["status"] == "RATE_LIMIT"

        fake2 = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(1.0, 0.0)),
            XHS_PATH_KEY: FakeResponse(403, {"detail": "forbidden"}),
        }))
        assert json.loads(module.search_xiaohongshu.invoke({"keyword": "成都"}))["status"] == "AUTH_ERROR"
        assert len(fake2.calls_to(XHS_PATH_KEY)) == 1


# ==================================================
# 正常路径：允许调用 + 归一化
# ==================================================

class TestAllowedSearch:
    def test_allows_call_when_free_credit_positive_and_balance_zero(self, module, monkeypatch, env_token):
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0)),
            XHS_PATH_KEY: _ok(XHS_PAYLOAD),
        }))

        envelope = json.loads(
            module.search_xiaohongshu.invoke({"keyword": " 成都美食 ", "page": 2, "time_filter": "week"})
        )

        assert envelope["status"] == "OK"
        assert envelope["count"] == 1  # 广告卡（无 note_id）被丢弃，不补造
        assert len(fake.calls_to(XHS_PATH_KEY)) == 1

        item = envelope["items"][0]
        assert item["platform"] == "xhs"
        assert item["content_id"] == "66f0123456789abc000001"
        assert item["title"] == "成都三天两夜攻略"
        assert item["desc"].startswith("第一天宽窄巷子")
        assert item["author"] == "小吃货阿May"
        assert item["author_id"] == "u1001"
        assert item["likes"] == 2385  # 上游给的是字符串 "2385"
        assert item["comments"] == 120
        assert item["collects"] == 815
        assert item["publish_time"].startswith("2023-09-07")  # 毫秒时间戳 → ISO
        assert item["url"] == "https://www.xiaohongshu.com/explore/66f0123456789abc000001"
        assert item["source"] == "tikhub"
        assert isinstance(item["raw"], dict)

        # 参数拼装：去空格、页码透传、英文别名映射成上游要的中文常量。
        call = fake.calls_to(XHS_PATH_KEY)[0]
        assert call["method"] == "GET"
        assert call["params"]["keyword"] == "成都美食"
        assert call["params"]["page"] == 2
        assert call["params"]["sort_type"] == "general"
        assert call["params"]["time_filter"] == "一周内"

    def test_douyin_general_search_is_a_post_with_openapi_body(self, module, monkeypatch, env_token):
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0)),
            DOUYIN_PATH_KEY: _ok(DOUYIN_PAYLOAD),
        }))

        envelope = json.loads(module.search_douyin.invoke({"keyword": "成都美食", "page": 1}))

        assert envelope["status"] == "OK"
        assert envelope["count"] == 1

        item = envelope["items"][0]
        assert item["platform"] == "douyin"
        assert item["content_id"] == "7300000000000000001"
        assert item["title"] == "成都美食合集｜本地人带路"  # desc 首行，不是编的
        assert item["author"] == "成都吃喝玩乐"
        assert item["author_id"] == "123456"
        assert item["likes"] == 12345
        assert item["comments"] == 678
        assert item["collects"] is None  # 抖音没有收藏数 → None，不填 0
        assert item["publish_time"].startswith("2023-09-07")
        assert item["url"].endswith("/video/7300000000000000001")
        assert item["source"] == "tikhub"

        # openapi 的 GeneralSearchV3Request：offset=0、search_id/backtrace 传空串。
        call = fake.calls_to(DOUYIN_PATH_KEY)[0]
        assert call["method"] == "POST"
        assert call["body"] == {
            "keyword": "成都美食",
            "offset": 0,
            "page": 1,
            "search_id": "",
            "backtrace": "",
        }

    def test_empty_result_set_reports_empty(self, module, monkeypatch, env_token):
        _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0)),
            XHS_PATH_KEY: _ok({"code": 200, "data": {"items": []}}),
        }))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "不存在的关键词"}))

        assert envelope["status"] == "EMPTY"
        assert envelope["items"] == []
        assert envelope["error"] is None

    def test_invalid_sort_is_rejected_before_any_http_call(self, module, monkeypatch, env_token):
        """参数非法要在发请求之前拒绝 —— 少一次计费往返。"""
        fake = _install(monkeypatch, module, FakeHttpx({}))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都", "sort": "most_viewed"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert "sort" in envelope["error"]
        assert fake.calls == []

    def test_blank_keyword_is_rejected_without_http(self, module, monkeypatch, env_token):
        fake = _install(monkeypatch, module, FakeHttpx({}))

        envelope = json.loads(module.search_douyin.invoke({"keyword": "   "}))

        assert envelope["status"] == "UNAVAILABLE"
        assert fake.calls == []


# ==================================================
# 配额探针
# ==================================================

class TestQuotaProbe:
    def test_quota_probe_is_callable_when_free_credit_is_zero(self, module, monkeypatch, env_token):
        """账户接口不是内容接口：free_credit = 0 时依然要能查（否则上层不知道发生了什么）。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(0.0, 0.0)),
        }))

        envelope = json.loads(module.get_tikhub_quota.invoke({}))

        assert envelope["status"] == "OK"
        assert envelope["quota"] == {"free_credit": 0.0, "balance": 0.0}
        assert envelope["items"] == []
        assert len(fake.calls) == 1
        assert fake.calls_to(XHS_PATH_KEY) == []

    def test_quota_probe_without_token_returns_auth_error_and_sends_nothing(self, module, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        fake = _install(monkeypatch, module, FakeHttpx({}))

        envelope = json.loads(module.get_tikhub_quota.invoke({}))

        assert envelope["status"] == "AUTH_ERROR"
        assert TOKEN_ENV in envelope["error"]
        assert fake.calls == []


# ==================================================
# 鉴权与 token 泄漏
# ==================================================

class TestTokenHandling:
    def test_missing_token_returns_auth_error_and_sends_no_request(self, module, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        fake = _install(monkeypatch, module, FakeHttpx({}))

        envelope = json.loads(module.search_xiaohongshu.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "AUTH_ERROR"
        assert envelope["items"] == []
        assert TOKEN_ENV in envelope["error"]
        assert fake.calls == []  # 连账户接口都不调

    def test_token_never_appears_in_output_or_error(self, module, monkeypatch, env_token):
        """上游把 token 回显进错误体 / 异常文案时，返回值里也不能出现它。"""
        # 1) 上游错误体里回显了 token
        _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0)),
            XHS_PATH_KEY: FakeResponse(500, f"internal error, token={FAKE_TOKEN}"),
        }))
        raw = module.search_xiaohongshu.invoke({"keyword": "成都"})
        assert FAKE_TOKEN not in raw
        assert "***" in raw or "HTTP 500" in raw

        # 2) 传输层异常文案里带了 token
        _install(monkeypatch, module, FakeHttpx(
            {USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0))},
            raises={XHS_PATH_KEY: httpx.HTTPError(f"connect failed for token {FAKE_TOKEN}")},
        ))
        raw = module.search_xiaohongshu.invoke({"keyword": "成都"})
        assert FAKE_TOKEN not in raw
        assert json.loads(raw)["status"] == "UNAVAILABLE"

    def test_token_never_appears_in_query_or_headers_echo(self, module, monkeypatch, env_token):
        """token 只允许出现在请求头里（Authorization），不进 query、不进返回值。"""
        fake = _install(monkeypatch, module, FakeHttpx({
            USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0)),
            XHS_PATH_KEY: _ok(XHS_PAYLOAD),
        }))

        raw = module.search_xiaohongshu.invoke({"keyword": "成都美食", "page": 1, "sort": "general"})
        envelope = json.loads(raw)

        assert FAKE_TOKEN not in raw
        for call in fake.calls:
            assert call["headers"].get("Authorization") == f"Bearer {FAKE_TOKEN}"
            assert FAKE_TOKEN not in json.dumps(call["params"], ensure_ascii=False)
            assert FAKE_TOKEN not in json.dumps(call["body"], ensure_ascii=False)
            assert FAKE_TOKEN not in call["url"]

    def test_account_probe_never_returns_email_or_token(self, module, monkeypatch, env_token):
        """账户响应里的邮箱等个人信息不进返回值。"""
        _install(monkeypatch, module, FakeHttpx({USER_INFO_PATH_KEY: _ok(_user_info(0.0, 0.0))}))

        raw = module.get_tikhub_quota.invoke({})

        assert FAKE_TOKEN not in raw
        assert "traveller@example.com" not in raw


# ==================================================
# FREE_ONLY 约束
# ==================================================

class TestFreeOnlyConstraint:
    def test_tools_expose_no_recharge_or_switch(self, module):
        """没有充值 tool，也没有关掉 FREE_ONLY 的开关 —— 名字集合是硬断言。"""
        names = {tool.name for tool in module.TOOLS}

        assert names == EXPECTED_TOOL_NAMES
        for name in names:
            for fragment in FORBIDDEN_TOOL_FRAGMENTS:
                assert fragment not in name.lower(), f"tool {name} 命中禁用片段 {fragment}"

    def test_free_only_is_a_module_constant_not_a_parameter(self, module):
        assert module.FREE_ONLY is True
        assert module.SAFE_MODE is True

        for tool in module.TOOLS:
            schema = tool.args_schema.model_json_schema() if hasattr(tool, "args_schema") else {}
            properties = set((schema or {}).get("properties", {}))
            assert not any("credit" in key or "free" in key or "balance" in key for key in properties), (
                f"{tool.name} 暴露了额度相关参数，等于给了一个绕过免费约束的旋钮"
            )

    def test_quota_gate_refuses_to_run_if_free_only_is_flipped(self, module, monkeypatch, env_token):
        """把常量改成 False 不会"放行付费调用"，而是直接抛错。"""
        fake = _install(monkeypatch, module, FakeHttpx({USER_INFO_PATH_KEY: _ok(_user_info(10.0, 0.0))}))

        monkeypatch.setattr(module, "FREE_ONLY", False)

        with pytest.raises(RuntimeError):
            module.search_xiaohongshu.invoke({"keyword": "成都"})

        assert fake.calls_to(XHS_PATH_KEY) == []


# ==================================================
# Manifest / PluginLoader
# ==================================================

class TestManifestAndLoader:
    def test_manifest_declares_expected_module_and_no_entrypoint(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))

        assert manifest["plugin"]["name"] == "tikhub_social"
        assert manifest["plugin"]["enabled"] is True
        assert manifest["plugin"]["version"] == "0.1.0"
        assert manifest["tools"]["modules"] == ["tools.search"]
        assert (PLUGIN_DIR / "tools" / "search.py").is_file()
        # 声明了 tools.modules 时允许没有 entrypoint（见 loader.py 的 _load_entrypoint）
        assert not (PLUGIN_DIR / "plugin.py").exists()

    def test_manifest_description_reads_like_user_speech(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
        description = manifest["plugin"]["description"]

        for keyword in ("小红书", "抖音", "攻略", "美食", "FREE_CREDIT_EXHAUSTED"):
            assert keyword in description, keyword
        assert "不会自动付费" in description

    def test_plugin_loader_discovers_tikhub_social_and_loads_three_tools(self, monkeypatch):
        """与装配期完全一致的发现 / 加载路径（只 import，不发请求）。"""
        from superharness.capabilities import PluginLoader

        monkeypatch.delenv(TOKEN_ENV, raising=False)

        root = REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins"
        loader = PluginLoader(root)

        candidates = {candidate.name: candidate for candidate in loader.candidates()}
        assert "tikhub_social" in candidates
        assert candidates["tikhub_social"].id == "plugin:tikhub_social"
        assert candidates["tikhub_social"].kind == "plugin"

        manifest = next(item for item in loader.manifests if item.name == "tikhub_social")
        tools = loader.load_tools(manifest)

        assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES
