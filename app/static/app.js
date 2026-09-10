import { appState, authFetch } from "./js/app-state.js";
import { initScreen1 } from "./js/screen1-auth.js";
import { initScreen2 } from "./js/screen2-gate.js";
import { initScreen3 } from "./js/screen3/index.js";

const loadingView = document.getElementById("loading-view");
const authView = document.getElementById("auth-view");
const gateView = document.getElementById("dashboard-view");
const appView = document.getElementById("app-view");

const backToGateBtn = document.getElementById("back-to-gate");
const appLogoutBtn = document.getElementById("app-logout");

// "gate" (Pantalla 2: premium+seguridad) y "app" (Pantalla 3: la app de
// música) son dos pantallas distintas post-login - showView() ya no es
// binario "dashboard sí/no" como antes de esta pieza.
function showView(view) {
  loadingView.hidden = view !== "loading";
  authView.hidden = view !== "auth";
  gateView.hidden = view !== "gate";
  appView.hidden = view !== "app";
  if (view !== "app") {
    screen3.leave();
  }
}

async function fetchMe() {
  const response = await authFetch("/auth/me");
  if (!response.ok) throw new Error("No se pudo obtener el usuario actual");
  return response.json();
}

function handleSessionExpired() {
  appState.accessToken = null;
  appState.currentUser = null;
  screen1.reset();
  showView("auth");
}

async function logout() {
  try {
    await fetch("/auth/logout", { method: "POST" });
  } catch {
    // Best-effort: aunque falle la llamada de red, seguimos cerrando la
    // sesión en el cliente (el refresh token en el servidor expira solo).
  }
  handleSessionExpired();
}

// Único sitio que decide Pantalla 2 vs Pantalla 3 - se llama tras login,
// refresh silencioso, confirmar TOTP, y activar/desactivar premium (los
// cuatro momentos en los que is_premium/totp_enabled pueden haber cambiado).
function routeAfterAuth(user) {
  appState.currentUser = user;
  screen2.render(user);
  if (user.is_premium && user.totp_enabled) {
    showView("app");
    screen3.enter(user);
  } else {
    showView("gate");
  }
}

async function refreshUserAndRoute() {
  try {
    const user = await fetchMe();
    routeAfterAuth(user);
  } catch {
    handleSessionExpired();
  }
}

async function trySilentRefresh() {
  try {
    const response = await fetch("/auth/refresh", { method: "POST" });
    if (!response.ok) {
      showView("auth");
      return;
    }
    const data = await response.json();
    appState.accessToken = data.access_token;
    await refreshUserAndRoute();
  } catch {
    showView("auth");
  }
}

const screen1 = initScreen1({ fetchMe, onAuthenticated: routeAfterAuth });
const screen2 = initScreen2({ onUserUpdated: refreshUserAndRoute, logout });
// onGateLost reutiliza refreshUserAndRoute (re-fetch + reevaluar el gate) -
// cubre tanto un 403 real de /playlists/* (premium/2FA revocado en otra
// pestaña mientras el usuario seguía en la Pantalla 3) como el botón
// "Volver a la app" de la Pantalla 2 (mismo flujo, sentido inverso).
const screen3 = initScreen3({ handleSessionExpired, onGateLost: refreshUserAndRoute });

backToGateBtn.addEventListener("click", () => {
  showView("gate");
});
appLogoutBtn.addEventListener("click", () => logout());

trySilentRefresh();
