import { resolveApiUrl } from "./client";
import type { JobEventData, JobRecord } from "./types";

export const TERMINAL_STATUSES = new Set(["succeeded", "rejected", "failed"]);
export const JOB_EVENT_TYPES = [
  "snapshot",
  "progress",
  "waiting_input",
  "succeeded",
  "rejected",
  "failed",
] as const;

const JOB_STATUSES = new Set(["pending", "running", "waiting_input", "succeeded", "rejected", "failed"]);
const JOB_STAGES = new Set([
  "queued", "running", "parsing", "memory_loading", "metadata_generating",
  "metadata_validating", "question_planning", "waiting_input", "metric_generating",
  "metric_validating", "memory_building", "persisting", "succeeded", "rejected",
  "failed", "stream_error",
]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const isNullable = (value: unknown, validate: (candidate: unknown) => boolean): boolean =>
  value === null || validate(value);

const isMetricQuestion = (value: unknown): boolean => isRecord(value)
  && typeof value.question_id === "string" && value.question_id.length > 0
  && typeof value.prompt === "string" && value.prompt.length > 0
  && typeof value.fact_table_id === "string"
  && Array.isArray(value.column_ids) && value.column_ids.every((item) => typeof item === "string")
  && typeof value.required === "boolean";

const isJobResult = (value: unknown): boolean => isRecord(value)
  && typeof value.ddl_hash === "string"
  && typeof value.table_count === "number"
  && typeof value.column_count === "number"
  && typeof value.metric_count === "number";

const isJobError = (value: unknown): boolean => isRecord(value)
  && typeof value.code === "string"
  && typeof value.stage === "string"
  && typeof value.retryable === "boolean"
  && Number.isInteger(value.attempt)
  && isRecord(value.details)
  && Object.values(value.details).every((detail) => typeof detail === "string");

const isJobEventData = (value: unknown): value is JobEventData => isRecord(value)
  && typeof value.job_id === "string" && value.job_id.length > 0
  && Number.isInteger(value.revision) && Number(value.revision) >= 0
  && Number.isInteger(value.attempt) && Number(value.attempt) >= 0
  && typeof value.status === "string" && JOB_STATUSES.has(value.status)
  && typeof value.stage === "string" && JOB_STAGES.has(value.stage)
  && typeof value.emitted_at === "string" && value.emitted_at.length > 0
  && isNullable(value.questions, (questions) => Array.isArray(questions) && questions.every(isMetricQuestion))
  && isNullable(value.result, isJobResult)
  && isNullable(value.error, isJobError);

interface JobEventHandlers {
  getAuthoritativeJob: () => Promise<JobRecord>;
  onEvent: (event: JobEventData) => void;
  onJob: (job: JobRecord) => void;
  onConnection: (message: string) => void;
  onError: (error: unknown) => void;
}

export interface JobEventSubscription {
  close: () => void;
}

export function connectJobEvents(
  eventsUrl: string,
  handlers: JobEventHandlers,
  EventSourceType: typeof EventSource | undefined = globalThis.EventSource,
): JobEventSubscription {
  let source: EventSource | null = null;
  let pollTimer: number | null = null;
  let closed = false;
  let authoritativeReadInFlight = false;
  let authoritativeReadQueued = false;
  let authoritativeReadMustSucceed = false;
  let authoritativeRetryTimer: number | null = null;

  const close = () => {
    closed = true;
    source?.close();
    source = null;
    if (pollTimer !== null) window.clearInterval(pollTimer);
    if (authoritativeRetryTimer !== null) window.clearTimeout(authoritativeRetryTimer);
    pollTimer = null;
    authoritativeRetryTimer = null;
  };

  const poll = async (mustSucceed = false) => {
    if (closed) return;
    if (authoritativeReadInFlight) {
      authoritativeReadQueued = true;
      authoritativeReadMustSucceed ||= mustSucceed;
      return;
    }
    authoritativeReadInFlight = true;
    try {
      const job = await handlers.getAuthoritativeJob();
      if (closed) return;
      if (mustSucceed && authoritativeRetryTimer !== null) {
        window.clearTimeout(authoritativeRetryTimer);
        authoritativeRetryTimer = null;
      }
      handlers.onJob(job);
      if (TERMINAL_STATUSES.has(job.status)) {
        close();
        handlers.onConnection("任务已到达终态");
      }
    } catch (error) {
      if (!closed) {
        handlers.onError(error);
        if (mustSucceed && authoritativeRetryTimer === null) {
          authoritativeRetryTimer = window.setTimeout(() => {
            authoritativeRetryTimer = null;
            void poll(true);
          }, 3000);
        }
      }
    } finally {
      authoritativeReadInFlight = false;
      if (authoritativeReadQueued && !closed) {
        const queuedMustSucceed = authoritativeReadMustSucceed;
        authoritativeReadQueued = false;
        authoritativeReadMustSucceed = false;
        void poll(queuedMustSucceed);
      }
    }
  };

  const startPolling = () => {
    if (pollTimer !== null || closed) return;
    handlers.onConnection("事件流已中断，正在查询权威状态并等待重连");
    void poll();
    pollTimer = window.setInterval(() => void poll(), 3000);
  };

  const checkInterruptedStream = async () => {
    if (closed) return;
    // 浏览器原生 EventSource 会自动重连并携带 Last-Event-ID；这里不关闭连接，
    // 只回读一次权威状态，避免等待重连期间把旧事件当作当前任务事实。
    handlers.onConnection("事件流已中断，正在查询权威状态并等待重连");
    await poll();
  };

  const stopPolling = () => {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    pollTimer = null;
  };

  if (!EventSourceType) {
    startPolling();
    return { close };
  }

  source = new EventSourceType(resolveApiUrl(eventsUrl));
  source.onopen = () => {
    // 保留同一个 EventSource，让浏览器在重连时自动携带 Last-Event-ID；GET 只作权威回退。
    stopPolling();
    handlers.onConnection("事件流已连接");
  };
  const receive = (event: MessageEvent<string>) => {
    try {
      const data: unknown = JSON.parse(event.data);
      if (!isJobEventData(data)) {
        throw new Error("事件流响应不符合 JobEventData 契约");
      }
      handlers.onEvent(data);
      if (data.status === "waiting_input") {
        void poll(true);
      }
      if (TERMINAL_STATUSES.has(data.status)) {
        close();
        handlers.onConnection("任务已到达终态");
      }
    } catch (error) {
      handlers.onError(error);
      startPolling();
    }
  };
  JOB_EVENT_TYPES.forEach((type) => source?.addEventListener(type, receive as EventListener));
  source.addEventListener("stream_error", startPolling);
  source.onerror = () => void checkInterruptedStream();
  return { close };
}
