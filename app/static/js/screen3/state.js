export const screen3State = {
  discoverySongs: [],
  playlists: [],
  selectedPlaylistId: null,
  selectedPlaylistDetail: null,
  // La cola se fija a la lista desde la que se pulsó "reproducir" por última
  // vez (Descubrimiento o la playlist seleccionada) - cambiar de playlist
  // seleccionada o recargar Descubrimiento NO cambia la cola de una
  // reproducción ya en curso, solo afecta a la siguiente vez que se pulse
  // play (decisión explícita, ver plan de la pieza).
  currentQueue: [],
  currentQueueIndex: -1,
  currentSongId: null,
  isPlaying: false,
};
