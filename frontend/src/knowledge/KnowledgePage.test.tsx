import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { deleteMemory, getMemory, getMemoryHistory, searchMemories, updateMemory } from "../api/dataAgent";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../api/dataAgent", () => ({
  deleteMemory: vi.fn(), getMemory: vi.fn(), getMemoryHistory: vi.fn(), searchMemories: vi.fn(), updateMemory: vi.fn(),
}));

describe("knowledge corrections", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/knowledge?memory=memory-1");
    vi.mocked(getMemory).mockResolvedValue({ uid: "memory-1", source: "warehouse", category: "metadata", memory_key: "orders", memory_text: "订单", content: { name: "订单" }, record_version: 1, status: "active" });
    vi.mocked(getMemoryHistory).mockResolvedValue({ items: [], offset: 0, limit: 50, has_more: false });
    vi.mocked(updateMemory).mockResolvedValue({ memory_uid: "memory-1", requires_reprocess: true });
    vi.mocked(searchMemories).mockReset();
    vi.mocked(deleteMemory).mockReset();
  });

  it("explains that a saved authoritative correction requires DDL reprocessing", async () => {
    render(<KnowledgePage />);
    fireEvent.click(await screen.findByRole("button", { name: "修正内容" }));
    fireEvent.click(screen.getByRole("button", { name: "保存修正" }));
    expect(await screen.findByRole("status")).toHaveTextContent("重新载入并提交 DDL");
  });

  it("disables detail mutations while another memory detail is loading", async () => {
    vi.mocked(getMemory)
      .mockResolvedValueOnce({ uid: "memory-1", source: "warehouse", category: "metadata", memory_key: "orders", memory_text: "订单", content: {}, record_version: 1, status: "active" })
      .mockImplementationOnce(() => new Promise(() => undefined));
    vi.mocked(getMemoryHistory)
      .mockResolvedValueOnce({ items: [], offset: 0, limit: 50, has_more: false })
      .mockImplementationOnce(() => new Promise(() => undefined));
    vi.mocked(searchMemories).mockResolvedValue({
      items: [{
        memory: { uid: "memory-2", source: "warehouse", category: "metadata", memory_key: "customers", memory_text: "客户", content: {}, record_version: 1, status: "active" },
        score: 1,
        signals: [],
      }],
      degraded_targets: [],
    });
    render(<KnowledgePage />);
    await screen.findByRole("button", { name: "软删除" });
    fireEvent.change(screen.getByLabelText("查询"), { target: { value: "客户" } });
    fireEvent.submit(screen.getByRole("button", { name: "搜索知识 →" }).closest("form")!);
    fireEvent.click(await screen.findByRole("button", { name: /客户/ }));

    expect(screen.getByRole("button", { name: "软删除" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "修正内容" })).toBeDisabled();
    expect(deleteMemory).not.toHaveBeenCalled();
  });
});
