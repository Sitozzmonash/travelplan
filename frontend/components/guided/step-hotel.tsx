"use client";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { PreferenceSentinel } from "@/types/session";
import type { GuidedDraft } from "./draft";
import { hotelDetailLabels, maxPricePresetLabel } from "./draft";
import { ChoiceGrid, ChipToggle, SentinelRow, StepSection } from "./option-cards";
import {
  ALLOW_CHANGE_OPTIONS,
  HOTEL_PRIORITY_OPTIONS,
  MAX_PRICE_PRESETS,
  MIN_STAR_OPTIONS,
  ROOM_TYPE_OPTIONS,
  SENTINEL_OPTIONS,
  optionLabel,
} from "./options";

/**
 * Step 3 酒店偏好（§7）。
 * 策略 → hotel_priority；四个补充项 → hotel_max_price_per_night / hotel_min_star /
 * hotel_room_type / hotel_allow_change。
 * 补充项都能选「不限 / 不确定 / 帮我选」，三者在后端是三种不同的值；
 * 完全没表态的项不会写进 PATCH，让后端自己的稳定默认策略生效。
 */

interface StepHotelProps {
  draft: GuidedDraft;
  onChange: (patch: Partial<GuidedDraft["hotel"]>) => void;
  /** 后端能力声明：false 时「最低星级」置灰（数据源不返回星级）。缺省视为可用。 */
  hotelStarFilterAvailable?: boolean;
}

export function StepHotel({ draft, onChange, hotelStarFilterAvailable = true }: StepHotelProps) {
  const hotel = draft.hotel;
  const details = hotelDetailLabels(hotel, { includeStar: hotelStarFilterAvailable });

  return (
    <div className="grid gap-6">
      <StepSection title="酒店你更看重什么？" hint="这一项会进入酒店候选的真实评分；选「帮我选」时按位置与评分不差、再综合价格的稳定策略来。">
        <ChoiceGrid
          ariaLabel="酒店策略"
          options={HOTEL_PRIORITY_OPTIONS}
          value={hotel.priority}
          onChange={(next) => onChange({ priority: next })}
          columns={3}
          dense
        />
      </StepSection>

      <StepSection title="每晚价格上限" hint="直接填数字就是明确上限；也可以选「不限 / 不确定 / 帮我选」。">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={hotel.maxPriceText}
            inputMode="numeric"
            aria-label="每晚价格上限"
            placeholder="例如 500"
            className="w-[140px]"
            disabled={hotel.maxPriceSentinel !== null}
            onChange={(event) => onChange({ maxPriceText: event.target.value, maxPriceSentinel: null })}
          />
          {MAX_PRICE_PRESETS.map((preset) => (
            <Chip
              key={preset}
              label={maxPricePresetLabel(preset)}
              selected={hotel.maxPriceSentinel === null && hotel.maxPriceText.trim() === String(preset)}
              onClick={() => onChange({ maxPriceText: String(preset), maxPriceSentinel: null })}
            />
          ))}
        </div>
        <SentinelRow
          ariaLabel="每晚价格上限的其他选择"
          options={SENTINEL_OPTIONS}
          value={hotel.maxPriceSentinel}
          onChange={(value) => onChange({ maxPriceSentinel: value, maxPriceText: "" })}
        />
      </StepSection>

      <StepSection
        title="最低星级"
        hint={
          hotelStarFilterAvailable
            ? "选具体星级表示「至少这个档次」。"
            : "数据源当前不返回星级，暂不可用 —— 选了也不会生效，因此这里禁用。"
        }
      >
        <ChipToggle
          ariaLabel="最低星级"
          options={MIN_STAR_OPTIONS}
          values={typeof hotel.minStar === "number" ? [hotel.minStar] : []}
          disabled={!hotelStarFilterAvailable}
          onToggle={(value) => onChange({ minStar: typeof hotel.minStar === "number" && hotel.minStar === value ? null : value })}
        />
        <SentinelRow
          ariaLabel="最低星级的其他选择"
          options={SENTINEL_OPTIONS}
          value={typeof hotel.minStar === "string" ? hotel.minStar : null}
          disabled={!hotelStarFilterAvailable}
          onChange={(value) => onChange({ minStar: value })}
        />
        {!hotelStarFilterAvailable ? (
          <p className="rounded-md bg-muted/60 px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
            数据源当前不返回星级，暂不可用。每晚价格上限等其他条件仍然生效。
          </p>
        ) : null}
      </StepSection>

      <StepSection title="房型" hint="人多或带小孩时，可以指定房型。">
        <ChipToggle
          ariaLabel="房型"
          options={ROOM_TYPE_OPTIONS}
          values={typeof hotel.roomType === "string" && !SENTINEL_OPTIONS.some((item) => item.value === hotel.roomType) ? [hotel.roomType] : []}
          onToggle={(value) => onChange({ roomType: hotel.roomType === value ? null : value })}
        />
        <SentinelRow
          ariaLabel="房型的其他选择"
          options={SENTINEL_OPTIONS}
          value={typeof hotel.roomType === "string" && SENTINEL_OPTIONS.some((item) => item.value === hotel.roomType) ? (hotel.roomType as PreferenceSentinel) : null}
          onChange={(value) => onChange({ roomType: value })}
        />
      </StepSection>

      <StepSection title="接受换酒店吗？" hint="行程跨度大时，换到就近的酒店能减少通勤；不想折腾就固定一家。">
        <ChoiceGrid
          ariaLabel="是否接受换酒店"
          options={ALLOW_CHANGE_OPTIONS}
          value={typeof hotel.allowChange === "boolean" ? hotel.allowChange : null}
          onChange={(next) => onChange({ allowChange: next })}
          dense
        />
        <SentinelRow
          ariaLabel="是否接受换酒店的其他选择"
          options={SENTINEL_OPTIONS}
          value={typeof hotel.allowChange === "string" ? hotel.allowChange : null}
          onChange={(value) => onChange({ allowChange: value })}
        />
      </StepSection>

      <p className="rounded-lg bg-muted/50 px-3 py-2.5 text-[11px] leading-5 text-muted-foreground">
        将提交：{optionLabel(HOTEL_PRIORITY_OPTIONS, hotel.priority) ?? "帮我选（默认）"}
        {details.length > 0 ? ` · ${details.join(" · ")}` : " · 补充条件未表态，交给系统按默认策略筛选"}
      </p>
    </div>
  );
}

function Chip({
  label,
  selected,
  onClick,
}: {
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        "inline-flex min-h-[36px] items-center rounded-full border px-3 text-xs transition-colors",
        selected
          ? "border-primary/50 bg-accent text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}
