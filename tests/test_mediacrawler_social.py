"""mediacrawler_social Plugin 的单元测试。

**不联网、不起真进程**：所有抓取都通过替换模块里的 ``_spawn`` 来完成（它是模块里
唯一落地到子进程的函数）。产物文件用 tmp_path 造假目录 + 假 json/jsonl 造出来。

测试的核心不是"能抓到数据"（那要真登录、真风控，无法在单测里稳定复现），而是：
    * 目录 / 入口缺失时给出**可操作**的 UNAVAILABLE，而不是含糊的失败；
    * 超时映射成 TIMEOUT；
    * 成功路径按 MediaCrawler 的字段名正确归一化；
    * **任何失败路径的 items 都必须是空的** —— 这个 Plugin 绝不允许伪造攻略。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = (
    REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins" / "mediacrawler_social"
)
MODULE_PATH = PLUGIN_DIR / "tools" / "search.py"

EXPECTED_TOOL_NAMES = {
    "check_mediacrawler_available",
    "search_xhs_via_mediacrawler",
    "search_douyin_via_mediacrawler",
}

VENDOR_ENV = "MEDIACRAWLER_DIR"

#: 假的 MediaCrawler 产物（字段名按 NanmiCoder/MediaCrawler 的 xhs / dy 输出习惯写，
#: 这是"上游形状"，不是本仓库的字段）。
FAKE_XHS_RECORDS = [
    {
        "note_id": "66f0123456789abc000001",
        "title": "成都三天两夜攻略",
        "desc": "第一天宽窄巷子，第二天熊猫基地。",
        "nickname": "小吃货阿May",
        "user_id": "u1001",
        "liked_count": "2385",
        "collected_count": "815",
        "comment_count": "120",
        "note_url": "https://www.xiaohongshu.com/explore/66f0123456789abc000001",
        "time": 1694102400000,
    },
    {
        "note_id": "66f0123456789abc000002",
        "title": "成都避坑指南",
        "desc": "别在景区门口打车。",
        "nickname": "成都本地通",
        "user_id": "u1002",
        "liked_count": 900,
        "comment_count": 33,
        "time": 1694102400000,
    },
]

FAKE_DOUYIN_RECORDS = [
    {
        "aweme_id": "7300000000000000001",
        "desc": "成都美食合集｜本地人带路",
        "nickname": "成都吃喝玩乐",
        "uid": "123456",
        "liked_count": 12345,
        "comment_count": 678,
        "aweme_url": "https://www.douyin.com/video/7300000000000000001",
        "create_time": 1694102400,
    }
]


def _load_module():
    spec = importlib.util.spec_from_file_location("mediacrawler_social_search", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSpawn:
    """``_spawn`` 的替身：记录调用参数，返回预置结果或抛异常。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", raises=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, cmd, cwd, timeout):
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd), "timeout": timeout})

        if self.raises is not None:
            raise self.raises

        return subprocess.CompletedProcess(
            args=list(cmd), returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture
def module():
    return _load_module()


@pytest.fixture
def no_vendor_env(monkeypatch):
    """清掉环境变量，保证默认目录解析走的是仓库内路径。"""
    monkeypatch.delenv(VENDOR_ENV, raising=False)
    return monkeypatch


def _make_vendor(tmp_path: Path, *, with_entry: bool = True, with_data: bool = True) -> Path:
    """造一个假的 MediaCrawler 目录。"""
    vendor = tmp_path / "MediaCrawler"
    vendor.mkdir(parents=True, exist_ok=True)

    if with_entry:
        (vendor / "main.py").write_text("# fake MediaCrawler entry\n", encoding="utf-8")
    if with_data:
        (vendor / "data").mkdir(exist_ok=True)

    return vendor


def _write_jsonl(vendor: Path, name: str, records: list[dict]) -> Path:
    data_dir = vendor / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / name
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _write_json(vendor: Path, name: str, payload) -> Path:
    data_dir = vendor / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ==================================================
# 不可用：目录 / 入口
# ==================================================

class TestUnavailable:
    def test_missing_directory_returns_unavailable_with_clone_hint(self, module, monkeypatch, tmp_path, no_vendor_env):
        missing = tmp_path / "not-cloned-yet"
        monkeypatch.setenv(VENDOR_ENV, str(missing))

        fake = FakeSpawn()
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert envelope["count"] == 0
        assert envelope["error"] is not None
        assert "NanmiCoder/MediaCrawler" in envelope["error"]
        assert "data/vendors/MediaCrawler" in envelope["error"] or "data\\vendors\\MediaCrawler" in envelope["error"]
        assert VENDOR_ENV in envelope["error"]
        assert str(missing) in envelope["vendor"]["dir"]
        assert envelope["vendor"]["entry"] is None
        assert fake.calls == []  # 目录都没有，绝不启动进程

    def test_directory_present_but_entry_missing(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path, with_entry=False)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))

        fake = FakeSpawn()
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_douyin_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert "入口" in envelope["error"]
        assert "main.py" in envelope["error"]
        assert envelope["vendor"]["entry"] is None
        assert fake.calls == []

    def test_default_directory_is_repo_data_vendors_and_currently_missing(self, module, no_vendor_env):
        """默认路径指向仓库内的 data/vendors/MediaCrawler —— 实测该目录当前不存在。"""
        envelope = json.loads(module.check_mediacrawler_available.invoke({}))

        expected = (REPO_ROOT / "data" / "vendors" / "MediaCrawler").resolve()
        assert envelope["status"] == "UNAVAILABLE"
        assert Path(envelope["vendor"]["dir"]) == expected
        assert not expected.is_dir()  # 如实确认：仓库里没有 vendor 进来

    def test_probe_reports_ok_when_directory_and_entry_exist(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))

        fake = FakeSpawn()
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.check_mediacrawler_available.invoke({}))

        assert envelope["status"] == "OK"
        assert envelope["vendor"]["entry"] == "main.py"
        assert fake.calls == []  # 探针永远不启动子进程


# ==================================================
# 超时 / 进程失败
# ==================================================

class TestProcessFailures:
    def test_timeout_maps_to_timeout_status(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))

        fake = FakeSpawn(raises=subprocess.TimeoutExpired(cmd="main.py", timeout=5))
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食", "timeout": 5}))

        assert envelope["status"] == "TIMEOUT"
        assert envelope["items"] == []
        assert envelope["count"] == 0
        assert "5s" in envelope["error"]
        assert "登录" in envelope["error"]  # 给出最可能的原因
        assert len(fake.calls) == 1  # 超时不重试
        assert fake.calls[0]["timeout"] == 5.0

    def test_nonzero_exit_returns_unavailable_and_no_items(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=2, stdout="", stderr="Traceback: boom"))

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert "2" in envelope["error"]
        assert "boom" in envelope["error"]

    def test_login_required_output_gives_actionable_reason(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        monkeypatch.setattr(
            module, "_spawn", FakeSpawn(returncode=1, stderr="请使用手机扫码登录，扫码后按回车")
        )

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert "登录" in envelope["error"]
        assert "扫码" in envelope["error"]

    def test_risk_control_output_gives_actionable_reason(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        monkeypatch.setattr(
            module, "_spawn", FakeSpawn(returncode=1, stderr="检测到账号异常，触发风控，请完成验证码")
        )

        envelope = json.loads(module.search_douyin_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert "风控" in envelope["error"]

    def test_exit_zero_without_new_artifacts_is_unavailable(self, module, monkeypatch, tmp_path, no_vendor_env):
        """退出码 0 但没写出产物：可能是被静默拦截 —— 如实报告，不猜内容。"""
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0, stdout="done"))

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert "json" in envelope["error"]

    def test_unparsable_artifact_is_invalid_response(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        (vendor / "data" / "xhs_result.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "INVALID_RESPONSE"
        assert envelope["items"] == []

    def test_records_without_content_id_yield_empty_not_fabricated(self, module, monkeypatch, tmp_path, no_vendor_env):
        """产物里全是无法识别的记录 → EMPTY + items=[]，绝不补造攻略。"""
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        _write_jsonl(vendor, "xhs_search.jsonl", [{"foo": 1}, {"bar": "baz"}])
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "EMPTY"
        assert envelope["items"] == []
        assert envelope["count"] == 0


# ==================================================
# 成功路径：归一化
# ==================================================

class TestNormalization:
    def test_jsonl_artifact_is_normalized(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        result = _write_jsonl(vendor, "xhs_search_20260918.jsonl", FAKE_XHS_RECORDS)

        fake = FakeSpawn(returncode=0, stdout="crawled 2 notes")
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食攻略"}))

        assert envelope["status"] == "OK"
        assert envelope["count"] == 2
        assert envelope["provider"] == "mediacrawler"
        assert envelope["platform"] == "xhs"
        assert envelope["vendor"]["result_file"] == str(result)

        item = envelope["items"][0]
        assert item["platform"] == "xhs"
        assert item["content_id"] == "66f0123456789abc000001"
        assert item["title"] == "成都三天两夜攻略"
        assert item["desc"].startswith("第一天宽窄巷子")
        assert item["author"] == "小吃货阿May"
        assert item["author_id"] == "u1001"
        assert item["likes"] == 2385  # 上游是字符串 "2385"
        assert item["comments"] == 120
        assert item["collects"] == 815
        assert item["publish_time"].startswith("2023-09-07")
        assert item["url"].endswith("66f0123456789abc000001")
        assert item["source"] == "mediacrawler"
        assert isinstance(item["raw"], dict)

        # 第二条没给 note_url：用 note_id 拼出规范链接（改名，不是编内容）。
        assert envelope["items"][1]["url"] == "https://www.xiaohongshu.com/explore/66f0123456789abc000002"
        assert envelope["items"][1]["collects"] is None  # 没给就是 None，不填 0

        # 子进程参数：cwd 必须是 MediaCrawler 根目录（它按相对路径找配置与输出）
        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["cwd"] == vendor
        assert call["cmd"][0] == sys.executable
        assert call["cmd"][1] == str(vendor / "main.py")
        assert "--platform" in call["cmd"] and "xhs" in call["cmd"]
        assert "--type" in call["cmd"] and "search" in call["cmd"]
        assert "--keywords" in call["cmd"] and "成都美食攻略" in call["cmd"]

    def test_douyin_json_artifact_is_normalized(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        _write_json(vendor, "dy_search.json", FAKE_DOUYIN_RECORDS)

        fake = FakeSpawn(returncode=0)
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_douyin_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "OK"
        assert envelope["platform"] == "douyin"

        item = envelope["items"][0]
        assert item["content_id"] == "7300000000000000001"
        assert item["author"] == "成都吃喝玩乐"
        assert item["likes"] == 12345
        assert item["comments"] == 678
        assert item["url"].endswith("/video/7300000000000000001")
        assert item["publish_time"].startswith("2023-09-07")

        assert fake.calls[0]["cmd"][fake.calls[0]["cmd"].index("--platform") + 1] == "dy"

    def test_wrapped_json_payload_is_understood(self, module, monkeypatch, tmp_path, no_vendor_env):
        """上游有的版本写 {"data": [...]} 包一层，也要认。"""
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        _write_json(vendor, "xhs_search.json", {"data": FAKE_XHS_RECORDS})
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食"}))

        assert envelope["status"] == "OK"
        assert envelope["count"] == 2

    def test_limit_caps_returned_items(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        _write_jsonl(vendor, "xhs_search.jsonl", FAKE_XHS_RECORDS)
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(
            module.search_xhs_via_mediacrawler.invoke({"keyword": "成都美食", "limit": 1})
        )

        assert envelope["status"] == "OK"
        assert envelope["count"] == 1

    def test_explicit_result_file_is_used(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        result = _write_jsonl(vendor, "xhs_search.jsonl", FAKE_XHS_RECORDS)
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(
            module.search_xhs_via_mediacrawler.invoke(
                {"keyword": "成都美食", "result_file": "data/xhs_search.jsonl"}
            )
        )

        assert envelope["status"] == "OK"
        assert envelope["vendor"]["result_file"] == str(result)

    def test_missing_explicit_result_file_is_unavailable(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        monkeypatch.setattr(module, "_spawn", FakeSpawn(returncode=0))

        envelope = json.loads(
            module.search_xhs_via_mediacrawler.invoke(
                {"keyword": "成都美食", "result_file": "data/nope.jsonl"}
            )
        )

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []

    def test_blank_keyword_is_rejected_without_spawning(self, module, monkeypatch, tmp_path, no_vendor_env):
        vendor = _make_vendor(tmp_path)
        monkeypatch.setenv(VENDOR_ENV, str(vendor))
        fake = FakeSpawn(returncode=0)
        monkeypatch.setattr(module, "_spawn", fake)

        envelope = json.loads(module.search_xhs_via_mediacrawler.invoke({"keyword": "   "}))

        assert envelope["status"] == "UNAVAILABLE"
        assert envelope["items"] == []
        assert fake.calls == []


# ==================================================
# 定位与约束
# ==================================================

class TestPositioning:
    def test_module_documents_that_it_is_a_fallback_not_a_production_provider(self, module):
        doc = module.__doc__ or ""
        assert "fallback" in doc
        assert "不伪造" in doc

    def test_no_vendor_source_is_committed_into_the_repo(self):
        """MediaCrawler 源码不能被 vendor 进仓库（只允许调用 data/vendors 下的外部克隆）。"""
        assert not (REPO_ROOT / "data" / "vendors" / "MediaCrawler" / "main.py").exists()
        # Plugin 目录里只有清单与 tool 模块
        assert sorted(path.name for path in PLUGIN_DIR.iterdir()) == ["plugin.toml", "tools"]

    def test_tools_expose_expected_names(self, module):
        assert {tool.name for tool in module.TOOLS} == EXPECTED_TOOL_NAMES


# ==================================================
# Manifest / PluginLoader
# ==================================================

class TestManifestAndLoader:
    def test_manifest_declares_expected_module_and_no_entrypoint(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))

        assert manifest["plugin"]["name"] == "mediacrawler_social"
        assert manifest["plugin"]["enabled"] is True
        assert manifest["plugin"]["version"] == "0.1.0"
        assert manifest["tools"]["modules"] == ["tools.search"]
        assert (PLUGIN_DIR / "tools" / "search.py").is_file()
        assert not (PLUGIN_DIR / "plugin.py").exists()

    def test_manifest_description_reads_like_user_speech(self):
        manifest = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
        description = manifest["plugin"]["description"]

        for keyword in ("免费", "备用", "MediaCrawler", "小红书", "抖音", "data/vendors/MediaCrawler"):
            assert keyword in description, keyword
        assert "不会伪造" in description

    def test_plugin_loader_discovers_mediacrawler_social_and_loads_three_tools(self, monkeypatch):
        from superharness.capabilities import PluginLoader

        monkeypatch.delenv(VENDOR_ENV, raising=False)

        root = REPO_ROOT / "super_harness" / "superharness" / "capabilities" / "plugins"
        loader = PluginLoader(root)

        candidates = {candidate.name: candidate for candidate in loader.candidates()}
        assert "mediacrawler_social" in candidates
        assert candidates["mediacrawler_social"].id == "plugin:mediacrawler_social"

        manifest = next(item for item in loader.manifests if item.name == "mediacrawler_social")
        tools = loader.load_tools(manifest)

        assert {tool.name for tool in tools} == EXPECTED_TOOL_NAMES
