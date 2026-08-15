import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryApi } from "../src/api";
import { ConfirmDialog } from "../src/components/ConfirmDialog";
import { AppShell } from "../src/layout/AppShell";
import { MemoriesPage } from "../src/pages/memory/MemoriesPage";
import type { UiMode } from "../src/storage";
import type { MemoryPurgePreviewResult, MemoryRecord } from "../src/types";
import { copyText } from "../src/utils/files";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(document, "execCommand");
const elementScrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");

function restoreProperty(target: object, key: PropertyKey, descriptor?: PropertyDescriptor) {
  if (descriptor) Object.defineProperty(target, key, descriptor);
  else Reflect.deleteProperty(target, key);
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
  restoreProperty(navigator, "clipboard", clipboardDescriptor);
  restoreProperty(document, "execCommand", execCommandDescriptor);
  restoreProperty(HTMLElement.prototype, "scrollTo", elementScrollDescriptor);
});

describe("safe browser fallbacks", () => {
  it("falls back when Clipboard API exists but rejects on LAN HTTP", async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException("NotAllowedError"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand
    });

    await copyText("LAN clipboard value");

    expect(writeText).toHaveBeenCalledWith("LAN clipboard value");
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).not.toBeInTheDocument();
  });

  it.each(["warning", "danger"] as const)(
    "focuses cancel for a %s confirmation",
    async (tone) => {
      render(
        <ConfirmDialog
          state={{ message: "请确认", confirmLabel: "继续", cancelLabel: "取消", tone }}
          onResolve={vi.fn()}
        />
      );

      await waitFor(() => expect(screen.getByRole("button", { name: "取消" })).toHaveFocus());
    }
  );

  it("focuses the primary action for a normal confirmation", async () => {
    render(
      <ConfirmDialog
        state={{ message: "请确认", confirmLabel: "继续", cancelLabel: "取消", tone: "default" }}
        onResolve={vi.fn()}
      />
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "继续" })).toHaveFocus());
  });
});

describe("simple and expert navigation", () => {
  function ShellHarness({ activePage = "dashboard" }: { activePage?: "dashboard" | "evaluation" }) {
    const [mode, setMode] = useState<UiMode>("simple");
    return (
      <AppShell
        activePage={activePage}
        settings={{ apiBaseUrl: "http://localhost:2026", apiKey: "gateway-key", userId: "default" }}
        serviceStatus={{ loading: false, tone: "ok", message: "已就绪" }}
        theme="dark"
        uiMode={mode}
        onToggleTheme={vi.fn()}
        onToggleUiMode={() => setMode((current) => (current === "simple" ? "expert" : "simple"))}
        onPageChange={vi.fn()}
        onRefreshService={vi.fn()}
      >
        <div>页面内容</div>
      </AppShell>
    );
  }

  it("defaults to common pages and exposes expert tools on demand", async () => {
    const user = userEvent.setup();
    render(<ShellHarness />);

    expect(screen.queryByRole("button", { name: "评测闭环" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "切换到专家模式" }));
    expect(screen.getByRole("button", { name: "评测闭环" })).toBeInTheDocument();
  });

  it("keeps an expert deep link visible while simple navigation is active", () => {
    render(<ShellHarness activePage="evaluation" />);

    expect(screen.getByText("页面内容")).toBeInTheDocument();
    expect(screen.getByText("评测闭环")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "评测闭环" })).not.toBeInTheDocument();
  });

  it("scrolls to the top when the current mobile destination is tapped again", async () => {
    const user = userEvent.setup();
    const contentScrollTo = vi.fn();
    const windowScrollTo = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: contentScrollTo
    });
    vi.stubGlobal("scrollTo", windowScrollTo);
    render(<ShellHarness />);

    await user.click(screen.getByRole("button", { name: "工作室" }));

    expect(contentScrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "auto" });
    expect(windowScrollTo).toHaveBeenCalledWith({ top: 0, left: 0, behavior: "auto" });
  });
});

describe("selected-memory export client", () => {
  it("posts only selected IDs to the server-side export endpoint", async () => {
    const payload = { version: 1, memories: [{ id: "mem-a" }, { id: "mem-b" }] };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = new MemoryApi({
      apiBaseUrl: "http://localhost:2026",
      apiKey: "gateway-key",
      userId: "default"
    });

    const result = await api.exportSelectedMemories(["mem-a", "mem-b"]);

    expect(result).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://localhost:2026/memories/export/selection");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ memory_ids: ["mem-a", "mem-b"] });
  });
});

describe("two-phase batch purge", () => {
  const preview: MemoryPurgePreviewResult = {
    requested_memory_ids: ["memory-root"],
    purge_memory_ids: ["memory-dependent", "memory-root"],
    dependent_memory_ids: ["memory-dependent"],
    affected_core_memory_sections: [
      { id: "core-profile", section: "profile", version: 2, active: true }
    ],
    fingerprint: "a".repeat(64),
    effects: {
      requested_memories_deleted: 1,
      dependent_memories_deleted: 1,
      memories_deleted: 2,
      space_links_deleted: 1,
      temporal_references_relinked: 0,
      core_sections_scrubbed: 1,
      core_history_scrubbed: 2,
      decision_logs_scrubbed: 3
    },
    preview_token: "signed-preview-token",
    expires_at: "2026-08-09T12:10:00+00:00"
  };

  it("posts the signed preview fields to the atomic commit endpoint", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(preview), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            purged: true,
            requested_memory_ids: preview.requested_memory_ids,
            purged_memory_ids: preview.purge_memory_ids,
            dependent_memory_ids: preview.dependent_memory_ids,
            affected_core_memory_sections: preview.affected_core_memory_sections,
            fingerprint: preview.fingerprint,
            effects: preview.effects,
            audit_log_id: "audit-1"
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        )
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = new MemoryApi({
      apiBaseUrl: "http://localhost:2026",
      apiKey: "gateway-key",
      userId: "default"
    });

    const planned = await api.previewDeletedMemoriesPurge(["memory-root"]);
    await api.commitDeletedMemoriesPurge(
      planned.requested_memory_ids,
      planned.fingerprint,
      planned.preview_token
    );

    const [previewUrl, previewInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(previewUrl).toBe("http://localhost:2026/memories/deleted/purge/preview");
    expect(JSON.parse(String(previewInit.body))).toEqual({ memory_ids: ["memory-root"] });
    const [commitUrl, commitInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(commitUrl).toBe("http://localhost:2026/memories/deleted/purge/commit");
    expect(JSON.parse(String(commitInit.body))).toEqual({
      memory_ids: ["memory-root"],
      fingerprint: preview.fingerprint,
      preview_token: preview.preview_token
    });
  });

  it("shows the real closure and Core impact before commit with cancel focused", async () => {
    window.location.hash = "#/memories?tab=recycle";
    const memory = {
      id: "memory-root",
      content: "Root memory",
      type: "semantic",
      status: "archived",
      importance: 5,
      confidence: 0.9,
      stability: "stable",
      sensitivity: "normal",
      usage_count: 0,
      topics: [],
      entities: [],
      space_ids: [],
      updated_at: "2026-08-09T12:00:00+00:00"
    } as MemoryRecord;
    const previewDeletedMemoriesPurge = vi.fn().mockResolvedValue(preview);
    const commitDeletedMemoriesPurge = vi.fn().mockResolvedValue({
      purged: true,
      requested_memory_ids: preview.requested_memory_ids,
      purged_memory_ids: preview.purge_memory_ids,
      dependent_memory_ids: preview.dependent_memory_ids,
      affected_core_memory_sections: preview.affected_core_memory_sections,
      fingerprint: preview.fingerprint,
      effects: preview.effects,
      audit_log_id: "audit-1"
    });
    const purgeDeletedMemory = vi.fn();
    const api = {
      listDeletedMemories: vi.fn().mockResolvedValue([memory]),
      listMemorySpaces: vi.fn().mockResolvedValue([]),
      previewDeletedMemoriesPurge,
      commitDeletedMemoriesPurge,
      purgeDeletedMemory
    } as unknown as MemoryApi;
    const notify = vi.fn();
    const user = userEvent.setup();
    render(
      <MemoriesPage
        api={api}
        notify={notify}
        openMemory={vi.fn()}
        refreshKey={0}
      />
    );

    await user.click(await screen.findByRole("checkbox", { name: "选择记忆：Root memory" }));
    await user.click(screen.getByRole("button", { name: "永久删除" }));

    expect(previewDeletedMemoriesPurge).toHaveBeenCalledWith(["memory-root"]);
    expect(await screen.findByText(/实际将永久删除/)).toHaveTextContent("2");
    expect(screen.getByText(/Core 影响/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "取消，保留记忆" })).toHaveFocus()
    );
    expect(commitDeletedMemoriesPurge).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "按以上范围永久删除" }));
    await waitFor(() =>
      expect(commitDeletedMemoriesPurge).toHaveBeenCalledWith(
        preview.requested_memory_ids,
        preview.fingerprint,
        preview.preview_token
      )
    );
    expect(purgeDeletedMemory).not.toHaveBeenCalled();
  });
});

describe("revision-aware memory clients", () => {
  it("adds expected_revision to every Memory PATCH", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ updated: true, memory: { revision: 8 } }), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ updated: true, memory: { revision: 9 } }), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        })
      );
    vi.stubGlobal("fetch", fetchMock);
    const api = new MemoryApi({
      apiBaseUrl: "http://localhost:2026",
      apiKey: "gateway-key",
      userId: "default"
    });

    await api.updateMemory("memory-a", { content: "new content" }, 7);
    await api.updateMemorySpaces("memory-a", { space_ids: ["space-a"] }, 8);

    const requests = fetchMock.mock.calls.map(([, init]) => {
      const request = init as RequestInit;
      return JSON.parse(String(request.body)) as Record<string, unknown>;
    });
    expect(requests).toEqual([
      { content: "new content", expected_revision: 7 },
      { space_ids: ["space-a"], expected_revision: 8 }
    ]);
  });
});
