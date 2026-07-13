from __future__ import annotations

import argparse
import json
from math import ceil, hypot
import os
import stat
import sys
import tempfile
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Sequence
from contextlib import suppress
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QDrag, QIcon, QImage, QImageReader, QKeySequence, QMovie, QPen, QPixmap, QShortcut
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRubberBand,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PIL import ExifTags, Image, ImageOps

from .app_icon import DESKTOP_FILE_ID, app_icon_bytes, virtual_folder_icon_bytes
from .catalog import (
    DUPLICATE_DELETE_EXACT,
    DUPLICATE_DELETE_VERY_SIMILAR,
    TRASH_DIR_NAME,
    Catalog,
    DuplicateMatchGroups,
    DuplicateDeletionResult,
    is_inside_trash_rel_path,
    is_trash_rel_path,
    is_image_name,
    parse_tag_entry,
)
from .config import (
    DEFAULT_THUMBNAIL_COLUMNS,
    MAX_THUMBNAIL_COLUMNS,
    MIN_THUMBNAIL_COLUMNS,
    NORMAL_DELETE,
    WIPE_ON_DELETE,
    AppConfig,
    WindowConfig,
    config_disabled,
    default_config_path,
    load_config,
    save_config,
)
from .folder_icon import render_folder_icon
from .image_ops import (
    EditOperation,
    ImageFileIdentity,
    UnsafeImageSaveError,
    apply_operation_to_image,
    apply_operations_to_file,
    clone_heal_brush_in_place,
    snapshot_image_file_identity,
)
from .indexer import ActionPriority, BackgroundIndexer, IndexTask, IndexTaskCancelled
from .models import CatalogSettings, DirectoryRecord, ImageRecord, PaneRecord, SortOrder
from .navigation import ImageNavigator
from .safe_image import open_catalog_image
from .workspace import Workspace

CATALOG_ROOT_ROLE = Qt.ItemDataRole.UserRole
DIR_REL_ROLE = Qt.ItemDataRole.UserRole + 1
VIRTUAL_KIND_ROLE = Qt.ItemDataRole.UserRole + 2
VIRTUAL_VALUE_ROLE = Qt.ItemDataRole.UserRole + 3
TIMINGS_FILE_NAME = "timings.json"
MAX_TIMING_EVENTS = 1000
TREE_BUILD_BATCH_SIZE = 400
TREE_BUILD_BUDGET_SECONDS = 0.008
THUMBNAIL_MODEL_BATCH_SIZE = 400
VIRTUAL_KIND_ROOT = "virtual-root"
VIRTUAL_KIND_TAG_ROOT = "tag-root"
VIRTUAL_KIND_TAG = "tag"
VIRTUAL_KIND_DUPLICATES = "duplicates"
VIRTUAL_KIND_VERY_SIMILAR = "very-similar"
VIRTUAL_KIND_PHYSICAL = "physical"
TreeStateKey = tuple[Path, str, str, str]


@dataclass(slots=True)
class CatalogOpenResult:
    catalog: Catalog
    init_duration_ms: float


@dataclass(slots=True)
class CatalogOpenTask:
    root: Path
    future: Future[CatalogOpenResult]
    log_event: bool
    selected_at: float | None
    started_at: float
    discard_result: bool = False


@dataclass(slots=True)
class TreeBuildTask:
    catalog: Catalog
    directories: list[str]
    total: int | None
    page_offset: int
    processed: int
    seen_directories: set[str]
    expanded_items: set[TreeStateKey]
    known_items: set[TreeStateKey]
    item_by_dir: dict[str, QTreeWidgetItem]
    index: int
    selected_item: QTreeWidgetItem | None
    started_at: float
    reason: str


@dataclass(slots=True)
class VirtualViewResult:
    root: Path
    kind: str
    value: str
    sort_order: SortOrder
    fingerprint: int
    images: list[PaneRecord]
    duration_ms: float


@dataclass(slots=True)
class VirtualViewTask:
    root: Path
    kind: str
    value: str
    sort_order: SortOrder
    fingerprint: int
    future: Future[VirtualViewResult]
    started_at: float
    selection_keys: set[tuple[str, str]]
    current_key: tuple[str, str] | None
    scroll_key: TreeStateKey | None
    cancel_event: Event


@dataclass(slots=True)
class DuplicateDeleteTask:
    root: Path
    kind: str
    task: IndexTask
    future: Future[DuplicateDeletionResult]
    started_at: float


@dataclass(slots=True)
class DeletePayloadResult:
    requested: int
    deleted: int
    affected_roots: set[Path]


@dataclass(slots=True)
class DeletePayloadTask:
    root: Path
    rel_paths: tuple[str, ...]
    task: IndexTask
    future: Future[DeletePayloadResult]
    started_at: float


@dataclass(slots=True)
class MovePayloadResult:
    requested: int
    moved: int
    affected_roots: set[Path]
    created_dir_rel: str | None = None


@dataclass(slots=True)
class MovePayloadTask:
    dest_root: Path
    dest_dir_rel: str
    affected_roots: set[Path]
    task: IndexTask
    future: Future[MovePayloadResult]
    started_at: float
    source_images: tuple[tuple[Path, str], ...] = ()
    source_directories: tuple[tuple[Path, str], ...] = ()
    completion_verb: str = "Moved"
    error_title: str = "Move"


DIALOG_STYLESHEET = """
QDialog,
QMessageBox {
    background: #f6f7f9;
    color: #202124;
}
QLabel,
QCheckBox {
    background: transparent;
    color: #202124;
}
QScrollArea,
QScrollArea QWidget,
QWidget#tagContainer {
    background: #f6f7f9;
    color: #202124;
}
QFrame#propertiesFrame {
    background: #ffffff;
    color: #202124;
    border: 1px solid #9aa0a6;
}
QFrame#propertiesFrame QLabel {
    background: transparent;
    color: #202124;
}
QComboBox,
QLineEdit,
QSpinBox,
QPlainTextEdit {
    background: #ffffff;
    color: #202124;
    border: 1px solid #9aa0a6;
    padding: 3px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #202124;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QPushButton {
    background: #ffffff;
    color: #202124;
    border: 1px solid #9aa0a6;
    padding: 5px 10px;
}
QPushButton:default {
    border: 2px solid #2563eb;
}
QPushButton:hover {
    background: #eef2ff;
}
QListWidget {
    background: #ffffff;
    color: #202124;
    border: 1px solid #9aa0a6;
}
QListWidget::item {
    color: #202124;
    padding: 8px;
}
QListWidget::item:selected {
    background: #2563eb;
    color: #ffffff;
}
"""

MESSAGE_BUTTON_STYLESHEET = """
QPushButton {
    background: #ffffff;
    color: #202124;
    border: 1px solid #9aa0a6;
    padding: 5px 10px;
}
QPushButton:hover {
    background: #eef2ff;
}
"""


class ThumbnailModel(QAbstractListModel):
    MIME_TYPE = "application/x-marnwick-images"
    CARD_PADDING = 1
    LABEL_GAP = 0

    def __init__(self) -> None:
        super().__init__()
        self.catalog: Catalog | None = None
        self.images: list[PaneRecord] = []
        self._visible_count = 0
        self.tile_size = 160
        self.device_pixel_ratio = 1.0
        self._pixmap_cache: dict[str, QPixmap] = {}

    def set_images(self, catalog: Catalog | None, images: list[PaneRecord]) -> None:
        self.beginResetModel()
        self.catalog = catalog
        self.images = images
        self._visible_count = min(len(images), THUMBNAIL_MODEL_BATCH_SIZE)
        self._pixmap_cache.clear()
        self.endResetModel()

    def set_tile_size(self, size: int) -> None:
        self.tile_size = size
        self._pixmap_cache.clear()
        self._emit_size_changed()

    def set_device_pixel_ratio(self, device_pixel_ratio: float) -> None:
        device_pixel_ratio = max(1.0, float(device_pixel_ratio))
        if abs(self.device_pixel_ratio - device_pixel_ratio) < 0.01:
            return
        self.device_pixel_ratio = device_pixel_ratio
        self._emit_size_changed()

    def _emit_size_changed(self) -> None:
        if self._visible_count <= 0:
            return
        top_left = self.index(0, 0)
        bottom_right = self.index(self._visible_count - 1, 0)
        self.dataChanged.emit(
            top_left,
            bottom_right,
            [Qt.ItemDataRole.DecorationRole, Qt.ItemDataRole.SizeHintRole],
        )

    def logical_tile_size(self) -> int:
        return max(1, ceil(self.tile_size / self.device_pixel_ratio))

    def card_size(self, font: QFont | None = None) -> QSize:
        font_metrics = QFontMetrics(font or QApplication.font())
        inset = 2 * self.CARD_PADDING
        logical_tile_size = self.logical_tile_size()
        return QSize(logical_tile_size + inset, logical_tile_size + font_metrics.height() + self.LABEL_GAP + inset)

    def refresh_thumbnail(self, rel_path: str) -> None:
        for row, record in enumerate(self.images):
            if record.rel_path != rel_path:
                if isinstance(record, DirectoryRecord) and (
                    rel_path == record.dir_rel or rel_path.startswith(f"{record.dir_rel}/")
                ):
                    self._pixmap_cache.pop(record.rel_path, None)
                    if row >= self._visible_count:
                        continue
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])
                continue
            self._pixmap_cache.pop(rel_path, None)
            if row >= self._visible_count:
                return
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])
            return

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._visible_count

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802 - Qt API
        return not parent.isValid() and self._visible_count < len(self.images)

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802 - Qt API
        if parent.isValid() or self._visible_count >= len(self.images):
            return
        start = self._visible_count
        end = min(len(self.images), start + THUMBNAIL_MODEL_BATCH_SIZE) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._visible_count = end + 1
        self.endInsertRows()

    def ensure_row_loaded(self, row: int) -> None:
        if row < self._visible_count or row < 0 or row >= len(self.images):
            return
        start = self._visible_count
        end = min(len(self.images), row + 1) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._visible_count = end + 1
        self.endInsertRows()

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or index.row() >= self._visible_count:
            return None
        record = self.images[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return record.filename
        if role == Qt.ItemDataRole.UserRole:
            return record.rel_path
        if role == Qt.ItemDataRole.ToolTipRole and isinstance(record, ImageRecord):
            return "\n".join(
                [
                    f"Path: {record.absolute_path.parent}",
                    f"Filename: {record.filename}",
                    f"Dimensions: {record.width} x {record.height}",
                    f"Size: {format_bytes(record.size_bytes)}",
                ]
            )
        if role == Qt.ItemDataRole.DecorationRole:
            if isinstance(record, DirectoryRecord):
                pixmap = self._pixmap_cache.get(record.rel_path)
                if pixmap is None:
                    pixmap = self.folder_tile_pixmap(record)
                    self._pixmap_cache[record.rel_path] = pixmap
                return pixmap
            pixmap = self._pixmap_cache.get(record.rel_path)
            if pixmap is None:
                pixmap = QPixmap()
                thumb_blob = record.thumb_blob
                if thumb_blob is None and self.catalog is not None:
                    thumb_blob = self.catalog.get_thumbnail_blob(record.rel_path)
                if thumb_blob:
                    pixmap.loadFromData(thumb_blob)
                if pixmap.isNull():
                    pixmap = self._placeholder_pixmap()
                self._pixmap_cache[record.rel_path] = pixmap
            return pixmap
        if role == Qt.ItemDataRole.SizeHintRole:
            return self.card_size()
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid():
            flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        return flags

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [self.MIME_TYPE]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        payload = []
        seen: set[int] = set()
        if self.catalog is None:
            return QMimeData()
        for index in indexes:
            if not index.isValid() or index.row() in seen:
                continue
            seen.add(index.row())
            record = self.images[index.row()]
            payload.append(
                {
                    "catalog_root": str(self.catalog.root),
                    "rel_path": record.rel_path,
                    "kind": "directory" if isinstance(record, DirectoryRecord) else "image",
                }
            )
        mime = QMimeData()
        mime.setData(self.MIME_TYPE, json.dumps(payload).encode("utf-8"))
        return mime

    def _placeholder_pixmap(self) -> QPixmap:
        pixmap = QPixmap(self.tile_size, self.tile_size)
        pixmap.fill(QColor("#d0d5dd"))
        return pixmap

    def folder_tile_pixmap(self, record: DirectoryRecord) -> QPixmap:
        preview_items = list(record.preview_items[:4])
        if not preview_items:
            preview_items = list(record.preview_blobs[:4])
        if len(preview_items) < 4 and record.allow_preview_fallback and self.catalog is not None:
            preview_items = self.catalog.folder_preview_items_under(record.dir_rel, limit=4)
        return pixmap_from_pil_image(render_folder_icon(preview_items[:4], max(1, self.tile_size)))


class ThumbnailDelegate(QStyledItemDelegate):
    PADDING = ThumbnailModel.CARD_PADDING
    LABEL_GAP = ThumbnailModel.LABEL_GAP
    MAX_SCALED_PIXMAPS = 4096

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scaled_pixmap_cache: OrderedDict[tuple[int, int, int, int], QPixmap] = OrderedDict()

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # type: ignore[no-untyped-def]
        painter.save()
        rect = self.card_rect(option, index)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if is_selected:
            painter.fillRect(option.rect, QColor("#0067ff"))

        font_metrics = QFontMetrics(option.font)
        label_height = font_metrics.height()
        content_rect = rect.adjusted(self.PADDING, self.PADDING, -self.PADDING, -self.PADDING)
        label_rect = QRect(
            content_rect.left(),
            content_rect.bottom() - label_height + 1,
            max(1, content_rect.width()),
            label_height,
        )
        image_rect = QRect(
            content_rect.left(),
            content_rect.top(),
            max(1, content_rect.width()),
            max(1, label_rect.top() - self.LABEL_GAP - content_rect.top()),
        )

        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(decoration, QPixmap):
            pixmap = decoration
        elif isinstance(decoration, QIcon):
            pixmap = decoration.pixmap(image_rect.size())
        else:
            pixmap = QPixmap()
        if not pixmap.isNull():
            scaled_pixmap, scaled_size = self._display_pixmap(pixmap, image_rect.size(), option.widget)
            target_rect = QRect(
                image_rect.left() + int((image_rect.width() - scaled_size.width()) / 2),
                image_rect.top() + int((image_rect.height() - scaled_size.height()) / 2),
                scaled_size.width(),
                scaled_size.height(),
            )
            painter.drawPixmap(target_rect.topLeft(), scaled_pixmap)

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setPen(QColor("#ffffff") if is_selected else QColor("#111827"))
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            font_metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, label_rect.width()),
        )
        painter.restore()

    def _display_pixmap(self, pixmap: QPixmap, logical_bounds: QSize, widget: QWidget | None) -> tuple[QPixmap, QSize]:
        device_pixel_ratio = widget_device_pixel_ratio(widget) if widget is not None else 1.0
        physical_bounds = physical_size_for_logical(logical_bounds, device_pixel_ratio)
        physical_size = pixmap.size()
        physical_size.scale(physical_bounds, Qt.AspectRatioMode.KeepAspectRatio)
        key = (
            int(pixmap.cacheKey()),
            physical_size.width(),
            physical_size.height(),
            int(round(device_pixel_ratio * 1000)),
        )
        scaled = self._scaled_pixmap_cache.get(key)
        if scaled is None:
            scaled = pixmap.scaled(
                physical_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(device_pixel_ratio)
            self._scaled_pixmap_cache[key] = scaled
            if len(self._scaled_pixmap_cache) > self.MAX_SCALED_PIXMAPS:
                self._scaled_pixmap_cache.popitem(last=False)
        else:
            self._scaled_pixmap_cache.move_to_end(key)
        logical_size = QSize(
            max(1, int(round(scaled.width() / device_pixel_ratio))),
            max(1, int(round(scaled.height() / device_pixel_ratio))),
        )
        return scaled, logical_size

    def card_rect(self, option, index: QModelIndex) -> QRect:  # type: ignore[no-untyped-def]
        model = index.model()
        if isinstance(model, ThumbnailModel):
            base_size = model.card_size(option.font)
        else:
            base_size = option.rect.size()
        width = min(base_size.width(), option.rect.width())
        height = min(base_size.height(), option.rect.height())
        left = option.rect.left() + max(0, int((option.rect.width() - width) / 2))
        return QRect(left, option.rect.top(), width, height)

    def sizeHint(self, option, index: QModelIndex) -> QSize:  # type: ignore[no-untyped-def]
        model = index.model()
        if isinstance(model, ThumbnailModel):
            return model.card_size(option.font)
        return super().sizeHint(option, index)


class ThumbnailView(QListView):
    SMOOTH_SCROLL_STEP = 24
    DRAG_ICON_SIZE = 72
    _single_drag_pixmap: QPixmap | None = None
    _multi_drag_pixmap: QPixmap | None = None

    def __init__(self, window: "MainWindow | None" = None) -> None:
        super().__init__()
        self.main_window = window
        self._drag_start_pos: QPoint | None = None
        self._drag_indexes: list[QModelIndex] = []
        self._drag_payload: list[dict[str, str]] = []
        self._drag_destination_item: QTreeWidgetItem | None = None
        self._drag_destination_root: Path | None = None
        self._drag_destination_dir_rel: str | None = None
        self._manual_drag_active = False
        self._drag_cursor_active = False
        self._drag_cursor_restore_count = 0
        self._manual_drag_watchdog = QTimer(self)
        self._manual_drag_watchdog.setInterval(50)
        self._manual_drag_watchdog.timeout.connect(self._poll_manual_drag)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(self.SMOOTH_SCROLL_STEP)
        self.horizontalScrollBar().setSingleStep(self.SMOOTH_SCROLL_STEP)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(256)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if self.main_window is not None and hasattr(self.main_window, "refresh_thumbnail_layout"):
            self.main_window.refresh_thumbnail_layout()

    def event(self, event: QEvent) -> bool:
        if self._manual_drag_active and event.type() in {
            QEvent.Type.Hide,
            QEvent.Type.WindowDeactivate,
        }:
            self.cleanup_manual_drag()
        return super().event(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._manual_drag_active:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self.finish_manual_drag(event.globalPosition().toPoint())
                event.accept()
                return
            self.update_manual_drag(event.globalPosition().toPoint())
            event.accept()
            return
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and self._drag_start_pos is not None
            and (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            indexes = self.selected_drag_indexes()
            if indexes:
                self.begin_manual_drag(indexes, event.globalPosition().toPoint())
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._manual_drag_active:
            self.finish_manual_drag(event.globalPosition().toPoint())
            event.accept()
            return
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._manual_drag_active and event.key() == Qt.Key.Key_Escape:
            self.cleanup_manual_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def manual_drag_active(self) -> bool:
        return self._manual_drag_active

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        selection = self.selectionModel()
        if selection is None:
            return
        indexes = [index for index in selection.selectedIndexes() if index.isValid()]
        if not indexes:
            current = self.currentIndex()
            indexes = [current] if current.isValid() else []
        if not indexes:
            return
        model = self.model()
        if model is None:
            return
        mime_data = model.mimeData(indexes)
        if not mime_data.hasFormat(ThumbnailModel.MIME_TYPE):
            return
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag_pixmap = self.drag_pixmap_for_indexes(indexes)
        hotspot = QPoint(int(drag_pixmap.width() / 2), int(drag_pixmap.height() / 2))
        drag.setPixmap(drag_pixmap)
        drag.setHotSpot(hotspot)
        drag.setDragCursor(drag_pixmap, Qt.DropAction.MoveAction)
        drag.exec(supported_actions, Qt.DropAction.MoveAction)

    def selected_drag_indexes(self) -> list[QModelIndex]:
        selection = self.selectionModel()
        indexes = selection.selectedIndexes() if selection is not None else []
        valid_indexes = []
        seen: set[int] = set()
        for index in indexes:
            if not index.isValid() or index.row() in seen:
                continue
            seen.add(index.row())
            valid_indexes.append(index)
        if valid_indexes:
            return valid_indexes
        current = self.currentIndex()
        return [current] if current.isValid() else []

    def begin_manual_drag(self, indexes: list[QModelIndex], global_pos: QPoint) -> bool:
        if self._manual_drag_active or self._drag_cursor_active:
            self.cleanup_manual_drag()
        model = self.model()
        if not isinstance(model, ThumbnailModel) or model.catalog is None:
            return False
        valid_indexes = [index for index in indexes if index.isValid()]
        if not valid_indexes:
            return False
        payload = self.drag_payload_for_indexes(valid_indexes)
        if not payload:
            return False
        self._drag_indexes = valid_indexes
        self._drag_payload = payload
        drag_pixmap = self.drag_pixmap_for_indexes(valid_indexes)
        hotspot = QPoint(int(drag_pixmap.width() / 2), int(drag_pixmap.height() / 2))
        self._manual_drag_active = True
        self._drag_cursor_active = True
        try:
            QApplication.setOverrideCursor(QCursor(drag_pixmap, hotspot.x(), hotspot.y()))
            self._drag_cursor_restore_count += 1
            self.update_manual_drag(global_pos)
            self._manual_drag_watchdog.start()
        except Exception:
            self.cleanup_manual_drag()
            raise
        return True

    def _poll_manual_drag(self) -> None:
        if not self._manual_drag_active:
            self._manual_drag_watchdog.stop()
            return
        global_pos = QCursor.pos()
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self.update_manual_drag(global_pos)
            return
        self.finish_manual_drag(global_pos)

    def update_manual_drag(self, global_pos: QPoint) -> None:
        item = self.tree_item_at_global(global_pos)
        self._drag_destination_item = item
        self._drag_destination_root = None
        self._drag_destination_dir_rel = None
        if item is not None:
            self._drag_destination_root = Path(item.data(0, CATALOG_ROOT_ROLE))
            self._drag_destination_dir_rel = item.data(0, DIR_REL_ROLE) or ""
            if self.main_window is not None:
                self.main_window.tree.set_drag_hover_item(item)
            return
        if self.main_window is not None:
            self.main_window.tree.set_drag_hover_item(None)
        record = self.folder_record_at_global(global_pos)
        if record is not None:
            self._drag_destination_root = record.catalog_root
            self._drag_destination_dir_rel = record.dir_rel

    def finish_manual_drag(self, global_pos: QPoint) -> None:
        move_request: tuple["MainWindow", list[dict[str, str]], Path, str] | None = None
        try:
            self.update_manual_drag(global_pos)
            if (
                self.main_window is not None
                and self._drag_payload
                and self._drag_destination_root is not None
                and self._drag_destination_dir_rel is not None
            ):
                payload = list(self._drag_payload)
                if payload:
                    move_request = (
                        self.main_window,
                        payload,
                        self._drag_destination_root,
                        self._drag_destination_dir_rel,
                    )
        finally:
            self.cleanup_manual_drag()
        if move_request is not None:
            window, payload, root, dir_rel = move_request
            QTimer.singleShot(
                0,
                lambda: (
                    None
                    if window._closing
                    else window.move_payload_to_directory(payload, root, dir_rel)
                ),
            )

    def cleanup_manual_drag(self) -> None:
        if self.main_window is not None:
            self.main_window.tree.set_drag_hover_item(None)
        self._manual_drag_watchdog.stop()
        while self._drag_cursor_restore_count > 0 and QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
            self._drag_cursor_restore_count -= 1
        self._drag_cursor_restore_count = 0
        self._drag_start_pos = None
        self._drag_indexes = []
        self._drag_payload = []
        self._drag_destination_item = None
        self._drag_destination_root = None
        self._drag_destination_dir_rel = None
        self._manual_drag_active = False
        self._drag_cursor_active = False

    def tree_item_at_global(self, global_pos: QPoint) -> QTreeWidgetItem | None:
        if self.main_window is None:
            return None
        tree = self.main_window.tree
        viewport_pos = tree.viewport().mapFromGlobal(global_pos)
        if not tree.viewport().rect().contains(viewport_pos):
            return None
        item = tree.itemAt(viewport_pos)
        if item is not None and self.main_window.is_virtual_tree_item(item):
            return None
        return item

    def folder_record_at_global(self, global_pos: QPoint) -> DirectoryRecord | None:
        viewport_pos = self.viewport().mapFromGlobal(global_pos)
        if not self.viewport().rect().contains(viewport_pos):
            return None
        index = self.indexAt(viewport_pos)
        if not index.isValid():
            return None
        model = self.model()
        if not isinstance(model, ThumbnailModel) or index.row() >= len(model.images):
            return None
        record = model.images[index.row()]
        if isinstance(record, DirectoryRecord):
            return record
        return None

    def drag_pixmap_for_indexes(self, indexes: list[QModelIndex]) -> QPixmap:
        return self.static_drag_pixmap(multiple=len(indexes) > 1)

    def drag_payload_for_indexes(self, indexes: list[QModelIndex]) -> list[dict[str, str]]:
        model = self.model()
        if model is None:
            return []
        mime_data = model.mimeData(indexes)
        if not mime_data.hasFormat(ThumbnailModel.MIME_TYPE):
            return []
        data = bytes(mime_data.data(ThumbnailModel.MIME_TYPE)).decode("utf-8")
        payload = json.loads(data)
        return payload if isinstance(payload, list) else []

    @classmethod
    def static_drag_pixmap(cls, *, multiple: bool) -> QPixmap:
        if multiple:
            if cls._multi_drag_pixmap is None:
                cls._multi_drag_pixmap = cls._build_static_drag_pixmap(multiple=True)
            return cls._multi_drag_pixmap
        if cls._single_drag_pixmap is None:
            cls._single_drag_pixmap = cls._build_static_drag_pixmap(multiple=False)
        return cls._single_drag_pixmap

    @classmethod
    def _build_static_drag_pixmap(cls, *, multiple: bool) -> QPixmap:
        size = cls.DRAG_ICON_SIZE
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if multiple:
            cls._draw_photo_icon(painter, QRect(16, 6, 38, 30), "#dbeafe", "#1d4ed8")
            cls._draw_photo_icon(painter, QRect(20, 15, 38, 30), "#bfdbfe", "#1d4ed8")
            cls._draw_photo_icon(painter, QRect(24, 24, 38, 30), "#ffffff", "#1d4ed8")
        else:
            cls._draw_photo_icon(painter, QRect(12, 14, 48, 38), "#ffffff", "#1d4ed8")
        painter.end()
        return pixmap

    @staticmethod
    def _draw_photo_icon(painter: QPainter, rect: QRect, fill: str, stroke: str) -> None:
        painter.setBrush(QBrush(QColor(fill)))
        painter.setPen(QPen(QColor(stroke), 3))
        painter.drawRect(rect)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#facc15")))
        sun_size = max(5, int(rect.width() * 0.16))
        painter.drawEllipse(rect.left() + 7, rect.top() + 6, sun_size, sun_size)

        painter.setBrush(QBrush(QColor("#16a34a")))
        bottom = rect.bottom() - 4
        painter.drawPolygon(
            [
                QPoint(rect.left() + 5, bottom),
                QPoint(rect.left() + int(rect.width() * 0.38), rect.top() + int(rect.height() * 0.52)),
                QPoint(rect.left() + int(rect.width() * 0.68), bottom),
            ]
        )
        painter.setBrush(QBrush(QColor("#15803d")))
        painter.drawPolygon(
            [
                QPoint(rect.left() + int(rect.width() * 0.32), bottom),
                QPoint(rect.left() + int(rect.width() * 0.68), rect.top() + int(rect.height() * 0.42)),
                QPoint(rect.right() - 5, bottom),
            ]
        )


class DirectoryTree(QTreeWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self._drag_hover_item: QTreeWidgetItem | None = None
        self._drag_hover_background = QBrush()
        self._drag_hover_foreground = QBrush()
        self._drag_hover_font = QFont()
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_context_menu)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None or item.parent() is None or self.window.is_virtual_tree_item(item):
            return
        payload = [
            {
                "catalog_root": str(Path(item.data(0, CATALOG_ROOT_ROLE)).resolve()),
                "rel_path": item.data(0, DIR_REL_ROLE),
                "kind": "directory",
            }
        ]
        mime = QMimeData()
        mime.setData(ThumbnailModel.MIME_TYPE, json.dumps(payload).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon).pixmap(QSize(96, 96))
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(48, 48))
        self.window._directory_drag_active = True
        try:
            drag.exec(supported_actions, Qt.DropAction.MoveAction)
        finally:
            self.set_drag_hover_item(None)
            self.window._directory_drag_active = False
            if self.window._tree_rebuild_deferred:
                self.window._tree_rebuild_deferred = False
                self.window.rebuild_tree()

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasFormat(ThumbnailModel.MIME_TYPE):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasFormat(ThumbnailModel.MIME_TYPE):
            item = self.itemAt(event.position().toPoint())
            if item is not None and self.window.is_virtual_tree_item(item):
                item = None
            self.set_drag_hover_item(item)
            if item is None:
                event.ignore()
            else:
                event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.set_drag_hover_item(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        item = self.itemAt(event.position().toPoint())
        self.set_drag_hover_item(None)
        if item is None or not event.mimeData().hasFormat(ThumbnailModel.MIME_TYPE):
            super().dropEvent(event)
            return
        if self.window.is_virtual_tree_item(item):
            event.ignore()
            return
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        dir_rel = item.data(0, DIR_REL_ROLE)
        try:
            data = bytes(event.mimeData().data(ThumbnailModel.MIME_TYPE)).decode("utf-8")
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            event.ignore()
            return
        self.window.move_payload_to_directory(payload, root, dir_rel)
        event.acceptProposedAction()

    def set_drag_hover_item(self, item: QTreeWidgetItem | None) -> None:
        if self._drag_hover_item is item:
            return
        if self._drag_hover_item is not None:
            self._drag_hover_item.setBackground(0, self._drag_hover_background)
            self._drag_hover_item.setForeground(0, self._drag_hover_foreground)
            self._drag_hover_item.setFont(0, self._drag_hover_font)
        self._drag_hover_item = item
        if item is not None:
            self._drag_hover_background = item.background(0)
            self._drag_hover_foreground = item.foreground(0)
            self._drag_hover_font = item.font(0)
            hover_font = QFont(self._drag_hover_font)
            hover_font.setBold(True)
            item.setBackground(0, QBrush(QColor("#1d4ed8")))
            item.setForeground(0, QBrush(QColor("#ffffff")))
            item.setFont(0, hover_font)

    def _open_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        item = self.itemAt(pos)
        if item is None:
            return
        if self.window.is_virtual_tree_item(item):
            return
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        dir_rel = item.data(0, DIR_REL_ROLE)
        menu = QMenu(self)
        restore_action = None
        if self.window.is_restorable_trash_rel(dir_rel):
            restore_action = menu.addAction("Restore")
            menu.addSeparator()
        properties_action = menu.addAction("Properties")
        create_action = menu.addAction("Create Directory")
        delete_action = menu.addAction("Delete Directory") if dir_rel else None
        preferences_action = tags_action = close_action = None
        if item.parent() is None:
            menu.addSeparator()
            preferences_action = menu.addAction("Preferences")
            tags_action = menu.addAction("Tags")
            close_action = menu.addAction("Close")
        selected = menu.exec(self.viewport().mapToGlobal(pos))
        if restore_action is not None and selected == restore_action:
            self.window.restore_trash_directory(root, dir_rel)
        elif selected == properties_action:
            self.window.open_directory_properties(root, dir_rel)
        elif selected == create_action:
            self.window.create_directory(root, dir_rel)
        elif delete_action is not None and selected == delete_action:
            self.window.delete_directory(root, dir_rel)
        elif preferences_action is not None and selected == preferences_action:
            self.window.open_catalog_preferences(root)
        elif tags_action is not None and selected == tags_action:
            self.window.open_catalog_tags(root)
        elif close_action is not None and selected == close_action:
            self.window.close_catalog(root)


class MainWindow(QMainWindow):
    def __init__(self, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Marnwick")
        self.setWindowIcon(load_app_icon())
        self.config_path = config_path or default_config_path()
        self.config_enabled = config_path is not None or not config_disabled()
        self.app_config = load_config(self.config_path) if self.config_enabled else AppConfig()
        self.workspace = Workspace()
        self.indexer = BackgroundIndexer()
        self.catalog_open_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marnwick-open")
        self.virtual_view_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marnwick-virtual")
        # Kept for compatibility with older direct callers; UI file work is
        # routed through BackgroundIndexer.submit_action below.
        self.duplicate_delete_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marnwick-delete")
        self.file_move_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marnwick-move")
        self.current_catalog: Catalog | None = None
        self.current_dir_rel = ""
        self.current_virtual_kind: str | None = None
        self.current_virtual_value = ""
        self.current_sort = self.sort_order_from_config(self.app_config.sort_order)
        self.thumbnail_columns = self.thumbnail_columns_from_config(self.app_config.thumbnail_size)
        self._swept_catalog_roots: set[Path] = set()
        self._pruned_catalog_roots: set[Path] = set()
        self._idle_index_tasks: dict[Path, IndexTask] = {}
        self._directory_discovery_tasks: dict[Path, IndexTask] = {}
        self._directory_discovery_retries: dict[Path, int] = {}
        self._directory_discovery_retry_at: dict[Path, float] = {}
        self._directory_index_tasks: dict[tuple[Path, str], IndexTask] = {}
        self._thumbnail_prune_tasks: dict[Path, IndexTask] = {}
        self._resume_idle_refresh_roots: set[Path] = set()
        self._shallow_tree_roots: set[Path] = set()
        self._catalog_open_tasks: dict[Future[CatalogOpenResult], CatalogOpenTask] = {}
        self._virtual_view_tasks: dict[Future[VirtualViewResult], VirtualViewTask] = {}
        self._very_similar_cache: dict[tuple[Path, str, int], list[ImageRecord]] = {}
        self._duplicate_delete_task: DuplicateDeleteTask | None = None
        self._delete_payload_tasks: list[DeletePayloadTask] = []
        self._delete_payload_task: DeletePayloadTask | None = None
        self._move_payload_tasks: list[MovePayloadTask] = []
        self._move_payload_task: MovePayloadTask | None = None
        self._tree_build_task: TreeBuildTask | None = None
        self._pending_tree_rebuilds: dict[Path, tuple[Catalog, str]] = {}
        self._thumbnail_scroll_positions: dict[TreeStateKey, tuple[int, int]] = {}
        self._thumbnail_selections: dict[
            TreeStateKey,
            tuple[set[tuple[str, str]], tuple[str, str] | None],
        ] = {}
        self._thumbnail_scroll_key: TreeStateKey | None = None
        self._thumbnail_scroll_restore_generation = 0
        self._indexing_was_active = False
        self._catalog_intent_root: Path | None = None
        self._closing = False
        self._unavailable_catalog_paths: list[str] = []
        self._directory_drag_active = False
        self._tree_rebuild_deferred = False

        self.tree = DirectoryTree(self)
        self.tree.itemClicked.connect(self._directory_clicked)

        self.model = ThumbnailModel()
        self.thumbnail_view = ThumbnailView(self)
        self.thumbnail_view.setModel(self.model)
        self.thumbnail_view.setItemDelegate(ThumbnailDelegate(self.thumbnail_view))
        self.thumbnail_view.setViewMode(QListView.ViewMode.IconMode)
        self.thumbnail_view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.thumbnail_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.thumbnail_view.setMovement(QListView.Movement.Static)
        self.thumbnail_view.setUniformItemSizes(True)
        self.thumbnail_view.setDragEnabled(False)
        self.thumbnail_view.setDragDropMode(QListView.DragDropMode.DragOnly)
        self.thumbnail_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.thumbnail_view.setSpacing(0)
        self.thumbnail_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.thumbnail_view.customContextMenuRequested.connect(self._open_thumbnail_context_menu)
        self.thumbnail_view.doubleClicked.connect(lambda index: self.open_viewer(index, random_mode=False))
        self.thumbnail_view.installEventFilter(self)
        selection_model = self.thumbnail_view.selectionModel()
        selection_model.currentChanged.connect(lambda *_: self.update_selection_status())
        selection_model.selectionChanged.connect(lambda *_: self.update_selection_status())

        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(MIN_THUMBNAIL_COLUMNS, MAX_THUMBNAIL_COLUMNS)
        self.size_slider.setValue(self.thumbnail_columns)
        self.size_slider.setToolTip("Thumbnails per row")
        self.size_slider.valueChanged.connect(self._thumbnail_size_changed)

        self.sort_combo = QComboBox()
        for sort_order in SortOrder:
            self.sort_combo.addItem(sort_order.label, sort_order.value)
        sort_index = self.sort_combo.findData(self.current_sort.value)
        if sort_index >= 0:
            self.sort_combo.setCurrentIndex(sort_index)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)
        controls = QHBoxLayout()
        self.native_thumbnail_button = QPushButton("Default columns")
        self.native_thumbnail_button.setToolTip("Reset to the default number of thumbnails per row")
        self.native_thumbnail_button.clicked.connect(self.set_thumbnail_size_to_native)
        controls.addWidget(self.native_thumbnail_button)
        controls.addWidget(self.size_slider, 1)
        controls.addWidget(QLabel("Columns"))
        controls.addWidget(QLabel("Sort"))
        controls.addWidget(self.sort_combo)
        right_layout.addLayout(controls)
        right_layout.addWidget(self.thumbnail_view, 1)

        splitter = QSplitter()
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.virtual_folder_icon = load_virtual_folder_icon()

        self.status_left_label = QLabel("-")
        self.status_left_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.progress_label = QLabel("Ready")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.statusBar().addWidget(self.status_left_label, 1)
        self.statusBar().addWidget(self.progress_label, 2)
        self.statusBar().addWidget(self.progress_bar, 1)

        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(200)
        self.progress_timer.timeout.connect(self._poll_indexer)
        self.progress_timer.start()

        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(1000)
        self.idle_timer.timeout.connect(self._schedule_idle_indexing)
        self.idle_timer.start()

        self._build_menus()
        self.restore_window_config()
        self.restore_catalogs_from_config()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._closing = True
        self.idle_timer.stop()
        self.progress_timer.stop()
        try:
            self.save_window_config()
        except OSError as error:
            self._closing = False
            self.progress_timer.start()
            self.idle_timer.start()
            show_error(self, "Save Configuration", str(error))
            event.ignore()
            return
        self._wait_for_pending_file_tasks_on_exit()
        self._cancel_active_duplicate_delete_task(wait=True)
        self._cancel_active_delete_payload_task(wait=True)
        self._cancel_active_move_payload_task(wait=True)
        self._shutdown_catalog_open_tasks()
        self._cancel_all_virtual_view_tasks()
        self.virtual_view_executor.shutdown(wait=True, cancel_futures=True)
        self._virtual_view_tasks.clear()
        self.duplicate_delete_executor.shutdown(wait=True, cancel_futures=True)
        self.file_move_executor.shutdown(wait=True, cancel_futures=True)
        self.indexer.shutdown()
        self.workspace.close()
        super().closeEvent(event)

    def _shutdown_catalog_open_tasks(self) -> None:
        for future, task in self._catalog_open_tasks.items():
            task.discard_result = True
            future.cancel()
        self._drain_discarded_catalog_open_tasks()
        if self._catalog_open_tasks:
            dialog = QDialog(self)
            dialog.setWindowTitle("Finishing Catalog Open")
            dialog.setWindowIcon(load_app_icon())
            dialog.setStyleSheet(DIALOG_STYLESHEET)
            dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Finishing catalog initialization safely..."))
            progress = QProgressBar()
            progress.setRange(0, 0)
            layout.addWidget(progress)

            def update() -> None:
                self._drain_discarded_catalog_open_tasks()
                if not self._catalog_open_tasks:
                    dialog.accept()

            timer = QTimer(dialog)
            timer.setInterval(50)
            timer.timeout.connect(update)
            timer.start()
            dialog.exec()
            timer.stop()
        self.catalog_open_executor.shutdown(wait=True, cancel_futures=True)

    def _drain_discarded_catalog_open_tasks(self) -> None:
        for future, task in list(self._catalog_open_tasks.items()):
            if not future.done():
                continue
            self._catalog_open_tasks.pop(future, None)
            if future.cancelled():
                continue
            result: CatalogOpenResult | None = None
            try:
                result = future.result()
            except Exception as error:
                self._append_timing_event(
                    task.root,
                    "discarded_catalog_init_failed",
                    None,
                    {"error": str(error)},
                )
            if result is not None:
                result.catalog.close()

    def _cancel_all_virtual_view_tasks(self) -> None:
        for future, task in list(self._virtual_view_tasks.items()):
            task.cancel_event.set()
            future.cancel()

    def _wait_for_pending_file_tasks_on_exit(self) -> None:
        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        if not self._has_pending_file_tasks():
            return
        for catalog in self.workspace.catalogs:
            self.indexer.cancel_idle_tasks(catalog.root)
            self.indexer.cancel_directory_tasks(catalog.root)
        dialog = QDialog(self)
        dialog.setWindowTitle("Finishing File Changes")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        label = QLabel("Finishing pending file changes...")
        label.setMinimumWidth(480)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(label)
        layout.addWidget(progress)
        dialog.setModal(True)

        def update() -> None:
            self._settle_duplicate_delete_task()
            self._settle_delete_payload_task()
            self._settle_move_payload_task()
            active_move = self._active_move_payload_task()
            active_delete_payload = self._active_delete_payload_task()
            active_duplicate = self._active_duplicate_delete_task()
            active_task = active_move.task if active_move is not None else None
            if active_task is None and active_delete_payload is not None:
                active_task = active_delete_payload.task
            if active_task is None and active_duplicate is not None:
                active_task = active_duplicate.task
            if active_task is None:
                dialog.accept()
                return
            snapshot = active_task.snapshot()
            if snapshot.total is None:
                progress.setRange(0, 0)
                detail = snapshot.current or snapshot.label
            else:
                total = max(snapshot.total, 1)
                progress.setRange(0, total)
                progress.setValue(min(snapshot.processed, total))
                detail = f"{snapshot.processed}/{snapshot.total}"
                if snapshot.current:
                    detail = f"{detail}: {snapshot.current}"
            label.setText(f"{snapshot.label}: {detail}")

        timer = QTimer(dialog)
        timer.setInterval(100)
        timer.timeout.connect(update)
        timer.start()
        update()
        if self._has_pending_file_tasks():
            dialog.exec()
        timer.stop()

    def _has_pending_file_tasks(self) -> bool:
        return (
            self._has_pending_move_payload_tasks()
            or self._has_pending_delete_payload_tasks()
            or self._has_active_duplicate_delete_task()
        )

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self.refresh_thumbnail_layout()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self.refresh_thumbnail_layout()

    def restore_window_config(self) -> None:
        window = self.app_config.window
        width = window.width
        height = window.height
        screens = [screen.availableGeometry() for screen in QApplication.screens()]
        if screens:
            primary = QApplication.primaryScreen()
            available = primary.availableGeometry() if primary is not None else screens[0]
            width = min(width, available.width())
            height = min(height, available.height())
        self.resize(width, height)
        if window.x is not None and window.y is not None:
            target = QRect(window.x, window.y, width, height)
            visible = any(
                geometry.intersected(target).width() >= 64
                and geometry.intersected(target).height() >= 64
                for geometry in screens
            )
            if visible or not screens:
                self.move(window.x, window.y)
            else:
                self.move(
                    available.x() + max(0, (available.width() - width) // 2),
                    available.y() + max(0, (available.height() - height) // 2),
                )
        if window.maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def restore_catalogs_from_config(self) -> None:
        if not self.config_enabled:
            return
        for catalog_path in self.app_config.catalogs:
            path = Path(catalog_path).expanduser()
            if path.is_dir():
                try:
                    self.open_catalog(path, log_event=False)
                except Exception as error:
                    # A broken or temporarily locked catalog must not prevent
                    # the rest of the workspace from opening. Keep the path
                    # so a later run can try it again.
                    self._unavailable_catalog_paths.append(catalog_path)
                    self._append_timing_event(path, "catalog_restore_failed", None, {"error": str(error)})
                    self.progress_label.setText(f"Could not restore catalog {path.name or path}")
            else:
                self._unavailable_catalog_paths.append(catalog_path)

    def save_window_config(self) -> None:
        if not self.config_enabled:
            return
        snapshot = self.current_app_config()
        save_config(snapshot, self.config_path)
        self.app_config.catalogs = list(snapshot.catalogs)
        self.app_config._loaded_catalogs = snapshot._loaded_catalogs

    def current_app_config(self) -> AppConfig:
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        return AppConfig(
            window=WindowConfig(
                x=geometry.x(),
                y=geometry.y(),
                width=geometry.width(),
                height=geometry.height(),
                maximized=self.isMaximized(),
            ),
            catalogs=list(
                dict.fromkeys(
                    [str(catalog.root) for catalog in self.workspace.catalogs]
                    + self._unavailable_catalog_paths
                )
            ),
            thumbnail_size=self.thumbnail_columns,
            delete_behavior=self.app_config.delete_behavior,
            sort_order=self.current_sort.value,
            _loaded_catalogs=self.app_config._loaded_catalogs,
        )

    def apply_app_config(self, config: AppConfig) -> None:
        self.app_config = config
        self.set_thumbnail_size(self.thumbnail_columns_from_config(config.thumbnail_size))
        self.set_sort_order(self.sort_order_from_config(config.sort_order))
        self.sync_catalogs_to_config(config.catalogs)
        if config.window.maximized:
            self.showMaximized()
        else:
            if self.isMaximized():
                self.showNormal()
            self.restore_window_config()
        if self.config_enabled:
            self.save_window_config()

    def sync_catalogs_to_config(self, catalog_paths: list[str]) -> None:
        requested: list[Path] = []
        unavailable: list[str] = []
        seen: set[Path] = set()
        for catalog_path in catalog_paths:
            path = Path(catalog_path).expanduser()
            if not path.is_dir():
                unavailable.append(catalog_path)
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                requested.append(resolved)
        self._unavailable_catalog_paths = list(dict.fromkeys(unavailable))
        for catalog in list(self.workspace.catalogs):
            if catalog.root not in seen:
                self.close_catalog(catalog.root)
        for path in requested:
            if self.workspace.catalog_for_root(path) is None:
                self.open_catalog(path)

    def wipe_on_delete_enabled(self) -> bool:
        return self.app_config.delete_behavior == WIPE_ON_DELETE

    def sort_order_from_config(self, value: str) -> SortOrder:
        try:
            return SortOrder(value)
        except (TypeError, ValueError):
            return SortOrder.NAME_ASC

    def thumbnail_columns_from_config(self, value: int) -> int:
        if isinstance(value, bool):
            return DEFAULT_THUMBNAIL_COLUMNS
        try:
            integer = int(value)
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_THUMBNAIL_COLUMNS
        if MIN_THUMBNAIL_COLUMNS <= integer <= MAX_THUMBNAIL_COLUMNS:
            return integer
        if integer >= 64:
            return max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, round(960 / integer)))
        return DEFAULT_THUMBNAIL_COLUMNS

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched == self.thumbnail_view and event.type() == QEvent.Type.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            modifiers = event.modifiers()  # type: ignore[attr-defined]
            if key == Qt.Key.Key_C and modifiers & Qt.KeyboardModifier.ControlModifier:
                self.copy_selected_files()
                return True
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.open_viewer(self.thumbnail_keyboard_activation_index(), random_mode=False)
                return True
            if key == Qt.Key.Key_S:
                self.open_viewer(self.thumbnail_keyboard_activation_index(), random_mode=True)
                return True
            if key == Qt.Key.Key_T:
                self.open_tag_dialog_for_selection()
                return True
        return super().eventFilter(watched, event)

    def _build_menus(self) -> None:
        self.file_menu = QMenu("File", self)
        self.menuBar().addMenu(self.file_menu)
        open_action = QAction("Open", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_catalog_dialog)
        self.file_menu.addAction(open_action)

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.file_menu.addAction(quit_action)

        self.tools_menu = QMenu("Tools", self)
        self.menuBar().addMenu(self.tools_menu)
        self.refresh_catalog_action = QAction("Refresh Catalog", self)
        self.refresh_catalog_action.triggered.connect(self.refresh_current_catalog)
        self.tools_menu.addAction(self.refresh_catalog_action)
        self.auto_delete_duplicates_action = QAction("Automatically Delete Duplicates", self)
        self.auto_delete_duplicates_action.triggered.connect(self.automatically_delete_duplicates)
        self.auto_delete_duplicates_action.setVisible(False)
        self.tools_menu.addAction(self.auto_delete_duplicates_action)
        self.logs_action = QAction("Logs", self)
        self.logs_action.triggered.connect(self.open_logs)
        self.tools_menu.addAction(self.logs_action)
        self.prune_thumbnails_action = QAction("Prune Thumbnails", self)
        self.prune_thumbnails_action.triggered.connect(self.prune_current_catalog_thumbnails)
        self.tools_menu.addAction(self.prune_thumbnails_action)
        self.preferences_action = QAction("Preferences", self)
        self.preferences_action.triggered.connect(self.open_app_preferences)
        self.tools_menu.addAction(self.preferences_action)
        self.tools_menu.aboutToShow.connect(self._update_tools_menu_actions)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_catalog_dialog)

    def _update_tools_menu_actions(self) -> None:
        duplicate_view_selected = self.current_virtual_kind in {
            VIRTUAL_KIND_DUPLICATES,
            VIRTUAL_KIND_VERY_SIMILAR,
        }
        self.auto_delete_duplicates_action.setVisible(duplicate_view_selected)
        self.auto_delete_duplicates_action.setEnabled(
            duplicate_view_selected
            and self.current_catalog is not None
            and self._duplicate_delete_task is None
            and not self._has_pending_delete_payload_tasks()
            and not self._has_pending_move_payload_tasks()
            and self._active_virtual_view_task() is None
        )

    def open_catalog_dialog(self) -> None:
        dialog_started_at = monotonic()
        directory = QFileDialog.getExistingDirectory(self, "Open catalog")
        if directory:
            selected_at = monotonic()
            self.defer_open_catalog(
                Path(directory),
                log_event=True,
                selected_at=selected_at,
                dialog_duration_ms=(selected_at - dialog_started_at) * 1000,
            )

    def refresh_current_catalog(self) -> None:
        if self.current_catalog is None:
            QMessageBox.information(self, "Refresh Catalog", "Open or select a catalog first.")
            return
        catalog = self.current_catalog
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root)
        self._swept_catalog_roots.discard(catalog.root)
        self._idle_index_tasks[catalog.root] = self.indexer.refresh_catalog(
            catalog.root,
            interactive=True,
            force=True,
        )
        self._poll_indexer()

    def open_app_preferences(self) -> None:
        dialog = AppPreferencesDialog(self.current_app_config(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.apply_app_config(dialog.selected_config())

    def open_logs(self) -> None:
        LogsDialog(self.workspace.catalogs, self).exec()

    def prune_current_catalog_thumbnails(self) -> None:
        if self.current_catalog is None:
            QMessageBox.information(self, "Prune Thumbnails", "Open or select a catalog first.")
            return
        catalog = self.current_catalog
        self._pruned_catalog_roots.discard(catalog.root)
        self._thumbnail_prune_tasks[catalog.root] = self.indexer.prune_thumbnails(
            catalog.root,
            interactive=True,
            force=True,
        )
        self._poll_indexer()

    def automatically_delete_duplicates(self) -> None:
        if self.current_catalog is None:
            return
        if self.current_virtual_kind not in {VIRTUAL_KIND_DUPLICATES, VIRTUAL_KIND_VERY_SIMILAR}:
            return
        catalog = self.current_catalog
        virtual_kind = self.current_virtual_kind
        mode = (
            DUPLICATE_DELETE_EXACT
            if virtual_kind == VIRTUAL_KIND_DUPLICATES
            else DUPLICATE_DELETE_VERY_SIMILAR
        )
        if self._duplicate_delete_task is not None:
            self._show_duplicate_delete_status(self._duplicate_delete_task.task.snapshot())
            return
        if self._has_pending_delete_payload_tasks():
            self.progress_label.setText("Wait for pending deletes to finish")
            return
        if self._active_virtual_view_task() is not None:
            self.progress_label.setText("Wait for the virtual directory to finish building")
            return
        if not ask_automatically_delete_duplicates(self):
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root)
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        task, future = self.indexer.submit_action(
            "Moving duplicates to trash",
            catalog.root,
            None,
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda action_task: self._duplicate_delete_worker(
                catalog.root,
                mode,
                action_task,
            ),
            key=f"duplicate-delete:{catalog.root}:{mode}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
        )
        self._duplicate_delete_task = DuplicateDeleteTask(
            root=catalog.root,
            kind=virtual_kind,
            task=task,
            future=future,
            started_at=monotonic(),
        )
        self._show_duplicate_delete_status(task.snapshot())
        self._update_tools_menu_actions()

    def _duplicate_delete_worker(
        self,
        root: Path,
        mode: str,
        task: IndexTask,
    ) -> DuplicateDeletionResult:
        try:
            with Catalog(root) as catalog:
                result = catalog.move_duplicate_images_to_trash(
                    mode,
                    progress_callback=task.update,
                    cancel_check=task.check_canceled,
                )
            task.mark_done()
            return result
        except IndexTaskCancelled:
            task.mark_canceled()
            raise
        except Exception as error:
            task.mark_failed(error)
            raise

    def open_catalog_preferences(self, root: Path) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        dialog = PreferencesDialog([catalog], catalog, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_catalog, settings = dialog.selected_settings()
        self.apply_catalog_settings(selected_catalog, settings)

    def apply_catalog_settings(self, catalog: Catalog, settings: CatalogSettings) -> None:
        old_size = catalog.settings.thumbnail_native_size
        catalog.set_settings(settings)
        if old_size == settings.thumbnail_native_size:
            return
        self.indexer.cancel_idle_tasks(catalog.root)
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        self._idle_index_tasks.pop(catalog.root, None)
        if self.current_catalog and self.current_catalog.root == catalog.root:
            self.queue_directory_index(catalog, self.current_dir_rel, force=True)
        self._schedule_idle_indexing()

    def open_catalog_tags(self, root: Path) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        dialog = CatalogTagsDialog(catalog, self)
        dialog.exec()
        self.rebuild_tree()

    def open_directory_properties(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        DirectoryPropertiesDialog(catalog, dir_rel, self).exec()

    def create_directory(self, root: Path, parent_dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        parent_path = catalog.abs_path(parent_dir_rel) if parent_dir_rel else catalog.root
        dialog = DirectoryNameDialog(parent_path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.directory_name()
        self._queue_catalog_mutation(
            catalog,
            label="Creating directory",
            dest_dir_rel=parent_dir_rel,
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._create_directory_worker(
                catalog.root,
                parent_dir_rel,
                name,
                task,
            ),
            completion_verb="Created",
            error_title="Create Directory",
        )

    def delete_directory(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None or not dir_rel:
            return
        directory = catalog.abs_path(dir_rel)
        if not ask_delete_directory(self, directory):
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        try:
            expected_identity = catalog.directory_identity(dir_rel)
        except (OSError, ValueError) as error:
            show_error(self, "Delete Directory", str(error))
            return
        parent_rel = Path(dir_rel).parent.as_posix()
        self._queue_catalog_mutation(
            catalog,
            label="Deleting directory",
            dest_dir_rel="" if parent_rel == "." else parent_rel,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda task: self._delete_directory_worker(
                catalog.root,
                dir_rel,
                self.wipe_on_delete_enabled(),
                expected_identity,
                task,
            ),
            source_directories=((catalog.root, dir_rel),),
            completion_verb="Deleted",
            error_title="Delete Directory",
        )

    def is_restorable_trash_rel(self, rel_path: str) -> bool:
        return is_inside_trash_rel_path(rel_path)

    def is_restorable_trash_record(self, record: PaneRecord) -> bool:
        if isinstance(record, DirectoryRecord):
            return self.is_restorable_trash_rel(record.dir_rel)
        return self.is_restorable_trash_rel(record.rel_path)

    def restore_trash_directory(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        self._queue_restore_records(catalog, (("directory", dir_rel),))

    def restore_selected_trash_records(
        self,
        *,
        catalog: Catalog | None = None,
        records: Sequence[PaneRecord] | None = None,
    ) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None:
            return
        selected_records = [
            record
            for record in (list(records) if records is not None else self.selected_records())
            if self.is_restorable_trash_record(record)
        ]
        if not selected_records:
            return
        restore_items = tuple(
            ("directory", record.dir_rel)
            if isinstance(record, DirectoryRecord)
            else ("image", record.rel_path)
            for record in sorted(selected_records, key=lambda item: item.rel_path.count("/"), reverse=True)
        )
        self._queue_restore_records(catalog, restore_items)

    def _queue_restore_records(
        self,
        catalog: Catalog,
        restore_items: tuple[tuple[str, str], ...],
    ) -> None:
        if not restore_items or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        source_images = tuple(
            (catalog.root, rel_path) for kind, rel_path in restore_items if kind == "image"
        )
        source_directories = tuple(
            (catalog.root, rel_path) for kind, rel_path in restore_items if kind == "directory"
        )
        self._queue_catalog_mutation(
            catalog,
            label="Restoring from trash",
            dest_dir_rel=TRASH_DIR_NAME,
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._restore_records_worker(
                catalog.root,
                restore_items,
                task,
            ),
            source_images=source_images,
            source_directories=source_directories,
            completion_verb="Restored",
            error_title="Restore",
        )

    def _queue_catalog_mutation(
        self,
        catalog: Catalog,
        *,
        label: str,
        dest_dir_rel: str,
        priority: ActionPriority,
        worker: Callable[[IndexTask], MovePayloadResult],
        source_images: tuple[tuple[Path, str], ...] = (),
        source_directories: tuple[tuple[Path, str], ...] = (),
        completion_verb: str,
        error_title: str,
    ) -> MovePayloadTask:
        root = catalog.root
        self.indexer.cancel_idle_tasks(root)
        self.indexer.cancel_directory_tasks(root)
        task, future = self.indexer.submit_action(
            label,
            root,
            dest_dir_rel,
            priority=priority,
            worker=worker,
            key=f"catalog-mutation:{root}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
        )
        mutation = MovePayloadTask(
            dest_root=root,
            dest_dir_rel=dest_dir_rel,
            affected_roots={root},
            task=task,
            future=future,
            started_at=monotonic(),
            source_images=source_images,
            source_directories=source_directories,
            completion_verb=completion_verb,
            error_title=error_title,
        )
        self._move_payload_tasks.append(mutation)
        self._refresh_active_move_payload_task()
        if self.current_catalog is catalog and (source_images or source_directories):
            for _, source_dir_rel in source_directories:
                if self.current_dir_rel == source_dir_rel or self.current_dir_rel.startswith(f"{source_dir_rel}/"):
                    parent_rel = Path(source_dir_rel).parent.as_posix()
                    self.current_dir_rel = "" if parent_rel == "." else parent_rel
                    self.current_virtual_kind = None
                    self.current_virtual_value = ""
                    break
            self.load_current_directory(preserve_selection=True)
        self._show_move_payload_status(task.snapshot())
        self._update_tools_menu_actions()
        return mutation

    def _create_directory_worker(
        self,
        root: Path,
        parent_dir_rel: str,
        name: str,
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, parent_dir_rel or ".")
        with Catalog(root) as catalog:
            created_dir_rel = catalog.create_directory(parent_dir_rel, name)
        task.update(1, 1, created_dir_rel)
        task.mark_done()
        return MovePayloadResult(1, 1, {root}, created_dir_rel=created_dir_rel)

    def queue_image_edit(
        self,
        catalog: Catalog,
        rel_path: str,
        operations: Sequence[EditOperation],
        *,
        preserve_file_dates: bool,
        expected_identity: ImageFileIdentity | None = None,
    ) -> MovePayloadTask:
        operation_list = tuple(operations)
        return self._queue_catalog_mutation(
            catalog,
            label="Saving image edit",
            dest_dir_rel=Path(rel_path).parent.as_posix(),
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._save_image_edit_worker(
                catalog.root,
                rel_path,
                operation_list,
                preserve_file_dates,
                expected_identity,
                task,
            ),
            completion_verb="Saved",
            error_title="Save Image",
        )

    def _save_image_edit_worker(
        self,
        root: Path,
        rel_path: str,
        operations: tuple[EditOperation, ...],
        preserve_file_dates: bool,
        expected_identity: ImageFileIdentity | None,
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, rel_path)
        with Catalog(root) as catalog:
            path = catalog.mutation_path(rel_path)
            apply_operations_to_file(
                path,
                operations,
                preserve_timestamp=False,
                preserve_file_dates=preserve_file_dates,
                expected_identity=expected_identity,
            )
            with suppress(Exception):
                catalog.append_log(
                    f"File edit saved{' with preserved dates' if preserve_file_dates else ''}: {rel_path}"
                )
            with suppress(Exception):
                catalog.rebuild_thumbnail(rel_path)
        task.update(1, 1, rel_path)
        task.mark_done()
        return MovePayloadResult(1, 1, {root})

    def _delete_directory_worker(
        self,
        root: Path,
        dir_rel: str,
        wipe: bool,
        expected_identity: tuple[int, int, int, int],
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, dir_rel)
        with Catalog(root) as catalog:
            catalog.delete_directory(
                dir_rel,
                wipe=wipe,
                expected_identity=expected_identity,
            )
        task.update(1, 1, dir_rel)
        task.mark_done()
        return MovePayloadResult(1, 1, {root})

    def _restore_records_worker(
        self,
        root: Path,
        restore_items: tuple[tuple[str, str], ...],
        task: IndexTask,
    ) -> MovePayloadResult:
        restored = 0
        with Catalog(root) as catalog:
            for index, (kind, rel_path) in enumerate(restore_items, start=1):
                task.update(index - 1, len(restore_items), rel_path)
                if kind == "directory":
                    catalog.restore_directory_from_trash(rel_path)
                else:
                    catalog.restore_image_from_trash(rel_path)
                restored += 1
                task.update(index, len(restore_items), rel_path)
        task.mark_done()
        return MovePayloadResult(len(restore_items), restored, {root})

    def _finish_trash_restore(self, catalog: Catalog, *source_rel_paths: str) -> None:
        self._drop_very_similar_cache(catalog.root)
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        if self.current_catalog is not None and self.current_catalog.root == catalog.root:
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            for source_rel_path in source_rel_paths:
                if self.current_dir_rel == source_rel_path or self.current_dir_rel.startswith(f"{source_rel_path}/"):
                    parent_rel = Path(source_rel_path).parent.as_posix()
                    self.current_dir_rel = TRASH_DIR_NAME if parent_rel == "." else parent_rel
                    break
        self.rebuild_tree()
        self.load_current_directory()

    def open_catalog(self, root: Path, *, log_event: bool = True) -> None:
        operation_started_at = monotonic()
        self._catalog_intent_root = root.expanduser().resolve()
        init_started_at = monotonic()
        was_open = self.workspace.catalog_for_root(root) is not None
        catalog = self.workspace.open_catalog(root)
        self._record_timing_phase(
            catalog.root,
            "catalog_init_sync",
            init_started_at,
            {"was_open": was_open},
        )
        self._finish_open_catalog_ui(
            catalog,
            was_open=was_open,
            log_event=log_event,
            operation_started_at=operation_started_at,
            mode="sync",
            make_current=True,
        )

    def defer_open_catalog(
        self,
        root: Path,
        *,
        log_event: bool = True,
        selected_at: float | None = None,
        dialog_duration_ms: float | None = None,
    ) -> None:
        selected_at = selected_at or monotonic()
        details: dict[str, object] = {}
        if dialog_duration_ms is not None:
            details["dialog_duration_ms"] = round(dialog_duration_ms, 3)
        self._append_timing_event(root, "dialog_selected", 0.0, details)
        self._show_catalog_open_status(root)
        QTimer.singleShot(
            0,
            lambda: self.open_catalog_async(root, log_event=log_event, selected_at=selected_at),
        )

    def open_catalog_async(
        self,
        root: Path,
        *,
        log_event: bool = True,
        selected_at: float | None = None,
    ) -> None:
        if self._closing:
            return
        operation_started_at = monotonic()
        intended_root = root.expanduser().resolve()
        self._catalog_intent_root = intended_root
        if selected_at is not None:
            self._append_timing_event(
                root,
                "deferred_open_start",
                (operation_started_at - selected_at) * 1000,
            )
        existing = self.workspace.catalog_for_root(root)
        if existing is not None:
            self._finish_open_catalog_ui(
                existing,
                was_open=True,
                log_event=log_event,
                operation_started_at=operation_started_at,
                mode="async_existing",
                make_current=True,
            )
            return
        for task in self._catalog_open_tasks.values():
            if task.root.expanduser().resolve() == intended_root:
                task.discard_result = False
                self._show_catalog_open_status(root)
                return
        future = self.catalog_open_executor.submit(self._open_catalog_worker, root)
        self._catalog_open_tasks[future] = CatalogOpenTask(
            root=root,
            future=future,
            log_event=log_event,
            selected_at=selected_at,
            started_at=operation_started_at,
        )
        self._show_catalog_open_status(root)

    def _open_catalog_worker(self, root: Path) -> CatalogOpenResult:
        init_started_at = monotonic()
        catalog = Catalog(root)
        return CatalogOpenResult(catalog, (monotonic() - init_started_at) * 1000)

    def _finish_open_catalog_ui(
        self,
        catalog: Catalog,
        *,
        was_open: bool,
        log_event: bool,
        operation_started_at: float,
        mode: str,
        make_current: bool,
    ) -> None:
        retained_unavailable: list[str] = []
        for path_text in self._unavailable_catalog_paths:
            try:
                matches = Path(path_text).expanduser().absolute() == catalog.root
            except OSError:
                matches = False
            if not matches:
                retained_unavailable.append(path_text)
        self._unavailable_catalog_paths = retained_unavailable
        if log_event and not was_open:
            phase_started_at = monotonic()
            catalog.append_log("Catalog added to workspace")
            self._record_timing_phase(catalog.root, "append_open_log", phase_started_at, {"mode": mode})
        phase_started_at = monotonic()
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        self._idle_index_tasks.pop(catalog.root, None)
        if not was_open:
            self._shallow_tree_roots.add(catalog.root)
        self._record_timing_phase(catalog.root, "prepare_open_state", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
        self._directory_discovery_tasks[catalog.root] = self.indexer.discover_directories(catalog.root)
        self._record_timing_phase(catalog.root, "start_directory_discovery", phase_started_at, {"mode": mode})

        if make_current:
            self.current_catalog = catalog
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""

        phase_started_at = monotonic()
        self.rebuild_tree()
        self._record_timing_phase(catalog.root, "rebuild_tree", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
        if make_current:
            self.load_current_directory()
        self._record_timing_phase(catalog.root, "load_current_directory", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
        self.queue_directory_index(catalog, "", interactive=False)
        self._record_timing_phase(catalog.root, "queue_root_directory_index", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
        self._schedule_idle_indexing()
        self._record_timing_phase(catalog.root, "schedule_idle_indexing", phase_started_at, {"mode": mode})

        self._append_timing_event(
            catalog.root,
            "open_catalog_total",
            (monotonic() - operation_started_at) * 1000,
            {"mode": mode, "was_open": was_open},
        )

    def _show_catalog_open_status(self, root: Path) -> None:
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText(f"Opening catalog {root.name or root}")

    def _record_timing_phase(
        self,
        root: Path,
        phase: str,
        started_at: float,
        details: dict[str, object] | None = None,
    ) -> None:
        self._append_timing_event(root, phase, (monotonic() - started_at) * 1000, details)

    def _append_timing_event(
        self,
        root: Path,
        phase: str,
        duration_ms: float | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            state_dir = root.expanduser() / ".marnwick"
            if state_dir.is_symlink():
                return
            state_dir.mkdir(parents=True, exist_ok=True)
            timings_path = state_dir / TIMINGS_FILE_NAME
            if state_dir.is_symlink() or timings_path.is_symlink():
                return
            try:
                payload = json.loads(timings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            events = payload.get("events")
            if not isinstance(events, list):
                events = []
            event: dict[str, object] = {
                "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "operation": "open_catalog",
                "phase": phase,
                "root": str(root.expanduser()),
            }
            if duration_ms is not None:
                event["duration_ms"] = round(float(duration_ms), 3)
            if details:
                event["details"] = {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in details.items()
                }
            events.append(event)
            payload["version"] = 1
            payload["events"] = events[-MAX_TIMING_EVENTS:]
            data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{timings_path.name}.",
                suffix=".tmp",
                dir=state_dir,
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                if timings_path.is_symlink():
                    return
                os.replace(temp_path, timings_path)
            finally:
                if fd >= 0:
                    os.close(fd)
                temp_path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            return

    def _settle_catalog_open_tasks(self) -> None:
        for future, task in list(self._catalog_open_tasks.items()):
            if not future.done():
                continue
            self._catalog_open_tasks.pop(future, None)
            if future.cancelled():
                self._append_timing_event(task.root, "catalog_init_canceled", None)
                continue
            try:
                result = future.result()
            except Exception as error:
                self._append_timing_event(
                    task.root,
                    "catalog_init_failed",
                    (monotonic() - task.started_at) * 1000,
                    {"error": str(error)},
                )
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(0)
                self.progress_label.setText("Ready")
                show_error(self, "Open Catalog", str(error))
                continue
            if task.discard_result or self._closing:
                result.catalog.close()
                continue
            self._append_timing_event(result.catalog.root, "catalog_init", result.init_duration_ms)
            catalog, was_open = self.workspace.adopt_catalog(result.catalog)
            self._finish_open_catalog_ui(
                catalog,
                was_open=was_open,
                log_event=task.log_event,
                operation_started_at=task.started_at,
                mode="async",
                make_current=self._catalog_intent_root == catalog.root,
            )

    def _has_active_catalog_open_tasks(self) -> bool:
        return bool(self._catalog_open_tasks)

    def _active_catalog_open_task(self) -> CatalogOpenTask | None:
        tasks = list(self._catalog_open_tasks.values())
        if not tasks:
            return None
        return sorted(tasks, key=lambda task: task.started_at)[0]

    def _cancel_catalog_open_tasks(self, root: Path) -> None:
        for future, task in list(self._catalog_open_tasks.items()):
            try:
                matches_root = task.root.expanduser().resolve() == root.expanduser().resolve()
            except OSError:
                matches_root = task.root.expanduser() == root.expanduser()
            if not matches_root:
                continue
            task.discard_result = True
            if future.cancel():
                self._catalog_open_tasks.pop(future, None)
            self._append_timing_event(task.root, "catalog_init_canceled", None)

    def close_catalog(self, root: Path) -> None:
        resolved = root.resolve()
        if self._catalog_intent_root == resolved:
            self._catalog_intent_root = None
        self._cancel_catalog_open_tasks(resolved)
        self._cancel_virtual_view_tasks(resolved)
        self._cancel_duplicate_delete_task(resolved, wait=False)
        self._cancel_move_payload_task(resolved, wait=False)
        self._wait_for_catalog_file_tasks(resolved)
        for task_root, task in list(self._idle_index_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for task_root, task in list(self._directory_discovery_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for (task_root, _), task in list(self._directory_index_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for task_root, task in list(self._thumbnail_prune_tasks.items()):
            if task_root == resolved:
                task.cancel()
        self._drop_very_similar_cache(resolved)
        catalog = self.workspace.catalog_for_root(root)
        if catalog is not None:
            catalog.append_log("Catalog removed from workspace")
        self.workspace.close_catalog(root)
        self._swept_catalog_roots.discard(resolved)
        self._pruned_catalog_roots.discard(resolved)
        self._idle_index_tasks.pop(resolved, None)
        self._directory_discovery_tasks.pop(resolved, None)
        self._directory_discovery_retries.pop(resolved, None)
        self._directory_discovery_retry_at.pop(resolved, None)
        self._directory_index_tasks = {
            key: task for key, task in self._directory_index_tasks.items() if key[0] != resolved
        }
        self._thumbnail_prune_tasks.pop(resolved, None)
        self._resume_idle_refresh_roots.discard(resolved)
        self._shallow_tree_roots.discard(resolved)
        self._pending_tree_rebuilds.pop(resolved, None)
        if self.current_catalog and self.current_catalog.root == resolved:
            self.current_catalog = None
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            self.model.set_images(None, [])
            self.update_selection_status()
        self.rebuild_tree()

    def _wait_for_catalog_file_tasks(self, root: Path) -> None:
        """Responsively settle protected file changes before closing a catalog."""

        resolved = root.expanduser().resolve()

        def pending_tasks() -> list[DuplicateDeleteTask | DeletePayloadTask | MovePayloadTask]:
            tasks: list[DuplicateDeleteTask | DeletePayloadTask | MovePayloadTask] = []
            duplicate = self._duplicate_delete_task
            if duplicate is not None and duplicate.root == resolved and not duplicate.future.done():
                tasks.append(duplicate)
            tasks.extend(
                task
                for task in self._delete_payload_tasks
                if task.root == resolved and not task.future.done()
            )
            tasks.extend(
                task
                for task in self._move_payload_tasks
                if resolved in task.affected_roots and not task.future.done()
            )
            return tasks

        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        if not pending_tasks():
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Finishing File Changes")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        label = QLabel("Finishing pending file changes safely...")
        label.setMinimumWidth(480)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(label)
        layout.addWidget(progress)
        dialog.setModal(True)

        def update() -> None:
            self._settle_duplicate_delete_task()
            self._settle_delete_payload_task()
            self._settle_move_payload_task()
            active = pending_tasks()
            if not active:
                dialog.accept()
                return
            snapshot = active[0].task.snapshot()
            if snapshot.total is None:
                progress.setRange(0, 0)
                detail = snapshot.current or snapshot.label
            else:
                total = max(snapshot.total, 1)
                progress.setRange(0, total)
                progress.setValue(min(snapshot.processed, total))
                detail = f"{snapshot.processed}/{snapshot.total}"
                if snapshot.current:
                    detail = f"{detail}: {snapshot.current}"
            label.setText(f"{snapshot.label}: {detail}")

        timer = QTimer(dialog)
        timer.setInterval(50)
        timer.timeout.connect(update)
        timer.start()
        update()
        if pending_tasks():
            dialog.exec()
        timer.stop()

    def rebuild_tree(self) -> None:
        if self._closing:
            return
        if self._directory_drag_active:
            self._tree_rebuild_deferred = True
            return
        self._tree_build_task = None
        self._pending_tree_rebuilds.clear()
        expanded_items = self._expanded_tree_items()
        known_items = self._known_tree_items()
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None
        deferred_catalogs: list[Catalog] = []
        for catalog in self.workspace.catalogs:
            root_item = QTreeWidgetItem([catalog.root.name or str(catalog.root)])
            root_item.setIcon(0, self.folder_icon)
            root_item.setToolTip(0, str(catalog.root))
            root_item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
            root_item.setData(0, DIR_REL_ROLE, "")
            self.tree.addTopLevelItem(root_item)
            item_by_dir = {"": root_item}
            if self._is_current_tree_item(catalog.root, ""):
                selected_item = root_item
            # Opening a catalog must not parse a huge cache or materialize the
            # entire database-backed tree on the GUI thread. A shallow
            # filesystem read gives immediate navigation; known descendants
            # are acquired in bounded pages on later event-loop turns.
            directory_rels = self._initial_tree_directory_rels(catalog)
            if (
                self.current_catalog is catalog
                and self.current_virtual_kind is None
                and self.current_dir_rel
            ):
                selected_parts = Path(self.current_dir_rel).parts
                selected_ancestors = [
                    Path(*selected_parts[:index]).as_posix()
                    for index in range(1, len(selected_parts) + 1)
                ]
                directory_rels = sorted(
                    {*directory_rels, *selected_ancestors},
                    key=lambda item: (item.casefold(), item),
                )
            for dir_rel in directory_rels:
                if not dir_rel:
                    continue
                parent_rel = Path(dir_rel).parent.as_posix()
                if parent_rel == ".":
                    parent_rel = ""
                parent_item = item_by_dir.get(parent_rel, root_item)
                item = QTreeWidgetItem([Path(dir_rel).name])
                item.setIcon(0, self.folder_icon)
                item.setToolTip(0, str(catalog.root / dir_rel))
                item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
                item.setData(0, DIR_REL_ROLE, dir_rel)
                parent_item.addChild(item)
                item_by_dir[dir_rel] = item
                if self._is_current_tree_item(catalog.root, dir_rel):
                    selected_item = item
            root_item.setExpanded(True)
            for dir_rel, item in item_by_dir.items():
                if self._tree_state_key_for_directory(catalog.root, dir_rel) in expanded_items:
                    item.setExpanded(True)
            virtual_selected_item = self._add_virtual_tree_items(catalog, root_item, expanded_items, known_items)
            if virtual_selected_item is not None:
                selected_item = virtual_selected_item
            if catalog.root not in self._shallow_tree_roots:
                deferred_catalogs.append(catalog)
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
            self._expand_tree_item_ancestors(selected_item)
            self.tree.scrollToItem(selected_item)
        for catalog in deferred_catalogs:
            self._pending_tree_rebuilds[catalog.root] = (catalog, "large_tree")
        if deferred_catalogs:
            QTimer.singleShot(0, self._start_next_pending_tree_rebuild)

    def _request_incremental_tree_rebuild(self, catalog: Catalog, *, reason: str) -> None:
        if self._directory_drag_active:
            self._tree_rebuild_deferred = True
            return
        active_task = self._tree_build_task
        if active_task is not None:
            self._pending_tree_rebuilds[catalog.root] = (catalog, reason)
            self._append_timing_event(
                catalog.root,
                "queue_incremental_tree_rebuild",
                None,
                {"reason": reason},
            )
            return
        self._start_incremental_tree_rebuild(catalog, reason=reason)

    def _start_incremental_tree_rebuild(self, catalog: Catalog, *, reason: str) -> None:
        if self._closing or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        phase_started_at = monotonic()
        self._pending_tree_rebuilds.pop(catalog.root, None)
        expanded_items = self._expanded_tree_items()
        known_items = self._known_tree_items()
        root_item = self._tree_item_for_root(catalog.root)
        if root_item is None:
            root_item = QTreeWidgetItem([catalog.root.name or str(catalog.root)])
            root_item.setIcon(0, self.folder_icon)
            root_item.setToolTip(0, str(catalog.root))
            root_item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
            root_item.setData(0, DIR_REL_ROLE, "")
            self.tree.addTopLevelItem(root_item)
            item_by_dir = {"": root_item}
        else:
            item_by_dir = {"": root_item}
            stack = [root_item]
            while stack:
                parent_item = stack.pop()
                for child_index in range(parent_item.childCount() - 1, -1, -1):
                    child = parent_item.child(child_index)
                    if child.data(0, VIRTUAL_KIND_ROLE):
                        parent_item.takeChild(child_index)
                        continue
                    child_rel = child.data(0, DIR_REL_ROLE)
                    if child_rel:
                        item_by_dir[str(child_rel)] = child
                    stack.append(child)
        root_item.setExpanded(True)
        self._tree_build_task = TreeBuildTask(
            catalog=catalog,
            directories=[],
            total=None,
            page_offset=0,
            processed=0,
            seen_directories=set(),
            expanded_items=expanded_items,
            known_items=known_items,
            item_by_dir=item_by_dir,
            index=0,
            selected_item=root_item if self._is_current_tree_item(catalog.root, "") else None,
            started_at=phase_started_at,
            reason=reason,
        )
        self._record_timing_phase(
            catalog.root,
            "start_incremental_tree_rebuild",
            phase_started_at,
            {"reason": reason},
        )
        self._continue_incremental_tree_rebuild()

    def _continue_incremental_tree_rebuild(self) -> None:
        if self._closing:
            self._tree_build_task = None
            self._pending_tree_rebuilds.clear()
            return
        if self._directory_drag_active:
            self._tree_rebuild_deferred = True
            return
        task = self._tree_build_task
        if task is None:
            return
        deadline = monotonic() + TREE_BUILD_BUDGET_SECONDS
        processed_this_turn = 0
        while task.total is None or task.processed < task.total:
            if task.index >= len(task.directories):
                page = task.catalog.list_known_directories(
                    limit=TREE_BUILD_BATCH_SIZE,
                    offset=task.page_offset,
                )
                task.page_offset += len(page)
                task.directories = [dir_rel for dir_rel in page if dir_rel]
                task.index = 0
                if len(page) < TREE_BUILD_BATCH_SIZE:
                    task.total = task.processed + len(task.directories)
                if not page:
                    task.total = task.processed
                    break
                if not task.directories:
                    continue
            dir_rel = task.directories[task.index]
            task.index += 1
            task.processed += 1
            parent_rel = ""
            for part in Path(dir_rel).parts:
                current_rel = f"{parent_rel}/{part}" if parent_rel else part
                item = task.item_by_dir.get(current_rel)
                if item is None:
                    parent_item = task.item_by_dir[parent_rel]
                    item = QTreeWidgetItem([part])
                    item.setIcon(0, self.folder_icon)
                    item.setToolTip(0, str(task.catalog.root / current_rel))
                    item.setData(0, CATALOG_ROOT_ROLE, str(task.catalog.root))
                    item.setData(0, DIR_REL_ROLE, current_rel)
                    parent_item.addChild(item)
                    task.item_by_dir[current_rel] = item
                    if self._tree_state_key_for_directory(task.catalog.root, current_rel) in task.expanded_items:
                        item.setExpanded(True)
                if self._is_current_tree_item(task.catalog.root, current_rel):
                    task.selected_item = item
                task.seen_directories.add(current_rel)
                parent_rel = current_rel
            processed_this_turn += 1
            if processed_this_turn >= TREE_BUILD_BATCH_SIZE or monotonic() >= deadline:
                break
        total = task.total
        if total is None or task.processed < total:
            progress_total = total if total is not None else task.processed + TREE_BUILD_BATCH_SIZE
            self.progress_bar.setRange(0, max(progress_total, 1))
            self.progress_bar.setValue(task.processed)
            if total is None:
                self.progress_label.setText(f"Building folder tree: {task.processed} folders")
            else:
                self.progress_label.setText(f"Building folder tree {task.processed}/{total}")
            QTimer.singleShot(0, self._continue_incremental_tree_rebuild)
            return
        for dir_rel in sorted(
            set(task.item_by_dir) - {""} - task.seen_directories,
            key=lambda value: len(Path(value).parts),
            reverse=True,
        ):
            item = task.item_by_dir.pop(dir_rel, None)
            if item is not None and item.parent() is not None:
                item.parent().removeChild(item)
        virtual_selected_item = self._add_virtual_tree_items(
            task.catalog,
            task.item_by_dir[""],
            task.expanded_items,
            task.known_items,
        )
        if self.current_catalog is not None and self.current_catalog.root == task.catalog.root:
            if self.current_virtual_kind is None:
                task.selected_item = task.item_by_dir.get(self.current_dir_rel)
            else:
                task.selected_item = virtual_selected_item
        else:
            task.selected_item = None
        if task.selected_item is not None:
            self.tree.setCurrentItem(task.selected_item)
            self._expand_tree_item_ancestors(task.selected_item)
            self.tree.scrollToItem(task.selected_item)
        self._append_timing_event(
            task.catalog.root,
            "incremental_tree_rebuild_complete",
            (monotonic() - task.started_at) * 1000,
            {"reason": task.reason, "directories": task.processed},
        )
        self._tree_build_task = None
        self._start_next_pending_tree_rebuild()

    def _start_next_pending_tree_rebuild(self) -> None:
        if self._closing:
            self._pending_tree_rebuilds.clear()
            return
        while self._pending_tree_rebuilds:
            root, (_, reason) = self._pending_tree_rebuilds.popitem()
            catalog = self.workspace.catalog_for_root(root)
            if catalog is None:
                continue
            QTimer.singleShot(
                0,
                lambda catalog=catalog, reason=reason: self._start_incremental_tree_rebuild(catalog, reason=reason),
            )
            return

    def _tree_item_for_root(self, root: Path) -> QTreeWidgetItem | None:
        resolved = root.resolve()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if Path(item.data(0, CATALOG_ROOT_ROLE)).resolve() == resolved:
                return item
        return None

    def _add_virtual_tree_items(
        self,
        catalog: Catalog,
        root_item: QTreeWidgetItem,
        expanded_items: set[TreeStateKey],
        known_items: set[TreeStateKey],
    ) -> QTreeWidgetItem | None:
        selected_item: QTreeWidgetItem | None = None

        virtual_root = QTreeWidgetItem(["Virtual Directories"])
        self._set_virtual_tree_item_data(virtual_root, catalog, VIRTUAL_KIND_ROOT, "")
        root_item.addChild(virtual_root)
        virtual_root.setExpanded(
            self._virtual_tree_item_should_expand(
                catalog.root,
                VIRTUAL_KIND_ROOT,
                "",
                expanded_items,
                known_items,
                default=True,
            )
        )

        tags_root = QTreeWidgetItem(["Tags"])
        self._set_virtual_tree_item_data(tags_root, catalog, VIRTUAL_KIND_TAG_ROOT, "")
        virtual_root.addChild(tags_root)
        tags_root.setExpanded(
            self._virtual_tree_item_should_expand(
                catalog.root,
                VIRTUAL_KIND_TAG_ROOT,
                "",
                expanded_items,
                known_items,
                default=True,
            )
        )
        for tag in catalog.list_tags():
            item = QTreeWidgetItem([tag])
            self._set_virtual_tree_item_data(item, catalog, VIRTUAL_KIND_TAG, tag)
            tags_root.addChild(item)
            if self._is_current_virtual_item(catalog.root, VIRTUAL_KIND_TAG, tag):
                selected_item = item

        duplicates_item = QTreeWidgetItem(["Exact Duplicates"])
        self._set_virtual_tree_item_data(duplicates_item, catalog, VIRTUAL_KIND_DUPLICATES, "")
        virtual_root.addChild(duplicates_item)
        if self._is_current_virtual_item(catalog.root, VIRTUAL_KIND_DUPLICATES, ""):
            selected_item = duplicates_item

        very_similar_item = QTreeWidgetItem(["Very Similar"])
        self._set_virtual_tree_item_data(very_similar_item, catalog, VIRTUAL_KIND_VERY_SIMILAR, "")
        virtual_root.addChild(very_similar_item)
        if self._is_current_virtual_item(catalog.root, VIRTUAL_KIND_VERY_SIMILAR, ""):
            selected_item = very_similar_item
        return selected_item

    def _tree_state_key_for_directory(self, root: Path, dir_rel: str) -> TreeStateKey:
        return (root.resolve(), "dir", dir_rel, "")

    def _tree_state_key_for_virtual(self, root: Path, kind: str, value: str) -> TreeStateKey:
        return (root.resolve(), "virtual", kind, value)

    def _tree_item_state_key(self, item: QTreeWidgetItem) -> TreeStateKey:
        root = Path(item.data(0, CATALOG_ROOT_ROLE)).resolve()
        virtual_kind = item.data(0, VIRTUAL_KIND_ROLE) or ""
        if virtual_kind:
            return self._tree_state_key_for_virtual(root, virtual_kind, item.data(0, VIRTUAL_VALUE_ROLE) or "")
        return self._tree_state_key_for_directory(root, item.data(0, DIR_REL_ROLE) or "")

    def _virtual_tree_item_should_expand(
        self,
        root: Path,
        kind: str,
        value: str,
        expanded_items: set[TreeStateKey],
        known_items: set[TreeStateKey],
        *,
        default: bool,
    ) -> bool:
        key = self._tree_state_key_for_virtual(root, kind, value)
        if key in expanded_items:
            return True
        if key in known_items:
            return False
        return default

    def _set_virtual_tree_item_data(
        self,
        item: QTreeWidgetItem,
        catalog: Catalog,
        kind: str,
        value: str,
    ) -> None:
        item.setIcon(0, self.virtual_folder_icon)
        item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
        item.setData(0, DIR_REL_ROLE, "")
        item.setData(0, VIRTUAL_KIND_ROLE, kind)
        item.setData(0, VIRTUAL_VALUE_ROLE, value)
        if kind == VIRTUAL_KIND_TAG:
            item.setToolTip(0, f"Images tagged {value}")
        elif kind == VIRTUAL_KIND_DUPLICATES:
            item.setToolTip(0, "Images with matching exact content hashes")
        elif kind == VIRTUAL_KIND_VERY_SIMILAR:
            item.setToolTip(0, "Images with close aspect ratios, perceptual hashes, and color distributions")
        else:
            item.setToolTip(0, "Virtual directories")

    def is_virtual_tree_item(self, item: QTreeWidgetItem | None) -> bool:
        return item is not None and bool(item.data(0, VIRTUAL_KIND_ROLE))

    def _initial_tree_directory_rels(self, catalog: Catalog) -> list[str]:
        """Return a bounded shallow tree without reading the cache or database inventory."""

        directories: list[str] = []
        try:
            with os.scandir(catalog.root) as entries:
                for entry in entries:
                    if entry.name == ".marnwick" or not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        directories.append(catalog.rel_path(Path(entry.path)))
                    except (OSError, UnicodeError, ValueError):
                        continue
                    if len(directories) >= TREE_BUILD_BATCH_SIZE:
                        break
        except OSError:
            return []
        return sorted(directories, key=lambda item: (item.casefold(), item))

    def _expanded_tree_items(self) -> set[TreeStateKey]:
        expanded: set[TreeStateKey] = set()
        stack = [
            self.tree.topLevelItem(index)
            for index in range(self.tree.topLevelItemCount() - 1, -1, -1)
        ]
        while stack:
            item = stack.pop()
            if item.isExpanded():
                expanded.add(self._tree_item_state_key(item))
            stack.extend(item.child(index) for index in range(item.childCount() - 1, -1, -1))
        return expanded

    def _known_tree_items(self) -> set[TreeStateKey]:
        known: set[TreeStateKey] = set()
        stack = [
            self.tree.topLevelItem(index)
            for index in range(self.tree.topLevelItemCount() - 1, -1, -1)
        ]
        while stack:
            item = stack.pop()
            known.add(self._tree_item_state_key(item))
            stack.extend(item.child(index) for index in range(item.childCount() - 1, -1, -1))
        return known

    def _is_current_tree_item(self, root: Path, dir_rel: str) -> bool:
        return (
            self.current_catalog is not None
            and self.current_catalog.root == root
            and self.current_virtual_kind is None
            and self.current_dir_rel == dir_rel
        )

    def _is_current_virtual_item(self, root: Path, kind: str, value: str) -> bool:
        return (
            self.current_catalog is not None
            and self.current_catalog.root == root
            and self.current_virtual_kind == kind
            and self.current_virtual_value == value
        )

    def _expand_tree_item_ancestors(self, item: QTreeWidgetItem) -> None:
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

    def reload_tree_and_directory(
        self,
        *,
        preserve_tree_scroll: bool = False,
        preserve_selection: bool = True,
    ) -> None:
        if self._closing:
            return
        tree_scroll_position = self._tree_scroll_position() if preserve_tree_scroll else None
        self._ensure_current_directory_exists()
        self.rebuild_tree()
        self.load_current_directory(preserve_selection=preserve_selection)
        if tree_scroll_position is not None:
            self._restore_tree_scroll_position(tree_scroll_position)
            QTimer.singleShot(
                0,
                lambda position=tree_scroll_position: self._restore_tree_scroll_position(position),
            )

    def _ensure_current_directory_exists(self) -> None:
        catalog = self.current_catalog
        if catalog is None or self.current_virtual_kind is not None:
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            self.current_catalog = None
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            self.model.set_images(None, [])
            return
        while self.current_dir_rel:
            try:
                exists = catalog.abs_path(self.current_dir_rel).is_dir()
            except (OSError, ValueError):
                exists = False
            if exists:
                return
            parent_rel = Path(self.current_dir_rel).parent.as_posix()
            self.current_dir_rel = "" if parent_rel == "." else parent_rel

    def _tree_scroll_position(self) -> tuple[int, int]:
        return (
            self.tree.verticalScrollBar().value(),
            self.tree.horizontalScrollBar().value(),
        )

    def _restore_tree_scroll_position(self, position: tuple[int, int]) -> None:
        if self._closing:
            return
        vertical, horizontal = position
        vertical_bar = self.tree.verticalScrollBar()
        horizontal_bar = self.tree.horizontalScrollBar()
        vertical_bar.setValue(max(vertical_bar.minimum(), min(vertical, vertical_bar.maximum())))
        horizontal_bar.setValue(max(horizontal_bar.minimum(), min(horizontal, horizontal_bar.maximum())))

    def _directory_clicked(self, item: QTreeWidgetItem) -> None:
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        self._catalog_intent_root = catalog.root
        if self.is_virtual_tree_item(item):
            self._virtual_directory_clicked(catalog, item)
            return
        dir_rel = item.data(0, DIR_REL_ROLE)
        idle_task = self._idle_index_tasks.get(catalog.root)
        if idle_task is not None and not idle_task.snapshot().done:
            self._resume_idle_refresh_roots.add(catalog.root)
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root, keep_dir_rel=dir_rel)
        self.current_catalog = catalog
        self.current_dir_rel = dir_rel
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self.load_current_directory()
        self.queue_directory_index(catalog, self.current_dir_rel)

    def _virtual_directory_clicked(self, catalog: Catalog, item: QTreeWidgetItem) -> None:
        kind = item.data(0, VIRTUAL_KIND_ROLE)
        value = item.data(0, VIRTUAL_VALUE_ROLE) or ""
        if kind in {VIRTUAL_KIND_ROOT, VIRTUAL_KIND_TAG_ROOT}:
            item.setExpanded(not item.isExpanded())
            return
        if kind not in {VIRTUAL_KIND_TAG, VIRTUAL_KIND_DUPLICATES, VIRTUAL_KIND_VERY_SIMILAR}:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        self._catalog_intent_root = catalog.root
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root)
        self.current_catalog = catalog
        self.current_dir_rel = ""
        self.current_virtual_kind = kind
        self.current_virtual_value = value
        self.load_current_directory()

    def load_current_directory(self, *, preserve_selection: bool = False) -> None:
        self._remember_thumbnail_scroll_position()
        self._remember_thumbnail_selection()
        target_scroll_key = self._current_thumbnail_scroll_key()
        if preserve_selection:
            selection_keys = self._thumbnail_selection_keys()
            current_key = self._current_thumbnail_selection_key()
        else:
            remembered = self._thumbnail_selections.get(target_scroll_key) if target_scroll_key is not None else None
            selection_keys, current_key = remembered if remembered is not None else (set(), None)
            preserve_selection = remembered is not None
        if self.current_catalog is None:
            self.model.set_images(None, [])
            self._thumbnail_scroll_key = None
            self.update_selection_status()
            return
        if self.current_virtual_kind is not None:
            self.load_current_virtual_directory(
                preserve_selection=preserve_selection,
                selection_keys=selection_keys,
                current_key=current_key,
                scroll_key=target_scroll_key,
            )
            return
        if self.current_catalog.directory_pane_record_count(self.current_dir_rel) > THUMBNAIL_MODEL_BATCH_SIZE:
            self._load_large_physical_directory(
                preserve_selection=preserve_selection,
                selection_keys=selection_keys,
                current_key=current_key,
                scroll_key=target_scroll_key,
            )
            return
        images = self.current_catalog.list_images_with_placeholders(
            self.current_dir_rel,
            self.current_sort,
            include_blobs=False,
            placeholder_scan_budget_ms=0,
            placeholder_limit=0,
        )
        if self.current_catalog.root in self._shallow_tree_roots:
            directories = self._shallow_child_directories(self.current_catalog, self.current_dir_rel)
        else:
            directories = self.current_catalog.list_child_directories(
                self.current_dir_rel,
                self.current_sort,
                include_previews=True,
                include_filesystem_preview_fallback=False,
            )
        records = self._without_pending_delete_records(self.current_catalog.root, [*directories, *images])
        self.model.set_images(self.current_catalog, records)
        self.refresh_thumbnail_layout()
        if preserve_selection:
            self._restore_thumbnail_selection(selection_keys, current_key)
        self._restore_thumbnail_scroll_position(target_scroll_key)
        self.update_selection_status()

    def _load_large_physical_directory(
        self,
        *,
        preserve_selection: bool,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> None:
        catalog = self.current_catalog
        if catalog is None or self.current_virtual_kind is not None:
            return
        dir_rel = self.current_dir_rel
        fingerprint = catalog.catalog_database_mtime_ns()
        task = self._matching_virtual_view_task(
            catalog.root,
            VIRTUAL_KIND_PHYSICAL,
            dir_rel,
            self.current_sort,
            fingerprint,
        )
        if task is None:
            cancel_event = Event()
            future = self.virtual_view_executor.submit(
                self._physical_view_worker,
                catalog.root,
                dir_rel,
                self.current_sort.value,
                fingerprint,
                cancel_event,
            )
            task = VirtualViewTask(
                root=catalog.root,
                kind=VIRTUAL_KIND_PHYSICAL,
                value=dir_rel,
                sort_order=self.current_sort,
                fingerprint=fingerprint,
                future=future,
                started_at=monotonic(),
                selection_keys=set(selection_keys),
                current_key=current_key,
                scroll_key=scroll_key,
                cancel_event=cancel_event,
            )
            self._virtual_view_tasks[future] = task
        elif preserve_selection:
            task.selection_keys = set(selection_keys)
            task.current_key = current_key
            task.scroll_key = scroll_key
        self.model.set_images(catalog, [])
        self.refresh_thumbnail_layout()
        self._restore_thumbnail_scroll_position(scroll_key)
        self.update_selection_status()
        self._show_virtual_view_status(task)

    def _physical_view_worker(
        self,
        root: Path,
        dir_rel: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog(root) as catalog:
            images = catalog.list_images_with_placeholders(
                dir_rel,
                sort_order,
                include_blobs=False,
                placeholder_scan_budget_ms=0,
                placeholder_limit=0,
                cancel_check=check_canceled,
            )
            check_canceled()
            directories = catalog.list_child_directories(
                dir_rel,
                sort_order,
                include_previews=True,
                include_filesystem_preview_fallback=False,
                cancel_check=check_canceled,
            )
        check_canceled()
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_PHYSICAL,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=[*directories, *images],
            duration_ms=(monotonic() - started_at) * 1000,
        )

    def _current_thumbnail_scroll_key(self) -> TreeStateKey | None:
        if self.current_catalog is None:
            return None
        if self.current_virtual_kind is not None:
            return self._tree_state_key_for_virtual(
                self.current_catalog.root,
                self.current_virtual_kind,
                self.current_virtual_value,
            )
        return self._tree_state_key_for_directory(self.current_catalog.root, self.current_dir_rel)

    def _remember_thumbnail_scroll_position(self) -> None:
        if self._thumbnail_scroll_key is None:
            return
        self._thumbnail_scroll_positions[self._thumbnail_scroll_key] = (
            self.thumbnail_view.verticalScrollBar().value(),
            self.thumbnail_view.horizontalScrollBar().value(),
        )

    def _remember_thumbnail_selection(self) -> None:
        if self._thumbnail_scroll_key is None:
            return
        self._thumbnail_selections[self._thumbnail_scroll_key] = (
            self._thumbnail_selection_keys(),
            self._current_thumbnail_selection_key(),
        )

    def _restore_thumbnail_scroll_position(self, key: TreeStateKey | None) -> None:
        self._thumbnail_scroll_key = key
        vertical, horizontal = self._thumbnail_scroll_positions.get(key, (0, 0)) if key is not None else (0, 0)
        self._thumbnail_scroll_restore_generation += 1
        generation = self._thumbnail_scroll_restore_generation
        self._set_thumbnail_scroll_position(key, vertical, horizontal, retries=5, generation=generation)

    def _set_thumbnail_scroll_position(
        self,
        key: TreeStateKey | None,
        vertical: int,
        horizontal: int,
        *,
        retries: int,
        generation: int,
    ) -> None:
        if self._closing:
            return
        if key != self._thumbnail_scroll_key or generation != self._thumbnail_scroll_restore_generation:
            return
        vertical_bar = self.thumbnail_view.verticalScrollBar()
        horizontal_bar = self.thumbnail_view.horizontalScrollBar()
        vertical_bar.setValue(max(vertical_bar.minimum(), min(vertical, vertical_bar.maximum())))
        horizontal_bar.setValue(max(horizontal_bar.minimum(), min(horizontal, horizontal_bar.maximum())))
        if retries <= 0:
            return
        if vertical <= vertical_bar.maximum() and horizontal <= horizontal_bar.maximum():
            return
        next_retries = retries - 1
        QTimer.singleShot(
            0,
            partial(
                self._set_thumbnail_scroll_position,
                key,
                vertical,
                horizontal,
                retries=next_retries,
                generation=generation,
            ),
        )

    def _cancel_thumbnail_scroll_restore(self) -> None:
        self._thumbnail_scroll_restore_generation += 1

    def _shallow_child_directories(self, catalog: Catalog, dir_rel: str) -> list[DirectoryRecord]:
        if not catalog.directory_tree_cache_available():
            return catalog.list_filesystem_child_directories(dir_rel, self.current_sort)
        records: list[DirectoryRecord] = []
        for child_rel in catalog.list_cached_child_directory_rels(dir_rel):
            path = catalog.abs_path(child_rel)
            try:
                stat = path.stat()
            except OSError:
                stat_mtime = 0
            else:
                stat_mtime = stat.st_mtime_ns
            records.append(
                DirectoryRecord(
                    catalog_root=catalog.root,
                    dir_rel=child_rel,
                    name=Path(child_rel).name,
                    mtime_ns=stat_mtime,
                    allow_preview_fallback=False,
                )
            )
        return sorted(records, key=catalog._directory_sort_key(self.current_sort), reverse=catalog._record_sort_reverse(self.current_sort))

    def load_current_virtual_directory(
        self,
        *,
        preserve_selection: bool = False,
        selection_keys: set[tuple[str, str]] | None = None,
        current_key: tuple[str, str] | None = None,
        scroll_key: TreeStateKey | None = None,
    ) -> None:
        if self.current_catalog is None or self.current_virtual_kind is None:
            return
        if preserve_selection and selection_keys is None:
            selection_keys = self._thumbnail_selection_keys()
            current_key = self._current_thumbnail_selection_key()
        if self.current_virtual_kind in {VIRTUAL_KIND_TAG, VIRTUAL_KIND_DUPLICATES}:
            self._load_simple_virtual_directory(
                preserve_selection=preserve_selection,
                selection_keys=selection_keys or set(),
                current_key=current_key,
                scroll_key=scroll_key,
            )
            return
        if self.current_virtual_kind == VIRTUAL_KIND_VERY_SIMILAR:
            self._load_very_similar_virtual_directory(
                preserve_selection=preserve_selection,
                selection_keys=selection_keys or set(),
                current_key=current_key,
                scroll_key=scroll_key,
            )
            return
        self.model.set_images(self.current_catalog, [])
        self.refresh_thumbnail_layout()
        self._restore_thumbnail_scroll_position(scroll_key)
        self.update_selection_status()

    def _load_simple_virtual_directory(
        self,
        *,
        preserve_selection: bool,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> None:
        catalog = self.current_catalog
        kind = self.current_virtual_kind
        if catalog is None or kind not in {VIRTUAL_KIND_TAG, VIRTUAL_KIND_DUPLICATES}:
            return
        value = self.current_virtual_value
        fingerprint = catalog.catalog_database_mtime_ns()
        task = self._matching_virtual_view_task(
            catalog.root,
            kind,
            value,
            self.current_sort,
            fingerprint,
        )
        if task is None:
            cancel_event = Event()
            future = self.virtual_view_executor.submit(
                self._simple_virtual_view_worker,
                catalog.root,
                kind,
                value,
                self.current_sort.value,
                fingerprint,
                cancel_event,
            )
            task = VirtualViewTask(
                root=catalog.root,
                kind=kind,
                value=value,
                sort_order=self.current_sort,
                fingerprint=fingerprint,
                future=future,
                started_at=monotonic(),
                selection_keys=set(selection_keys),
                current_key=current_key,
                scroll_key=scroll_key,
                cancel_event=cancel_event,
            )
            self._virtual_view_tasks[future] = task
        elif preserve_selection:
            task.selection_keys = set(selection_keys)
            task.current_key = current_key
            task.scroll_key = scroll_key
        self.model.set_images(catalog, [])
        self.refresh_thumbnail_layout()
        self._restore_thumbnail_scroll_position(scroll_key)
        self.update_selection_status()
        self._show_virtual_view_status(task)

    def _simple_virtual_view_worker(
        self,
        root: Path,
        kind: str,
        value: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog(root) as catalog:
            if kind == VIRTUAL_KIND_TAG:
                images = catalog.list_images_for_tag(
                    value,
                    sort_order,
                    include_blobs=False,
                    cancel_check=check_canceled,
                )
            else:
                images = catalog.list_duplicate_images(
                    sort_order,
                    include_blobs=False,
                    cancel_check=check_canceled,
                )
        check_canceled()
        return VirtualViewResult(
            root=root,
            kind=kind,
            value=value,
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=images,
            duration_ms=(monotonic() - started_at) * 1000,
        )

    def _thumbnail_record_key(self, record: PaneRecord) -> tuple[str, str]:
        if isinstance(record, DirectoryRecord):
            return ("directory", record.dir_rel)
        return ("image", record.rel_path)

    def _pending_delete_image_rels(self, root: Path) -> set[str]:
        resolved_root = root.expanduser().resolve()
        rel_paths: set[str] = set()
        for delete_task in self._delete_payload_tasks:
            if delete_task.root != resolved_root or delete_task.future.done():
                continue
            rel_paths.update(delete_task.rel_paths)
        return rel_paths

    def _without_pending_delete_records(
        self,
        root: Path,
        records: Sequence[PaneRecord],
    ) -> list[PaneRecord]:
        resolved_root = root.expanduser().resolve()
        pending_images = self._pending_delete_image_rels(resolved_root)
        pending_directories: set[str] = set()
        for move_task in self._move_payload_tasks:
            if move_task.future.done():
                continue
            pending_images.update(
                rel_path
                for source_root, rel_path in move_task.source_images
                if source_root == resolved_root
            )
            pending_directories.update(
                rel_path
                for source_root, rel_path in move_task.source_directories
                if source_root == resolved_root
            )
        if not pending_images and not pending_directories:
            return list(records)

        def inside_pending_directory(rel_path: str) -> bool:
            return any(
                rel_path == directory_rel or rel_path.startswith(f"{directory_rel}/")
                for directory_rel in pending_directories
            )

        return [
            record
            for record in records
            if (
                isinstance(record, ImageRecord)
                and record.rel_path not in pending_images
                and not inside_pending_directory(record.rel_path)
            )
            or (
                isinstance(record, DirectoryRecord)
                and not inside_pending_directory(record.dir_rel)
            )
        ]

    def _thumbnail_selection_keys(self) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for index in self.thumbnail_view.selectedIndexes():
            if index.isValid() and index.row() < len(self.model.images):
                keys.add(self._thumbnail_record_key(self.model.images[index.row()]))
        return keys

    def _current_thumbnail_selection_key(self) -> tuple[str, str] | None:
        current = self.thumbnail_view.currentIndex()
        if not current.isValid() or current.row() >= len(self.model.images):
            return None
        return self._thumbnail_record_key(self.model.images[current.row()])

    def _restore_thumbnail_selection(
        self,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
    ) -> None:
        selection = self.thumbnail_view.selectionModel()
        if selection is None:
            return
        matching_rows = [
            row
            for row, record in enumerate(self.model.images)
            if self._thumbnail_record_key(record) in selection_keys
            or self._thumbnail_record_key(record) == current_key
        ]
        if matching_rows:
            self.model.ensure_row_loaded(max(matching_rows))
        selection.clearSelection()
        current_index = QModelIndex()
        for row, record in enumerate(self.model.images):
            key = self._thumbnail_record_key(record)
            if key not in selection_keys and key != current_key:
                continue
            index = self.model.index(row, 0)
            if key in selection_keys:
                selection.select(index, QItemSelectionModel.SelectionFlag.Select)
            if key == current_key:
                current_index = index
        if current_index.isValid():
            selection.setCurrentIndex(current_index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _load_very_similar_virtual_directory(
        self,
        *,
        preserve_selection: bool,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> None:
        if self.current_catalog is None:
            return
        catalog = self.current_catalog
        fingerprint = catalog.catalog_database_mtime_ns()
        cache_key = self._very_similar_cache_key(catalog.root, self.current_sort, fingerprint)
        cached_images = self._very_similar_cache.get(cache_key)
        if cached_images is not None:
            self.model.set_images(
                catalog,
                self._without_pending_delete_records(catalog.root, cached_images),
            )
            self.refresh_thumbnail_layout()
            if preserve_selection:
                self._restore_thumbnail_selection(selection_keys, current_key)
            self._restore_thumbnail_scroll_position(scroll_key)
            self.update_selection_status()
            return

        task = self._matching_virtual_view_task(
            catalog.root,
            VIRTUAL_KIND_VERY_SIMILAR,
            "",
            self.current_sort,
            fingerprint,
        )
        if task is None:
            cancel_event = Event()
            future = self.virtual_view_executor.submit(
                self._very_similar_virtual_view_worker,
                catalog.root,
                self.current_sort.value,
                fingerprint,
                cancel_event,
            )
            task = VirtualViewTask(
                root=catalog.root,
                kind=VIRTUAL_KIND_VERY_SIMILAR,
                value="",
                sort_order=self.current_sort,
                fingerprint=fingerprint,
                future=future,
                started_at=monotonic(),
                selection_keys=set(selection_keys),
                current_key=current_key,
                scroll_key=scroll_key,
                cancel_event=cancel_event,
            )
            self._virtual_view_tasks[future] = task
        elif preserve_selection:
            task.selection_keys = set(selection_keys)
            task.current_key = current_key
            task.scroll_key = scroll_key

        self.model.set_images(catalog, [])
        self.refresh_thumbnail_layout()
        self._restore_thumbnail_scroll_position(scroll_key)
        self.update_selection_status()
        self._show_virtual_view_status(task)

    def _very_similar_virtual_view_worker(
        self,
        root: Path,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        with Catalog(root) as catalog:
            images = catalog.list_very_similar_images(
                sort_order,
                include_blobs=False,
                cancel_check=check_canceled,
            )
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_VERY_SIMILAR,
            value="",
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=images,
            duration_ms=(monotonic() - started_at) * 1000,
        )

    def _very_similar_cache_key(
        self,
        root: Path,
        sort_order: SortOrder,
        fingerprint: int,
    ) -> tuple[Path, str, int]:
        return (root.expanduser().resolve(), sort_order.value, fingerprint)

    def _drop_very_similar_cache(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        self._very_similar_cache = {
            key: images for key, images in self._very_similar_cache.items() if key[0] != resolved
        }

    def _matching_virtual_view_task(
        self,
        root: Path,
        kind: str,
        value: str,
        sort_order: SortOrder,
        fingerprint: int,
    ) -> VirtualViewTask | None:
        resolved = root.expanduser().resolve()
        for task in self._virtual_view_tasks.values():
            if (
                task.root == resolved
                and task.kind == kind
                and task.value == value
                and task.sort_order == sort_order
                and task.fingerprint == fingerprint
            ):
                return task
        return None

    def _cancel_virtual_view_tasks(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        for future, task in list(self._virtual_view_tasks.items()):
            if task.root != resolved:
                continue
            task.cancel_event.set()
            if future.cancel():
                self._virtual_view_tasks.pop(future, None)

    def _settle_virtual_view_tasks(self) -> None:
        for future, task in list(self._virtual_view_tasks.items()):
            if not future.done():
                continue
            self._virtual_view_tasks.pop(future, None)
            if future.cancelled() or task.cancel_event.is_set():
                continue
            try:
                result = future.result()
            except Exception as error:
                if self._is_current_virtual_task(task):
                    self.progress_bar.setRange(0, 1)
                    self.progress_bar.setValue(0)
                    self.progress_label.setText("Ready")
                    if task.kind == VIRTUAL_KIND_VERY_SIMILAR:
                        title = "Very Similar"
                    elif task.kind == VIRTUAL_KIND_PHYSICAL:
                        title = "Load Folder"
                    else:
                        title = "Virtual Directory"
                    show_error(self, title, str(error))
                continue

            if result.kind == VIRTUAL_KIND_VERY_SIMILAR:
                cache_key = self._very_similar_cache_key(result.root, result.sort_order, result.fingerprint)
                self._very_similar_cache = {
                    key: images
                    for key, images in self._very_similar_cache.items()
                    if key[0] != cache_key[0] or key[1] != cache_key[1]
                }
                self._very_similar_cache[cache_key] = list(result.images)
                self._append_timing_event(
                    result.root,
                    "very_similar_virtual_view",
                    result.duration_ms,
                    {"images": len(result.images)},
                )
            if not self._is_current_virtual_result(result):
                continue
            catalog = self.current_catalog
            if catalog is None:
                continue
            current_fingerprint = catalog.catalog_database_mtime_ns()
            if current_fingerprint != result.fingerprint:
                self.load_current_directory(preserve_selection=True)
                continue
            self.model.set_images(
                catalog,
                self._without_pending_delete_records(catalog.root, result.images),
            )
            self.refresh_thumbnail_layout()
            self._restore_thumbnail_selection(task.selection_keys, task.current_key)
            self._restore_thumbnail_scroll_position(task.scroll_key)
            self.update_selection_status()

    def _active_virtual_view_task(self) -> VirtualViewTask | None:
        active_tasks = [
            task
            for task in self._virtual_view_tasks.values()
            if self._is_current_virtual_task(task)
        ]
        if not active_tasks:
            return None
        return sorted(active_tasks, key=lambda task: task.started_at)[0]

    def _is_current_virtual_task(self, task: VirtualViewTask) -> bool:
        if task.kind == VIRTUAL_KIND_PHYSICAL:
            return (
                self.current_catalog is not None
                and self.current_catalog.root == task.root
                and self.current_virtual_kind is None
                and self.current_dir_rel == task.value
                and self.current_sort == task.sort_order
            )
        return (
            self.current_catalog is not None
            and self.current_catalog.root == task.root
            and self.current_virtual_kind == task.kind
            and self.current_virtual_value == task.value
            and self.current_sort == task.sort_order
        )

    def _is_current_virtual_result(self, result: VirtualViewResult) -> bool:
        if result.kind == VIRTUAL_KIND_PHYSICAL:
            return (
                self.current_catalog is not None
                and self.current_catalog.root == result.root
                and self.current_virtual_kind is None
                and self.current_dir_rel == result.value
                and self.current_sort == result.sort_order
            )
        return (
            self.current_catalog is not None
            and self.current_catalog.root == result.root
            and self.current_virtual_kind == result.kind
            and self.current_virtual_value == result.value
            and self.current_sort == result.sort_order
        )

    def _show_virtual_view_status(self, task: VirtualViewTask) -> None:
        elapsed = int(monotonic() - task.started_at)
        self.progress_bar.setRange(0, 0)
        if task.kind == VIRTUAL_KIND_VERY_SIMILAR:
            self.progress_label.setText(f"Building Very Similar view ({elapsed}s)")
        elif task.kind == VIRTUAL_KIND_PHYSICAL:
            self.progress_label.setText(f"Loading large folder ({elapsed}s)")
        else:
            self.progress_label.setText("Building virtual directory")

    def _cancel_active_duplicate_delete_task(self, *, wait: bool = False) -> None:
        task = self._duplicate_delete_task
        if task is None:
            return
        self._cancel_duplicate_delete_task(task.root, wait=wait)

    def _cancel_duplicate_delete_task(self, root: Path, *, wait: bool = False) -> None:
        task = self._duplicate_delete_task
        if task is None or task.root != root.expanduser().resolve():
            return
        task.task.cancel()
        if wait:
            self._wait_for_duplicate_delete_task(task)
        else:
            self._settle_duplicate_delete_task()
        self._update_tools_menu_actions()

    def _wait_for_duplicate_delete_task(self, task: DuplicateDeleteTask) -> None:
        if not task.future.done():
            with suppress(Exception):
                task.future.result()
        self._settle_duplicate_delete_task()

    def _settle_duplicate_delete_task(self) -> None:
        delete_task = self._duplicate_delete_task
        if delete_task is None or not delete_task.future.done():
            return
        self._duplicate_delete_task = None
        self._update_tools_menu_actions()
        snapshot = delete_task.task.snapshot()
        if delete_task.future.cancelled() or snapshot.canceled:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            return
        try:
            result = delete_task.future.result()
        except IndexTaskCancelled:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            return
        except Exception as error:
            self._drop_very_similar_cache(delete_task.root)
            self._swept_catalog_roots.discard(delete_task.root)
            self._pruned_catalog_roots.discard(delete_task.root)
            if self.current_catalog is not None and self.current_catalog.root == delete_task.root:
                self.rebuild_tree()
                self.load_current_directory(preserve_selection=True)
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            show_error(self, "Automatically Delete Duplicates", str(error))
            return

        self._drop_very_similar_cache(delete_task.root)
        self._swept_catalog_roots.discard(delete_task.root)
        self._pruned_catalog_roots.discard(delete_task.root)
        if self.current_catalog is not None and self.current_catalog.root == delete_task.root:
            self.rebuild_tree()
            self.load_current_directory(preserve_selection=True)
        self.progress_bar.setRange(0, max(result.planned_delete_count, 1))
        self.progress_bar.setValue(min(result.deleted, max(result.planned_delete_count, 1)))
        self.progress_label.setText(
            f"Moved {result.deleted} duplicate image(s) to {TRASH_DIR_NAME} from {result.groups} group(s)"
        )

    def _active_duplicate_delete_task(self) -> DuplicateDeleteTask | None:
        task = self._duplicate_delete_task
        if task is None:
            return None
        if task.future.done():
            return None
        return task

    def _has_active_duplicate_delete_task(self) -> bool:
        return self._active_duplicate_delete_task() is not None

    def _show_duplicate_delete_status(self, snapshot: IndexProgressSnapshot) -> None:
        if snapshot.total is None:
            self.progress_bar.setRange(0, 0)
            detail = snapshot.current or "Finding duplicate groups"
        else:
            self.progress_bar.setRange(0, max(snapshot.total, 1))
            self.progress_bar.setValue(min(snapshot.processed, max(snapshot.total, 1)))
            detail = f"{snapshot.processed}/{snapshot.total}"
            if snapshot.current:
                detail = f"{detail}: {snapshot.current}"
        self.progress_label.setText(f"Moving duplicates to {TRASH_DIR_NAME}: {detail}")

    def _cancel_active_delete_payload_task(self, *, wait: bool = False) -> None:
        tasks = list(self._delete_payload_tasks)
        if not tasks:
            self._refresh_active_delete_payload_task()
            return
        for task in tasks:
            task.task.cancel()
        if wait:
            for task in tasks:
                self._wait_for_delete_payload_task(task)
        else:
            self._settle_delete_payload_task()
        self._update_tools_menu_actions()

    def _wait_delete_payload_tasks_for_root(self, root: Path) -> None:
        resolved = root.expanduser().resolve()
        for task in [item for item in self._delete_payload_tasks if item.root == resolved]:
            self._wait_for_delete_payload_task(task)

    def _wait_for_delete_payload_task(self, task: DeletePayloadTask) -> None:
        if not task.future.done():
            with suppress(Exception):
                task.future.result()
        self._settle_delete_payload_task()

    def _settle_delete_payload_task(self) -> None:
        completed = [task for task in self._delete_payload_tasks if task.future.done()]
        if not completed:
            self._refresh_active_delete_payload_task()
            return
        self._update_tools_menu_actions()
        last_result: DeletePayloadResult | None = None
        last_error: BaseException | None = None
        canceled = False
        affected_roots: set[Path] = set()
        for delete_task in completed:
            self._delete_payload_tasks.remove(delete_task)
            affected_roots.add(delete_task.root)
            if delete_task.future.cancelled():
                canceled = True
                continue
            try:
                result = delete_task.future.result()
            except IndexTaskCancelled:
                canceled = True
                continue
            except Exception as error:
                last_error = error
                continue
            affected_roots.update(result.affected_roots)
            last_result = result
        self._refresh_active_delete_payload_task()
        self._update_tools_menu_actions()
        if affected_roots:
            self._refresh_after_file_delete(affected_roots)
        if last_error is not None:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            show_error(self, "Delete", str(last_error))
            return
        if last_result is not None:
            self.progress_bar.setRange(0, max(last_result.requested, 1))
            self.progress_bar.setValue(min(last_result.requested, max(last_result.requested, 1)))
            self.progress_label.setText(f"Deleted {last_result.deleted} image(s)")
            return
        if canceled and not self._delete_payload_tasks:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")

    def _refresh_after_file_delete(self, affected_roots: set[Path]) -> None:
        for root in affected_roots:
            resolved_root = root.expanduser().resolve()
            self._swept_catalog_roots.discard(resolved_root)
            self._pruned_catalog_roots.discard(resolved_root)
            self._drop_very_similar_cache(resolved_root)
        self.reload_tree_and_directory(preserve_tree_scroll=True)

    def _active_delete_payload_task(self) -> DeletePayloadTask | None:
        self._refresh_active_delete_payload_task()
        return self._delete_payload_task

    def _has_pending_delete_payload_tasks(self) -> bool:
        self._refresh_active_delete_payload_task()
        return bool(self._delete_payload_tasks)

    def _refresh_active_delete_payload_task(self) -> None:
        self._delete_payload_task = next(
            (task for task in self._delete_payload_tasks if not task.future.done()),
            None,
        )

    def _show_delete_payload_status(self, snapshot: IndexProgressSnapshot) -> None:
        if snapshot.total is None:
            self.progress_bar.setRange(0, 0)
            detail = snapshot.current or "Preparing delete"
        else:
            total = max(snapshot.total, 1)
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(snapshot.processed, total))
            detail = f"{snapshot.processed}/{snapshot.total}"
            if snapshot.current:
                detail = f"{detail}: {snapshot.current}"
        self.progress_label.setText(f"Deleting images: {detail}")

    def _cancel_active_move_payload_task(self, *, wait: bool = False) -> None:
        tasks = list(self._move_payload_tasks)
        if not tasks:
            self._refresh_active_move_payload_task()
            return
        for task in tasks:
            task.task.cancel()
        if wait:
            for task in tasks:
                self._wait_for_move_payload_task(task)
        else:
            self._settle_move_payload_task()
        self._update_tools_menu_actions()

    def _cancel_move_payload_task(self, root: Path, *, wait: bool = False) -> None:
        resolved = root.expanduser().resolve()
        tasks = [task for task in self._move_payload_tasks if resolved in task.affected_roots]
        if not tasks:
            self._refresh_active_move_payload_task()
            return
        for task in tasks:
            task.task.cancel()
        if wait:
            for task in tasks:
                self._wait_for_move_payload_task(task)
        else:
            self._settle_move_payload_task()
        self._update_tools_menu_actions()

    def _wait_for_move_payload_task(self, task: MovePayloadTask) -> None:
        if not task.future.done():
            with suppress(Exception):
                task.future.result()
        self._settle_move_payload_task()

    def _settle_move_payload_task(self) -> None:
        completed = [task for task in self._move_payload_tasks if task.future.done()]
        if not completed:
            self._refresh_active_move_payload_task()
            return
        self._update_tools_menu_actions()
        last_result: MovePayloadResult | None = None
        last_error: BaseException | None = None
        last_error_title = "Move"
        last_completion_verb = "Moved"
        canceled = False
        affected_roots: set[Path] = set()
        created_directories: list[tuple[Path, str]] = []
        for move_task in completed:
            self._move_payload_tasks.remove(move_task)
            affected_roots.update(move_task.affected_roots)
            if move_task.future.cancelled():
                canceled = True
                continue
            try:
                result = move_task.future.result()
            except IndexTaskCancelled:
                canceled = True
                continue
            except Exception as error:
                last_error = error
                last_error_title = move_task.error_title
                continue
            affected_roots.update(result.affected_roots)
            last_result = result
            last_completion_verb = move_task.completion_verb
            if result.created_dir_rel is not None:
                created_directories.append((move_task.dest_root, result.created_dir_rel))
        self._refresh_active_move_payload_task()
        if affected_roots:
            self._refresh_after_move_payload(affected_roots)
        for root, dir_rel in created_directories:
            catalog = self.workspace.catalog_for_root(root)
            if catalog is not None:
                self.queue_directory_index(catalog, dir_rel, interactive=False)
        if last_error is not None:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            show_error(self, last_error_title, str(last_error))
            return
        if last_result is not None:
            self.progress_bar.setRange(0, max(last_result.requested, 1))
            self.progress_bar.setValue(min(last_result.requested, max(last_result.requested, 1)))
            self.progress_label.setText(f"{last_completion_verb} {last_result.moved} item(s)")
            return
        if canceled and not self._move_payload_tasks:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")

    def _refresh_after_move_payload(self, affected_roots: set[Path]) -> None:
        for root in affected_roots:
            resolved_root = root.expanduser().resolve()
            self._swept_catalog_roots.discard(resolved_root)
            self._pruned_catalog_roots.discard(resolved_root)
            self._drop_very_similar_cache(resolved_root)
        self.reload_tree_and_directory(preserve_tree_scroll=True)

    def _active_move_payload_task(self) -> MovePayloadTask | None:
        self._refresh_active_move_payload_task()
        return self._move_payload_task

    def _has_pending_move_payload_tasks(self) -> bool:
        self._refresh_active_move_payload_task()
        return bool(self._move_payload_tasks)

    def _refresh_active_move_payload_task(self) -> None:
        self._move_payload_task = next(
            (task for task in self._move_payload_tasks if not task.future.done()),
            None,
        )

    def _show_move_payload_status(self, snapshot: IndexProgressSnapshot) -> None:
        if snapshot.total is None:
            self.progress_bar.setRange(0, 0)
            detail = snapshot.current or "Preparing move"
        else:
            total = max(snapshot.total, 1)
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(snapshot.processed, total))
            detail = f"{snapshot.processed}/{snapshot.total}"
            if snapshot.current:
                detail = f"{detail}: {snapshot.current}"
        self.progress_label.setText(f"{snapshot.label}: {detail}")

    def _thumbnail_size_changed(self, value: int) -> None:
        self.set_thumbnail_size(value)

    def set_thumbnail_size(self, value: int) -> None:
        value = max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, int(value)))
        if value < self.size_slider.minimum() or value > self.size_slider.maximum():
            self.size_slider.setRange(min(self.size_slider.minimum(), value), max(self.size_slider.maximum(), value))
        if self.size_slider.value() != value:
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(value)
            self.size_slider.blockSignals(False)
        self.thumbnail_columns = value
        self.app_config.thumbnail_size = value
        self.refresh_thumbnail_layout()

    def refresh_thumbnail_layout(self) -> None:
        device_pixel_ratio = widget_device_pixel_ratio(self.thumbnail_view)
        self.model.set_device_pixel_ratio(device_pixel_ratio)
        grid_size, logical_tile_size = self.thumbnail_grid_size_for_width(
            self.stable_thumbnail_layout_width(),
        )
        physical_tile_size = max(1, int(round(logical_tile_size * device_pixel_ratio)))
        if self.model.tile_size != physical_tile_size:
            self.model.set_tile_size(physical_tile_size)
        logical_tile_size = self.model.logical_tile_size()
        icon_size = QSize(logical_tile_size, logical_tile_size)
        if self.thumbnail_view.iconSize() != icon_size:
            self.thumbnail_view.setIconSize(icon_size)
        if self.thumbnail_view.gridSize() != grid_size:
            self.thumbnail_view.setGridSize(grid_size)

    def stable_thumbnail_layout_width(self) -> int:
        viewport_width = max(1, int(self.thumbnail_view.viewport().width()))
        columns = max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, self.thumbnail_columns))
        if self.model.rowCount() <= columns:
            return viewport_width
        if self.thumbnail_view.verticalScrollBar().isVisible():
            return viewport_width
        scrollbar_width = max(0, int(self.thumbnail_view.verticalScrollBar().sizeHint().width()))
        return max(1, viewport_width - scrollbar_width)

    def thumbnail_grid_size_for_width(self, available_width: int) -> tuple[QSize, int]:
        columns = max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, self.thumbnail_columns))
        available_width = max(1, int(available_width))
        grid_width = max(1, available_width // columns)
        font_metrics = QFontMetrics(self.thumbnail_view.font())
        inset = 2 * self.model.CARD_PADDING
        logical_tile_size = max(24, grid_width - inset)
        grid_height = logical_tile_size + font_metrics.height() + self.model.LABEL_GAP + inset
        return QSize(grid_width, grid_height), logical_tile_size

    def set_thumbnail_size_to_native(self) -> None:
        self.set_thumbnail_size(DEFAULT_THUMBNAIL_COLUMNS)

    def _sort_changed(self) -> None:
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        self.set_sort_order(SortOrder(self.sort_combo.currentData()))
        self.load_current_directory(preserve_selection=True)

    def set_sort_order(self, sort_order: SortOrder) -> None:
        self.current_sort = sort_order
        self.app_config.sort_order = sort_order.value
        index = self.sort_combo.findData(sort_order.value)
        if index >= 0 and self.sort_combo.currentIndex() != index:
            self.sort_combo.blockSignals(True)
            self.sort_combo.setCurrentIndex(index)
            self.sort_combo.blockSignals(False)

    def selected_records(self) -> list[PaneRecord]:
        rows = sorted({index.row() for index in self.thumbnail_view.selectedIndexes()})
        return [self.model.images[row] for row in rows if row < len(self.model.images)]

    def selected_rel_paths(self) -> list[str]:
        return [record.rel_path for record in self.selected_records() if isinstance(record, ImageRecord)]

    def thumbnail_keyboard_activation_index(self) -> QModelIndex:
        selection = self.thumbnail_view.selectionModel()
        current = self.thumbnail_view.currentIndex()
        if (
            current.isValid()
            and current.row() < len(self.model.images)
            and selection is not None
            and selection.isSelected(current)
        ):
            return current
        selected_rows = sorted({index.row() for index in self.thumbnail_view.selectedIndexes()})
        for row in selected_rows:
            if row < len(self.model.images):
                return self.model.index(row, 0)
        for row, record in enumerate(self.model.images):
            if isinstance(record, ImageRecord):
                self.model.ensure_row_loaded(row)
                return self.model.index(row, 0)
        return QModelIndex()

    def current_selected_row(self) -> int | None:
        current = self.thumbnail_view.currentIndex()
        if current.isValid() and current.row() < len(self.model.images):
            if self.thumbnail_view.selectionModel().isSelected(current):
                return current.row()
        selected_rows = sorted({index.row() for index in self.thumbnail_view.selectedIndexes()})
        for row in selected_rows:
            if row < len(self.model.images):
                return row
        return None

    def update_selection_status(self) -> None:
        if self.current_catalog is None:
            self.status_left_label.setText("-")
            return
        total = sum(1 for record in self.model.images if isinstance(record, ImageRecord))
        row = self.current_selected_row()
        if row is None:
            text = str(total)
        else:
            record = self.model.images[row]
            if isinstance(record, DirectoryRecord):
                text = str(total)
            else:
                image_records = [item for item in self.model.images if isinstance(item, ImageRecord)]
                ordinal = image_records.index(record) + 1 if record in image_records else row + 1
                text = (
                    f"{ordinal} / {total} "
                    f"[{record.width}x{record.height} - {format_bytes(record.size_bytes)}]"
                )
        self.status_left_label.setText(text)

    def copy_selected_files(self) -> None:
        if self.current_catalog is None:
            return
        paths = [self.current_catalog.abs_path(rel_path) for rel_path in self.selected_rel_paths()]
        copy_files_to_clipboard(paths)

    def delete_selected(
        self,
        *,
        catalog: Catalog | None = None,
        rel_paths: Sequence[str] | None = None,
    ) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None:
            return
        selected_rel_paths = list(rel_paths) if rel_paths is not None else self.selected_rel_paths()
        if not selected_rel_paths:
            return
        if not ask_delete_files(self, len(selected_rel_paths)):
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        self.queue_delete_images(
            catalog,
            selected_rel_paths,
            wipe=self.wipe_on_delete_enabled(),
            remove_from_current_view=True,
        )

    def queue_delete_images(
        self,
        catalog: Catalog,
        rel_paths: Sequence[str],
        *,
        wipe: bool,
        remove_from_current_view: bool,
    ) -> None:
        unique_rel_paths = list(dict.fromkeys(rel_paths))
        if not unique_rel_paths:
            return
        root = catalog.root
        self.indexer.cancel_idle_tasks(root)
        self.indexer.cancel_directory_tasks(root)
        self._swept_catalog_roots.discard(root)
        self._pruned_catalog_roots.discard(root)
        self._drop_very_similar_cache(root)
        task, future = self.indexer.submit_action(
            "Deleting images",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda action_task: self._delete_images_worker(
                root,
                unique_rel_paths,
                wipe,
                action_task,
            ),
            key=f"delete:{root}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
        )
        delete_task = DeletePayloadTask(
            root=root,
            rel_paths=tuple(unique_rel_paths),
            task=task,
            future=future,
            started_at=monotonic(),
        )
        self._delete_payload_tasks.append(delete_task)
        self._refresh_active_delete_payload_task()
        if remove_from_current_view:
            self._remove_records_from_current_view(root, image_rels=set(unique_rel_paths))
        if self._delete_payload_task is not None:
            self._show_delete_payload_status(self._delete_payload_task.task.snapshot())
        self._update_tools_menu_actions()

    def _delete_images_worker(
        self,
        root: Path,
        rel_paths: Sequence[str],
        wipe: bool,
        task: IndexTask,
    ) -> DeletePayloadResult:
        try:
            with Catalog(root) as catalog:
                deleted = catalog.delete_images(
                    rel_paths,
                    wipe=wipe,
                    progress_callback=task.update,
                    cancel_check=task.check_canceled,
                )
            task.mark_done()
            return DeletePayloadResult(
                requested=len(rel_paths),
                deleted=deleted,
                affected_roots={root.expanduser().resolve()},
            )
        except IndexTaskCancelled:
            task.mark_canceled()
            raise
        except Exception as error:
            task.mark_failed(error)
            raise

    def _open_thumbnail_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        if self.current_catalog is None:
            return
        catalog = self.current_catalog
        index = self.thumbnail_view.indexAt(pos)
        if not index.isValid() or index.row() >= len(self.model.images):
            return
        if not self.thumbnail_view.selectionModel().isSelected(index):
            self.thumbnail_view.selectionModel().clearSelection()
            self.thumbnail_view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)
            self.thumbnail_view.setCurrentIndex(index)
        record = self.model.images[index.row()]
        selected_records = list(self.selected_records())
        selected_rel_paths = [
            item.rel_path for item in selected_records if isinstance(item, ImageRecord)
        ]
        menu = QMenu(self)
        actions = self._thumbnail_context_menu_actions(menu, record)
        selected = menu.exec(self.thumbnail_view.viewport().mapToGlobal(pos))
        if selected is None:
            return
        if selected == actions.get("restore"):
            self.restore_selected_trash_records(catalog=catalog, records=selected_records)
        elif selected == actions.get("open") and isinstance(record, DirectoryRecord):
            self.navigate_to_directory(record.dir_rel, catalog=catalog)
        elif selected == actions.get("properties") and isinstance(record, DirectoryRecord):
            self.open_directory_properties(catalog.root, record.dir_rel)
        elif selected == actions.get("delete_directory") and isinstance(record, DirectoryRecord):
            self.delete_directory(catalog.root, record.dir_rel)
        elif selected == actions.get("list_duplicates") and isinstance(record, ImageRecord):
            self.open_duplicate_list_dialog(record, catalog=catalog)
        elif selected == actions.get("delete"):
            self.delete_selected(catalog=catalog, rel_paths=selected_rel_paths)
        elif selected == actions.get("metadata") and isinstance(record, ImageRecord):
            MetadataDialog(record.absolute_path, self).exec()

    def _thumbnail_context_menu_actions(self, menu: QMenu, record: PaneRecord) -> dict[str, QAction]:
        actions: dict[str, QAction] = {}
        if self.is_restorable_trash_record(record):
            actions["restore"] = menu.addAction("Restore")
            menu.addSeparator()
        if isinstance(record, DirectoryRecord):
            actions["open"] = menu.addAction("Open")
            actions["properties"] = menu.addAction("Properties")
            if record.dir_rel:
                actions["delete_directory"] = menu.addAction("Delete Directory")
            return actions
        actions["list_duplicates"] = menu.addAction("List Duplicates")
        actions["delete"] = menu.addAction("Delete")
        actions["metadata"] = menu.addAction("Metadata")
        return actions

    def open_duplicate_list_dialog(self, record: ImageRecord, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None:
            return
        matches = catalog.duplicate_matches_for_image(
            record.rel_path,
            SortOrder.NAME_ASC,
            include_blobs=False,
        )
        DuplicateListDialog(
            catalog,
            record,
            matches,
            lambda rel_path: self.navigate_to_image(rel_path, catalog=catalog),
            self,
        ).exec()

    def open_tag_dialog_for_selection(self) -> None:
        if self.current_catalog is None:
            return
        rel_paths = self.selected_rel_paths()
        if len(rel_paths) != 1:
            return
        catalog = self.current_catalog
        rel_path = rel_paths[0]
        dialog = TagDialog(catalog, rel_path, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.workspace.catalog_for_root(catalog.root) is not catalog:
                return
            catalog.set_image_tags(rel_path, dialog.selected_tags(), replace=True)
            self.rebuild_tree()
            if self.current_catalog is catalog and self.current_virtual_kind is not None:
                self.load_current_directory(preserve_selection=True)

    def open_viewer(self, index: QModelIndex, *, random_mode: bool) -> None:
        if self.current_catalog is None:
            return
        if not index.isValid() or index.row() >= len(self.model.images):
            index = self.thumbnail_keyboard_activation_index()
        if not index.isValid() or index.row() >= len(self.model.images):
            return
        selected_record = self.model.images[index.row()]
        if isinstance(selected_record, DirectoryRecord):
            self.navigate_to_directory(selected_record.dir_rel)
            return
        order = [record.rel_path for record in self.model.images if isinstance(record, ImageRecord)]
        start = selected_record.rel_path
        navigator = (
            ImageNavigator.random(order, start)
            if random_mode
            else ImageNavigator.sequential(order, start)
        )
        catalog = self.current_catalog
        viewer = FullscreenViewer(catalog, navigator, self, wipe_on_delete=self.wipe_on_delete_enabled())
        viewer.showFullScreen()
        viewer.exec()
        last_viewed = viewer.last_viewed_rel_path
        if self.current_catalog is catalog:
            self.load_current_directory()
            self.select_rel_path(last_viewed)

    def navigate_to_directory(self, dir_rel: str, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        self._catalog_intent_root = catalog.root
        self.current_catalog = catalog
        self.current_dir_rel = dir_rel
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self.load_current_directory()
        self.queue_directory_index(catalog, dir_rel)
        self.rebuild_tree()

    def navigate_to_image(self, rel_path: str, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        self._catalog_intent_root = catalog.root
        self.current_catalog = catalog
        dir_rel = Path(rel_path).parent.as_posix()
        self.current_dir_rel = "" if dir_rel == "." else dir_rel
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self.rebuild_tree()
        self.load_current_directory()
        self.select_rel_path(rel_path)

    def move_payload_to_directory(self, payload: object, dest_root: Path, dest_dir_rel: str) -> None:
        self._settle_move_payload_task()
        dest_catalog = self.workspace.catalog_for_root(dest_root)
        if dest_catalog is None:
            return
        try:
            dest_catalog.abs_path(dest_dir_rel) if dest_dir_rel else dest_catalog.root
        except ValueError:
            return
        if not isinstance(payload, list):
            return
        image_groups: dict[Path, list[str]] = defaultdict(list)
        directory_groups: dict[Path, list[str]] = defaultdict(list)
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                source_root = Path(item["catalog_root"]).resolve()
                rel_path = item["rel_path"]
            except (KeyError, OSError):
                continue
            if not isinstance(rel_path, str):
                continue
            source_catalog = self.workspace.catalog_for_root(source_root)
            if source_catalog is None:
                continue
            kind = item.get("kind", "image")
            if kind not in {"image", "directory"}:
                continue
            try:
                source_path = source_catalog.abs_path(rel_path)
            except ValueError:
                continue
            if kind == "directory":
                if not rel_path or not source_path.is_dir():
                    continue
                group = directory_groups
            else:
                if not is_image_name(Path(rel_path).name) or not source_path.is_file():
                    continue
                group = image_groups
            group[source_root].append(rel_path)
        image_payload = {
            root: list(dict.fromkeys(rel_paths))
            for root, rel_paths in image_groups.items()
            if rel_paths
        }
        directory_payload = {
            root: sorted(set(dir_rels), key=lambda value: value.count("/"))
            for root, dir_rels in directory_groups.items()
            if dir_rels
        }
        if not image_payload and not directory_payload:
            return
        if is_trash_rel_path(dest_dir_rel):
            source_roots = {*directory_payload.keys(), *image_payload.keys()}
            if any(source_root != dest_catalog.root for source_root in source_roots):
                show_error(self, "Move", "Cannot move items into another catalog's trash.")
                return
        affected_roots = {dest_catalog.root, *directory_payload.keys(), *image_payload.keys()}
        for root in affected_roots:
            self.indexer.cancel_idle_tasks(root)
            self.indexer.cancel_directory_tasks(root)
        cross_catalog = any(root != dest_catalog.root for root in affected_roots)
        priority = (
            ActionPriority.FILE_MOVE_CROSS_CATALOG
            if cross_catalog
            else ActionPriority.FILE_MOVE_WITHIN_CATALOG
        )
        task, future = self.indexer.submit_action(
            "Moving items",
            dest_catalog.root,
            dest_dir_rel,
            priority=priority,
            worker=lambda action_task: self._move_payload_worker(
                image_payload,
                directory_payload,
                dest_catalog.root,
                dest_dir_rel,
                self.wipe_on_delete_enabled(),
                action_task,
            ),
            key=f"move:{dest_catalog.root}:{dest_dir_rel}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
        )
        move_task = MovePayloadTask(
            dest_root=dest_catalog.root,
            dest_dir_rel=dest_dir_rel,
            affected_roots=set(affected_roots),
            task=task,
            future=future,
            started_at=monotonic(),
            source_images=tuple(
                (root, rel_path)
                for root, rel_paths in image_payload.items()
                for rel_path in rel_paths
            ),
            source_directories=tuple(
                (root, rel_path)
                for root, rel_paths in directory_payload.items()
                for rel_path in rel_paths
            ),
        )
        self._move_payload_tasks.append(move_task)
        self._refresh_active_move_payload_task()
        if self.current_catalog is not None:
            current_root = self.current_catalog.root
            for source_dir_rel in directory_payload.get(current_root, ()):
                if self.current_dir_rel == source_dir_rel or self.current_dir_rel.startswith(f"{source_dir_rel}/"):
                    parent_rel = Path(source_dir_rel).parent.as_posix()
                    self.current_dir_rel = "" if parent_rel == "." else parent_rel
                    self.current_virtual_kind = None
                    self.current_virtual_value = ""
                    break
        self._remove_queued_move_records_from_current_view(image_payload, directory_payload)
        if self._move_payload_task is not None:
            self._show_move_payload_status(self._move_payload_task.task.snapshot())
        self._update_tools_menu_actions()

    def _remove_queued_move_records_from_current_view(
        self,
        image_payload: dict[Path, list[str]],
        directory_payload: dict[Path, list[str]],
    ) -> None:
        if self.current_catalog is None:
            return
        root = self.current_catalog.root
        self._remove_records_from_current_view(
            root,
            image_rels=set(image_payload.get(root, ())),
            directory_rels=set(directory_payload.get(root, ())),
        )

    def _move_payload_worker(
        self,
        image_groups: dict[Path, list[str]],
        directory_groups: dict[Path, list[str]],
        dest_root: Path,
        dest_dir_rel: str,
        wipe_on_delete: bool,
        task: IndexTask,
    ) -> MovePayloadResult:
        affected_roots = {dest_root, *directory_groups.keys(), *image_groups.keys()}
        requested = sum(len(items) for items in directory_groups.values()) + sum(
            len(items) for items in image_groups.values()
        )
        processed = 0
        moved = 0
        catalogs: dict[Path, Catalog] = {}

        def catalog_for(root: Path) -> Catalog:
            resolved = root.expanduser().resolve()
            catalog = catalogs.get(resolved)
            if catalog is None:
                catalog = Catalog(resolved)
                catalogs[resolved] = catalog
            return catalog

        task.update(0, requested, dest_dir_rel or ".")
        try:
            dest_catalog = catalog_for(dest_root)
            for source_root, dir_rels in directory_groups.items():
                source_catalog = catalog_for(source_root)
                base_processed = processed

                def directory_progress(local_processed: int, _total: int | None, current: str) -> None:
                    task.update(min(base_processed + local_processed, requested), requested, current)

                results = source_catalog.move_directories(
                    dir_rels,
                    dest_catalog,
                    dest_dir_rel,
                    wipe_on_delete=wipe_on_delete,
                    progress_callback=directory_progress,
                    cancel_check=task.check_canceled,
                )
                processed += len(dir_rels)
                moved += len(results)
                task.update(min(processed, requested), requested, dest_dir_rel or ".")
            for source_root, rel_paths in image_groups.items():
                source_catalog = catalog_for(source_root)
                base_processed = processed

                def image_progress(local_processed: int, _total: int | None, current: str) -> None:
                    task.update(min(base_processed + local_processed, requested), requested, current)

                results = source_catalog.move_images(
                    rel_paths,
                    dest_catalog,
                    dest_dir_rel,
                    wipe_on_delete=wipe_on_delete,
                    progress_callback=image_progress,
                    cancel_check=task.check_canceled,
                )
                processed += len(rel_paths)
                moved += len(results)
                task.update(min(processed, requested), requested, dest_dir_rel or ".")
            task.mark_done()
            return MovePayloadResult(requested=requested, moved=moved, affected_roots=affected_roots)
        except IndexTaskCancelled:
            task.mark_canceled()
            raise
        except Exception as error:
            task.mark_failed(error)
            raise
        finally:
            for catalog in catalogs.values():
                with suppress(Exception):
                    catalog.close()

    def _remove_records_from_current_view(
        self,
        root: Path,
        *,
        image_rels: set[str] | None = None,
        directory_rels: set[str] | None = None,
    ) -> None:
        if self.current_catalog is None or self.current_catalog.root != root.expanduser().resolve():
            return
        image_rels = image_rels or set()
        directory_rels = directory_rels or set()
        if not image_rels and not directory_rels:
            return
        records = list(self.model.images)
        remove_flags: list[bool] = []
        filtered: list[PaneRecord] = []
        removed_rows: list[int] = []
        for row, record in enumerate(records):
            remove = (
                record.rel_path in image_rels
                if isinstance(record, ImageRecord)
                else record.dir_rel in directory_rels
            )
            remove_flags.append(remove)
            if remove:
                removed_rows.append(row)
                continue
            filtered.append(record)
        if not removed_rows:
            return
        anchor_key = self._thumbnail_anchor_key_after_removal(records, remove_flags, removed_rows)
        self.model.set_images(self.current_catalog, filtered)
        self.refresh_thumbnail_layout()
        if anchor_key is not None:
            self._select_thumbnail_record_key(anchor_key)
        else:
            self.update_selection_status()
            self._remember_thumbnail_scroll_position()

    def _thumbnail_anchor_key_after_removal(
        self,
        records: Sequence[PaneRecord],
        remove_flags: Sequence[bool],
        removed_rows: Sequence[int],
    ) -> tuple[str, str] | None:
        current = self.thumbnail_view.currentIndex()
        if current.isValid() and current.row() < len(records) and not remove_flags[current.row()]:
            return self._thumbnail_record_key(records[current.row()])
        start_row = min(removed_rows)
        for row in range(start_row, len(records)):
            if not remove_flags[row]:
                return self._thumbnail_record_key(records[row])
        for row in range(start_row - 1, -1, -1):
            if not remove_flags[row]:
                return self._thumbnail_record_key(records[row])
        return None

    def _select_thumbnail_record_key(self, key: tuple[str, str]) -> bool:
        kind, rel_path = key
        for row, record in enumerate(self.model.images):
            if self._thumbnail_record_key(record) != (kind, rel_path):
                continue
            self.model.ensure_row_loaded(row)
            index = self.model.index(row, 0)
            selection = self.thumbnail_view.selectionModel()
            selection.clearSelection()
            selection.select(index, QItemSelectionModel.SelectionFlag.Select)
            self.thumbnail_view.setCurrentIndex(index)
            self._cancel_thumbnail_scroll_restore()
            self.thumbnail_view.scrollTo(index, QListView.ScrollHint.PositionAtCenter)
            self.update_selection_status()
            self._remember_thumbnail_scroll_position()
            return True
        return False

    def select_rel_path(self, rel_path: str) -> None:
        for row, record in enumerate(self.model.images):
            if not isinstance(record, ImageRecord) or record.rel_path != rel_path:
                continue
            self._select_thumbnail_record_key(("image", rel_path))
            return

    def sync_thumbnail_to_rel_path(self, catalog: Catalog, rel_path: str) -> None:
        if self.current_catalog is None or self.current_catalog.root != catalog.root:
            return
        self.select_rel_path(rel_path)

    def queue_directory_index(
        self,
        catalog: Catalog,
        dir_rel: str,
        *,
        force: bool = False,
        interactive: bool = True,
    ) -> None:
        task = self.indexer.refresh_directory(catalog.root, dir_rel, interactive=interactive, force=force)
        self._directory_index_tasks[(catalog.root, dir_rel)] = task
        self._poll_indexer()

    def _schedule_idle_indexing(self) -> None:
        if self._closing:
            return
        if self.thumbnail_view.manual_drag_active():
            return
        if self._has_active_catalog_open_tasks():
            return
        if self._has_active_duplicate_delete_task():
            return
        if self._has_pending_delete_payload_tasks():
            return
        if self._has_pending_move_payload_tasks():
            return
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        if self._tree_build_task is not None or self._pending_tree_rebuilds:
            return
        if self.indexer.has_active_tasks():
            return
        now = monotonic()
        for catalog in self.workspace.catalogs:
            if catalog.root not in self._shallow_tree_roots:
                continue
            if catalog.root in self._directory_discovery_tasks:
                continue
            if now < self._directory_discovery_retry_at.get(catalog.root, 0.0):
                continue
            self._directory_discovery_tasks[catalog.root] = self.indexer.discover_directories(
                catalog.root,
                interactive=False,
            )
            return
        for catalog in self.workspace.catalogs:
            if catalog.root not in self._swept_catalog_roots:
                self._idle_index_tasks[catalog.root] = self.indexer.refresh_catalog(
                    catalog.root,
                    interactive=False,
                )
                return
        for catalog in self.workspace.catalogs:
            if catalog.root not in self._pruned_catalog_roots:
                self._thumbnail_prune_tasks[catalog.root] = self.indexer.prune_thumbnails(
                    catalog.root,
                    interactive=False,
                )
                return

    def _poll_indexer(self) -> None:
        if self.thumbnail_view.manual_drag_active():
            return
        drain_completed = getattr(self.indexer, "drain_completed_snapshots", None)
        if callable(drain_completed):
            drain_completed()
        self._settle_catalog_open_tasks()
        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        self._settle_virtual_view_tasks()
        active_open_task = self._active_catalog_open_task()
        if active_open_task is not None:
            self._show_catalog_open_status(active_open_task.root)
            return
        snapshots = self.indexer.active_snapshots()
        active_delete_task = self._active_duplicate_delete_task()
        if active_delete_task is not None and self._task_is_running(active_delete_task.task, snapshots):
            self._show_duplicate_delete_status(active_delete_task.task.snapshot())
            return
        active_delete_payload_task = self._active_delete_payload_task()
        if active_delete_payload_task is not None and self._task_is_running(active_delete_payload_task.task, snapshots):
            self._show_delete_payload_status(active_delete_payload_task.task.snapshot())
            return
        active_move_task = self._active_move_payload_task()
        if active_move_task is not None and self._task_is_running(active_move_task.task, snapshots):
            self._show_move_payload_status(active_move_task.task.snapshot())
            return
        active_virtual_task = self._active_virtual_view_task()
        if active_virtual_task is not None:
            self._show_virtual_view_status(active_virtual_task)
            return
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        snapshots = self.indexer.active_snapshots()
        visible_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.interactive or not snapshot.label.startswith("Pruning thumbnails")
        ]
        if not visible_snapshots:
            self._indexing_was_active = False
            self._schedule_idle_indexing()
            if self._tree_build_task is not None:
                self._show_tree_build_status(self._tree_build_task)
                return
            if snapshots:
                return
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            self.statusBar().clearMessage()
            return

        self._indexing_was_active = True
        snapshot = sorted(visible_snapshots, key=lambda item: (item.interactive, item.started_at), reverse=True)[0]
        if snapshot.total is None:
            synthetic_total = max(snapshot.processed + 64, 1)
            self.progress_bar.setRange(0, synthetic_total)
            self.progress_bar.setValue(min(snapshot.processed, synthetic_total))
            if snapshot.label.startswith("Discovering folders"):
                progress_unit = "folders found"
            elif snapshot.label.startswith("Pruning thumbnails"):
                progress_unit = "thumbnail rows checked"
            else:
                progress_unit = "images checked"
            detail = f"{snapshot.label}: {snapshot.processed} {progress_unit}"
        elif snapshot.total == 0:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            detail = f"{snapshot.label}: {snapshot.current or 'nothing to index'}"
        else:
            self.progress_bar.setRange(0, snapshot.total)
            self.progress_bar.setValue(min(snapshot.processed, snapshot.total))
            if snapshot.label.startswith("Refreshing catalog"):
                detail = (
                    f"{snapshot.label} ({snapshot.processed}/{snapshot.total}): "
                    f"{self.indexing_progress_path(snapshot)}"
                )
            else:
                detail = f"{snapshot.label}: {snapshot.processed}/{snapshot.total}"
        if snapshot.current and snapshot.total != 0:
            if not snapshot.label.startswith("Refreshing catalog"):
                detail = f"{detail} - {self.indexing_progress_path(snapshot)}"
            if (
                self.current_catalog is not None
                and snapshot.root == self.current_catalog.root
                and Path(snapshot.current).parent.as_posix() == (self.current_dir_rel or ".")
            ):
                self.model.refresh_thumbnail(snapshot.current)
        self.progress_label.setText(detail)

    def _task_is_running(self, task: IndexTask, snapshots: Sequence[IndexProgressSnapshot]) -> bool:
        return any(
            snapshot.started_at == task.started_at
            and snapshot.label == task.label
            and snapshot.root == task.root
            for snapshot in snapshots
        )

    def _show_tree_build_status(self, task: TreeBuildTask) -> None:
        if task.total is None:
            self.progress_bar.setRange(0, max(task.processed + TREE_BUILD_BATCH_SIZE, 1))
            self.progress_bar.setValue(task.processed)
            self.progress_label.setText(f"Building folder tree: {task.processed} folders")
            return
        self.progress_bar.setRange(0, max(task.total, 1))
        self.progress_bar.setValue(task.processed)
        self.progress_label.setText(f"Building folder tree {task.processed}/{task.total}")

    def indexing_progress_path(self, snapshot: IndexProgressSnapshot) -> str:
        if snapshot.dir_rel is not None:
            return snapshot.dir_rel or "."
        if not snapshot.current:
            return "."
        current_text = snapshot.current
        if current_text.startswith("Finding images in "):
            current_text = current_text.removeprefix("Finding images in ").strip()
        if current_text == snapshot.root.name or current_text == str(snapshot.root):
            return "."
        if current_text in {"Catalog scan complete", "Directory scan complete", "Folder discovery complete"}:
            return current_text
        current = Path(current_text)
        if not current.suffix:
            return current.as_posix() or "."
        parent = current.parent.as_posix()
        if parent == ".":
            return "."
        return parent

    def _settle_idle_tasks(self) -> None:
        for root, task in list(self._idle_index_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._idle_index_tasks.pop(root, None)
            if snapshot.error is None and not snapshot.canceled:
                self._swept_catalog_roots.add(root)
                if self.current_catalog is not None and self.current_catalog.root == root:
                    self.load_current_directory(preserve_selection=True)
                if snapshot.interactive or task.force_refresh:
                    catalog = self.workspace.catalog_for_root(root)
                    if catalog is not None:
                        self._request_incremental_tree_rebuild(catalog, reason="catalog_refresh")

    def _settle_directory_index_tasks(self) -> None:
        completed_roots: set[Path] = set()
        reload_current = False
        for key, task in list(self._directory_index_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._directory_index_tasks.pop(key, None)
            completed_roots.add(key[0])
            if (
                snapshot.error is None
                and not snapshot.canceled
                and self.current_catalog is not None
                and self.current_catalog.root == key[0]
                and self.current_virtual_kind is None
                and self.current_dir_rel == key[1]
            ):
                reload_current = True
        for root in completed_roots:
            if any(key[0] == root for key in self._directory_index_tasks):
                continue
            if root in self._resume_idle_refresh_roots:
                self._resume_idle_refresh_roots.discard(root)
                self._swept_catalog_roots.discard(root)
        if reload_current:
            self.load_current_directory(preserve_selection=True)

    def _settle_directory_discovery_tasks(self) -> None:
        for root, task in list(self._directory_discovery_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._directory_discovery_tasks.pop(root, None)
            if snapshot.error is None and not snapshot.canceled:
                self._directory_discovery_retries.pop(root, None)
                self._directory_discovery_retry_at.pop(root, None)
                self._shallow_tree_roots.discard(root)
                catalog = self.workspace.catalog_for_root(root)
                if catalog is not None:
                    self._request_incremental_tree_rebuild(catalog, reason="directory_discovery")
                continue
            self._shallow_tree_roots.add(root)
            retries = self._directory_discovery_retries.get(root, 0) + 1
            self._directory_discovery_retries[root] = retries
            # Cancellations are normal when a user selects another folder.
            # Retry once the indexer becomes idle, indefinitely, with bounded
            # backoff so a persistent I/O error cannot create a hot loop.
            retry_delay = min(30.0, 0.25 * (2 ** min(retries - 1, 7)))
            self._directory_discovery_retry_at[root] = monotonic() + retry_delay

    def _settle_thumbnail_prune_tasks(self) -> None:
        for root, task in list(self._thumbnail_prune_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._thumbnail_prune_tasks.pop(root, None)
            if snapshot.error is None and not snapshot.canceled:
                self._pruned_catalog_roots.add(root)
                if self.current_catalog is not None and self.current_catalog.root == root:
                    self.load_current_directory(preserve_selection=True)


class TagDialog(QDialog):
    def __init__(self, catalog: Catalog, rel_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.rel_path = rel_path
        self.setWindowTitle("Tags")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setStyleSheet(DIALOG_STYLESHEET)
        scroll.setWidgetResizable(True)
        tag_container = QWidget()
        tag_container.setObjectName("tagContainer")
        tag_container.setStyleSheet("background: #f6f7f9; color: #202124;")
        tag_layout = QVBoxLayout(tag_container)
        selected = set(catalog.get_image_tags(rel_path))
        self.checkboxes: list[QCheckBox] = []
        for tag in catalog.list_tags():
            checkbox = QCheckBox(tag)
            checkbox.setStyleSheet("background: transparent; color: #202124;")
            checkbox.setChecked(tag in selected)
            self.checkboxes.append(checkbox)
            tag_layout.addWidget(checkbox)
        tag_layout.addStretch(1)
        scroll.setWidget(tag_container)
        layout.addWidget(scroll)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Comma-separated tags")
        self.entry.returnPressed.connect(self.accept)
        self.setFocusProxy(self.entry)
        layout.addWidget(self.entry)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, 420)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        QTimer.singleShot(0, self.focus_entry)

    def focus_entry(self) -> None:
        self.entry.setFocus(Qt.FocusReason.OtherFocusReason)

    def selected_tags(self) -> list[str]:
        names = [checkbox.text() for checkbox in self.checkboxes if checkbox.isChecked()]
        names.extend(parse_tag_entry(self.entry.text()))
        seen: set[str] = set()
        result: list[str] = []
        for name in names:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                result.append(name)
        return result


class DuplicateListDialog(QDialog):
    def __init__(
        self,
        catalog: Catalog,
        source: ImageRecord,
        matches: DuplicateMatchGroups,
        navigate_callback: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.source = source
        self.matches = matches
        self.navigate_callback = navigate_callback
        self.setWindowTitle("Duplicates")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        source_label = QLabel(source.rel_path)
        source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(source_label)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._item_double_clicked)
        layout.addWidget(self.list_widget, 1)

        self._add_section("Exact Duplicates", matches.exact)
        self._add_section("Very Similar", matches.very_similar)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(720, 420)

    def _add_section(self, title: str, records: Sequence[ImageRecord]) -> None:
        header = QListWidgetItem(title)
        header_font = header.font()
        header_font.setBold(True)
        header.setFont(header_font)
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list_widget.addItem(header)
        if not records:
            empty = QListWidgetItem("  (none)")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(empty)
            return
        for record in records:
            item = QListWidgetItem(f"  {record.rel_path}")
            item.setToolTip(str(record.absolute_path))
            item.setData(Qt.ItemDataRole.UserRole, record.rel_path)
            self.list_widget.addItem(item)

    def _item_double_clicked(self, item: QListWidgetItem) -> None:
        rel_path = item.data(Qt.ItemDataRole.UserRole)
        if not rel_path:
            return
        self.navigate_callback(str(rel_path))
        self.accept()


class AppPreferencesDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loaded_catalogs = config._loaded_catalogs
        self.setWindowTitle("Preferences")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.window_x = QSpinBox()
        self.window_x.setRange(-100000, 100000)
        self.window_x.setValue(config.window.x if config.window.x is not None else 0)
        self.window_y = QSpinBox()
        self.window_y.setRange(-100000, 100000)
        self.window_y.setValue(config.window.y if config.window.y is not None else 0)
        self.window_width = QSpinBox()
        self.window_width.setRange(200, 20000)
        self.window_width.setValue(config.window.width)
        self.window_height = QSpinBox()
        self.window_height.setRange(200, 20000)
        self.window_height.setValue(config.window.height)
        self.window_maximized = QCheckBox()
        self.window_maximized.setChecked(config.window.maximized)

        self.thumbnail_size = QSpinBox()
        self.thumbnail_size.setRange(MIN_THUMBNAIL_COLUMNS, MAX_THUMBNAIL_COLUMNS)
        self.thumbnail_size.setSingleStep(1)
        self.thumbnail_size.setValue(
            max(MIN_THUMBNAIL_COLUMNS, min(MAX_THUMBNAIL_COLUMNS, int(config.thumbnail_size)))
        )

        self.sort_order = QComboBox()
        for sort_order in SortOrder:
            self.sort_order.addItem(sort_order.label, sort_order.value)
        sort_index = self.sort_order.findData(config.sort_order)
        if sort_index >= 0:
            self.sort_order.setCurrentIndex(sort_index)

        self.delete_behavior = QComboBox()
        self.delete_behavior.addItem("Normal Delete", NORMAL_DELETE)
        self.delete_behavior.addItem("Wipe on Delete", WIPE_ON_DELETE)
        index = self.delete_behavior.findData(config.delete_behavior)
        if index >= 0:
            self.delete_behavior.setCurrentIndex(index)

        form.addRow("Window x", self.window_x)
        form.addRow("Window y", self.window_y)
        form.addRow("Window width", self.window_width)
        form.addRow("Window height", self.window_height)
        form.addRow("Window maximized", self.window_maximized)
        form.addRow("Thumbnails per row", self.thumbnail_size)
        form.addRow("Sort order", self.sort_order)
        form.addRow("Delete behavior", self.delete_behavior)
        layout.addLayout(form)

        layout.addWidget(QLabel("Catalogs"))
        self.catalog_list = QListWidget()
        for catalog_path in config.catalogs:
            self.catalog_list.addItem(catalog_path)
        layout.addWidget(self.catalog_list)

        catalog_buttons = QHBoxLayout()
        add_catalog = QPushButton("Add")
        remove_catalog = QPushButton("Remove")
        add_catalog.clicked.connect(self.add_catalog)
        remove_catalog.clicked.connect(self.remove_selected_catalogs)
        catalog_buttons.addWidget(add_catalog)
        catalog_buttons.addWidget(remove_catalog)
        catalog_buttons.addStretch(1)
        layout.addLayout(catalog_buttons)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_catalog(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Add catalog")
        if directory:
            self.catalog_list.addItem(str(Path(directory).expanduser()))

    def remove_selected_catalogs(self) -> None:
        for item in self.catalog_list.selectedItems():
            row = self.catalog_list.row(item)
            self.catalog_list.takeItem(row)

    def selected_config(self) -> AppConfig:
        return AppConfig(
            window=WindowConfig(
                x=self.window_x.value(),
                y=self.window_y.value(),
                width=self.window_width.value(),
                height=self.window_height.value(),
                maximized=self.window_maximized.isChecked(),
            ),
            catalogs=[
                self.catalog_list.item(index).text()
                for index in range(self.catalog_list.count())
            ],
            thumbnail_size=self.thumbnail_size.value(),
            delete_behavior=str(self.delete_behavior.currentData()),
            sort_order=str(self.sort_order.currentData()),
            _loaded_catalogs=self._loaded_catalogs,
        )


class PreferencesDialog(QDialog):
    def __init__(
        self,
        catalogs: list[Catalog],
        current_catalog: Catalog | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalogs = catalogs
        self.setWindowTitle("Preferences")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.catalog_combo = QComboBox()
        for catalog in catalogs:
            self.catalog_combo.addItem(str(catalog.root), str(catalog.root))
        if current_catalog is not None:
            current_index = self.catalog_combo.findData(str(current_catalog.root))
            if current_index >= 0:
                self.catalog_combo.setCurrentIndex(current_index)

        self.thumbnail_size = QSpinBox()
        self.thumbnail_size.setRange(64, 4096)
        self.thumbnail_size.setSingleStep(64)
        self.thumbnail_size.setValue(self._selected_catalog().settings.thumbnail_native_size)
        self.prune_parallelism = QSpinBox()
        self.prune_parallelism.setRange(1, 64)
        self.prune_parallelism.setValue(self._selected_catalog().settings.prune_parallelism)
        self.catalog_combo.currentIndexChanged.connect(self._catalog_changed)

        form.addRow("Catalog", self.catalog_combo)
        form.addRow("Saved thumbnail size", self.thumbnail_size)
        form.addRow("Thumbnail prune threads", self.prune_parallelism)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_settings(self) -> tuple[Catalog, CatalogSettings]:
        return self._selected_catalog(), CatalogSettings(
            thumbnail_native_size=self.thumbnail_size.value(),
            prune_parallelism=self.prune_parallelism.value(),
        )

    def _catalog_changed(self) -> None:
        settings = self._selected_catalog().settings
        self.thumbnail_size.setValue(settings.thumbnail_native_size)
        self.prune_parallelism.setValue(settings.prune_parallelism)

    def _selected_catalog(self) -> Catalog:
        root = Path(str(self.catalog_combo.currentData())).resolve()
        for catalog in self.catalogs:
            if catalog.root == root:
                return catalog
        return self.catalogs[0]


class CatalogTagsDialog(QDialog):
    def __init__(self, catalog: Catalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.setWindowTitle("Catalog Tags")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        add_row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Comma-separated tags")
        self.entry.returnPressed.connect(self.add_tags)
        add_button = QPushButton("Add")
        add_button.clicked.connect(self.add_tags)
        add_row.addWidget(self.entry, 1)
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()
        self.resize(420, 500)

    def refresh(self) -> None:
        self.list_widget.clear()
        for tag in self.catalog.list_tags():
            self.list_widget.addItem(tag)

    def add_tags(self) -> None:
        names = parse_tag_entry(self.entry.text())
        if not names:
            return
        self.catalog.define_tags(names)
        self.entry.clear()
        self.refresh()


class LogsDialog(QDialog):
    def __init__(self, catalogs: list[Catalog], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)
        self.copy_buttons: list[QPushButton] = []

        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        rows = QVBoxLayout(container)
        rows.setContentsMargins(8, 8, 8, 8)

        entries: list[tuple[str, str]] = []
        for catalog in catalogs:
            catalog_name = catalog.root.name or str(catalog.root)
            for line in catalog.read_log_lines():
                entries.append((line, f"{catalog_name}: {line}"))
        entries.sort(key=lambda item: item[0])

        if entries:
            for _, display_line in entries:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                line_entry = QLineEdit(display_line)
                line_entry.setReadOnly(True)
                line_entry.setMinimumWidth(0)
                line_entry.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                line_entry.setToolTip(display_line)
                copy_button = QPushButton("Copy")
                copy_button.clicked.connect(lambda checked=False, line=display_line: self.copy_line(line))
                self.copy_buttons.append(copy_button)
                row_layout.addWidget(line_entry, 1)
                row_layout.addWidget(copy_button)
                rows.addWidget(row)
        else:
            rows.addWidget(QLabel("No log entries"))
        rows.addStretch(1)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(900, 520)

    def copy_line(self, line: str) -> None:
        QApplication.clipboard().setText(line)


class DirectoryNameDialog(QDialog):
    def __init__(self, parent_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Directory")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Parent: {parent_path}"))
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Folder name")
        self.entry.returnPressed.connect(self.accept)
        layout.addWidget(self.entry)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(460, 130)

    def directory_name(self) -> str:
        return self.entry.text().strip()


class DirectoryPropertiesDialog(QDialog):
    SCAN_BUDGET_SECONDS = 0.008

    def __init__(self, catalog: Catalog, dir_rel: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.dir_rel = dir_rel
        self.path = catalog.abs_path(dir_rel) if dir_rel else catalog.root
        self.image_count = 0
        self.other_file_count = 0
        self.image_size_bytes = 0
        self.other_file_size_bytes = 0
        self._indexed_image_sizes = catalog.indexed_image_sizes_under(dir_rel)
        self._pending_dirs: list[Path] = [self.path]
        self._iterator: os.ScandirIterator[str] | None = None
        self._status_text = "Counting..."

        self.setWindowTitle("Properties")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        frame = QFrame()
        frame.setObjectName("propertiesFrame")
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setLineWidth(1)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(14, 14, 14, 14)
        form = QFormLayout()

        path_row = QHBoxLayout()
        self.path_entry = QLineEdit(str(self.path))
        self.path_entry.setReadOnly(True)
        self.path_entry.setMinimumWidth(0)
        self.path_entry.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.path_entry.setToolTip(str(self.path))
        copy_button = QPushButton("Copy")
        copy_button.clicked.connect(self.copy_path)
        path_row.addWidget(self.path_entry, 1)
        path_row.addWidget(copy_button)
        form.addRow("Path", path_row)

        self.image_count_label = QLabel("0")
        self.other_count_label = QLabel("0")
        self.image_size_label = QLabel("0 B")
        self.other_size_label = QLabel("0 B")
        self.database_size_label = QLabel(format_bytes(catalog.catalog_database_size_bytes()))
        self.thumbnail_repository_size_label = QLabel(format_bytes(catalog.thumbnail_repository_size_bytes()))
        form.addRow("Images", self.image_count_label)
        form.addRow("Other files", self.other_count_label)
        form.addRow("Image size", self.image_size_label)
        form.addRow("Other file size", self.other_size_label)
        if not dir_rel:
            form.addRow("Database size", self.database_size_label)
            form.addRow("Thumbnail repository size", self.thumbnail_repository_size_label)
        frame_layout.addLayout(form)

        self.status_label = QLabel()
        self.status_label.setMinimumWidth(0)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._set_status_text(self._status_text)
        frame_layout.addWidget(self.status_label)
        layout.addWidget(frame)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.timer = QTimer(self)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self._scan_step)
        self._start_scan()
        self.resize(700, 240)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_iterator()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._refresh_status_label_text()

    def copy_path(self) -> None:
        QApplication.clipboard().setText(str(self.path))

    def is_counting(self) -> bool:
        return self._iterator is not None or bool(self._pending_dirs)

    def _start_scan(self) -> None:
        self.timer.start()

    def _scan_step(self) -> None:
        deadline = monotonic() + self.SCAN_BUDGET_SECONDS
        while monotonic() < deadline:
            if self._iterator is None:
                if not self._pending_dirs:
                    self._set_status_text("Ready")
                    self._update_labels()
                    self.timer.stop()
                    return
                current = self._pending_dirs.pop()
                try:
                    self._iterator = os.scandir(current)
                    self._set_status_text(f"Counting {current}")
                except OSError:
                    continue
            try:
                entry = next(self._iterator)
            except StopIteration:
                self._close_current_iterator()
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name != ".marnwick":
                        self._pending_dirs.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if is_image_name(entry.name):
                self.image_count += 1
                try:
                    rel_path = self.catalog.rel_path(Path(entry.path))
                except ValueError:
                    rel_path = ""
                cached_size = self._indexed_image_sizes.get(rel_path)
                if cached_size is not None:
                    self.image_size_bytes += cached_size
                    continue
                try:
                    self.image_size_bytes += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                continue
            try:
                self.other_file_size_bytes += entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            self.other_file_count += 1
        self._update_labels()

    def _update_labels(self) -> None:
        self.image_count_label.setText(str(self.image_count))
        self.other_count_label.setText(str(self.other_file_count))
        self.image_size_label.setText(format_bytes(self.image_size_bytes))
        self.other_size_label.setText(format_bytes(self.other_file_size_bytes))

    def _set_status_text(self, text: str) -> None:
        self._status_text = text
        self._refresh_status_label_text()

    def _refresh_status_label_text(self) -> None:
        if not hasattr(self, "status_label"):
            return
        available_width = self.status_label.contentsRect().width()
        if available_width <= 0:
            available_width = max(1, self.width() - 80)
        metrics = QFontMetrics(self.status_label.font())
        visible_text = metrics.elidedText(
            self._status_text,
            Qt.TextElideMode.ElideMiddle,
            available_width,
        )
        self.status_label.setText(visible_text)
        self.status_label.setToolTip(self._status_text if visible_text != self._status_text else "")

    def _close_iterator(self) -> None:
        self._pending_dirs.clear()
        self._close_current_iterator()
        self.timer.stop()

    def _close_current_iterator(self) -> None:
        if self._iterator is not None:
            self._iterator.close()
            self._iterator = None


class MetadataDialog(QDialog):
    def __init__(self, image_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Metadata")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(metadata_text(image_path))
        layout.addWidget(text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(720, 560)


def metadata_text(image_path: Path) -> str:
    lines: list[str] = []
    lines.append(f"Path: {image_path}")
    try:
        stat = image_path.stat()
        lines.append(f"File size: {stat.st_size} bytes")
        lines.append(f"Modified: {datetime.fromtimestamp(stat.st_mtime).isoformat(sep=' ', timespec='seconds')}")
    except OSError as error:
        lines.append(f"File stat error: {error}")

    try:
        with open_catalog_image(image_path) as image:
            lines.append(f"Format: {image.format or 'unknown'}")
            lines.append(f"Dimensions: {image.width} x {image.height}")
            lines.append(f"Mode: {image.mode}")
            if image.info:
                lines.append("")
                lines.append("Image Info")
                for key in sorted(image.info):
                    if key == "exif":
                        continue
                    lines.append(f"{key}: {image.info[key]}")
            exif = image.getexif()
            if exif:
                lines.append("")
                lines.append("EXIF")
                for tag_id in sorted(exif):
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    value = exif.get(tag_id)
                    lines.append(f"{tag_name}: {value}")
    except Exception as error:
        lines.append(f"Metadata read error: {error}")
    return "\n".join(lines)


class EditCommandDialog(QDialog):
    COMMANDS = [
        ("L", "Rotate 90 left", "rotate_left"),
        ("R", "Rotate 90 right", "rotate_right"),
        ("V", "Flip vertical", "flip_vertical"),
        ("H", "Flip horizontal", "flip_horizontal"),
        ("I", "Remove red eye", "red_eye"),
        ("C", "Crop", "crop"),
        ("X", "Clone and heal", "clone_heal"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.command: str | None = None
        self._shortcuts: list[QShortcut] = []
        self.setWindowTitle("Edit")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for shortcut, label, command in self.COMMANDS:
            item = QListWidgetItem(f"{shortcut}    {label}")
            item.setData(Qt.ItemDataRole.UserRole, command)
            self.list_widget.addItem(item)
            shortcut_obj = QShortcut(QKeySequence(shortcut), self)
            shortcut_obj.activated.connect(lambda command=command: self.choose_command(command))
            self._shortcuts.append(shortcut_obj)
        self.list_widget.itemDoubleClicked.connect(self._item_chosen)
        self.list_widget.installEventFilter(self)
        layout.addWidget(self.list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(360, 320)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        key_text = event.text().upper()
        for shortcut, _, command in self.COMMANDS:
            if key_text == shortcut:
                self.choose_command(command)
                return
        super().keyPressEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched == self.list_widget and event.type() == QEvent.Type.KeyPress:
            key_text = event.text().upper()  # type: ignore[attr-defined]
            for shortcut, _, command in self.COMMANDS:
                if key_text == shortcut:
                    self.choose_command(command)
                    return True
        return super().eventFilter(watched, event)

    def selected_command(self) -> str | None:
        return self.command

    def choose_command(self, command: str) -> None:
        self.command = command
        self.accept()

    def _item_chosen(self, item: QListWidgetItem) -> None:
        self.choose_command(str(item.data(Qt.ItemDataRole.UserRole)))


class CloneBrushOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.center: QPoint | None = None
        self.radius = 32
        self.source_set = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.hide()

    def update_brush(self, center: QPoint | None, radius: int, source_set: bool) -> None:
        self.center = center
        self.radius = max(1, radius)
        self.source_set = source_set
        self.setVisible(center is not None)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.center is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#f8fafc" if self.source_set else "#f97316"), 2)
        if not self.source_set:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(self.center, self.radius, self.radius)


class CircularSelectionOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.selection_rect = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.hide()

    def update_selection(self, rect: QRect | None) -> None:
        self.selection_rect = QRect() if rect is None else QRect(rect)
        self.setVisible(rect is not None and not self.selection_rect.isNull())
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.selection_rect.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#38bdf8"), 2))
        painter.setBrush(QColor(56, 189, 248, 48))
        painter.drawEllipse(self.selection_rect)


class ImageDisplayLabel(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self._display_pixmap = QPixmap()
        self._target_rect: QRect | None = None

    def set_display_pixmap(self, pixmap: QPixmap, target_rect: QRect | None = None) -> None:
        self._display_pixmap = QPixmap(pixmap)
        self._target_rect = QRect(target_rect) if target_rect is not None else None
        self.update()

    def clear_display_pixmap(self) -> None:
        self._display_pixmap = QPixmap()
        self._target_rect = None
        super().clear()
        self.update()

    def display_pixmap(self) -> QPixmap:
        return QPixmap(self._display_pixmap)

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._display_pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("black"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        logical_size = logical_size_for_physical(
            self._display_pixmap.size(),
            self._display_pixmap.devicePixelRatio(),
        )
        target = self._target_rect or QRect(
            int((self.width() - logical_size.width()) / 2),
            int((self.height() - logical_size.height()) / 2),
            logical_size.width(),
            logical_size.height(),
        )
        painter.drawPixmap(target, self._display_pixmap)


class FullscreenViewer(QDialog):
    ZOOM_STEP = 1.25
    MAX_ZOOM = 16.0
    PAN_KEY_STEP = 80

    def __init__(
        self,
        catalog: Catalog,
        navigator: ImageNavigator,
        parent: QWidget | None = None,
        *,
        wipe_on_delete: bool = False,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.navigator = navigator
        self.wipe_on_delete = wipe_on_delete
        self.last_viewed_rel_path = navigator.current
        self.operations: list[EditOperation] = []
        self.edit_mode: str | None = None
        self.clone_source_center: tuple[int, int] | None = None
        self.clone_brush_radius_label = 32
        self.clone_painting = False
        self.clone_drag_start_target: tuple[int, int] | None = None
        self.clone_last_target: tuple[int, int] | None = None
        self.drag_origin: QPoint | None = None
        self.preview_path: Path | None = None
        self.preview_image: Image.Image | None = None
        self.preview_image_current = False
        self.display_preview_image: Image.Image | None = None
        self.display_preview_size: tuple[int, int] | None = None
        self.base_pixmap = QPixmap()
        self.load_error: str | None = None
        self.loaded_file_identity: ImageFileIdentity | None = None
        self.movie: QMovie | None = None
        self.zoom_level = 1.0
        self.pan_offset = QPoint(0, 0)
        self.pan_drag_start: QPoint | None = None
        self.pan_offset_at_drag_start = QPoint(0, 0)
        self.info_overlay_enabled = False
        self.setWindowTitle("Marnwick")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet("background: black;")
        self.label = ImageDisplayLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMouseTracking(True)
        self.label.installEventFilter(self)
        self.info_overlay = QLabel(self.label)
        self.info_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.info_overlay.setTextFormat(Qt.TextFormat.PlainText)
        self.info_overlay.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.info_overlay.setWordWrap(True)
        self.info_overlay.setStyleSheet(
            "color: #f8fafc; background: rgba(0, 0, 0, 175); padding: 8px 10px; border-radius: 4px;"
        )
        self.info_overlay.hide()
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.label)
        self.clone_overlay = CloneBrushOverlay(self.label)
        self.red_eye_overlay = CircularSelectionOverlay(self.label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label, 1)
        self.load_current()
        self.update_cursor_visibility()

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        key = event.key()
        modifiers = event.modifiers()
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
            return
        if key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.zoom_out()
            return
        if key == Qt.Key.Key_C and modifiers & Qt.KeyboardModifier.ControlModifier:
            copy_files_to_clipboard([self.current_path])
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_current_image()
            return
        if key == Qt.Key.Key_E:
            self.open_edit_tools()
            return
        if key == Qt.Key.Key_Z:
            self.toggle_info_overlay()
            return
        if self.is_zoomed() and key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if key == Qt.Key.Key_Left:
                self.pan_by(-self.PAN_KEY_STEP, 0)
            elif key == Qt.Key.Key_Right:
                self.pan_by(self.PAN_KEY_STEP, 0)
            elif key == Qt.Key.Key_Up:
                self.pan_by(0, -self.PAN_KEY_STEP)
            else:
                self.pan_by(0, self.PAN_KEY_STEP)
            return
        if key == Qt.Key.Key_Right:
            self.navigate(1)
            return
        if key == Qt.Key.Key_Left:
            self.navigate(-1)
            return
        if key == Qt.Key.Key_T:
            self.open_tags()
            return
        if key == Qt.Key.Key_Escape:
            if self.edit_mode is not None:
                self.exit_region_edit()
                return
            if self.is_zoomed():
                self.reset_zoom()
                return
            if self.confirm_pending_edits():
                self.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self.update_cursor_visibility()

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched == self.label and self.edit_mode == "clone_heal":
            return self.handle_clone_event(event)
        if watched == self.label and self.edit_mode is not None:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
                self.drag_origin = event.position().toPoint()  # type: ignore[attr-defined]
                if self.edit_mode == "red_eye":
                    self.red_eye_overlay.setGeometry(self.label.rect())
                    self.red_eye_overlay.update_selection(QRect(self.drag_origin, QSize(1, 1)))
                else:
                    self.rubber_band.setGeometry(QRect(self.drag_origin, QSize()))
                    self.rubber_band.show()
                return True
            if event.type() == QEvent.Type.MouseMove and self.drag_origin is not None:
                current = event.position().toPoint()  # type: ignore[attr-defined]
                rect = self.region_selection_rect(self.drag_origin, current)
                if self.edit_mode == "red_eye":
                    self.red_eye_overlay.update_selection(rect)
                else:
                    self.rubber_band.setGeometry(rect)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and self.drag_origin is not None:
                current = event.position().toPoint()  # type: ignore[attr-defined]
                rect = self.region_selection_rect(self.drag_origin, current)
                self.drag_origin = None
                self.rubber_band.hide()
                self.red_eye_overlay.update_selection(None)
                self.complete_region_drag(rect)
                return True
        if watched == self.label and self.edit_mode is None and self.is_zoomed():
            return self.handle_zoom_pan_event(event)
        return super().eventFilter(watched, event)

    def handle_zoom_pan_event(self, event: QEvent) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            point = event.position().toPoint()  # type: ignore[attr-defined]
            if not self.displayed_image_rect().contains(point):
                return False
            self.pan_drag_start = point
            self.pan_offset_at_drag_start = QPoint(self.pan_offset)
            self.update_cursor_visibility()
            return True
        if event_type == QEvent.Type.MouseMove and self.pan_drag_start is not None:
            if not (event.buttons() & Qt.MouseButton.LeftButton):  # type: ignore[attr-defined]
                return True
            point = event.position().toPoint()  # type: ignore[attr-defined]
            delta = point - self.pan_drag_start
            self.set_pan_offset(self.pan_offset_at_drag_start + delta)
            return True
        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            self.pan_drag_start = None
            self.update_cursor_visibility()
            return True
        return False

    def handle_clone_event(self, event: QEvent) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.Wheel:
            point = event.position().toPoint()  # type: ignore[attr-defined]
            delta = event.angleDelta().y()  # type: ignore[attr-defined]
            if delta:
                direction = 1 if delta > 0 else -1
                steps = max(1, abs(delta) // 120)
                self.set_clone_brush_radius(self.clone_brush_radius_label + direction * steps * 4)
            self.update_clone_overlay(point)
            return True
        if event_type == QEvent.Type.MouseMove:
            point = event.position().toPoint()  # type: ignore[attr-defined]
            self.update_clone_overlay(point)
            if self.clone_painting and event.buttons() & Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
                target = self.image_point_from_label_point(point)
                if target is not None:
                    self.paint_clone_to(target)
            return True
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:  # type: ignore[attr-defined]
            point = event.position().toPoint()  # type: ignore[attr-defined]
            self.update_clone_overlay(point)
            image_point = self.image_point_from_label_point(point)
            if image_point is not None:
                self.clone_source_center = image_point
                self.clone_painting = False
                self.clone_drag_start_target = None
                self.clone_last_target = None
                self.update_clone_overlay(point)
            return True
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            point = event.position().toPoint()  # type: ignore[attr-defined]
            self.update_clone_overlay(point)
            image_point = self.image_point_from_label_point(point)
            if image_point is None:
                return True
            if self.clone_source_center is None:
                return True
            self.clone_painting = True
            self.clone_drag_start_target = image_point
            self.clone_last_target = None
            self.paint_clone_to(image_point)
            return True
        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            self.clone_painting = False
            self.clone_drag_start_target = None
            self.clone_last_target = None
            self.update_clone_overlay(event.position().toPoint())  # type: ignore[attr-defined]
            return True
        if event_type == QEvent.Type.Leave:
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, self.clone_source_center is not None)
            return True
        return False

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if hasattr(self, "clone_overlay"):
            self.clone_overlay.setGeometry(self.label.rect())
        if hasattr(self, "red_eye_overlay"):
            self.red_eye_overlay.setGeometry(self.label.rect())
        if hasattr(self, "pan_offset"):
            self.pan_offset = self.clamped_pan_offset(self.pan_offset)
        self._fit_pixmap()
        if hasattr(self, "info_overlay"):
            self.update_info_overlay()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self.confirm_pending_edits():
            event.ignore()
            self.update_cursor_visibility()
            return
        self.restore_cursor_visibility()
        self.stop_movie()
        self.cleanup_preview()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self.restore_cursor_visibility()
        super().done(result)

    @property
    def current_path(self) -> Path:
        return self.catalog.abs_path(self.navigator.current)

    def navigate(self, step: int) -> None:
        if not self.confirm_pending_edits():
            return
        next_rel_path = self.navigator.next() if step > 0 else self.navigator.previous()
        if next_rel_path is None:
            self.accept()
            return
        self.load_current()

    def delete_current_image(self) -> None:
        if not self.confirm_pending_edits():
            return
        rel_path = self.navigator.current
        if not self.run_with_visible_cursor(lambda: ask_delete_files(self, 1)):
            return
        parent = self.parent()
        if isinstance(parent, MainWindow):
            parent.queue_delete_images(
                self.catalog,
                [rel_path],
                wipe=self.wipe_on_delete,
                remove_from_current_view=True,
            )
        else:
            self.catalog.delete_images([rel_path], wipe=self.wipe_on_delete)
        old_index = self.navigator.index
        self.navigator.order = [item for item in self.navigator.order if item != rel_path]
        if not self.navigator.order:
            self.accept()
            return
        self.navigator.index = min(old_index, len(self.navigator.order) - 1)
        self.load_current()

    def load_current(self) -> None:
        self.stop_movie()
        self.cleanup_preview()
        self.operations.clear()
        self.exit_region_edit()
        self.zoom_level = 1.0
        self.pan_offset = QPoint(0, 0)
        self.pan_drag_start = None
        self.pan_offset_at_drag_start = QPoint(0, 0)
        self.last_viewed_rel_path = self.navigator.current
        parent = self.parent()
        if isinstance(parent, MainWindow):
            parent.sync_thumbnail_to_rel_path(self.catalog, self.last_viewed_rel_path)
        self.preview_image = None
        self.preview_image_current = False
        self.display_preview_image = None
        self.display_preview_size = None
        self.load_error = None
        self.loaded_file_identity = None
        try:
            path = self.catalog.mutation_path(self.navigator.current)
            if not path.is_file():
                raise FileNotFoundError(path)
            identity = snapshot_image_file_identity(path)
            self.base_pixmap = load_oriented_pixmap(path)
            post_decode_stat = path.stat(follow_symlinks=False)
            post_decode_fields = (
                int(post_decode_stat.st_dev),
                int(post_decode_stat.st_ino),
                int(post_decode_stat.st_nlink),
                int(post_decode_stat.st_size),
                int(post_decode_stat.st_mtime_ns),
                int(getattr(post_decode_stat, "st_ctime_ns", 0)),
            )
            identity_fields = (
                identity.device,
                identity.inode,
                identity.link_count,
                identity.size,
                identity.modified_ns,
                identity.changed_ns,
            )
            if not stat.S_ISREG(post_decode_stat.st_mode) or post_decode_fields != identity_fields:
                raise OSError(f"{path.name} changed while it was being opened; reload it before editing")
            self.loaded_file_identity = identity
            if path.suffix.casefold() == ".gif" and self.start_movie():
                self.update_info_overlay()
                return
            if self.base_pixmap.isNull():
                raise ValueError(f"Unable to decode {path.name}")
        except (OSError, ValueError, UnsafeImageSaveError) as error:
            self.load_error = str(error)
            self.base_pixmap = QPixmap()
            self.label.clear_display_pixmap()
            self.label.setText(f"Unable to display image\n{error}")
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.update_info_overlay()
            return
        self._fit_pixmap()
        self.update_info_overlay()

    def toggle_info_overlay(self) -> None:
        self.info_overlay_enabled = not self.info_overlay_enabled
        self.update_info_overlay()

    def update_info_overlay(self) -> None:
        if not self.info_overlay_enabled:
            self.info_overlay.hide()
            return
        self.info_overlay.setText(self.info_overlay_text())
        self.position_info_overlay()
        self.info_overlay.show()
        self.info_overlay.raise_()

    def info_overlay_text(self) -> str:
        try:
            stat = self.current_path.stat()
            file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            file_date = "Unavailable"
        total = len(self.navigator.order)
        ordinal = self.navigator.index + 1 if total else 0
        return "\n".join(
            [
                f"Full path: {self.current_path}",
                f"File date: {file_date}",
                f"{ordinal} of {total} images",
            ]
        )

    def position_info_overlay(self) -> None:
        margin = 16
        max_width = max(1, self.label.width() - 2 * margin)
        max_height = max(1, self.label.height() - 2 * margin)
        self.info_overlay.setMaximumWidth(max_width)
        self.info_overlay.adjustSize()
        width = min(max_width, max(1, self.info_overlay.width()))
        height = min(max_height, max(1, self.info_overlay.height()))
        self.info_overlay.setGeometry(
            margin,
            max(margin, self.label.height() - height - margin),
            width,
            height,
        )

    def start_movie(self) -> bool:
        movie = QMovie(str(self.current_path))
        if not movie.isValid():
            movie.deleteLater()
            return False
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        movie.setScaledSize(self.displayed_image_rect().size())
        self.label.clear_display_pixmap()
        self.label.setMovie(movie)
        movie.start()
        self.movie = movie
        return True

    def stop_movie(self) -> None:
        if self.movie is None:
            return
        self.movie.stop()
        self.movie.deleteLater()
        self.movie = None
        self.label.clear_display_pixmap()

    def _fit_pixmap(self) -> None:
        if self.movie is not None and self.display_preview_image is None:
            self.movie.setScaledSize(self.displayed_image_rect().size())
            return
        if self.base_pixmap.isNull():
            self.label.clear_display_pixmap()
            return
        self.pan_offset = self.clamped_pan_offset(self.pan_offset)
        display_rect = self.displayed_image_rect()
        if self.display_preview_image is not None:
            target_size = self.display_preview_target_size()
            if self.display_preview_size != target_size:
                self.rebuild_display_preview()
            if self.display_preview_image is not None:
                self.label.set_display_pixmap(
                    pixmap_from_pil_image(
                        self.display_preview_image,
                        device_pixel_ratio=self.image_device_pixel_ratio(),
                    ),
                    display_rect,
                )
                return
        device_pixel_ratio = self.image_device_pixel_ratio()
        target_size = physical_size_for_logical(display_rect.size(), device_pixel_ratio)
        scaled = self.base_pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(device_pixel_ratio)
        self.label.set_display_pixmap(scaled, display_rect)

    def is_zoomed(self) -> bool:
        return self.zoom_level > 1.001

    def zoom_in(self) -> None:
        if self.base_pixmap.isNull():
            return
        self.zoom_level = min(self.MAX_ZOOM, self.zoom_level * self.ZOOM_STEP)
        self.pan_offset = self.clamped_pan_offset(self.pan_offset)
        self.update_cursor_visibility()
        self._fit_pixmap()

    def zoom_out(self) -> None:
        if not self.is_zoomed():
            return
        next_zoom = self.zoom_level / self.ZOOM_STEP
        if next_zoom <= 1.001:
            self.reset_zoom()
            return
        self.zoom_level = max(1.0, next_zoom)
        self.pan_offset = self.clamped_pan_offset(self.pan_offset)
        self._fit_pixmap()

    def reset_zoom(self) -> None:
        self.zoom_level = 1.0
        self.pan_offset = QPoint(0, 0)
        self.pan_drag_start = None
        self.pan_offset_at_drag_start = QPoint(0, 0)
        self.update_cursor_visibility()
        self._fit_pixmap()

    def pan_by(self, dx: int, dy: int) -> None:
        if not self.is_zoomed():
            return
        self.set_pan_offset(self.pan_offset + QPoint(dx, dy))

    def set_pan_offset(self, offset: QPoint) -> None:
        self.pan_offset = self.clamped_pan_offset(offset)
        self._fit_pixmap()

    def clamped_pan_offset(self, offset: QPoint) -> QPoint:
        size = self.zoomed_image_logical_size()
        max_x = max(0, int((size.width() - self.label.width()) / 2))
        max_y = max(0, int((size.height() - self.label.height()) / 2))
        return QPoint(
            max(-max_x, min(max_x, offset.x())),
            max(-max_y, min(max_y, offset.y())),
        )

    def open_tags(self) -> None:
        dialog: TagDialog | None = None

        def exec_dialog() -> int:
            nonlocal dialog
            dialog = TagDialog(self.catalog, self.navigator.current, self)
            return int(dialog.exec())

        if self.run_with_visible_cursor(exec_dialog) == int(QDialog.DialogCode.Accepted) and dialog is not None:
            self.catalog.set_image_tags(self.navigator.current, dialog.selected_tags(), replace=True)
            parent = self.parent()
            if isinstance(parent, MainWindow):
                parent.rebuild_tree()
                if parent.current_catalog is self.catalog and parent.current_virtual_kind is not None:
                    parent.load_current_directory(preserve_selection=True)

    def open_edit_tools(self) -> None:
        if self.base_pixmap.isNull() or self.load_error is not None:
            show_error(self, "Edit Image", self.load_error or "The image could not be decoded.")
            return
        dialog: EditCommandDialog | None = None

        def exec_dialog() -> int:
            nonlocal dialog
            dialog = EditCommandDialog(self)
            return int(dialog.exec())

        if self.run_with_visible_cursor(exec_dialog) != int(QDialog.DialogCode.Accepted) or dialog is None:
            return
        command = dialog.selected_command()
        if command in {"rotate_left", "rotate_right", "flip_horizontal", "flip_vertical"}:
            self.apply_instant_operation(command)
        elif command == "crop":
            self.start_region_edit("crop")
        elif command == "red_eye":
            self.start_region_edit("red_eye")
        elif command == "clone_heal":
            self.start_region_edit("clone_heal")

    def apply_instant_operation(self, name: str) -> None:
        self.exit_region_edit()
        self.operations.append(EditOperation(name))
        self.render_preview()

    def start_region_edit(self, mode: str) -> None:
        if self.movie is not None:
            self.stop_movie()
            self._fit_pixmap()
        self.edit_mode = mode
        self.drag_origin = None
        self.rubber_band.hide()
        self.clone_source_center = None
        self.clone_painting = False
        self.clone_drag_start_target = None
        self.clone_last_target = None
        if mode == "clone_heal":
            self.clone_overlay.setGeometry(self.label.rect())
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, False)
            self.red_eye_overlay.update_selection(None)
        else:
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, False)
            if mode == "red_eye":
                self.red_eye_overlay.setGeometry(self.label.rect())
            else:
                self.red_eye_overlay.update_selection(None)
        self.update_cursor_visibility()

    def exit_region_edit(self) -> None:
        self.edit_mode = None
        self.clone_source_center = None
        self.clone_painting = False
        self.clone_drag_start_target = None
        self.clone_last_target = None
        self.drag_origin = None
        if hasattr(self, "rubber_band"):
            self.rubber_band.hide()
        if hasattr(self, "clone_overlay"):
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, False)
        if hasattr(self, "red_eye_overlay"):
            self.red_eye_overlay.update_selection(None)
        if hasattr(self, "label"):
            self.update_cursor_visibility()

    def update_cursor_visibility(self) -> None:
        if not hasattr(self, "label"):
            return
        if self.edit_mode is None:
            self.setCursor(Qt.CursorShape.BlankCursor)
            self.label.setCursor(Qt.CursorShape.BlankCursor)
            return
        self.unsetCursor()
        self.label.setCursor(Qt.CursorShape.CrossCursor)

    def restore_cursor_visibility(self) -> None:
        self.unsetCursor()
        if hasattr(self, "label"):
            self.label.unsetCursor()

    def run_with_visible_cursor(self, callback: Callable[[], object]) -> object:
        self.restore_cursor_visibility()
        try:
            return callback()
        finally:
            self.update_cursor_visibility()

    def region_selection_rect(self, origin: QPoint, current: QPoint) -> QRect:
        if self.edit_mode != "red_eye":
            return QRect(origin, current).normalized()
        dx = current.x() - origin.x()
        dy = current.y() - origin.y()
        side = max(abs(dx), abs(dy), 1)
        x_direction = -1 if dx < 0 else 1
        y_direction = -1 if dy < 0 else 1
        corner = QPoint(origin.x() + x_direction * side, origin.y() + y_direction * side)
        return QRect(origin, corner).normalized()

    def complete_region_drag(self, rect: QRect) -> None:
        box = self.image_box_from_label_rect(rect)
        if box is None:
            return
        if self.edit_mode == "crop":
            self.operations.append(
                EditOperation(
                    "crop",
                    {"left": box[0], "top": box[1], "right": box[2], "bottom": box[3]},
                )
            )
            self.exit_region_edit()
            self.render_preview()
            return
        if self.edit_mode == "red_eye":
            self.operations.append(
                EditOperation(
                    "red_eye",
                    {"left": box[0], "top": box[1], "right": box[2], "bottom": box[3], "ellipse": True},
                )
            )
            self.render_preview()
            return

    def image_box_from_label_rect(self, rect: QRect) -> tuple[int, int, int, int] | None:
        if self.base_pixmap.isNull():
            return None
        display_rect = self.displayed_image_rect()
        clipped = rect.intersected(display_rect)
        if clipped.width() < 2 or clipped.height() < 2:
            return None
        scale_x = self.base_pixmap.width() / display_rect.width()
        scale_y = self.base_pixmap.height() / display_rect.height()
        left = int((clipped.left() - display_rect.left()) * scale_x)
        top = int((clipped.top() - display_rect.top()) * scale_y)
        right = int((clipped.right() + 1 - display_rect.left()) * scale_x)
        bottom = int((clipped.bottom() + 1 - display_rect.top()) * scale_y)
        left = max(0, min(left, self.base_pixmap.width() - 1))
        top = max(0, min(top, self.base_pixmap.height() - 1))
        right = max(left + 1, min(right, self.base_pixmap.width()))
        bottom = max(top + 1, min(bottom, self.base_pixmap.height()))
        return left, top, right, bottom

    def image_point_from_label_point(self, point: QPoint) -> tuple[int, int] | None:
        if self.base_pixmap.isNull():
            return None
        display_rect = self.displayed_image_rect()
        if not display_rect.contains(point) or display_rect.width() <= 0 or display_rect.height() <= 0:
            return None
        scale_x = self.base_pixmap.width() / display_rect.width()
        scale_y = self.base_pixmap.height() / display_rect.height()
        x = int((point.x() - display_rect.left()) * scale_x)
        y = int((point.y() - display_rect.top()) * scale_y)
        x = max(0, min(x, self.base_pixmap.width() - 1))
        y = max(0, min(y, self.base_pixmap.height() - 1))
        return x, y

    def image_radius_from_label_radius(self) -> int:
        if self.base_pixmap.isNull():
            return 1
        display_rect = self.displayed_image_rect()
        if display_rect.width() <= 0:
            return max(1, self.clone_brush_radius_label)
        scale = self.base_pixmap.width() / display_rect.width()
        return max(1, int(round(self.clone_brush_radius_label * scale)))

    def set_clone_brush_radius(self, radius: int) -> None:
        max_radius = max(8, min(256, max(1, min(self.label.width(), self.label.height()) // 2)))
        self.clone_brush_radius_label = max(4, min(max_radius, radius))

    def update_clone_overlay(self, point: QPoint | None) -> None:
        if self.edit_mode != "clone_heal":
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, False)
            return
        if point is None or not self.displayed_image_rect().contains(point):
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, self.clone_source_center is not None)
            return
        self.clone_overlay.update_brush(point, self.clone_brush_radius_label, self.clone_source_center is not None)

    def paint_clone_to(self, target: tuple[int, int]) -> None:
        if self.clone_source_center is None:
            return
        if self.clone_drag_start_target is None:
            self.clone_drag_start_target = target
        image_radius = self.image_radius_from_label_radius()
        operations: list[EditOperation] = []
        for sample in self.clone_stroke_samples(self.clone_last_target, target, image_radius):
            delta_x = sample[0] - self.clone_drag_start_target[0]
            delta_y = sample[1] - self.clone_drag_start_target[1]
            source = (self.clone_source_center[0] + delta_x, self.clone_source_center[1] + delta_y)
            operations.append(
                EditOperation(
                    "clone_heal",
                    {"source_center": source, "target_center": sample, "radius": image_radius},
                )
            )
        if not operations:
            return
        self.apply_clone_operations_to_display(operations)
        self.operations.extend(operations)
        self.preview_image_current = False
        self.clone_last_target = target

    def clone_stroke_samples(
        self,
        last_target: tuple[int, int] | None,
        target: tuple[int, int],
        radius: int,
    ) -> list[tuple[int, int]]:
        if last_target is None:
            return [target]
        dx = target[0] - last_target[0]
        dy = target[1] - last_target[1]
        distance = hypot(dx, dy)
        step = max(1.0, radius * 0.45)
        count = max(1, ceil(distance / step))
        samples: list[tuple[int, int]] = []
        for index in range(1, count + 1):
            fraction = index / count
            sample = (
                int(round(last_target[0] + dx * fraction)),
                int(round(last_target[1] + dy * fraction)),
            )
            if not samples or samples[-1] != sample:
                samples.append(sample)
        return samples

    def displayed_image_rect(self) -> QRect:
        scaled_size = self.zoomed_image_logical_size()
        offset = self.clamped_pan_offset(self.pan_offset)
        left = int((self.label.width() - scaled_size.width()) / 2) + offset.x()
        top = int((self.label.height() - scaled_size.height()) / 2) + offset.y()
        return QRect(left, top, scaled_size.width(), scaled_size.height())

    def zoomed_image_logical_size(self) -> QSize:
        device_pixel_ratio = self.image_device_pixel_ratio()
        scaled_size = self.base_pixmap.size()
        scaled_size.scale(
            physical_size_for_logical(self.label.size(), device_pixel_ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        scaled_size = logical_size_for_physical(scaled_size, device_pixel_ratio)
        return QSize(
            max(1, int(round(scaled_size.width() * self.zoom_level))),
            max(1, int(round(scaled_size.height() * self.zoom_level))),
        )

    def image_device_pixel_ratio(self) -> float:
        return widget_device_pixel_ratio(self.label)

    def render_preview(self) -> None:
        self.stop_movie()
        self.cleanup_preview()
        try:
            self.preview_image = self.render_operations_to_image(self.operations)
        except (OSError, ValueError) as error:
            self.operations.clear()
            self.load_current()
            show_error(self, "Edit Image", str(error))
            return
        self.preview_image_current = True
        self.display_preview_image = None
        self.display_preview_size = None
        self.base_pixmap = pixmap_from_pil_image(self.preview_image)
        self._fit_pixmap()

    def render_operations_to_image(self, operations: list[EditOperation]) -> Image.Image:
        with open_catalog_image(self.current_path) as image:
            edited = ImageOps.exif_transpose(image).copy()
            for operation in operations:
                edited = apply_operation_to_image(edited, operation)
            return edited

    def ensure_preview_image(self) -> Image.Image:
        if self.preview_image is None:
            self.preview_image = self.render_operations_to_image(self.operations)
            self.preview_image_current = True
        return self.preview_image

    def apply_operations_to_preview(self, operations: list[EditOperation]) -> None:
        edited = self.ensure_preview_image()
        for operation in operations:
            edited = apply_operation_to_image(edited, operation)
        self.preview_image = edited
        self.preview_image_current = True
        self.base_pixmap = pixmap_from_pil_image(edited)
        self._fit_pixmap()

    def display_preview_target_size(self) -> tuple[int, int]:
        display_rect = self.displayed_image_rect()
        device_pixel_ratio = self.image_device_pixel_ratio()
        return (
            max(1, int(round(display_rect.width() * device_pixel_ratio))),
            max(1, int(round(display_rect.height() * device_pixel_ratio))),
        )

    def ensure_display_preview_image(self) -> Image.Image:
        target_size = self.display_preview_target_size()
        if self.display_preview_image is not None and self.display_preview_size == target_size:
            return self.display_preview_image
        source = self.preview_image if self.preview_image is not None and self.preview_image_current else None
        if source is None:
            source = self.render_operations_to_image(self.operations)
        display = source.convert("RGB").resize(target_size, Image.Resampling.BILINEAR)
        self.display_preview_image = display
        self.display_preview_size = target_size
        return display

    def rebuild_display_preview(self) -> None:
        if self.display_preview_image is None:
            return
        self.display_preview_image = None
        self.display_preview_size = None
        self.ensure_display_preview_image()

    def apply_clone_operations_to_display(self, operations: list[EditOperation]) -> None:
        display = self.ensure_display_preview_image()
        width, height = self.display_preview_target_size()
        scale_x = width / max(1, self.base_pixmap.width())
        scale_y = height / max(1, self.base_pixmap.height())
        for operation in operations:
            params = operation.params or {}
            if operation.name != "clone_heal" or "source_center" not in params:
                display = apply_operation_to_image(display, operation)
                continue
            source_x, source_y = params["source_center"]
            target_x, target_y = params["target_center"]
            clone_heal_brush_in_place(
                display,
                (int(source_x * scale_x), int(source_y * scale_y)),
                (int(target_x * scale_x), int(target_y * scale_y)),
                max(1, int(params["radius"] * max(scale_x, scale_y))),
            )
        self.display_preview_image = display
        self.display_preview_size = (width, height)
        self.label.set_display_pixmap(
            pixmap_from_pil_image(display, device_pixel_ratio=self.image_device_pixel_ratio()),
            self.displayed_image_rect(),
        )

    def confirm_pending_edits(self) -> bool:
        if not self.operations:
            return True
        response = self.run_with_visible_cursor(lambda: ask_save_edits(self))
        if response == "cancel":
            return False
        if response in {"save", "save_preserve_date"}:
            try:
                parent = self.parent()
                if isinstance(parent, MainWindow):
                    mutation = parent.queue_image_edit(
                        self.catalog,
                        self.navigator.current,
                        self.operations,
                        preserve_file_dates=response == "save_preserve_date",
                        expected_identity=self.loaded_file_identity,
                    )
                    if not self._wait_for_image_edit(parent, mutation):
                        return False
                else:
                    apply_operations_to_file(
                        self.catalog.mutation_path(self.navigator.current),
                        self.operations,
                        preserve_timestamp=False,
                        preserve_file_dates=response == "save_preserve_date",
                        expected_identity=self.loaded_file_identity,
                    )
                    with suppress(Exception):
                        self.catalog.rebuild_thumbnail(self.navigator.current)
            except (OSError, ValueError, UnsafeImageSaveError) as error:
                show_error(self, "Save Image", str(error))
                return False
        self.operations.clear()
        self.cleanup_preview()
        self.load_current()
        return True

    def _wait_for_image_edit(self, window: MainWindow, mutation: MovePayloadTask) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("Saving Image")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        label = QLabel("Saving image without losing metadata or frames...")
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(label)
        layout.addWidget(progress)

        def update() -> None:
            snapshot = mutation.task.snapshot()
            if snapshot.total is not None:
                progress.setRange(0, max(snapshot.total, 1))
                progress.setValue(min(snapshot.processed, max(snapshot.total, 1)))
            if snapshot.current:
                label.setText(f"Saving {snapshot.current}")
            if mutation.future.done():
                dialog.accept()

        timer = QTimer(dialog)
        timer.setInterval(50)
        timer.timeout.connect(update)
        timer.start()
        update()
        if not mutation.future.done():
            dialog.exec()
        timer.stop()
        try:
            mutation.future.result()
        except Exception:
            window._settle_move_payload_task()
            return False
        window._settle_move_payload_task()
        return True

    def cleanup_preview(self) -> None:
        if self.preview_path is not None:
            self.preview_path.unlink(missing_ok=True)
            self.preview_path = None
        self.preview_image = None
        self.preview_image_current = False
        self.display_preview_image = None
        self.display_preview_size = None


def load_app_icon() -> QIcon:
    pixmap = QPixmap()
    pixmap.loadFromData(app_icon_bytes(), "PNG")
    return QIcon(pixmap)


def load_virtual_folder_icon() -> QIcon:
    pixmap = QPixmap()
    pixmap.loadFromData(virtual_folder_icon_bytes(), "PNG")
    return QIcon(pixmap)


def widget_device_pixel_ratio(widget: QWidget) -> float:
    window = widget.window()
    handle = window.windowHandle() if window is not None else None
    screen = handle.screen() if handle is not None else None
    if screen is None:
        screen = widget.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    return max(1.0, float(screen.devicePixelRatio()) if screen is not None else 1.0)


def physical_size_for_logical(size: QSize, device_pixel_ratio: float) -> QSize:
    return QSize(
        max(1, int(round(size.width() * device_pixel_ratio))),
        max(1, int(round(size.height() * device_pixel_ratio))),
    )


def logical_size_for_physical(size: QSize, device_pixel_ratio: float) -> QSize:
    return QSize(
        max(1, int(ceil(size.width() / device_pixel_ratio))),
        max(1, int(ceil(size.height() / device_pixel_ratio))),
    )


def load_oriented_pixmap(path: Path) -> QPixmap:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if not image.isNull():
        return QPixmap.fromImage(image)
    return QPixmap(str(path))


def pixmap_from_pil_image(image: Image.Image, *, device_pixel_ratio: float = 1.0) -> QPixmap:
    if image.mode == "RGBA":
        rgba = image
    elif "A" in image.getbands():
        rgba = image.convert("RGBA")
    else:
        rgb = image.convert("RGB")
        data = rgb.tobytes("raw", "RGB")
        qimage = QImage(data, rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimage)
        pixmap.setDevicePixelRatio(device_pixel_ratio)
        return pixmap
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()
    pixmap = QPixmap.fromImage(qimage)
    pixmap.setDevicePixelRatio(device_pixel_ratio)
    return pixmap


def format_bytes(size: int) -> str:
    units = ["B", "kB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def ask_delete_files(parent: QWidget, count: int) -> bool:
    box, delete_button = create_delete_message_box(parent, count)
    box.exec()
    return box.clickedButton() == delete_button


def ask_automatically_delete_duplicates(parent: QWidget) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("Automatically Delete Duplicates")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("Automatically move duplicates in this virtual directory to trash?")
    box.setInformativeText(
        f"Marnwick will keep one image from each duplicate group and move the rest into {TRASH_DIR_NAME}."
    )
    box.setStyleSheet(DIALOG_STYLESHEET)
    delete_button = box.addButton("Move Duplicates", QMessageBox.ButtonRole.AcceptRole)
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    style_message_box_buttons(box)
    box.exec()
    return box.clickedButton() == delete_button


def create_delete_message_box(parent: QWidget, count: int) -> tuple[QMessageBox, QPushButton]:
    box = QMessageBox(parent)
    box.setWindowTitle("Delete")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(f"Delete {count} selected image(s)?")
    box.setInformativeText("This removes the file from disk.")
    box.setStyleSheet(DIALOG_STYLESHEET)
    delete_button = box.addButton("Delete", QMessageBox.ButtonRole.AcceptRole)
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(delete_button)
    box.setEscapeButton(cancel_button)
    style_message_box_buttons(box)
    return box, delete_button


def ask_delete_directory(parent: QWidget, directory: Path) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("Delete Directory")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(f"Delete directory {directory.name}?")
    box.setInformativeText("This removes the directory and everything inside it from disk.")
    box.setStyleSheet(DIALOG_STYLESHEET)
    delete_button = box.addButton("Delete", QMessageBox.ButtonRole.AcceptRole)
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    style_message_box_buttons(box)
    box.exec()
    return box.clickedButton() == delete_button


def ask_save_edits(parent: QWidget) -> str:
    box, save_button, preserve_button, discard_button = create_save_edits_message_box(parent)
    box.exec()
    clicked = box.clickedButton()
    if clicked == save_button:
        return "save"
    if clicked == preserve_button:
        return "save_preserve_date"
    if clicked == discard_button:
        return "discard"
    return "cancel"


def create_save_edits_message_box(parent: QWidget | None) -> tuple[QMessageBox, QPushButton, QPushButton, QPushButton]:
    box = QMessageBox(parent)
    box.setWindowTitle("Save edits")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText("Save edits to this image?")
    box.setStyleSheet(DIALOG_STYLESHEET)
    save_button = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
    preserve_button = box.addButton("Save && Preserve Dates", QMessageBox.ButtonRole.AcceptRole)
    discard_button = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
    cancel_button = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(preserve_button)
    box.setEscapeButton(cancel_button)
    style_message_box_buttons(box)
    return box, save_button, preserve_button, discard_button


def style_message_box_buttons(box: QMessageBox) -> None:
    for button in box.buttons():
        button.setStyleSheet(MESSAGE_BUTTON_STYLESHEET)


def show_error(parent: QWidget, title: str, message: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(message)
    box.setStyleSheet(DIALOG_STYLESHEET)
    box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    style_message_box_buttons(box)
    box.exec()


def copy_files_to_clipboard(paths: list[Path]) -> None:
    existing_paths = [path.resolve() for path in paths if path.exists()]
    if not existing_paths:
        return
    urls = [QUrl.fromLocalFile(str(path)) for path in existing_paths]
    uri_lines = [url.toString() for url in urls]
    mime = QMimeData()
    mime.setUrls(urls)
    mime.setText("\n".join(uri_lines))
    mime.setData("x-special/gnome-copied-files", ("copy\n" + "\n".join(uri_lines)).encode("utf-8"))
    QApplication.clipboard().setMimeData(mime)


def parse_runtime_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    raw_argv = list(argv or [])
    if not raw_argv:
        raw_argv = ["marnwick"]
    parser = argparse.ArgumentParser(
        prog=raw_argv[0],
        description="Browse and organize local image catalogs.",
    )
    parser.add_argument("--codex-debug", action="store_true")
    parser.add_argument("--codex-debug-port", "--debug-port", dest="codex_debug_port", type=int, default=8675)
    parser.add_argument("--codex-debug-token-file", "--debug-token-file", dest="codex_debug_token_file")
    parser.add_argument("--codex-debug-token", "--debug-token", dest="deprecated_debug_token")
    args, remaining = parser.parse_known_args(raw_argv[1:])
    if args.deprecated_debug_token is not None:
        parser.error("--debug-token was removed; use MARNWICK_DEBUG_TOKEN or --debug-token-file")
    return args, [raw_argv[0], *remaining]


def read_debug_token_file(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    stat_result = path.stat()
    if os.name != "nt" and stat_result.st_mode & 0o077:
        raise PermissionError(f"debug token file must not be readable by group or others: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("debug token file is empty")
    return token


def run(argv: list[str] | None = None) -> int:
    runtime_args, qt_argv = parse_runtime_args(argv)
    app = QApplication(qt_argv)
    app.setApplicationName("Marnwick")
    app.setApplicationDisplayName("Marnwick")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(DESKTOP_FILE_ID)
    app.setWindowIcon(load_app_icon())
    window = MainWindow()
    if runtime_args.codex_debug:
        from .debug import DebugCommandServer

        supplied_token = os.environ.get("MARNWICK_DEBUG_TOKEN") or read_debug_token_file(
            runtime_args.codex_debug_token_file
        )
        window.debug_command_server = DebugCommandServer(
            window,
            port=runtime_args.codex_debug_port,
            token=supplied_token,
        )
        if not supplied_token:
            print(f"Marnwick debug token: {window.debug_command_server.token}", file=sys.stderr)
    window.show()
    return app.exec()
