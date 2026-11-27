"""Isolated process and queue tests that never launch an installed media player."""
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

import pytest

from marnwick import external_video as backend
from marnwick.external_video import QueueJob

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux backend fixtures use POSIX paths")


@pytest.fixture
def clips(tmp_path):
    paths = (tmp_path / 'z space;$(echo NO).mp4', tmp_path / '-a.mp4')
    for path in paths:
        path.write_bytes(b'synthetic; no decoder is used')
    return paths


def drain(job):
    values = []
    while not job.messages.empty():
        values.append(job.messages.get_nowait())
    return values


def test_vlc_argv_preserves_order_and_literal_paths(clips, monkeypatch):
    player = backend.DefaultPlayer('VLC', 'vlc.desktop', '/usr/bin/vlc')
    launched = []

    def popen(args, **kwargs):
        assert not kwargs.get('shell', False)
        launched.append(args)

    monkeypatch.setattr(backend.subprocess, 'Popen', popen)
    backend.launch_native(player, clips)
    command, = launched
    assert command[0] == '/usr/bin/vlc'
    assert command[-2:] == [p.as_uri() for p in clips]
    assert command[1:6] == ['--no-one-instance', '--no-playlist-enqueue', '--no-random',
                             '--no-loop', '--no-repeat']
    assert '--no-play-and-stop' in command and '--no-start-paused' in command
    assert not backend.DefaultPlayer('VLC', 'org.videolan.VLC.desktop', '/usr/bin/flatpak').native_queue
    assert not backend.DefaultPlayer('Other', 'other.desktop', '/usr/bin/vlc').native_queue


def test_query_failure_reports_helper_error_without_operands(monkeypatch):
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, ['helper', 'fixture operand'], stderr='No association')
    monkeypatch.setattr(backend.subprocess, 'run', fail)
    with pytest.raises(backend.PlayerError, match='No association') as error:
        backend._run(['/helper', 'fixture operand'])
    assert 'fixture operand' not in str(error.value)


def test_native_handoff_is_one_launch_not_pid_sequencing(clips, monkeypatch):
    player = backend.DefaultPlayer('VLC', 'vlc.desktop', '/usr/bin/vlc')
    calls = []
    monkeypatch.setattr(backend, 'default_player', lambda path: player)
    class Child:
        def wait(self):
            calls.append('reaped')
            return 0
    monkeypatch.setattr(backend, 'launch_native', lambda p, paths: calls.append(paths) or Child())
    monkeypatch.setattr(backend, 'launch_one', lambda *a: pytest.fail('must delegate native playlist'))
    job = QueueJob(clips)
    job.run()
    assert calls == [clips, 'reaped']
    assert [kind for kind, _ in drain(job)] == ['native']


class Process:
    pid = 123
    def __init__(self, exited=False):
        self.finished = exited
        self.closed = False
        self.polled = Event()
    def exited(self):
        self.polled.set()
        return self.finished
    def close(self):
        self.closed = True


def test_fallback_waits_for_actual_process_exit_and_preserves_order(clips, monkeypatch):
    player = backend.DefaultPlayer('Showtime', 'org.gnome.Showtime.desktop')
    monkeypatch.setattr(backend, 'default_player', lambda path: player)
    processes = [Process(), Process()]
    launched = []
    def launch(p, path, *_):
        launched.append(path)
        return processes[len(launched) - 1]
    monkeypatch.setattr(backend, 'launch_one', launch)
    job = QueueJob(clips)
    job.start()
    try:
        assert processes[0].polled.wait(3)
        assert launched == [clips[0]]  # includes pause/EOS: no process exit
        processes[0].finished = True
        assert processes[1].polled.wait(3)
        assert launched == list(clips)
        processes[1].finished = True
        job.thread.join(3)
        assert not job.thread.is_alive()
        assert all(p.closed for p in processes)
        assert drain(job)[-1][0] == 'finished'
    finally:
        job.stop()
        job.thread.join(3)


def test_cancel_preserves_player_and_prevents_remaining_launches(clips, monkeypatch):
    monkeypatch.setattr(backend, 'default_player', lambda path: backend.DefaultPlayer('Other', 'other'))
    process = Process()
    launched = []
    monkeypatch.setattr(backend, 'launch_one', lambda p, path, *_: launched.append(path) or process)
    job = QueueJob(clips)
    job.start()
    assert process.polled.wait(3)
    job.stop()
    job.thread.join(3)
    process.finished = True  # late completion must not resume the queue
    assert not job.thread.is_alive()
    assert launched == [clips[0]]
    assert process.closed
    assert not any(kind == 'finished' for kind, _ in drain(job))


def test_cancel_during_discovery_prevents_launch(clips, monkeypatch):
    job = QueueJob(clips)
    def discover(path):
        job.stop()
        return backend.DefaultPlayer('Other', 'other')
    monkeypatch.setattr(backend, 'default_player', discover)
    monkeypatch.setattr(backend, 'launch_one', lambda *a: pytest.fail('cancelled'))
    job.run()
    assert drain(job) == []


@pytest.mark.parametrize('failure', ['launch', 'monitor', 'missing'])
def test_failures_stop_without_launching_next(clips, monkeypatch, failure):
    monkeypatch.setattr(backend, 'default_player', lambda path: backend.DefaultPlayer('Other', 'other'))
    calls = []
    class Broken(Process):
        def exited(self):
            raise backend.PlayerError('tracking unavailable')
    def launch(p, path, *_):
        calls.append(path)
        if failure == 'launch':
            raise backend.PlayerError('launch failed')
        return Broken()
    monkeypatch.setattr(backend, 'launch_one', launch)
    job = QueueJob((clips[0], clips[1] / 'missing') if failure == 'missing' else clips)
    job.run()
    assert calls == ([] if failure == 'missing' else [clips[0]])
    assert drain(job)[-1][0] == 'error'


def test_linux_discovery_reads_actual_association_and_not_display_name(tmp_path, clips, monkeypatch):
    data = tmp_path / 'share'
    apps = data / 'applications'
    apps.mkdir(parents=True)
    (apps / 'org.gnome.Showtime.desktop').write_text(
        '[Desktop Entry]\nName=VLC\nExec=showtime --new-window %U\nDBusActivatable=true\n')
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    monkeypatch.setenv('XDG_DATA_DIRS', str(data))
    monkeypatch.setattr(backend, '_tool', lambda n: n)
    monkeypatch.setattr(backend.shutil, 'which', lambda n: '/usr/bin/' + n)
    monkeypatch.setattr(backend, '_run', lambda args: 'video/mp4' if args[2] == 'filetype' else 'org.gnome.Showtime.desktop')
    player = backend._linux_player(clips[0])
    assert player.dbus_name == 'org.gnome.Showtime'
    assert not player.native_queue


def launch_protocol(player, path, process=None, open_file=None):
    from unittest.mock import Mock

    return backend.launch_one(player, path, Event(), Mock(previous=None, signals=Mock(pin_process=Mock(return_value=process), open=open_file or Mock())))


def test_linux_launcher_exit_does_not_signal_completion(clips, monkeypatch):
    calls = []
    monkeypatch.setattr(backend, '_tool', lambda n: n)
    monkeypatch.setattr(backend, '_run', lambda args: pytest.fail('unexpected Open helper'))
    monkeypatch.setattr(backend, '_dbus_owner', lambda name: ':1.42')
    monkeypatch.setattr(backend, '_dbus', lambda method, *args: '(uint32 2,)')
    process = Process()
    player = backend.DefaultPlayer('Showtime', 'org.gnome.Showtime.desktop', desktop_file=Path('/org.gnome.Showtime.desktop'), dbus_name='org.gnome.Showtime')

    def open_file(name, owner, object_path, uri, cancelled, check_binding):
        check_binding()
        calls.append((name, owner, object_path, uri, cancelled))

    assert launch_protocol(player, clips[0], process, open_file) is process
    assert not process.exited()
    (name, owner, object_path, uri, cancelled), = calls
    assert (name, owner, object_path, uri) == (
        player.dbus_name, ':1.42', '/org/gnome/Showtime', clips[0].as_uri())
    assert not cancelled.is_set()


def test_linux_untrackable_launcher_stops_after_first_open(clips, monkeypatch):
    calls = []
    monkeypatch.setattr(backend, '_tool', lambda n: n)
    monkeypatch.setattr(backend, '_run', lambda args: calls.append(args) or '')
    with pytest.raises(backend.PlayerError, match='actual player PID'):
        launch_protocol(backend.DefaultPlayer('Other', 'other', desktop_file=Path('/test.desktop')), clips[0])
    assert len(calls) == 1
    assert calls == [['gio', 'launch', '/test.desktop', clips[0].as_uri()]]


@pytest.mark.skipif(not sys.platform.startswith('linux') or not hasattr(os, 'pidfd_open'), reason='Linux pidfd')
def test_pidfd_tracks_real_child_lifetime(monkeypatch):
    child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    try:
        tracker = backend.LinuxPlayerProcess(child.pid, os.pidfd_open(child.pid))
        try:
            assert not tracker.exited()
            tracker.owner_binding = ('org.example.Player', ':1.42')
            monkeypatch.setattr(backend, '_dbus_owner', lambda name: ':1.42')
            assert not tracker.exited()
            child.stdin.close()
            child.wait(timeout=5)
            assert not tracker.exited()  # The synthetic bus has not released the name yet.
            monkeypatch.setattr(backend, '_dbus_owner', lambda name: None)
            assert tracker.exited()
        finally:
            tracker.close()
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_closed_dialog_never_launches(clips, monkeypatch):
    pytest.importorskip("PySide6")
    from marnwick.video_player import LinuxVideoQueueDialog
    from test_ui import app

    qt = app()
    monkeypatch.setattr(backend, 'default_player', lambda p: pytest.fail('closed dialog'))
    dialog = LinuxVideoQueueDialog(clips)
    dialog.reject()
    qt.processEvents()
    assert dialog.job.cancelled.is_set()
    dialog.job.thread.join(3)
    dialog.deleteLater()
    qt.processEvents()


def test_queue_status_displays_angle_brackets_literally(clips, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt

    from marnwick.video_player import LinuxVideoQueueDialog
    from test_ui import app

    qt = app()
    monkeypatch.setattr(QueueJob, "start", lambda self: None)
    dialog = LinuxVideoQueueDialog(clips)
    try:
        dialog.job.messages.put(("error", "Could not open <b>clip.mp4"))
        dialog._drain()
        assert dialog.label.textFormat() == Qt.TextFormat.PlainText
        assert dialog.label.text() == "Queue stopped: Could not open <b>clip.mp4"
    finally:
        dialog.reject()
        dialog.deleteLater()
        qt.processEvents()


def test_mixed_defaults_use_one_file_at_a_time(clips, monkeypatch):
    players = [backend.DefaultPlayer('VLC', 'vlc.desktop', '/usr/bin/vlc'),
               backend.DefaultPlayer('Other', 'other')]
    monkeypatch.setattr(backend, 'default_player', lambda path: players[clips.index(path)])
    launches = []
    monkeypatch.setattr(backend, 'launch_native', lambda *a: pytest.fail('mixed associations'))
    monkeypatch.setattr(backend, 'launch_one', lambda player, path, *_: launches.append((player, path)) or Process(True))
    job = QueueJob(clips)
    job.run()
    assert launches == list(zip(players, clips))


def test_cancel_during_launch_closes_tracker_without_advancing(clips, monkeypatch):
    player = backend.DefaultPlayer('Other', 'other')
    monkeypatch.setattr(backend, 'default_player', lambda path: player)
    job = QueueJob(clips)
    process = Process(True)
    calls = []
    def launch(player, path, *_):
        calls.append(path)
        job.stop()
        return process
    monkeypatch.setattr(backend, 'launch_one', launch)
    job.run()
    assert calls == [clips[0]]
    assert process.closed


def test_native_launch_failure_is_reported(clips, monkeypatch):
    monkeypatch.setattr(backend, 'default_player', lambda path: backend.DefaultPlayer('VLC', 'vlc.desktop', '/usr/bin/vlc'))
    class Failed:
        def wait(self):
            return 1
    monkeypatch.setattr(backend, 'launch_native', lambda *a: Failed())
    job = QueueJob(clips)
    job.run()
    assert drain(job)[-1] == ('error', 'VLC exited with status 1. Check the playlist in the player.')


def test_dbus_identity_change_releases_pid_handle(clips, monkeypatch):
    monkeypatch.setattr(backend, '_tool', lambda n: n)
    monkeypatch.setattr(backend, '_run', lambda args: '()')
    owners = iter([':1.42', ':1.43'])
    monkeypatch.setattr(backend, '_dbus_owner', lambda name: next(owners))
    monkeypatch.setattr(backend, '_dbus', lambda method, *args: '(uint32 2,)')
    process = Process()
    with pytest.raises(backend.PlayerError, match='identity changed'):
        launch_protocol(backend.DefaultPlayer('Other', 'other', desktop_file=Path('/org.test.desktop'), dbus_name='org.test'), clips[0], process)
    assert process.closed
