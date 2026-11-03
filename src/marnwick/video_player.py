"""Sequential playback for a snapshot of the thumbnail selection."""
from pathlib import Path
from collections.abc import Sequence

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout


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
