/**
 * FileDropZone — drag-and-drop + click-to-browse file input.
 *
 * Visual feedback on drag-over, MIME filtering, size validation.
 */

import { useCallback, useRef, useState } from "react";

interface FileDropZoneProps {
  onFiles: (files: File[]) => void;
  accept?: string;
  maxSizeBytes?: number;
  multiple?: boolean;
}

export function FileDropZone({
  onFiles,
  accept = ".pdf,.xlsx,.txt,.docx,.pptx,.xls,.csv",
  maxSizeBytes = 524_288_000,
  multiple = false,
}: FileDropZoneProps) {
  const [dragActive, setDragActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const validate = useCallback(
    (files: FileList | File[]) => {
      const arr = Array.from(files);
      for (const f of arr) {
        if (f.size > maxSizeBytes) {
          setError(`"${f.name}" exceeds ${(maxSizeBytes / 1_048_576).toFixed(0)} MB limit`);
          return null;
        }
      }
      setError(null);
      return arr;
    },
    [maxSizeBytes],
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDragActive(false);
      const valid = validate(e.dataTransfer.files);
      if (valid && valid.length > 0) {
        setSelectedName(valid.map((f) => f.name).join(", "));
        onFiles(valid);
      }
    },
    [onFiles, validate],
  );

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      if (!e.target.files) return;
      const valid = validate(e.target.files);
      if (valid && valid.length > 0) {
        setSelectedName(valid.map((f) => f.name).join(", "));
        onFiles(valid);
      }
    },
    [onFiles, validate],
  );

  return (
    <div
      className={`drop-zone ${dragActive ? "active" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={() => setDragActive(false)}
      onDrop={handleDrop}
      onClick={() => inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        multiple={multiple}
        onChange={handleChange}
        style={{ display: "none" }}
      />
      {selectedName ? (
        <>
          <p style={{ fontWeight: 600, color: "var(--brand-400)" }}>📎 {selectedName}</p>
          <p style={{ fontSize: "0.75rem", marginTop: 4, color: "var(--text-muted)" }}>
            Click or drop to replace
          </p>
        </>
      ) : (
        <>
          <p>📂 Drag and drop files here, or click to browse</p>
          <p style={{ fontSize: "0.75rem", marginTop: 8, color: "var(--text-muted)" }}>
            Supported: PDF, DOCX, XLSX, TXT, PPTX (max {(maxSizeBytes / 1_048_576).toFixed(0)} MB)
          </p>
        </>
      )}
      {error && (
        <p style={{ color: "var(--danger)", fontSize: "0.8125rem", marginTop: 8 }}>{error}</p>
      )}
    </div>
  );
}
