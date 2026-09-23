/**
 * 高德 JS SDK v2 加载器。
 *
 * 只负责一件事：注入 `https://webapi.amap.com/maps?v=2.0&key=...` 并返回全局 `AMap`
 * 命名空间。脚本只注入一次，加载结果在页面内全局复用（避免切换 Day 时重复拉取 SDK）。
 *
 * SSR 安全：模块顶层不碰 `window`，只能在浏览器里调用 `loadAmap`。
 */

/** 结果页只用到的 AMap API 子集，按需补充，不做全量声明。 */
export interface AMapMarkerInstance {
  on(event: "click", handler: () => void): void;
  setContent(content: string): void;
}

export interface AMapPolylineInstance {
  show(): void;
  hide(): void;
}

export interface AMapMapInstance {
  add(overlays: unknown[]): void;
  remove(overlays: unknown[]): void;
  setFitView(overlays?: unknown[], immediately?: boolean, padding?: number[]): void;
  destroy(): void;
}

export interface AMapNamespace {
  Map: new (container: HTMLElement, options: Record<string, unknown>) => AMapMapInstance;
  Marker: new (options: {
    position: [number, number];
    content: string;
    title?: string;
    anchor?: string;
    zIndex?: number;
  }) => AMapMarkerInstance;
  Polyline: new (options: {
    path: [number, number][];
    strokeColor?: string;
    strokeWeight?: number;
    strokeOpacity?: number;
    lineJoin?: "round" | "miter" | "bevel";
  }) => AMapPolylineInstance;
}

const SCRIPT_ID = "amap-sdk-script";
const LOAD_TIMEOUT_MS = 10_000;

let loadPromise: Promise<AMapNamespace> | null = null;

function amapGlobal(): AMapNamespace | undefined {
  return (window as unknown as { AMap?: AMapNamespace }).AMap;
}

function waitForAmap(timeoutMs: number): Promise<AMapNamespace> {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const poll = () => {
      const api = amapGlobal();
      if (api) {
        resolve(api);
        return;
      }
      if (Date.now() - startedAt >= timeoutMs) {
        reject(new Error("AMap SDK 初始化超时（Key 无效或缺少安全密钥时会这样）"));
        return;
      }
      window.setTimeout(poll, 200);
    };
    poll();
  });
}

/**
 * 加载高德 SDK 并返回 `AMap` 命名空间。
 *
 * 脚本加载成功但 `window.AMap` 迟迟不出现（Key 无效 / 需要 securityJsCode）时按失败处理，
 * 由调用方回落示意图。
 */
export function loadAmap(key: string): Promise<AMapNamespace> {
  if (loadPromise) return loadPromise;

  loadPromise = new Promise<AMapNamespace>((resolve, reject) => {
    if (typeof window === "undefined") {
      reject(new Error("loadAmap 只能在浏览器环境调用"));
      return;
    }

    const existing = amapGlobal();
    if (existing) {
      resolve(existing);
      return;
    }

    const script = document.createElement("script");
    script.id = SCRIPT_ID;
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`;
    script.async = true;

    let settled = false;
    const fail = (reason: string) => {
      if (settled) return;
      settled = true;
      loadPromise = null; // 允许下次调用重试
      reject(new Error(reason));
    };

    script.onerror = () => fail("AMap SDK 脚本加载失败");
    script.onload = () => {
      waitForAmap(LOAD_TIMEOUT_MS).then(
        (api) => {
          if (settled) return;
          settled = true;
          resolve(api);
        },
        (error: unknown) => {
          fail(error instanceof Error ? error.message : String(error));
        },
      );
    };

    document.head.appendChild(script);
  });

  return loadPromise;
}
