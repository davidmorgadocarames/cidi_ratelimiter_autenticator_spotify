import { authFetch } from "../app-state.js";
import { extractErrorMessage } from "../shared.js";

async function requestJson(url, options) {
  const response = await authFetch(url, options);
  if (response.status === 401) {
    const error = new Error("Sesión expirada");
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(extractErrorMessage(data, "No se pudo completar la operación"));
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

// "Descubrimiento semanal": catálogo real, no /songs/popular (ese depende de
// song_plays y estaría vacío en un catálogo recién sembrado) - ver plan.
export function fetchDiscoverySongs() {
  return requestJson("/songs?limit=50");
}

export function listPlaylists() {
  return requestJson("/playlists");
}

export function createPlaylist(name) {
  return requestJson("/playlists", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function getPlaylist(playlistId) {
  return requestJson(`/playlists/${playlistId}`);
}

export function addSongToPlaylist(playlistId, songId) {
  return requestJson(`/playlists/${playlistId}/songs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ song_id: songId }),
  });
}

export function removeSongFromPlaylist(playlistId, songId) {
  return requestJson(`/playlists/${playlistId}/songs/${songId}`, { method: "DELETE" });
}

export function reorderPlaylist(playlistId, songIds) {
  return requestJson(`/playlists/${playlistId}/songs/order`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ song_ids: songIds }),
  });
}

export function getStreamUrl(songId) {
  return requestJson(`/songs/${songId}/stream`);
}
