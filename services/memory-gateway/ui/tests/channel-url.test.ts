import { describe, expect, it } from "vitest";
import { channelUrlKey, distinctEmbeddingBaseUrl } from "../src/utils/channelUrl";

describe("channelUrl", () => {
  it("treats trailing slashes as the same channel URL", () => {
    expect(channelUrlKey(" https://api.example/v1/ ")).toBe("https://api.example/v1");
  });

  it("omits embedding_base_url when it matches the chat address", () => {
    expect(
      distinctEmbeddingBaseUrl(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/",
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
      )
    ).toBeUndefined();
    expect(distinctEmbeddingBaseUrl("https://api.example/v1", "")).toBeUndefined();
  });

  it("keeps a distinct embedding access point", () => {
    expect(
      distinctEmbeddingBaseUrl(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/api/v2/apps/workspace/compatible-mode/v1/"
      )
    ).toBe("https://dashscope.aliyuncs.com/api/v2/apps/workspace/compatible-mode/v1");
  });
});
