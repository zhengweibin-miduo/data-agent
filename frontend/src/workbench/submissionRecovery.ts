export const PENDING_SUBMISSION_KEY = "schema-loom-pending-submission";
export const ACCEPTANCE_RECONCILIATION_WINDOW_MS = 120_000;

export interface SubmissionAttempt {
  fingerprint: string;
  submissionId: string;
  replayable: boolean;
}

export interface PersistedSubmissionAttempt {
  submissionId: string;
  startedAt: number;
  replayable: boolean;
}

export function submissionFingerprint(source: string, ddl: string): string {
  return `${source}\n${ddl}`;
}

export function parsePersistedSubmissionAttempt(raw: string | null): PersistedSubmissionAttempt | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedSubmissionAttempt>;
    return typeof parsed.submissionId === "string" && typeof parsed.startedAt === "number"
      ? {
        submissionId: parsed.submissionId,
        startedAt: parsed.startedAt,
        replayable: typeof parsed.replayable === "boolean" ? parsed.replayable : true,
      }
      : null;
  } catch {
    return null;
  }
}

export function workbenchJobIdFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/workbench\/([^/]+)$/);
  if (!match?.[1]) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

export function workbenchJobPath(jobId: string): string {
  return `/workbench/${encodeURIComponent(jobId)}`;
}

export function recoveryCoordinates({
  inMemorySubmissionId,
  persistedSubmissionId,
  pathJobId,
}: {
  inMemorySubmissionId: string | null;
  persistedSubmissionId: string | null;
  pathJobId: string | null;
}): { primaryJobId: string | null; fallbackJobId: string | null } {
  const primaryJobId = inMemorySubmissionId ?? persistedSubmissionId ?? pathJobId;
  return {
    primaryJobId,
    fallbackJobId: primaryJobId && pathJobId !== primaryJobId ? pathJobId : null,
  };
}

export type PersistedNotFoundDecision = "legacy_non_replayable" | "retry_acceptance" | "release";

export function classifyPersistedSubmissionNotFound(
  attempt: PersistedSubmissionAttempt | null,
  jobId: string,
  now: number,
): PersistedNotFoundDecision {
  if (attempt?.submissionId !== jobId) return "release";
  if (!attempt.replayable) return "legacy_non_replayable";
  return now - attempt.startedAt < ACCEPTANCE_RECONCILIATION_WINDOW_MS
    ? "retry_acceptance"
    : "release";
}

export type SubmissionAttemptDecision =
  | { kind: "input_mismatch" }
  | { kind: "legacy_non_replayable" }
  | { kind: "ready"; attempt: SubmissionAttempt };

export function resolveSubmissionAttempt({
  pendingAttempt,
  fingerprint,
  newSubmissionId,
}: {
  pendingAttempt: SubmissionAttempt | null;
  fingerprint: string;
  newSubmissionId: string;
}): SubmissionAttemptDecision {
  if (pendingAttempt && pendingAttempt.fingerprint !== fingerprint) return { kind: "input_mismatch" };
  if (pendingAttempt && !pendingAttempt.replayable) return { kind: "legacy_non_replayable" };
  return {
    kind: "ready",
    attempt: pendingAttempt ?? { fingerprint, submissionId: newSubmissionId, replayable: true },
  };
}
