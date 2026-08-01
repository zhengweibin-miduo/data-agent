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

const isStringArray = (payload: unknown): payload is string[] =>
  Array.isArray(payload) && payload.every((item) => typeof item === "string");

const isPhysicalColumn = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.id === "string"
    && typeof payload.name === "string"
    && typeof payload.data_type === "string"
    && isNullable(payload.comment, (value) => typeof value === "string")
    && typeof payload.nullable === "boolean"
    && isNullable(
      payload.structural_role,
      (value) => value === "primary_key" || value === "foreign_key",
    );
};

const isPhysicalTable = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.id === "string"
    && isNullable(payload.schema_name, (value) => typeof value === "string")
    && typeof payload.name === "string"
    && typeof payload.qualified_name === "string"
    && isNullable(payload.comment, (value) => typeof value === "string")
    && Array.isArray(payload.columns) && payload.columns.every(isPhysicalColumn)
    && isStringArray(payload.primary_key);
};

const isPreviewRelationship = (payload: unknown): boolean => {
  if (!isRecord(payload)) return false;
  return typeof payload.source_table_id === "string"
    && typeof payload.source_column_id === "string"
    && typeof payload.target_table_id === "string"
    && typeof payload.target_column_id === "string"
    && typeof payload.target_table_name === "string"
    && typeof payload.target_column_name === "string";
};

const isDDLPreview = (payload: unknown): payload is DDLPreview => {
  if (!isRecord(payload)) return false;
  return typeof payload.source === "string"
    && Array.isArray(payload.tables) && payload.tables.every(isPhysicalTable)
    && Array.isArray(payload.relationships) && payload.relationships.every(isPreviewRelationship)
    && Number.isInteger(payload.table_count) && Number(payload.table_count) >= 0
    && Number.isInteger(payload.column_count) && Number(payload.column_count) >= 0;
};

const isMemoryDetail = (payload: unknown): payload is MemoryDetail => {
  if (!isRecord(payload)) return false;
  return typeof payload.uid === "string" && payload.uid.length > 0
    && typeof payload.source === "string" && payload.source.length > 0
    && typeof payload.category === "string" && payload.category.length > 0
    && typeof payload.memory_key === "string" && payload.memory_key.length > 0
    && typeof payload.memory_text === "string"
    && isRecord(payload.content)
    && Number.isInteger(payload.record_version) && Number(payload.record_version) >= 1
    && typeof payload.status === "string" && payload.status.length > 0;
};

const isMemorySearchResponse = (payload: unknown): payload is MemorySearchResponse => {
  if (!isRecord(payload)) return false;
  return Array.isArray(payload.items) && payload.items.every((item) =>
    isRecord(item)
      && isMemoryDetail(item.memory)
      && typeof item.score === "number" && Number.isFinite(item.score) && item.score >= 0
      && isStringArray(item.signals))
    && isStringArray(payload.degraded_targets);
};

const MEMORY_EVENT_TYPES = new Set(["ADD", "UPDATE", "MERGE", "DELETE", "NOOP", "EXPIRE", "LINK"]);
const MEMORY_ACTOR_TYPES = new Set(["WORKFLOW", "USER", "SYSTEM"]);

const isMemoryHistoryPage = (payload: unknown): payload is MemoryHistoryPage => {
  if (!isRecord(payload)) return false;
  return Array.isArray(payload.items) && payload.items.every((item) =>
    isRecord(item)
      && Number.isInteger(item.id) && Number(item.id) >= 1
      && typeof item.memory_uid === "string" && item.memory_uid.length > 0
      && typeof item.event_type === "string" && MEMORY_EVENT_TYPES.has(item.event_type)
      && isNullable(item.old_content, isRecord)
      && isNullable(item.new_content, isRecord)
      && isNullable(item.job_id, (value) => typeof value === "string")
      && typeof item.actor_type === "string" && MEMORY_ACTOR_TYPES.has(item.actor_type)
      && typeof item.created_at === "string" && item.created_at.length > 0)
    && Number.isInteger(payload.offset) && Number(payload.offset) >= 0
    && Number.isInteger(payload.limit) && Number(payload.limit) >= 1
    && typeof payload.has_more === "boolean";
};

const isChatTurnResponse = (payload: unknown): payload is ChatTurnResponse => {
  if (!isRecord(payload) || !isRecord(payload.message)) return false;
  return typeof payload.message.uid === "string" && payload.message.uid.length > 0
    && typeof payload.message.content === "string"
    && (payload.readiness === "proceed"
      || payload.readiness === "data_preparing"
      || payload.readiness === "intent_unresolved");
};

const isConversationCreated = (payload: unknown): payload is ConversationCreated =>
  isRecord(payload) && typeof payload.uid === "string" && payload.uid.length > 0;

const isMemoryUpdateResult = (payload: unknown): payload is MemoryMutationResult =>
  isRecord(payload)
  && typeof payload.memory_uid === "string" && payload.memory_uid.length > 0
  && Number.isInteger(payload.event_id) && Number(payload.event_id) >= 1
  && Number.isInteger(payload.record_version) && Number(payload.record_version) >= 1
  && typeof payload.requires_reprocess === "boolean";

const isMemoryDeleteResult = (payload: unknown): payload is MemoryMutationResult =>
  isRecord(payload)
  && typeof payload.memory_uid === "string" && payload.memory_uid.length > 0
  && payload.deleted === true;

const supportsSubmissionIdempotency = async (): Promise<boolean> => {
  const health = await apiRequest<unknown>("/api/v1/health");
  return isRecord(health)
    && isRecord(health.capabilities)
    && health.capabilities.ddl_submission_idempotency === true;
};

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
    validateResponse: isDDLPreview,
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
  const { submission_id: submissionId, ...legacyCompatibleInput } = input;
  const idempotencySupported = await supportsSubmissionIdempotency();
  const submit = () => apiRequest<JobAccepted>("/api/v1/metadata/ddl-jobs", {
    method: "POST",
    // Keep the JSON body compatible with backend versions released before
    // client-coordinated acceptance. Only a backend that advertises support
    // receives the custom header, so legacy cross-origin CORS remains valid.
    body: JSON.stringify(legacyCompatibleInput),
    headers: idempotencySupported ? { "Idempotency-Key": submissionId } : undefined,
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
    validateResponse: isConversationCreated,
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
    validateResponse: isChatTurnResponse,
  });

export const searchMemories = (source: string, query: string): Promise<MemorySearchResponse> =>
  apiRequest(`/api/v1/metadata/memories/search?source=${encodeURIComponent(source)}&query=${encodeURIComponent(query)}`, {
    validateResponse: isMemorySearchResponse,
  });

export const getMemory = (uid: string): Promise<MemoryDetail> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}`, {
    validateResponse: isMemoryDetail,
  });

export const getMemoryHistory = (uid: string): Promise<MemoryHistoryPage> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}/history`, {
    validateResponse: isMemoryHistoryPage,
  });

export const updateMemory = (
  uid: string,
  content: Record<string, unknown>,
  expectedVersion: number,
): Promise<MemoryMutationResult> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}`, {
    method: "PATCH",
    body: JSON.stringify({ content, expected_version: expectedVersion }),
    validateResponse: isMemoryUpdateResult,
  });

export const deleteMemory = (
  uid: string,
  expectedVersion: number,
): Promise<MemoryMutationResult> =>
  apiRequest(`/api/v1/metadata/memories/${encodeURIComponent(uid)}?expected_version=${expectedVersion}`, {
    method: "DELETE",
    validateResponse: isMemoryDeleteResult,
  });
