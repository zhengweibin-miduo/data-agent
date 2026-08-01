import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createConversation, getJob, getMemory, getMemoryHistory, previewDDL, searchMemories,
  sendChatTurn, submitDDL, updateMemory,
} from "./dataAgent";

const capabilityResponse = () => new Response(JSON.stringify({
  status: "ok", capabilities: { ddl_submission_idempotency: true },
}), { status: 200, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("DDL job submission", () => {
  it("does not submit without a conclusive capability response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: {
      code: "health_unavailable", stage: "health", retryable: true,
    } }), { status: 503, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)", submission_id: "job-1",
    })).rejects.toMatchObject({ status: 503, code: "health_unavailable", retryable: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not dispatch after capability discovery is cancelled", async () => {
    let resolveHealth!: (response: Response) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>((resolve) => {
      resolveHealth = resolve;
    }));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const onDispatch = vi.fn();

    const request = submitDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)", submission_id: "job-1",
    }, controller.signal, onDispatch);
    controller.abort();
    resolveHealth(new Response(JSON.stringify({ detail: "Not Found" }), {
      status: 404, headers: { "Content-Type": "application/json" },
    }));

    await expect(request).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onDispatch).not.toHaveBeenCalled();
  });

  it.each([
    {},
    null,
    { status: "ready" },
    { status: "ok", capabilities: null },
    { status: "ok", capabilities: {} },
    { status: "ok", capabilities: { ddl_submission_idempotency: "true" } },
  ])("does not submit after an invalid successful capability response", async (payload) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)", submission_id: "job-1",
    })).rejects.toMatchObject({
      status: 502, code: "invalid_response", stage: "response", retryable: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([{}, null, { job_id: 123 }, { job_id: "job-1", status: "pending" }])(
    "rejects invalid successful acceptance DTOs",
    async (payload) => {
      vi.stubGlobal("fetch", vi.fn()
        .mockResolvedValueOnce(capabilityResponse())
        .mockResolvedValueOnce(new Response(JSON.stringify(payload), {
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
      .mockResolvedValueOnce(capabilityResponse())
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
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.slice(1).map((call) => JSON.parse(String(call[1]?.body)))).toEqual([
      { source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)" },
      { source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)" },
    ]);
    expect(fetchMock.mock.calls.slice(1).map((call) => call[1]?.headers)).toEqual([
      { "Content-Type": "application/json", "Idempotency-Key": "11111111-1111-4111-8111-111111111111" },
      { "Content-Type": "application/json", "Idempotency-Key": "11111111-1111-4111-8111-111111111111" },
    ]);
  });

  it("keeps the request body compatible with backends that forbid unknown fields", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, options: RequestInit) => {
      if (url.endsWith("/api/v1/health")) {
        return Promise.resolve(new Response(JSON.stringify({ status: "ok" }), { status: 200 }));
      }
      const body = JSON.parse(String(options.body)) as Record<string, unknown>;
      if ("submission_id" in body) {
        return Promise.resolve(new Response(JSON.stringify({ detail: "extra_forbidden" }), { status: 422 }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        job_id: "legacy-job", status: "pending", status_url: "/api/v1/metadata/ddl-jobs/legacy-job", events_url: null,
      }), { status: 202, headers: { "Content-Type": "application/json" } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)",
      submission_id: "11111111-1111-4111-8111-111111111111",
    })).resolves.toMatchObject({ job_id: "legacy-job" });
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("treats a missing health endpoint as a legacy backend", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not Found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        job_id: "legacy-job", status: "pending",
        status_url: "/api/v1/metadata/ddl-jobs/legacy-job", events_url: null,
      }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitDDL({
      source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)",
      submission_id: "11111111-1111-4111-8111-111111111111",
    })).resolves.toMatchObject({ job_id: "legacy-job" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ "Content-Type": "application/json" });
  });

  it("does not replay a timed-out submission against a legacy backend", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not Found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }))
      .mockImplementationOnce((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }));
    vi.stubGlobal("fetch", fetchMock);

    const request = submitDDL({
      source: "dw",
      dialect: "mysql",
      ddl: "CREATE TABLE t(id INT)",
      submission_id: "11111111-1111-4111-8111-111111111111",
    });
    const rejection = expect(request).rejects.toMatchObject({
      code: "legacy_submission_timeout", stage: "acceptance", retryable: false,
    });
    await vi.advanceTimersByTimeAsync(30_000);

    await rejection;
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ "Content-Type": "application/json" });
  });

  it.each([
    ["network failure", () => Promise.reject(new TypeError("connection reset"))],
    ["malformed success", () => Promise.resolve(new Response("{}", {
      status: 202, headers: { "Content-Type": "application/json" },
    }))],
    ["proxy failure", () => Promise.resolve(new Response(JSON.stringify({ error: { code: "upstream_error" } }), {
      status: 503, headers: { "Content-Type": "application/json" },
    }))],
  ])("marks a legacy submission uncertain after %s", async (_label, submitResponse) => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not Found" }), {
        status: 404, headers: { "Content-Type": "application/json" },
      }))
      .mockImplementationOnce(submitResponse);
    vi.stubGlobal("fetch", fetchMock);

    await expect(submitDDL({
      source: "dw",
      dialect: "mysql",
      ddl: "CREATE TABLE t(id INT)",
      submission_id: "11111111-1111-4111-8111-111111111111",
    })).rejects.toMatchObject({
      code: "legacy_submission_uncertain", stage: "acceptance", retryable: false,
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("conversation creation", () => {
  it.each([{}, null, { uid: "" }, { uid: 42 }])("rejects invalid successful creation DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));
    await expect(createConversation("user-1")).rejects.toMatchObject({
      status: 502, code: "invalid_response", retryable: true,
    });
  });
});

describe("memory mutation", () => {
  it.each([
    {},
    { memory_uid: "memory-1", event_id: 1, record_version: 2 },
    { memory_uid: "memory-1", event_id: "1", record_version: 2, requires_reprocess: true },
    { memory_uid: "memory-1", event_id: 1, record_version: 2, requires_reprocess: "true" },
  ])("rejects invalid successful update DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));
    await expect(updateMemory("memory-1", { table: "orders" }, 1)).rejects.toMatchObject({
      status: 502, code: "invalid_response", retryable: true,
    });
  });
});

describe("chat turn", () => {
  it.each([
    {},
    { message: {} },
    { message: { uid: "assistant-1", content: "回复" } },
    { message: { uid: 1, content: "回复" }, readiness: "proceed" },
  ])("rejects invalid successful chat DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));

    await expect(sendChatTurn("conversation-1", {
      user_id: "user-1", turn_uid: "turn-1", content: "问题",
      ddl_context: { source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)" },
    })).rejects.toMatchObject({ status: 502, code: "invalid_response", retryable: true });
  });

  it("accepts a complete chat DTO", async () => {
    const payload = { message: { uid: "assistant-1", content: "回复" }, readiness: "proceed" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));

    await expect(sendChatTurn("conversation-1", {
      user_id: "user-1", turn_uid: "turn-1", content: "问题",
      ddl_context: { source: "dw", dialect: "mysql", ddl: "CREATE TABLE t(id INT)" },
    })).resolves.toEqual(payload);
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

describe("memory search", () => {
  const validMemory = {
    uid: "memory-1",
    source: "dw",
    category: "ddl.semantic",
    memory_key: "orders",
    memory_text: "订单事实表",
    content: { table: "orders" },
    record_version: 1,
    status: "active",
  };

  it.each([
    {},
    { items: [], degraded_targets: "elasticsearch" },
    { items: [{ memory: validMemory, score: "1", signals: [] }], degraded_targets: [] },
    { items: [{ memory: { ...validMemory, content: null }, score: 1, signals: [] }], degraded_targets: [] },
    { items: [{ memory: validMemory, score: 1, signals: [42] }], degraded_targets: [] },
  ])("rejects invalid successful search DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(searchMemories("dw", "订单")).rejects.toMatchObject({
      status: 502, code: "invalid_response", stage: "response", retryable: true,
    });
  });

  it("accepts a complete memory search DTO", async () => {
    const payload = {
      items: [{ memory: validMemory, score: 1.25, signals: ["exact"] }],
      degraded_targets: ["qdrant"],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(searchMemories("dw", "订单")).resolves.toEqual(payload);
  });
});

describe("memory history", () => {
  const validEvent = {
    id: 1,
    memory_uid: "memory-1",
    event_type: "ADD",
    old_content: null,
    new_content: { table: "orders" },
    job_id: "job-1",
    actor_type: "WORKFLOW",
    created_at: "2026-08-01T12:00:00Z",
  };
  const validPage = { items: [validEvent], offset: 0, limit: 50, has_more: false };

  it.each([
    {},
    { ...validPage, items: [{ ...validEvent, event_type: "UNKNOWN" }] },
    { ...validPage, items: [{ ...validEvent, old_content: "invalid" }] },
    { ...validPage, items: [{ ...validEvent, actor_type: 42 }] },
    { ...validPage, has_more: "false" },
  ])("rejects invalid successful history DTOs", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(getMemoryHistory("memory-1")).rejects.toMatchObject({
      status: 502, code: "invalid_response", stage: "response", retryable: true,
    });
  });

  it("accepts a complete memory history DTO", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(validPage), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));

    await expect(getMemoryHistory("memory-1")).resolves.toEqual(validPage);
  });
});

describe("memory detail", () => {
  const validMemory = {
    uid: "memory-1", source: "dw", category: "ddl.semantic", memory_key: "orders",
    memory_text: "订单事实表", content: { table: "orders" }, record_version: 1, status: "active",
  };

  it.each([{}, { ...validMemory, uid: undefined }, { ...validMemory, content: null }, { ...validMemory, record_version: "1" }])(
    "rejects invalid successful detail DTOs",
    async (payload) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
        status: 200, headers: { "Content-Type": "application/json" },
      })));

      await expect(getMemory("memory-1")).rejects.toMatchObject({
        status: 502, code: "invalid_response", stage: "response", retryable: true,
      });
    },
  );

  it("accepts a complete memory detail DTO", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(validMemory), {
      status: 200, headers: { "Content-Type": "application/json" },
    })));
    await expect(getMemory("memory-1")).resolves.toEqual(validMemory);
  });
});
