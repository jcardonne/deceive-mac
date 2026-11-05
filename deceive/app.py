"""Menu bar app: the double-click entry point for non-developers."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import objc
from AppKit import (
    NSAlert,
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper

from .core import (
    CACHE_DIR,
    LOG_PATH,
    PRODUCTS,
    Session,
    riot_client_running,
    saved_status,
)

GAME_CHOICES = (("lol", "League of Legends"), ("lor", "Legends of Runeterra"), ("rc", "Riot Client only"))
STATUS_LABELS = {"offline": "Offline", "mobile": "Mobile", "chat": "Online"}
GAME_FILE = CACHE_DIR / "game"


def remember(key: str, value: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / key).write_text(value)


def recall(key: str) -> str | None:
    path = CACHE_DIR / key
    return path.read_text().strip() if path.exists() else None


def alert(title: str, message: str, buttons: tuple[str, ...] = ("OK",)) -> int:
    NSApp.activateIgnoringOtherApps_(True)
    box = NSAlert.alloc().init()
    box.setMessageText_(title)
    box.setInformativeText_(message)
    for label in buttons:
        box.addButtonWithTitle_(label)
    return box.runModal() - 1000  # NSAlertFirstButtonReturn == 1000


class DeceiveApp(NSObject):
    def init(self):
        self = objc.super(DeceiveApp, self).init()
        if self is None:
            return None
        self.session: Session | None = None
        self.status_item = None
        self.game = recall("game")
        self.status = saved_status()
        self._items: dict[str, NSMenuItem] = {}
        self._quit = False
        self._restarting = False
        self._grace_until = 0.0
        return self

    # ---------------------------------------------------------------- startup
    def applicationDidFinishLaunching_(self, notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._build_menu()

        if riot_client_running():
            choice = alert(
                "Deceive needs to restart the Riot Client",
                "To mask your online status, the Riot Client has to be started by "
                "Deceive. Continue to close it and relaunch it now?",
                ("Continue", "Quit Deceive"),
            )
            if choice != 0:
                NSApp.terminate_(None)
                return
            self._kill_client(wait=True)

        if self.game not in PRODUCTS:
            choice = alert(
                "Which game do you play?",
                "Deceive will start this game for you. You can change it later from "
                "the menu bar.",
                tuple(label for _, label in GAME_CHOICES),
            )
            self.game = GAME_CHOICES[max(0, min(len(GAME_CHOICES) - 1, choice))][0]

        if not self._start_session():
            NSApp.terminate_(None)
            return
        self._launch_game(self.game)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            3.0, self, "tick:", None, True,
        )

    def _start_session(self) -> bool:
        try:
            self.session = Session(self.status)
            self.session.start()
        except SystemExit as exc:
            alert("Deceive could not start", str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            alert("Deceive could not start", f"{exc}\n\nLog: {LOG_PATH}")
            return False
        return True

    def _launch_game(self, game: str) -> None:
        if self.session is None:
            return
        self.game = game
        remember("game", game)
        self._grace_until = time.time() + 30
        if not self.session.launch(game):
            alert("Deceive could not launch the game",
                  f"The Riot Client binary could not be started.\n\nLog: {LOG_PATH}")
        self._restarting = False

    def _kill_client(self, wait: bool) -> None:
        if self.session is not None:
            self.session.quit_client()
        else:
            subprocess.run(["pkill", "-f", "RiotClientServices"], check=False, capture_output=True)
        if wait:
            for _ in range(40):
                if not riot_client_running():
                    return
                time.sleep(0.25)

    # ------------------------------------------------------------------- menu
    def _build_menu(self) -> None:
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        button = self.status_item.button()
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "eye.slash", "Deceive",
        )
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
        button.setToolTip_("Deceive")

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Deceive", None, "")
        header.setEnabled_(False)
        menu.addItem_(header)
        self._items["header"] = header
        menu.addItem_(NSMenuItem.separatorItem())

        for key in ("offline", "mobile", "chat"):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"Appear {STATUS_LABELS[key].lower()}", "chooseStatus:", "",
            )
            item.setTarget_(self)
            item.setRepresentedObject_(key)
            menu.addItem_(item)
            self._items[key] = item

        menu.addItem_(NSMenuItem.separatorItem())
        enabled = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Masking enabled", "toggleEnabled:", "",
        )
        enabled.setTarget_(self)
        menu.addItem_(enabled)
        self._items["enabled"] = enabled

        menu.addItem_(NSMenuItem.separatorItem())
        for key, label in GAME_CHOICES:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"Restart into {label}", "restartInto:", "",
            )
            item.setTarget_(self)
            item.setRepresentedObject_(key)
            menu.addItem_(item)

        log_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show log in Finder", "openLog:", "",
        )
        log_item.setTarget_(self)
        menu.addItem_(log_item)

        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Deceive", "quit:", "",
        )
        quit_item.setTarget_(self)
        menu.addItem_(quit_item)

        self.status_item.setMenu_(menu)
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        session = self.session
        deceive = session.deceive if session else None
        state = deceive.status if deceive else self.status
        connected = bool(deceive.connections) if deceive else False
        masking = bool(deceive.enabled) if deceive else True

        self._items["header"].setTitle_(
            f"Appearing {STATUS_LABELS[state].lower()}"
            + ("" if connected else " (waiting for the client)")
        )
        for key in ("offline", "mobile", "chat"):
            self._items[key].setState_(1 if key == state else 0)
        self._items["enabled"].setState_(1 if masking else 0)
        if self.status_item.button() is not None:
            self.status_item.button().setAlphaValue_(1.0 if masking else 0.35)

    # ---------------------------------------------------------------- actions
    def chooseStatus_(self, sender):
        key = sender.representedObject()
        self.status = key
        if self.session is not None:
            self.session.deceive.set_status(key)
        self._refresh_menu()

    def toggleEnabled_(self, sender):
        if self.session is None:
            return
        deceive = self.session.deceive
        deceive.set_enabled(not deceive.enabled)
        self._refresh_menu()

    def restartInto_(self, sender):
        key = sender.representedObject()
        self._restart_game_with(key)

    def _restart_game_with(self, key: str) -> None:
        if self.session is None:
            self._launch_game(key)
            return
        self.game = key
        threading.Thread(target=self._restart_thread, daemon=True).start()

    def _restart_thread(self) -> None:
        self._restarting = True
        self._kill_client(wait=True)
        AppHelper.callAfter(self._launch_game, self.game)

    def openLog_(self, sender):
        subprocess.run(["open", "-R", str(LOG_PATH)], check=False)

    def quit_(self, sender):
        if self.session is not None and riot_client_running():
            choice = alert(
                "Quit Deceive?",
                "Your status will stop being masked and the Riot Client will be closed.",
                ("Quit Deceive", "Cancel"),
            )
            if choice != 0:
                return
        self._quit = True
        self._kill_client(wait=False)
        if self.session is not None:
            self.session.stop()
        NSApp.terminate_(None)

    def tick_(self, timer):
        if self._quit:
            return
        self._refresh_menu()
        if self.session is None:
            return
        if self._restarting or time.time() < self._grace_until:
            return  # the client is being killed or has just been started
        if self.session.client_running():
            return
        self._refresh_menu()
        alert("Riot Client closed",
              "The Riot Client has exited, so Deceive stopped masking your status.")
        if self.session is not None:
            self.session.stop()
        NSApp.terminate_(None)


def run() -> None:
    app = NSApplication.sharedApplication()
    delegate = DeceiveApp.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    sys.exit(run())
