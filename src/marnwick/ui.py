from __future__ import annotations

import json
from math import ceil, hypot
import os
from collections import OrderedDict, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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

from .app_icon import DESKTOP_FILE_ID, app_icon_bytes
from .catalog import Catalog, is_image_name, parse_tag_entry
from .config import (
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
    apply_operation_to_image,
    clone_heal_brush_in_place,
    save_image,
    save_image_preserving_file_dates,
)
from .indexer import BackgroundIndexer, IndexTask
from .models import CatalogSettings, DirectoryRecord, ImageRecord, PaneRecord, SortOrder
from .navigation import ImageNavigator
from .workspace import Workspace

CATALOG_ROOT_ROLE = Qt.ItemDataRole.UserRole
DIR_REL_ROLE = Qt.ItemDataRole.UserRole + 1
TIMINGS_FILE_NAME = "timings.json"
MAX_TIMING_EVENTS = 1000


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
        self.tile_size = 160
        self.device_pixel_ratio = 1.0
        self._pixmap_cache: dict[str, QPixmap] = {}

    def set_images(self, catalog: Catalog | None, images: list[PaneRecord]) -> None:
        self.beginResetModel()
        self.catalog = catalog
        self.images = images
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
        if not self.images:
            return
        top_left = self.index(0, 0)
        bottom_right = self.index(len(self.images) - 1, 0)
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
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])
                continue
            self._pixmap_cache.pop(rel_path, None)
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])
            return

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.images)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or index.row() >= len(self.images):
            return None
        record = self.images[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return record.filename
        if role == Qt.ItemDataRole.UserRole:
            return record.rel_path
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
        if len(preview_items) < 4 and self.catalog is not None:
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
            painter.fillRect(rect, QColor("#dbeafe"))

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
        painter.setPen(QColor("#111827") if not is_selected else QColor("#111827"))
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


class DragPreviewOverlay(QLabel):
    def __init__(self, pixmap: QPixmap, hotspot: QPoint) -> None:
        super().__init__(None)
        self.hotspot = hotspot
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setPixmap(pixmap)
        self.resize(pixmap.size())

    def start(self, global_pos: QPoint) -> None:
        self.move_to_global(global_pos)
        self.show()

    def stop(self) -> None:
        self.hide()

    def move_to_global(self, global_pos: QPoint) -> None:
        self.move(global_pos - self.hotspot)


class ThumbnailView(QListView):
    SMOOTH_SCROLL_STEP = 24

    def __init__(self, window: "MainWindow | None" = None) -> None:
        super().__init__()
        self.main_window = window
        self._drag_start_pos: QPoint | None = None
        self._drag_payload: list[dict[str, str]] | None = None
        self._drag_overlay: DragPreviewOverlay | None = None
        self._drag_destination_item: QTreeWidgetItem | None = None
        self._drag_cursor_active = False
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

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._drag_overlay is not None:
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
        if self._drag_overlay is not None:
            self.finish_manual_drag(event.globalPosition().toPoint())
            event.accept()
            return
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

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
        overlay = DragPreviewOverlay(drag_pixmap, hotspot)
        overlay.start(QCursor.pos())
        try:
            drag.exec(supported_actions, Qt.DropAction.MoveAction)
        finally:
            overlay.stop()
            overlay.deleteLater()

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
        model = self.model()
        if model is None:
            return False
        mime_data = model.mimeData(indexes)
        if not mime_data.hasFormat(ThumbnailModel.MIME_TYPE):
            return False
        data = bytes(mime_data.data(ThumbnailModel.MIME_TYPE)).decode("utf-8")
        self._drag_payload = json.loads(data)
        drag_pixmap = self.drag_pixmap_for_indexes(indexes)
        hotspot = QPoint(int(drag_pixmap.width() / 2), int(drag_pixmap.height() / 2))
        self._drag_overlay = DragPreviewOverlay(drag_pixmap, hotspot)
        self._drag_overlay.start(global_pos)
        QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
        self._drag_cursor_active = True
        self.update_manual_drag(global_pos)
        return True

    def update_manual_drag(self, global_pos: QPoint) -> None:
        if self._drag_overlay is not None:
            self._drag_overlay.move_to_global(global_pos)
        item = self.tree_item_at_global(global_pos)
        self._drag_destination_item = item
        if self.main_window is not None:
            self.main_window.tree.set_drag_hover_item(item)

    def finish_manual_drag(self, global_pos: QPoint) -> None:
        self.update_manual_drag(global_pos)
        move_request: tuple["MainWindow", list[dict[str, str]], Path, str] | None = None
        if self.main_window is not None and self._drag_payload is not None and self._drag_destination_item is not None:
            item = self._drag_destination_item
            root = Path(item.data(0, CATALOG_ROOT_ROLE))
            dir_rel = item.data(0, DIR_REL_ROLE)
            move_request = (self.main_window, list(self._drag_payload), root, dir_rel)
        self.cleanup_manual_drag()
        if move_request is not None:
            window, payload, root, dir_rel = move_request
            QTimer.singleShot(0, lambda: window.move_payload_to_directory(payload, root, dir_rel))

    def cleanup_manual_drag(self) -> None:
        if self.main_window is not None:
            self.main_window.tree.set_drag_hover_item(None)
        if self._drag_overlay is not None:
            self._drag_overlay.stop()
            self._drag_overlay.deleteLater()
        if self._drag_cursor_active:
            QApplication.restoreOverrideCursor()
        self._drag_start_pos = None
        self._drag_payload = None
        self._drag_overlay = None
        self._drag_destination_item = None
        self._drag_cursor_active = False

    def tree_item_at_global(self, global_pos: QPoint) -> QTreeWidgetItem | None:
        if self.main_window is None:
            return None
        tree = self.main_window.tree
        viewport_pos = tree.viewport().mapFromGlobal(global_pos)
        if not tree.viewport().rect().contains(viewport_pos):
            return None
        return tree.itemAt(viewport_pos)

    def drag_pixmap_for_indexes(self, indexes: list[QModelIndex]) -> QPixmap:
        size = 128
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(0.78)
        painter.setBrush(QBrush(QColor("#eef2ff")))
        painter.setPen(QPen(QColor("#2563eb"), 2))
        painter.drawRoundedRect(2, 2, size - 4, size - 4, 6, 6)

        thumbnail = self._drag_thumbnail(indexes[0])
        if not thumbnail.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            target = QRect(10, 10, size - 20, size - 20)
            scaled_size = thumbnail.size()
            scaled_size.scale(target.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(
                target.left() + int((target.width() - scaled_size.width()) / 2),
                target.top() + int((target.height() - scaled_size.height()) / 2),
                scaled_size.width(),
                scaled_size.height(),
            )
            painter.drawPixmap(target, thumbnail)
        if len({index.row() for index in indexes}) > 1:
            painter.setOpacity(0.92)
            painter.setBrush(QBrush(QColor("#2563eb")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(size - 38, size - 38, 32, 32)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(QRect(size - 38, size - 38, 32, 32), Qt.AlignmentFlag.AlignCenter, str(len(indexes)))
        painter.end()
        return pixmap

    def _drag_thumbnail(self, index: QModelIndex) -> QPixmap:
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(decoration, QPixmap):
            return decoration
        if isinstance(decoration, QIcon):
            return decoration.pixmap(QSize(64, 64))
        return self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon).pixmap(QSize(64, 64))


class DirectoryTree(QTreeWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self._drag_hover_item: QTreeWidgetItem | None = None
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_context_menu)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None or item.parent() is None:
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
        drag.exec(supported_actions, Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasFormat(ThumbnailModel.MIME_TYPE):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasFormat(ThumbnailModel.MIME_TYPE):
            self.set_drag_hover_item(self.itemAt(event.position().toPoint()))
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
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        dir_rel = item.data(0, DIR_REL_ROLE)
        data = bytes(event.mimeData().data(ThumbnailModel.MIME_TYPE)).decode("utf-8")
        self.window.move_payload_to_directory(json.loads(data), root, dir_rel)
        event.acceptProposedAction()

    def set_drag_hover_item(self, item: QTreeWidgetItem | None) -> None:
        if self._drag_hover_item is item:
            return
        if self._drag_hover_item is not None:
            self._drag_hover_item.setBackground(0, QBrush())
            self._drag_hover_item.setForeground(0, QBrush())
        self._drag_hover_item = item
        if item is not None:
            item.setBackground(0, QBrush(QColor("#dbeafe")))
            item.setForeground(0, QBrush(QColor("#111827")))

    def _open_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        item = self.itemAt(pos)
        if item is None:
            return
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        dir_rel = item.data(0, DIR_REL_ROLE)
        menu = QMenu(self)
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
        if selected == properties_action:
            self.window.open_directory_properties(root, dir_rel)
        elif selected == create_action:
            self.window.create_directory(root, dir_rel)
        elif delete_action is not None and selected == delete_action:
            self.window.delete_directory(root, dir_rel)
        elif preferences_action is not None and selected == preferences_action:
            self.window.open_catalog_preferences(root)
        elif selected == tags_action:
            self.window.open_catalog_tags(root)
        elif selected == close_action:
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
        self.current_catalog: Catalog | None = None
        self.current_dir_rel = ""
        self.current_sort = self.sort_order_from_config(self.app_config.sort_order)
        self._swept_catalog_roots: set[Path] = set()
        self._pruned_catalog_roots: set[Path] = set()
        self._idle_index_tasks: dict[Path, IndexTask] = {}
        self._directory_discovery_tasks: dict[Path, IndexTask] = {}
        self._directory_index_tasks: dict[tuple[Path, str], IndexTask] = {}
        self._thumbnail_prune_tasks: dict[Path, IndexTask] = {}
        self._resume_idle_refresh_roots: set[Path] = set()
        self._shallow_tree_roots: set[Path] = set()
        self._catalog_open_tasks: dict[Future[CatalogOpenResult], CatalogOpenTask] = {}
        self._indexing_was_active = False

        self.tree = DirectoryTree(self)
        self.tree.itemClicked.connect(self._directory_clicked)

        self.model = ThumbnailModel()
        self.model.set_tile_size(self.app_config.thumbnail_size)
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
        self.size_slider.setRange(64, 4096)
        self.size_slider.setValue(self.model.tile_size)
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
        self.native_thumbnail_button = QPushButton("Thumbnail size")
        self.native_thumbnail_button.setToolTip("Set display size to the selected catalog's saved thumbnail size")
        self.native_thumbnail_button.clicked.connect(self.set_thumbnail_size_to_native)
        controls.addWidget(self.native_thumbnail_button)
        controls.addWidget(self.size_slider, 1)
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
        self.save_window_config()
        self.catalog_open_executor.shutdown(wait=False, cancel_futures=True)
        self.indexer.shutdown()
        self.workspace.close()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self.refresh_thumbnail_layout()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self.refresh_thumbnail_layout()

    def restore_window_config(self) -> None:
        window = self.app_config.window
        self.resize(window.width, window.height)
        if window.x is not None and window.y is not None:
            self.move(window.x, window.y)
        if window.maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def restore_catalogs_from_config(self) -> None:
        if not self.config_enabled:
            return
        for catalog_path in self.app_config.catalogs:
            path = Path(catalog_path).expanduser()
            if path.is_dir():
                self.open_catalog(path, log_event=False)

    def save_window_config(self) -> None:
        if not self.config_enabled:
            return
        save_config(self.current_app_config(), self.config_path)

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
            catalogs=[str(catalog.root) for catalog in self.workspace.catalogs],
            thumbnail_size=self.model.tile_size,
            delete_behavior=self.app_config.delete_behavior,
            sort_order=self.current_sort.value,
        )

    def apply_app_config(self, config: AppConfig) -> None:
        self.app_config = config
        self.set_thumbnail_size(config.thumbnail_size)
        self.set_sort_order(self.sort_order_from_config(config.sort_order))
        self.sync_catalogs_to_config(config.catalogs)
        if config.window.maximized:
            self.showMaximized()
        else:
            if self.isMaximized():
                self.showNormal()
            self.resize(config.window.width, config.window.height)
            if config.window.x is not None and config.window.y is not None:
                self.move(config.window.x, config.window.y)
        if self.config_enabled:
            save_config(self.current_app_config(), self.config_path)

    def sync_catalogs_to_config(self, catalog_paths: list[str]) -> None:
        requested: list[Path] = []
        seen: set[Path] = set()
        for catalog_path in catalog_paths:
            path = Path(catalog_path).expanduser()
            if not path.is_dir():
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                requested.append(resolved)
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
        except ValueError:
            return SortOrder.NAME_ASC

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
                self.open_viewer(self.thumbnail_view.currentIndex(), random_mode=False)
                return True
            if key == Qt.Key.Key_S:
                self.open_viewer(self.thumbnail_view.currentIndex(), random_mode=True)
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
        self.logs_action = QAction("Logs", self)
        self.logs_action.triggered.connect(self.open_logs)
        self.tools_menu.addAction(self.logs_action)
        self.prune_thumbnails_action = QAction("Prune Thumbnails", self)
        self.prune_thumbnails_action.triggered.connect(self.prune_current_catalog_thumbnails)
        self.tools_menu.addAction(self.prune_thumbnails_action)
        self.preferences_action = QAction("Preferences", self)
        self.preferences_action.triggered.connect(self.open_app_preferences)
        self.tools_menu.addAction(self.preferences_action)

        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_catalog_dialog)

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
        try:
            new_dir_rel = catalog.create_directory(parent_dir_rel, dialog.directory_name())
        except (OSError, ValueError) as error:
            show_error(self, "Create Directory", str(error))
            return
        self._swept_catalog_roots.discard(catalog.root)
        self.current_catalog = catalog
        self.current_dir_rel = new_dir_rel
        self.rebuild_tree()
        self.load_current_directory()
        self.queue_directory_index(catalog, new_dir_rel)

    def delete_directory(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None or not dir_rel:
            return
        directory = catalog.abs_path(dir_rel)
        if not ask_delete_directory(self, directory):
            return
        self.indexer.cancel_idle_tasks(catalog.root)
        try:
            catalog.delete_directory(dir_rel, wipe=self.wipe_on_delete_enabled())
        except (OSError, ValueError) as error:
            show_error(self, "Delete Directory", str(error))
            return
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        if self.current_catalog and self.current_catalog.root == catalog.root:
            if self.current_dir_rel == dir_rel or self.current_dir_rel.startswith(f"{dir_rel}/"):
                parent_rel = Path(dir_rel).parent.as_posix()
                self.current_dir_rel = "" if parent_rel == "." else parent_rel
                self.current_catalog = catalog
        self.reload_tree_and_directory()
        self.queue_directory_index(catalog, self.current_dir_rel if self.current_catalog == catalog else "")

    def open_catalog(self, root: Path, *, log_event: bool = True) -> None:
        operation_started_at = monotonic()
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
        operation_started_at = monotonic()
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
            )
            return
        if any(task.root.expanduser() == root.expanduser() for task in self._catalog_open_tasks.values()):
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
    ) -> None:
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

        self.current_catalog = catalog
        self.current_dir_rel = ""

        phase_started_at = monotonic()
        self.rebuild_tree()
        self._record_timing_phase(catalog.root, "rebuild_tree", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
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
            state_dir.mkdir(parents=True, exist_ok=True)
            timings_path = state_dir / TIMINGS_FILE_NAME
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
            timings_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
            self._append_timing_event(result.catalog.root, "catalog_init", result.init_duration_ms)
            catalog, was_open = self.workspace.adopt_catalog(result.catalog)
            self._finish_open_catalog_ui(
                catalog,
                was_open=was_open,
                log_event=task.log_event,
                operation_started_at=task.started_at,
                mode="async",
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
            future.cancel()
            self._catalog_open_tasks.pop(future, None)
            self._append_timing_event(task.root, "catalog_init_canceled", None)

    def close_catalog(self, root: Path) -> None:
        resolved = root.resolve()
        self._cancel_catalog_open_tasks(resolved)
        catalog = self.workspace.catalog_for_root(root)
        if catalog is not None:
            catalog.append_log("Catalog removed from workspace")
        self.workspace.close_catalog(root)
        self._swept_catalog_roots.discard(resolved)
        self._pruned_catalog_roots.discard(resolved)
        self._idle_index_tasks.pop(resolved, None)
        self._directory_discovery_tasks.pop(resolved, None)
        self._directory_index_tasks = {
            key: task for key, task in self._directory_index_tasks.items() if key[0] != resolved
        }
        self._thumbnail_prune_tasks.pop(resolved, None)
        self._resume_idle_refresh_roots.discard(resolved)
        self._shallow_tree_roots.discard(resolved)
        if self.current_catalog and self.current_catalog.root == resolved:
            self.current_catalog = None
            self.current_dir_rel = ""
            self.model.set_images(None, [])
            self.update_selection_status()
        self.rebuild_tree()

    def rebuild_tree(self) -> None:
        expanded_items = self._expanded_tree_items()
        self.tree.clear()
        selected_item: QTreeWidgetItem | None = None
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
            for dir_rel in self._tree_directory_rels_for_catalog(catalog):
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
                if (catalog.root, dir_rel) in expanded_items:
                    item.setExpanded(True)
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
            self._expand_tree_item_ancestors(selected_item)
            self.tree.scrollToItem(selected_item)

    def _tree_directory_rels_for_catalog(self, catalog: Catalog) -> list[str]:
        if catalog.root in self._shallow_tree_roots:
            return catalog.list_filesystem_child_directory_rels("")
        return catalog.list_known_directories()

    def _expanded_tree_items(self) -> set[tuple[Path, str]]:
        expanded: set[tuple[Path, str]] = set()

        def visit(item: QTreeWidgetItem) -> None:
            if item.isExpanded():
                expanded.add((Path(item.data(0, CATALOG_ROOT_ROLE)).resolve(), item.data(0, DIR_REL_ROLE)))
            for index in range(item.childCount()):
                visit(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))
        return expanded

    def _is_current_tree_item(self, root: Path, dir_rel: str) -> bool:
        return (
            self.current_catalog is not None
            and self.current_catalog.root == root
            and self.current_dir_rel == dir_rel
        )

    def _expand_tree_item_ancestors(self, item: QTreeWidgetItem) -> None:
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

    def reload_tree_and_directory(self) -> None:
        self.rebuild_tree()
        self.load_current_directory()

    def _directory_clicked(self, item: QTreeWidgetItem) -> None:
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        dir_rel = item.data(0, DIR_REL_ROLE)
        idle_task = self._idle_index_tasks.get(catalog.root)
        if idle_task is not None and not idle_task.snapshot().done:
            self._resume_idle_refresh_roots.add(catalog.root)
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root, keep_dir_rel=dir_rel)
        self.current_catalog = catalog
        self.current_dir_rel = dir_rel
        self.load_current_directory()
        self.queue_directory_index(catalog, self.current_dir_rel)

    def load_current_directory(self) -> None:
        if self.current_catalog is None:
            self.model.set_images(None, [])
            self.update_selection_status()
            return
        images = self.current_catalog.list_images_with_placeholders(
            self.current_dir_rel,
            self.current_sort,
            include_blobs=False,
            placeholder_scan_budget_ms=15,
            placeholder_limit=2000,
        )
        if self.current_catalog.root in self._shallow_tree_roots:
            directories = self.current_catalog.list_filesystem_child_directories(
                self.current_dir_rel,
                self.current_sort,
            )
        else:
            directories = self.current_catalog.list_child_directories(self.current_dir_rel, self.current_sort)
        self.model.set_images(self.current_catalog, [*directories, *images])
        self.refresh_thumbnail_layout()
        self.update_selection_status()

    def _thumbnail_size_changed(self, value: int) -> None:
        self.set_thumbnail_size(value)

    def set_thumbnail_size(self, value: int) -> None:
        value = max(64, min(4096, int(value)))
        if value < self.size_slider.minimum() or value > self.size_slider.maximum():
            self.size_slider.setRange(min(self.size_slider.minimum(), value), max(self.size_slider.maximum(), value))
        if self.size_slider.value() != value:
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(value)
            self.size_slider.blockSignals(False)
        self.model.set_tile_size(value)
        self.app_config.thumbnail_size = value
        self.refresh_thumbnail_layout()

    def refresh_thumbnail_layout(self) -> None:
        self.model.set_device_pixel_ratio(widget_device_pixel_ratio(self.thumbnail_view))
        logical_tile_size = self.model.logical_tile_size()
        self.thumbnail_view.setIconSize(QSize(logical_tile_size, logical_tile_size))
        self.thumbnail_view.setGridSize(
            self.thumbnail_grid_size_for_width(self.thumbnail_view.viewport().width())
        )

    def thumbnail_grid_size_for_width(self, available_width: int) -> QSize:
        card_size = self.model.card_size(self.thumbnail_view.font())
        card_width = max(1, card_size.width())
        available_width = max(1, int(available_width))
        columns = max(1, available_width // card_width)
        grid_width = max(card_width, available_width // columns)
        return QSize(grid_width, card_size.height())

    def set_thumbnail_size_to_native(self) -> None:
        if self.current_catalog is None:
            return
        self.set_thumbnail_size(self.current_catalog.settings.thumbnail_native_size)

    def _sort_changed(self) -> None:
        self.set_sort_order(SortOrder(self.sort_combo.currentData()))
        self.load_current_directory()

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

    def delete_selected(self) -> None:
        if self.current_catalog is None:
            return
        rel_paths = self.selected_rel_paths()
        if not rel_paths:
            return
        if not ask_delete_files(self, len(rel_paths)):
            return
        self.indexer.cancel_idle_tasks(self.current_catalog.root)
        self.current_catalog.delete_images(rel_paths, wipe=self.wipe_on_delete_enabled())
        self._pruned_catalog_roots.discard(self.current_catalog.root)
        self.load_current_directory()

    def _open_thumbnail_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        if self.current_catalog is None:
            return
        index = self.thumbnail_view.indexAt(pos)
        if not index.isValid() or index.row() >= len(self.model.images):
            return
        if not self.thumbnail_view.selectionModel().isSelected(index):
            self.thumbnail_view.selectionModel().clearSelection()
            self.thumbnail_view.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)
            self.thumbnail_view.setCurrentIndex(index)
        record = self.model.images[index.row()]
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        metadata_action = menu.addAction("Metadata")
        selected = menu.exec(self.thumbnail_view.viewport().mapToGlobal(pos))
        if selected == delete_action:
            self.delete_selected()
        elif selected == metadata_action:
            MetadataDialog(record.absolute_path, self).exec()

    def open_tag_dialog_for_selection(self) -> None:
        if self.current_catalog is None:
            return
        rel_paths = self.selected_rel_paths()
        if len(rel_paths) != 1:
            return
        dialog = TagDialog(self.current_catalog, rel_paths[0], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.current_catalog.set_image_tags(rel_paths[0], dialog.selected_tags(), replace=True)

    def open_viewer(self, index: QModelIndex, *, random_mode: bool) -> None:
        if self.current_catalog is None or not index.isValid() or index.row() >= len(self.model.images):
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
        viewer = FullscreenViewer(self.current_catalog, navigator, self, wipe_on_delete=self.wipe_on_delete_enabled())
        viewer.showFullScreen()
        viewer.exec()
        last_viewed = viewer.last_viewed_rel_path
        self.load_current_directory()
        self.select_rel_path(last_viewed)

    def navigate_to_directory(self, dir_rel: str) -> None:
        if self.current_catalog is None:
            return
        self.current_dir_rel = dir_rel
        self.load_current_directory()
        self.queue_directory_index(self.current_catalog, dir_rel)
        self.rebuild_tree()

    def move_payload_to_directory(self, payload: list[dict[str, str]], dest_root: Path, dest_dir_rel: str) -> None:
        dest_catalog = self.workspace.catalog_for_root(dest_root)
        if dest_catalog is None:
            return
        image_groups: dict[Path, list[str]] = defaultdict(list)
        directory_groups: dict[Path, list[str]] = defaultdict(list)
        for item in payload:
            group = directory_groups if item.get("kind") == "directory" else image_groups
            group[Path(item["catalog_root"]).resolve()].append(item["rel_path"])
        affected_roots = {dest_catalog.root, *directory_groups.keys(), *image_groups.keys()}
        for root in affected_roots:
            self.indexer.cancel_idle_tasks(root)
            self.indexer.cancel_directory_tasks(root)
        try:
            for source_root, dir_rels in directory_groups.items():
                source_catalog = self.workspace.catalog_for_root(source_root)
                if source_catalog is None:
                    continue
                source_catalog.move_directories(
                    dir_rels,
                    dest_catalog,
                    dest_dir_rel,
                    wipe_on_delete=self.wipe_on_delete_enabled(),
                )
            for source_root, rel_paths in image_groups.items():
                source_catalog = self.workspace.catalog_for_root(source_root)
                if source_catalog is not None:
                    source_catalog.move_images(
                        rel_paths,
                        dest_catalog,
                        dest_dir_rel,
                        wipe_on_delete=self.wipe_on_delete_enabled(),
                    )
        except (OSError, ValueError) as error:
            show_error(self, "Move", str(error))
            return
        for source_root in [*directory_groups.keys(), *image_groups.keys(), dest_catalog.root]:
            resolved_root = Path(source_root).resolve()
            self._swept_catalog_roots.discard(resolved_root)
            self._pruned_catalog_roots.discard(resolved_root)
        self.reload_tree_and_directory()

    def select_rel_path(self, rel_path: str) -> None:
        for row, record in enumerate(self.model.images):
            if record.rel_path != rel_path:
                continue
            index = self.model.index(row, 0)
            selection = self.thumbnail_view.selectionModel()
            selection.clearSelection()
            selection.select(index, QItemSelectionModel.SelectionFlag.Select)
            self.thumbnail_view.setCurrentIndex(index)
            self.thumbnail_view.scrollTo(index, QListView.ScrollHint.PositionAtCenter)
            self.update_selection_status()
            return

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
        if self._has_active_catalog_open_tasks():
            return
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        if self.indexer.has_active_tasks():
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
        self._settle_catalog_open_tasks()
        active_open_task = self._active_catalog_open_task()
        if active_open_task is not None:
            self._show_catalog_open_status(active_open_task.root)
            return
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        snapshots = self.indexer.active_snapshots()
        if not snapshots:
            if self._indexing_was_active:
                self._indexing_was_active = False
                self.reload_tree_and_directory()
            self._schedule_idle_indexing()
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            self.statusBar().clearMessage()
            return

        self._indexing_was_active = True
        snapshot = sorted(snapshots, key=lambda item: (item.interactive, item.started_at), reverse=True)[0]
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

    def _settle_directory_index_tasks(self) -> None:
        completed_roots: set[Path] = set()
        for key, task in list(self._directory_index_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._directory_index_tasks.pop(key, None)
            completed_roots.add(key[0])
        for root in completed_roots:
            if any(key[0] == root for key in self._directory_index_tasks):
                continue
            if root in self._resume_idle_refresh_roots:
                self._resume_idle_refresh_roots.discard(root)
                self._swept_catalog_roots.discard(root)

    def _settle_directory_discovery_tasks(self) -> None:
        should_rebuild = False
        for root, task in list(self._directory_discovery_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._directory_discovery_tasks.pop(root, None)
            self._shallow_tree_roots.discard(root)
            if snapshot.error is None and not snapshot.canceled:
                should_rebuild = True
        if should_rebuild:
            self.rebuild_tree()

    def _settle_thumbnail_prune_tasks(self) -> None:
        for root, task in list(self._thumbnail_prune_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._thumbnail_prune_tasks.pop(root, None)
            if snapshot.error is None and not snapshot.canceled:
                self._pruned_catalog_roots.add(root)


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


class AppPreferencesDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.thumbnail_size.setRange(64, 4096)
        self.thumbnail_size.setSingleStep(16)
        self.thumbnail_size.setValue(config.thumbnail_size)

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
        form.addRow("Thumbnail display size", self.thumbnail_size)
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
        with Image.open(image_path) as image:
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
        self.movie: QMovie | None = None
        self.zoom_level = 1.0
        self.pan_offset = QPoint(0, 0)
        self.pan_drag_start: QPoint | None = None
        self.pan_offset_at_drag_start = QPoint(0, 0)
        self.setWindowTitle("Marnwick")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet("background: black;")
        self.label = ImageDisplayLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setMouseTracking(True)
        self.label.installEventFilter(self)
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.label)
        self.clone_overlay = CloneBrushOverlay(self.label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.label, 1)
        self.load_current()

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

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched == self.label and self.edit_mode == "clone_heal":
            return self.handle_clone_event(event)
        if watched == self.label and self.edit_mode is not None:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
                self.drag_origin = event.position().toPoint()  # type: ignore[attr-defined]
                self.rubber_band.setGeometry(QRect(self.drag_origin, QSize()))
                self.rubber_band.show()
                return True
            if event.type() == QEvent.Type.MouseMove and self.drag_origin is not None:
                current = event.position().toPoint()  # type: ignore[attr-defined]
                self.rubber_band.setGeometry(QRect(self.drag_origin, current).normalized())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and self.drag_origin is not None:
                current = event.position().toPoint()  # type: ignore[attr-defined]
                rect = QRect(self.drag_origin, current).normalized()
                self.drag_origin = None
                self.rubber_band.hide()
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
            self.label.setCursor(Qt.CursorShape.ClosedHandCursor)
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
            self.label.setCursor(Qt.CursorShape.OpenHandCursor if self.is_zoomed() else Qt.CursorShape.ArrowCursor)
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
        if hasattr(self, "pan_offset"):
            self.pan_offset = self.clamped_pan_offset(self.pan_offset)
        self._fit_pixmap()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self.confirm_pending_edits():
            event.ignore()
            return
        self.stop_movie()
        self.cleanup_preview()
        super().closeEvent(event)

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
        if not ask_delete_files(self, 1):
            return
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
        self.preview_image = None
        self.preview_image_current = False
        self.display_preview_image = None
        self.display_preview_size = None
        self.base_pixmap = load_oriented_pixmap(self.current_path)
        if self.current_path.suffix.casefold() == ".gif":
            self.start_movie()
            return
        self._fit_pixmap()

    def start_movie(self) -> None:
        movie = QMovie(str(self.current_path))
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        movie.setScaledSize(self.displayed_image_rect().size())
        self.label.clear_display_pixmap()
        self.label.setMovie(movie)
        movie.start()
        self.movie = movie

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
        self.label.setCursor(Qt.CursorShape.OpenHandCursor)
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
        if self.edit_mode is None:
            self.label.unsetCursor()
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
        dialog = TagDialog(self.catalog, self.navigator.current, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.catalog.set_image_tags(self.navigator.current, dialog.selected_tags(), replace=True)

    def open_edit_tools(self) -> None:
        dialog = EditCommandDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
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
            self.label.setCursor(Qt.CursorShape.BlankCursor)
        else:
            self.clone_overlay.update_brush(None, self.clone_brush_radius_label, False)
            self.label.setCursor(Qt.CursorShape.CrossCursor)

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
        if hasattr(self, "label"):
            self.label.unsetCursor()

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
                    {"left": box[0], "top": box[1], "right": box[2], "bottom": box[3]},
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
        self.preview_image = self.render_operations_to_image(self.operations)
        self.preview_image_current = True
        self.display_preview_image = None
        self.display_preview_size = None
        self.base_pixmap = pixmap_from_pil_image(self.preview_image)
        self._fit_pixmap()

    def render_operations_to_image(self, operations: list[EditOperation]) -> Image.Image:
        with Image.open(self.current_path) as image:
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
        response = ask_save_edits(self)
        if response == "cancel":
            return False
        if response in {"save", "save_preserve_date"}:
            image = (
                self.preview_image
                if self.preview_image is not None and self.preview_image_current
                else self.render_operations_to_image(self.operations)
            )
            if response == "save_preserve_date":
                save_image_preserving_file_dates(self.current_path, image)
                self.catalog.append_log(f"File edit saved with preserved dates: {self.navigator.current}")
            else:
                save_image(self.current_path, image)
                self.catalog.append_log(f"File edit saved: {self.navigator.current}")
            self.catalog.rebuild_thumbnail(self.navigator.current)
        self.operations.clear()
        self.cleanup_preview()
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
    box.setDefaultButton(save_button)
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


def run(argv: list[str] | None = None) -> int:
    app = QApplication(argv or [])
    app.setApplicationName("Marnwick")
    app.setApplicationDisplayName("Marnwick")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(DESKTOP_FILE_ID)
    app.setWindowIcon(load_app_icon())
    window = MainWindow()
    window.show()
    return app.exec()
