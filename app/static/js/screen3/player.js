import { getStreamUrl } from "./api.js";
import { formatDuration, applyCoverGradient } from "../shared.js";
import { screen3State } from "./state.js";

// deps: { onStateChange, onSessionExpired, onGateLost }
export function initPlayer({ onStateChange, onSessionExpired, onGateLost }) {
  const audio = document.getElementById("app-player");
  const bar = document.getElementById("player-bar");
  const cover = document.getElementById("player-cover");
  const titleEl = document.getElementById("player-song-title");
  const artistEl = document.getElementById("player-song-artist");
  const playPauseButton = document.getElementById("player-play-pause");
  const prevButton = document.getElementById("player-prev");
  const nextButton = document.getElementById("player-next");
  const progress = document.getElementById("player-progress");
  const timeEl = document.getElementById("player-time");
  const volumeInput = document.getElementById("player-volume");

  // Mismo patrón que catalogGeneration/playRequestId de la Pantalla 1/2
  // (descartar respuestas de fetch obsoletas): si el usuario pulsa
  // "Reproducir" en dos canciones distintas seguidas, gana la última
  // pulsada, no la que responda primero.
  let playRequestId = 0;
  let userIsSeeking = false;

  function currentSong() {
    if (screen3State.currentQueueIndex < 0) return null;
    return screen3State.currentQueue[screen3State.currentQueueIndex] || null;
  }

  function renderNowPlaying() {
    const song = currentSong();
    if (!song) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    applyCoverGradient(cover, song.id);
    titleEl.textContent = song.title;
    artistEl.textContent = song.artist;
    playPauseButton.textContent = screen3State.isPlaying ? "⏸" : "▶";
    playPauseButton.setAttribute(
      "aria-label",
      screen3State.isPlaying ? "Pausar" : "Reproducir"
    );
    prevButton.disabled = screen3State.currentQueueIndex <= 0;
    nextButton.disabled =
      screen3State.currentQueueIndex >= screen3State.currentQueue.length - 1;
  }

  async function loadAndPlay(queue, index) {
    const song = queue[index];
    if (!song) return;
    const requestId = ++playRequestId;
    screen3State.currentQueue = queue;
    screen3State.currentQueueIndex = index;
    screen3State.currentSongId = song.id;
    renderNowPlaying();
    try {
      const data = await getStreamUrl(song.id);
      if (requestId !== playRequestId) return;
      audio.src = data.url;
      await audio.play();
    } catch (err) {
      if (requestId !== playRequestId) return;
      if (err.status === 401) {
        onSessionExpired();
        return;
      }
      if (err.status === 403) {
        onGateLost();
        return;
      }
      screen3State.isPlaying = false;
      renderNowPlaying();
    }
  }

  playPauseButton.addEventListener("click", () => {
    if (!currentSong()) return;
    if (screen3State.isPlaying) {
      audio.pause();
    } else {
      audio.play().catch(() => {});
    }
  });

  prevButton.addEventListener("click", () => {
    if (screen3State.currentQueueIndex > 0) {
      loadAndPlay(screen3State.currentQueue, screen3State.currentQueueIndex - 1);
    }
  });

  nextButton.addEventListener("click", () => {
    if (screen3State.currentQueueIndex < screen3State.currentQueue.length - 1) {
      loadAndPlay(screen3State.currentQueue, screen3State.currentQueueIndex + 1);
    }
  });

  audio.addEventListener("play", () => {
    screen3State.isPlaying = true;
    renderNowPlaying();
    onStateChange();
  });
  audio.addEventListener("pause", () => {
    screen3State.isPlaying = false;
    renderNowPlaying();
    onStateChange();
  });
  audio.addEventListener("ended", () => {
    if (screen3State.currentQueueIndex < screen3State.currentQueue.length - 1) {
      loadAndPlay(screen3State.currentQueue, screen3State.currentQueueIndex + 1);
    } else {
      screen3State.isPlaying = false;
      renderNowPlaying();
      onStateChange();
    }
  });
  audio.addEventListener("timeupdate", () => {
    if (userIsSeeking) return;
    if (audio.duration) {
      progress.value = String((audio.currentTime / audio.duration) * 100);
    }
    timeEl.textContent = `${formatDuration(audio.currentTime) || "0:00"} / ${
      formatDuration(audio.duration) || "0:00"
    }`;
  });
  audio.addEventListener("loadedmetadata", () => {
    timeEl.textContent = `${formatDuration(audio.currentTime) || "0:00"} / ${
      formatDuration(audio.duration) || "0:00"
    }`;
  });

  progress.addEventListener("input", () => {
    userIsSeeking = true;
  });
  progress.addEventListener("change", () => {
    if (audio.duration) {
      audio.currentTime = (Number(progress.value) / 100) * audio.duration;
    }
    userIsSeeking = false;
  });

  volumeInput.addEventListener("input", () => {
    audio.volume = Number(volumeInput.value);
  });

  return {
    playSong(queue, index) {
      loadAndPlay(queue, index);
    },
    pause() {
      audio.pause();
    },
    stop() {
      audio.pause();
      audio.removeAttribute("src");
      screen3State.currentQueue = [];
      screen3State.currentQueueIndex = -1;
      screen3State.currentSongId = null;
      screen3State.isPlaying = false;
      renderNowPlaying();
    },
  };
}
