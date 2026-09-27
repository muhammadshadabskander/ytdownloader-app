"""
YouTube Video Downloader - Android App (Kivy)
"""

import os
import threading

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.progressbar import ProgressBar
from kivy.clock import Clock

ANDROID = True
try:
    from android.permissions import request_permissions, Permission
    from android.storage import primary_external_storage_path
except Exception:
    ANDROID = False


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


class MainLayout(BoxLayout):
    pass


class YTDownloaderApp(App):
    def build(self):
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

        root = BoxLayout(orientation="vertical", padding=20, spacing=15)

        root.add_widget(Label(
            text="YouTube Video Downloader",
            font_size="20sp",
            size_hint=(1, 0.12),
            bold=True,
        ))

        self.url_input = TextInput(
            hint_text="YouTube video ka link yahan paste karein",
            multiline=False,
            size_hint=(1, 0.12),
            font_size="16sp",
        )
        root.add_widget(self.url_input)

        self.folder_label = Label(
            text=f"Save to: {self.download_folder}",
            font_size="12sp",
            size_hint=(1, 0.08),
            color=(0.6, 0.6, 0.6, 1),
        )
        root.add_widget(self.folder_label)

        self.download_btn = Button(
            text="Download",
            size_hint=(1, 0.15),
            font_size="18sp",
            background_color=(0.88, 0.17, 0.17, 1),
        )
        self.download_btn.bind(on_press=self.start_download)
        root.add_widget(self.download_btn)

        self.progress = ProgressBar(max=100, value=0, size_hint=(1, 0.08))
        root.add_widget(self.progress)

        self.status_label = Label(
            text="Ready",
            font_size="14sp",
            size_hint=(1, 0.1),
            color=(0.5, 0.5, 0.5, 1),
        )
        root.add_widget(self.status_label)

        return root

    def set_status(self, text, *_args):
        self.status_label.text = text

    def set_progress(self, value, *_args):
        self.progress.value = value

    def start_download(self, instance):
        url = self.url_input.text.strip()
        if not url:
            self.set_status("Pehle YouTube link paste karein.")
            return

        self.download_btn.disabled = True
        self.progress.value = 0
        Clock.schedule_once(lambda dt: self.set_status("Starting download..."))

        thread = threading.Thread(target=self.download_video, args=(url,), daemon=True)
        thread.start()

    def progress_hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total:
                percent = downloaded / total * 100
                Clock.schedule_once(lambda dt: self.set_progress(percent))
                Clock.schedule_once(
                    lambda dt: self.set_status(f"Downloading... {percent:.1f}%")
                )
        elif d["status"] == "finished":
            Clock.schedule_once(lambda dt: self.set_status("Processing..."))

    def download_video(self, url):
        import yt_dlp

        # Android pr ffmpeg bundle karna mushkil hai, is liye single-file
        # (already merged) format prefer karte hain. Kabhi kabhi quality
        # kam ho sakti hai un videos par jo sirf split streams dete hain.
        ydl_opts = {
            "outtmpl": os.path.join(self.download_folder, "%(title)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "progress_hooks": [self.progress_hook],
            "noplaylist": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            Clock.schedule_once(lambda dt: self.set_progress(100))
            Clock.schedule_once(lambda dt: self.set_status("Done! Video download ho gayi."))
        except Exception as e:
            msg = str(e)
            Clock.schedule_once(lambda dt: self.set_status(f"Failed: {msg[:120]}"))
        finally:
            Clock.schedule_once(lambda dt: setattr(self.download_btn, "disabled", False))


if __name__ == "__main__":
    YTDownloaderApp().run()
