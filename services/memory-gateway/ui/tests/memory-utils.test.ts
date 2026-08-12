import { describe, expect, it } from "vitest";
import { contentDivergesFromSource } from "../src/utils/memory";

describe("contentDivergesFromSource", () => {
  it("不把第三人称改写当作偏离", () => {
    expect(
      contentDivergesFromSource("用户喜欢黑咖啡和爵士乐。", "我喜欢黑咖啡和爵士乐")
    ).toBe(false);
  });

  it("编辑加入原话没有的事实时判定偏离", () => {
    expect(
      contentDivergesFromSource(
        "用户喜欢喝拿铁，讨厌美式，每天必须两杯。",
        "我喜欢黑咖啡"
      )
    ).toBe(true);
  });

  it("英文正文同样按词覆盖判定", () => {
    expect(
      contentDivergesFromSource("User prefers dark mode in Kelivo.", "我平时用 Kelivo 时开 dark mode")
    ).toBe(false);
    expect(
      contentDivergesFromSource(
        "User switched to light theme with large fonts.",
        "我平时用 Kelivo 时开 dark mode"
      )
    ).toBe(true);
  });

  it("来源为空或正文过短时不提示", () => {
    expect(contentDivergesFromSource("用户喜欢咖啡。", null)).toBe(false);
    expect(contentDivergesFromSource("好的", "我喜欢黑咖啡")).toBe(false);
  });
});
