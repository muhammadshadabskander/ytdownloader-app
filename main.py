"""
Video Downloader - Android App (Kivy)
Downloads videos (or audio-only) from YouTube, TikTok, Dailymotion,
Instagram and Facebook.
Supports multiple simultaneous downloads, per-download cancel,
changing/opening the save folder, and a Pro unlock (device-locked code).

All on-screen text is English.
"""

import base64
import hashlib
import json
import os
import urllib.request
import subprocess
import sys
import threading
import uuid
from urllib.parse import quote

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.properties import ListProperty, NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.togglebutton import ToggleButton

ANDROID = True
try:
    from android.permissions import request_permissions, Permission
    from android.storage import primary_external_storage_path
    from jnius import autoclass, cast
except Exception:
    ANDROID = False


# ----------------------------------------------------------------------
# Business settings
# ----------------------------------------------------------------------
# Run `python generate_code.py setup` on YOUR computer once. It prints a
# public key -> paste it here. (The PRIVATE key never goes into the app.)
PUBLIC_KEY_HEX = "e0b1fe74117e1b95b608a4f221df314774b20ea66842350d515371c7c6966c6e"

FREE_DOWNLOAD_LIMIT = 5        # successful downloads allowed in the free version
PRO_PRICE = "Rs 500"           # shown to the customer in the unlock popup

# Optional: stops the free-download counter resetting when a customer clears
# app data or reinstalls. Leave the placeholders to skip this (local-only
# counting, same as before). See the setup steps you were given for how to
# fill these in.
FIREBASE_API_KEY = "AIzaSyCnC7QvuwUp5U4F4SDmOwepNsjbAE865uQ"
FIREBASE_DB_URL = "https://video-downloader-47572-default-rtdb.firebaseio.com"

# Payment / contact details shown in the Pro-unlock popup.
PAYMENT_TITLE = "M Shadab Sikandar"
PAYMENT_EASYPAISA_NUMBER = "0311-8590702"
CONTACT_WHATSAPP_NUMBER = "923118590702"  # country code + number, no leading 0, no symbols


# ---- Ed25519 signature check (pure Python, no extra libraries) ----
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _sha512(s):
    return hashlib.sha512(s).digest()


def _inv(x):
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    C = 2 * P[3] * Q[3] * _D % _P
    D = 2 * P[2] * Q[2] % _P
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    if (P[0] * Q[2] - Q[0] * P[2]) % _P != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % _P != 0:
        return False
    return True


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _h_modq(s):
    return int.from_bytes(_sha512(s), "little") % _Q


def ed25519_verify(public, msg, signature):
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _decompress(public)
        if not A:
            return False
        Rs = signature[:32]
        R = _decompress(Rs)
        if not R:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _Q:
            return False
        h = _h_modq(Rs + public + msg)
        return _equal(_mul(s, _G), _add(R, _mul(h, A)))
    except Exception:
        return False


def verify_unlock_code(device_id, code_text):
    """True only if code_text is a valid signature (made with YOUR private key)
    for exactly this device_id. A code for another phone will not pass."""
    try:
        public = bytes.fromhex(PUBLIC_KEY_HEX)
    except ValueError:
        return False
    clean = "".join(ch for ch in (code_text or "").upper() if ch.isalnum())
    try:
        sig = base64.b32decode(clean + "=" * (-len(clean) % 8))
    except Exception:
        return False
    return ed25519_verify(public, b"PRO1|" + device_id.encode("utf-8"), sig)


class DownloadCancelledError(Exception):
    """Raised from inside a progress_hook to abort an in-progress yt_dlp download."""
    pass


# ----------------------------------------------------------------------
# Theme
# ----------------------------------------------------------------------
BG = (0.06, 0.06, 0.09, 1)
CARD = (0.11, 0.11, 0.15, 1)
FIELD = (0.16, 0.16, 0.21, 1)
BTN = (0.20, 0.20, 0.26, 1)
TEXT = (0.95, 0.95, 0.97, 1)
MUTED = (0.60, 0.60, 0.68, 1)
ACCENT = (0.91, 0.22, 0.25, 1)
GREEN = (0.18, 0.68, 0.38, 1)
RED = (0.90, 0.30, 0.30, 1)

# Colour of the big "Add Download" button (change freely).
ADD_BTN_COLOR = (0.56, 0.32, 0.96, 1)   # purple

# Playlists are Pro-only (otherwise one playlist would bypass the free limit).
PLAYLIST_REQUIRES_PRO = True

PLATFORMS = [
    # key, label, color
    ("youtube", "YouTube", (0.86, 0.13, 0.13, 1)),
    ("tiktok", "TikTok", (0.05, 0.62, 0.64, 1)),
    ("instagram", "Instagram", (0.83, 0.22, 0.55, 1)),
    ("facebook", "Facebook", (0.09, 0.47, 0.95, 1)),
    ("dailymotion", "Dailymotion", (0.05, 0.40, 0.90, 1)),
]
PLATFORM_LABEL = {k: l for k, l, _ in PLATFORMS}
PLATFORM_COLOR = {k: c for k, _, c in PLATFORMS}

URL_HINTS = {
    "youtube": ["youtube.com", "youtu.be"],
    "tiktok": ["tiktok.com"],
    "instagram": ["instagram.com", "instagr.am"],
    "facebook": ["facebook.com", "fb.watch", "fb.com"],
    "dailymotion": ["dailymotion.com", "dai.ly"],
}


def detect_platform(url):
    u = url.lower()
    for key, needles in URL_HINTS.items():
        if any(n in u for n in needles):
            return key
    return None


# ----------------------------------------------------------------------
# Small styled widgets
# ----------------------------------------------------------------------
class Card(BoxLayout):
    """A BoxLayout with a rounded, filled background."""

    def __init__(self, color=CARD, radius=14, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            Color(*color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(radius)])
        self.bind(pos=self._upd, size=self._upd)

    def _upd(self, *_a):
        self._rect.pos = self.pos
        self._rect.size = self.size


class RoundedButton(Button):
    bg_color = ListProperty(list(BTN))
    radius = NumericProperty(dp(10))

    def __init__(self, bg_color=None, **kwargs):
        kwargs["background_normal"] = ""
        kwargs["background_down"] = ""
        kwargs["background_color"] = (0, 0, 0, 0)
        kwargs.setdefault("color", TEXT)
        super().__init__(**kwargs)
        if bg_color:
            self.bg_color = list(bg_color)
        with self.canvas.before:
            self._c = Color(*self.bg_color)
            self._r = RoundedRectangle(pos=self.pos, size=self.size, radius=[self.radius])
        self.bind(pos=self._upd, size=self._upd, bg_color=self._upd,
                  state=self._upd, disabled=self._upd)
        self._upd()

    def _upd(self, *_a):
        r, g, b, a = self.bg_color
        if self.disabled:
            self._c.rgba = (0.22, 0.22, 0.26, 1)
        elif self.state == "down":
            self._c.rgba = (r * 0.75, g * 0.75, b * 0.75, a)
        else:
            self._c.rgba = (r, g, b, a)
        self._r.pos = self.pos
        self._r.size = self.size
        self._r.radius = [self.radius]


class RoundedToggle(ToggleButton):
    def __init__(self, active_color=ACCENT, inactive_color=BTN, **kwargs):
        kwargs["background_normal"] = ""
        kwargs["background_down"] = ""
        kwargs["background_color"] = (0, 0, 0, 0)
        kwargs.setdefault("color", TEXT)
        kwargs.setdefault("allow_no_selection", False)
        super().__init__(**kwargs)
        self.active_color = active_color
        self.inactive_color = inactive_color
        with self.canvas.before:
            self._c = Color(*inactive_color)
            self._r = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
        self.bind(pos=self._upd, size=self._upd, state=self._upd)
        self._upd()

    def _upd(self, *_a):
        self._c.rgba = self.active_color if self.state == "down" else self.inactive_color
        self._r.pos = self.pos
        self._r.size = self.size


def styled_input(**kwargs):
    kwargs.setdefault("multiline", False)
    kwargs.setdefault("font_size", sp(15))
    kwargs.setdefault("padding", [dp(12), dp(13), dp(12), dp(13)])
    return TextInput(
        background_normal="", background_active="",
        background_color=FIELD, foreground_color=TEXT,
        hint_text_color=MUTED, cursor_color=ACCENT,
        selection_color=(0.91, 0.22, 0.25, 0.4),
        **kwargs,
    )


def wrap_label(text, font_size=12, color=MUTED, bold=False, halign="left"):
    """Label that wraps text and sizes its own height."""
    lbl = Label(text=text, font_size=sp(font_size), color=color, bold=bold,
                halign=halign, valign="middle", size_hint_y=None)
    lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
    lbl.bind(texture_size=lambda inst, ts: setattr(inst, "height", ts[1] + dp(6)))
    return lbl


def make_popup(title, content, size_hint):
    return Popup(
        title=title, content=content, size_hint=size_hint,
        background="", background_color=(0.10, 0.10, 0.14, 1),
        separator_color=ACCENT, title_color=TEXT, title_size=sp(16),
    )


# ----------------------------------------------------------------------
# Storage helpers
# ----------------------------------------------------------------------
def get_device_id(user_data_dir):
    if ANDROID:
        try:
            Secure = autoclass("android.provider.Settings$Secure")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = cast("android.app.Activity", PythonActivity.mActivity)
            android_id = Secure.getString(activity.getContentResolver(), Secure.ANDROID_ID)
            if android_id:
                return android_id
        except Exception:
            pass

    id_file = os.path.join(user_data_dir, "device_id.txt")
    try:
        if os.path.exists(id_file):
            with open(id_file, "r") as f:
                existing = f.read().strip()
                if existing:
                    return existing
    except Exception:
        pass

    new_id = uuid.uuid4().hex
    try:
        os.makedirs(user_data_dir, exist_ok=True)
        with open(id_file, "w") as f:
            f.write(new_id)
    except Exception:
        pass
    return new_id


def load_pro_status(user_data_dir, device_id):
    """Pro is active only if the saved code is a valid signature for THIS device."""
    lic_file = os.path.join(user_data_dir, "license.txt")
    try:
        with open(lic_file, "r") as f:
            return verify_unlock_code(device_id, f.read())
    except Exception:
        return False


def save_pro_license(user_data_dir, code):
    lic_file = os.path.join(user_data_dir, "license.txt")
    try:
        os.makedirs(user_data_dir, exist_ok=True)
        with open(lic_file, "w") as f:
            f.write(code.strip())
    except Exception:
        pass


def load_free_used(user_data_dir):
    try:
        with open(os.path.join(user_data_dir, "free_used.txt"), "r") as f:
            return max(0, int(f.read().strip()))
    except Exception:
        return 0


def save_free_used(user_data_dir, n):
    try:
        os.makedirs(user_data_dir, exist_ok=True)
        with open(os.path.join(user_data_dir, "free_used.txt"), "w") as f:
            f.write(str(int(n)))
    except Exception:
        pass


# ----------------------------------------------------------------------
# Firebase-backed download counter (optional).
#
# Keeps the free-download count tied to the Device ID on Firebase's
# servers, not just in a local file, so clearing app data or reinstalling
# the app does not refill the free quota. Only a full factory reset of the
# phone (which usually changes the Device ID) gets around it.
#
# Uses the Realtime Database REST API directly with urllib, so no extra
# Python packages are needed in buildozer.spec.
# ----------------------------------------------------------------------
def _firebase_configured():
    return (
        FIREBASE_API_KEY and not FIREBASE_API_KEY.startswith("PASTE")
        and FIREBASE_DB_URL and not FIREBASE_DB_URL.startswith("https://PASTE")
    )


def _http_json(url, method="GET", payload=None, timeout=8):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def firebase_anon_token():
    """Starts a fresh anonymous Firebase session. Returns an idToken, or None
    if Firebase isn't configured or the device has no internet access."""
    if not _firebase_configured():
        return None
    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
        f"?key={FIREBASE_API_KEY}"
    )
    try:
        result = _http_json(url, "POST", {"returnSecureToken": True})
        return result.get("idToken") if result else None
    except Exception:
        return None


def firebase_get_count(device_id):
    """Returns the download count Firebase has stored for this device,
    or None if it could not be reached (offline / not configured)."""
    token = firebase_anon_token()
    if not token:
        return None
    url = f"{FIREBASE_DB_URL}/downloads/{device_id}/count.json?auth={token}"
    try:
        result = _http_json(url, "GET")
        return int(result) if isinstance(result, (int, float)) else 0
    except Exception:
        return None


def firebase_increment_count(device_id):
    """Reads the current remote count and writes count+1. Returns the new
    count, or None if it could not be reached. The security rules only
    allow writing exactly old_value + 1, so this can never be used to set
    the counter back down, even by someone who extracts the API key."""
    token = firebase_anon_token()
    if not token:
        return None
    base = f"{FIREBASE_DB_URL}/downloads/{device_id}/count.json?auth={token}"
    try:
        current = _http_json(base, "GET")
        current = int(current) if isinstance(current, (int, float)) else 0
        new_value = current + 1
        _http_json(base, "PUT", new_value)
        return new_value
    except Exception:
        return None


def get_download_folder():
    if ANDROID:
        try:
            base = primary_external_storage_path()
            folder = os.path.join(base, "Download")
            os.makedirs(folder, exist_ok=True)
            return folder
        except Exception:
            pass
    # Fallback (desktop testing)
    folder = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(folder, exist_ok=True)
    return folder


# ----------------------------------------------------------------------
# UI: one row per download
# ----------------------------------------------------------------------
class DownloadRow(Card):
    """One card in the downloads list: badge, title, progress bar, status, cancel."""

    def __init__(self, url, mode, platform, on_cancel, playlist=False, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, height=dp(108),
                         padding=(dp(12), dp(8)), spacing=dp(6), **kwargs)

        top_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                            height=dp(34), spacing=dp(8))

        badge_text = PLATFORM_LABEL.get(platform, "Video")
        badge = Card(color=PLATFORM_COLOR.get(platform, BTN), radius=8,
                     size_hint=(None, None), size=(dp(84), dp(24)),
                     pos_hint={"center_y": 0.5})
        badge.add_widget(Label(text=f"{badge_text}", font_size=sp(11), bold=True, color=TEXT))
        top_row.add_widget(badge)

        kind = "AUDIO" if mode == "audio" else "VIDEO"
        mode_lbl = Label(text=f"PLAYLIST\n{kind}" if playlist else kind,
                         font_size=sp(9), color=MUTED, size_hint=(None, 1), width=dp(54),
                         halign="center")
        top_row.add_widget(mode_lbl)

        self.title_label = Label(
            text=url, font_size=sp(13), color=TEXT,
            halign="left", valign="middle",
            shorten=True, shorten_from="right",
        )
        self.title_label.bind(size=self._update_text_size)
        top_row.add_widget(self.title_label)

        self.cancel_btn = RoundedButton(
            text="Cancel", font_size=sp(12), size_hint=(None, 1), width=dp(70),
            bg_color=(0.55, 0.18, 0.20, 1),
        )
        self.cancel_btn.bind(on_press=on_cancel)
        top_row.add_widget(self.cancel_btn)
        self.add_widget(top_row)

        self.progress = ProgressBar(max=100, value=0, size_hint=(1, None), height=dp(10))
        self.add_widget(self.progress)

        self.status_label = Label(
            text="Queued...", font_size=sp(12), color=MUTED,
            size_hint=(1, 1), halign="left", valign="middle",
        )
        self.status_label.bind(size=self._update_text_size)
        self.add_widget(self.status_label)

    def _update_text_size(self, instance, size):
        instance.text_size = (size[0], size[1])

    def set_title(self, text):
        self.title_label.text = text

    def set_status(self, text, color=None):
        self.status_label.text = text
        self.status_label.color = color or MUTED

    def set_progress(self, value):
        self.progress.value = value

    def mark_finished(self):
        self.cancel_btn.disabled = True


class YTDownloaderApp(App):
    def build(self):
        self.title = "Video Downloader"
        Window.clearcolor = BG

        if ANDROID:
            try:
                request_permissions([
                    Permission.WRITE_EXTERNAL_STORAGE,
                    Permission.READ_EXTERNAL_STORAGE,
                    Permission.INTERNET,
                ])
            except Exception:
                pass

        self.download_folder = get_download_folder()
        self.downloads = {}  # id -> {"cancel_event": Event, "row": DownloadRow}
        self.active_download_count = 0
        self.selected_platform = "youtube"
        self.selected_mode = "video"
        self.selected_scope = "single"   # "single" or "playlist"

        self.device_id = get_device_id(self.user_data_dir)
        self.is_pro = load_pro_status(self.user_data_dir, self.device_id)
        self.free_used = load_free_used(self.user_data_dir)
        # Catches the case where the customer cleared app data / reinstalled:
        # pulls the real count back down from Firebase once a connection exists.
        threading.Thread(target=self._sync_startup_count, daemon=True).start()

        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))

        # ---- Header: title + Pro button ----
        header = Card(orientation="horizontal", size_hint=(1, None), height=dp(64),
                      padding=(dp(14), dp(8)), spacing=dp(8))
        title_box = BoxLayout(orientation="vertical")
        title_lbl = Label(
            text="Video Downloader", font_size=sp(19), bold=True, color=TEXT,
            halign="left", valign="middle",
        )
        title_lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        title_box.add_widget(title_lbl)
        self.quota_label = Label(
            text="", font_size=sp(11), color=MUTED, halign="left", valign="middle",
            size_hint=(1, None), height=dp(16),
        )
        self.quota_label.bind(size=lambda i, s: setattr(i, "text_size", s))
        title_box.add_widget(self.quota_label)
        header.add_widget(title_box)
        self.pro_btn = RoundedButton(
            text="PRO Unlocked" if self.is_pro else "Unlock Pro",
            font_size=sp(12), bold=True, size_hint=(None, 1), width=dp(112),
            bg_color=GREEN if self.is_pro else (0.85, 0.60, 0.10, 1),
        )
        self.pro_btn.bind(on_press=self.open_pro_popup)
        header.add_widget(self.pro_btn)
        root.add_widget(header)
        self.refresh_quota_label()

        # ---- Input card ----
        input_card = Card(orientation="vertical", size_hint=(1, None),
                          padding=dp(12), spacing=dp(8))
        # label 18 + platforms 86 + url 46 + options 42 + add 50 + 4 gaps + padding
        input_card.height = dp(18 + 86 + 46 + 42 + 50 + 32 + 24)

        plat_lbl = Label(
            text="Select platform", font_size=sp(12), color=MUTED,
            size_hint=(1, None), height=dp(18), halign="left", valign="middle",
        )
        plat_lbl.bind(size=lambda i, s: setattr(i, "text_size", s))
        input_card.add_widget(plat_lbl)

        # Platform buttons: two rows so ALL platforms are visible (no side scrolling)
        p_box = BoxLayout(orientation="vertical", size_hint=(1, None),
                          height=dp(86), spacing=dp(6))
        row1 = BoxLayout(orientation="horizontal", spacing=dp(6))
        row2 = BoxLayout(orientation="horizontal", spacing=dp(6))
        self.platform_buttons = {}
        for i, (key, label, color) in enumerate(PLATFORMS):
            btn = RoundedToggle(
                text=label, group="platform", font_size=sp(13), bold=True,
                active_color=color,
                state="down" if key == "youtube" else "normal",
            )
            btn.bind(on_press=lambda inst, k=key: self.select_platform(k))
            self.platform_buttons[key] = btn
            (row1 if i < 3 else row2).add_widget(btn)
        p_box.add_widget(row1)
        p_box.add_widget(row2)
        input_card.add_widget(p_box)

        # URL row: input + Paste
        url_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                            height=dp(46), spacing=dp(6))
        self.url_input = styled_input(hint_text="Paste YouTube video link here")
        self.url_input.bind(text=self.on_url_text)
        url_row.add_widget(self.url_input)
        paste_btn = RoundedButton(text="Paste", font_size=sp(13), size_hint=(None, 1),
                                  width=dp(70))
        paste_btn.bind(on_press=self.paste_from_clipboard)
        url_row.add_widget(paste_btn)
        input_card.add_widget(url_row)

        # Options row: [Video | Audio]   [Single | Playlist]
        opt_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                            height=dp(42), spacing=dp(6))
        blue = (0.30, 0.35, 0.85, 1)
        teal = (0.10, 0.62, 0.66, 1)
        self.video_mode_btn = RoundedToggle(
            text="Video", font_size=sp(12), group="mode", state="down", active_color=blue)
        self.video_mode_btn.bind(on_press=lambda inst: self.select_mode("video"))
        self.audio_mode_btn = RoundedToggle(
            text="Audio", font_size=sp(12), group="mode", active_color=blue)
        self.audio_mode_btn.bind(on_press=lambda inst: self.select_mode("audio"))
        self.single_btn = RoundedToggle(
            text="Single", font_size=sp(12), group="scope", state="down", active_color=teal)
        self.single_btn.bind(on_press=lambda inst: self.select_scope("single"))
        self.playlist_btn = RoundedToggle(
            text="Playlist", font_size=sp(12), group="scope", active_color=teal)
        self.playlist_btn.bind(on_press=lambda inst: self.select_scope("playlist"))
        opt_row.add_widget(self.video_mode_btn)
        opt_row.add_widget(self.audio_mode_btn)
        opt_row.add_widget(Label(size_hint=(None, 1), width=dp(4)))  # small spacer
        opt_row.add_widget(self.single_btn)
        opt_row.add_widget(self.playlist_btn)
        input_card.add_widget(opt_row)

        self.add_btn = RoundedButton(
            text="Add Download", font_size=sp(16), bold=True,
            size_hint=(1, None), height=dp(50), bg_color=ADD_BTN_COLOR,
        )
        self.add_btn.bind(on_press=self.add_download)
        input_card.add_widget(self.add_btn)
        root.add_widget(input_card)

        # ---- Folder row ----
        folder_card = Card(orientation="horizontal", size_hint=(1, None), height=dp(54),
                           padding=(dp(12), dp(8)), spacing=dp(6))
        self.folder_label = Label(
            text=f"Save to: {self.download_folder}", font_size=sp(11), color=MUTED,
            shorten=True, shorten_from="left", halign="left", valign="middle",
        )
        self.folder_label.bind(size=self._update_folder_label_text_size)
        folder_card.add_widget(self.folder_label)

        change_folder_btn = RoundedButton(text="Change", font_size=sp(12),
                                          size_hint=(None, 1), width=dp(70))
        change_folder_btn.bind(on_press=self.open_change_folder_popup)
        folder_card.add_widget(change_folder_btn)

        open_folder_btn = RoundedButton(text="Open", font_size=sp(12),
                                        size_hint=(None, 1), width=dp(60))
        open_folder_btn.bind(on_press=self.open_download_folder)
        folder_card.add_widget(open_folder_btn)
        root.add_widget(folder_card)

        # ---- Downloads list ----
        section = Label(text="Downloads", font_size=sp(14), bold=True, color=TEXT,
                        size_hint=(1, None), height=dp(22), halign="left", valign="middle")
        section.bind(size=lambda i, s: setattr(i, "text_size", s))
        root.add_widget(section)

        self.downloads_grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        self.downloads_grid.bind(minimum_height=self.downloads_grid.setter("height"))

        self.empty_label = Label(
            text="No downloads yet.\nPaste a link above to get started.",
            font_size=sp(12), color=MUTED, halign="center",
            size_hint_y=None, height=dp(80),
        )
        self.downloads_grid.add_widget(self.empty_label)

        scroll = ScrollView(size_hint=(1, 1), bar_width=dp(3))
        scroll.add_widget(self.downloads_grid)
        root.add_widget(scroll)

        return root

    def _update_folder_label_text_size(self, instance, size):
        instance.text_size = (size[0], size[1])

    def refresh_quota_label(self):
        if self.is_pro:
            self.quota_label.text = "Pro - unlimited downloads"
            self.quota_label.color = (0.45, 0.85, 0.55, 1)
        else:
            left = max(0, FREE_DOWNLOAD_LIMIT - self.free_used)
            self.quota_label.text = f"Free downloads left: {left} of {FREE_DOWNLOAD_LIMIT}"
            self.quota_label.color = MUTED if left > 0 else RED

    def _count_free_download(self):
        """Called after a SUCCESSFUL download. Failed/cancelled ones don't count."""
        if self.is_pro:
            return
        self.free_used += 1
        save_free_used(self.user_data_dir, self.free_used)
        self.refresh_quota_label()
        threading.Thread(target=self._sync_increment_remote, daemon=True).start()

    def _apply_remote_count(self, remote_count):
        # Only ever move the number UP to the server's value - never down,
        # so a flaky network reply can't quietly give free downloads back.
        if remote_count is None or remote_count <= self.free_used:
            return
        self.free_used = remote_count
        save_free_used(self.user_data_dir, self.free_used)
        self.refresh_quota_label()

    def _sync_startup_count(self):
        remote = firebase_get_count(self.device_id)
        if remote is not None:
            Clock.schedule_once(lambda dt: self._apply_remote_count(remote))

    def _sync_increment_remote(self):
        remote = firebase_increment_count(self.device_id)
        if remote is not None:
            Clock.schedule_once(lambda dt: self._apply_remote_count(remote))

    # ------------------------------------------------------------------
    # Platform / mode selection. yt_dlp auto-detects the site from the URL,
    # so the platform chips mainly drive the hint text and the badge.
    # Pasting a link also auto-selects the matching platform.
    # ------------------------------------------------------------------
    def select_platform(self, key):
        self.selected_platform = key
        self.url_input.hint_text = f"Paste {PLATFORM_LABEL[key]} video link here"

    def select_mode(self, mode):
        self.selected_mode = mode

    def select_scope(self, scope):
        self.selected_scope = scope

    def on_url_text(self, instance, text):
        key = detect_platform(text)
        if key and key != self.selected_platform:
            self.platform_buttons[key].state = "down"
            self.select_platform(key)
        # A pure playlist link -> switch to Playlist automatically
        if "/playlist?" in text.lower() and self.selected_scope != "playlist":
            self.playlist_btn.state = "down"
            self.select_scope("playlist")

    def paste_from_clipboard(self, instance):
        try:
            text = Clipboard.paste()
            if text:
                self.url_input.text = text.strip()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Adding / running downloads
    # ------------------------------------------------------------------
    def add_download(self, instance):
        url = self.url_input.text.strip()
        if not url:
            return

        if not self.is_pro and self.free_used >= FREE_DOWNLOAD_LIMIT:
            self.open_pro_popup(
                instance,
                message=(
                    f"Free limit reached ({FREE_DOWNLOAD_LIMIT} of {FREE_DOWNLOAD_LIMIT} downloads used).\n"
                    f"Unlock Pro for unlimited downloads: {PRO_PRICE}, one-time."
                ),
            )
            return

        if not self.is_pro and self.active_download_count >= 1:
            self.open_pro_popup(
                instance,
                message=(
                    "Free version allows only 1 download at a time.\n"
                    "Unlock Pro for unlimited simultaneous downloads."
                ),
            )
            return

        playlist = self.selected_scope == "playlist"
        if playlist and PLAYLIST_REQUIRES_PRO and not self.is_pro:
            self.open_pro_popup(
                instance,
                message="Playlist download is a Pro feature.\nUnlock Pro to download whole playlists.",
            )
            return

        self.url_input.text = ""
        mode = self.selected_mode
        platform = detect_platform(url) or self.selected_platform

        if self.empty_label.parent:
            self.downloads_grid.remove_widget(self.empty_label)

        download_id = str(uuid.uuid4())
        row = DownloadRow(url=url, mode=mode, platform=platform, playlist=playlist,
                          on_cancel=lambda inst: self.cancel_download(download_id))
        self.downloads[download_id] = {
            "cancel_event": threading.Event(),
            "row": row,
        }
        self.downloads_grid.add_widget(row, index=0)
        self.active_download_count += 1

        thread = threading.Thread(
            target=self.download_video, args=(url, download_id, mode, platform, playlist),
            daemon=True
        )
        thread.start()

    def _finish_download_slot(self):
        self.active_download_count = max(0, self.active_download_count - 1)

    def cancel_download(self, download_id):
        entry = self.downloads.get(download_id)
        if not entry:
            return
        entry["cancel_event"].set()
        Clock.schedule_once(lambda dt: entry["row"].set_status("Cancelling..."))
        entry["row"].cancel_btn.disabled = True

    def progress_hook_for(self, download_id):
        entry = self.downloads[download_id]
        row = entry["row"]
        cancel_event = entry["cancel_event"]
        state = {"last_key": None}

        def hook(d):
            if cancel_event.is_set():
                raise DownloadCancelledError("Cancelled by user")

            info = d.get("info_dict") or {}
            idx = info.get("playlist_index")
            total_items = info.get("n_entries") or info.get("playlist_count")
            prefix = f"[{idx}/{total_items}] " if idx and total_items else ""

            # Update the row title whenever a new item starts
            key = (idx, info.get("title"))
            if info.get("title") and key != state["last_key"]:
                state["last_key"] = key
                title = f"{prefix}{info['title']}"
                Clock.schedule_once(lambda dt: row.set_title(title))

            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    percent = downloaded / total * 100
                    Clock.schedule_once(lambda dt: row.set_progress(percent))
                    Clock.schedule_once(
                        lambda dt: row.set_status(f"{prefix}Downloading... {percent:.1f}%")
                    )
            elif d["status"] == "finished":
                Clock.schedule_once(lambda dt: row.set_status(f"{prefix}Processing..."))

        return hook

    def download_video(self, url, download_id, mode, platform="youtube", playlist=False):
        import yt_dlp

        entry = self.downloads[download_id]
        row = entry["row"]
        cancel_event = entry["cancel_event"]
        progress_hook = self.progress_hook_for(download_id)

        # Android's sys.stdout is not a proper file object, so yt_dlp's
        # default internal logger crashes. Use a silent logger instead and
        # turn off yt_dlp's own console output (we already get progress via
        # progress_hooks).
        class SilentLogger:
            def debug(self, msg):
                pass

            def warning(self, msg):
                pass

            def error(self, msg):
                pass

        def cancel_filter(info, *, incomplete=False):
            # Used in playlist mode: skips (and stops) remaining items after Cancel.
            return "Cancelled" if cancel_event.is_set() else None

        def make_opts(fmt, player_client):
            if playlist:
                outtmpl = os.path.join(
                    self.download_folder,
                    "%(playlist_title|Playlist).100B",
                    "%(playlist_index&{} - |)s%(title).120B.%(ext)s",
                )
            else:
                outtmpl = os.path.join(self.download_folder, "%(title).150B.%(ext)s")
            opts = {
                "outtmpl": outtmpl,
                "format": fmt,
                "progress_hooks": [progress_hook],
                "noplaylist": not playlist,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": SilentLogger(),
            }
            if playlist:
                # One unavailable/private video should not kill the whole playlist.
                opts["ignoreerrors"] = True
                opts["match_filter"] = cancel_filter
                opts["break_on_reject"] = True
            if player_client:
                # Only used for YouTube links.
                opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
            return opts

        base_fmt = "bestaudio/best" if mode == "audio" else "best"
        worst_fmt = "worst" if mode == "video" else "worstaudio/worst"

        if platform == "youtube":
            # YouTube currently forces "SABR streaming", which breaks the
            # default "web" client's format URLs. android/ios/tv clients avoid
            # this, so try them first.
            attempts = [
                ("android", base_fmt),
                ("ios", base_fmt),
                ("tv", base_fmt),
                ("android", worst_fmt),
                (None, base_fmt),
            ]
        else:
            # TikTok / Instagram / Facebook / Dailymotion: no player-client tricks needed.
            attempts = [
                (None, base_fmt),
                (None, worst_fmt),
            ]

        last_error = None
        cancelled = False

        for player_client, fmt in attempts:
            if cancel_event.is_set():
                cancelled = True
                break
            try:
                with yt_dlp.YoutubeDL(make_opts(fmt, player_client)) as ydl:
                    ydl.download([url])
                if cancel_event.is_set():
                    cancelled = True
                    break
                Clock.schedule_once(lambda dt: row.set_progress(100))
                Clock.schedule_once(lambda dt: row.set_status("Done!" if not playlist else "Playlist finished!", GREEN))
                Clock.schedule_once(lambda dt: self._count_free_download())
                Clock.schedule_once(lambda dt: row.mark_finished())
                Clock.schedule_once(lambda dt: self._finish_download_slot())
                return
            except DownloadCancelledError:
                cancelled = True
                break
            except yt_dlp.utils.DownloadError as e:
                last_error = e
                if cancel_event.is_set():
                    cancelled = True
                    break
                err_text = str(e).lower()
                if "requested format is not available" not in err_text and "sabr" not in err_text:
                    break
            except Exception as e:
                last_error = e
                break

        if cancelled:
            Clock.schedule_once(lambda dt: row.set_status("Cancelled."))
            Clock.schedule_once(lambda dt: row.mark_finished())
            Clock.schedule_once(lambda dt: self._finish_download_slot())
            return

        detail = str(last_error)[:100] if last_error else "unknown error"
        err_low = str(last_error).lower() if last_error else ""
        if "login" in err_low or "cookies" in err_low or "private" in err_low:
            detail = "this link needs login / is private"
        elif not cancel_event.is_set():
            try:
                with yt_dlp.YoutubeDL(make_opts("best", "android" if platform == "youtube" else None)) as ydl:
                    info = ydl.extract_info(url, download=False)
                available = sorted({
                    f.get("ext", "?") for f in info.get("formats", []) if f.get("url")
                })
                if available:
                    detail = f"available exts: {', '.join(available)}"
                else:
                    detail = "no formats found"
            except Exception:
                pass

        fail_text = f"Failed: {detail}"
        Clock.schedule_once(lambda dt: row.set_status(fail_text, RED))
        Clock.schedule_once(lambda dt: row.mark_finished())
        Clock.schedule_once(lambda dt: self._finish_download_slot())

    # ------------------------------------------------------------------
    # Pro unlock
    # ------------------------------------------------------------------
    def open_pro_popup(self, instance, message=None):
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

        if self.is_pro:
            content.add_widget(Label(
                text="Pro is already unlocked. Thank you!",
                font_size=sp(14), color=TEXT,
            ))
            popup = make_popup("Pro", content, (0.85, 0.3))
            close_btn = RoundedButton(text="OK", size_hint=(1, None), height=dp(44))
            close_btn.bind(on_press=popup.dismiss)
            content.add_widget(close_btn)
            popup.open()
            return

        # Scrollable body so nothing gets cut off on small screens
        body = GridLayout(cols=1, size_hint_y=None, spacing=dp(8))
        body.bind(minimum_height=body.setter("height"))

        if message:
            body.add_widget(wrap_label(message, 12, color=(0.95, 0.75, 0.30, 1)))

        body.add_widget(wrap_label(
            f"Pro price: {PRO_PRICE} (one-time, works on this phone only)\n"
            "1) Pay via EasyPaisa (details below)\n"
            "2) Send your Device ID + payment proof on WhatsApp\n"
            "3) Paste the unlock code you receive and tap Activate",
            12, color=TEXT,
        ))
        body.add_widget(wrap_label("Your Device ID:"))

        id_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                           height=dp(44), spacing=dp(6))
        id_input = styled_input(text=self.device_id, readonly=True, font_size=sp(12))
        id_row.add_widget(id_input)
        copy_btn = RoundedButton(text="Copy", font_size=sp(12), size_hint=(None, 1), width=dp(64))
        copy_btn.bind(on_press=lambda *_a: Clipboard.copy(self.device_id))
        id_row.add_widget(copy_btn)
        body.add_widget(id_row)

        body.add_widget(wrap_label("Payment & Contact Details", 13, color=TEXT, bold=True))
        body.add_widget(wrap_label(
            f"EasyPaisa Number: {PAYMENT_EASYPAISA_NUMBER}\nAccount Title: {PAYMENT_TITLE}",
            12, color=TEXT,
        ))

        whatsapp_btn = RoundedButton(
            text="Contact on WhatsApp", font_size=sp(13),
            size_hint=(1, None), height=dp(44), bg_color=(0.15, 0.65, 0.35, 1),
        )
        whatsapp_btn.bind(on_press=self.open_whatsapp_contact)
        body.add_widget(whatsapp_btn)

        body.add_widget(wrap_label("Paste the unlock code you receive after payment:"))
        code_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                             height=dp(46), spacing=dp(6))
        code_input = styled_input(hint_text="Unlock code", font_size=sp(11))
        code_row.add_widget(code_input)
        paste_code_btn = RoundedButton(text="Paste", font_size=sp(12),
                                       size_hint=(None, 1), width=dp(64))
        paste_code_btn.bind(
            on_press=lambda *_a: setattr(code_input, "text", (Clipboard.paste() or "").strip()))
        code_row.add_widget(paste_code_btn)
        body.add_widget(code_row)

        result_label = wrap_label("", 12, color=RED)
        body.add_widget(result_label)

        sv = ScrollView(size_hint=(1, 1), bar_width=dp(3))
        sv.add_widget(body)
        content.add_widget(sv)

        activate_btn = RoundedButton(text="Activate", font_size=sp(15), bold=True,
                                     size_hint=(1, None), height=dp(48), bg_color=GREEN)
        content.add_widget(activate_btn)

        popup = make_popup("Unlock Pro", content, (0.94, 0.92))

        def try_activate(*_a):
            entered = code_input.text.strip()
            if not entered:
                result_label.text = "Please paste your unlock code."
                return
            if verify_unlock_code(self.device_id, entered):
                self.is_pro = True
                save_pro_license(self.user_data_dir, entered)
                self.pro_btn.text = "PRO Unlocked"
                self.pro_btn.bg_color = list(GREEN)
                self.refresh_quota_label()
                popup.dismiss()
            elif PUBLIC_KEY_HEX.startswith("PASTE"):
                result_label.text = "Developer: set PUBLIC_KEY_HEX in main.py first."
            else:
                result_label.text = "Incorrect code for this device. Please check again."

        activate_btn.bind(on_press=try_activate)
        popup.open()

    def open_whatsapp_contact(self, instance):
        message = f"Assalam o Alaikum, I have made the payment. My Device ID is: {self.device_id}"
        target_url = f"https://wa.me/{CONTACT_WHATSAPP_NUMBER}?text={quote(message)}"
        if ANDROID:
            try:
                Intent = autoclass("android.content.Intent")
                Uri = autoclass("android.net.Uri")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = cast("android.app.Activity", PythonActivity.mActivity)
                intent = Intent(Intent.ACTION_VIEW)
                intent.setData(Uri.parse(target_url))
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                activity.startActivity(intent)
            except Exception:
                pass
        else:
            import webbrowser
            webbrowser.open(target_url)

    # ------------------------------------------------------------------
    # Change folder
    # ------------------------------------------------------------------
    def open_change_folder_popup(self, instance):
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))

        path_input = styled_input(text=self.download_folder, size_hint=(1, None), height=dp(46))
        content.add_widget(path_input)

        info_label = wrap_label("Type the full folder path (it will be created if it doesn't exist).")
        content.add_widget(info_label)

        btn_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                            height=dp(46), spacing=dp(10))
        save_btn = RoundedButton(text="Save", bg_color=GREEN)
        cancel_btn = RoundedButton(text="Cancel")
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(save_btn)
        content.add_widget(btn_row)

        popup = make_popup("Change Save Folder", content, (0.92, 0.42))

        cancel_btn.bind(on_press=popup.dismiss)
        save_btn.bind(on_press=lambda *_a: self._apply_new_folder(path_input.text.strip(), popup))
        popup.open()

    def _apply_new_folder(self, new_path, popup):
        if not new_path:
            return
        try:
            os.makedirs(new_path, exist_ok=True)
            test_file = os.path.join(new_path, ".write_test")
            with open(test_file, "w") as f:
                f.write("")
            os.remove(test_file)
        except Exception:
            return

        self.download_folder = new_path
        self.folder_label.text = f"Save to: {self.download_folder}"
        popup.dismiss()

    # ------------------------------------------------------------------
    # Open folder in file manager
    # ------------------------------------------------------------------
    def open_download_folder(self, instance):
        if ANDROID:
            self._open_folder_android(self.download_folder)
        else:
            self._open_folder_desktop(self.download_folder)

    def _open_folder_android(self, folder_path):
        try:
            base = primary_external_storage_path()
            rel_path = os.path.relpath(folder_path, base)
            if rel_path == ".":
                rel_path = ""
            rel_path = rel_path.replace(os.sep, "/")
            doc_id = f"primary:{rel_path}" if rel_path else "primary:"

            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = cast("android.app.Activity", PythonActivity.mActivity)

            uri_string = (
                "content://com.android.externalstorage.documents/document/"
                + quote(doc_id, safe="")
            )
            uri = Uri.parse(uri_string)

            intent = Intent(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, "vnd.android.document/directory")
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity.startActivity(intent)
        except Exception:
            try:
                DownloadManager = autoclass("android.app.DownloadManager")
                Intent = autoclass("android.content.Intent")
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                activity = cast("android.app.Activity", PythonActivity.mActivity)
                intent = Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                activity.startActivity(intent)
            except Exception:
                pass

    def _open_folder_desktop(self, folder_path):
        try:
            if sys.platform == "win32":
                os.startfile(folder_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
        except Exception:
            pass


if __name__ == "__main__":
    YTDownloaderApp().run()
