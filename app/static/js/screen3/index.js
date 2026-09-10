import {
  fetchDiscoverySongs,
  listPlaylists,
  createPlaylist,
  getPlaylist,
  addSongToPlaylist,
  removeSongFromPlaylist,
  reorderPlaylist,
} from "./api.js";
import { renderDiscoveryList, updateDiscoveryNowPlaying } from "./discovery.js";
import { renderPlaylistPanel, updatePlaylistNowPlaying } from "./playlist-panel.js";
import { initTopBar } from "./topbar.js";
import { initPlayer } from "./player.js";
import { screen3State } from "./state.js";
import { showError, clearError } from "../shared.js";

// deps: { handleSessionExpired, onGateLost } - ambos vienen de app.js.
// handleSessionExpired limpia appState y vuelve a la Pantalla 1 (401, token
// inválido/expirado). onGateLost reevalúa is_premium/totp_enabled y enruta
// a la Pantalla 2 si ya no se cumplen (403 del gate server-side de
// /playlists/* - puede pasar si otra pestaña desactiva premium mientras
// esta sigue en la Pantalla 3).
export function initScreen3({ handleSessionExpired, onGateLost }) {
  const discoveryList = document.getElementById("discovery-list");
  const playlistPanelSection = document.getElementById("playlist-panel");
  const playlistSongsList = document.getElementById("playlist-songs");
  const playlistEmptyState = document.getElementById("playlist-empty-state");
  const playlistPanelTitle = document.getElementById("playlist-panel-title");
  const playlistPanelMeta = document.getElementById("playlist-panel-meta");
  const appError = document.getElementById("app-error");

  // Guarda que los listeners de DOM (topbar, drop-target del panel) solo se
  // adjunten una vez, aunque routeAfterAuth entre a la Pantalla 3 varias
  // veces en la misma sesión - pero /playlists SÍ se re-fetch en cada
  // entrada, por si pasó tiempo real entre medias.
  let initialized = false;

  // Descarta respuestas de mutación (add/remove/reorder) que lleguen fuera
  // de orden - mismo patrón que playRequestId en player.js. Sin esto, dos
  // clics rápidos en "+" sobre canciones distintas pueden hacer que la
  // respuesta del PRIMERO (que commiteó antes en el servidor pero tarda más
  // en volver) llegue DESPUÉS y sobrescriba selectedPlaylistDetail con un
  // estado más viejo - la DB queda correcta (el servidor serializa con
  // FOR UPDATE), pero el DOM mostraría una canción de menos hasta la
  // siguiente mutación o re-entrada (hallazgo real de la revisión
  // backend/devops sobre la implementación).
  let mutationSequence = 0;

  function onSessionExpired() {
    player.stop();
    handleSessionExpired();
  }

  const player = initPlayer({
    // Solo actualiza clases/iconos de "reproduciendo ahora" sobre el DOM ya
    // existente, NO un renderAll() completo - hallazgo real de la revisión
    // "abogado del diablo": renderAll() en cada play/pause reconstruía las
    // dos listas enteras, re-disparando la animación de entrada de CADA fila
    // y re-anunciando la lista completa por los aria-live en cada pausa,
    // no solo cuando cambian los datos.
    onStateChange: () => {
      updateDiscoveryNowPlaying(discoveryList, screen3State.currentSongId, screen3State.isPlaying);
      updatePlaylistNowPlaying(
        playlistSongsList,
        screen3State.currentSongId,
        screen3State.isPlaying
      );
    },
    onSessionExpired,
    onGateLost,
  });

  function syncPlaylistSummaryFromDetail(detail) {
    const index = screen3State.playlists.findIndex(
      (p) => String(p.id) === String(detail.id)
    );
    const summary = {
      id: detail.id,
      name: detail.name,
      created_at: detail.created_at,
      song_count: detail.song_count,
      total_duration_seconds: detail.total_duration_seconds,
    };
    if (index === -1) {
      screen3State.playlists.push(summary);
    } else {
      screen3State.playlists[index] = summary;
    }
  }

  function flashExistingRow(songId) {
    const row = playlistSongsList.querySelector(`[data-song-id="${songId}"]`);
    if (!row) return;
    row.classList.add("shake");
    setTimeout(() => row.classList.remove("shake"), 400);
  }

  function isSongInSelectedPlaylist(songId) {
    const detail = screen3State.selectedPlaylistDetail;
    return !!detail && detail.songs.some((item) => String(item.song.id) === String(songId));
  }

  function playlistQueue() {
    // La cola es un snapshot tomado al pulsar "reproducir" (ver
    // player.js/state.js) - si el usuario reordena o quita canciones de la
    // playlist MIENTRAS algo suena, la cola en curso no se resincroniza
    // sola (decisión explícita del plan: cambiar de playlist/recargar
    // Descubrimiento no debe alterar una reproducción ya en marcha). Puede
    // hacer que prev/next avancen sobre un orden ya desactualizado frente a
    // lo que se ve en pantalla - comportamiento predecible pero confuso,
    // aceptado (hallazgo de la revisión "abogado del diablo").
    const detail = screen3State.selectedPlaylistDetail;
    return detail ? detail.songs.map((item) => item.song) : [];
  }

  function renderAll() {
    renderDiscoveryList(discoveryList, screen3State.discoverySongs, {
      onAdd: handleAddSong,
      onPlay: (song) => {
        const index = screen3State.discoverySongs.findIndex(
          (s) => String(s.id) === String(song.id)
        );
        if (index !== -1) player.playSong(screen3State.discoverySongs, index);
      },
      currentSongId: screen3State.currentSongId,
      isPlaying: screen3State.isPlaying,
    });

    const detail = screen3State.selectedPlaylistDetail;
    playlistPanelTitle.textContent = detail ? detail.name : "Tu lista";
    playlistPanelMeta.textContent = detail ? `${detail.song_count} canciones` : "";
    playlistEmptyState.textContent = screen3State.selectedPlaylistId
      ? "Arrastra canciones aquí para crear tu playlist"
      : "Crea o selecciona una playlist arriba para empezar";

    renderPlaylistPanel(playlistSongsList, playlistEmptyState, detail, {
      onPlay: (song) => {
        const queue = playlistQueue();
        const index = queue.findIndex((s) => String(s.id) === String(song.id));
        if (index !== -1) player.playSong(queue, index);
      },
      onRemove: handleRemoveSong,
      onMoveUp: (index) => handleMove(index, index - 1),
      onMoveDown: (index) => handleMove(index, index + 1),
      onReorderDrop: handleReorderDrop,
      currentSongId: screen3State.currentSongId,
      isPlaying: screen3State.isPlaying,
    });

    topBar.renderPlaylistOptions(
      screen3State.playlists,
      screen3State.selectedPlaylistId
    );
  }

  async function handleAddSong(song) {
    clearError(appError);
    if (!screen3State.selectedPlaylistId) {
      showError(appError, "Crea o selecciona una playlist primero.");
      return;
    }
    const sequence = ++mutationSequence;
    try {
      const detail = await addSongToPlaylist(screen3State.selectedPlaylistId, song.id);
      if (sequence !== mutationSequence) return;
      screen3State.selectedPlaylistDetail = detail;
      syncPlaylistSummaryFromDetail(detail);
      renderAll();
    } catch (err) {
      if (sequence !== mutationSequence) return;
      if (err.status === 401) return onSessionExpired();
      if (err.status === 403) return onGateLost();
      // 409 puede significar tres cosas distintas (duplicado, canción no
      // "ready", límite de 500 alcanzado) - solo la primera es "esperable"
      // y tiene su propio feedback visual (shake). Las otras dos deben
      // mostrarse como error real, no tratarse silenciosamente como si la
      // canción ya estuviera en la lista (hallazgo real de la revisión
      // "abogado del diablo": antes CUALQUIER 409 se trataba como
      // duplicado, y si la fila no existía el fallo quedaba mudo).
      if (err.status === 409 && isSongInSelectedPlaylist(song.id)) {
        flashExistingRow(song.id);
        return;
      }
      showError(appError, err.message);
    }
  }

  async function handleRemoveSong(song) {
    clearError(appError);
    const sequence = ++mutationSequence;
    try {
      const detail = await removeSongFromPlaylist(
        screen3State.selectedPlaylistId,
        song.id
      );
      if (sequence !== mutationSequence) return;
      screen3State.selectedPlaylistDetail = detail;
      syncPlaylistSummaryFromDetail(detail);
      renderAll();
    } catch (err) {
      if (sequence !== mutationSequence) return;
      if (err.status === 401) return onSessionExpired();
      if (err.status === 403) return onGateLost();
      showError(appError, err.message);
    }
  }

  async function submitReorder(newOrderIds) {
    clearError(appError);
    const sequence = ++mutationSequence;
    try {
      const detail = await reorderPlaylist(screen3State.selectedPlaylistId, newOrderIds);
      if (sequence !== mutationSequence) return;
      screen3State.selectedPlaylistDetail = detail;
      syncPlaylistSummaryFromDetail(detail);
      renderAll();
    } catch (err) {
      if (sequence !== mutationSequence) return;
      if (err.status === 401) return onSessionExpired();
      if (err.status === 403) return onGateLost();
      // El DOM no se tocó de forma optimista antes de esta respuesta (el
      // reorder por drag/botones solo actualiza tras confirmar el
      // servidor), así que no hay nada que "deshacer" aquí - solo mostrar
      // el error, el estado ya mostrado sigue siendo válido.
      showError(appError, err.message);
    }
  }

  function handleMove(fromIndex, toIndex) {
    const detail = screen3State.selectedPlaylistDetail;
    if (!detail || toIndex < 0 || toIndex >= detail.songs.length) return;
    const ids = detail.songs.map((item) => item.song.id);
    const [moved] = ids.splice(fromIndex, 1);
    ids.splice(toIndex, 0, moved);
    submitReorder(ids);
  }

  function handleReorderDrop(draggedSongId, targetSongId, insertBefore) {
    const detail = screen3State.selectedPlaylistDetail;
    if (!detail) return;
    const ids = detail.songs.map((item) => String(item.song.id));
    const fromIndex = ids.indexOf(String(draggedSongId));
    if (fromIndex === -1) return;
    ids.splice(fromIndex, 1);
    let targetIndex = ids.indexOf(String(targetSongId));
    if (targetIndex === -1) return;
    if (!insertBefore) targetIndex += 1;
    ids.splice(targetIndex, 0, String(draggedSongId));
    submitReorder(ids.map(Number));
  }

  async function selectPlaylist(playlistId) {
    if (!playlistId) {
      screen3State.selectedPlaylistId = null;
      screen3State.selectedPlaylistDetail = null;
      renderAll();
      return;
    }
    try {
      const detail = await getPlaylist(playlistId);
      screen3State.selectedPlaylistId = String(playlistId);
      screen3State.selectedPlaylistDetail = detail;
      renderAll();
    } catch (err) {
      if (err.status === 401) return onSessionExpired();
      if (err.status === 403) return onGateLost();
      showError(appError, err.message);
    }
  }

  async function handleCreatePlaylist(name) {
    const created = await createPlaylist(name); // deja que topbar.js muestre el error si falla
    screen3State.playlists.push(created);
    await selectPlaylist(created.id);
  }

  const topBar = initTopBar({
    onSelectPlaylist: selectPlaylist,
    onCreatePlaylist: handleCreatePlaylist,
  });

  function attachDropTargetOnce() {
    if (initialized) return;
    initialized = true;

    playlistPanelSection.addEventListener("dragover", (event) => {
      if (!event.dataTransfer.types.includes("application/x-song-id")) return;
      event.preventDefault();
      playlistPanelSection.classList.add("drop-target-active");
    });
    playlistPanelSection.addEventListener("dragleave", (event) => {
      if (!playlistPanelSection.contains(event.relatedTarget)) {
        playlistPanelSection.classList.remove("drop-target-active");
      }
    });
    playlistPanelSection.addEventListener("drop", (event) => {
      if (!event.dataTransfer.types.includes("application/x-song-id")) return;
      event.preventDefault();
      playlistPanelSection.classList.remove("drop-target-active");
      const songId = Number(event.dataTransfer.getData("application/x-song-id"));
      const song = screen3State.discoverySongs.find((s) => s.id === songId);
      if (song) handleAddSong(song);
    });
  }

  return {
    async enter() {
      attachDropTargetOnce();
      clearError(appError);
      try {
        const [songs, playlists] = await Promise.all([
          fetchDiscoverySongs(),
          listPlaylists(),
        ]);
        screen3State.discoverySongs = songs;
        screen3State.playlists = playlists;

        const stillExists = playlists.some(
          (p) => String(p.id) === String(screen3State.selectedPlaylistId)
        );
        if (!stillExists) {
          screen3State.selectedPlaylistId = playlists.length
            ? String(playlists[0].id)
            : null;
        }
        if (screen3State.selectedPlaylistId) {
          screen3State.selectedPlaylistDetail = await getPlaylist(
            screen3State.selectedPlaylistId
          );
        } else {
          screen3State.selectedPlaylistDetail = null;
        }
        renderAll();
      } catch (err) {
        if (err.status === 401) return onSessionExpired();
        if (err.status === 403) return onGateLost();
        showError(appError, err.message || "No se pudo cargar la app de música");
      }
    },
    leave() {
      player.pause();
    },
  };
}
