/**
 * DocumentViewer — detail page for a single document with real API data.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { buildApiClient, ApiError } from "../api";
import { useToast } from "../components/Toast";
import { Spinner } from "../components/Spinner";
import type { Document as DocType } from "../types";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function lifecycleBadge(state: string) {
  const s = state.toLowerCase();
  if (s === "completed" || s === "accepted")
    return <span className="badge badge-success">● {state}</span>;
  if (s === "processing")
    return <span className="badge badge-processing">◉ {state}</span>;
  if (s === "failed" || s === "quarantined")
    return <span className="badge badge-danger">✕ {state}</span>;
  return <span className="badge badge-neutral">{state}</span>;
}

export function DocumentViewer() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { addToast } = useToast();
  const api = useMemo(() => buildApiClient(), []);

  const [doc, setDoc] = useState<DocType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchDocument = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.getDocument(id);
      setDoc(data);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`${err.code}: ${err.message}`);
      } else {
        setError("Failed to load document");
      }
    } finally {
      setLoading(false);
    }
  }, [api, id]);

  useEffect(() => {
    fetchDocument();
  }, [fetchDocument]);

  async function handleDelete() {
    if (!id) return;
    if (!confirm("Are you sure you want to delete this document?")) return;
    try {
      await api.deleteDocument(id, crypto.randomUUID());
      addToast("Deletion started", "success");
      navigate("/documents");
    } catch (err) {
      if (err instanceof ApiError) {
        addToast(`Delete failed: ${err.message}`, "error");
      }
    }
  }

  async function handleUploadVersion(file: File) {
    if (!id) return;
    try {
      const result = await api.uploadVersion(id, file, crypto.randomUUID());
      addToast(`New version uploaded: ${result.version_id.slice(0, 8)}…`, "success");
      const ops = JSON.parse(localStorage.getItem("documind_ops") || "[]");
      ops.unshift({ id: result.operation_id, addedAt: Date.now() });
      localStorage.setItem("documind_ops", JSON.stringify(ops.slice(0, 50)));
      fetchDocument();
    } catch (err) {
      if (err instanceof ApiError) {
        addToast(`Upload failed: ${err.message}`, "error");
      }
    }
  }

  if (loading) {
    return (
      <div className="fade-in" style={{ padding: 64, textAlign: "center" }}>
        <Spinner size={32} />
        <p style={{ marginTop: 12, color: "var(--text-muted)" }}>Loading document…</p>
      </div>
    );
  }

  if (error || !doc) {
    return (
      <div className="fade-in">
        <div className="page-header">
          <h1>Document Not Found</h1>
          <p>{error || "The document could not be loaded."}</p>
        </div>
        <div className="page-body">
          <button className="btn btn-secondary" onClick={() => navigate("/documents")}>
            ← Back to Documents
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fade-in">
      <div className="page-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <button
              className="btn btn-secondary"
              style={{ marginBottom: 12, padding: "4px 12px", fontSize: "0.75rem" }}
              onClick={() => navigate("/documents")}
            >
              ← Back
            </button>
            <h1>{doc.title}</h1>
            <p>ID: {doc.id}</p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <label className="btn btn-primary" style={{ cursor: "pointer" }}>
              📎 Upload Version
              <input
                type="file"
                style={{ display: "none" }}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) handleUploadVersion(f);
                }}
              />
            </label>
            <button className="btn btn-danger" onClick={handleDelete}>
              🗑 Delete
            </button>
          </div>
        </div>
      </div>

      <div className="page-body">
        <div className="card-grid" style={{ marginBottom: 24 }}>
          <div className="stat-card">
            <div className="stat-label">Type</div>
            <div className="stat-value" style={{ fontSize: "1rem", marginTop: 8 }}>
              {doc.declared_type_id?.slice(0, 8) ?? "—"}
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Versions</div>
            <div className="stat-value">{doc.versions.length}</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Created</div>
            <div className="stat-value" style={{ fontSize: "1rem", marginTop: 8 }}>
              {formatDate(doc.created_at)}
            </div>
          </div>
        </div>

        <div className="card">
          <h3 style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: 16 }}>
            Version History
          </h3>
          {doc.versions.length === 0 ? (
            <p style={{ color: "var(--text-muted)", textAlign: "center", padding: 32 }}>
              No versions found. The document may still be processing.
            </p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Filename</th>
                  <th>Size</th>
                  <th>Status</th>
                  <th>SHA256</th>
                  <th>Created</th>
                  <th>Completed</th>
                </tr>
              </thead>
              <tbody>
                {doc.versions.map((v) => (
                  <tr key={v.id}>
                    <td style={{ fontWeight: 600 }}>v{v.version_number}</td>
                    <td className="truncate" style={{ maxWidth: 200 }}>
                      {v.original_filename}
                    </td>
                    <td>{formatBytes(v.byte_size)}</td>
                    <td>{lifecycleBadge(v.lifecycle_state)}</td>
                    <td
                      className="truncate"
                      style={{ maxWidth: 120, fontSize: "0.75rem", fontFamily: "monospace" }}
                    >
                      {v.content_sha256.slice(0, 16)}…
                    </td>
                    <td style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                      {formatDate(v.created_at)}
                    </td>
                    <td style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                      {v.completed_at ? formatDate(v.completed_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
