/**
 * Sidebar navigation component.
 */

import { NavLink } from "react-router-dom";

interface SidebarProps {
  user: { name?: string; email?: string } | undefined;
  onLogout: () => void;
}

export function Sidebar({ user, onLogout }: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <div className="logo-icon">D</div>
          DocuMind
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
