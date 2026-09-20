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
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.models import Decision, Evidence, Place, TripIntent, TripPlan, utcnow

#: 默认库位置：项目根 /data/travelplan.db（PRD §17）。
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "travelplan.db"


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

CREATE INDEX IF NOT EXISTS idx_sources_run ON sources(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_places_run ON places(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_plan_items_run ON plan_items(run_id, day_index);
"""


class TravelPlanStore:
    """按 run 归档业务证据的 SQLite 封装。

    每次操作开一个短连接（`with self._connect()`）：这个库的写入量是"一次 run 几十行"，
    连接池带来的复杂度远大于收益，而短连接天然不会把连接泄露到下一次 run。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # 外键约束打开：evidence → sources、place_evidence → places 的引用完整性
        # 靠数据库保证，而不是靠调用方自觉。
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate_places_scope(self, conn: sqlite3.Connection) -> None:
        """把旧的「全局 place_id 主键」表迁到「按 run 建档」的新表。

        `CREATE TABLE IF NOT EXISTS` 不会改已存在的表，所以老库必须显式迁一次 ——
        否则它继续用旧主键跑，第二次 run 照样顶掉第一次的 place 行。
        迁移不丢数据：旧行原样搬过去，只有「跨 run 的 place_evidence」被丢掉，
        而那种链接本来就是错的（它把 A run 的证据挂到了 B run 的 place 上）。
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='places'"
        ).fetchone()
        if row is None or "PRIMARY KEY (run_id, place_id)" in (row["sql"] or ""):
            return

        conn.execute("ALTER TABLE places RENAME TO places_legacy_v1")
        conn.execute("ALTER TABLE place_evidence RENAME TO place_evidence_legacy_v1")
        # 索引名在库里是全局唯一的：rename 之后 idx_places_run 还挂在旧表上，
        # 不先删掉的话 SCHEMA 里的 CREATE INDEX IF NOT EXISTS 会因为「名字已被占用」
        # 直接跳过，新表就永远没有索引（旧表被 DROP 时索引也跟着没了）。
        conn.execute("DROP INDEX IF EXISTS idx_places_run")
        conn.executescript(SCHEMA)
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
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, user_id: str | None = None, original_query: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, user_id, created_at, status, original_query)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, user_id, utcnow().isoformat(), "running", original_query),
            )

    def finish_run(self, run_id: str, status: str = "completed") -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))

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
