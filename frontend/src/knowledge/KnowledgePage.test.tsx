import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getMemory, getMemoryHistory, updateMemory } from "../api/dataAgent";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../api/dataAgent", () => ({
  deleteMemory: vi.fn(), getMemory: vi.fn(), getMemoryHistory: vi.fn(), searchMemories: vi.fn(), updateMemory: vi.fn(),
}));

describe("knowledge corrections", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/knowledge?memory=memory-1");
    vi.mocked(getMemory).mockResolvedValue({ uid: "memory-1", source: "warehouse", category: "metadata", memory_key: "orders", memory_text: "订单", content: { name: "订单" }, record_version: 1, status: "active" });
    vi.mocked(getMemoryHistory).mockResolvedValue({ items: [] });
    vi.mocked(updateMemory).mockResolvedValue({ memory_uid: "memory-1", requires_reprocess: true });
  });

  it("explains that a saved authoritative correction requires DDL reprocessing", async () => {
    render(<KnowledgePage />);
    fireEvent.click(await screen.findByRole("button", { name: "修正内容" }));
    fireEvent.click(screen.getByRole("button", { name: "保存修正" }));
    expect(await screen.findByRole("status")).toHaveTextContent("重新载入并提交 DDL");
  });
});
