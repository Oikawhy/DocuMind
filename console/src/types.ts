/**
 * TypeScript interfaces mirroring backend Pydantic schemas.
 *
 * Kept in a single file so every page imports from one source of truth.
 */

/* ------------------------------------------------------------------ */
/* Documents                                                           */
/* ------------------------------------------------------------------ */

export interface DocumentVersion {
  id: string;
  version_number: number;
  lifecycle_state: string;
  original_filename: string;
  byte_size: number;
  content_sha256: string;
  created_at: string;
  completed_at: string | null;
  failure_code: string | null;
}

export interface Document {
  id: string;
  title: string;
  declared_type_id: string;
  created_at: string;
  deletion_requested_at: string | null;
  versions: DocumentVersion[];
}

export interface DocumentSummary {
  id: string;
  title: string;
  declared_type_id: string;
  created_at: string;
  lifecycle_state: string | null;
}

export interface AdmissionResponse {
  document_id: string;
  version_id: string;
  operation_id: string;
  lifecycle_state: string;
  status_url: string;
}

/* ------------------------------------------------------------------ */
/* Operations                                                          */
/* ------------------------------------------------------------------ */

export interface OperationStage {
  name: string;
  status: string;
  trace_id: string;
  started_at: string | null;
  ended_at: string | null;
  safe_error_code: string | null;
}

export interface Operation {
  id: string;
  type: string;
  status: string;
  document_id: string | null;
  version_id: string | null;
  safe_error_code: string | null;
  stages: OperationStage[];
}

/* ------------------------------------------------------------------ */
/* Chat                                                                */
/* ------------------------------------------------------------------ */

export interface Citation {
  citation_id: string;
  document_id: string;
  version_id: string;
  version_number: number;
  chunk_id: string;
  excerpt: string;
  content_sha256: string;
}

export interface ChatResponse {
  session_id: string;
  message_id: string;
  answer: string;
  abstained: boolean;
  citations: Citation[];
  confidence: number | null;
  route: string | null;
  agent_path: string[];
  policy_revisions: string[];
  trace_id: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  token_count: number | null;
  created_at: string;
}

export interface SessionSummary {
  id: string;
  created_at: string;
  retention_expires_at: string;
  message_count: number;
}

export interface SessionDetail {
  id: string;
  created_at: string;
  retention_expires_at: string;
  messages: ChatMessage[];
  summary: string | null;
}

/* ------------------------------------------------------------------ */
/* Common                                                              */
/* ------------------------------------------------------------------ */

export interface CursorPage<T = Record<string, unknown>> {
  items: T[];
  next_cursor: string | null;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  trace_id: string;
  details: { field: string | null; reason: string }[];
}

export interface ApiErrorResponse {
  error: ApiErrorBody;
}

export interface HealthResponse {
  service: string;
  status: string;
}
