"""Video queue dialogs for a snapshot of the thumbnail selection."""
from pathlib import Path
from collections.abc import Sequence
from queue import Empty

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout

from .external_video import QueueJob


class VideoQueueDialog(QDialog):
    def __init__(self, paths: Sequence[Path], parent=None) -> None:
        super().__init__(parent)
        self.paths = tuple(paths)
        self.position = -1
        self._stopped = False
        self._advancing = False
        self.failed_paths: list[Path] = []
        self.setWindowTitle("Marnwick — Video queue")
        self.resize(960, 640)
        layout = QVBoxLayout(self)
        self.label = QLabel(self)
        layout.addWidget(self.label)
        self.video = QVideoWidget(self)
        layout.addWidget(self.video, 1)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.pause_button = QPushButton("Pause", self)
        buttons.addButton(self.pause_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.pause_button.clicked.connect(self.toggle_pause)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.player.mediaStatusChanged.connect(self._status_changed)
        self.player.errorOccurred.connect(self._error)
        QTimer.singleShot(0, self._next)

    def _next(self) -> None:
        if self._stopped:
            return
        self._advancing = True
        self.player.stop()
        self.position += 1
        if self.position >= len(self.paths):
            self.stop()
            if self.failed_paths:
                self.label.setText(
                    "Playback finished. Could not play: "
                    + ", ".join(path.name for path in self.failed_paths)
                )
                self.pause_button.setEnabled(False)
            else:
                self.accept()
            return
        path = self.paths[self.position]
        self.label.setText(f"{self.position + 1} / {len(self.paths)} — {path.name}")
        self.pause_button.setText("Pause")
        self._advancing = False
        if not path.is_file():
            self._error()
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        if not self._advancing:
            self.player.play()

    def _schedule_next(self, *, failed: bool = False) -> None:
        if self._stopped or self._advancing:
            return
        self._advancing = True
        if failed:
            self.failed_paths.append(self.paths[self.position])
        # A backend can emit both InvalidMedia and errorOccurred for one file.
        # Defer replacement of its source until that signal batch has finished.
        QTimer.singleShot(0, self._next)

    def _status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._schedule_next()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._schedule_next(failed=True)

    def _error(self, *_args) -> None:
        self._schedule_next(failed=True)

    def toggle_pause(self) -> None:
        if self._stopped or self._advancing:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.pause_button.setText("Play")
        else:
            self.player.play()
            self.pause_button.setText("Pause")

    def stop(self) -> None:
        self._stopped = True
        self.player.stop()

    def done(self, result: int) -> None:
        self.stop()
        super().done(result)


class LinuxVideoQueueDialog(QDialog):
    def __init__(self, paths: Sequence[Path], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Marnwick — Default-player queue")
        self.resize(560, 180)
        layout = QVBoxLayout(self)
        self.label = QLabel("Finding the default video player…", self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        instructions = QLabel("Close this queue to cancel future launches. The current player stays open.", self)
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_button.setAutoDefault(False)
        close_button.setDefault(False)
        self.job = QueueJob(paths)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(100)
        QTimer.singleShot(0, self, self.job.start)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def _drain(self):
        while True:
            try:
                kind, message = self.job.messages.get_nowait()
            except Empty:
                return
            if not self.job.cancelled.is_set():
                self.label.setText(("Queue stopped: " if kind == "error" else "") + message)

    def stop(self) -> None:
        self.job.stop()
        self.timer.stop()

    def done(self, result: int) -> None:
        self.stop()
        super().done(result)
