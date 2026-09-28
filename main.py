"""
Video Downloader - Android App (Kivy)
Downloads videos (or audio-only) from YouTube, TikTok and Dailymotion.
Supports multiple simultaneous downloads, per-download cancel,
changing/opening the save folder, and a Pro unlock (device-locked code).

All on-screen text is English.
"""

import hashlib
import hmac
import os
import subprocess
import sys
import threading
import uuid
from urllib.parse import quote

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.progressbar import ProgressBar
from kivy.uix.popup import Popup
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard

ANDROID = True
try:
    from android.permissions import request_permissions, Permission
    from android.storage import primary_external_storage_path
    from jnius import autoclass, cast
except Exception:
    ANDROID = False


# !!! CHANGE THIS SECRET KEY AND NEVER SHARE IT WITH ANYONE !!!
# This must be EXACTLY the same in generate_code.py.
SECRET_KEY = b"shadab-khan1234567890"

# Payment / contact details shown in the Pro-unlock popup.
PAYMENT_TITLE = "M Shadab Sikandar"
PAYMENT_EASYPAISA_NUMBER = "0311-8590702"
CONTACT_WHATSAPP_NUMBER = "923118590702"  # country code + number, no leading 0, no symbols


def generate_unlock_code(device_id):
    """Device ID + SECRET_KEY -> a 10-character unlock code.
    generate_code.py must use the exact same function/key to match."""
    digest = hmac.new(SECRET_KEY, device_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:10].upper()


class DownloadCancelledError(Exception):
    """Raised from inside a progress_hook to abort an in-progress yt_dlp download."""
    pass


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


def load_pro_status(user_data_dir):
    status_file = os.path.join(user_data_dir, "pro_status.txt")
    try:
        with open(status_file, "r") as f:
            return f.read().strip() == "unlocked"
    except Exception:
        return False


def save_pro_status(user_data_dir):
    status_file = os.path.join(user_data_dir, "pro_status.txt")
    try:
        os.makedirs(user_data_dir, exist_ok=True)
        with open(status_file, "w") as f:
            f.write("unlocked")
    except Exception:
        pass


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
class DownloadRow(BoxLayout):
    """One row in the downloads list: title, progress bar, status, cancel button."""

    def __init__(self, url, mode, on_cancel, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, height=100,
                          padding=(8, 6), spacing=4, **kwargs)

        tag = "[Audio]" if mode == "audio" else "[Video]"

        top_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.4))
        self.title_label = Label(
            text=f"{tag} {url}",
            font_size="13sp",
            halign="left",
            valign="middle",
            shorten=True,
            shorten_from="right",
            size_hint=(0.75, 1),
        )
        self.title_label.bind(size=self._update_text_size)
        top_row.add_widget(self.title_label)

        self.cancel_btn = Button(
            text="Cancel",
            font_size="12sp",
            size_hint=(0.25, 1),
        )
        self.cancel_btn.bind(on_press=on_cancel)
        top_row.add_widget(self.cancel_btn)
        self.add_widget(top_row)

        self.progress = ProgressBar(max=100, value=0, size_hint=(1, 0.3))
        self.add_widget(self.progress)

        self.status_label = Label(
            text="Queued...",
            font_size="11sp",
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(1, 0.3),
            halign="left",
            valign="middle",
        )
        self.status_label.bind(size=self._update_text_size)
        self.add_widget(self.status_label)

    def _update_text_size(self, instance, size):
        instance.text_size = (size[0], size[1])

    def set_status(self, text):
        self.status_label.text = text

    def set_progress(self, value):
        self.progress.value = value

    def mark_finished(self):
        self.cancel_btn.disabled = True


class YTDownloaderApp(App):
    def build(self):
        self.title = "Video Downloader"

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

        self.device_id = get_device_id(self.user_data_dir)
        self.is_pro = load_pro_status(self.user_data_dir)

        root = BoxLayout(orientation="vertical", padding=20, spacing=10)

        # ---- Title + Pro button ----
        title_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.1))
        title_row.add_widget(Label(
            text="Video Downloader",
            font_size="18sp",
            size_hint=(0.7, 1),
            bold=True,
            halign="left",
            valign="middle",
        ))
        self.pro_btn = Button(
            text="PRO Unlocked" if self.is_pro else "Unlock Pro",
            font_size="12sp",
            size_hint=(0.3, 1),
            background_color=(0.2, 0.6, 0.2, 1) if self.is_pro else (0.5, 0.5, 0.5, 1),
        )
        self.pro_btn.bind(on_press=self.open_pro_popup)
        title_row.add_widget(self.pro_btn)
        root.add_widget(title_row)

        # ---- Platform selector ----
        root.add_widget(Label(
            text="Select platform:",
            font_size="12sp",
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(1, 0.05),
            halign="left",
        ))

        platform_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.08), spacing=6)
        self.platform_buttons = {}
        for key, label in [("youtube", "YouTube"), ("tiktok", "TikTok"), ("dailymotion", "Dailymotion")]:
            btn = ToggleButton(text=label, group="platform", font_size="13sp",
                                state="down" if key == "youtube" else "normal")
            btn.bind(on_press=lambda inst, k=key: self.select_platform(k))
            self.platform_buttons[key] = btn
            platform_row.add_widget(btn)
        root.add_widget(platform_row)

        # ---- Link input ----
        self.link_hint_label = Label(
            text="Paste YouTube video link below",
            font_size="12sp",
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(1, 0.05),
            halign="left",
        )
        root.add_widget(self.link_hint_label)

        self.url_input = TextInput(
            hint_text="Paste video link here",
            multiline=False,
            size_hint=(1, 0.08),
            font_size="15sp",
        )
        root.add_widget(self.url_input)

        # ---- Mode selector (Video / Audio only) ----
        mode_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.08), spacing=6)
        self.video_mode_btn = ToggleButton(
            text="Video", font_size="13sp", group="mode", state="down",
        )
        self.video_mode_btn.bind(on_press=lambda inst: self.select_mode("video"))
        mode_row.add_widget(self.video_mode_btn)

        self.audio_mode_btn = ToggleButton(
            text="Audio Only", font_size="13sp", group="mode",
        )
        self.audio_mode_btn.bind(on_press=lambda inst: self.select_mode("audio"))
        mode_row.add_widget(self.audio_mode_btn)
        root.add_widget(mode_row)

        self.add_btn = Button(
            text="Add Download",
            size_hint=(1, 0.1),
            font_size="16sp",
            background_color=(0.88, 0.17, 0.17, 1),
        )
        self.add_btn.bind(on_press=self.add_download)
        root.add_widget(self.add_btn)

        # ---- Folder row: path label + change + open ----
        folder_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.08), spacing=8)

        self.folder_label = Label(
            text=f"Save to: {self.download_folder}",
            font_size="11sp",
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(0.5, 1),
            shorten=True,
            shorten_from="left",
            halign="left",
            valign="middle",
        )
        self.folder_label.bind(size=self._update_folder_label_text_size)
        folder_row.add_widget(self.folder_label)

        change_folder_btn = Button(text="Change", font_size="12sp", size_hint=(0.25, 1))
        change_folder_btn.bind(on_press=self.open_change_folder_popup)
        folder_row.add_widget(change_folder_btn)

        open_folder_btn = Button(text="Open", font_size="12sp", size_hint=(0.25, 1))
        open_folder_btn.bind(on_press=self.open_download_folder)
        folder_row.add_widget(open_folder_btn)

        root.add_widget(folder_row)

        # ---- Scrollable list of downloads ----
        self.downloads_grid = GridLayout(cols=1, size_hint_y=None, spacing=10)
        self.downloads_grid.bind(minimum_height=self.downloads_grid.setter("height"))

        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(self.downloads_grid)
        root.add_widget(scroll)

        return root

    def _update_folder_label_text_size(self, instance, size):
        instance.text_size = (size[0], size[1])

    # ------------------------------------------------------------------
    # Platform / mode selection (mostly cosmetic: yt_dlp auto-detects the
    # site from the URL itself, so the same download logic already works
    # for YouTube, TikTok and Dailymotion links without special-casing).
    # ------------------------------------------------------------------
    def select_platform(self, key):
        self.selected_platform = key
        hints = {
            "youtube": "Paste YouTube video link below",
            "tiktok": "Paste TikTok video link below",
            "dailymotion": "Paste Dailymotion video link below",
        }
        self.link_hint_label.text = hints[key]

    def select_mode(self, mode):
        self.selected_mode = mode

    # ------------------------------------------------------------------
    # Adding / running downloads
    # ------------------------------------------------------------------
    def add_download(self, instance):
        url = self.url_input.text.strip()
        if not url:
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

        self.url_input.text = ""
        mode = self.selected_mode

        download_id = str(uuid.uuid4())
        row = DownloadRow(url=url, mode=mode,
                           on_cancel=lambda inst: self.cancel_download(download_id))
        self.downloads[download_id] = {
            "cancel_event": threading.Event(),
            "row": row,
        }
        self.downloads_grid.add_widget(row, index=0)
        self.active_download_count += 1

        thread = threading.Thread(
            target=self.download_video, args=(url, download_id, mode), daemon=True
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

        def hook(d):
            if cancel_event.is_set():
                raise DownloadCancelledError("Cancelled by user")

            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    percent = downloaded / total * 100
                    Clock.schedule_once(lambda dt: row.set_progress(percent))
                    Clock.schedule_once(
                        lambda dt: row.set_status(f"Downloading... {percent:.1f}%")
                    )
            elif d["status"] == "finished":
                Clock.schedule_once(lambda dt: row.set_status("Processing..."))

        return hook

    def download_video(self, url, download_id, mode):
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

        def make_opts(fmt, player_client):
            opts = {
                "outtmpl": os.path.join(self.download_folder, "%(title)s.%(ext)s"),
                "format": fmt,
                "progress_hooks": [progress_hook],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": SilentLogger(),
            }
            if player_client:
                # Only affects YouTube links. Harmless no-op for
                # TikTok/Dailymotion, which yt_dlp auto-detects from the URL.
                opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
            return opts

        base_fmt = "bestaudio/best" if mode == "audio" else "best"

        # YouTube currently forces "SABR streaming", which breaks the
        # default "web" client's format URLs. android/ios/tv clients avoid
        # this, so try them first (harmless for non-YouTube links).
        attempts = [
            ("android", base_fmt),
            ("ios", base_fmt),
            ("tv", base_fmt),
            ("android", "worst" if mode == "video" else "worstaudio/worst"),
            (None, base_fmt),
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
                Clock.schedule_once(lambda dt: row.set_progress(100))
                Clock.schedule_once(lambda dt: row.set_status("Done!"))
                Clock.schedule_once(lambda dt: row.mark_finished())
                Clock.schedule_once(lambda dt: self._finish_download_slot())
                return
            except DownloadCancelledError:
                cancelled = True
                break
            except yt_dlp.utils.DownloadError as e:
                last_error = e
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
        if not cancel_event.is_set():
            try:
                with yt_dlp.YoutubeDL(make_opts("best", "android")) as ydl:
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
        Clock.schedule_once(lambda dt: row.set_status(fail_text))
        Clock.schedule_once(lambda dt: row.mark_finished())
        Clock.schedule_once(lambda dt: self._finish_download_slot())

    # ------------------------------------------------------------------
    # Pro unlock
    # ------------------------------------------------------------------
    def open_pro_popup(self, instance, message=None):
        content = BoxLayout(orientation="vertical", padding=10, spacing=8)

        if self.is_pro:
            content.add_widget(Label(
                text="Pro is already unlocked. Thank you!",
                font_size="14sp",
            ))
            popup = Popup(title="Pro", content=content, size_hint=(0.85, 0.3))
            close_btn = Button(text="OK", size_hint=(1, 0.4))
            close_btn.bind(on_press=popup.dismiss)
            content.add_widget(close_btn)
            popup.open()
            return

        if message:
            content.add_widget(Label(text=message, font_size="12sp", size_hint=(1, 0.14)))

        content.add_widget(Label(
            text="Copy your Device ID below and send it when you pay:",
            font_size="12sp", size_hint=(1, 0.1),
        ))

        id_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.12), spacing=6)
        id_input = TextInput(text=self.device_id, multiline=False, readonly=True, font_size="12sp")
        id_row.add_widget(id_input)
        copy_btn = Button(text="Copy", font_size="12sp", size_hint=(0.3, 1))
        copy_btn.bind(on_press=lambda *_a: Clipboard.copy(self.device_id))
        id_row.add_widget(copy_btn)
        content.add_widget(id_row)

        # ---- Payment / contact details ----
        content.add_widget(Label(
            text="Payment & Contact Details",
            font_size="13sp", bold=True, size_hint=(1, 0.09),
        ))
        content.add_widget(Label(
            text=f"EasyPaisa Number: {PAYMENT_EASYPAISA_NUMBER}\nAccount Title: {PAYMENT_TITLE}",
            font_size="12sp", size_hint=(1, 0.14),
        ))

        whatsapp_btn = Button(
            text="Contact on WhatsApp",
            font_size="12sp", size_hint=(1, 0.1),
            background_color=(0.15, 0.65, 0.35, 1),
        )
        whatsapp_btn.bind(on_press=self.open_whatsapp_contact)
        content.add_widget(whatsapp_btn)

        content.add_widget(Label(
            text="Enter the unlock code you receive after payment:",
            font_size="12sp", size_hint=(1, 0.1),
        ))
        code_input = TextInput(hint_text="Unlock code", multiline=False, font_size="14sp",
                                size_hint=(1, 0.11))
        content.add_widget(code_input)

        result_label = Label(text="", font_size="12sp",
                              color=(0.8, 0.2, 0.2, 1), size_hint=(1, 0.08))
        content.add_widget(result_label)

        activate_btn = Button(text="Activate", font_size="14sp",
                               size_hint=(1, 0.12), background_color=(0.2, 0.6, 0.2, 1))
        content.add_widget(activate_btn)

        popup = Popup(title="Unlock Pro", content=content, size_hint=(0.94, 0.92))

        def try_activate(*_a):
            entered = code_input.text.strip().upper()
            expected = generate_unlock_code(self.device_id)
            if entered and entered == expected:
                self.is_pro = True
                save_pro_status(self.user_data_dir)
                self.pro_btn.text = "PRO Unlocked"
                self.pro_btn.background_color = (0.2, 0.6, 0.2, 1)
                popup.dismiss()
            else:
                result_label.text = "Incorrect code. Please check again."

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
        content = BoxLayout(orientation="vertical", padding=10, spacing=10)

        path_input = TextInput(
            text=self.download_folder,
            multiline=False,
            font_size="14sp",
            size_hint=(1, 0.4),
        )
        content.add_widget(path_input)

        info_label = Label(
            text="Type the full folder path (it will be created if it doesn't exist).",
            font_size="12sp",
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(1, 0.28),
        )
        content.add_widget(info_label)

        btn_row = BoxLayout(orientation="horizontal", size_hint=(1, 0.32), spacing=10)
        save_btn = Button(text="Save", background_color=(0.2, 0.6, 0.2, 1))
        cancel_btn = Button(text="Cancel")
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(save_btn)
        content.add_widget(btn_row)

        popup = Popup(
            title="Change Save Folder",
            content=content,
            size_hint=(0.9, 0.45),
        )

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
