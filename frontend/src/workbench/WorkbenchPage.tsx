import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, formatApiError } from "../api/client";
import {
  createConversation,
  getJob,
  previewDDL,
  sendChatTurn,
  submitAnswers,
  submitDDL,
} from "../api/dataAgent";
import { connectJobEvents, TERMINAL_STATUSES } from "../api/jobEvents";
import type { DDLPreview, JobRecord } from "../api/types";
import { LineageCanvas } from "./LineageCanvas";
import {
  PENDING_SUBMISSION_KEY,
  parsePersistedSubmissionAttempt,
  recoveryCoordinates,
  resolveSubmissionAttempt,
  submissionFingerprint,
  type PersistedSubmissionAttempt,
  type SubmissionAttempt,
  workbenchJobIdFromPath,
  workbenchJobPath,
} from "./submissionRecovery";
import { TraceDock } from "./TraceDock";
import { useChatSession, type WorkbenchOperation } from "./useChatSession";
import { useJobRestore } from "./useJobRestore";
import { useJobSubscription } from "./useJobSubscription";

const DEFAULT_DDL = `CREATE TABLE customers (\n  id BIGINT PRIMARY KEY,\n  name VARCHAR(120) NOT NULL\n);\n\nCREATE TABLE orders (\n  id BIGINT PRIMARY KEY,\n  customer_id BIGINT NOT NULL,\n  total DECIMAL(12,2) NOT NULL,\n  FOREIGN KEY (customer_id) REFERENCES customers(id)\n);`;
const MAX_DDL_BYTES = 262_144;
const MAX_SOURCE_CHARS = 128;
const SOURCE_PATTERN = /^[\w.-]+$/;

// Keep the acceptance coordinate alive across SPA view unmounts. The DDL itself
// remains component-owned and is never copied into browser storage.
let pendingSubmissionAttempt: SubmissionAttempt | null = null;

function readPersistedSubmissionAttempt(): PersistedSubmissionAttempt | null {
  const raw = sessionStorage.getItem(PENDING_SUBMISSION_KEY);
  const parsed = parsePersistedSubmissionAttempt(raw);
  if (raw && !parsed) sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
  return parsed;
}

function persistSubmissionAttempt(submissionId: string, replayable = true): void {
  const previous = readPersistedSubmissionAttempt();
  const startedAt = previous?.submissionId === submissionId ? previous.startedAt : Date.now();
  sessionStorage.setItem(PENDING_SUBMISSION_KEY, JSON.stringify({ submissionId, startedAt, replayable }));
}

function clearPersistedSubmissionAttempt(submissionId: string): void {
  if (readPersistedSubmissionAttempt()?.submissionId === submissionId) {
    sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
  }
}

const randomId = () => globalThis.crypto?.randomUUID?.()
  ?? "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (token) => {
    const value = Math.floor(Math.random() * 16);
    return (token === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });

function jobIdFromCurrentPath(): string | null {
  return workbenchJobIdFromPath(window.location.pathname);
}

const jobSubscriptionTransport = { getJob, connect: connectJobEvents };
const formatJobSubscriptionError = (cause: unknown) => formatApiError(cause, "任务状态更新失败");

interface WorkbenchPageProps {
  onUnsavedChange?: (unsaved: boolean) => void;
  onNavigationBlockChange?: (blocked: boolean) => void;
}

export function WorkbenchPage({ onUnsavedChange, onNavigationBlockChange }: WorkbenchPageProps = {}) {
  const persistedSubmission = useRef(readPersistedSubmissionAttempt());
  const recovery = useRef(recoveryCoordinates({
    inMemorySubmissionId: pendingSubmissionAttempt?.submissionId ?? null,
    persistedSubmissionId: persistedSubmission.current?.submissionId ?? null,
    pathJobId: jobIdFromCurrentPath(),
  }));
  const restoredJob = useRef(recovery.current.primaryJobId);
  const fallbackRestoredJob = useRef(recovery.current.fallbackJobId);
  const [source, setSource] = useState(restoredJob.current ? "" : "commerce_prod");
  const [ddl, setDDL] = useState(restoredJob.current ? "" : DEFAULT_DDL);
  const [preview, setPreview] = useState<DDLPreview | null>(null);
  const [previewFingerprint, setPreviewFingerprint] = useState("");
  const [busy, setBusy] = useState<WorkbenchOperation>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittedFingerprint, setSubmittedFingerprint] = useState<string | null>(null);
  const mounted = useRef(true);
  const submitController = useRef<AbortController | null>(null);
  const {
    job,
    stage,
    reachedStages,
    connection,
    error,
    setConnection,
    setError,
    claim: claimJob,
    isCurrent: isCurrentJob,
    acceptAuthoritativeJob: acceptJob,
    watch: watchJob,
    stop: stopWatchingJob,
  } = useJobSubscription({
    transport: jobSubscriptionTransport,
    formatError: formatJobSubscriptionError,
  });
  const clearRecoveryCoordinate = useCallback((jobId: string) => {
    if (pendingSubmissionAttempt?.submissionId === jobId) pendingSubmissionAttempt = null;
    clearPersistedSubmissionAttempt(jobId);
  }, []);
  const handleRestoredJob = useCallback((record: JobRecord) => {
    setSource(record.source);
    setDDL("");
    setPreview(null);
    setPreviewFingerprint("");
    setSubmittedFingerprint(submissionFingerprint(record.source, ""));
  }, []);
  const handleRestoreReleased = useCallback(() => {
    setSource("commerce_prod");
    setDDL(DEFAULT_DDL);
    setSubmittedFingerprint(null);
  }, []);
  const { restoringJob, restoredJobId } = useJobRestore({
    initialJobId: restoredJob.current,
    fallbackJobId: fallbackRestoredJob.current,
    persistedSubmission: persistedSubmission.current,
    getJob,
    claimJob,
    isCurrentJob,
    acceptJob,
    watchJob,
    stopWatchingJob,
    setConnection,
    setError,
    clearRecoveryCoordinate,
    onRestored: handleRestoredJob,
    onReleased: handleRestoreReleased,
    formatError: formatApiError,
  });
  const {
    chatInput,
    setChatInput,
    chatMessages,
    failedChat,
    setAnswer,
    send: sendChat,
    retry: retryChat,
    askToDraft,
    recordSubmittedDDLContext,
  } = useChatSession({
    source,
    ddl,
    answers,
    setAnswers,
    job,
    restoredJobId,
    interactionBusy: busy,
    setInteractionBusy: setBusy,
    setError,
    createConversation,
    sendChatTurn,
    formatError: formatApiError,
    randomId,
  });

  const inputFingerprint = submissionFingerprint(source, ddl);
  const ddlBytes = new TextEncoder().encode(ddl).length;
  const sourceValue = source.trim();
  const sourceValid = sourceValue.length > 0
    && sourceValue.length <= MAX_SOURCE_CHARS
    && SOURCE_PATTERN.test(sourceValue);
  const inputValid = sourceValid && ddl.trim().length > 0 && ddlBytes <= MAX_DDL_BYTES;
  const previewStale = Boolean(preview && previewFingerprint !== inputFingerprint);
  const highlightedIds = job?.status === "waiting_input"
    ? (job.questions ?? []).flatMap((question) => [question.fact_table_id, ...question.column_ids])
    : [];

  useEffect(() => {
    onUnsavedChange?.(Boolean(ddl.trim()) && inputFingerprint !== submittedFingerprint);
    return () => onUnsavedChange?.(false);
  }, [ddl, inputFingerprint, onUnsavedChange, submittedFingerprint]);

  useEffect(() => {
    onNavigationBlockChange?.(busy === "chat" || Boolean(failedChat));
    return () => onNavigationBlockChange?.(false);
  }, [busy, failedChat, onNavigationBlockChange]);

  useEffect(() => {
    // StrictMode replays effect setup after cleanup in development.
    mounted.current = true;
    return () => {
      mounted.current = false;
      submitController.current?.abort();
    };
  }, []);

  const handlePreview = async () => {
    if (!sourceValid) { setError("数据源需为 1–128 字符，仅可使用字母、数字、下划线、点或连字符。"); return; }
    if (ddlBytes > MAX_DDL_BYTES) { setError("DDL 超过 262,144 bytes，请拆分后再预览。"); return; }
    setBusy("preview"); setError("");
    try {
      const result = await previewDDL({ source: source.trim(), dialect: "mysql", ddl });
      setPreview(result); setPreviewFingerprint(inputFingerprint);
    } catch (cause) { setError(formatApiError(cause, "结构预览失败")); }
    finally { setBusy(null); }
  };

  const handleSubmit = async () => {
    if (job && !TERMINAL_STATUSES.has(job.status)) {
      setError("当前任务仍在处理或等待澄清，请完成后再提交新任务。");
      return;
    }
    if (!inputValid) { setError("请先修正数据源命名或 DDL 字节限制。"); return; }
    if (!preview || previewStale) { setError("请先预览当前 source 和 DDL，再生成语义。"); return; }
    const submissionDecision = resolveSubmissionAttempt({
      pendingAttempt: pendingSubmissionAttempt,
      fingerprint: inputFingerprint,
      newSubmissionId: randomId(),
    });
    if (submissionDecision.kind === "input_mismatch") {
      setError("上一份 DDL 的任务受理结果尚未确认，请恢复原输入并重试以找回任务坐标。");
      return;
    }
    if (submissionDecision.kind === "legacy_non_replayable") {
      setError("旧版后端的任务受理结果未知，不能安全重复提交；请由管理员查询任务状态或升级后端。");
      return;
    }
    setBusy("submit"); setError(""); setAnswers({});
    const controller = new AbortController();
    submitController.current?.abort();
    submitController.current = controller;
    const submissionId = submissionDecision.attempt.submissionId;
    pendingSubmissionAttempt = submissionDecision.attempt;
    persistSubmissionAttempt(submissionId);
    const previousPath = window.location.pathname;
    // The client-generated submission ID is also the server job ID. Persist it
    // before POST so a full reload can reconcile an accepted-but-unanswered request.
    window.history.replaceState(null, "", workbenchJobPath(submissionId));
    try {
      const accepted = await submitDDL({
        source: source.trim(), dialect: "mysql", ddl, submission_id: submissionId,
      }, controller.signal, (idempotencySupported) => {
        if (idempotencySupported || pendingSubmissionAttempt?.submissionId !== submissionId) return;
        pendingSubmissionAttempt.replayable = false;
        persistSubmissionAttempt(submissionId, false);
      });
      if (!mounted.current || controller.signal.aborted || submitController.current !== controller) return;
      if (pendingSubmissionAttempt?.submissionId === submissionId) pendingSubmissionAttempt = null;
      clearPersistedSubmissionAttempt(submissionId);
      const pending: JobRecord = {
        job_id: accepted.job_id, source: source.trim(), status: "pending", revision: 0,
        attempt: 0, question_round: 0, question_set_id: null, questions: null, result: null, error: null,
      };
      setSubmittedFingerprint(inputFingerprint);
      recordSubmittedDDLContext({ source: source.trim(), dialect: "mysql", ddl });
      window.history.replaceState(null, "", workbenchJobPath(accepted.job_id));
      watchJob(accepted.job_id, accepted.events_url ?? `${accepted.status_url}/events`, {
        initialJob: pending,
        connection: "任务已受理，正在连接事件流",
      });
    } catch (cause) {
      if (cause instanceof ApiError
        && (cause.code === "legacy_submission_timeout" || cause.code === "legacy_submission_uncertain")
        && pendingSubmissionAttempt?.submissionId === submissionId) {
        pendingSubmissionAttempt.replayable = false;
        persistSubmissionAttempt(submissionId, false);
      }
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500
        && cause.status !== 408 && !cause.retryable
        && pendingSubmissionAttempt?.submissionId === submissionId) {
        pendingSubmissionAttempt = null;
        clearPersistedSubmissionAttempt(submissionId);
        window.history.replaceState(null, "", previousPath);
      }
      if (mounted.current && !controller.signal.aborted) setError(formatApiError(cause, "任务提交失败"));
    } finally {
      if (submitController.current === controller) submitController.current = null;
      if (mounted.current) setBusy(null);
    }
  };

  const handleAnswers = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy !== null || !job?.question_set_id) return;
    const missingRequired = (job.questions ?? []).find(
      (question) => question.required && !answers[question.question_id]?.trim(),
    );
    if (missingRequired) {
      setError("请填写所有必答业务依据后再继续。");
      document.getElementById(`answer-${missingRequired.question_id}`)?.focus();
      return;
    }
    setBusy("answers"); setError("");
    try {
      const updated = await submitAnswers(job.job_id, {
        revision: job.revision,
        question_set_id: job.question_set_id,
        answers: (job.questions ?? []).filter((question) => answers[question.question_id]?.trim()).map((question) => ({
          question_id: question.question_id, answer: answers[question.question_id]!.trim(),
        })),
      });
      acceptJob(updated); setAnswers({});
      watchJob(job.job_id, `/api/v1/metadata/ddl-jobs/${encodeURIComponent(job.job_id)}/events`);
    } catch (cause) { setError(formatApiError(cause, "澄清回答未提交")); }
    finally { setBusy(null); }
  };

  const handleChat = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void sendChat();
  };

  return (
    <div className="workbench-page">
      <aside className="ddl-panel" aria-labelledby="ddl-title" aria-busy={restoringJob || busy === "preview" || busy === "submit"}>
        <div className="panel-kicker">DDL / SCHEMA</div><h1 id="ddl-title">把物理结构织成语义</h1>
        <label htmlFor="source">数据源</label>
        <input id="source" name="source" value={source} onChange={(event) => setSource(event.target.value)} maxLength={MAX_SOURCE_CHARS} pattern="[\\w.-]+" autoComplete="off" spellCheck={false} aria-describedby="source-help" disabled={restoringJob} required />
        <p id="source-help" className="field-note">1–128 字符：字母、数字、下划线、点或连字符。</p>
        <label htmlFor="ddl">MySQL DDL</label>
        <textarea id="ddl" name="ddl" className="ddl-editor" value={ddl} onChange={(event) => setDDL(event.target.value)} autoComplete="off" spellCheck={false} aria-describedby="ddl-help" disabled={restoringJob} required />
        <div id="ddl-help" className={`editor-footer ${ddlBytes > MAX_DDL_BYTES ? "over-limit" : ""}`}>
          <span>{new Intl.NumberFormat("en-US").format(ddlBytes)} / 262,144 bytes</span>
          <span>50 tables · 500 columns</span>
        </div>
        <div className="ddl-actions">
          <button type="button" className="secondary-action" disabled={restoringJob || busy !== null || !inputValid} onClick={() => void handlePreview()}>{busy === "preview" ? "正在解析…" : "预览结构"}</button>
          <button type="button" className="primary-action" disabled={restoringJob || busy !== null || Boolean(job && !TERMINAL_STATUSES.has(job.status)) || !inputValid || !preview || previewStale} onClick={() => void handleSubmit()}>{busy === "submit" ? "正在受理…" : "生成语义 →"}</button>
        </div>
        <div className={`preview-state ${previewStale ? "stale" : ""}`} aria-live="polite">
          {previewStale ? "DDL 已变化，请重新预览" : preview ? `${preview.table_count} 表 · ${preview.column_count} 列 · PREVIEW READY` : "尚未建立结构预览"}
        </div>
      </aside>

      <LineageCanvas preview={preview} highlightedIds={highlightedIds} />

      <aside className="chat-panel" aria-labelledby="chat-title">
        <div className="panel-kicker">AI · DDL COPILOT</div><h2 id="chat-title">当前结构协作</h2>
        <div className="chat-log" role="log" aria-live="polite">
          {chatMessages.map((message) => <div key={message.id} className={`chat-message ${message.role}`}><strong>{message.role === "assistant" ? "AI" : "你"}</strong><p>{message.content}</p></div>)}
        </div>
        <form className="chat-form" onSubmit={(event) => void handleChat(event)} aria-busy={busy === "chat"}>
          <label htmlFor="chat-input">补充业务背景或询问当前 DDL</label>
          <textarea id="chat-input" name="chat_content" rows={4} value={chatInput} onChange={(event) => setChatInput(event.target.value)} autoComplete="off" disabled={restoringJob || !ddl.trim() || busy !== null || Boolean(failedChat)} />
          <button className="primary-action" type="submit" disabled={restoringJob || !chatInput.trim() || busy !== null || Boolean(failedChat)}>{busy === "chat" ? "生成中…" : "发送 →"}</button>
        </form>
        {failedChat && (
          <button className="secondary-action chat-retry" type="button" disabled={busy !== null} onClick={() => void retryChat()}>
            重试上一轮 AI 回复
          </button>
        )}
      </aside>

      <TraceDock
        job={job} currentStage={stage} reachedStages={reachedStages} connection={connection} error={error}
        answers={answers} interactionBusy={busy !== null} submittingAnswers={busy === "answers"} onAnswerChange={setAnswer}
        onSubmitAnswers={(event) => void handleAnswers(event)} onDraftQuestion={askToDraft}
      />
    </div>
  );
}
