"use client";

import { Check, Clock3, Database, Hotel, MapPin, Search, UtensilsCrossed } from "lucide-react";
import { cn } from "@/lib/utils";
import type { HotelArea, SessionView } from "@/types/session";

interface DiscoveryResearchProps {
  session: SessionView | null;
  settled: boolean;
}

/**
 * Discovery 的可见研究进度（F4 / B3）。所有数量只读 Session 实际返回，
 * 缺失就显示进行中，不用前端倒计时或虚构结果数填充。
 */
export function DiscoveryResearch({ session, settled }: DiscoveryResearchProps) {
  if (!session) return null;

  const places = session.poi_pools.attraction.length || session.place_candidates.filter((item) => item.category !== "food").length;
  const food = session.poi_pools.food.length || session.place_candidates.filter((item) => item.category === "food").length;
  const facts = [
    { key: "transport", label: session.transport_candidates.length ? `找到 ${session.transport_candidates.length} 个交通候选` : "正在查询交通", done: session.transport_candidates.length > 0, icon: Search },
    { key: "hotel", label: session.hotel_candidates.length ? `找到 ${session.hotel_candidates.length} 家酒店候选` : "正在比较酒店", done: session.hotel_candidates.length > 0, icon: Hotel },
    { key: "guide", label: guideLabel(session), done: hasEvent(session, "social"), icon: Database },
    { key: "places", label: places ? `发现 ${places} 个景点和体验` : "正在归类景点和体验", done: places > 0, icon: MapPin },
    { key: "food", label: food ? `发现 ${food} 个美食候选` : "正在匹配当地美食", done: food > 0, icon: UtensilsCrossed },
    { key: "area", label: session.hotel_areas.length ? `找到 ${session.hotel_areas.length} 个推荐住宿区域` : "正在整理住宿区域", done: session.hotel_areas.length > 0, icon: Hotel },
  ];

  return (
    <section className="rounded-xl border border-border bg-muted/25 px-3.5 py-3.5 sm:px-4" aria-label="正在研究这趟旅行">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-foreground">正在研究这趟旅行</p>
          <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {settled ? "探索结果已就绪，下面可以确认你更想要的安排。" : "你可以继续填写偏好；已找到的结果会逐步出现在这里。"}
          </p>
        </div>
        {!settled ? <Clock3 className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden /> : null}
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
          已按你的酒店策略排序；正式规划会在这些区域内比较可订酒店。
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

function guideLabel(session: SessionView): string {
  const event = session.events.find((item) => item.event === "social");
  return event?.detail ?? "正在阅读旅行攻略";
}

function hasEvent(session: SessionView, event: string): boolean {
  return session.events.some((item) => item.event === event);
}

function ageLabel(value: string): string | null {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  const hours = Math.max(0, Math.floor((Date.now() - timestamp) / 3_600_000));
  if (hours < 24) return `${Math.max(1, hours)} 小时`;
  return `${Math.floor(hours / 24)} 天`;
}
