import { useEffect, useRef, type FormEvent } from "react";

import type { JobRecord, JobStage, MetricQuestion } from "../api/types";

const STAGES: ReadonlyArray<readonly [JobStage, string]> = [
  ["queued", "任务已受理"],
  ["running", "开始处理"],
  ["parsing", "解析物理结构"],
  ["memory_loading", "加载可复用知识"],
  ["metadata_generating", "生成表列语义"],
  ["metadata_validating", "校验表列语义"],
  ["question_planning", "规划指标澄清"],
  ["waiting_input", "等待业务澄清"],
  ["metric_generating", "生成指标定义"],
  ["metric_validating", "校验指标定义"],
  ["memory_building", "整理可复用知识"],
  ["persisting", "持久化语义快照"],
  ["succeeded", "语义元数据已生成"],
];

const STATUS_LABELS: Record<string, string> = {
  pending: "已受理",
  running: "运行中",
  waiting_input: "等待澄清",
  succeeded: "生成完成",
  rejected: "已拒绝",
  failed: "处理失败",
};

interface TraceDockProps {
  job: JobRecord | null;
  currentStage: JobStage | null;
  reachedStages: Map<JobStage, string>;
  connection: string;
  error: string;
  answers: Record<string, string>;
  interactionBusy: boolean;
  submittingAnswers: boolean;
  onAnswerChange: (questionId: string, answer: string) => void;
  onSubmitAnswers: (event: FormEvent<HTMLFormElement>) => void;
  onDraftQuestion: (question: MetricQuestion) => void;
}

function statusTone(status: string): string {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "rejected") return "error";
  if (status === "pending" || status === "waiting_input") return "pending";
  return "running";
}

export function TraceDock({
  job,
  currentStage,
  reachedStages,
  connection,
  error,
  answers,
  interactionBusy,
  submittingAnswers,
  onAnswerChange,
  onSubmitAnswers,
  onDraftQuestion,
}: TraceDockProps) {
  const errorRef = useRef<HTMLDivElement>(null);
  const questions = job?.status === "waiting_input" && job.question_set_id ? job.questions ?? [] : [];
  const visibleStages = STAGES.filter(([stage]) => reachedStages.has(stage) || stage === currentStage);
  useEffect(() => {
    const active = document.activeElement;
    const editing = active instanceof HTMLInputElement
      || active instanceof HTMLTextAreaElement
      || active instanceof HTMLSelectElement;
    if (error && !editing) errorRef.current?.focus();
  }, [error]);
  return (
    <section className="trace-dock" aria-labelledby="trace-title" aria-busy={interactionBusy}>
      <header className="trace-header">
        <div><span>PUBLIC STAGES</span><h2 id="trace-title">Schema Trace</h2></div>
        <div className="task-coordinate">
          <span>{job ? `source: ${job.source}` : ""}</span>
          <span>{job ? `job: ${job.job_id}` : ""}</span>
          <span className="status-chip" data-tone={job ? statusTone(job.status) : undefined}>{job ? STATUS_LABELS[job.status] ?? job.status : "未提交"}</span>
          <span className="connection-state" aria-live="polite">{connection}</span>
        </div>
      </header>
      <ol className="trace-list">
        {!job && <li className="current"><strong>等待提交</strong><small>先预览结构，再生成语义</small></li>}
        {visibleStages.map(([stage, label]) => (
          <li
            key={stage}
            className={`${stage === currentStage ? "current" : "reached"} ${stage === "waiting_input" ? "waiting" : ""}`}
          >
            <strong>{label}</strong>
            <small>{reachedStages.get(stage) ? new Date(reachedStages.get(stage) ?? "").toLocaleTimeString("zh-CN", { hour12: false }) : "当前阶段"}</small>
          </li>
        ))}
      </ol>
      {questions.length > 0 && (
        <form className="clarification" onSubmit={onSubmitAnswers} aria-busy={submittingAnswers}>
          <h3>需要你确认的业务含义</h3>
          {questions.map((question) => (
            <div key={question.question_id} className="question-card">
              <p className="question-meta">{question.required ? "必答" : "可选"} · {[question.fact_table_id, ...question.column_ids].filter(Boolean).join(" · ")}</p>
              <div>
                <label htmlFor={`answer-${question.question_id}`}>{question.prompt}</label>
                <textarea
                  id={`answer-${question.question_id}`}
                  name={`answer_${question.question_id}`}
                  required={question.required}
                  rows={3}
                  autoComplete="off"
                  value={answers[question.question_id] ?? ""}
                  disabled={interactionBusy}
                  onChange={(event) => onAnswerChange(question.question_id, event.target.value)}
                />
              </div>
              <div className="question-actions"><button className="quiet-action" type="button" disabled={interactionBusy} onClick={() => onDraftQuestion(question)}>让 AI 起草</button></div>
            </div>
          ))}
          <button className="primary-action" type="submit" disabled={interactionBusy}>
            {submittingAnswers ? "正在提交…" : "提交回答并继续 →"}
          </button>
        </form>
      )}
      {job?.status === "succeeded" && job.result && (
        <div className="terminal-result">
          <h3>语义元数据已生成</h3>
          <div className="result-counts">
            {[[job.result.table_count, "表"], [job.result.column_count, "列"], [job.result.metric_count, "指标"]].map(([value, label]) => (
              <span key={label}><strong>{value}</strong><small>{label}</small></span>
            ))}
          </div>
          <p className="mono">DDL 指纹 {job.result.ddl_hash}</p>
          <a href={`/knowledge?source=${encodeURIComponent(job.source)}`}>到知识记忆核对口径 →</a>
        </div>
      )}
      {(job?.status === "rejected" || job?.status === "failed") && (
        <div className="terminal-result failed">
          <h3>{job.status === "rejected" ? "任务已拒绝" : "任务处理失败"}</h3>
          <p className="mono">{job.error?.code ?? "unknown_error"} · {job.error?.stage ?? job.status}{job.error?.retryable ? " · 可重试" : ""}</p>
          <p>{job.error?.retryable ? "可检查本机服务后重新提交这份 DDL。" : "请修正 DDL 或业务输入后重新提交。"}</p>
        </div>
      )}
      {error && <div ref={errorRef} className="error-summary" role="alert" tabIndex={-1}>{error}</div>}
    </section>
  );
}
