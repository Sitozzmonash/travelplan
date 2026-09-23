"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BedDouble,
  Clock,
  Hourglass,
  Info,
  LoaderCircle,
  MapPin,
  MapPinned,
  Route,
} from "lucide-react";
import type { MapPoint } from "@/types/plan";
import { cn } from "@/lib/utils";
import { formatDistance, formatDuration } from "@/lib/format";
import {
  loadAmap,
  type AMapMapInstance,
  type AMapMarkerInstance,
  type AMapNamespace,
} from "@/lib/amap-loader";

/** 地图选中地点的旅行信息（feedback §11：弹窗展示这些，不再显示经纬度）。 */
interface TravelMapSelectedDetail {
  name: string;
  order: number;
  startTime?: string | null;
  stayMinutes?: number | null;
  distanceFromPreviousMeters?: number | null;
  area?: string | null;
}

interface TravelMapProps {
  points: MapPoint[];
  dayLabel: string;
  areaLabel: string;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  /** 住宿不属于当天的路线编号，但要作为动线的固定参照点显示。 */
  hotel?: { name: string; lat: number; lng: number } | null;
  totalDistanceMeters?: number | null;
  totalMinutes?: number | null;
  heightClassName?: string;
  className?: string;
  /** 选中地点的旅行信息；缺失时（旧调用方）弹窗退化为只显示名称与编号。 */
  selectedDetail?: TravelMapSelectedDetail | null;
}

/**
 * 地图面板（FRONTEND_DESIGN §14 / feedback §4）。
 *
 * 配了 `NEXT_PUBLIC_AMAP_KEY` 时用真实高德 JS SDK v2（真实道路 / 路线 / 点位编号 /
 * 住宿标记），没有 Key 或 SDK 加载失败时整体回落为下面的 SVG 示意地图。两套分支的
 * 接口一致（MapPoint + 选中态 + 路线 + 汇总），页面结构不因实现切换而改变。
 */
export function TravelMap({
  points,
  dayLabel,
  areaLabel,
  selectedId,
  onSelect,
  hotel,
  totalDistanceMeters,
  totalMinutes,
  heightClassName = "h-[280px] sm:h-[320px]",
  className,
  selectedDetail,
}: TravelMapProps) {
  const amapKey = process.env.NEXT_PUBLIC_AMAP_KEY;
  const canLoadAmap = typeof amapKey === "string" && amapKey.trim().length > 0;

  const [amapState, setAmapState] = useState<"loading" | "ready" | "failed">("loading");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapCtxRef = useRef<{ api: AMapNamespace; map: AMapMapInstance } | null>(null);
  const markersRef = useRef<Map<string, AMapMarkerInstance>>(new Map());
  const overlayCacheRef = useRef<unknown[]>([]);
  const fittedKeyRef = useRef("");
  const onSelectRef = useRef(onSelect);

  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  // 只在浏览器里注入 SDK 并初始化地图；任何一步失败都回落 SVG，绝不让页面白屏。
  useEffect(() => {
    if (!canLoadAmap || amapState !== "loading" || !amapKey) return;
    let cancelled = false;
    loadAmap(amapKey)
      .then((api) => {
        if (cancelled || !containerRef.current) return;
        try {
          mapCtxRef.current = { api, map: new api.Map(containerRef.current, { viewMode: "2D" }) };
          setAmapState("ready");
        } catch {
          setAmapState("failed");
        }
      })
      .catch(() => {
        if (!cancelled) setAmapState("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [canLoadAmap, amapKey, amapState]);

  // 卸载时销毁地图实例，避免内存泄漏与重复初始化。
  useEffect(() => {
    return () => {
      mapCtxRef.current?.map.destroy();
      mapCtxRef.current = null;
    };
  }, []);

  // 把点位 / 酒店 / 选中态重画到高德图上；只在点位集合变化时 fitView，避免选中即跳动。
  const renderOverlays = useCallback(() => {
    const ctx = mapCtxRef.current;
    if (!ctx || amapState !== "ready") return;

    if (overlayCacheRef.current.length) {
      ctx.map.remove(overlayCacheRef.current);
      overlayCacheRef.current = [];
    }
    markersRef.current = new Map();

    const path: [number, number][] = points.map((point) => [point.lng, point.lat]);
    const hasSelection = Boolean(selectedId);

    if (path.length > 1) {
      const base = new ctx.api.Polyline({
        path,
        strokeColor: "var(--color-primary)",
        strokeWeight: 5,
        strokeOpacity: hasSelection ? 0.18 : 0.4,
        lineJoin: "round",
      });
      const highlight = new ctx.api.Polyline({
        path,
        strokeColor: "var(--color-primary)",
        strokeWeight: 5,
        strokeOpacity: hasSelection ? 0.9 : 0,
        lineJoin: "round",
      });
      overlayCacheRef.current.push(base, highlight);
    }

    points.forEach((point) => {
      const active = point.id === selectedId;
      const marker = new ctx.api.Marker({
        position: [point.lng, point.lat],
        content: pointMarkerHtml(point.order, active),
        title: `${point.order}. ${point.name}`,
        anchor: "center",
        zIndex: active ? 120 : 100,
      });
      marker.on("click", () => onSelectRef.current?.(point.id));
      markersRef.current.set(point.id, marker);
      overlayCacheRef.current.push(marker);
    });

    let hotelMarker: AMapMarkerInstance | null = null;
    if (hotel) {
      hotelMarker = new ctx.api.Marker({
        position: [hotel.lng, hotel.lat],
        content: hotelMarkerHtml(),
        title: `住宿中心：${hotel.name}`,
        anchor: "center",
        zIndex: 110,
      });
      overlayCacheRef.current.push(hotelMarker);
    }

    ctx.map.add(overlayCacheRef.current);

    const fitKey = points.map((point) => point.id).join("|") + (hotel ? `|h:${hotel.name}` : "");
    if (fittedKeyRef.current !== fitKey) {
      fittedKeyRef.current = fitKey;
      const overlays: AMapMarkerInstance[] = [...markersRef.current.values()].filter(
        (marker): marker is AMapMarkerInstance => Boolean(marker),
      );
      if (hotelMarker) overlays.push(hotelMarker);
      ctx.map.setFitView(overlays, false, [40, 40, 40, 40]);
    }
  }, [points, hotel, amapState, selectedId]);

  useEffect(() => {
    renderOverlays();
  }, [renderOverlays]);

  const selected = points.find((point) => point.id === selectedId) ?? null;
  const useAmap = canLoadAmap && amapState !== "failed";
  const isEmpty = points.length === 0 && !hotel;
  // 仅示意图分支用到的投影布局。
  const layout = project(points, hotel);

  return (
    <div className={cn("overflow-hidden rounded-xl border border-border bg-card", className)} data-testid="travel-map">
      <div className="flex items-start justify-between gap-2 border-b border-border/70 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{dayLabel}</p>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{areaLabel || "当天未标注主要区域"}</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-muted/50 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          <Info className="size-3" aria-hidden />
          {useAmap ? "高德地图" : "示意地图"}
        </span>
      </div>

      <div className="relative">
        {useAmap ? (
          <div ref={containerRef} className={cn("relative w-full overflow-hidden", heightClassName)}>
            {amapState === "loading" ? <MapLoadingHint /> : null}
            {isEmpty ? <EmptyState /> : null}
          </div>
        ) : (
          <div className={cn("relative bg-[oklch(0.978_0.006_140)]", heightClassName)}>
            <svg
              viewBox="0 0 100 62"
              preserveAspectRatio="xMidYMid slice"
              className="absolute inset-0 size-full"
              role="img"
              aria-label={`${dayLabel} ${areaLabel} 路线示意`}
            >
              <MapBackdrop seed={hashSeed(areaLabel + dayLabel)} />

              {layout.routePath ? (
                <polyline
                  points={layout.routePath}
                  fill="none"
                  stroke="var(--color-primary)"
                  strokeWidth={1.1}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  opacity={0.55}
                />
              ) : null}

              {layout.hotel ? (
                <g transform={`translate(${layout.hotel.x} ${layout.hotel.y})`}>
                  <title>住宿中心：{layout.hotel.name}</title>
                  <circle r={4.8} fill="var(--color-card)" stroke="var(--color-primary)" strokeWidth={0.9} />
                  <path d="M-2.1 1.8V-1.2h4.2v3M-2.7 1.8h5.4M-1.2-1.2v1.5M1.2-1.2v1.5" fill="none" stroke="var(--color-primary)" strokeWidth={0.75} strokeLinecap="round" />
                  <text y={6.4} textAnchor="middle" fontSize={2.35} fill="var(--color-foreground)" opacity={0.82}>
                    住宿
                  </text>
                </g>
              ) : null}

              {layout.points.map((point) => {
                const active = point.id === selectedId;
                return (
                  <g
                    key={point.id}
                    transform={`translate(${point.x} ${point.y})`}
                    className={onSelect ? "cursor-pointer" : undefined}
                    onClick={() => onSelect?.(point.id)}
                  >
                    <title>{`${point.order}. ${point.name}`}</title>
                    {active ? (
                      <circle r={5.4} fill="var(--color-primary)" opacity={0.16} />
                    ) : null}
                    <circle
                      r={active ? 3.2 : 2.6}
                      fill={active ? "var(--color-primary)" : "var(--color-card)"}
                      stroke={active ? "var(--color-primary)" : "var(--color-primary)"}
                      strokeWidth={0.7}
                      strokeOpacity={active ? 1 : 0.5}
                    />
                    <text
                      y={0.95}
                      textAnchor="middle"
                      fontSize={2.9}
                      fontWeight={600}
                      fill={active ? "var(--color-primary-foreground)" : "var(--color-primary)"}
                    >
                      {point.order}
                    </text>
                    {active ? (
                      <g transform="translate(0 -6.2)">
                        <text
                          textAnchor="middle"
                          fontSize={2.7}
                          fill="var(--color-foreground)"
                          className="select-none"
                          opacity={0.85}
                        >
                          {truncate(point.name, 12)}
                        </text>
                      </g>
                    ) : null}
                  </g>
                );
              })}
            </svg>

            {isEmpty ? <EmptyState /> : null}
          </div>
        )}

        {selected ? (
          <div className="absolute inset-x-3 bottom-3 z-20 rounded-lg border border-border bg-card/95 px-3 py-2 shadow-sm backdrop-blur-sm">
            <p className="truncate text-xs font-medium text-foreground">
              {selectedDetail?.order ?? selected.order}. {selectedDetail?.name ?? selected.name}
            </p>
            {selectedDetail ? (
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                {selectedDetail.startTime ? (
                  <span className="inline-flex items-center gap-1">
                    <Clock className="size-3" aria-hidden /> {selectedDetail.startTime} 到达
                  </span>
                ) : null}
                {selectedDetail.stayMinutes != null ? (
                  <span className="inline-flex items-center gap-1">
                    <Hourglass className="size-3" aria-hidden /> 预计停留{" "}
                    {formatDuration(selectedDetail.stayMinutes)}
                  </span>
                ) : null}
                {selectedDetail.distanceFromPreviousMeters != null ? (
                  <span className="inline-flex items-center gap-1">
                    <Route className="size-3" aria-hidden /> 距上一站{" "}
                    {formatDistance(selectedDetail.distanceFromPreviousMeters)}
                  </span>
                ) : null}
                {selectedDetail.area ? (
                  <span className="inline-flex items-center gap-1">
                    <MapPin className="size-3" aria-hidden /> {selectedDetail.area}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border/70 px-4 py-2.5 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <Route className="size-3.5" aria-hidden />
          总移动时间
          <span className="tabular text-foreground">{formatDuration(totalMinutes ?? null)}</span>
        </span>
        <span className="tabular">总距离 {formatDistance(totalDistanceMeters ?? null)}</span>
        <span className="tabular">{points.length} 个标记点</span>
        {hotel ? (
          <span className="inline-flex items-center gap-1 text-foreground/80">
            <BedDouble className="size-3.5" aria-hidden /> 住宿中心
          </span>
        ) : null}
        {onSelect ? <span className="text-muted-foreground/70">点击编号可高亮对应安排</span> : null}
      </div>
    </div>
  );
}

function MapLoadingHint() {
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center gap-2 bg-muted/20 text-xs text-muted-foreground">
      <LoaderCircle className="size-4 animate-spin" aria-hidden />
      正在加载高德地图…
    </div>
  );
}

function EmptyState() {
  return (
    <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-1.5 px-6 text-center">
      <MapPinned className="size-5 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium text-foreground">这一天的安排没有可定位的地点</p>
      <p className="text-xs leading-5 text-muted-foreground">
        当天以交通或自由活动为主，因此没有可以标注在地图上的地点。
      </p>
    </div>
  );
}

// ==================================================
// 高德 marker 的 HTML 内容（编号打点 / 住宿标记）
// ==================================================

function pointMarkerHtml(order: number, active: boolean): string {
  const size = active ? 26 : 22;
  return `<div style="display:flex;align-items:center;justify-content:center;width:${size}px;height:${size}px;box-sizing:border-box;border-radius:9999px;font-size:12px;font-weight:600;line-height:1;background:${active ? "var(--color-primary)" : "var(--color-card)"};color:${active ? "var(--color-primary-foreground)" : "var(--color-primary)"};border:${active ? "none" : "2px solid var(--color-primary)"};box-shadow:${active ? "0 0 0 4px color-mix(in srgb, var(--color-primary) 16%, transparent)" : "0 1px 4px rgba(0,0,0,.25)"}">${order}</div>`;
}

function hotelMarkerHtml(): string {
  return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px"><span style="display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:8px;background:var(--color-primary);color:var(--color-primary-foreground);box-shadow:0 1px 4px rgba(0,0,0,.25)"><svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/></svg></span><span style="font-size:10px;line-height:1;color:var(--color-muted-foreground);background:var(--color-card);padding:1px 4px;border-radius:4px;border:1px solid var(--color-border)">住宿</span></div>`;
}

// ==================================================
// 投影：经纬度 → SVG 坐标（等距近似，考虑纬度收敛）
// ==================================================

interface ProjectedPoint extends MapPoint {
  x: number;
  y: number;
}

interface ProjectedHotel {
  name: string;
  x: number;
  y: number;
}

interface MapLayout {
  points: ProjectedPoint[];
  routePath: string;
  hotel: ProjectedHotel | null;
}

const VIEW_W = 100;
const VIEW_H = 62;
const PADDING = 14;

function project(points: MapPoint[], hotel?: TravelMapProps["hotel"]): MapLayout {
  const coordinates = hotel ? [...points, { ...hotel }] : points;
  if (!coordinates.length) return { points: [], routePath: "", hotel: null };

  const midLat = coordinates.reduce((sum, point) => sum + point.lat, 0) / coordinates.length;
  const lonScale = Math.cos((midLat * Math.PI) / 180);

  const xs = coordinates.map((point) => point.lng * lonScale);
  const ys = coordinates.map((point) => point.lat);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 0.0001;
  const spanY = maxY - minY || 0.0001;

  const usableW = VIEW_W - PADDING * 2;
  const usableH = VIEW_H - PADDING * 2;

  const projected = points.map((point, index) => {
    const x = PADDING + ((xs[index] - minX) / spanX) * usableW;
    // 纬度越大越靠北 → SVG y 越小
    const y = PADDING + (1 - (ys[index] - minY) / spanY) * usableH;
    return { ...point, x, y };
  });

  const hotelIndex = hotel ? coordinates.length - 1 : -1;
  const projectedHotel =
    hotel && hotelIndex >= 0
      ? {
          name: hotel.name,
          x: PADDING + ((xs[hotelIndex] - minX) / spanX) * usableW,
          y: PADDING + (1 - (ys[hotelIndex] - minY) / spanY) * usableH,
        }
      : null;

  const routePath = projected.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ");
  return { points: projected, routePath, hotel: projectedHotel };
}

// ==================================================
// 背景装饰：固定的伪随机图案，保证每次渲染一致
// ==================================================

function hashSeed(input: string): number {
  let hash = 2166136261;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function mulberry32(seed: number) {
  let state = seed;
  return () => {
    state |= 0;
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function MapBackdrop({ seed }: { seed: number }) {
  const random = mulberry32(seed);
  const blocks: { x: number; y: number; w: number; h: number }[] = [];
  for (let index = 0; index < 26; index += 1) {
    const w = 4 + random() * 9;
    const h = 3 + random() * 6;
    blocks.push({ x: random() * (VIEW_W - w), y: random() * (VIEW_H - h), w, h });
  }

  const roads: string[] = [];
  for (let index = 0; index < 5; index += 1) {
    const y = 6 + index * 12 + random() * 3;
    roads.push(`M0 ${y.toFixed(1)} L${VIEW_W} ${(y - 4 + random() * 8).toFixed(1)}`);
  }
  for (let index = 0; index < 4; index += 1) {
    const x = 10 + index * 26 + random() * 4;
    roads.push(`M${x.toFixed(1)} 0 L${(x + 5 - random() * 10).toFixed(1)} ${VIEW_H}`);
  }

  const river = `M-2 ${(48 + random() * 4).toFixed(1)} C 18 ${(40 + random() * 6).toFixed(1)}, 38 ${(
    56 +
    random() * 4
  ).toFixed(1)}, 60 ${(46 + random() * 5).toFixed(1)} S 88 ${(52 + random() * 5).toFixed(1)}, 102 ${(
    44 +
    random() * 5
  ).toFixed(1)}`;

  return (
    <g aria-hidden>
      <rect x={0} y={0} width={VIEW_W} height={VIEW_H} fill="oklch(0.978 0.006 140)" />
      {blocks.map((block, index) => (
        <rect
          key={`block-${index}`}
          x={block.x}
          y={block.y}
          width={block.w}
          height={block.h}
          rx={0.6}
          fill="oklch(0.962 0.006 150)"
        />
      ))}
      {blocks.slice(0, 4).map((block, index) => (
        <rect
          key={`park-${index}`}
          x={block.x + 2}
          y={block.y + 1}
          width={block.w + 6}
          height={block.h + 3}
          rx={1.4}
          fill="oklch(0.955 0.026 145)"
          opacity={0.7}
        />
      ))}
      <path d={river} fill="none" stroke="oklch(0.9 0.028 220)" strokeWidth={3.2} strokeLinecap="round" />
      {roads.map((road, index) => (
        <path
          key={`road-${index}`}
          d={road}
          fill="none"
          stroke="oklch(0.998 0 0)"
          strokeWidth={index % 3 === 0 ? 1.9 : 1.1}
          strokeLinecap="round"
        />
      ))}
    </g>
  );
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max)}…` : value;
}
