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
  apiRequest(`/api/v1/metadata/ddl-jobs/${encodeURIComponent(jobId)}`);

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
