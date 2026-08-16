/**
 * Model Gateway admin key 的会话保持：
 * 只写 sessionStorage（同标签页内切换页面不丢失，关闭标签页即失效），
 * 永不写 localStorage，也不进入任何日志。
 */
const MODEL_ADMIN_KEY_SESSION_KEY = "memory-console.modelAdminKey";

export function loadModelAdminKey(): string {
  try {
    return sessionStorage.getItem(MODEL_ADMIN_KEY_SESSION_KEY) || "";
  } catch {
    return "";
  }
}

export function saveModelAdminKey(key: string) {
  try {
    sessionStorage.setItem(MODEL_ADMIN_KEY_SESSION_KEY, key);
  } catch {
    // 隐私模式等写入受限场景静默降级为仅组件内存。
  }
}

export function clearModelAdminKey() {
  try {
    sessionStorage.removeItem(MODEL_ADMIN_KEY_SESSION_KEY);
  } catch {
    // 同上。
  }
}
