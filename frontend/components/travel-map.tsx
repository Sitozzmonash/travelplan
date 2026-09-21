"use client";

import { BedDouble, Info, MapPinned, Route } from "lucide-react";
import type { MapPoint } from "@/types/plan";
import { cn } from "@/lib/utils";
import { formatDistance, formatDuration } from "@/lib/format";

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
}

/**
 * 地图面板占位实现（FRONTEND_DESIGN §14）。
 *
 * 这里不使用任何真实地图 SDK，也不需要任何 Key：用 SVG 画出一个示意地图，
 * 但组件接口与真实高德 JS SDK 一致（MapPoint + 选中态 + 路线 + 汇总），
 * 未来替换实现时页面结构不需要改。
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
}: TravelMapProps) {
  const layout = project(points, hotel);
  const selected = points.find((point) => point.id === selectedId) ?? null;

  return (
    <div className={cn("overflow-hidden rounded-xl border border-border bg-card", className)} data-testid="travel-map">
      <div className="flex items-start justify-between gap-2 border-b border-border/70 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{dayLabel}</p>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">{areaLabel || "当天未标注主要区域"}</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-muted/50 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          <Info className="size-3" aria-hidden />
          示意地图
        </span>
      </div>

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

        {points.length === 0 && !hotel ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 px-6 text-center">
            <MapPinned className="size-5 text-muted-foreground" aria-hidden />
            <p className="text-sm font-medium text-foreground">这一天的安排没有可定位的地点</p>
            <p className="text-xs leading-5 text-muted-foreground">
              当天以交通或自由活动为主，因此没有可以标注在地图上的地点。
            </p>
          </div>
        ) : null}

        {selected ? (
          <div className="absolute inset-x-3 bottom-3 rounded-lg border border-border bg-card/95 px-3 py-2 shadow-sm backdrop-blur-sm">
            <p className="truncate text-xs font-medium text-foreground">
              {selected.order}. {selected.name}
            </p>
            <p className="tabular mt-0.5 text-[11px] text-muted-foreground">
              {selected.lat.toFixed(4)}, {selected.lng.toFixed(4)}
            </p>
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
