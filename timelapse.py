#!/usr/bin/env python3
"""
ipcam-timelapse — Multi-camera timelapse capture for Linux/Docker
"""

import ftplib
import html
import io
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests
import yaml
from astral import LocationInfo
from astral.sun import sun, elevation, azimuth
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class DailyFileHandler(logging.Handler):
    """Logghandler som skriver til daglige filer: {camera_name}_{YYYY-MM-DD}.log.
    Roterer ved midnatt og rydder opp gamle filer basert på retention_days."""

    def __init__(self, log_dir: Path, camera_name: str,
                 retention_days: int = 30, cleanup_enabled: bool = True):
        super().__init__()
        self.log_dir = log_dir
        self.camera_name = camera_name
        self.retention_days = retention_days
        self.cleanup_enabled = cleanup_enabled
        self._current_date: date | None = None
        self._file = None
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._open()

    def _log_path(self, d: date) -> Path:
        return self.log_dir / f"{self.camera_name}_{d.strftime('%Y-%m-%d')}.log"

    def _open(self):
        today = date.today()
        if self._file:
            self._file.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._file = open(self._log_path(today), "a", encoding="utf-8")
        self._current_date = today

    def _cleanup(self):
        cutoff = date.today() - timedelta(days=self.retention_days)
        for f in self.log_dir.glob(f"{self.camera_name}_????-??-??.log"):
            try:
                file_date = datetime.strptime(f.stem[-10:], "%Y-%m-%d").date()
                if file_date < cutoff:
                    f.unlink()
            except ValueError:
                pass

    def emit(self, record):
        today = date.today()
        if today != self._current_date:
            self._open()
            if self.cleanup_enabled:
                self._cleanup()
        try:
            self._file.write(self.format(record) + "\n")
            self._file.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._file:
            self._file.close()
        super().close()

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf",
]

WEEKDAYS_NO = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lordag", "sondag"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path="config.yaml"):
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

# Siste tidspunkt hvert kamera målte uskarpt bilde. Er flere kameraer uskarpe samtidig,
# er det vær (tåke, dis, snø), ikke linsen — ingen linse mister fokus på to kameraer samtidig.
_BLUR_SEEN: dict[str, datetime] = {}


def local_now(timezone_str):
    return datetime.now(ZoneInfo(timezone_str))


def get_sun_times(lat, lon, date_obj, timezone_str, sunrise_offset=0, sunset_offset=0):
    tz = ZoneInfo(timezone_str)
    location = LocationInfo(latitude=lat, longitude=lon, timezone=timezone_str)
    s = sun(location.observer, date=date_obj, tzinfo=tz)
    return (
        s["sunrise"] + timedelta(minutes=sunrise_offset),
        s["sunset"] + timedelta(minutes=sunset_offset),
    )


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

_SECRET_PARAM_RE = re.compile(r"(?i)((?:password|passwd|pwd|pass|token|user|username)=)[^&\s'\")]*")


def redact_secrets(text: str) -> str:
    """Maskerer passord/brukernavn i URL-er (query-parametre) i feilmeldinger,
    slik at de ikke havner i logg, /health eller dashbord."""
    return _SECRET_PARAM_RE.sub(r"\1***", text)


def fetch_image(url, timeout=15):
    resp = requests.get(url, timeout=timeout, stream=True)
    resp.raise_for_status()
    return resp.content


def _load_font(size):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def add_timestamp(image_bytes, timestamp_str, font_size=28, position_h="left", position_v="bottom"):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = _load_font(font_size)

    bbox = draw.textbbox((0, 0), timestamp_str, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    img_w, img_h = img.size
    margin = 10

    if position_h == "right":
        x = img_w - text_w - margin
    elif position_h == "center":
        x = (img_w - text_w) // 2
    else:  # left
        x = margin

    if position_v == "top":
        y = margin
    else:  # bottom
        y = img_h - text_h - margin * 2

    pad = 4
    draw.rectangle(
        [x - pad, y - pad, x + text_w + pad, y + text_h + pad],
        fill=(0, 0, 0, 160),
    )
    draw.text((x + 2, y + 2), timestamp_str, font=font, fill=(0, 0, 0))
    draw.text((x, y), timestamp_str, font=font, fill=(255, 255, 255))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def save_image(image_bytes, root_folder, now, save_daily=True):
    root = Path(root_folder)
    root.mkdir(parents=True, exist_ok=True)

    (root / "latest.jpg").write_bytes(image_bytes)

    if save_daily:
        date_str = now.strftime("%Y-%m-%d")
        daily = root / date_str
        daily.mkdir(exist_ok=True)
        filename = now.strftime("%Y-%m-%d_%H-%M-%S") + ".jpg"
        (daily / filename).write_bytes(image_bytes)


# ---------------------------------------------------------------------------
# FTP helper (shared)
# ---------------------------------------------------------------------------

def ftp_upload_bytes(data: bytes, ftp_cfg: dict, filename: str):
    with ftplib.FTP() as ftp:
        ftp.connect(ftp_cfg["host"], ftp_cfg.get("port", 21), timeout=20)
        ftp.login(ftp_cfg["username"], ftp_cfg["password"])
        folder = ftp_cfg.get("folder", "/")
        if folder:
            ftp.cwd(folder)
        ftp.storbinary(f"STOR {filename}", io.BytesIO(data))


def ftp_upload_file(path: Path, ftp_cfg: dict, filename: str):
    host = ftp_cfg["host"]
    port = ftp_cfg.get("port", 21)
    user = ftp_cfg["username"]
    password = ftp_cfg["password"]
    folder = ftp_cfg.get("folder", "/").strip("/")

    url = f"ftp://{host}:{port}/{folder}/{filename}"
    result = subprocess.run(
        ["curl", "--silent", "--show-error", "--ftp-create-dirs",
         "--upload-file", str(path), "--user", f"{user}:{password}", url],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise IOError(f"curl FTP upload feilet (rc={result.returncode}): {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------

def send_pushover(token: str, user: str, title: str, message: str) -> bool:
    """Sender Pushover-varsel. Returnerer True ved bekreftet levering (HTTP 200, status=1)."""
    log = logging.getLogger("pushover")
    try:
        resp = requests.post("https://api.pushover.net/1/messages.json", data={
            "token": token,
            "user": user,
            "title": title,
            "message": message,
        }, timeout=5)
        if resp.status_code != 200:
            log.error("Pushover avvist (HTTP %d): %s — «%s: %s»",
                      resp.status_code, resp.text[:200], title, message)
            return False
        log.info("Pushover sendt — «%s: %s»", title, message)
        return True
    except Exception as exc:
        log.error("Pushover-varsling feilet: %s — «%s: %s»", exc, title, message)
        return False


# ---------------------------------------------------------------------------
# PTZ helper
# ---------------------------------------------------------------------------

def goto_ptz_preset(cam_cfg: dict, preset_id: int, speed: int = 32):
    """Beveger PTZ-kamera til en lagret preset-posisjon via Reolink JSON-API."""
    ip = cam_cfg["ip"]
    login_url = f"http://{ip}/api.cgi?cmd=Login&token=null"
    login_payload = [{"cmd": "Login", "action": 0, "param": {
        "User": {"userName": cam_cfg["username"], "password": cam_cfg["password"]}
    }}]
    token = requests.post(login_url, json=login_payload, timeout=5).json()[0]["value"]["Token"]["name"]
    try:
        ptz_url = f"http://{ip}/api.cgi?token={token}"
        ptz_payload = [{"cmd": "PtzCtrl", "action": 0, "param": {
            "channel": cam_cfg.get("channel", 0),
            "op": "ToPos",
            "id": preset_id,
            "speed": speed,
        }}]
        requests.post(ptz_url, json=ptz_payload, timeout=5)
    finally:
        requests.post(
            f"http://{ip}/api.cgi?cmd=Logout&token={token}",
            json=[{"cmd": "Logout", "action": 0, "param": {}}],
            timeout=5,
        )


# ---------------------------------------------------------------------------
# Dark image detection / image comparison
# ---------------------------------------------------------------------------

def _laplacian_variance(image_bytes: bytes) -> float:
    """Laplacian-varians som mål på bildeskarphef. Høyere = skarpere."""
    arr = np.array(Image.open(io.BytesIO(image_bytes)).convert("L"), dtype=float)
    lap = (np.roll(arr, 1, 0) + np.roll(arr, -1, 0) +
           np.roll(arr, 1, 1) + np.roll(arr, -1, 1) - 4 * arr)
    return float(lap.var())


def images_similar(ref: bytes, cur: bytes, threshold: float = 0.90,
                   min_std: float = 8.0) -> tuple[bool, float]:
    """Sammenligner to bilder med Pearson-korrelasjon på 64×64 thumbnail.
    Returnerer (True, corr) hvis bildene ser like ut (kamera ikke har beveget seg).

    Er et av bildene nesten helt mørkt (stddev < min_std) domineres korrelasjonen
    av noen få lyskilder, og en lampe som slås av/på ser ut som en kamerabevegelse.
    Da returneres (True, nan): kan ikke bedømmes, ikke hopp over.
    Ved feil returneres (True, 1.0) (ikke hopp over).
    """
    try:
        def thumb(b):
            return np.array(Image.open(io.BytesIO(b)).convert("L").resize((64, 64)), dtype=float)
        arr1, arr2 = thumb(ref), thumb(cur)
        if min(arr1.std(), arr2.std()) < min_std:
            return True, float("nan")
        corr = float(np.corrcoef(arr1.flatten(), arr2.flatten())[0, 1])
        return bool(corr >= threshold), corr
    except Exception:
        return True, 1.0


def is_dark(image_path: Path, darkness_threshold: int = 30, dark_percent_limit: float = 98.5) -> bool:
    """Returns True if the image is considered too dark to keep."""
    try:
        img = Image.open(image_path).convert("L")
        pixels = np.array(img)
        dark_pct = (pixels < darkness_threshold).sum() / pixels.size * 100
        return dark_pct >= dark_percent_limit
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Timelapse generator
# ---------------------------------------------------------------------------

class TimelapseGenerator:
    """Generates rolling hourly and end-of-day timelapse videos from captured images."""

    def __init__(self, cam_cfg: dict, global_cfg: dict):
        self.cam_cfg = cam_cfg
        self.tl_cfg = cam_cfg.get("timelapse", {})
        self.ftp_cfg = cam_cfg.get("ftp", {})   # shared FTP config for the camera
        self.name = cam_cfg["name"]
        self.tz = global_cfg.get("timezone", "Europe/Oslo")
        self.log = logging.getLogger(f"{self.name}.timelapse")

        self.last_hourly_hour: int | None = None   # hour number of last hourly run
        self.last_hourly_date: date | None = None
        self.last_daily_date: date | None = None
        self.last_final_rolling_date: date | None = None  # siste avsluttende rolling ved solnedgang

        self._lock = threading.Lock()              # prevent concurrent ffmpeg runs
        self._running = False

    def check(self, now: datetime, sunset: datetime | None, save_daylight_only: bool = False, is_daylight: bool = True):
        if not self.tl_cfg.get("enabled", False):
            return
        if self._running:
            return

        # --- Hourly timelapse at the top of each hour (kun i dagslys ved save_daylight_only) ---
        if now.minute == 0:
            if not save_daylight_only or is_daylight:
                if self.last_hourly_hour != now.hour or self.last_hourly_date != now.date():
                    threading.Thread(
                        target=self._make_hourly, args=(now,), daemon=True
                    ).start()
                    return  # Don't also check daily in same tick

        # --- Avsluttende rolling timelapse ved solnedgang (kun ved save_daylight_only) ---
        if save_daylight_only and sunset is not None and now >= sunset:
            if self.last_final_rolling_date != now.date():
                self.log.info("Lager avsluttende rolling timelapse ved solnedgang")
                threading.Thread(
                    target=self._make_final_rolling, args=(now,), daemon=True
                ).start()
                return

        # --- Daily timelapse after sunset ---
        if sunset is not None and now >= sunset and self.last_daily_date != now.date():
            threading.Thread(
                target=self._make_daily, args=(now,), daemon=True
            ).start()

    # ------------------------------------------------------------------
    # Image collection
    # ------------------------------------------------------------------

    def _collect_images(self, since: datetime, until: datetime) -> list[Path]:
        root = Path(self.cam_cfg["root_folder"])
        images = []
        current = since.date()
        tz = ZoneInfo(self.tz)

        while current <= until.date():
            daily_folder = root / current.strftime("%Y-%m-%d")
            if daily_folder.exists():
                for img in sorted(daily_folder.glob("*.jpg")):
                    try:
                        ts = datetime.strptime(img.stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=tz)
                        if since <= ts <= until:
                            images.append(img)
                    except ValueError:
                        pass
            current += timedelta(days=1)

        return images

    # ------------------------------------------------------------------
    # Video encoding
    # ------------------------------------------------------------------

    def _encode(self, images: list[Path], output: Path) -> bool:
        if not images:
            self.log.warning("No images available for timelapse")
            return False

        fps = self.tl_cfg.get("fps", 10)
        crf = self.tl_cfg.get("crf", 23)  # 0=lossless, 51=lavest kvalitet; 18-28 er typisk

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for img in images:
                f.write(f"file '{img.absolute()}'\n")
            filelist = f.name

        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0", "-i", filelist,
                    "-vf", f"fps={fps}",
                    "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                self.log.error("ffmpeg error: %s", result.stderr[-800:])
                output.unlink(missing_ok=True)
                return False
            if not output.exists() or output.stat().st_size == 0:
                self.log.error("ffmpeg produced empty output file")
                output.unlink(missing_ok=True)
                return False
            return True
        except subprocess.TimeoutExpired:
            self.log.error("ffmpeg timed out")
            output.unlink(missing_ok=True)
            return False
        finally:
            Path(filelist).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Hourly rolling timelapse
    # ------------------------------------------------------------------

    def _make_hourly(self, now: datetime):
        with self._lock:
            self._running = True
            try:
                rolling_hours = self.tl_cfg.get("rolling_hours", 4)
                since = now - timedelta(hours=rolling_hours)
                self.log.info("Hourly timelapse — last %dh (%s → %s)", rolling_hours,
                              since.strftime("%H:%M"), now.strftime("%H:%M"))

                images = self._collect_images(since, now)
                if not images:
                    self.log.warning("No images found for hourly timelapse")
                    return

                out_dir = Path(self.cam_cfg["root_folder"]) / "timelapse"
                out_dir.mkdir(parents=True, exist_ok=True)
                output = out_dir / "rolling.mp4"

                if self._encode(images, output):
                    self.log.info("Hourly timelapse OK — %d frames → %s", len(images), output)
                    self.last_hourly_hour = now.hour
                    self.last_hourly_date = now.date()
                    filename = self.ftp_cfg.get("hourly_filename", "timelapse_rolling.mp4")
                    self._ftp_upload_video(output, filename)
            finally:
                self._running = False

    # ------------------------------------------------------------------
    # Final rolling timelapse at sunset (save_daylight_only)
    # ------------------------------------------------------------------

    def _make_final_rolling(self, now: datetime):
        with self._lock:
            self._running = True
            try:
                rolling_hours = self.tl_cfg.get("rolling_hours", 4)
                since = now - timedelta(hours=rolling_hours)
                self.log.info("Final rolling timelapse ved solnedgang — last %dh (%s → %s)",
                              rolling_hours, since.strftime("%H:%M"), now.strftime("%H:%M"))

                images = self._collect_images(since, now)
                if not images:
                    self.log.warning("No images found for final rolling timelapse")
                    return

                out_dir = Path(self.cam_cfg["root_folder"]) / "timelapse"
                out_dir.mkdir(parents=True, exist_ok=True)
                output = out_dir / "rolling.mp4"

                if self._encode(images, output):
                    self.log.info("Final rolling timelapse OK — %d frames → %s", len(images), output)
                    self.last_final_rolling_date = now.date()
                    filename = self.ftp_cfg.get("hourly_filename", "timelapse_rolling.mp4")
                    self._ftp_upload_video(output, filename)
            finally:
                self._running = False

    # ------------------------------------------------------------------
    # End-of-day full timelapse
    # ------------------------------------------------------------------

    def _make_daily(self, now: datetime):
        with self._lock:
            self._running = True
            try:
                today = now.date()
                tz = ZoneInfo(self.tz)
                day_start = datetime(today.year, today.month, today.day, tzinfo=tz)

                self.log.info("Daily timelapse — %s", today.isoformat())
                images = self._collect_images(day_start, now)
                if not images:
                    self.log.warning("No images found for daily timelapse")
                    return

                weekday = WEEKDAYS_NO[today.weekday()]
                filename = f"{self.name}_{weekday}.mp4"
                out_dir = Path(self.cam_cfg["root_folder"]) / "timelapse"
                out_dir.mkdir(parents=True, exist_ok=True)
                output = out_dir / filename

                if self._encode(images, output):
                    self.log.info("Daily timelapse OK — %d frames → %s", len(images), filename)
                    self.last_daily_date = today
                    self._ftp_upload_video(output, filename)
            finally:
                self._running = False

    # ------------------------------------------------------------------
    # FTP upload
    # ------------------------------------------------------------------

    def _ftp_upload_video(self, path: Path, filename: str):
        if not self.ftp_cfg.get("enabled", False):
            return
        try:
            ftp_upload_file(path, self.ftp_cfg, filename)
            self.log.info("FTP upload OK: %s", filename)
        except Exception as exc:
            self.log.error("FTP upload failed: %s", exc)


# ---------------------------------------------------------------------------
# Dark image cleaner
# ---------------------------------------------------------------------------

class DarkCleaner:
    """Deletes dark/unusable images from daily archive folders on a schedule."""

    def __init__(self, cam_cfg: dict, global_cfg: dict):
        self.cam_cfg = cam_cfg
        self.name = cam_cfg["name"]
        self.tz = global_cfg.get("timezone", "Europe/Oslo")
        self.log = logging.getLogger(f"{self.name}.darkclean")
        self.last_cleanup_date: date | None = None

    def check(self, now: datetime):
        dc = self.cam_cfg.get("dark_cleanup", {})
        if not dc.get("enabled", False):
            return
        try:
            run_h, run_m = map(int, dc.get("run_at", "12:00").split(":"))
        except ValueError:
            self.log.warning("Ugyldig run_at-format i dark_cleanup: %s", dc.get("run_at"))
            return
        if now.hour != run_h or now.minute != run_m:
            return
        if self.last_cleanup_date == now.date():
            return
        self.last_cleanup_date = now.date()
        threading.Thread(target=self._cleanup, args=(now, dc), daemon=True).start()

    def _cleanup(self, now: datetime, dc: dict):
        threshold = dc.get("darkness_threshold", 30)
        limit = dc.get("dark_percent_limit", 98.5)
        lookback_h = dc.get("lookback_hours", 24)
        since = now - timedelta(hours=lookback_h)
        root = Path(self.cam_cfg["root_folder"])
        tz = ZoneInfo(self.tz)
        deleted = checked = 0

        current = since.date()
        while current <= now.date():
            daily = root / current.strftime("%Y-%m-%d")
            if daily.exists():
                for img in sorted(daily.glob("*.jpg")):
                    try:
                        ts = datetime.strptime(img.stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=tz)
                        if ts < since:
                            continue
                    except ValueError:
                        pass
                    checked += 1
                    if is_dark(img, threshold, limit):
                        img.unlink()
                        deleted += 1
            current += timedelta(days=1)

        self.log.info(
            "Mørk-opprydding: slettet %d av %d bilder (terskel: pixelverdi < %d, grense: %.1f%%)",
            deleted, checked, threshold, limit,
        )


# ---------------------------------------------------------------------------
# Camera worker
# ---------------------------------------------------------------------------

class CameraWorker:
    def __init__(self, cam_cfg, global_cfg):
        self.cfg = cam_cfg
        self._global_cfg = global_cfg
        self.name = cam_cfg["name"]
        self.tz = global_cfg.get("timezone", "Europe/Oslo")
        self.log = logging.getLogger(self.name)

        self.last_capture: datetime | None = None
        self._last_run_time: datetime | None = None
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.total_captures: int = 0
        self.started_at: datetime = local_now(self.tz)

        log_cfg = cam_cfg.get("logging", {})
        if log_cfg.get("enabled", False):
            handler = DailyFileHandler(
                log_dir=Path(log_cfg["path"]) / self.name,
                camera_name=self.name,
                retention_days=log_cfg.get("retention_days", 30),
                cleanup_enabled=log_cfg.get("cleanup_enabled", True),
            )
            self.log.addHandler(handler)
            self.log.propagate = True  # også til konsoll

        self.timelapse = TimelapseGenerator(cam_cfg, global_cfg)
        self.dark_cleaner = DarkCleaner(cam_cfg, global_cfg)
        self._last_mode_sig: tuple = ()
        self._last_logged_date: date | None = None
        self._ptz_ref_image: bytes | None = None
        self._last_successful_ptz_capture: datetime | None = None
        self._stall_alert_sent: bool = False
        self._stall_ref_time: datetime = self.started_at
        self._blur_streak: int = 0
        self._blur_since: datetime | None = None        # start på sammenhengende uskarphet
        self._recover_streak: int = 0
        self._focus_state: str = "ok"                    # ok | uskarp | uskarp_urettet
        self._jiggle_times: list[datetime] = []          # zoom-jiggle i dag
        self._next_jiggle_at: datetime | None = None
        self._last_sharpness: float | None = None
        self._last_visibility: float | None = None
        self._last_focus_check: datetime | None = None
        self._focus_inactive_reason: str | None = None  # settes av siste fokussjekk
        self._last_skip: datetime | None = None         # siste planlagte hopp (PTZ i manuell posisjon)
        self._skip_reason: str | None = None

    def _mode_sig(self) -> tuple:
        """Fingeravtrykk av feltene som påvirker modus-logging."""
        return (
            self.cfg.get("mode", "continuous"),
            self.cfg.get("save_daylight_only", False),
            self.cfg.get("latitude"),
            self.cfg.get("longitude"),
            self.cfg.get("sunrise_offset_minutes", 0),
            self.cfg.get("sunset_offset_minutes", 0),
        )

    def log_mode(self, force: bool = False):
        now = local_now(self.tz)
        today = now.date()
        sig = self._mode_sig()
        date_changed = today != self._last_logged_date
        if not force and sig == self._last_mode_sig and not date_changed:
            return
        self._last_mode_sig = sig
        self._last_logged_date = today

        mode = self.cfg.get("mode", "continuous")
        sunrise, sunset = self._sun_times(now)

        if mode == "sunrise_to_sunset":
            if sunrise and sunset:
                sr_off = self.cfg.get("sunrise_offset_minutes", 0)
                ss_off = self.cfg.get("sunset_offset_minutes", 0)
                self.log.info(
                    "Modus: soloppgang til solnedgang — "
                    "opptak fra %s (offset %+d min) til %s (offset %+d min)",
                    sunrise.strftime("%H:%M"), sr_off,
                    sunset.strftime("%H:%M"), ss_off,
                )
            else:
                self.log.info("Modus: soloppgang til solnedgang — mangler koordinater")
        else:
            self.log.info("Modus: kontinuerlig")

        if self.cfg.get("save_daylight_only", False):
            self.log.info("Kun dagslysbilder lagres (save_daylight_only: true)")

        if sunset:
            self.log.info(
                "Dagstimelapse lages etter solnedgang: %s (offset %+d min) — dato: %s",
                sunset.strftime("%H:%M"), self.cfg.get("sunset_offset_minutes", 0),
                today.isoformat(),
            )
        else:
            self.log.info("Dagstimelapse: solnedgang ikke tilgjengelig ��� mangler koordinater")

    def health(self) -> dict:
        now = local_now(self.tz)
        interval = self.cfg.get("interval_minutes", 5)

        # Et planlagt hopp (PTZ i manuell posisjon) er aktivitet, ikke feil.
        last_activity = self.last_capture
        if self._last_skip is not None and (last_activity is None or self._last_skip > last_activity):
            last_activity = self._last_skip
        paused = (
            self._last_skip is not None
            and (self.last_capture is None or self._last_skip > self.last_capture)
        )

        if last_activity is None:
            elapsed_since_start = (now - self.started_at).total_seconds() / 60
            status = "error" if elapsed_since_start > interval * 2 else "starting"
        else:
            overdue_min = (now - last_activity).total_seconds() / 60
            if self.consecutive_failures >= 3 or overdue_min > interval * 5:
                status = "error"
            elif self.consecutive_failures >= 1 or overdue_min > interval * 2:
                status = "warning"
            elif paused:
                status = "paused"
            else:
                status = "ok"

        d = {
            "name": self.name,
            "status": status,
            "paused_reason": self._skip_reason if status == "paused" else None,
            "last_capture": self.last_capture.isoformat() if self.last_capture else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_captures": self.total_captures,
            "interval_minutes": interval,
            "mode": self.cfg.get("mode", "continuous"),
        }
        if self.cfg.get("focus", {}).get("enabled", False):
            d["focus_status"] = self._focus_status(now)
            d["focus_detail"] = {
                "state": self._focus_state,
                "blur_since": self._blur_since.isoformat() if self._blur_since else None,
                "blur_minutes": round((now - self._blur_since).total_seconds() / 60) if self._blur_since else 0,
                "jiggles_today": len([t for t in self._jiggle_times if t.date() == now.date()]),
                "next_jiggle_at": self._next_jiggle_at.isoformat() if self._next_jiggle_at else None,
                "sharpness": self._last_sharpness,
                "visibility": self._last_visibility,
                "last_check": self._last_focus_check.isoformat() if self._last_focus_check else None,
            }

        fc = self.cfg.get("focus", {})
        d["settings"] = {
            "save_daylight_only": self.cfg.get("save_daylight_only", False),
            "ptz":        self.cfg.get("ptz", {}).get("preset_id") is not None,
            "timelapse":  self.cfg.get("timelapse", {}).get("enabled", False),
            "ftp":        self.cfg.get("ftp", {}).get("enabled", False),
            "logging":    self.cfg.get("logging", {}).get("enabled", False),
            "stall_alert": self.cfg.get("stall_alert", {}).get("enabled", False),
            "stall_minutes": self.cfg.get("stall_alert", {}).get("minutes"),
            "focus":      fc.get("enabled", False),
            "focus_alert_only": fc.get("alert_only", True),
            "focus_unfixed_after": fc.get("unfixed_after_minutes", 60),
            "focus_interval":   fc.get("interval_minutes"),
            "focus_min_elevation": fc.get("min_sun_elevation", 0),
        }
        return d

    def _focus_status(self, now: datetime) -> str:
        """Fokusstatus for health/dashbord: ok | uskarp | inaktiv (<årsak>)."""
        if not self._is_daylight(now):
            return "inaktiv (natt)"
        reason = self._focus_inactive_reason
        if reason:
            return f"inaktiv ({reason})"
        return {"ok": "ok", "uskarp": "uskarp", "uskarp_urettet": "uskarp (ikke rettet)"}[self._focus_state]

    def _sun_times(self, now: datetime):
        """Returns (sunrise, sunset) or (None, None) if no coordinates configured."""
        if "latitude" not in self.cfg or "longitude" not in self.cfg:
            return None, None
        return get_sun_times(
            lat=self.cfg["latitude"],
            lon=self.cfg["longitude"],
            date_obj=now.date(),
            timezone_str=self.tz,
            sunrise_offset=self.cfg.get("sunrise_offset_minutes", 0),
            sunset_offset=self.cfg.get("sunset_offset_minutes", 0),
        )

    def _sun_position(self, now: datetime):
        """Returns (elevation_deg, azimuth_deg) or (None, None) if no coordinates."""
        if "latitude" not in self.cfg or "longitude" not in self.cfg:
            return None, None
        loc = LocationInfo(latitude=self.cfg["latitude"], longitude=self.cfg["longitude"],
                           timezone=self.tz)
        elev = elevation(loc.observer, dateandtime=now)
        azim = azimuth(loc.observer, dateandtime=now)
        return elev, azim

    def _is_active_time(self, now):
        mode = self.cfg.get("mode", "continuous")
        if mode != "sunrise_to_sunset":
            return True
        _, sunset = self._sun_times(now)
        sunrise, _ = self._sun_times(now)
        if sunrise is None:
            return True
        return sunrise <= now <= sunset

    def _is_daylight(self, now):
        sunrise, sunset = self._sun_times(now)
        if sunrise is None:
            return True
        return sunrise <= now <= sunset

    def due(self, now):
        interval = self.cfg.get("interval_minutes", 5)
        ref = self._last_run_time if self._last_run_time is not None else self.last_capture
        if ref is not None:
            elapsed_min = (now - ref).total_seconds() / 60
            if elapsed_min < interval:
                return False
        return self._is_active_time(now)

    def run(self):
        now = local_now(self.tz)
        self._last_run_time = now
        try:
            ptz = self.cfg.get("ptz", {})
            if ptz.get("preset_id") is not None:
                cam = self.cfg["camera"]
                override_minutes = ptz.get("override_minutes", 10)

                if override_minutes:
                    overdue = (
                        self._last_successful_ptz_capture is None or
                        (now - self._last_successful_ptz_capture).total_seconds() > override_minutes * 60
                    )
                else:
                    overdue = self._last_successful_ptz_capture is None  # kun første gang

                if (ptz.get("image_check", False) and not overdue
                        and self._ptz_ref_image is not None):
                    try:
                        probe = fetch_image(self.cfg["url"])
                        sim_threshold = ptz.get("image_similarity_threshold", 0.90)
                        similar, corr = images_similar(
                            self._ptz_ref_image, probe, sim_threshold,
                            min_std=ptz.get("image_check_min_std", 8.0),
                        )
                        if not similar:
                            self.log.info(
                                "PTZ i manuell posisjon (bildeforskjell) — hopper over "
                                "(Pearson=%.4f, terskel=%.2f)",
                                corr, sim_threshold,
                            )
                            self._last_skip = now
                            self._skip_reason = "manuell posisjon"
                            return
                        if corr != corr:  # nan = for mørkt til å bedømme
                            self.log.debug("Bildesjekk: for mørkt til å bedømme, kjører preset")
                        else:
                            self.log.debug("Bildesjekk OK (Pearson=%.4f, terskel=%.2f)", corr, sim_threshold)
                    except Exception as exc:
                        self.log.debug("Probe-bildesjekk feilet, fortsetter: %s", redact_secrets(str(exc)))

                try:
                    goto_ptz_preset(cam, ptz["preset_id"])
                    settle = ptz.get("settle_seconds", 2)
                    if settle > 0:
                        time.sleep(settle)
                except Exception as exc:
                    self.log.warning("PTZ preset-kall feilet: %s", exc)

            self.log.info("Fetching image...")
            image_bytes = fetch_image(self.cfg["url"])
            raw_image_bytes = image_bytes  # bevart for PTZ-referanse (uten tidsstempel)

            # Fokussjekken setter kun status/varsler — bildet lagres uansett.
            if self.cfg.get("focus", {}).get("enabled", False) and self._is_daylight(now):
                try:
                    self._run_focus_check(image_bytes, now)
                except Exception as exc:
                    self.log.warning("Fokussjekk feilet, fortsetter opptak: %s", exc)

            ts_cfg = self.cfg.get("timestamp", {})
            ts_str = now.strftime(ts_cfg.get("date_format", "%d.%m.%Y %H:%M:%S"))
            image_bytes = add_timestamp(
                image_bytes,
                ts_str,
                font_size=ts_cfg.get("font_size", 28),
                position_h=ts_cfg.get("horizontal", "left"),
                position_v=ts_cfg.get("vertical", "bottom"),
            )

            save_daily = not (self.cfg.get("save_daylight_only", False) and not self._is_daylight(now))
            save_image(image_bytes, self.cfg["root_folder"], now, save_daily=save_daily)
            if save_daily:
                self.log.info("Saved — %s", self.cfg["root_folder"])
            else:
                self.log.debug("Utenfor dagslys — kun latest.jpg oppdatert")

            ftp = self.cfg.get("ftp", {})
            if ftp.get("enabled", False):
                try:
                    ftp_upload_bytes(image_bytes, ftp, ftp.get("latest_filename", "latest.jpg"))
                    self.log.info("FTP upload OK")
                except Exception as exc:
                    self.log.error("FTP upload failed: %s", exc)


            self.last_capture = now
            self.last_error = None
            self.consecutive_failures = 0
            self.total_captures += 1
            if self.cfg.get("ptz", {}).get("preset_id") is not None:
                self._last_successful_ptz_capture = now
                if self.cfg["ptz"].get("image_check", False):
                    self._ptz_ref_image = raw_image_bytes

        except Exception as exc:
            self.consecutive_failures += 1
            self.last_error = redact_secrets(str(exc))
            self.log.error("Capture failed (%d in a row): %s", self.consecutive_failures, self.last_error)

    def check_timelapse(self, now: datetime):
        _, sunset = self._sun_times(now)
        save_daylight_only = self.cfg.get("save_daylight_only", False)
        self.timelapse.check(now, sunset,
                             save_daylight_only=save_daylight_only,
                             is_daylight=self._is_daylight(now))

    def check_dark_cleanup(self, now: datetime):
        self.dark_cleaner.cam_cfg = self.cfg
        self.dark_cleaner.check(now)

    def check_stall_alert(self, now: datetime, global_cfg: dict):
        sa = self.cfg.get("stall_alert", {})
        if not sa.get("enabled", False):
            return
        po = global_cfg.get("pushover", {})
        if not po.get("enabled", True) or not po.get("token") or not po.get("user"):
            return

        # Ikke varsle utenfor aktiv tid (natt osv.) — reset referansetid så vi ikke
        # slår alarm umiddelbart ved soloppgang etter en natt uten bilder.
        is_active = self._is_active_time(now) and (
            not self.cfg.get("save_daylight_only", False) or self._is_daylight(now)
        )
        if not is_active:
            self._stall_alert_sent = False
            self._stall_ref_time = now
            return

        stall_minutes = sa.get("minutes", 10)
        ref = max(
            self.last_capture if self.last_capture is not None else self.started_at,
            self._last_skip if self._last_skip is not None else self.started_at,  # planlagt hopp = aktivitet
            self._stall_ref_time,
        )
        elapsed_min = (now - ref).total_seconds() / 60

        if elapsed_min >= stall_minutes and not self._stall_alert_sent:
            self._stall_alert_sent = True
            msg = f"Ingen timelapse-bilder på {int(elapsed_min)} minutter"
            send_pushover(po["token"], po["user"], f"{self.name} — Timelapse stanset", msg)
            self.log.warning("Pushover stall-varsel sendt: %s", msg)
        elif elapsed_min < stall_minutes and self._stall_alert_sent:
            self._stall_alert_sent = False
            send_pushover(po["token"], po["user"], f"{self.name} — Timelapse", "Timelapse er tilbake i gang")
            self.log.info("Pushover stall-gjenoppretting sendt")

    def _zoom_jiggle(self):
        fc = self.cfg["focus"]["camera"]
        ip = fc["ip"]
        token = None
        try:
            login_payload = [{"cmd": "Login", "action": 0, "param": {
                "User": {"userName": fc["username"], "password": fc["password"]}
            }}]
            token = requests.post(
                f"http://{ip}/api.cgi?cmd=Login&token=null",
                json=login_payload, timeout=5,
            ).json()[0]["value"]["Token"]["name"]

            zoom_normal = self.cfg["focus"]["zoom_normal"]
            zoom_out = self.cfg["focus"]["zoom_out"]
            channel = fc.get("channel", 0)
            self.log.warning("ZOOM JIGGLE — ut til %d, tilbake til %d", zoom_out, zoom_normal)

            def sett_zoom(pos):
                requests.post(
                    f"http://{ip}/api.cgi?token={token}",
                    json=[{"cmd": "StartZoomFocus", "action": 0, "param": {
                        "ZoomFocus": {"channel": channel, "op": "ZoomPos", "pos": pos}
                    }}],
                    timeout=5,
                )

            sett_zoom(zoom_out)
            time.sleep(3)
            sett_zoom(zoom_normal)
            time.sleep(3)
            self.log.warning("ZOOM JIGGLE FERDIG")
        except Exception as exc:
            self.log.error("Feil ved zoom-jiggle: %s", exc)
        finally:
            if token:
                try:
                    requests.post(
                        f"http://{ip}/api.cgi?cmd=Logout&token={token}",
                        json=[{"cmd": "Logout", "action": 0, "param": {}}],
                        timeout=5,
                    )
                except Exception:
                    pass

    def _focus_inactive(self, now: datetime, reason: str, logmsg: str, *args):
        """Marker fokussjekken som inaktiv (ikke feil) og nullstill streak."""
        self.log.info(logmsg, *args)
        self._blur_streak = 0
        self._last_focus_check = now
        self._focus_inactive_reason = reason

    def _run_focus_check(self, image_bytes: bytes, now: datetime) -> None:
        """Analyserer bilde for fokus og oppdaterer _focus_state / _focus_inactive_reason.
        Påvirker ikke selve opptaket.

        Rekkefølge:
        0. Ekskludering: tidsvindu, solhøyde, solsektor, nattmodus (svart/hvitt), lysnivå
        1. Siktsjekk: lav histogram-spredning (stddev) → tåke/dis/motlys → ikke fokusvarsel
        2. Fokussjekk: lav Laplacian-varians i sentrum-ROI → uskarp linse → varsel
        """
        fc = self.cfg["focus"]
        interval_min = fc.get("interval_minutes", 5)

        if self._last_focus_check is not None:
            if (now - self._last_focus_check).total_seconds() / 60 < interval_min:
                return
            if now - self._last_focus_check > timedelta(hours=2):
                self._reset_focus_tracking(now, "pause på over 2 timer")

        # --- Tidsbasert ekskludering (kjente problemvinduer, f.eks. lav sol) ---
        for window in fc.get("exclude_hours", []):
            try:
                h_start, m_start = map(int, window["start"].split(":"))
                h_end, m_end = map(int, window["end"].split(":"))
                t = now.hour * 60 + now.minute
                if h_start * 60 + m_start <= t < h_end * 60 + m_end:
                    self._focus_inactive(
                        now, f"tidsvindu {window['start']}–{window['end']}",
                        "Fokus: ekskludert tidsvindu %s–%s, hopper over sjekk",
                        window["start"], window["end"],
                    )
                    return
            except (KeyError, ValueError):
                pass

        # --- Solhøyde-ekskludering (symmetrisk morgen og kveld) ---
        # Rett etter soloppgang / før solnedgang gir lavt lys, tåke og nattmodus
        # lave skarphetsverdier uten at linsen er ute av fokus.
        min_elev = fc.get("min_sun_elevation", 0)
        if min_elev:
            elev, _ = self._sun_position(now)
            if elev is not None and elev < min_elev:
                self._focus_inactive(
                    now, f"sol {elev:.1f}° < {min_elev:g}°",
                    "Fokus: sol for lav (høyde=%.1f° < %g°), hopper over sjekk", elev, min_elev,
                )
                return

        # --- Solnedgang-ekskludering (eldre, minuttbasert; foretrekk min_sun_elevation) ---
        exclude_before_sunset = fc.get("exclude_before_sunset_minutes", 0)
        if exclude_before_sunset > 0:
            _, sunset = self._sun_times(now)
            if sunset is not None:
                mins_to_sunset = (sunset - now).total_seconds() / 60
                if 0 <= mins_to_sunset <= exclude_before_sunset:
                    self._focus_inactive(
                        now, f"solnedgang {sunset.strftime('%H:%M')}",
                        "Fokus: solnedgang-vindu (solnedgang %s, %.0f min igjen), hopper over sjekk",
                        sunset.strftime("%H:%M"), mins_to_sunset,
                    )
                    return

        # --- Solposisjon-ekskludering (azimut + høyde) ---
        sun_az = fc.get("exclude_sun_azimuth", {})
        if sun_az:
            az_from = sun_az.get("azimuth_from")
            az_to = sun_az.get("azimuth_to")
            min_elev = sun_az.get("min_elevation", 0)
            if az_from is not None and az_to is not None:
                elev, azim = self._sun_position(now)
                if elev is not None and elev >= min_elev:
                    in_sector = (az_from <= azim <= az_to) if az_from <= az_to else (azim >= az_from or azim <= az_to)
                    if in_sector:
                        self._focus_inactive(
                            now, f"motlys azimut {azim:.0f}°",
                            "Fokus: sol i ekskludert sektor (azimut=%.0f°, høyde=%.1f°), hopper over sjekk",
                            azim, elev,
                        )
                        return

        rgb = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"), dtype=float)
        arr = rgb.mean(axis=2)
        lysnivaa = float(arr.mean())

        # --- Nattmodus (svart/hvitt, IR) ---
        # Kameraets forsterkning holder lysnivået oppe om natten, så light_threshold
        # fanger det ikke. Fargemetning ≈ 0 er derimot en sikker markør for IR-modus.
        fargemetning = float(np.abs(rgb[..., 0] - rgb[..., 1]).mean()
                             + np.abs(rgb[..., 1] - rgb[..., 2]).mean())
        if fargemetning < fc.get("colour_threshold", 2.0):
            self._focus_inactive(
                now, "nattmodus",
                "Fokus: NATTMODUS (svart/hvitt) — fargemetning=%.2f, lysnivaa=%.1f, hopper over sjekk",
                fargemetning, lysnivaa,
            )
            return

        if lysnivaa < fc.get("light_threshold", 30.0):
            self._focus_inactive(
                now, "natt",
                "Fokus: NATT — lysnivaa=%.1f, hopper over sjekk", lysnivaa,
            )
            return

        # --- Steg 1: Siktsjekk via histogram-spredning ---
        # Lav stddev betyr uniformt/flatt bilde → tåke, dis, kondens, motlys
        hist_stddev = float(arr.std())
        visibility_threshold = fc.get("visibility_threshold", 0.0)
        if visibility_threshold > 0 and hist_stddev < visibility_threshold:
            self._focus_inactive(
                now, "dårlig sikt",
                "Fokus: DÅRLIG SIKT — histogram-stddev=%.1f < %.1f (tåke/dis/motlys), "
                "hopper over fokussjekk",
                hist_stddev, visibility_threshold,
            )
            return

        # --- Steg 2: Fokussjekk på sentrum-ROI ---
        roi_frac = fc.get("roi_fraction", 1.0)
        if roi_frac < 1.0:
            h, w = arr.shape
            margin_y = int(h * (1 - roi_frac) / 2)
            margin_x = int(w * (1 - roi_frac) / 2)
            roi = arr[margin_y: h - margin_y, margin_x: w - margin_x]
        else:
            roi = arr

        lap = (np.roll(roi, 1, 0) + np.roll(roi, -1, 0) +
               np.roll(roi, 1, 1) + np.roll(roi, -1, 1) - 4 * roi)
        # np.roll bretter kantene rundt, så ytterste rad/kolonne sammenligner venstre mot høyre kant
        # og la 5–13 til skarpheten (tåkebilder målte 13 i stedet for 1). Bare det indre teller.
        varians = float(lap[1:-1, 1:-1].var())
        self._last_focus_check = now
        self._focus_inactive_reason = None
        self._last_sharpness = round(varians, 1)
        self._last_visibility = round(hist_stddev, 1)
        thr = fc.get("focus_threshold", 45.0)
        alert_only = fc.get("alert_only", True)

        if varians < thr:
            self._blur_streak += 1
            self._recover_streak = 0
            if self._blur_since is None:
                self._blur_since = now
            _BLUR_SEEN[self.name] = now

            # --- Kryss-kamera: flere kameraer uskarpe samtidig er vær, ikke linse ---
            window = fc.get("weather_window_minutes", 15)
            others = [n for n, t in _BLUR_SEEN.items()
                      if n != self.name and (now - t) <= timedelta(minutes=window)]
            if window > 0 and others:
                self._focus_inactive_reason = "dårlig sikt (flere kameraer)"
                self.log.info(
                    "Fokus: USKARP (skarphet=%.1f, sikt-stddev=%.1f), men %s er også uskarpt — regnes som vær",
                    varians, hist_stddev, ", ".join(others),
                )
                return

            dur_min = (now - self._blur_since).total_seconds() / 60
            confirm = fc.get("confirm_cycles", 2)
            confirm_min = fc.get("confirm_minutes", 15)
            self.log.warning(
                "Fokus: USKARP — skarphet=%.1f, lysnivaa=%.1f, sikt-stddev=%.1f, fargemetning=%.1f "
                "(syklus %d/%d, %.0f/%d min)",
                varians, lysnivaa, hist_stddev, fargemetning, self._blur_streak, confirm, dur_min, confirm_min,
            )
            if self._blur_streak < confirm or dur_min < confirm_min:
                return

            if self._focus_state == "ok":
                self._focus_state = "uskarp"
                self.log.warning(
                    "Fokus: uskarphet bekreftet etter %.0f min — %s",
                    dur_min, "kun overvåking (alert_only)" if alert_only else "prøver zoom-jiggle",
                )
            if not alert_only:
                self._maybe_jiggle(now, fc)

            unfixed_after = fc.get("unfixed_after_minutes", 60)
            if self._focus_state == "uskarp" and dur_min >= unfixed_after:
                self._focus_state = "uskarp_urettet"
                n_jiggle = len([t for t in self._jiggle_times if t.date() == now.date()])
                msg = (f"Kamera ute av fokus i {dur_min:.0f} min og lar seg ikke rette — "
                       f"skarphet={varians:.1f}, {n_jiggle} zoom-jiggle i dag")
                self.log.error("Fokus: IKKE RETTET — %s", msg)
                self._notify_focus(msg)
            return

        # --- Skarpt nok ---
        if self._focus_state == "ok":
            self._blur_streak = 0
            self._blur_since = None
            self.log.info(
                "Fokus: OK — skarphet=%.1f, lysnivaa=%.1f, sikt-stddev=%.1f, fargemetning=%.1f",
                varians, lysnivaa, hist_stddev, fargemetning,
            )
            return

        # Hysterese: tilbake først når skarpheten er klart over terskelen, flere ganger på rad
        recover_at = thr * fc.get("recover_factor", 2.0)
        if varians < recover_at:
            self._recover_streak = 0
            self.log.info(
                "Fokus: SVAKT — skarphet=%.1f er over %.0f men under %.0f, holder status «%s»",
                varians, thr, recover_at, self._focus_status(now),
            )
            return
        self._recover_streak += 1
        need = fc.get("recover_cycles", 2)
        if self._recover_streak < need:
            self.log.info("Fokus: bedring — skarphet=%.1f (%d/%d)", varians, self._recover_streak, need)
            return
        dur_min = (now - self._blur_since).total_seconds() / 60 if self._blur_since else 0
        n_jiggle = len([t for t in self._jiggle_times if t.date() == now.date()])
        was_unfixed = self._focus_state == "uskarp_urettet"
        self.log.warning(
            "Fokus: TILBAKE — skarphet=%.1f etter %.0f min uskarpt, %d zoom-jiggle i dag",
            varians, dur_min, n_jiggle,
        )
        if was_unfixed:
            self._notify_focus(f"Kamera tilbake i fokus — skarphet={varians:.1f} etter {dur_min:.0f} min")
        self._focus_state = "ok"
        self._blur_streak = 0
        self._blur_since = None
        self._recover_streak = 0
        self._next_jiggle_at = None

    def _reset_focus_tracking(self, now: datetime, why: str):
        """Nullstill fokussporingen (ny dag / lang pause): gårsdagens uskarphet skal vurderes på nytt."""
        if self._focus_state != "ok" or self._blur_since is not None:
            self.log.info("Fokus: nullstiller sporing (%s), status var «%s»", why, self._focus_state)
        self._focus_state = "ok"
        self._blur_streak = 0
        self._blur_since = None
        self._recover_streak = 0
        self._next_jiggle_at = None
        self._jiggle_times = [t for t in self._jiggle_times if t.date() == now.date()]

    def _maybe_jiggle(self, now: datetime, fc: dict):
        """Zoom-jiggle med økende ventetid og tak per dag. Kalles bare ved bekreftet uskarphet."""
        today = [t for t in self._jiggle_times if t.date() == now.date()]
        self._jiggle_times = today
        max_per_day = fc.get("jiggle_max_per_day", 4)
        if self._next_jiggle_at and now < self._next_jiggle_at:
            return
        if len(today) >= max_per_day:
            if self._next_jiggle_at is not None:
                self.log.warning("Fokus: %d zoom-jiggle i dag, taket er nådd — ingen flere forsøk", len(today))
                self._next_jiggle_at = None
            return
        self._zoom_jiggle()
        self._jiggle_times.append(now)
        wait = fc.get("jiggle_wait_minutes", 10) * (2 ** (len(self._jiggle_times) - 1))  # 10, 20, 40, 80 min
        self._next_jiggle_at = now + timedelta(minutes=wait)
        self.log.warning(
            "Fokus: zoom-jiggle %d/%d utført, ny vurdering tidligst om %d min",
            len(self._jiggle_times), max_per_day, wait,
        )

    def _notify_focus(self, message: str):
        """Varsel om fokus som ikke lot seg rette (og når det er tilbake). Kan slås av med focus.notify: false."""
        if not self.cfg.get("focus", {}).get("notify", True):
            return
        po = self._global_cfg.get("pushover", {})
        if po.get("enabled", True) and po.get("token") and po.get("user"):
            send_pushover(po["token"], po["user"], f"{self.name} — Fokus", message)


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #1a1a2e; color: #e0e0e0; font-family: system-ui, -apple-system, sans-serif; font-size: 14px; }
header { display: flex; align-items: center; gap: 12px; padding: 14px 20px; background: #0f3460; border-bottom: 1px solid #2a2a4a; flex-wrap: wrap; }
h1 { font-size: 17px; font-weight: 600; }
#last-updated { margin-left: auto; font-size: 12px; color: #aaa; }
.camera-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; padding: 20px; }
.camera-card { background: #16213e; border-radius: 8px; overflow: hidden; border: 1px solid #2a2a4a; }
.card-header { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; background: #0f3460; }
.cam-name { font-weight: 600; font-size: 15px; }
.preview-wrapper { background: #111; height: 220px; overflow: hidden; }
.preview-img { width: 100%; height: 100%; object-fit: cover; display: block; }
.no-image { color: #555; font-size: 13px; padding: 24px; text-align: center; }
.card-meta { padding: 10px 14px; display: flex; flex-direction: column; gap: 5px; }
.meta-row { display: flex; justify-content: space-between; gap: 8px; }
.label { color: #888; }
.value { color: #ddd; text-align: right; }
.focus-ok      { color: #2ecc71; }
.focus-uskarp  { color: #e74c3c; }
.focus-svak    { color: #f1c40f; }
.focus-inaktiv { color: #888; }
.error-msg { margin-top: 4px; padding: 6px 8px; background: #3d1515; border-left: 3px solid #e74c3c; border-radius: 3px; font-size: 12px; color: #ff9999; word-break: break-word; }
.badge { padding: 2px 9px; border-radius: 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
.s-ok      { background: #1a4a2e; color: #2ecc71; }
.s-warning { background: #4a3010; color: #f39c12; }
.s-error   { background: #4a1515; color: #e74c3c; }
.s-starting{ background: #102a4a; color: #3498db; }
.s-paused  { background: #2a2a4a; color: #a0a0c0; }
.overall-badge { font-size: 13px; }
.settings { display: flex; flex-wrap: wrap; gap: 5px; padding: 8px 14px 10px; border-top: 1px solid #2a2a4a; }
.tag { padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 500; }
.tag-on  { background: #1a3a4a; color: #5bc8f5; }
.tag-off { background: #222; color: #555; }
"""

_DASHBOARD_JS = """
function buildTags(s) {
  function tag(label, on, title) {
    return '<span class="tag ' + (on ? 'tag-on' : 'tag-off') + '" title="' + (title || '') + '">' + label + '</span>';
  }
  var t = '';
  t += tag('dagslys', s.save_daylight_only, 'Kun dagslysbilder lagres (soloppgang til solnedgang)');
  t += tag('PTZ', s.ptz, 'Kamera beveges til fast preset-posisjon før hvert bilde');
  t += tag('timelapse', s.timelapse, 'Genererer rullende timelapse-video hver time');
  t += tag('FTP', s.ftp, 'Siste bilde lastes opp via FTP etter hvert opptak');
  if (s.focus) {
    var fmode = (s.focus_alert_only ? 'Kun overvåking ved uskarphet' : 'Kjører zoom-jiggle for å gjenopprette fokus') + '. Varsler først etter ' + (s.focus_unfixed_after || 60) + ' min uten bedring';
    t += tag('fokus ' + (s.focus_alert_only ? 'alert' : 'jiggle') + ' ' + s.focus_interval + 'min', true, 'Fokussjekk hvert ' + s.focus_interval + ' min. ' + fmode + '. Inaktiv om natten, i nattmodus (svart/hvitt) og når solen står under ' + (s.focus_min_elevation || 0) + '°.');
  } else {
    t += tag('fokus', false, 'Fokussjekk er ikke aktivert');
  }
  t += tag('stall ' + s.stall_minutes + 'min', s.stall_alert, 'Pushover-varsel hvis ingen bilder på ' + s.stall_minutes + ' minutter');
  t += tag('logging', s.logging, 'Logger til daglige filer på disk');
  return t;
}
function formatAgo(iso) {
  if (!iso) return '\u2014';
  var diff = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (diff < 60) return 'for ' + diff + ' sek siden';
  if (diff < 3600) return 'for ' + Math.floor(diff / 60) + ' min siden';
  return 'for ' + Math.floor(diff / 3600) + ' t siden';
}
function setStatus(el, s) {
  el.className = el.className.replace(/\\bs-\\w+\\b/g, '').trim() + ' s-' + s;
  el.textContent = s.toUpperCase();
}
async function refresh() {
  try {
    var r = await fetch('/health');
    var data = await r.json();
    setStatus(document.querySelector('.overall-badge'), data.status);
    var ts = Date.now();
    data.cameras.forEach(function(cam) {
      var card = document.querySelector('[data-camera="' + cam.name + '"]');
      if (!card) return;
      setStatus(card.querySelector('.cam-badge'), cam.status);
      card.querySelector('.val-capture').textContent = formatAgo(cam.last_capture);
      card.querySelector('.val-captures').textContent = cam.total_captures;
      var err = card.querySelector('.error-msg');
      if (cam.last_error) { err.textContent = cam.last_error; err.style.display = 'block'; }
      else { err.style.display = 'none'; }
      var img = card.querySelector('.preview-img');
      if (img) img.src = '/camera/' + cam.name + '/latest.jpg?t=' + ts;
      var sw = card.querySelector('.settings');
      if (sw && cam.settings) sw.innerHTML = buildTags(cam.settings);
      var frow = card.querySelector('.focus-row');
      if (frow) {
        if (cam.focus_status) {
          var cls = cam.focus_status === 'ok' ? 'focus-ok'
                  : cam.focus_status.indexOf('ikke rettet') >= 0 ? 'focus-uskarp'
                  : cam.focus_status === 'uskarp' ? 'focus-svak'
                  : 'focus-inaktiv';
          frow.querySelector('.val-focus').className = 'value val-focus ' + cls;
          frow.querySelector('.val-focus').textContent = cam.focus_status;
          frow.style.display = '';
        } else {
          frow.style.display = 'none';
        }
      }
    });
    document.getElementById('last-updated').textContent =
      'Oppdatert: ' + new Date().toLocaleTimeString('nb-NO');
  } catch(e) {
    document.getElementById('last-updated').textContent =
      'Feil: ' + new Date().toLocaleTimeString('nb-NO');
  }
}
refresh();
setInterval(refresh, 30000);
"""


def _format_capture_time(iso_str) -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_str))
        diff = int((datetime.now(dt.tzinfo) - dt).total_seconds())
        if diff < 60:
            return f"for {diff} sek siden"
        if diff < 3600:
            return f"for {diff // 60} min siden"
        return f"for {diff // 3600} t siden"
    except Exception:
        return str(iso_str)


def _build_dashboard_html(overall: str, cameras: list) -> str:
    cards = ""
    for cam in cameras:
        name = html.escape(cam["name"])
        status = cam["status"]
        last_cap = _format_capture_time(cam.get("last_capture"))
        total = cam.get("total_captures", 0)
        interval = cam.get("interval_minutes", "?")
        mode = html.escape(cam.get("mode", "?"))
        err_text = html.escape(cam.get("last_error") or "")
        err_display = "block" if cam.get("last_error") else "none"
        cards += (
            f'\n      <div class="camera-card" data-camera="{name}">'
            f'\n        <div class="card-header">'
            f'\n          <span class="cam-name">{name}</span>'
            f'\n          <span class="badge cam-badge s-{status}">{status.upper()}</span>'
            f'\n        </div>'
            f'\n        <div class="preview-wrapper">'
            f'\n          <img class="preview-img" src="/camera/{name}/latest.jpg" alt="{name}"'
            f'\n               onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">'
            f'\n          <div class="no-image" style="display:none">Bilde ikke tilgjengelig</div>'
            f'\n        </div>'
            f'\n        <div class="card-meta">'
            f'\n          <div class="meta-row"><span class="label">Siste bilde</span><span class="value val-capture">{last_cap}</span></div>'
            f'\n          <div class="meta-row"><span class="label">Captures</span><span class="value val-captures">{total}</span></div>'
            f'\n          <div class="meta-row"><span class="label">Intervall</span><span class="value">{interval} min</span></div>'
            f'\n          <div class="meta-row"><span class="label">Modus</span><span class="value">{mode}</span></div>'
            f'\n          <div class="meta-row focus-row"><span class="label">Fokus</span><span class="value val-focus">—</span></div>'
            f'\n          <div class="error-msg" style="display:{err_display}">{err_text}</div>'
            f'\n        </div>'
            f'\n        <div class="settings"></div>'
            f'\n      </div>'
        )
    return (
        '<!DOCTYPE html>\n<html lang="no">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>ipcam-timelapse</title>\n"
        f"<style>{_DASHBOARD_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        "  <h1>ipcam-timelapse</h1>\n"
        f'  <div class="badge overall-badge s-{overall}">{overall.upper()}</div>\n'
        '  <span id="last-updated">Laster...</span>\n'
        "</header>\n"
        f'<div class="camera-grid">{cards}\n</div>\n'
        f"<script>{_DASHBOARD_JS}</script>\n"
        "</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# Health check HTTP server
# ---------------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    workers: list[CameraWorker] = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]  # strip query string
        if path == "/":
            self._serve_dashboard()
        elif path == "/health":
            self._serve_health_json()
        elif path.startswith("/camera/") and path.endswith("/latest.jpg"):
            self._serve_camera_image(path)
        elif path.startswith("/health/"):
            self._serve_camera_health(path)
        else:
            self.send_response(404)
            self.end_headers()

    def _overall_status(self):
        statuses = [w.health() for w in self.workers]
        overall = "ok"
        for s in statuses:
            if s["status"] == "error":
                overall = "error"
                break
            if s["status"] == "warning":
                overall = "warning"
        return overall, statuses

    def _serve_health_json(self):
        overall, camera_statuses = self._overall_status()
        body = json.dumps(
            {"status": overall, "cameras": camera_statuses},
            indent=2,
            default=str,
        ).encode()
        http_status = 200 if overall in ("ok", "starting", "warning", "paused") else 503
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self):
        overall, camera_statuses = self._overall_status()
        body = _build_dashboard_html(overall, camera_statuses).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_camera_health(self, path):
        # /health/{name} | /health/{name}/capture | /health/{name}/ftp | /health/{name}/focus
        parts = path[len("/health/"):].strip("/").split("/")
        cam_name = parts[0]
        check = parts[1] if len(parts) > 1 else None
        worker = next((w for w in self.workers if w.name == cam_name), None)
        if worker is None:
            self._simple(404, "Camera not found")
            return

        h = worker.health()

        if check is None:
            ok = h["status"] in ("ok", "starting", "warning", "paused")
            self._simple(200 if ok else 503, h["status"])

        elif check == "capture":
            ok = h["status"] != "error"
            msg = f"status={h['status']}"
            if h.get("paused_reason"):
                msg += f" ({h['paused_reason']})"
            if h.get("last_error"):
                msg += f", error={h['last_error']}"
            self._simple(200 if ok else 503, msg)

        elif check == "ftp":
            # FTP-feil vises i last_error hvis siste capture feilet pga. FTP
            ftp_enabled = worker.cfg.get("ftp", {}).get("enabled", False)
            if not ftp_enabled:
                self._simple(200, "FTP disabled")
                return
            err = h.get("last_error") or ""
            ftp_error = "ftp" in err.lower() or "upload" in err.lower()
            self._simple(503 if ftp_error else 200, "error: " + err if ftp_error else "ok")

        elif check == "focus":
            focus_enabled = worker.cfg.get("focus", {}).get("enabled", False)
            if not focus_enabled:
                self._simple(200, "Focus disabled")
                return
            focus_status = h.get("focus_status", "ok")
            # inaktiv = natt/vær, ikke feil; «uskarp» håndteres fortsatt (jiggle) og er heller ikke feil ennå
            if focus_status.startswith("uskarp (ikke rettet)"):
                self._simple(503, focus_status)
            else:
                self._simple(200, focus_status)

        else:
            self._simple(404, "Unknown check")

    def _simple(self, code: int, message: str):
        body = message.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_camera_image(self, path):
        parts = path.split("/")  # ['', 'camera', '{name}', 'latest.jpg']
        if len(parts) != 4:
            self.send_response(404)
            self.end_headers()
            return
        cam_name = parts[2]
        worker = next((w for w in self.workers if w.name == cam_name), None)
        if worker is None:
            self.send_response(404)
            self.end_headers()
            return
        image_path = Path(worker.cfg["root_folder"]) / "latest.jpg"
        if not image_path.exists():
            self.send_response(404)
            self.end_headers()
            return
        data = image_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def start_health_server(workers, port=8080):
    HealthHandler.workers = workers
    server = HTTPServer(("", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger("health").info("Health endpoint: http://0.0.0.0:%d/health", port)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    global_cfg = cfg.get("global", {})
    workers = [CameraWorker(c, global_cfg) for c in cfg.get("cameras", [])]

    health_port = global_cfg.get("health_port", 8080)
    start_health_server(workers, port=health_port)

    logging.getLogger().info("Starting timelapse — %d camera(s)", len(workers))
    for w in workers:
        w.log_mode(force=True)

    while True:
        try:
            cfg = load_config()
            global_cfg = cfg.get("global", {})
            cam_cfgs = {c["name"]: c for c in cfg.get("cameras", [])}
            for w in workers:
                if w.name in cam_cfgs:
                    w.cfg = cam_cfgs[w.name]
                    w.log_mode()
        except Exception as exc:
            logging.getLogger().warning("Config reload failed: %s", exc)

        tz = global_cfg.get("timezone", "Europe/Oslo")
        now = local_now(tz)
        for w in workers:
            if w.due(now):
                w.run()
            w.check_timelapse(now)
            w.check_dark_cleanup(now)
            w.check_stall_alert(now, global_cfg)

        # Sov til neste kamera er klar (maks 30s)
        now = local_now(tz)
        sleep_secs = 30.0
        for w in workers:
            ref = w._last_run_time or w.last_capture
            if ref is not None:
                interval_secs = w.cfg.get("interval_minutes", 5) * 60
                secs_until = interval_secs - (now - ref).total_seconds()
                sleep_secs = min(sleep_secs, max(1.0, secs_until))
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
