/**
 * React hook that provides an API client with the current OIDC access token.
 *
 * In production (OIDC configured), reads the access_token from useAuth().
 * In dev mode (no OIDC), falls back to VITE_DEV_TOKEN.
 */

import { useMemo } from "react";
import { useAuth } from "react-oidc-context";
import { buildApiClient, ApiClient } from "../api";

/**
 * Returns an ApiClient wired with the current user's access token.
 * Falls back to VITE_DEV_TOKEN in dev mode (no OIDC).
 */
export function useApi(): ApiClient {
  const oidcAuthority = import.meta.env.VITE_OIDC_AUTHORITY;

  if (!oidcAuthority) {
    // Dev mode: no OIDC, use dev token.
    // eslint-disable-next-line react-hooks/rules-of-hooks
    return useMemo(() => buildApiClient(), []);
  }

  // eslint-disable-next-line react-hooks/rules-of-hooks
  const auth = useAuth();
  const token = auth.user?.access_token;
  // eslint-disable-next-line react-hooks/rules-of-hooks
  return useMemo(() => buildApiClient(token ?? undefined), [token]);
}
