This is a [Next.js](https://nextjs.org) project bootstrapped with [`create-next-app`](https://nextjs.org/docs/app/api-reference/cli/create-next-app).

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
# or
bun dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

This project uses [`next/font`](https://nextjs.org/docs/app/building-your-application/optimizing/fonts) to automatically optimize and load [Geist](https://vercel.com/font), a new font family for Vercel.

## Learn More

To learn more about Next.js, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.

You can check out [the Next.js GitHub repository](https://github.com/vercel/next.js) - your feedback and contributions are welcome!

## Deploy on Vercel

The easiest way to deploy your Next.js app is to use the [Vercel Platform](https://vercel.com/new?utm_medium=default-template&filter=next.js&utm_source=create-next-app&utm_campaign=create-next-app-readme) from the creators of Next.js.

Check out our [Next.js deployment documentation](https://nextjs.org/docs/app/building-your-application/deploying) for more details.

## 部署（本项目的两份前端托管）

同一份 `frontend/` 代码同时部署到两个平台，**后端的 `TRAVELPLAN_CORS_ORIGINS` 必须同时包含这两个域名**，
否则被漏掉的那一个在浏览器里会直接 `TypeError: Failed to fetch`（不是 500，很容易误判成后端挂了）：

| 平台 | 用途 | 域名 |
| --- | --- | --- |
| Vercel | 国外访问 | `https://travelplan-web.vercel.app` |
| EdgeOne Pages | 国内访问 | `https://travelplan-web2-tf8jdorj.edgeone.dev` |

两个编译期变量都必须内联进浏览器包，**改了要重新构建才生效**：

- `NEXT_PUBLIC_API_BASE_URL` —— 生产必须指向 `https://travelplan-api.onrender.com`
- `NEXT_PUBLIC_USE_MOCK_API=false`

`.env.production` 入库就是为了让平台自动构建时天然拿到正确的后端地址；
EdgeOne 另外在 `edgeone.json` 的 `buildCommand` 里显式注入（因为平台会按过滤规则丢掉 `.env*`）。

管理端 Token（`TRAVELPLAN_ADMIN_TOKEN`）**只放本地 `.env.local`**：配了它，`/admin` 打开就自动填入
（同源路由 `/admin/default-token` 服务端读出，不进浏览器包）。前端托管平台上一律不要配这个变量——
那条路由会把它原样返回给调用者；线上没有它时返回空值，管理台照旧手填。

**Vercel 侧的固定配置**（改错会导致 Git 构建失败，因为 Next.js 应用在子目录里）：

- Root Directory = `frontend`
- Framework Preset = `Next.js`
- Production Branch = `main`

部署后**必查**：线上包里内联的是不是生产后端域名，而不是 `localhost:8000`。
站点能打开、静态资源全 200 并不代表配置对了——这是最容易静默出错的一项。

```bash
curl -s https://<域名>/ | grep -oE '/_next/static/[^"\\]+\.js' | sort -u \
  | while read -r f; do curl -s "https://<域名>$f"; done \
  | grep -o "travelplan-api.onrender.com" | head -1
```
