// Estado compartido entre módulos - objeto mutable exportado (no un `let`
// reexportado) para que cualquier módulo pueda leer/escribir sus campos sin
// pasar por un setter, mismo estilo imperativo que ya usaba app.js antes del
// split en módulos.
export const appState = { accessToken: null, currentUser: null };

export function authFetch(url, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${appState.accessToken}` };
  return fetch(url, { ...options, headers });
}
