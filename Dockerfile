# TravelPlan API 容器镜像。
#
# 为什么需要它：Vercel 的 Serverless 环境跑不了这个 API 的全部能力（各条限制见
# docs/09_部署说明.md）。规划一次要串几十个真实 Provider 调用、要写本地 SQLite、
# 还要拉起 `npx 12306-mcp` 这个 stdio 子进程 —— 这三件事都要求"常驻进程 + 可写磁盘"。
# 所以 Vercel 承载前端（以及可选的读接口），规划 Worker 用这个镜像跑在
# Railway / Fly.io / Render / 任意 VPS 上。
#
# 基础镜像里同时装 Node：12306 走 MCP stdio（`npx -y 12306-mcp`），没有 Node 就没有主源。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRAVELPLAN_OUTPUT_DIR=/data/outputs \
    TRAVELPLAN_DB_PATH=/data/travelplan.db

# nodejs/npm 供 MCP server 使用；ca-certificates 供 HTTPS Provider 使用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

WORKDIR /srv

# 先装依赖再拷业务代码：依赖没变时这一层可以复用，构建快得多。
# requirements.txt 里含 `-e ./super_harness[mcp]`，所以 submodule 必须先到位。
COPY super_harness/ ./super_harness/
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app/ ./app/
COPY main.py ./

# 运行时数据（SQLite 与 outputs/）都落在 /data，挂一个卷就能持久化。
RUN mkdir -p /data/outputs
VOLUME ["/data"]

EXPOSE 8000
# 单 worker：SQLite 与进程内 Run 执行器都是单机语义，多 worker 会让轮询读到别的实例的库。
# 需要并发就多开容器实例 + 外部数据库，而不是在同一个容器里加 worker。
CMD ["uvicorn", "app.api:api", "--host", "0.0.0.0", "--port", "8000"]
