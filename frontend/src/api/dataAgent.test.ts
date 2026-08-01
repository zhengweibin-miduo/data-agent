import { afterEach, describe, expect, it, vi } from "vitest";

import { getJob, submitDDL } from "./dataAgent";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("DDL job submission", () => {
  it.each([{}, null, { job_id: 123 }, { job_id: "job-1", status: "pending" }])(
    "rejects invalid successful acceptance DTOs",
    async (payload) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      })));

      await expect(submitDDL({
        source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)", submission_id: "job-1",
      })).rejects.toMatchObject({
        status: 502, code: "invalid_response", stage: "response", retryable: true,
      });
    },
  );

  it("replays the same acceptance coordinate after a response timeout", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockImplementationOnce((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        job_id: "11111111-1111-4111-8111-111111111111",
        status: "pending",
        status_url: "/api/v1/metadata/ddl-jobs/11111111-1111-4111-8111-111111111111",
        events_url: null,
      }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const request = submitDDL({
      source: "dw",
      dialect: "mysql",
      ddl: "CREATE TABLE t(id INT)",
      submission_id: "11111111-1111-4111-8111-111111111111",
    });
    await vi.advanceTimersByTimeAsync(30_000);

    await expect(request).resolves.toMatchObject({ job_id: "11111111-1111-4111-8111-111111111111" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map((call) => call[1]?.body)).toEqual([
      expect.stringContaining("11111111-1111-4111-8111-111111111111"),
      expect.stringContaining("11111111-1111-4111-8111-111111111111"),
    ]);
  });
});

describe("authoritative job reads", () => {
  it.each([{}, null, { job_id: "job-1" }, { job_id: "job-1", status: "waiting_input" }])(
    "rejects invalid successful JobRecord DTOs",
    async (payload) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })));

      await expect(getJob("job-1")).rejects.toMatchObject({
        status: 502, code: "invalid_response", stage: "response", retryable: true,
      });
    },
  );
});
