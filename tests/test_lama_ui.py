from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MARNWICK_DISABLE_CONFIG", "1")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest

from marnwick.ui import EditCommandDialog, MainWindow


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
