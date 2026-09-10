import { formatDuration, applyCoverGradient } from "../shared.js";

const REORDER_MIME = "application/x-reorder-song-id";

function buildPlaylistRow(item, index, callbacks) {
  const { onPlay, onRemove, onMoveUp, onMoveDown, currentSongId, isPlaying, canMoveUp, canMoveDown } =
    callbacks;
  const song = item.song;

  const li = document.createElement("li");
  li.className = "playlist-song-row";
  li.draggable = true;
  li.dataset.songId = String(song.id);

  const cover = document.createElement("div");
  cover.className = "cover-art";
  applyCoverGradient(cover, song.id);

  const info = document.createElement("div");
  info.className = "song-info";
  info.addEventListener("click", () => onPlay(song));
  info.classList.add("clickable");
  const title = document.createElement("p");
  title.className = "song-title";
  title.textContent = song.title;
  const artist = document.createElement("p");
  artist.className = "song-artist hint";
  artist.textContent = song.artist;
  info.append(title, artist);

  const duration = document.createElement("span");
  duration.className = "song-duration hint";
  duration.textContent = formatDuration(song.duration_seconds) || "";

  const isCurrent = String(song.id) === String(currentSongId) && isPlaying;
  if (isCurrent) li.classList.add("now-playing");

  const upButton = document.createElement("button");
  upButton.type = "button";
  upButton.className = "move-button";
  upButton.textContent = "▲";
  upButton.setAttribute("aria-label", `Mover ${song.title} arriba`);
  upButton.disabled = !canMoveUp;
  upButton.addEventListener("click", () => onMoveUp(index));

  const downButton = document.createElement("button");
  downButton.type = "button";
  downButton.className = "move-button";
  downButton.textContent = "▼";
  downButton.setAttribute("aria-label", `Mover ${song.title} abajo`);
  downButton.disabled = !canMoveDown;
  downButton.addEventListener("click", () => onMoveDown(index));

  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "remove-button";
  removeButton.textContent = "✕";
  removeButton.setAttribute("aria-label", `Quitar ${song.title} de la playlist`);
  removeButton.addEventListener("click", () => onRemove(song));

  li.append(cover, info, duration, upButton, downButton, removeButton);

  li.addEventListener("dragstart", (event) => {
    event.dataTransfer.setData(REORDER_MIME, String(song.id));
    event.dataTransfer.effectAllowed = "move";
    li.classList.add("dragging");
  });
  li.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    li.classList.remove("drop-before", "drop-after");
  });
  li.addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes(REORDER_MIME)) return;
    event.preventDefault();
    const rect = li.getBoundingClientRect();
    const before = event.clientY - rect.top < rect.height / 2;
    li.classList.toggle("drop-before", before);
    li.classList.toggle("drop-after", !before);
  });
  li.addEventListener("dragleave", () => {
    li.classList.remove("drop-before", "drop-after");
  });
  li.addEventListener("drop", (event) => {
    if (!event.dataTransfer.types.includes(REORDER_MIME)) return;
    event.preventDefault();
    const draggedSongId = event.dataTransfer.getData(REORDER_MIME);
    const insertBefore = li.classList.contains("drop-before");
    li.classList.remove("drop-before", "drop-after");
    callbacks.onReorderDrop(draggedSongId, song.id, insertBefore);
  });

  return li;
}

export function renderPlaylistPanel(listContainer, emptyStateEl, detail, callbacks) {
  listContainer.textContent = "";
  const songs = detail ? detail.songs : [];

  emptyStateEl.hidden = songs.length > 0;
  listContainer.hidden = songs.length === 0;

  songs.forEach((item, index) => {
    listContainer.appendChild(
      buildPlaylistRow(item, index, {
        ...callbacks,
        canMoveUp: index > 0,
        canMoveDown: index < songs.length - 1,
      })
    );
  });
}

// Igual que discovery.js::updateDiscoveryNowPlaying - solo alterna la clase
// "now-playing" sobre las filas ya existentes, sin reconstruir la lista
// (evita re-disparar la animación de entrada y el aria-live en cada
// play/pause, ver screen3/index.js).
export function updatePlaylistNowPlaying(container, currentSongId, isPlaying) {
  for (const li of container.querySelectorAll(".playlist-song-row")) {
    const isCurrent = li.dataset.songId === String(currentSongId) && isPlaying;
    li.classList.toggle("now-playing", isCurrent);
  }
}

export { REORDER_MIME };
