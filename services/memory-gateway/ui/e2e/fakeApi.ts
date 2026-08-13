import type { Page, Request, Route } from "@playwright/test";

export type RecordedApiCall = {
  method: string;
  pathname: string;
  search: string;
  body: unknown;
  authorizationPresent: boolean;
};

export type FakeApiState = {
  calls: RecordedApiCall[];
  blockedExternalUrls: string[];
  unknownApiPaths: string[];
  exportBodies: unknown[];
  purgePreviewBodies: unknown[];
  purgeCommitBodies: unknown[];
};

const revision = "a".repeat(64);

const emptyControl = {
  revision,
  admin_required: true,
  connections: [],
  deployments: [],
  routes: [],
  pricing: []
};

function syntheticMemory(index: number, deleted = false) {
  const number = String(index).padStart(2, "0");
  return {
    id: `${deleted ? "deleted" : "active"}-memory-${number}`,
    user_id: "e2e-user",
    content: `${deleted ? "合成回收站记忆" : "合成记忆"} ${number}：用于移动端浏览器验收，不含真实用户数据。`,
    type: "semantic",
    importance: 6,
    confidence: 0.96,
    valence: 0,
    arousal: 0,
    source_message: null,
    source_conversation_id: null,
    last_used_at: null,
    usage_count: index,
    stability: "stable",
    valid_from: null,
    valid_until: null,
    review_after: null,
    sensitivity: "normal",
    evidence_memory_ids: [],
    topics: ["合成测试"],
    entities: [],
    space_ids: [],
    temporal_subject: null,
    temporal_predicate: null,
    status: "dynamic",
    revision: 1,
    created_at: "2026-08-09T08:00:00+00:00",
    updated_at: "2026-08-09T08:00:00+00:00",
    deleted_at: deleted ? "2026-08-09T09:00:00+00:00" : null
  };
}

const activeMemories = Array.from({ length: 32 }, (_, index) => syntheticMemory(index + 1));
const deletedMemories = Array.from({ length: 2 }, (_, index) => syntheticMemory(index + 1, true));

const providersStatus = {
  runtime: {
    model_gateway_enabled: true,
    model_gateway_base_url: "http://model-gateway:2030/v1",
    chat_source: "model_gateway",
    knowledge_source: "model_gateway",
    providers_path: "",
    routes_path: ""
  },
  embedding: {
    model: "memory.embedding",
    base_url: "http://model-gateway:2030/v1",
    dimensions: 1024,
    configured: false,
    mode: "auto",
    state: "off",
    code: "embedding_route_off"
  },
  providers: [],
  routes: [],
  control: emptyControl,
  config_error: "",
  setup: {
    state: "needs_model",
    service_ready: true,
    model_gateway_connected: true,
    chat_ready: false,
    required_chat_routes: ["memory.chat"],
    usable_chat_routes: [],
    missing_chat_routes: ["memory.chat"],
    next_action: "configure_model"
  }
};

const purgePreview = {
  requested_memory_ids: ["deleted-memory-01"],
  purge_memory_ids: ["deleted-memory-01", "deleted-memory-02"],
  dependent_memory_ids: ["deleted-memory-02"],
  affected_core_memory_sections: [
    { id: "synthetic-core", section: "profile", version: 2, active: true }
  ],
  fingerprint: "b".repeat(64),
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
  preview_token: "synthetic-signed-preview-token",
  expires_at: "2026-08-09T12:10:00+00:00"
};

function isStaticRequest(url: URL): boolean {
  return (
    url.pathname === "/ui" ||
    url.pathname.startsWith("/ui/") ||
    url.pathname.startsWith("/@") ||
    url.pathname.startsWith("/src/") ||
    url.pathname.startsWith("/node_modules/") ||
    url.pathname.startsWith("/@fs/") ||
    url.pathname === "/favicon.ico"
  );
}

function requestBody(request: Request): unknown {
  const raw = request.postData();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return "<non-json-body>";
  }
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body)
  });
}

export async function installFakeApi(page: Page): Promise<FakeApiState> {
  const state: FakeApiState = {
    calls: [],
    blockedExternalUrls: [],
    unknownApiPaths: [],
    exportBodies: [],
    purgePreviewBodies: [],
    purgeCommitBodies: []
  };

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (!['127.0.0.1', 'memory-platform.test'].includes(url.hostname)) {
      state.blockedExternalUrls.push(url.toString());
      await route.abort("blockedbyclient");
      return;
    }
    if (isStaticRequest(url)) {
      await route.continue();
      return;
    }

    const call: RecordedApiCall = {
      method: request.method(),
      pathname: url.pathname,
      search: url.search,
      body: requestBody(request),
      authorizationPresent: Boolean(request.headers().authorization)
    };
    state.calls.push(call);

    if (url.pathname === "/health") return json(route, { status: "ok" });
    if (url.pathname === "/memories/health") {
      return json(route, {
        status: "ok",
        checked_at: "2026-08-09T10:00:00+00:00",
        summary: { errors: 0, warnings: 0, info: 0 },
        issues: []
      });
    }
    if (url.pathname === "/memories/report") {
      return json(route, {
        user_id: "e2e-user",
        generated_at: "2026-08-09T10:00:00+00:00",
        counts: { active_memories: activeMemories.length, deleted_memories: 2, core_sections: 0 },
        memory_spaces: [],
        sections: []
      });
    }
    if (url.pathname === "/memories/review") {
      return json(route, { total: activeMemories.length, recommendations: [] });
    }
    if (url.pathname === "/memories/evaluation/recall/workbench") {
      return json(route, {
        snapshot: "synthetic",
        labels_path: "synthetic",
        user_id: "e2e-user",
        target_label_min: 0,
        target_label_max: 0,
        labels: [],
        summary: { queries_total: 0, queries_graded: 0 },
        validation_issues: [],
        candidates: [],
        last_results: {}
      });
    }
    if (url.pathname === "/knowledge/status") {
      return json(route, {
        available: true,
        status: "ready",
        counts: { active: 0, deleted: 0, failed: 0 },
        max_document_bytes: 5_242_880
      });
    }
    if (url.pathname === "/providers/status") return json(route, providersStatus);
    if (url.pathname === "/providers/admin/check") return json(route, { valid: true });
    if (url.pathname === "/providers/admin/configuration") return json(route, emptyControl);
    if (url.pathname === "/providers/channels/discover") {
      return json(route, {
        valid: true,
        persisted: false,
        revision,
        candidate: {
          connection_id: "",
          channel_operator: "deepseek",
          base_url: "https://api.deepseek.com",
          adapter: "deepseek",
          auth_type: "bearer",
          allowed_private_networks: [],
          models_endpoint: "/models"
        },
        models: [{ id: "synthetic-chat-model", model_author: "unknown", aliases: [] }],
        report: {
          mode: "discovery",
          summary: { ok: 1 },
          connections: [
            {
              connection_id: "synthetic",
              status: "ok",
              level: "ok",
              detail: "合成渠道只读检查通过",
              discovered_model_count: 1,
              discovered_models: ["synthetic-chat-model"]
            }
          ]
        }
      });
    }
    if (url.pathname === "/providers/channel-bundles/validate") {
      return json(route, {
        valid: true,
        applied: false,
        connection_id: "synthetic",
        deployment_ids: ["synthetic-chat"],
        changed_routes: ["memory.chat"],
        revision,
        discovery: { mode: "discovery", summary: { ok: 1 }, connections: [] }
      });
    }
    if (url.pathname === "/providers/channel-bundles/apply") {
      return json(route, {
        valid: true,
        applied: true,
        connection_id: "synthetic",
        deployment_ids: ["synthetic-chat"],
        changed_routes: ["memory.chat"],
        revision: "c".repeat(64),
        discovery: { mode: "discovery", summary: { ok: 1 }, connections: [] }
      });
    }
    if (url.pathname === "/auth/tokens") {
      return json(route, {
        data: [],
        current_user_id: "e2e-user",
        legacy_key_enabled: false,
        authenticated_with_legacy_key: false,
        allowed_create_roles: ["chat", "mcp"]
      });
    }
    if (url.pathname === "/memories/spaces") return json(route, { data: [] });
    if (url.pathname === "/memories/deleted" && request.method() === "GET") {
      return json(route, { data: deletedMemories });
    }
    if (url.pathname === "/memories" && request.method() === "GET") {
      return json(route, { data: activeMemories });
    }
    if (url.pathname === "/memories/export/selection") {
      state.exportBodies.push(call.body);
      const ids = ((call.body as { memory_ids?: string[] } | null)?.memory_ids || []);
      return json(route, {
        version: 3,
        user_id: "e2e-user",
        memories: activeMemories.filter((memory) => ids.includes(memory.id)),
        deleted_memories: [],
        core_memory_sections: [],
        memory_spaces: []
      });
    }
    if (url.pathname === "/memories/deleted/purge/preview") {
      state.purgePreviewBodies.push(call.body);
      return json(route, purgePreview);
    }
    if (url.pathname === "/memories/deleted/purge/commit") {
      state.purgeCommitBodies.push(call.body);
      return json(route, {
        purged: true,
        requested_memory_ids: purgePreview.requested_memory_ids,
        purged_memory_ids: purgePreview.purge_memory_ids,
        dependent_memory_ids: purgePreview.dependent_memory_ids,
        affected_core_memory_sections: purgePreview.affected_core_memory_sections,
        fingerprint: purgePreview.fingerprint,
        effects: purgePreview.effects,
        audit_log_id: "synthetic-audit"
      });
    }

    state.unknownApiPaths.push(`${request.method()} ${url.pathname}${url.search}`);
    return json(route, { detail: "unhandled synthetic API request" }, 404);
  });

  return state;
}

export async function seedConsoleSettings(page: Page, apiBaseUrl: string) {
  await page.addInitScript((baseUrl) => {
    localStorage.setItem("memory-console.apiBaseUrl", baseUrl);
    localStorage.setItem(
      "memory-console.gatewayApiKey",
      "mgw_e2econsole01_synthetic_console_secret_0000000000000000"
    );
    localStorage.setItem("memory-console.userId", "e2e-user");
    localStorage.setItem("memory-console.uiMode", "simple");
  }, apiBaseUrl);
}
