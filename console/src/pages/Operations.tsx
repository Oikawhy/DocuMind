/**
 * Operations page — live polling of document processing pipelines.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { buildApiClient, ApiError } from "../api";
import { Spinner } from "../components/Spinner";
import type { Operation } from "../types";

interface TrackedOp {
  id: string;
  addedAt: number;
}


function statusBadge(status: string) {
  const s = status.toLowerCase();
  if (s === "completed" || s === "succeeded")
    return <span className="badge badge-success">● {status}</span>;
  if (s === "running" || s === "pending" || s === "processing")
    return <span className="badge badge-processing">◉ {status}</span>;
  if (s === "failed" || s === "error")
    return <span className="badge badge-danger">✕ {status}</span>;
  return <span className="badge badge-neutral">{status}</span>;
}

function stageDuration(started: string | null, ended: string | null): string {
  if (!started) return "—";
  const start = new Date(started).getTime();
  const end = ended ? new Date(ended).getTime() : Date.now();
  const ms = end - start;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function Operations() {
  const api = useMemo(() => buildApiClient(), []);

  const [operations, setOperations] = useState<Map<string, Operation>>(new Map());
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchOperations = useCallback(async () => {
    const stored: TrackedOp[] = JSON.parse(localStorage.getItem("documind_ops") || "[]");
    // Remove entries older than 1 hour
    const cutoff = Date.now() - 3_600_000;
    const active = stored.filter((op) => op.addedAt > cutoff);
    if (active.length !== stored.length) {
      localStorage.setItem("documind_ops", JSON.stringify(active));
    }

    if (active.length === 0) {
      setLoading(false);
      return;
    }

    const results = new Map<string, Operation>();
    await Promise.allSettled(
      active.map(async (op) => {
        try {
          const data = await api.getOperation(op.id);
          results.set(op.id, data);
        } catch (err) {
          if (err instanceof ApiError) {
            // Still show it with error status
            results.set(op.id, {
              id: op.id,
              type: "unknown",
              status: "error",
              document_id: null,
              version_id: null,
              safe_error_code: err.message,
              stages: [],
            });
          }
        }
      }),
    );

    setOperations(results);
    setLoading(false);
  }, [api]);

  useEffect(() => {
    fetchOperations();
    const interval = setInterval(fetchOperations, 5000);
    return () => clearInterval(interval);
  }, [fetchOperations]);

  const ops = Array.from(operations.values());
  const activeCount = ops.filter((o) =>
    ["running", "pending", "processing"].includes(o.status.toLowerCase()),
  ).length;
  const completedCount = ops.filter((o) =>
    ["completed", "succeeded"].includes(o.status.toLowerCase()),
  ).length;
  const failedCount = ops.filter((o) =>
    ["failed", "error"].includes(o.status.toLowerCase()),
  ).length;

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Operations</h1>
        <p>Track document processing pipelines and workflow status</p>
      </div>

      <div className="page-body">
        <div className="card-grid" style={{ marginBottom: 24 }}>
          <div className="stat-card">
            <div className="stat-label">Active Workflows</div>
            <div className="stat-value" style={{ color: "var(--brand-400)" }}>
              {activeCount}
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Completed</div>
            <div className="stat-value" style={{ color: "var(--success)" }}>
              {completedCount}
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Failed</div>
            <div className="stat-value" style={{ color: "var(--danger)" }}>
              {failedCount}
            </div>
          </div>
        </div>

        <div className="card">
          {loading ? (
            <div style={{ padding: 32, textAlign: "center" }}>
              <Spinner size={32} />
              <p style={{ marginTop: 12, color: "var(--text-muted)" }}>Loading operations…</p>
            </div>
          ) : ops.length === 0 ? (
            <div className="empty-state">
              <div className="empty-icon">⚙️</div>
              <h3>No tracked operations</h3>
              <p>Upload a document to see its processing pipeline here.</p>
            </div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Operation ID</th>
                  <th>Type</th>
                  <th>Document</th>
                  <th>Status</th>
                  <th>Stages</th>
                </tr>
              </thead>
              <tbody>
                {ops.map((op) => (
                  <>
                    <tr
                      key={op.id}
                      className="expandable-trigger"
                      onClick={() => setExpandedId(expandedId === op.id ? null : op.id)}
                    >
                      <td style={{ fontFamily: "monospace", fontSize: "0.8125rem" }}>
                        {op.id.slice(0, 8)}…
                        <span style={{ marginLeft: 6, fontSize: "0.6875rem", opacity: 0.5 }}>
                          {expandedId === op.id ? "▼" : "▶"}
                        </span>
                      </td>
                      <td>{op.type}</td>
                      <td style={{ fontFamily: "monospace", fontSize: "0.8125rem" }}>
                        {op.document_id?.slice(0, 8) ?? "—"}
                      </td>
                      <td>{statusBadge(op.status)}</td>
                      <td>
                        {op.stages.length > 0
                          ? `${op.stages.filter((s) => ["completed", "succeeded"].includes(s.status.toLowerCase())).length}/${op.stages.length}`
                          : "—"}
                      </td>
                    </tr>
                    {expandedId === op.id && op.stages.length > 0 && (
                      <tr key={`${op.id}-expanded`} className="expanded-content">
                        <td colSpan={5}>
                          <div className="stage-list">
                            {op.stages.map((stage) => (
                              <div key={stage.name} className="stage-item">
                                <span className="stage-name">{stage.name}</span>
                                {statusBadge(stage.status)}
                                {stage.safe_error_code && (
                                  <span style={{ color: "var(--danger)", fontSize: "0.75rem" }}>
                                    {stage.safe_error_code}
                                  </span>
                                )}
                                <span className="stage-duration">
                                  {stageDuration(stage.started_at, stage.ended_at)}
                                </span>
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
