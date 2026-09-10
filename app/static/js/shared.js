export const NETWORK_ERROR_MESSAGE = "Error de red. Inténtalo de nuevo.";

export function showError(el, message) {
  el.textContent = message;
  el.hidden = false;
}

export function clearError(el) {
  el.textContent = "";
  el.hidden = true;
}

// FastAPI/Pydantic devuelve `detail` como string en errores "de negocio"
// (HTTPException) pero como una LISTA de objetos {loc, msg, type} en errores
// 422 de validación de body. Sin esto, `data.detail || fallback` asigna el
// array a textContent y el usuario ve literalmente "[object Object]".
export function extractErrorMessage(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string" && detail.length > 0) return detail;
  if (Array.isArray(detail) && detail.length > 0 && typeof detail[0]?.msg === "string") {
    return detail[0].msg;
  }
  return fallback;
}

// button.dataset.loading marca explícitamente "este botón sigue mostrando su
// label de carga"; si algo ya puso el label definitivo durante fn(), lo
// borra para que el finally de abajo no lo pise de vuelta.
export async function withLoading(button, labelWhileLoading, fn) {
  const originalLabel = button.textContent;
  button.disabled = true;
  if (labelWhileLoading) {
    button.textContent = labelWhileLoading;
    button.dataset.loading = "true";
  }
  try {
    await fn();
  } finally {
    button.disabled = false;
    if (button.dataset.loading === "true") {
      button.textContent = originalLabel;
    }
    delete button.dataset.loading;
  }
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const secs = String(total % 60).padStart(2, "0");
  return `${minutes}:${secs}`;
}

// Hash FNV-1a determinista sobre un id de canción -> matiz 0-360. Song no
// tiene campo de imagen - misma canción produce siempre el mismo "gradiente
// de portada", estable entre renders/recargas, sin depender de imágenes
// externas.
export function hueForSongId(songId) {
  let hash = 0x811c9dc5;
  const str = String(songId);
  for (let i = 0; i < str.length; i++) {
    hash ^= str.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return Math.abs(hash) % 360;
}

export function applyCoverGradient(el, songId) {
  const hue = hueForSongId(songId);
  const hue2 = (hue + 40) % 360;
  el.style.background = `linear-gradient(135deg, hsl(${hue} 70% 55%), hsl(${hue2} 70% 40%))`;
}
