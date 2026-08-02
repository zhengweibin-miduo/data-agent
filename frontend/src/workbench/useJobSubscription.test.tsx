import { StrictMode, useEffect } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { JobEventData, JobRecord } from "../api/types";
import {
  type JobLifecycleHandlers,
  type JobSubscriptionTransport,
  useJobSubscription,
} from "./useJobSubscription";

const job = (overrides: Partial<JobRecord> = {}): JobRecord => ({
  job_id: "job-1", source: "warehouse", status: "running", revision: 1, attempt: 1,
  question_round: 0, question_set_id: null, questions: null, result: null, error: null,
  ...overrides,
});

const event = (overrides: Partial<JobEventData> = {}): JobEventData => ({
  job_id: "job-1", status: "running", stage: "running", revision: 1, attempt: 1,
  emitted_at: "2026-08-02T00:00:00Z", questions: null, result: null, error: null,
  ...overrides,
});

function lifecycleTransport(authoritativeJob: JobRecord) {
  const subscriptions: Array<{ handlers: JobLifecycleHandlers; close: ReturnType<typeof vi.fn> }> = [];
  const transport: JobSubscriptionTransport = {
    getJob: vi.fn().mockResolvedValue(authoritativeJob),
    connect: vi.fn((_eventsUrl, handlers) => {
      const close = vi.fn();
      subscriptions.push({ handlers, close });
      return { close };
    }),
  };
  return { transport, subscriptions };
}

describe("useJobSubscription", () => {
  it("accepts waiting_input only after the transport performs an authoritative GET", async () => {
    const authoritative = job({
      status: "waiting_input", revision: 3, question_round: 1, question_set_id: "set-3",
      questions: [{ question_id: "q-1", prompt: "依据？", fact_table_id: "orders", column_ids: [], required: true }],
    });
    const { transport, subscriptions } = lifecycleTransport(authoritative);
    const { result } = renderHook(() => useJobSubscription({ transport }));
    act(() => result.current.watch("job-1", "/events/job-1", { initialJob: job() }));

    act(() => subscriptions[0]!.handlers.onEvent(event({
      status: "waiting_input", stage: "waiting_input", revision: 3,
      questions: authoritative.questions,
    })));
    expect(result.current.job?.question_set_id).toBeNull();
    expect(result.current.job?.questions).toBeNull();

    await act(async () => {
      const record = await subscriptions[0]!.handlers.getAuthoritativeJob();
      subscriptions[0]!.handlers.onJob(record);
    });
    expect(transport.getJob).toHaveBeenCalledWith("job-1");
    expect(result.current.job?.question_set_id).toBe("set-3");
  });

  it("stops the active subscription at a terminal event", () => {
    const { transport, subscriptions } = lifecycleTransport(job());
    const { result } = renderHook(() => useJobSubscription({ transport }));
    act(() => result.current.watch("job-1", "/events/job-1", { initialJob: job() }));
    act(() => subscriptions[0]!.handlers.onEvent(event({ status: "succeeded", stage: "succeeded" })));
    expect(subscriptions[0]!.close).toHaveBeenCalledOnce();
    expect(result.current.job?.status).toBe("succeeded");
  });

  it("rejects late updates from a stale job", () => {
    const { transport, subscriptions } = lifecycleTransport(job());
    const { result } = renderHook(() => useJobSubscription({ transport }));
    act(() => result.current.watch("job-1", "/events/job-1", { initialJob: job() }));
    act(() => result.current.watch("job-2", "/events/job-2", { initialJob: job({ job_id: "job-2", revision: 2 }) }));
    act(() => subscriptions[0]!.handlers.onJob(job({ revision: 99 })));
    expect(result.current.job).toMatchObject({ job_id: "job-2", revision: 2 });
    expect(subscriptions[0]!.close).toHaveBeenCalledOnce();
  });

  it("cleans every StrictMode replay and unmount subscription", async () => {
    const { transport, subscriptions } = lifecycleTransport(job());
    const useWatchingSubscription = () => {
      const subscription = useJobSubscription({ transport });
      const watch = subscription.watch;
      useEffect(() => watch("job-1", "/events/job-1", { initialJob: job() }), [watch]);
      return subscription;
    };
    const rendered = renderHook(useWatchingSubscription, { wrapper: StrictMode });
    await waitFor(() => expect(subscriptions).toHaveLength(2));
    expect(subscriptions[0]!.close).toHaveBeenCalledOnce();
    rendered.unmount();
    expect(subscriptions[1]!.close).toHaveBeenCalledOnce();
  });
});
