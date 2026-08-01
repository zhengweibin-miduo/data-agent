import { afterEach, describe, expect, it, vi } from "vitest";

import { connectJobEvents } from "./jobEvents";
import type { JobEventData, JobRecord } from "./types";

class FakeEventSource {
  static latest: FakeEventSource;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly url: string;
  private listeners = new Map<string, EventListener>();
  close = vi.fn();

  constructor(url: string | URL) { this.url = String(url); FakeEventSource.latest = this; }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener); }
  emit(type: string, data: JobEventData) { this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify(data) })); }
}

const waitingJob: JobRecord = {
  job_id: "job-1", source: "commerce", status: "waiting_input", revision: 3, attempt: 1,
  question_round: 1, question_set_id: "set-1", questions: [], result: null, error: null,
};

afterEach(() => vi.restoreAllMocks());

describe("job event adapter", () => {
  it("fetches the authoritative JobRecord before enabling waiting-input controls", async () => {
    const getAuthoritativeJob = vi.fn().mockResolvedValue(waitingJob);
    const onJob = vi.fn();
    const subscription = connectJobEvents("/api/v1/metadata/ddl-jobs/job-1/events", {
      getAuthoritativeJob, onJob, onEvent: vi.fn(), onConnection: vi.fn(), onError: vi.fn(),
    }, FakeEventSource as unknown as typeof EventSource);

    FakeEventSource.latest.emit("waiting_input", {
      job_id: "job-1", revision: 3, attempt: 1, status: "waiting_input", stage: "waiting_input",
      emitted_at: "2026-08-01T00:00:00Z", questions: [], result: null, error: null,
    });
    await vi.waitFor(() => expect(getAuthoritativeJob).toHaveBeenCalledOnce());
    expect(onJob).toHaveBeenCalledWith(waitingJob);
    subscription.close();
  });

  it("queries authoritative state while preserving EventSource reconnect semantics", async () => {
    vi.useFakeTimers();
    const onConnection = vi.fn();
    const getAuthoritativeJob = vi.fn().mockResolvedValue({ ...waitingJob, status: "running", question_set_id: null });
    const subscription = connectJobEvents("/events", { getAuthoritativeJob, onJob: vi.fn(), onEvent: vi.fn(), onConnection, onError: vi.fn() }, FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.latest.onerror?.(new Event("error"));
    await vi.advanceTimersByTimeAsync(0);
    expect(getAuthoritativeJob).toHaveBeenCalled();
    expect(FakeEventSource.latest.close).not.toHaveBeenCalled();
    expect(onConnection).toHaveBeenCalledWith("事件流已中断，正在查询权威状态并等待重连");

    FakeEventSource.latest.onopen?.(new Event("open"));
    await vi.advanceTimersByTimeAsync(3000);
    expect(getAuthoritativeJob).toHaveBeenCalledOnce();
    expect(onConnection).toHaveBeenLastCalledWith("事件流已连接");
    subscription.close();
    vi.useRealTimers();
  });
});
