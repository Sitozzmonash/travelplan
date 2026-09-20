"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { toAdminApiError, type AdminApiError } from "@/lib/admin-api";

export interface AdminResource<T> {
  data: T | null;
  error: AdminApiError | null;
  loading: boolean;
  reload: () => void;
}

interface ResourceState<T> {
  /** 最近一次拿到结果的请求标识（key#nonce）；与当前请求不同即表示正在加载。 */
  requestId: string;
  data: T | null;
  error: AdminApiError | null;
}

const EMPTY_SUBSCRIBE = () => () => {};

/**
 * 客户端水合标记。
 *
 * 管理台依赖只在浏览器里存在的状态（Token 等），服务端渲染阶段拿不到。
 * 用 useSyncExternalStore 的 server snapshot 判断，而不是 useEffect + setState：
 * 既不会 hydration mismatch，也不会触发「在 effect 里同步 setState」的反模式。
 */
export function useHydrated(): boolean {
  return useSyncExternalStore(EMPTY_SUBSCRIBE, () => true, () => false);
}

/**
 * 单个管理端点的读取状态机：loading / error / data + 手动重试。
 *
 * 为什么用 `key` 而不是依赖数组：
 * 调用方大多把过滤条件（status、offset…）拼进 key，key 一变就重新请求；
 * 这样 hooks 的依赖是静态可读的，不会出现「依赖漏写导致筛选项不生效」这类问题。
 *
 * loader 通过 ref 转发，拿到的是本次渲染最新的闭包，因此重试不会用上一次的旧参数。
 */
export function useAdminResource<T>(
  key: string,
  loader: () => Promise<T>,
  enabled = true,
): AdminResource<T> {
  const loaderRef = useRef(loader);
  const [state, setState] = useState<ResourceState<T>>({
    requestId: "",
    data: null,
    error: null,
  });
  const [nonce, setNonce] = useState(0);
  const requestId = `${key}#${nonce}`;
  // 派生而不是在 effect 里 setState：请求标识对不上就说明这次请求还没有结果。
  const loading = enabled && state.requestId !== requestId;

  // 先同步 loader，再执行请求：两个 effect 按声明顺序执行。
  useEffect(() => {
    loaderRef.current = loader;
  });

  useEffect(() => {
    // 未启用时不请求、也不清空已有数据：清空会让刚刚拿到的探测结论在下一次渲染里消失。
    if (!enabled) return;
    let cancelled = false;
    loaderRef.current().then(
      (data) => {
        if (!cancelled) setState({ requestId, data, error: null });
      },
      (cause: unknown) => {
        if (!cancelled) {
          // 失败时保留上一次的数据：这样「部分失败」仍能展示旧内容 + 一条错误提示。
          setState((previous) => ({
            requestId,
            data: previous.data,
            error: toAdminApiError(cause),
          }));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key, enabled, nonce, requestId]);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  return { data: state.data, error: state.error, loading, reload };
}
