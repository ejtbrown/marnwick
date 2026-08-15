from __future__ import annotations

import os
from time import monotonic, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
import pytest

from marnwick.config import RemoteLamaConfig
from marnwick.remote_lama import certificate_sha1_thumbprint, trusted_certificate_der
from marnwick.ui import (
    EditCommandDialog,
    LamaBusyOverlay,
    MainWindow,
    RemoteLamaDialog,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_edit_dialog_exposes_lama_with_m_hotkey(app: QApplication) -> None:
    dialog = EditCommandDialog()
    dialog.show()

    assert dialog.list_widget.findItems(
        "M    LaMa",
        Qt.MatchFlag.MatchExactly,
    )
    QTest.keyClick(dialog.list_widget, Qt.Key.Key_M)

    assert dialog.result() == int(EditCommandDialog.DialogCode.Accepted)
    assert dialog.selected_command() == "lama"
    dialog.deleteLater()
    app.processEvents()


def test_tools_menu_exposes_lama_model_download(app: QApplication) -> None:
    window = MainWindow()

    assert "LaMa Model" in window.download_lama_action.text()
    assert window.remote_lama_action.text() == "Remote GPU…"

    window.close()
    window.deleteLater()
    app.processEvents()


def test_lama_busy_overlay_shows_indeterminate_local_progress(
    app: QApplication,
) -> None:
    parent = QWidget()
    parent.resize(800, 600)
    parent.show()
    overlay = LamaBusyOverlay(parent)
    overlay.setGeometry(parent.rect())

    overlay.start()
    app.processEvents()

    assert overlay.isVisible()
    assert overlay.progress.minimum() == 0
    assert overlay.progress.maximum() == 0
    assert "filling the masked area" in overlay.title_label.text()
    assert "Selecting the local processing runtime" in overlay.detail_label.text()

    overlay.set_execution_provider("WebGPU")

    assert "Using WebGPU for local inference" in overlay.detail_label.text()
    assert "fallback" not in overlay.detail_label.text().lower()

    overlay.stop()
    assert overlay.isHidden()
    parent.close()
    parent.deleteLater()
    app.processEvents()


def test_remote_lama_dialog_retrieves_and_trusts_certificate(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certificate = b"offered-certificate"
    monkeypatch.setattr(
        "marnwick.ui.retrieve_server_certificate",
        lambda host, port: certificate,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog = RemoteLamaDialog(RemoteLamaConfig())
    try:
        dialog.retrieve_button.click()
        deadline = monotonic() + 2
        while dialog.trusted_config is None and monotonic() < deadline:
            app.processEvents()
            dialog._settle_certificate()
            sleep(0.01)

        assert dialog.trusted_config is not None
        assert dialog.trusted_config.host == "172.31.254.1"
        assert dialog.trusted_config.port == 8443
        assert trusted_certificate_der(dialog.trusted_config) == certificate
        assert certificate_sha1_thumbprint(certificate) in dialog.thumbprint_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_lama_busy_overlay_describes_remote_transport(app: QApplication) -> None:
    parent = QWidget()
    overlay = LamaBusyOverlay(parent)

    overlay.start(remote=True)
    assert "pinned TLS" in overlay.detail_label.text()

    overlay.set_execution_provider("Remote GPU")
    assert "trusted HTTPS" in overlay.detail_label.text()
    assert "local inference" not in overlay.detail_label.text()

    overlay.stop()
    parent.deleteLater()
    app.processEvents()
