/**
 * Centralised API client for the DocuMind backend.
 *
 * - In OIDC mode the access_token from `react-oidc-context` is injected
 *   via the `useApi()` hook.
 * - In dev mode (no OIDC authority) the optional `VITE_DEV_TOKEN` env
 *   var is used instead.
 * - All requests go through the Vite dev proxy (`/v1` → localhost:8000).
 */

import type {
  AdmissionResponse,
  ApiErrorBody,
  ChatResponse,
  CursorPage,
  Document,
  HealthResponse,
  Operation,
  SessionDetail,
  SessionSummary,
} from "./types";

/* ------------------------------------------------------------------ */
/* Error wrapper                                                       */
/* ------------------------------------------------------------------ */

export class ApiError extends Error {
  code: string;
  traceId: string;
  details: { field: string | null; reason: string }[];

  constructor(body: ApiErrorBody, public status: number) {
    super(body.message);
    this.name = "ApiError";
    this.code = body.code;
    this.traceId = body.trace_id;
    this.details = body.details;
  }
}

/* ------------------------------------------------------------------ */
/* Client                                                              */
/* ------------------------------------------------------------------ */

export class ApiClient {
  private token: string | null;

  constructor(token: string | null) {
    this.token = token;
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { ...extra };
    if (this.token) {
      h["Authorization"] = `Bearer ${this.token}`;
    }
    return h;
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(path, {
      ...init,
      headers: { ...this.headers(), ...init?.headers },
    });
    if (!res.ok) {
      let body: ApiErrorBody;
      try {
        const json = await res.json();
        body = json.error ?? {
          code: "UNKNOWN",
          message: res.statusText,
          trace_id: "",
          details: [],
        };
      } catch {
        body = {
          code: "NETWORK_ERROR",
          message: res.statusText || "Request failed",
          trace_id: "",
          details: [],
        };
      }
      throw new ApiError(body, res.status);
    }
    if (res.status === 204) return undefined as T;
    return res.json() as Promise<T>;
  }

  /* ---- Health ---------------------------------------------------- */

  health(): Promise<HealthResponse> {
    return this.request("/health");
  }

  /* ---- Documents ------------------------------------------------- */

  listDocuments(
    cursor?: string,
    limit = 50,
    filters?: { type?: string; state?: string },
  ): Promise<CursorPage> {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    params.set("limit", String(limit));
    if (filters?.type) params.set("type", filters.type);
    if (filters?.state) params.set("state", filters.state);
    return this.request(`/v1/documents?${params}`);
  }

  getDocument(id: string): Promise<Document> {
    return this.request(`/v1/documents/${id}`);
  }

  /**
   * Upload a document with progress tracking (T9-17).
   *
   * Uses XMLHttpRequest instead of fetch to support upload progress events.
   */
  async uploadDocument(
    file: File,
    title: string,
    declaredType: string,
    idempotencyKey: string,
    options?: {
      labels?: string;
      onProgress?: (percent: number) => void;
    },
  ): Promise<AdmissionResponse> {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    form.append("declared_type", declaredType);
    form.append("labels", options?.labels ?? "");

    if (options?.onProgress) {
      // Use XMLHttpRequest for progress tracking.
      return new Promise<AdmissionResponse>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/v1/documents");

        // Set auth header.
        if (this.token) {
          xhr.setRequestHeader("Authorization", `Bearer ${this.token}`);
        }
        xhr.setRequestHeader("Idempotency-Key", idempotencyKey);

        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            options.onProgress!(Math.round((e.loaded / e.total) * 100));
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(JSON.parse(xhr.responseText));
          } else {
            try {
              const json = JSON.parse(xhr.responseText);
              reject(new ApiError(json.error ?? {
                code: "UNKNOWN",
                message: xhr.statusText,
                trace_id: "",
                details: [],
              }, xhr.status));
            } catch {
              reject(new Error(xhr.statusText || "Upload failed"));
            }
          }
        };

        xhr.onerror = () => reject(new Error("Network error during upload"));
        xhr.send(form);
      });
    }

    return this.request("/v1/documents", {
      method: "POST",
      headers: this.headers({ "Idempotency-Key": idempotencyKey }),
      body: form,
    });
  }

  async uploadVersion(
    documentId: string,
    file: File,
    idempotencyKey: string,
  ): Promise<AdmissionResponse> {
    const form = new FormData();
    form.append("file", file);
    return this.request(`/v1/documents/${documentId}/versions`, {
      method: "POST",
      headers: this.headers({ "Idempotency-Key": idempotencyKey }),
      body: form,
    });
  }

  deleteDocument(
    id: string,
    idempotencyKey: string,
  ): Promise<{ operation_id: string; status_url: string }> {
    return this.request(`/v1/documents/${id}`, {
      method: "DELETE",
      headers: this.headers({ "Idempotency-Key": idempotencyKey }),
    });
  }

  /* ---- Operations ------------------------------------------------ */

  getOperation(id: string): Promise<Operation> {
    return this.request(`/v1/operations/${id}`);
  }

  /* ---- Chat ------------------------------------------------------ */

  chat(
    message: string,
    sessionId?: string,
    documentIds?: string[],
    mode?: string,
    locale = "en",
  ): Promise<ChatResponse> {
    return this.request("/v1/chat", {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        message,
        session_id: sessionId ?? null,
        locale,
        document_ids: documentIds ?? [],
        mode: mode ?? null,
      }),
    });
  }

  listSessions(cursor?: string, limit = 20): Promise<CursorPage<SessionSummary>> {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    params.set("limit", String(limit));
    return this.request(`/v1/chat/sessions?${params}`);
  }

  getSession(id: string): Promise<SessionDetail> {
    return this.request(`/v1/chat/sessions/${id}`);
  }

  deleteSession(id: string): Promise<void> {
    return this.request(`/v1/chat/sessions/${id}`, { method: "DELETE" });
  }

  submitFeedback(
    messageId: string,
    score: number,
    comment?: string,
  ): Promise<{ status: string }> {
    return this.request(`/v1/chat/messages/${messageId}/feedback`, {
      method: "POST",
      headers: this.headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ score, comment: comment ?? null }),
    });
  }
}

/* ------------------------------------------------------------------ */
/* React helper                                                        */
/* ------------------------------------------------------------------ */

/**
 * Build an API client for a given access token.
 *
 * In dev mode (no OIDC) we fall back to VITE_DEV_TOKEN.
 */
export function buildApiClient(accessToken?: string): ApiClient {
  const token =
    accessToken || (import.meta.env.VITE_DEV_TOKEN as string) || null;
  return new ApiClient(token);
}
