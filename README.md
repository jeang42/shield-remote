# shield-remote

Browser remote for TV boxes on the LAN, one page for all of them (device picker in the header).

- **Android TV / Google TV** (the Shield): Android TV Remote protocol v2, the one the Google
  TV phone app uses (TLS on 6466, pairing code on 6467), via `androidtvremote2`. Live text
  input that mirrors the focused field, current app and volume.
- **adb** (Fire TV sticks, anything with network ADB): one persistent `adb shell` per device.
  Keys, long-press, `input text` on Enter (ASCII only), app launch by package or deep link.
  Fire TV setup: Settings → My Fire TV → Developer Options → ADB debugging on (if Developer
  Options is hidden, click the device name under About 7 times), then accept the
  "Allow debugging?" prompt on the TV when the remote first connects.

Devices live in `data/devices.json`; add them from the page or use "scan network" (probes the
/24 for ports 5555 and 6466). Screenshots use adb for either kind.

    uv run server.py            # http://<this host>:8780/  (REMOTE_PORT to change)

- First run with an Android TV device: the page shows a pairing panel. Press Start; type the code the TV shows.
  The client cert/key live in `data/` (gitignored); delete it to re-pair.
- Shield address comes from `SHIELD_IP` or `../shield.env`, default 192.168.1.3.
- D-pad or swipe touchpad, long-press on OK/Back/Home, hold-to-repeat on arrows and
  volume, media keys, live keyboard (mirrors the focused TV text field), app launcher
  (package names or deep links, editable per browser), screenshot preview.
- Desktop shortcuts: arrows, Enter=OK, Esc/Backspace=Back, h=Home, space=play/pause, +/-, m.
- No authentication: anyone on the LAN who can reach the port can drive the TV.

## Service

Runs as a user systemd unit (`shield-remote.service`, copied to `~/.config/systemd/user/`),
settings in `remote.env`:

    systemctl --user restart shield-remote      # after editing code or remote.env
    journalctl --user -u shield-remote -f

## Behind nginx

All page URLs (WebSocket, screenshots) are relative, so it works at a path prefix with no
`sub_filter` rewriting. Set `REMOTE_HOST=127.0.0.1` in `remote.env`, restart, and proxy:

    location = /remote { return 301 /remote/; }
    location /remote/ {
        proxy_pass http://127.0.0.1:8780/;
        include /etc/nginx/snippets/lan-proxy.conf;   # has the WebSocket Upgrade headers
    }

The trailing slash on `/remote/` matters: the page resolves `ws` and `screenshot` against it.
There is no login, so anything that can reach nginx can drive the TVs.
