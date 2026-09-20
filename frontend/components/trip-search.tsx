"use client";

import { useState } from "react";
import { ArrowRight, ChevronDown, SlidersHorizontal, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { CityCombobox } from "@/components/city-combobox";
import { cn } from "@/lib/utils";

const EXAMPLES = [
  {
    label: "北京 → 成都 5天",
    message: "10月1日从北京去成都玩5天，两个人预算6000，喜欢美食、拍照和历史文化",
    structured: { origin: "北京", destination: "成都", days: "5", travelers: "2", budget: "6000" },
    preferences: ["美食", "拍照", "历史"],
    pace: "平衡",
  },
  {
    label: "上海 → 杭州 周末",
    message: "这周末从上海去杭州玩2天，两个人，喜欢自然和拍照，节奏轻松一点",
    structured: { origin: "上海", destination: "杭州", days: "2", travelers: "2", budget: "" },
    preferences: ["自然", "拍照", "轻松"],
    pace: "轻松",
  },
  {
    label: "广州 → 重庆 4天美食",
    message: "从广州去重庆玩4天，两个人预算5000，主要想吃美食，节奏紧凑一点",
    structured: { origin: "广州", destination: "重庆", days: "4", travelers: "2", budget: "5000" },
    preferences: ["美食", "夜生活"],
    pace: "特种兵",
  },
  {
    label: "长春 → 成都 国庆",
    message: "国庆从长春去成都玩6天，两个人预算8000，想看熊猫和自然风光，节奏轻松",
    structured: { origin: "长春", destination: "成都", days: "6", travelers: "2", budget: "8000" },
    preferences: ["自然", "亲子", "轻松"],
    pace: "轻松",
  },
] as const;

const PREFERENCE_OPTIONS = ["美食", "拍照", "自然", "历史", "亲子", "购物", "夜生活", "轻松"];
const PACE_OPTIONS = ["轻松", "平衡", "特种兵"] as const;

type Pace = (typeof PACE_OPTIONS)[number];

interface TripSearchProps {
  onSubmit: (message: string) => void;
  submitting?: boolean;
}

/**
 * 首页主输入（FRONTEND_DESIGN §5）。
 * 自然语言是主入口；下方结构化条件只是把内容拼进同一段请求，不做任何意图解析。
 */
export function TripSearch({ onSubmit, submitting = false }: TripSearchProps) {
  const [message, setMessage] = useState("");
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [startDate, setStartDate] = useState("");
  const [days, setDays] = useState("");
  const [travelers, setTravelers] = useState("2");
  const [budget, setBudget] = useState("");
  const [preferences, setPreferences] = useState<string[]>([]);
  const [pace, setPace] = useState<Pace>("平衡");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function togglePreference(value: string) {
    setPreferences((prev) => (prev.includes(value) ? prev.filter((item) => item !== value) : [...prev, value]));
  }

  function applyExample(index: number) {
    const example = EXAMPLES[index];
    setMessage(example.message);
    setOrigin(example.structured.origin);
    setDestination(example.structured.destination);
    setDays(example.structured.days);
    setTravelers(example.structured.travelers);
    setBudget(example.structured.budget);
    setPreferences([...example.preferences]);
    setPace(example.pace as Pace);
    setAdvancedOpen(true);
    setError(null);
  }

  function composeMessage(): string {
    const parts: string[] = [];
    if (message.trim()) parts.push(message.trim());
    const structured: string[] = [];
    if (origin.trim()) structured.push(`出发地${origin.trim()}`);
    if (destination.trim()) structured.push(`目的地${destination.trim()}`);
    if (startDate) structured.push(`出发日期 ${startDate}`);
    if (days.trim()) structured.push(`共 ${days.trim()} 天`);
    if (travelers.trim()) structured.push(`${travelers.trim()} 人出行`);
    if (budget.trim()) structured.push(`总预算 ${budget.trim()} 元`);
    if (preferences.length) structured.push(`偏好 ${preferences.join("、")}`);
    if (pace) structured.push(`节奏${pace}`);
    if (structured.length) parts.push(`（补充条件：${structured.join("，")}）`);
    return parts.join("");
  }

  function handleSubmit() {
    const composed = composeMessage();
    if (!composed) {
      setError("请先用一句话描述你想怎么旅行，或至少填写出发地与目的地。");
      return;
    }
    setError(null);
    onSubmit(composed);
  }

  return (
    <div className="w-full">
      <div className="rounded-xl border border-border bg-card p-3 shadow-sm sm:p-4">
        <Textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") handleSubmit();
          }}
          rows={4}
          placeholder="例如：10月1日从北京去成都玩5天，两个人预算6000，喜欢美食、拍照和历史文化"
          className="min-h-[104px] resize-y border-0 bg-transparent px-1 text-base leading-7 shadow-none focus-visible:ring-0 sm:text-base"
          aria-label="描述你的旅行需求"
        />

        <div className="mt-3 flex flex-col gap-3 border-t border-border/70 pt-3 sm:flex-row sm:items-center sm:justify-between">
          <button
            type="button"
            onClick={() => setAdvancedOpen((prev) => !prev)}
            className="inline-flex items-center gap-1.5 self-start text-xs text-muted-foreground transition-colors hover:text-foreground"
            aria-expanded={advancedOpen}
          >
            <SlidersHorizontal className="size-3.5" aria-hidden />
            高级条件（可选）
            <ChevronDown className={cn("size-3.5 transition-transform", advancedOpen && "rotate-180")} aria-hidden />
          </button>

          <div className="flex items-center gap-3">
            <span className="hidden text-[11px] text-muted-foreground sm:inline">⌘ / Ctrl + Enter 直接开始</span>
            <Button size="lg" onClick={handleSubmit} disabled={submitting}>
              <Sparkles />
              开始规划
              <ArrowRight />
            </Button>
          </div>
        </div>

        {advancedOpen ? (
          <div className="mt-3 grid gap-4 border-t border-border/70 pt-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              <Field label="出发地">
                <CityCombobox
                  value={origin}
                  onChange={setOrigin}
                  placeholder="北京"
                  ariaLabel="出发地"
                />
              </Field>
              <Field label="目的地">
                <CityCombobox
                  value={destination}
                  onChange={setDestination}
                  placeholder="成都"
                  ariaLabel="目的地"
                />
              </Field>
              <Field label="出发日期">
                <Input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
              </Field>
              <Field label="天数">
                <Input
                  value={days}
                  inputMode="numeric"
                  onChange={(event) => setDays(event.target.value)}
                  placeholder="5"
                />
              </Field>
              <Field label="人数">
                <Input
                  value={travelers}
                  inputMode="numeric"
                  onChange={(event) => setTravelers(event.target.value)}
                  placeholder="2"
                />
              </Field>
              <Field label="总预算（元）">
                <Input
                  value={budget}
                  inputMode="numeric"
                  onChange={(event) => setBudget(event.target.value)}
                  placeholder="6000"
                />
              </Field>
            </div>

            <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-start">
              <div>
                <p className="mb-2 text-[11px] text-muted-foreground">偏好</p>
                <div className="flex flex-wrap gap-1.5">
                  {PREFERENCE_OPTIONS.map((option) => (
                    <Chip
                      key={option}
                      label={option}
                      selected={preferences.includes(option)}
                      onClick={() => togglePreference(option)}
                    />
                  ))}
                </div>
              </div>
              <div>
                <p className="mb-2 text-[11px] text-muted-foreground sm:text-right">节奏</p>
                <div className="flex flex-wrap gap-1.5">
                  {PACE_OPTIONS.map((option) => (
                    <Chip key={option} label={option} selected={pace === option} onClick={() => setPace(option)} />
                  ))}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </div>

      {error ? (
        <p className="mt-2 text-xs text-danger-subtle-foreground">{error}</p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted-foreground">快捷示例</span>
        {EXAMPLES.map((example, index) => (
          <button
            key={example.label}
            type="button"
            onClick={() => applyExample(index)}
            className="rounded-full border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
          >
            {example.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="grid gap-1.5">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function Chip({ label, selected, onClick }: { label: string; selected: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "rounded-full border px-2.5 py-1 text-xs transition-colors",
        selected
          ? "border-primary/40 bg-accent text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}
