import { describe, expect, it } from "vitest";

import {
  ACCEPTANCE_RECONCILIATION_WINDOW_MS,
  PENDING_SUBMISSION_KEY,
  classifyPersistedSubmissionNotFound,
  parsePersistedSubmissionAttempt,
  recoveryCoordinates,
  resolveSubmissionAttempt,
  submissionFingerprint,
  workbenchJobIdFromPath,
  workbenchJobPath,
} from "./submissionRecovery";

describe("submission recovery", () => {
  it("preserves the storage, fingerprint, and encoded URL coordinates", () => {
    expect(PENDING_SUBMISSION_KEY).toBe("schema-loom-pending-submission");
    expect(submissionFingerprint("warehouse", "CREATE TABLE orders (id INT);"))
      .toBe("warehouse\nCREATE TABLE orders (id INT);");
    expect(workbenchJobPath("job/with spaces")).toBe("/workbench/job%2Fwith%20spaces");
    expect(workbenchJobIdFromPath("/workbench/job%2Fwith%20spaces")).toBe("job/with spaces");
  });

  it("ignores a malformed encoded job coordinate instead of crashing the workbench", () => {
    expect(workbenchJobIdFromPath("/workbench/%E0%A4%A")).toBeNull();
  });

  it("defaults legacy persisted records to replayable without changing their acceptance start", () => {
    expect(parsePersistedSubmissionAttempt(JSON.stringify({ submissionId: "job-1", startedAt: 42 })))
      .toEqual({ submissionId: "job-1", startedAt: 42, replayable: true });
    expect(parsePersistedSubmissionAttempt("not-json")).toBeNull();
  });

  it("retries a provisional 404 only inside the bounded acceptance window", () => {
    const attempt = { submissionId: "job-1", startedAt: 1_000, replayable: true };
    expect(classifyPersistedSubmissionNotFound(attempt, "job-1", 1_000 + ACCEPTANCE_RECONCILIATION_WINDOW_MS - 1))
      .toBe("retry_acceptance");
    expect(classifyPersistedSubmissionNotFound(attempt, "job-1", 1_000 + ACCEPTANCE_RECONCILIATION_WINDOW_MS))
      .toBe("release");
  });

  it("keeps an uncertain legacy submission non-replayable", () => {
    const attempt = { submissionId: "job-1", startedAt: 0, replayable: false };
    expect(classifyPersistedSubmissionNotFound(attempt, "job-1", Number.MAX_SAFE_INTEGER))
      .toBe("legacy_non_replayable");
    expect(resolveSubmissionAttempt({
      pendingAttempt: { fingerprint: "source\nddl", submissionId: "job-1", replayable: false },
      fingerprint: "source\nddl",
      newSubmissionId: "job-2",
    })).toEqual({ kind: "legacy_non_replayable" });
  });

  it("reconciles a retained coordinate before a different deep link and rejects changed input", () => {
    expect(recoveryCoordinates({
      inMemorySubmissionId: null,
      persistedSubmissionId: "pending-job",
      pathJobId: "deep-link-job",
    })).toEqual({ primaryJobId: "pending-job", fallbackJobId: "deep-link-job" });
    expect(resolveSubmissionAttempt({
      pendingAttempt: { fingerprint: "old\nddl", submissionId: "pending-job", replayable: true },
      fingerprint: "new\nddl",
      newSubmissionId: "new-job",
    })).toEqual({ kind: "input_mismatch" });
  });
});
