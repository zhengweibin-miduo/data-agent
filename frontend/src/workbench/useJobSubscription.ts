import { useCallback, useEffect, useRef, useState } from "react";

import { TERMINAL_STATUSES, type JobEventSubscription } from "../api/jobEvents";
import type { JobEventData, JobRecord, JobStage } from "../api/types";

export interface JobLifecycleHandlers {
  getAuthoritativeJob: () => Promise<JobRecord>;
  onEvent: (event: JobEventData) => void;
  onJob: (job: JobRecord) => void;
  onConnection: (message: string) => void;
  onError: (error: unknown) => void;
}

export interface JobSubscriptionTransport {
  getJob: (jobId: string) => Promise<JobRecord>;
  connect: (eventsUrl: string, handlers: JobLifecycleHandlers) => JobEventSubscription;
}

interface WatchOptions {
  initialJob?: JobRecord;
  connection?: string;
}

const inferredStage = (job: JobRecord): JobStage => {
  if (job.status === "pending") return "queued";
  if (job.status === "running") return "running";
  return job.status;
};

const defaultFormatError = (cause: unknown): string => cause instanceof Error ? cause.message : String(cause);

export function useJobSubscription({
  transport,
  formatError = defaultFormatError,
}: {
  transport: JobSubscriptionTransport;
  formatError?: (cause: unknown) => string;
}) {
  const [job, setJob] = useState<JobRecord | null>(null);
  const [stage, setStage] = useState<JobStage | null>(null);
  const [reachedStages, setReachedStages] = useState(new Map<JobStage, string>());
  const [connection, setConnection] = useState("尚未连接事件流");
  const [error, setError] = useState("");
  const currentJobId = useRef<string | null>(null);
  const subscription = useRef<JobEventSubscription | null>(null);
  const mounted = useRef(true);

  const isCurrent = useCallback((jobId: string) => currentJobId.current === jobId, []);

  const claim = useCallback((jobId: string) => {
    currentJobId.current = jobId;
  }, []);

  const closeActive = useCallback(() => {
    subscription.current?.close();
    subscription.current = null;
  }, []);

  const stop = useCallback(() => {
    closeActive();
    currentJobId.current = null;
  }, [closeActive]);

  const recordStage = useCallback((next: JobStage, emittedAt = new Date().toISOString()) => {
    if (!mounted.current) return;
    setStage(next);
    setReachedStages((previous) => new Map(previous).set(next, emittedAt));
  }, []);

  const acceptAuthoritativeJob = useCallback((next: JobRecord) => {
    if (!mounted.current || currentJobId.current !== next.job_id) return false;
    setJob(next);
    recordStage(inferredStage(next));
    if (TERMINAL_STATUSES.has(next.status)) closeActive();
    return true;
  }, [closeActive, recordStage]);

  const watch = useCallback((jobId: string, eventsUrl: string, options: WatchOptions = {}) => {
    closeActive();
    claim(jobId);
    if (options.initialJob?.job_id === jobId) {
      const initialStage = inferredStage(options.initialJob);
      setJob(options.initialJob);
      setStage(initialStage);
      setReachedStages(new Map([[initialStage, new Date().toISOString()]]));
    }
    if (options.connection) setConnection(options.connection);

    const ownsJob = () => mounted.current && currentJobId.current === jobId;
    subscription.current = transport.connect(eventsUrl, {
      getAuthoritativeJob: () => transport.getJob(jobId),
      onEvent: (nextEvent) => {
        if (!ownsJob() || nextEvent.job_id !== jobId) return;
        recordStage(nextEvent.stage, nextEvent.emitted_at);
        setJob((previous) => previous ? {
          ...previous,
          status: nextEvent.status,
          revision: nextEvent.revision,
          attempt: nextEvent.attempt,
          question_set_id: nextEvent.status === "waiting_input" ? null : previous.question_set_id,
          questions: nextEvent.status === "waiting_input" ? null : nextEvent.questions,
          result: nextEvent.result,
          error: nextEvent.error,
        } : previous);
        if (TERMINAL_STATUSES.has(nextEvent.status)) closeActive();
      },
      onJob: (nextJob) => {
        if (ownsJob()) acceptAuthoritativeJob(nextJob);
      },
      onConnection: (message) => {
        if (ownsJob()) setConnection(message);
      },
      onError: (cause) => {
        if (ownsJob()) setError(formatError(cause));
      },
    });
  }, [acceptAuthoritativeJob, claim, closeActive, formatError, recordStage, transport]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stop();
    };
  }, [stop]);

  return {
    job,
    stage,
    reachedStages,
    connection,
    error,
    setConnection,
    setError,
    claim,
    isCurrent,
    acceptAuthoritativeJob,
    watch,
    stop,
  };
}
