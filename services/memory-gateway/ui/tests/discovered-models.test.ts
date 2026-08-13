import { describe, expect, it } from "vitest";
import { filterDiscoveredChatModels, isLikelyChatModelId } from "../src/utils/discoveredModels";

describe("discovered chat model filter", () => {
  it("keeps chat IDs and drops embedding, tts, and image generators", () => {
    expect(isLikelyChatModelId("deepseek-v4-flash-0731")).toBe(true);
    expect(isLikelyChatModelId("qwen3.8-max")).toBe(true);
    expect(isLikelyChatModelId("qwen3.7-text-embedding")).toBe(false);
    expect(isLikelyChatModelId("qwen-tts-flash")).toBe(false);
    expect(isLikelyChatModelId("qwen-image-2.0")).toBe(false);
  });

  it("filters by substring and falls back to the full list when nothing looks like chat", () => {
    const models = [
      { id: "deepseek-chat" },
      { id: "qwen3.7-text-embedding" },
      { id: "qwen-max", aliases: ["qwen-largest"] }
    ];
    expect(filterDiscoveredChatModels(models).map((model) => model.id)).toEqual([
      "deepseek-chat",
      "qwen-max"
    ]);
    expect(filterDiscoveredChatModels(models, "largest").map((model) => model.id)).toEqual([
      "qwen-max"
    ]);
    expect(filterDiscoveredChatModels([{ id: "text-embedding-v3" }])).toEqual([
      { id: "text-embedding-v3" }
    ]);
  });
});
