/**
 * UploadModal — modal dialog for uploading a new document.
 *
 * Uses FileDropZone for file selection, form fields for metadata,
 * and calls the API client to submit.
 */

import { useState } from "react";
import { FileDropZone } from "./FileDropZone";
import { Spinner } from "./Spinner";
import type { ApiClient } from "../api";
import { ApiError } from "../api";
import type { AdmissionResponse } from "../types";

interface UploadModalProps {
  api: ApiClient;
  onClose: () => void;
  onSuccess: (result: AdmissionResponse) => void;
}

export function UploadModal({ api, onClose, onSuccess }: UploadModalProps) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [declaredType, setDeclaredType] = useState("general");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file || !title.trim()) return;

    setUploading(true);
    setError(null);

    try {
      const idempotencyKey = crypto.randomUUID();
      const result = await api.uploadDocument(file, title.trim(), declaredType, idempotencyKey);
      onSuccess(result);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`${err.code}: ${err.message}`);
      } else {
        setError("Upload failed. Please try again.");
      }
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content fade-in" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Upload Document</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <FileDropZone onFiles={(files) => setFile(files[0])} />

          <div className="form-group">
            <label htmlFor="upload-title">Title</label>
            <input
              id="upload-title"
              type="text"
              className="form-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Document title"
              required
            />
          </div>

          <div className="form-group">
            <label htmlFor="upload-type">Declared Type</label>
            <select
              id="upload-type"
              className="form-input"
              value={declaredType}
              onChange={(e) => setDeclaredType(e.target.value)}
            >
              <option value="general">General</option>
              <option value="financial_report">Financial Report</option>
              <option value="invoice">Invoice</option>
              <option value="receipt">Receipt</option>
              <option value="spreadsheet">Spreadsheet</option>
              <option value="log">Log</option>
              <option value="contract">Contract</option>
            </select>
          </div>

          {error && (
            <div className="form-error">{error}</div>
          )}

          <div className="modal-actions">
            <button type="button" className="btn btn-secondary" onClick={onClose} disabled={uploading}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!file || !title.trim() || uploading}
            >
              {uploading ? <><Spinner size={16} /> Uploading…</> : "Upload"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
