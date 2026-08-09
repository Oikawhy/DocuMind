/**
 * Documents page — document list with real API data and upload.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { buildApiClient, ApiError } from "../api";
import { useToast } from "../components/Toast";
import { UploadModal } from "../components/UploadModal";
import { Spinner } from "../components/Spinner";
import type { AdmissionResponse } from "../types";

interface DocRow {
  id: string;
  title: string;
  declared_type_id: string;
  created_at: string;
  lifecycle_state: string | null;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function lifecycleBadge(state: string | null) {
  if (!state) return <span className="badge badge-neutral">unknown</span>;
  const s = state.toLowerCase();
  if (s === "completed" || s === "accepted")
    return <span className="badge badge-success">● {state}</span>;
  if (s === "processing")
    return <span className="badge badge-processing">◉ {state}</span>;
  if (s === "failed" || s === "quarantined")
    return <span className="badge badge-danger">✕ {state}</span>;
  return <span className="badge badge-neutral">{state}</span>;
}

export function Documents() {
  const navigate = useNavigate();
  const { addToast } = useToast();
  const api = useMemo(() => buildApiClient(), []);

  const [documents, setDocuments] = useState<DocRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const fetchDocuments = useCallback(
    async (cursor?: string) => {
      try {
        setLoading(true);
        const page = await api.listDocuments(cursor);
        const rows = page.items as unknown as DocRow[];
        if (cursor) {
          setDocuments((prev) => [...prev, ...rows]);
        } else {
          setDocuments(rows);
        }
        setNextCursor(page.next_cursor);
      } catch (err) {
        if (err instanceof ApiError) {
          addToast(`Failed to load documents: ${err.message}`, "error");
        } else {
          addToast("Failed to load documents", "error");
        }
      } finally {
        setLoading(false);
      }
    },
    [api, addToast],
  );

  useEffect(() => {
    fetchDocuments();
  }, [fetchDocuments]);

  function handleUploadSuccess(result: AdmissionResponse) {
    setShowUpload(false);
    addToast(`Document uploaded! Operation: ${result.operation_id.slice(0, 8)}…`, "success");
    // Store operation ID for the Operations page
    const ops = JSON.parse(localStorage.getItem("documind_ops") || "[]");
    ops.unshift({ id: result.operation_id, addedAt: Date.now() });
    localStorage.setItem("documind_ops", JSON.stringify(ops.slice(0, 50)));
    // Refresh list
    fetchDocuments();
  }

  async function handleDelete(docId: string) {
    if (!confirm("Are you sure you want to delete this document? This action cannot be undone."))
      return;
    setDeletingId(docId);
    try {
      await api.deleteDocument(docId, crypto.randomUUID());
      addToast("Document deletion started", "success");
      fetchDocuments();
    } catch (err) {
      if (err instanceof ApiError) {
        addToast(`Delete failed: ${err.message}`, "error");
      } else {
        addToast("Delete failed", "error");
      }
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="fade-in">
      <div className="page-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <h1>Documents</h1>
            <p>Manage and search your document library</p>
          </div>
          <button className="btn btn-primary" onClick={() => setShowUpload(true)}>
            📤 Upload Document
          </button>
        </div>
      </div>

      <div className="page-body">
        {showUpload && (
          <UploadModal
            api={api}
            onClose={() => setShowUpload(false)}
            onSuccess={handleUploadSuccess}
          />
        )}

        <div className="card">
          {loading && documents.length === 0 ? (
            <div style={{ padding: 32, textAlign: "center" }}>
              <Spinner size={32} />
              <p style={{ marginTop: 12, color: "var(--text-muted)" }}>Loading documents…</p>
            </div>
          ) : documents.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">📁</div>
              <h3>No documents yet</h3>
              <p>Upload your first document to get started with intelligent analysis.</p>
            </div>
          ) : (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Type</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th style={{ width: 80 }}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {documents.map((doc) => (
                    <tr
                      key={doc.id}
                      className="expandable-trigger"
                      onClick={() => navigate(`/documents/${doc.id}`)}
                    >
                      <td style={{ fontWeight: 500 }}>{doc.title}</td>
                      <td>
                        <span className="badge badge-neutral">
                          {doc.declared_type_id?.slice(0, 8) ?? "—"}
                        </span>
                      </td>
                      <td>{lifecycleBadge(doc.lifecycle_state)}</td>
                      <td style={{ color: "var(--text-muted)", fontSize: "0.8125rem" }}>
                        {formatDate(doc.created_at)}
                      </td>
                      <td>
                        <button
                          className="btn btn-danger"
                          style={{ padding: "4px 10px", fontSize: "0.75rem" }}
                          disabled={deletingId === doc.id}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDelete(doc.id);
                          }}
                        >
                          {deletingId === doc.id ? <Spinner size={12} /> : "🗑"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {nextCursor && (
                <div style={{ textAlign: "center", padding: 16 }}>
                  <button
                    className="btn btn-secondary"
                    onClick={() => fetchDocuments(nextCursor)}
                    disabled={loading}
                  >
                    {loading ? <Spinner size={14} /> : "Load more"}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
