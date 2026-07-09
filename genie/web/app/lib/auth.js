// Session token + identity storage for Google Sign-In. The token is returned
// by the backend's /auth/google after verifying a Google ID token, and sent on
// every API call as `Authorization: Bearer <token>`. We also keep the signed-in
// email + admin flag so the UI can show who's logged in and gate the admin view.
export const TOKEN_KEY = "genie_session";
export const EMAIL_KEY = "genie_email";
export const ADMIN_KEY = "genie_is_admin";

const _ls = () => (typeof window !== "undefined" ? window.localStorage : null);

export const getToken = () => _ls()?.getItem(TOKEN_KEY) || null;
export const getEmail = () => _ls()?.getItem(EMAIL_KEY) || "";
export const getIsAdmin = () => _ls()?.getItem(ADMIN_KEY) === "1";

export const setToken = (t) => { _ls()?.setItem(TOKEN_KEY, t); };

export const setIdentity = (email, isAdmin) => {
  const ls = _ls();
  if (!ls) return;
  if (email) ls.setItem(EMAIL_KEY, email);
  ls.setItem(ADMIN_KEY, isAdmin ? "1" : "0");
};

export const clearToken = () => {
  const ls = _ls();
  if (!ls) return;
  ls.removeItem(TOKEN_KEY);
  ls.removeItem(EMAIL_KEY);
  ls.removeItem(ADMIN_KEY);
};

// Sign the user out and return to the login screen.
export const logout = () => {
  clearToken();
  if (typeof window !== "undefined") window.location.reload();
};
