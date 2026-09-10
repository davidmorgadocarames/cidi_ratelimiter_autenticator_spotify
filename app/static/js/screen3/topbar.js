import { withLoading, showError, clearError } from "../shared.js";

// deps: { onSelectPlaylist(id), onCreatePlaylist(name) -> Promise }
export function initTopBar({ onSelectPlaylist, onCreatePlaylist }) {
  const selector = document.getElementById("playlist-selector");
  const newPlaylistButton = document.getElementById("new-playlist-button");

  const modal = document.getElementById("create-playlist-modal");
  const nameInput = document.getElementById("new-playlist-name");
  const errorEl = document.getElementById("create-playlist-error");
  const confirmButton = document.getElementById("create-playlist-confirm");
  const cancelButton = document.getElementById("create-playlist-cancel");

  // El HTML ya declara role="dialog" aria-modal="true" - sin este manejo de
  // teclado/foco esa semántica era falsa (hallazgo real de la revisión
  // "abogado del diablo"): Escape debe cerrar, Tab no debe poder salir del
  // modal hacia el fondo, y el foco debe volver al botón que lo abrió.
  const focusableSelector = "input, button:not(:disabled)";

  function handleKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeModal();
      return;
    }
    if (event.key === "Enter" && event.target === nameInput) {
      event.preventDefault();
      confirmButton.click();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(modal.querySelectorAll(focusableSelector));
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openModal() {
    clearError(errorEl);
    nameInput.value = "";
    modal.hidden = false;
    nameInput.focus();
    document.addEventListener("keydown", handleKeydown);
  }

  function closeModal() {
    modal.hidden = true;
    document.removeEventListener("keydown", handleKeydown);
    newPlaylistButton.focus();
  }

  selector.addEventListener("change", () => {
    onSelectPlaylist(selector.value);
  });

  newPlaylistButton.addEventListener("click", openModal);
  cancelButton.addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });

  confirmButton.addEventListener("click", async () => {
    clearError(errorEl);
    await withLoading(confirmButton, "Creando…", async () => {
      try {
        await onCreatePlaylist(nameInput.value);
        closeModal();
      } catch (err) {
        showError(errorEl, err.message || "No se pudo crear la playlist");
      }
    });
  });

  return {
    // Nunca innerHTML con el nombre de la playlist - texto libre introducido
    // por el propio usuario, se renderiza con textContent en cada <option>.
    renderPlaylistOptions(playlists, selectedId) {
      selector.textContent = "";
      for (const playlist of playlists) {
        const option = document.createElement("option");
        option.value = String(playlist.id);
        option.textContent = `${playlist.name} (${playlist.song_count})`;
        if (String(playlist.id) === String(selectedId)) option.selected = true;
        selector.appendChild(option);
      }
    },
  };
}
