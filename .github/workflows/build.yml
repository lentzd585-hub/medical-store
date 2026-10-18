name: Build APK
on: [push, workflow_dispatch]
jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install system packages
        run: |
          sudo apt-get update
          sudo apt-get install -y git zip unzip openjdk-17-jdk autoconf libtool pkg-config zlib1g-dev libncurses-dev cmake libffi-dev libssl-dev build-essential ccache
      - name: Install buildozer
        run: pip install --upgrade pip buildozer cython==0.29.36 virtualenv
      - name: Build APK
        run: buildozer -v android debug
      - uses: actions/upload-artifact@v4
        with:
          name: medical-store-apk
          path: bin/*.apk
