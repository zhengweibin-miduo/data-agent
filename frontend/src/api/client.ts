const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");
export const API_REQUEST_TIMEOUT_MS = 30_000;
// A chat turn can perform two readiness calls plus one answer call, and every
// model call can consume the configured initial attempt and two retries.
export const CHAT_REQUEST_TIMEOUT_MS = 660_000;

interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
  validateResponse?: (payload: unknown) => boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly stage: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(status: number, payload: unknown) {
    const envelope = payload && typeof payload === "object" && !Array.isArray(payload)
      ? payload as Record<string, unknown>
      : {};
    const projection = envelope.error && typeof envelope.error === "object" && !Array.isArray(envelope.error)
      ? envelope.error as Record<string, unknown>
      : {};
    const code = typeof projection.code === "string" ? projection.code : `http_${status}`;
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.stage = typeof projection.stage === "string" ? projection.stage : "request";
    this.retryable = typeof projection.retryable === "boolean" ? projection.retryable : false;
    this.details = projection.details && typeof projection.details === "object" && !Array.isArray(projection.details)
      ? projection.details as Record<string, unknown>
      : {};
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
  const {
    timeoutMs = API_REQUEST_TIMEOUT_MS,
    signal: callerSignal,
    validateResponse,
    ...requestOptions
  } = options;
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
    let payload: unknown = null;
    if (response.status !== 204) {
      try {
        payload = await response.json();
      } catch (error) {
        if (timedOut) throw error;
        if (response.ok) {
          throw new ApiError(502, {
            error: { code: "invalid_response", stage: "response", retryable: true },
          });
        }
        payload = {};
      }
    }
    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    if (validateResponse && !validateResponse(payload)) {
      throw new ApiError(502, {
        error: { code: "invalid_response", stage: "response", retryable: true },
      });
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
