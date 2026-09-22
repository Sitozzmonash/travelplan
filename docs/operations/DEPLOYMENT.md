# 部署说明

一句话：架构是**固定**的 —— **Vercel（Next.js 前端）+ Render（FastAPI 常驻容器）+ Neon PostgreSQL**。
"把整个后端塞进 Vercel Function"这条路线本仓明确不走，原因见第 2 节（每条都对应一个代码事实）。

```text
Browser
  │
  ▼
Vercel        Next.js 前端（frontend/，NEXT_PUBLIC_API_BASE_URL 指向后端）
  │  HTTPS
  ▼
Render        FastAPI 常驻容器（仓库自带 Dockerfile，Runtime=Docker）
  │  DATABASE_URL（含 sslmode=require）
  ▼
Neon          PostgreSQL（project travelplan / branch production）
```

三件事各归各位：

| 层 | 承载 | 关键约束 |
| --- | --- | --- |
| Vercel | 前端页面、管理端 UI | 只是静态/SSR 前端，不跑长任务 |
| Render | `uvicorn app.api:api` 单 worker 常驻 | `POST` 返回 202 之后，后台线程**继续在同一个进程里跑** |
| Neon | 全部业务数据（run / trace / evidence / session / badcase…） | 容器重启不丢数据；本地盘只当临时产物目录 |

## 1. 为什么后端不能放在 Vercel 的 Serverless 上

以下每条都对应一个具体的代码事实，不是"性能担忧"。Vercel 的限制数字取自官方文档
（[Functions Limits](https://vercel.com/docs/functions/limitations)，最后更新 2026-08-24）。

| # | 代码事实 | 与 Vercel 的冲突 |
| --- | --- | --- |
| 1 | 一次真实规划要几分钟：**旧固定流程**优化前基线实测 616 秒（约 10.3 分钟；`_acceptance/perf_before.json`），优化后为 7~9 分钟；**主路径（Agent Loop）**的墙钟上限默认 `AGENT_RUN_TIMEOUT_SECONDS=600`，一次正常规划仍需分钟级 | 函数最长执行时间：Hobby **300s**、Pro 默认 300s / 上限 **800s**。Hobby 必然 504 |
| 2 | `background:true` 用**进程内** `ThreadPoolExecutor`（`app/api.py:_RUN_EXECUTOR`） | 响应返回后实例会被冻结/回收，后台 run 不保证继续跑 → 永远停在 `RUNNING` 的记录，而前端还在轮询 |
| 3 | 12306 走 MCP **stdio**（`npx -y 12306-mcp`） | 需要 Node 运行时 + 长驻子进程 + 首次 `npx` 下载；Vercel 的 Python 函数镜像里没有 Node，也不能长期持有子进程 |
| 4 | MediaCrawler 需要本地克隆目录 + 已登录浏览器会话 | 只读文件系统 + 无持久进程 + 无浏览器会话，**不可能** |
| 5 | 产物写 `outputs/<run_id>/`（六件文件），由 `/api/v1/plans/{id}/artifacts/...` 下载 | 函数文件系统只读；产物需要对象存储才能被下载接口读到 |
| 6 | 进程内状态：Run 执行器、健康检查用的 Postgres 连接池（`app/db.py:_ConnectionPool`） | 每个函数实例都是新进程，"进程内共享"等于没有共享 |

反过来，以下能力在 Vercel 上是没问题的（纯 HTTP 出站）：途牛、高德、TikHub、LLM
（以及旧固定流程遗留的 Jev），以及管理端的环境变量鉴权。所以前端放 Vercel 完全够用（第 6 节）。

## 2. Neon PostgreSQL

### 2.1 后端选择规则（`app/db.py: resolve_backend`）

```text
显式传 db_path（测试 / Benchmark）      → SQLite（临时库）
DATABASE_URL 非空                       → Postgres（Neon）
两者都没有                              → 本地 SQLite（TRAVELPLAN_DB_PATH 或 data/travelplan.db）
```

显式 `db_path` 优先级最高是有意的：测试与 Benchmark 永远传临时库，不会因为开发机 `.env`
里存在 Neon 连接串就把测试数据写到线上。

### 2.2 绝不静默回退

**配了 `DATABASE_URL` 却连不上时，进程直接抛 `PostgresUnavailable`，绝不退回 SQLite。**

为什么这条必须写成硬规则：一旦回退，线上会"看起来一切正常"，实际把数据写进了容器本地盘 ——
重启即全丢，而监控上什么异常都看不到。

| 场景 | 表现 |
| --- | --- |
| `GET /api/v1/health` | `status=degraded`、`database_connected=false`、`store.error` 里带 `PostgresUnavailable: 无法连接 postgres（Neon）：...` |
| `store.describe()` 仍能回答"本该是哪个后端" | `{"backend": "postgres", "host": "...", "target": "postgresql://***@host/db"}`（凭据永远脱敏） |
| `POST /api/v1/plans` | 显式 5xx（异常直接冒泡），不会"假装成功" |
| SQLite 文件 | **不会被创建** |

验收脚本 `scripts/verify_deployment.py no-fallback` 就是照这条规则写的：它在一个子进程里
用不可达的 `DATABASE_URL` 起真服务，然后断言上面四行，并检查 `TRAVELPLAN_DB_PATH` 指向的
SQLite 文件**不存在**。

另外 `app/db.py` 的连接池在**建连失败时会把预占的池名额还回去**：否则第 N+1 次调用会误判
"池子满了"，把"2 秒清晰报错"退化成"干等 15 秒超时"，而且 Neon 恢复后池子仍被幽灵名额占着
（脚本里连续 5 次健康检查的耗时断言就是防这个回归）。

### 2.3 建表与方言隔离

- `TravelPlanStore()` 构造时就调用 `init_schema()`（幂等 `CREATE TABLE IF NOT EXISTS`），
  所以第一次连上 Neon 就会把 25 张表建出来，不需要手工执行 SQL。
- SQLite 与 Postgres 的差异**只在 `app/db.py` 里**翻译：
  `?` → `%s`、`INSERT OR REPLACE` → `ON CONFLICT ... DO UPDATE`、`INTEGER PRIMARY KEY AUTOINCREMENT`
  → identity 列、`REAL` → `DOUBLE PRECISION`、`LIKE` → `ILIKE`（保留 SQLite 的检索手感）、
  自省走 `information_schema` 而不是 `PRAGMA`。`app/store.py` 的方法体一行都不用改，
  更不要在 store 里写 `if postgres: ... else: ...`。
- **DDL 里每一列都必须显式写类型**。SQLite 允许 `CREATE TABLE t (a, b REAL)` 这种省略类型的写法，
  Postgres 不允许——而且报错发生在 `IF NOT EXISTS` 判断**之前**（先解析再判断存在），
  所以"表已经建过"也救不了它。2026-09-21 踩过一次：`city_pois` 有 8 个列漏写类型，
  本地 SQLite 测试全绿、线上 Postgres 连库都连不上，所有读库接口 500。
  改完 DDL 要**在 Postgres 上真跑一遍**（可在 Neon 开临时分支验证完再删）再推。

### 2.4 Health 契约字段（实现见 `app/api.py` 的 health 端点）

```json
{
  "status": "ok",
  "database_backend": "postgres",
  "database_connected": true,
  "store": {"backend": "postgres", "host": "...", "target": "postgresql://***@host/db", "ok": true, "error": null}
}
```

- `database_backend`：`sqlite` / `postgres`，来自 `store.describe()`；
- `database_connected`：`init_schema()` 真的成功才为 `true`。**它不会因为退回本地 SQLite 而变 true**。

### 2.5 连接与冷启动注意

- 用 Neon 的**池化**连接串（host 里带 `-pooler`）；`app/db.py` 另外在进程内维持一个
  上限 4 条的共享连接池，避免每次操作都重新 TLS 握手。
- Neon 免费层 **scale to zero**：闲置后会休眠，下一次查询有几百毫秒的唤醒时间。
  对"一次规划几分钟"的场景无影响，但会让部署后的第一次 `/health` 稍慢。
- 不要把连接串写进任何配置文件或镜像层，也不要打印：本仓统一从环境变量 `DATABASE_URL` 读，
  且 `describe()` 只回显 `postgresql://***@host/db`。

## 3. Render（后端常驻容器）

### 3.1 用蓝图（推荐）

仓库根目录的 [`render.yaml`](../../render.yaml) 就是蓝图：Docker 运行时、免费层、`ohio` 区
（与 Neon `us-east-2` 同区）、健康检查 `/api/v1/health`、以及需要手填的 Secret 环境变量清单。

```text
Render 控制台 → New → Blueprint → 选 Sitozzmonash/travelplan → 填 sync:false 的那些值
```

### 3.2 用脚本（可复现，本文实测用的就是它）

[`scripts/render_deploy.py`](../../scripts/render_deploy.py) 走 Render REST API，凭证从
`~/.render/cli.yaml`（Render CLI 登录态）或 `RENDER_API_KEY` 读，**只进请求头、绝不打印**：

```bash
python scripts/render_deploy.py info                     # 看当前 workspace 有哪些服务（不碰别人的）
python scripts/render_deploy.py create --name travelplan-api --region ohio --plan free
python scripts/render_deploy.py env --service travelplan-api     # 把 .env 里的 Key/DATABASE_URL 推上去
python scripts/render_deploy.py deploy --service travelplan-api  # 触发部署并等待到 live
```

`env` 子命令**只打印 key 名**；`TRAVELPLAN_ADMIN_TOKEN` 不在 `.env` 里时会现场生成一个并
追加进 `.env`（该文件已被 `.gitignore`，不会进仓库）。

### 3.3 控制台手动建（等价做法）

```text
Type=Web Service / Runtime=Docker / Public Git repo=https://github.com/Sitozzmonash/travelplan
Branch=main / Dockerfile Path=./Dockerfile / Docker Context=.
Instance Type=Free（免费层没有持久盘）
Health Check Path=/api/v1/health
Region=Ohio（与 Neon 同区；其它区也能跑，只是每次查询多几十毫秒跨区延迟）
环境变量：见第 5 节
```

### 3.4 端口与"没有持久盘"

- 容器监听 `${PORT:-8000}`（`Dockerfile` 的 CMD）。Render 通过 `PORT` 注入端口，
  自建 `docker run` 不设 `PORT` 时回落到 8000。
- `Dockerfile` 里 `ENV TRAVELPLAN_DB_PATH=/data/travelplan.db`、`TRAVELPLAN_OUTPUT_DIR=/data/outputs`。
  Render **免费层没有持久盘**（`VOLUME` 指令在 Render 上被忽略），但这不是问题：
  - 业务数据在 **Neon**，重启不丢；
  - `/data` 只是**临时产物目录**，镜像构建时 `mkdir -p /data/outputs` 已经建好，
    即使不挂盘也能写（实测：部署后的 run 仍能产出并下载 `plan.json` / `audit_report.json`）；
  - 只有"未配置 `DATABASE_URL`"时，`/data/travelplan.db` 才会被真正用到 —— 生产环境不允许这种情况
    （健康检查会直接 degraded）。
- 单 worker：进程内 Run 执行器与轮询状态都是单实例语义。**不要加 `--workers`**，
  需要并发就多开服务实例 + 外部数据库。

## 4. 部署后怎么验证（可复现）

```bash
# 1) 健康与数据库
curl -s https://<你的服务>.onrender.com/api/v1/health
#    期望：status=ok、database_backend=postgres、database_connected=true

# 2) 后台 Run：202 → 轮询到终态（这是"常驻容器"的核心证据）
python scripts/verify_deployment.py render https://<你的服务>.onrender.com
#    脚本会：POST {background:true}（期望 202）→ 每 10s 轮询 /status →
#            直到 SUCCESS / DEGRADED / FAILED → 再拉 plan.json / audit_report.json

# 3) 容器内真的有 node（12306 MCP 依赖它）
render logs -r <srv-id> --type build --limit 500 --confirm -o text | grep -E "^.*v20|npm"
#    构建日志里能直接看到 Dockerfile 的 `node --version && npm --version`：实测 v20.19.2 / 9.2.0
#    运行时的证据在 run 里：12306 主源返回真实车次即说明 `npx -y 12306-mcp` 在这个容器里起来了
#    （免费层不允许一次性 job，`render jobs create` 会被拒：new paid services not allowed）
```

只有 `/api/v1/admin/**` 需要鉴权（`Authorization: Bearer $TRAVELPLAN_ADMIN_TOKEN`）；
`/health`、`/plans`、`/plans/{id}/status`、`/plans/{id}/artifacts/*` 无需鉴权，方便外部探活。

## 5. 环境变量清单

后端（Render，`sync:false` 的都是 Secret）：

| Key | 必填 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 生产必填 | Neon 连接串（含 `sslmode=require`）。配了它后端才走 Postgres |
| `MODEL_NAME` / `MODEL_BASE_URL` / `MODEL_API_KEY` | 是 | LLM 主干 |
| `MODEL_*_bk1` / `MODEL_*_bk2` | 建议 | 主模型的备用端点（三件一套：`MODEL_NAME_bk1` / `MODEL_BASE_URL_bk1` / `MODEL_API_KEY_bk1`，`_bk2` 同理）。主模型限流/欠费时按 主 → bk1 → bk2 降级；一项缺就整套跳过 |
| `MAX_AGENT_STEPS` / `AGENT_RUN_TIMEOUT_SECONDS` | 否 | 主规划 Agent 的步数与墙钟护栏（默认 40 / 600s），可用环境变量或管理端 Config 调 |
| `AMAP_API_KEY` | 是 | 地点验证 / 路线 |
| `TIKHUB_API_TOKEN` | 是 | 攻略主源（失败回退 MediaCrawler → Web Search） |
| `TUNIU_API_KEY` | 是 | 交通 / 酒店备用源 |
| `TAVILY_API_KEY` | 建议 | Web Search 兜底 |
| `JEV_API_KEY` | 否 | 只在直接跑旧固定流程时才需要；主规划（Agent Loop）不调用 Jev |
| `TRAVELPLAN_ADMIN_TOKEN` | 是 | 管理端 Bearer Token。**不配则 `/api/v1/admin/**` 一律 503**（安全默认） |
| `TRAVELPLAN_CORS_ORIGINS` | 前端上线后必填 | 逗号分隔的允许来源，如 `https://travelplan.vercel.app`；默认放行 `http://localhost:3000` 与 `http://127.0.0.1:3000` 两个来源（`app/api.py:DEFAULT_CORS_ORIGINS`） |
| `TRAVELPLAN_OUTPUT_DIR` | 否 | 默认 `/data/outputs`（容器内临时目录） |
| `TRAVELPLAN_DB_PATH` | 否 | 仅"没有 `DATABASE_URL`"时生效；默认 `/data/travelplan.db` |
| `TRAVELPLAN_DISABLE_MCP` | 否 | 置真时不注册任何 MCP Server（12306 走 `npx` 子进程，不需要密钥，只有这个开关能拦住它）。**生产不要设**；测试默认由 conftest 设为 1 |
| `TRAVELPLAN_SKIP_ORPHAN_SWEEP` | 否 | 置真时跳过"启动收敛孤儿 run"。**生产不要设**；测试默认由 conftest 设为 1（避免触发启动事件时写到开发库） |
| `RAILWAY_12306_COMMAND` / `TUNIU_COMMAND` / `MEDIACRAWLER_DIR` | 否 | 自定义外部命令/目录 |

前端（Vercel）：`NEXT_PUBLIC_API_BASE_URL`（编译期内联，改了要重新部署）；
可选 `NEXT_PUBLIC_USE_MOCK_API=false`。管理端 Token **不放这里**，在管理页面里填（存 localStorage）。
`TRAVELPLAN_ADMIN_TOKEN` 也不要配到前端托管平台：`frontend/app/admin/default-token/route.ts`
会把它原样返回给任何调用者（那条路由的本意只是本地开发免手输，本地值来自不入库的
`.env.local`；线上前端没有这个变量，端点返回空值，管理台回到手填）。

## 6. 前端托管

前端只依赖两个编译期变量（`NEXT_PUBLIC_API_BASE_URL`、`NEXT_PUBLIC_USE_MOCK_API`），
Vercel 与 EdgeOne Pages 共用同一份 `frontend/`。**两个托管都要能把后端域名内联进包里**，
这是唯一容易静默出错的地方（见 6.2）。

### 6.1 Vercel

```text
1) Vercel 新建项目 → 选这个 GitHub 仓库
2) Root Directory 设成 frontend/
3) 环境变量：NEXT_PUBLIC_API_BASE_URL = https://<你的 Render 域名>
4) 部署（Next.js 会被自动识别）
```

同域名的两种做法：

- **Vercel rewrites**：在 Vercel 项目里加 rewrite 把 `/api/*` 转发到容器域名，前端用同域相对路径
  （`NEXT_PUBLIC_API_BASE_URL=""`）。管理端用 `Authorization` 头，rewrite 不会丢头。
- **容器侧反代**：把 `frontend/` 构建产物打进容器，由容器（nginx / Caddy）统一对外。

跨域直连时记得配 `TRAVELPLAN_CORS_ORIGINS`（后端只放行白名单来源）。

### 6.2 EdgeOne Pages（国内可访问）

边缘节点在国内的可访问性更好，但**后端搬不过去**：EdgeOne Cloud Functions 单请求
"默认 30 秒，可配置至 120 秒"，而一次真实规划要 7~9 分钟，且 `POST /plans {background:true}`
返回 202 之后还要靠**同一个进程**里的后台线程继续跑——函数实例在响应返回后会被冻结，
"HTTP 已返回、任务还在跑"这条链路不成立。所以架构仍然是
**EdgeOne Pages（前端）+ Render（FastAPI 常驻容器）+ Neon**。

配置在 `frontend/edgeone.json`：

```json
{
  "installCommand": "npm install",
  "buildCommand": "NEXT_PUBLIC_API_BASE_URL=https://travelplan-api.onrender.com NEXT_PUBLIC_USE_MOCK_API=false npm run build",
  "outputDirectory": ".next",
  "nodeVersion": "22"
}
```

**为什么把变量写进 `buildCommand` 而不是只放 `.env.production`**：EdgeOne 上传源码时会按过滤
规则丢掉 `.env*`。实测第一次部署时构建期没拿到这个变量，线上包里的代码**原样保留**了
`process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000"`，浏览器里取到 `undefined`，
于是前端实际指向 localhost——站点能打开、资源全 200，**看不出来坏**。写进 `buildCommand`
是唯一不依赖平台文件过滤策略的做法。`.env.production` 仍然保留（本地构建与 Git 导入方式用得上）。

部署：

```bash
npm install -g edgeone
edgeone login                                        # 浏览器授权一次（国际站在用 GitHub/邮箱登录）
edgeone makers deploy frontend --name travelplan-web  # 注意排除 node_modules/.next
```

`--anonymous` 可以免登录先部署，拿到临时域名 + claim 链接；**必须在链接给出的期限前 claim**，
否则项目会被平台删除。匿名部署期间 `edgeone.json` 与源码会被上传，**不要把任何 Secret 放进
`frontend/`**（前端本来也不该有）。

**换域名必须同步 CORS**：`TRAVELPLAN_CORS_ORIGINS` 是严格白名单，新域名不加进去，浏览器侧
直接报 `TypeError: Failed to fetch`（不是 500，很容易误判成后端挂了）。改完要重新部署后端。

部署后三项都要验，**第 2 项最容易漏**：

```bash
# 1) 站点本身
curl -s -o /dev/null -w "%{http_code}\n" https://<域名>/
# 2) 线上包里的后端地址确实被内联（不是 localhost）
curl -s https://<域名>/ | grep -oE '/_next/static/chunks/[^"\\]+\.js' | sort -u \
  | while read -r f; do curl -s "https://<域名>$f"; done | grep -o "travelplan-api.onrender.com" | head -1
# 3) 预检放行（把 <域名> 换成本次部署的域名）
curl -s -i -X OPTIONS https://travelplan-api.onrender.com/api/v1/plans \
  -H "Origin: https://<域名>" -H "Access-Control-Request-Method: POST" | grep -i access-control-allow-origin
```

最可靠的验收还是**在浏览器里打开站点发一次真实请求**：跨域、内联地址、后端健康三项会一起暴露。

2026-09-21 部署实例：**`https://travelplan-web.edgeone.cool`**（项目名 `travelplan-web`，
Project ID `makers-dqiuwptx7qmf`，global 区，由 `edgeone makers deploy frontend --name travelplan-web` 部署）。

> **踩过的坑（同日）**：更早一次的 `https://travelplan-web2-tf8jdorj.edgeone.dev` 在 2026-09-21 17:43
> 被平台删除，访问返回 `404 NOT_FOUND / The site does not exist.`。匿名部署（`--anonymous`）若未在
> 期限内 claim，项目会被平台回收 —— **匿名部署只能当临时预览，不要当正式环境**。
> 重新部署必须**已登录**（`edgeone login`），否则项目仍是一次性的。
> 换域名后 `TRAVELPLAN_CORS_ORIGINS` 要同步改「本地 `.env` + Render 线上环境变量」两处，漏了就是 `Failed to fetch`。


## 7. Render 免费层实测特性（观察到的）

| 现象 | 实测 |
| --- | --- |
| 休眠 | 免费 Web Service 闲置 **15 分钟**后 spin down；下一次请求触发冷启动 |
| 冷启动 | 本轮**没有实测到冷启动时长**（只观察到"明显慢于常驻时的 `/health`"）；容器要重新起进程并加载 LangGraph / Provider 装配，所以首次 `/health` 会明显滞后 |
| 轮询能否保持唤醒 | 能：轮询期间有持续入站流量，实例不会被判定闲置（实测轮询全程 `/status` 稳定返回）。**但"保持唤醒"不等于"能跑完"** —— 见下面的 512Mi 上限：真实 run 被 OOM kill 过多次，且倒下的阶段不止一个（`search_social_guides` 与 `extract_and_normalize_places` 都发生过） |
| 持久盘 | 无。`/data` 随实例回收而丢失，所以数据必须落 Neon（本架构已经如此） |
| 一次性 job（`render jobs create`） | 免费层拒绝：`400 new paid services not allowed` ⇒ 容器内 `node --version` 只能用构建日志 + 运行时行为证据 |
| 构建耗时 | Docker 全量构建（apt 装 Node + pip 装 LangGraph 全家桶）约 **5 分钟**；没有持久盘所以每次部署都重建 |
| **内存上限** | **512Mi，会被 OOM kill**。实测 2026-09-20 15:30:49 `server_failed: oomKilled {memoryLimit: 512Mi}`，当时正跑一个 2 天行程的 run（`search_social_guides` 阶段，旧固定流程的阶段名）。Render 随后 15:31:17 `server_available`，**进程内 Run 执行器随之消失**，那条 run 就永远停在 `RUNNING`。主路径换成 Agent Loop 后阶段名不再固定，但"同进程后台线程 + 512Mi"这个组合没变，内存风险依旧 |
| 健康检查 | 5 秒超时。`/health` 若在探针里跑建表（二十多条 DDL 走 Neon）会超时，部署被判不健康而回滚（实测 `update_failed: HTTP health check failed (timed out after 5 seconds)`）。现已改为一次 `SELECT 1`，并把 store 收敛为进程内单例，探针 0.5s 级 |

这两条对开发/演示的影响：**第一个请求会慢几秒到几十秒**（冷启动），前端应该给足超时并显示"正在唤醒服务"；
正式跑长任务前可以先打一次 `/api/v1/health` 预热。

**免费层跑长任务的真实结论**：本次实测里，**任何行程都没能跑到终态** ——
三次真实 run 都被 OOM kill，但倒下的阶段不止一个：一次 2 天行程倒在更早的
`search_social_guides`（就是上表 `15:30:49` 那次 oomKilled），另一次 2 天行程与一次 1 天行程
则倒在 `extract_and_normalize_places` 刚开始时（1 天那次：`15:44:39` 进入该阶段，
`15:44:44` 就被杀，只差 5 秒），说明这是

> **内存上限问题，不是行程长度的函数。**

> 这一段的阶段名来自当时的**旧固定流程**（已退役）。主路径换成 Agent Loop 后阶段由 Agent
> 实际调用的工具决定，但"Python + MCP Node 进程 + 大体积原始 payload + Discovery 批量 prompt"
> 这几项内存占用与"同进程后台线程"的执行语义都没变，结论仍然成立。

内存怎么被吃掉的（按可能性排）：容器里同时驻留 Python（LangGraph + 全部 Provider 装配）、
**`npx 12306-mcp` 拉起的 Node 进程**、TikTok/小红书这类大体积原始 payload、
以及抽取阶段一次性拼出来的批量 prompt。512Mi 装不下这一整套。

可选的缓解手段（按"改动大小"排）：

1. **把实例升到更大内存档位** —— 最直接，长任务本来就不适合 512Mi。
2. **在免费层关掉容器内 MCP**（`TRAVELPLAN_DISABLE_MCP=1`，这是测试用的同一个开关）：
   不再拉起 Node 进程，12306 如实降级为 `UNAVAILABLE`，火车由途牛兜底，
   行程仍然产出一条明确的降级说明。代价是丢掉 12306 这个主源，**不建议在生产长期这么配**。
3. **预热城市知识库**（`scripts/preheat_cities.py`，见 [数据复用与实体](../architecture/DATA_REUSE_ENTITY.md) §9）：
   把 Discovery 里最贵的「社媒检索 + 模型抽取」移到离线，线上只剩机酒火的实时查询，
   热门城市命中缓存后 Discovery 的负载与耗时都大幅下降。
   **这个脚本要在本地 / CI 跑**（只要求写同一个 Neon）：放这台 512Mi 的实例上跑，
   等于换个地方犯同一个错。
4. 收窄单次 run 的规模（`DISCOVERY_MAX_PLACES` / `PLANNER_POI_LIMIT` / `USER_VISIBLE_POI_LIMIT`，
   管理端 Config 页就是运行时开关），以及**减少原始 payload 的驻留**（抽取阶段只保留
   截断后的正文，不再同时握着 provider 原始响应）。
5. 把 run 执行器拆到独立进程/队列，让 Web 进程只负责收活与查询状态。

> **探针也不是无辜的**：`/api/v1/health` 只做一次 `SELECT 1`，但那次 SELECT 要从**进程内共享
> 连接池**取连接（`app/db.py`，上限 4 条，等待上限 15 秒）—— 而 Discovery 期间 4 个 worker
> 线程正不停写库。池被占满时探针会排在后面，**最坏要等 15 秒，是平台健康检查预算（5 秒）的 3 倍**。
> 2026-09-21 10:12 UTC 那次实例被杀（`unhealthy: HTTP health check failed (timed out after 5 seconds)`）
> 就是这条路径，它把当时正在跑的 Discovery 一起带走了。

> **已被重启打断的 run 现在会自己收敛**：进程启动时（FastAPI lifespan）会把上一进程遗留的
> `RUNNING` 统一标成 `CANCELLED`，并带上原因"服务重启导致本次运行中断…"。
> 前端本来就有 CANCELLED 的文案，所以用户看到的是"这次规划已被取消"而不是一直转圈。
> 收敛只改状态、不删数据（Evidence / Trace / 产物都留着）。
> 测试通过 `TRAVELPLAN_SKIP_ORPHAN_SWEEP=1` 关掉它，避免触发启动事件时写到开发库。


## 8. 实测记录（本次部署验证做了什么）

已在 **Neon + Render 免费层**上真实验证：

| 项 | 证据 |
| --- | --- |
| Neon 连接与建表 | `python scripts/verify_deployment.py neon`：`get_store()` → `backend_name=postgres`，`init_schema()` 后 `information_schema.tables` 数到 **25 张表**（脚本动态打印真实数量，见 `scripts/verify_deployment.py:170`）；服务器 `PostgreSQL 18.6` |
| Neon 真实读写回环 | 同一次运行里写入 run / run_stages / run_metrics / planning_session 并全部读回（断言全过） |
| 绝不静默回退 | `python scripts/verify_deployment.py no-fallback`：`status=degraded`、`database_backend=postgres`、`database_connected=false`、`store.error="PostgresUnavailable: 无法连接 postgres（Neon）：ConnectionTimeout..."`、`POST /plans` 5xx、**SQLite 文件未被创建**；连续 5 次健康检查耗时 `[2.03, 2.03, 2.05, 2.06, 2.05]s`（无 15s 池满假象） |
| Render 服务 | `travelplan-api`（`srv-danu823m8hqs73cj67ng`，region=ohio，plan=free）→ `https://travelplan-api.onrender.com`；无需绑定支付方式即可创建免费层 |
| 部署成功 | deploy `dep-danu84dahn7c73e5her0` → `live`（约 5 分钟，从 commit `bd911e1`） |
| 健康检查 | `GET /api/v1/health` → `200`、`store.backend=postgres`、`store.ok=true`、指向 Neon 池化主机 |
| 容器内有 node | 构建日志 `node --version && npm --version` → `v20.19.2` / `9.2.0`；运行时由 12306 主源（`npx -y 12306-mcp`）的真实查询佐证 |
| 后台 Run 在 202 后继续跑 | 部署后的 `POST /api/v1/plans {background:true}` → `202`，`/status` 轮询到终态（见交付报告的具体 run_id 与阶段序列） |
| 无持久盘也能工作 | 部署后的 run 仍能下载 `/plans/{id}/artifacts/plan.json` 与 `audit_report.json` |
| 环境变量完整 | 服务上共 13 个环境变量（`render.yaml` 列的 Provider Key + `DATABASE_URL` + `TRAVELPLAN_ADMIN_TOKEN` + `TRAVELPLAN_CORS_ORIGINS` + 两个路径），值只落在 Render，仓库里只有 key 名 |

**未验证 / 需要注意**：

- MediaCrawler 在容器里仍不可用（需要本地克隆 + 浏览器会话），攻略只走 TikHub → Web Search。

## 9. 每日定时预热

预热脚本（`scripts/preheat_cities.py`）的 `--daily` 模式由 GitHub Actions 在每天
**北京时间（/马来西亚时间）03:00** 自动触发：workflow 的 cron 是 `0 19 * * *`（UTC），
UTC 19:00 = 北京 / 马来西亚次日 03:00。跑在 `ubuntu-latest` 上，**串行**处理城市、
每天最多 15 座、按天轮转起点。

**批调度归代码，每座城市内部归 Agent**：跑哪些城、串行、限量、跳过判定、退出码都在脚本里；
一座城市内部「搜哪些攻略（小红书/抖音/网页）」「正文里哪些地名值得抽」「哪些点要到高德核实、
要不要补详情」「哪些条目真的入库」由 `app/preheat_agent.py` 的预热 Agent 调工具决定
（工具集 `app/preheat_tools.py`）。入库仍走已有两条路径（实体归一化 + 城市缓存四张表），
所以缓存结构与实时路径完全一致，**读路径一行都不用改**：
之后所有去这些城市的会话都走城市知识库命中路径（见第 7 节缓解手段 3 与
[数据复用与实体](../architecture/DATA_REUSE_ENTITY.md) §2.1）。

预热的护栏是 `MAX_PREHEAT_AGENT_STEPS` / `PREHEAT_AGENT_TIMEOUT_SECONDS`；
失败即不落库（跑完了但一个 POI 都没登记也算失败），所以坏城市不会写半座城进缓存。

**为什么放 CI 而不是 Render**：沿用第 7 节的同一组理由 —— 线上是 512Mi 单实例，
预热一跑就跟健康检查抢 CPU 与数据库连接，会把正在服务的请求拖死（第 7 节实测过
OOM kill 与健康检查超时）。CI 只要求写同一个库（Neon），预热放到那里，
线上实例不会"换个地方犯同一个错"。

**仓库需要配置的 secrets**（值请在 GitHub repo Settings → Secrets and variables →
Actions 里配，**不要把值写进任何文件**）：

`DATABASE_URL`、`AMAP_API_KEY`、`TAVILY_API_KEY`、`TIKHUB_API_TOKEN`、
`MODEL_NAME`、`MODEL_BASE_URL`、`MODEL_API_KEY`（可选再加 `MODEL_*_bk1` / `_bk2`）。
workflow 里仍传了 `JEV_API_KEY`，那是历史遗留、预热 Agent 用不到。

可选：仓库 Variable `PREHEAT_DAILY_LIMIT`（默认 15）控制每天最多预热的城市数。

**手动触发**：

- GitHub Actions 页面 → 选中本 workflow → **Run workflow**（`workflow_dispatch`）；
- 或本地跑同样的命令：`python scripts/preheat_cities.py --daily`（本地读 `.env`，
  需要同样的环境变量；且当前库必须是共享的 Postgres，否则脚本会拒绝执行）。

失败处理：脚本退出码非 0（有城市失败 / 未落库）时 job 直接失败，Actions 页面标红；
不接外部通知渠道，避免引入第三方依赖。
