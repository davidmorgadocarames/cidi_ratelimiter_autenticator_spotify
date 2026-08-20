import json
import subprocess

_PROBE_TIMEOUT_SECONDS = 30
_TRANSCODE_TIMEOUT_SECONDS = 300


class TranscodeError(Exception):
    """Fallo controlado de ffmpeg/ffprobe (código de salida != 0, timeout, o
    salida sin la duración esperada) - nunca se propaga el stderr crudo del
    proceso a la respuesta HTTP, solo se loguea en el servidor."""


def probe_duration_seconds(path: str) -> float:
    """Corre ffprobe sobre el archivo. Además de extraer la duración, ESTA es
    la validación real de "es audio de verdad" - nunca el header Content-Type
    del multipart, que el cliente puede falsear libremente."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError("ffprobe superó el timeout") from exc

    if result.returncode != 0:
        raise TranscodeError(
            f"ffprobe devolvió código {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )

    try:
        payload = json.loads(result.stdout)
        return float(payload["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise TranscodeError("ffprobe no reportó una duración válida") from exc


def transcode_to_mp3(input_path: str, output_path: str) -> None:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                input_path,
                "-vn",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-b:a",
                "192k",
                output_path,
                "-y",
            ],
            capture_output=True,
            timeout=_TRANSCODE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError("ffmpeg superó el timeout") from exc

    if result.returncode != 0:
        raise TranscodeError(
            f"ffmpeg devolvió código {result.returncode}: "
            f"{result.stderr.decode(errors='replace')}"
        )
