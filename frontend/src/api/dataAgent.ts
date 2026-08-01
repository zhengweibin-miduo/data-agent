import { ApiError, apiRequest, CHAT_REQUEST_TIMEOUT_MS } from "./client";
import type {
  ChatTurnResponse,
  ConversationCreated,
  DDLInput,
  DDLPreview,
  DDLSubmissionInput,
  JobAccepted,
  JobRecord,
  MemoryDetail,
  MemoryHistoryPage,
  MemoryMutationResult,
  MemorySearchResponse,
} from "./types";

const JOB_STATUSES = new Set(["pending", "running", "waiting_input", "succeeded", "rejected", "failed"]);

const isRecord = (payload: unknown): payload is Record<string, unknown> =>
  Boolean(payload) && typeof payload === "object" && !Array.isArray(payload);

const isNullable = (payload: unknown, validate: (value: unknown) => boolean): boolean =>
  payload === null || validate(payload);

const isMetricQuestion = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.question_id === "string" && payload.question_id.length > 0
    && typeof payload.prompt === "string" && payload.prompt.length > 0
    && typeof payload.fact_table_id === "string"
    && Array.isArray(payload.column_ids) && payload.column_ids.every((item) => typeof item === "string")
    && typeof payload.required === "boolean";
};

const isJobResult = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.ddl_hash === "string"
    && typeof payload.table_count === "number"
    && typeof payload.column_count === "number"
    && typeof payload.metric_count === "number";
};

const isJobError = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.code === "string"
    && typeof payload.stage === "string"
    && typeof payload.retryable === "boolean"
    && typeof payload.attempt === "number"
    && isRecord(payload.details)
    && Object.values(payload.details).every((value) => typeof value === "string");
};

const isJobRecord = (payload: unknown): payload is JobRecord => {
  if (!isRecord(payload)) return false;
  return typeof payload.job_id === "string" && payload.job_id.length > 0
    && typeof payload.source === "string" && payload.source.length > 0
    && typeof payload.status === "string" && JOB_STATUSES.has(payload.status)
    && Number.isInteger(payload.revision) && Number(payload.revision) >= 0
    && Number.isInteger(payload.attempt) && Number(payload.attempt) >= 0
    && Number.isInteger(payload.question_round) && Number(payload.question_round) >= 0
    && isNullable(payload.question_set_id, (value) => typeof value === "string")
    && isNullable(payload.questions, (value) => Array.isArray(value) && value.every(isMetricQuestion))
    && isNullable(payload.result, isJobResult)
    && isNullable(payload.error, isJobError)
    && typeof payload.created_at === "string"
    && typeof payload.updated_at === "string"
    && isNullable(payload.expires_at, (value) => typeof value === "string");
};

export const previewDDL = (input: DDLInput): Promise<DDLPreview> =>
  apiRequest("/api/v1/metadata/ddl-preview", {
    method: "POST",
    body: JSON.stringify(input),
  });

export async function submitDDL(input: DDLSubmissionInput, signal?: AbortSignal): Promise<JobAccepted> {
  const isJobAccepted = (payload: unknown): payload is JobAccepted => {
    if (!payload || typeof payload !== "object") return false;
    const candidate = payload as Record<string, unknown>;
    return typeof candidate.job_id === "string" && candidate.job_id.length > 0
      && candidate.status === "pending"
      && typeof candidate.status_url === "string" && candidate.status_url.length > 0
      && (candidate.events_url === null || typeof candidate.events_url === "string");
  };
  const submit = () => apiRequest<JobAccepted>("/api/v1/metadata/ddl-jobs", {
    method: "POST",
    body: JSON.stringify(input),
    signal,
    validateResponse: isJobAccepted,
  });
  try {
    return await submit();
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== "request_timeout" || signal?.aborted) throw error;
    return submit();
  }
}

export const getJob = (jobId: string): Promise<JobRecord> =>
  apiRequest(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(jobId)}`, {
    validateResponse: isJobRecord,
  });

export const submitAnswers = (
  jobId: string,
  payload: {
    revision: number;
    question_set_id: string;
    answers: Array<{ question_id: string; answer: string }>;
  },
): Promise<JobRecord> =>
  apiRequest(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(jobId)}/answers`, {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const createConversation = (userId: string): Promise<ConversationCreated> =>
  apiRequest("/api/v1/conversations", {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });

export const sendChatTurn = (
  conversationUid: string,
  payload: {
    user_id: string;
    turn_uid: string;
    content: string;
    ddl_context: DDLInput;
  },
): Promise<ChatTurnResponse> =>
  apiRequest(`/api/v1/conversations/${encodeURIComponent(conversationUid)}/chat-turns`, {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: CHAT_REQUEST_TIMEOUT_MS,
  });

export const searchMemories = (source: string, query: string): Promise<MemorySearchResponse> =>
  apiRequest(`/api/v1/metadata/memories/search?source=${encodeURIComponent(source)}&query=${encodeURIComponent(query)}`);

export const getMemory = (uid: string): Promise<MemoryDetail> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}`);

export const getMemoryHistory = (uid: string): Promise<MemoryHistoryPage> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}/history`);

export const updateMemory = (
  uid: string,
  content: Record<string, unknown>,
  expectedVersion: number,
): Promise<MemoryMutationResult> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}`, {
    method: "PATCH",
    body: JSON.stringify({ content, expected_version: expectedVersion }),
  });

export const deleteMemory = (
  uid: string,
  expectedVersion: number,
): Promise<MemoryMutationResult> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}?expected_version=${expectedVersion}`, {
    method: "DELETE",
  });
