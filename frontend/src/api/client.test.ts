import { describe, expect, it } from "vitest";

import { ApiError, resolveApiUrl } from "./client";

describe("API client", () => {
  it("resolves relative and explicit API bases without duplicating /api", () => {
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "")).toBe("/api/v1/metadata/ddl-preview");
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "https://api.example.test")).toBe("https://api.example.test/api/v1/metadata/ddl-preview");
    expect(resolveApiUrl("/api/v1/metadata/ddl-preview", "https://example.test/api")).toBe("https://example.test/api/v1/metadata/ddl-preview");
  });

  it("projects stable API error fields", () => {
    const error = new ApiError(409, { error: { code: "revision_conflict", stage: "waiting_input", retryable: true, details: { expected: 4 } } });
    expect(error).toMatchObject({ status: 409, code: "revision_conflict", stage: "waiting_input", retryable: true, details: { expected: 4 } });
  });
});
