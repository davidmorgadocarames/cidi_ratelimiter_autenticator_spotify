import { formatDuration, applyCoverGradient } from "../shared.js";

// Nunca innerHTML/interpolación de string con title/artist - son datos de
// otros usuarios sin sanitizar (ver app/api/songs.py upload_song). Todo el
// DOM se construye con createElement + textContent, igual que ya hacía
// renderSearchResults antes de esta pieza.
function buildSongCard(song, { onAdd, onPlay, currentSongId, isPlaying }) {
  const li = document.createElement("li");
  li.className = "song-card";
  li.draggable = true;
  li.dataset.songId = String(song.id);

  const cover = document.createElement("div");
  cover.className = "cover-art";
  applyCoverGradient(cover, song.id);

  const info = document.createElement("div");
  info.className = "song-info";
  const title = document.createElement("p");
  title.className = "song-title";
  title.textContent = song.title;
  const artist = document.createElement("p");
  artist.className = "song-artist hint";
  artist.textContent = song.artist;
  info.append(title, artist);

  const duration = document.createElement("span");
  duration.className = "song-duration hint";
  const formatted = formatDuration(song.duration_seconds);
  duration.textContent = formatted || "";

  const playButton = document.createElement("button");
  playButton.type = "button";
  playButton.className = "play-hover-button";
  const isCurrent = String(song.id) === String(currentSongId) && isPlaying;
  playButton.textContent = isCurrent ? "⏸" : "▶";
  playButton.setAttribute("aria-label", isCurrent ? `Pausar ${song.title}` : `Reproducir ${song.title}`);
  playButton.addEventListener("click", () => onPlay(song));
  if (isCurrent) li.classList.add("now-playing");

  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.className = "add-button";
  addButton.textContent = "+";
  addButton.setAttribute("aria-label", `Añadir ${song.title} a la playlist`);
  addButton.addEventListener("click", () => onAdd(song));

  li.append(cover, info, duration, playButton, addButton);

  li.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData("application/x-song-id", String(song.id));
    event.dataTransfer.effectAllowed = "copy";
    li.classList.add("dragging");
  });
  li.addEventListener("dragend", () => li.classList.remove("dragging"));

  return li;
}

export function renderDiscoveryList(container, songs, callbacks) {
  container.textContent = "";
  if (songs.length === 0) {
    const li = document.createElement("li");
    li.className = "hint";
    li.textContent = "Todavía no hay canciones en el catálogo.";
    container.appendChild(li);
    return;
  }
  for (const song of songs) {
    container.appendChild(buildSongCard(song, callbacks));
  }
}

// Actualiza solo la clase "now-playing" y el icono/aria-label del botón de
// play sobre las filas YA existentes, sin reconstruir la lista - se llama en
// cada play/pause/ended, así que reconstruir aquí re-dispararía la
// animación de entrada de TODAS las filas y el aria-live de la lista
// volvería a anunciarla entera en cada pausa (hallazgo real de la revisión
// "abogado del diablo" sobre la implementación).
export function updateDiscoveryNowPlaying(container, currentSongId, isPlaying) {
  for (const li of container.querySelectorAll(".song-card")) {
    const isCurrent = li.dataset.songId === String(currentSongId) && isPlaying;
    li.classList.toggle("now-playing", isCurrent);
    const playButton = li.querySelector(".play-hover-button");
    if (!playButton) continue;
    playButton.textContent = isCurrent ? "⏸" : "▶";
    const title = li.querySelector(".song-title")?.textContent || "";
    playButton.setAttribute("aria-label", isCurrent ? `Pausar ${title}` : `Reproducir ${title}`);
  }
}
