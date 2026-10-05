"""Shield Remote: control TV boxes on the LAN from a browser.

Two backends:
- androidtv: Android TV Remote protocol v2 (TLS on 6466, paired once on 6467) via
  androidtvremote2. Used for the Shield; proper live text input, app and volume state.
- adb: adb over TCP (5555) with one persistent `adb shell` per device. For boxes without
  Google's remote service, such as Fire TV sticks. Text is typed with `input text`.

The browser talks to this server over one WebSocket; every message names its device.
Screenshots go through adb for either kind when adb is authorized.
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
from pathlib import Path

from aiohttp import WSMsgType, web
from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEVICES_FILE = DATA / "devices.json"
ADB = os.environ.get("ADB", str(Path.home() / ".local/opt/platform-tools/adb"))
HOST = os.environ.get("REMOTE_HOST", "0.0.0.0")  # 127.0.0.1 once nginx fronts it
PORT = int(os.environ.get("REMOTE_PORT", "8780"))
KINDS = ("androidtv", "adb")

KEY_RE = re.compile(r"^KEYCODE_[A-Z0-9_]+$")
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

log = logging.getLogger("shield-remote")


def shield_ip() -> str:
    if ip := os.environ.get("SHIELD_IP"):
        return ip
    env = HERE.parent / "shield.env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.startswith("SHIELD_IP="):
                return ln.split("=", 1)[1].strip().strip("'\"")
    return "192.168.1.3"


def toast(text: str) -> dict:
    return {"t": "toast", "v": text}


async def run(*args: str, timeout: float = 15) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{args[1:3]} timed out") from None
    return proc.returncode or 0, out, err


class Device:
    """One TV box. Subclasses implement connect/handle; state is pushed via the hub."""

    kind = ""

    def __init__(self, hub: "Hub", dev_id: str, name: str, host: str):
        self.hub, self.id, self.name, self.host = hub, dev_id, name, host
        self.status, self.error = "starting", ""
        self.shot_lock = asyncio.Lock()  # one screencap at a time per device

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "host": self.host}

    def state(self) -> dict:
        return {"t": "state", "dev": self.id, **self.info(), "status": self.status,
                "error": self.error, "is_on": None, "app": None, "volume": None, "device": {}}

    def set_status(self, status: str, error: str = ""):
        if (status, error) == (self.status, self.error):
            return
        self.status, self.error = status, error
        log.info("%s: %s %s", self.id, status, error)
        self.hub.push(self.state())

    async def start(self): ...
    def stop(self): ...
    async def handle(self, msg: dict) -> dict | None: ...

    async def screenshot(self) -> bytes:
        async with self.shot_lock:
            code, out, err = await run(ADB, "-s", f"{self.host}:5555", "exec-out", "screencap", "-p")
        if code or not out.startswith(b"\x89PNG"):
            raise RuntimeError((err or out[:200]).decode(errors="replace").strip() or "screencap failed")
        return out


class AndroidTVDevice(Device):
    kind = "androidtv"

    def __init__(self, *a):
        super().__init__(*a)
        self.remote: AndroidTVRemote | None = None
        self.reconnecting = False
        self.retry: asyncio.TimerHandle | None = None

    async def start(self):
        self.remote = r = AndroidTVRemote(
            client_name="Shield Web Remote",
            certfile=str(DATA / "cert.pem"),
            keyfile=str(DATA / "key.pem"),
            host=self.host,
        )
        if await r.async_generate_cert_if_missing():
            log.info("generated client certificate in %s", DATA)
        r.add_is_on_updated_callback(lambda _: self.hub.push(self.state()))
        r.add_current_app_updated_callback(lambda _: self.hub.push(self.state()))
        r.add_volume_info_updated_callback(lambda _: self.hub.push(self.state()))
        r.add_is_available_updated_callback(self._on_available)
        await self.connect()

    def stop(self):
        if self.retry:
            self.retry.cancel()
        if self.remote:
            self.remote.disconnect()

    async def connect(self):
        assert self.remote
        if self.status == "connected":
            return
        try:
            await self.remote.async_connect()
        except InvalidAuth:
            self.set_status("needs_pairing")
            return
        except (CannotConnect, ConnectionClosed) as exc:
            self.set_status("offline", str(exc))
            self.retry = asyncio.get_running_loop().call_later(
                10, lambda: asyncio.ensure_future(self.connect()))
            return
        self.set_status("connected")
        if not self.reconnecting:  # one background reconnect loop per connection
            self.reconnecting = True
            self.remote.keep_reconnecting(lambda: self.set_status("needs_pairing"))

    def _on_available(self, ok: bool):
        if self.status in ("connected", "offline"):
            self.set_status("connected" if ok else "offline")

    def state(self) -> dict:
        s = super().state()
        r = self.remote
        if self.status == "connected" and r is not None:
            s.update(is_on=r.is_on, app=r.current_app, volume=r.volume_info,
                     device=r.device_info or {})
        return s

    async def handle(self, msg: dict) -> dict | None:
        t, r = msg.get("t"), self.remote
        assert r
        if t == "pair_start":
            self.reconnecting = False  # start_pairing disconnects, ending the reconnect loop
            self.set_status("pairing")
            await r.async_start_pairing()
            return toast("Enter the code shown on the TV")
        if t == "pair_finish":
            try:
                await r.async_finish_pairing(msg["code"].strip().upper())
            except (InvalidAuth, ConnectionClosed) as exc:
                self.set_status("needs_pairing", f"Pairing failed: {exc or 'wrong code?'}")
                return None
            await self.connect()
            return toast("Paired")
        if t == "reconnect":
            await self.connect()
            return None
        if self.status != "connected":
            return toast(f"{self.name} not connected ({self.status})")
        if t == "key":
            r.send_key_command(msg["k"], msg.get("d", "SHORT"))
        elif t == "text":
            # The protocol sets the whole field, so the browser sends its full box each edit.
            if msg["v"]:
                r.send_text(msg["v"])
            else:
                r.send_key_command("KEYCODE_DEL")
        elif t == "app":
            r.send_launch_app_command(msg["v"])
        else:
            return toast(f"Unknown command {t!r}")
        return None


class AdbDevice(Device):
    """adb over TCP. Commands go down one long-lived `adb shell` so presses stay snappy."""

    kind = "adb"

    def __init__(self, *a):
        super().__init__(*a)
        self.target = f"{self.host}:5555"
        self.shell: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self._watch())

    def stop(self):
        if self.task:
            self.task.cancel()
        self._close_shell()

    def _close_shell(self):
        if self.shell and self.shell.returncode is None:
            self.shell.kill()
        self.shell = None

    async def _watch(self):
        """Keep the device connected: (re)connect when needed, re-check every 10 s."""
        while True:
            try:
                if not await self._healthy():
                    self._close_shell()
                    await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.set_status("offline", str(exc))
            await asyncio.sleep(10 if self.status == "connected" else 5)

    async def _state(self) -> tuple[str, str]:
        code, out, err = await run(ADB, "-s", self.target, "get-state", timeout=10)
        return out.decode().strip(), err.decode().strip()

    async def _healthy(self) -> bool:
        if not self.shell or self.shell.returncode is not None:
            return False
        state, _ = await self._state()
        return state == "device"

    async def _connect(self):
        await run(ADB, "connect", self.target, timeout=10)
        state, err = await self._state()
        if state != "device":
            if "unauthorized" in err:
                self.set_status("unauthorized", "Accept the 'Allow debugging' prompt on the TV")
            else:
                self.set_status("offline", err.splitlines()[-1] if err else "not reachable on :5555")
            return
        self.shell = await asyncio.create_subprocess_exec(
            ADB, "-s", self.target, "shell",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        self.set_status("connected")

    async def _send(self, cmd: str):
        if not self.shell or self.shell.returncode is not None or not self.shell.stdin:
            self.set_status("offline", "adb shell closed")
            raise RuntimeError("adb shell closed; reconnecting")
        self.shell.stdin.write(cmd.encode() + b"\n")
        await self.shell.stdin.drain()

    async def handle(self, msg: dict) -> dict | None:
        t = msg.get("t")
        if t == "reconnect":
            self._close_shell()
            await self._connect()
            return None
        if self.status != "connected":
            return toast(f"{self.name} not connected ({self.status})")
        if t == "key":
            k = msg["k"]
            if not KEY_RE.match(k):
                return toast(f"Bad key {k!r}")
            d = msg.get("d", "SHORT")
            if d == "START_LONG":
                await self._send(f"input keyevent --longpress {k}")
            elif d == "SHORT":
                await self._send(f"input keyevent {k}")
            # END_LONG: the long press was already sent whole
        elif t == "text":
            text = msg["v"]
            if not text:
                return None
            if not text.isascii():
                return toast("adb can only type plain ASCII text")
            # `input text` reads %s as a space; quote the rest for the device shell.
            await self._send("input text " + shlex.quote(text.replace(" ", "%s")))
        elif t == "app":
            target = msg["v"].strip()
            if PKG_RE.match(target):
                pkg = target
                await self._send(
                    "c=$(cmd package resolve-activity --brief -a android.intent.action.MAIN"
                    f" -c android.intent.category.LEANBACK_LAUNCHER {pkg} | tail -n 1);"
                    ' case "$c" in */*) am start -n "$c" ;;'
                    f" *) monkey -p {pkg} -c android.intent.category.LAUNCHER 1 ;; esac"
                    " >/dev/null 2>&1")
            else:
                await self._send("am start -a android.intent.action.VIEW -d "
                                 + shlex.quote(target) + " >/dev/null 2>&1")
        else:
            return toast(f"Unknown command {t!r}")
        return None


class Hub:
    """Owns the devices and fans their state out to every open browser."""

    def __init__(self):
        DATA.mkdir(exist_ok=True)
        self.clients: set[web.WebSocketResponse] = set()
        self.devices: dict[str, Device] = {}

    def load(self) -> list[dict]:
        if DEVICES_FILE.exists():
            return json.loads(DEVICES_FILE.read_text())
        return [{"id": "shield", "name": "Shield", "kind": "androidtv", "host": shield_ip()}]

    def save(self):
        DEVICES_FILE.write_text(json.dumps([d.info() for d in self.devices.values()], indent=2))

    async def start(self):
        for d in self.load():
            await self.add(d["name"], d["host"], d["kind"], d["id"], save=False)
        self.save()

    def stop(self):
        for d in self.devices.values():
            d.stop()

    async def add(self, name: str, host: str, kind: str, dev_id: str = "", save: bool = True) -> Device:
        name, host = name.strip(), host.strip()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        ipaddress.ip_address(host)  # raises ValueError on junk
        if not name:
            raise ValueError("name is required")
        if not dev_id:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tv"
            dev_id, n = base, 2
            while dev_id in self.devices:
                dev_id, n = f"{base}-{n}", n + 1
        cls = AndroidTVDevice if kind == "androidtv" else AdbDevice
        dev = cls(self, dev_id, name, host)
        self.devices[dev_id] = dev
        if save:
            self.save()
            self.push_devices()
        await dev.start()
        return dev

    def remove(self, dev_id: str):
        dev = self.devices.pop(dev_id)
        dev.stop()
        self.save()
        self.push_devices()

    def device_list(self) -> dict:
        return {"t": "devices", "list": [d.info() for d in self.devices.values()]}

    def push_devices(self):
        self.push(self.device_list())
        for d in self.devices.values():
            self.push(d.state())

    def push(self, msg: dict):
        data = json.dumps(msg)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(ws.send_str(data))

    async def scan(self) -> dict:
        """Probe this /24 for adb (5555) and Android TV remote (6466) listeners."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((shield_ip(), 9))  # no packets sent; just picks the LAN interface
            me = s.getsockname()[0]
        net = ipaddress.ip_network(f"{me}/24", strict=False)
        sem = asyncio.Semaphore(128)

        async def probe(ip: str, port: int) -> bool:
            async with sem:
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.8)
                except (OSError, asyncio.TimeoutError):
                    return False
                w.close()
                return True

        hosts = [str(h) for h in net.hosts() if str(h) != me]
        adb, atv = await asyncio.gather(
            asyncio.gather(*(probe(h, 5555) for h in hosts)),
            asyncio.gather(*(probe(h, 6466) for h in hosts)))
        known = {d.host: d.name for d in self.devices.values()}
        found = []
        for h, a, v in zip(hosts, adb, atv):
            if a or v:
                try:
                    hostname = (await asyncio.wait_for(
                        asyncio.to_thread(socket.gethostbyaddr, h), 2))[0]
                except (OSError, asyncio.TimeoutError):
                    hostname = ""
                found.append({"host": h, "adb": a, "androidtv": v, "hostname": hostname,
                              "known": known.get(h, "")})
        return {"t": "scan", "net": str(net), "found": found}

    async def handle(self, msg: dict) -> dict | None:
        t = msg.get("t")
        if t == "add_device":
            dev = await self.add(msg.get("name", ""), msg.get("host", ""), msg.get("kind", ""))
            return {"t": "added", "dev": dev.id}
        if t == "remove_device":
            self.remove(msg["dev"])
            return toast("Removed")
        if t == "scan":
            return await self.scan()
        dev = self.devices.get(msg.get("dev", ""))
        if not dev:
            return toast("Pick a device first")
        return await dev.handle(msg)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    hub.clients.add(ws)
    await ws.send_str(json.dumps(hub.device_list()))
    for d in hub.devices.values():
        await ws.send_str(json.dumps(d.state()))
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                reply = await hub.handle(json.loads(m.data))
            except ValueError as exc:  # bad input from the page; no traceback needed
                reply = toast(str(exc))
            except Exception as exc:  # report to the page instead of dropping the socket
                log.exception("command failed: %s", m.data)
                reply = toast(f"Error: {exc}")
            if reply:
                await ws.send_str(json.dumps(reply))
    finally:
        hub.clients.discard(ws)
    return ws


async def screenshot_handler(request: web.Request) -> web.Response:
    dev = request.app["hub"].devices.get(request.query.get("dev", ""))
    if not dev:
        return web.Response(status=404, text="Unknown device")
    try:
        png = await dev.screenshot()
    except Exception as exc:
        return web.Response(status=502, text=f"Screenshot unavailable: {exc}")
    return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(HERE / "static" / "index.html")


async def on_startup(app: web.Application):
    await app["hub"].start()


async def on_cleanup(app: web.Application):
    app["hub"].stop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = web.Application()
    app["hub"] = Hub()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/screenshot", screenshot_handler)
    app.router.add_static("/static", HERE / "static")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
