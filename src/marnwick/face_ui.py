from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QStringListModel, Qt, QTimer
from PySide6.QtGui import QIcon, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .async_utils import RolloverThreadPoolExecutor
from .catalog import Catalog, CatalogStorageIdentity
from .faces import (
    FACE_STATUS_ACTIVE,
    FACE_STATUS_IGNORED,
    FACE_STATUS_NOT_FACE,
    FaceReviewGroup,
    FaceStore,
    FaceTile,
    PersonRecord,
)


FaceMutationSubmitter = Callable[[str, tuple[int, ...], object | None], Future[object]]
OpenPhotoCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class FaceGroupLoad:
    generation: int
    groups: tuple[FaceReviewGroup, ...]
    people: tuple[PersonRecord, ...]
    stats: dict[str, int]


@dataclass(frozen=True, slots=True)
class FaceTileLoad:
    group_key: str
    tiles: tuple[FaceTile, ...]
    thumbnail_bytes: tuple[bytes, ...]


class FaceManagerDialog(QDialog):
    """Keyboard-first batch review for catalog-local face suggestions."""

    def __init__(
        self,
        catalog: Catalog,
        mutation_submitter: FaceMutationSubmitter,
        parent: QWidget | None = None,
        *,
        initial_view: str = "review",
        open_photo: OpenPhotoCallback | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self._submit_mutation = mutation_submitter
        self._open_photo = open_photo
        self._groups: tuple[FaceReviewGroup, ...] = ()
        self._people: tuple[PersonRecord, ...] = ()
        self._current_group: FaceReviewGroup | None = None
        self._current_tiles: tuple[FaceTile, ...] = ()
        self._group_future: Future[FaceGroupLoad] | None = None
        self._tile_future: Future[FaceTileLoad] | None = None
        self._mutation_future: Future[object] | None = None
        self._pending_mutation: tuple[
            str,
            tuple[int, ...],
            object | None,
        ] | None = None
        self._load_generation = 0
        self._closed = False
        self._busy = False
        self._showing_all_faces = False
        self._loader = RolloverThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-face-review",
            max_pending=1,
            max_retired=2,
        )
        self.setWindowTitle(f"People — {catalog.root.name or catalog.root}")
        self.resize(1180, 760)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Marnwick proposes coherent groups; you supply identity. "
            "Batch decisions are reversible and remain local to this catalog."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.view_combo = QComboBox()
        for label, value in (
            ("Review by payoff", "review"),
            ("Likely known people", "suggestions"),
            ("Unnamed groups", "unnamed"),
            ("Named people", "people"),
            ("Loose faces", "loose"),
            ("Ignored", "ignored"),
            ("Not a face", "not_faces"),
        ):
            self.view_combo.addItem(label, value)
        selected_view = self.view_combo.findData(initial_view)
        self.view_combo.setCurrentIndex(selected_view if selected_view >= 0 else 0)
        self.view_combo.currentIndexChanged.connect(lambda _index: self.refresh_groups())
        controls.addWidget(QLabel("Queue"))
        controls.addWidget(self.view_combo)
        self.stats_label = QLabel()
        controls.addWidget(self.stats_label, 1)
        self.undo_button = QPushButton("Undo last")
        self.undo_button.setToolTip("Undo the most recent face-management decision")
        self.undo_button.clicked.connect(lambda: self._mutate("undo", (), None))
        controls.addWidget(self.undo_button)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_groups)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        splitter = QSplitter()
        self.group_list = QListWidget()
        self.group_list.setMinimumWidth(310)
        self.group_list.currentRowChanged.connect(self._group_selected)
        self.group_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.group_list.customContextMenuRequested.connect(
            self._open_group_context_menu
        )
        splitter.addWidget(self.group_list)

        review_panel = QWidget()
        review_layout = QVBoxLayout(review_panel)
        self.group_title = QLabel("Select a review group")
        self.group_title.setStyleSheet("font-size: 18px; font-weight: 600")
        review_layout.addWidget(self.group_title)
        self.group_detail = QLabel()
        self.group_detail.setWordWrap(True)
        review_layout.addWidget(self.group_detail)

        self.face_list = QListWidget()
        self.face_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.face_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.face_list.setMovement(QListWidget.Movement.Static)
        self.face_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.face_list.setIconSize(QSize(112, 112))
        self.face_list.setGridSize(QSize(142, 156))
        self.face_list.currentRowChanged.connect(self._face_selected)
        self.face_list.itemSelectionChanged.connect(self._update_remove_button)
        self.face_list.itemDoubleClicked.connect(lambda _item: self._open_selected_photo())
        review_layout.addWidget(self.face_list, 1)

        self.context_label = QLabel("Double-click a face to open its source photograph.")
        review_layout.addWidget(self.context_label)

        identity = QHBoxLayout()
        self.name_entry = QLineEdit()
        self.name_entry.setPlaceholderText("Person name")
        self._person_name_model = QStringListModel([], self)
        self._name_completer = QCompleter([], self)
        self._name_completer.setModel(self._person_name_model)
        self._name_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._name_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.name_entry.setCompleter(self._name_completer)
        self.name_entry.returnPressed.connect(self._confirm_or_name)
        identity.addWidget(self.name_entry, 1)
        self.name_button = QPushButton("Name group")
        self.name_button.clicked.connect(self._confirm_or_name)
        identity.addWidget(self.name_button)
        self.review_all_button = QPushButton("Show all faces")
        self.review_all_button.setToolTip(
            "Load every face in this group instead of the representative 16"
        )
        self.review_all_button.clicked.connect(self._load_all_current_faces)
        identity.addWidget(self.review_all_button)
        review_layout.addLayout(identity)

        decisions = QHBoxLayout()
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setToolTip("Confirm the proposed identity (Enter)")
        self.confirm_button.clicked.connect(self._confirm_or_name)
        decisions.addWidget(self.confirm_button)
        self.different_button = QPushButton("Different person")
        self.different_button.setToolTip("Remember this negative relationship (X)")
        self.different_button.clicked.connect(self._different)
        decisions.addWidget(self.different_button)
        self.remove_button = QPushButton("Remove from group")
        self.remove_button.setToolTip(
            "Remove the selected face or faces from this group (R)"
        )
        self.remove_button.clicked.connect(self._remove_selected_from_group)
        self.remove_button.setEnabled(False)
        decisions.addWidget(self.remove_button)
        self.unsure_button = QPushButton("Not sure")
        self.unsure_button.setToolTip("Defer for seven days (?)")
        self.unsure_button.clicked.connect(lambda: self._mutate("defer", self._target_ids(), None))
        decisions.addWidget(self.unsure_button)
        self.ignore_button = QPushButton("Ignore")
        self.ignore_button.setToolTip("Hide a real face or person you do not want organized (I)")
        self.ignore_button.clicked.connect(
            lambda: self._mutate("status", self._target_ids(), FACE_STATUS_IGNORED)
        )
        decisions.addWidget(self.ignore_button)
        self.not_face_button = QPushButton("Not a face")
        self.not_face_button.setToolTip("Suppress an incorrect detection (Delete)")
        self.not_face_button.clicked.connect(
            lambda: self._mutate("status", self._target_ids(), FACE_STATUS_NOT_FACE)
        )
        decisions.addWidget(self.not_face_button)
        self.group_selected_button = QPushButton("Group selected")
        self.group_selected_button.setToolTip(
            "Group the selected loose faces and run identity recognition"
        )
        self.group_selected_button.clicked.connect(self._group_selected_loose_faces)
        self.group_selected_button.setVisible(False)
        decisions.addWidget(self.group_selected_button)
        review_layout.addLayout(decisions)
        self._remove_shortcut = QShortcut(QKeySequence("R"), self)
        self._remove_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._remove_shortcut.activated.connect(self._remove_selected_from_group)
        self.name_entry.installEventFilter(self)
        splitter.addWidget(review_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._settle_work)
        self._poll_timer.start()
        self.refresh_groups()

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()
        if self._group_future is not None:
            self._group_future.cancel()
        if self._tile_future is not None:
            self._tile_future.cancel()
        self._loader.shutdown(wait=False, cancel_futures=True)

    def done(self, result: int) -> None:
        self._shutdown()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown()
        super().closeEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.name_entry:
            if event.type() == QEvent.Type.FocusIn:
                self._remove_shortcut.setEnabled(False)
            elif event.type() == QEvent.Type.FocusOut:
                self._remove_shortcut.setEnabled(not self._busy)
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.name_entry.hasFocus():
            super().keyPressEvent(event)
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self._confirm_or_name()
            return
        if event.key() == Qt.Key.Key_X:
            self._different()
            return
        if (
            event.key() == Qt.Key.Key_R
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self._remove_selected_from_group()
            return
        if event.key() == Qt.Key.Key_I:
            self._mutate("status", self._target_ids(), FACE_STATUS_IGNORED)
            return
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            self._mutate("status", self._target_ids(), FACE_STATUS_NOT_FACE)
            return
        if event.key() == Qt.Key.Key_Question:
            self._mutate("defer", self._target_ids(), None)
            return
        if event.key() == Qt.Key.Key_Space:
            self._open_selected_photo()
            return
        super().keyPressEvent(event)

    def refresh_groups(self) -> None:
        if self._mutation_future is not None:
            return
        self._load_generation += 1
        generation = self._load_generation
        if self._group_future is not None:
            self._group_future.cancel()
        self.group_list.clear()
        self.face_list.clear()
        self.group_title.setText("Building the prioritized review queue…")
        self.group_detail.setText("Large, coherent decisions are presented first.")
        self._set_busy(True)
        try:
            self._group_future = self._loader.submit(
                _load_groups,
                self.catalog.root,
                self.catalog.root_identity,
                self.catalog.storage_identity,
                str(self.view_combo.currentData()),
                generation,
            )
        except RuntimeError:
            self.group_title.setText("The catalog reader is busy")
            self._set_busy(False)

    def _group_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._groups):
            self._current_group = None
            return
        group = self._groups[row]
        self._current_group = group
        self._showing_all_faces = False
        self.confirm_button.setText(
            "Restore" if group.kind in {"ignored", "not_faces"} else "Confirm"
        )
        self.group_title.setText(f"{group.title} · {group.count:,} face{'s' if group.count != 1 else ''}")
        if group.kind == "proposal":
            self.group_detail.setText(
                f"Every candidate clears the conservative identity threshold. "
                f"The least-certain score in this core is {group.confidence:.3f}."
            )
            self.name_entry.setText(group.proposed_person_name or "")
        elif group.kind == "unnamed":
            self.group_detail.setText(
                "A conservative similarity core. Representatives deliberately span its appearance variation."
            )
            self.name_entry.clear()
        elif group.kind == "named":
            self.group_detail.setText("Confirmed faces for this person. Select mistakes to correct or ignore.")
            self.name_entry.setText(group.proposed_person_name or "")
        else:
            self.group_detail.setText("Use context or defer this low-payoff decision until later.")
            self.name_entry.clear()
        self.review_all_button.setVisible(
            len(group.face_ids) > len(group.representative_ids)
        )
        self.group_selected_button.setVisible(group.kind == "loose")
        self._load_tiles(group, group.representative_ids)
        self._update_remove_button()
        QTimer.singleShot(0, self._focus_name_entry)

    def _focus_name_entry(self) -> None:
        if self._closed or self._current_group is None or not self.name_entry.isEnabled():
            return
        self.name_entry.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_entry.selectAll()

    def _load_all_current_faces(self) -> None:
        if self._current_group is not None:
            self._showing_all_faces = True
            self.review_all_button.hide()
            self._load_tiles(self._current_group, self._current_group.face_ids)

    def _load_tiles(self, group: FaceReviewGroup, face_ids: Sequence[int]) -> None:
        if self._tile_future is not None:
            self._tile_future.cancel()
        self.face_list.clear()
        self.context_label.setText("Loading face crops…")
        try:
            self._tile_future = self._loader.submit(
                _load_tiles,
                self.catalog.root,
                self.catalog.root_identity,
                self.catalog.storage_identity,
                group.key,
                tuple(face_ids),
            )
        except RuntimeError:
            self.context_label.setText("Catalog reader busy; choose the group again to retry.")

    def _face_selected(self, row: int) -> None:
        if 0 <= row < len(self._current_tiles):
            tile = self._current_tiles[row]
            self.context_label.setText(
                f"{tile.rel_path} · detection {tile.detection_score:.2f} · quality {tile.quality:.2f}"
            )

    def _open_selected_photo(self) -> None:
        row = self.face_list.currentRow()
        if self._open_photo is not None and 0 <= row < len(self._current_tiles):
            self._open_photo(self._current_tiles[row].rel_path)

    def _target_ids(self) -> tuple[int, ...]:
        selected = self._selected_ids()
        if selected:
            return selected
        return self._current_group.face_ids if self._current_group is not None else ()

    def _selected_ids(self) -> tuple[int, ...]:
        return tuple(
            int(item.data(Qt.ItemDataRole.UserRole))
            for item in self.face_list.selectedItems()
        )

    def _can_remove_selected_from_group(self) -> bool:
        group = self._current_group
        selected = self._selected_ids()
        if group is None or not selected:
            return False
        if group.kind == "unnamed":
            selected_set = set(selected)
            return any(face_id not in selected_set for face_id in group.face_ids)
        return group.kind in {"proposal", "named"} and group.proposed_person_id is not None

    def _update_remove_button(self) -> None:
        if hasattr(self, "remove_button"):
            self.remove_button.setEnabled(
                not self._busy and self._can_remove_selected_from_group()
            )
        if hasattr(self, "group_selected_button"):
            self.group_selected_button.setEnabled(
                not self._busy
                and self._current_group is not None
                and self._current_group.kind == "loose"
                and len(self._selected_ids()) >= 2
            )

    def _group_selected_loose_faces(self) -> None:
        selected = self._selected_ids()
        if (
            self._current_group is None
            or self._current_group.kind != "loose"
            or len(selected) < 2
        ):
            return
        self._mutate("group", selected, None)

    def _open_group_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        item = self.group_list.itemAt(pos)
        if item is None:
            return
        row = self.group_list.row(item)
        if row < 0 or row >= len(self._groups):
            return
        self.group_list.setCurrentRow(row)
        menu = QMenu(self.group_list)
        loose_action = menu.addAction("Mark all faces as loose")
        selected = menu.exec(self.group_list.viewport().mapToGlobal(pos))
        menu.deleteLater()
        if selected == loose_action:
            self._mark_current_group_loose()

    def _mark_current_group_loose(self) -> None:
        group = self._current_group
        if group is None:
            return
        self._mutate(
            "loose",
            group.face_ids,
            group.proposed_person_id,
        )

    def _remove_selected_from_group(self) -> None:
        group = self._current_group
        selected = self._selected_ids()
        if group is None or not selected:
            return
        selected_set = set(selected)
        if group.kind == "unnamed":
            remainder = tuple(
                face_id for face_id in group.face_ids if face_id not in selected_set
            )
            if remainder:
                self._mutate("remove", selected, {"remaining_face_ids": remainder})
            return
        if group.kind in {"proposal", "named"} and group.proposed_person_id is not None:
            self._mutate(
                "remove",
                selected,
                {"person_id": group.proposed_person_id},
            )

    def _confirm_or_name(self) -> None:
        group = self._current_group
        ids = self._target_ids()
        if group is None or not ids:
            return
        if group.kind in {"ignored", "not_faces"}:
            self._mutate("status", ids, FACE_STATUS_ACTIVE)
            return
        name = " ".join(self.name_entry.text().split())
        if group.kind == "proposal" and group.proposed_person_id is not None:
            self._mutate(
                "name",
                ids,
                {"name": group.proposed_person_name or name, "person_id": group.proposed_person_id},
            )
            return
        if not name:
            self.name_entry.setFocus()
            return
        self._mutate("name", ids, {"name": name, "person_id": None})

    def _different(self) -> None:
        group = self._current_group
        if group is None:
            return
        if group.proposed_person_id is not None:
            self._mutate("different", self._target_ids(), group.proposed_person_id)
            return
        selected = self._selected_ids()
        selected_set = set(selected)
        remainder = tuple(face_id for face_id in group.face_ids if face_id not in selected_set)
        if group.kind == "unnamed" and selected and remainder:
            self._mutate("separate", selected, remainder)
            return
        QMessageBox.information(
            self,
            "Different person",
            (
                "Select the representative face or faces that do not belong in this "
                "unnamed group, then choose Different person."
                if group.kind == "unnamed"
                else "This action applies to a proposed identity or a selected subset of an unnamed group."
            ),
        )

    def _mutate(self, kind: str, face_ids: tuple[int, ...], value: object | None) -> None:
        if self._mutation_future is not None or (kind != "undo" and not face_ids):
            return
        try:
            self._mutation_future = self._submit_mutation(kind, face_ids, value)
        except Exception as error:
            QMessageBox.critical(self, "People", str(error))
            return
        self._pending_mutation = (kind, face_ids, value)
        self._set_busy(True)
        self.group_detail.setText("Applying the reversible catalog decision…")

    def _settle_work(self) -> None:
        group_future = self._group_future
        if group_future is not None and group_future.done():
            self._group_future = None
            if not group_future.cancelled():
                try:
                    result = group_future.result()
                except Exception as error:
                    self.group_title.setText("Could not build the review queue")
                    self.group_detail.setText(str(error))
                    self._set_busy(False)
                else:
                    if result.generation != self._load_generation:
                        return
                    self._groups = result.groups
                    self._people = result.people
                    self._person_name_model.setStringList(
                        [person.name for person in self._people]
                    )
                    self.stats_label.setText(
                        f"{result.stats['named']:,}/{result.stats['faces']:,} named · "
                        f"{result.stats['people']:,} people · {result.stats['pending_images']:,} images pending"
                    )
                    self.group_list.clear()
                    for group in self._groups:
                        self.group_list.addItem(self._group_list_label(group))
                    if self._groups:
                        self.group_list.setCurrentRow(0)
                    else:
                        self.group_title.setText("Nothing needs review in this queue")
                        self.group_detail.setText("Background face indexing may add new decisions later.")
                    self._set_busy(False)
        tile_future = self._tile_future
        if tile_future is not None and tile_future.done():
            self._tile_future = None
            if not tile_future.cancelled():
                try:
                    result = tile_future.result()
                except Exception as error:
                    self.context_label.setText(str(error))
                else:
                    if self._current_group is not None and result.group_key == self._current_group.key:
                        self._current_tiles = result.tiles
                        self.face_list.clear()
                        for tile, data in zip(result.tiles, result.thumbnail_bytes, strict=True):
                            item = QListWidgetItem(tile.filename)
                            item.setData(Qt.ItemDataRole.UserRole, tile.id)
                            pixmap = QPixmap()
                            if data and pixmap.loadFromData(data, "JPEG"):
                                item.setIcon(QIcon(pixmap))
                            item.setToolTip(tile.rel_path)
                            self.face_list.addItem(item)
                        self.context_label.setText(
                            f"Showing {len(result.tiles):,} face crops. "
                            "No selection means the decision applies to the complete group."
                        )
        mutation_future = self._mutation_future
        if mutation_future is not None and mutation_future.done():
            self._mutation_future = None
            try:
                result = mutation_future.result()
            except Exception as error:
                self._pending_mutation = None
                QMessageBox.critical(self, "People", str(error))
                self._set_busy(False)
            else:
                pending = self._pending_mutation
                self._pending_mutation = None
                if pending is None:
                    self.refresh_groups()
                    return
                kind, requested_ids, value = pending
                local_exclusion = kind in {
                    "remove",
                    "defer",
                    "different",
                    "separate",
                } or (
                    kind == "status" and value != FACE_STATUS_ACTIVE
                )
                if local_exclusion:
                    affected_ids = (
                        tuple(int(face_id) for face_id in result)
                        if isinstance(result, (tuple, list))
                        else requested_ids
                    )
                    self._apply_local_exclusion(affected_ids)
                    self._set_busy(False)
                elif kind == "group":
                    review_index = self.view_combo.findData("review")
                    if review_index >= 0 and self.view_combo.currentIndex() != review_index:
                        self.view_combo.setCurrentIndex(review_index)
                    else:
                        self.refresh_groups()
                else:
                    self.refresh_groups()

    @staticmethod
    def _group_list_label(group: FaceReviewGroup) -> str:
        suffix = f" · {group.confidence:.3f}" if group.kind == "proposal" else ""
        return f"{group.title}    {group.count:,}{suffix}"

    def _apply_local_exclusion(self, face_ids: Sequence[int]) -> None:
        group = self._current_group
        row = self.group_list.currentRow()
        if group is None or row < 0 or row >= len(self._groups):
            return
        removed = set(int(face_id) for face_id in face_ids)
        remaining_ids = tuple(
            face_id for face_id in group.face_ids if face_id not in removed
        )
        if len(remaining_ids) == len(group.face_ids):
            return
        for item_row in range(self.face_list.count() - 1, -1, -1):
            item = self.face_list.item(item_row)
            if int(item.data(Qt.ItemDataRole.UserRole)) in removed:
                self.face_list.takeItem(item_row)
        self._current_tiles = tuple(
            tile for tile in self._current_tiles if tile.id not in removed
        )
        groups = list(self._groups)
        if not remaining_ids:
            groups.pop(row)
            self._groups = tuple(groups)
            self._current_group = None
            self.group_list.takeItem(row)
            if groups:
                self.group_list.setCurrentRow(min(row, len(groups) - 1))
            else:
                self.group_title.setText("Nothing remains in this queue")
                self.group_detail.setText(
                    "Use Refresh to include decisions added by background indexing."
                )
                self.face_list.clear()
                self.review_all_button.hide()
                self.group_selected_button.hide()
            return
        updated = replace(
            group,
            face_ids=remaining_ids,
            representative_ids=tuple(
                face_id
                for face_id in group.representative_ids
                if face_id not in removed
            ),
            count=len(remaining_ids),
        )
        groups[row] = updated
        self._groups = tuple(groups)
        self._current_group = updated
        item = self.group_list.item(row)
        if item is not None:
            item.setText(self._group_list_label(updated))
        self.group_title.setText(
            f"{updated.title} · {updated.count:,} "
            f"face{'s' if updated.count != 1 else ''}"
        )
        self.group_detail.setText(
            "Excluded faces were removed in place; continue reviewing this group."
        )
        self.review_all_button.setVisible(
            not self._showing_all_faces
            and len(updated.face_ids) > len(updated.representative_ids)
        )
        self.context_label.setText(
            f"Showing {self.face_list.count():,} face crops. "
            "Excluded faces were removed without rebuilding the queue."
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self.view_combo,
            self.group_list,
            self.name_entry,
            self.name_button,
            self.confirm_button,
            self.different_button,
            self.remove_button,
            self.unsure_button,
            self.ignore_button,
            self.not_face_button,
            self.undo_button,
            self.review_all_button,
            self.group_selected_button,
        ):
            widget.setEnabled(not busy)
        self._remove_shortcut.setEnabled(not busy and not self.name_entry.hasFocus())
        self._update_remove_button()


def _load_groups(
    root: Path,
    root_identity: tuple[int, int],
    storage_identity: CatalogStorageIdentity,
    view: str,
    generation: int,
) -> FaceGroupLoad:
    with Catalog.open_reader(
        root,
        expected_root_identity=root_identity,
        expected_storage_identity=storage_identity,
    ) as catalog:
        store = FaceStore(catalog)
        return FaceGroupLoad(
            generation=generation,
            groups=tuple(store.groups_for_view(view)),
            people=tuple(store.people()),
            stats=store.stats(),
        )


def _load_tiles(
    root: Path,
    root_identity: tuple[int, int],
    storage_identity: CatalogStorageIdentity,
    group_key: str,
    face_ids: tuple[int, ...],
) -> FaceTileLoad:
    with Catalog.open_reader(
        root,
        expected_root_identity=root_identity,
        expected_storage_identity=storage_identity,
    ) as catalog:
        store = FaceStore(catalog)
        tiles = tuple(store.tiles(face_ids))
        return FaceTileLoad(
            group_key=group_key,
            tiles=tiles,
            thumbnail_bytes=tuple(store.thumbnail_bytes(tile) for tile in tiles),
        )
