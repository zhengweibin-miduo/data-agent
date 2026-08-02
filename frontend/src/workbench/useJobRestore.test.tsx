import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { JobRecord } from "../api/types";
import { useJobRestore } from "./useJobRestore";

const record: JobRecord = {
  job_id: "job-1", source: "warehouse", status: "running", revision: 2, attempt: 1,
  question_round: 0, question_set_id: null, questions: null, result: null, error: null,
};

function options(getJob: () => Promise<JobRecord>) {
  let current: string | null = null;
  return {
    initialJobId: "job-1",
    fallbackJobId: null,
    persistedSubmission: null,
    getJob,
    claimJob: vi.fn((jobId: string) => { current = jobId; }),
    isCurrentJob: vi.fn((jobId: string) => current === jobId),
    acceptJob: vi.fn(),
    watchJob: vi.fn(),
    stopWatchingJob: vi.fn(() => { current = null; }),
    setConnection: vi.fn(),
    setError: vi.fn(),
    clearRecoveryCoordinate: vi.fn(),
    onRestored: vi.fn(),
    onReleased: vi.fn(),
    formatError: vi.fn(() => "恢复失败"),
    delay: vi.fn(async () => undefined),
  };
}

describe("useJobRestore", () => {
  it("keeps input locked until the authoritative job is restored and watched", async () => {
    const input = options(vi.fn().mockResolvedValue(record));
    const hook = renderHook(() => useJobRestore(input));
    expect(hook.result.current.restoringJob).toBe(true);
    await waitFor(() => expect(input.onRestored).toHaveBeenCalledWith(record));
    expect(hook.result.current.restoringJob).toBe(false);
    expect(input.watchJob).toHaveBeenCalledWith("job-1", "/api/v1/metadata/ddl-jobs/job-1/events");
  });

  it("releases the coordinate only after an authoritative not-found result", async () => {
    const input = options(vi.fn().mockRejectedValue(new ApiError(404, {})));
    const hook = renderHook(() => useJobRestore(input));
    await waitFor(() => expect(input.onReleased).toHaveBeenCalledOnce());
    expect(input.clearRecoveryCoordinate).toHaveBeenCalledWith("job-1");
    expect(hook.result.current.restoringJob).toBe(false);
  });

  it("ignores a stale continuation after ownership changes", async () => {
    let resolve!: (value: JobRecord) => void;
    const input = options(
      vi.fn(() => new Promise<JobRecord>((done) => { resolve = done; })),
    );
    renderHook(() => useJobRestore(input));
    await waitFor(() => expect(input.claimJob).toHaveBeenCalledWith("job-1"));
    input.stopWatchingJob();
    resolve(record);
    await Promise.resolve();
    expect(input.onRestored).not.toHaveBeenCalled();
  });

  it("cancels a pending retry delay when the restore owner unmounts", async () => {
    let retrySignal: AbortSignal | undefined;
    const input = options(vi.fn().mockRejectedValue(new ApiError(503, {})));
    input.delay = vi.fn((_milliseconds: number, signal?: AbortSignal) => {
      retrySignal = signal;
      return new Promise<void>(() => undefined);
    });
    const hook = renderHook(() => useJobRestore(input));
    await waitFor(() => expect(input.delay).toHaveBeenCalledOnce());

    hook.unmount();

    expect(retrySignal?.aborted).toBe(true);
  });
});
