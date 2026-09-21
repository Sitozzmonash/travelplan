"use client";

import type { Pace } from "@/types/session";
import type { GuidedDraft } from "./draft";
import { StepHotel } from "./step-hotel";
import { StepPace } from "./step-pace";
import { StepTransport } from "./step-transport";

/**
 * 第 3 步「偏好」：交通 + 酒店 + 节奏合成一页。
 *
 * 这三块原本各占一页，但每页其实只问一两件事，用户要点三次「继续」才到开始，
 * 体感很磨。它们都是"选个偏好"而不是"填信息"，且彼此独立 —— 合成一页既不增加
 * 必填项，也少了两次跳转。三个子块仍是原来那三个组件，没有复制任何选项逻辑。
 *
 * 一页里每一项都仍然可以「随便 / 帮我选 / 不确定」，直接点「开始规划」即可：
 * 没表态的项不会写进 PATCH，交给后端的稳定默认策略。
 */

interface StepPreferencesProps {
  draft: GuidedDraft;
  onTransport: (patch: Partial<GuidedDraft["transport"]>) => void;
  onHotel: (patch: Partial<GuidedDraft["hotel"]>) => void;
  onPace: (pace: Pace) => void;
  /** 后端能力声明：false 时「最低星级」置灰（数据源不返回星级）。 */
  hotelStarFilterAvailable?: boolean;
  /** 后端能力声明：false 时「接受换酒店」置灰（当前全程只订一家）。 */
  hotelAllowChangeAvailable?: boolean;
}

export function StepPreferences({
  draft,
  onTransport,
  onHotel,
  onPace,
  hotelStarFilterAvailable,
  hotelAllowChangeAvailable,
}: StepPreferencesProps) {
  return (
    <div className="grid gap-7">
      <StepTransport draft={draft} onChange={onTransport} />
      <Divider label="住哪一类" />
      <StepHotel
        draft={draft}
        onChange={onHotel}
        hotelStarFilterAvailable={hotelStarFilterAvailable}
        hotelAllowChangeAvailable={hotelAllowChangeAvailable}
      />
      <Divider label="每天怎么玩" />
      <StepPace pace={draft.pace} onChange={onPace} />
    </div>
  );
}

function Divider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3" role="presentation">
      <span className="h-px flex-1 bg-border" />
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}
