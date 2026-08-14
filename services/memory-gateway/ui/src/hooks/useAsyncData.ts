import { useCallback, useEffect, useState, type DependencyList } from "react";
import { isAbortError } from "../api";
import { errorMessage } from "../utils/format";

export type LoadState<T> = {
  loading: boolean;
  error: string | null;
  data: T | null;
};

// 页面数据加载模板：挂载和依赖变化时带 AbortController 拉取，
// 过期请求在 cleanup 里被 abort，直接丢弃，不覆盖新结果。
// reload() 用于刷新按钮等手动重取（不带 signal，不会被取消）。
export function useAsyncData<T>(
  fetcher: (signal?: AbortSignal) => Promise<T>,
  deps: DependencyList
): { state: LoadState<T>; reload: () => Promise<void> } {
  const [state, setState] = useState<LoadState<T>>({
    loading: true,
    error: null,
    data: null
  });

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setState({ loading: true, error: null, data: null });
      try {
        setState({ loading: false, error: null, data: await fetcher(signal) });
      } catch (error) {
        if (isAbortError(error)) return;
        setState({ loading: false, error: errorMessage(error), data: null });
      }
    },
    // fetcher 由调用方内联编写，依赖通过 deps 显式传入。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return { state, reload: load };
}
