"""Android launcher: runs the Flask app offline inside the phone and shows it in a WebView."""
import os, secrets, threading
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.utils import platform

URL = "http://127.0.0.1:5000"


class MSMApp(App):
    title = "Medical Store Manager"

    def build(self):
        data = self.user_data_dir
        os.makedirs(data, exist_ok=True)
        os.environ["MSM_DB"] = os.path.join(data, "msm.db")
        keyfile = os.path.join(data, "secret.key")
        if not os.path.exists(keyfile):
            with open(keyfile, "w") as f:
                f.write(secrets.token_hex(32))
        os.environ["MSM_SECRET"] = open(keyfile).read().strip()

        import medical_store
        medical_store.init_db()
        from werkzeug.serving import make_server
        self.srv = make_server("127.0.0.1", 5000, medical_store.app, threaded=True)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        box = BoxLayout(orientation="vertical", padding=30, spacing=20)
        box.add_widget(Label(text="Medical Store Manager\nOffline - server running\nLogin: admin / admin123",
                             halign="center", font_size="20sp"))
        b1 = Button(text="Open in app", size_hint=(1, .25))
        b1.bind(on_release=lambda *_: self.open_webview())
        b2 = Button(text="Open in browser (best for downloads/uploads)", size_hint=(1, .25))
        b2.bind(on_release=lambda *_: self.open_browser())
        box.add_widget(b1)
        box.add_widget(b2)
        return box

    def open_browser(self):
        if platform != "android":
            import webbrowser; webbrowser.open(URL); return
        from jnius import autoclass
        Intent, Uri = autoclass("android.content.Intent"), autoclass("android.net.Uri")
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(URL)))

    def open_webview(self):
        if platform != "android":
            return self.open_browser()
        from android.runnable import run_on_ui_thread

        @run_on_ui_thread
        def make():
            from jnius import autoclass
            WebView = autoclass("android.webkit.WebView")
            WebViewClient = autoclass("android.webkit.WebViewClient")
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            wv = WebView(act)
            s = wv.getSettings()
            s.setJavaScriptEnabled(True)
            s.setDomStorageEnabled(True)
            wv.setWebViewClient(WebViewClient())
            act.setContentView(wv)
            wv.loadUrl(URL)
        make()


if __name__ == "__main__":
    MSMApp().run()
