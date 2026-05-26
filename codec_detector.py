import json
import subprocess
import os


class ProbeResult:
    def __init__(self, codec: str, duration_sec: float, size_bytes: int,
                 width: int, height: int):
        self.codec = codec
        self.duration_sec = duration_sec
        self.size_bytes = size_bytes
        self.width = width
        self.height = height

    @property
    def is_x265(self) -> bool:
        return self.codec in ("hevc", "h265", "x265")


def probe(path: str) -> ProbeResult | None:
    """Run ffprobe on a file and return a ProbeResult, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        if not video_stream:
            return None

        codec = video_stream.get("codec_name", "unknown").lower()
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))

        fmt = data.get("format", {})
        duration_sec = float(fmt.get("duration", 0))
        size_bytes = int(fmt.get("size", os.path.getsize(path)))

        return ProbeResult(codec, duration_sec, size_bytes, width, height)

    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
        return None


def verify_file(path: str, expected_codec: str = None,
                ref_duration: float = None, tolerance: float = 0.05) -> bool:
    """Return True if file is playable and optionally matches codec/duration."""
    result = probe(path)
    if result is None:
        return False
    if expected_codec and result.codec != expected_codec:
        return False
    if ref_duration and ref_duration > 0:
        diff = abs(result.duration_sec - ref_duration) / ref_duration
        if diff > tolerance:
            return False
    return True
