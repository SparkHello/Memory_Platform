import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearModelAdminKey,
  loadModelAdminKey,
  saveModelAdminKey
} from "../src/utils/adminKeySession";

const SESSION_KEY = "memory-console.modelAdminKey";

describe("admin key session", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("初始状态读取为空字符串", () => {
    expect(loadModelAdminKey()).toBe("");
  });

  it("保存写入 sessionStorage，同标签页内可恢复", () => {
    saveModelAdminKey("mg_admin_synthetic_key");
    expect(sessionStorage.getItem(SESSION_KEY)).toBe("mg_admin_synthetic_key");
    expect(loadModelAdminKey()).toBe("mg_admin_synthetic_key");
  });

  it("清除后 sessionStorage 不再保留，读取回到空字符串", () => {
    saveModelAdminKey("mg_admin_synthetic_key");
    clearModelAdminKey();
    expect(sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(loadModelAdminKey()).toBe("");
  });

  it("保存/读取/清除全程不触碰 localStorage", () => {
    const setItem = vi.spyOn(localStorage, "setItem");
    const removeItem = vi.spyOn(localStorage, "removeItem");
    saveModelAdminKey("mg_admin_synthetic_key");
    loadModelAdminKey();
    clearModelAdminKey();
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it("sessionStorage 写入受限时静默降级：读取为空、写入与清除不抛出", () => {
    const denied = () => {
      throw new DOMException("denied", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(denied);

    expect(loadModelAdminKey()).toBe("");
    expect(() => saveModelAdminKey("mg_admin_synthetic_key")).not.toThrow();
    expect(() => clearModelAdminKey()).not.toThrow();
  });
});
