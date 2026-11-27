"""Owner binding races at the subprocess boundary; no installed application runs."""
from contextlib import ExitStack, contextmanager
from time import monotonic, sleep
from pathlib import Path
import subprocess
import sys
from xml.sax.saxutils import escape
from threading import Condition, Event

import pytest

from marnwick import external_video as backend
from marnwick.external_video import LinuxPlayerProcess, QueueJob

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux backend fixtures use POSIX paths")


NO_OWNER = 'Error: GDBus.Error:org.freedesktop.DBus.Error.NameHasNoOwner: Synthetic name released'


class Handle:
    def __init__(self, pid):
        self.pid = pid
        self.closed = 0
        self.dead = False

    def exited(self):
        return self.dead

    def close(self):
        self.closed += 1


class Bus:
    A, B = ':1.41', ':1.42'

    def __init__(self, monkeypatch, tmp_path):
        self.owner = self.A
        self.transitions = []
        self.name = 'org.example.Player'
        self.player = backend.DefaultPlayer('Synthetic', self.name + '.desktop',
            desktop_file=tmp_path / (self.name + '.desktop'), dbus_name=self.name)
        self.paths = (tmp_path / 'blue.webm', tmp_path / 'red.webm')
        for path in self.paths:
            path.write_bytes(b'synthetic, never decoded')
        self.handles = []
        self.calls = []
        self.deliveries = []
        self.lookup_count = 0
        self.start_reply = '(uint32 2,)'
        self.open_reply = '()'
        self.change = None
        self.failure = None
        self.failure_occurrence = 1
        self.entered = Event()
        self.release = Event()
        self.release.set()
        monkeypatch.setattr(backend, 'LinuxOwnerSignals', lambda: self)
        monkeypatch.setattr(backend, '_tool', lambda name: name)
        monkeypatch.setattr(backend.subprocess, 'run', self.run)
        monkeypatch.setattr(backend, 'default_player', lambda path: self.player)
    def launch(self, player, path):
        with ExitStack() as resources:
            return backend.launch_one(player, path, Event(), backend.QueueOwnership(resources))

    def subscribe(self, name):
        assert name == self.name

    def pending(self):
        pending, self.transitions = self.transitions, []
        return pending

    def close(self):
        pass

    def replace_owner(self, owner):
        self.transitions.append((self.name, self.owner or '', owner or ''))
        self.owner = owner

    def pin_process(self, owner):
        self.calls.append(('GetConnectionCredentials', owner))
        if self.failure and self.failure[0] == 'GetConnectionCredentials':
            raise backend.PlayerError('Synthetic credential request failed') from self.failure[1]
        handle = Handle(1001 if owner == self.A else 1002)
        self.handles.append(handle)
        if self.change == 'after_pin':
            self.replace_owner(self.B)
        if self.change == 'pin_dead':
            handle.dead = True
        return handle

    def run(self, args, **kwargs):
        assert args[0] == 'gdbus'
        assert kwargs['timeout'] == 5 and kwargs['check']
        assert kwargs.get('shell', False) is False
        method = args[args.index('--method') + 1].rsplit('.', 1)[-1]
        self.calls.append((method, args))
        if (self.failure and self.failure[0] == method
                and sum(name == method for name, _ in self.calls) == self.failure_occurrence):
            _, exc = self.failure
            self.failure = None
            raise exc
        if method == 'StartServiceByName':
            if self.start_reply == '(uint32 1,)':
                self.owner = None
                self.replace_owner(self.A if not self.deliveries else self.B)
            result = self.start_reply
        elif method == 'GetNameOwner':
            self.lookup_count += 1
            if self.change == 'second_lookup' and self.lookup_count == 2:
                self.replace_owner(self.B)
            if self.change == 'lost_after_open' and self.deliveries:
                self.replace_owner(None)
            if self.owner is None:
                raise subprocess.CalledProcessError(1, args, stderr=NO_OWNER)
            result = f"('{self.owner}',)"
        else:
            pytest.fail(f'unexpected helper method: {method}')
        return subprocess.CompletedProcess(args, 0, result + '\n', '')

    def open(self, name, destination, object_path, uri, cancelled, check_binding):
        assert name == self.name
        if self.change == 'before_dispatch':
            self.replace_owner(self.B)
        check_binding()
        self.calls.append(('Open', destination))
        if self.failure and self.failure[0] == 'Open':
            raise backend.PlayerError('Synthetic Open failure') from self.failure[1]
        self.deliveries.append(destination)
        self.entered.set()
        assert self.release.wait(3), 'pending test Open was not released'
        if self.change == 'after_open':
            self.replace_owner(self.B)
        if self.change == 'ack_dead' and self.handles:
            self.handles[0].dead = True
        if self.open_reply != '()':
            raise backend.PlayerError('Invalid Open acknowledgment')



@pytest.fixture
def bus(monkeypatch, tmp_path):
    return Bus(monkeypatch, tmp_path)


@pytest.mark.parametrize('start_reply', ['(uint32 1,)', '(uint32 2,)'])
def test_activation_pin_unique_open_and_same_handle_return(bus, start_reply):
    bus.start_reply = start_reply
    handle = bus.launch(bus.player, bus.paths[0])
    assert [method for method, _ in bus.calls] == [
        'StartServiceByName', 'GetNameOwner', 'GetConnectionCredentials',
        'GetNameOwner', 'Open', 'GetNameOwner']
    assert bus.deliveries == [bus.A]
    assert bus.calls[4][1] == bus.A
    assert handle is bus.handles[0] and handle.pid == 1001 and handle.closed == 0
    handle.close()
    assert handle.closed == 1


@pytest.mark.parametrize('change', ['after_open', 'second_lookup', 'after_pin', 'before_dispatch'])
def test_replacement_never_rebinds_to_B_even_with_A_alive_and_time_to_retry(bus, change):
    bus.change = change
    with pytest.raises(backend.PlayerError, match='identity changed'):
        bus.launch(bus.player, bus.paths[0])
    assert bus.owner == bus.B
    assert len(bus.handles) == 1 and bus.handles[0].pid == 1001
    assert bus.handles[0].closed == 1 and not bus.handles[0].dead
    assert bus.B not in bus.deliveries
    assert len(bus.deliveries) <= 1
    assert bus.lookup_count <= 3


def test_owner_disappears_after_open_and_A_remains_alive_no_retry(bus):
    bus.change = 'lost_after_open'
    with pytest.raises(backend.PlayerError, match='identity changed'):
        bus.launch(bus.player, bus.paths[0])
    assert bus.deliveries == [bus.A]
    assert len(bus.handles) == 1 and bus.handles[0].closed == 1
    assert not bus.handles[0].dead
    assert bus.lookup_count == 3


@pytest.mark.parametrize('change', ['pin_dead', 'ack_dead'])
def test_pinned_process_exit_stops_without_rebinding(bus, change):
    bus.change = change
    with pytest.raises(backend.PlayerError, match='identity changed|exited'):
        bus.launch(bus.player, bus.paths[0])
    assert len(bus.handles) == 1 and bus.handles[0].closed == 1
    assert len(bus.deliveries) == (0 if change == 'pin_dead' else 1)


@pytest.mark.parametrize('reply', ['', '()', '(uint32 0,)', '(uint32 3,)', '(1,)', '(true,)'])
def test_invalid_activation_result_never_opens_or_pins(bus, reply):
    bus.start_reply = reply
    with pytest.raises(backend.PlayerError, match='activation'):
        bus.launch(bus.player, bus.paths[0])
    assert bus.deliveries == [] and bus.handles == []
    assert len(bus.calls) == 1


@pytest.mark.parametrize('method', ['StartServiceByName', 'GetNameOwner', 'GetConnectionCredentials', 'Open'])
@pytest.mark.parametrize('kind', ['error', 'timeout'])
def test_operation_failure_is_not_retried_and_releases_owned_handle(bus, method, kind):
    error = (subprocess.CalledProcessError(1, ['gdbus'], stderr='synthetic error') if kind == 'error'
             else subprocess.TimeoutExpired(['gdbus'], 5))
    bus.failure = (method, error)
    with pytest.raises(backend.PlayerError):
        bus.launch(bus.player, bus.paths[0])
    assert sum(name == method for name, _ in bus.calls) == 1
    assert not bus.deliveries
    assert len(bus.handles) == (1 if method == 'Open' else 0)
    assert all(handle.closed == 1 for handle in bus.handles)


def test_gdbus_lookup_protects_leading_hyphen_operand(bus):
    backend._dbus('GetNameOwner', '-org.-player')
    args = bus.calls[0][1]
    assert args[-2:] == ['--', '-org.-player']


@pytest.mark.parametrize('lookup', [2, 3])
@pytest.mark.parametrize('kind', ['error', 'timeout'])
def test_ownership_recheck_failure_closes_pin_and_never_retries(bus, lookup, kind):
    error = (subprocess.CalledProcessError(1, ['gdbus'], stderr='owner disappeared') if kind == 'error'
             else subprocess.TimeoutExpired(['gdbus'], 5))
    bus.failure = ('GetNameOwner', error)
    bus.failure_occurrence = lookup
    with pytest.raises(backend.PlayerError):
        bus.launch(bus.player, bus.paths[0])
    assert len(bus.handles) == 1 and bus.handles[0].closed == 1
    assert not bus.handles[0].dead
    assert bus.deliveries == ([] if lookup == 2 else [bus.A])
    assert sum(method == 'GetNameOwner' for method, _ in bus.calls) == lookup


def test_gdbus_activation_protects_leading_hyphen_operand(bus):
    bus.name = '-org.-player'
    player = backend.DefaultPlayer('Synthetic', bus.name + '.desktop',
        desktop_file=Path('/private') / (bus.name + '.desktop'), dbus_name=bus.name)
    handle = bus.launch(player, bus.paths[0])
    assert bus.calls[0][0] == 'StartServiceByName'
    assert bus.calls[0][1][-3:] == ['--', '-org.-player', 'uint32 0']
    assert bus.deliveries == [bus.A]
    handle.close()


@pytest.mark.parametrize('reply', ['()', '(true,)'])
def test_cancellation_pending_open_releases_exact_pin_without_advancing(bus, reply):
    bus.open_reply = reply
    bus.release.clear()
    job = QueueJob(bus.paths)
    job.start()
    try:
        assert bus.entered.wait(3)
        assert len(bus.handles) == 1 and bus.handles[0].closed == 0
        job.stop()
        bus.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert bus.deliveries == [bus.A]
        assert bus.handles[0].closed == 1 and not bus.handles[0].dead
        assert not any(kind in {'finished', 'error'} for kind, _ in list(job.messages.queue))
    finally:
        job.stop()
        bus.release.set()
        job.thread.join(3)


@pytest.mark.parametrize(('owner_after_open', 'exited'), [(Bus.B, False), (None, False), (Bus.B, True)])
def test_owner_change_after_ack_stops_queue(bus, monkeypatch, owner_after_open, exited):
    class LiveHandle(LinuxPlayerProcess):
        def __init__(self, pid):
            super().__init__(pid, -1)
            self.closed = False

        def _pid_exited(self):
            return self.owner_binding is not None and exited

        def close(self):
            self.closed = True

    handle = LiveHandle(1001)
    original_run = bus.run

    def run(args, **kwargs):
        if ('org.freedesktop.DBus.GetNameOwner' in args and handle.owner_binding
                and bus.owner == bus.A):
            bus.replace_owner(owner_after_open)
        return original_run(args, **kwargs)

    monkeypatch.setattr(backend.subprocess, 'run', run)
    monkeypatch.setattr(bus, 'pin_process', lambda owner: handle)
    job = QueueJob(bus.paths)
    job.run()
    assert handle.closed
    assert bus.deliveries == [Bus.A]
    assert list(job.messages.queue)[-1][0] == 'error'


def test_unverifiable_pidfd_never_opens_or_advances(bus, monkeypatch):
    def pin(owner):
        bus.calls.append(('GetConnectionCredentials', owner))
        raise ProcessLookupError("Synthetic owner process disappeared")

    monkeypatch.setattr(bus, 'pin_process', pin)
    job = QueueJob(bus.paths)
    job.run()
    assert not bus.deliveries
    assert [method for method, _ in bus.calls] == [
        'StartServiceByName', 'GetNameOwner', 'GetConnectionCredentials']
    assert list(job.messages.queue)[-1][0] == 'error'


def end_first_process(bus, monkeypatch):
    """Release A during the first lifetime poll, before rediscovery of clip two."""
    pin = bus.pin_process

    def track(owner):
        handle = pin(owner)
        if len(bus.handles) == 1:
            def exited():
                if getattr(handle, 'owner_binding', None) is None:
                    return False
                bus.replace_owner(None)
                handle.dead = True
                return True

            handle.exited = exited
        return handle

    bus.start_reply = '(uint32 2,)'
    monkeypatch.setattr(bus, 'pin_process', track)


@pytest.mark.parametrize('transient', [False, True])
def test_replacement_during_rediscovery_is_detected_even_if_it_leaves(bus, monkeypatch, transient):
    end_first_process(bus, monkeypatch)
    lookups = []

    def discover(path):
        lookups.append(path)
        if len(lookups) == 4:
            assert bus.owner is None and bus.handles[0].dead
            bus.replace_owner(bus.B)
            if transient:
                bus.replace_owner(None)
                bus.start_reply = '(uint32 1,)'
        return bus.player

    monkeypatch.setattr(backend, 'default_player', discover)
    job = QueueJob(bus.paths)
    job.run()
    assert bus.deliveries == [bus.A]
    assert sum(method == 'StartServiceByName' for method, _ in bus.calls) == 1
    assert list(job.messages.queue)[-1][0] == 'error'


@pytest.mark.parametrize('transient', [False, True])
def test_replacement_at_next_activation_boundary_stops_queue(bus, monkeypatch, transient):
    end_first_process(bus, monkeypatch)
    original = bus.run

    def run(args, **kwargs):
        if 'org.freedesktop.DBus.StartServiceByName' in args and bus.deliveries:
            bus.replace_owner(bus.B)
            if transient:
                bus.replace_owner(None)
                bus.start_reply = '(uint32 1,)'
        return original(args, **kwargs)

    monkeypatch.setattr(backend.subprocess, 'run', run)
    job = QueueJob(bus.paths)
    job.run()
    assert bus.deliveries == [bus.A]
    assert sum(method == 'StartServiceByName' for method, _ in bus.calls) == 2
    assert list(job.messages.queue)[-1][0] == 'error'
    assert all(handle.closed == 1 for handle in bus.handles)


@pytest.mark.parametrize('boundary', ['StartServiceByName', 'GetNameOwner', 'GetConnectionCredentials'])
def test_cancellation_at_second_clip_boundary_prevents_open(bus, monkeypatch, boundary):
    end_first_process(bus, monkeypatch)
    original = bus.run
    original_pin = bus.pin_process
    entered, release = Event(), Event()

    def run(args, **kwargs):
        method = args[args.index('--method') + 1].rsplit('.', 1)[-1]
        second_clip = bus.deliveries == [bus.A] and bus.handles[0].dead
        if second_clip and method == 'StartServiceByName':
            bus.start_reply = '(uint32 1,)'
        reply = original(args, **kwargs)
        if second_clip and method == boundary:
            entered.set()
            assert release.wait(3)
        return reply

    def pin(owner):
        if owner == bus.B and boundary == 'GetConnectionCredentials':
            entered.set()
            assert release.wait(3)
        return original_pin(owner)

    monkeypatch.setattr(bus, 'pin_process', pin)
    monkeypatch.setattr(backend.subprocess, 'run', run)
    job = QueueJob(bus.paths)
    job.start()
    try:
        assert entered.wait(3)
        job.stop()
        release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert bus.deliveries == [bus.A]
        assert bus.owner == bus.B
        assert all(handle.closed == 1 for handle in bus.handles)
        assert not any(kind in {'finished', 'error'} for kind, _ in list(job.messages.queue))
    finally:
        job.stop()
        release.set()
        job.thread.join(3)


@pytest.fixture
def private_bus(monkeypatch):
    import select
    import shutil
    from tempfile import TemporaryDirectory

    if not sys.platform.startswith('linux'):
        pytest.skip('Linux libdbus transport')
    daemon = shutil.which('dbus-daemon')
    if not daemon or not shutil.which('gdbus'):
        pytest.skip('private-bus fixture requires dbus-daemon and gdbus')

    @contextmanager
    def start_bus(root):
        address = f'unix:path={root / "bus"}'
        config = root / 'bus.conf'
        config.write_text(f"""
<busconfig>
  <type>session</type>
  <listen>{escape(address)}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{escape(str(root))}</servicedir>
  <policy context="default">
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
        with subprocess.Popen([daemon, '--nofork', '--print-address',
                               '--config-file=' + str(config)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as server:
            try:
                assert select.select([server.stdout], [], [], 5)[0], 'private bus did not start'
                ready = server.stdout.readline().decode()
                if not ready:
                    assert server.wait(timeout=3) != 0
                    diagnostic = server.stderr.read().decode()
                    if 'Failed to bind socket' in diagnostic and (
                            'Operation not permitted' in diagnostic or 'Permission denied' in diagnostic):
                        pytest.skip(f'private Unix socket blocked by the execution environment: {diagnostic.strip()}')
                    pytest.fail(diagnostic)
                assert ready.startswith(address + ',guid=')
                yield address, server
            finally:
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=3)

    with TemporaryDirectory(prefix='marnwick-dbus-', dir='/tmp') as directory:
        root = Path(directory)
        with start_bus(root) as (address, server):
            monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', address)
            yield root, server, start_bus


def test_signal_transport_retains_transient_owner_on_private_bus(private_bus, monkeypatch):
    import ctypes

    name = 'org.example.MarnwickTest'
    root, server, start_bus = private_bus
    lib = ctypes.CDLL('libdbus-1.so.3')
    request_name = lib.dbus_bus_request_name
    request_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_void_p]
    request_name.restype = ctypes.c_int
    with ExitStack() as resources:
        observer = backend.LinuxOwnerSignals()
        resources.callback(observer.close)
        observer.subscribe(name)
        owners = []
        for _ in range(2):
            client = backend.LinuxOwnerSignals()
            try:
                assert request_name(client.connection, name.encode(), 4, None) == 1
                owners.append(backend._dbus_owner(name))
            finally:
                client.close()
            deadline = monotonic() + 3
            while backend._dbus_owner(name) is not None:
                assert monotonic() < deadline
                sleep(.01)
        a, b = owners
        assert a != b
        assert list(observer.pending()) == [
            (name, '', a), (name, a, ''), (name, '', b), (name, b, '')]

        original = backend.LinuxOwnerSignals()
        resources.callback(original.close)
        assert request_name(original.connection, name.encode(), 4, None) == 1
        original_owner = backend._dbus_owner(name)

        replacement_root = root / 'replacement'
        replacement_root.mkdir()
        with start_bus(replacement_root) as (address, _), ExitStack() as replacement_clients:
            monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', address)
            unique_name = lib.dbus_bus_get_unique_name
            unique_name.argtypes = [ctypes.c_void_p]
            unique_name.restype = ctypes.c_char_p
            # The fresh bus reuses the old bus's unique name for a different client.
            for _ in range(int(original_owner.split('.')[1]) + 1):
                replacement = backend.LinuxOwnerSignals()
                replacement_clients.callback(replacement.close)
                if unique_name(replacement.connection).decode() == original_owner:
                    break
            else:
                pytest.fail(f'replacement bus did not assign {original_owner}')
            assert request_name(replacement.connection, name.encode(), 4, None) == 1
            assert backend._dbus_owner(name) == original_owner

            send = observer._connection_send_with_reply_and_block

            def replace_at_dispatch(connection, message, timeout, error):
                if observer._message_get_type(message) == 1 and get_member(message) == b'Open':
                    server.terminate()
                    server.wait(timeout=3)
                return send(connection, message, timeout, error)

            get_member = lib.dbus_message_get_member
            get_member.argtypes = [ctypes.c_void_p]
            get_member.restype = ctypes.c_char_p
            monkeypatch.setattr(observer, '_connection_send_with_reply_and_block', replace_at_dispatch)
            uri = (root / 'synthetic.webm').as_uri()
            with pytest.raises(backend.PlayerError, match='D-Bus Open failed'):
                observer.open(name, original_owner, '/org/example/MarnwickTest', uri, Event(), lambda: None)

            read_write = lib.dbus_connection_read_write
            read_write.argtypes = [ctypes.c_void_p, ctypes.c_int]
            read_write.restype = ctypes.c_int
            assert read_write(replacement.connection, 100)
            while message := replacement._connection_pop_message(replacement.connection):
                try:
                    assert get_member(message) != b'Open'
                finally:
                    replacement._message_unref(message)


def test_numeric_pid_substitution_cannot_admit_the_replacement(bus, monkeypatch):
    original = Handle(1001)
    replacement = Handle(original.pid)
    numeric_processes = {original.pid: original}

    def pin_credentials(owner):
        assert owner == bus.A
        credential_pid = original.pid
        # The bus still owns the name through a retained connection, but
        # another live process now occupies the credential's numeric PID.
        original.dead = True
        numeric_processes[credential_pid] = replacement
        return original

    def resolve_numeric_pid(pid):
        pytest.fail(f'numeric lookup would pin the replacement: {numeric_processes[pid] is replacement}')

    monkeypatch.setattr(bus, 'pin_process', pin_credentials)
    monkeypatch.setattr(backend.os, 'pidfd_open', resolve_numeric_pid, raising=False)
    job = QueueJob(bus.paths)
    job.run()
    assert bus.owner == bus.A and numeric_processes[original.pid] is replacement
    assert original.closed == 1 and not replacement.dead
    assert bus.deliveries == []
    assert list(job.messages.queue)[-1][0] == 'error'


@pytest.mark.parametrize(('release_signal', 'exit_poll'), [
    (True, 1), (False, 1), (True, 2), (True, None),
])
def test_first_wait_lookup_can_observe_exit_and_released_name(bus, monkeypatch, release_signal, exit_poll):
    now = 0.0
    wait_polls = 0
    queued = release_signal and exit_poll is not None

    class TrackedHandle(LinuxPlayerProcess):
        def __init__(self):
            super().__init__(1001, -1)
            self.closed = 0

        def _pid_exited(self):
            return self.owner_binding is not None and wait_polls == exit_poll

        def close(self):
            self.closed += 1

    first = TrackedHandle()
    original_pin = bus.pin_process
    original_run = bus.run

    def run(args, **kwargs):
        assert 'org.freedesktop.DBus.NameHasOwner' not in args
        if ('org.freedesktop.DBus.GetNameOwner' in args and first.owner_binding
                and not first.closed and wait_polls == 1):
            if release_signal:
                bus.replace_owner(None)
            else:
                bus.owner = None
            bus.start_reply = '(uint32 1,)'
        return original_run(args, **kwargs)

    def pin(owner):
        if owner == bus.A:
            return first
        handle = original_pin(owner)

        def exited():
            if not getattr(handle, 'owner_binding', None):
                return False
            bus.replace_owner(None)
            return True

        handle.exited = exited
        return handle

    monkeypatch.setattr(backend.subprocess, 'run', run)
    monkeypatch.setattr(bus, 'pin_process', pin)
    job = QueueJob(bus.paths)

    def wait(interval):
        nonlocal now, wait_polls
        now += interval
        wait_polls += 1
        assert wait_polls <= 6
        if wait_polls == 2 and exit_poll != 1:
            assert bus.deliveries == [bus.A]
            assert first.closed == 0
        return False

    monkeypatch.setattr(backend, 'monotonic', lambda: now)
    monkeypatch.setattr(job.cancelled, 'wait', wait)
    job.run()
    assert first.closed == 1
    assert bus.deliveries == ([bus.A, bus.B] if queued else [bus.A])
    kind, message = list(job.messages.queue)[-1]
    assert kind == ('finished' if queued else 'error')
    if exit_poll is None:
        assert wait_polls == 5
        assert 'without a verified process exit' in message


# Activated by the fixture daemon. No desktop integrations or media decoders.
SERVICE = """
import ctypes
import os
from pathlib import Path
import sys
from time import monotonic, sleep

root = Path(sys.argv[1])
name = sys.argv[2].encode()
sys.path.insert(0, sys.argv[3])
os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={root / "bus"}'
from marnwick.external_video import LinuxOwnerSignals
bus = ctypes.CDLL('libdbus-1.so.3')
p, s, i = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
signatures = {
    'bus_request_name': (i, p, s, ctypes.c_uint, p),
    'connection_read_write': (i, p, i),
    'connection_send': (i, p, p, p),
    'connection_flush': (None, p),
    'message_is_method_call': (i, p, s, s),
    'message_get_path': (s, p),
    'message_get_signature': (s, p),
    'message_get_type': (i, p),
    'message_new_method_return': (p, p),
    'message_new_error': (p, p, s, s),
    'free_string_array': (None, ctypes.POINTER(s)),
}
for symbol, (result, *args) in signatures.items():
    function = getattr(bus, 'dbus_' + symbol)
    function.restype = result
    function.argtypes = args

client = LinuxOwnerSignals()
assert bus.dbus_bus_request_name(client.connection, name, 4, None) == 1
deadline = monotonic() + 18
pid = os.getpid()
try:
    while (monotonic() < deadline and not (root / f'exit-{pid}').exists()
           and not (root / 'stop').exists()):
        assert bus.dbus_connection_read_write(client.connection, 25)
        while message := client._connection_pop_message(client.connection):
            try:
                if bus.dbus_message_get_type(message) != 1:  # DBUS_MESSAGE_TYPE_METHOD_CALL
                    continue
                if bus.dbus_message_is_method_call(message, b'org.freedesktop.Application', b'Open'):
                    assert bus.dbus_message_get_path(message) == b'/org/example/MarnwickTest'
                    assert bus.dbus_message_get_signature(message) == b'asa{sv}'
                    urls, count = ctypes.POINTER(s)(), i()
                    assert client._message_get_args(message, None, ord('a'), i(ord('s')),
                                                    ctypes.byref(urls), ctypes.byref(count), i(0))
                    try:
                        assert count.value == 1
                        uri = urls[0].decode()
                    finally:
                        bus.dbus_free_string_array(urls)
                    reply = bus.dbus_message_new_method_return(message)
                    with (root / 'opens').open('a') as log:
                        log.write(f'{pid} {uri}\\n')
                else:
                    reply = bus.dbus_message_new_error(
                        message, b'org.freedesktop.DBus.Error.UnknownMethod', b'Synthetic service')
                    if not reply:
                        continue
                assert bus.dbus_connection_send(client.connection, reply, None)
                client._message_unref(reply)
                bus.dbus_connection_flush(client.connection)
            finally:
                client._message_unref(message)
except BaseException:
    client.close()
    raise
# Exit the process with the connection still open.
os._exit(0)
"""


def test_acknowledged_open_then_real_exit_then_next_open(private_bus, monkeypatch):
    import ctypes
    import os
    import shlex

    root, _, _ = private_bus
    name = 'org.example.MarnwickTest'
    desktop = root / (name + '.desktop')
    paths = (root / "blue ' ü.webm", root / '-red.webm')
    for path in paths:
        path.write_bytes(b'synthetic media')
    service = root / 'service.py'
    service.write_text(SERVICE)
    source = str(Path(backend.__file__).resolve().parents[1])
    command = shlex.join([sys.executable, str(service), str(root), name, source])
    (root / (name + '.service')).write_text(f'[D-BUS Service]\nName={name}\nExec={command}\n')
    monkeypatch.setattr(backend, 'default_player', lambda path: backend.DefaultPlayer(
        'Synthetic', desktop.name, desktop_file=desktop, dbus_name=name))

    # Older daemons/kernels can return a numeric ProcessID without a ProcessFD.
    client = backend.LinuxOwnerSignals()
    try:
        unique_name = ctypes.CDLL('libdbus-1.so.3').dbus_bus_get_unique_name
        unique_name.argtypes = [ctypes.c_void_p]
        unique_name.restype = ctypes.c_char_p
        try:
            probe = client.pin_process(unique_name(client.connection).decode())
        except backend.PlayerError as exc:
            if 'did not provide a connection-bound process handle' in str(exc):
                pytest.skip('the private bus and kernel must supply GetConnectionCredentials ProcessFD')
            raise
        try:
            assert probe.pid == os.getpid() and not probe.exited()
        finally:
            probe.close()
    finally:
        client.close()

    job = QueueJob(paths)
    pending_pids = set()
    polled = Condition()
    real_exited = LinuxPlayerProcess.exited

    def exited(process):
        result = real_exited(process)
        if process.owner_binding and not result:
            with polled:
                pending_pids.add(process.pid)
                polled.notify_all()
        return result

    def await_admission(pid):
        # The binding is set only after Open is acknowledged and verified.
        with polled:
            assert polled.wait_for(lambda: int(pid) in pending_pids, timeout=4), list(job.messages.queue)

    monkeypatch.setattr(LinuxPlayerProcess, 'exited', exited)
    opens = root / 'opens'

    def deliveries():
        return opens.read_text().splitlines() if opens.exists() else []

    def await_deliveries(count):
        deadline = monotonic() + 5
        while len(rows := deliveries()) < count:
            assert job.thread.is_alive(), list(job.messages.queue)
            assert monotonic() < deadline, list(job.messages.queue)
            sleep(.02)
        return [row.split(' ', 1) for row in rows]

    job.start()
    try:
        [(first, uri)] = await_deliveries(1)
        assert uri == paths[0].as_uri()
        await_admission(first)
        assert len(deliveries()) == 1
        (root / f'exit-{first}').touch()

        [_, (second, uri)] = await_deliveries(2)
        assert second != first and uri == paths[1].as_uri()
        await_admission(second)
        (root / f'exit-{second}').touch()
        job.thread.join(5)
        assert not job.thread.is_alive()
        assert list(job.messages.queue)[-1][0] == 'finished'
        assert len(deliveries()) == 2
    finally:
        job.stop()
        (root / 'stop').touch()
        job.thread.join(8)
