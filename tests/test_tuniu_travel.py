"""tuniu_travel Plugin 的单元测试。

**不联网、不调真实 CLI**：所有外部调用都被替身拦住，测试只验证
「参数怎么拼、key 从哪来、返回怎么解析、失败怎么映射、有没有越界暴露写操作」。

模块加载方式与 PluginLoader 一致（``spec_from_file_location`` 按文件路径加载），
所以这里也按路径加载 —— 顺便验证了「tool 模块不能 import 同 Plugin 其它模块」
这条约束真的成立。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = (
    REPO_ROOT
    / "super_harness"
    / "superharness"
    / "capabilities"
    / "plugins"
    / "tuniu_travel"
)
TOOLS_DIR = PLUGIN_DIR / "tools"

MODULE_NAMES = ("flight", "hotel", "train", "ticket", "cruise", "holiday")

EXPECTED_TOOL_NAMES = {
    "tuniu_search_flights",
    "tuniu_search_hotels",
    "tuniu_search_trains",
    "tuniu_search_scenic_tickets",
    "tuniu_search_cruises",
    "tuniu_search_holiday_packages",
}

#: V1 只查询：这些写操作即使 CLI 有，也不允许出现在任何 tool 里。
FORBIDDEN_WRITE_OPS = {
    "saveOrder",
    "cancelOrder",
    "bookTrain",
    "tuniuHotelCreateOrder",
    "create_ticket_order",
    "saveCruiseOrder",
    "saveHolidayOrder",
    "queryTrainOrderDetail",
}

ALLOWED_SERVERS = {"flight", "hotel", "train", "ticket", "cruise", "holiday", "intelflight"}

FAKE_KEY = "tuniu-test-key-abc123"


# ==================================================
# 加载 / 替身
# ==================================================

def _load_module(name: str):
    """按文件路径加载 plugin 内的 tool 模块（与 PluginLoader 的做法一致）。"""
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tuniu_travel_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCLI:
    """subprocess.run 的替身：记录调用、返回预置输出。"""

    def __init__(self, stdout: bytes = b"", returncode: int = 0, stderr: bytes = b"", raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr=self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]

    @property
    def env(self) -> dict:
        return self.calls[-1][1]["env"]


def _install(monkeypatch, fake: FakeCLI) -> FakeCLI:
    monkeypatch.setenv("TUNIU_API_KEY", FAKE_KEY)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _envelope(result, *, success: bool = True, structured: bool = True, error=None) -> bytes:
    """按实测的 CLI 信封形状造一份 stdout。"""
    text = json.dumps(result, ensure_ascii=False)

    if not success:
        payload = {"success": False, "error": error or {"type": "Unknown", "message": "", "code": 0}}
    else:
        inner = {"content": [{"type": "text", "text": text}], "isError": False}
        if structured:
            inner["structuredContent"] = {"result": text}
        payload = {
            "success": True,
            "result": inner,
            "metadata": {"server": "x", "tool": "y", "latency_ms": 12},
        }

    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _out(raw: str) -> dict:
    return json.loads(raw)


# ==================================================
# 实测样本（原样取自真实返回）
# ==================================================

FLIGHT_RESULT = {
    "data": [
        {
            "airlineCompany": "国航",
            "flightNumber": "CA4110",
            "craftType": "32Q",
            "arrivalTime": "2026-10-01 22:30",
            "arrivalAirport": "双流",
            "arrivalTerminal": "T2",
            "departureTime": "2026-10-01 19:30",
            "departureAirport": "首都",
            "departureTerminal": "T3",
            "cabinClass": "经济舱",
            "remainingSeats": "4",
            "totalDuration": "3h",
            "type": "直飞",
            "basePrice": "1170",
            "flyTime": "3h",
            "totalTax": "120",
        }
    ],
    "successCode": True,
    "totalPageNum": 1,
}

HOTEL_RESULT = {
    "message": "找到 8 家酒店",
    "success": True,
    "queryId": "qid-001",
    "totalPageNum": 2,
    "currentPageNum": 1,
    "cityInfo": {"cityCode": 2802, "cityName": "成都"},
    "hotels": [
        {
            "hotelId": 1912107132,
            "hotelName": "丽橙酒店·逸（成都春熙路天府广场店）",
            "starName": "高档型",
            "firstPic": "https://example.com/a.jpg",
            "address": "顺城大街 1 号",
            "cityName": "成都",
            "cityCode": 2802,
            "business": "顺城大街/天府广场",
            "brandName": "美居",
            "commentScore": 4.8,
            "commentDigest": "位置很好",
            "lowestPrice": 600,
            "meal": "无早餐",
            "refund": "限时取消",
            "roomName": "智悦大床房+双早",
            "roomArea": "28㎡",
            "roomWindow": "部分有窗",
        }
    ],
}

TRAIN_RESULT = {
    "successCode": True,
    "data": [
        {
            "trainNum": "K117",
            "departStationName": "北京西",
            "destStationName": "成都西",
            "trainType": "direct",
            "departureTime": "2026-10-01 11:54",
            "arrivalTime": "2026-10-02 19:16",
            "price": {
                "rwPrice": "703.0",
                "wzPrice": "251.0",
                "ywPrice": "456.0",
                "yzPrice": "251.0",
                "edzPrice": "",
                "swzPrice": "",
            },
            "duration": "31时22分",
            "seatAvailable": {
                "rwNum": 1,
                "wzNum": 0,
                "ywNum": 99,
                "yzNum": 12,
                "edzNum": None,
                "swzNum": None,
            },
        }
    ],
}


# ==================================================
# CLI 参数构造 / 凭据传递
# ==================================================

class TestCLIInvocation:
    def test_command_shape(self):
        flight = _load_module("flight")
        payload = {"departureCityName": "北京", "arrivalCityName": "成都", "departureDate": "2026-10-01"}

        cmd = flight._build_command("flight", "searchLowestPriceFlight", payload, binary="C:/fake/tuniu.cmd")

        assert cmd[0] == "C:/fake/tuniu.cmd"
        assert cmd[1:4] == ["call", "flight", "searchLowestPriceFlight"]
        assert cmd[4] == "-a"
        assert json.loads(cmd[5]) == payload
        assert cmd[6:] == ["-o", "json"]

    def test_payload_json_is_utf8_unescaped(self):
        flight = _load_module("flight")
        cmd = flight._build_command("flight", "searchLowestPriceFlight", {"departureCityName": "北京"})
        # ensure_ascii=False：中文直接进参数字符串，CLI 侧才不用二次解码
        assert "北京" in cmd[5]

    def test_which_is_used_for_bare_command(self):
        """裸命令名必须走 shutil.which：Windows 上 tuniu 实际是 tuniu.cmd。"""
        flight = _load_module("flight")
        resolved = flight._resolve_binary()

        if os.name == "nt":
            assert resolved.lower().endswith((".cmd", ".bat")) or Path(resolved).exists()

        # 显式传入时不做解析，原样使用
        assert flight._resolve_binary("C:/fake/tuniu.cmd") == "C:/fake/tuniu.cmd"

    def test_key_passed_via_env_and_not_in_argv(self, monkeypatch):
        flight = _load_module("flight")
        fake = _install(monkeypatch, FakeCLI(_envelope(FLIGHT_RESULT)))

        flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        )

        assert fake.env["TUNIU_API_KEY"] == FAKE_KEY
        # key 绝不能进命令行参数（会出现在进程列表里）
        assert all(FAKE_KEY not in part for part in fake.argv)

    def test_key_not_hardcoded_in_source(self):
        for name in MODULE_NAMES + ("__init__",):
            path = TOOLS_DIR / f"{name}.py"
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            assert "TUNIU_API_KEY" in text or name == "__init__"
            # 不得出现任何看起来像已写入的 key 字面量
            assert "sk-" not in text
            assert FAKE_KEY not in text

    def test_default_timeout_helpers(self):
        flight = _load_module("flight")
        ticket = _load_module("ticket")

        assert flight.DEFAULT_TIMEOUT == 60.0
        # ticket 实测 >90s，默认超时必须更大
        assert ticket.DEFAULT_TIMEOUT >= 120.0

    def test_timeout_argument_forwarded(self, monkeypatch):
        ticket = _load_module("ticket")
        fake = _install(monkeypatch, FakeCLI(_envelope({"data": []})))

        ticket.tuniu_search_scenic_tickets.invoke({"scenic_name": "武侯祠", "timeout": 7})

        assert fake.calls[-1][1]["timeout"] == 7
        assert json.loads(fake.argv[5]) == {"scenic_name": "武侯祠"}


# ==================================================
# 失败映射
# ==================================================

class TestFailureMapping:
    def test_missing_key_is_auth_error_and_cli_not_called(self, monkeypatch):
        flight = _load_module("flight")
        monkeypatch.delenv("TUNIU_API_KEY", raising=False)
        fake = FakeCLI(_envelope(FLIGHT_RESULT))
        monkeypatch.setattr(subprocess, "run", fake)

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "AUTH_ERROR"
        assert "未配置 TUNIU_API_KEY" in out["error"]
        assert fake.calls == []
        assert out["count"] == 0
        assert out["items"] == []

    def test_timeout_maps_to_timeout(self, monkeypatch):
        flight = _load_module("flight")
        fake = _install(monkeypatch, FakeCLI(raises=subprocess.TimeoutExpired(cmd=["tuniu"], timeout=1)))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "TIMEOUT"
        assert out["items"] == []
        assert out["error"]
        assert fake.calls  # 确实尝试过调用

    def test_oauth_login_required_maps_to_auth_error(self, monkeypatch):
        hotel = _load_module("hotel")
        _install(monkeypatch, FakeCLI(_envelope(
            None,
            success=False,
            error={"type": "OAuthLoginRequiredError", "message": "请先登录", "code": 110},
        )))

        out = _out(hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05"}
        ))

        assert out["status"] == "AUTH_ERROR"
        assert "OAuthLoginRequiredError" in out["error"]
        assert "110" in out["error"]

    def test_other_business_error_maps_to_unavailable(self, monkeypatch):
        hotel = _load_module("hotel")
        _install(monkeypatch, FakeCLI(_envelope(
            None,
            success=False,
            error={"type": "UpstreamTimeoutError", "message": "上游超时", "code": 500},
        )))

        out = _out(hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05"}
        ))

        assert out["status"] == "UNAVAILABLE"

    def test_nonzero_exit_code_maps_to_unavailable(self, monkeypatch):
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(b"", returncode=1, stderr=b"boom"))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "UNAVAILABLE"
        assert "1" in out["error"]

    def test_non_json_stdout_maps_to_invalid_response(self, monkeypatch):
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(b"not json at all"))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "INVALID_RESPONSE"

    def test_noisy_stdout_still_parsed(self, monkeypatch):
        """CLI 偶尔会先打一行提示；宽容解析必须扛住。"""
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(b"\xe6\x8f\x90\xe7\xa4\xba: \xe6\xad\xa3\xe5\x9c\xa8\xe6\x9f\xa5\xe8\xaf\xa2\n" + _envelope(FLIGHT_RESULT)))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "OK"

    def test_key_leaked_into_stdout_is_redacted(self, monkeypatch):
        """CLI 万一把 key 回显出来，返回值里也必须是掩码。"""
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(f"warning: key={FAKE_KEY}\n".encode() + _envelope(FLIGHT_RESULT)))

        raw = flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        )

        assert FAKE_KEY not in raw
        assert _out(raw)["status"] == "OK"

    def test_empty_list_maps_to_empty(self, monkeypatch):
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(_envelope({"data": [], "successCode": True})))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "EMPTY"
        assert out["count"] == 0
        assert out["notes"]

    def test_unexpected_shape_reports_actual_keys(self, monkeypatch):
        """形状变了要能自证：说明里带上实际键，方便定位。"""
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(_envelope({"unexpected": 1})))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["status"] == "EMPTY"
        assert "unexpected" in out["notes"][0]


# ==================================================
# 返回解析 / 归一化
# ==================================================

class TestParsing:
    def test_envelope_contract(self, monkeypatch):
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(_envelope(FLIGHT_RESULT)))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert set(out) >= {"status", "provider", "fetched_at", "query", "count", "items", "error"}
        assert out["provider"] == "tuniu"
        assert out["count"] == 1
        assert out["error"] is None
        assert out["query"] == {
            "departureCityName": "北京",
            "arrivalCityName": "成都",
            "departureDate": "2026-10-01",
        }
        # 带时区的 ISO8601
        assert datetime.fromisoformat(out["fetched_at"]).tzinfo is not None

    def test_flight_normalization(self, monkeypatch):
        flight = _load_module("flight")
        _install(monkeypatch, FakeCLI(_envelope(FLIGHT_RESULT)))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))
        item = out["items"][0]

        assert item["flight_no"] == "CA4110"
        assert item["airline"] == "国航"
        assert item["departure_airport"] == "首都T3"
        assert item["arrival_airport"] == "双流T2"
        assert item["departure_time"] == "2026-10-01 19:30"
        assert item["duration_minutes"] == 180
        assert item["remaining_seats"] == 4
        assert item["is_direct"] is True
        assert item["currency"] == "CNY"
        # basePrice + totalTax
        assert item["price"] == 1290.0
        assert item["price_breakdown"] == {"base": 1170.0, "tax": 120.0}

    def test_structured_content_wins_over_content_text(self, monkeypatch):
        """两条路径都在时优先 structuredContent.result。"""
        flight = _load_module("flight")
        payload = {
            "success": True,
            "result": {
                "content": [{"type": "text", "text": json.dumps({"data": [{"flightNumber": "WRONG"}]}, ensure_ascii=False)}],
                "structuredContent": {"result": json.dumps(FLIGHT_RESULT, ensure_ascii=False)},
                "isError": False,
            },
        }
        _install(monkeypatch, FakeCLI(json.dumps(payload, ensure_ascii=False).encode()))

        out = _out(flight.tuniu_search_flights.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))

        assert out["items"][0]["flight_no"] == "CA4110"

    def test_hotel_falls_back_to_content_text(self, monkeypatch):
        """hotel 只填 content[0].text，没有 structuredContent —— 必须能兜住。"""
        hotel = _load_module("hotel")
        _install(monkeypatch, FakeCLI(_envelope(HOTEL_RESULT, structured=False)))

        out = _out(hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05"}
        ))
        item = out["items"][0]

        assert out["status"] == "OK"
        assert item["hotel_id"] == 1912107132
        assert item["name"].startswith("丽橙酒店")
        assert item["business_area"] == "顺城大街/天府广场"
        assert item["rating"] == 4.8
        assert item["price_per_night"] == 600.0
        # lowestPrice 是起价，必须标注单位
        assert item["price_unit"] == "元起/晚"
        assert item["room_type"] == "智悦大床房+双早"
        assert item["meal"] == "无早餐"
        assert item["cancellation_policy"] == "限时取消"

    def test_hotel_pagination_extras(self, monkeypatch):
        hotel = _load_module("hotel")
        _install(monkeypatch, FakeCLI(_envelope(HOTEL_RESULT, structured=False)))

        out = _out(hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05"}
        ))

        assert out["query_id"] == "qid-001"
        assert out["total_page_num"] == 2
        assert out["current_page_num"] == 1

    def test_hotel_paging_args_only_sent_when_used(self, monkeypatch):
        hotel = _load_module("hotel")
        fake = _install(monkeypatch, FakeCLI(_envelope(HOTEL_RESULT, structured=False)))

        hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05",
             "page_num": 2, "query_id": "qid-001"}
        )
        assert json.loads(fake.argv[5]) == {
            "cityName": "成都", "checkIn": "2026-10-01", "checkOut": "2026-10-05",
            "pageNum": 2, "queryId": "qid-001",
        }

        hotel.tuniu_search_hotels.invoke(
            {"city": "成都", "check_in": "2026-10-01", "check_out": "2026-10-05"}
        )
        assert json.loads(fake.argv[5]) == {
            "cityName": "成都", "checkIn": "2026-10-01", "checkOut": "2026-10-05",
        }

    def test_train_normalization(self, monkeypatch):
        train = _load_module("train")
        _install(monkeypatch, FakeCLI(_envelope(TRAIN_RESULT)))

        out = _out(train.tuniu_search_trains.invoke(
            {"departure_city": "北京", "arrival_city": "成都", "departure_date": "2026-10-01"}
        ))
        item = out["items"][0]

        assert item["train_no"] == "K117"
        assert item["origin_station"] == "北京西"
        assert item["destination_station"] == "成都西"
        assert item["departure_at"] == "2026-10-01 11:54"
        assert item["arrival_at"] == "2026-10-02 19:16"
        # "31时22分"
        assert item["duration_minutes"] == 31 * 60 + 22
        # 空串（无此席别）不能变成 0 元
        assert item["prices"] == {"软卧": 703.0, "无座": 251.0, "硬卧": 456.0, "硬座": 251.0}
        assert "二等座" not in item["prices"]
        assert "商务座" not in item["prices"]
        assert item["price_range"] == {"min": 251.0, "max": 703.0}
        # 0 张余票是有效信息，null 才是"不适用"
        assert item["seats_available"] == {"软卧": 1, "无座": 0, "硬卧": 99, "硬座": 12}

    def test_unknown_shape_keeps_raw_fields(self, monkeypatch):
        """未实测的接口：别名映射 + 原始字段一并返回（不猜、不丢）。"""
        cruise = _load_module("cruise")
        result = {
            "cruises": [
                {
                    "cruiseName": "地中海号",
                    "shipName": "地中海号",
                    "minPrice": "3999",
                    "departsDate": "2026-10-18",
                    "vendorOnlyField": "keep-me",
                }
            ]
        }
        _install(monkeypatch, FakeCLI(_envelope(result)))

        out = _out(cruise.tuniu_search_cruises.invoke(
            {"departs_date_begin": "2026-10-17", "departs_date_end": "2026-10-20"}
        ))
        item = out["items"][0]

        assert out["status"] == "OK"
        assert item["name"] == "地中海号"
        assert item["price"] == "3999"
        assert item["departure_date"] == "2026-10-18"
        assert item["vendorOnlyField"] == "keep-me"

    def test_holiday_payload_shape(self, monkeypatch):
        holiday = _load_module("holiday")
        fake = _install(monkeypatch, FakeCLI(_envelope({"data": [{"productName": "三亚 5 日游"}]})))

        out = _out(holiday.tuniu_search_holiday_packages.invoke(
            {"destination": "三亚", "departs_date_begin": "2026-10-10", "departs_date_end": "2026-10-15"}
        ))

        assert json.loads(fake.argv[5]) == {
            "destinationName": "三亚",
            "departsDateBegin": "2026-10-10",
            "departsDateEnd": "2026-10-15",
        }
        assert out["items"][0]["name"] == "三亚 5 日游"

    def test_ticket_uses_scenic_name_argument(self, monkeypatch):
        ticket = _load_module("ticket")
        fake = _install(monkeypatch, FakeCLI(_envelope({"tickets": [{"scenicName": "武侯祠", "cheapestPrice": "50"}]})))

        out = _out(ticket.tuniu_search_scenic_tickets.invoke({"scenic_name": "武侯祠"}))

        # 参数名是 scenic_name，不是 city
        assert json.loads(fake.argv[5]) == {"scenic_name": "武侯祠"}
        assert out["items"][0]["name"] == "武侯祠"
        assert out["items"][0]["price"] == "50"


# ==================================================
# duration / 数值解析
# ==================================================

class TestHelpers:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("3h", 180),
            ("3H", 180),
            ("2h50m", 170),
            ("31时22分", 31 * 60 + 22),
            ("1小时30分钟", 90),
            ("45分", 45),
            ("45min", 45),
            ("1:30", 90),
            ("120", 120),
            (120, 120),
            ("", None),
            (None, None),
            ("待定", None),
        ],
    )
    def test_parse_duration(self, raw, expected):
        flight = _load_module("flight")
        assert flight._parse_duration_minutes(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("1170", 1170.0),
            ("703.0", 703.0),
            (600, 600.0),
            ("", None),
            (None, None),
            ("暂无", None),
        ],
    )
    def test_to_float(self, raw, expected):
        flight = _load_module("flight")
        assert flight._to_float(raw) == expected

    def test_to_int_handles_bool(self):
        flight = _load_module("flight")
        assert flight._to_int("4") == 4
        assert flight._to_int(True) is None
        assert flight._to_int("") is None

    def test_flight_without_prices_gives_none(self):
        flight = _load_module("flight")
        item = flight._normalize_item({"flightNumber": "XX1", "basePrice": "", "totalTax": ""})
        assert item["price"] is None
        assert item["price_breakdown"] == {"base": None, "tax": None}

    def test_train_without_valid_prices_gives_none_range(self):
        train = _load_module("train")
        item = train._normalize_item({"trainNum": "G1", "price": {"edzPrice": ""}})
        assert item["prices"] == {}
        assert item["price_range"] is None


# ==================================================
# 只读边界 / Manifest
# ==================================================

class TestSafetyBoundary:
    def test_exactly_six_read_only_tools(self):
        names = set()
        for name in MODULE_NAMES:
            module = _load_module(name)
            tools = module.TOOLS
            assert isinstance(tools, list) and len(tools) == 1
            names.update(tool.name for tool in tools)

        assert names == EXPECTED_TOOL_NAMES

    @pytest.mark.parametrize("module_name", MODULE_NAMES)
    def test_no_write_op_in_tool_name(self, module_name):
        module = _load_module(module_name)
        for tool in module.TOOLS:
            lowered = tool.name.lower()
            for op in FORBIDDEN_WRITE_OPS:
                assert op.lower() not in lowered

    @pytest.mark.parametrize("module_name", MODULE_NAMES)
    def test_cli_tool_constant_is_a_query(self, module_name):
        """真正发给 CLI 的 tool 名不能是写操作 —— 这是唯一能真的下单的参数。"""
        module = _load_module(module_name)
        assert module.SERVER in ALLOWED_SERVERS
        assert module.TOOL_NAME not in FORBIDDEN_WRITE_OPS
        # 只读接口的命名特征是 search / query（hotel 是 tuniuHotelSearch）
        assert any(word in module.TOOL_NAME.lower() for word in ("search", "query"))
        for word in ("order", "book", "create", "cancel", "save"):
            assert word not in module.TOOL_NAME.lower()

    @pytest.mark.parametrize("module_name", MODULE_NAMES)
    def test_tool_has_chinese_docstring(self, module_name):
        module = _load_module(module_name)
        for tool in module.TOOLS:
            assert tool.description
            assert "Args:" in tool.description

    def test_manifest_declares_six_modules(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))

        assert manifest["plugin"]["name"] == "tuniu_travel"
        assert manifest["plugin"]["enabled"] is True
        assert manifest["plugin"]["version"] == "0.1.0"

        modules = manifest["tools"]["modules"]
        assert modules == [f"tools.{name}" for name in MODULE_NAMES]
        for dotted in modules:
            assert (PLUGIN_DIR / Path(*dotted.split(".")).with_suffix(".py")).exists()

        # 没有 plugin.py 也必须能加载（loader 在声明了 modules 时允许省略 entrypoint）
        assert not (PLUGIN_DIR / "plugin.py").exists()

    def test_manifest_description_reads_like_user_speech(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
        description = manifest["plugin"]["description"]

        # 会被 BM25 拿去和用户 Query 匹配，所以要含用户会说的词
        for keyword in ("机票", "酒店", "火车票", "门票", "邮轮", "跟团游"):
            assert keyword in description
        assert "只查询" in description
