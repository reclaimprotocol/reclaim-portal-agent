// Session token storage for Google Sign-In. The token is returned by the
// backend's /auth/google after verifying a Google ID token, and sent on every
// API call as `Authorization: Bearer <token>`.
export const TOKEN_KEY = "genie_session";

export const getToken = () =>
  typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) : null;

export const setToken = (t) => {
  if (typeof window !== "undefined") window.localStorage.setItem(TOKEN_KEY, t);
};

export const clearToken = () => {
  if (typeof window !== "undefined") window.localStorage.removeItem(TOKEN_KEY);
};
