import Link from "next/link";
import { Compass } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col items-start gap-4 px-4 py-20 sm:px-6">
      <span className="inline-flex size-9 items-center justify-center rounded-lg bg-muted text-muted-foreground">
        <Compass className="size-4" aria-hidden />
      </span>
      <h1 className="text-lg font-semibold text-foreground">这个页面不存在</h1>
      <p className="text-sm leading-6 text-muted-foreground">
        链接可能不完整，或者这个行程编号已经失效。行程页面只有通过生成结果才能打开，请返回首页重新规划一次。
      </p>
      <Button nativeButton={false} render={<Link href="/" />}>
        返回首页
      </Button>
    </div>
  );
}
