[app]
title = Medical Store Manager
package.name = medicalstore
package.domain = org.medstore
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0
requirements = python3,kivy==2.3.0,flask,werkzeug,jinja2,markupsafe,itsdangerous,click,blinker,sqlite3
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True
android.manifest.application_attributes = android:usesCleartextTraffic="true"

[buildozer]
log_level = 2
warn_on_root = 1
