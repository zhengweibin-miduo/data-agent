import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createConversation, sendChatTurn } from "../api/dataAgent";
import { WorkbenchPage } from "./WorkbenchPage";

vi.mock("../api/dataAgent", () => ({
  createConversation: vi.fn(),
  getJob: vi.fn(),
  previewDDL: vi.fn(),
  sendChatTurn: vi.fn(),
  submitAnswers: vi.fn(),
  submitDDL: vi.fn(),
}));

describe("workbench chat", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.mocked(createConversation).mockReset().mockResolvedValue({ uid: "conversation-1" });
    vi.mocked(sendChatTurn).mockReset();
  });

  it("reuses the failed turn UID without duplicating the user message", async () => {
    vi.mocked(sendChatTurn)
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "重试成功" } });
    render(<WorkbenchPage />);

    fireEvent.change(screen.getByLabelText("补充业务背景或询问当前 DDL"), {
      target: { value: "解释订单表" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    const retry = await screen.findByRole("button", { name: "重试上一轮 AI 回复" });
    fireEvent.click(retry);

    await screen.findByText("重试成功");
    expect(sendChatTurn).toHaveBeenCalledTimes(2);
    const firstAttempt = vi.mocked(sendChatTurn).mock.calls[0]?.[1];
    const secondAttempt = vi.mocked(sendChatTurn).mock.calls[1]?.[1];
    expect(secondAttempt?.turn_uid).toBe(firstAttempt?.turn_uid);
    await waitFor(() => expect(screen.getAllByText("解释订单表")).toHaveLength(1));
  });

  it("shows the server-aligned source and DDL submission constraints", () => {
    render(<WorkbenchPage />);

    expect(screen.getByText("1–128 字符：字母、数字、下划线、点或连字符。")).toBeInTheDocument();
    expect(screen.getByText(/\/ 262,144 bytes$/)).toBeInTheDocument();
    expect(screen.getByText("50 tables · 500 columns")).toBeInTheDocument();
    expect(screen.getByLabelText("数据源")).toHaveAttribute("maxlength", "128");
  });
});
