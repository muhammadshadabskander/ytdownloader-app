[app]
title = Video Downloader
package.name = ytdownloader
package.domain = org.shadab.ytdownloader

source.dir = .
# "ttf" added so the bundled Urdu font (assets/NotoNaskhArabicUI-Regular.ttf)
# gets packaged into the APK.
source.include_exts = py,png,jpg,kv,atlas,ttf

version = 1.0

# Python 3.11 explicitly pin ki hui hai, kyunky latest Python 3.14 Kivy
# 2.3.0 k C-code k sath compatible nahi (compile errors deta hai).
# Python 3.11 Kivy 2.3.0 k sath fully tested/compatible hai.
#
# arabic-reshaper + python-bidi added: these make Urdu text display
# correctly (properly joined letters) inside the app's bilingual UI.
requirements = python3==3.11.6,hostpython3==3.11.6,kivy==2.3.0,yt-dlp,certifi,requests,urllib3,charset_normalizer,idna,brotli,websockets,pyjnius,arabic-reshaper,python-bidi==0.4.2,six

orientation = portrait
fullscreen = 0

# Android permissions
android.permissions = INTERNET,WRITE_EXTERNAL_STORAGE,READ_EXTERNAL_STORAGE

# Sirf 64-bit target karo (modern phones sab 64-bit hain), 32-bit
# armeabi-v7a build karny ma libffi compile error aata hai (NDK toolchain
# issue), is liye usay skip kr rahe hain.
android.archs = arm64-v8a

# Android API / SDK settings (buildozer defaults usually fine, kept explicit)
android.api = 34
# minapi 24 zaroori hai kyunky Python 3.14 ka 'pwritev' function API 23 pr
# available nahi (NDK headers ma), warna compile error aata hai.
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
