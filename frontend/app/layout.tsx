import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { AppHeader } from "@/components/app-header";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "TravelPlan · 用真实信息规划中国国内行程",
    template: "%s · TravelPlan",
  },
  description:
    "TravelPlan 会先查交通、酒店、攻略与路线，再给出可执行的行程：每条安排都能看到来源、可信度与广告风险，价格标注查询时间。",
  applicationName: "TravelPlan",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="zh-CN"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <AppHeader />
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
