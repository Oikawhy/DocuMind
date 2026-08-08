/**
 * Login page — OIDC browser redirect.
 */

import { useAuth } from "react-oidc-context";

export function Login() {
  const auth = useAuth();

  return (
    <div className="login-page">
      <div className="login-card fade-in">
        <div className="sidebar-logo" style={{ justifyContent: "center", marginBottom: 24 }}>
          <div className="logo-icon" style={{ width: 40, height: 40, fontSize: "1rem" }}>D</div>
        </div>
        <h1>Welcome to DocuMind</h1>
        <p>Self-hosted document intelligence platform.<br />Sign in with your organization's identity provider.</p>
        <button className="btn btn-primary" onClick={() => auth.signinRedirect()}>
          Sign In with SSO
        </button>
      </div>
    </div>
  );
}
