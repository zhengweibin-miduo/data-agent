import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { createConversation, getJob, previewDDL, sendChatTurn, submitAnswers, submitDDL } from "../api/dataAgent";
import { connectJobEvents } from "../api/jobEvents";
import type { JobEventData, JobRecord } from "../api/types";
import { WorkbenchPage } from "./WorkbenchPage";

const lifecycleHarness = vi.hoisted(() => ({
  connections: [] as Array<{
    getAuthoritativeJob: () => Promise<JobRecord>;
    onEvent: (event: JobEventData) => void;
    onJob: (job: JobRecord) => void;
  }>,
}));

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
  return {
    ...actual,
    connectJobEvents: vi.fn((_eventsUrl, handlers) => {
      lifecycleHarness.connections.push(handlers);
      return { close: vi.fn() };
    }),
  };
});

const waitingJob = (overrides: Partial<JobRecord> = {}): JobRecord => ({
  job_id: "job-1", source: "warehouse", status: "waiting_input", revision: 2, attempt: 1,
  question_round: 1, question_set_id: "set-2", questions: [{
    question_id: "question-2", prompt: "第二轮问题", fact_table_id: "orders", column_ids: [], required: true,
  }], result: null, error: null, ...overrides,
});

const jobLifecycle = {
  publishJob(job: JobRecord, connection = 0) {
    lifecycleHarness.connections[connection]?.onJob(job);
  },
};

async function previewAndSubmit() {
  const previewButton = screen.getByRole("button", { name: "预览结构" });
  fireEvent.click(previewButton);
  const submitButton = screen.getByRole("button", { name: "生成语义 →" });
  await waitFor(() => expect(submitButton).toBeEnabled());
  fireEvent.click(submitButton);
}

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
    lifecycleHarness.connections.length = 0;
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

  it("keeps navigation blocked while a lease-bearing failed chat awaits retry", async () => {
    const onNavigationBlockChange = vi.fn();
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new Error("temporary failure"));
    render(<WorkbenchPage onNavigationBlockChange={onNavigationBlockChange} />);
    fireEvent.change(screen.getByLabelText("补充业务背景或询问当前 DDL"), { target: { value: "第一轮" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    await screen.findByRole("button", { name: "重试上一轮 AI 回复" });
    expect(onNavigationBlockChange).toHaveBeenLastCalledWith(true);
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

  it("keeps a deep-link restore locked and retries after a transient GET failure", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob)
      .mockRejectedValueOnce(new ApiError(503, { error: { code: "temporarily_unavailable", retryable: true } }))
      .mockResolvedValueOnce(waitingJob());
    render(<WorkbenchPage />);

    await waitFor(() => expect(getJob).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("数据源")).toBeDisabled();
    expect(screen.getByRole("button", { name: "预览结构" })).toBeDisabled();
    await waitFor(() => expect(screen.getByText("第二轮问题")).toBeInTheDocument(), { timeout: 2_500 });
    expect(connectJobEvents).toHaveBeenCalledOnce();
  });

  it("restarts the stage trace when a second task is submitted", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL)
      .mockResolvedValueOnce({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null })
      .mockResolvedValueOnce({ job_id: "job-2", status: "pending", status_url: "/jobs/job-2", events_url: null });
    render(<WorkbenchPage />);
    await previewAndSubmit();
    await waitFor(() => expect(connectJobEvents).toHaveBeenCalledTimes(1));
    jobLifecycle.publishJob(waitingJob({ status: "succeeded", question_set_id: null, questions: null, result: { ddl_hash: "hash", table_count: 1, column_count: 2, metric_count: 1 } }));
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
    await previewAndSubmit();
    rendered.unmount();

    resolveSubmit({ job_id: "late-job", status: "pending", status_url: "/jobs/late-job", events_url: null });
    await Promise.resolve();
    expect(window.location.pathname).toMatch(/^\/workbench\/[0-9a-f-]+$/);
    expect(connectJobEvents).not.toHaveBeenCalled();

    const pending = JSON.parse(sessionStorage.getItem("schema-loom-pending-submission")!);
    sessionStorage.setItem("schema-loom-pending-submission", JSON.stringify({ ...pending, startedAt: 0 }));
    vi.mocked(getJob).mockRejectedValue(new ApiError(404, { error: { code: "job_not_found", retryable: false } }));
    const reconciled = render(<WorkbenchPage />);
    await waitFor(() => expect(screen.getByLabelText("数据源")).toBeEnabled());
    reconciled.unmount();
  });

  it("writes the submission coordinate to the URL before acceptance returns", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    let rejectSubmit!: (cause: unknown) => void;
    vi.mocked(submitDDL).mockImplementation(() => new Promise((_resolve, reject) => { rejectSubmit = reject; }));
    render(<WorkbenchPage />);
    await previewAndSubmit();

    await waitFor(() => expect(submitDDL).toHaveBeenCalledOnce());
    const submissionId = vi.mocked(submitDDL).mock.calls[0]![0].submission_id!;
    expect(window.location.pathname).toBe(`/workbench/${submissionId}`);
    rejectSubmit(new ApiError(409, { error: { code: "source_busy", retryable: false } }));
    await screen.findByText(/任务提交失败/);
    expect(window.location.pathname).toBe("/workbench");
  });

  it("retries an initial 404 while a persisted submission may still be accepting", async () => {
    const submissionId = "11111111-1111-4111-8111-111111111111";
    sessionStorage.setItem("schema-loom-pending-submission", JSON.stringify({
      submissionId,
      startedAt: Date.now(),
    }));
    window.history.replaceState(null, "", `/workbench/${submissionId}`);
    vi.mocked(getJob)
      .mockRejectedValueOnce(new ApiError(404, { error: { code: "job_not_found", retryable: false } }))
      .mockResolvedValueOnce(waitingJob({ job_id: submissionId }));

    render(<WorkbenchPage />);

    expect(screen.getByLabelText("数据源")).toBeDisabled();
    await waitFor(() => expect(screen.getByText("第二轮问题")).toBeInTheDocument(), { timeout: 2_500 });
    expect(getJob).toHaveBeenCalledTimes(2);
    expect(sessionStorage.getItem("schema-loom-pending-submission")).toBeNull();
    expect(connectJobEvents).toHaveBeenCalledOnce();
  });

  it("falls back to the URL task after a stale persisted coordinate returns 404", async () => {
    const staleSubmissionId = "11111111-1111-4111-8111-111111111111";
    sessionStorage.setItem("schema-loom-pending-submission", JSON.stringify({
      submissionId: staleSubmissionId,
      startedAt: 0,
    }));
    window.history.replaceState(null, "", "/workbench/url-job");
    vi.mocked(getJob)
      .mockRejectedValueOnce(new ApiError(404, { error: { code: "job_not_found", retryable: false } }))
      .mockResolvedValueOnce(waitingJob({ job_id: "url-job" }));

    render(<WorkbenchPage />);

    expect(await screen.findByText("第二轮问题")).toBeInTheDocument();
    expect(getJob).toHaveBeenNthCalledWith(1, staleSubmissionId);
    expect(getJob).toHaveBeenNthCalledWith(2, "url-job");
    expect(window.location.pathname).toBe("/workbench/url-job");
  });

  it("keeps the submission coordinate after a malformed successful response", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockRejectedValue(new ApiError(502, {
      error: { code: "invalid_response", stage: "response", retryable: true },
    }));
    const first = render(<WorkbenchPage />);
    await previewAndSubmit();

    await screen.findByText(/invalid_response/);
    const submissionId = vi.mocked(submitDDL).mock.calls[0]![0].submission_id!;
    expect(window.location.pathname).toBe(`/workbench/${submissionId}`);
    expect(JSON.parse(sessionStorage.getItem("schema-loom-pending-submission")!)).toMatchObject({ submissionId });

    first.unmount();
    sessionStorage.setItem("schema-loom-pending-submission", JSON.stringify({ submissionId, startedAt: 0 }));
    vi.mocked(getJob).mockRejectedValue(new ApiError(404, { error: { code: "job_not_found", retryable: false } }));
    render(<WorkbenchPage />);
    await waitFor(() => expect(sessionStorage.getItem("schema-loom-pending-submission")).toBeNull());
  });

  it("reconciles a custom accepted submission after repeated timeouts and an SPA remount", async () => {
    const customDDL = "CREATE TABLE custom_orders (id BIGINT);";
    vi.mocked(previewDDL).mockResolvedValue({ source: "custom_source", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockRejectedValue(new ApiError(408, { error: { code: "request_timeout", retryable: true } }));
    const first = render(<WorkbenchPage />);
    fireEvent.change(screen.getByLabelText("数据源"), { target: { value: "custom_source" } });
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: customDDL } });
    await previewAndSubmit();
    await screen.findByText(/任务提交失败/);
    const submissionId = vi.mocked(submitDDL).mock.calls[0]![0].submission_id!;
    first.unmount();

    vi.mocked(getJob).mockResolvedValue(waitingJob({ job_id: submissionId, source: "custom_source" }));
    render(<WorkbenchPage />);

    expect(screen.getByLabelText("数据源")).toBeDisabled();
    await waitFor(() => expect(window.location.pathname).toBe(`/workbench/${submissionId}`));
    expect(await screen.findByText("第二轮问题")).toBeInTheDocument();
    expect(connectJobEvents).toHaveBeenCalledOnce();
  });

  it("reconciles a pending submission before the job in the current deep link", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "new_source", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockRejectedValue(new ApiError(408, { error: { code: "request_timeout", retryable: true } }));
    window.history.replaceState(null, "", "/workbench/old-job");
    vi.mocked(getJob).mockResolvedValueOnce({
      job_id: "old-job", source: "old_source", status: "succeeded", revision: 2, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    const first = render(<WorkbenchPage />);
    await waitFor(() => expect(screen.getByLabelText("MySQL DDL")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("数据源"), { target: { value: "new_source" } });
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE new_orders (id BIGINT);" } });
    await previewAndSubmit();
    await screen.findByText(/任务提交失败/);
    const submissionId = vi.mocked(submitDDL).mock.calls[0]![0].submission_id!;
    first.unmount();

    vi.mocked(getJob).mockReset().mockResolvedValue(waitingJob({ job_id: submissionId, source: "new_source" }));
    render(<WorkbenchPage />);

    await waitFor(() => expect(getJob).toHaveBeenCalledWith(submissionId));
    expect(getJob).not.toHaveBeenCalledWith("old-job");
    expect(window.location.pathname).toBe(`/workbench/${submissionId}`);
  });

  it("does not replace an active task with a new submission", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-1", source: "warehouse", status: "running", revision: 1, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    render(<WorkbenchPage />);

    await waitFor(() => expect(screen.getByLabelText("MySQL DDL")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("数据源"), { target: { value: "other_source" } });
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE other_orders (id BIGINT);" } });

    expect(screen.getByRole("button", { name: "生成语义 →" })).toBeDisabled();
    expect(submitDDL).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/workbench/job-1");
  });

  it("releases an unaccepted coordinate after a deterministic source conflict", async () => {
    vi.mocked(previewDDL).mockImplementation(async ({ source }) => ({
      source, tables: [], relationships: [], table_count: 0, column_count: 0,
    }));
    vi.mocked(submitDDL)
      .mockRejectedValueOnce(new ApiError(409, { error: { code: "source_busy", retryable: false } }))
      .mockResolvedValueOnce({ job_id: "new-job", status: "pending", status_url: "/jobs/new-job", events_url: null });
    render(<WorkbenchPage />);
    await previewAndSubmit();
    await screen.findByText(/source_busy/);

    fireEvent.change(screen.getByLabelText("数据源"), { target: { value: "available_source" } });
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE available (id INT);" } });
    await previewAndSubmit();

    await waitFor(() => expect(window.location.pathname).toBe("/workbench/new-job"));
    expect(submitDDL).toHaveBeenCalledTimes(2);
    expect(vi.mocked(submitDDL).mock.calls[1]![0].submission_id)
      .not.toBe(vi.mocked(submitDDL).mock.calls[0]![0].submission_id);
  });

  it("preserves an unconfirmed acceptance coordinate when the DDL changes", async () => {
    vi.mocked(previewDDL).mockImplementation(async ({ source }) => ({
      source, tables: [], relationships: [], table_count: 0, column_count: 0,
    }));
    vi.mocked(submitDDL)
      .mockRejectedValueOnce(new ApiError(0, { error: { code: "request_timeout", retryable: true } }))
      .mockResolvedValueOnce({ job_id: "recovered-job", status: "pending", status_url: "/jobs/recovered-job", events_url: null });
    render(<WorkbenchPage />);

    await previewAndSubmit();
    await screen.findByText(/任务提交失败/);
    const originalDDL = (screen.getByLabelText("MySQL DDL") as HTMLTextAreaElement).value;
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE changed (id INT);" } });
    await previewAndSubmit();

    expect(submitDDL).toHaveBeenCalledOnce();
    expect(screen.getByRole("alert")).toHaveTextContent("上一份 DDL 的任务受理结果尚未确认");
    expect(screen.getByRole("alert")).toHaveFocus();

    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: originalDDL } });
    await previewAndSubmit();
    await waitFor(() => expect(window.location.pathname).toBe("/workbench/recovered-job"));
  });

  it.each(["legacy_submission_timeout", "legacy_submission_uncertain"])(
    "does not manually replay an uncertain legacy submission (%s)", async (code) => {
    vi.mocked(previewDDL).mockResolvedValue({
      source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0,
    });
    vi.mocked(submitDDL).mockRejectedValue(new ApiError(408, {
      error: { code, stage: "acceptance", retryable: false },
    }));
    const page = render(<WorkbenchPage />);

    await previewAndSubmit();
    await screen.findByText(new RegExp(code));
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));

    expect(submitDDL).toHaveBeenCalledOnce();
    expect(screen.getByRole("alert")).toHaveTextContent("旧版后端的任务受理结果未知");
    const submissionId = vi.mocked(submitDDL).mock.calls[0]![0].submission_id;
    page.unmount();
    vi.mocked(getJob).mockResolvedValue({
      job_id: submissionId, source: "commerce_prod", status: "succeeded", revision: 1,
      attempt: 1, question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    render(<WorkbenchPage />);
    await waitFor(() => expect(getJob).toHaveBeenCalledWith(submissionId));
  });

  it("keeps a legacy submission non-replayable after a full document reload", async () => {
    const submissionId = "11111111-1111-4111-8111-111111111111";
    sessionStorage.setItem("schema-loom-pending-submission", JSON.stringify({
      submissionId,
      startedAt: 0,
      replayable: false,
    }));
    window.history.replaceState(null, "", `/workbench/${submissionId}`);
    vi.mocked(getJob).mockRejectedValue(new ApiError(404, {
      error: { code: "job_not_found", retryable: false },
    }));

    render(<WorkbenchPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("旧版后端的任务受理结果未知");
    expect(screen.getByLabelText("数据源")).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成语义 →" })).toBeDisabled();
    expect(sessionStorage.getItem("schema-loom-pending-submission")).not.toBeNull();
    expect(getJob).toHaveBeenCalledOnce();
  });

  it("persists the legacy replay gate before the submission response settles", async () => {
    let resolveSubmission!: (value: Awaited<ReturnType<typeof submitDDL>>) => void;
    const submissionResponse = new Promise<Awaited<ReturnType<typeof submitDDL>>>((resolve) => {
      resolveSubmission = resolve;
    });
    vi.mocked(previewDDL).mockResolvedValue({
      source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0,
    });
    vi.mocked(submitDDL).mockImplementation(async (_input, _signal, onDispatch) => {
      onDispatch?.(false);
      return submissionResponse;
    });
    render(<WorkbenchPage />);

    await previewAndSubmit();

    await waitFor(() => expect(JSON.parse(
      sessionStorage.getItem("schema-loom-pending-submission") ?? "{}",
    )).toMatchObject({ replayable: false }));
    resolveSubmission({
      job_id: "legacy-job", status: "pending", status_url: "/jobs/legacy-job", events_url: null,
    });
    await waitFor(() => expect(window.location.pathname).toBe("/workbench/legacy-job"));
  });

  it("accepts a task submission after StrictMode replays effects", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "strict-job", status: "pending", status_url: "/jobs/strict-job", events_url: null });
    render(<StrictMode><WorkbenchPage /></StrictMode>);

    await previewAndSubmit();

    await waitFor(() => expect(window.location.pathname).toBe("/workbench/strict-job"));
    expect(await screen.findByText("任务已受理")).toBeInTheDocument();
    expect(connectJobEvents).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "生成语义 →" })).toBeDisabled();
  });

  it("marks edits after a successful submission as unsaved", async () => {
    const onUnsavedChange = vi.fn();
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    render(<WorkbenchPage onUnsavedChange={onUnsavedChange} />);
    await previewAndSubmit();
    await waitFor(() => expect(onUnsavedChange).toHaveBeenLastCalledWith(false));
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE changed (id INT);" } });
    await waitFor(() => expect(onUnsavedChange).toHaveBeenLastCalledWith(true));
  });

  it("reuses the failed turn UID without duplicating the user message", async () => {
    vi.mocked(sendChatTurn)
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "重试成功" }, readiness: "proceed" });
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

  it("keeps clarification answers when the acceptance response is invalid", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue(waitingJob());
    vi.mocked(submitAnswers).mockRejectedValue(new ApiError(502, {
      error: { code: "invalid_response", stage: "response", retryable: true },
    }));
    render(<WorkbenchPage />);
    const answer = await screen.findByLabelText("第二轮问题");
    fireEvent.change(answer, { target: { value: "人工业务依据" } });
    fireEvent.click(screen.getByRole("button", { name: "提交回答并继续 →" }));

    await screen.findByText(/invalid_response/);
    expect(answer).toHaveValue("人工业务依据");
    expect(screen.getByText("第二轮问题")).toBeInTheDocument();
  });

  it("reuses the original DDL snapshot when a failed chat turn is retried", async () => {
    vi.mocked(sendChatTurn).mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "重试成功" }, readiness: "proceed" });
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
      .mockResolvedValueOnce({ message: { uid: "assistant-1", content: "已恢复" }, readiness: "proceed" });
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

  it("preserves an answer edited while an AI draft is pending", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    let resolveChat!: (response: { message: { uid: string; content: string }; readiness: "proceed" }) => void;
    vi.mocked(sendChatTurn).mockImplementation(() => new Promise((resolve) => { resolveChat = resolve; }));
    render(<WorkbenchPage />);

    await previewAndSubmit();
    await waitFor(() => expect(connectJobEvents).toHaveBeenCalledOnce());
    jobLifecycle.publishJob(waitingJob());
    const answer = await screen.findByLabelText("第二轮问题");
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE orders (id INT);" } });
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledOnce());
    fireEvent.change(answer, { target: { value: "人工填写的业务依据" } });
    resolveChat({ message: { uid: "assistant-draft", content: "晚到的 AI 草稿" }, readiness: "proceed" });

    await screen.findByText("晚到的 AI 草稿");
    expect(answer).toHaveValue("人工填写的业务依据");
  });

  it("uses the submitted DDL snapshot when drafting clarification after edits", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    vi.mocked(sendChatTurn).mockResolvedValue({ message: { uid: "assistant-draft", content: "草稿" }, readiness: "proceed" });
    render(<WorkbenchPage />);
    const submittedDDL = (screen.getByLabelText("MySQL DDL") as HTMLTextAreaElement).value;
    await previewAndSubmit();
    await waitFor(() => expect(connectJobEvents).toHaveBeenCalledOnce());
    jobLifecycle.publishJob(waitingJob());
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE changed (id INT);" } });
    fireEvent.click(await screen.findByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledOnce());
    expect(vi.mocked(sendChatTurn).mock.calls[0]?.[1].ddl_context.ddl).toBe(submittedDDL);
  });

  it("freezes reloaded DDL for clarification drafts on a restored task", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue(waitingJob());
    vi.mocked(sendChatTurn).mockResolvedValue({ message: { uid: "assistant-draft", content: "恢复任务草稿" }, readiness: "proceed" });
    render(<WorkbenchPage />);

    const answer = await screen.findByLabelText("第二轮问题");
    const restoredDDL = "CREATE TABLE orders (id INT);";
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: restoredDDL } });
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledOnce());
    expect(vi.mocked(sendChatTurn).mock.calls[0]?.[1].ddl_context).toEqual({
      source: "warehouse", dialect: "mysql", ddl: restoredDDL,
    });
    await waitFor(() => expect(answer).toHaveValue("恢复任务草稿"));
  });

  it("releases an invalid restored draft context before drafting again", async () => {
    window.history.replaceState(null, "", "/workbench/job-1");
    vi.mocked(getJob).mockResolvedValue(waitingJob());
    vi.mocked(sendChatTurn)
      .mockRejectedValueOnce(new ApiError(422, { error: { code: "invalid_ddl", retryable: false } }))
      .mockResolvedValueOnce({ message: { uid: "assistant-draft", content: "修正后的草稿" }, readiness: "proceed" });
    render(<WorkbenchPage />);

    await screen.findByLabelText("第二轮问题");
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "INVALID DDL" } });
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    await screen.findByText(/AI 请求校验失败/);

    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE fixed (id INT);" } });
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));

    expect(await screen.findAllByText("修正后的草稿")).toHaveLength(2);
    expect(vi.mocked(sendChatTurn).mock.calls[1]?.[1].ddl_context.ddl)
      .toBe("CREATE TABLE fixed (id INT);");
  });

  it("blocks clarification submission while an AI draft is pending", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    vi.mocked(sendChatTurn).mockImplementation(() => new Promise(() => undefined));
    render(<WorkbenchPage />);
    await previewAndSubmit();
    await waitFor(() => expect(connectJobEvents).toHaveBeenCalledOnce());
    jobLifecycle.publishJob(waitingJob());
    const answer = await screen.findByLabelText("第二轮问题");
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE orders (id INT);" } });
    fireEvent.change(answer, { target: { value: "人工业务依据" } });
    fireEvent.click(screen.getByRole("button", { name: "让 AI 起草" }));
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledOnce());

    const submit = screen.getByRole("button", { name: "提交回答并继续 →" });
    expect(submit).toBeDisabled();
    fireEvent.submit(submit.closest("form")!);
    expect(submitAnswers).not.toHaveBeenCalled();
  });

  it("keeps chat disabled while another request is busy", async () => {
    let resolvePreview!: (preview: { source: string; tables: []; relationships: []; table_count: number; column_count: number }) => void;
    vi.mocked(previewDDL).mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve; }));
    render(<WorkbenchPage />);
    const chatInput = screen.getByLabelText("补充业务背景或询问当前 DDL");
    fireEvent.change(chatInput, { target: { value: "解释订单表" } });

    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));

    expect(chatInput).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 →" })).toBeDisabled();
    fireEvent.submit(chatInput.closest("form")!);
    expect(sendChatTurn).not.toHaveBeenCalled();
    resolvePreview({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    await waitFor(() => expect(chatInput).toBeEnabled());
  });

  it("shows the server-aligned source and DDL submission constraints", () => {
    render(<WorkbenchPage />);

    expect(screen.getByText("1–128 字符：字母、数字、下划线、点或连字符。")).toBeInTheDocument();
    expect(screen.getByText(/\/ 262,144 bytes$/)).toBeInTheDocument();
    expect(screen.getByText("50 tables · 500 columns")).toBeInTheDocument();
    expect(screen.getByLabelText("数据源")).toHaveAttribute("maxlength", "128");
  });
});
