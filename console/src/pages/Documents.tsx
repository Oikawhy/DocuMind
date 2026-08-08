/**
 * Documents page — document list with upload capability.
 */

import { useState } from "react";

export function Documents() {
  const [uploading, setUploading] = useState(false);

  return (
    <div className="fade-in">
      <div className="page-header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <h1>Documents</h1>
            <p>Manage and search your document library</p>
          </div>
          <button
            className="btn btn-primary"
            onClick={() => setUploading(!uploading)}
          >
            📤 Upload Document
          </button>
        </div>
      </div>

      <div className="page-body">
        {uploading && (
          <div className="card fade-in" style={{ marginBottom: 24 }}>
            <h3 style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: 16 }}>Upload Document</h3>
            <div
              style={{
                border: "2px dashed var(--surface-border)",
                borderRadius: "var(--radius-lg)",
                padding: 48,
                textAlign: "center",
                color: "var(--text-secondary)",
              }}
            >
              <p>Drag and drop files here, or click to browse</p>
              <p style={{ fontSize: "0.75rem", marginTop: 8, color: "var(--text-muted)" }}>
                Supported: PDF, DOCX, XLSX, PPTX, images (max 500 MB)
              </p>
            </div>
          </div>
        )}

        <div className="card">
          <div className="empty-state">
            <div className="empty-icon">📁</div>
            <h3>No documents yet</h3>
            <p>Upload your first document to get started with intelligent analysis.</p>
          </div>
        </div>
      </div>
    </div>
  );
}
