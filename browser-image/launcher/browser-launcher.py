#!/usr/bin/env python3
"""Browser chooser shown in every remote session (plan §23-24).

Two buttons, Google Chrome and Mozilla Firefox. The chosen browser runs as this (unprivileged)
user while the chooser is hidden; when it exits, normally or by crashing, the chooser comes back.
Runs only the two fixed commands below and never takes a command from the user or the environment.
No web server, no Docker access.

Keys: C or 1 = Chrome, F or 2 = Firefox.
"""

import glob
import os
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

HOME = os.path.expanduser("~")

# The container has no keyring: Chrome stores saved passwords protected only by file
# permissions on the host (plan §24). Never --no-sandbox (plan §44).
CHROME = {
    "label": "Google Chrome",
    "icon": "google-chrome",
    "argv": [
        "/usr/bin/google-chrome-stable",
        "--start-maximized",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
    ],
    "process_names": {"chrome"},
    # Chrome keeps hostname-pid in SingletonLock; the container hostname is stable (browser-<uid>).
    "locks": [os.path.join(HOME, ".config/google-chrome/Singleton*")],
}
FIREFOX = {
    "label": "Mozilla Firefox",
    "icon": "firefox",
    "argv": ["/usr/bin/firefox"],
    "process_names": {"firefox", "firefox-bin"},
    # Older profiles live in ~/.mozilla, newer Firefox releases use ~/.config/mozilla (XDG).
    "locks": [
        os.path.join(HOME, base, "firefox/*", name)
        for base in (".mozilla", ".config/mozilla")
        for name in ("lock", ".parentlock")
    ],
}

# A browser that exits this fast with an error almost certainly failed to start.
QUICK_FAILURE_SECONDS = 5


def running(process_names):
    """True if a process with one of these names runs as this user."""
    uid = os.getuid()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() in process_names:
                    return True
        except OSError:
            continue
    return False


def clear_stale_locks(browser):
    """Remove profile locks left by a crash or container restart, only if the browser is not running."""
    if running(browser["process_names"]):
        return
    for pattern in browser["locks"]:
        for path in glob.glob(pattern):
            try:
                os.unlink(path)
                print(f"browser-launcher: removed stale lock {path}", flush=True)
            except OSError as exc:
                print(f"browser-launcher: cannot remove {path}: {exc}", file=sys.stderr, flush=True)


class Launcher(Gtk.Window):
    def __init__(self):
        super().__init__(title="Choose your browser")
        self.set_default_size(520, 360)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", lambda *_: True)  # closing the chooser is not an exit path
        self.connect("key-press-event", self.on_key)
        self.child_pid = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label()
        title.set_markup('<span size="xx-large" weight="bold">Choose your browser</span>')
        box.pack_start(title, False, False, 0)

        for browser in (CHROME, FIREFOX):
            button = Gtk.Button()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            content.pack_start(Gtk.Image.new_from_icon_name(browser["icon"], Gtk.IconSize.DIALOG), False, False, 0)
            label = Gtk.Label()
            label.set_markup(f'<span size="x-large">{GLib.markup_escape_text(browser["label"])}</span>')
            content.pack_start(label, False, False, 0)
            button.add(content)
            button.set_size_request(360, 96)
            button.connect("clicked", lambda _b, br=browser: self.launch(br))
            box.pack_start(button, False, False, 0)

        self.status = Gtk.Label()
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(60)
        box.pack_start(self.status, False, False, 0)

        self.add(box)

    def on_key(self, _widget, event):
        key = Gtk.accelerator_name(event.keyval, 0).lower()
        if key in ("c", "1", "kp_1"):
            self.launch(CHROME)
        elif key in ("f", "2", "kp_2"):
            self.launch(FIREFOX)
        return False

    def launch(self, browser):
        if self.child_pid is not None:
            return
        clear_stale_locks(browser)
        self.status.set_text("")
        try:
            proc = subprocess.Popen(browser["argv"], cwd=HOME, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            self.status.set_text(f"Could not start {browser['label']}: {exc}")
            return
        print(f"browser-launcher: started {browser['label']} (pid {proc.pid})", flush=True)
        self.child_pid = proc.pid
        started = time.monotonic()
        self.hide()
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, self.on_exit, (browser, started))

    def on_exit(self, pid, status, data):
        browser, started = data
        code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
        print(f"browser-launcher: {browser['label']} exited ({code})", flush=True)
        GLib.spawn_close_pid(pid)
        self.child_pid = None
        if code != 0 and time.monotonic() - started < QUICK_FAILURE_SECONDS:
            self.status.set_text(f"{browser['label']} failed to start (exit code {code}). Ask your administrator to check the container log.")
        self.show_all()
        self.present()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    window = Launcher()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
