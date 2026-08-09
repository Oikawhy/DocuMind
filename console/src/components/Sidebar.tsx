/**
 * Sidebar navigation with health indicator.
 */

import { useEffect, useMemo, useState } from "react";
import { NavLink } from "react-router-dom";
import { buildApiClient } from "../api";

interface SidebarProps {
  user: { name?: string; email?: string } | undefined;
  onLogout: () => void;
}

export function Sidebar({ user, onLogout }: SidebarProps) {
  const api = useMemo(() => buildApiClient(), []);
  const [healthy, setHealthy] = useState<boolean | null>(null);

  useEffect(() => {
    function check() {
      api
        .health()
        .then(() => setHealthy(true))
        .catch(() => setHealthy(false));
    }
    check();
    const interval = setInterval(check, 30_000);
    return () => clearInterval(interval);
  }, [api]);

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="logo-icon">D</div>
          DocuMind
          <span
            className={`health-dot ${healthy === true ? "healthy" : healthy === false ? "unhealthy" : ""}`}
            title={healthy === true ? "Backend connected" : healthy === false ? "Backend unreachable" : "Checking…"}
          />
        </div>
      </div>

      <nav className="sidebar-nav">
        <NavLink to="/documents" className={({ isActive }) => isActive ? "active" : ""}>
          <span className="nav-icon">📄</span>
          Documents
        </NavLink>
        <NavLink to="/chat" className={({ isActive }) => isActive ? "active" : ""}>
          <span className="nav-icon">💬</span>
          Chat
        </NavLink>
        <NavLink to="/operations" className={({ isActive }) => isActive ? "active" : ""}>
          <span className="nav-icon">⚙️</span>
          Operations
        </NavLink>
        <NavLink to="/admin" className={({ isActive }) => isActive ? "active" : ""}>
          <span className="nav-icon">🔧</span>
          Admin
        </NavLink>
      </nav>

      <div className="sidebar-footer">
        {user ? (
          <>
            <div style={{ marginBottom: 4 }}>{user.name ?? user.email ?? "User"}</div>
            <button
              className="btn btn-secondary"
              style={{ width: "100%", fontSize: "0.75rem", padding: "6px 12px" }}
              onClick={onLogout}
            >
              Sign Out
            </button>
          </>
        ) : (
          <div>Development mode</div>
        )}
      </div>
    </aside>
  );
}
