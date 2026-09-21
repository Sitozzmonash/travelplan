/**
 * 本地默认管理 Token 的下发端点。
 *
 * 存在的理由只有一个：让 `frontend/.env.local` 里配好的 `TRAVELPLAN_ADMIN_TOKEN`
 * 自动填进管理台，操作员打开 /admin 不用再手输一次（等价于"取消登录"）。
 *
 * 为什么用服务端路由，而不是 `NEXT_PUBLIC_ADMIN_TOKEN`：
 * 后者的值会在 build 期内联进浏览器产物，等于把管理 Token 公开给任何访问者；
 * 这里读的是**非 public** 变量，只在请求时由 Node 进程读一次，不参与前端打包。
 * 因此这条路由也是「同源、只对本机进程有意义」的：Token 没有离开过本机的
 * 服务端进程与浏览器之间的这条连接。
 *
 * 安全边界（重要，别踩）：
 *   · 本地开发：值来自 `frontend/.env.local`（已被 `.gitignore` 忽略），只有本机进程读得到；
 *   · 线上（Vercel / EdgeOne）：前端项目里没有这个变量（`.env.production` 只有
 *     `NEXT_PUBLIC_*`），端点返回 `{token: null}`，管理台回到「手填 Token」的老行为；
 *   · 反过来，谁要是把 `TRAVELPLAN_ADMIN_TOKEN` 配到了前端托管平台的环境变量里，
 *     这个端点就等于把 Token 挂到公网上——**不要把后端 Secret 配到前端**。
 *
 * 路径刻意不放在 `/api/*` 下：Vercel 部署常把 `/api/*` rewrite 到后端容器，
 * 那条 rewrite 会把本路由一起吞掉（浏览器拿到 404）。静态托管下没有这条路由时，
 * 前端按"拿不到默认值"处理，退化成手填，不会因此打不开管理台。
 */

export const dynamic = "force-dynamic";

export async function GET() {
  const token = process.env.TRAVELPLAN_ADMIN_TOKEN?.trim() ?? "";
  return Response.json(
    { token: token.length > 0 ? token : null },
    { headers: { "Cache-Control": "no-store" } },
  );
}
