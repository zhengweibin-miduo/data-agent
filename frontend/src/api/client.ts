import type { ApiErrorEnvelope } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");

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
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    ...options,
    headers: options.body
      ? { "Content-Type": "application/json", ...options.headers }
      : options.headers,
  });
  const payload = response.status === 204
    ? null
    : await response.json().catch(() => ({} as ApiErrorEnvelope));
  if (!response.ok) {
    throw new ApiError(response.status, payload as ApiErrorEnvelope);
  }
  return payload as T;
}

export function formatApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  return `${fallback}（${error.code} · ${error.stage}${error.retryable ? " · 可重试" : ""}）`;
}
