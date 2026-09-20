"""本地起服务：强制走本地 SQLite，不碰 Neon。

为什么需要它：`.env` 里一旦配了 `DATABASE_URL`（生产用），`python -m uvicorn app.api:api`
就会**连到线上库**去建表、写 run。后果有两个，都很糟：

  1. 本地随手起的测试 run 会写进生产数据；
  2. 每次请求都要绕一圈 Neon（实测 `/health` 要 20s+），看起来像"后端挂了"。

所以本地开发统一用这个入口。它只做一件事：在导入 app 之前清掉 Postgres 相关变量，
其余参数与直接跑 uvicorn 完全一致。

用法：
    python scripts/serve_local.py                      # 127.0.0.1:8013
    python scripts/serve_local.py --port 8015
    python scripts/serve_local.py --db D:/tmp/dev.db   # 另起一个隔离库
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 会让 store 走 Postgres 的变量。在 import app 之前清掉，避免"本地跑却连线上"。
POSTGRES_ENV = ("DATABASE_URL", "DATABASE_URL_UNPOOLED", "PGHOST", "PGDATABASE", "PGUSER")


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 SQLite 服务入口（不连 Neon）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--db", help="SQLite 路径；不传则用 app 默认（data/travelplan.db）")
    parser.add_argument("--output-dir", help="run 产物目录；不传则用 app 默认")
    args = parser.parse_args()

    for name in POSTGRES_ENV:
        os.environ.pop(name, None)
    if args.db:
        os.environ["TRAVELPLAN_DB_PATH"] = args.db
    if args.output_dir:
        os.environ["TRAVELPLAN_OUTPUT_DIR"] = args.output_dir

    # 顺序很关键，反了就没用：
    #   1) 先导入 app.providers —— 它会经 super_harness 执行 load_dotenv()，把仓库根目录
    #      的 .env（含 DATABASE_URL）**重新灌进 os.environ**；
    #   2) 再清 Postgres 变量；
    #   3) 最后才导入 app.api —— store 与运行时配置都在这一步解析，此时环境已经是干净的。
    # "先清后导"是无效的：清完又被 load_dotenv 填回来了（实测 /health 仍然报 postgres）。
    import uvicorn

    import app.providers  # noqa: F401 —— 触发 load_dotenv，必须早于下面的清理

    for name in POSTGRES_ENV:
        os.environ.pop(name, None)

    from app.config import database_url
    from app.api import api

    resolved = database_url()
    if resolved:
        print(f"[serve_local] 仍然解析到 Postgres（{resolved[:24]}…），已中止以避免写线上库")
        return 2
    print(f"[serve_local] SQLite 模式：{os.environ.get('TRAVELPLAN_DB_PATH') or 'data/travelplan.db'}")
    uvicorn.run(api, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
