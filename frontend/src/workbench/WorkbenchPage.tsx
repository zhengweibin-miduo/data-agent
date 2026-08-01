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
import { connectJobEvents, TERMINAL_STATUSES, type JobEventSubscription } from "../api/jobEvents";
import type { DDLPreview, JobEventData, JobRecord, JobStage, MetricQuestion } from "../api/types";
import { LineageCanvas } from "./LineageCanvas";
import { TraceDock } from "./TraceDock";

const DEFAULT_DDL = `CREATE TABLE customers (\n  id BIGINT PRIMARY KEY,\n  name VARCHAR(120) NOT NULL\n);\n\nCREATE TABLE orders (\n  id BIGINT PRIMARY KEY,\n  customer_id BIGINT NOT NULL,\n  total DECIMAL(12,2) NOT NULL,\n  FOREIGN KEY (customer_id) REFERENCES customers(id)\n);`;
const MAX_DDL_BYTES = 262_144;
const MAX_SOURCE_CHARS = 128;
const SOURCE_PATTERN = /^[\w.-]+$/;

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

interface ChatAttempt {
  turnUid: string;
  content: string;
  draftQuestion: MetricQuestion | null;
  draftAnswerSnapshot: string | null;
  ddlContext: { source: string; dialect: "mysql"; ddl: string };
}

interface SubmissionAttempt {
  fingerprint: string;
  submissionId: string;
}

// Keep the acceptance coordinate alive across SPA view unmounts. The DDL itself
// remains component-owned and is never copied into browser storage.
let pendingSubmissionAttempt: SubmissionAttempt | null = null;

const randomId = () => globalThis.crypto?.randomUUID?.()
  ?? "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (token) => {
    const value = Math.floor(Math.random() * 16);
    return (token === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });

function restoredJobId(): string | null {
  const match = window.location.pathname.match(/^\/workbench\/([^/]+)$/);
  return match?.[1] ? decodeURIComponent(match[1]) : null;
}

function inferredStage(job: JobRecord): JobStage {
  if (job.status === "pending") return "queued";
  if (job.status === "running") return "running";
  return job.status;
}

interface WorkbenchPageProps {
  onUnsavedChange?: (unsaved: boolean) => void;
  onNavigationBlockChange?: (blocked: boolean) => void;
}

export function WorkbenchPage({ onUnsavedChange, onNavigationBlockChange }: WorkbenchPageProps = {}) {
  const restoredJob = useRef(restoredJobId());
  const [source, setSource] = useState(restoredJob.current ? "" : "commerce_prod");
  const [ddl, setDDL] = useState(restoredJob.current ? "" : DEFAULT_DDL);
  const [restoringJob, setRestoringJob] = useState(Boolean(restoredJob.current));
  const [preview, setPreview] = useState<DDLPreview | null>(null);
  const [previewFingerprint, setPreviewFingerprint] = useState("");
  const [job, setJob] = useState<JobRecord | null>(null);
  const [stage, setStage] = useState<JobStage | null>(null);
  const [reachedStages, setReachedStages] = useState(new Map<JobStage, string>());
  const [connection, setConnection] = useState("尚未连接事件流");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"preview" | "submit" | "answers" | "chat" | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [chatInput, setChatInput] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    { id: "welcome", role: "assistant", content: "载入 DDL 后，我可以围绕当前 source、表列和澄清问题协作。" },
  ]);
  const [draftQuestion, setDraftQuestion] = useState<MetricQuestion | null>(null);
  const [failedChat, setFailedChat] = useState<ChatAttempt | null>(null);
  const [submittedFingerprint, setSubmittedFingerprint] = useState<string | null>(null);
  const subscription = useRef<JobEventSubscription | null>(null);
  const currentJobId = useRef<string | null>(restoredJob.current);
  const mounted = useRef(true);
  const submitController = useRef<AbortController | null>(null);
  const submittedDDLContext = useRef<ChatAttempt["ddlContext"] | null>(null);

  const inputFingerprint = `${source}\n${ddl}`;
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

  const recordStage = useCallback((next: JobStage, emittedAt = new Date().toISOString()) => {
    setStage(next);
    setReachedStages((previous) => new Map(previous).set(next, emittedAt));
  }, []);

  const acceptJob = useCallback((next: JobRecord) => {
    if (currentJobId.current && next.job_id !== currentJobId.current) return;
    setJob(next);
    recordStage(inferredStage(next));
    if (TERMINAL_STATUSES.has(next.status)) subscription.current?.close();
  }, [recordStage]);

  const acceptEvent = useCallback((event: JobEventData) => {
    if (currentJobId.current && event.job_id !== currentJobId.current) return;
    recordStage(event.stage, event.emitted_at);
    setJob((previous) => previous ? {
      ...previous,
      status: event.status,
      revision: event.revision,
      attempt: event.attempt,
      // waiting_input 事件没有 question_set_id；在权威 GET 完成前不得沿用旧坐标。
      question_set_id: event.status === "waiting_input" ? null : previous.question_set_id,
      questions: event.status === "waiting_input" ? null : event.questions,
      result: event.result,
      error: event.error,
    } : previous);
  }, [recordStage]);

  const watchJob = useCallback((jobId: string, eventsUrl: string) => {
    subscription.current?.close();
    currentJobId.current = jobId;
    subscription.current = connectJobEvents(eventsUrl, {
      getAuthoritativeJob: () => getJob(jobId),
      onEvent: acceptEvent,
      onJob: acceptJob,
      onConnection: setConnection,
      onError: (cause) => setError(formatApiError(cause, "任务状态更新失败")),
    });
  }, [acceptEvent, acceptJob]);

  useEffect(() => {
    const jobId = restoredJob.current;
    if (!jobId) return;
    let cancelled = false;
    setConnection("正在恢复任务状态");
    void getJob(jobId).then((record) => {
      if (cancelled || currentJobId.current !== jobId) return;
      setSource(record.source);
      setDDL("");
      setPreview(null);
      setPreviewFingerprint("");
      setSubmittedFingerprint(`${record.source}\n`);
      setRestoringJob(false);
      acceptJob(record);
      if (!TERMINAL_STATUSES.has(record.status)) {
        watchJob(jobId, `/api/v1/metadata/ddl-jobs/${encodeURIComponent(jobId)}/events`);
      }
    }).catch((cause) => {
      if (cancelled || currentJobId.current !== jobId) return;
      setRestoringJob(false);
      setError(formatApiError(cause, "无法恢复这个任务"));
    });
    return () => { cancelled = true; subscription.current?.close(); };
  }, [acceptJob, watchJob]);

  useEffect(() => {
    // StrictMode replays effect setup after cleanup in development.
    mounted.current = true;
    return () => {
      mounted.current = false;
      submitController.current?.abort();
      subscription.current?.close();
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
    if (!inputValid) { setError("请先修正数据源命名或 DDL 字节限制。"); return; }
    if (!preview || previewStale) { setError("请先预览当前 source 和 DDL，再生成语义。"); return; }
    setBusy("submit"); setError(""); setAnswers({});
    const controller = new AbortController();
    submitController.current?.abort();
    submitController.current = controller;
    const submissionId = pendingSubmissionAttempt?.fingerprint === inputFingerprint
      ? pendingSubmissionAttempt.submissionId
      : randomId();
    pendingSubmissionAttempt = { fingerprint: inputFingerprint, submissionId };
    try {
      const accepted = await submitDDL({
        source: source.trim(), dialect: "mysql", ddl, submission_id: submissionId,
      }, controller.signal);
      if (!mounted.current || controller.signal.aborted || submitController.current !== controller) return;
      if (pendingSubmissionAttempt?.submissionId === submissionId) pendingSubmissionAttempt = null;
      const pending: JobRecord = {
        job_id: accepted.job_id, source: source.trim(), status: "pending", revision: 0,
        attempt: 0, question_round: 0, question_set_id: null, questions: null, result: null, error: null,
      };
      currentJobId.current = accepted.job_id;
      const queuedAt = new Date().toISOString();
      setReachedStages(new Map([["queued", queuedAt]]));
      setStage("queued");
      setJob(pending);
      setSubmittedFingerprint(inputFingerprint);
      submittedDDLContext.current = { source: source.trim(), dialect: "mysql", ddl };
      window.history.replaceState(null, "", `/workbench/${encodeURIComponent(accepted.job_id)}`);
      setConnection("任务已受理，正在连接事件流");
      watchJob(accepted.job_id, accepted.events_url ?? `${accepted.status_url}/events`);
    } catch (cause) {
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

  const sendChatAttempt = async (attempt: ChatAttempt, appendUserMessage: boolean) => {
    if (appendUserMessage) {
      setChatMessages((items) => [...items, { id: attempt.turnUid, role: "user", content: attempt.content }]);
      setChatInput("");
    }
    setBusy("chat"); setError(""); setFailedChat(null);
    try {
      const userId = localStorage.getItem("schema-loom-user") ?? `local-${randomId()}`;
      localStorage.setItem("schema-loom-user", userId);
      let conversationUid = sessionStorage.getItem("schema-loom-conversation");
      let response;
      try {
        if (!conversationUid) {
          conversationUid = (await createConversation(userId)).uid;
          sessionStorage.setItem("schema-loom-conversation", conversationUid);
        }
        response = await sendChatTurn(conversationUid, {
          user_id: userId, turn_uid: attempt.turnUid, content: attempt.content, ddl_context: attempt.ddlContext,
        });
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
        sessionStorage.removeItem("schema-loom-conversation");
        conversationUid = (await createConversation(userId)).uid;
        sessionStorage.setItem("schema-loom-conversation", conversationUid);
        response = await sendChatTurn(conversationUid, {
          user_id: userId, turn_uid: attempt.turnUid, content: attempt.content, ddl_context: attempt.ddlContext,
        });
      }
      setChatMessages((items) => [...items, { id: response.message.uid ?? randomId(), role: "assistant", content: response.message.content }]);
      if (attempt.draftQuestion) {
        setAnswers((items) => items[attempt.draftQuestion!.question_id] === attempt.draftAnswerSnapshot
          ? { ...items, [attempt.draftQuestion!.question_id]: response.message.content }
          : items);
      }
      setDraftQuestion(null);
    } catch (cause) {
      const deterministicClientError = cause instanceof ApiError
        && cause.status >= 400 && cause.status < 500
        && cause.status !== 409 && !cause.retryable;
      setFailedChat(deterministicClientError ? null : attempt);
      setError(formatApiError(cause, deterministicClientError
        ? "AI 请求校验失败，请修正输入后重新发送"
        : "AI 回复生成失败，请重试上一轮"));
    }
    finally { setBusy(null); }
  };

  const handleChat = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const content = chatInput.trim();
    if (!content || !source.trim() || !ddl.trim() || busy !== null || failedChat) return;
    const ddlContext = draftQuestion ? submittedDDLContext.current : null;
    if (draftQuestion && !ddlContext) {
      setError("当前任务缺少已提交的 DDL 上下文，无法起草澄清答案。");
      return;
    }
    void sendChatAttempt({
      turnUid: randomId(), content, draftQuestion,
      draftAnswerSnapshot: draftQuestion ? (answers[draftQuestion.question_id] ?? "") : null,
      ddlContext: ddlContext ?? { source: source.trim(), dialect: "mysql", ddl },
    }, true);
  };

  const askToDraft = (question: MetricQuestion) => {
    setDraftQuestion(question);
    setChatInput(`请根据当前 DDL 起草这个问题的回答：${question.prompt}`);
  };

  return (
    <div className="workbench-page">
      <aside className="ddl-panel" aria-labelledby="ddl-title">
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
          <button type="button" className="primary-action" disabled={restoringJob || busy !== null || !inputValid || !preview || previewStale} onClick={() => void handleSubmit()}>{busy === "submit" ? "正在受理…" : "生成语义 →"}</button>
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
        <form className="chat-form" onSubmit={(event) => void handleChat(event)}>
          <label htmlFor="chat-input">补充业务背景或询问当前 DDL</label>
          <textarea id="chat-input" name="chat_content" rows={4} value={chatInput} onChange={(event) => setChatInput(event.target.value)} autoComplete="off" disabled={restoringJob || !ddl.trim() || busy !== null || Boolean(failedChat)} />
          <button className="primary-action" type="submit" disabled={restoringJob || !chatInput.trim() || busy !== null || Boolean(failedChat)}>{busy === "chat" ? "生成中…" : "发送 →"}</button>
        </form>
        {failedChat && (
          <button className="secondary-action chat-retry" type="button" disabled={busy !== null} onClick={() => void sendChatAttempt(failedChat, false)}>
            重试上一轮 AI 回复
          </button>
        )}
      </aside>

      <TraceDock
        job={job} currentStage={stage} reachedStages={reachedStages} connection={connection} error={error}
        answers={answers} interactionBusy={busy !== null} submittingAnswers={busy === "answers"} onAnswerChange={(questionId, answer) => setAnswers((items) => ({ ...items, [questionId]: answer }))}
        onSubmitAnswers={(event) => void handleAnswers(event)} onDraftQuestion={askToDraft}
      />
    </div>
  );
}
