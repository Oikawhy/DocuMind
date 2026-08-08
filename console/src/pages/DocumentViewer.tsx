/**
 * Document viewer — detail page for a single document.
 */

import { useParams } from "react-router-dom";

export function DocumentViewer() {
  const { id } = useParams<{ id: string }>();

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Document Viewer</h1>
        <p>Document ID: {id}</p>
      </div>

      <div className="page-body">
        <div className="card-grid">
          <div className="stat-card">
            <div className="stat-label">Status</div>
            <div style={{ marginTop: 8 }}>
              <span className="badge badge-success">● Completed</span>
            </div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Versions</div>
            <div className="stat-value">—</div>
          </div>
          <div className="stat-card">
            <div className="stat-label">Chunks</div>
            <div className="stat-value">—</div>
          </div>
        </div>

        <div className="card" style={{ marginTop: 24 }}>
          <div className="empty-state">
            <div className="empty-icon">🔍</div>
            <h3>Document content will appear here</h3>
            <p>View extracted text, structured data, and version history.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
