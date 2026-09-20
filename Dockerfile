# TravelPlan API 容器镜像（生产架构里的「Render 常驻容器」）。
#
# 为什么后端必须是常驻容器：一次真实规划要串几十个真实 Provider 调用、耗时数分钟。
# `POST /api/v1/plans {background:true}` 只负责创建 run 并**立刻返回 202**，真正的执行
# 继续留在**同一个进程**的后台线程里，前端轮询 `/status` 拿最终状态。Vercel 的
# Serverless Function 在响应返回后就会被冻结/回收，承载不了「HTTP 已返回、任务还在跑」，
# 所以前端在 Vercel、后端用这个镜像跑在 Render（Railway / Fly.io / 任意 VPS 同样可用）。
#
# 数据落在 Neon PostgreSQL（`DATABASE_URL`）；容器里的 `/data` 只是**临时产物目录**
# （outputs/、以及未配置 DATABASE_URL 时的本地 SQLite），Render 免费层没有持久盘也不影响
# 正确性 —— 只要配好 DATABASE_URL，重启丢的只是产物文件，业务数据都在 Neon 上。
#
# 基础镜像里同时装 Node：两个 Provider 都需要它 ——
#   1) 12306 走 MCP stdio（`npx -y 12306-mcp`），没有 Node 就没有铁路主源；
#   2) 途牛 CLI（公共 npm 包 `tuniu-cli`）提供机票/酒店/火车/门票备用源，
#      不装的话容器里所有途牛调用都会 UNAVAILABLE（FileNotFoundError），
#      线上等于只剩 12306 一条腿（2026-09-20 在 Render 上实测到过这个现象）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TRAVELPLAN_OUTPUT_DIR=/data/outputs \
    TRAVELPLAN_DB_PATH=/data/travelplan.db

# nodejs/npm 供 MCP server 与途牛 CLI 使用；ca-certificates 供 HTTPS Provider 使用。
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# 途牛 CLI 固定版本装进镜像（而不是运行时 npx）：它每次 Run 都要被调用多次，
# 运行时现装会给每次冷启动加十几秒，而且构建期失败会直接让部署失败（比线上才发现好）。
# superharness 默认从 PATH 找 `tuniu`，也可以用 TUNIU_COMMAND 覆盖。
ARG TUNIU_CLI_VERSION=1.1.4
RUN npm install -g "tuniu-cli@${TUNIU_CLI_VERSION}" && tuniu --version

WORKDIR /srv

# 先装依赖再拷业务代码：依赖没变时这一层可以复用，构建快得多。
# requirements.txt 里含 `-e ./super_harness[mcp]`，所以 submodule 必须先到位。
COPY super_harness/ ./super_harness/
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app/ ./app/
COPY main.py ./

# 运行时数据（SQLite 与 outputs/）都落在 /data；自建 docker run -v 挂卷即可持久化。
# Render 会忽略 VOLUME 指令（免费层也没有持久盘），所以这里**不依赖**它必须被挂载：
# 只要配了 DATABASE_URL，业务数据就在 Neon 上，/data 只是临时产物目录。
RUN mkdir -p /data/outputs
VOLUME ["/data"]

# Render / Railway 之类的平台通过 PORT 注入监听端口；本地 docker run 不设置时回落 8000。
EXPOSE 8000
# 单 worker：进程内 Run 执行器（后台线程）与轮询状态都是单实例语义，多 worker 会让
# 轮询打到没跑任务的实例上。需要并发就多开容器实例 + 外部数据库，而不是加 worker。
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.api:api --host 0.0.0.0 --port ${PORT:-8000}"]
