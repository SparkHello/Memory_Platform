import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../src/components/ErrorBoundary";

// React 会把渲染异常也打到 console.error；stub 掉保持输出干净并断言已记录。
function silenceConsoleError() {
  return vi.spyOn(console, "error").mockImplementation(() => {});
}

function Bomb({ controller, message = "boom" }: { controller: { shouldThrow: boolean }; message?: string }) {
  if (controller.shouldThrow) throw new Error(message);
  return <p>正常内容</p>;
}

function BombHarness({ onGoHome }: { onGoHome?: () => void }) {
  const [page, setPage] = useState("memories");
  const controller = useState({ shouldThrow: true })[0];
  return (
    <>
      <button
        type="button"
        onClick={() => {
          controller.shouldThrow = false;
          setPage("dashboard");
        }}
      >
        切换页面
      </button>
      <ErrorBoundary variant="page" resetKeys={[page]} onGoHome={onGoHome}>
        <Bomb controller={controller} />
      </ErrorBoundary>
    </>
  );
}

describe("ErrorBoundary", () => {
  it("page 变体：渲染异常时显示错误页而不是子树", () => {
    const consoleError = silenceConsoleError();
    render(
      <ErrorBoundary variant="page" onGoHome={vi.fn()}>
        <Bomb controller={{ shouldThrow: true }} message="读取字段失败：x 为 undefined" />
      </ErrorBoundary>
    );

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("页面出现错误")).toBeInTheDocument();
    expect(screen.getByText(/侧栏导航和其他功能不受影响/)).toBeInTheDocument();
    expect(screen.getByText(/读取字段失败/)).toBeInTheDocument();
    expect(screen.queryByText("正常内容")).not.toBeInTheDocument();
    expect(consoleError).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "重试" })).toHaveFocus();
  });

  it("「重试」清除错误后子组件不再抛错即恢复正常渲染", async () => {
    silenceConsoleError();
    const user = userEvent.setup();
    const controller = { shouldThrow: true };
    render(
      <ErrorBoundary variant="page">
        <Bomb controller={controller} />
      </ErrorBoundary>
    );
    expect(screen.getByText("页面出现错误")).toBeInTheDocument();

    controller.shouldThrow = false;
    await user.click(screen.getByRole("button", { name: "重试" }));

    expect(screen.getByText("正常内容")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("「返回工作室」触发 onGoHome，resetKeys 变化后自动复位", async () => {
    silenceConsoleError();
    const user = userEvent.setup();
    const onGoHome = vi.fn();
    render(<BombHarness onGoHome={onGoHome} />);
    expect(screen.getByText("页面出现错误")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "返回工作室" }));
    expect(onGoHome).toHaveBeenCalledTimes(1);

    // 点击后错误页仍在（父组件尚未导航）；切页导致 resetKeys 变化后自动复位。
    await user.click(screen.getByRole("button", { name: "切换页面" }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("overlay 变体：只提供「关闭」，不提供「重试」", async () => {
    silenceConsoleError();
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(
      <ErrorBoundary variant="overlay" onDismiss={onDismiss}>
        <Bomb controller={{ shouldThrow: true }} />
      </ErrorBoundary>
    );

    expect(screen.getByText("此面板出现错误")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("「复制诊断信息」走 Clipboard API，成功后显示已复制", async () => {
    silenceConsoleError();
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });
    render(
      <ErrorBoundary variant="page">
        <Bomb controller={{ shouldThrow: true }} message="诊断样本" />
      </ErrorBoundary>
    );

    await user.click(screen.getByRole("button", { name: "复制诊断信息" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument()
    );
    const payload = writeText.mock.calls[0]?.[0] as string;
    expect(payload).toContain("诊断样本");
    expect(payload).toContain("组件栈");
    expect(payload).toContain(window.location.href);
  });

  it("Clipboard API 在 LAN HTTP 拒绝时回退到 execCommand", async () => {
    silenceConsoleError();
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: vi.fn().mockRejectedValue(new DOMException("NotAllowedError"))
      }
    });
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand
    });
    render(
      <ErrorBoundary variant="page">
        <Bomb controller={{ shouldThrow: true }} />
      </ErrorBoundary>
    );

    await user.click(screen.getByRole("button", { name: "复制诊断信息" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument()
    );
    expect(execCommand).toHaveBeenCalledWith("copy");
  });
});
