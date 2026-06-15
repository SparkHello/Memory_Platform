import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../utils/format";

export type LoadState<T> = {
  loading: boolean;
  error: string | null;
  data: T | null;
};

export function useAsyncData<T>(
  loader: () => Promise<T>,
  options: { immediate?: boolean } = {}
) {
  const { immediate = true } = options;
  const [state, setState] = useState<LoadState<T>>({
    loading: immediate,
    error: null,
    data: null
  });

  const load = useCallback(async () => {
    setState({ loading: true, error: null, data: null });
    try {
      setState({ loading: false, error: null, data: await loader() });
    } catch (error) {
      setState({ loading: false, error: errorMessage(error), data: null });
    }
  }, [loader]);

  useEffect(() => {
    if (immediate) {
      void load();
    }
  }, [immediate, load]);

  return { ...state, setState, load };
}
