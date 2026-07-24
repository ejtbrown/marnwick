from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
import pytest

from marnwick.ui import EditCommandDialog, LamaBusyOverlay, MainWindow


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
