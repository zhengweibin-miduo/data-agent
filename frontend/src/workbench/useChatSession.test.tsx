import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { MetricQuestion } from "../api/types";
import { useChatSession } from "./useChatSession";

const question: MetricQuestion = {
  question_id: "q-1", prompt: "口径是什么？", required: true,
  fact_table_id: "table-1", column_ids: ["column-1"],
};

function createHarness(overrides: Partial<Parameters<typeof useChatSession>[0]> = {}) {
  const createConversation = vi.fn().mockResolvedValue({ uid: "conversation-1" });
  const sendChatTurn = vi.fn().mockResolvedValue({
    message: { uid: "assistant-1", content: "AI 草稿" }, readiness: "proceed" as const,
  });
  const setInteractionBusy = vi.fn();
  const setError = vi.fn();
  let nextId = 0;
  const result = renderHook(() => {
    const [answers, setAnswers] = useState<Record<string, string>>({ "q-1": "请求时答案" });
    return useChatSession({
      source: "warehouse", ddl: "CREATE TABLE facts (id INT);", answers, setAnswers,
      job: null, restoredJobId: null, interactionBusy: null, setInteractionBusy, setError,
      createConversation, sendChatTurn, formatError: () => "请求失败", randomId: () => `id-${++nextId}`,
      ...overrides,
    });
  });
  return { ...result, createConversation, sendChatTurn, setInteractionBusy, setError };
}

describe("useChatSession", () => {
  it("reuses the failed turn coordinate on retry", async () => {
    const harness = createHarness();
    harness.sendChatTurn.mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({
      message: { uid: "assistant-2", content: "恢复回复" }, readiness: "proceed",
    });
    act(() => harness.result.current.setChatInput("请解释"));
    await act(() => harness.result.current.send());
    const failedTurn = harness.sendChatTurn.mock.calls[0]![1].turn_uid;
    await act(() => harness.result.current.retry());
    expect(harness.sendChatTurn.mock.calls[1]![1].turn_uid).toBe(failedTurn);
  });

  it("recreates a missing conversation without changing the turn coordinate", async () => {
    sessionStorage.setItem("schema-loom-conversation", "gone");
    const harness = createHarness();
    harness.sendChatTurn.mockRejectedValueOnce(new ApiError(404, { error: { code: "missing", retryable: false } }));
    act(() => harness.result.current.setChatInput("请解释"));
    await act(() => harness.result.current.send());
    expect(harness.createConversation).toHaveBeenCalledOnce();
    expect(harness.sendChatTurn.mock.calls[1]![1].turn_uid).toBe(harness.sendChatTurn.mock.calls[0]![1].turn_uid);
  });

  it("releases retry and restored draft context after a deterministic validation failure", async () => {
    const harness = createHarness({ restoredJobId: "job-1" });
    act(() => {
      harness.result.current.recordSubmittedDDLContext({ source: "warehouse", dialect: "mysql", ddl: "CREATE TABLE facts (id INT);" }, true);
      harness.result.current.askToDraft(question);
      harness.result.current.setChatInput("起草");
    });
    harness.sendChatTurn.mockRejectedValueOnce(new ApiError(422, { error: { code: "invalid", retryable: false } }));
    await act(() => harness.result.current.send());
    expect(harness.result.current.failedChat).toBeNull();
    expect(harness.result.current.draftQuestion).toBeNull();
    expect(harness.result.current.hasSubmittedDDLContext).toBe(false);
  });

  it("does not overwrite an answer changed after the draft request began", async () => {
    let resolve!: (value: { message: { uid: string; content: string }; readiness: "proceed" }) => void;
    const harness = createHarness();
    harness.sendChatTurn.mockImplementation(() => new Promise((done) => { resolve = done; }));
    act(() => {
      harness.result.current.recordSubmittedDDLContext({ source: "warehouse", dialect: "mysql", ddl: "CREATE TABLE facts (id INT);" });
      harness.result.current.askToDraft(question);
      harness.result.current.setChatInput("起草");
    });
    const pending = act(() => harness.result.current.send());
    act(() => harness.result.current.setAnswer("q-1", "人工答案"));
    resolve({ message: { uid: "assistant-2", content: "晚到草稿" }, readiness: "proceed" });
    await pending;
    await waitFor(() => expect(harness.result.current.answers["q-1"]).toBe("人工答案"));
  });
});
