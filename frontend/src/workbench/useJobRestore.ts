import { useEffect, useState } from "react";

import { ApiError } from "../api/client";
import { TERMINAL_STATUSES } from "../api/jobEvents";
import type { JobRecord } from "../api/types";
import {
  classifyPersistedSubmissionNotFound,
  type PersistedSubmissionAttempt,
  workbenchJobPath,
} from "./submissionRecovery";

interface UseJobRestoreOptions {
  initialJobId: string | null;
  fallbackJobId: string | null;
  persistedSubmission: PersistedSubmissionAttempt | null;
  getJob: (jobId: string) => Promise<JobRecord>;
  claimJob: (jobId: string) => void;
  isCurrentJob: (jobId: string) => boolean;
  acceptJob: (job: JobRecord) => boolean | void;
  watchJob: (jobId: string, eventsUrl: string) => void;
  stopWatchingJob: () => void;
  setConnection: (message: string) => void;
  setError: (message: string) => void;
  clearRecoveryCoordinate: (jobId: string) => void;
  onRestored: (job: JobRecord) => void;
  onReleased: () => void;
  formatError: (cause: unknown, fallback: string) => string;
  delay?: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

const browserDelay = (milliseconds: number, signal?: AbortSignal) => new Promise<void>((resolve) => {
  if (signal?.aborted) {
    resolve();
    return;
  }
  let timer: number | null = null;
  const finish = () => {
    if (timer !== null) window.clearTimeout(timer);
    signal?.removeEventListener("abort", finish);
    resolve();
  };
  signal?.addEventListener("abort", finish, { once: true });
  timer = window.setTimeout(() => {
    timer = null;
    signal?.removeEventListener("abort", finish);
    resolve();
  }, milliseconds);
});

export function useJobRestore({
  initialJobId,
  fallbackJobId,
  persistedSubmission,
  getJob,
  claimJob,
  isCurrentJob,
  acceptJob,
  watchJob,
  stopWatchingJob,
  setConnection,
  setError,
  clearRecoveryCoordinate,
  onRestored,
  onReleased,
  formatError,
  delay = browserDelay,
}: UseJobRestoreOptions) {
  const [restoringJob, setRestoringJob] = useState(Boolean(initialJobId));
  const [restoredJobId, setRestoredJobId] = useState(initialJobId);

  useEffect(() => {
    if (!initialJobId) return;
    let cancelled = false;
    const retryController = new AbortController();
    setConnection("正在恢复任务状态");
    void (async () => {
      const candidates = [initialJobId, fallbackJobId].filter((jobId): jobId is string => Boolean(jobId));
      for (const jobId of candidates) {
        claimJob(jobId);
        let retryDelay = 1_000;
        while (!cancelled && isCurrentJob(jobId)) {
          try {
            const record = await getJob(jobId);
            if (cancelled || !isCurrentJob(jobId)) return;
            clearRecoveryCoordinate(jobId);
            const jobPath = workbenchJobPath(jobId);
            if (window.location.pathname !== jobPath) window.history.replaceState(null, "", jobPath);
            onRestored(record);
            setRestoredJobId(jobId);
            setRestoringJob(false);
            acceptJob(record);
            if (!TERMINAL_STATUSES.has(record.status)) {
              watchJob(jobId, `/api/v1/metadata/ddl-jobs/${encodeURIComponent(jobId)}/events`);
            }
            return;
          } catch (cause) {
            if (cancelled || !isCurrentJob(jobId)) return;
            const notFound = cause instanceof ApiError && cause.status === 404;
            if (notFound) {
              const decision = classifyPersistedSubmissionNotFound(persistedSubmission, jobId, Date.now());
              if (decision === "legacy_non_replayable") {
                setError("旧版后端的任务受理结果未知，不能安全重复提交；请由管理员查询任务状态或升级后端。");
                return;
              }
              if (decision === "retry_acceptance") {
                await delay(retryDelay, retryController.signal);
                retryDelay = Math.min(retryDelay * 2, 5_000);
                continue;
              }
              clearRecoveryCoordinate(jobId);
              break;
            }
            setError(formatError(cause, "无法恢复这个任务，正在重试"));
            await delay(retryDelay, retryController.signal);
            retryDelay = Math.min(retryDelay * 2, 5_000);
          }
        }
      }
      if (cancelled) return;
      stopWatchingJob();
      setRestoredJobId(null);
      setRestoringJob(false);
      onReleased();
    })();
    return () => {
      cancelled = true;
      retryController.abort();
      stopWatchingJob();
    };
  }, [
    acceptJob, claimJob, clearRecoveryCoordinate, delay, fallbackJobId, formatError, getJob,
    initialJobId, isCurrentJob, onReleased, onRestored, persistedSubmission, setConnection,
    setError, stopWatchingJob, watchJob,
  ]);

  return { restoringJob, restoredJobId };
}
