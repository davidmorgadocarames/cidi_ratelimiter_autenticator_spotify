import { appState } from "./app-state.js";
import {
  NETWORK_ERROR_MESSAGE,
  withLoading,
  extractErrorMessage,
  showError,
  clearError,
} from "./shared.js";

// deps: { fetchMe, onAuthenticated } - onAuthenticated(user) es la única
// forma en que este módulo le dice a app.js "ya hay sesión, decide a qué
// pantalla ir" (routeAfterAuth vive en app.js, no aquí).
export function initScreen1({ fetchMe, onAuthenticated }) {
  const loginSection = document.getElementById("login-section");
  const registerSection = document.getElementById("register-section");

  const loginForm = document.getElementById("login-form");
  const loginEmail = document.getElementById("login-email");
  const loginPassword = document.getElementById("login-password");
  const loginError = document.getElementById("login-error");
  const loginSubmit = document.getElementById("login-submit");

  const registerForm = document.getElementById("register-form");
  const registerEmail = document.getElementById("register-email");
  const registerPassword = document.getElementById("register-password");
  const registerError = document.getElementById("register-error");
  const registerSubmit = document.getElementById("register-submit");

  const showRegisterBtn = document.getElementById("show-register");
  const showLoginBtn = document.getElementById("show-login");

  const resendVerificationBtn = document.getElementById("resend-verification");
  const resendVerificationHint = document.getElementById("resend-verification-hint");

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError(loginError);
    await withLoading(loginSubmit, "Iniciando sesión…", async () => {
      try {
        const body = new URLSearchParams();
        body.set("username", loginEmail.value);
        body.set("password", loginPassword.value);
        const response = await fetch("/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(loginError, extractErrorMessage(data, "No se pudo iniciar sesión"));
          loginPassword.focus();
          return;
        }
        const data = await response.json();
        appState.accessToken = data.access_token;
        const user = await fetchMe();
        onAuthenticated(user);
      } catch {
        showError(loginError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError(registerError);
    await withLoading(registerSubmit, "Creando cuenta…", async () => {
      try {
        const response = await fetch("/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: registerEmail.value, password: registerPassword.value }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(registerError, extractErrorMessage(data, "No se pudo crear la cuenta"));
          registerEmail.focus();
          return;
        }

        // Sin auto-login: la cuenta nace con email_verified=false, un POST
        // /auth/login aquí fallaría con 403 el 100% de las veces por
        // construcción, además de desperdiciar un token del bucket
        // "sensitive" de login. Se cambia directo a login con
        // email+contraseña precargados y un aviso.
        loginEmail.value = registerEmail.value;
        loginPassword.value = registerPassword.value;
        registerSection.hidden = true;
        loginSection.hidden = false;
        showError(
          loginError,
          "Cuenta creada. Revisa tu correo para verificar tu email antes de iniciar sesión."
        );
      } catch {
        showError(registerError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  showRegisterBtn.addEventListener("click", () => {
    clearError(loginError);
    registerEmail.value = loginEmail.value;
    loginSection.hidden = true;
    registerSection.hidden = false;
    registerEmail.focus();
  });

  showLoginBtn.addEventListener("click", () => {
    clearError(registerError);
    loginEmail.value = registerEmail.value;
    registerSection.hidden = true;
    loginSection.hidden = false;
    loginEmail.focus();
  });

  resendVerificationBtn.addEventListener("click", async () => {
    // Solo una de las dos secciones está visible a la vez - mirar el
    // atributo "hidden" es la regla sin ambigüedad para decidir de cuál
    // tomar el email.
    const email = loginSection.hidden ? registerEmail.value : loginEmail.value;
    resendVerificationHint.hidden = true;
    if (!email) {
      resendVerificationHint.textContent = "Escribe tu email arriba primero.";
      resendVerificationHint.hidden = false;
      return;
    }
    await withLoading(resendVerificationBtn, "Enviando…", async () => {
      try {
        const response = await fetch("/auth/resend-verification", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        const data = await response.json().catch(() => ({}));
        resendVerificationHint.textContent = extractErrorMessage(
          data,
          "No se pudo reenviar el email."
        );
        resendVerificationHint.hidden = false;
      } catch {
        resendVerificationHint.textContent = NETWORK_ERROR_MESSAGE;
        resendVerificationHint.hidden = false;
      }
    });
  });

  return {
    reset() {
      loginForm.reset();
      registerForm.reset();
      clearError(loginError);
      clearError(registerError);
      resendVerificationHint.hidden = true;
    },
  };
}
