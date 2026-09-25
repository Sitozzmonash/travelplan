"""TravelPlan 业务证据库（PRD §17）。

为什么自己开一个 SQLite，而不是用 SuperHarness Memory
---------------------------------------------------
Memory 存的是"对话与 agent 状态"，这里存的是"这次 run 查到了什么、信不信、用了没有"。
两者生命周期和查询方式都不一样：审计要按 run_id 把 source → evidence → place →
decision → plan 全捞出来，这是关系型查询，不是向量检索。PRD §17 明确两者不混淆。
所以本模块只做一件事：把业务证据按 run 归档，别的都不管。

为什么 JSON 字段存成 TEXT
------------------------
sources / evidence / plans 的结构由 Provider 决定，随时可能多一个字段。把原始返回和
归一化结果整块存 JSON，比给每个 Provider 建列更耐改；查询靠 run_id + 主键，
不需要对这些 blob 做条件查询，所以不牺牲什么。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

# 方言差异（占位符 / INSERT OR REPLACE / PRAGMA / 连接池）**全部**在 app/db.py 里，
# 本模块的方法体继续只写 SQLite 原生 SQL —— 想看"两种库差在哪"，只需要读 app/db.py。
# `DEFAULT_DB_PATH` / `DB_PATH_ENV` / `default_db_path` 是历史定义、在此再导出，
# 保持 api 与测试的既有导入路径不变。
from app.db import (
    DB_PATH_ENV,
    DEFAULT_DB_PATH,
    PostgresBackend,
    SqliteBackend,
    default_db_path,
    resolve_backend,
)
from app.models import Decision, Evidence, Place, TripIntent, TripPlan, utcnow


#: 统一的 run 状态表达式：`run_progress.status` 优先，缺失时按 `runs.status` 的历史值折算。
#: 历史值是小写（completed/failed/running/cancelled），对外一律大写口径。
_STATUS_EXPR = (
    "COALESCE(p.status, CASE r.status"
    " WHEN 'completed' THEN 'SUCCESS'"
    " WHEN 'failed' THEN 'FAILED'"
    " WHEN 'running' THEN 'RUNNING'"
    " WHEN 'cancelled' THEN 'CANCELLED'"
    " ELSE 'UNKNOWN' END)"
)


def _percentile(values: list[int], ratio: float) -> int | None:
    """最近邻百分位。样本很少时不做插值 —— 4 次调用算出来的 P95 插值是在编数字。"""

    if not values:
        return None
    index = min(len(values) - 1, max(0, int(round(ratio * (len(values) - 1)))))
    return int(values[index])


#: 调用参数里绝不允许落库的键（大小写无关）。在**存储边界**做，而不是只靠调用方——
#: 只要有一个调用点忘了脱敏，密钥就会进库并通过管理端接口回显出去。
_SENSITIVE_QUERY_KEYS = ("api_key", "apikey", "key", "token", "access_token", "authorization", "secret", "password")


def _scrub_query(query: Any) -> dict[str, Any]:
    """丢掉凭据类参数，其余原样保留（Provider Health 要看到"查了什么"）。"""

    if not isinstance(query, dict):
        return {}
    return {
        str(key): ("***" if any(mark in str(key).lower() for mark in _SENSITIVE_QUERY_KEYS) else value)
        for key, value in query.items()
    }


def _json(value: Any) -> str:
    """统一 JSON 序列化：ensure_ascii=False 让中文在库里可读，排查问题不用再转码。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text: Any, fallback: Any) -> Any:
    if text is None:
        return fallback
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


# ======================================================================
# SCHEMA：一份 DDL，两边共用
# ======================================================================
# 为什么可以共用一份（而不是维护 SQLite / Postgres 两份）：
#   1. 只用两种方言都支持的语法子集：CREATE TABLE/INDEX IF NOT EXISTS、复合主键、
#      复合外键、TEXT/INTEGER/REAL、ON CONFLICT（SCHEMA 里本来就没有用到）；
#   2. 唯二的差异点（`INTEGER PRIMARY KEY AUTOINCREMENT` → identity、`REAL` → DOUBLE
#      PRECISION）由 app/db.py 的 `translate_postgres_schema()` 在生成 Postgres 版本时
#      统一替换。在这份字符串里保持 SQLite 写法，SQLite 侧行为与历史版本逐字节一致。
# 判断依据：主键一律是显式 TEXT（或 (run_id, place_id) 复合），不走 SQLite 的 rowid 语义；
# 只有 trip_requests / decisions / plan_items 三张自增表依赖 `id` 的递增顺序，由 identity 列承接。
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    user_id         TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    original_query  TEXT
);

CREATE TABLE IF NOT EXISTS trip_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    raw_query   TEXT,
    intent_json TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    provider        TEXT,
    source_type     TEXT,
    source_url      TEXT,
    query_json      TEXT,
    fetched_at      TEXT,
    raw_json        TEXT,
    normalized_json TEXT,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id         TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(run_id),
    source_id           TEXT REFERENCES sources(source_id),
    source_type         TEXT,
    provider            TEXT,
    source_url          TEXT,
    title               TEXT,
    author              TEXT,
    published_at        TEXT,
    fetched_at          TEXT,
    text                TEXT,
    place_mentions_json TEXT,
    specific_dishes_json TEXT,
    raw_metrics_json    TEXT
);

CREATE TABLE IF NOT EXISTS places (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    place_id        TEXT NOT NULL,
    name            TEXT,
    normalized_name TEXT,
    type            TEXT,
    lat             REAL,
    lng             REAL,
    address         TEXT,
    city            TEXT,
    district        TEXT,
    opening_hours   TEXT,
    amap_verified   INTEGER DEFAULT 0,
    aliases_json    TEXT,
    -- 主键必须带上 run_id：place_id 是高德的全球唯一 POI id，若只用它做主键，
    -- 第二次 run 走到同一个 POI 时 INSERT OR REPLACE 会**顶掉上一个 run 的行**
    -- （run_id 被改写成新 run），于是前一次 run 查不到自己的 place，
    -- 而 place_evidence 里两个 run 的 evidence 会挂到同一个 place_id 上。
    PRIMARY KEY (run_id, place_id)
);

CREATE TABLE IF NOT EXISTS place_evidence (
    run_id      TEXT NOT NULL,
    place_id    TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    PRIMARY KEY (run_id, place_id, evidence_id),
    -- 复合外键：证据只能挂到**同一 run 内**属于该 run 的 place 上。
    FOREIGN KEY (run_id, place_id) REFERENCES places(run_id, place_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    stage           TEXT,
    entity_id       TEXT,
    decision        TEXT,
    reason_codes_json TEXT,
    reason          TEXT,
    scores_json     TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    run_id     TEXT PRIMARY KEY REFERENCES runs(run_id),
    plan_json  TEXT,
    plan_md    TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(run_id),
    day_index INTEGER,
    item_id   TEXT,
    item_json TEXT
);

-- 业务层运行状态：供前端长任务轮询与管理后台读取。它不替代 SuperHarness
-- EventBus/Trace；这里只存一次旅行规划的阶段事实与可聚合摘要。
CREATE TABLE IF NOT EXISTS run_progress (
    run_id          TEXT PRIMARY KEY REFERENCES runs(run_id),
    status          TEXT NOT NULL,
    current_stage   TEXT,
    message         TEXT,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    finished_at     TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS run_stages (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    stage_id        TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    facts_json      TEXT,
    PRIMARY KEY (run_id, stage_id)
);

CREATE TABLE IF NOT EXISTS trace_spans (
    span_id         TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    parent_span_id  TEXT,
    component       TEXT NOT NULL,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    attributes_json TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id              TEXT PRIMARY KEY REFERENCES runs(run_id),
    duration_ms         INTEGER,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cached_tokens       INTEGER,
    total_tokens        INTEGER,
    llm_calls           INTEGER,
    jev_calls           INTEGER,
    tool_calls          INTEGER,
    provider_failures   INTEGER,
    badcase_count       INTEGER,
    cost                REAL,
    quality_score       REAL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS badcases (
    badcase_id              TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL REFERENCES runs(run_id),
    category                TEXT NOT NULL,
    severity                TEXT NOT NULL,
    symptom                 TEXT NOT NULL,
    expected                TEXT,
    actual                  TEXT,
    suspected_root_cause    TEXT,
    root_cause_status       TEXT NOT NULL DEFAULT 'pending',
    detected_by             TEXT,
    trace_refs_json         TEXT,
    analysis_status         TEXT NOT NULL DEFAULT 'pending',
    fixed_status            TEXT NOT NULL DEFAULT 'unfixed',
    introduced_in           TEXT,
    fixed_in                TEXT,
    created_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_run ON sources(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_places_run ON places(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_plan_items_run ON plan_items(run_id, day_index);
CREATE INDEX IF NOT EXISTS idx_run_stages_run ON run_stages(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_spans_run ON trace_spans(run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_badcases_run ON badcases(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_badcases_triage ON badcases(analysis_status, category, created_at);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    benchmark_run_id    TEXT PRIMARY KEY,
    suite               TEXT NOT NULL,
    version             TEXT NOT NULL,
    travelplan_commit   TEXT,
    superharness_commit TEXT,
    model               TEXT,
    fixture_version     TEXT,
    config_json         TEXT,
    jev_enabled         INTEGER NOT NULL DEFAULT 0,
    live                INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    case_count          INTEGER,
    passed              INTEGER,
    failed              INTEGER,
    metrics_json        TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_case_results (
    result_id           TEXT PRIMARY KEY,
    benchmark_run_id    TEXT NOT NULL REFERENCES benchmark_runs(benchmark_run_id),
    case_id             TEXT NOT NULL,
    suite               TEXT NOT NULL,
    title               TEXT,
    status              TEXT NOT NULL,
    metrics_json        TEXT,
    detail_json         TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_benchmark_case_results_run
    ON benchmark_case_results(benchmark_run_id, suite, case_id);

CREATE TABLE IF NOT EXISTS evolution_runs (
    evolution_run_id    TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    badcase_count       INTEGER NOT NULL DEFAULT 0,
    experience_count    INTEGER NOT NULL DEFAULT 0,
    decision            TEXT,
    summary             TEXT,
    steps_json          TEXT,
    before_metrics_json TEXT,
    after_metrics_json  TEXT,
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS experiences (
    experience_id        TEXT PRIMARY KEY,
    evolution_run_id     TEXT REFERENCES evolution_runs(evolution_run_id),
    source_badcases_json TEXT,
    failure_pattern      TEXT NOT NULL,
    verified_root_cause  TEXT,
    affected_module      TEXT,
    recommended_change   TEXT,
    before_metrics_json  TEXT,
    after_metrics_json   TEXT,
    decision             TEXT,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_experiences_evolution ON experiences(evolution_run_id, created_at);

-- 引导式旅程的"规划前会话"。
-- 为什么单独一张表：用户还在点偏好时不该产生正式 Run（关掉页面不等于一次 FAILED），
-- 而 Discovery 又需要提前把真实候选查出来并缓存给后续正式 Run 复用。
-- 只有 JSON 列 + 一个状态字段：这是临时会话，不值得为它建十几张表。
CREATE TABLE IF NOT EXISTS planning_sessions (
    session_id          TEXT PRIMARY KEY,
    status              TEXT NOT NULL,
    discovery_status    TEXT NOT NULL DEFAULT 'PENDING',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    basic_intent_json   TEXT,
    preferences_json    TEXT,
    poi_selections_json TEXT,
    transport_json      TEXT,
    hotels_json         TEXT,
    places_json         TEXT,
    evidence_summary_json TEXT,
    degradations_json   TEXT,
    events_json         TEXT,
    discovery_json      TEXT,
    prefetch_json       TEXT,
    error               TEXT,
    run_id              TEXT
);

CREATE INDEX IF NOT EXISTS idx_planning_sessions_updated ON planning_sessions(updated_at);

-- 跨会话复用的城市知识。它与 run 级 ``places`` 完全分开：后者是审计快照，
-- 这里是按城市更新的攻略/POI 基础信息，绝不能共享 run_id 主键。
CREATE TABLE IF NOT EXISTS city_pois (
    city TEXT NOT NULL, place_id TEXT NOT NULL,
    name TEXT, normalized_name TEXT, category TEXT, lng REAL, lat REAL,
    address TEXT, business_area TEXT, district TEXT, opening_hours TEXT, phone TEXT, rating REAL,
    amap_verified INTEGER NOT NULL DEFAULT 0,
    trust_score REAL, ad_risk REAL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(city, place_id)
);

CREATE TABLE IF NOT EXISTS city_poi_mentions (
    city TEXT NOT NULL, place_id TEXT NOT NULL,
    raw_name TEXT NOT NULL, source_type TEXT NOT NULL,
    provider TEXT, source_url TEXT, snippet TEXT,
    evidence_key TEXT,
    tone TEXT, published_at TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY(city, source_url, raw_name)
);

-- 跨会话复用的**攻略正文**。
-- 与 city_poi_mentions 的分工：后者是"某篇攻略提到了某个地点"的索引（供 trust/ad_risk
-- 重算），这里是正文本身，让第二个去同一座城市的会话不必把社媒与模型抽取全价重付一遍。
-- 主键用 ``evidence_key``（内容键 = source_url/title 的哈希）而不是 run 级 ``evidence_id``：
-- 后者的形状是 ``{source_id}-e{n}``、内嵌 session_id，跨会话必然不同，用它做城市级键
-- 会为同一篇文章反复累积新行。
CREATE TABLE IF NOT EXISTS city_evidences (
    city TEXT NOT NULL,
    evidence_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    provider TEXT,
    source_url TEXT,
    title TEXT,
    author TEXT,
    published_at TEXT,
    fetched_at TEXT,
    text TEXT NOT NULL DEFAULT '',
    place_mentions_json TEXT NOT NULL DEFAULT '[]',
    specific_dishes_json TEXT NOT NULL DEFAULT '[]',
    raw_metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(city, evidence_key)
);

-- 城市级的检索词计划：复用它可以连 query_expansion 那次模型调用一起省掉。
CREATE TABLE IF NOT EXISTS city_cache_meta (
    city TEXT PRIMARY KEY,
    social_queries_json TEXT NOT NULL DEFAULT '[]',
    social_served_queries_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_city_pois_updated ON city_pois(city, updated_at);
CREATE INDEX IF NOT EXISTS idx_city_poi_mentions_updated ON city_poi_mentions(city, place_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_city_evidences_updated ON city_evidences(city, updated_at);

-- ======================================================================
-- Canonical Place 实体层（"同一个现实地点只有一个身份"）
-- ======================================================================
-- 与 run 级 ``places`` 的关系：那是**某一次 run 的证据快照**（主键带 run_id，永不跨 run
-- 复用），这里是**跨 run / 跨会话的现实实体**。两者必须分开：把 canonical 身份塞进
-- places 就等于让一次 run 的去重结论改写所有历史 run 的证据链。
--
-- 表名刻意叫 canonical_places 而不是方案里写的 places，也是同一个原因：``places``
-- 已经被 run 级快照占用，同名会让"读到的到底是快照还是实体"变成靠猜。
CREATE TABLE IF NOT EXISTS canonical_places (
    canonical_place_id TEXT PRIMARY KEY,
    canonical_name     TEXT NOT NULL,
    normalized_name    TEXT NOT NULL,
    city               TEXT NOT NULL,
    district           TEXT,
    business_area      TEXT,
    lng                REAL,
    lat                REAL,
    address            TEXT,
    opening_hours      TEXT,
    category           TEXT NOT NULL DEFAULT 'other',
    -- place = 用户真正想去的地点；facility = 它的子设施（入口/停车场/售票处/游客中心…）。
    -- 子设施保留自己的身份（它可能有真实用途，例如"从这里进"），但不该和母体并列成候选。
    kind               TEXT NOT NULL DEFAULT 'place',
    -- 子设施指回母体；母体自己为 NULL。
    parent_place_id    TEXT,
    confidence         REAL NOT NULL DEFAULT 0,
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_canonical_places_city_name
    ON canonical_places(city, normalized_name);
CREATE INDEX IF NOT EXISTS idx_canonical_places_parent
    ON canonical_places(parent_place_id);

-- 别名索引：解析一个名字时**先查这里**，命中就完全不必打 Provider。
-- 主键是 (city, normalized_alias) 而不是 (canonical_place_id, alias)：同一座城市里
-- 一个归一化别名只能指向一个实体。否则 alias 表既能表达"一个名字属于两个地点"，
-- 又会在解析时被迫在两者之间随便选一个 —— 那是错并的温床，不如让写入时就冲突。
CREATE TABLE IF NOT EXISTS place_aliases (
    city               TEXT NOT NULL,
    normalized_alias   TEXT NOT NULL,
    alias              TEXT NOT NULL,
    canonical_place_id TEXT NOT NULL,
    source             TEXT NOT NULL DEFAULT 'derived',
    confidence         REAL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(city, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_place_aliases_place
    ON place_aliases(canonical_place_id);

-- Provider 引用：高德 poi_id 只是**证据**，不是身份。
-- 主键 (provider, provider_place_id)：一个 Provider 的一个 ID 只能映射到一个 canonical
-- 实体（这是"同一个 POI 第二次出现时不必再判断"的根据）；反过来一个 canonical 实体
-- 可以挂多个 Provider ID（景区主 POI + 子 POI + 旧 ID + 换 Provider）。
CREATE TABLE IF NOT EXISTS place_provider_refs (
    provider           TEXT NOT NULL,
    provider_place_id  TEXT NOT NULL,
    canonical_place_id TEXT NOT NULL,
    -- 冗余存一份 city：按城市批量装载引用时不必 join canonical_places（那是一次全表扫）。
    city               TEXT NOT NULL DEFAULT '',
    provider_name      TEXT,
    -- Provider 给的原始类型串（高德形如"交通设施服务;地铁站;地铁站"）。必须留：它是
    -- "这个 POI 是不是子设施"最可靠的结构化依据，而缓存命中时我们不会再打 Provider。
    provider_type      TEXT,
    lng                REAL,
    lat                REAL,
    address            TEXT,
    district           TEXT,
    business_area      TEXT,
    opening_hours      TEXT,
    fetched_at         TEXT,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(provider, provider_place_id)
);

CREATE INDEX IF NOT EXISTS idx_place_provider_refs_place
    ON place_provider_refs(canonical_place_id);
CREATE INDEX IF NOT EXISTS idx_place_provider_refs_city
    ON place_provider_refs(city);

-- Evidence → Canonical Place 的挂载。表名带 _links 是为了**不和 run 级 place_evidence
-- 撞名**：后者是 (run_id, place_id, evidence_id) 的 run 内证据链，语义完全不同。
-- evidence_key 用城市级内容键（与 city_evidences 同源），跨会话稳定。
CREATE TABLE IF NOT EXISTS place_evidence_links (
    canonical_place_id TEXT NOT NULL,
    city               TEXT NOT NULL,
    evidence_key       TEXT NOT NULL,
    mention_text       TEXT NOT NULL DEFAULT '',
    confidence         REAL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY(canonical_place_id, evidence_key, mention_text)
);

CREATE INDEX IF NOT EXISTS idx_place_evidence_links_city
    ON place_evidence_links(city, evidence_key);

-- 主点 / 子点关系。方案 §10：熊猫基地与"熊猫基地南门"是两个实体 + 一条父子边，
-- 而不是粗暴合成一个 Place。
CREATE TABLE IF NOT EXISTS place_relations (
    parent_place_id TEXT NOT NULL,
    child_place_id  TEXT NOT NULL,
    relation_type   TEXT NOT NULL DEFAULT 'child',
    -- 同 place_provider_refs：按城市批量装载时省掉一次 join。
    city            TEXT NOT NULL DEFAULT '',
    confidence      REAL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(parent_place_id, child_place_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_place_relations_child
    ON place_relations(child_place_id);
CREATE INDEX IF NOT EXISTS idx_place_relations_city
    ON place_relations(city);

-- 跨会话 / 跨 run 的查询缓存。回答的是"以前这个 query 查到了什么"，**不回答**
-- "这个 query 对应的实体是谁" —— 那必须再过一次 Entity Resolver（方案 §14）。
CREATE TABLE IF NOT EXISTS poi_query_cache (
    city             TEXT NOT NULL,
    query_type       TEXT NOT NULL,
    normalized_query TEXT NOT NULL,
    provider         TEXT,
    matched_place_ids_json   TEXT NOT NULL DEFAULT '[]',
    provider_result_ids_json TEXT NOT NULL DEFAULT '[]',
    result_count     INTEGER NOT NULL DEFAULT 0,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    fetched_at       TEXT NOT NULL,
    expires_at       TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY(city, query_type, normalized_query)
);

-- Resolver 留痕（方案 §21）：每次"复用了谁 / 并了谁 / 丢了谁 / 为什么没查 Provider"
-- 都要能回答。不写它，管理端就只能看到结果、看不到判断过程。
CREATE TABLE IF NOT EXISTS place_resolver_traces (
    trace_id           TEXT PRIMARY KEY,
    run_id             TEXT,
    session_id         TEXT,
    city               TEXT,
    operation          TEXT NOT NULL,
    action             TEXT NOT NULL,
    query              TEXT,
    canonical_place_id TEXT,
    parent_place_id    TEXT,
    relation_type      TEXT,
    confidence         REAL,
    reason             TEXT,
    signals_json       TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_place_resolver_traces_run
    ON place_resolver_traces(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_place_resolver_traces_city
    ON place_resolver_traces(city, created_at);

-- Provider 调用账本（观测用）。
-- 与 sources 的分工：sources 是"这次 run 用了什么证据"；provider_calls 是"谁在什么时候
-- 调了哪个数据源、成不成、多快" —— 后者要能记录还没有 run 的 Discovery 调用。
CREATE TABLE IF NOT EXISTS provider_calls (
    call_id         TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    tool            TEXT NOT NULL,
    status          TEXT NOT NULL,
    source_type     TEXT NOT NULL,
    source_id       TEXT,
    run_id          TEXT,
    session_id      TEXT,
    fetched_at      TEXT NOT NULL,
    duration_ms     INTEGER,
    returned        INTEGER,
    error           TEXT,
    fallback        INTEGER NOT NULL DEFAULT 0,
    query_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_provider_calls_provider ON provider_calls(provider, fetched_at);
CREATE INDEX IF NOT EXISTS idx_provider_calls_source ON provider_calls(source_type, fetched_at);

-- 管理端可运行时修改的非 Secret 配置（覆盖环境变量，落库以便重启后仍在）。
CREATE TABLE IF NOT EXISTS runtime_config (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

-- 城市推荐缓存：同一目的地 LLM 给出的第二页推荐清单落库，命中后整页零 LLM。
-- 与 city_cache 的区别：city_cache 存"攻略/POI 事实"，这张表存"LLM 选出来的推荐本身"。
CREATE TABLE IF NOT EXISTS city_recommendations (
    city TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


#: canonical 实体层各表**后加的列**：`ALTER TABLE ADD COLUMN` 用的 DDL。
#: 空元组的表表示当前没有待补的列 —— 新增列时一律在这里登记，不要只改 SCHEMA 里的
#: CREATE TABLE：那份 DDL 对已存在的表不生效（见 `_migrate_canonical_layer`）。
_CANONICAL_LAYER_COLUMNS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("canonical_places", (("opening_hours", "TEXT"),)),
    ("place_provider_refs", (("city", "TEXT NOT NULL DEFAULT ''"), ("provider_type", "TEXT"))),
    ("place_relations", (("city", "TEXT NOT NULL DEFAULT ''"),)),
    ("place_aliases", ()),
    ("place_evidence_links", ()),
    ("poi_query_cache", ()),
    ("place_resolver_traces", ()),
)


class TravelPlanStore:
    """按 run 归档业务证据的封装（SQLite 默认，PostgreSQL/Neon 可选）。
    后端选择（见 ``app/db.resolve_backend``）：
      - 显式传入 ``db_path`` → **一律 SQLite**（测试/Benchmark 传临时库，绝不会写到线上）；
      - 未传 ``db_path`` 且环境变量 ``DATABASE_URL`` 非空 → Postgres（Neon）；
      - 否则 → 本地 SQLite（``TRAVELPLAN_DB_PATH`` / ``data/travelplan.db``）。

    SQLite 下每次操作开一个短连接（`with self._connect()`）；Postgres 下由 ``app/db.py``
    复用进程内共享连接池。对外方法签名与返回值语义在两种后端下完全一致。
    """

    def __init__(
        self, db_path: str | Path | None = None, *, database_url: str | None = None,
        read_only: bool = False,
    ) -> None:
        self._read_only = read_only
        if read_only:
            if db_path is None and database_url is None:
                from app.config import database_url as configured_url

                database_url = configured_url()
            if db_path is not None or not database_url:
                db_path = Path(db_path) if db_path is not None else default_db_path()
                if not db_path.is_file():
                    raise FileNotFoundError(db_path)
        self._backend: SqliteBackend | PostgresBackend = resolve_backend(db_path, database_url)
        #: SQLite 时有值；Postgres 下为 None（真实目标见 ``describe()``）。
        self.db_path: Path | None = getattr(self._backend, "path", None)
        if not read_only:
            self.init_schema()

    @property
    def backend_name(self) -> str:
        """``sqlite`` 或 ``postgres``。"""

        return self._backend.name

    def describe(self) -> dict[str, Any]:
        """自描述（供 /health 上报后端类型与目标；绝不含用户名/密码）。"""

        return self._backend.describe()

    def ping(self) -> None:
        """轻量连通性验证（`SELECT 1`，不做 DDL）。供 /health 使用。"""

        self._backend.ping()

    def close(self) -> None:
        """释放后端资源（Postgres 连接池）。SQLite 无状态，是空操作；幂等。"""

        self._backend.close()

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------

    def _connect(self) -> Any:
        """取一条连接。SQLite 返回原生 ``sqlite3.Connection``；Postgres 返回翻译层包装。"""

        if self._read_only:
            return self._connect_read_only()
        return self._backend.connect()

    @contextmanager
    def _connect_read_only(self) -> Iterator[Any]:
        if self.db_path is not None:
            import sqlite3

            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()
        else:
            with self._backend.connect() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                yield conn

    def _migrate_places_scope(self, conn: Any) -> None:
        """把旧的「全局 place_id 主键」表迁到「按 run 建档」的新表。

        `CREATE TABLE IF NOT EXISTS` 不会改已存在的表，所以老库必须显式迁一次 ——
        否则它继续用旧主键跑，第二次 run 照样顶掉第一次的 place 行。
        迁移不丢数据：旧行原样搬过去，只有「跨 run 的 place_evidence」被丢掉，
        而那种链接本来就是错的（它把 A run 的证据挂到了 B run 的 place 上）。

        方言无关：用后端自省（SQLite 走 PRAGMA、Postgres 走 information_schema）拿到
        当前主键列，而不是去解析 ``sqlite_master.sql`` 文本。
        """
        primary_key = self._backend.table_primary_key(conn, "places")
        if not primary_key:
            return  # 表还不存在：交给随后的 CREATE TABLE
        if len(primary_key) == 2 and set(primary_key) == {"run_id", "place_id"}:
            return  # 已经是按 run 建档的新表

        conn.execute("ALTER TABLE places RENAME TO places_legacy_v1")
        conn.execute("ALTER TABLE place_evidence RENAME TO place_evidence_legacy_v1")
        # 索引名在库里是全局唯一的：rename 之后 idx_places_run 还挂在旧表上，
        # 不先删掉的话 SCHEMA 里的 CREATE INDEX IF NOT EXISTS 会因为「名字已被占用」
        # 直接跳过，新表就永远没有索引（旧表被 DROP 时索引也跟着没了）。
        conn.execute("DROP INDEX IF EXISTS idx_places_run")
        conn.executescript(self._backend.schema_ddl(SCHEMA))
        conn.execute(
            "INSERT OR IGNORE INTO places"
            " (run_id, place_id, name, normalized_name, type, lat, lng, address, city,"
            "  district, opening_hours, amap_verified, aliases_json)"
            " SELECT run_id, place_id, name, normalized_name, type, lat, lng, address, city,"
            "        district, opening_hours, amap_verified, aliases_json"
            " FROM places_legacy_v1"
        )
        conn.execute(
            "INSERT OR IGNORE INTO place_evidence (run_id, place_id, evidence_id)"
            " SELECT e.run_id, pe.place_id, pe.evidence_id"
            " FROM place_evidence_legacy_v1 pe"
            " JOIN evidence e ON e.evidence_id = pe.evidence_id"
            " JOIN places p ON p.run_id = e.run_id AND p.place_id = pe.place_id"
        )
        conn.execute("DROP TABLE place_evidence_legacy_v1")
        conn.execute("DROP TABLE places_legacy_v1")

    def init_schema(self) -> None:
        """建表（幂等）。构造函数里就调用，调用方不必记得先 migrate。"""
        with self._connect() as conn:
            self._migrate_places_scope(conn)
            # 补列必须在 `executescript(SCHEMA)` **之前**：那份脚本里有
            # `CREATE INDEX IF NOT EXISTS ... ON canonical_places(parent_place_id)`，
            # 老表缺这一列时索引语句会先炸掉，迁移根本没机会跑。
            self._migrate_canonical_layer(conn)
            self._migrate_city_cache_metadata(conn)
            # 同一份 SCHEMA 字符串给两种方言用：DDL 只用两边都支持的子集
            # （IF NOT EXISTS / 复合主键 / 复合外键 / 普通类型），方言细节由 backend 翻译。
            conn.executescript(self._backend.schema_ddl(SCHEMA))
            self._migrate_run_source(conn)
            self._migrate_session_prefetch(conn)
            self._migrate_run_metrics_quality(conn)

    def _migrate_canonical_layer(self, conn: Any) -> None:
        """给 canonical 实体层的表补齐后加的列。

        为什么需要：`CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做，而这张表是
        先上线的代码建出来的、`opening_hours` 是后加的 —— 老库拿到的是没有这一列的版本，
        写入时直接 `UndefinedColumn`。线上已经真实发生过一次（重启后预热全城 0 候选）。
        空列表表示"这张表当前没有待补的列"，留着是为了让新增列时有个明确的落点。
        """

        for table, columns in _CANONICAL_LAYER_COLUMNS:
            existing = self._backend.table_columns(conn, table)
            if not existing:
                continue  # 表还不存在：交给 SCHEMA 里的 CREATE TABLE
            for column, ddl in columns:
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_city_cache_metadata(self, conn: Any) -> None:
        """仅补兼容列：两种后端共用自省，不重建主键、不更新或删除历史行。"""

        for table, column, ddl in (
            ("city_pois", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("city_poi_mentions", "evidence_key", "TEXT"),
        ):
            columns = self._backend.table_columns(conn, table)
            if columns and column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_run_source(self, conn: Any) -> None:
        """给 `runs` 补 `source` / `source_session_id`（`CREATE TABLE IF NOT EXISTS` 不会改老表）。

        为什么需要：管理端要能回答"这次 run 是引导式还是随手一句话跑出来的"，
        并且要能把 Benchmark 用例运行从真实运行列表里过滤掉，否则列表会被它淹没。
        """

        existing = self._backend.table_columns(conn, "runs")
        if "source" not in existing:
            conn.execute("ALTER TABLE runs ADD COLUMN source TEXT DEFAULT 'quick'")
        if "source_session_id" not in existing:
            conn.execute("ALTER TABLE runs ADD COLUMN source_session_id TEXT")

    def _migrate_session_prefetch(self, conn: Any) -> None:
        """给 planning_sessions 补 `prefetch_json`（老库没有这一列）。"""

        columns = self._backend.table_columns(conn, "planning_sessions")
        if columns and "prefetch_json" not in columns:
            conn.execute("ALTER TABLE planning_sessions ADD COLUMN prefetch_json TEXT")
        if columns and "discovery_json" not in columns:
            conn.execute("ALTER TABLE planning_sessions ADD COLUMN discovery_json TEXT")

    def _migrate_run_metrics_quality(self, conn: Any) -> None:
        """给 run_metrics 补 `quality_score`（老库没有这一列，`CREATE TABLE IF NOT EXISTS` 不会改老表）。

        为什么需要：Dashboard 冷加载要对抽样 run 逐个解 plan_json 算质量分（Neon 上单次
        1~2s），Run 完成时把质量分写进这一列之后，dashboard 有列值就直接读，不再解析计划。
        老库没有这列时写入会 `UndefinedColumn`，所以必须显式 ALTER（幂等：有了就跳过）。
        """

        columns = self._backend.table_columns(conn, "run_metrics")
        if columns and "quality_score" not in columns:
            conn.execute("ALTER TABLE run_metrics ADD COLUMN quality_score REAL")

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        user_id: str | None = None,
        original_query: str = "",
        *,
        source: str = "quick",
        source_session_id: str | None = None,
    ) -> None:
        created_at = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs"
                " (run_id, user_id, created_at, status, original_query, source, source_session_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, user_id, created_at, "running", original_query, source, source_session_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO run_progress"
                " (run_id, status, current_stage, message, started_at, updated_at, finished_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                (run_id, "RUNNING", None, "规划任务已创建", created_at, created_at),
            )

    def finish_run(self, run_id: str, status: str = "completed", *, error: str | None = None) -> None:
        """结束旧 run 状态，同时更新对外标准化状态。

        ``runs.status`` 保持历史 API 的小写兼容；``run_progress.status`` 提供管理端和
        新前端统一使用的 ``SUCCESS/FAILED/DEGRADED/CANCELLED`` 契约。
        """
        canonical = {
            "completed": "SUCCESS",
            "cancelled": "CANCELLED",
            "running": "RUNNING",
        }.get(status, "FAILED")
        finished_at = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
            conn.execute(
                "UPDATE run_progress SET status=?, message=?, updated_at=?, finished_at=?, error=?"
                " WHERE run_id=?",
                (canonical, "规划已结束", finished_at, finished_at, error, run_id),
            )

    def cancel_orphan_runs(self, *, reason: str) -> list[str]:
        """把上一个进程遗留的 `RUNNING` run 收敛成 `CANCELLED`，返回被收敛的 run_id。

        为什么必须有：run 执行器是"同进程后台线程"语义。容器被重启（Render 免费层会在
        512Mi OOM 之后重启）时线程随进程消失，`run_progress.status` 就永远停在 RUNNING，
        前端会一直轮询 —— 实测一次 OOM 之后积了 3 条这样的 run。

        进程刚启动时不可能有正在跑的 run，所以这里查到的 RUNNING **一定是孤儿**。
        只动状态、不删数据：Evidence / Trace / 产物都留着，便于排查。
        """

        finished_at = utcnow().isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT run_id FROM run_progress WHERE status='RUNNING'").fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            if not run_ids:
                return []
            conn.execute(
                "UPDATE run_progress SET status='CANCELLED', message=?, updated_at=?, finished_at=?, error=?"
                " WHERE status='RUNNING'",
                (reason, finished_at, finished_at, reason),
            )
            # 兼容旧 API 的小写状态列，两处口径必须一致，否则管理端会自相矛盾。
            conn.execute("UPDATE runs SET status='cancelled' WHERE status='running'")
        return run_ids

    # ------------------------------------------------------------------
    # 运行状态、Trace 与管理端摘要
    # ------------------------------------------------------------------

    def update_stage(
        self,
        run_id: str,
        stage_id: str,
        status: str,
        *,
        message: str = "",
        facts: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """原子更新当前步骤，轮询端只会看到真实已发生的状态。"""
        updated_at = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_stages (run_id, stage_id, status, message, started_at, finished_at, facts_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(run_id, stage_id) DO UPDATE SET"
                " status=excluded.status, message=excluded.message,"
                " started_at=COALESCE(run_stages.started_at, excluded.started_at),"
                " finished_at=excluded.finished_at, facts_json=excluded.facts_json",
                (run_id, stage_id, status, message, started_at, finished_at, _json(facts or {})),
            )
            conn.execute(
                "UPDATE run_progress SET current_stage=?, message=?, updated_at=? WHERE run_id=?",
                (stage_id, message, updated_at, run_id),
            )

    def get_run_progress(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            progress = conn.execute("SELECT * FROM run_progress WHERE run_id=?", (run_id,)).fetchone()
            if progress is None:
                return None
            stages = conn.execute(
                "SELECT * FROM run_stages WHERE run_id=? ORDER BY started_at, stage_id", (run_id,)
            ).fetchall()
        result = dict(progress)
        result["stages"] = [
            {
                "stage_id": row["stage_id"],
                "status": row["status"],
                "message": row["message"] or "",
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "facts": _loads(row["facts_json"], {}),
            }
            for row in stages
        ]
        return result

    def set_progress_status(self, run_id: str, status: str, message: str) -> None:
        """只更新对外状态，不触碰旧 ``runs.status`` 的兼容字段。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_progress SET status=?, message=?, updated_at=? WHERE run_id=?",
                (status, message, utcnow().isoformat(), run_id),
            )

    def save_trace_span(
        self,
        run_id: str,
        span_id: str,
        *,
        component: str,
        name: str,
        status: str,
        started_at: str,
        finished_at: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trace_spans"
                " (span_id, run_id, parent_span_id, component, name, status, started_at, finished_at, attributes_json, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (span_id, run_id, parent_span_id, component, name, status, started_at, finished_at, _json(attributes or {}), error),
            )

    def save_run_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        fields = (
            "duration_ms", "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
            "llm_calls", "jev_calls", "tool_calls", "provider_failures", "badcase_count", "cost",
            "quality_score",
        )
        values = [metrics.get(field) for field in fields]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_metrics"
                " (run_id, duration_ms, input_tokens, output_tokens, cached_tokens, total_tokens,"
                " llm_calls, jev_calls, tool_calls, provider_failures, badcase_count, cost, quality_score, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, *values, utcnow().isoformat()),
            )

    def list_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
        include_benchmark: bool = True,
    ) -> list[dict]:
        """管理端列表只取摘要，不把整份 trace/audit 塞进首页。

        `status` 一定是一个字符串：老 run 没有 `run_progress` 行（该表是后加的），
        此时按 `runs.status` 的历史值折算成统一口径，而不是回一个 null ——
        消费方（管理端）拿到 null 只能整块不渲染，等于白白丢掉列表里其余正常的行。
        """

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append(f"{_STATUS_EXPR}=?")
            params.append(status)
        if q:
            # 只按 run_id 前缀/子串搜（用户手里只有这个），同时允许搜原始需求文本。
            clauses.append("(r.run_id LIKE ? OR COALESCE(r.original_query,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if not include_benchmark:
            # Benchmark 用例运行（source=benchmark）默认不出现在运行列表里：
            # 一次评测会产生几十条 run，会把真实 run 挤到后面看不到。
            clauses.append("COALESCE(r.source, 'quick') <> 'benchmark'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.run_id, r.user_id, r.created_at, r.status AS legacy_status,"
                f" COALESCE(r.original_query, '') AS original_query, {_STATUS_EXPR} AS status,"
                " COALESCE(r.source, 'quick') AS source, r.source_session_id,"
                " p.current_stage, p.message, p.started_at, p.updated_at, p.finished_at,"
                " m.duration_ms, m.total_tokens, m.llm_calls, m.jev_calls, m.tool_calls, m.provider_failures, m.badcase_count, m.cost, m.quality_score"
                " FROM runs r LEFT JOIN run_progress p ON p.run_id=r.run_id"
                " LEFT JOIN run_metrics m ON m.run_id=r.run_id"
                + where
                + " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 200)), max(0, offset)],
            ).fetchall()
        return [dict(row) for row in rows]

    def count_runs(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        include_benchmark: bool = True,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append(f"{_STATUS_EXPR}=?")
            params.append(status)
        if q:
            clauses.append("(r.run_id LIKE ? OR COALESCE(r.original_query,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if not include_benchmark:
            clauses.append("COALESCE(r.source, 'quick') <> 'benchmark'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM runs r LEFT JOIN run_progress p ON p.run_id=r.run_id" + where,
                    params,
                ).fetchone()["n"]
            )

    def set_run_source(self, run_id: str, source: str, source_session_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET source=?, source_session_id=? WHERE run_id=?",
                (source, source_session_id, run_id),
            )

    def get_run_metrics(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM run_metrics WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_trace_spans(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_spans WHERE run_id=? ORDER BY started_at, span_id", (run_id,)
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "attributes_json"},
                "attributes": _loads(row["attributes_json"], {}),
            }
            for row in rows
        ]

    def save_trip_request(self, run_id: str, raw_query: str, intent: TripIntent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO trip_requests (run_id, raw_query, intent_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (run_id, raw_query, _json(intent.model_dump(mode="json")), utcnow().isoformat()),
            )

    # ------------------------------------------------------------------
    # source / evidence
    # ------------------------------------------------------------------

    def save_source(self, run_id: str, source: dict) -> str:
        """保存一次 Provider 调用（统一信封形状），返回 source_id。

        信封里的 query / raw / normalized 是审计的核心：只有它们能回答
        "搜了什么、返回了什么、归一化后是什么"（PRD §28 audit_report）。
        """
        source_id = str(source.get("source_id") or source.get("id") or "")
        if not source_id:
            source_id = f"{run_id}-src-{utcnow().timestamp()}"
        fetched_at = source.get("fetched_at") or utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sources"
                " (source_id, run_id, provider, source_type, source_url, query_json,"
                "  fetched_at, raw_json, normalized_json, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    run_id,
                    str(source.get("provider") or "unknown"),
                    str(source.get("source_type") or ""),
                    source.get("source_url"),
                    _json(source.get("query") or {}),
                    str(fetched_at),
                    _json(source.get("raw") or {}),
                    _json(source.get("normalized") or {}),
                    str(source.get("status") or "OK"),
                ),
            )
        return source_id

    def save_evidence(self, run_id: str, evidence: Evidence, source_id: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence"
                " (evidence_id, run_id, source_id, source_type, provider, source_url, title,"
                "  author, published_at, fetched_at, text, place_mentions_json,"
                "  specific_dishes_json, raw_metrics_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.id,
                    run_id,
                    source_id or evidence.source_id,
                    evidence.source_type,
                    evidence.provider,
                    evidence.source_url,
                    evidence.title,
                    evidence.author,
                    evidence.published_at.isoformat() if evidence.published_at else None,
                    evidence.fetched_at.isoformat(),
                    evidence.text,
                    _json(evidence.place_mentions),
                    _json(evidence.specific_dishes),
                    _json(evidence.raw_metrics),
                ),
            )

    # ------------------------------------------------------------------
    # place
    # ------------------------------------------------------------------

    def save_place(self, run_id: str, place: Place) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO places"
                " (place_id, run_id, name, normalized_name, type, lat, lng, address, city,"
                "  district, opening_hours, amap_verified, aliases_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    place.place_id,
                    run_id,
                    place.name,
                    place.normalized_name or place.alias_key,
                    place.type,
                    place.lat,
                    place.lng,
                    place.address,
                    place.city,
                    place.district,
                    place.opening_hours,
                    1 if place.amap_verified else 0,
                    _json(place.aliases),
                ),
            )

    def link_place_evidence(self, run_id: str, place_id: str, evidence_id: str) -> None:
        """建立 Place ↔ Evidence 多对多（PRD §28：item → place → evidence → source 要能走通）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO place_evidence (run_id, place_id, evidence_id)"
                " VALUES (?, ?, ?)",
                (run_id, place_id, evidence_id),
            )

    # ------------------------------------------------------------------
    # city cache
    # ------------------------------------------------------------------

    def upsert_city_pois(self, city: str, rows: Iterable[dict[str, Any]]) -> int:
        """写入城市 POI 基础信息；旧刷新绝不覆盖较新的结果。"""

        values = [
            (
                city,
                str(row["place_id"]),
                row.get("name"), row.get("normalized_name"), row.get("category"),
                row.get("lng"), row.get("lat"), row.get("address"), row.get("business_area"),
                row.get("district"), row.get("opening_hours"), row.get("phone"), row.get("rating"),
                1 if row.get("amap_verified") else 0, row.get("trust_score"), row.get("ad_risk"),
                int(row.get("evidence_count") or 0), row.get("updated_at") or utcnow().isoformat(),
                _json(row["metadata"]) if "metadata" in row else row.get("metadata_json"),
            )
            for row in rows if row.get("place_id")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO city_pois"
                " (city, place_id, name, normalized_name, category, lng, lat, address, business_area,"
                "  district, opening_hours, phone, rating, amap_verified, trust_score, ad_risk,"
                "  evidence_count, updated_at, metadata_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, '{}'))"
                " ON CONFLICT(city, place_id) DO UPDATE SET"
                " name=excluded.name, normalized_name=excluded.normalized_name, category=excluded.category,"
                " lng=excluded.lng, lat=excluded.lat, address=excluded.address,"
                " business_area=excluded.business_area, district=excluded.district,"
                " opening_hours=excluded.opening_hours, phone=excluded.phone, rating=excluded.rating,"
                " amap_verified=excluded.amap_verified, trust_score=excluded.trust_score,"
                " ad_risk=excluded.ad_risk, evidence_count=excluded.evidence_count, updated_at=excluded.updated_at,"
                " metadata_json=CASE WHEN excluded.metadata_json='{}' THEN city_pois.metadata_json"
                " ELSE excluded.metadata_json END"
                " WHERE excluded.updated_at >= city_pois.updated_at",
                values,
            )
        return len(values)

    def upsert_city_poi_mentions(self, city: str, rows: Iterable[dict[str, Any]]) -> int:
        """无 URL 时用内部内容键占用旧 PK 的 URL 槽，读取时还原，不伪造外部来源。

        不破坏旧主键：同 URL / 同 raw_name 的多标题提及仍只能存一行，正文表不受影响。
        """

        values = [
            (
                city, str(row["place_id"]), str(row["raw_name"]), str(row.get("source_type") or "social"),
                row.get("provider"),
                str(row.get("source_url") or (
                    f"city-evidence:{row['evidence_key']}" if row.get("evidence_key") else ""
                )),
                row.get("snippet"), row.get("tone"),
                row.get("published_at"), row.get("updated_at") or utcnow().isoformat(),
                row.get("evidence_key"),
            )
            for row in rows if row.get("place_id") and row.get("raw_name")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO city_poi_mentions"
                " (city, place_id, raw_name, source_type, provider, source_url, snippet, tone, published_at, updated_at, evidence_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(city, source_url, raw_name) DO UPDATE SET"
                " place_id=excluded.place_id, source_type=excluded.source_type, provider=excluded.provider,"
                " snippet=excluded.snippet, tone=excluded.tone, published_at=excluded.published_at,"
                " updated_at=excluded.updated_at, evidence_key=COALESCE(excluded.evidence_key, city_poi_mentions.evidence_key)"
                " WHERE excluded.updated_at >= city_poi_mentions.updated_at",
                values,
            )
        return len(values)

    def get_city_cache_rows(self, city: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取单个城市的缓存原始行，时间有效性由 city_cache 统一判断。"""

        with self._connect() as conn:
            pois = conn.execute(
                "SELECT * FROM city_pois WHERE city=? ORDER BY updated_at DESC, name", (city,)
            ).fetchall()
            mentions = conn.execute(
                "SELECT * FROM city_poi_mentions WHERE city=? ORDER BY updated_at DESC, raw_name", (city,)
            ).fetchall()
        decoded_mentions = [dict(row) for row in mentions]
        for row in decoded_mentions:
            if row.get("evidence_key") and row.get("source_url") == f"city-evidence:{row['evidence_key']}":
                row["source_url"] = None
        return [dict(row) for row in pois], decoded_mentions

    def upsert_city_evidences(self, city: str, rows: Iterable[dict[str, Any]]) -> int:
        """写入城市攻略正文；同一篇（city, evidence_key）保留最新一次抓取。"""

        values = [
            (
                city,
                str(row["evidence_key"]),
                str(row.get("source_type") or "social"),
                row.get("provider"),
                row.get("source_url"),
                row.get("title"),
                row.get("author"),
                row.get("published_at"),
                row.get("fetched_at"),
                str(row.get("text") or ""),
                _json(row.get("place_mentions") or []),
                _json(row.get("specific_dishes") or []),
                _json(row.get("raw_metrics") or {}),
                row.get("updated_at") or utcnow().isoformat(),
            )
            for row in rows if row.get("evidence_key")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO city_evidences"
                " (city, evidence_key, source_type, provider, source_url, title, author,"
                "  published_at, fetched_at, text, place_mentions_json, specific_dishes_json,"
                "  raw_metrics_json, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(city, evidence_key) DO UPDATE SET"
                " source_type=excluded.source_type, provider=excluded.provider,"
                " source_url=excluded.source_url, title=excluded.title, author=excluded.author,"
                " published_at=excluded.published_at, fetched_at=excluded.fetched_at,"
                " text=excluded.text, place_mentions_json=excluded.place_mentions_json,"
                " specific_dishes_json=excluded.specific_dishes_json,"
                " raw_metrics_json=excluded.raw_metrics_json, updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= city_evidences.updated_at",
                values,
            )
        return len(values)

    def get_city_evidence_rows(self, city: str) -> list[dict[str, Any]]:
        """读取单个城市的攻略正文行（时间有效性由 city_cache 统一判断）。"""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM city_evidences WHERE city=? ORDER BY updated_at DESC, evidence_key",
                (city,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_city_cache_meta(
        self,
        city: str,
        *,
        social_queries: Sequence[str] = (),
        social_served_queries: Sequence[str] = (),
        updated_at: str | None = None,
    ) -> None:
        """记录该城市的检索词计划与真正搜过的检索词。"""

        timestamp = updated_at or utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO city_cache_meta"
                " (city, social_queries_json, social_served_queries_json, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(city) DO UPDATE SET"
                " social_queries_json=excluded.social_queries_json,"
                " social_served_queries_json=excluded.social_served_queries_json,"
                " updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= city_cache_meta.updated_at",
                (
                    city,
                    _json(list(social_queries)),
                    _json(list(social_served_queries)),
                    timestamp,
                ),
            )

    def get_city_cache_meta(self, city: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM city_cache_meta WHERE city=?", (city,)
            ).fetchone()
        return dict(row) if row is not None else None

    def prune_city_cache(self, city: str, *, guide_before: str, poi_before: str) -> int:
        """清掉该城市已经超过各自 TTL 的行，返回删除行数。

        WHY 必须清：`city_cache` 的新鲜度口径是"**所有**行都在 TTL 内才算新鲜"
        （最旧一条决定整座城市的年龄）。只增不删的话，一条再也搜不到的旧攻略会让
        这座城市**永远 stale**，于是每个新会话都触发一次后台全量刷新 —— TTL 名存实亡。
        删的是各自 TTL 之外的行，所以"用旧数据兜底再后台刷新"的语义不受影响。

        `guide_before` / `poi_before` 是 ISO 时间戳阈值，由 city_cache 按 TTL 算好传入。
        """

        removed = 0
        with self._connect() as conn:
            for table, column, threshold in (
                ("city_evidences", "updated_at", guide_before),
                ("city_poi_mentions", "updated_at", guide_before),
                ("city_pois", "updated_at", poi_before),
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE city=? AND {column} < ?", (city, threshold)
                )
                removed += int(cursor.rowcount or 0)
        return removed

    # ------------------------------------------------------------------
    # 城市推荐缓存（第二页 LLM 推荐清单）
    # ------------------------------------------------------------------
    # 与上面 city_cache 系表的区别：那里存"攻略/POI 事实"，这里存"LLM 选出来的推荐本身"。
    # 命中 TTL 内同城推荐时整页零 LLM；`created_at` 记录首次生成，`updated_at` 每次覆盖刷新。

    def save_city_recommendation(self, city: str, payload: Mapping[str, Any]) -> None:
        """upsert 城市推荐缓存：created_at 保留首次创建时间，updated_at 每次覆盖都刷新为 now。"""

        timestamp = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO city_recommendations (city, payload_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(city) DO UPDATE SET"
                " payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (city, _json(payload), timestamp, timestamp),
            )

    def get_city_recommendation(self, city: str) -> dict[str, Any] | None:
        """读城市推荐缓存。返回 {"city":..., "payload": <dict>, "created_at": <iso>, "updated_at": <iso>}，
        payload 是从 payload_json 反序列化的 dict；没有行返回 None。"""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM city_recommendations WHERE city=?", (city,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        try:
            payload = json.loads(record["payload_json"])
        except (TypeError, ValueError):
            # 行存在但 payload_json 损坏：按"未命中"处理，绝不把读缓存的异常抛给上层。
            return None
        return {
            "city": record["city"],
            "payload": payload,
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def invalidate_city_recommendation(self, city: str) -> None:
        """删除该城市的推荐缓存。"""

        with self._connect() as conn:
            conn.execute("DELETE FROM city_recommendations WHERE city=?", (city,))

    # ------------------------------------------------------------------
    # canonical place 实体层
    # ------------------------------------------------------------------

    def upsert_canonical_places(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入 Canonical Place 实体；旧写入不覆盖较新的。

        `created_at` 只在首次插入时写，后续更新不动它 —— 否则"这个实体是什么时候
        第一次被认识的"会随每次刷新往前漂。
        """

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row["canonical_place_id"]),
                str(row.get("canonical_name") or ""),
                str(row.get("normalized_name") or ""),
                str(row.get("city") or ""),
                row.get("district"), row.get("business_area"),
                row.get("lng"), row.get("lat"), row.get("address"),
                row.get("opening_hours"),
                str(row.get("category") or "other"),
                str(row.get("kind") or "place"),
                row.get("parent_place_id"),
                float(row.get("confidence") or 0.0),
                int(row.get("evidence_count") or 0),
                row.get("created_at") or timestamp,
                row.get("updated_at") or timestamp,
            )
            for row in rows if row.get("canonical_place_id")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO canonical_places"
                " (canonical_place_id, canonical_name, normalized_name, city, district,"
                "  business_area, lng, lat, address, opening_hours, category, kind, parent_place_id,"
                "  confidence, evidence_count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(canonical_place_id) DO UPDATE SET"
                " canonical_name=excluded.canonical_name, normalized_name=excluded.normalized_name,"
                " city=excluded.city, district=excluded.district, business_area=excluded.business_area,"
                " lng=excluded.lng, lat=excluded.lat, address=excluded.address,"
                " opening_hours=excluded.opening_hours,"
                " category=excluded.category, kind=excluded.kind, parent_place_id=excluded.parent_place_id,"
                " confidence=excluded.confidence, evidence_count=excluded.evidence_count,"
                " updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= canonical_places.updated_at",
                values,
            )
        return len(values)

    def get_canonical_places(
        self, city: str, *, canonical_ids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """按城市读取实体（可选只取指定的几个 id）。"""

        sql = "SELECT * FROM canonical_places WHERE city=?"
        params: list[Any] = [city]
        if canonical_ids is not None:
            ids = [str(item) for item in canonical_ids if str(item)]
            if not ids:
                return []
            sql += f" AND canonical_place_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY canonical_name", params).fetchall()
        return [dict(row) for row in rows]

    def count_canonical_places(self, city: str | None = None) -> int:
        """实体总数（管理端只用它报"这座城市的实体底座建了多少"）。"""

        with self._connect() as conn:
            if city:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM canonical_places WHERE city=?", (city,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS n FROM canonical_places").fetchone()
        return int(row["n"] if row is not None else 0)

    def upsert_place_aliases(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入别名索引。

        冲突时**不改 canonical_place_id**：主键 (city, normalized_alias) 的语义是
        "这个别名属于谁"，让后来者顶掉它等于允许一次错误的写入改写已有身份。
        只更新置信度与来源，身份冲突由 Entity Resolver 在写入前解决（它能看到两边）。
        """

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row.get("city") or ""),
                str(row["normalized_alias"]),
                str(row.get("alias") or ""),
                str(row["canonical_place_id"]),
                str(row.get("source") or "derived"),
                row.get("confidence"),
                row.get("updated_at") or timestamp,
            )
            for row in rows
            if row.get("normalized_alias") and row.get("canonical_place_id")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO place_aliases"
                " (city, normalized_alias, alias, canonical_place_id, source, confidence, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(city, normalized_alias) DO UPDATE SET"
                " alias=excluded.alias, source=excluded.source, confidence=excluded.confidence,"
                " updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= place_aliases.updated_at"
                "   AND place_aliases.canonical_place_id = excluded.canonical_place_id",
                values,
            )
        return len(values)

    def get_place_aliases(
        self, city: str, *, normalized_aliases: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """按城市读取别名（可选只取指定的几个归一化别名）。"""

        sql = "SELECT * FROM place_aliases WHERE city=?"
        params: list[Any] = [city]
        if normalized_aliases is not None:
            keys = [str(item) for item in normalized_aliases if str(item)]
            if not keys:
                return []
            sql += f" AND normalized_alias IN ({','.join('?' * len(keys))})"
            params.extend(keys)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_place_provider_refs(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入 Provider 引用（高德 poi_id → canonical 实体）。"""

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row.get("provider") or ""),
                str(row["provider_place_id"]),
                str(row["canonical_place_id"]),
                str(row.get("city") or ""),
                row.get("provider_name"), row.get("provider_type"),
                row.get("lng"), row.get("lat"),
                row.get("address"), row.get("district"), row.get("business_area"),
                row.get("opening_hours"), row.get("fetched_at"),
                row.get("updated_at") or timestamp,
            )
            for row in rows
            if row.get("provider_place_id") and row.get("canonical_place_id")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO place_provider_refs"
                " (provider, provider_place_id, canonical_place_id, city, provider_name, provider_type,"
                "  lng, lat, address, district, business_area, opening_hours, fetched_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(provider, provider_place_id) DO UPDATE SET"
                " canonical_place_id=excluded.canonical_place_id, city=excluded.city,"
                " provider_name=excluded.provider_name, provider_type=excluded.provider_type,"
                " lng=excluded.lng, lat=excluded.lat,"
                " address=excluded.address, district=excluded.district,"
                " business_area=excluded.business_area, opening_hours=excluded.opening_hours,"
                " fetched_at=excluded.fetched_at, updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= place_provider_refs.updated_at",
                values,
            )
        return len(values)

    def get_place_provider_refs(
        self,
        city: str | None = None,
        *,
        keys: Sequence[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """按 (provider, provider_place_id) 或按城市读取 Provider 引用。

        `keys` 的用法是"这批高德 POI 我以前见过吗"：一次查完，别在循环里逐条查。
        """

        clauses: list[str] = []
        params: list[Any] = []
        if city:
            clauses.append("city=?")
            params.append(city)
        if keys is not None:
            pairs = [(str(p), str(i)) for p, i in keys if p and i]
            if not pairs:
                return []
            clauses.append(
                "(" + " OR ".join("(provider=? AND provider_place_id=?)" for _ in pairs) + ")"
            )
            for provider, provider_id in pairs:
                params.extend([provider, provider_id])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM place_provider_refs" + where
                # 顺序必须稳定：调用方按顺序决定"哪条引用是主引用"，而它决定复用回来的
                # 候选带哪个 Provider ID。不排序时同一个实体两次会话可能带不同的 ID。
                + " ORDER BY provider, provider_place_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_place_evidence_links(self, rows: Iterable[dict[str, Any]]) -> int:
        """把攻略证据挂到 canonical 实体上。"""

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row["canonical_place_id"]),
                str(row.get("city") or ""),
                str(row["evidence_key"]),
                str(row.get("mention_text") or ""),
                row.get("confidence"),
                row.get("updated_at") or timestamp,
            )
            for row in rows
            if row.get("canonical_place_id") and row.get("evidence_key")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO place_evidence_links"
                " (canonical_place_id, city, evidence_key, mention_text, confidence, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(canonical_place_id, evidence_key, mention_text) DO UPDATE SET"
                " confidence=excluded.confidence, updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= place_evidence_links.updated_at",
                values,
            )
        return len(values)

    def get_place_evidence_links(self, city: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM place_evidence_links WHERE city=?", (city,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_place_relations(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入主点 / 子点关系。"""

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row["parent_place_id"]),
                str(row["child_place_id"]),
                str(row.get("relation_type") or "child"),
                str(row.get("city") or ""),
                row.get("confidence"),
                row.get("updated_at") or timestamp,
            )
            for row in rows
            if row.get("parent_place_id") and row.get("child_place_id")
            and str(row.get("parent_place_id")) != str(row.get("child_place_id"))
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO place_relations"
                " (parent_place_id, child_place_id, relation_type, city, confidence, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(parent_place_id, child_place_id, relation_type) DO UPDATE SET"
                " city=excluded.city, confidence=excluded.confidence, updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= place_relations.updated_at",
                values,
            )
        return len(values)

    def get_place_relations(self, city: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM place_relations WHERE city=?", (city,)
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_poi_query_cache(self, city: str, rows: Iterable[dict[str, Any]]) -> int:
        """写入跨会话查询缓存。

        `hit_count` 用**累加**而不是覆盖：它是"这条缓存被复用了几次"的计数器，
        与 `matched_place_ids` 这类会被最新一次刷新改写的结果字段语义不同。
        """

        timestamp = utcnow().isoformat()
        values = [
            (
                city,
                str(row.get("query_type") or "search_poi"),
                str(row["normalized_query"]),
                row.get("provider"),
                _json(list(row.get("matched_place_ids") or [])),
                _json(list(row.get("provider_result_ids") or [])),
                int(row.get("result_count") or 0),
                int(row.get("hit_count") or 0),
                row.get("fetched_at") or timestamp,
                row.get("expires_at"),
                row.get("updated_at") or timestamp,
            )
            for row in rows if row.get("normalized_query")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO poi_query_cache"
                " (city, query_type, normalized_query, provider, matched_place_ids_json,"
                "  provider_result_ids_json, result_count, hit_count, fetched_at, expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(city, query_type, normalized_query) DO UPDATE SET"
                " provider=excluded.provider,"
                " matched_place_ids_json=excluded.matched_place_ids_json,"
                " provider_result_ids_json=excluded.provider_result_ids_json,"
                " result_count=excluded.result_count,"
                " hit_count=poi_query_cache.hit_count + excluded.hit_count,"
                " fetched_at=excluded.fetched_at, expires_at=excluded.expires_at,"
                " updated_at=excluded.updated_at",
                values,
            )
        return len(values)

    def get_poi_query_cache_rows(self, city: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM poi_query_cache WHERE city=? ORDER BY normalized_query", (city,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_poi_query_cache(
        self, city: str, *, query_type: str, normalized_query: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM poi_query_cache WHERE city=? AND query_type=? AND normalized_query=?",
                (city, query_type, normalized_query),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_place_resolver_traces(self, rows: Iterable[dict[str, Any]]) -> int:
        """写入 Resolver 留痕（谁被复用 / 并进谁 / 为什么没查 Provider）。"""

        timestamp = utcnow().isoformat()
        values = [
            (
                str(row["trace_id"]),
                row.get("run_id"), row.get("session_id"), row.get("city"),
                str(row.get("operation") or "resolve"),
                str(row.get("action") or "unknown"),
                row.get("query"), row.get("canonical_place_id"),
                row.get("parent_place_id"), row.get("relation_type"),
                row.get("confidence"), row.get("reason"),
                _json(dict(row.get("signals") or {})),
                row.get("created_at") or timestamp,
            )
            for row in rows if row.get("trace_id")
        ]
        if not values:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO place_resolver_traces"
                " (trace_id, run_id, session_id, city, operation, action, query, canonical_place_id,"
                "  parent_place_id, relation_type, confidence, reason, signals_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def list_place_resolver_traces(
        self,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        city: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if city:
            clauses.append("city=?")
            params.append(city)
        if action:
            clauses.append("action=?")
            params.append(action)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM place_resolver_traces" + where + " ORDER BY created_at DESC LIMIT ?",
                [*params, max(1, min(int(limit), 1000))],
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_city_cache(self, city: str, *, include_canonical: bool = True) -> dict[str, int]:
        """**整城删除**城市缓存（不是按 TTL 清过期行）。

        与 `prune_city_cache` 的分工：后者只删各自 TTL 之外的行，清不掉"还新鲜但内容是
        错的"那些 —— 而错数据不会因为时间过去而变对（去重键是 (city, place_id)，一次新的
        预热只会再加一批子设施、顶不掉旧的）。重建那条路必须先整体清空，见
        `scripts/rebuild_city_cache.py`。

        `include_canonical=True` 时连 canonical 实体层一起清：实体层的输入就是这批脏 POI，
        留着它等于让重建读回旧身份，重建出来的还是同一份东西。
        **run 级 `places` / `place_evidence` 一律不动** —— 那是历史 run 的证据链。
        """

        removed: dict[str, int] = {}
        with self._connect() as conn:
            for table in ("city_pois", "city_poi_mentions", "city_evidences", "city_cache_meta"):
                cursor = conn.execute(f"DELETE FROM {table} WHERE city=?", (city,))
                removed[table] = int(cursor.rowcount or 0)
            if include_canonical:
                for table in (
                    "canonical_places",
                    "place_aliases",
                    "place_provider_refs",
                    "place_evidence_links",
                    "place_relations",
                    "poi_query_cache",
                ):
                    cursor = conn.execute(f"DELETE FROM {table} WHERE city=?", (city,))
                    removed[table] = int(cursor.rowcount or 0)
        return removed

    # ------------------------------------------------------------------
    # decision
    # ------------------------------------------------------------------

    def save_decision(self, run_id: str, decision: Decision) -> None:
        self.save_decisions_bulk(run_id, [decision])

    def save_decisions_bulk(self, run_id: str, decisions: Iterable[Decision]) -> None:
        rows = [
            (
                run_id,
                decision.agent_or_stage,
                decision.entity_id,
                decision.status.value if hasattr(decision.status, "value") else str(decision.status),
                _json(decision.reason_codes),
                decision.reason_text,
                _json(decision.scores),
                decision.timestamp.isoformat(),
            )
            for decision in decisions
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO decisions"
                " (run_id, stage, entity_id, decision, reason_codes_json, reason,"
                "  scores_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    # ------------------------------------------------------------------
    # plan
    # ------------------------------------------------------------------

    def save_plan(self, plan: TripPlan, plan_md: str = "") -> None:
        """存最终计划：整份 JSON + Markdown，并按天拆 item 便于前端/审计按天查询。"""
        plan_dict = plan.to_json_dict()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO plans (run_id, plan_json, plan_md, created_at)"
                " VALUES (?, ?, ?, ?)",
                (plan.run_id, _json(plan_dict), plan_md, utcnow().isoformat()),
            )
            conn.execute("DELETE FROM plan_items WHERE run_id=?", (plan.run_id,))
            rows = [
                (plan.run_id, day.day_index, item.id, _json(item.model_dump(mode="json")))
                for day in plan.days
                for item in day.items
            ]
            if rows:
                conn.executemany(
                    "INSERT INTO plan_items (run_id, day_index, item_id, item_json)"
                    " VALUES (?, ?, ?, ?)",
                    rows,
                )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            request = conn.execute(
                "SELECT * FROM trip_requests WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
            ).fetchone()
        if request is not None:
            result["trip_request"] = {
                **dict(request),
                "intent": _loads(request["intent_json"], {}),
            }
        return result

    def get_plan(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plans WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            items = conn.execute(
                "SELECT day_index, item_id, item_json FROM plan_items WHERE run_id=?"
                " ORDER BY day_index, id",
                (run_id,),
            ).fetchall()
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "plan": _loads(row["plan_json"], {}),
            "plan_md": row["plan_md"] or "",
            "items": [
                {
                    "day_index": item["day_index"],
                    "item_id": item["item_id"],
                    "item": _loads(item["item_json"], {}),
                }
                for item in items
            ],
        }

    def get_decisions(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "stage": row["stage"],
                "entity_id": row["entity_id"],
                "decision": row["decision"],
                "reason_codes": _loads(row["reason_codes_json"], []),
                "reason": row["reason"],
                "scores": _loads(row["scores_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_sources(self, run_id: str) -> list[dict]:
        """按 run 列出所有 Provider 调用（审计"搜了什么 / 谁返回的 / 状态如何"）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE run_id=? ORDER BY fetched_at, source_id", (run_id,)
            ).fetchall()
        return [
            {
                "source_id": row["source_id"],
                "provider": row["provider"],
                "source_type": row["source_type"],
                "source_url": row["source_url"],
                "query": _loads(row["query_json"], {}),
                "fetched_at": row["fetched_at"],
                "raw": _loads(row["raw_json"], {}),
                "normalized": _loads(row["normalized_json"], {}),
                "status": row["status"],
            }
            for row in rows
        ]

    def get_evidence(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE run_id=? ORDER BY fetched_at, evidence_id", (run_id,)
            ).fetchall()
        return [
            {
                "evidence_id": row["evidence_id"],
                "source_id": row["source_id"],
                "source_type": row["source_type"],
                "provider": row["provider"],
                "source_url": row["source_url"],
                "title": row["title"],
                "author": row["author"],
                "published_at": row["published_at"],
                "fetched_at": row["fetched_at"],
                "text": row["text"],
                "place_mentions": _loads(row["place_mentions_json"], []),
                "specific_dishes": _loads(row["specific_dishes_json"], []),
                "raw_metrics": _loads(row["raw_metrics_json"], {}),
            }
            for row in rows
        ]

    def get_places(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM places WHERE run_id=? ORDER BY place_id", (run_id,)
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "aliases_json"},
                "aliases": _loads(row["aliases_json"], []),
                "amap_verified": bool(row["amap_verified"]),
            }
            for row in rows
        ]

    def get_place_evidence_ids(self, run_id: str, place_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT evidence_id FROM place_evidence WHERE run_id=? AND place_id=?"
                " ORDER BY evidence_id",
                (run_id, place_id),
            ).fetchall()
        return [row["evidence_id"] for row in rows]

    # ------------------------------------------------------------------
    # badcase
    # ------------------------------------------------------------------

    #: 管理员可写的字段白名单。不给任意列名，避免把 UPDATE 拼成注入面。
    BADCASE_EDITABLE = (
        "analysis_status",
        "fixed_status",
        "root_cause_status",
        "suspected_root_cause",
        "introduced_in",
        "fixed_in",
        "severity",
    )

    def save_badcase(self, badcase: dict[str, Any]) -> str:
        """写入一条 Bad Case（按 badcase_id 幂等覆盖）。

        `badcase_id` 由检测规则按 (run_id, category, symptom) 决定，所以同一次 run
        重复检测不会产生重复行 —— 重跑一条 run 不应该把 Bad Case 越积越多。
        """
        badcase_id = str(badcase["badcase_id"])
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO badcases"
                " (badcase_id, run_id, category, severity, symptom, expected, actual,"
                "  suspected_root_cause, root_cause_status, detected_by, trace_refs_json,"
                "  analysis_status, fixed_status, introduced_in, fixed_in, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    badcase_id,
                    badcase["run_id"],
                    badcase["category"],
                    badcase.get("severity") or "medium",
                    badcase.get("symptom") or "",
                    badcase.get("expected"),
                    badcase.get("actual"),
                    badcase.get("suspected_root_cause"),
                    badcase.get("root_cause_status") or "suspected",
                    badcase.get("detected_by") or "rule",
                    _json(list(badcase.get("trace_refs") or [])),
                    badcase.get("analysis_status") or "pending",
                    badcase.get("fixed_status") or "unfixed",
                    badcase.get("introduced_in"),
                    badcase.get("fixed_in"),
                    badcase.get("created_at") or utcnow().isoformat(),
                ),
            )
        return badcase_id

    def save_badcases(self, badcases: Iterable[dict[str, Any]]) -> list[str]:
        return [self.save_badcase(badcase) for badcase in badcases]

    @staticmethod
    def _badcase_row(row: Any) -> dict[str, Any]:
        data = {key: row[key] for key in row.keys() if key != "trace_refs_json"}
        data["trace_refs"] = _loads(row["trace_refs_json"], [])
        return data

    def get_badcase(self, badcase_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM badcases WHERE badcase_id=?", (badcase_id,)).fetchone()
        return self._badcase_row(row) if row is not None else None

    def list_badcases(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        analysis_status: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        run_id: str | None = None,
    ) -> tuple[list[dict], int]:
        """返回 (当前页, 命中总数)。管理端要同时显示列表与总数，所以一次给全。"""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("analysis_status", analysis_status),
            ("category", category),
            ("severity", severity),
            ("run_id", run_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM badcases{where}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM badcases{where} ORDER BY created_at DESC, badcase_id"
                " LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 500)), max(0, offset)],
            ).fetchall()
        return [self._badcase_row(row) for row in rows], int(total)

    def badcase_facets(self) -> dict[str, dict[str, int]]:
        """给管理端筛选器用的计数（category / severity / analysis_status）。"""
        facets: dict[str, dict[str, int]] = {"category": {}, "severity": {}, "analysis_status": {}}
        with self._connect() as conn:
            for field in facets:
                rows = conn.execute(
                    f"SELECT {field} AS value, COUNT(*) AS n FROM badcases GROUP BY {field}"
                ).fetchall()
                facets[field] = {str(row["value"]): int(row["n"]) for row in rows}
        return facets

    def count_badcases(self, *, run_id: str | None = None, analysis_status: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if analysis_status:
            clauses.append("analysis_status=?")
            params.append(analysis_status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) AS n FROM badcases{where}", params).fetchone()["n"])

    def update_badcase(self, badcase_id: str, fields: dict[str, Any]) -> dict | None:
        """只允许白名单字段；未知字段直接忽略而不是拼进 SQL。"""
        updates = {key: value for key, value in fields.items() if key in self.BADCASE_EDITABLE and value is not None}
        if updates:
            assignments = ", ".join(f"{key}=?" for key in updates)
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE badcases SET {assignments} WHERE badcase_id=?",
                    [*updates.values(), badcase_id],
                )
        return self.get_badcase(badcase_id)

    # ------------------------------------------------------------------
    # benchmark
    # ------------------------------------------------------------------

    def save_benchmark_run(self, run: dict[str, Any]) -> str:
        """插入或更新一条 Benchmark 运行（创建时只给头部字段，结束时补指标）。"""
        fields = (
            "benchmark_run_id", "suite", "version", "travelplan_commit", "superharness_commit",
            "model", "fixture_version", "config_json", "jev_enabled", "live", "status",
            "started_at", "finished_at", "case_count", "passed", "failed", "metrics_json", "notes",
        )
        values = [_json(run.get(field) or {}) if field == "config_json" else run.get(field) for field in fields]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO benchmark_runs"
                " (benchmark_run_id, suite, version, travelplan_commit, superharness_commit, model,"
                "  fixture_version, config_json, jev_enabled, live, status, started_at, finished_at,"
                "  case_count, passed, failed, metrics_json, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return str(run["benchmark_run_id"])

    def update_benchmark_run(self, benchmark_run_id: str, fields: dict[str, Any]) -> None:
        allowed = ("status", "finished_at", "case_count", "passed", "failed", "notes")
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        if fields.get("metrics") is not None:
            updates["metrics_json"] = _json(fields["metrics"])
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE benchmark_runs SET {assignments} WHERE benchmark_run_id=?",
                [*updates.values(), benchmark_run_id],
            )

    def save_benchmark_case_result(self, result: dict[str, Any]) -> str:
        result_id = str(result.get("result_id") or f"{result['benchmark_run_id']}:{result['case_id']}")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO benchmark_case_results"
                " (result_id, benchmark_run_id, case_id, suite, title, status, metrics_json,"
                "  detail_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result_id,
                    result["benchmark_run_id"],
                    result["case_id"],
                    result.get("suite") or "",
                    result.get("title"),
                    result.get("status") or "fail",
                    _json(result.get("metrics") or {}),
                    _json(result.get("detail") or {}),
                    result.get("created_at") or utcnow().isoformat(),
                ),
            )
        return result_id

    @staticmethod
    def _benchmark_run_row(row: Any) -> dict[str, Any]:
        data = {key: row[key] for key in row.keys() if key not in ("config_json", "metrics_json")}
        data["config"] = _loads(row["config_json"], {})
        data["metrics"] = _loads(row["metrics_json"], {})
        data["jev_enabled"] = bool(row["jev_enabled"])
        data["live"] = bool(row["live"])
        return data

    def get_benchmark_run(self, benchmark_run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM benchmark_runs WHERE benchmark_run_id=?", (benchmark_run_id,)
            ).fetchone()
        return self._benchmark_run_row(row) if row is not None else None

    def list_benchmark_runs(self, *, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM benchmark_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._benchmark_run_row(row) for row in rows]

    def get_benchmark_case_results(self, benchmark_run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM benchmark_case_results WHERE benchmark_run_id=?"
                " ORDER BY suite, case_id",
                (benchmark_run_id,),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key not in ("metrics_json", "detail_json")},
                "metrics": _loads(row["metrics_json"], {}),
                "detail": _loads(row["detail_json"], {}),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # evolution / experience
    # ------------------------------------------------------------------

    def save_evolution_run(self, run: dict[str, Any]) -> str:
        evolution_run_id = str(run["evolution_run_id"])
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evolution_runs"
                " (evolution_run_id, status, badcase_count, experience_count, decision, summary,"
                "  steps_json, before_metrics_json, after_metrics_json, created_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evolution_run_id,
                    run.get("status") or "RUNNING",
                    int(run.get("badcase_count") or 0),
                    int(run.get("experience_count") or 0),
                    run.get("decision"),
                    run.get("summary"),
                    _json(run.get("steps") or []),
                    _json(run.get("before_metrics") or {}),
                    _json(run.get("after_metrics") or {}),
                    run.get("created_at") or utcnow().isoformat(),
                    run.get("finished_at"),
                ),
            )
        return evolution_run_id

    def update_evolution_run(self, evolution_run_id: str, fields: dict[str, Any]) -> None:
        """结束时补齐结论。steps/decision 允许显式写成空值，所以只过滤 None 以外的缺失键。"""
        allowed = ("status", "badcase_count", "experience_count", "decision", "summary", "finished_at")
        updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
        for key, column in (
            ("steps", "steps_json"),
            ("before_metrics", "before_metrics_json"),
            ("after_metrics", "after_metrics_json"),
        ):
            if fields.get(key) is not None:
                updates[column] = _json(fields[key])
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE evolution_runs SET {assignments} WHERE evolution_run_id=?",
                [*updates.values(), evolution_run_id],
            )

    @staticmethod
    def _evolution_row(row: Any) -> dict[str, Any]:
        data = {
            key: row[key]
            for key in row.keys()
            if key not in ("steps_json", "before_metrics_json", "after_metrics_json")
        }
        data["steps"] = _loads(row["steps_json"], [])
        data["before_metrics"] = _loads(row["before_metrics_json"], {})
        data["after_metrics"] = _loads(row["after_metrics_json"], {})
        return data

    def get_evolution_run(self, evolution_run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_runs WHERE evolution_run_id=?", (evolution_run_id,)
            ).fetchone()
        return self._evolution_row(row) if row is not None else None

    def list_evolution_runs(self, *, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._evolution_row(row) for row in rows]

    def save_experience(self, experience: dict[str, Any]) -> str:
        experience_id = str(experience["experience_id"])
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO experiences"
                " (experience_id, evolution_run_id, source_badcases_json, failure_pattern,"
                "  verified_root_cause, affected_module, recommended_change, before_metrics_json,"
                "  after_metrics_json, decision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experience_id,
                    experience.get("evolution_run_id"),
                    _json(experience.get("source_badcases") or []),
                    experience.get("failure_pattern") or "",
                    experience.get("verified_root_cause"),
                    experience.get("affected_module"),
                    experience.get("recommended_change"),
                    _json(experience.get("before_metrics") or {}),
                    _json(experience.get("after_metrics") or {}),
                    experience.get("decision"),
                    experience.get("created_at") or utcnow().isoformat(),
                ),
            )
        return experience_id

    def list_experiences(self, *, evolution_run_id: str | None = None, limit: int = 100) -> list[dict]:
        where = " WHERE evolution_run_id=?" if evolution_run_id else ""
        params: list[Any] = [evolution_run_id] if evolution_run_id else []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiences" + where + " ORDER BY created_at DESC LIMIT ?",
                [*params, max(1, min(limit, 500))],
            ).fetchall()
        return [
            {
                **{
                    key: row[key]
                    for key in row.keys()
                    if key not in ("source_badcases_json", "before_metrics_json", "after_metrics_json")
                },
                "source_badcases": _loads(row["source_badcases_json"], []),
                "before_metrics": _loads(row["before_metrics_json"], {}),
                "after_metrics": _loads(row["after_metrics_json"], {}),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # planning session（引导式旅程的"规划前会话"）
    # ------------------------------------------------------------------

    def save_planning_session(self, session: dict[str, Any]) -> str:
        """写入初始/导入快照；运行中的会话必须走 update_planning_session。

        不能用读到的旧快照替换正在被用户、Discovery 和启动请求共同修改的行。
        """

        session_id = str(session["session_id"])
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO planning_sessions"
                " (session_id, status, discovery_status, created_at, updated_at, expires_at,"
                "  basic_intent_json, preferences_json, poi_selections_json, transport_json,"
                "  hotels_json, places_json, evidence_summary_json, degradations_json,"
                "  events_json, discovery_json, prefetch_json, error, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    session.get("status") or "COLLECTING",
                    session.get("discovery_status") or "PENDING",
                    session.get("created_at") or utcnow().isoformat(),
                    session.get("updated_at") or utcnow().isoformat(),
                    session.get("expires_at") or utcnow().isoformat(),
                    _json(session.get("basic_intent") or {}),
                    _json(session.get("preferences") or {}),
                    _json(session.get("poi_selections") or {}),
                    _json(session.get("transport_candidates") or []),
                    _json(session.get("hotel_candidates") or []),
                    _json(session.get("place_candidates") or []),
                    _json(session.get("evidence_summary") or {}),
                    _json(session.get("degradations") or []),
                    _json(session.get("events") or []),
                    _json(session.get("discovery") or {}),
                    _json(session.get("prefetch") or {}),
                    session.get("error"),
                    session.get("run_id"),
                ),
            )
        return session_id

    @contextmanager
    def _planning_session_transaction(self, session_id: str) -> Iterator[tuple[dict | None, Any]]:
        """锁定后读取最新草稿，仅保存变化字段；事务内禁止 Provider/等待操作。

        首条 no-op UPDATE 在 SQLite 获取写锁，在 Postgres 获取行锁，所以后续
        读改写跨线程/进程都串行，不依赖进程内锁或 SQLite 专属 BEGIN IMMEDIATE。
        """

        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE planning_sessions SET session_id=session_id WHERE session_id=?",
                    (session_id,),
                )
                row = conn.execute(
                    "SELECT * FROM planning_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                if row is None:
                    yield None, conn
                    return
                session = self._session_row(row)
                stamp = utcnow().isoformat()
                if session["status"] not in ("STARTING", "CANCELLED", "EXPIRED") and session["expires_at"] < stamp:
                    session["status"] = "EXPIRED"
                if session["status"] == "EXPIRED" and not any(
                    event.get("event") == "session_expired" for event in session["events"]
                ):
                    session["events"].append({
                        "event": "session_expired", "at": stamp,
                        "detail": f"超过 TTL 未开始规划（{session['expires_at']}）",
                    })
                yield session, conn
                # 列名固定映射，调用方不能把任意 payload key 拼进 SQL。
                json_columns = {
                    "basic_intent": "basic_intent_json", "preferences": "preferences_json",
                    "poi_selections": "poi_selections_json", "transport_candidates": "transport_json",
                    "hotel_candidates": "hotels_json", "place_candidates": "places_json",
                    "evidence_summary": "evidence_summary_json", "degradations": "degradations_json",
                    "events": "events_json", "discovery": "discovery_json", "prefetch": "prefetch_json",
                }
                values = {column: _json(session[key]) for key, column in json_columns.items()}
                values.update({key: session.get(key) for key in ("status", "discovery_status", "error", "run_id")})
                changed = {key: value for key, value in values.items() if value != row[key]}
                if changed:
                    session["updated_at"] = utcnow().isoformat()
                    changed["updated_at"] = session["updated_at"]
                    conn.execute(
                        "UPDATE planning_sessions SET " + ", ".join(f"{key}=?" for key in changed)
                        + " WHERE session_id=?", (*changed.values(), session_id),
                    )
        finally:
            # Postgres 上下文已归还池；原生 SQLite 上下文只 commit，不会 close。
            if isinstance(self._backend, SqliteBackend):
                conn.close()

    def update_planning_session(
        self, session_id: str, update: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any] | None:
        """update 只修改锁内读到的最新行；异常将回滚，不得嵌套访问 store。"""

        with self._planning_session_transaction(session_id) as (session, _conn):
            if session is not None:
                update(session)
        return session

    def start_planning_session_run(
        self, session_id: str, run_id: str, confirm: Callable[[dict[str, Any]], str]
    ) -> tuple[dict[str, Any] | None, bool]:
        """占用草稿、创建 Run 和进度在同一事务；仅赢家可提交后台任务。"""

        created = False
        with self._planning_session_transaction(session_id) as (session, conn):
            if session is not None and not session.get("run_id") and session["status"] in (
                "COLLECTING", "DISCOVERING", "READY"
            ):
                query = confirm(session)
                stamp = utcnow().isoformat()
                conn.execute(
                    "INSERT INTO runs (run_id, user_id, created_at, status, original_query, source, source_session_id)"
                    " VALUES (?, NULL, ?, 'running', ?, 'guided', ?)",
                    (run_id, stamp, query, session_id),
                )
                conn.execute(
                    "INSERT INTO run_progress"
                    " (run_id, status, current_stage, message, started_at, updated_at, finished_at, error)"
                    " VALUES (?, 'RUNNING', NULL, ?, ?, ?, NULL, NULL)",
                    (run_id, "规划任务已创建", stamp, stamp),
                )
                session["status"] = "STARTING"
                session["run_id"] = run_id
                created = True
        return session, created

    @staticmethod
    def _session_row(row: Any) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "status": row["status"],
            "discovery_status": row["discovery_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "basic_intent": _loads(row["basic_intent_json"], {}),
            "preferences": _loads(row["preferences_json"], {}),
            "poi_selections": _loads(row["poi_selections_json"], {}),
            "transport_candidates": _loads(row["transport_json"], []),
            "hotel_candidates": _loads(row["hotels_json"], []),
            "place_candidates": _loads(row["places_json"], []),
            "evidence_summary": _loads(row["evidence_summary_json"], {}),
            "degradations": _loads(row["degradations_json"], []),
            "events": _loads(row["events_json"], []),
            "discovery": _loads(row["discovery_json"], {}),
            "prefetch": _loads(row["prefetch_json"], {}),
            "error": row["error"],
            "run_id": row["run_id"],
        }

    def get_planning_session(self, session_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM planning_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return self._session_row(row) if row is not None else None

    def list_planning_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        status: str | None = None,
        destination: str | None = None,
        has_run: bool | None = None,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if q:
            clauses.append("(session_id LIKE ? OR COALESCE(run_id,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if status:
            clauses.append("status=?")
            params.append(status)
        if destination:
            clauses.append("basic_intent_json LIKE ?")
            params.append(f"%{destination}%")
        if has_run is True:
            clauses.append("run_id IS NOT NULL")
        elif has_run is False:
            clauses.append("run_id IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM planning_sessions" + where, params
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM planning_sessions"
                + where
                + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 200)), max(0, offset)],
            ).fetchall()
        return [self._session_row(row) for row in rows], int(total)

    def delete_planning_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM planning_sessions WHERE session_id=?", (session_id,))

    def expire_planning_sessions(self, *, now: str | None = None) -> int:
        """把过期会话标成 EXPIRED。

        不删除：管理端仍要能看到"这个用户开了会话但没走到规划"，删掉就失去了诊断线索。
        """

        stamp = now or utcnow().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE planning_sessions SET status='EXPIRED', updated_at=?"
                " WHERE expires_at < ? AND status NOT IN ('EXPIRED','CANCELLED','STARTING')",
                (stamp, stamp),
            )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------
    # Provider 调用账本（Provider Health 的数据源）
    # ------------------------------------------------------------------

    def save_provider_call(self, call: dict[str, Any]) -> str:
        """记一次 Provider 调用。`call_id` 由调用方给（要能幂等重放）。"""

        call_id = str(call.get("call_id") or "")
        if not call_id:
            call_id = f"{call.get('provider')}-{call.get('tool')}-{call.get('fetched_at')}-{utcnow().timestamp()}"
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_calls"
                " (call_id, provider, tool, status, source_type, source_id, run_id, session_id,"
                "  fetched_at, duration_ms, returned, error, fallback, query_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    str(call.get("provider") or "unknown"),
                    str(call.get("tool") or "unknown"),
                    str(call.get("status") or "UNKNOWN"),
                    str(call.get("source_type") or "run"),
                    call.get("source_id"),
                    call.get("run_id"),
                    call.get("session_id"),
                    str(call.get("fetched_at") or utcnow().isoformat()),
                    call.get("duration_ms"),
                    call.get("returned"),
                    call.get("error"),
                    1 if call.get("fallback") else 0,
                    _json(_scrub_query(call.get("query"))),
                ),
            )
        return call_id

    def save_provider_calls(self, calls: Iterable[dict[str, Any]]) -> int:
        count = 0
        for call in calls:
            self.save_provider_call(call)
            count += 1
        return count

    def list_provider_calls(
        self,
        *,
        provider: str | None = None,
        source_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if source_type:
            clauses.append("source_type=?")
            params.append(source_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM provider_calls"
                + where
                + " ORDER BY fetched_at DESC, call_id LIMIT ?",
                [*params, max(1, min(limit, 1000))],
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "query_json"},
                "query": _loads(row["query_json"], {}),
                "fallback": bool(row["fallback"]),
            }
            for row in rows
        ]

    def provider_names(self) -> list[str]:
        """出现过的 Provider（页面必须能自动扩展，不能写死一份清单）。"""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT provider FROM provider_calls ORDER BY provider"
            ).fetchall()
        return [str(row["provider"]) for row in rows]

    def provider_call_stats(self, *, limit_per_provider: int = 200) -> list[dict[str, Any]]:
        """按 Provider 聚合真实调用数据。

        `limit_per_provider` 是"看最近多少次调用"，不是时间窗：样本量固定，
        才能让成功率与 P95 在不同 Provider 之间可比（调用量差异极大）。
        """

        stats: list[dict[str, Any]] = []
        for provider in self.provider_names():
            calls = self.list_provider_calls(provider=provider, limit=limit_per_provider)
            successes = [call for call in calls if call["status"] == "OK"]
            failures = [call for call in calls if call["status"] != "OK"]
            latencies = sorted(
                int(call["duration_ms"])
                for call in calls
                if isinstance(call.get("duration_ms"), int)
            )
            stats.append(
                {
                    "provider": provider,
                    "calls": len(calls),
                    "successes": len(successes),
                    "failures": len(failures),
                    "timeouts": sum(1 for call in calls if call["status"] == "TIMEOUT"),
                    "auth_errors": sum(1 for call in calls if call["status"] == "AUTH_ERROR"),
                    "rate_limited": sum(1 for call in calls if call["status"] == "RATE_LIMIT"),
                    "empty": sum(1 for call in calls if call["status"] == "EMPTY"),
                    "fallback_count": sum(1 for call in calls if call["fallback"]),
                    "last_call_at": calls[0]["fetched_at"] if calls else None,
                    "last_success_at": successes[0]["fetched_at"] if successes else None,
                    "last_failure_at": failures[0]["fetched_at"] if failures else None,
                    "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
                    "p95_latency_ms": _percentile(latencies, 0.95),
                    "tools": sorted({str(call["tool"]) for call in calls}),
                    "sources": sorted({str(call["source_type"]) for call in calls}),
                    "last_error": failures[0]["error"] if failures and failures[0].get("error") else None,
                    "last_status": calls[0]["status"] if calls else None,
                }
            )
        return stats

    def count_planning_sessions(
        self, *, status: str | None = None, has_run: bool | None = None
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if has_run is True:
            clauses.append("run_id IS NOT NULL")
        elif has_run is False:
            clauses.append("run_id IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM planning_sessions" + where, params
                ).fetchone()["n"]
            )

    # ------------------------------------------------------------------
    # runtime config（管理端可编辑的非 Secret 配置）
    # ------------------------------------------------------------------

    def set_runtime_config(self, key: str, value: Any, *, updated_by: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runtime_config (key, value_json, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?)",
                (key, _json({"value": value}), utcnow().isoformat(), updated_by),
            )

    def delete_runtime_config(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM runtime_config WHERE key=?", (key,))

    def list_runtime_config(self) -> dict[str, dict[str, Any]]:
        """返回 `{key: {value, updated_at, updated_by}}`。"""

        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runtime_config ORDER BY key").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = _loads(row["value_json"], {})
            result[row["key"]] = {
                "value": payload.get("value") if isinstance(payload, dict) else payload,
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
            }
        return result
