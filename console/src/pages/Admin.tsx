/**
 * Admin page — clickable cards with expandable detail panels.
 */

import { useEffect, useMemo, useState } from "react";
import { buildApiClient } from "../api";

interface Section {
  key: string;
  title: string;
  description: string;
  icon: string;
  available: boolean;
}

const sections: Section[] = [
  {
    key: "labels",
    title: "Labels & Types",
    description: "Manage document labels and declared types",
    icon: "🏷️",
    available: false,
  },
  {
    key: "roles",
    title: "Role Mappings",
    description: "Map IdP groups to roles and label access",
    icon: "👥",
    available: false,
  },
  {
    key: "chunks",
    title: "Chunk Profiles",
    description: "Configure chunking algorithms and parameters",
    icon: "📊",
    available: false,
  },
  {
    key: "templates",
    title: "Templates",
    description: "Manage extraction templates and proposals",
    icon: "📋",
    available: false,
  },
  {
    key: "models",
    title: "Model Routes",
    description: "Configure LLM routes and BYOK consent",
    icon: "🤖",
    available: false,
  },
  {
    key: "holds",
    title: "Legal Holds",
    description: "Impose and release legal holds on documents",
    icon: "⚖️",
    available: false,
  },
  {
    key: "deadletters",
    title: "Dead Letters",
    description: "View and replay failed outbox events",
    icon: "📬",
    available: false,
  },
  {
    key: "audit",
    title: "Audit Log",
    description: "Browse hash-chained audit events",
    icon: "🔒",
    available: false,
  },
];

export function Admin() {
  const api = useMemo(() => buildApiClient(), []);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [healthy, setHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    api
      .health()
      .then(() => setHealthy(true))
      .catch(() => setHealthy(false));
  }, [api]);

  return (
    <div className="fade-in">
      <div className="page-header">
        <h1>Administration</h1>
        <p>Platform configuration and compliance management</p>
      </div>

      <div className="page-body">
        {/* Health status */}
        <div className="card" style={{ marginBottom: 24, padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              className={`health-dot ${healthy === true ? "healthy" : healthy === false ? "unhealthy" : ""}`}
            />
            <span style={{ fontSize: "0.875rem", fontWeight: 500 }}>
              Backend Status:{" "}
              {healthy === null
                ? "Checking…"
                : healthy
                  ? "Connected"
                  : "Unreachable"}
            </span>
          </div>
        </div>

        <div className="card-grid">
          {sections.map((section) => (
            <div
              key={section.key}
              className="card"
              style={{
                cursor: "pointer",
                transition: "all var(--transition-fast)",
                borderColor:
                  expandedKey === section.key ? "var(--brand-500)" : undefined,
              }}
              onClick={() =>
                setExpandedKey(expandedKey === section.key ? null : section.key)
              }
            >
              <div style={{ fontSize: "1.5rem", marginBottom: 12 }}>
                {section.icon}
              </div>
              <h3
                style={{
                  fontSize: "0.9375rem",
                  fontWeight: 600,
                  marginBottom: 4,
                }}
              >
                {section.title}
              </h3>
              <p
                style={{
                  fontSize: "0.8125rem",
                  color: "var(--text-secondary)",
                }}
              >
                {section.description}
              </p>
              {!section.available && (
                <div
                  style={{
                    marginTop: 12,
                    fontSize: "0.6875rem",
                    color: "var(--text-muted)",
                    fontStyle: "italic",
                  }}
                >
                  Available after Task 10 — Administration API
                </div>
              )}
            </div>
          ))}
        </div>

        {expandedKey && (
          <div className="card fade-in" style={{ marginTop: 24 }}>
            <h3
              style={{
                fontSize: "1rem",
                fontWeight: 600,
                marginBottom: 12,
              }}
            >
              {sections.find((s) => s.key === expandedKey)?.icon}{" "}
              {sections.find((s) => s.key === expandedKey)?.title}
            </h3>
            <div
              style={{
                padding: 32,
                textAlign: "center",
                color: "var(--text-muted)",
              }}
            >
              <p style={{ fontSize: "0.875rem" }}>
                This section will be fully functional after the{" "}
                <strong>Administration API</strong> (Task 10) is implemented.
              </p>
              <p
                style={{
                  fontSize: "0.8125rem",
                  marginTop: 8,
                  color: "var(--text-muted)",
                }}
              >
                The backend endpoints{" "}
                <code style={{ color: "var(--brand-400)" }}>
                  /v1/admin/{expandedKey}
                </code>{" "}
                are not yet available.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
