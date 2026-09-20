"use client";

import { useId, useMemo, useState } from "react";
import { CHINA_CITIES, type ChinaCity, searchCities } from "@/lib/cities";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

interface CityComboboxProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** 无障碍名称，例如「出发地」。 */
  ariaLabel: string;
}

/** 空输入时的建议：只放常见旅行城市，避免一上来就列出一串地名。 */
const COMMON_CITY_NAMES = ["北京", "上海", "广州", "深圳", "成都", "重庆", "杭州", "西安"];
const COMMON_CITIES = COMMON_CITY_NAMES.map((name) => CHINA_CITIES.find((city) => city.name === name)).filter(
  (city): city is ChinaCity => Boolean(city),
);

/**
 * 城市输入框（FRONTEND_DESIGN §5）。
 *
 * 只做输入建议，不做校验：后端接受任意自然语言，所以任何不在数据集里的城市
 * 都必须能照样输入并提交。键盘支持 ↑ / ↓ 选择、Enter 采纳、Esc 关闭。
 */
export function CityCombobox({ value, onChange, placeholder, ariaLabel }: CityComboboxProps) {
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);

  const query = value.trim();
  const suggestions = useMemo(() => (query ? searchCities(query) : COMMON_CITIES), [query]);
  const hasSuggestions = suggestions.length > 0;

  function choose(city: ChinaCity) {
    onChange(city.name);
    setOpen(false);
    setHighlight(0);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      setOpen(true);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (hasSuggestions) setHighlight((current) => (current + 1) % suggestions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (hasSuggestions) setHighlight((current) => (current - 1 + suggestions.length) % suggestions.length);
    } else if (event.key === "Enter") {
      // 只有明确选中了一条建议才拦截 Enter；否则保持自由文本提交。
      if (open && suggestions[highlight]) {
        event.preventDefault();
        choose(suggestions[highlight]);
      }
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative">
      <Input
        value={value}
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-autocomplete="list"
        autoComplete="off"
        placeholder={placeholder}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
          setHighlight(0);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={handleKeyDown}
      />

      {open ? (
        <div className="absolute z-20 mt-1 w-full overflow-hidden rounded-lg border border-border bg-popover shadow-md">
          {hasSuggestions ? (
            <ul id={listId} role="listbox" aria-label="城市建议" className="max-h-64 overflow-y-auto py-1">
              {suggestions.map((city, index) => (
                <li
                  key={`${city.region}-${city.name}`}
                  role="option"
                  aria-selected={index === highlight}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setHighlight(index)}
                  onClick={() => choose(city)}
                  className={cn(
                    "flex cursor-pointer items-center justify-between gap-2 px-2.5 py-1.5 text-xs",
                    index === highlight ? "bg-accent text-accent-foreground" : "text-foreground/90",
                  )}
                >
                  <span className="truncate">{city.name}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{city.region}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p id={listId} className="px-2.5 py-2 text-[11px] leading-5 text-muted-foreground">
              没有匹配的城市，仍可直接使用「{query}」：后端接受任意自然语言输入，不限制城市范围。
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
