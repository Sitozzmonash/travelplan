"""代码版本信息。

Trace、Bad Case、Benchmark Baseline 都必须能回答"这条结论是哪一版代码跑出来的"。
否则一份两周前的 Bad Case 无法判断现在还成不成立 —— 这是回归集能被信任的前提。

优先读环境变量（部署环境里没有 .git），退回本地 git。
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_commit(repo: Path) -> str | None:
    """取短 commit。取不到就返回 None —— 宁可写 unknown，也不要编一个版本号。"""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


@lru_cache(maxsize=8)
def git_commit(repo_path: str) -> str | None:
    return _git_commit(Path(repo_path))


def travelplan_commit() -> str | None:
    return os.environ.get("TRAVELPLAN_COMMIT") or git_commit(str(REPO_ROOT))


def superharness_commit() -> str | None:
    return os.environ.get("SUPERHARNESS_COMMIT") or git_commit(str(REPO_ROOT / "super_harness"))


def model_name() -> str:
    return os.environ.get("MODEL_NAME") or "unset"


def fixture_version() -> str:
    """Fixture 版本。改了 cases/fixtures 就要改它，否则两份 baseline 不可比。"""

    return os.environ.get("TRAVELPLAN_FIXTURE_VERSION") or "fixtures.v1"
