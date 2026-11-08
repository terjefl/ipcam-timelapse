"""Tester for fokus-bare-kameraer (save_images: false) og snapshot/API over https.

Kjøres med `pytest` fra repo-roten. Ingen nettverkstilgang: requests mockes.
"""

import io
import json
import threading
import urllib.request
from http.server import HTTPServer

import numpy as np
import pytest
from PIL import Image

import timelapse


def _jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8), "RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def sharp_image() -> bytes:
    """Fargerik støy: høy Laplacian-varians, høy fargemetning, lysnivå rundt 128."""
    rng = np.random.default_rng(1)
    return _jpeg(rng.integers(0, 256, size=(120, 160, 3)))


def blurry_image() -> bytes:
    """Jevn fargegradient: lysnivå og fargemetning godt over tersklene, men nesten ingen kanter."""
    y, x = np.mgrid[0:120, 0:160]
    arr = np.stack([x * 255 / 159, np.full_like(x, 100), y * 255 / 119], axis=2)
    return _jpeg(arr)


def focus_only_cfg(name="CAM-FOKUS", **focus_overrides) -> dict:
    focus = {
        "enabled": True,
        "interval_minutes": 0,       # sjekk ved hvert bilde
        "focus_threshold": 20.0,
        "light_threshold": 30.0,
        "confirm_cycles": 1,
        "confirm_minutes": 0,
        "alert_only": True,
        "visibility_threshold": 0,
        "roi_fraction": 1.0,
        "notify": False,
        "weather_window_minutes": 0,
        "camera": {"ip": "192.0.2.15", "username": "u", "password": "p", "verify_tls": False},
    }
    focus.update(focus_overrides)
    return {
        "name": name,
        "url": "https://192.0.2.15/cgi-bin/api.cgi?cmd=Snap&channel=0&user=u&password=p",
        "verify_tls": False,
        "save_images": False,
        "interval_minutes": 1,
        "focus": focus,
    }


@pytest.fixture(autouse=True)
def _reset_blur_seen():
    timelapse._BLUR_SEEN.clear()
    yield
    timelapse._BLUR_SEEN.clear()


def test_fokus_bare_kamera_lagrer_ikke_til_disk(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kall = {}

    def fake_fetch(url, timeout=15, verify=True):
        kall["verify"] = verify
        return sharp_image()

    monkeypatch.setattr(timelapse, "fetch_image", fake_fetch)
    w = timelapse.CameraWorker(focus_only_cfg(), {"timezone": "Europe/Oslo"})
    w.run()

    assert kall["verify"] is False, "verify_tls: false skal gi verify=False til requests"
    assert list(tmp_path.iterdir()) == [], "ingen filer skal skrives"
    assert w._latest_image == sharp_image()
    assert w.last_capture is not None and w.total_captures == 1
    h = w.health()
    assert h["status"] == "ok"
    assert h["settings"]["save_images"] is False
    assert h["focus_status"] == "ok"
    assert h["focus_detail"]["sharpness"] > 20


def test_fokus_bare_kamera_oppdager_uskarphet(monkeypatch):
    monkeypatch.setattr(timelapse, "fetch_image", lambda url, timeout=15, verify=True: blurry_image())
    w = timelapse.CameraWorker(focus_only_cfg(), {"timezone": "Europe/Oslo"})
    w.run()
    h = w.health()
    assert h["focus_status"] == "uskarp"
    assert h["focus_detail"]["sharpness"] < 20


def test_weather_window_null_holder_kameraet_utenfor(monkeypatch):
    """weather_window_minutes: 0 skal verken bruke eller bidra til værsjekken på tvers."""
    monkeypatch.setattr(timelapse, "fetch_image", lambda url, timeout=15, verify=True: blurry_image())
    utenfor = timelapse.CameraWorker(focus_only_cfg("UTENFOR"), {})
    utenfor.run()
    assert "UTENFOR" not in timelapse._BLUR_SEEN

    med = timelapse.CameraWorker(focus_only_cfg("MED", weather_window_minutes=15), {})
    med.run()
    assert "MED" in timelapse._BLUR_SEEN
    assert med.health()["focus_status"] == "uskarp", "ingen andre i _BLUR_SEEN, så ikke vær"


def test_timelapse_og_opprydding_hoppes_over(monkeypatch):
    cfg = focus_only_cfg()
    cfg["timelapse"] = {"enabled": True}
    cfg["dark_cleanup"] = {"enabled": True, "run_at": "12:00"}
    w = timelapse.CameraWorker(cfg, {})
    kalt = []
    monkeypatch.setattr(w.timelapse, "check", lambda *a, **k: kalt.append("timelapse"))
    monkeypatch.setattr(w.dark_cleaner, "check", lambda *a, **k: kalt.append("dark"))
    now = timelapse.local_now("Europe/Oslo")
    w.check_timelapse(now)
    w.check_dark_cleanup(now)
    assert kalt == []


def test_health_server_gir_latest_jpg_fra_minnet(monkeypatch):
    monkeypatch.setattr(timelapse, "fetch_image", lambda url, timeout=15, verify=True: sharp_image())
    w = timelapse.CameraWorker(focus_only_cfg(), {})
    w.run()

    timelapse.HealthHandler.workers = [w]
    server = HTTPServer(("127.0.0.1", 0), timelapse.HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/camera/CAM-FOKUS/latest.jpg") as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/jpeg"
            assert r.read() == sharp_image()
        with urllib.request.urlopen(f"{base}/health/CAM-FOKUS/focus") as r:
            assert r.status == 200 and r.read() == b"ok"
        with urllib.request.urlopen(f"{base}/health") as r:
            data = json.loads(r.read())
            cam = data["cameras"][0]
            assert cam["last_capture"] is not None
            assert cam["focus_status"] == "ok"
            assert "focus_detail" in cam
            assert cam["settings"]["save_images"] is False
            assert cam["ip"] == "192.0.2.15"
    finally:
        server.shutdown()
        timelapse.HealthHandler.workers = []


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def test_reolink_post_folger_omdirigering_som_post(monkeypatch):
    """Reolink med HTTPS-omdirigering svarer 302 på http; kallet skal gjentas som POST over https."""
    kall = []

    def fake_post(url, json=None, timeout=5, allow_redirects=True, verify=True):
        kall.append((url, allow_redirects, verify))
        if url.startswith("http://"):
            return _Resp(302)
        return _Resp(200, [{"value": {"Token": {"name": "T"}}}])

    monkeypatch.setattr(timelapse.requests, "post", fake_post)
    cam = {"ip": "192.0.2.15", "username": "u", "password": "p", "verify_tls": False}
    assert timelapse.reolink_login(cam) == "T"
    assert [u for u, _, _ in kall] == [
        "http://192.0.2.15/api.cgi?cmd=Login&token=null",
        "https://192.0.2.15/api.cgi?cmd=Login&token=null",
    ]
    assert all(ar is False and v is False for _, ar, v in kall)


def test_reolink_post_https_direkte(monkeypatch):
    kall = []
    monkeypatch.setattr(timelapse.requests, "post",
                        lambda url, **kw: kall.append((url, kw["verify"])) or _Resp(200))
    timelapse.reolink_post({"ip": "192.0.2.15", "https": True}, "token=T", [])
    assert kall == [("https://192.0.2.15/api.cgi?token=T", True)]


def test_zoom_jiggle_bruker_focus_camera(monkeypatch):
    """Jiggle logger inn, setter zoom ut og tilbake, og logger ut, alt via reolink_post."""
    kall = []

    def fake_post(url, json=None, timeout=5, allow_redirects=True, verify=True):
        kall.append((url, json[0]["cmd"], json[0]["param"]))
        return _Resp(200, [{"value": {"Token": {"name": "T"}}}])

    monkeypatch.setattr(timelapse.requests, "post", fake_post)
    monkeypatch.setattr(timelapse.time, "sleep", lambda s: None)
    cfg = focus_only_cfg(alert_only=False, zoom_normal=26, zoom_out=24)
    w = timelapse.CameraWorker(cfg, {})
    w._zoom_jiggle()
    cmds = [c for _, c, _ in kall]
    assert cmds == ["Login", "StartZoomFocus", "StartZoomFocus", "Logout"]
    assert kall[1][2]["ZoomFocus"]["pos"] == 24
    assert kall[2][2]["ZoomFocus"]["pos"] == 26
    assert all(u.startswith("http://192.0.2.15/api.cgi?") for u, _, _ in kall)


def test_camera_ip_fra_config():
    assert timelapse.camera_ip({"focus": {"camera": {"ip": "10.0.0.5"}}, "url": "http://10.0.0.9/x"}) == "10.0.0.5"
    assert timelapse.camera_ip({"camera": {"ip": "10.0.0.6"}}) == "10.0.0.6"
    assert timelapse.camera_ip({"url": "https://10.0.0.7/cgi-bin/api.cgi?cmd=Snap&user=u&password=p"}) == "10.0.0.7"
    assert timelapse.camera_ip({}) is None
