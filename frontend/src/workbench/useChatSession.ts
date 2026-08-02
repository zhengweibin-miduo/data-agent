import { type Dispatch, type SetStateAction, useCallback, useRef, useState } from "react";

import { ApiError } from "../api/client";
import type { ChatTurnResponse, ConversationCreated, DDLInput, JobRecord, MetricQuestion } from "../api/types";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

interface ChatAttempt {
  turnUid: string;
  content: string;
  draftQuestion: MetricQuestion | null;
  draftAnswerSnapshot: string | null;
  ddlContext: DDLInput;
}

export type WorkbenchOperation = "preview" | "submit" | "answers" | "chat" | null;

interface UseChatSessionOptions {
  source: string;
  ddl: string;
  answers: Record<string, string>;
  setAnswers: Dispatch<SetStateAction<Record<string, string>>>;
  job: JobRecord | null;
  restoredJobId: string | null;
  interactionBusy: WorkbenchOperation;
  setInteractionBusy: Dispatch<SetStateAction<WorkbenchOperation>> | ((value: WorkbenchOperation) => void);
  setError: (message: string) => void;
  createConversation: (userId: string) => Promise<ConversationCreated>;
  sendChatTurn: (conversationUid: string, payload: {
    user_id: string;
    turn_uid: string;
    content: string;
    ddl_context: DDLInput;
  }) => Promise<ChatTurnResponse>;
  formatError: (cause: unknown, fallback: string) => string;
  randomId: () => string;
}

const USER_KEY = "schema-loom-user";
const CONVERSATION_KEY = "schema-loom-conversation";

export function useChatSession(options: UseChatSessionOptions) {
  const {
    source, ddl, answers, setAnswers, job, restoredJobId, interactionBusy,
    setInteractionBusy, setError, createConversation, sendChatTurn, formatError, randomId,
  } = options;
  const [chatInput, setChatInput] = useState("");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    { id: "welcome", role: "assistant", content: "载入 DDL 后，我可以围绕当前 source、表列和澄清问题协作。" },
  ]);
  const [draftQuestion, setDraftQuestion] = useState<MetricQuestion | null>(null);
  const [failedChat, setFailedChat] = useState<ChatAttempt | null>(null);
  const submittedDDLContext = useRef<DDLInput | null>(null);
  const [hasSubmittedDDLContext, setHasSubmittedDDLContext] = useState(false);
  const restoredDraftContext = useRef(false);

  const recordSubmittedDDLContext = useCallback((context: DDLInput | null, restored = false) => {
    submittedDDLContext.current = context;
    restoredDraftContext.current = restored && context !== null;
    setHasSubmittedDDLContext(context !== null);
  }, []);

  const setAnswer = useCallback((questionId: string, answer: string) => {
    setAnswers((items) => ({ ...items, [questionId]: answer }));
  }, [setAnswers]);

  const sendAttempt = useCallback(async (attempt: ChatAttempt, appendUserMessage: boolean) => {
    if (appendUserMessage) {
      setChatMessages((items) => [...items, { id: attempt.turnUid, role: "user", content: attempt.content }]);
      setChatInput("");
    }
    setInteractionBusy("chat");
    setError("");
    setFailedChat(null);
    try {
      const userId = localStorage.getItem(USER_KEY) ?? `local-${randomId()}`;
      localStorage.setItem(USER_KEY, userId);
      let conversationUid = sessionStorage.getItem(CONVERSATION_KEY);
      let response: ChatTurnResponse;
      try {
        if (!conversationUid) {
          conversationUid = (await createConversation(userId)).uid;
          sessionStorage.setItem(CONVERSATION_KEY, conversationUid);
        }
        response = await sendChatTurn(conversationUid, {
          user_id: userId, turn_uid: attempt.turnUid, content: attempt.content, ddl_context: attempt.ddlContext,
        });
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
        sessionStorage.removeItem(CONVERSATION_KEY);
        conversationUid = (await createConversation(userId)).uid;
        sessionStorage.setItem(CONVERSATION_KEY, conversationUid);
        response = await sendChatTurn(conversationUid, {
          user_id: userId, turn_uid: attempt.turnUid, content: attempt.content, ddl_context: attempt.ddlContext,
        });
      }
      setChatMessages((items) => [...items, {
        id: response.message.uid ?? randomId(), role: "assistant", content: response.message.content,
      }]);
      if (attempt.draftQuestion) {
        setAnswers((items) => (items[attempt.draftQuestion!.question_id] ?? "") === attempt.draftAnswerSnapshot
          ? { ...items, [attempt.draftQuestion!.question_id]: response.message.content }
          : items);
      }
      setDraftQuestion(null);
    } catch (cause) {
      const deterministicClientError = cause instanceof ApiError
        && cause.status >= 400 && cause.status < 500
        && cause.status !== 409 && !cause.retryable;
      setFailedChat(deterministicClientError ? null : attempt);
      if (deterministicClientError && attempt.draftQuestion && restoredDraftContext.current) {
        recordSubmittedDDLContext(null);
        setDraftQuestion(null);
      }
      setError(formatError(cause, deterministicClientError
        ? "AI 请求校验失败，请修正输入后重新发送"
        : "AI 回复生成失败，请重试上一轮"));
    } finally {
      setInteractionBusy(null);
    }
  }, [createConversation, formatError, randomId, recordSubmittedDDLContext, sendChatTurn, setAnswers, setError, setInteractionBusy]);

  const send = useCallback(async () => {
    const content = chatInput.trim();
    if (!content || !source.trim() || !ddl.trim() || interactionBusy !== null || failedChat) return;
    const ddlContext = draftQuestion ? submittedDDLContext.current : null;
    if (draftQuestion && !ddlContext) {
      setError("当前任务缺少已提交的 DDL 上下文，无法起草澄清答案。");
      setDraftQuestion(null);
      return;
    }
    await sendAttempt({
      turnUid: randomId(), content, draftQuestion,
      draftAnswerSnapshot: draftQuestion ? (answers[draftQuestion.question_id] ?? "") : null,
      ddlContext: ddlContext ?? { source: source.trim(), dialect: "mysql", ddl },
    }, true);
  }, [answers, chatInput, ddl, draftQuestion, failedChat, interactionBusy, randomId, sendAttempt, setError, source]);

  const retry = useCallback(async () => {
    if (!failedChat || interactionBusy !== null) return;
    await sendAttempt(failedChat, false);
  }, [failedChat, interactionBusy, sendAttempt]);

  const askToDraft = useCallback((question: MetricQuestion) => {
    if (!submittedDDLContext.current && restoredJobId) {
      if (!job || source.trim() !== job.source || !ddl.trim()) {
        setDraftQuestion(null);
        setError("请先重新载入当前任务的原始 DDL，再让 AI 起草澄清答案。");
        return;
      }
      recordSubmittedDDLContext({ source: source.trim(), dialect: "mysql", ddl }, true);
    }
    setDraftQuestion(question);
    setChatInput(`请根据当前 DDL 起草这个问题的回答：${question.prompt}`);
  }, [ddl, job, recordSubmittedDDLContext, restoredJobId, setError, source]);

  return {
    answers,
    chatInput,
    setChatInput,
    chatMessages,
    draftQuestion,
    failedChat,
    hasSubmittedDDLContext,
    setAnswer,
    send,
    retry,
    askToDraft,
    recordSubmittedDDLContext,
  };
}
