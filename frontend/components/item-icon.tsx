import { Camera, Clock3, Hotel, Landmark, Plane, Route, TrainFront, UtensilsCrossed } from "lucide-react";
import type { ItemType } from "@/types/plan";
import { cn } from "@/lib/utils";

export const ITEM_TYPE_LABELS: Record<ItemType, string> = {
  attraction: "景点",
  food: "餐饮",
  hotel: "住宿",
  transport: "交通",
  activity: "活动",
  free_time: "自由时间",
};

interface ItemTypeIconProps {
  type: ItemType;
  transportMode?: string | null;
  className?: string;
}

/** 用图标区分类型，不用 Emoji、不铺颜色（FRONTEND_DESIGN §11、§29）。 */
export function ItemTypeIcon({ type, transportMode, className }: ItemTypeIconProps) {
  const shared = { className: cn("size-4", className), "aria-hidden": true } as const;

  if (type === "food") return <UtensilsCrossed {...shared} />;
  if (type === "hotel") return <Hotel {...shared} />;
  if (type === "activity") return <Camera {...shared} />;
  if (type === "free_time") return <Clock3 {...shared} />;
  if (type === "transport") {
    if (transportMode === "flight") return <Plane {...shared} />;
    if (transportMode === "train") return <TrainFront {...shared} />;
    return <Route {...shared} />;
  }
  return <Landmark {...shared} />;
}
