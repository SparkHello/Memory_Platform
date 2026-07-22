export type LoadState<T> = {
  loading: boolean;
  error: string | null;
  data: T | null;
};
