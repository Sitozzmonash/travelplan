export default function PlanLoading() {
  return (
    <div className="mx-auto w-full max-w-[1440px] px-4 py-6 sm:px-6">
      <div className="mb-4 h-4 w-32 animate-pulse rounded bg-muted" />
      <div className="space-y-5">
        <div className="h-32 animate-pulse rounded-xl border border-border bg-card" />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <div key={index} className="h-28 animate-pulse rounded-xl border border-border bg-card" />
          ))}
        </div>
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
          <div className="space-y-3">
            <div className="h-9 animate-pulse rounded-lg bg-muted" />
            {[0, 1, 2, 3].map((index) => (
              <div key={index} className="h-32 animate-pulse rounded-xl border border-border bg-card" />
            ))}
          </div>
          <div className="space-y-5">
            <div className="h-40 animate-pulse rounded-xl border border-border bg-card" />
            <div className="h-56 animate-pulse rounded-xl border border-border bg-card" />
          </div>
        </div>
      </div>
      <p className="mt-4 text-center text-xs text-muted-foreground">
        正在读取行程数据：交通、酒店与每天安排会按顺序渲染。
      </p>
    </div>
  );
}
