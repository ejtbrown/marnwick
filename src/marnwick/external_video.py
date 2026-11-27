"""Default-player discovery and process lifetime tracking (no shell commands).

Native queues are deliberately an allowlist. A launch helper finishing is never
treated as a player finishing. Unknown lifecycle integrations fail closed after
opening the current file, rather than launching the rest simultaneously.
"""
from __future__ import annotations

import configparser
from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from queue import Queue
from threading import Event, Thread
from time import monotonic


class PlayerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DefaultPlayer:
    name: str
    identity: str
    executable: str | None = None
    desktop_file: Path | None = None
    dbus_name: str | None = None

    @property
    def native_queue(self) -> bool:
        return self.executable is not None and (
            Path(self.executable).name.lower() == "vlc"
            and self.identity.lower() in {
                "vlc.desktop", "org.videolan.vlc.desktop"
            }
        )


def _run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True,
                              timeout=5, env=env).stdout.strip()
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[:240] or f"exit status {exc.returncode}"
        raise PlayerError(f"Default-player operation failed ({Path(args[0]).name}): {detail}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlayerError(f"Default-player operation unavailable or timed out ({Path(args[0]).name}).") from exc


def _tool(name: str) -> str:
    result = shutil.which(name)
    if result is None:
        raise PlayerError(f"Default-player integration requires {name}, which was not found.")
    return result


def _desktop_file(identity: str) -> Path:
    if not identity.endswith(".desktop") or "/" in identity or "\\" in identity:
        raise PlayerError("The default player returned an invalid desktop entry ID.")
    roots = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")]
    roots.extend((os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"))
    for root in roots:
        if not Path(root).is_absolute():
            continue
        directory = Path(root) / "applications"
        direct = directory / identity
        if direct.is_file():
            return direct
        # A desktop ID encodes subdirectories as hyphens.
        for path in directory.glob("**/*.desktop"):
            if str(path.relative_to(directory)).replace(os.sep, "-") == identity:
                return path
    raise PlayerError(f"Cannot locate the default player's desktop entry: {identity}")


def _linux_player(path: Path) -> DefaultPlayer:
    mime = _run([_tool("xdg-mime"), "query", "filetype", str(path)])
    identity = _run([_tool("xdg-mime"), "query", "default", mime])
    desktop = _desktop_file(identity)
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.read(desktop, encoding="utf-8")
    try:
        entry = config["Desktop Entry"]
        command = shlex.split(entry.get("Exec", ""))
        executable = shutil.which(command[0]) if command else None
        # Wrapped/sandboxed VLC needs its own verified adapter; do not discard
        # wrapper arguments or mistake flatpak/snap for the player process.
        # Activation uses the filename, not the desktop ID's encoded directories.
        dbus = desktop.name[:-8] if entry.getboolean("DBusActivatable", False) else None
        return DefaultPlayer(entry.get("Name", identity), identity, executable, desktop, dbus)
    except (KeyError, ValueError) as exc:
        raise PlayerError(f"Invalid desktop entry for {identity}") from exc


def default_player(path: Path) -> DefaultPlayer:
    return _linux_player(path)


def vlc_command(player: DefaultPlayer, paths: tuple[Path, ...]) -> list[str]:
    if not player.native_queue:
        raise PlayerError("This default player has no known native queue adapter.")
    # A dedicated instance, ordered playlist, no saved shuffle/repeat behavior.
    # URIs avoid options, whitespace, and VLC's colon-prefixed item options.
    options = ["--no-random", "--no-loop", "--no-repeat", "--no-play-and-stop",
               "--no-play-and-pause", "--no-start-paused", "--playlist-autostart", "--",
               *(path.resolve().as_uri() for path in paths)]
    return [player.executable, "--no-one-instance", "--no-playlist-enqueue", *options]


def launch_native(player: DefaultPlayer, paths: tuple[Path, ...]):
    try:
        return subprocess.Popen(vlc_command(player, paths), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise PlayerError(f"Could not start {player.name}: {exc}") from exc


def _dbus(method: str, value: str, *arguments: str) -> str:
    return _run([_tool("gdbus"), "call", "--session", "--dest", "org.freedesktop.DBus",
                 "--object-path", "/org/freedesktop/DBus", "--method",
                 "org.freedesktop.DBus." + method, "--timeout", "5", "--", value, *arguments],
                env={**os.environ, "LC_ALL": "C"})


def _dbus_owner(name: str) -> str | None:
    try:
        result = _dbus("GetNameOwner", name)
    except PlayerError as exc:
        error = exc.__cause__
        if (isinstance(error, subprocess.CalledProcessError)
                and (error.stderr or "").startswith(
                    "Error: GDBus.Error:org.freedesktop.DBus.Error.NameHasNoOwner:")):
            return None
        raise
    match = re.fullmatch(r"\('(:[0-9]+\.[0-9]+)',\)", result)
    if match is None:
        raise PlayerError("Could not identify the default player's D-Bus connection.")
    return match[1]


class _MessageIter(ctypes.Structure):
    # Public DBusMessageIter storage layout from dbus-message.h.
    _fields_ = [
        ("pointers", ctypes.c_void_p * 2),
        ("u32", ctypes.c_uint32),
        ("integers", ctypes.c_int * 9),
        ("padding", ctypes.c_void_p * 2),
    ]


class LinuxOwnerSignals:
    """Track ownership and dispatch Open on one private libdbus connection."""

    def __init__(self):
        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        try:
            bus = ctypes.CDLL("libdbus-1.so.3")
        except OSError as exc:
            raise PlayerError("D-Bus ownership tracking requires libdbus-1.so.3.") from exc
        pointer, string, integer = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
        signatures = {
            "threads_init_default": (integer,),
            "bus_get_private": (pointer, integer, pointer),
            "bus_register": (integer, pointer, pointer),
            "connection_open_private": (pointer, string, pointer),
            "connection_set_exit_on_disconnect": (None, pointer, integer),
            "connection_close": (None, pointer),
            "connection_unref": (None, pointer),
            "connection_send_with_reply_and_block": (pointer, pointer, pointer, integer, pointer),
            "connection_pop_message": (pointer, pointer),
            "message_new_method_call": (pointer, string, string, string, string),
            "message_append_args": (integer, pointer, integer),
            "message_is_signal": (integer, pointer, string, string),
            "message_get_type": (integer, pointer),
            "message_get_sender": (string, pointer),
            "message_get_args": (integer, pointer, pointer, integer),
            "message_has_signature": (integer, pointer, string),
            "message_iter_init_append": (None, pointer, pointer),
            "message_iter_open_container": (integer, pointer, integer, string, pointer),
            "message_iter_append_basic": (integer, pointer, integer, pointer),
            "message_iter_close_container": (integer, pointer, pointer),
            "message_iter_init": (integer, pointer, pointer),
            "message_iter_next": (integer, pointer),
            "message_iter_get_arg_type": (integer, pointer),
            "message_iter_recurse": (None, pointer, pointer),
            "message_iter_get_basic": (None, pointer, pointer),
            "message_unref": (None, pointer),
        }
        for name, (result, *args) in signatures.items():
            function = getattr(bus, "dbus_" + name)
            function.restype = result
            function.argtypes = args
            setattr(self, "_" + name, function)
        if not self._threads_init_default():
            raise PlayerError("Could not initialize D-Bus ownership tracking.")
        if address:
            self.connection = self._connection_open_private(address.encode(), None)
        else:
            self.connection = self._bus_get_private(0, None)  # DBUS_BUS_SESSION
        if not self.connection:
            raise PlayerError("Could not connect to the session bus to track ownership.")
        self._connection_set_exit_on_disconnect(self.connection, 0)
        if address and not self._bus_register(self.connection, None):
            self.close()
            raise PlayerError("Could not register D-Bus ownership tracking.")

    def close(self):
        self._connection_close(self.connection)
        self._connection_unref(self.connection)

    def subscribe(self, name: str):
        rule = ("type='signal',sender='org.freedesktop.DBus',"
                "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                f"path='/org/freedesktop/DBus',arg0='{name}'")
        self._bus_call(b"AddMatch", rule.encode())

    def _bus_call(self, method: bytes, argument: bytes | None = None):
        reply = self._bus_reply(method, argument)
        self._message_unref(reply)

    def _bus_reply(self, method: bytes, argument: bytes | None = None):
        interface = b"org.freedesktop.DBus"
        request = self._message_new_method_call(interface, b"/org/freedesktop/DBus", interface, method)
        if not request:
            raise PlayerError("Could not synchronize D-Bus ownership tracking.")
        try:
            if argument is not None and not self._message_append_args(
                    request, ord("s"), ctypes.byref(ctypes.c_char_p(argument)), ctypes.c_int(0)):
                raise PlayerError("Could not encode the D-Bus ownership request.")
            reply = self._connection_send_with_reply_and_block(self.connection, request, 5000, None)
            if not reply:
                raise PlayerError("Could not synchronize D-Bus ownership tracking.")
            return reply
        finally:
            self._message_unref(request)

    def open(self, name: str, owner: str, object_path: str, uri: str, cancelled: Event,
             check_binding: Callable[[], None]):
        destination = owner.encode()
        # Helper lookups are advisory. Check the name on the connection that
        # supplied ProcessFD before sending any file data.
        reply = self._bus_reply(b"GetNameOwner", name.encode())
        try:
            current = ctypes.c_char_p()
            if (self._message_get_sender(reply) != b"org.freedesktop.DBus"
                    or not self._message_has_signature(reply, b"s")
                    or not self._message_get_args(reply, None, ord("s"),
                                                  ctypes.byref(current), ctypes.c_int(0))
                    or current.value != destination):
                raise PlayerError("Player identity changed during launch.")
        finally:
            self._message_unref(reply)
        request = self._message_new_method_call(
            destination, object_path.encode(), b"org.freedesktop.Application", b"Open")
        if not request:
            raise PlayerError("Could not create the default player's D-Bus Open request.")
        with ExitStack() as resources:
            resources.callback(self._message_unref, request)
            arguments, urls, platform_data = (ctypes.byref(_MessageIter()) for _ in range(3))
            self._message_iter_init_append(request, arguments)
            encoded_uri = ctypes.c_char_p(uri.encode())
            if not (
                self._message_iter_open_container(arguments, ord("a"), b"s", urls)
                and self._message_iter_append_basic(urls, ord("s"), ctypes.byref(encoded_uri))
                and self._message_iter_close_container(arguments, urls)
                and self._message_iter_open_container(arguments, ord("a"), b"{sv}", platform_data)
                and self._message_iter_close_container(arguments, platform_data)
            ):
                raise PlayerError("Could not encode the default player's D-Bus Open request.")

            _check_cancelled(cancelled)
            check_binding()
            _check_cancelled(cancelled)
            reply = self._connection_send_with_reply_and_block(self.connection, request, 5000, None)
            if not reply:
                raise PlayerError("D-Bus Open failed or timed out. Queue stopped.")
            resources.callback(self._message_unref, reply)
            if (self._message_get_type(reply) != 2  # DBUS_MESSAGE_TYPE_METHOD_RETURN
                    or self._message_get_sender(reply) != destination
                    or not self._message_has_signature(reply, b"")):
                raise PlayerError("The default player returned an invalid Open acknowledgment. Queue stopped.")

    def pin_process(self, owner: str):
        reply = self._bus_reply(b"GetConnectionCredentials", owner.encode())
        with ExitStack() as resources:
            resources.callback(self._message_unref, reply)
            if (self._message_get_sender(reply) != b"org.freedesktop.DBus"
                    or not self._message_has_signature(reply, b"a{sv}")):
                raise PlayerError("Invalid D-Bus connection credentials.")
            root, entries, entry, value, process_fd = (ctypes.byref(_MessageIter()) for _ in range(5))
            self._message_iter_init(reply, root)
            self._message_iter_recurse(root, entries)
            pid = 0
            seen = set()
            while self._message_iter_get_arg_type(entries):
                self._message_iter_recurse(entries, entry)
                key = ctypes.c_char_p()
                self._message_iter_get_basic(entry, ctypes.byref(key))
                if key.value in {b"ProcessID", b"ProcessFD"}:
                    if key.value in seen:
                        raise PlayerError("Duplicate D-Bus process credentials.")
                    seen.add(key.value)
                    self._message_iter_next(entry)
                    self._message_iter_recurse(entry, value)
                    kind = self._message_iter_get_arg_type(value)
                    if key.value == b"ProcessID" and kind == ord("u"):
                        process_id = ctypes.c_uint32()
                        self._message_iter_get_basic(value, ctypes.byref(process_id))
                        pid = process_id.value
                    elif key.value == b"ProcessFD" and kind == ord("h"):
                        self._message_iter_recurse(entry, process_fd)
                    else:
                        raise PlayerError("Invalid D-Bus process credential type.")
                self._message_iter_next(entries)
            if not pid or b"ProcessFD" not in seen:
                raise PlayerError("The session bus did not provide a connection-bound process handle.")
            # libdbus returns a duplicate owned by the caller.
            descriptor = ctypes.c_int(-1)
            self._message_iter_get_basic(process_fd, ctypes.byref(descriptor))
            if descriptor.value < 0:
                raise PlayerError("Could not retain the connection-bound process handle.")
            return LinuxPlayerProcess(pid, descriptor.value)

    def pending(self):
        # GetId is a barrier on this same connection. The bus sends preceding
        # ownership signals before its reply, even if a replacement already left.
        self._bus_call(b"GetId")
        while message := self._connection_pop_message(self.connection):
            try:
                if (self._message_get_sender(message) == b"org.freedesktop.DBus"
                        and self._message_is_signal(message, b"org.freedesktop.DBus", b"NameOwnerChanged")):
                    name, old, new = ctypes.c_char_p(), ctypes.c_char_p(), ctypes.c_char_p()
                    if not self._message_get_args(message, None, ord("s"), ctypes.byref(name),
                                                  ctypes.c_int(ord("s")), ctypes.byref(old),
                                                  ctypes.c_int(ord("s")), ctypes.byref(new), ctypes.c_int(0)):
                        raise PlayerError("Could not read a D-Bus ownership transition.")
                    yield name.value.decode(), old.value.decode(), new.value.decode()
            finally:
                self._message_unref(message)


class QueueOwnership:
    """Keep the last acknowledged owner under observation through the next Open."""

    def __init__(self, resources: ExitStack):
        self.resources = resources
        self.signals = None
        self.subscribed = set()
        self.changes = {}
        self.previous = None

    def observe(self, name: str):
        if self.signals is None:
            self.signals = LinuxOwnerSignals()
            self.resources.callback(self.signals.close)
        if name not in self.subscribed:
            self.signals.subscribe(name)
            self.subscribed.add(name)
        self.changes.setdefault(name, [])

    def check(self, name: str | None = None, expected: tuple[tuple[str, str], ...] = (),
              *, waiting: bool = False):
        if self.signals is None:
            return
        for changed, old, new in self.signals.pending():
            if changed in self.changes:
                self.changes[changed].append((old, new))
        if self.previous is not None:
            previous_name, previous_owner = self.previous
            release = [(previous_owner, "")]
            if previous_name == name:
                expected = (*release, *expected)
            else:
                observed = self.changes[previous_name]
                if observed != release and not (waiting and not observed):
                    raise PlayerError("Player identity changed while tracking D-Bus ownership.")
        if name is not None and self.changes[name] != list(expected):
            raise PlayerError("Player identity changed while tracking D-Bus ownership.")

    def acknowledge(self, name: str, owner: str):
        self.changes = {name: []}
        self.previous = (name, owner)


def _check_cancelled(cancelled: Event):
    if cancelled.is_set():
        raise PlayerError("Video queue cancelled.")


class LinuxPlayerProcess:
    """pidfd pins the actual service process, including across PID reuse."""
    _RELEASE_EXIT_GRACE = 1.0

    def __init__(self, pid: int, fd: int):
        self.pid = pid
        self.fd = fd
        self.owner_binding: tuple[str, str] | None = None
        self._release_deadline: float | None = None

    def exited(self) -> bool:
        if self.owner_binding is None:
            return self._pid_exited()

        name, owner = self.owner_binding
        current = _dbus_owner(name)
        if current == owner and self._release_deadline is None:
            # Wait for the bus to release the name as well as for process exit.
            return False
        if current is None:
            # The bus can observe socket closure before the kernel signals exit.
            now = monotonic()
            if self._release_deadline is None:
                self._release_deadline = now + self._RELEASE_EXIT_GRACE
            if now >= self._release_deadline:
                raise PlayerError("Player released its D-Bus name without a verified process exit.")
            return self._pid_exited()
        raise PlayerError("Player identity changed or could not be verified while waiting for exit.")

    def _pid_exited(self) -> bool:
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        for _, events in poller.poll(0):
            if events & (select.POLLERR | select.POLLNVAL):
                raise PlayerError("Cannot verify player process exit.")
            return bool(events & select.POLLIN)
        return False

    def close(self) -> None:
        os.close(self.fd)


def launch_one(player: DefaultPlayer, path: Path, cancelled: Event, ownership: QueueOwnership):
    uri = path.resolve().as_uri()
    if player.dbus_name is not None:
        name = player.dbus_name
        # Desktop Entry D-Bus activation: well-known filename-derived name;
        # dots become slashes and hyphens become underscores in the object path.
        if (len(name) > 255
                or re.fullmatch(r"[A-Za-z_-][A-Za-z0-9_-]*(?:\.[A-Za-z_-][A-Za-z0-9_-]*)+", name) is None
                or player.desktop_file is None
                or player.desktop_file.name != name + ".desktop"):
            raise PlayerError("The default player's desktop entry has an invalid D-Bus identity.")
        object_path = "/" + name.replace(".", "/").replace("-", "_")
        # Activation is not ownership proof: the service can exit or be replaced
        # immediately afterward. Pin one unique owner before dispatching the URI.
        # Keep the prior subscription active across default discovery and activation.
        ownership.check()
        ownership.observe(name)
        _check_cancelled(cancelled)
        activation = _dbus("StartServiceByName", name, "uint32 0")
        _check_cancelled(cancelled)
        if activation not in {"(uint32 1,)", "(uint32 2,)"}:
            raise PlayerError("The default player returned an invalid service activation result. Queue stopped.")
        owner = _dbus_owner(name)
        if owner is None:
            raise PlayerError("Player identity changed during launch.")
        _check_cancelled(cancelled)
        expected = (("", owner),) if activation == "(uint32 1,)" else ()
        if ownership.previous is not None and ownership.previous[0] == name and not expected:
            raise PlayerError("Player identity changed before D-Bus activation.")
        process = ownership.signals.pin_process(owner)
        try:
            _check_cancelled(cancelled)
            if _dbus_owner(name) != owner:
                raise PlayerError("Player identity changed during launch.")

            def check_binding():
                # Drain transitions after the blocking same-connection lookup,
                # then poll the pinned process immediately before sending Open.
                ownership.check(name, expected)
                if process.exited():
                    raise PlayerError("Player identity changed during launch.")

            _check_cancelled(cancelled)
            ownership.signals.open(name, owner, object_path, uri, cancelled, check_binding)
            _check_cancelled(cancelled)
            if _dbus_owner(name) != owner or process.exited():
                raise PlayerError("Player identity changed during launch.")
            ownership.check(name, expected)
            ownership.acknowledge(name, owner)
            process.owner_binding = (name, owner)
            return process
        except Exception:
            process.close()  # Release this pin once; never retry or rebind to another owner.
            raise
    else:
        # Preserve non-D-Bus Exec/field-code handling. This helper is never
        # treated as the player, and an untrackable queue stops after this request.
        ownership.check()
        command = [_tool("gio"), "launch", str(player.desktop_file), uri]
        _check_cancelled(cancelled)
        _run(command)
    raise PlayerError(
        f"Requested opening {path.name} in {player.name}, but cannot identify its actual player PID. "
        "The remaining queue was stopped; a launcher exit is not playback completion."
    )


class QueueJob:
    def __init__(self, paths: Sequence[Path]):
        self.paths = tuple(Path(path).resolve() for path in paths)
        self.cancelled = Event()
        self.messages = Queue()
        self.thread = Thread(target=self.run, name="default-video-queue", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.cancelled.set()

    def run(self):
        try:
            self._run()
        except Exception as exc:
            # A platform query/launch failure must never cause an advance.
            if not self.cancelled.is_set():
                self.messages.put(("error", str(exc)))

    def _run(self):
        if self.cancelled.is_set():
            return
        if not self.paths:
            raise PlayerError("No videos selected.")
        for path in self.paths:
            if not path.is_file():
                raise PlayerError(f"Video is missing: {path}")
        players = []
        for path in self.paths:
            if self.cancelled.is_set():
                return
            players.append(default_player(path))
        if self.cancelled.is_set():
            return
        if players[0].native_queue and all(player == players[0] for player in players):
            process = launch_native(players[0], self.paths)
            self.messages.put(("native", f"Sent {len(self.paths)} videos in pane order to {players[0].name}. "
                               "Playback is now controlled by that player; closing this queue does not stop its playlist."))
            # Reap our child, without treating its exit as evidence of playback
            # completion or killing it when the Marnwick dialog is closed.
            result = process.wait()
            if result != 0:
                raise PlayerError(f"{players[0].name} exited with status {result}. Check the playlist in the player.")
            return
        with ExitStack() as resources:
            self._run_sequential(QueueOwnership(resources))

    def _run_sequential(self, ownership: QueueOwnership):
        for position, path in enumerate(self.paths):
            if self.cancelled.is_set():
                return
            # Resolve again: the user may have changed an association while
            # watching the preceding file. Never change it ourselves.
            player = default_player(path)
            if self.cancelled.is_set():
                return
            self.messages.put(("status", f"Opening {position + 1} / {len(self.paths)} — {path.name} in {player.name}"))
            process = launch_one(player, path, self.cancelled, ownership)
            try:
                self.messages.put(("status", f"{position + 1} / {len(self.paths)} — {path.name} in {player.name} "
                                   f"(PID {process.pid}). Exit the player application to open the next video. "
                                   "Pausing or reaching the end does not advance the queue."))
                while not self.cancelled.wait(.25):
                    exited = process.exited()
                    ownership.check(waiting=not exited)
                    if exited:
                        break
                if self.cancelled.is_set():
                    return
            finally:
                process.close()  # release tracking handle only, never terminate
        self.messages.put(("finished", "All launch requests completed and their tracked player processes exited."))
