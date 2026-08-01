import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

describe("application shell", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/workbench");
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("renders the independent workbench and switches to knowledge memory", () => {
    render(<App />);
    expect(screen.getByRole("heading", { name: "把物理结构织成语义" })).toBeInTheDocument();
    expect(screen.getByLabelText("Schema 关系画布，可横向和纵向滚动")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "知识记忆" }));
    expect(screen.getByRole("heading", { name: "知识记忆" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/knowledge");
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

  it("restores the task deep link when cancelled history navigation would discard edits", () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    window.history.replaceState(null, "", "/workbench/job-1");
    render(<App />);
    fireEvent.change(screen.getByLabelText("MySQL DDL"), { target: { value: "CREATE TABLE edited (id INT);" } });

    window.history.replaceState(null, "", "/knowledge");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(window.location.pathname).toBe("/workbench/job-1");
  });
});
