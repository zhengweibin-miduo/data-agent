import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { getJob, previewDDL, submitDDL } from "./api/dataAgent";

vi.mock("./api/dataAgent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api/dataAgent")>();
  return { ...actual, getJob: vi.fn(), previewDDL: vi.fn(), submitDDL: vi.fn() };
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
});
