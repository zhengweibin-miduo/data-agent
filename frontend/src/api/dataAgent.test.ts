import { afterEach, describe, expect, it, vi } from "vitest";

import { getJob, previewDDL, submitDDL } from "./dataAgent";

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

describe("DDL preview", () => {
  const validPreview = {
    source: "dw",
    tables: [{
      id: "table-1",
      schema_name: null,
      name: "orders",
      qualified_name: "orders",
      comment: null,
      columns: [{
        id: "column-1",
        name: "id",
        data_type: "INT",
        comment: null,
        nullable: false,
        structural_role: "primary_key",
      }],
      primary_key: ["id"],
    }],
    relationships: [],
    table_count: 1,
    column_count: 1,
  };

  it.each([
    {},
    { ...validPreview, relationships: undefined },
    { ...validPreview, table_count: "1" },
    { ...validPreview, tables: [{ ...validPreview.tables[0], columns: "invalid" }] },
  ])("rejects invalid successful preview DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(previewDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE orders(id INT PRIMARY KEY)",
    })).rejects.toMatchObject({
      status: 502, code: "invalid_response", stage: "response", retryable: true,
    });
  });

  it("accepts a complete preview DTO", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(validPreview), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(previewDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE orders(id INT PRIMARY KEY)",
    })).resolves.toEqual(validPreview);
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
