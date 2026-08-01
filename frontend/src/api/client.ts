import type { ApiErrorEnvelope } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");
export const API_REQUEST_TIMEOUT_MS = 30_000;
// A chat turn can perform two readiness calls plus one answer call, and every
// model call can consume the configured initial attempt and two retries.
export const CHAT_REQUEST_TIMEOUT_MS = 660_000;

interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly stage: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(status: number, payload: ApiErrorEnvelope) {
    const projection = payload.error ?? {};
    const code = projection.code ?? `http_${status}`;
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.stage = projection.stage ?? "request";
    this.retryable = Boolean(projection.retryable);
    this.details = projection.details ?? {};
  }
}

export function resolveApiUrl(path: string, base = API_BASE): string {
  if (!base) return path;
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (base.endsWith("/api") && normalizedPath.startsWith("/api/")) {
    return `${base}${normalizedPath.slice(4)}`;
  }
  return `${base}${normalizedPath}`;
}

export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { timeoutMs = API_REQUEST_TIMEOUT_MS, signal: callerSignal, ...requestOptions } = options;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  let timedOut = false;
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(resolveApiUrl(path), {
      ...requestOptions,
      signal: controller.signal,
      headers: requestOptions.body
        ? { "Content-Type": "application/json", ...requestOptions.headers }
        : requestOptions.headers,
    });
    const payload = response.status === 204
      ? null
      : await response.json().catch((error: unknown) => {
        if (timedOut) throw error;
        return {} as ApiErrorEnvelope;
      });
    if (!response.ok) {
      throw new ApiError(response.status, payload as ApiErrorEnvelope);
    }
    return payload as T;
  } catch (error) {
    if (timedOut) {
      throw new ApiError(408, { error: { code: "request_timeout", stage: "request", retryable: true } });
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

export function formatApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  return `${fallback}（${error.code} · ${error.stage}${error.retryable ? " · 可重试" : ""}）`;
}
