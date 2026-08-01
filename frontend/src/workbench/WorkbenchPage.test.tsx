import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { createConversation, getJob, previewDDL, sendChatTurn, submitAnswers, submitDDL } from "../api/dataAgent";
import { connectJobEvents } from "../api/jobEvents";
import type { JobEventData, JobRecord } from "../api/types";
import { WorkbenchPage } from "./WorkbenchPage";

vi.mock("../api/dataAgent", () => ({
  createConversation: vi.fn(),
  getJob: vi.fn(),
  previewDDL: vi.fn(),
  sendChatTurn: vi.fn(),
  submitAnswers: vi.fn(),
  submitDDL: vi.fn(),
}));
vi.mock("../api/jobEvents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/jobEvents")>();
  return { ...actual, connectJobEvents: vi.fn(() => ({ close: vi.fn() })) };
});

const waitingJob = (overrides: Partial<JobRecord> = {}): JobRecord => ({
  job_id: "job-1", source: "warehouse", status: "waiting_input", revision: 2, attempt: 1,
  question_round: 1, question_set_id: "set-2", questions: [{
    question_id: "question-2", prompt: "第二轮问题", fact_table_id: "orders", column_ids: [], required: true,
  }], result: null, error: null, ...overrides,
});

describe("workbench chat", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.mocked(createConversation).mockReset().mockResolvedValue({ uid: "conversation-1" });
    vi.mocked(sendChatTurn).mockReset();
    vi.mocked(getJob).mockReset();
    vi.mocked(previewDDL).mockReset();
    vi.mocked(submitDDL).mockReset();
    vi.mocked(submitAnswers).mockReset();
    vi.mocked(connectJobEvents).mockClear();
    window.history.replaceState(null, "", "/workbench");
  });

  it("blocks a new chat turn until the failed turn is retried", async () => {
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new Error("temporary failure"));
    render(<WorkbenchPage />);
    const input = screen.getByLabelText("补充业务背景或询问当前 DDL");
    fireEvent.change(input, { target: { value: "第一轮" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    await screen.findByRole("button", { name: "重试上一轮 AI 回复" });
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 →" })).toBeDisabled();
    expect(sendChatTurn).toHaveBeenCalledTimes(1);
  });

  it("clears stale clarification coordinates until the authoritative read succeeds", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValueOnce(waitingJob({ question_set_id: "set-1" }));
    render(<WorkbenchPage />);
    expect(await screen.findByText("第二轮问题")).toBeInTheDocument();

    const event: JobEventData = {
      job_id: "job-1", status: "waiting_input", stage: "waiting_input", revision: 3, attempt: 2,
      emitted_at: new Date().toISOString(), questions: waitingJob().questions, result: null, error: null,
    };
    vi.mocked(connectJobEvents).mock.calls[0]?.[1].onEvent(event);
    await waitFor(() => expect(screen.queryByText("第二轮问题")).not.toBeInTheDocument());
    vi.mocked(getJob).mockRejectedValueOnce(new Error("GET failed"));
    await expect(vi.mocked(connectJobEvents).mock.calls[0]![1].getAuthoritativeJob()).rejects.toThrow();
  });

  it("restores the server source without enabling preview or chat for sample DDL", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue(waitingJob({ status: "succeeded", question_set_id: null, questions: null }));
    render(<WorkbenchPage />);

    await waitFor(() => expect(screen.getByLabelText("数据源")).toHaveValue("warehouse"));
    expect(screen.getByLabelText("MySQL DDL")).toHaveValue("");
    expect(screen.getByRole("button", { name: "预览结构" })).toBeDisabled();
    expect(screen.getByLabelText("补充业务背景或询问当前 DDL")).toBeDisabled();
  });

  it("clears and locks sample inputs while a deep-link restore is pending", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    let resolveRestore!: (job: JobRecord) => void;
    vi.mocked(getJob).mockImplementation(() => new Promise((resolve) => { resolveRestore = resolve; }));
    render(<WorkbenchPage />);

    expect(screen.getByLabelText("数据源")).toHaveValue("");
    expect(screen.getByLabelText("数据源")).toBeDisabled();
    expect(screen.getByLabelText("MySQL DDL")).toHaveValue("");
    expect(screen.getByLabelText("MySQL DDL")).toBeDisabled();
    expect(screen.getByRole("button", { name: "预览结构" })).toBeDisabled();
    expect(screen.getByLabelText("补充业务背景或询问当前 DDL")).toBeDisabled();

    resolveRestore(waitingJob());
    await waitFor(() => expect(screen.getByLabelText("数据源")).toBeEnabled());
  });

  it("restarts the stage trace when a second task is submitted", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL)
      .mockResolvedValueOnce({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null })
      .mockResolvedValueOnce({ job_id: "job-2", status: "pending", status_url: "/jobs/job-2", events_url: null });
    render(<WorkbenchPage />);
    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成语义 →" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    await waitFor(() => expect(connectJobEvents).toHaveBeenCalledTimes(1));
    vi.mocked(connectJobEvents).mock.calls[0]![1].onJob(waitingJob({ status: "succeeded", question_set_id: null, questions: null, result: { ddl_hash: "hash", table_count: 1, column_count: 2, metric_count: 1 } }));
    expect(await screen.findAllByText("语义元数据已生成")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    await screen.findByText("任务已受理");
    await waitFor(() => expect(screen.queryByText("语义元数据已生成")).not.toBeInTheDocument());
  });

  it("discards a late task submission after the workbench unmounts", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    let resolveSubmit!: (job: { job_id: string; status: "pending"; status_url: string; events_url: null }) => void;
    vi.mocked(submitDDL).mockImplementation((_input, signal) => new Promise((resolve) => {
      expect(signal).toBeInstanceOf(AbortSignal);
      resolveSubmit = resolve;
    }));
    const rendered = render(<WorkbenchPage />);
    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成语义 →" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    rendered.unmount();

    resolveSubmit({ job_id: "late-job", status: "pending", status_url: "/jobs/late-job", events_url: null });
    await Promise.resolve();
    expect(window.location.pathname).toBe("/workbench");
    expect(connectJobEvents).not.toHaveBeenCalled();
  });

  it("marks edits after a successful submission as unsaved", async () => {
    const onUnsavedChange = vi.fn();
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    render(<WorkbenchPage onUnsavedChange={onUnsavedChange} />);
    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成语义 →" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    await waitFor(() => expect(onUnsavedChange).toHaveBeenLastCalledWith(false));
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE changed (id INT);" } });
    await waitFor(() => expect(onUnsavedChange).toHaveBeenLastCalledWith(true));
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

  it("blocks whitespace-only required clarification answers and focuses the first missing field", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue(waitingJob({
      questions: [
        { question_id: "answered", prompt: "已填写", fact_table_id: "orders", column_ids: [], required: true },
        { question_id: "missing", prompt: "仍需填写", fact_table_id: "orders", column_ids: [], required: true },
      ],
    }));
    render(<WorkbenchPage />);
    fireEvent.change(await screen.findByLabelText("已填写"), { target: { value: "有效依据" } });
    fireEvent.change(screen.getByLabelText("仍需填写"), { target: { value: "   " } });
    fireEvent.submit(screen.getByRole("button", { name: "提交回答并继续 →" }).closest("form")!);

    expect(submitAnswers).not.toHaveBeenCalled();
    expect(screen.getByLabelText("仍需填写")).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent("请填写所有必答业务依据后再继续。");
  });

  it("reuses the original DDL snapshot when a failed chat turn is retried", async () => {
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "重试成功" } });
    render(<WorkbenchPage />);
    const originalDDL = (screen.getByLabelText("MySQL DDL") as HTMLTextAreaElement).value;
    fireEvent.change(screen.getByLabelText("补充业务背景或询问当前 DDL"), { target: { value: "解释结构" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    const retry = await screen.findByRole("button", { name: "重试上一轮 AI 回复" });
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE changed (id INT);" } });
    fireEvent.click(retry);
    await screen.findByText("重试成功");

    expect(vi.mocked(sendChatTurn).mock.calls[1]?.[1].ddl_context.ddl).toBe(originalDDL);
  });

  it("recreates a missing conversation while preserving the turn UID", async () => {
    sessionStorage.setItem("schema-loom-conversation", "stale-conversation");
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new ApiError(404, { error: { code: "conversation_not_found" } }))
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "已恢复" } });
    render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("补充业务背景或询问当前 DDL"), { target: { value: "解释结构" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    await screen.findByText("已恢复");

    expect(createConversation).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem("schema-loom-conversation")).toBe("conversation-1");
    expect(vi.mocked(sendChatTurn).mock.calls[1]?.[1].turn_uid).toBe(vi.mocked(sendChatTurn).mock.calls[0]?.[1].turn_uid);
  });

  it("releases the chat retry gate after a deterministic validation failure", async () => {
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new ApiError(422, {
      error: { code: "invalid_ddl", stage: "request", retryable: false },
    }));
    render(<WorkbenchPage />);
    const input = screen.getByLabelText("补充业务背景或询问当前 DDL");
    fireEvent.change(input, { target: { value: "解释无效结构" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    await screen.findByText(/AI 请求校验失败，请修正输入后重新发送/);
    expect(screen.queryByRole("button", { name: "重试上一轮 AI 回复" })).not.toBeInTheDocument();
    expect(input).toBeEnabled();
    fireEvent.change(input, { target: { value: "修正后重新发送" } });
    expect(screen.getByRole("button", { name: "发送 →" })).toBeEnabled();
  });

  it("shows the server-aligned source and DDL submission constraints", () => {
    render(<WorkbenchPage />);

    expect(screen.getByText("1–128 字符：字母、数字、下划线、点或连字符。")).toBeInTheDocument();
    expect(screen.getByText(/\/ 262,144 bytes$/)).toBeInTheDocument();
    expect(screen.getByText("50 tables · 500 columns")).toBeInTheDocument();
    expect(screen.getByLabelText("数据源")).toHaveAttribute("maxlength", "128");
  });
});
