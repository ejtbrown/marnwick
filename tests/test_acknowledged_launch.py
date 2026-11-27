"""Linux Open acknowledgment regressions; all launch/process operations are mocks."""
from pathlib import Path
import subprocess
import sys
from threading import Event
from unittest.mock import Mock

import pytest

from marnwick import external_video as backend
from marnwick.external_video import QueueJob

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux backend fixtures use POSIX paths")


class TrackedProcess:
    pid = 5678

    def __init__(self):
        self.finished = False
        self.closed = False
        self.polled = Event()
        self.poll_count = 0

    def exited(self):
        self.poll_count += 1
        if self.poll_count > 2:  # Two admission checks precede queue lifetime polling.
            self.polled.set()
        return self.finished

    def close(self):
        self.closed = True


class LaunchHarness:
    def __init__(self, monkeypatch, tmp_path):
        self.player = backend.DefaultPlayer(
            'Synthetic', 'org.example.Player.desktop',
            desktop_file=tmp_path / 'org.example.Player.desktop', dbus_name='org.example.Player')
        self.paths = (tmp_path / "blue ' \\ ;$(touch injected)`cmd`\n☃.webm", tmp_path / '-red.webm')
        for path in self.paths:
            path.write_bytes(b'no media decoder')
        self.calls = []
        self.acknowledged = False
        self.pinned = False
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.reply_type = 2
        self.reply_signature = b''
        self.reply_sender = None
        self.connected = True
        self.replace_bus = False
        self.owner = None
        self.bound_owner = None
        self.process = TrackedProcess()
        self.open_count = 0
        # Exercise the real Open implementation with a controlled wire boundary.
        transport = backend.LinuxOwnerSignals.__new__(backend.LinuxOwnerSignals)
        self.signals = Mock(wraps=transport)
        transport.connection = object()
        transport._bus_reply = Mock(return_value='owner reply')
        transport._message_get_args = self.owner_args
        transport._message_get_sender = lambda reply: (
            b'org.freedesktop.DBus' if reply == 'owner reply' else self.reply_sender or self.owner.encode())
        transport._message_has_signature = lambda reply, signature: (
            signature == (b's' if reply == 'owner reply' else self.reply_signature))
        transport._message_get_type = lambda reply: self.reply_type
        transport._message_new_method_call = Mock(return_value='request')
        transport._message_unref = Mock()
        transport._message_iter_init_append = Mock()
        transport._message_iter_open_container = Mock(return_value=True)
        transport._message_iter_append_basic = Mock(return_value=True)
        transport._message_iter_close_container = Mock(return_value=True)
        transport._connection_send_with_reply_and_block = self.send
        self.transport = transport
        self.signals.subscribe = Mock()
        self.signals.close = Mock()
        self.transitions = []
        self.signals.pending = Mock(side_effect=self.pending)
        monkeypatch.setattr(backend, 'LinuxOwnerSignals', lambda: self.signals)
        monkeypatch.setattr(backend, '_tool', lambda name: name)
        monkeypatch.setattr(backend.subprocess, 'run', self.run)
        self.signals.pin_process = Mock(side_effect=self.track)
        monkeypatch.setattr(backend, 'default_player', lambda path: self.player)

    def launch(self, player, path):
        from contextlib import ExitStack

        with ExitStack() as resources:
            return backend.launch_one(player, path, Event(), backend.QueueOwnership(resources))

    def pending(self):
        transitions, self.transitions = self.transitions, []
        return transitions

    def change_owner(self, owner, name=None):
        self.transitions.append((name or self.player.dbus_name, self.owner or '', owner or ''))
        self.owner = owner

    def track(self, owner):
        assert not self.acknowledged, 'must pin the unique owner before Open'
        self.pinned = True
        self.pinned_connection = self.transport.connection
        assert owner == self.owner
        return self.process

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert kwargs.get('shell', False) is False
        assert kwargs['check'] and kwargs['capture_output'] and kwargs['text']
        assert 0 < kwargs['timeout'] <= 10
        if args[0] == 'gio':  # Safe old-source behavior, never an actual desktop launch.
            return subprocess.CompletedProcess(args, 0, '', '')
        assert args[0] == 'gdbus'
        method = args[args.index('--method') + 1]
        if method == 'org.freedesktop.DBus.StartServiceByName':
            self.acknowledged = False
            self.pinned = False
            result = '(uint32 2,)' if self.owner else '(uint32 1,)'
            if self.owner is None:
                self.change_owner(':1.43' if self.open_count else ':1.42', args[-2])
        else:
            assert self.owner, 'activation must precede owner/PID lookup'
            assert method == 'org.freedesktop.DBus.GetNameOwner'
            result = f"('{self.owner}',)"
        return subprocess.CompletedProcess(args, 0, result, '')

    def owner_args(self, _reply, _error, _type, owner, _end):
        owner._obj.value = (self.bound_owner or self.owner).encode()
        return True

    def send(self, connection, request, timeout, error):
        assert connection is self.pinned_connection
        assert request == 'request' and 0 < timeout <= 5000
        assert self.pinned
        self.open_count += 1
        self.entered.set()
        assert self.release.wait(3), 'test did not release pending request'
        if self.replace_bus:
            # The replacement advertises the same unique name. Any helper
            # would connect there, but the connection that pinned it is gone.
            self.connected = False
        if not self.connected:
            return None
        self.acknowledged = (
            self.reply_type == 2 and self.reply_signature == b'' and self.reply_sender is None)
        return 'open reply'


@pytest.fixture
def launch(monkeypatch, tmp_path):
    return LaunchHarness(monkeypatch, tmp_path)


@pytest.mark.parametrize('existing_owner', [False, True])
def test_open_acknowledged_before_returning_tracker_cold_or_existing_owner(launch, existing_owner):
    launch.owner = ':1.7' if existing_owner else None
    process = launch.launch(launch.player, launch.paths[0])
    assert process is launch.process
    assert launch.open_count == 1
    launch.transport._message_new_method_call.assert_called_once_with(
        launch.owner.encode(), b'/org/example/Player', b'org.freedesktop.Application', b'Open')
    containers = launch.transport._message_iter_open_container.call_args_list
    assert [call.args[1:3] for call in containers] == [(ord('a'), b's'), (ord('a'), b'{sv}')]
    array, kind, value = launch.transport._message_iter_append_basic.call_args.args
    assert array is containers[0].args[3] and kind == ord('s')
    assert value._obj.value == launch.paths[0].resolve().as_uri().encode()
    assert launch.acknowledged
    assert launch.owner == (':1.7' if existing_owner else ':1.42')
    assert not process.exited()  # An acknowledged Open is not player exit.


@pytest.mark.parametrize(('name', 'object_path'), [
    ('org.example.Player', '/org/example/Player'),
    ('org.example.Player-with-dash', '/org/example/Player_with_dash'),
    ('_org._7_zip.Player_2', '/_org/_7_zip/Player_2'),
    ('-org.-player', '/_org/_player'),
    ('a.b', '/a/b'),
    ('a.' + 'b' * 253, '/a/' + 'b' * 253),
])
def test_valid_names_and_canonical_paths(launch, name, object_path):
    player = backend.DefaultPlayer('Synthetic', name + '.desktop',
        desktop_file=Path('/private') / (name + '.desktop'), dbus_name=name)
    assert launch.launch(player, launch.paths[0]) is launch.process
    assert launch.calls[0][0][-3:] == ['--', name, 'uint32 0']
    launch.transport._message_new_method_call.assert_called_once_with(
        launch.owner.encode(), object_path.encode(), b'org.freedesktop.Application', b'Open')


@pytest.mark.parametrize('name', [
    '', 'nodot', '.org.player', 'org.player.', 'org..player', 'org.7player',
    '7org.player', ':1.42', 'org.player/other', 'org.player\\other',
    'org.pläyer', 'org.player\n', 'org.player;cmd', 'a.' + 'b' * 254,
])
def test_invalid_dbus_identity_fails_before_any_launch(launch, name):
    player = backend.DefaultPlayer('Synthetic', name + '.desktop',
        desktop_file=Path('/private') / (name + '.desktop'), dbus_name=name)
    with pytest.raises(backend.PlayerError, match='D-Bus|desktop'):
        launch.launch(player, launch.paths[0])
    assert launch.calls == []


def test_mismatched_resolved_desktop_and_bus_name_fails_closed(launch):
    player = backend.DefaultPlayer('Synthetic', 'org.example.Player.desktop',
        desktop_file=Path('/private/org.example.Different.desktop'), dbus_name='org.example.Player')
    with pytest.raises(backend.PlayerError, match='D-Bus|desktop'):
        launch.launch(player, launch.paths[0])
    assert launch.calls == []


def test_nested_desktop_id_uses_resolved_filename_for_bus(monkeypatch, tmp_path):
    directory = tmp_path / 'applications' / 'nested'
    directory.mkdir(parents=True)
    desktop = directory / 'org.example.Player-with-dash.desktop'
    desktop.write_text('[Desktop Entry]\nName=VLC\nExec=synthetic %U\nDBusActivatable=true\n')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    monkeypatch.setenv('XDG_DATA_DIRS', str(tmp_path))
    monkeypatch.setattr(backend, '_tool', lambda name: name)
    monkeypatch.setattr(backend.shutil, 'which', lambda name: None)
    monkeypatch.setattr(backend, '_run', lambda args: 'video/webm' if args[2] == 'filetype'
                        else 'nested-org.example.Player-with-dash.desktop')
    player = backend._linux_player(tmp_path / 'blue.webm')
    assert player.desktop_file == desktop
    assert player.identity == 'nested-org.example.Player-with-dash.desktop'
    assert player.dbus_name == 'org.example.Player-with-dash'
    assert not player.native_queue


@pytest.mark.parametrize(('reply_type', 'signature', 'sender'), [
    (2, b'b', None), (2, b's', None), (2, b'', b':1.99'), (3, b'', None),
])
def test_invalid_open_reply_stops_queue_without_tracking_or_retry(launch, reply_type, signature, sender):
    launch.reply_type = reply_type
    launch.reply_signature = signature
    launch.reply_sender = sender
    job = QueueJob(launch.paths)
    job.run()
    messages = list(job.messages.queue)
    assert messages[-1][0] == 'error'
    assert 'acknowledg' in messages[-1][1].lower()
    assert launch.open_count == 1
    assert len(launch.calls) == 3
    assert launch.process.closed
    assert not launch.process.polled.is_set()


def test_same_bus_owner_mismatch_prevents_open(launch):
    launch.bound_owner = ':1.99'
    job = QueueJob(launch.paths)
    job.run()
    assert list(job.messages.queue)[-1][0] == 'error'
    assert launch.open_count == 0
    assert launch.process.closed


@pytest.mark.parametrize('change', ['transient_owner', 'process_exit'])
def test_second_open_rechecks_binding_after_same_bus_lookup(launch, monkeypatch, change):
    first, second = launch.process, TrackedProcess()
    job = QueueJob(launch.paths)

    def finish_first(_interval):
        assert launch.open_count == 1
        first.finished = True
        launch.change_owner(None)
        launch.process = second
        return False

    def owner_reply(method, name):
        assert method == b'GetNameOwner'
        assert name == launch.player.dbus_name.encode()
        if launch.open_count == 1:
            if change == 'transient_owner':
                owner = launch.owner
                launch.change_owner(':1.99')
                launch.change_owner(owner)
            else:
                second.finished = True
        return 'owner reply'

    monkeypatch.setattr(job.cancelled, 'wait', finish_first)
    launch.transport._bus_reply.side_effect = owner_reply
    job.run()
    assert launch.transport._bus_reply.call_count == 2
    assert launch.open_count == 1
    assert first.closed and second.closed
    kind, message = list(job.messages.queue)[-1]
    assert kind == 'error'
    assert 'identity changed' in message


def test_cancellation_during_same_bus_owner_check_prevents_open(launch):
    job = QueueJob(launch.paths)

    def owner_reply(*args):
        job.stop()
        return 'owner reply'

    launch.transport._bus_reply.side_effect = owner_reply
    job.run()
    assert launch.open_count == 0
    assert launch.process.closed
    assert [kind for kind, _ in list(job.messages.queue)] == ['status']


@pytest.mark.parametrize('replace_bus', [False, True])
def test_open_transport_failure_stops_without_fallback_or_tracking(launch, replace_bus):
    launch.replace_bus = replace_bus
    launch.connected = replace_bus
    job = QueueJob(launch.paths)
    job.run()
    assert list(job.messages.queue)[-1][0] == 'error'
    assert launch.open_count == 1
    assert len(launch.calls) == 3
    assert not launch.acknowledged
    assert launch.process.closed
    assert not launch.process.polled.is_set()


@pytest.mark.parametrize('fail_pending', [False, True])
def test_cancel_while_open_is_pending_never_advances_or_kills_player(launch, fail_pending):
    launch.release.clear()
    if fail_pending:
        launch.connected = False
    job = QueueJob(launch.paths)
    job.start()
    try:
        assert launch.entered.wait(3)
        assert not launch.process.polled.is_set()
        job.stop()
        launch.release.set()
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert launch.open_count == 1
        assert launch.process.closed
        assert not launch.process.finished
        assert not any(kind in {'finished', 'error'} for kind, _ in list(job.messages.queue))
    finally:
        job.stop()
        launch.release.set()
        job.thread.join(3)


def test_cancel_after_acknowledgment_releases_handle_without_player_exit(launch):
    job = QueueJob(launch.paths)
    job.start()
    try:
        assert launch.process.polled.wait(3)
        job.stop()
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert launch.open_count == 1
        assert launch.process.closed
        assert not launch.process.finished
        assert not any(kind == 'finished' for kind, _ in list(job.messages.queue))
    finally:
        job.stop()
        job.thread.join(3)


def test_completion_reports_launch_requests_and_tracked_exits(launch):
    launch.process.finished = True
    # Both admission checks must see a live process; queue polling sees its exit.
    calls = 0
    def exited():
        nonlocal calls
        calls += 1
        if calls % 3 == 0:
            launch.change_owner(None)
            return True
        return False
    launch.process.exited = exited
    job = QueueJob(launch.paths)
    job.run()
    assert launch.open_count == 2
    assert list(job.messages.queue)[-1] == (
        'finished', 'All launch requests completed and their tracked player processes exited.')
