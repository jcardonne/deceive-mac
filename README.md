<p align="center"><img src="assets/icon.png" width="128" alt="Deceive for macOS"></p>

# Deceive for macOS

[![build](https://github.com/jcardonne/deceive-mac/actions/workflows/build.yml/badge.svg)](https://github.com/jcardonne/deceive-mac/actions/workflows/build.yml)
[![release](https://img.shields.io/github/v/release/jcardonne/deceive-mac?label=download)](https://github.com/jcardonne/deceive-mac/releases/latest)
[![license: GPL v3](https://img.shields.io/badge/license-GPLv3-blue.svg)](LICENSE)

Appear offline in **League of Legends**, **Teamfight Tactics** and **Legends of Runeterra**
on macOS, without losing anything you actually use: you can still talk in lobbies and
champion select, invite friends and queue up.

A native macOS port of [Deceive](https://github.com/molenzwiebel/Deceive) for Windows.
Menu bar app, no terminal, no configuration files.

## Install

1. Open the [latest release](https://github.com/jcardonne/deceive-mac/releases/latest)
   and download the DMG for your Mac:
   - `Deceive-1.0.0-arm64.dmg` for Apple Silicon (M1 and later)
   - `Deceive-1.0.0-x64.dmg` for Intel Macs
2. Open the DMG and drag **Deceive** into **Applications**.
3. **First launch only:** in Finder, right-click (or Control-click) **Deceive** and choose
   **Open**, then **Open** again in the dialog. The app is not notarized by Apple, so macOS
   asks once. On macOS 15 and later you may also need
   *System Settings → Privacy & Security → Open Anyway*.
4. Quit the Riot Client if it is running. Deceive offers to close it for you, because it has
   to start the client itself.
5. Double-click **Deceive**, pick your game the first time, and sign in as usual.

An eye icon appears in the menu bar. Your status is masked from that moment on.

## Using it

Click the menu bar icon:

| Item | What it does |
| --- | --- |
| Appear offline | Friends see you offline, you keep playing normally |
| Appear mobile | Shows the phone icon next to your name |
| Appear online | Stops masking without quitting |
| Masking enabled | Master switch (same as above, but keeps the status you picked) |
| Restart into … | Relaunches the Riot Client into another game |
| Show log in Finder | Opens `~/Library/Logs/deceive-mac.log` |
| Quit Deceive | Stops masking and closes the Riot Client |

The header line tells you whether the client is connected: *Appearing offline* alone means
active, *(waiting for the client)* means the Riot Client has not signed in to chat yet.

A fake friend called **Deceive Active!** appears in your friends list. Send it one of these
messages to change settings without leaving the game:

```
offline    mobile    online    enable    disable    status
```

## FAQ

**Why does it need to restart the Riot Client?**
Deceive sits between the client and Riot's chat servers, so it has to start the client with
its own configuration. If the client is already open, Deceive offers to close and relaunch it.

**Can my friends invite me while I appear offline?**
No, that is the point of the feature. Switch to **Appear online**, invite them, then switch
back: once they are in your lobby everything works as usual.

**Does chat in lobbies and champion select still work?**
Yes. Only your global presence is filtered; lobby messages are left untouched.

**My own client still shows me as available. Is it broken?**
No. The LoL client always shows your own local state. What friends see is decided by the
chat presence Deceive rewrites, and you can check it in the log (see below).

**How can I be sure it works?**
Open `~/Library/Logs/deceive-mac.log`. You should see the real chat server and the relay:

```
real chat server is euw1.chat.si.riotgames.com:5223
relaying chat euw1.chat.si.riotgames.com:5223
```

The definitive check is a second account: sign in on a phone or another PC and look at your
friends list. It must show you as offline.

**macOS says the app is damaged, or nothing happens.**
Right-click **Deceive** → **Open**. On macOS 15+, allow it in
*System Settings → Privacy & Security*. If the file was copied around, remove the quarantine
flag once:

```
xattr -dr com.apple.quarantine /Applications/Deceive.app
```

**Deceive says a domain does not resolve.**
The relay uses `deceive-localhost.molenzwiebel.xyz`, which must resolve to `127.0.0.1`.
Change your DNS to 1.1.1.1 or 8.8.8.8, or add this line to `/etc/hosts`:

```
127.0.0.1 deceive-localhost.molenzwiebel.xyz
```

**Can I get banned?**
Riot has [confirmed](https://i.thijsmolendijk.nl/deceive_ok.png) that Deceive itself is fine
to use, and this port uses the same technique. It may break whenever Riot changes something,
and you use it at your own risk.

**What about VALORANT?**
VALORANT has no macOS client, so there is nothing to mask. League, TFT and Legends of
Runeterra work.

**Where is everything stored?**
Settings, the cached TLS certificate and the last status live in
`~/Library/Caches/deceive-mac/`; the log is `~/Library/Logs/deceive-mac.log`.

## Command line (optional)

The same engine is usable without the app, which is handy for scripting or debugging:

```
python3 -m deceive lol                        # launch League through Deceive
python3 -m deceive --status offline --debug   # verbose stanza log
python3 -m deceive --selftest                 # 26 offline checks
```

Requirements: Python 3.11 or newer (`cryptography`, `pyobjc-framework-Cocoa`).

## Build from source

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
ARCH=arm64 ./scripts/build_app.sh     # dist/Deceive.app + dist/Deceive-1.0.0-arm64.dmg
```

`ARCH=x86_64` builds the Intel bundle; run it on an Intel Mac (or CI does it for you).
The icon is generated at build time by `scripts/make_icon.py`, so the repository carries
no hand-made binary assets.

## How it works

1. The Riot Client is started with `--client-config-url` pointing at a local HTTP server.
2. That server forwards the real `clientconfig.rpg.riotgames.com` requests and rewrites
   `chat.host`, `chat.port` and the affinity list to a loopback endpoint.
3. A local TLS relay answers with the publicly trusted certificate for
   `deceive-localhost.molenzwiebel.xyz` and forwards the connection to the real chat server,
   so no trust store or keychain change is needed.
4. `<presence>` stanzas are rewritten on the way out (offline / mobile / online), which is
   exactly what friends see; everything else passes through untouched.

## Credits and license

This is a macOS port of [Deceive](https://github.com/molenzwiebel/Deceive) by Thijs
Molendijk, reimplemented in Python with the same approach. Licensed under the GNU General
Public License v3.0, see [LICENSE](LICENSE).

Not affiliated with Riot Games. League of Legends, Teamfight Tactics and Legends of
Runeterra are trademarks of Riot Games, Inc.
