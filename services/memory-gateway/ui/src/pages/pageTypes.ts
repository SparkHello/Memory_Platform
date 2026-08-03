import type { ToastKind } from "../hooks/useToast";

export type Notify = (message: string, kind?: ToastKind) => void;
