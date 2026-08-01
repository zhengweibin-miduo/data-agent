export type JobStatus =
  | "pending"
  | "running"
  | "waiting_input"
  | "succeeded"
  | "rejected"
  | "failed";

export type JobStage =
  | "queued"
  | "running"
  | "parsing"
  | "memory_loading"
  | "metadata_generating"
  | "metadata_validating"
  | "question_planning"
  | "waiting_input"
  | "metric_generating"
  | "metric_validating"
  | "memory_building"
  | "persisting"
  | "succeeded"
  | "rejected"
  | "failed"
  | "stream_error";

export interface DDLInput {
  source: string;
  dialect: "mysql";
  ddl: string;
}

export interface DDLSubmissionInput extends DDLInput {
  submission_id: string;
}

export interface PhysicalColumn {
  id: string;
  name: string;
  data_type: string;
  comment: string | null;
  nullable: boolean;
  structural_role: "primary_key" | "foreign_key" | null;
}

export interface PhysicalTable {
  id: string;
  schema_name: string | null;
  name: string;
  qualified_name: string;
  comment: string | null;
  columns: PhysicalColumn[];
  primary_key: string[];
}

export interface PreviewRelationship {
  source_table_id: string;
  source_column_id: string;
  target_table_id: string;
  target_column_id: string;
  target_table_name: string;
  target_column_name: string;
}

export interface DDLPreview {
  source: string;
  tables: PhysicalTable[];
  relationships: PreviewRelationship[];
  table_count: number;
  column_count: number;
}

export interface MetricQuestion {
  question_id: string;
  prompt: string;
  fact_table_id: string;
  column_ids: string[];
  required: boolean;
}

export interface JobError {
  code: string;
  stage: string;
  retryable: boolean;
  attempt: number;
  details: Record<string, string>;
}

export interface JobResult {
  ddl_hash: string;
  table_count: number;
  column_count: number;
  metric_count: number;
}

export interface JobRecord {
  job_id: string;
  source: string;
  status: JobStatus;
  revision: number;
  attempt: number;
  question_round: number;
  question_set_id: string | null;
  questions: MetricQuestion[] | null;
  result: JobResult | null;
  error: JobError | null;
  created_at?: string;
  updated_at?: string;
  expires_at?: string | null;
}

export interface JobAccepted {
  job_id: string;
  status: "pending";
  status_url: string;
  events_url: string | null;
}

export interface JobEventData {
  job_id: string;
  revision: number;
  attempt: number;
  status: JobStatus;
  stage: JobStage;
  emitted_at: string;
  questions: MetricQuestion[] | null;
  result: JobResult | null;
  error: JobError | null;
}

export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    stage?: string;
    retryable?: boolean;
    details?: Record<string, unknown>;
  };
}

export interface MessageRecord {
  uid?: string;
  content: string;
}

export interface ConversationCreated {
  uid: string;
}

export interface ChatTurnResponse {
  message: MessageRecord;
}

export interface MemoryDetail {
  uid: string;
  source: string;
  category: string;
  memory_key: string;
  memory_text: string;
  content: Record<string, unknown>;
  record_version: number;
  status: string;
}

export interface MemorySearchResponse {
  items: Array<{ memory: MemoryDetail; score: number; signals: string[] }>;
  degraded_targets: string[];
}

export interface MemoryHistoryPage {
  items: Array<{
    event_type: string;
    created_at: string;
    actor_type: string;
  }>;
}

export interface MemoryMutationResult {
  memory_uid: string;
  event_id?: string;
  record_version?: number;
  requires_reprocess?: boolean;
  deleted?: boolean;
}
