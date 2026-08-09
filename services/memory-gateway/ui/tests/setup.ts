import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Node 26 exposes an incomplete experimental localStorage global that can shadow
// jsdom's implementation. Use a deterministic in-memory Storage for UI tests.
const values = new Map<string, string>();
const testStorage: Storage = {
  get length() {
    return values.size;
  },
  clear() {
    values.clear();
  },
  getItem(key) {
    return values.get(key) ?? null;
  },
  key(index) {
    return [...values.keys()][index] ?? null;
  },
  removeItem(key) {
    values.delete(key);
  },
  setItem(key, value) {
    values.set(key, String(value));
  }
};

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: testStorage
});

afterEach(() => {
  cleanup();
  testStorage.clear();
});
