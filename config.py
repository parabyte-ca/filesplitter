import os
import socket


def _find_free_port(preferred: int) -> int:
    """Return preferred port if free, otherwise next available port."""
    port = preferred
    while port < preferred + 20:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                port += 1
    return port


def _parse_paths(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


MEDIA_PATHS: list[str] = _parse_paths(os.environ.get("MEDIA_PATHS", "/media"))

SCENE_THRESHOLD: float = float(os.environ.get("SCENE_THRESHOLD", "0.4"))
MIN_SCENE_DURATION: int = int(os.environ.get("MIN_SCENE_DURATION", "120"))

# Scene detection method: "auto" (select then blackdetect fallback), "select", or "black"
SCENE_METHOD: str = os.environ.get("SCENE_METHOD", "auto")
BLACK_MIN_DURATION: float = float(os.environ.get("BLACK_MIN_DURATION", "0.5"))
BLACK_PIX_TH: float = float(os.environ.get("BLACK_PIX_TH", "0.10"))

SPLIT_SCENE_METHOD: str = os.environ.get("SPLIT_SCENE_METHOD", "combined")
SPLIT_MIN_EPISODE_GAP: int = int(os.environ.get("SPLIT_MIN_EPISODE_GAP", "2700"))  # 45 min
SPLIT_BLACK_MIN_DURATION: float = float(os.environ.get("SPLIT_BLACK_MIN_DURATION", "2.0"))
SPLIT_FREEZE_MIN_DURATION: float = float(os.environ.get("SPLIT_FREEZE_MIN_DURATION", "2.0"))
SPLIT_EPISODE_COUNT: int = int(os.environ.get("SPLIT_EPISODE_COUNT", "0"))  # 0 = auto

SPLIT_MIN_DURATION: int = int(os.environ.get("SPLIT_MIN_DURATION", "5400"))   # 90 min
SPLIT_MIN_SIZE: int = int(os.environ.get("SPLIT_MIN_SIZE", str(2 * 1024 ** 3)))  # 2 GB
SPLIT_KEYWORDS: list[str] = _parse_paths(
    os.environ.get("SPLIT_KEYWORDS", "Vol,Compilation,Anthology,Collection,Kink")
)

X265_CRF: int = int(os.environ.get("X265_CRF", "28"))
X265_PRESET: str = os.environ.get("X265_PRESET", "medium")

# cpu — software libx265 (default)
# nvenc — NVIDIA hardware hevc_nvenc (requires GPU device passthrough in docker-compose.yml)
ENCODER_BACKEND: str = os.environ.get("ENCODER_BACKEND", "cpu")

# original | 480p | 720p | 1080p | 1440p | 2160p
TARGET_RESOLUTION: str = os.environ.get("TARGET_RESOLUTION", "original")

RESOLUTION_MAP: dict[str, int] = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "2160p": 2160,
}

MAX_WORKERS: int = int(os.environ.get("MAX_WORKERS", "2"))

VIDEO_EXTENSIONS: set[str] = set(
    os.environ.get(
        "VIDEO_EXTENSIONS", ".mp4,.mkv,.avi,.mov,.wmv,.m4v,.ts,.flv"
    ).split(",")
)

DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "/app/data/filesplitter.db")

FFPROBE_TIMEOUT: int = int(os.environ.get("FFPROBE_TIMEOUT", "60"))

# ThePornDB API key for content-based scene naming (free registration at theporndb.net/register)
TPDB_API_KEY: str = os.environ.get("TPDB_API_KEY", "")

# Set to require a password when accessed through Cloudflare Tunnel (external access).
# Direct LAN access is always unrestricted.
DASHBOARD_PASSWORD: str = os.environ.get("DASHBOARD_PASSWORD", "")

# Override the auto-generated session secret key (stored in DB). Only needed if you
# want sessions to survive a full database reset.
FLASK_SECRET_KEY: str = os.environ.get("FLASK_SECRET_KEY", "")

_preferred_port = int(os.environ.get("FLASK_PORT", "4250"))
FLASK_PORT: int = _find_free_port(_preferred_port)
if FLASK_PORT != _preferred_port:
    print(f"[config] Port {_preferred_port} in use, using {FLASK_PORT} instead")
