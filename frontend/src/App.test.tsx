import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import {
  createConversation, getJob, getMemory, getMemoryHistory, previewDDL, sendChatTurn, submitDDL,
  updateMemory,
} from "./api/dataAgent";

vi.mock("./api/dataAgent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/dataAgent")>();
  return {
    ...actual, createConversation: vi.fn(), getJob: vi.fn(), getMemory: vi.fn(),
    getMemoryHistory: vi.fn(), previewDDL: vi.fn(), sendChatTurn: vi.fn(), submitDDL: vi.fn(),
    updateMemory: vi.fn(),
  };
});
vi.mock("./api/jobEvents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/jobEvents")>();
  return { ...actual, connectJobEvents: vi.fn(() => ({ close: vi.fn() })) };
});

describe("application shell", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/workbench");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(previewDDL).mockReset();
    vi.mocked(submitDDL).mockReset();
    vi.mocked(getJob).mockReset();
    vi.mocked(createConversation).mockReset().mockResolvedValue({ uid: "conversation-1" });
    vi.mocked(sendChatTurn).mockReset();
    vi.mocked(getMemory).mockReset();
    vi.mocked(getMemoryHistory).mockReset();
    vi.mocked(updateMemory).mockReset();
    vi.spyOn(window, "alert").mockImplementation(() => undefined);
  });

  it("renders the independent workbench and switches to knowledge memory", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
    expect(screen.getByLabelText("Schema 关系画布，可横向和纵向滚动")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "知识记忆" }));
    expect(screen.getByRole("heading", { name: "知识记忆" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/knowledge");
  });

  it("uses the workbench root when navigating from a directly loaded knowledge page", () => {
    window.history.replaceState(null, "", "/knowledge");
    render(<App />);

    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));

    expect(window.location.pathname).toBe("/workbench");
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
  });

  it("keeps the workbench open when the user cancels unsaved DDL navigation", () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<App />);

    fireEvent.click(screen.getByRole("link", { name: "知识记忆" }));

    expect(window.confirm).toHaveBeenCalledWith("当前 DDL 尚未提交，确定离开工作台？");
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/workbench");
  });

  it("restores the workbench when browser history navigation is cancelled", () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<App />);

    window.history.replaceState(null, "", "/knowledge");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(window.confirm).toHaveBeenCalledWith("当前 DDL 尚未提交，确定离开工作台？");
    expect(window.location.pathname).toBe("/workbench");
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
  });

  it("restores the task deep link when cancelled history navigation would discard edits", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-1", source: "warehouse", status: "succeeded", revision: 2, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    window.history.replaceState(null, "", "/workbench/job-1");
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("MySQL DDL")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE edited (id INT);" } });

    window.history.replaceState(null, "", "/knowledge");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(window.location.pathname).toBe("/workbench/job-1");
  });

  it("preserves the active task path across SPA navigation", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-1", status: "pending", status_url: "/jobs/job-1", events_url: null });
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-1", source: "commerce_prod", status: "running", revision: 1, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成语义 →" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    await waitFor(() => expect(window.location.pathname).toBe("/workbench/job-1"));

    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));
    expect(window.location.pathname).toBe("/workbench/job-1");
    fireEvent.click(screen.getByRole("link", { name: "知识记忆" }));
    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));
    expect(window.location.pathname).toBe("/workbench/job-1");
  });

  it("remembers a clean task path when browser history leaves the workbench", async () => {
    vi.mocked(previewDDL).mockResolvedValue({ source: "commerce_prod", tables: [], relationships: [], table_count: 0, column_count: 0 });
    vi.mocked(submitDDL).mockResolvedValue({ job_id: "job-popstate", status: "pending", status_url: "/jobs/job-popstate", events_url: null });
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-popstate", source: "commerce_prod", status: "running", revision: 1, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    window.history.replaceState(null, "", "/knowledge");
    render(<App />);
    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));
    fireEvent.click(screen.getByRole("button", { name: "预览结构" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成语义 →" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "生成语义 →" }));
    await waitFor(() => expect(window.location.pathname).toBe("/workbench/job-popstate"));

    window.history.replaceState(null, "", "/knowledge");
    window.dispatchEvent(new PopStateEvent("popstate"));
    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));

    expect(window.location.pathname).toBe("/workbench/job-popstate");
  });

  it("keeps the workbench mounted while a chat turn is in flight", async () => {
    vi.mocked(sendChatTurn).mockImplementation(() => new Promise(() => undefined));
    render(<App />);
    fireEvent.change(screen.getByLabelText("补充业务背景或询问当前 DDL"), { target: { value: "解释订单表" } });
    fireEvent.click(screen.getByRole("button", { name: "发送 →" }));
    await waitFor(() => expect(sendChatTurn).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("link", { name: "知识记忆" }));

    expect(window.alert).toHaveBeenCalledWith("AI 回复仍在生成或等待重试，请完成当前轮次后再离开工作台。");
    expect(window.location.pathname).toBe("/workbench");
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
  });

  it("keeps the knowledge page mounted while a correction is in flight", async () => {
    let resolveUpdate!: (result: Awaited<ReturnType<typeof updateMemory>>) => void;
    vi.mocked(getMemory).mockResolvedValue({
      uid: "memory-1", source: "warehouse", category: "metadata", memory_key: "orders",
      memory_text: "订单", content: { name: "订单" }, record_version: 1, status: "active",
    });
    vi.mocked(getMemoryHistory).mockResolvedValue({ items: [], offset: 0, limit: 50, has_more: false });
    vi.mocked(updateMemory).mockImplementation(() => new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    window.history.replaceState(null, "", "/knowledge?memory=memory-1");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "修正内容" }));
    fireEvent.click(screen.getByRole("button", { name: "保存修正" }));

    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));
    expect(window.alert).toHaveBeenCalledWith("知识修正正在保存，请等待完成后再离开知识页。");
    expect(window.location.pathname).toBe("/knowledge");

    resolveUpdate({
      memory_uid: "memory-1", event_id: 2, record_version: 2, requires_reprocess: true,
    });
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));
    expect(window.location.pathname).toBe("/workbench");
  });

  it("remounts the workbench when history changes its task coordinate", async () => {
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-2", source: "warehouse-2", status: "running", revision: 1, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    window.history.replaceState(null, "", "/workbench/job-1");
    render(<App />);
    await waitFor(() => expect(getJob).toHaveBeenCalledWith("job-1"));

    window.history.replaceState(null, "", "/workbench/job-2");
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => expect(getJob).toHaveBeenCalledWith("job-2"));
  });

  it("blocks same-view task history navigation when the workbench has unsaved DDL", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    vi.mocked(getJob).mockResolvedValue({
      job_id: "job-1", source: "warehouse", status: "succeeded", revision: 2, attempt: 1,
      question_round: 0, question_set_id: null, questions: null, result: null, error: null,
    });
    window.history.replaceState(null, "", "/workbench/job-1");
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("MySQL DDL")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE edited (id INT);" } });

    window.history.replaceState(null, "", "/workbench/job-2");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(window.confirm).toHaveBeenCalledWith("当前 DDL 尚未提交，确定离开工作台？");
    expect(window.location.pathname).toBe("/workbench/job-1");
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("does not create history when the active workbench navigation already targets the URL", () => {
    const pushState = vi.spyOn(window.history, "pushState");
    render(<App />);

    fireEvent.click(screen.getByRole("link", { name: "结构工作台" }));

    expect(pushState).not.toHaveBeenCalled();
  });
});
