/**
 * DocuMind Console — App shell with OIDC authentication and routing.
 *
 * Wraps the application in an AuthProvider (oidc-client-ts) and
 * provides React Router routes to each page.  Unauthenticated
 * visitors see the Login page; authenticated users get the sidebar
 * shell with all navigation.
 */

import { AuthProvider, useAuth } from "react-oidc-context";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import type { WebStorageStateStore } from "oidc-client-ts";

import { Sidebar } from "./components/Sidebar";
import { ToastProvider } from "./components/Toast";
import { Login } from "./pages/Login";
import { Documents } from "./pages/Documents";
import { DocumentViewer } from "./pages/DocumentViewer";
import { Chat } from "./pages/Chat";
import { Admin } from "./pages/Admin";
import { Operations } from "./pages/Operations";

/* OIDC configuration — reads from env vars at build time.
 * In production these are set via Vite's define/env mechanism.  During
 * development the values default to empty strings so the shell renders
 * even without a real IdP.
 */
const oidcConfig = {
  authority: import.meta.env.VITE_OIDC_AUTHORITY ?? "",
  client_id: import.meta.env.VITE_OIDC_CLIENT_ID ?? "documind",
  redirect_uri: import.meta.env.VITE_OIDC_REDIRECT_URI ?? window.location.origin,
  post_logout_redirect_uri: window.location.origin,
  scope: "openid profile email groups",
  // Store tokens in sessionStorage for security.
  userStore: undefined as unknown as WebStorageStateStore,
};

function AuthenticatedShell() {
  const auth = useAuth();

  if (auth.isLoading) {
    return (
      <div className="login-page">
        <div className="login-card fade-in">
          <h1>DocuMind</h1>
          <p>Verifying identity…</p>
          <div className="skeleton" style={{ height: 44, marginTop: 24 }} />
        </div>
      </div>
    );
  }

  if (auth.error) {
    return (
      <div className="login-page">
        <div className="login-card fade-in">
          <h1>Authentication Error</h1>
          <p>{auth.error.message}</p>
          <button className="btn btn-primary" onClick={() => auth.signinRedirect()}>
            Retry Sign In
          </button>
        </div>
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return <Login />;
  }

  return (
    <div className="app-layout">
      <Sidebar user={auth.user?.profile} onLogout={() => auth.signoutRedirect()} />
      <div className="main-content">
        <Routes>
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/documents/:id" element={<DocumentViewer />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/operations" element={<Operations />} />
          <Route path="/admin" element={<Admin />} />
          <Route path="*" element={<Navigate to="/documents" replace />} />
        </Routes>
      </div>
    </div>
  );
}

export default function App() {
  // If no OIDC authority is configured, render the shell without auth
  // (development mode / placeholder).
  if (!oidcConfig.authority) {
    return (
      <ToastProvider>
        <BrowserRouter>
          <div className="app-layout">
            <Sidebar user={undefined} onLogout={() => {}} />
            <div className="main-content">
              <Routes>
                <Route path="/" element={<Navigate to="/documents" replace />} />
                <Route path="/documents" element={<Documents />} />
                <Route path="/documents/:id" element={<DocumentViewer />} />
                <Route path="/chat" element={<Chat />} />
                <Route path="/operations" element={<Operations />} />
                <Route path="/admin" element={<Admin />} />
                <Route path="*" element={<Navigate to="/documents" replace />} />
              </Routes>
            </div>
          </div>
        </BrowserRouter>
      </ToastProvider>
    );
  }

  return (
    <ToastProvider>
      <AuthProvider {...oidcConfig}>
        <BrowserRouter>
          <AuthenticatedShell />
        </BrowserRouter>
      </AuthProvider>
    </ToastProvider>
  );
}
