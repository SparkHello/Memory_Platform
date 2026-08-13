import { describe, expect, it } from "vitest";
import { ApiError } from "../src/api";
import { errorMessage, inferErrorCredential } from "../src/utils/format";

describe("errorMessage credential typing", () => {
  it("defaults bare 401 to Console token guidance", () => {
    const error = new ApiError(401, "Unauthorized", undefined, undefined, "/memories");
    expect(errorMessage(error)).toMatch(/Console token/);
    expect(errorMessage(error)).toMatch(/gateway\.txt/);
    expect(inferErrorCredential(error)).toBe("console");
  });

  it("maps mechanism-only bearer token detail to Console token guidance", () => {
    const error = new ApiError(
      401,
      "Authorization Bearer token 无效",
      undefined,
      undefined,
      "/memories/report"
    );
    expect(errorMessage(error)).toMatch(/Console token/);
    expect(errorMessage(error)).toMatch(/gateway\.txt/);
    expect(inferErrorCredential(error)).toBe("console");
  });

  it("uses admin copy for /providers/admin paths", () => {
    const error = new ApiError(
      401,
      "Unauthorized",
      undefined,
      undefined,
      "/providers/admin/check"
    );
    expect(errorMessage(error)).toMatch(/admin\.txt/);
    expect(errorMessage(error)).toMatch(/不是同一把钥匙/);
    expect(inferErrorCredential(error)).toBe("admin");
  });

  it("honors explicit credential option over path", () => {
    const error = new ApiError(401, "Unauthorized", undefined, undefined, "/memories");
    expect(errorMessage(error, { credential: "admin" })).toMatch(/admin\.txt/);
  });

  it("prefers structured admin codes", () => {
    const error = new ApiError(
      401,
      "请输入 Model Gateway admin 客户端密钥后再执行配置操作",
      "admin_key_required",
      undefined,
      "/providers/admin/configuration"
    );
    expect(inferErrorCredential(error)).toBe("admin");
    expect(errorMessage(error)).toMatch(/admin/);
    expect(errorMessage(error)).not.toMatch(/^访问凭证无效，请在设置中核对当前设备的 Console token$/);
  });

  it("keeps specific server detail for admin 401 and adds cross-key hint when needed", () => {
    const error = new ApiError(
      401,
      "请输入 Model Gateway admin 客户端密钥后再执行配置操作",
      undefined,
      undefined,
      "/providers/admin/check"
    );
    const message = errorMessage(error);
    expect(message).toContain("请输入 Model Gateway admin");
    expect(message).toMatch(/不是同一把钥匙|Console token/);
  });

  it("does not force Console token wording on admin 401 with full server message", () => {
    const error = new ApiError(
      401,
      "Model Gateway 拒绝了 admin 密钥。请确认粘贴的是 credentials/admin.txt，而不是登录网页用的 Console token（gateway.txt）",
      "admin_auth_failed",
      undefined,
      "/providers/admin/check"
    );
    expect(errorMessage(error)).toContain("credentials/admin.txt");
    expect(errorMessage(error)).not.toMatch(/请在设置中核对当前设备的 Console token$/);
  });

  it("still surfaces non-401 detail statuses", () => {
    const error = new ApiError(422, "memory_access 只能是 read 或 read-write");
    expect(errorMessage(error)).toContain("memory_access");
  });
});
