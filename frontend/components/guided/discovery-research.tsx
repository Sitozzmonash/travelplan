"use client";

import { Check, Clock3, Database, Hotel, MapPin, Search, Sparkles, UtensilsCrossed } from "lucide-react";
import { isDiscoverySettled, isSessionUnusable } from "@/lib/sessions";
import { cn } from "@/lib/utils";
import type { DiscoveryStage, HotelArea, SessionView } from "@/types/session";
import { discoveryPlaces, isFoodCategory } from "./options";

interface DiscoveryResearchProps {
  session: SessionView | null;
  settled: boolean;
}

/** 只呈现真实阶段和推荐结果；缺失状态不冒充进行中或已完成。 */
export function DiscoveryResearch({ session, settled }: DiscoveryResearchProps) {
  if (!session) return null;

  const stopped = settled || isDiscoverySettled(session) || isSessionUnusable(session);
  const candidates = discoveryPlaces(session);
  const food = candidates.filter((item) => isFoodCategory(item.category)).length;
  const places = candidates.length - food;
  const failed = session.recommendation?.status === "FAILED" || session.discovery_status === "FAILED";
  const title = isSessionUnusable(session) ? "探索已停止" : stopped ? failed ? "推荐生成失败" : "探索结果" : "正在研究这趟旅行";
  const facts = [
    { key: "guide", ...stageFact("数据库攻略", discoveryStage(session, ["database", "database_guides", "city_guides", "recall_city_guides", "social"], ["database_guides", "recall_city_guides", "social_discovery", "social"]), stopped), icon: Database },
    { key: "web", ...stageFact("Web 补充", discoveryStage(session, ["web", "web_guides", "search_web_guides"], ["web_supplement", "search_web_guides", "web_discovery", "web"]), stopped), icon: Search },
    { key: "recommendation", ...recommendationFact(session, candidates.length, stopped), icon: Sparkles },
    { key: "places", label: places ? `推荐 ${places} 个景点和体验` : stopped ? "暂无推荐景点和体验" : "尚无推荐景点和体验", done: places > 0, icon: MapPin },
    { key: "food", label: food ? `推荐 ${food} 个美食地点` : stopped ? "暂无推荐美食" : "尚无推荐美食", done: food > 0, icon: UtensilsCrossed },
    { key: "area", label: session.hotel_areas.length ? `推荐 ${session.hotel_areas.length} 个住宿区域` : "暂无推荐住宿区域", done: session.hotel_areas.length > 0, icon: Hotel },
  ];

  return (
    <section className="rounded-xl border border-border bg-muted/25 px-3.5 py-3.5 sm:px-4" aria-label={title}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground">{title}</p>
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {stopped
              ? candidates.length && !failed ? "可确认下面的推荐；交通班次与酒店产品将在正式规划时查询。" : "本次暂无可用推荐。可以先填写偏好，再返回重试探索。"
              : "你可以先填写偏好；推荐就绪后再正式开始。交通和酒店产品届时查询。"}
          </p>
        </div>
        {!stopped ? <Clock3 className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden /> : null}
      </div>
      <ul className="mt-3 grid gap-2 sm:grid-cols-2">
        {facts.map(({ key, label, done, icon: Icon }) => (
          <li key={key} className="flex min-w-0 items-center gap-2 text-xs">
            <span
              className={cn(
                "flex size-5 shrink-0 items-center justify-center rounded-full",
                done ? "bg-success-subtle text-success-subtle-foreground" : "bg-muted text-muted-foreground",
              )}
            >
              {done ? <Check className="size-3" aria-hidden /> : <Icon className="size-3" aria-hidden />}
            </span>
            <span className={cn("truncate", done ? "text-foreground/85" : "text-muted-foreground")}>{label}</span>
          </li>
        ))}
      </ul>
      <FreshnessNotice session={session} />
    </section>
  );
}

/** 契约 3：只在后端明确给到 source + 合法 ISO 时间时说明缓存新鲜度。 */
export function FreshnessNotice({ session }: { session: SessionView }) {
  if (!session.source || !session.updated_at) return null;
  const age = ageLabel(session.updated_at);
  if (!age) return null;
  const city = session.basic_intent?.destination ?? "目的地";
  const message =
    session.source === "city_cache"
      ? `地点信息来自 ${age}前整理的${city}攻略库；本次发现的增量会在正式规划时继续核对。`
      : `地点信息来自本次实时查询（${age}前更新）。`;
  return <p className="mt-3 border-t border-border/70 pt-2.5 text-[11px] leading-5 text-muted-foreground">{message}</p>;
}

/**
 * 契约 2 的区域候选。现在后端尚未提供“锁定区域”的 PATCH 字段，因此只如实展示
 * 研究结论；不能做一个看似可选、实际不会写进正式 Run 的假按钮。
 */
export function HotelAreaRecommendations({ areas }: { areas: HotelArea[] }) {
  if (!areas.length) return null;
  return (
    <section className="grid gap-2.5" aria-labelledby="hotel-area-heading">
      <div>
        <h3 id="hotel-area-heading" className="text-sm font-medium text-foreground">推荐住宿区域</h3>
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
          根据攻略与游玩范围推荐；正式规划时再结合酒店偏好比较可订酒店。
        </p>
      </div>
      <div className="grid gap-2.5 sm:grid-cols-3">
        {areas.map((area, index) => (
          <article
            key={area.key}
            className={cn(
              "rounded-xl border px-3 py-3",
              index === 0 ? "border-primary/35 bg-accent/35" : "border-border bg-card",
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <h4 className="text-sm font-medium text-foreground">{area.name}</h4>
              {index === 0 ? <span className="shrink-0 text-[11px] text-primary">优先推荐</span> : null}
            </div>
            {area.reason ? <p className="mt-1.5 line-clamp-2 text-[11px] leading-5 text-muted-foreground">{area.reason}</p> : null}
            {area.tags.length ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {area.tags.slice(0, 3).map((tag) => (
                  <span key={tag} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{tag}</span>
                ))}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function discoveryStage(session: SessionView, keys: string[], events: string[]): DiscoveryStage | undefined {
  for (const key of keys) {
    if (session.discovery?.[key]) return session.discovery[key];
  }
  const event = session.events.slice().reverse().find((item) => events.some((name) =>
    item.event === name || item.event === `${name}_started` || item.event === `${name}_finished` || item.event === `${name}_failed`,
  ));
  if (!event) return undefined;
  const status = event.detail?.match(/status=([A-Z_]+)/i)?.[1]?.toUpperCase();
  const count = event.detail?.match(/(?:结果\s*|已阅读\s*)(\d+)/)?.[1];
  return {
    status: status ?? (event.event?.endsWith("_failed") ? "FAILED" : event.event?.endsWith("_started") ? "RUNNING" : "READY"),
    result_count: count === undefined ? null : Number(count),
  };
}

function stageFact(label: string, stage: DiscoveryStage | undefined, stopped: boolean): { label: string; done: boolean } {
  const status = stage?.status?.toUpperCase();
  const count = stage?.result_count;
  if (status === "FAILED" || status === "ERROR" || status === "TIMEOUT") return { label: `${label}：未成功获取结果`, done: false };
  if (status === "SKIPPED" || status === "CANCELLED") return { label: `${label}：本次未执行或已停止`, done: false };
  if (status === "EMPTY" || status === "NO_RESULTS") return { label: `${label}：无结果`, done: false };
  if (status && ["READY", "SUCCESS", "OK", "COMPLETED", "PARTIAL", "CACHE", "STALE_CACHE"].includes(status)) {
    const result = count === 0 ? "无结果" : typeof count === "number" && count > 0 ? `${count} 条结果` : "结果数量未提供";
    return { label: `${label}：${status === "PARTIAL" ? "部分返回" : "检索结束"}，${result}`, done: typeof count === "number" && count > 0 };
  }
  if (stopped) return { label: `${label}：已结束，未返回阶段结果`, done: false };
  return { label: `${label}：${status === "RUNNING" || status === "DISCOVERING" ? "正在处理" : status === "PENDING" ? "等待处理" : "等待阶段状态"}`, done: false };
}

function recommendationFact(session: SessionView, count: number, stopped: boolean): { label: string; done: boolean } {
  const recommendation = session.recommendation;
  if (recommendation?.status === "FAILED" || session.discovery_status === "FAILED") return { label: "推荐生成：失败，请重试", done: false };
  if (recommendation?.status === "READY" || recommendation?.status === "PARTIAL") {
    const source = recommendation.source === "evidence_fallback" ? "证据降级推荐" : recommendation.source === "llm" ? "模型推荐" : "推荐";
    return { label: count ? `${source}：${count} 个地点${recommendation.status === "PARTIAL" ? "（部分可用）" : ""}` : "推荐生成：无可用地点", done: count > 0 };
  }
  if (stopped) return { label: count ? `已有 ${count} 个可选地点` : "推荐生成：无可用地点", done: count > 0 };
  const stage = discoveryStage(session, ["recommendation"], ["recommendation"]);
  return stageFact("推荐生成", stage ?? (recommendation ? { status: recommendation.status } : undefined), false);
}

function ageLabel(value: string): string | null {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  const hours = Math.max(0, Math.floor((Date.now() - timestamp) / 3_600_000));
  if (hours < 24) return `${Math.max(1, hours)} 小时`;
  return `${Math.floor(hours / 24)} 天`;
}
