import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest, CHAT_REQUEST_TIMEOUT_MS, resolveApiUrl } from "./client";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("API client", () => {
  it("covers the server's full sequential chat model budget", () => {
    expect(CHAT_REQUEST_TIMEOUT_MS).toBe(660_000);
  });

  it("resolves relative and explicit API bases without duplicating /api", () => {
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "")).toBe("/api/v1/metadata/ddl-preview");
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "https://api.example.test")).toBe("https://api.example.test/api/v1/metadata/ddl-preview");
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "https://example.test/api")).toBe("https://example.test/api/v1/metadata/ddl-preview");
  });

  it("projects stable API error fields", () => {
    const error = new ApiError(409, { error: { code: "revision_conflict", stage: "waiting_input", retryable: true, details: { expected: 4 } } });
    expect(error).toMatchObject({ status: 409, code: "revision_conflict", stage: "waiting_input", retryable: true, details: { expected: 4 } });
  });

  it("aborts a request at the configured deadline with a stable timeout error", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })));

    const request = apiRequest("/api/v1/health", { timeoutMs: 25 });
    const expectation = expect(request).rejects.toMatchObject({
      status: 408, code: "request_timeout", stage: "request", retryable: true,
    });
    await vi.advanceTimersByTimeAsync(25);
    await expectation;
  });
});
