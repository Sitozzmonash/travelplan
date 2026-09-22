"use client";

import type { Pace } from "@/types/session";
import type { GuidedDraft } from "./draft";
import { StepHotel } from "./step-hotel";
import { StepPace } from "./step-pace";
import { StepTransport } from "./step-transport";

/**
 * 第 2 步「偏好」：交通 + 酒店 + 节奏合成一页。
 *
 * 这三块原本各占一页，但每页其实只问一两件事，用户要点三次「继续」才到开始，
 * 体感很磨。它们都是"选个偏好"而不是"填信息"，且彼此独立 —— 合成一页既不增加
 * 必填项，也少了两次跳转。三个子块仍是原来那三个组件，没有复制任何选项逻辑。
 *
 * 一页里每一项都仍然可以「随便 / 帮我选 / 不确定」，直接点「开始规划」即可：
 * 没表态的项不会写进 PATCH，交给后端的稳定默认策略。
 *
 * 三块之间用分隔线分组，因此内外统一用 gap-5：同组内的题目间距与跨组的分隔线两侧
 * 留白一致，整页的呼吸节奏只有一种，不再出现某块偏松、某块偏紧。
 */

interface StepPreferencesProps {
  draft: GuidedDraft;
  onTransport: (patch: Partial<GuidedDraft["transport"]>) => void;
  onHotel: (patch: Partial<GuidedDraft["hotel"]>) => void;
  onPace: (pace: Pace) => void;
}

export function StepPreferences({ draft, onTransport, onHotel, onPace }: StepPreferencesProps) {
  return (
    <div className="grid gap-5">
      <StepTransport draft={draft} onChange={onTransport} />
      <Divider label="住哪一类" />
      <StepHotel draft={draft} onChange={onHotel} />
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
