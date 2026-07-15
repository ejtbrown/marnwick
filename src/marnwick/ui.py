from __future__ import annotations

import argparse
import hashlib
import io
import json
from math import ceil, hypot
import os
import sqlite3
import stat
import sys
import tempfile
import weakref
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Event, Lock
from time import monotonic

from PySide6.QtCore import (
    QAbstractListModel,
    QBuffer,
    QByteArray,
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPoint,
    QRect,
    QSize,
    Signal,
    Qt,
    QTimer,
    QUrl,
    QIODevice,
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

from .async_utils import (
    AbandonableThreadPoolExecutor,
    AtomicSaveThreadPoolExecutor,
    ExecutorSaturatedError,
    LatestOnlyThreadPoolExecutor,
    RolloverThreadPoolExecutor,
    SharedExecutorLease,
    shared_dialog_executor,
    shared_viewer_load_executor,
    shared_viewer_page_executor,
    shared_viewer_preview_executor,
)
from .app_icon import DESKTOP_FILE_ID, app_icon_bytes, virtual_folder_icon_bytes
from .catalog import (
    DUPLICATE_DELETE_EXACT,
    DUPLICATE_DELETE_VERY_SIMILAR,
    QUERY_PAGE_MAX_SIZE,
    TRASH_DIR_NAME,
    Catalog,
    CatalogStorageIdentity,
    DuplicateMatchGroups,
    DuplicateDeletionResult,
    is_inside_trash_rel_path,
    is_exact_image_hash,
    is_marnwick_internal_artifact_name,
    is_trash_rel_path,
    is_image_name,
    normalize_tag,
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
    FileDateSnapshot,
    ImageFileIdentity,
    ImageSaveCommittedError,
    UnsafeImageSaveError,
    apply_operation_to_image,
    apply_operations_to_file,
    apply_operations_to_file_with_proof,
    clone_heal_brush_in_place,
    snapshot_image_file_identity,
    snapshot_image_file_identity_with_dates,
)
from .indexer import ActionPriority, BackgroundIndexer, IndexTask, IndexTaskCancelled
from .models import CatalogSettings, DirectoryRecord, FolderPreviewRecord, ImageRecord, PaneRecord, SortOrder
from .navigation import ImageNavigator
from .safe_image import MAX_IMAGE_PIXELS, open_catalog_image
from .workspace import Workspace

CATALOG_ROOT_ROLE = Qt.ItemDataRole.UserRole
DIR_REL_ROLE = Qt.ItemDataRole.UserRole + 1
VIRTUAL_KIND_ROLE = Qt.ItemDataRole.UserRole + 2
VIRTUAL_VALUE_ROLE = Qt.ItemDataRole.UserRole + 3
TREE_LOAD_MORE_ROLE = Qt.ItemDataRole.UserRole + 4
TREE_LOAD_MORE_TAGS_ROLE = Qt.ItemDataRole.UserRole + 5
TIMINGS_FILE_NAME = "timings.json"
MAX_TIMING_EVENTS = 1000
MAX_TIMING_FILE_BYTES = 8 * 1024 * 1024
TREE_BUILD_BATCH_SIZE = 400
TREE_BUILD_BUDGET_SECONDS = 0.008
TREE_PAGE_POLL_INTERVAL_MS = 15
INITIAL_TREE_ENTRY_LIMIT = 512
INITIAL_TREE_SCAN_BUDGET_SECONDS = 0.006
MAX_AUTOMATIC_TREE_ITEMS = 4096
TREE_CHILD_PAGE_SIZE = 400
MAX_TREE_TAG_ITEMS = 500
THUMBNAIL_MODEL_BATCH_SIZE = 400
# Database-backed pane pages are intentionally smaller than the legacy model
# exposure batch. The first query and its row objects stay bounded while still
# filling several screens on typical displays.
PANE_QUERY_PAGE_SIZE = 200
# Rebuilds may need to locate the currently displayed image far beyond page
# zero. Larger worker-side pages keep that progressive search responsive while
# remaining bounded and leaving all SQLite/image work off the GUI thread.
VIEWER_REBUILD_PAGE_SIZE = 2048
PHYSICAL_PREVIEW_DIRECTORY_LIMIT = 128
PHYSICAL_PREVIEW_SCAN_BUDGET_SECONDS = 0.012
PENDING_THUMBNAIL_REFRESH_BATCH_SIZE = 32
# Submit at most four reads in the active epoch. If all four become stuck, one
# bounded rollover exposes four replacement lanes for the newly selected pane.
MAX_PENDING_THUMBNAIL_LOADS = 4
MAX_WAITING_THUMBNAIL_LOADS = 256
THUMBNAIL_RESULTS_PER_TICK = 4
MAX_THUMBNAIL_PIXMAP_CACHE_ITEMS = 512
MAX_THUMBNAIL_PIXMAP_CACHE_BYTES = 256 * 1024 * 1024
MAX_PENDING_THUMBNAIL_RETRIES = 2048
MAX_THUMBNAIL_VIEW_STATES = 256
MAX_VERY_SIMILAR_CACHE_ENTRIES = 8
MAX_CATALOG_OPEN_TASKS = 8
MAX_PENDING_IMAGE_SAVES = 8
MAX_THUMBNAIL_BLOB_BYTES = 32 * 1024 * 1024
MAX_THUMBNAIL_DIMENSION = 4096
MAX_THUMBNAIL_DECODE_PIXELS = MAX_THUMBNAIL_DIMENSION * MAX_THUMBNAIL_DIMENSION
MAX_INTERACTIVE_PREVIEW_DIMENSION = 4096
MAX_ANIMATED_IMAGE_BYTES = 128 * 1024 * 1024
IMAGE_RECONCILE_MAX_ATTEMPTS = 3
IMAGE_RECONCILE_RETRY_BASE_MS = 250
VIRTUAL_KIND_ROOT = "virtual-root"
VIRTUAL_KIND_TAG_ROOT = "tag-root"
VIRTUAL_KIND_TAG = "tag"
VIRTUAL_KIND_DUPLICATES = "duplicates"
VIRTUAL_KIND_VERY_SIMILAR = "very-similar"
VIRTUAL_KIND_PHYSICAL = "physical"
VIRTUAL_KIND_PHYSICAL_PREVIEW = "physical-preview"
TreeStateKey = tuple[Path, str, str, str]


@dataclass(slots=True)
class CatalogOpenResult:
    catalog: Catalog
    init_duration_ms: float


@dataclass(slots=True)
class ViewerLoadResult:
    rel_path: str
    path: Path
    identity: ImageFileIdentity
    original_file_dates: FileDateSnapshot
    image: QImage
    movie_bytes: bytes | None = None


@dataclass(slots=True)
class PreviewRenderResult:
    rel_path: str
    operations: tuple[EditOperation, ...]
    image: Image.Image
    image_size: tuple[int, int]


@dataclass(slots=True)
class ViewerNavigationPage:
    rel_paths: list[str]
    next_offset: int
    has_more: bool
    total_images: int


@dataclass(slots=True)
class PagedImageNavigator:
    """A loaded-prefix navigator whose later database pages are fetched lazily.

    Random navigation is progressive: the already loaded prefix is shuffled,
    then each newly fetched page is shuffled independently without repeats.
    That keeps memory bounded while still crossing every page boundary.
    """

    order: list[str]
    index: int
    next_offset: int
    has_more: bool
    total_count: int
    page_loader: Callable[[int, int, Event], ViewerNavigationPage]
    random_mode: bool = False
    view_kind: str = VIRTUAL_KIND_PHYSICAL
    _seen: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._seen.update(self.order)

    @property
    def current(self) -> str:
        return self.order[self.index]

    def next(self) -> str | None:
        if self.index + 1 >= len(self.order):
            return None
        self.index += 1
        return self.current

    def previous(self) -> str | None:
        if self.index <= 0:
            return None
        self.index -= 1
        return self.current

    def append_page(self, page: ViewerNavigationPage) -> int:
        rel_paths = [rel_path for rel_path in page.rel_paths if rel_path not in self._seen]
        if self.random_mode and rel_paths:
            rel_paths = ImageNavigator.random(rel_paths, rel_paths[0]).order
        self.order.extend(rel_paths)
        self._seen.update(rel_paths)
        self.next_offset = page.next_offset
        self.has_more = page.has_more
        self.total_count = max(len(self.order), page.total_images)
        return len(rel_paths)


@dataclass(frozen=True, slots=True)
class ViewerDeletePending:
    """One optimistically hidden viewer row awaiting mutation settlement."""

    rel_path: str
    removed_index: int


@dataclass(slots=True)
class ThumbnailIndexResult:
    row_by_rel: dict[str, int]
    row_by_key: dict[tuple[str, str], int]
    directory_rows: tuple[int, ...]
    image_count: int
    image_ordinal_by_row: dict[int, int]
    first_image_row: int | None


@dataclass(slots=True)
class CatalogOpenTask:
    root: Path
    future: Future[CatalogOpenResult]
    log_event: bool
    selected_at: float | None
    started_at: float
    discard_result: bool = False
    configured_restore: bool = False
    select_when_opened: bool = True
    intent_sequence: int = 0


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
    rebuilt_item_by_dir: dict[str, QTreeWidgetItem]
    cleanup_iterator: object | None
    index: int
    selected_item: QTreeWidgetItem | None
    started_at: float
    reason: str
    generation: int
    page_future: Future[TreePageResult] | None
    page_cancel_event: Event | None
    tags: tuple[str, ...]


@dataclass(slots=True)
class TreePageResult:
    root: Path
    generation: int
    offset: int
    directories: list[str]
    tags: tuple[str, ...] | None
    tags_have_more: bool = False


@dataclass(slots=True)
class TreeChildrenPageResult:
    root: Path
    parent_dir_rel: str
    offset: int
    directories: list[str]
    next_offset: int
    has_more: bool


@dataclass(slots=True)
class TreeChildrenTask:
    catalog: Catalog
    parent_dir_rel: str
    offset: int
    future: Future[TreeChildrenPageResult]
    cancel_event: Event


@dataclass(slots=True)
class TreeTagsPageResult:
    root: Path
    offset: int
    tags: tuple[str, ...]
    next_offset: int
    has_more: bool


@dataclass(slots=True)
class TreeTagsTask:
    catalog: Catalog
    offset: int
    future: Future[TreeTagsPageResult]
    cancel_event: Event


@dataclass(slots=True)
class TreePathSelectionTask:
    catalog: Catalog
    dir_rel: str
    parts: tuple[str, ...]
    item_by_dir: dict[str, QTreeWidgetItem]
    current_item: QTreeWidgetItem
    current_rel: str
    index: int
    generation: int


@dataclass(slots=True)
class VirtualViewResult:
    root: Path
    kind: str
    value: str
    sort_order: SortOrder
    fingerprint: int
    images: list[PaneRecord]
    duration_ms: float
    cache_version: int = 0
    total_records: int | None = None
    total_images: int | None = None
    page_offset: int = 0
    next_offset: int = 0
    has_more: bool = False
    stable_row_by_key: dict[tuple[str, str], int] | None = None
    stable_order_token: str | None = None
    stable_image_order: list[str] | None = None
    excluded_image_rels: frozenset[str] = frozenset()
    excluded_directory_rels: frozenset[str] = frozenset()


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
    cache_version: int = 0
    page_offset: int = 0
    physical_reconcile: bool = False


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
    remaining_rel_paths: tuple[str, ...] | None = ()
    error: BaseException | None = None
    canceled: bool = False


@dataclass(slots=True)
class DeletePayloadOutcome:
    """Worker-owned filesystem postcondition retained even if teardown fails."""

    remaining_rel_paths: tuple[str, ...] | None


@dataclass(slots=True)
class DeletePayloadTask:
    root: Path
    rel_paths: tuple[str, ...]
    expected_identities: dict[str, object]
    expected_proofs: dict[str, object]
    task: IndexTask
    future: Future[DeletePayloadResult]
    started_at: float
    outcome: DeletePayloadOutcome
    viewer: FullscreenViewer | None = None


@dataclass(slots=True)
class MovePayloadResult:
    requested: int
    moved: int
    affected_roots: set[Path]
    created_dir_rel: str | None = None
    warning: str | None = None
    target_proofs: dict[str, object] | None = None
    catalog_settings: CatalogSettings | None = None
    force_thumbnail_reindex: bool = False
    reconcile_subtrees: tuple[tuple[Path, str], ...] = ()
    reconcile_images: tuple[tuple[Path, str], ...] = ()


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
    expected_image_identities: dict[Path, dict[str, object]] | None = None
    expected_directory_identities: dict[Path, dict[str, object]] | None = None
    expected_destination_identity: object | None = None
    target_images: tuple[tuple[Path, str], ...] = ()
    completion_verb: str = "Moved"
    error_title: str = "Move"
    dedicated_executor: bool = False
    edit_operations: tuple[EditOperation, ...] = ()
    edit_owner: QWidget | None = None
    navigation_owner: FullscreenViewer | None = None


@dataclass(slots=True)
class MutationIdentityResult:
    image_identities: dict[Path, dict[str, object]]
    directory_identities: dict[Path, dict[str, object]]
    destination_identity: object | None = None


@dataclass(slots=True)
class MoveIdentityPreflightTask:
    generation: int
    dest_catalog: Catalog
    catalog_context: dict[Path, Catalog]
    dest_dir_rel: str
    image_payload: dict[Path, list[str]]
    directory_payload: dict[Path, list[str]]
    affected_roots: set[Path]
    wipe_on_delete: bool
    future: Future[MutationIdentityResult]


@dataclass(slots=True)
class RestoreIdentityPreflightTask:
    generation: int
    catalog: Catalog
    restore_items: tuple[tuple[str, str], ...]
    future: Future[MutationIdentityResult]


@dataclass(slots=True)
class ImageReconcileContext:
    root: Path
    rel_path: str
    owner: QWidget | None = None
    expected_proof: object | None = None
    attempt: int = 0
    replace_task: IndexTask | None = None


@dataclass(frozen=True, slots=True)
class PostMoveReconcileContext:
    root: Path
    subtree_rel: str | None = None
    image_rels: tuple[str, ...] = ()
    image_dir_rels: frozenset[str] = frozenset()
    attempt: int = 0
    expected_root_identity: tuple[int, int] | None = None
    expected_storage_identity: CatalogStorageIdentity | None = None
    completion_lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    completed_image_rels: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )

    def mark_image_completed(self, rel_path: str) -> None:
        """Record exact pipeline publication from a catalog worker thread."""

        with self.completion_lock:
            self.completed_image_rels.add(rel_path)

    def unfinished_image_rels(self) -> tuple[str, ...]:
        """Return inputs without an exact publication acknowledgement."""

        with self.completion_lock:
            completed = frozenset(self.completed_image_rels)
        return tuple(
            rel_path
            for rel_path in self.image_rels
            if rel_path not in completed
        )

    def contains_directory(self, dir_rel: str) -> bool:
        if self.subtree_rel is not None:
            return (
                not self.subtree_rel
                or dir_rel == self.subtree_rel
                or dir_rel.startswith(f"{self.subtree_rel}/")
            )
        return dir_rel in self.image_dir_rels


@dataclass(slots=True)
class DeferredDeleteRequest:
    catalog: Catalog
    rel_paths: tuple[str, ...]
    expected_identities: dict[str, object]
    expected_proofs: dict[str, object]
    wipe: bool
    remove_from_current_view: bool
    dependencies: tuple[MovePayloadTask, ...]
    reconciliation_tasks: tuple[IndexTask, ...] = ()
    viewer: FullscreenViewer | None = None


@dataclass(slots=True)
class DeleteConfirmationTask:
    catalog: Catalog
    kind: str
    rel_paths: tuple[str, ...]
    directory_rel: str
    owner: QWidget
    intent_sequence: int
    future: Future[object]
    wipe: bool
    remove_from_current_view: bool


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
    indexesReady = Signal(int)
    rowExposureReady = Signal()
    MIME_TYPE = "application/x-marnwick-images"
    CARD_PADDING = 1
    LABEL_GAP = 0

    def __init__(self) -> None:
        super().__init__()
        self.catalog: Catalog | None = None
        self.images: list[PaneRecord] = []
        self._visible_count = 0
        self._row_by_rel: dict[str, int] = {}
        self._row_by_key: dict[tuple[str, str], int] = {}
        self._complete_row_by_key: Mapping[tuple[str, str], int] | None = None
        self._complete_order_token: str | None = None
        self._complete_image_order: list[str] | None = None
        self._ensure_row_target: int | None = None
        self._row_exposure_generation = 0
        self._directory_rows: list[int] = []
        self._image_count = 0
        self._indexed_image_count = 0
        self._image_ordinal_by_row: dict[int, int] = {}
        self._first_image_row: int | None = None
        self.tile_size = 160
        self.device_pixel_ratio = 1.0
        self._pixmap_cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._pixmap_cache_costs: dict[str, int] = {}
        self._pixmap_cache_bytes = 0
        self._image_placeholder: QPixmap | None = None
        self._folder_placeholder: QPixmap | None = None
        self._pending_thumbnail_rels: OrderedDict[str, None] = OrderedDict()
        self._thumbnail_generation = 0
        self._thumbnail_waiting: deque[
            tuple[
                int,
                Path,
                tuple[int, int],
                CatalogStorageIdentity,
                int,
                ImageRecord,
            ]
        ] = deque()
        self._thumbnail_waiting_rels: set[str] = set()
        self._thumbnail_retry_rels: set[str] = set()
        self._thumbnail_futures: dict[
            Future[QImage | None], tuple[int, int, ImageRecord]
        ] = {}
        self._folder_waiting: deque[
            tuple[
                int,
                Path,
                tuple[int, int],
                CatalogStorageIdentity,
                int,
                DirectoryRecord,
                int,
            ]
        ] = deque()
        self._folder_waiting_rels: set[str] = set()
        self._folder_futures: dict[
            Future[Image.Image], tuple[int, int, DirectoryRecord, int]
        ] = {}
        self._thumbnail_executor = RolloverThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="marnwick-thumbnail",
            max_pending=4,
            max_retired=1,
        )
        self._record_index_executor = RolloverThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-thumbnail-index",
            max_pending=1,
            max_retired=1,
        )
        self._record_index_future: Future[tuple[int, int | None]] | None = None
        self._record_index_cancel_event: Event | None = None
        self._record_lookup_future: Future[dict[tuple[str, str], int]] | None = None
        self._record_lookup_cancel_event: Event | None = None
        self._located_row_by_key: dict[tuple[str, str], int] = {}
        self._record_lookup_attempted_keys: set[tuple[str, str]] = set()
        self._record_index_generation = 0
        self._record_indexes_pending = False
        self._page_request_callback: Callable[[int], None] | None = None
        self._page_fetch_pending = False
        self._has_more_pages = False
        self._next_page_offset = 0
        self._total_record_count = 0
        self.thumbnail_repair_requested: Callable[[Path, str], None] | None = None
        self._thumbnail_closed = False
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setInterval(20)
        self._thumbnail_timer.timeout.connect(self._settle_thumbnail_loads)
        self._record_index_timer = QTimer(self)
        self._record_index_timer.setInterval(20)
        self._record_index_timer.timeout.connect(self._settle_record_index)

    def set_images(
        self,
        catalog: Catalog | None,
        images: list[PaneRecord],
        *,
        complete_row_by_key: Mapping[tuple[str, str], int] | None = None,
        complete_order_token: str | None = None,
        complete_image_count: int | None = None,
        complete_image_order: list[str] | None = None,
        preserve_pixmap_cache: bool = False,
    ) -> None:
        previous_images = self.images
        previous_complete_rows = self._complete_row_by_key
        previous_rows = self._row_by_key
        self.beginResetModel()
        self._cancel_thumbnail_loads()
        self._cancel_record_index()
        self._row_exposure_generation += 1
        self.catalog = catalog
        self.images = images
        self._page_request_callback = None
        self._page_fetch_pending = False
        self._has_more_pages = False
        self._next_page_offset = len(images)
        self._total_record_count = len(images)
        self._visible_count = min(len(images), THUMBNAIL_MODEL_BATCH_SIZE)
        self._row_by_rel = {}
        self._row_by_key = {}
        # Physical preview workers construct and exclusively own this map.
        # Adopt it directly so publishing a huge folder remains O(1) on Qt.
        self._complete_row_by_key = complete_row_by_key
        self._complete_order_token = complete_order_token
        self._complete_image_order = complete_image_order
        self._ensure_row_target = None
        self._located_row_by_key = {}
        self._record_lookup_attempted_keys.clear()
        directory_rows: list[int] = []
        self._image_count = 0
        self._indexed_image_count = 0
        self._image_ordinal_by_row = {}
        self._first_image_row = None
        for row, record in enumerate(images[:THUMBNAIL_MODEL_BATCH_SIZE]):
            self._index_record(row, record, directory_rows)
        self._directory_rows = directory_rows
        if complete_image_count is not None:
            self._image_count = max(0, int(complete_image_count))
            self._first_image_row = (
                len(images) - self._image_count if self._image_count else None
            )
        if preserve_pixmap_cache and complete_row_by_key is not None:
            for rel_path in list(self._pixmap_cache):
                previous_row = (
                    previous_complete_rows.get(("image", rel_path))
                    if previous_complete_rows is not None
                    else previous_rows.get(("image", rel_path))
                )
                new_row = complete_row_by_key.get(("image", rel_path))
                if previous_row is None or new_row is None:
                    previous_row = (
                        previous_complete_rows.get(("directory", rel_path))
                        if previous_complete_rows is not None
                        else previous_rows.get(("directory", rel_path))
                    )
                    new_row = complete_row_by_key.get(("directory", rel_path))
                if previous_row is None or new_row is None:
                    self._drop_cached_pixmap(rel_path)
                    continue
                previous = previous_images[previous_row]
                current = images[new_row]
                preserve_image = (
                    isinstance(previous, ImageRecord)
                    and isinstance(current, ImageRecord)
                    and self._same_image_incarnation(previous, current)
                )
                preserve_directory = (
                    isinstance(previous, DirectoryRecord)
                    and isinstance(current, DirectoryRecord)
                    and previous.dir_rel == current.dir_rel
                    and previous.mtime_ns == current.mtime_ns
                )
                if not preserve_image and not preserve_directory and previous != current:
                    self._drop_cached_pixmap(rel_path)
        else:
            self._clear_pixmap_cache()
        self._pending_thumbnail_rels.clear()
        self.endResetModel()
        if (
            complete_image_count is None
            and len(images) > THUMBNAIL_MODEL_BATCH_SIZE
            and not self._thumbnail_closed
        ):
            self._record_indexes_pending = True
            cancel_event = Event()
            self._record_index_cancel_event = cancel_event
            try:
                self._record_index_future = self._record_index_executor.submit(
                    self._build_record_index,
                    images,
                    cancel_event,
                )
            except RuntimeError:
                self._record_indexes_pending = False
                self._record_index_cancel_event = None
            else:
                self._record_index_timer.start()

    @staticmethod
    def _same_image_incarnation(
        first: ImageRecord,
        second: ImageRecord,
    ) -> bool:
        """Return whether two records can safely share metadata/a pixmap."""

        return (
            first.size_bytes == second.size_bytes
            and first.mtime_ns == second.mtime_ns
            and first.ctime_ns == second.ctime_ns
            and not (
                first.file_identity is not None
                and second.file_identity is not None
                and first.file_identity != second.file_identity
            )
            and not (
                first.image_hash is not None
                and second.image_hash is not None
                and first.image_hash != second.image_hash
            )
        )

    def set_paged_images(
        self,
        catalog: Catalog,
        images: list[PaneRecord],
        *,
        total_records: int,
        total_images: int,
        next_offset: int,
        has_more: bool,
        request_page: Callable[[int], None],
    ) -> None:
        """Publish one bounded pane page and defer every later page to a worker."""

        self.set_images(catalog, images)
        self._page_request_callback = request_page
        self._page_fetch_pending = False
        self._next_page_offset = max(0, int(next_offset))
        self._total_record_count = max(len(images), int(total_records))
        self._has_more_pages = bool(has_more and self._next_page_offset < self._total_record_count)
        self._image_count = max(self._indexed_image_count, int(total_images))

    @property
    def is_paged(self) -> bool:
        return self._page_request_callback is not None

    @property
    def may_have_more_pages(self) -> bool:
        return self.is_paged and (self._has_more_pages or self._page_fetch_pending)

    @property
    def has_more_pages(self) -> bool:
        return self.is_paged and self._has_more_pages

    @property
    def total_record_count(self) -> int:
        return self._total_record_count

    @property
    def complete_order_token(self) -> str | None:
        return self._complete_order_token

    @property
    def has_complete_row_map(self) -> bool:
        return self._complete_row_by_key is not None

    @property
    def complete_image_order(self) -> list[str] | None:
        return self._complete_image_order

    @property
    def next_page_offset(self) -> int:
        return self._next_page_offset

    def merge_preview_records(self, images: list[PaneRecord]) -> bool:
        """Merge a bounded filesystem preview into an already-published page."""

        if not self.is_paged or self._page_fetch_pending:
            return False
        request_page = self._page_request_callback
        assert request_page is not None
        old_keys = {
            (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            for record in self.images
        }
        extras = [
            record
            for record in images
            if (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            not in old_keys
        ]
        if not extras:
            return False
        catalog = self.catalog
        if catalog is None:
            return False
        incoming_keys = {
            (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            for record in images
        }
        if old_keys.issubset(incoming_keys):
            # MainWindow supplies a globally sorted union, so retain its order.
            published_images = images
        else:
            # Be defensive for direct/custom callers that supply only the
            # preview prefix. Never let a partial filesystem scan erase an
            # indexed row that was already visible.
            published_images = [*self.images, *extras]
        self.set_paged_images(
            catalog,
            published_images,
            total_records=self._total_record_count + len(extras),
            total_images=self._image_count + sum(isinstance(record, ImageRecord) for record in extras),
            next_offset=self._next_page_offset,
            has_more=self._has_more_pages,
            request_page=request_page,
        )
        return True

    def update_records_in_place(self, records: Sequence[PaneRecord]) -> int:
        """Replace metadata for existing keys without changing pane geometry.

        Filesystem preview rows establish the selected directory's membership
        and order. Database indexing may then enrich those exact rows, but it
        must not reset the model or move a thumbnail underneath the user.
        """

        if not self.images or not records:
            return 0
        row_by_key = self._complete_row_by_key
        if row_by_key is None:
            row_by_key = {
                (
                    "directory" if isinstance(record, DirectoryRecord) else "image",
                    record.rel_path,
                ): row
                for row, record in enumerate(self.images)
            }
        changed_rows: list[int] = []
        for record in records:
            key = (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            row = row_by_key.get(key)
            if row is None:
                continue
            previous = self.images[row]
            if type(previous) is not type(record) or previous == record:
                continue
            if isinstance(previous, ImageRecord) and isinstance(record, ImageRecord):
                # Physical filesystem records are the membership/incarnation
                # authority. A late SQLite page can describe the same path
                # before it was replaced; never let that stale metadata (and
                # its thumbnail reference) overwrite the current placeholder.
                known_hash_changed = (
                    previous.image_hash is not None
                    and record.image_hash is not None
                    and previous.image_hash != record.image_hash
                )
                if not self._same_image_incarnation(previous, record):
                    if known_hash_changed or (
                        previous.size_bytes == record.size_bytes
                        and previous.mtime_ns == record.mtime_ns
                    ):
                        self._drop_cached_pixmap(record.rel_path)
                        self._mark_thumbnail_pending(record.rel_path)
                        if row < self._visible_count:
                            self._queue_thumbnail_row(row, retry=True)
                    continue
                if known_hash_changed:
                    self._drop_cached_pixmap(record.rel_path)
                    self._mark_thumbnail_pending(record.rel_path)
                    if row < self._visible_count:
                        self._queue_thumbnail_row(row, retry=True)
                    continue
            elif isinstance(previous, DirectoryRecord):
                self._drop_cached_pixmap(record.rel_path)
            if (
                isinstance(previous, ImageRecord)
                and isinstance(record, ImageRecord)
                and record.file_identity is None
                and previous.file_identity is not None
            ):
                record = replace(record, file_identity=previous.file_identity)
            self.images[row] = record
            changed_rows.append(row)
            if (
                isinstance(record, ImageRecord)
                and self._cached_pixmap(record.rel_path) is None
            ):
                self._mark_thumbnail_pending(record.rel_path)
                if row < self._visible_count:
                    self._queue_thumbnail_row(row, retry=True)

        for row in changed_rows:
            if row >= self._visible_count:
                continue
            index = self.index(row, 0)
            self.dataChanged.emit(
                index,
                index,
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.DecorationRole,
                    Qt.ItemDataRole.ToolTipRole,
                ],
            )
        return len(changed_rows)

    def replace_complete_records_in_place(
        self,
        records: list[PaneRecord],
        *,
        total_images: int | None,
    ) -> None:
        """Adopt same-key/order worker records without a reset or GUI O(N) pass."""

        if len(records) != len(self.images):
            raise ValueError("same-order replacement changed row count")
        previous_records = self.images
        row_by_key = self._complete_row_by_key or {}
        for rel_path in list(self._pixmap_cache):
            row = row_by_key.get(("image", rel_path))
            if row is None:
                row = row_by_key.get(("directory", rel_path))
            if row is None:
                continue
            previous = previous_records[row]
            current = records[row]
            same_image_incarnation = (
                isinstance(previous, ImageRecord)
                and isinstance(current, ImageRecord)
                and previous.rel_path == current.rel_path
                and self._same_image_incarnation(previous, current)
            )
            same_directory_incarnation = (
                isinstance(previous, DirectoryRecord)
                and isinstance(current, DirectoryRecord)
                and previous.dir_rel == current.dir_rel
                and previous.mtime_ns == current.mtime_ns
            )
            if (
                previous != current
                and not same_image_incarnation
                and not same_directory_incarnation
            ):
                self._drop_cached_pixmap(rel_path)
        self.images = records
        if total_images is not None:
            self._image_count = max(0, int(total_images))
        # Only visible rows need repaint/thumbnail admission now. Hidden rows
        # read the adopted records directly when Qt exposes their next batch.
        for row in range(self._visible_count):
            record = records[row]
            if (
                isinstance(record, ImageRecord)
                and self._cached_pixmap(record.rel_path) is None
            ):
                self._mark_thumbnail_pending(record.rel_path)
                self._queue_thumbnail_row(row, retry=True)
        if self._visible_count:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(self._visible_count - 1, 0),
                [
                    Qt.ItemDataRole.DisplayRole,
                    Qt.ItemDataRole.DecorationRole,
                    Qt.ItemDataRole.ToolTipRole,
                ],
            )

    def append_page(
        self,
        images: Sequence[PaneRecord],
        *,
        expected_offset: int,
        next_offset: int,
        has_more: bool,
        total_records: int,
        total_images: int,
    ) -> bool:
        """Append a completed worker page if it still matches the model cursor."""

        if not self.is_paged or expected_offset != self._next_page_offset:
            return False
        self._page_fetch_pending = False
        start = len(self.images)
        new_records = [
            record
            for record in images
            if (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            not in self._row_by_key
        ]
        if new_records:
            end = start + len(new_records) - 1
            self.beginInsertRows(QModelIndex(), start, end)
            self.images.extend(new_records)
            directory_rows = self._directory_rows
            for row in range(start, end + 1):
                self._index_record(row, self.images[row], directory_rows)
            self._visible_count = len(self.images)
        self._next_page_offset = max(expected_offset, int(next_offset))
        self._total_record_count = max(len(self.images), int(total_records))
        self._has_more_pages = bool(has_more and self._next_page_offset < self._total_record_count)
        self._image_count = max(self._indexed_image_count, int(total_images))
        if new_records:
            self.endInsertRows()
        self.indexesReady.emit(self._record_index_generation)
        return True

    def page_fetch_failed(self, expected_offset: int) -> None:
        if self.is_paged and expected_offset == self._next_page_offset:
            self._page_fetch_pending = False
            # Avoid an automatic-view retry loop. An explicit reload obtains a
            # fresh cursor and can retry a transient database failure.
            self._has_more_pages = False

    def replace_loaded_records(self, images: list[PaneRecord]) -> None:
        """Replace loaded rows after a local mutation without discarding paging."""

        if not self.is_paged:
            self.set_images(self.catalog, images)
            return
        assert self.catalog is not None
        request_page = self._page_request_callback
        assert request_page is not None
        removed_records = max(0, len(self.images) - len(images))
        old_loaded_images = sum(isinstance(record, ImageRecord) for record in self.images)
        new_loaded_images = sum(isinstance(record, ImageRecord) for record in images)
        removed_images = max(0, old_loaded_images - new_loaded_images)
        self.set_paged_images(
            self.catalog,
            images,
            total_records=max(0, self._total_record_count - removed_records),
            total_images=max(0, self._image_count - removed_images),
            next_offset=self._next_page_offset,
            # Offset pagination is no longer causal once a loaded row has
            # been removed. Mutation settlement performs a fresh generation
            # reload before any later page may be requested.
            has_more=False,
            request_page=request_page,
        )

    @property
    def record_indexes_pending(self) -> bool:
        return self._record_indexes_pending

    def _index_record(
        self,
        row: int,
        record: PaneRecord,
        directory_rows: list[int] | None = None,
    ) -> None:
        self._row_by_rel[record.rel_path] = row
        if isinstance(record, DirectoryRecord):
            self._row_by_key[("directory", record.dir_rel)] = row
            if directory_rows is not None:
                directory_rows.append(row)
            return
        self._row_by_key[("image", record.rel_path)] = row
        self._indexed_image_count += 1
        self._image_count = max(self._image_count, self._indexed_image_count)
        self._image_ordinal_by_row[row] = self._indexed_image_count
        if self._first_image_row is None:
            self._first_image_row = row

    @staticmethod
    def _build_record_index(
        images: Sequence[PaneRecord],
        cancel_event: Event,
    ) -> tuple[int, int | None]:
        """Build only compact summary data; exposed rows are indexed lazily."""

        image_count = 0
        first_image_row: int | None = None
        for row, record in enumerate(images):
            if row % 128 == 0 and cancel_event.is_set():
                raise IndexTaskCancelled()
            if isinstance(record, DirectoryRecord):
                continue
            image_count += 1
            if first_image_row is None:
                first_image_row = row
        return image_count, first_image_row

    def _settle_record_index(self) -> None:
        generation = self._record_index_generation
        settled = False
        future = self._record_index_future
        if future is not None and future.done():
            self._record_index_future = None
            self._record_index_cancel_event = None
            settled = True
            if not future.cancelled():
                with suppress(Exception):
                    image_count, first_image_row = future.result()
                    self._image_count = image_count
                    self._first_image_row = first_image_row
        lookup = self._record_lookup_future
        if lookup is not None and lookup.done():
            self._record_lookup_future = None
            self._record_lookup_cancel_event = None
            settled = True
            if not lookup.cancelled():
                with suppress(Exception):
                    self._located_row_by_key.update(lookup.result())
        self._record_indexes_pending = (
            self._record_index_future is not None or self._record_lookup_future is not None
        )
        if not self._record_indexes_pending:
            self._record_index_timer.stop()
        if settled and generation == self._record_index_generation:
            self.indexesReady.emit(generation)

    @staticmethod
    def _find_record_keys(
        images: Sequence[PaneRecord],
        keys: frozenset[tuple[str, str]],
        cancel_event: Event,
    ) -> dict[tuple[str, str], int]:
        found: dict[tuple[str, str], int] = {}
        for row, record in enumerate(images):
            if row % 128 == 0 and cancel_event.is_set():
                raise IndexTaskCancelled()
            key = (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
            )
            if key in keys:
                found[key] = row
                if len(found) == len(keys):
                    break
        return found

    def locate_rows_for_keys(self, keys: set[tuple[str, str]]) -> None:
        if self.is_paged:
            # Loaded rows are indexed as each page arrives. Searching the
            # bounded prefix cannot locate a later database page.
            return
        unresolved = frozenset(
            key
            for key in keys
            if key not in self._row_by_key and key not in self._located_row_by_key
            and (
                self._complete_row_by_key is None
                or key not in self._complete_row_by_key
            )
            and key not in self._record_lookup_attempted_keys
        )
        if not unresolved or self._thumbnail_closed:
            return
        if self._record_lookup_cancel_event is not None:
            self._record_lookup_cancel_event.set()
        if self._record_lookup_future is not None:
            self._record_lookup_future.cancel()
        cancel_event = Event()
        self._record_lookup_attempted_keys.update(unresolved)
        self._record_lookup_cancel_event = cancel_event
        try:
            self._record_lookup_future = self._record_index_executor.submit(
                self._find_record_keys,
                self.images,
                unresolved,
                cancel_event,
            )
        except RuntimeError:
            self._record_lookup_future = None
            self._record_lookup_cancel_event = None
            return
        self._record_indexes_pending = True
        self._record_index_timer.start()

    def _cancel_record_index(self) -> None:
        self._record_index_generation += 1
        self._record_indexes_pending = False
        self._record_index_timer.stop()
        if self._record_index_cancel_event is not None:
            self._record_index_cancel_event.set()
            self._record_index_cancel_event = None
        if self._record_index_future is not None:
            self._record_index_future.cancel()
            self._record_index_future = None
        if self._record_lookup_cancel_event is not None:
            self._record_lookup_cancel_event.set()
            self._record_lookup_cancel_event = None
        if self._record_lookup_future is not None:
            self._record_lookup_future.cancel()
            self._record_lookup_future = None
        # These scans are pure Python and check their cancel event every 128
        # rows. The active single-worker epoch can roll over once if a stale
        # scan fails to unwind, while the two-worker lifetime cap remains fixed.

    @property
    def image_count(self) -> int:
        return self._image_count

    @property
    def first_image_row(self) -> int | None:
        return self._first_image_row

    def image_ordinal(self, row: int) -> int | None:
        ordinal = self._image_ordinal_by_row.get(row)
        if (
            ordinal is None
            and self._first_image_row is not None
            and row >= self._first_image_row
        ):
            ordinal = row - self._first_image_row + 1
        return ordinal

    def row_for_key(self, key: tuple[str, str]) -> int | None:
        row = self._row_by_key.get(key)
        if row is None and self._complete_row_by_key is not None:
            row = self._complete_row_by_key.get(key)
        return row if row is not None else self._located_row_by_key.get(key)

    @staticmethod
    def _pixmap_cost(pixmap: QPixmap) -> int:
        return max(1, pixmap.width()) * max(1, pixmap.height()) * max(1, pixmap.depth()) // 8

    def _cached_pixmap(self, rel_path: str) -> QPixmap | None:
        pixmap = self._pixmap_cache.get(rel_path)
        if pixmap is not None:
            self._pixmap_cache.move_to_end(rel_path)
        return pixmap

    def _cache_pixmap(self, rel_path: str, pixmap: QPixmap) -> None:
        self._drop_cached_pixmap(rel_path)
        cost = self._pixmap_cost(pixmap)
        self._pixmap_cache[rel_path] = pixmap
        self._pixmap_cache_costs[rel_path] = cost
        self._pixmap_cache_bytes += cost
        while (
            len(self._pixmap_cache) > MAX_THUMBNAIL_PIXMAP_CACHE_ITEMS
            or self._pixmap_cache_bytes > MAX_THUMBNAIL_PIXMAP_CACHE_BYTES
        ):
            evicted_rel_path, _ = self._pixmap_cache.popitem(last=False)
            self._pixmap_cache_bytes -= self._pixmap_cache_costs.pop(evicted_rel_path, 0)

    def _drop_cached_pixmap(self, rel_path: str) -> None:
        if self._pixmap_cache.pop(rel_path, None) is not None:
            self._pixmap_cache_bytes -= self._pixmap_cache_costs.pop(rel_path, 0)

    def _clear_pixmap_cache(self) -> None:
        self._pixmap_cache.clear()
        self._pixmap_cache_costs.clear()
        self._pixmap_cache_bytes = 0

    def set_tile_size(self, size: int) -> None:
        self._cancel_thumbnail_loads()
        self.tile_size = size
        # Image cache pixmaps are source thumbnails; the delegate scales them
        # to the current card size and maintains its own bounded scaled cache.
        # Preserve those sources during interactive window resizing. Folder
        # mosaics are rendered for an exact tile size and must be regenerated.
        for rel_path in list(self._pixmap_cache):
            if self.row_for_key(("image", rel_path)) is None:
                self._drop_cached_pixmap(rel_path)
        self._image_placeholder = None
        self._folder_placeholder = None
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
        row = self.row_for_key(("image", rel_path))
        if row is not None:
            self._drop_cached_pixmap(rel_path)
            self._mark_thumbnail_pending(rel_path)
            self._queue_thumbnail_row(row)
            if row < self._visible_count:
                model_index = self.index(row, 0)
                self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])
        parent = Path(rel_path).parent
        while parent.as_posix() not in {"", "."}:
            parent_rel = parent.as_posix()
            directory_row = self.row_for_key(("directory", parent_rel))
            parent = parent.parent
            if directory_row is None or directory_row >= len(self.images):
                continue
            record = self.images[directory_row]
            if not isinstance(record, DirectoryRecord):
                continue
            self._drop_cached_pixmap(record.rel_path)
            if directory_row < self._visible_count:
                model_index = self.index(directory_row, 0)
                self.dataChanged.emit(model_index, model_index, [Qt.ItemDataRole.DecorationRole])

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._visible_count

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: N802 - Qt API
        if parent.isValid():
            return False
        if self.is_paged:
            return self._has_more_pages and not self._page_fetch_pending
        return self._visible_count < len(self.images)

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: N802 - Qt API
        if self.is_paged:
            if parent.isValid() or self._page_fetch_pending or not self._has_more_pages:
                return
            callback = self._page_request_callback
            if callback is None:
                return
            offset = self._next_page_offset
            self._page_fetch_pending = True
            try:
                callback(offset)
            except RuntimeError:
                self.page_fetch_failed(offset)
            return
        if parent.isValid() or self._visible_count >= len(self.images):
            return
        start = self._visible_count
        end = min(len(self.images), start + THUMBNAIL_MODEL_BATCH_SIZE) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        directory_rows = self._directory_rows
        for row in range(start, end + 1):
            self._index_record(row, self.images[row], directory_rows)
        self._visible_count = end + 1
        self.endInsertRows()
        target = self._ensure_row_target
        if target is not None:
            if target < self._visible_count:
                self._ensure_row_target = None
                self.rowExposureReady.emit()
            else:
                # Expose at most one normal model batch per event-loop turn.
                QTimer.singleShot(
                    0,
                    partial(
                        self._continue_row_exposure,
                        self._row_exposure_generation,
                    ),
                )

    def _continue_row_exposure(self, generation: int) -> None:
        if (
            generation != self._row_exposure_generation
            or self._ensure_row_target is None
            or self.is_paged
        ):
            return
        self.fetchMore()

    def ensure_row_loaded(self, row: int) -> bool:
        if row < self._visible_count or row < 0 or row >= len(self.images):
            return 0 <= row < self._visible_count
        if self._complete_row_by_key is not None:
            self._ensure_row_target = max(self._ensure_row_target or 0, row)
            self.fetchMore()
            return row < self._visible_count
        start = self._visible_count
        end = min(len(self.images), row + 1) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        directory_rows = self._directory_rows
        for exposed_row in range(start, end + 1):
            self._index_record(exposed_row, self.images[exposed_row], directory_rows)
        self._visible_count = end + 1
        self.endInsertRows()
        return True

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
                pixmap = self._cached_pixmap(record.rel_path)
                if pixmap is None:
                    pixmap = self._folder_placeholder_pixmap()
                    self._queue_folder_row(index.row())
                return pixmap
            pixmap = self._cached_pixmap(record.rel_path)
            if pixmap is None:
                # SQLite lookup, cache-path validation, and file reads can all
                # block on a busy or slow catalog. Painting must only enqueue
                # bounded work and return immediately.
                pixmap = self._placeholder_pixmap()
                self._mark_thumbnail_pending(record.rel_path)
                self._queue_thumbnail_row(index.row())
            return pixmap
        if role == Qt.ItemDataRole.SizeHintRole:
            return self.card_size()
        return None

    def refresh_pending_thumbnails(self, *, limit: int = PENDING_THUMBNAIL_REFRESH_BATCH_SIZE) -> None:
        """Retry a bounded rotating slice of visible placeholder thumbnails."""

        if not self._pending_thumbnail_rels:
            return
        selected_rows: list[int] = []
        count = min(max(1, limit), len(self._pending_thumbnail_rels))
        for _ in range(count):
            rel_path, _ = self._pending_thumbnail_rels.popitem(last=False)
            self._pending_thumbnail_rels[rel_path] = None
            row = self.row_for_key(("image", rel_path))
            if row is None or row >= self._visible_count:
                continue
            if isinstance(self.images[row], ImageRecord):
                selected_rows.append(row)
        for row in selected_rows:
            self._queue_thumbnail_row(row, retry=True)

    def _mark_thumbnail_pending(self, rel_path: str) -> None:
        if rel_path in self._pending_thumbnail_rels:
            self._pending_thumbnail_rels.move_to_end(rel_path)
            return
        self._pending_thumbnail_rels[rel_path] = None
        while len(self._pending_thumbnail_rels) > MAX_PENDING_THUMBNAIL_RETRIES:
            self._pending_thumbnail_rels.popitem(last=False)

    def _clear_thumbnail_pending(self, rel_path: str) -> None:
        self._pending_thumbnail_rels.pop(rel_path, None)

    @staticmethod
    def _catalog_database_path(root: Path) -> Path | None:
        state_dir = root / ".marnwick"
        database = state_dir / "catalog.sqlite3"
        try:
            state_stat = state_dir.lstat()
            database_stat = database.lstat()
        except OSError:
            return None
        if (
            stat.S_ISLNK(state_stat.st_mode)
            or not stat.S_ISDIR(state_stat.st_mode)
            or stat.S_ISLNK(database_stat.st_mode)
            or not stat.S_ISREG(database_stat.st_mode)
        ):
            return None
        return database

    @staticmethod
    def _read_thumbnail_cache_file(root: Path, thumb_rel_path: str | None) -> bytes | None:
        if not thumb_rel_path:
            return None
        relative = Path(thumb_rel_path)
        if relative.is_absolute() or not relative.parts or relative.parts[0] != "thumbnails":
            return None
        if any(part in {"", ".", ".."} for part in relative.parts):
            return None
        state_dir = root / ".marnwick"
        current = state_dir
        try:
            for part in relative.parts[:-1]:
                current = current / part
                mode = current.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return None
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(state_dir / relative, flags)
            try:
                opened_stat = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_size <= 0
                    or opened_stat.st_size > MAX_THUMBNAIL_BLOB_BYTES
                ):
                    return None
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)
        except OSError:
            return None

    @classmethod
    def _thumbnail_rows(
        cls,
        root: Path,
        query: str,
        params: Sequence[object],
    ) -> list[sqlite3.Row]:
        database = cls._catalog_database_path(root)
        if database is None:
            return []
        try:
            connection = sqlite3.connect(
                f"{database.as_uri()}?mode=ro",
                uri=True,
                timeout=0.1,
            )
            connection.row_factory = sqlite3.Row
            try:
                return list(connection.execute(query, params))
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return []

    @classmethod
    def _load_thumbnail_blob(
        cls,
        root: Path,
        rel_path: str,
        embedded_blob: bytes | None,
    ) -> bytes | None:
        """Load cache bytes only when SQLite describes the live file inode.

        Filesystem placeholders deliberately have no database incarnation
        token. Looking up a thumbnail by pathname alone could therefore paint
        the old contents after a same-name replacement. Validate size, mtime,
        and change time against a pinned regular-file descriptor before using
        either cache storage. The embedded value is intentionally ignored: it
        may have come from an older database page than the row read here.
        """

        del embedded_blob
        try:
            filesystem = Catalog.open_filesystem_handle(root)
            path = filesystem.mutation_path(rel_path)
            parent = path.parent
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            with filesystem._open_catalog_directory_fd(parent) as parent_fd:  # noqa: SLF001
                fd = os.open(
                    path if parent_fd is None else path.name,
                    flags,
                    **({} if parent_fd is None else {"dir_fd": parent_fd}),
                )
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode):
                        return None
                    changed_ns = filesystem._path_change_time_ns(path, opened)  # noqa: SLF001
                    rows = cls._thumbnail_rows(
                        root,
                        """
                        SELECT file_size_bytes, modified_at_ns, ctime_ns, thumb_rel_path,
                            CASE WHEN length(thumb_blob) <= ?
                                THEN thumb_blob ELSE NULL END AS thumb_blob
                        FROM images
                        WHERE rel_path = ?
                        """,
                        (MAX_THUMBNAIL_BLOB_BYTES, rel_path),
                    )
                    if not rows:
                        return None
                    row = rows[0]
                    if (
                        int(row["file_size_bytes"]) != int(opened.st_size)
                        or int(row["modified_at_ns"]) != int(opened.st_mtime_ns)
                        or int(row["ctime_ns"]) != changed_ns
                    ):
                        return None
                    blob = cls._read_thumbnail_cache_file(
                        root,
                        row["thumb_rel_path"],
                    )
                    if not blob:
                        legacy_blob = row["thumb_blob"]
                        if (
                            not legacy_blob
                            or len(legacy_blob) > MAX_THUMBNAIL_BLOB_BYTES
                        ):
                            return None
                        blob = bytes(legacy_blob)
                    after = os.fstat(fd)
                    opened_identity = (
                        int(opened.st_dev),
                        int(opened.st_ino),
                        int(opened.st_size),
                        int(opened.st_mtime_ns),
                        int(getattr(opened, "st_ctime_ns", 0)),
                    )
                    after_identity = (
                        int(after.st_dev),
                        int(after.st_ino),
                        int(after.st_size),
                        int(after.st_mtime_ns),
                        int(getattr(after, "st_ctime_ns", 0)),
                    )
                    if opened_identity != after_identity:
                        return None
                    if parent_fd is None:
                        named = path.stat(follow_symlinks=False)
                    else:
                        named = os.stat(
                            path.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                        if not filesystem._directory_fd_still_names_path(  # noqa: SLF001
                            parent_fd,
                            parent,
                        ):
                            return None
                    if (
                        not stat.S_ISREG(named.st_mode)
                        or (int(named.st_dev), int(named.st_ino))
                        != (int(opened.st_dev), int(opened.st_ino))
                    ):
                        return None
                    return blob
                finally:
                    os.close(fd)
        except (OSError, ValueError):
            return None

    @classmethod
    def _load_thumbnail_image(cls, root: Path, rel_path: str, embedded_blob: bytes | None) -> QImage | None:
        blob = cls._load_thumbnail_blob(root, rel_path, embedded_blob)
        if not blob or len(blob) > MAX_THUMBNAIL_BLOB_BYTES:
            return None
        try:
            with Image.open(io.BytesIO(blob)) as image:
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width > MAX_THUMBNAIL_DIMENSION
                    or height > MAX_THUMBNAIL_DIMENSION
                    or width * height > MAX_THUMBNAIL_DECODE_PIXELS
                ):
                    return None
                image.verify()
        except Exception:
            return None
        decoded = QImage.fromData(blob)
        return None if decoded.isNull() else decoded

    @staticmethod
    def _root_identity_matches(root: Path, expected: tuple[int, int]) -> bool:
        try:
            root_stat = root.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(root_stat.st_mode)
            and not stat.S_ISLNK(root_stat.st_mode)
            and (int(root_stat.st_dev), int(root_stat.st_ino)) == expected
        )

    def _load_thumbnail_image_if_current(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        rel_path: str,
        embedded_blob: bytes | None,
    ) -> QImage | None:
        try:
            Catalog.assert_storage_identity(
                root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
        except OSError:
            return None
        image = self._load_thumbnail_image(root, rel_path, embedded_blob)
        try:
            Catalog.assert_storage_identity(
                root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
        except OSError:
            return None
        return image

    def _queue_thumbnail_row(self, row: int, *, retry: bool = False) -> None:
        if self.catalog is None or row < 0 or row >= self._visible_count:
            return
        record = self.images[row]
        if not isinstance(record, ImageRecord):
            return
        rel_path = record.rel_path
        if rel_path in self._thumbnail_waiting_rels:
            if retry:
                self._thumbnail_retry_rels.add(rel_path)
            return
        if any(
            queued_record.rel_path == rel_path
            for _, _, queued_record in self._thumbnail_futures.values()
        ):
            if retry:
                self._thumbnail_retry_rels.add(rel_path)
            return
        while len(self._thumbnail_waiting) >= MAX_WAITING_THUMBNAIL_LOADS:
            _, _, _, _, _, evicted_record = self._thumbnail_waiting.popleft()
            self._thumbnail_waiting_rels.discard(evicted_record.rel_path)
        self._thumbnail_waiting.append(
            (
                self._thumbnail_generation,
                self.catalog.root,
                self.catalog.root_identity,
                self.catalog.storage_identity,
                row,
                record,
            )
        )
        self._thumbnail_waiting_rels.add(rel_path)
        self._pump_thumbnail_loads()

    def _pump_thumbnail_loads(self) -> None:
        if self._thumbnail_closed:
            return

        def outstanding() -> int:
            return len(self._thumbnail_futures) + len(self._folder_futures)

        while self._thumbnail_waiting and outstanding() < MAX_PENDING_THUMBNAIL_LOADS:
            waiting = self._thumbnail_waiting.popleft()
            generation, root, root_identity, storage_identity, row, queued_record = waiting
            rel_path = queued_record.rel_path
            self._thumbnail_waiting_rels.discard(rel_path)
            if generation != self._thumbnail_generation:
                continue
            if row < 0 or row >= self._visible_count:
                continue
            current_record = self.images[row]
            if (
                not isinstance(current_record, ImageRecord)
                or current_record.rel_path != rel_path
                or not self._same_image_incarnation(queued_record, current_record)
            ):
                if (
                    isinstance(current_record, ImageRecord)
                    and current_record.rel_path not in self._thumbnail_waiting_rels
                ):
                    self._thumbnail_waiting.append(
                        (
                            generation,
                            root,
                            root_identity,
                            storage_identity,
                            row,
                            current_record,
                        )
                    )
                    self._thumbnail_waiting_rels.add(current_record.rel_path)
                continue
            try:
                future = self._thumbnail_executor.submit(
                    self._load_thumbnail_image_if_current,
                    root,
                    root_identity,
                    storage_identity,
                    rel_path,
                    queued_record.thumb_blob,
                )
            except RuntimeError:
                # The fixed executor can still contain canceled calls from
                # rapid prior panes. Keep the newest request and poll for
                # admission instead of losing it after popleft().
                self._thumbnail_waiting.appendleft(waiting)
                self._thumbnail_waiting_rels.add(rel_path)
                self._thumbnail_timer.start()
                return
            self._thumbnail_futures[future] = (generation, row, queued_record)
        while self._folder_waiting and outstanding() < MAX_PENDING_THUMBNAIL_LOADS:
            waiting_folder = self._folder_waiting.popleft()
            generation, root, root_identity, storage_identity, row, record, tile_size = waiting_folder
            self._folder_waiting_rels.discard(record.rel_path)
            if generation != self._thumbnail_generation:
                continue
            if row < 0 or row >= self._visible_count:
                continue
            current_record = self.images[row]
            if (
                not isinstance(current_record, DirectoryRecord)
                or current_record != record
                or self.tile_size != tile_size
            ):
                if (
                    isinstance(current_record, DirectoryRecord)
                    and current_record.rel_path not in self._folder_waiting_rels
                ):
                    self._folder_waiting.append(
                        (
                            generation,
                            root,
                            root_identity,
                            storage_identity,
                            row,
                            current_record,
                            self.tile_size,
                        )
                    )
                    self._folder_waiting_rels.add(current_record.rel_path)
                continue
            try:
                future = self._thumbnail_executor.submit(
                    self._render_folder_tile_if_current,
                    root,
                    root_identity,
                    storage_identity,
                    record,
                    tile_size,
                )
            except RuntimeError:
                self._folder_waiting.appendleft(waiting_folder)
                self._folder_waiting_rels.add(record.rel_path)
                self._thumbnail_timer.start()
                return
            self._folder_futures[future] = (generation, row, record, tile_size)
        if self._thumbnail_futures or self._thumbnail_waiting or self._folder_futures or self._folder_waiting:
            self._thumbnail_timer.start()

    def _settle_thumbnail_loads(self) -> None:
        settled = 0
        for future, (generation, row, queued_record) in list(self._thumbnail_futures.items()):
            if settled >= THUMBNAIL_RESULTS_PER_TICK or not future.done():
                continue
            settled += 1
            self._thumbnail_futures.pop(future, None)
            rel_path = queued_record.rel_path
            if future.cancelled():
                continue
            try:
                image = future.result()
            except Exception:
                image = None
            if generation != self._thumbnail_generation:
                continue
            if row < 0 or row >= self._visible_count:
                continue
            record = self.images[row]
            if not isinstance(record, ImageRecord) or record.rel_path != rel_path:
                continue
            if not self._same_image_incarnation(queued_record, record):
                self._mark_thumbnail_pending(record.rel_path)
                self._queue_thumbnail_row(row, retry=True)
                continue
            pixmap = QPixmap.fromImage(image) if image is not None else QPixmap()
            if pixmap.isNull():
                self._mark_thumbnail_pending(rel_path)
                if record.id >= 0 and self.thumbnail_repair_requested is not None:
                    self.thumbnail_repair_requested(record.catalog_root, rel_path)
                if rel_path in self._thumbnail_retry_rels:
                    self._thumbnail_retry_rels.discard(rel_path)
                    self._queue_thumbnail_row(row, retry=True)
            else:
                self._cache_pixmap(rel_path, pixmap)
                self._clear_thumbnail_pending(rel_path)
                self._thumbnail_retry_rels.discard(rel_path)
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
        for future, (
            generation,
            row,
            queued_record,
            queued_tile_size,
        ) in list(self._folder_futures.items()):
            if settled >= THUMBNAIL_RESULTS_PER_TICK or not future.done():
                continue
            settled += 1
            self._folder_futures.pop(future, None)
            if future.cancelled() or generation != self._thumbnail_generation:
                continue
            try:
                image = future.result()
            except Exception:
                continue
            if row < 0 or row >= self._visible_count:
                continue
            record = self.images[row]
            if not isinstance(record, DirectoryRecord):
                continue
            if record != queued_record or self.tile_size != queued_tile_size:
                self._queue_folder_row(row)
                continue
            self._cache_pixmap(record.rel_path, pixmap_from_pil_image(image))
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
        self._pump_thumbnail_loads()
        if (
            not self._thumbnail_futures
            and not self._thumbnail_waiting
            and not self._folder_futures
            and not self._folder_waiting
        ):
            self._thumbnail_timer.stop()

    def _cancel_thumbnail_loads(self) -> None:
        self._thumbnail_generation += 1
        self._thumbnail_waiting.clear()
        self._thumbnail_waiting_rels.clear()
        self._thumbnail_retry_rels.clear()
        self._folder_waiting.clear()
        self._folder_waiting_rels.clear()
        for future in self._thumbnail_futures:
            future.cancel()
        for future in self._folder_futures:
            future.cancel()
        self._thumbnail_futures.clear()
        self._folder_futures.clear()
        self._thumbnail_timer.stop()
        # Saturation can retire one four-worker epoch so the next pane starts
        # while stale native reads unwind. The retired generation is still
        # counted, keeping a hard process-wide cap of eight thumbnail workers.

    def close(self) -> None:
        if self._thumbnail_closed:
            return
        self._thumbnail_closed = True
        self._cancel_thumbnail_loads()
        self._cancel_record_index()
        self._thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        self._record_index_executor.shutdown(wait=False, cancel_futures=True)

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
        if self._image_placeholder is None:
            self._image_placeholder = QPixmap(self.tile_size, self.tile_size)
            self._image_placeholder.fill(QColor("#d0d5dd"))
        return self._image_placeholder

    def _folder_placeholder_pixmap(self) -> QPixmap:
        if self._folder_placeholder is None:
            self._folder_placeholder = QApplication.style().standardIcon(
                QStyle.StandardPixmap.SP_DirIcon
            ).pixmap(QSize(self.tile_size, self.tile_size))
        return self._folder_placeholder

    def _queue_folder_row(self, row: int) -> None:
        if self.catalog is None or row < 0 or row >= self._visible_count:
            return
        record = self.images[row]
        if not isinstance(record, DirectoryRecord):
            return
        if record.rel_path in self._folder_waiting_rels:
            return
        if any(
            queued_record.rel_path == record.rel_path
            for _, _, queued_record, _ in self._folder_futures.values()
        ):
            return
        while len(self._folder_waiting) >= MAX_WAITING_THUMBNAIL_LOADS:
            _, _, _, _, _, evicted_record, _ = self._folder_waiting.popleft()
            self._folder_waiting_rels.discard(evicted_record.rel_path)
        self._folder_waiting.append(
            (
                self._thumbnail_generation,
                self.catalog.root,
                self.catalog.root_identity,
                self.catalog.storage_identity,
                row,
                record,
                self.tile_size,
            )
        )
        self._folder_waiting_rels.add(record.rel_path)
        self._pump_thumbnail_loads()

    @classmethod
    def _render_folder_tile(cls, root: Path, record: DirectoryRecord, tile_size: int) -> Image.Image:
        preview_items: list[object] = list(record.preview_items[:4])
        if not preview_items:
            preview_items = list(record.preview_blobs[:4])
        if len(preview_items) < 4 and record.allow_preview_fallback:
            if record.dir_rel:
                condition = "dir_rel = ? OR (dir_rel >= ? AND dir_rel < ?)"
                params: list[object] = [
                    record.dir_rel,
                    f"{record.dir_rel}/",
                    f"{record.dir_rel}0",
                ]
            else:
                condition = "1 = 1"
                params = []
            rows = cls._thumbnail_rows(
                root,
                f"""
                SELECT thumb_rel_path,
                    CASE WHEN length(thumb_blob) <= ? THEN thumb_blob ELSE NULL END AS thumb_blob
                FROM images
                WHERE {condition}
                ORDER BY rel_path COLLATE NOCASE ASC
                LIMIT 4
                """,
                [MAX_THUMBNAIL_BLOB_BYTES, *params],
            )
            preview_items = []
            for row in rows:
                blob = cls._read_thumbnail_cache_file(root, row["thumb_rel_path"])
                if (
                    not blob
                    and row["thumb_blob"]
                    and len(row["thumb_blob"]) <= MAX_THUMBNAIL_BLOB_BYTES
                ):
                    blob = bytes(row["thumb_blob"])
                if blob:
                    preview_items.append(FolderPreviewRecord("image", blob))
        return render_folder_icon(preview_items[:4], max(1, tile_size))

    @classmethod
    def _render_folder_tile_if_current(
        cls,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        record: DirectoryRecord,
        tile_size: int,
    ) -> Image.Image:
        Catalog.assert_storage_identity(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        )
        image = cls._render_folder_tile(root, record, tile_size)
        Catalog.assert_storage_identity(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        )
        return image

    def folder_tile_pixmap(self, record: DirectoryRecord) -> QPixmap:
        if self.catalog is None:
            return self._folder_placeholder_pixmap()
        return pixmap_from_pil_image(self._render_folder_tile(self.catalog.root, record, self.tile_size))


class ThumbnailDelegate(QStyledItemDelegate):
    PADDING = ThumbnailModel.CARD_PADDING
    LABEL_GAP = ThumbnailModel.LABEL_GAP
    MAX_SCALED_PIXMAPS = 512
    MAX_SCALED_PIXMAP_BYTES = 128 * 1024 * 1024

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scaled_pixmap_cache: OrderedDict[tuple[int, int, int, int], QPixmap] = OrderedDict()
        self._scaled_pixmap_costs: dict[tuple[int, int, int, int], int] = {}
        self._scaled_pixmap_bytes = 0

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
            cost = max(1, scaled.width()) * max(1, scaled.height()) * max(1, scaled.depth()) // 8
            self._scaled_pixmap_costs[key] = cost
            self._scaled_pixmap_bytes += cost
            while (
                len(self._scaled_pixmap_cache) > self.MAX_SCALED_PIXMAPS
                or self._scaled_pixmap_bytes > self.MAX_SCALED_PIXMAP_BYTES
            ):
                evicted_key, _ = self._scaled_pixmap_cache.popitem(last=False)
                self._scaled_pixmap_bytes -= self._scaled_pixmap_costs.pop(evicted_key, 0)
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
        was_active = self._manual_drag_active
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
        if was_active and self.main_window is not None:
            # A thumbnail drag deliberately freezes tree publication so its
            # destination cannot move underneath the cursor.  Resume on the
            # next event-loop turn, after every piece of drag state is clear.
            QTimer.singleShot(
                0,
                self.main_window._resume_deferred_tree_publication,
            )

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
                "catalog_root": str(Path(item.data(0, CATALOG_ROOT_ROLE))),
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
            self.window._resume_deferred_tree_publication()

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
    initialConfigLoadFinished = Signal(int)

    def __init__(self, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Marnwick")
        self.setWindowIcon(load_app_icon())
        self.config_path = config_path or default_config_path()
        self.config_enabled = config_path is not None or not config_disabled()
        self.app_config = AppConfig()
        self._applying_initial_config = False
        self._initial_config_controls_changed = False
        self._initial_config_catalog_interacted = False
        self._initial_config_geometry_changed = False
        self._initial_config_catalogs_replaced = False
        self._initial_config_catalog_exclusions: set[Path] = set()
        self._initial_config_tracking_ready = False
        self._initial_config_load_generation = 0
        self.config_load_executor: AbandonableThreadPoolExecutor | None = None
        self._initial_config_load_future: Future[AppConfig] | None = None
        self.initialConfigLoadFinished.connect(
            self._settle_initial_config_load,
            Qt.ConnectionType.QueuedConnection,
        )
        if self.config_enabled:
            # A config path can live on a slow or disconnected home mount. Let
            # the window become interactive with safe defaults while it loads.
            self.config_load_executor = AbandonableThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="marnwick-config-load",
                max_pending=1,
            )
            self._initial_config_load_generation += 1
            load_generation = self._initial_config_load_generation
            self._initial_config_load_future = self.config_load_executor.submit(
                load_config,
                self.config_path,
            )
            window_ref = weakref.ref(self)

            def notify_config_loaded(_future: Future[AppConfig]) -> None:
                window = window_ref()
                if window is None or getattr(window, "_closing", False):
                    return
                try:
                    window.initialConfigLoadFinished.emit(load_generation)
                except RuntimeError:
                    # The Python wrapper can briefly outlive its deleted Qt
                    # object when a slow read finishes during application exit.
                    return

            self._initial_config_load_future.add_done_callback(notify_config_loaded)
        self.workspace = Workspace()
        # Several bounded read/index lanes let a selected directory and a
        # protected mutation pass an obsolete native filesystem call. The
        # indexer itself still serializes non-preemptible mutations.
        self.indexer = BackgroundIndexer(max_workers=4)
        # A catalog on an unavailable or very slow filesystem must not hold up
        # a newer catalog selection.
        self.catalog_open_executor = RolloverThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="marnwick-open",
            max_pending=2,
            max_retired=3,
        )
        self.timing_executor = AbandonableThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-timing",
            max_pending=32,
        )
        self.config_save_executor = LatestOnlyThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-config-save",
        )
        self._config_save_futures: set[Future[None]] = set()
        self._config_save_contexts: dict[Future[None], tuple[int, tuple[str, ...]]] = {}
        self._config_save_sequence = 0
        # A canceled filesystem/SQLite read may never return from native code.
        # Finite rollover epochs let the latest directory selection bypass a
        # few fully stuck generations while preserving the eight-thread caps.
        self.virtual_view_executor = RolloverThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="marnwick-virtual",
            max_pending=2,
            max_retired=3,
        )
        self.physical_preview_executor = RolloverThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="marnwick-physical-preview",
            max_pending=2,
            max_retired=3,
        )
        # Tree database pages are isolated from pane reads. When a genuinely
        # blocked old catalog is preempted, this executor can be rolled over so
        # the current catalog never waits behind an uninterruptible OS call.
        self.tree_read_executor = RolloverThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-tree-read",
            max_pending=1,
            max_retired=3,
        )
        # Kept for compatibility with older direct callers; UI file work is
        # routed through BackgroundIndexer.submit_action below.
        self.duplicate_delete_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marnwick-delete")
        # Protected image encodes are serialized per catalog by admission in
        # queue_image_edit, while several fixed lanes let an unrelated catalog
        # remain usable if one codec or filesystem stalls.
        self.file_move_executor = AtomicSaveThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="marnwick-save",
            max_pending=MAX_PENDING_IMAGE_SAVES,
        )
        self.identity_executor = RolloverThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marnwick-confirm-identity",
            max_pending=1,
            max_retired=3,
        )
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
        self._very_similar_cache: OrderedDict[
            tuple[Path, str, int], list[ImageRecord]
        ] = OrderedDict()
        self._very_similar_cache_versions: dict[Path, int] = {}
        self._duplicate_delete_task: DuplicateDeleteTask | None = None
        self._delete_payload_tasks: list[DeletePayloadTask] = []
        self._delete_payload_task: DeletePayloadTask | None = None
        self._move_payload_tasks: list[MovePayloadTask] = []
        self._move_payload_task: MovePayloadTask | None = None
        self._mutation_identity_generation = 0
        self._move_identity_preflights: dict[
            Future[MutationIdentityResult], MoveIdentityPreflightTask
        ] = {}
        self._restore_identity_preflights: dict[
            Future[MutationIdentityResult], RestoreIdentityPreflightTask
        ] = {}
        self._deferred_delete_requests: list[DeferredDeleteRequest] = []
        self._delete_confirmation_tasks: dict[Future[object], DeleteConfirmationTask] = {}
        self._settling_delete_confirmation_keys: set[
            tuple[Path, tuple[int, int], str, tuple[str, ...]]
        ] = set()
        self._image_reconcile_tasks: dict[IndexTask, ImageReconcileContext] = {}
        self._image_reconcile_retries: dict[tuple[Path, str], ImageReconcileContext] = {}
        self._post_move_reconcile_tasks: dict[IndexTask, PostMoveReconcileContext] = {}
        self._thumbnail_repair_tasks: dict[tuple[Path, str], IndexTask] = {}
        self._failed_image_edits: dict[tuple[Path, str], tuple[EditOperation, ...]] = {}
        self._tree_build_task: TreeBuildTask | None = None
        self._tree_build_generation = 0
        self._tree_path_selection_task: TreePathSelectionTask | None = None
        self._tree_path_selection_generation = 0
        self._tree_children_tasks: dict[Future[TreeChildrenPageResult], TreeChildrenTask] = {}
        self._tree_child_next_offsets: dict[tuple[Path, str], int | None] = {}
        # A completed discovery/rebuild can require a fresh offset-zero page
        # while an older page for the same parent is still registered. Keep
        # that intent until the stale task settles instead of dropping it.
        self._tree_child_refresh_pending: set[tuple[Path, str]] = set()
        self._tree_tags_tasks: dict[Future[TreeTagsPageResult], TreeTagsTask] = {}
        self._tree_tag_next_offsets: dict[Path, int | None] = {}
        self._tree_tag_cache: dict[Path, tuple[str, ...]] = {}
        self._tree_item_maps: dict[Path, dict[str, QTreeWidgetItem]] = {}
        self._tree_expanded_state: set[TreeStateKey] = set()
        self._tree_known_state: set[TreeStateKey] = set()
        self._pending_tree_rebuilds: dict[Path, tuple[Catalog, str]] = {}
        self._thumbnail_scroll_positions: OrderedDict[
            TreeStateKey, tuple[int, int]
        ] = OrderedDict()
        self._thumbnail_selections: OrderedDict[
            TreeStateKey,
            tuple[set[tuple[str, str]], tuple[str, str] | None],
        ] = OrderedDict()
        self._thumbnail_scroll_key: TreeStateKey | None = None
        self._thumbnail_scroll_restore_generation = 0
        self._indexing_was_active = False
        self._catalog_intent_root: Path | None = None
        self._catalog_intent_sequence = 0
        self._current_catalog_intent_sequence = 0
        self._successful_catalog_open_intents: dict[Path, int] = {}
        self._failed_catalog_open_intents: set[int] = set()
        self._closing = False
        self._unavailable_catalog_paths: list[str] = []
        self._directory_drag_active = False
        self._tree_rebuild_deferred = False
        self._physical_pane_generation = 0
        self._last_physical_progress_reload_at = 0.0
        self._pending_physical_progress_reload: tuple[Path, str, SortOrder] | None = None
        self._physical_progress_reload_timer_pending = False
        self._physical_full_result_generation = 0
        self._physical_preview_result_generation = 0
        self._physical_preview_retry_generation: int | None = None
        self._physical_reconcile_pending_generation: int | None = None
        self._pending_exclusion_cache: dict[
            Path,
            tuple[
                tuple[tuple[tuple[int, bool], ...], tuple[int, ...], tuple[tuple[int, bool], ...]],
                tuple[frozenset[str], frozenset[str]],
            ],
        ] = {}
        self._pending_thumbnail_index_restore: tuple[
            set[tuple[str, str]],
            tuple[str, str] | None,
        ] | None = None
        self._pending_rel_path_selection: tuple[Path, str, int] | None = None

        self.tree = DirectoryTree(self)
        self.tree.itemClicked.connect(self._directory_clicked)
        self.tree.itemExpanded.connect(self._tree_item_expanded)
        self.tree.itemCollapsed.connect(self._tree_item_collapsed)

        self.model = ThumbnailModel()
        self.model.thumbnail_repair_requested = self._queue_thumbnail_repair
        self.model.indexesReady.connect(self._thumbnail_indexes_ready)
        self.model.rowExposureReady.connect(self._thumbnail_rows_exposed)
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
        if self._initial_config_load_future is None:
            self.restore_catalogs_from_config()
        else:
            self._settle_initial_config_load(self._initial_config_load_generation)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        initial_config_pending = self._initial_config_load_pending()
        self._closing = True
        self._cancel_tree_path_selection()
        self._pending_physical_progress_reload = None
        self.idle_timer.stop()
        self.progress_timer.stop()
        # Until the initial read completes we do not have the merge baseline
        # or the user's persisted preferences. Writing the temporary defaults
        # here could atomically destroy a valid config merely because its
        # filesystem was slow. A canceled close keeps the reader alive; a
        # terminal close preserves the existing file and abandons the read.
        config_future = (
            None
            if initial_config_pending
            else self.save_window_config(wait=False)
        )
        if config_future is not None:
            try:
                config_future.result(timeout=0.1)
            except FutureTimeoutError:
                # The atomic ancillary write may finish on its daemon lane;
                # never turn a slow home/config filesystem into an exit hang.
                pass
            except OSError as error:
                show_error(self, "Save Configuration", str(error))
        if not self._wait_for_pending_file_tasks_on_exit():
            self._closing = False
            self.progress_timer.start()
            self.idle_timer.start()
            event.ignore()
            return
        if self._failed_image_edits and not ask_discard_failed_edits_on_exit(
            self,
            len(self._failed_image_edits),
        ):
            # Failed encodes retain the exact edit operation list in memory so
            # reopening the image can retry it.  Never silently lose that last
            # copy just because the main window was closed.
            self._closing = False
            self.progress_timer.start()
            self.idle_timer.start()
            event.ignore()
            return
        self._failed_image_edits.clear()
        self._cancel_active_duplicate_delete_task(wait=True)
        self._cancel_active_delete_payload_task(wait=True)
        self._cancel_active_move_payload_task(wait=True)
        self._shutdown_initial_config_load()
        self._shutdown_catalog_open_tasks()
        self.timing_executor.shutdown(wait=False, cancel_futures=True)
        # The executor has only a single pending slot containing the complete
        # newest snapshot.  Preserve it for a final daemon-thread durability
        # attempt instead of canceling it just because the UI is now closed.
        self.config_save_executor.shutdown(wait=False, cancel_futures=False)
        self.model.close()
        self._cancel_all_virtual_view_tasks()
        self.virtual_view_executor.shutdown(wait=False, cancel_futures=True)
        self.physical_preview_executor.shutdown(wait=False, cancel_futures=True)
        self._cancel_active_tree_build()
        for future, task in list(self._tree_children_tasks.items()):
            task.cancel_event.set()
            future.cancel()
        self._tree_children_tasks.clear()
        self._tree_child_refresh_pending.clear()
        for future, task in list(self._tree_tags_tasks.items()):
            task.cancel_event.set()
            future.cancel()
        self._tree_tags_tasks.clear()
        self.tree_read_executor.shutdown(wait=False, cancel_futures=True)
        self._virtual_view_tasks.clear()
        self.duplicate_delete_executor.shutdown(wait=True, cancel_futures=True)
        self.file_move_executor.shutdown(wait=True, cancel_futures=True)
        for future in self._delete_confirmation_tasks:
            future.cancel()
        self._delete_confirmation_tasks.clear()
        self._settling_delete_confirmation_keys.clear()
        self.identity_executor.shutdown(wait=False, cancel_futures=True)
        self.indexer.shutdown()
        self.workspace.close()
        super().closeEvent(event)

    def _shutdown_catalog_open_tasks(self) -> None:
        for future, task in list(self._catalog_open_tasks.items()):
            task.discard_result = True
            if not future.cancel():
                future.add_done_callback(self._close_discarded_catalog_open_result)
        self._catalog_open_tasks.clear()
        # Opening only initializes catalog bookkeeping. A filesystem call on a
        # disconnected mount may be uninterruptible, so this daemon read/init
        # pool is abandoned on quit; completed Catalog handles are closed by
        # the callback above.
        self.catalog_open_executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _close_discarded_catalog_open_result(
        future: Future[CatalogOpenResult],
    ) -> None:
        if future.cancelled():
            return
        try:
            result = future.result()
        except BaseException:
            return
        with suppress(Exception):
            result.catalog.close()

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

    def _wait_for_pending_file_tasks_on_exit(self) -> bool:
        self._settle_move_identity_preflights()
        self._settle_restore_identity_preflights()
        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        self._settle_image_reconcile_tasks()
        self._submit_pending_image_reconcile_retries()
        self._flush_deferred_delete_requests()
        if not self._has_pending_file_tasks():
            return True
        for catalog in self.workspace.catalogs:
            self.indexer.cancel_idle_tasks(catalog.root)
            self.indexer.cancel_directory_tasks(catalog.root)
        dialog = QDialog(self)
        dialog.setWindowTitle("Finishing File Changes")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        label = QLabel("Finishing pending file changes...")
        label.setMinimumWidth(480)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(label)
        layout.addWidget(progress)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("Keep Marnwick Open")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setModal(True)

        def update() -> None:
            self._settle_move_identity_preflights()
            self._settle_restore_identity_preflights()
            self._settle_duplicate_delete_task()
            self._settle_delete_payload_task()
            self._settle_move_payload_task()
            self._settle_image_reconcile_tasks()
            self._submit_pending_image_reconcile_retries()
            self._flush_deferred_delete_requests()
            active_move = self._active_move_payload_task()
            active_delete_payload = self._active_delete_payload_task()
            active_duplicate = self._active_duplicate_delete_task()
            active_task = active_move.task if active_move is not None else None
            if active_task is None:
                active_task = next(
                    (task for task in self._image_reconcile_tasks if not task.snapshot().done),
                    None,
                )
            if active_task is None and active_delete_payload is not None:
                active_task = active_delete_payload.task
            if active_task is None and active_duplicate is not None:
                active_task = active_duplicate.task
            if active_task is None:
                if self._move_identity_preflights or self._restore_identity_preflights:
                    label.setText("Checking pending file changes...")
                    return
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
        finished = not self._has_pending_file_tasks()
        dialog.deleteLater()
        return finished

    def _has_pending_file_tasks(self) -> bool:
        return (
            bool(self._move_identity_preflights)
            or bool(self._restore_identity_preflights)
            or self._has_pending_move_payload_tasks()
            or self._has_pending_delete_payload_tasks()
            or self._has_active_duplicate_delete_task()
            or bool(self._image_reconcile_tasks)
            or bool(self._image_reconcile_retries)
            or bool(self._deferred_delete_requests)
        )

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        self.refresh_thumbnail_layout()
        # Initial show/layout emits resize and move events that are not user
        # intent. Start tracking on the next turn, before a person can interact
        # but after Qt has placed the new top-level window.
        QTimer.singleShot(0, self._enable_initial_config_geometry_tracking)

    def _enable_initial_config_geometry_tracking(self) -> None:
        if not self._closing and self.isVisible():
            self._initial_config_tracking_ready = True

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._mark_initial_config_geometry_interaction()
        self.refresh_thumbnail_layout()

    def moveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().moveEvent(event)
        self._mark_initial_config_geometry_interaction()

    def changeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._mark_initial_config_geometry_interaction()

    def _initial_config_load_pending(self) -> bool:
        return self._initial_config_load_future is not None

    def _mark_initial_config_controls_interaction(self) -> None:
        if self._initial_config_load_pending() and not self._applying_initial_config:
            self._initial_config_controls_changed = True

    def _mark_initial_config_catalog_interaction(self, *, replaced: bool = False) -> None:
        if self._initial_config_load_pending() and not self._applying_initial_config:
            self._initial_config_catalog_interacted = True
            self._initial_config_catalogs_replaced |= replaced

    def _mark_initial_config_geometry_interaction(self) -> None:
        if (
            self._initial_config_tracking_ready
            and self._initial_config_load_pending()
            and not self._applying_initial_config
        ):
            self._initial_config_geometry_changed = True

    def _shutdown_initial_config_load(self) -> None:
        self._initial_config_load_generation += 1
        future = self._initial_config_load_future
        self._initial_config_load_future = None
        if future is not None:
            future.cancel()
        executor = self.config_load_executor
        self.config_load_executor = None
        if executor is not None:
            # Config loading is read-only. A native read on a disconnected
            # home directory cannot be interrupted, so abandon its single
            # daemon lane instead of making application exit wait for it.
            executor.shutdown(wait=False, cancel_futures=True)

    def _settle_initial_config_load(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._initial_config_load_generation:
            return
        future = self._initial_config_load_future
        if future is None or not future.done():
            return

        # Snapshot newer UI intent before replacing the in-memory config with
        # the load result. Each guarded section below either applies the disk
        # value or preserves the user's newer value, never a mixture chosen by
        # completion timing.
        current = self.current_app_config()
        controls_changed = self._initial_config_controls_changed
        catalogs_interacted = self._initial_config_catalog_interacted
        catalogs_replaced = self._initial_config_catalogs_replaced
        geometry_changed = self._initial_config_geometry_changed
        self._initial_config_load_future = None
        executor = self.config_load_executor
        self.config_load_executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        if future.cancelled() or self._closing:
            return
        try:
            loaded = future.result()
        except Exception as error:
            self.statusBar().showMessage(
                f"Configuration could not be loaded: {error}",
                8000,
            )
            return

        configured_catalogs = tuple(
            path_text
            for path_text in dict.fromkeys(loaded.catalogs)
            if self._configured_catalog_path(path_text)
            not in self._initial_config_catalog_exclusions
        )
        prior_sort = self.current_sort
        self._applying_initial_config = True
        try:
            self.app_config = loaded
            if controls_changed:
                loaded.thumbnail_size = current.thumbnail_size
                loaded.sort_order = current.sort_order
                loaded.delete_behavior = current.delete_behavior
            else:
                self.set_thumbnail_size(
                    self.thumbnail_columns_from_config(loaded.thumbnail_size)
                )
                self.set_sort_order(self.sort_order_from_config(loaded.sort_order))

            if geometry_changed:
                loaded.window = current.window
            else:
                self.restore_window_config()
        finally:
            self._applying_initial_config = False

        if not controls_changed and prior_sort != self.current_sort and self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
            self.load_current_directory(preserve_selection=True)

        # Accepting the catalog preferences dialog is an authoritative list
        # replacement. A simple open/select is different: configured catalogs
        # can still be added in the background, but none may take selection
        # away from that newer explicit intent.
        if catalogs_replaced:
            return
        self.restore_catalogs_from_config(
            catalog_paths=configured_catalogs,
            select_when_opened=not catalogs_interacted and self.current_catalog is None,
        )

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

    def restore_catalogs_from_config(
        self,
        *,
        catalog_paths: tuple[str, ...] | None = None,
        select_when_opened: bool | None = None,
    ) -> None:
        if not self.config_enabled:
            return
        if catalog_paths is None:
            catalog_paths = tuple(dict.fromkeys(self.app_config.catalogs))
        if select_when_opened is None:
            select_when_opened = (
                not self._initial_config_catalog_interacted
                and self.current_catalog is None
            )
        self._unavailable_catalog_paths = list(catalog_paths)
        selection_guard_sequence = self._catalog_intent_sequence
        QTimer.singleShot(
            0,
            lambda: self._start_configured_catalog_opens(
                catalog_paths,
                select_when_opened=bool(select_when_opened),
                selection_guard_sequence=selection_guard_sequence,
            ),
        )

    @staticmethod
    def _configured_catalog_path(path_text: str) -> Path | None:
        try:
            return Path(path_text).expanduser().absolute()
        except (OSError, RuntimeError, ValueError):
            return None

    def _start_configured_catalog_opens(
        self,
        catalog_paths: tuple[str, ...],
        *,
        select_when_opened: bool,
        selection_guard_sequence: int,
    ) -> None:
        if self._closing:
            return
        select_when_opened = (
            select_when_opened
            and self.current_catalog is None
            and self._catalog_intent_sequence == selection_guard_sequence
        )
        for catalog_path in catalog_paths:
            root = self._configured_catalog_path(catalog_path)
            if root is None:
                self.progress_label.setText(
                    f"Could not restore catalog {catalog_path}"
                )
                continue
            self.open_catalog_async(
                root,
                log_event=False,
                configured_restore=True,
                select_when_opened=select_when_opened,
            )

    def save_window_config(self, *, wait: bool = True) -> Future[None] | None:
        if not self.config_enabled:
            return None
        # Incorporate the last durably completed local intent before capturing
        # another merge baseline. A queued snapshot that gets superseded must
        # never advance this baseline: doing so can make its additions vanish
        # from the coalesced replacement.
        self._settle_config_saves()
        snapshot = self.current_app_config()
        desired_catalogs = tuple(snapshot.catalogs)
        self.app_config.catalogs = list(snapshot.catalogs)
        if wait:
            # Serialize an explicitly synchronous save after any already
            # running snapshot; direct execution here could otherwise overtake
            # the worker and then be overwritten by stale state.
            future = self.config_save_executor.submit(save_config, snapshot, self.config_path)
            future.result()
            self._settle_config_saves()
            self.app_config._loaded_catalogs = desired_catalogs
            return None
        self._config_save_sequence += 1
        future = self.config_save_executor.submit(save_config, snapshot, self.config_path)
        self._config_save_futures.add(future)
        self._config_save_contexts[future] = (
            self._config_save_sequence,
            desired_catalogs,
        )
        self._settle_config_saves()
        return future

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
                    + [str(task.root) for task in self._catalog_open_tasks.values() if not task.discard_result]
                    + self._unavailable_catalog_paths
                )
            ),
            thumbnail_size=self.thumbnail_columns,
            delete_behavior=self.app_config.delete_behavior,
            sort_order=self.current_sort.value,
            _loaded_catalogs=self.app_config._loaded_catalogs,
        )

    def _settle_config_saves(self) -> None:
        settled = [future for future in self._config_save_futures if future.done()]
        settled.sort(key=lambda future: self._config_save_contexts.get(future, (0, ()))[0])
        for future in settled:
            if not future.done():
                continue
            self._config_save_futures.discard(future)
            _sequence, desired_catalogs = self._config_save_contexts.pop(future, (0, ()))
            if future.cancelled():
                continue
            try:
                future.result()
            except Exception as error:
                if not self._closing:
                    self.statusBar().showMessage(
                        f"Configuration could not be saved: {error}",
                        8000,
                    )
            else:
                # Keep the baseline in terms of this process's desired list,
                # not save_config()'s merged on-disk list. This continues to
                # preserve independent catalogs added by another process.
                self.app_config._loaded_catalogs = desired_catalogs

    def apply_app_config(self, config: AppConfig) -> None:
        self._mark_initial_config_controls_interaction()
        self._mark_initial_config_geometry_interaction()
        self._mark_initial_config_catalog_interaction(replaced=True)
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
            self.save_window_config(wait=False)

    def sync_catalogs_to_config(self, catalog_paths: list[str]) -> None:
        self._mark_initial_config_catalog_interaction(replaced=True)
        requested: list[Path] = []
        seen: set[Path] = set()
        for catalog_path in catalog_paths:
            # Validation and canonicalization may block on an unavailable
            # filesystem, so keep the GUI path operation lexical and let the
            # catalog-open worker perform all I/O.
            path = Path(catalog_path).expanduser().absolute()
            if path not in seen:
                seen.add(path)
                requested.append(path)
        self._unavailable_catalog_paths = []
        open_catalogs = list(self.workspace.catalogs)
        for catalog in open_catalogs:
            if catalog.root not in seen and catalog.root.absolute() not in seen:
                self.close_catalog(catalog.root)
        open_roots = {catalog.root for catalog in self.workspace.catalogs}
        for path in requested:
            if path not in open_roots:
                self.open_catalog_async(path, configured_restore=True)

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
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        QTimer.singleShot(0, self._poll_indexer)

    def open_app_preferences(self) -> None:
        dialog = AppPreferencesDialog(self.current_app_config(), self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_config() if accepted else None
        dialog.deleteLater()
        if selected is None:
            return
        self.apply_app_config(selected)

    def open_logs(self) -> None:
        dialog = LogsDialog(self.workspace.catalogs, self)
        dialog.exec()
        dialog.deleteLater()

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
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        QTimer.singleShot(0, self._poll_indexer)

    def automatically_delete_duplicates(self) -> None:
        if self.current_catalog is None:
            return
        if self.current_virtual_kind not in {VIRTUAL_KIND_DUPLICATES, VIRTUAL_KIND_VERY_SIMILAR}:
            return
        catalog = self.current_catalog
        if self._pending_image_save_targets(catalog.root):
            self.progress_label.setText("Wait for the pending image save before deleting duplicates")
            return
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
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
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
            with Catalog.open_writer(
                root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
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
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected = dialog.selected_settings() if accepted else None
        dialog.deleteLater()
        if selected is None:
            return
        selected_catalog, settings = selected
        self.apply_catalog_settings(selected_catalog, settings)

    def apply_catalog_settings(self, catalog: Catalog, settings: CatalogSettings) -> None:
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        self._queue_catalog_mutation(
            catalog,
            label="Updating catalog settings",
            dest_dir_rel="",
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._set_catalog_settings_worker(
                catalog.root,
                catalog.root_identity,
                settings,
                task,
            ),
            completion_verb="Updated",
            error_title="Catalog Preferences",
        )

    def open_catalog_tags(self, root: Path) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        dialog = CatalogTagsDialog(catalog, self)
        dialog.exec()
        requested_tags = dialog.requested_tags()
        dialog.deleteLater()
        if requested_tags:
            self.queue_catalog_tags(catalog, requested_tags)

    def queue_catalog_tags(self, catalog: Catalog, names: Sequence[str]) -> MovePayloadTask | None:
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return None
        requested_names = tuple(names)
        if not requested_names:
            return None
        return self._queue_catalog_mutation(
            catalog,
            label="Adding catalog tags",
            dest_dir_rel="",
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._define_catalog_tags_worker(
                catalog.root,
                catalog.root_identity,
                requested_names,
                task,
            ),
            completion_verb="Added",
            error_title="Catalog Tags",
        )

    def queue_image_tags(
        self,
        catalog: Catalog,
        rel_path: str,
        names: Sequence[str],
        *,
        expected_identity: object | None = None,
        owner: FullscreenViewer | None = None,
    ) -> MovePayloadTask | None:
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return None
        requested_names = tuple(names)
        mutation = self._queue_catalog_mutation(
            catalog,
            label="Updating image tags",
            dest_dir_rel=rel_path.rpartition("/")[0],
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._set_image_tags_worker(
                catalog.root,
                catalog.root_identity,
                rel_path,
                requested_names,
                expected_identity,
                task,
            ),
            completion_verb="Updated",
            error_title="Image Tags",
        )
        if mutation is not None:
            mutation.navigation_owner = owner
        return mutation

    def open_directory_properties(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        dialog = DirectoryPropertiesDialog(catalog, dir_rel, self)
        dialog.exec()
        dialog.deleteLater()

    def create_directory(self, root: Path, parent_dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        try:
            identity_future = self.identity_executor.submit(
                self._capture_directory_identity_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                parent_dir_rel,
            )
        except RuntimeError:
            self._show_identity_preflight_busy("create a directory")
            return
        # This is display text from a validated tree row. Avoid resolving or
        # statting a possibly slow filesystem path on the GUI thread.
        parent_path = catalog.root.joinpath(*Path(parent_dir_rel).parts)
        dialog = DirectoryNameDialog(parent_path, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        name = dialog.directory_name() if accepted else ""
        # Tests and embedding callers may provide a lightweight dialog double;
        # real Qt dialogs still get their normal deferred destruction.
        with suppress(AttributeError, RuntimeError):
            dialog.deleteLater()
        if not accepted:
            identity_future.cancel()
            return
        if not self._wait_for_directory_identity(identity_future):
            return
        try:
            expected_parent_identity = identity_future.result()
        except (OSError, ValueError) as error:
            show_error(self, "Create Directory", str(error))
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        self._queue_catalog_mutation(
            catalog,
            label="Creating directory",
            dest_dir_rel=parent_dir_rel,
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._create_directory_worker(
                catalog.root,
                catalog.root_identity,
                parent_dir_rel,
                name,
                expected_parent_identity,
                task,
            ),
            completion_verb="Created",
            error_title="Create Directory",
        )

    def _wait_for_directory_identity(self, future: Future[object]) -> bool:
        if future.done():
            return not future.cancelled()
        dialog = QDialog(self)
        dialog.setWindowTitle("Checking Directory")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Verifying the destination directory…"))
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(progress)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        timer = QTimer(dialog)
        timer.setInterval(25)
        timer.timeout.connect(lambda: dialog.accept() if future.done() else None)
        timer.start()
        dialog.exec()
        timer.stop()
        completed = future.done() and not future.cancelled()
        if not completed:
            future.cancel()
        dialog.deleteLater()
        return completed

    @staticmethod
    def _capture_directory_identity_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        parent_dir_rel: str,
    ) -> object:
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            return catalog.directory_identity(parent_dir_rel)

    def delete_directory(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None or not dir_rel:
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        self._start_delete_confirmation(
            catalog,
            kind="directory",
            directory_rel=dir_rel,
            owner=self,
            wipe=self.wipe_on_delete_enabled(),
            remove_from_current_view=True,
        )

    def _queue_confirmed_directory_delete(
        self,
        catalog: Catalog,
        dir_rel: str,
        expected_identity: object,
        *,
        wipe: bool,
    ) -> None:
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        if any(
            target == dir_rel or target.startswith(f"{dir_rel}/")
            for target in self._pending_image_save_targets(catalog.root)
        ):
            self.progress_label.setText("Wait for the pending image save before deleting this directory")
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
                wipe,
                expected_identity,
                task,
            ),
            source_directories=((catalog.root, dir_rel),),
            completion_verb="Deleted",
            error_title="Delete Directory",
        )

    @staticmethod
    def _capture_delete_confirmation_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        rel_paths: tuple[str, ...],
        directory_rel: str,
        expected_incarnations: Mapping[str, ImageRecord | ImageFileIdentity],
    ) -> object:
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            if directory_rel:
                return catalog.directory_identity(directory_rel)
            identities: dict[str, object] = {}
            for rel_path in rel_paths:
                current_identity = catalog.file_identity(rel_path)
                expected = expected_incarnations.get(rel_path)
                if isinstance(expected, ImageFileIdentity):
                    displayed_identity = snapshot_image_file_identity(
                        catalog.mutation_path(rel_path)
                    )
                    if displayed_identity != expected:
                        raise OSError(
                            f"image changed since it was displayed: {rel_path}"
                        )
                elif isinstance(expected, ImageRecord):
                    if (
                        expected.file_identity is not None
                        and current_identity != expected.file_identity
                    ):
                        raise OSError(
                            f"image changed since its thumbnail was displayed: {rel_path}"
                        )
                    if (
                        expected.size_bytes <= 0
                        or expected.ctime_ns <= 0
                        or current_identity[3] != expected.size_bytes
                        or current_identity[4] != expected.mtime_ns
                        or current_identity[5] != expected.ctime_ns
                    ):
                        raise OSError(
                            f"image changed since its thumbnail was displayed: {rel_path}"
                        )
                    if is_exact_image_hash(expected.image_hash):
                        current_proof = catalog.file_proof(rel_path)
                        if (
                            current_proof[:5] != current_identity[:5]
                            or current_proof[5].casefold()
                            != str(expected.image_hash).casefold()
                        ):
                            raise OSError(
                                f"image content changed since its thumbnail was displayed: {rel_path}"
                            )
                identities[rel_path] = current_identity
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
            return identities

    def _start_delete_confirmation(
        self,
        catalog: Catalog,
        *,
        kind: str,
        rel_paths: Sequence[str] = (),
        directory_rel: str = "",
        owner: QWidget,
        wipe: bool,
        remove_from_current_view: bool,
        expected_incarnations: Mapping[
            str,
            ImageRecord | ImageFileIdentity,
        ] | None = None,
    ) -> None:
        unique_rel_paths = tuple(dict.fromkeys(rel_paths))
        if not directory_rel and not unique_rel_paths:
            return
        request_key = self._delete_confirmation_key(
            catalog,
            unique_rel_paths,
            directory_rel,
        )
        if request_key in self._settling_delete_confirmation_keys or any(
            self._delete_confirmation_key(
                pending.catalog,
                pending.rel_paths,
                pending.directory_rel,
            )
            == request_key
            for pending in self._delete_confirmation_tasks.values()
        ):
            self.progress_label.setText("Delete confirmation already pending")
            return
        try:
            future = self.identity_executor.submit(
                self._capture_delete_confirmation_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                unique_rel_paths,
                directory_rel,
                dict(expected_incarnations or {}),
            )
        except RuntimeError:
            self._show_identity_preflight_busy("delete")
            return
        task = DeleteConfirmationTask(
            catalog=catalog,
            kind=kind,
            rel_paths=unique_rel_paths,
            directory_rel=directory_rel,
            owner=owner,
            intent_sequence=self._catalog_intent_sequence,
            future=future,
            wipe=wipe,
            remove_from_current_view=remove_from_current_view,
        )
        self._delete_confirmation_tasks[future] = task
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Checking selected files before delete")

    @staticmethod
    def _delete_confirmation_key(
        catalog: Catalog,
        rel_paths: tuple[str, ...],
        directory_rel: str,
    ) -> tuple[Path, tuple[int, int], str, tuple[str, ...]]:
        # Selection order is presentation state, not delete-request identity.
        # Treat repeated shortcuts for the same files as one pending prompt
        # even if Qt reports the selected indexes in a different order.
        return catalog.root, catalog.root_identity, directory_rel, tuple(sorted(rel_paths))

    def _delete_confirmation_is_current(self, task: DeleteConfirmationTask) -> bool:
        if self._closing or self.workspace.catalog_for_root(task.catalog.root) is not task.catalog:
            return False
        if task.intent_sequence != self._catalog_intent_sequence:
            return False
        if task.kind == "images-main-selection":
            return tuple(dict.fromkeys(self.selected_rel_paths())) == task.rel_paths
        if task.kind == "image-viewer":
            viewer = task.owner
            return (
                isinstance(viewer, FullscreenViewer)
                and not viewer._load_closed
                and len(task.rel_paths) == 1
                and viewer.navigator.current == task.rel_paths[0]
            )
        return True

    def _delete_confirmation_can_commit_after_prompt(
        self,
        task: DeleteConfirmationTask,
    ) -> bool:
        """Revalidate the target without retargeting an accepted main prompt.

        A modal confirmation remains bound to the catalog/identities shown
        when it opened. If another catalog window becomes active during the
        nested event loop, accepting must neither redirect the delete nor
        silently cancel that explicit answer. A fullscreen prompt still
        requires its viewer to be on the same image.
        """

        if self._closing or self.workspace.catalog_for_root(task.catalog.root) is not task.catalog:
            return False
        if task.kind == "images-main-selection":
            return True
        return self._delete_confirmation_is_current(task)

    def _settle_delete_confirmations(self) -> None:
        for future, task in list(self._delete_confirmation_tasks.items()):
            if not future.done():
                continue
            self._delete_confirmation_tasks.pop(future, None)
            request_key = self._delete_confirmation_key(
                task.catalog,
                task.rel_paths,
                task.directory_rel,
            )
            self._settling_delete_confirmation_keys.add(request_key)
            try:
                self._finish_delete_confirmation(future, task)
            finally:
                self._settling_delete_confirmation_keys.discard(request_key)

    def _finish_delete_confirmation(
        self,
        future: Future[object],
        task: DeleteConfirmationTask,
    ) -> None:
        """Finish one identity check while suppressing nested duplicate prompts."""

        if future.cancelled() or not self._delete_confirmation_is_current(task):
            return
        try:
            captured = future.result()
        except Exception as error:
            show_error(task.owner, "Delete Directory" if task.directory_rel else "Delete", str(error))
            if self.current_catalog is task.catalog:
                self.load_current_directory(preserve_selection=True)
            return
        if task.directory_rel:
            directory = task.catalog.root.joinpath(*Path(task.directory_rel).parts)
            if not ask_delete_directory(task.owner, directory):
                return
            if (
                not self._delete_confirmation_can_commit_after_prompt(task)
                or self.workspace.catalog_for_root(task.catalog.root) is not task.catalog
            ):
                return
            self._queue_confirmed_directory_delete(
                task.catalog,
                task.directory_rel,
                captured,
                wipe=task.wipe,
            )
            return
        if not isinstance(captured, dict) or any(
            rel_path not in captured for rel_path in task.rel_paths
        ):
            show_error(task.owner, "Delete", "An image changed or disappeared before confirmation.")
            return
        if isinstance(task.owner, FullscreenViewer):
            confirmed = bool(
                task.owner.run_with_visible_cursor(
                    lambda: ask_delete_files(task.owner, len(task.rel_paths))
                )
            )
        else:
            confirmed = ask_delete_files(task.owner, len(task.rel_paths))
        if not confirmed:
            return
        if (
            not self._delete_confirmation_can_commit_after_prompt(task)
            or self.workspace.catalog_for_root(task.catalog.root) is not task.catalog
        ):
            return
        self.queue_delete_images(
            task.catalog,
            task.rel_paths,
            expected_identities=captured,
            wipe=task.wipe,
            remove_from_current_view=task.remove_from_current_view,
            viewer=task.owner if isinstance(task.owner, FullscreenViewer) else None,
        )

    def is_restorable_trash_rel(self, rel_path: str) -> bool:
        return is_inside_trash_rel_path(rel_path)

    def is_restorable_trash_record(self, record: PaneRecord) -> bool:
        if isinstance(record, DirectoryRecord):
            return self.is_restorable_trash_rel(record.dir_rel)
        return self.is_restorable_trash_rel(record.rel_path)

    def restore_trash_directory(self, root: Path, dir_rel: str) -> None:
        catalog = self.workspace.catalog_for_exact_root(root.expanduser().absolute())
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
        if (
            not restore_items
            or self.workspace.catalog_for_exact_root(catalog.root) is not catalog
        ):
            return
        pending_targets = self._pending_image_save_targets(catalog.root)
        overlaps_pending_save = any(
            rel_path in pending_targets
            if kind == "image"
            else any(
                target == rel_path or target.startswith(f"{rel_path}/")
                for target in pending_targets
            )
            for kind, rel_path in restore_items
        )
        if overlaps_pending_save:
            self.progress_label.setText("Wait for the pending image save before restoring from trash")
            return
        self._start_restore_identity_preflight(catalog, restore_items)

    @staticmethod
    def _capture_mutation_identities_worker(
        image_groups: Mapping[Path, Sequence[str]],
        directory_groups: Mapping[Path, Sequence[str]],
        expected_root_identities: Mapping[Path, tuple[int, int]],
        expected_storage_identities: Mapping[Path, CatalogStorageIdentity],
    ) -> MutationIdentityResult:
        image_identities: dict[Path, dict[str, object]] = {}
        directory_identities: dict[Path, dict[str, object]] = {}
        for root in {*image_groups, *directory_groups}:
            with Catalog.open_reader(
                root,
                expected_root_identity=expected_root_identities.get(root),
                expected_storage_identity=expected_storage_identities.get(root),
            ) as catalog:
                image_rels = tuple(image_groups.get(root, ()))
                directory_rels = tuple(directory_groups.get(root, ()))
                if image_rels:
                    image_identities[root] = dict(catalog.file_identities(image_rels))
                if directory_rels:
                    directory_identities[root] = {
                        dir_rel: catalog.directory_identity(dir_rel)
                        for dir_rel in directory_rels
                    }
        return MutationIdentityResult(image_identities, directory_identities)

    @classmethod
    def _capture_move_identities_worker(
        cls,
        image_groups: Mapping[Path, Sequence[str]],
        directory_groups: Mapping[Path, Sequence[str]],
        dest_root: Path,
        dest_dir_rel: str,
        expected_root_identities: Mapping[Path, tuple[int, int]],
        expected_storage_identities: Mapping[Path, CatalogStorageIdentity],
    ) -> MutationIdentityResult:
        captured = cls._capture_mutation_identities_worker(
            image_groups,
            directory_groups,
            expected_root_identities,
            expected_storage_identities,
        )
        with Catalog.open_reader(
            dest_root,
            expected_root_identity=expected_root_identities.get(dest_root),
            expected_storage_identity=expected_storage_identities.get(dest_root),
        ) as dest_catalog:
            captured.destination_identity = dest_catalog.directory_identity(dest_dir_rel)
        return captured

    def _next_mutation_identity_generation(self) -> int:
        self._mutation_identity_generation += 1
        return self._mutation_identity_generation

    def _show_identity_preflight_busy(self, action: str) -> None:
        """Make bounded admission visible instead of silently dropping an action."""

        if self._closing:
            return
        message = f"Cannot {action} yet: file checks are busy; try again shortly"
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label.setText(message)
        self.statusBar().showMessage(message, 5000)

    def _start_restore_identity_preflight(
        self,
        catalog: Catalog,
        restore_items: tuple[tuple[str, str], ...],
    ) -> None:
        image_groups = {
            catalog.root: [
                rel_path for kind, rel_path in restore_items if kind == "image"
            ]
        }
        directory_groups = {
            catalog.root: [
                rel_path for kind, rel_path in restore_items if kind == "directory"
            ]
        }
        image_groups = {root: items for root, items in image_groups.items() if items}
        directory_groups = {
            root: items for root, items in directory_groups.items() if items
        }
        try:
            future = self.identity_executor.submit(
                self._capture_mutation_identities_worker,
                image_groups,
                directory_groups,
                {catalog.root: catalog.root_identity},
                {catalog.root: catalog.storage_identity},
            )
        except RuntimeError:
            self._show_identity_preflight_busy("restore")
            return
        preflight = RestoreIdentityPreflightTask(
            generation=self._next_mutation_identity_generation(),
            catalog=catalog,
            restore_items=restore_items,
            future=future,
        )
        self._restore_identity_preflights[future] = preflight
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Checking items before restore")

    def _settle_restore_identity_preflights(self) -> None:
        for future, preflight in list(self._restore_identity_preflights.items()):
            if not future.done():
                continue
            self._restore_identity_preflights.pop(future, None)
            if future.cancelled() or not self._restore_identity_preflight_is_current(preflight):
                continue
            try:
                captured = future.result()
            except Exception as error:
                self._mutation_identity_preflight_failed(
                    "Restore",
                    error,
                    {preflight.catalog.root},
                )
                continue
            self._enqueue_preflighted_restore(preflight, captured)

    def _restore_identity_preflight_is_current(
        self,
        preflight: RestoreIdentityPreflightTask,
    ) -> bool:
        return (
            preflight.generation <= self._mutation_identity_generation
            and self.workspace.catalog_for_exact_root(preflight.catalog.root)
            is preflight.catalog
        )

    def _enqueue_preflighted_restore(
        self,
        preflight: RestoreIdentityPreflightTask,
        captured: MutationIdentityResult,
    ) -> None:
        catalog = preflight.catalog
        restore_items = preflight.restore_items
        source_images = tuple(
            (catalog.root, rel_path)
            for kind, rel_path in restore_items
            if kind == "image"
        )
        source_directories = tuple(
            (catalog.root, rel_path)
            for kind, rel_path in restore_items
            if kind == "directory"
        )
        self._queue_catalog_mutation(
            catalog,
            label="Restoring from trash",
            dest_dir_rel=TRASH_DIR_NAME,
            priority=ActionPriority.FILE_MOVE_WITHIN_CATALOG,
            worker=lambda task: self._restore_records_worker(
                catalog.root,
                restore_items,
                captured.image_identities,
                captured.directory_identities,
                task,
            ),
            source_images=source_images,
            source_directories=source_directories,
            expected_image_identities=captured.image_identities,
            expected_directory_identities=captured.directory_identities,
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
        expected_image_identities: dict[Path, dict[str, object]] | None = None,
        expected_directory_identities: dict[Path, dict[str, object]] | None = None,
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
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
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
            expected_image_identities=expected_image_identities,
            expected_directory_identities=expected_directory_identities,
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

    @staticmethod
    def _define_catalog_tags_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        names: Sequence[str],
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, len(names), "catalog tags")
        task.check_canceled()
        with Catalog.open_writer(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            stored = catalog.define_tags(names)
        task.update(len(names), len(names), "catalog tags")
        task.mark_done()
        return MovePayloadResult(len(names), len(stored), {root})

    @staticmethod
    def _set_image_tags_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        rel_path: str,
        names: Sequence[str],
        expected_identity: object | None,
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, rel_path)
        task.check_canceled()
        with Catalog.open_writer(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            catalog.set_image_tags(
                rel_path,
                names,
                replace=True,
                expected_identity=expected_identity,
            )
        task.update(1, 1, rel_path)
        task.mark_done()
        return MovePayloadResult(1, 1, {root})

    @staticmethod
    def _set_catalog_settings_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        settings: CatalogSettings,
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, "catalog settings")
        task.check_canceled()
        with Catalog.open_writer(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            old_thumbnail_size = catalog.settings.thumbnail_native_size
            catalog.set_settings(settings)
        task.update(1, 1, "catalog settings")
        task.mark_done()
        return MovePayloadResult(
            1,
            1,
            {root},
            catalog_settings=settings,
            force_thumbnail_reindex=(old_thumbnail_size != settings.thumbnail_native_size),
        )

    def _create_directory_worker(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        parent_dir_rel: str,
        name: str,
        expected_parent_identity: object,
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, parent_dir_rel or ".")
        with Catalog.open_writer(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            created_dir_rel = catalog.create_directory(
                parent_dir_rel,
                name,
                expected_parent_identity=expected_parent_identity,
            )
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
        original_file_dates: FileDateSnapshot | None = None,
        owner: QWidget | None = None,
    ) -> MovePayloadTask:
        operation_list = tuple(operations)
        root = catalog.root
        pending_image_saves = sum(
            1
            for move_task in self._move_payload_tasks
            if move_task.dedicated_executor and not move_task.future.done()
        )
        if pending_image_saves >= MAX_PENDING_IMAGE_SAVES:
            raise OSError(
                "Too many image saves are already pending. Wait for one to finish and try again."
            )
        if any(
            not delete_task.future.done()
            and delete_task.root == root
            and rel_path in delete_task.rel_paths
            for delete_task in self._delete_payload_tasks
        ) or any(
            not move_task.future.done() and root in move_task.affected_roots
            for move_task in self._move_payload_tasks
        ) or any(
            root in preflight.affected_roots
            for preflight in self._move_identity_preflights.values()
        ) or any(
            root == preflight.catalog.root
            for preflight in self._restore_identity_preflights.values()
        ) or (
            self._duplicate_delete_task is not None
            and not self._duplicate_delete_task.future.done()
            and self._duplicate_delete_task.root == root
        ):
            raise OSError(
                "Wait for the pending catalog file operation before saving this image."
            )
        dir_rel = Path(rel_path).parent.as_posix()
        if dir_rel == ".":
            dir_rel = ""
        self._failed_image_edits.pop((root, rel_path), None)
        self.indexer.cancel_idle_tasks(root)
        self.indexer.cancel_directory_tasks(root)
        task = IndexTask(
            "Saving image edit",
            root,
            dir_rel,
            interactive=True,
            idle_sleep_seconds=0.0,
            force_refresh=True,
            priority=ActionPriority.FILE_DELETE,
            preemptible=False,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        try:
            future = self.file_move_executor.submit(
                self._save_image_edit_worker,
                root,
                rel_path,
                operation_list,
                preserve_file_dates,
                expected_identity,
                original_file_dates,
                task,
                expected_root_identity=catalog.root_identity,
            )
        except ExecutorSaturatedError as error:
            raise OSError(
                "Too many image saves are already pending. Wait for one to finish and try again."
            ) from error
        task.bind_future(future)
        mutation = MovePayloadTask(
            dest_root=root,
            dest_dir_rel=dir_rel,
            affected_roots={root},
            task=task,
            future=future,
            started_at=monotonic(),
            target_images=((root, rel_path),),
            completion_verb="Saved",
            error_title="Save Image",
            dedicated_executor=True,
            edit_operations=operation_list,
            edit_owner=owner,
        )
        self._move_payload_tasks.append(mutation)
        self._refresh_active_move_payload_task()
        if isinstance(owner, FullscreenViewer):
            owner.image_save_started(rel_path)
        self._show_move_payload_status(task.snapshot())
        self._update_tools_menu_actions()
        return mutation

    def _save_image_edit_worker(
        self,
        root: Path,
        rel_path: str,
        operations: tuple[EditOperation, ...],
        preserve_file_dates: bool,
        expected_identity: ImageFileIdentity | None,
        original_file_dates: FileDateSnapshot | None,
        task: IndexTask,
        *,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> MovePayloadResult:
        try:
            task.update(0, 1, rel_path)
            warning: str | None = None
            committed_proof = None
            try:
                with Catalog.open_filesystem_handle(
                    root,
                    expected_root_identity=expected_root_identity,
                ) as catalog:
                    path = catalog.mutation_path(rel_path)
                    save_result = apply_operations_to_file_with_proof(
                        path,
                        operations,
                        preserve_timestamp=False,
                        preserve_file_dates=preserve_file_dates,
                        expected_identity=expected_identity,
                        original_file_dates=original_file_dates,
                    )
                committed_proof = save_result.committed_proof
            except ImageSaveCommittedError as error:
                warning = str(error)
                committed_proof = error.committed_proof
            target_proofs = (
                {rel_path: committed_proof.as_catalog_proof()}
                if committed_proof is not None
                else {}
            )
            if committed_proof is None:
                proof_warning = (
                    "The image was saved, but no causal proof of the committed bytes was available."
                )
                warning = f"{warning}\n\n{proof_warning}" if warning else proof_warning
            task.update(1, 1, rel_path)
            task.mark_done()
            return MovePayloadResult(
                1,
                1,
                {root},
                warning=warning,
                target_proofs=target_proofs,
            )
        except Exception as error:
            task.mark_failed(error)
            raise

    def _reconcile_saved_image_worker(
        self,
        root: Path,
        rel_path: str,
        expected_proof: object | None,
        task: IndexTask,
    ) -> None:
        task.update(0, 1, rel_path)
        with Catalog.open_writer(
            root,
            expected_root_identity=task.expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            if not isinstance(expected_proof, tuple) or len(expected_proof) != 6:
                raise OSError(f"Saved image proof is unavailable: {rel_path}")
            record = catalog.index_image(
                rel_path,
                task.check_canceled,
                force=True,
                expected_proof=expected_proof,
            )
            if record is None:
                raise FileNotFoundError(f"Saved image could not be re-indexed: {rel_path}")
            if (
                catalog.file_identity(rel_path)[:5] != expected_proof[:5]
                or record.image_hash != expected_proof[5]
            ):
                raise OSError(f"Saved image changed during catalog reconciliation: {rel_path}")
            catalog.append_log(f"File edit saved: {rel_path}")
        task.update(1, 1, rel_path)
        task.mark_done()

    def _queue_thumbnail_repair(self, root: Path, rel_path: str) -> None:
        resolved = root
        key = (resolved, rel_path)
        if key in self._thumbnail_repair_tasks:
            return
        catalog = self.workspace.catalog_for_root(resolved)
        if catalog is None:
            return
        dir_rel = rel_path.rpartition("/")[0]
        task, _ = self.indexer.submit_action(
            "Repairing thumbnail",
            resolved,
            dir_rel,
            priority=ActionPriority.THUMBNAIL_INDEX,
            worker=lambda action_task: self._repair_thumbnail_worker(
                resolved,
                rel_path,
                action_task,
            ),
            key=f"thumbnail-repair:{resolved}:{rel_path}",
            interactive=False,
            force_refresh=True,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        self._thumbnail_repair_tasks[key] = task

    @staticmethod
    def _repair_thumbnail_worker(root: Path, rel_path: str, task: IndexTask) -> None:
        task.update(0, 1, rel_path)
        with Catalog.open_writer(
            root,
            expected_root_identity=task.expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            record = catalog.index_image(rel_path, task.check_canceled, force=True)
            if record is None:
                raise FileNotFoundError(rel_path)
        task.update(1, 1, rel_path)
        task.mark_done()

    def _settle_thumbnail_repair_tasks(self) -> None:
        for key, task in list(self._thumbnail_repair_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._thumbnail_repair_tasks.pop(key, None)
            root, rel_path = key
            if snapshot.error is not None or snapshot.canceled:
                continue
            if self.current_catalog is not None and self.current_catalog.root == root:
                self.model.refresh_thumbnail(rel_path)

    def _delete_directory_worker(
        self,
        root: Path,
        dir_rel: str,
        wipe: bool,
        expected_identity: tuple[int, int, int, int],
        task: IndexTask,
    ) -> MovePayloadResult:
        task.update(0, 1, dir_rel)
        with Catalog.open_writer(
            root,
            expected_root_identity=task.expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
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
        expected_image_identities: Mapping[Path, Mapping[str, object]],
        expected_directory_identities: Mapping[Path, Mapping[str, object]],
        task: IndexTask,
    ) -> MovePayloadResult:
        restored = 0
        reconcile_subtrees: list[tuple[Path, str]] = []
        reconcile_images: list[tuple[Path, str]] = []
        with Catalog.open_writer(
            root,
            expected_root_identity=task.expected_root_identity,
            expected_storage_identity=task.expected_storage_identity,
        ) as catalog:
            expected_images = expected_image_identities.get(root, {})
            expected_directories = expected_directory_identities.get(root, {})
            missing_identities = [
                rel_path
                for kind, rel_path in restore_items
                if rel_path not in (
                    expected_directories if kind == "directory" else expected_images
                )
            ]
            if missing_identities:
                raise OSError(
                    "identity was not captured before restore: "
                    + ", ".join(missing_identities[:3])
                )
            self._verify_expected_mutation_identities(
                catalog,
                expected_images,
                expected_directories,
                verb="restore",
            )
            for index, (kind, rel_path) in enumerate(restore_items, start=1):
                task.update(index - 1, len(restore_items), rel_path)
                if kind == "directory":
                    if rel_path not in expected_directories:
                        raise OSError(f"directory identity was not captured before restore: {rel_path}")
                    result = catalog.restore_directory_from_trash(
                        rel_path,
                        expected_identity=expected_directories[rel_path],
                    )
                    reconcile_subtrees.append(
                        (result.dest_catalog_root, result.dest_rel_path)
                    )
                else:
                    if rel_path not in expected_images:
                        raise OSError(f"image identity was not captured before restore: {rel_path}")
                    result = catalog.restore_image_from_trash(
                        rel_path,
                        expected_identity=expected_images[rel_path],
                    )
                    reconcile_images.append(
                        (result.dest_catalog_root, result.dest_rel_path)
                    )
                restored += 1
                task.update(index, len(restore_items), rel_path)
        task.mark_done()
        return MovePayloadResult(
            len(restore_items),
            restored,
            {root},
            reconcile_subtrees=tuple(reconcile_subtrees),
            reconcile_images=tuple(reconcile_images),
        )

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
        intended_root = root.expanduser().resolve()
        intent_sequence = self._record_catalog_intent(intended_root)
        init_started_at = monotonic()
        was_open = self.workspace.catalog_for_root(root) is not None
        catalog = self.workspace.open_catalog(root)
        self._successful_catalog_open_intents[catalog.root] = intent_sequence
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
        configured_restore: bool = False,
        select_when_opened: bool = True,
    ) -> None:
        if self._closing:
            return
        operation_started_at = monotonic()
        # Keep the GUI path lexical.  resolve()/is_dir() may block for seconds
        # on an unavailable mount; canonicalization belongs to the worker.
        intended_root = root.expanduser().absolute()
        intent_sequence = (
            self._record_catalog_intent(
                intended_root,
                initial_restore=configured_restore,
            )
            if select_when_opened
            else 0
        )
        if selected_at is not None:
            self._append_timing_event(
                root,
                "deferred_open_start",
                (operation_started_at - selected_at) * 1000,
            )
        existing = (
            None
            if configured_restore
            else self.workspace.catalog_for_exact_root(intended_root)
        )
        if existing is not None:
            if select_when_opened:
                self._successful_catalog_open_intents[existing.root] = intent_sequence
            self._finish_open_catalog_ui(
                existing,
                was_open=True,
                log_event=log_event,
                operation_started_at=operation_started_at,
                mode="async_existing",
                make_current=select_when_opened,
            )
            return
        for task in self._catalog_open_tasks.values():
            task_root = task.root.expanduser().absolute()
            if task_root == intended_root:
                task.discard_result = False
                if select_when_opened:
                    task.select_when_opened = True
                    task.intent_sequence = intent_sequence
                    task.configured_restore = task.configured_restore and configured_restore
                    task.log_event = task.log_event or log_event
                    task.selected_at = selected_at or task.selected_at
                self._show_catalog_open_status(root)
                return
        self._trim_catalog_open_tasks_for_submission()
        try:
            future = self.catalog_open_executor.submit(self._open_catalog_worker, root)
        except RuntimeError:
            # Every lane can be trapped in an unavailable filesystem. Keep the
            # GUI responsive and retain a strict global bound instead of
            # accumulating an unlimited queue of superseded open intents.
            # This intent still reached a terminal failure. Marking it lets a
            # previously admitted success become current when it finishes;
            # otherwise the rejected sequence can strand every older result
            # as permanently stale.
            if select_when_opened:
                self._failed_catalog_open_intents.add(intent_sequence)
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText(
                "Catalog open workers are busy; try this catalog again shortly"
            )
            self._select_newest_successful_catalog_if_needed()
            return
        self._catalog_open_tasks[future] = CatalogOpenTask(
            root=root,
            future=future,
            log_event=log_event,
            selected_at=selected_at,
            started_at=operation_started_at,
            configured_restore=configured_restore,
            select_when_opened=select_when_opened,
            intent_sequence=intent_sequence,
        )
        self._show_catalog_open_status(root)

    def _trim_catalog_open_tasks_for_submission(self) -> None:
        """Bound stale open intents while retaining several fallback lanes."""

        while len(self._catalog_open_tasks) >= MAX_CATALOG_OPEN_TASKS:
            future, task = min(
                self._catalog_open_tasks.items(),
                key=lambda item: item[1].started_at,
            )
            self._catalog_open_tasks.pop(future, None)
            task.discard_result = True
            if not future.cancel():
                future.add_done_callback(self._close_discarded_catalog_open_result)

    def _record_catalog_intent(
        self,
        root: Path | None,
        *,
        initial_restore: bool = False,
    ) -> int:
        if not initial_restore:
            self._mark_initial_config_catalog_interaction()
        self._catalog_intent_sequence += 1
        self._catalog_intent_root = None if root is None else root.expanduser().absolute()
        return self._catalog_intent_sequence

    def _open_catalog_worker(self, root: Path) -> CatalogOpenResult:
        init_started_at = monotonic()
        expanded_root = root.expanduser()
        if not expanded_root.is_dir():
            raise FileNotFoundError(f"Catalog directory is unavailable: {expanded_root}")
        catalog = Catalog(expanded_root, create_root=False)
        # Preferences and pane setup read this cached snapshot on Qt. Warm it
        # while the catalog is still on its open worker so a busy SQLite file
        # cannot make the first preferences dialog pause.
        _ = catalog.settings
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
            self._append_catalog_log_async(catalog, "Catalog added to workspace")
            self._record_timing_phase(catalog.root, "append_open_log", phase_started_at, {"mode": mode})
        phase_started_at = monotonic()
        self._swept_catalog_roots.discard(catalog.root)
        self._pruned_catalog_roots.discard(catalog.root)
        self._idle_index_tasks.pop(catalog.root, None)
        if not was_open:
            self._shallow_tree_roots.add(catalog.root)
        self._record_timing_phase(catalog.root, "prepare_open_state", phase_started_at, {"mode": mode})

        if make_current:
            self.current_catalog = catalog
            self._current_catalog_intent_sequence = self._catalog_intent_sequence
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
        # Submit the visible root before deep discovery. Both share the
        # serialized index lane, so the inverse order can leave the initial
        # thumbnail pane waiting behind a very large recursive tree walk.
        self.queue_directory_index(catalog, "", interactive=make_current)
        self._record_timing_phase(catalog.root, "queue_root_directory_index", phase_started_at, {"mode": mode})

        phase_started_at = monotonic()
        self._directory_discovery_tasks[catalog.root] = self.indexer.discover_directories(
            catalog.root,
            interactive=False,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        self._record_timing_phase(catalog.root, "start_directory_discovery", phase_started_at, {"mode": mode})
        QTimer.singleShot(0, self._poll_indexer)

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

    def _append_catalog_log_async(self, catalog: Catalog, message: str) -> None:
        """Queue ancillary logging without touching a slow mount on the GUI."""

        if self._closing:
            return
        try:
            self.timing_executor.submit(catalog.append_log, message)
        except RuntimeError:
            return

    def _append_timing_event(
        self,
        root: Path,
        phase: str,
        duration_ms: float | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        if self._closing:
            return
        # Timing is ancillary, so fail closed unless an already-open Catalog
        # can authorize the exact state tree. Capturing these immutable inode
        # identities on Qt is lookup-only and prevents a queued timing write
        # from following a replacement ``.marnwick`` directory.
        catalog = self.workspace.catalog_for_exact_root(root.expanduser().absolute())
        if catalog is None:
            return
        try:
            self.timing_executor.submit(
                self._write_timing_event,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                phase,
                duration_ms,
                details,
            )
        except RuntimeError:
            return

    def _write_timing_event(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        phase: str,
        duration_ms: float | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            expanded_root = root.expanduser().absolute()
            Catalog.assert_storage_identity(
                expanded_root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
            state_dir = expanded_root / ".marnwick"
            timings_path = state_dir / TIMINGS_FILE_NAME
            if timings_path.is_symlink():
                return
            payload: object = {}
            read_fd = -1
            try:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                read_fd = os.open(timings_path, flags)
                timing_stat = os.fstat(read_fd)
                if (
                    not stat.S_ISREG(timing_stat.st_mode)
                    or timing_stat.st_nlink != 1
                    or timing_stat.st_size > MAX_TIMING_FILE_BYTES
                ):
                    return
                chunks: list[bytes] = []
                remaining = timing_stat.st_size + 1
                while remaining > 0:
                    chunk = os.read(read_fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if len(raw) != timing_stat.st_size:
                    return
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = {}
            finally:
                if read_fd >= 0:
                    os.close(read_fd)
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
                    key: (
                        str(value)[:4096]
                        if isinstance(value, (Path, str))
                        else value
                    )
                    for key, value in details.items()
                }
            events.append(event)
            payload["version"] = 1
            payload["events"] = events[-MAX_TIMING_EVENTS:]
            data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            while len(data) > MAX_TIMING_FILE_BYTES and len(payload["events"]) > 1:
                payload["events"] = payload["events"][len(payload["events"]) // 2 :]
                data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(data) > MAX_TIMING_FILE_BYTES:
                return
            Catalog.assert_storage_identity(
                expanded_root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
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
                Catalog.assert_storage_identity(
                    expanded_root,
                    expected_root_identity=expected_root_identity,
                    expected_storage_identity=expected_storage_identity,
                )
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
                if task.configured_restore:
                    catalog_path = str(task.root.expanduser())
                    if catalog_path not in self._unavailable_catalog_paths:
                        self._unavailable_catalog_paths.append(catalog_path)
                    self.progress_label.setText(
                        f"Could not restore catalog {task.root.name or task.root}"
                    )
                else:
                    show_error(self, "Open Catalog", str(error))
                if task.select_when_opened:
                    self._failed_catalog_open_intents.add(task.intent_sequence)
                continue
            if task.discard_result or self._closing:
                result.catalog.close()
                continue
            catalog, was_open = self.workspace.adopt_catalog(result.catalog)
            self._append_timing_event(catalog.root, "catalog_init", result.init_duration_ms)
            if task.select_when_opened:
                self._successful_catalog_open_intents[catalog.root] = max(
                    task.intent_sequence,
                    self._successful_catalog_open_intents.get(catalog.root, 0),
                )
            if task.configured_restore:
                requested_text = str(task.root.expanduser())
                self._unavailable_catalog_paths = [
                    path_text
                    for path_text in self._unavailable_catalog_paths
                    if path_text != requested_text
                ]
            make_current = (
                task.select_when_opened
                and task.intent_sequence == self._catalog_intent_sequence
            )
            self._finish_open_catalog_ui(
                catalog,
                was_open=was_open,
                log_event=task.log_event,
                operation_started_at=task.started_at,
                mode="async",
                make_current=make_current,
            )
        self._select_newest_successful_catalog_if_needed()

    def _select_newest_successful_catalog_if_needed(self) -> None:
        if self._closing or self._catalog_intent_sequence not in self._failed_catalog_open_intents:
            return
        if any(
            not task.discard_result
            and task.intent_sequence == self._catalog_intent_sequence
            for task in self._catalog_open_tasks.values()
        ):
            return
        candidates = [
            (sequence, catalog)
            for catalog in self.workspace.catalogs
            if (sequence := self._successful_catalog_open_intents.get(catalog.root)) is not None
        ]
        if not candidates:
            return
        sequence, catalog = max(candidates, key=lambda item: item[0])
        if sequence <= self._current_catalog_intent_sequence:
            return
        self.current_catalog = catalog
        self._current_catalog_intent_sequence = sequence
        self.current_dir_rel = ""
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self._catalog_intent_root = catalog.root
        self.rebuild_tree()
        self.load_current_directory()
        self.queue_directory_index(catalog, "", interactive=True)

    def _has_active_catalog_open_tasks(self) -> bool:
        return bool(self._catalog_open_tasks)

    def _active_catalog_open_task(self) -> CatalogOpenTask | None:
        tasks = list(self._catalog_open_tasks.values())
        if not tasks:
            return None
        return max(tasks, key=lambda task: task.intent_sequence)

    def _cancel_catalog_open_tasks(self, root: Path) -> None:
        intended_root = root.expanduser().absolute()
        for future, task in list(self._catalog_open_tasks.items()):
            matches_root = task.root.expanduser().absolute() == intended_root
            if not matches_root:
                continue
            task.discard_result = True
            if future.cancel():
                self._catalog_open_tasks.pop(future, None)
            self._append_timing_event(task.root, "catalog_init_canceled", None)

    def close_catalog(self, root: Path) -> None:
        if self._initial_config_load_pending() and not self._applying_initial_config:
            self._initial_config_catalog_exclusions.add(root.expanduser().absolute())
        self._mark_initial_config_catalog_interaction()
        exact_catalog = self.workspace.catalog_for_exact_root(root.expanduser().absolute())
        resolved = exact_catalog.root if exact_catalog is not None else root.expanduser().absolute()
        self._pending_exclusion_cache.pop(resolved, None)
        if self._catalog_intent_root in {resolved, root.expanduser().absolute()}:
            self._record_catalog_intent(None)
        self._cancel_catalog_open_tasks(resolved)
        self._cancel_virtual_view_tasks(resolved)
        self._cancel_duplicate_delete_task(resolved, wait=False)
        self._cancel_move_payload_task(resolved, wait=False)
        # Identity capture is read-only and authorizes no mutation by itself.
        # Detach it on close so a blocked disconnected source cannot trap the
        # user in an uncloseable modal or enqueue work after the catalog closes.
        self._cancel_mutation_identity_preflights(resolved)
        for future, confirmation in list(self._delete_confirmation_tasks.items()):
            if confirmation.catalog.root != resolved:
                continue
            self._delete_confirmation_tasks.pop(future, None)
            future.cancel()
        if not self._wait_for_catalog_file_tasks(resolved):
            return
        for task_root, task in list(self._idle_index_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for task_root, task in list(self._directory_discovery_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for (task_root, _), task in list(self._directory_index_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for task, context in list(self._post_move_reconcile_tasks.items()):
            if context.root == resolved:
                task.cancel()
        for task_root, task in list(self._thumbnail_prune_tasks.items()):
            if task_root == resolved:
                task.cancel()
        for (task_root, _), task in list(self._thumbnail_repair_tasks.items()):
            if task_root == resolved:
                task.cancel()
        self._drop_very_similar_cache(resolved)
        catalog = self.workspace.catalog_for_root(root)
        if catalog is not None:
            self._append_catalog_log_async(catalog, "Catalog removed from workspace")
        if self.current_catalog and self.current_catalog.root == resolved:
            # Invalidate queued thumbnail generations before closing the
            # UI-owned catalog connection. Workers use independent read-only
            # connections, but stale results must never repaint the next view.
            self.model.set_images(None, [])
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
        self._post_move_reconcile_tasks = {
            task: context
            for task, context in self._post_move_reconcile_tasks.items()
            if context.root != resolved
        }
        self._thumbnail_prune_tasks.pop(resolved, None)
        self._thumbnail_repair_tasks = {
            key: task for key, task in self._thumbnail_repair_tasks.items() if key[0] != resolved
        }
        self._image_reconcile_retries = {
            key: context
            for key, context in self._image_reconcile_retries.items()
            if key[0] != resolved
        }
        self._resume_idle_refresh_roots.discard(resolved)
        self._shallow_tree_roots.discard(resolved)
        self._pending_tree_rebuilds.pop(resolved, None)
        for future, task in list(self._tree_children_tasks.items()):
            if task.catalog.root != resolved:
                continue
            task.cancel_event.set()
            future.cancel()
            self._tree_children_tasks.pop(future, None)
        self._tree_child_next_offsets = {
            key: offset
            for key, offset in self._tree_child_next_offsets.items()
            if key[0] != resolved
        }
        self._tree_child_refresh_pending = {
            key for key in self._tree_child_refresh_pending if key[0] != resolved
        }
        for future, task in list(self._tree_tags_tasks.items()):
            if task.catalog.root != resolved:
                continue
            task.cancel_event.set()
            future.cancel()
            self._tree_tags_tasks.pop(future, None)
        self._tree_tag_next_offsets.pop(resolved, None)
        self._tree_item_maps.pop(resolved, None)
        self._tree_tag_cache.pop(resolved, None)
        self._tree_expanded_state = {key for key in self._tree_expanded_state if key[0] != resolved}
        self._tree_known_state = {key for key in self._tree_known_state if key[0] != resolved}
        self._successful_catalog_open_intents.pop(resolved, None)
        if self.current_catalog and self.current_catalog.root == resolved:
            self.current_catalog = None
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            self.update_selection_status()
        self.rebuild_tree()

    def _wait_for_catalog_file_tasks(self, root: Path) -> bool:
        """Responsively settle protected file changes before closing a catalog."""

        resolved = root

        def pending_tasks() -> list[IndexTask]:
            tasks: list[IndexTask] = []
            duplicate = self._duplicate_delete_task
            if duplicate is not None and duplicate.root == resolved and not duplicate.future.done():
                tasks.append(duplicate.task)
            tasks.extend(
                task.task
                for task in self._delete_payload_tasks
                if task.root == resolved and not task.future.done()
            )
            tasks.extend(
                task.task
                for task in self._move_payload_tasks
                if resolved in task.affected_roots and not task.future.done()
            )
            tasks.extend(
                task
                for task, context in self._image_reconcile_tasks.items()
                if context.root == resolved and not task.snapshot().done
            )
            return tasks

        def has_pending_preflight() -> bool:
            return any(
                resolved in preflight.affected_roots
                for preflight in self._move_identity_preflights.values()
            ) or any(
                preflight.catalog.root == resolved
                for preflight in self._restore_identity_preflights.values()
            )

        self._settle_move_identity_preflights()
        self._settle_restore_identity_preflights()
        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        self._settle_image_reconcile_tasks()
        self._submit_pending_image_reconcile_retries(resolved)
        self._flush_deferred_delete_requests()
        if not pending_tasks() and not has_pending_preflight():
            return True

        dialog = QDialog(self)
        dialog.setWindowTitle("Finishing File Changes")
        dialog.setWindowIcon(load_app_icon())
        dialog.setStyleSheet(DIALOG_STYLESHEET)
        layout = QVBoxLayout(dialog)
        label = QLabel("Finishing pending file changes safely...")
        label.setMinimumWidth(480)
        progress = QProgressBar()
        progress.setRange(0, 0)
        layout.addWidget(label)
        layout.addWidget(progress)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setText("Keep Catalog Open")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setModal(True)

        def update() -> None:
            self._settle_move_identity_preflights()
            self._settle_restore_identity_preflights()
            self._settle_duplicate_delete_task()
            self._settle_delete_payload_task()
            self._settle_move_payload_task()
            self._settle_image_reconcile_tasks()
            self._submit_pending_image_reconcile_retries(resolved)
            self._flush_deferred_delete_requests()
            active = pending_tasks()
            if not active:
                if has_pending_preflight():
                    label.setText("Checking pending file changes safely...")
                    return
                dialog.accept()
                return
            snapshot = active[0].snapshot()
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
        if pending_tasks() or has_pending_preflight():
            dialog.exec()
        timer.stop()
        finished = not pending_tasks() and not has_pending_preflight()
        with suppress(AttributeError, RuntimeError):
            dialog.deleteLater()
        return finished

    def _tree_publication_blocked(self) -> bool:
        """Whether changing tree geometry could invalidate an active drop target."""

        thumbnail_view = getattr(self, "thumbnail_view", None)
        return self._directory_drag_active or bool(
            thumbnail_view is not None and thumbnail_view.manual_drag_active()
        )

    def _resume_deferred_tree_publication(self) -> None:
        """Resume bounded tree work after either drag implementation finishes."""

        if self._closing or self._tree_publication_blocked():
            return
        if self._tree_rebuild_deferred:
            self._tree_rebuild_deferred = False
            self.rebuild_tree()
            return
        build_task = self._tree_build_task
        if build_task is not None:
            QTimer.singleShot(
                0,
                partial(
                    self._continue_incremental_tree_rebuild,
                    build_task.generation,
                ),
            )
        path_task = self._tree_path_selection_task
        if path_task is not None:
            QTimer.singleShot(
                0,
                partial(
                    self._continue_tree_path_selection,
                    path_task.generation,
                ),
            )
        if self._tree_children_tasks or self._tree_tags_tasks:
            QTimer.singleShot(0, self._poll_indexer)

    @staticmethod
    def _tree_directory_order_key(dir_rel: str) -> tuple[str, str]:
        return (dir_rel.casefold(), dir_rel)

    @staticmethod
    def _physical_tree_directory_rel(item: QTreeWidgetItem) -> str | None:
        if (
            item.data(0, VIRTUAL_KIND_ROLE)
            or item.data(0, TREE_LOAD_MORE_ROLE) is not None
            or item.data(0, TREE_LOAD_MORE_TAGS_ROLE) is not None
        ):
            return None
        dir_rel = item.data(0, DIR_REL_ROLE)
        return str(dir_rel) if dir_rel else None

    def _insert_tree_directory_item(
        self,
        parent: QTreeWidgetItem,
        item: QTreeWidgetItem,
        dir_rel: str,
    ) -> None:
        """Place one physical directory among its lexical siblings.

        On-demand navigation can materialize a path before background
        discovery returns earlier siblings.  Every physical insertion and
        reconciliation uses this helper so arrival order never becomes UI
        order.  Virtual and load-more rows remain after physical folders.
        """

        order_key = self._tree_directory_order_key(dir_rel)
        current_parent = item.parent()
        if current_parent is not None:
            current_index = current_parent.indexOfChild(item)
            if current_index >= 0:
                if current_parent is parent:
                    previous_rel = (
                        self._physical_tree_directory_rel(parent.child(current_index - 1))
                        if current_index > 0
                        else None
                    )
                    next_rel = (
                        self._physical_tree_directory_rel(parent.child(current_index + 1))
                        if current_index + 1 < parent.childCount()
                        else None
                    )
                    previous_is_ordered = current_index == 0 or (
                        previous_rel is not None
                        and self._tree_directory_order_key(previous_rel) <= order_key
                    )
                    next_is_ordered = next_rel is None or (
                        order_key <= self._tree_directory_order_key(next_rel)
                    )
                    if previous_is_ordered and next_is_ordered:
                        return
                current_parent.takeChild(current_index)
        physical_count = parent.childCount()
        while physical_count > 0:
            if self._physical_tree_directory_rel(parent.child(physical_count - 1)) is not None:
                break
            physical_count -= 1
        lower = 0
        upper = physical_count
        while lower < upper:
            middle = (lower + upper) // 2
            sibling_rel = self._physical_tree_directory_rel(parent.child(middle))
            if sibling_rel is None or order_key < self._tree_directory_order_key(sibling_rel):
                upper = middle
            else:
                lower = middle + 1
        parent.insertChild(lower, item)

    def rebuild_tree(self) -> None:
        if self._closing:
            return
        if self._tree_publication_blocked():
            self._tree_rebuild_deferred = True
            return
        self._cancel_tree_path_selection()
        self._cancel_active_tree_build(rollover_if_running=True)
        for future, task in list(self._tree_children_tasks.items()):
            task.cancel_event.set()
            future.cancel()
        self._tree_children_tasks.clear()
        for future, task in list(self._tree_tags_tasks.items()):
            task.cancel_event.set()
            future.cancel()
        self._tree_tags_tasks.clear()
        self._tree_child_next_offsets.clear()
        self._tree_child_refresh_pending.clear()
        self._pending_tree_rebuilds.clear()
        expanded_items = self._expanded_tree_items()
        known_items = self._known_tree_items()
        self.tree.clear()
        self._tree_item_maps.clear()
        selected_item: QTreeWidgetItem | None = None
        deferred_catalogs: list[Catalog] = []
        for catalog in self.workspace.catalogs:
            root_item = QTreeWidgetItem([catalog.root.name or str(catalog.root)])
            root_item.setIcon(0, self.folder_icon)
            root_item.setToolTip(0, str(catalog.root))
            root_item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
            root_item.setData(0, DIR_REL_ROLE, "")
            root_item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
            self.tree.addTopLevelItem(root_item)
            item_by_dir = {"": root_item}
            if self._is_current_tree_item(catalog.root, ""):
                selected_item = root_item
            # Opening a catalog must not parse a huge cache or materialize the
            # entire database-backed tree on the GUI thread. A shallow
            # filesystem read gives immediate navigation; known descendants
            # are acquired in bounded pages on later event-loop turns.
            directory_rels = self._initial_tree_directory_rels(catalog)
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
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )
                self._insert_tree_directory_item(parent_item, item, dir_rel)
                item_by_dir[dir_rel] = item
                if self._is_current_tree_item(catalog.root, dir_rel):
                    selected_item = item
            root_item.setExpanded(True)
            for dir_rel, item in item_by_dir.items():
                if self._tree_state_key_for_directory(catalog.root, dir_rel) in expanded_items:
                    item.setExpanded(True)
            virtual_selected_item = self._add_virtual_tree_items(
                catalog,
                root_item,
                expanded_items,
                known_items,
                tags=self._tree_tag_cache.get(catalog.root, ()),
            )
            if virtual_selected_item is not None:
                selected_item = virtual_selected_item
            if catalog.root not in self._shallow_tree_roots:
                deferred_catalogs.append(catalog)
            self._tree_item_maps[catalog.root] = item_by_dir
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
            self._expand_tree_item_ancestors(selected_item)
            self.tree.scrollToItem(selected_item)
        for catalog in deferred_catalogs:
            self._pending_tree_rebuilds[catalog.root] = (catalog, "large_tree")
        if deferred_catalogs:
            # Submission only schedules an off-thread read. Starting the
            # current catalog now also lets incremental path selection mark
            # its ancestors as protected from the build's stale-item cleanup.
            self._start_next_pending_tree_rebuild()
        if self.current_catalog is not None and self.current_virtual_kind is None:
            self._ensure_current_tree_path(self.current_catalog, self.current_dir_rel)
            current_root = self.current_catalog.root
            QTimer.singleShot(
                0,
                lambda root=current_root: self._request_tree_children_for_directory(
                    root,
                    "",
                ),
            )

    def _request_incremental_tree_rebuild(self, catalog: Catalog, *, reason: str) -> None:
        if self._tree_publication_blocked():
            self._tree_rebuild_deferred = True
            return
        active_task = self._tree_build_task
        if active_task is not None:
            if self.current_catalog is catalog and active_task.catalog is not catalog:
                old_catalog = active_task.catalog
                old_reason = active_task.reason
                self._pending_tree_rebuilds[old_catalog.root] = (old_catalog, old_reason)
                self._cancel_active_tree_build(rollover_if_running=True)
                self._start_incremental_tree_rebuild(catalog, reason=reason)
                return
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
        if self._tree_build_task is not None:
            self._cancel_active_tree_build(rollover_if_running=True)
        phase_started_at = monotonic()
        self._tree_build_generation += 1
        generation = self._tree_build_generation
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
            root_item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
            self.tree.addTopLevelItem(root_item)
            item_by_dir = {"": root_item}
        else:
            item_by_dir = self._tree_item_maps.get(catalog.root, {"": root_item})
            # Keep the previous fixed virtual subtree visible while the page
            # read is in flight. It is replaced atomically after the physical
            # tree has caught up.
        # Child pages may have been read while discovery was still committing
        # its inventory. Their terminal offsets and childless indicators are
        # no longer authoritative once this rebuild starts from the completed
        # database view. Invalidate them now; the root and expanded branches
        # are reconciled after the bounded flat build.
        self._tree_child_next_offsets = {
            key: offset
            for key, offset in self._tree_child_next_offsets.items()
            if key[0] != catalog.root
        }
        self._tree_child_refresh_pending = {
            key for key in self._tree_child_refresh_pending if key[0] != catalog.root
        }
        for item in item_by_dir.values():
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
        root_item.setExpanded(True)
        self._tree_build_task = TreeBuildTask(
            catalog=catalog,
            directories=[],
            total=None,
            page_offset=0,
            processed=0,
            # A bounded rebuild cannot prove that older, lazily loaded rows are
            # stale after it stops. Preserve them until a later explicit child
            # page replaces that part of the tree instead of deleting valid
            # navigation targets merely because they fell beyond the cap.
            seen_directories=set(item_by_dir),
            expanded_items=expanded_items,
            known_items=known_items,
            item_by_dir=item_by_dir,
            rebuilt_item_by_dir=dict(item_by_dir),
            cleanup_iterator=None,
            index=0,
            selected_item=root_item if self._is_current_tree_item(catalog.root, "") else None,
            started_at=phase_started_at,
            reason=reason,
            generation=generation,
            page_future=None,
            page_cancel_event=None,
            tags=self._tree_tag_cache.get(catalog.root, ()),
        )
        self._record_timing_phase(
            catalog.root,
            "start_incremental_tree_rebuild",
            phase_started_at,
            {"reason": reason},
        )
        self._continue_incremental_tree_rebuild()

    def _cancel_active_tree_build(self, *, rollover_if_running: bool = False) -> None:
        task = self._tree_build_task
        if task is None:
            return
        self._tree_build_generation += 1
        if task.page_cancel_event is not None:
            task.page_cancel_event.set()
        page_future = task.page_future
        page_still_running = False
        if page_future is not None:
            page_still_running = not page_future.cancel()
        self._tree_build_task = None
        # Admission rolls a saturated epoch only while one of three bounded
        # retirement slots remains. This lets a replacement page bypass stale
        # native reads without leaking one daemon thread per navigation.
        if rollover_if_running and page_still_running:
            self.tree_read_executor.rollover(cancel_futures=True)

    @staticmethod
    def _read_tree_page_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        generation: int,
        offset: int,
        cancel_event: Event,
    ) -> TreePageResult:
        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            directories = catalog.list_known_directories_with_prefix_page(
                "",
                limit=TREE_BUILD_BATCH_SIZE,
                offset=offset,
                cancel_check=check_canceled,
            )
            if offset == 0:
                tag_rows = catalog._conn.execute(  # noqa: SLF001 - bounded read-only worker query
                    """
                    SELECT name
                    FROM tags
                    ORDER BY name COLLATE NOCASE, name
                    LIMIT ?
                    """,
                    (MAX_TREE_TAG_ITEMS + 1,),
                )
                tag_page = tuple(str(row["name"]) for row in tag_rows)
                tags_have_more = len(tag_page) > MAX_TREE_TAG_ITEMS
                tags = tag_page[:MAX_TREE_TAG_ITEMS]
            else:
                tags = None
                tags_have_more = False
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        check_canceled()
        return TreePageResult(
            root=root,
            generation=generation,
            offset=offset,
            directories=directories,
            tags=tags,
            tags_have_more=tags_have_more,
        )

    def _submit_tree_page(self, task: TreeBuildTask) -> None:
        cancel_event = Event()
        try:
            future = self.tree_read_executor.submit(
                self._read_tree_page_worker,
                task.catalog.root,
                task.catalog.root_identity,
                task.catalog.storage_identity,
                task.generation,
                task.page_offset,
                cancel_event,
            )
        except RuntimeError as error:
            self._finish_tree_build_error(task, error)
            return
        task.page_cancel_event = cancel_event
        task.page_future = future
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText(
            f"Loading folder tree: {task.processed} folders ready"
        )
        QTimer.singleShot(
            TREE_PAGE_POLL_INTERVAL_MS,
            lambda generation=task.generation: self._continue_incremental_tree_rebuild(
                generation
            ),
        )

    def _finish_tree_build_error(
        self,
        task: TreeBuildTask,
        error: BaseException,
    ) -> None:
        if self._tree_build_task is not task:
            return
        self._tree_build_task = None
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Ready")
        if self.current_catalog is task.catalog and not isinstance(error, IndexTaskCancelled):
            show_error(self, "Folder Tree", str(error))
        self._start_next_pending_tree_rebuild()

    def _continue_incremental_tree_rebuild(self, generation: int | None = None) -> None:
        if self._closing:
            self._cancel_active_tree_build()
            self._pending_tree_rebuilds.clear()
            return
        if self._tree_publication_blocked():
            self._tree_rebuild_deferred = True
            return
        task = self._tree_build_task
        if task is None:
            return
        if generation is not None and task.generation != generation:
            return
        if task.generation != self._tree_build_generation:
            return
        page_future = task.page_future
        if page_future is not None:
            if not page_future.done():
                self.progress_bar.setRange(0, 0)
                self.progress_label.setText(
                    f"Loading folder tree: {task.processed} folders ready"
                )
                QTimer.singleShot(
                    TREE_PAGE_POLL_INTERVAL_MS,
                    lambda generation=task.generation: self._continue_incremental_tree_rebuild(
                        generation
                    ),
                )
                return
            task.page_future = None
            task.page_cancel_event = None
            if page_future.cancelled():
                self._finish_tree_build_error(task, IndexTaskCancelled())
                return
            try:
                page_result = page_future.result()
            except Exception as error:
                self._finish_tree_build_error(task, error)
                return
            if (
                self._tree_build_task is not task
                or page_result.generation != task.generation
                or page_result.root != task.catalog.root
                or page_result.offset != task.page_offset
            ):
                return
            page = page_result.directories
            task.page_offset += len(page)
            task.directories = [dir_rel for dir_rel in page if dir_rel]
            task.index = 0
            if page_result.tags is not None:
                task.tags = page_result.tags
                self._tree_tag_cache[task.catalog.root] = page_result.tags
                self._tree_tag_next_offsets[task.catalog.root] = (
                    len(page_result.tags) if page_result.tags_have_more else None
                )
            if len(page) < TREE_BUILD_BATCH_SIZE:
                task.total = task.processed + len(task.directories)
            if not page:
                task.total = task.processed
        deadline = monotonic() + TREE_BUILD_BUDGET_SECONDS
        processed_this_turn = 0
        item_work_this_turn = 0
        while task.total is None or task.processed < task.total:
            if len(task.rebuilt_item_by_dir) >= MAX_AUTOMATIC_TREE_ITEMS:
                # The tree is a navigation aid, not a second in-memory catalog.
                # Expanded branches and explicit path selection fill in later
                # rows on demand without an unbounded stream of Qt objects.
                task.total = task.processed
                break
            if task.index >= len(task.directories):
                self._submit_tree_page(task)
                return
            dir_rel = task.directories[task.index]
            task.index += 1
            task.processed += 1
            parent_rel = dir_rel.rpartition("/")[0]
            parent_item = task.item_by_dir.get(parent_rel)
            if parent_item is not None:
                item = task.item_by_dir.get(dir_rel)
                if item is None:
                    item = QTreeWidgetItem([dir_rel.rpartition("/")[2]])
                    item.setIcon(0, self.folder_icon)
                    item.setToolTip(0, str(task.catalog.root / dir_rel))
                    item.setData(0, CATALOG_ROOT_ROLE, str(task.catalog.root))
                    item.setData(0, DIR_REL_ROLE, dir_rel)
                    item.setChildIndicatorPolicy(
                        QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                    )
                    self._insert_tree_directory_item(parent_item, item, dir_rel)
                    task.item_by_dir[dir_rel] = item
                    item_work_this_turn += 1
                    if self._tree_state_key_for_directory(task.catalog.root, dir_rel) in task.expanded_items:
                        item.setExpanded(True)
                else:
                    self._insert_tree_directory_item(parent_item, item, dir_rel)
                task.seen_directories.add(dir_rel)
                task.rebuilt_item_by_dir[dir_rel] = item
                if self._is_current_tree_item(task.catalog.root, dir_rel):
                    task.selected_item = item
            else:
                # Defensive fallback for inventories that omit an ancestor.
                # Ordinarily every row attaches directly to its already-known
                # parent, avoiding repeated full path walks for deep trees.
                current_parent = ""
                completed_path = True
                for part in Path(dir_rel).parts:
                    if (
                        item_work_this_turn >= TREE_BUILD_BATCH_SIZE
                        or monotonic() >= deadline
                    ):
                        completed_path = False
                        break
                    current_rel = f"{current_parent}/{part}" if current_parent else part
                    item = task.item_by_dir.get(current_rel)
                    if item is None:
                        fallback_parent = task.item_by_dir.get(current_parent)
                        if fallback_parent is None:
                            completed_path = False
                            break
                        item = QTreeWidgetItem([part])
                        item.setIcon(0, self.folder_icon)
                        item.setToolTip(0, str(task.catalog.root / current_rel))
                        item.setData(0, CATALOG_ROOT_ROLE, str(task.catalog.root))
                        item.setData(0, DIR_REL_ROLE, current_rel)
                        item.setChildIndicatorPolicy(
                            QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                        )
                        self._insert_tree_directory_item(
                            fallback_parent,
                            item,
                            current_rel,
                        )
                        task.item_by_dir[current_rel] = item
                        item_work_this_turn += 1
                    else:
                        fallback_parent = task.item_by_dir.get(current_parent)
                        if fallback_parent is not None:
                            self._insert_tree_directory_item(
                                fallback_parent,
                                item,
                                current_rel,
                            )
                    task.seen_directories.add(current_rel)
                    task.rebuilt_item_by_dir[current_rel] = item
                    current_parent = current_rel
                if not completed_path:
                    task.index -= 1
                    task.processed -= 1
                    break
            processed_this_turn += 1
            if (
                processed_this_turn >= TREE_BUILD_BATCH_SIZE
                or item_work_this_turn >= TREE_BUILD_BATCH_SIZE
                or monotonic() >= deadline
            ):
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
        if task.cleanup_iterator is None:
            task.cleanup_iterator = iter(task.item_by_dir.items())
        cleanup_complete = False
        cleaned_this_turn = 0
        while cleaned_this_turn < TREE_BUILD_BATCH_SIZE and monotonic() < deadline:
            try:
                dir_rel, item = next(task.cleanup_iterator)  # type: ignore[arg-type]
            except StopIteration:
                cleanup_complete = True
                break
            cleaned_this_turn += 1
            if not dir_rel or dir_rel in task.seen_directories:
                continue
            parent_rel = dir_rel.rpartition("/")[0]
            # Removing the highest stale ancestor detaches its whole subtree;
            # descendants do not need individual Qt operations.
            if parent_rel and parent_rel not in task.seen_directories:
                continue
            parent_item = item.parent()
            if parent_item is not None:
                parent_item.removeChild(item)
        if not cleanup_complete:
            self.progress_label.setText(f"Updating folder tree: {task.processed} folders")
            QTimer.singleShot(0, self._continue_incremental_tree_rebuild)
            return
        task.item_by_dir = task.rebuilt_item_by_dir
        self._tree_item_maps[task.catalog.root] = task.rebuilt_item_by_dir
        root_item = task.item_by_dir[""]
        for child_index in range(root_item.childCount() - 1, -1, -1):
            child = root_item.child(child_index)
            if child.data(0, VIRTUAL_KIND_ROLE):
                root_item.takeChild(child_index)
        virtual_selected_item = self._add_virtual_tree_items(
            task.catalog,
            root_item,
            task.expanded_items,
            task.known_items,
            tags=task.tags,
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
        self._append_timing_event(
            task.catalog.root,
            "incremental_tree_rebuild_complete",
            (monotonic() - task.started_at) * 1000,
            {"reason": task.reason, "directories": task.processed},
        )
        self._tree_build_task = None
        self._start_next_pending_tree_rebuild()
        if self.current_catalog is task.catalog:
            reconcile_rels = {""}
            reconcile_rels.update(
                key[2]
                for key in task.expanded_items
                if key[0] == task.catalog.root
                and key[1] == "dir"
                and key[2] in task.item_by_dir
            )
            if self.current_virtual_kind is None and self.current_dir_rel:
                reconcile_rels.add(self.current_dir_rel.rpartition("/")[0])
            for dir_rel in reconcile_rels:
                self._tree_child_refresh_pending.add((task.catalog.root, dir_rel))
            QTimer.singleShot(0, self._submit_pending_tree_child_refresh)

    def _start_next_pending_tree_rebuild(self) -> None:
        if self._closing:
            self._pending_tree_rebuilds.clear()
            return
        while self._pending_tree_rebuilds:
            current_root = self.current_catalog.root if self.current_catalog is not None else None
            if current_root is not None and current_root in self._pending_tree_rebuilds:
                root = current_root
                _, reason = self._pending_tree_rebuilds.pop(root)
            else:
                root, (_, reason) = self._pending_tree_rebuilds.popitem()
            catalog = self.workspace.catalog_for_root(root)
            if catalog is None:
                continue
            self._start_incremental_tree_rebuild(catalog, reason=reason)
            return

    def _tree_item_for_root(self, root: Path) -> QTreeWidgetItem | None:
        resolved = root.expanduser().absolute()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if Path(item.data(0, CATALOG_ROOT_ROLE)).expanduser().absolute() == resolved:
                return item
        return None

    def _cancel_tree_path_selection(self) -> None:
        self._tree_path_selection_generation += 1
        self._tree_path_selection_task = None

    def _ensure_current_tree_path(self, catalog: Catalog, dir_rel: str) -> None:
        """Select a path without clearing or materializing deep paths in one turn."""

        self._cancel_tree_path_selection()

        root_item = self._tree_item_for_root(catalog.root)
        item_by_dir = self._tree_item_maps.get(catalog.root)
        if root_item is None:
            root_item = QTreeWidgetItem([catalog.root.name or str(catalog.root)])
            root_item.setIcon(0, self.folder_icon)
            root_item.setToolTip(0, str(catalog.root))
            root_item.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
            root_item.setData(0, DIR_REL_ROLE, "")
            root_item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
            self.tree.addTopLevelItem(root_item)
            item_by_dir = {"": root_item}
            self._tree_item_maps[catalog.root] = item_by_dir
            self._add_virtual_tree_items(
                catalog,
                root_item,
                self._expanded_tree_items(),
                self._known_tree_items(),
                tags=self._tree_tag_cache.get(catalog.root, ()),
            )
        elif item_by_dir is None:
            item_by_dir = {"": root_item}
            self._tree_item_maps[catalog.root] = item_by_dir
        root_item.setExpanded(True)
        generation = self._tree_path_selection_generation
        self._tree_path_selection_task = TreePathSelectionTask(
            catalog=catalog,
            dir_rel=dir_rel,
            parts=tuple(Path(dir_rel).parts),
            item_by_dir=item_by_dir,
            current_item=root_item,
            current_rel="",
            index=0,
            generation=generation,
        )
        self._continue_tree_path_selection(generation)

    def _continue_tree_path_selection(self, generation: int) -> None:
        task = self._tree_path_selection_task
        if task is None or task.generation != generation:
            return
        if self._closing:
            self._cancel_tree_path_selection()
            return
        if self._tree_publication_blocked():
            QTimer.singleShot(
                TREE_PAGE_POLL_INTERVAL_MS,
                lambda generation=generation: self._continue_tree_path_selection(generation),
            )
            return
        if (
            self.workspace.catalog_for_root(task.catalog.root) is not task.catalog
            or self.current_catalog is not task.catalog
            or self.current_virtual_kind is not None
            or self.current_dir_rel != task.dir_rel
        ):
            self._cancel_tree_path_selection()
            return

        current_map = self._tree_item_maps.get(task.catalog.root)
        if current_map is None or current_map.get("") is None:
            self._cancel_tree_path_selection()
            return
        task.item_by_dir = current_map
        mapped_current = current_map.get(task.current_rel)
        if mapped_current is not None:
            task.current_item = mapped_current
        deadline = monotonic() + TREE_BUILD_BUDGET_SECONDS
        work_this_turn = 0
        while task.index < len(task.parts):
            part = task.parts[task.index]
            next_rel = f"{task.current_rel}/{part}" if task.current_rel else part
            child = current_map.get(next_rel)
            if child is None:
                child = QTreeWidgetItem([part])
                child.setIcon(0, self.folder_icon)
                child.setToolTip(0, str(task.catalog.root / next_rel))
                child.setData(0, CATALOG_ROOT_ROLE, str(task.catalog.root))
                child.setData(0, DIR_REL_ROLE, next_rel)
                child.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )
                self._insert_tree_directory_item(task.current_item, child, next_rel)
                current_map[next_rel] = child
            else:
                self._insert_tree_directory_item(task.current_item, child, next_rel)
            task.current_item.setExpanded(True)
            task.current_item = child
            task.current_rel = next_rel
            task.index += 1
            work_this_turn += 1
            active_build = self._tree_build_task
            if active_build is not None and active_build.catalog is task.catalog:
                active_build.seen_directories.add(next_rel)
                active_build.rebuilt_item_by_dir[next_rel] = child
            if (
                work_this_turn >= TREE_BUILD_BATCH_SIZE
                or monotonic() >= deadline
            ):
                break

        if task.index < len(task.parts):
            QTimer.singleShot(
                0,
                lambda generation=generation: self._continue_tree_path_selection(generation),
            )
            return
        self.tree.setCurrentItem(task.current_item)
        self.tree.scrollToItem(task.current_item)
        self._tree_path_selection_task = None

    def _add_virtual_tree_items(
        self,
        catalog: Catalog,
        root_item: QTreeWidgetItem,
        expanded_items: set[TreeStateKey],
        known_items: set[TreeStateKey],
        *,
        tags: Sequence[str],
    ) -> QTreeWidgetItem | None:
        selected_item: QTreeWidgetItem | None = None
        tag_names = tuple(dict.fromkeys(tags))

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
        for tag in tag_names:
            item = QTreeWidgetItem([tag])
            self._set_virtual_tree_item_data(item, catalog, VIRTUAL_KIND_TAG, tag)
            tags_root.addChild(item)
            if self._is_current_virtual_item(catalog.root, VIRTUAL_KIND_TAG, tag):
                selected_item = item
        tag_next_offset = self._tree_tag_next_offsets.get(catalog.root)
        if tag_next_offset is not None:
            self._add_tree_load_more_tags_item(
                tags_root,
                catalog,
                tag_next_offset,
            )

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
        self._tree_known_state.update(
            {
                self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_ROOT, ""),
                self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_TAG_ROOT, ""),
                self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_DUPLICATES, ""),
                self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_VERY_SIMILAR, ""),
                *(
                    self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_TAG, tag)
                    for tag in tag_names
                ),
            }
        )
        return selected_item

    def _tree_state_key_for_directory(self, root: Path, dir_rel: str) -> TreeStateKey:
        return (root.expanduser().absolute(), "dir", dir_rel, "")

    def _tree_state_key_for_virtual(self, root: Path, kind: str, value: str) -> TreeStateKey:
        return (root.expanduser().absolute(), "virtual", kind, value)

    def _tree_item_state_key(self, item: QTreeWidgetItem) -> TreeStateKey:
        root = Path(item.data(0, CATALOG_ROOT_ROLE)).expanduser().absolute()
        virtual_kind = item.data(0, VIRTUAL_KIND_ROLE) or ""
        if virtual_kind:
            return self._tree_state_key_for_virtual(root, virtual_kind, item.data(0, VIRTUAL_VALUE_ROLE) or "")
        return self._tree_state_key_for_directory(root, item.data(0, DIR_REL_ROLE) or "")

    @staticmethod
    def _read_tree_children_page_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        parent_dir_rel: str,
        offset: int,
        cancel_event: Event,
    ) -> TreeChildrenPageResult:
        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            catalog._conn.execute("BEGIN")  # noqa: SLF001 - count/page snapshot
            try:
                total = catalog.known_child_directory_count(
                    parent_dir_rel,
                    cancel_check=check_canceled,
                )
                directories = catalog.list_known_child_directories_page(
                    parent_dir_rel,
                    limit=TREE_CHILD_PAGE_SIZE,
                    offset=offset,
                    cancel_check=check_canceled,
                )
            finally:
                with suppress(sqlite3.Error):
                    catalog._conn.execute("ROLLBACK")  # noqa: SLF001
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        check_canceled()
        next_offset = offset + len(directories)
        return TreeChildrenPageResult(
            root=root,
            parent_dir_rel=parent_dir_rel,
            offset=offset,
            directories=directories,
            next_offset=next_offset,
            has_more=next_offset < total,
        )

    @staticmethod
    def _remove_tree_load_more_items(item: QTreeWidgetItem) -> None:
        for child_index in range(item.childCount() - 1, -1, -1):
            child = item.child(child_index)
            if child.data(0, TREE_LOAD_MORE_ROLE) is not None:
                item.takeChild(child_index)

    def _add_tree_load_more_item(
        self,
        item: QTreeWidgetItem,
        catalog: Catalog,
        parent_dir_rel: str,
        offset: int,
        *,
        retry: bool = False,
    ) -> None:
        self._remove_tree_load_more_items(item)
        sentinel = QTreeWidgetItem(
            ["Retry loading folders…" if retry else "Load more folders…"]
        )
        sentinel.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
        sentinel.setData(0, DIR_REL_ROLE, parent_dir_rel)
        sentinel.setData(0, TREE_LOAD_MORE_ROLE, max(0, int(offset)))
        sentinel.setToolTip(0, "Load the next bounded page of subfolders")
        item.addChild(sentinel)

    def _request_tree_children_for_directory(
        self,
        root: Path,
        dir_rel: str,
        *,
        offset: int | None = None,
    ) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        item = self._tree_item_maps.get(catalog.root, {}).get(dir_rel)
        if item is not None:
            self._request_tree_children(item, offset=offset)

    def _request_tree_children(
        self,
        item: QTreeWidgetItem,
        *,
        offset: int | None = None,
    ) -> None:
        if self._closing or self.is_virtual_tree_item(item):
            return
        root_value = item.data(0, CATALOG_ROOT_ROLE)
        if not root_value:
            return
        root = Path(root_value).expanduser().absolute()
        parent_dir_rel = item.data(0, DIR_REL_ROLE) or ""
        if self._tree_publication_blocked():
            QTimer.singleShot(
                TREE_PAGE_POLL_INTERVAL_MS,
                partial(
                    self._request_tree_children_for_directory,
                    root,
                    parent_dir_rel,
                    offset=offset,
                ),
            )
            return
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        item_by_dir = self._tree_item_maps.get(catalog.root)
        if item_by_dir is None or item_by_dir.get(parent_dir_rel) is not item:
            return
        key = (catalog.root, parent_dir_rel)
        if offset is None:
            if key in self._tree_child_next_offsets:
                offset = self._tree_child_next_offsets[key]
                if offset is None:
                    return
            else:
                offset = 0
        offset = max(0, int(offset))
        if any(
            task.catalog is catalog and task.parent_dir_rel == parent_dir_rel
            for task in self._tree_children_tasks.values()
        ):
            if offset == 0:
                self._tree_child_refresh_pending.add(key)
            return
        self._remove_tree_load_more_items(item)
        cancel_event = Event()
        try:
            future = self.tree_read_executor.submit(
                self._read_tree_children_page_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                parent_dir_rel,
                offset,
                cancel_event,
            )
        except RuntimeError:
            self._add_tree_load_more_item(
                item,
                catalog,
                parent_dir_rel,
                offset,
                retry=True,
            )
            return
        self._tree_children_tasks[future] = TreeChildrenTask(
            catalog=catalog,
            parent_dir_rel=parent_dir_rel,
            offset=offset,
            future=future,
            cancel_event=cancel_event,
        )

    def _settle_tree_children_tasks(self) -> None:
        if self._tree_publication_blocked():
            return
        for future, task in list(self._tree_children_tasks.items()):
            if not future.done():
                continue
            self._tree_children_tasks.pop(future, None)
            catalog = task.catalog
            item = self._tree_item_maps.get(catalog.root, {}).get(task.parent_dir_rel)
            if future.cancelled() or self._closing:
                continue
            try:
                result = future.result()
            except Exception as error:
                if (
                    item is not None
                    and self.workspace.catalog_for_root(catalog.root) is catalog
                    and not isinstance(error, IndexTaskCancelled)
                ):
                    self._add_tree_load_more_item(
                        item,
                        catalog,
                        task.parent_dir_rel,
                        task.offset,
                        retry=True,
                    )
                continue
            if (
                result.root != catalog.root
                or result.parent_dir_rel != task.parent_dir_rel
                or result.offset != task.offset
                or self.workspace.catalog_for_root(catalog.root) is not catalog
                or item is None
            ):
                continue
            item_by_dir = self._tree_item_maps.get(catalog.root)
            if item_by_dir is None or item_by_dir.get(task.parent_dir_rel) is not item:
                continue
            self._remove_tree_load_more_items(item)
            if task.offset == 0:
                returned = set(result.directories)
                direct_loaded = {
                    dir_rel
                    for dir_rel in item_by_dir
                    if dir_rel and dir_rel.rpartition("/")[0] == task.parent_dir_rel
                }
                if result.directories:
                    boundary = result.directories[-1]
                    boundary_key = (boundary.casefold(), boundary)
                    candidates = {
                        dir_rel
                        for dir_rel in direct_loaded
                        if (dir_rel.casefold(), dir_rel) <= boundary_key
                    }
                elif not result.has_more:
                    candidates = direct_loaded
                else:
                    candidates = set()
                for stale_rel in candidates - returned:
                    stale_item = item_by_dir.get(stale_rel)
                    if stale_item is not None and stale_item.parent() is item:
                        item.removeChild(stale_item)
                    stale_prefix = f"{stale_rel}/"
                    for mapped_rel in [
                        mapped_rel
                        for mapped_rel in item_by_dir
                        if mapped_rel == stale_rel or mapped_rel.startswith(stale_prefix)
                    ]:
                        item_by_dir.pop(mapped_rel, None)
                        active_build = self._tree_build_task
                        if active_build is not None and active_build.catalog is catalog:
                            active_build.seen_directories.discard(mapped_rel)
                            active_build.rebuilt_item_by_dir.pop(mapped_rel, None)
            for dir_rel in result.directories:
                if dir_rel.rpartition("/")[0] != task.parent_dir_rel:
                    continue
                child = item_by_dir.get(dir_rel)
                if child is None:
                    child = QTreeWidgetItem([dir_rel.rpartition("/")[2]])
                    child.setIcon(0, self.folder_icon)
                    child.setToolTip(0, str(catalog.root / dir_rel))
                    child.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
                    child.setData(0, DIR_REL_ROLE, dir_rel)
                    child.setChildIndicatorPolicy(
                        QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                    )
                    self._insert_tree_directory_item(item, child, dir_rel)
                    item_by_dir[dir_rel] = child
                else:
                    self._insert_tree_directory_item(item, child, dir_rel)
                active_build = self._tree_build_task
                if active_build is not None and active_build.catalog is catalog:
                    active_build.seen_directories.add(dir_rel)
                    active_build.rebuilt_item_by_dir[dir_rel] = child
                if (
                    self._tree_state_key_for_directory(catalog.root, dir_rel)
                    in self._tree_expanded_state
                ):
                    child.setExpanded(True)
            next_offset = result.next_offset if result.has_more else None
            self._tree_child_next_offsets[(catalog.root, task.parent_dir_rel)] = next_offset
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
            )
            if next_offset is not None:
                self._add_tree_load_more_item(
                    item,
                    catalog,
                    task.parent_dir_rel,
                    next_offset,
                )
        self._submit_pending_tree_child_refresh()

    def _submit_pending_tree_child_refresh(self) -> None:
        """Submit one deferred offset-zero child reconciliation.

        The tree reader admits only one page at a time. Serializing pending
        refreshes here retains that bound and, importantly, lets a discovery
        refresh survive an older page for the same parent instead of treating
        the stale page as the final inventory.
        """

        if (
            self._closing
            or self._tree_publication_blocked()
            or self._tree_build_task is not None
            or self._tree_children_tasks
        ):
            return
        while self._tree_child_refresh_pending:
            root, dir_rel = min(
                self._tree_child_refresh_pending,
                key=lambda value: (
                    0
                    if self.current_catalog is not None
                    and value[0] == self.current_catalog.root
                    else 1,
                    value[1].count("/"),
                    value[1].casefold(),
                    value[1],
                ),
            )
            catalog = self.workspace.catalog_for_exact_root(root)
            item = self._tree_item_maps.get(root, {}).get(dir_rel)
            if catalog is None or item is None:
                self._tree_child_refresh_pending.discard((root, dir_rel))
                continue
            self._request_tree_children(item, offset=0)
            if any(
                task.catalog is catalog and task.parent_dir_rel == dir_rel
                for task in self._tree_children_tasks.values()
            ):
                self._tree_child_refresh_pending.discard((root, dir_rel))
            return

    @staticmethod
    def _read_tree_tags_page_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        offset: int,
        cancel_event: Event,
    ) -> TreeTagsPageResult:
        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            with catalog._sqlite_cancel_progress(check_canceled):  # noqa: SLF001
                rows = catalog._conn.execute(  # noqa: SLF001 - bounded read-only page
                    """
                    SELECT name
                    FROM tags
                    ORDER BY name COLLATE NOCASE, name
                    LIMIT ? OFFSET ?
                    """,
                    (MAX_TREE_TAG_ITEMS + 1, max(0, offset)),
                )
                page = tuple(str(row["name"]) for row in rows)
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        check_canceled()
        has_more = len(page) > MAX_TREE_TAG_ITEMS
        tags = page[:MAX_TREE_TAG_ITEMS]
        return TreeTagsPageResult(
            root=root,
            offset=offset,
            tags=tags,
            next_offset=offset + len(tags),
            has_more=has_more,
        )

    @staticmethod
    def _remove_tree_load_more_tags_items(tags_root: QTreeWidgetItem) -> None:
        for child_index in range(tags_root.childCount() - 1, -1, -1):
            child = tags_root.child(child_index)
            if child.data(0, TREE_LOAD_MORE_TAGS_ROLE) is not None:
                tags_root.takeChild(child_index)

    def _add_tree_load_more_tags_item(
        self,
        tags_root: QTreeWidgetItem,
        catalog: Catalog,
        offset: int,
        *,
        retry: bool = False,
    ) -> None:
        self._remove_tree_load_more_tags_items(tags_root)
        sentinel = QTreeWidgetItem(
            ["Retry loading tags…" if retry else "Load more tags…"]
        )
        sentinel.setData(0, CATALOG_ROOT_ROLE, str(catalog.root))
        sentinel.setData(0, TREE_LOAD_MORE_TAGS_ROLE, max(0, int(offset)))
        sentinel.setToolTip(0, "Load the next bounded page of catalog tags")
        tags_root.addChild(sentinel)

    def _tag_tree_root_for_catalog(self, catalog: Catalog) -> QTreeWidgetItem | None:
        root_item = self._tree_item_for_root(catalog.root)
        if root_item is None:
            return None
        for child_index in range(root_item.childCount()):
            virtual_root = root_item.child(child_index)
            if virtual_root.data(0, VIRTUAL_KIND_ROLE) != VIRTUAL_KIND_ROOT:
                continue
            for virtual_index in range(virtual_root.childCount()):
                item = virtual_root.child(virtual_index)
                if item.data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_TAG_ROOT:
                    return item
        return None

    def _request_tree_tags_for_catalog(
        self,
        root: Path,
        *,
        offset: int | None = None,
    ) -> None:
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        tags_root = self._tag_tree_root_for_catalog(catalog)
        if tags_root is not None:
            self._request_tree_tags(tags_root, offset=offset)

    def _request_tree_tags(
        self,
        tags_root: QTreeWidgetItem,
        *,
        offset: int | None = None,
    ) -> None:
        if self._closing or tags_root.data(0, VIRTUAL_KIND_ROLE) != VIRTUAL_KIND_TAG_ROOT:
            return
        root_value = tags_root.data(0, CATALOG_ROOT_ROLE)
        if not root_value:
            return
        root = Path(root_value)
        if self._tree_publication_blocked():
            QTimer.singleShot(
                TREE_PAGE_POLL_INTERVAL_MS,
                partial(
                    self._request_tree_tags_for_catalog,
                    root,
                    offset=offset,
                ),
            )
            return
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None or self._tag_tree_root_for_catalog(catalog) is not tags_root:
            return
        if offset is None:
            offset = self._tree_tag_next_offsets.get(catalog.root)
            if offset is None:
                return
        offset = max(0, int(offset))
        if any(task.catalog is catalog for task in self._tree_tags_tasks.values()):
            return
        self._remove_tree_load_more_tags_items(tags_root)
        cancel_event = Event()
        try:
            future = self.tree_read_executor.submit(
                self._read_tree_tags_page_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                offset,
                cancel_event,
            )
        except RuntimeError:
            self._add_tree_load_more_tags_item(
                tags_root,
                catalog,
                offset,
                retry=True,
            )
            return
        self._tree_tags_tasks[future] = TreeTagsTask(
            catalog=catalog,
            offset=offset,
            future=future,
            cancel_event=cancel_event,
        )

    def _settle_tree_tags_tasks(self) -> None:
        if self._tree_publication_blocked():
            return
        for future, task in list(self._tree_tags_tasks.items()):
            if not future.done():
                continue
            self._tree_tags_tasks.pop(future, None)
            catalog = task.catalog
            tags_root = self._tag_tree_root_for_catalog(catalog)
            if future.cancelled() or self._closing:
                continue
            try:
                result = future.result()
            except Exception as error:
                if (
                    tags_root is not None
                    and self.workspace.catalog_for_root(catalog.root) is catalog
                    and not isinstance(error, IndexTaskCancelled)
                ):
                    self._add_tree_load_more_tags_item(
                        tags_root,
                        catalog,
                        task.offset,
                        retry=True,
                    )
                continue
            if (
                result.root != catalog.root
                or result.offset != task.offset
                or self.workspace.catalog_for_root(catalog.root) is not catalog
                or tags_root is None
            ):
                continue
            self._remove_tree_load_more_tags_items(tags_root)
            known = {
                normalize_tag(tags_root.child(index).data(0, VIRTUAL_VALUE_ROLE) or "")
                for index in range(tags_root.childCount())
                if tags_root.child(index).data(0, VIRTUAL_KIND_ROLE) == VIRTUAL_KIND_TAG
            }
            selected_item: QTreeWidgetItem | None = None
            for tag in result.tags:
                normalized = normalize_tag(tag)
                if not normalized or normalized in known:
                    continue
                item = QTreeWidgetItem([tag])
                self._set_virtual_tree_item_data(item, catalog, VIRTUAL_KIND_TAG, tag)
                tags_root.addChild(item)
                known.add(normalized)
                self._tree_known_state.add(
                    self._tree_state_key_for_virtual(catalog.root, VIRTUAL_KIND_TAG, tag)
                )
                if self._is_current_virtual_item(catalog.root, VIRTUAL_KIND_TAG, tag):
                    selected_item = item
            next_offset = result.next_offset if result.has_more else None
            self._tree_tag_next_offsets[catalog.root] = next_offset
            if next_offset is not None:
                self._add_tree_load_more_tags_item(
                    tags_root,
                    catalog,
                    next_offset,
                )
            if selected_item is not None:
                self.tree.setCurrentItem(selected_item)
                self.tree.scrollToItem(selected_item)

    def _tree_item_expanded(self, item: QTreeWidgetItem) -> None:
        if (
            item.data(0, TREE_LOAD_MORE_ROLE) is not None
            or item.data(0, TREE_LOAD_MORE_TAGS_ROLE) is not None
        ):
            return
        key = self._tree_item_state_key(item)
        self._tree_known_state.add(key)
        self._tree_expanded_state.add(key)
        self._request_tree_children(item)

    def _tree_item_collapsed(self, item: QTreeWidgetItem) -> None:
        if (
            item.data(0, TREE_LOAD_MORE_ROLE) is not None
            or item.data(0, TREE_LOAD_MORE_TAGS_ROLE) is not None
        ):
            return
        key = self._tree_item_state_key(item)
        self._tree_known_state.add(key)
        self._tree_expanded_state.discard(key)

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
        """Keep initial construction constant-time; selection fills paths incrementally."""

        del catalog
        return []

    def _expanded_tree_items(self) -> set[TreeStateKey]:
        return set(self._tree_expanded_state)

    def _known_tree_items(self) -> set[TreeStateKey]:
        return set(self._tree_known_state)

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
        # Whether a directory still exists is established by the pane worker.
        # A blocking stat here would freeze every refresh on an unavailable
        # mount. Mutation completion already selects a known parent when it
        # removes the directory currently being viewed.

    def _tree_scroll_position(self) -> tuple[int, int]:
        return (
            self.tree.verticalScrollBar().value(),
            self.tree.horizontalScrollBar().value(),
        )

    def _restore_tree_scroll_position(self, position: tuple[int, int]) -> None:
        if self._closing:
            return
        if self._tree_publication_blocked():
            QTimer.singleShot(
                TREE_PAGE_POLL_INTERVAL_MS,
                partial(self._restore_tree_scroll_position, position),
            )
            return
        vertical, horizontal = position
        vertical_bar = self.tree.verticalScrollBar()
        horizontal_bar = self.tree.horizontalScrollBar()
        vertical_bar.setValue(max(vertical_bar.minimum(), min(vertical, vertical_bar.maximum())))
        horizontal_bar.setValue(max(horizontal_bar.minimum(), min(horizontal, horizontal_bar.maximum())))

    def _directory_clicked(self, item: QTreeWidgetItem) -> None:
        load_more_tags_offset = item.data(0, TREE_LOAD_MORE_TAGS_ROLE)
        if load_more_tags_offset is not None:
            parent = item.parent()
            if parent is not None:
                self._request_tree_tags(parent, offset=int(load_more_tags_offset))
            return
        load_more_offset = item.data(0, TREE_LOAD_MORE_ROLE)
        if load_more_offset is not None:
            parent = item.parent()
            if parent is not None:
                self._request_tree_children(parent, offset=int(load_more_offset))
            return
        root = Path(item.data(0, CATALOG_ROOT_ROLE))
        catalog = self.workspace.catalog_for_root(root)
        if catalog is None:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        if self.is_virtual_tree_item(item):
            self._virtual_directory_clicked(catalog, item)
            return
        intent_sequence = self._record_catalog_intent(catalog.root)
        dir_rel = item.data(0, DIR_REL_ROLE)
        idle_task = self._idle_index_tasks.get(catalog.root)
        if idle_task is not None and not idle_task.snapshot().done:
            self._resume_idle_refresh_roots.add(catalog.root)
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root, keep_dir_rel=dir_rel)
        self.current_catalog = catalog
        self._current_catalog_intent_sequence = intent_sequence
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
        intent_sequence = self._record_catalog_intent(catalog.root)
        self.indexer.cancel_idle_tasks(catalog.root)
        self.indexer.cancel_directory_tasks(catalog.root)
        self.current_catalog = catalog
        self._current_catalog_intent_sequence = intent_sequence
        self.current_dir_rel = ""
        self.current_virtual_kind = kind
        self.current_virtual_value = value
        self.load_current_directory()

    def load_current_directory(self, *, preserve_selection: bool = False) -> None:
        # Any explicit load satisfies an earlier indexing-progress request.
        # New progress observed while this load runs will set a fresh trailing
        # request without canceling this generation.
        self._pending_physical_progress_reload = None
        self._physical_pane_generation += 1
        for future, stale_task in list(self._virtual_view_tasks.items()):
            if stale_task.fingerprint == self._physical_pane_generation:
                continue
            stale_task.cancel_event.set()
            future.cancel()
            self._virtual_view_tasks.pop(future, None)
        self._physical_reconcile_pending_generation = None
        pane_generation = self._physical_pane_generation
        pending_selection = self._pending_rel_path_selection
        if pending_selection is not None and pending_selection[2] != pane_generation:
            self._pending_rel_path_selection = None
        self._remember_thumbnail_scroll_position()
        self._remember_thumbnail_selection()
        target_scroll_key = self._current_thumbnail_scroll_key()
        if preserve_selection:
            selection_keys = self._thumbnail_selection_keys()
            current_key = self._current_thumbnail_selection_key()
        else:
            remembered = self._thumbnail_selections.get(target_scroll_key) if target_scroll_key is not None else None
            if remembered is not None and target_scroll_key is not None:
                self._thumbnail_selections.move_to_end(target_scroll_key)
            selection_keys, current_key = remembered if remembered is not None else (set(), None)
            preserve_selection = remembered is not None
        if self.current_catalog is None:
            self.model.set_images(None, [])
            self._thumbnail_scroll_key = None
            self.update_selection_status()
            return
        if self.workspace.catalog_for_root(self.current_catalog.root) is not self.current_catalog:
            self.current_catalog = None
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            self.model.set_images(None, [])
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
        self._load_large_physical_directory(
            preserve_selection=preserve_selection,
            selection_keys=selection_keys,
            current_key=current_key,
            scroll_key=target_scroll_key,
            pane_generation=pane_generation,
        )

    def _load_large_physical_directory(
        self,
        *,
        preserve_selection: bool,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
        pane_generation: int,
    ) -> None:
        catalog = self.current_catalog
        if catalog is None or self.current_virtual_kind is not None:
            return
        dir_rel = self.current_dir_rel
        # Never scan/stat the selected directory on the GUI thread. Retain
        # cheap in-memory tree placeholders while the complete worker result
        # is assembled with its own Catalog connection.
        reuse_existing_records = (
            self.model.catalog is catalog and self._thumbnail_scroll_key == scroll_key
        )
        if reuse_existing_records:
            initial_records: list[PaneRecord] = []
        else:
            initial_records = self._known_shallow_child_directories(catalog, dir_rel)
        fingerprint = pane_generation
        for future, stale_task in list(self._virtual_view_tasks.items()):
            if stale_task.root != catalog.root or stale_task.kind not in {
                VIRTUAL_KIND_PHYSICAL,
                VIRTUAL_KIND_PHYSICAL_PREVIEW,
            }:
                continue
            if stale_task.fingerprint == fingerprint:
                continue
            stale_task.cancel_event.set()
            future.cancel()
            # A canceled running future is already guarded by its generation
            # and cancel event; retaining it forever only leaks task metadata.
            self._virtual_view_tasks.pop(future, None)
        preview_task = self._matching_virtual_view_task(
            catalog.root,
            VIRTUAL_KIND_PHYSICAL_PREVIEW,
            dir_rel,
            self.current_sort,
            fingerprint,
        )
        if preview_task is None:
            preview_task = self._submit_physical_preview_task(
                catalog,
                dir_rel,
                self.current_sort,
                fingerprint,
                selection_keys,
                current_key,
                scroll_key,
                retry_attempt=0,
            )
        task = self._matching_virtual_view_task(
            catalog.root,
            VIRTUAL_KIND_PHYSICAL,
            dir_rel,
            self.current_sort,
            fingerprint,
        )
        admission_failed = False
        if task is None:
            cancel_event = Event()
            try:
                future = self.virtual_view_executor.submit(
                    self._physical_view_worker,
                    catalog.root,
                    catalog.root_identity,
                    catalog.storage_identity,
                    dir_rel,
                    self.current_sort.value,
                    fingerprint,
                    cancel_event,
                )
            except RuntimeError:
                task = None
                admission_failed = True
            else:
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
        if not reuse_existing_records:
            self.model.set_images(
                catalog,
                self._without_pending_delete_records(catalog.root, initial_records),
            )
            self.refresh_thumbnail_layout()
            self._restore_thumbnail_scroll_position(scroll_key)
            self.update_selection_status()
        if task is not None:
            self._show_virtual_view_status(task)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Catalog reader busy; retrying on the next reload")

    def _submit_physical_preview_task(
        self,
        catalog: Catalog,
        dir_rel: str,
        sort_order: SortOrder,
        fingerprint: int,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
        *,
        retry_attempt: int,
    ) -> VirtualViewTask | None:
        cancel_event = Event()
        excluded_image_rels, excluded_directory_rels = (
            self._pending_physical_exclusions(catalog.root)
        )
        try:
            future = self.physical_preview_executor.submit(
                self._quick_physical_view_worker,
                catalog.root,
                catalog.root_identity,
                dir_rel,
                sort_order.value,
                fingerprint,
                cancel_event,
                excluded_image_rels,
                excluded_directory_rels,
            )
        except RuntimeError:
            self._schedule_physical_preview_retry(
                catalog.root,
                dir_rel,
                sort_order,
                fingerprint,
                selection_keys,
                current_key,
                scroll_key,
                retry_attempt=retry_attempt + 1,
            )
            return None
        if self._physical_preview_retry_generation == fingerprint:
            self._physical_preview_retry_generation = None
        task = VirtualViewTask(
            root=catalog.root,
            kind=VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            future=future,
            started_at=monotonic(),
            selection_keys=set(selection_keys),
            current_key=current_key,
            scroll_key=scroll_key,
            cancel_event=cancel_event,
        )
        self._virtual_view_tasks[future] = task
        return task

    def _submit_filtered_physical_snapshot_task(
        self,
        catalog: Catalog,
        dir_rel: str,
        sort_order: SortOrder,
        fingerprint: int,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> VirtualViewTask | None:
        """Hide admitted mutations by filtering the owned snapshot off Qt."""

        cancel_event = Event()
        excluded_image_rels, excluded_directory_rels = (
            self._pending_physical_exclusions(catalog.root)
        )
        try:
            future = self.physical_preview_executor.submit(
                self._filtered_physical_snapshot_worker,
                catalog.root,
                dir_rel,
                sort_order.value,
                fingerprint,
                self.model.images,
                cancel_event,
                excluded_image_rels,
                excluded_directory_rels,
            )
        except RuntimeError:
            return self._submit_physical_preview_task(
                catalog,
                dir_rel,
                sort_order,
                fingerprint,
                selection_keys,
                current_key,
                scroll_key,
                retry_attempt=0,
            )
        task = VirtualViewTask(
            root=catalog.root,
            kind=VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            future=future,
            started_at=monotonic(),
            selection_keys=set(selection_keys),
            current_key=current_key,
            scroll_key=scroll_key,
            cancel_event=cancel_event,
        )
        self._virtual_view_tasks[future] = task
        return task

    @staticmethod
    def _filtered_physical_snapshot_worker(
        root: Path,
        dir_rel: str,
        sort_value: str,
        fingerprint: int,
        records: Sequence[PaneRecord],
        cancel_event: Event,
        excluded_image_rels: frozenset[str],
        excluded_directory_rels: frozenset[str],
    ) -> VirtualViewResult:
        started_at = monotonic()
        exclude_all = any(
            dir_rel == excluded or dir_rel.startswith(f"{excluded}/")
            for excluded in excluded_directory_rels
        )
        filtered: list[PaneRecord] = []
        row_by_key: dict[tuple[str, str], int] = {}
        image_order: list[str] = []
        order_hash = hashlib.sha256()
        for source_row, record in enumerate(records):
            if source_row % 256 == 0 and cancel_event.is_set():
                raise IndexTaskCancelled()
            is_directory = isinstance(record, DirectoryRecord)
            if exclude_all or (
                record.rel_path in (
                    excluded_directory_rels if is_directory else excluded_image_rels
                )
            ):
                continue
            row = len(filtered)
            filtered.append(record)
            row_by_key[
                ("directory" if is_directory else "image", record.rel_path)
            ] = row
            order_hash.update(b"d\0" if is_directory else b"i\0")
            order_hash.update(record.rel_path.encode("utf-8"))
            order_hash.update(b"\0")
            if not is_directory:
                image_order.append(record.rel_path)
        if cancel_event.is_set():
            raise IndexTaskCancelled()
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value=dir_rel,
            sort_order=SortOrder(sort_value),
            fingerprint=fingerprint,
            images=filtered,
            duration_ms=(monotonic() - started_at) * 1000,
            total_records=len(filtered),
            total_images=len(image_order),
            stable_row_by_key=row_by_key,
            stable_order_token=order_hash.hexdigest(),
            stable_image_order=image_order,
            excluded_image_rels=excluded_image_rels,
            excluded_directory_rels=excluded_directory_rels,
        )

    def _schedule_physical_preview_retry(
        self,
        root: Path,
        dir_rel: str,
        sort_order: SortOrder,
        fingerprint: int,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
        *,
        retry_attempt: int,
    ) -> None:
        if self._physical_preview_retry_generation == fingerprint:
            return
        self._physical_preview_retry_generation = fingerprint
        delay_ms = min(1000, 25 * (2 ** min(retry_attempt, 6)))
        QTimer.singleShot(
            delay_ms,
            partial(
                self._retry_physical_preview,
                root,
                dir_rel,
                sort_order,
                fingerprint,
                set(selection_keys),
                current_key,
                scroll_key,
                retry_attempt=retry_attempt,
            ),
        )

    def _retry_physical_preview(
        self,
        root: Path,
        dir_rel: str,
        sort_order: SortOrder,
        fingerprint: int,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
        *,
        retry_attempt: int,
    ) -> None:
        if self._physical_preview_retry_generation != fingerprint:
            return
        self._physical_preview_retry_generation = None
        catalog = self.current_catalog
        if (
            self._closing
            or catalog is None
            or catalog.root != root
            or self.current_virtual_kind is not None
            or self.current_dir_rel != dir_rel
            or self.current_sort != sort_order
            or self._physical_pane_generation != fingerprint
            or self._matching_virtual_view_task(
                root,
                VIRTUAL_KIND_PHYSICAL_PREVIEW,
                dir_rel,
                sort_order,
                fingerprint,
            )
            is not None
        ):
            return
        self._submit_physical_preview_task(
            catalog,
            dir_rel,
            sort_order,
            fingerprint,
            selection_keys,
            current_key,
            scroll_key,
            retry_attempt=retry_attempt,
        )

    def _request_physical_reconcile(self, root: Path, dir_rel: str) -> None:
        """Queue one metadata-complete, same-generation final row ordering."""

        catalog = self.current_catalog
        fingerprint = self._physical_pane_generation
        if (
            self._closing
            or catalog is None
            or catalog.root != root
            or self.current_virtual_kind is not None
            or self.current_dir_rel != dir_rel
        ):
            return
        if self._matching_virtual_view_task(
            root,
            VIRTUAL_KIND_PHYSICAL_PREVIEW,
            dir_rel,
            self.current_sort,
            fingerprint,
        ) is not None:
            self._physical_reconcile_pending_generation = fingerprint
            return
        self._physical_reconcile_pending_generation = None
        cancel_event = Event()
        excluded_image_rels, excluded_directory_rels = (
            self._pending_physical_exclusions(catalog.root)
        )
        try:
            future = self.physical_preview_executor.submit(
                self._reconciled_physical_view_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                dir_rel,
                self.current_sort.value,
                fingerprint,
                cancel_event,
                excluded_image_rels,
                excluded_directory_rels,
            )
        except RuntimeError:
            self._physical_reconcile_pending_generation = fingerprint
            QTimer.singleShot(
                100,
                partial(self._retry_physical_reconcile, root, dir_rel, fingerprint),
            )
            return
        self._virtual_view_tasks[future] = VirtualViewTask(
            root=root,
            kind=VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value=dir_rel,
            sort_order=self.current_sort,
            fingerprint=fingerprint,
            future=future,
            started_at=monotonic(),
            selection_keys=self._thumbnail_selection_keys(),
            current_key=self._current_thumbnail_selection_key(),
            scroll_key=self._current_thumbnail_scroll_key(),
            cancel_event=cancel_event,
            physical_reconcile=True,
        )

    def _retry_physical_reconcile(
        self,
        root: Path,
        dir_rel: str,
        fingerprint: int,
    ) -> None:
        if self._physical_reconcile_pending_generation != fingerprint:
            return
        if fingerprint != self._physical_pane_generation:
            self._physical_reconcile_pending_generation = None
            return
        self._request_physical_reconcile(root, dir_rel)

    @staticmethod
    def _quick_physical_view_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        dir_rel: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
        excluded_image_rels: frozenset[str] = frozenset(),
        excluded_directory_rels: frozenset[str] = frozenset(),
    ) -> VirtualViewResult:
        """Enumerate one complete, sorted filesystem row set off the GUI thread."""

        started_at = monotonic()
        sort_order = SortOrder(sort_value)
        directory_records: list[DirectoryRecord] = []
        image_records: list[ImageRecord] = []
        relative = Path(dir_rel)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            if dir_rel:
                raise ValueError(f"Invalid catalog directory: {dir_rel}")
        filesystem = Catalog.open_filesystem_handle(
            root,
            expected_root_identity=expected_root_identity,
        )
        dir_path = (
            filesystem.mutation_path(dir_rel)
            if dir_rel
            else filesystem.root
        )

        exclude_entire_directory = any(
            dir_rel == directory_rel or dir_rel.startswith(f"{directory_rel}/")
            for directory_rel in excluded_directory_rels
        )
        excluded_direct_children = {
            directory_rel
            for directory_rel in excluded_directory_rels
            if directory_rel.rpartition("/")[0] == dir_rel
        }

        def inside_excluded_directory(rel_path: str) -> bool:
            return exclude_entire_directory or rel_path in excluded_direct_children

        with filesystem._open_catalog_directory_fd(dir_path) as directory_fd:  # noqa: SLF001
            initial_directory_stat = (
                dir_path.stat(follow_symlinks=False)
                if directory_fd is None
                else None
            )
            with os.scandir(dir_path if directory_fd is None else directory_fd) as entries:
                for entry in entries:
                    if cancel_event.is_set():
                        raise IndexTaskCancelled()
                    try:
                        entry.name.encode("utf-8")
                    except UnicodeError:
                        continue
                    rel_path = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
                    try:
                        if (
                            not is_marnwick_internal_artifact_name(entry.name)
                            and not inside_excluded_directory(rel_path)
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            entry_stat = entry.stat(follow_symlinks=False)
                            directory_records.append(
                                DirectoryRecord(
                                    catalog_root=root,
                                    dir_rel=rel_path,
                                    name=entry.name,
                                    mtime_ns=entry_stat.st_mtime_ns,
                                    allow_preview_fallback=False,
                                )
                            )
                        elif (
                            is_image_name(entry.name)
                            and not is_marnwick_internal_artifact_name(entry.name)
                            and rel_path not in excluded_image_rels
                            and not inside_excluded_directory(rel_path)
                            and entry.is_file(follow_symlinks=False)
                        ):
                            entry_stat = entry.stat(follow_symlinks=False)
                            entry_change_ns = filesystem._path_change_time_ns(  # noqa: SLF001
                                dir_path / entry.name,
                                entry_stat,
                            )
                            image_records.append(
                                ImageRecord(
                                    id=-1,
                                    catalog_root=root,
                                    rel_path=rel_path,
                                    dir_rel=dir_rel,
                                    filename=entry.name,
                                    size_bytes=entry_stat.st_size,
                                    mtime_ns=entry_stat.st_mtime_ns,
                                    width=0,
                                    height=0,
                                    aspect_ratio=0.0,
                                    thumb_width=0,
                                    thumb_height=0,
                                    ctime_ns=entry_change_ns,
                                    file_identity=(
                                        int(entry_stat.st_dev),
                                        int(entry_stat.st_ino),
                                        int(entry_stat.st_nlink),
                                        int(entry_stat.st_size),
                                        int(entry_stat.st_mtime_ns),
                                        entry_change_ns,
                                    ),
                                )
                            )
                    except OSError:
                        continue
            if directory_fd is not None:
                if not filesystem._directory_fd_still_names_path(  # noqa: SLF001
                    directory_fd,
                    dir_path,
                ):
                    raise OSError(
                        f"selected directory changed while it was being listed: {dir_rel or '.'}"
                    )
            else:
                current_dir_path = (
                    filesystem.mutation_path(dir_rel)
                    if dir_rel
                    else filesystem.root
                )
                current_directory_stat = current_dir_path.stat(follow_symlinks=False)
                assert initial_directory_stat is not None
                if (
                    int(current_directory_stat.st_dev),
                    int(current_directory_stat.st_ino),
                ) != (
                    int(initial_directory_stat.st_dev),
                    int(initial_directory_stat.st_ino),
                ):
                    raise OSError(
                        f"selected directory changed while it was being listed: {dir_rel or '.'}"
                    )

        def image_key(record: ImageRecord) -> tuple[object, ...]:
            name = record.filename.casefold()
            if sort_order in {SortOrder.SIZE_ASC, SortOrder.SIZE_DESC}:
                metric = record.size_bytes
                return (
                    -metric if sort_order == SortOrder.SIZE_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            if sort_order in {SortOrder.DATE_ASC, SortOrder.DATE_DESC}:
                metric = record.mtime_ns
                return (
                    -metric if sort_order == SortOrder.DATE_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            if sort_order in {SortOrder.ASPECT_ASC, SortOrder.ASPECT_DESC}:
                metric = record.aspect_ratio
                return (
                    -metric if sort_order == SortOrder.ASPECT_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            return name, record.rel_path.casefold()

        def directory_key(record: DirectoryRecord) -> tuple[object, ...]:
            name = record.filename.casefold()
            if sort_order in {SortOrder.SIZE_ASC, SortOrder.SIZE_DESC}:
                return record.size_bytes, name, record.rel_path.casefold()
            if sort_order in {SortOrder.DATE_ASC, SortOrder.DATE_DESC}:
                return record.mtime_ns, name, record.rel_path.casefold()
            if sort_order in {SortOrder.ASPECT_ASC, SortOrder.ASPECT_DESC}:
                return record.aspect_ratio, name, record.rel_path.casefold()
            return name, record.rel_path.casefold()

        # Image SQL keeps name/path ties ascending for metric-descending
        # sorts. Directory aggregate/date queries reverse the full key.
        image_reverse = sort_order == SortOrder.NAME_DESC
        directory_reverse = sort_order in {
            SortOrder.NAME_DESC,
            SortOrder.SIZE_DESC,
            SortOrder.DATE_DESC,
            SortOrder.ASPECT_DESC,
        }
        if cancel_event.is_set():
            raise IndexTaskCancelled()
        sorted_images = sorted(image_records, key=image_key, reverse=image_reverse)
        if cancel_event.is_set():
            raise IndexTaskCancelled()
        records: list[PaneRecord] = [
            *sorted(directory_records, key=directory_key, reverse=directory_reverse),
            *sorted_images,
        ]
        if cancel_event.is_set():
            raise IndexTaskCancelled()
        stable_row_by_key: dict[tuple[str, str], int] = {}
        order_hash = hashlib.sha256()
        for row, record in enumerate(records):
            if row % 256 == 0 and cancel_event.is_set():
                raise IndexTaskCancelled()
            stable_row_by_key[
                (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
                )
            ] = row
            order_hash.update(b"d\0" if isinstance(record, DirectoryRecord) else b"i\0")
            order_hash.update(record.rel_path.encode("utf-8"))
            order_hash.update(b"\0")
        stable_image_order: list[str] = []
        for row, record in enumerate(sorted_images):
            if row % 256 == 0 and cancel_event.is_set():
                raise IndexTaskCancelled()
            stable_image_order.append(record.rel_path)
        filesystem._assert_catalog_root_identity()  # noqa: SLF001 - stale-result guard
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_PHYSICAL_PREVIEW,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=records,
            duration_ms=(monotonic() - started_at) * 1000,
            total_records=len(records),
            total_images=len(image_records),
            stable_row_by_key=stable_row_by_key,
            stable_order_token=order_hash.hexdigest(),
            stable_image_order=stable_image_order,
            excluded_image_rels=excluded_image_rels,
            excluded_directory_rels=excluded_directory_rels,
        )

    @staticmethod
    def _reconciled_physical_view_worker(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        dir_rel: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
        excluded_image_rels: frozenset[str] = frozenset(),
        excluded_directory_rels: frozenset[str] = frozenset(),
    ) -> VirtualViewResult:
        """Build a final filesystem list enriched by the completed catalog scan.

        This may read every direct database row, so it is only queued after
        the fast filesystem placeholder list is already visible.
        """

        result = MainWindow._quick_physical_view_worker(
            root,
            expected_root_identity,
            dir_rel,
            sort_value,
            fingerprint,
            cancel_event,
            excluded_image_rels,
            excluded_directory_rels,
        )

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        sort_order = SortOrder(sort_value)
        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            indexed_images = catalog.list_images(
                dir_rel,
                SortOrder.NAME_ASC,
                include_blobs=False,
                cancel_check=check_canceled,
            )
            indexed_by_rel: dict[str, ImageRecord] = {}
            for row, record in enumerate(indexed_images):
                if row % 256 == 0:
                    check_canceled()
                indexed_by_rel[record.rel_path] = record
            indexed_directories = catalog.list_child_directories(
                dir_rel,
                sort_order,
                include_previews=False,
                include_filesystem_preview_fallback=True,
                cancel_check=check_canceled,
            )
            directory_by_rel: dict[str, DirectoryRecord] = {}
            for row, record in enumerate(indexed_directories):
                if row % 256 == 0:
                    check_canceled()
                directory_by_rel[record.dir_rel] = record
            catalog._assert_catalog_storage_identity()  # noqa: SLF001

            directories: list[DirectoryRecord] = []
            images: list[ImageRecord] = []
            for record in result.images:
                check_canceled()
                if isinstance(record, DirectoryRecord):
                    directories.append(directory_by_rel.get(record.dir_rel, record))
                    continue
                indexed = indexed_by_rel.get(record.rel_path)
                # Never apply metadata belonging to an older file incarnation.
                if (
                    indexed is not None
                    and indexed.size_bytes == record.size_bytes
                    and indexed.mtime_ns == record.mtime_ns
                    and indexed.ctime_ns == record.ctime_ns
                ):
                    images.append(
                        replace(
                            indexed,
                            file_identity=record.file_identity,
                        )
                    )
                else:
                    images.append(record)

            check_canceled()
            directories.sort(
                key=catalog._directory_sort_key(sort_order),  # noqa: SLF001
                reverse=catalog._record_sort_reverse(sort_order),  # noqa: SLF001
            )
            check_canceled()

        def image_key(record: ImageRecord) -> tuple[object, ...]:
            name = record.filename.casefold()
            if sort_order in {SortOrder.SIZE_ASC, SortOrder.SIZE_DESC}:
                metric = record.size_bytes
                return (
                    -metric if sort_order == SortOrder.SIZE_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            if sort_order in {SortOrder.DATE_ASC, SortOrder.DATE_DESC}:
                metric = record.mtime_ns
                return (
                    -metric if sort_order == SortOrder.DATE_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            if sort_order in {SortOrder.ASPECT_ASC, SortOrder.ASPECT_DESC}:
                metric = record.aspect_ratio
                return (
                    -metric if sort_order == SortOrder.ASPECT_DESC else metric,
                    name,
                    record.rel_path.casefold(),
                )
            return name, record.rel_path.casefold()

        check_canceled()
        images.sort(key=image_key, reverse=sort_order == SortOrder.NAME_DESC)
        check_canceled()
        records: list[PaneRecord] = [*directories, *images]
        row_by_key: dict[tuple[str, str], int] = {}
        order_hash = hashlib.sha256()
        for row, record in enumerate(records):
            if row % 256 == 0:
                check_canceled()
            row_by_key[
                (
                "directory" if isinstance(record, DirectoryRecord) else "image",
                record.rel_path,
                )
            ] = row
            order_hash.update(b"d\0" if isinstance(record, DirectoryRecord) else b"i\0")
            order_hash.update(record.rel_path.encode("utf-8"))
            order_hash.update(b"\0")
        stable_image_order: list[str] = []
        for row, record in enumerate(images):
            if row % 256 == 0:
                check_canceled()
            stable_image_order.append(record.rel_path)
        result.images = records
        result.stable_row_by_key = row_by_key
        result.stable_order_token = order_hash.hexdigest()
        result.stable_image_order = stable_image_order
        result.total_records = len(records)
        result.total_images = len(images)
        return result

    def _physical_view_worker(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        dir_rel: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
        page_offset: int = 0,
        page_limit: int = PANE_QUERY_PAGE_SIZE,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            catalog._conn.execute("BEGIN")  # noqa: SLF001 - one count/page read snapshot
            try:
                directory_count = catalog.known_child_directory_count(
                    dir_rel,
                    cancel_check=check_canceled,
                )
                image_count = catalog.image_count(dir_rel, cancel_check=check_canceled)
                check_canceled()
                directory_take = max(
                    0,
                    min(page_limit, directory_count - page_offset),
                )
                directories = (
                    catalog.list_child_directories_page(
                        dir_rel,
                        sort_order,
                        limit=directory_take,
                        offset=page_offset,
                        include_previews=True,
                        include_filesystem_preview_fallback=False,
                        cancel_check=check_canceled,
                    )
                    if directory_take
                    else []
                )
                image_take = page_limit - directory_take
                image_offset = max(0, page_offset - directory_count)
                images = (
                    catalog.list_images_page(
                        dir_rel,
                        sort_order,
                        limit=image_take,
                        offset=image_offset,
                        include_blobs=False,
                        cancel_check=check_canceled,
                    )
                    if image_take
                    else []
                )
            finally:
                with suppress(sqlite3.Error):
                    catalog._conn.execute("ROLLBACK")  # noqa: SLF001
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        check_canceled()
        total_records = directory_count + image_count
        next_offset = min(total_records, page_offset + page_limit)
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_PHYSICAL,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=[*directories, *images],
            duration_ms=(monotonic() - started_at) * 1000,
            total_records=total_records,
            total_images=image_count,
            page_offset=page_offset,
            next_offset=next_offset,
            has_more=next_offset < total_records,
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
        self._thumbnail_scroll_positions.move_to_end(self._thumbnail_scroll_key)
        while len(self._thumbnail_scroll_positions) > MAX_THUMBNAIL_VIEW_STATES:
            self._thumbnail_scroll_positions.popitem(last=False)

    def _remember_thumbnail_selection(self) -> None:
        if self._thumbnail_scroll_key is None:
            return
        self._thumbnail_selections[self._thumbnail_scroll_key] = (
            self._thumbnail_selection_keys(),
            self._current_thumbnail_selection_key(),
        )
        self._thumbnail_selections.move_to_end(self._thumbnail_scroll_key)
        while len(self._thumbnail_selections) > MAX_THUMBNAIL_VIEW_STATES:
            self._thumbnail_selections.popitem(last=False)

    def _restore_thumbnail_scroll_position(self, key: TreeStateKey | None) -> None:
        self._thumbnail_scroll_key = key
        position = self._thumbnail_scroll_positions.get(key) if key is not None else None
        if position is not None and key is not None:
            self._thumbnail_scroll_positions.move_to_end(key)
        vertical, horizontal = position or (0, 0)
        if vertical > 0 and self.model.has_complete_row_map and self.model.canFetchMore():
            row_height = max(1, self.model.card_size(self.thumbnail_view.font()).height())
            estimated_row = (
                ceil(vertical / row_height) * max(1, self.thumbnail_columns)
                + THUMBNAIL_MODEL_BATCH_SIZE
            )
            self.model.ensure_row_loaded(min(len(self.model.images) - 1, estimated_row))
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

    def _shallow_child_directories(
        self,
        catalog: Catalog,
        dir_rel: str,
        *,
        sort_order: SortOrder | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> list[DirectoryRecord]:
        """Return a strict time/entry-bounded child-directory preview."""

        records: list[DirectoryRecord] = []
        parent = catalog.abs_path(dir_rel) if dir_rel else catalog.root
        prefix = f"{dir_rel}/" if dir_rel else ""
        deadline = monotonic() + PHYSICAL_PREVIEW_SCAN_BUDGET_SECONDS
        examined = 0
        try:
            entries = os.scandir(parent)
        except OSError:
            return []
        with entries:
            for entry in entries:
                if cancel_check is not None:
                    cancel_check()
                examined += 1
                if (
                    examined > INITIAL_TREE_ENTRY_LIMIT
                    or len(records) >= PHYSICAL_PREVIEW_DIRECTORY_LIMIT
                    or monotonic() >= deadline
                ):
                    break
                if entry.name == ".marnwick":
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    entry.name.encode("utf-8")
                    stat_mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                except (OSError, UnicodeError):
                    continue
                child_rel = f"{prefix}{entry.name}"
                records.append(
                    DirectoryRecord(
                        catalog_root=catalog.root,
                        dir_rel=child_rel,
                        name=entry.name,
                        mtime_ns=stat_mtime,
                        allow_preview_fallback=False,
                    )
                )
        return sorted(
            records,
            key=catalog._directory_sort_key(sort_order or self.current_sort),
            reverse=catalog._record_sort_reverse(sort_order or self.current_sort),
        )

    def _known_shallow_child_directories(
        self,
        catalog: Catalog,
        dir_rel: str,
    ) -> list[DirectoryRecord]:
        item_map = self._tree_item_maps.get(catalog.root, {})
        records: list[DirectoryRecord] = []
        examined = 0
        for child_rel in item_map:
            examined += 1
            if examined > INITIAL_TREE_ENTRY_LIMIT or len(records) >= PHYSICAL_PREVIEW_DIRECTORY_LIMIT:
                break
            if not child_rel or child_rel.rpartition("/")[0] != dir_rel:
                continue
            records.append(
                DirectoryRecord(
                    catalog_root=catalog.root,
                    dir_rel=child_rel,
                    name=child_rel.rpartition("/")[2],
                    allow_preview_fallback=False,
                )
            )
        return sorted(
            records,
            key=catalog._directory_sort_key(self.current_sort),
            reverse=catalog._record_sort_reverse(self.current_sort),
        )

    def _finish_shallow_directory_preview(
        self,
        catalog: Catalog,
        dir_rel: str,
        generation: int,
        images: list[ImageRecord],
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> None:
        if (
            self._closing
            or generation != self._physical_pane_generation
            or self.current_catalog is not catalog
            or self.current_virtual_kind is not None
            or self.current_dir_rel != dir_rel
        ):
            return
        directories = self._shallow_child_directories(catalog, dir_rel)
        if not directories:
            return
        records = self._without_pending_delete_records(catalog.root, [*directories, *images])
        self.model.set_images(catalog, records)
        self.refresh_thumbnail_layout()
        self._restore_thumbnail_selection(selection_keys, current_key)
        self._restore_thumbnail_scroll_position(scroll_key)
        self.update_selection_status()
        self._apply_pending_rel_path_selection(generation, clear_on_success=False)

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
        fingerprint = self._physical_pane_generation
        admission_failed = False
        task = self._matching_virtual_view_task(
            catalog.root,
            kind,
            value,
            self.current_sort,
            fingerprint,
        )
        if task is None:
            cancel_event = Event()
            try:
                future = self.virtual_view_executor.submit(
                    self._simple_virtual_view_worker,
                    catalog.root,
                    catalog.root_identity,
                    catalog.storage_identity,
                    kind,
                    value,
                    self.current_sort.value,
                    fingerprint,
                    cancel_event,
                )
            except RuntimeError:
                task = None
                admission_failed = True
            else:
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
        retain_existing = (
            self.model.catalog is catalog
            and self._thumbnail_scroll_key == scroll_key
        )
        if not retain_existing:
            self.model.set_images(catalog, [])
            self.refresh_thumbnail_layout()
            self._restore_thumbnail_scroll_position(scroll_key)
            self.update_selection_status()
        if task is not None:
            self._show_virtual_view_status(task)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Catalog reader busy; retrying virtual directory")
            if admission_failed:
                QTimer.singleShot(
                    50,
                    partial(
                        self._retry_virtual_view_admission,
                        fingerprint,
                        set(selection_keys),
                        current_key,
                        scroll_key,
                    ),
                )

    def _simple_virtual_view_worker(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        kind: str,
        value: str,
        sort_value: str,
        fingerprint: int,
        cancel_event: Event,
        page_offset: int = 0,
        page_limit: int = PANE_QUERY_PAGE_SIZE,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        check_canceled()
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            catalog._conn.execute("BEGIN")  # noqa: SLF001 - one count/page read snapshot
            try:
                if kind == VIRTUAL_KIND_TAG:
                    total_images = catalog.tag_image_count(
                        value,
                        cancel_check=check_canceled,
                    )
                    images = catalog.list_images_for_tag_page(
                        value,
                        sort_order,
                        limit=page_limit,
                        offset=page_offset,
                        include_blobs=False,
                        cancel_check=check_canceled,
                    )
                else:
                    total_images = catalog.exact_duplicate_image_count(
                        cancel_check=check_canceled,
                    )
                    images = catalog.list_exact_duplicate_images_page(
                        sort_order,
                        limit=page_limit,
                        offset=page_offset,
                        include_blobs=False,
                        cancel_check=check_canceled,
                    )
            finally:
                with suppress(sqlite3.Error):
                    catalog._conn.execute("ROLLBACK")  # noqa: SLF001
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        check_canceled()
        next_offset = min(total_images, page_offset + page_limit)
        return VirtualViewResult(
            root=root,
            kind=kind,
            value=value,
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=images,
            duration_ms=(monotonic() - started_at) * 1000,
            total_records=total_images,
            total_images=total_images,
            page_offset=page_offset,
            next_offset=next_offset,
            has_more=next_offset < total_images,
        )

    def _pane_page_callback(
        self,
        *,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        kind: str,
        value: str,
        sort_order: SortOrder,
        fingerprint: int,
    ) -> Callable[[int], None]:
        return partial(
            self._request_pane_page,
            root,
            expected_root_identity,
            expected_storage_identity,
            kind,
            value,
            sort_order,
            fingerprint,
        )

    def _request_pane_page(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        kind: str,
        value: str,
        sort_order: SortOrder,
        fingerprint: int,
        page_offset: int,
    ) -> None:
        """Submit one bounded page without doing SQLite or filesystem work here."""

        catalog = self.current_catalog
        if (
            self._closing
            or catalog is None
            or catalog.root != root
            or catalog.root_identity != expected_root_identity
            or catalog.storage_identity != expected_storage_identity
            or self.workspace.catalog_for_root(root) is not catalog
            or self.current_sort != sort_order
            or self._physical_pane_generation != fingerprint
            or page_offset != self.model.next_page_offset
        ):
            self.model.page_fetch_failed(page_offset)
            return
        if kind == VIRTUAL_KIND_PHYSICAL:
            if self.current_virtual_kind is not None or self.current_dir_rel != value:
                self.model.page_fetch_failed(page_offset)
                return
            worker = self._physical_view_worker
        elif kind in {VIRTUAL_KIND_TAG, VIRTUAL_KIND_DUPLICATES}:
            if self.current_virtual_kind != kind or self.current_virtual_value != value:
                self.model.page_fetch_failed(page_offset)
                return
            worker = self._simple_virtual_view_worker
        else:
            self.model.page_fetch_failed(page_offset)
            return
        if self._matching_virtual_view_task(
            root,
            kind,
            value,
            sort_order,
            fingerprint,
            page_offset=page_offset,
        ) is not None:
            return
        cancel_event = Event()
        try:
            future = self.virtual_view_executor.submit(
                worker,
                root,
                expected_root_identity,
                expected_storage_identity,
                value,
                sort_order.value,
                fingerprint,
                cancel_event,
                page_offset,
                PANE_QUERY_PAGE_SIZE,
            )
        except RuntimeError:
            self.model.page_fetch_failed(page_offset)
            self.statusBar().showMessage("Catalog reader is busy; reload the pane to retry.", 3000)
            return
        self._virtual_view_tasks[future] = VirtualViewTask(
            root=root,
            kind=kind,
            value=value,
            sort_order=sort_order,
            fingerprint=fingerprint,
            future=future,
            started_at=monotonic(),
            selection_keys=set(),
            current_key=None,
            scroll_key=self._current_thumbnail_scroll_key(),
            cancel_event=cancel_event,
            page_offset=page_offset,
        )

    def _viewer_navigation_page_worker(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        kind: str,
        value: str,
        sort_order: SortOrder,
        page_offset: int,
        page_limit: int,
        cancel_event: Event,
    ) -> ViewerNavigationPage:
        if kind == VIRTUAL_KIND_PHYSICAL:
            result = self._physical_view_worker(
                root,
                expected_root_identity,
                expected_storage_identity,
                value,
                sort_order.value,
                0,
                cancel_event,
                page_offset,
                page_limit,
            )
        elif kind in {VIRTUAL_KIND_TAG, VIRTUAL_KIND_DUPLICATES}:
            result = self._simple_virtual_view_worker(
                root,
                expected_root_identity,
                expected_storage_identity,
                kind,
                value,
                sort_order.value,
                0,
                cancel_event,
                page_offset,
                page_limit,
            )
        else:
            raise ValueError(f"unsupported paged viewer kind: {kind}")
        return ViewerNavigationPage(
            rel_paths=[
                record.rel_path for record in result.images if isinstance(record, ImageRecord)
            ],
            next_offset=result.next_offset,
            has_more=result.has_more,
            total_images=result.total_images or 0,
        )

    def _thumbnail_record_key(self, record: PaneRecord) -> tuple[str, str]:
        if isinstance(record, DirectoryRecord):
            return ("directory", record.dir_rel)
        return ("image", record.rel_path)

    def _pending_delete_image_rels(self, root: Path) -> set[str]:
        resolved_root = root
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
        pending_images, pending_directories = self._pending_physical_exclusions(root)
        if not pending_images and not pending_directories:
            return records if isinstance(records, list) else list(records)

        def inside_pending_directory(rel_path: str) -> bool:
            candidate = rel_path
            while candidate:
                if candidate in pending_directories:
                    return True
                parent = candidate.rpartition("/")[0]
                if parent == candidate:
                    break
                candidate = parent
            return "" in pending_directories

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

    def _pending_physical_exclusions(
        self,
        root: Path,
    ) -> tuple[frozenset[str], frozenset[str]]:
        resolved_root = root
        delete_tasks = tuple(
            task for task in self._delete_payload_tasks if task.root == resolved_root
        )
        deferred_requests = tuple(
            request
            for request in self._deferred_delete_requests
            if request.catalog.root == resolved_root
        )
        move_tasks = tuple(
            task for task in self._move_payload_tasks if resolved_root in task.affected_roots
        )
        signature = (
            tuple((id(task), task.future.done()) for task in delete_tasks),
            tuple(id(request) for request in deferred_requests),
            tuple((id(task), task.future.done()) for task in move_tasks),
        )
        cached = self._pending_exclusion_cache.get(resolved_root)
        if cached is not None and cached[0] == signature:
            return cached[1]

        pending_images: set[str] = set()
        for delete_task in delete_tasks:
            if not delete_task.future.done():
                pending_images.update(delete_task.rel_paths)
        # A delete that is waiting for an in-flight save/reconciliation is
        # still an accepted user intent.  Keep it hidden across directory and
        # catalog navigation until the protected delete either commits or is
        # explicitly restored after failure.
        for request in deferred_requests:
            pending_images.update(request.rel_paths)
        pending_directories: set[str] = set()
        for move_task in move_tasks:
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
        result = frozenset(pending_images), frozenset(pending_directories)
        self._pending_exclusion_cache[resolved_root] = signature, result
        return result

    def _merge_physical_preview_records(
        self,
        catalog: Catalog,
        page_records: Sequence[PaneRecord],
        preview_records: Sequence[PaneRecord],
    ) -> list[PaneRecord]:
        """Merge two bounded prefixes, preferring indexed database records."""

        by_key: dict[tuple[str, str], PaneRecord] = {
            self._thumbnail_record_key(record): record for record in page_records
        }
        for record in preview_records:
            by_key.setdefault(self._thumbnail_record_key(record), record)
        directories = [
            record for record in by_key.values() if isinstance(record, DirectoryRecord)
        ]
        images = [
            record for record in by_key.values() if isinstance(record, ImageRecord)
        ]
        directories.sort(
            key=catalog._directory_sort_key(self.current_sort),
            reverse=catalog._record_sort_reverse(self.current_sort),
        )
        images.sort(
            key=catalog._record_sort_key(self.current_sort),
            reverse=catalog._record_sort_reverse(self.current_sort),
        )
        return [*directories, *images]

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
        keys = set(selection_keys)
        if current_key is not None:
            keys.add(current_key)
        self.model.locate_rows_for_keys(keys)
        if self.model.record_indexes_pending:
            self._pending_thumbnail_index_restore = (set(selection_keys), current_key)
        matching_rows = sorted(
            row
            for key in keys
            if (row := self.model.row_for_key(key)) is not None
        )
        unresolved_keys = {
            key for key in keys if self.model.row_for_key(key) is None
        }
        if unresolved_keys and self.model.may_have_more_pages:
            self._pending_thumbnail_index_restore = (set(selection_keys), current_key)
            if self.model.canFetchMore():
                self.model.fetchMore()
        if matching_rows:
            if not self.model.ensure_row_loaded(max(matching_rows)):
                self._pending_thumbnail_index_restore = (
                    set(selection_keys),
                    current_key,
                )
                return
        selection.clearSelection()
        current_index = QModelIndex()
        for row in matching_rows:
            key = self._thumbnail_record_key(self.model.images[row])
            if key not in selection_keys and key != current_key:
                continue
            index = self.model.index(row, 0)
            if key in selection_keys:
                selection.select(index, QItemSelectionModel.SelectionFlag.Select)
            if key == current_key:
                current_index = index
        if current_index.isValid():
            selection.setCurrentIndex(current_index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _thumbnail_indexes_ready(self, _generation: int) -> None:
        pending = self._pending_thumbnail_index_restore
        self._pending_thumbnail_index_restore = None
        if pending is not None:
            self._restore_thumbnail_selection(*pending)
        self.update_selection_status()
        self._apply_pending_rel_path_selection(self._physical_pane_generation)

    def _thumbnail_rows_exposed(self) -> None:
        pending = self._pending_thumbnail_index_restore
        self._pending_thumbnail_index_restore = None
        if pending is not None:
            self._restore_thumbnail_selection(*pending)
        if self._thumbnail_scroll_key is not None:
            self._restore_thumbnail_scroll_position(self._thumbnail_scroll_key)
        self._apply_pending_rel_path_selection(self._physical_pane_generation)

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
        fingerprint = self._physical_pane_generation
        cache_version = self._very_similar_cache_versions.get(catalog.root, 0)
        cache_key = self._very_similar_cache_key(catalog.root, self.current_sort, cache_version)
        cached_images = self._very_similar_cache.get(cache_key)
        if cached_images is not None:
            self._very_similar_cache.move_to_end(cache_key)
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
        admission_failed = False
        if task is None:
            cancel_event = Event()
            try:
                future = self.virtual_view_executor.submit(
                    self._very_similar_virtual_view_worker,
                    catalog.root,
                    catalog.root_identity,
                    catalog.storage_identity,
                    self.current_sort.value,
                    fingerprint,
                    cache_version,
                    cancel_event,
                )
            except RuntimeError:
                task = None
                admission_failed = True
            else:
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
                    cache_version=cache_version,
                )
                self._virtual_view_tasks[future] = task
        elif preserve_selection:
            task.selection_keys = set(selection_keys)
            task.current_key = current_key
            task.scroll_key = scroll_key

        retain_existing = (
            self.model.catalog is catalog
            and self._thumbnail_scroll_key == scroll_key
        )
        if not retain_existing:
            self.model.set_images(catalog, [])
            self.refresh_thumbnail_layout()
            self._restore_thumbnail_scroll_position(scroll_key)
            self.update_selection_status()
        if task is not None:
            self._show_virtual_view_status(task)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Catalog reader busy; retrying Very Similar")
            if admission_failed:
                QTimer.singleShot(
                    50,
                    partial(
                        self._retry_virtual_view_admission,
                        fingerprint,
                        set(selection_keys),
                        current_key,
                        scroll_key,
                    ),
                )

    def _retry_virtual_view_admission(
        self,
        fingerprint: int,
        selection_keys: set[tuple[str, str]],
        current_key: tuple[str, str] | None,
        scroll_key: TreeStateKey | None,
    ) -> None:
        if self._closing or fingerprint != self._physical_pane_generation:
            return
        self.load_current_virtual_directory(
            preserve_selection=True,
            selection_keys=selection_keys,
            current_key=current_key,
            scroll_key=scroll_key,
        )

    def _very_similar_virtual_view_worker(
        self,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        sort_value: str,
        fingerprint: int,
        cache_version: int,
        cancel_event: Event,
    ) -> VirtualViewResult:
        started_at = monotonic()
        sort_order = SortOrder(sort_value)

        def check_canceled() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled()

        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            images = catalog.list_very_similar_images(
                sort_order,
                include_blobs=False,
                cancel_check=check_canceled,
            )
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        return VirtualViewResult(
            root=root,
            kind=VIRTUAL_KIND_VERY_SIMILAR,
            value="",
            sort_order=sort_order,
            fingerprint=fingerprint,
            images=images,
            duration_ms=(monotonic() - started_at) * 1000,
            cache_version=cache_version,
        )

    def _very_similar_cache_key(
        self,
        root: Path,
        sort_order: SortOrder,
        fingerprint: int,
    ) -> tuple[Path, str, int]:
        return (root, sort_order.value, fingerprint)

    def _drop_very_similar_cache(self, root: Path) -> None:
        resolved = root
        self._very_similar_cache_versions[resolved] = (
            self._very_similar_cache_versions.get(resolved, 0) + 1
        )
        self._very_similar_cache = OrderedDict(
            (key, images)
            for key, images in self._very_similar_cache.items()
            if key[0] != resolved
        )

    def _matching_virtual_view_task(
        self,
        root: Path,
        kind: str,
        value: str,
        sort_order: SortOrder,
        fingerprint: int,
        *,
        page_offset: int = 0,
    ) -> VirtualViewTask | None:
        resolved = root
        for task in self._virtual_view_tasks.values():
            if (
                not task.cancel_event.is_set()
                and not task.future.cancelled()
                and task.root == resolved
                and task.kind == kind
                and task.value == value
                and task.sort_order == sort_order
                and task.fingerprint == fingerprint
                and task.page_offset == page_offset
            ):
                return task
        return None

    def _cancel_virtual_view_tasks(self, root: Path) -> None:
        resolved = root
        for future, task in list(self._virtual_view_tasks.items()):
            if task.root != resolved:
                continue
            task.cancel_event.set()
            future.cancel()
            self._virtual_view_tasks.pop(future, None)

    def _rollover_virtual_view_executors(self) -> None:
        # Compatibility hook: cancel tracked work while retaining bounded
        # epoch pools. Saturated epochs roll automatically, up to their fixed
        # retirement/thread cap, when replacement work is submitted.
        for future, task in list(self._virtual_view_tasks.items()):
            task.cancel_event.set()
            future.cancel()
            self._virtual_view_tasks.pop(future, None)

    def _settle_virtual_view_tasks(self) -> None:
        for future, task in list(self._virtual_view_tasks.items()):
            if not future.done():
                continue
            if (
                task.kind == VIRTUAL_KIND_PHYSICAL
                and task.page_offset == 0
                and (
                    self._physical_preview_retry_generation == task.fingerprint
                    or any(
                        preview.root == task.root
                        and preview.kind == VIRTUAL_KIND_PHYSICAL_PREVIEW
                        and preview.value == task.value
                        and preview.sort_order == task.sort_order
                        and preview.fingerprint == task.fingerprint
                        and not preview.cancel_event.is_set()
                        and not preview.future.cancelled()
                        for preview in self._virtual_view_tasks.values()
                    )
                )
            ):
                try:
                    early_result = future.result()
                except Exception:
                    early_result = None
                if early_result is not None and not early_result.images:
                    # An empty/brand-new catalog has no useful cache to paint.
                    # Wait for the filesystem snapshot so it publishes exactly
                    # once, including while preview admission is retrying.
                    continue
            self._virtual_view_tasks.pop(future, None)
            if future.cancelled() or task.cancel_event.is_set():
                if task.page_offset and self._is_current_virtual_task(task):
                    self.model.page_fetch_failed(task.page_offset)
                continue
            try:
                result = future.result()
            except Exception as error:
                if task.kind == VIRTUAL_KIND_PHYSICAL_PREVIEW:
                    if (
                        self._physical_reconcile_pending_generation == task.fingerprint
                        and not task.physical_reconcile
                    ):
                        self._request_physical_reconcile(task.root, task.value)
                    continue
                if task.page_offset:
                    if self._is_current_virtual_task(task):
                        self.model.page_fetch_failed(task.page_offset)
                        self.statusBar().showMessage(
                            f"Unable to load more catalog rows: {error}",
                            5000,
                        )
                    continue
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

            if result.kind == VIRTUAL_KIND_PHYSICAL_PREVIEW:
                if (
                    self._is_current_virtual_result(result)
                    and result.fingerprint == self._physical_pane_generation
                    and self.current_catalog is not None
                ):
                    current_exclusions = self._pending_physical_exclusions(
                        result.root
                    )
                    result_exclusions = (
                        result.excluded_image_rels,
                        result.excluded_directory_rels,
                    )
                    if current_exclusions != result_exclusions:
                        # Pending mutation membership changed while the worker
                        # enumerated. Rescan with the new small exclusion sets
                        # rather than filtering a huge list on the Qt thread.
                        if task.physical_reconcile:
                            self._request_physical_reconcile(task.root, task.value)
                        else:
                            self._submit_physical_preview_task(
                                self.current_catalog,
                                task.value,
                                task.sort_order,
                                task.fingerprint,
                                task.selection_keys,
                                task.current_key,
                                task.scroll_key,
                                retry_attempt=0,
                            )
                        continue
                    preview_records = result.images
                    stable_row_by_key = result.stable_row_by_key
                    stable_order_token = result.stable_order_token
                    stable_image_order = result.stable_image_order
                    total_images = result.total_images
                    if len(preview_records) != len(result.images):
                        stable_row_by_key = {
                            self._thumbnail_record_key(record): row
                            for row, record in enumerate(preview_records)
                        }
                        stable_order_token = None
                        stable_image_order = [
                            record.rel_path
                            for record in preview_records
                            if isinstance(record, ImageRecord)
                        ]
                        total_images = sum(
                            isinstance(record, ImageRecord)
                            for record in preview_records
                        )

                    cached_overlay = (
                        list(self.model.images[:QUERY_PAGE_MAX_SIZE])
                        if self.model.is_paged
                        and self.model.catalog is self.current_catalog
                        else []
                    )
                    same_complete_order = (
                        stable_order_token is not None
                        and stable_order_token == self.model.complete_order_token
                    )
                    published = not same_complete_order
                    if published:
                        self.model.set_images(
                            self.current_catalog,
                            preview_records,
                            complete_row_by_key=stable_row_by_key,
                            complete_order_token=stable_order_token,
                            complete_image_count=total_images,
                            complete_image_order=stable_image_order,
                            preserve_pixmap_cache=bool(cached_overlay),
                        )
                        self.model.update_records_in_place(cached_overlay)
                    elif same_complete_order:
                        self.model.replace_complete_records_in_place(
                            preview_records,
                            total_images=total_images,
                        )
                    self._physical_preview_result_generation = result.fingerprint
                    if published:
                        self.refresh_thumbnail_layout()
                        self._restore_thumbnail_selection(task.selection_keys, task.current_key)
                        self._restore_thumbnail_scroll_position(task.scroll_key)
                        self.update_selection_status()
                        self._apply_pending_rel_path_selection(
                            result.fingerprint,
                            clear_on_success=False,
                        )
                if self._physical_reconcile_pending_generation == task.fingerprint:
                    if task.physical_reconcile:
                        self._physical_reconcile_pending_generation = None
                    else:
                        self._request_physical_reconcile(task.root, task.value)
                continue
            if result.kind == VIRTUAL_KIND_VERY_SIMILAR:
                if result.cache_version == self._very_similar_cache_versions.get(result.root, 0):
                    cache_key = self._very_similar_cache_key(
                        result.root,
                        result.sort_order,
                        result.cache_version,
                    )
                    self._very_similar_cache = OrderedDict(
                        (key, images)
                        for key, images in self._very_similar_cache.items()
                        if key[0] != cache_key[0] or key[1] != cache_key[1]
                    )
                    self._very_similar_cache[cache_key] = list(result.images)
                    while len(self._very_similar_cache) > MAX_VERY_SIMILAR_CACHE_ENTRIES:
                        self._very_similar_cache.popitem(last=False)
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
            if result.fingerprint != self._physical_pane_generation:
                continue
            if result.kind == VIRTUAL_KIND_PHYSICAL:
                self._physical_full_result_generation = result.fingerprint
            filtered_records = self._without_pending_delete_records(
                catalog.root,
                result.images,
            )
            if (
                result.kind == VIRTUAL_KIND_PHYSICAL
                and result.page_offset == 0
                and self.model.catalog is catalog
                and self.model.has_complete_row_map
                and result.fingerprint != self._physical_preview_result_generation
                and (
                    self._physical_preview_retry_generation == result.fingerprint
                    or self._matching_virtual_view_task(
                        catalog.root,
                        VIRTUAL_KIND_PHYSICAL_PREVIEW,
                        result.value,
                        result.sort_order,
                        result.fingerprint,
                    )
                    is not None
                )
            ):
                # A same-folder reload retains the old complete snapshot until
                # the new filesystem snapshot is ready. A bounded DB page may
                # enrich matching incarnations, but must not merge/sort every
                # old row on Qt or temporarily turn the model back into a page.
                self.model.update_records_in_place(filtered_records)
                self._apply_pending_rel_path_selection(result.fingerprint)
                continue
            if (
                result.kind == VIRTUAL_KIND_PHYSICAL
                and result.page_offset == 0
                and result.fingerprint == self._physical_preview_result_generation
            ):
                # Keep every preview key at its original row. Indexed metadata
                # and thumbnails enrich those rows in place as database work
                # catches up; stale database-only rows are deliberately ignored.
                self.model.update_records_in_place(filtered_records)
                self._apply_pending_rel_path_selection(result.fingerprint)
                continue
            if (
                result.total_records is not None
                and result.total_images is not None
                and result.kind in {
                    VIRTUAL_KIND_PHYSICAL,
                    VIRTUAL_KIND_TAG,
                    VIRTUAL_KIND_DUPLICATES,
                }
            ):
                total_records = result.total_records
                total_images = result.total_images
                if (
                    result.kind == VIRTUAL_KIND_PHYSICAL
                    and result.page_offset == 0
                    and self.model.catalog is catalog
                    and not self.model.is_paged
                    and self.model.images
                ):
                    page_keys = {
                        self._thumbnail_record_key(record) for record in filtered_records
                    }
                    preview_extras = [
                        record
                        for record in self.model.images
                        if self._thumbnail_record_key(record) not in page_keys
                    ]
                    filtered_records = self._merge_physical_preview_records(
                        catalog,
                        filtered_records,
                        preview_extras,
                    )
                    total_records += len(preview_extras)
                    total_images += sum(
                        isinstance(record, ImageRecord) for record in preview_extras
                    )
                if result.page_offset:
                    appended = self.model.append_page(
                        filtered_records,
                        expected_offset=result.page_offset,
                        next_offset=result.next_offset,
                        has_more=result.has_more,
                        total_records=total_records,
                        total_images=total_images,
                    )
                    if not appended:
                        continue
                    self.refresh_thumbnail_layout()
                    self.update_selection_status()
                    self._apply_pending_rel_path_selection(result.fingerprint)
                    pending = self._pending_rel_path_selection
                    if (
                        pending is not None
                        and pending[2] == result.fingerprint
                        and self.model.canFetchMore()
                    ):
                        QTimer.singleShot(0, self.model.fetchMore)
                    continue
                self.model.set_paged_images(
                    catalog,
                    filtered_records,
                    total_records=total_records,
                    total_images=total_images,
                    next_offset=result.next_offset,
                    has_more=result.has_more,
                    request_page=self._pane_page_callback(
                        root=catalog.root,
                        expected_root_identity=catalog.root_identity,
                        expected_storage_identity=catalog.storage_identity,
                        kind=result.kind,
                        value=result.value,
                        sort_order=result.sort_order,
                        fingerprint=result.fingerprint,
                    ),
                )
            else:
                # Compatibility for very-similar and tests/custom workers that
                # intentionally return a fully materialized result.
                self.model.set_images(catalog, filtered_records)
            self.refresh_thumbnail_layout()
            self._restore_thumbnail_selection(task.selection_keys, task.current_key)
            self._restore_thumbnail_scroll_position(task.scroll_key)
            self.update_selection_status()
            self._apply_pending_rel_path_selection(result.fingerprint)
            pending = self._pending_rel_path_selection
            if (
                pending is not None
                and pending[2] == result.fingerprint
                and self.model.canFetchMore()
            ):
                QTimer.singleShot(0, self.model.fetchMore)
        self._flush_physical_progress_reload()

    def _request_physical_progress_reload(self, root: Path, dir_rel: str) -> None:
        catalog = self.current_catalog
        if (
            self._closing
            or catalog is None
            or catalog.root != root
            or self.current_virtual_kind is not None
            or self.current_dir_rel != dir_rel
        ):
            return
        self._pending_physical_progress_reload = (root, dir_rel, self.current_sort)
        self._flush_physical_progress_reload()

    def _request_physical_record_overlay(
        self,
        catalog: Catalog,
        dir_rel: str,
        sort_order: SortOrder,
    ) -> bool:
        fingerprint = self._physical_pane_generation
        if self._matching_virtual_view_task(
            catalog.root,
            VIRTUAL_KIND_PHYSICAL,
            dir_rel,
            sort_order,
            fingerprint,
        ) is not None:
            return False
        cancel_event = Event()
        try:
            future = self.virtual_view_executor.submit(
                self._physical_view_worker,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                dir_rel,
                sort_order.value,
                fingerprint,
                cancel_event,
                0,
                min(
                    QUERY_PAGE_MAX_SIZE,
                    max(
                        PANE_QUERY_PAGE_SIZE,
                        self.model.rowCount()
                        if self._physical_preview_result_generation == fingerprint
                        else 0,
                    ),
                ),
            )
        except RuntimeError:
            return False
        self._virtual_view_tasks[future] = VirtualViewTask(
            root=catalog.root,
            kind=VIRTUAL_KIND_PHYSICAL,
            value=dir_rel,
            sort_order=sort_order,
            fingerprint=fingerprint,
            future=future,
            started_at=monotonic(),
            selection_keys=self._thumbnail_selection_keys(),
            current_key=self._current_thumbnail_selection_key(),
            scroll_key=self._current_thumbnail_scroll_key(),
            cancel_event=cancel_event,
        )
        return True

    def _flush_physical_progress_reload(self) -> None:
        pending = self._pending_physical_progress_reload
        if pending is None:
            return
        root, dir_rel, sort_order = pending
        catalog = self.current_catalog
        if (
            self._closing
            or catalog is None
            or catalog.root != root
            or self.current_virtual_kind is not None
            or self.current_dir_rel != dir_rel
            or self.current_sort != sort_order
        ):
            self._pending_physical_progress_reload = None
            return
        # Let the current database materialization finish and publish. Starting
        # another generation here used to cancel the only load every progress
        # tick, so a large folder could remain empty forever.
        if any(
            task.root == root
            and task.kind == VIRTUAL_KIND_PHYSICAL
            and task.value == dir_rel
            and task.sort_order == sort_order
            and not task.cancel_event.is_set()
            and not task.future.cancelled()
            for task in self._virtual_view_tasks.values()
        ):
            return
        elapsed = monotonic() - self._last_physical_progress_reload_at
        if elapsed < 0.25:
            if not self._physical_progress_reload_timer_pending:
                self._physical_progress_reload_timer_pending = True
                delay_ms = max(1, int((0.25 - elapsed) * 1000))
                QTimer.singleShot(delay_ms, self._run_scheduled_physical_progress_reload)
            return
        if not self._request_physical_record_overlay(catalog, dir_rel, sort_order):
            if not self._physical_progress_reload_timer_pending:
                self._physical_progress_reload_timer_pending = True
                QTimer.singleShot(50, self._run_scheduled_physical_progress_reload)
            return
        self._pending_physical_progress_reload = None
        self._last_physical_progress_reload_at = monotonic()

    def _run_scheduled_physical_progress_reload(self) -> None:
        self._physical_progress_reload_timer_pending = False
        self._flush_physical_progress_reload()

    def _active_virtual_view_task(self) -> VirtualViewTask | None:
        active_tasks = [
            task
            for task in self._virtual_view_tasks.values()
            if self._is_current_virtual_task(task)
            and not task.cancel_event.is_set()
            and not task.future.cancelled()
        ]
        if not active_tasks:
            return None
        return sorted(active_tasks, key=lambda task: task.started_at)[0]

    def _is_current_virtual_task(self, task: VirtualViewTask) -> bool:
        if task.fingerprint != self._physical_pane_generation:
            return False
        if task.kind in {VIRTUAL_KIND_PHYSICAL, VIRTUAL_KIND_PHYSICAL_PREVIEW}:
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
        if result.fingerprint != self._physical_pane_generation:
            return False
        if result.kind in {VIRTUAL_KIND_PHYSICAL, VIRTUAL_KIND_PHYSICAL_PREVIEW}:
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
        elif task.kind == VIRTUAL_KIND_PHYSICAL_PREVIEW:
            self.progress_label.setText(f"Listing folder ({elapsed}s)")
        elif task.kind == VIRTUAL_KIND_PHYSICAL:
            self.progress_label.setText(f"Loading folder ({elapsed}s)")
        else:
            self.progress_label.setText("Building virtual directory")

    def _cancel_active_duplicate_delete_task(self, *, wait: bool = False) -> None:
        task = self._duplicate_delete_task
        if task is None:
            return
        self._cancel_duplicate_delete_task(task.root, wait=wait)

    def _cancel_duplicate_delete_task(self, root: Path, *, wait: bool = False) -> None:
        task = self._duplicate_delete_task
        if task is None or task.root != root:
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
            if self.current_virtual_kind == VIRTUAL_KIND_DUPLICATES:
                # The completed mutation invalidates every duplicate grouping.
                # Do not retain now-stale rows while the fresh query starts.
                self.model.set_images(self.current_catalog, [])
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
        resolved = root
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
                self._settle_viewer_delete(
                    delete_task,
                    path_removed=self._viewer_delete_path_removed_from_outcome(
                        delete_task
                    ),
                )
                continue
            try:
                result = delete_task.future.result()
            except IndexTaskCancelled:
                canceled = True
                self._settle_viewer_delete(
                    delete_task,
                    path_removed=self._viewer_delete_path_removed_from_outcome(
                        delete_task
                    ),
                )
                continue
            except Exception as error:
                last_error = error
                self._settle_viewer_delete(
                    delete_task,
                    path_removed=self._viewer_delete_path_removed_from_outcome(
                        delete_task
                    ),
                )
                continue
            affected_roots.update(result.affected_roots)
            self._settle_viewer_delete(
                delete_task,
                path_removed=(
                    None
                    if result.remaining_rel_paths is None
                    else (
                        not delete_task.rel_paths
                        or delete_task.rel_paths[0] not in result.remaining_rel_paths
                    )
                ),
            )
            if result.canceled:
                canceled = True
                continue
            if result.error is not None:
                last_error = result.error
                continue
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

    @staticmethod
    def _viewer_delete_path_removed_from_outcome(
        delete_task: DeletePayloadTask,
    ) -> bool | None:
        remaining = delete_task.outcome.remaining_rel_paths
        if remaining is None:
            return None
        return bool(
            not delete_task.rel_paths
            or delete_task.rel_paths[0] not in remaining
        )

    @staticmethod
    def _settle_viewer_delete(
        delete_task: DeletePayloadTask,
        *,
        path_removed: bool | None,
    ) -> None:
        viewer = delete_task.viewer
        if viewer is None or not delete_task.rel_paths:
            return
        if path_removed is None:
            viewer.image_delete_postcondition_unknown(delete_task.rel_paths[0])
        else:
            viewer.image_delete_finished(
                delete_task.rel_paths[0],
                path_removed=path_removed,
            )

    def _refresh_after_file_delete(self, affected_roots: set[Path]) -> None:
        for root in affected_roots:
            self._swept_catalog_roots.discard(root)
            self._pruned_catalog_roots.discard(root)
            self._drop_very_similar_cache(root)
        if self.current_catalog is not None and self.current_catalog.root in affected_roots:
            self.reload_tree_and_directory(preserve_tree_scroll=True)
        else:
            for root in affected_roots:
                catalog = self.workspace.catalog_for_root(root)
                if catalog is not None:
                    self._request_incremental_tree_rebuild(catalog, reason="file_delete")

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
        resolved = root
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
        last_error_owner: QWidget = self
        last_completion_verb = "Moved"
        committed_warnings: list[str] = []
        warning_owner: QWidget = self
        canceled = False
        immediate_refresh_roots: set[Path] = set()
        failed_refresh_roots: set[Path] = set()
        thumbnail_reindex_roots: set[Path] = set()
        created_directories: list[tuple[Path, str]] = []
        saved_images: list[tuple[MovePayloadTask, Path, str, object | None]] = []
        reconcile_subtrees: set[tuple[Path, str]] = set()
        reconcile_images: set[tuple[Path, str]] = set()
        for move_task in completed:
            self._move_payload_tasks.remove(move_task)
            if move_task.future.cancelled():
                canceled = True
                failed_refresh_roots.update(move_task.affected_roots)
                continue
            try:
                result = move_task.future.result()
            except IndexTaskCancelled:
                canceled = True
                failed_refresh_roots.update(move_task.affected_roots)
                continue
            except Exception as error:
                last_error = error
                last_error_title = move_task.error_title
                candidate = move_task.edit_owner
                if candidate is not None:
                    try:
                        if candidate.isVisible():
                            last_error_owner = candidate
                    except RuntimeError:
                        pass
                self._retain_failed_image_edit(move_task)
                if move_task.error_title != "Save Image":
                    failed_refresh_roots.update(move_task.affected_roots)
                continue
            last_result = result
            if move_task.navigation_owner is not None:
                with suppress(RuntimeError):
                    move_task.navigation_owner.invalidate_paged_navigation()
            if result.catalog_settings is not None:
                live_catalog = self.workspace.catalog_for_root(move_task.dest_root)
                if live_catalog is not None:
                    # The durable write was performed by the protected worker.
                    # Publishing its committed value only updates the in-memory
                    # cache and avoids a potentially blocking GUI-thread query.
                    live_catalog._settings_cache = result.catalog_settings  # noqa: SLF001
                if result.force_thumbnail_reindex:
                    thumbnail_reindex_roots.add(move_task.dest_root)
            if result.warning:
                committed_warnings.append(result.warning)
                candidate = move_task.edit_owner
                if candidate is not None:
                    try:
                        if candidate.isVisible():
                            warning_owner = candidate
                    except RuntimeError:
                        pass
            last_completion_verb = move_task.completion_verb
            if move_task.completion_verb == "Saved":
                for root, rel_path in move_task.target_images:
                    committed_proof = (
                        result.target_proofs.get(rel_path)
                        if result.target_proofs is not None
                        else None
                    )
                    saved_images.append((move_task, root, rel_path, committed_proof))
                    self._failed_image_edits.pop((root, rel_path), None)
            else:
                immediate_refresh_roots.update(move_task.affected_roots)
                immediate_refresh_roots.update(result.affected_roots)
                reconcile_subtrees.update(result.reconcile_subtrees)
                reconcile_images.update(result.reconcile_images)
            if result.created_dir_rel is not None:
                created_directories.append((move_task.dest_root, result.created_dir_rel))
        self._refresh_active_move_payload_task()
        refresh_roots = immediate_refresh_roots | failed_refresh_roots
        if refresh_roots:
            self._refresh_after_move_payload(refresh_roots)
        self._queue_post_move_reconciliations(
            reconcile_subtrees,
            reconcile_images,
        )
        for root in thumbnail_reindex_roots:
            self._swept_catalog_roots.discard(root)
            self._pruned_catalog_roots.discard(root)
            self._idle_index_tasks.pop(root, None)
            catalog = self.workspace.catalog_for_root(root)
            if self.current_catalog is catalog and catalog is not None:
                self.queue_directory_index(catalog, self.current_dir_rel, force=True)
        if thumbnail_reindex_roots:
            self._schedule_idle_indexing()
        for root, dir_rel in created_directories:
            catalog = self.workspace.catalog_for_root(root)
            if catalog is not None:
                self.queue_directory_index(catalog, dir_rel, interactive=False)
        for move_task, root, rel_path, committed_proof in saved_images:
            catalog = self.workspace.catalog_for_root(root)
            if catalog is None:
                if isinstance(move_task.edit_owner, FullscreenViewer):
                    move_task.edit_owner.image_save_failed(rel_path)
                for request in list(self._deferred_delete_requests):
                    if move_task in request.dependencies:
                        self._deferred_delete_requests.remove(request)
                        self._restore_deferred_delete_view(request)
                continue
            index_task = self._submit_image_reconciliation(
                ImageReconcileContext(
                    root,
                    rel_path,
                    move_task.edit_owner,
                    expected_proof=committed_proof,
                ),
            )
            for request in list(self._deferred_delete_requests):
                if move_task in request.dependencies:
                    if committed_proof is None:
                        self._deferred_delete_requests.remove(request)
                        self.progress_label.setText(
                            "Saved image bytes could not be verified; delete was not started"
                        )
                        self._restore_deferred_delete_view(request)
                        continue
                    request.expected_identities.pop(rel_path, None)
                    request.expected_proofs[rel_path] = committed_proof
                    request.reconciliation_tasks = (*request.reconciliation_tasks, index_task)
        if committed_warnings:
            show_error(
                warning_owner,
                "Save Image Warning",
                "The image edit was saved, but a later cleanup or durability step reported a warning:\n\n"
                + "\n\n".join(committed_warnings),
            )
        if last_error is not None:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            detail = str(last_error)
            if last_error_title == "Save Image":
                detail = f"{detail}\n\nThe edit operations were retained and will be restored when the image is opened again."
            show_error(last_error_owner, last_error_title, detail)
            self._flush_deferred_delete_requests()
            return
        if last_result is not None:
            self.progress_bar.setRange(0, max(last_result.requested, 1))
            self.progress_bar.setValue(min(last_result.requested, max(last_result.requested, 1)))
            self.progress_label.setText(f"{last_completion_verb} {last_result.moved} item(s)")
            self._flush_deferred_delete_requests()
            return
        if canceled and not self._move_payload_tasks:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
        self._flush_deferred_delete_requests()

    def _submit_image_reconciliation(self, context: ImageReconcileContext) -> IndexTask:
        dir_rel = context.rel_path.rpartition("/")[0]
        catalog = self.workspace.catalog_for_root(context.root)
        if catalog is None:
            raise OSError(f"catalog closed before saved image reconciliation: {context.root}")
        index_task, _ = self.indexer.submit_action(
            "Updating saved image",
            context.root,
            dir_rel,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda task: self._reconcile_saved_image_worker(
                context.root,
                context.rel_path,
                context.expected_proof,
                task,
            ),
            key=f"saved-image:{context.root}:{context.rel_path}:{monotonic()}",
            interactive=False,
            force_refresh=True,
            preemptible=False,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        previous = context.replace_task
        context.replace_task = None
        self._image_reconcile_tasks[index_task] = context
        if previous is not None:
            for request in self._deferred_delete_requests:
                if previous in request.reconciliation_tasks:
                    request.reconciliation_tasks = tuple(
                        index_task if task is previous else task
                        for task in request.reconciliation_tasks
                    )
        return index_task

    def _flush_deferred_delete_requests(self) -> None:
        for request in list(self._deferred_delete_requests):
            pending_targets = self._pending_image_save_targets(request.catalog.root)
            if any(rel_path in pending_targets for rel_path in request.rel_paths):
                continue
            if any(not dependency.future.done() for dependency in request.dependencies):
                continue
            try:
                for dependency in request.dependencies:
                    dependency.future.result()
            except Exception:
                self._deferred_delete_requests.remove(request)
                self._restore_deferred_delete_view(request)
                continue
            reconciliation_snapshots = [task.snapshot() for task in request.reconciliation_tasks]
            if any(not snapshot.done for snapshot in reconciliation_snapshots):
                continue
            self._deferred_delete_requests.remove(request)
            if any(snapshot.error is not None or snapshot.canceled for snapshot in reconciliation_snapshots):
                self.progress_label.setText("Saved image could not be reconciled; delete was not started")
                self._restore_deferred_delete_view(request)
                continue
            catalog = self.workspace.catalog_for_root(request.catalog.root)
            if catalog is not request.catalog:
                self._restore_deferred_delete_view(request)
                continue
            try:
                admitted = self.queue_delete_images(
                    catalog,
                    request.rel_paths,
                    expected_identities=request.expected_identities,
                    expected_proofs=request.expected_proofs,
                    wipe=request.wipe,
                    remove_from_current_view=False,
                    viewer=request.viewer,
                )
            except Exception as error:
                self.progress_label.setText(f"Delete could not be queued: {error}")
                self._restore_deferred_delete_view(request)
                continue
            if not admitted:
                self._restore_deferred_delete_view(request)

    def _restore_deferred_delete_view(self, request: DeferredDeleteRequest) -> None:
        if request.viewer is not None and request.rel_paths:
            request.viewer.image_delete_finished(
                request.rel_paths[0],
                path_removed=False,
            )
        if (
            request.remove_from_current_view
            and self.current_catalog is request.catalog
            and self.workspace.catalog_for_root(request.catalog.root) is request.catalog
        ):
            self.load_current_directory(preserve_selection=True)

    def _settle_image_reconcile_tasks(self) -> None:
        completed: list[tuple[IndexTask, ImageReconcileContext, IndexProgressSnapshot]] = []
        for task, context in list(self._image_reconcile_tasks.items()):
            snapshot = task.snapshot()
            if snapshot.done:
                completed.append((task, context, snapshot))
        for task, context, snapshot in completed:
            self._image_reconcile_tasks.pop(task, None)
            root = context.root
            rel_path = context.rel_path
            self._swept_catalog_roots.discard(root)
            self._pruned_catalog_roots.discard(root)
            self._drop_very_similar_cache(root)
            if snapshot.error is not None or snapshot.canceled:
                self.progress_label.setText(f"Could not update catalog after saving {Path(rel_path).name}")
                if context.attempt + 1 < IMAGE_RECONCILE_MAX_ATTEMPTS:
                    context.attempt += 1
                    context.replace_task = task
                    key = (root, rel_path)
                    self._image_reconcile_retries[key] = context
                    delay = 0 if self._closing else IMAGE_RECONCILE_RETRY_BASE_MS * (2 ** (context.attempt - 1))
                    QTimer.singleShot(
                        delay,
                        lambda key=key, attempt=context.attempt: self._retry_image_reconciliation(
                            key,
                            attempt,
                        ),
                    )
                elif isinstance(context.owner, FullscreenViewer):
                    with suppress(RuntimeError):
                        context.owner.image_save_reconcile_failed(rel_path)
                continue
            self._image_reconcile_retries.pop((root, rel_path), None)
            if isinstance(context.owner, FullscreenViewer):
                with suppress(RuntimeError):
                    context.owner.image_save_reconciled(rel_path)
            if self.current_catalog is not None and self.current_catalog.root == root:
                saved_dir_rel = Path(rel_path).parent.as_posix()
                if saved_dir_rel == ".":
                    saved_dir_rel = ""
                if self.current_virtual_kind is not None or self.current_dir_rel == saved_dir_rel:
                    # An edit can change duplicate/similarity membership and
                    # any size/date/aspect ordering. Refresh the visible
                    # virtual query as well as the matching physical folder;
                    # otherwise the pane behind the viewer remains stale.
                    self.load_current_directory(preserve_selection=True)
        if completed:
            self._flush_deferred_delete_requests()

    def _retry_image_reconciliation(self, key: tuple[Path, str], attempt: int) -> None:
        context = self._image_reconcile_retries.get(key)
        if context is None or context.attempt != attempt:
            return
        if self.workspace.catalog_for_root(context.root) is None:
            self._image_reconcile_retries.pop(key, None)
            if isinstance(context.owner, FullscreenViewer):
                with suppress(RuntimeError):
                    context.owner.image_save_reconcile_failed(context.rel_path)
            self._flush_deferred_delete_requests()
            return
        self._image_reconcile_retries.pop(key, None)
        self._submit_image_reconciliation(context)

    def _submit_pending_image_reconcile_retries(self, root: Path | None = None) -> None:
        resolved = root
        for key, context in list(self._image_reconcile_retries.items()):
            if resolved is not None and context.root != resolved:
                continue
            self._retry_image_reconciliation(key, context.attempt)

    def _retain_failed_image_edit(self, move_task: MovePayloadTask) -> None:
        if not move_task.edit_operations or len(move_task.target_images) != 1:
            return
        root, rel_path = move_task.target_images[0]
        self._failed_image_edits[(root, rel_path)] = move_task.edit_operations
        owner = move_task.edit_owner
        if isinstance(owner, FullscreenViewer):
            with suppress(RuntimeError):
                owner.image_save_failed(rel_path)
                if owner.isVisible() and owner.navigator.current == rel_path:
                    owner.load_current()

    def take_failed_image_edit(self, catalog: Catalog, rel_path: str) -> tuple[EditOperation, ...]:
        return self._failed_image_edits.pop((catalog.root, rel_path), ())

    def retain_failed_image_edit(
        self,
        catalog: Catalog,
        rel_path: str,
        operations: tuple[EditOperation, ...],
    ) -> None:
        self._failed_image_edits[(catalog.root, rel_path)] = operations

    def _queue_post_move_reconciliations(
        self,
        subtrees: set[tuple[Path, str]],
        images: set[tuple[Path, str]],
    ) -> None:
        """Start exact, low-priority indexing after the move has committed."""

        def covered_by_ancestor(rel_path: str, ancestors: set[str]) -> bool:
            candidate = rel_path.rpartition("/")[0]
            while True:
                if candidate in ancestors:
                    return True
                if not candidate:
                    return False
                candidate = candidate.rpartition("/")[0]

        subtree_groups: dict[Path, set[str]] = defaultdict(set)
        for root, dir_rel in subtrees:
            subtree_groups[root].add(dir_rel)
        minimal_subtrees: set[tuple[Path, str]] = set()
        minimal_subtrees_by_root: dict[Path, set[str]] = {}
        for root, root_targets in subtree_groups.items():
            minimal: set[str] = set()
            for dir_rel in sorted(
                root_targets,
                key=lambda value: (value.count("/"), value),
            ):
                if covered_by_ancestor(dir_rel, minimal):
                    continue
                minimal.add(dir_rel)
                minimal_subtrees.add((root, dir_rel))
            minimal_subtrees_by_root[root] = minimal

        image_groups: dict[Path, set[str]] = defaultdict(set)
        for root, rel_path in images:
            if covered_by_ancestor(
                rel_path,
                minimal_subtrees_by_root.get(root, set()),
            ):
                continue
            image_groups[root].add(rel_path)
        contexts = [
            *(
                PostMoveReconcileContext(root, subtree_rel=dir_rel)
                for root, dir_rel in minimal_subtrees
            ),
            *(
                PostMoveReconcileContext(
                    root,
                    image_rels=tuple(sorted(rel_paths)),
                    image_dir_rels=frozenset(
                        rel_path.rpartition("/")[0]
                        for rel_path in rel_paths
                    ),
                )
                for root, rel_paths in image_groups.items()
                if rel_paths
            ),
        ]
        queued = False
        for context in sorted(
            contexts,
            key=lambda item: (
                str(item.root),
                item.subtree_rel is None,
                item.subtree_rel or "",
            ),
        ):
            queued = self._submit_post_move_reconcile_context(context) or queued
        if queued:
            QTimer.singleShot(0, self._poll_indexer)

    def _submit_post_move_reconcile_context(
        self,
        context: PostMoveReconcileContext,
    ) -> bool:
        catalog = self.workspace.catalog_for_root(context.root)
        if catalog is None or self._closing:
            return False
        if (
            context.expected_root_identity is not None
            and catalog.root_identity != context.expected_root_identity
        ) or (
            context.expected_storage_identity is not None
            and catalog.storage_identity != context.expected_storage_identity
        ):
            return False
        if context.expected_root_identity is None:
            context = PostMoveReconcileContext(
                context.root,
                subtree_rel=context.subtree_rel,
                image_rels=context.image_rels,
                image_dir_rels=context.image_dir_rels,
                attempt=context.attempt,
                expected_root_identity=catalog.root_identity,
                expected_storage_identity=catalog.storage_identity,
            )
        self._swept_catalog_roots.discard(context.root)
        self._pruned_catalog_roots.discard(context.root)
        if context.subtree_rel is not None:
            task = self.indexer.refresh_subtree(
                context.root,
                context.subtree_rel,
                interactive=False,
                expected_root_identity=catalog.root_identity,
                expected_storage_identity=catalog.storage_identity,
            )
        else:
            if not context.image_rels:
                return False
            task = self.indexer.reconcile_images(
                context.root,
                context.image_rels,
                interactive=False,
                completion_callback=context.mark_image_completed,
                expected_root_identity=catalog.root_identity,
                expected_storage_identity=catalog.storage_identity,
            )
        self._post_move_reconcile_tasks[task] = context
        return True

    def _retry_post_move_reconcile_context(
        self,
        context: PostMoveReconcileContext,
    ) -> None:
        if self._submit_post_move_reconcile_context(context):
            QTimer.singleShot(0, self._poll_indexer)

    def _settle_post_move_reconcile_tasks(self) -> None:
        reload_current = False
        rebuild_roots: set[Path] = set()
        for task, context in list(self._post_move_reconcile_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._post_move_reconcile_tasks.pop(task, None)
            self._swept_catalog_roots.discard(context.root)
            self._pruned_catalog_roots.discard(context.root)
            self._drop_very_similar_cache(context.root)
            if snapshot.error is not None or snapshot.canceled:
                if context.subtree_rel is not None:
                    retry_context = PostMoveReconcileContext(
                        context.root,
                        subtree_rel=context.subtree_rel,
                        attempt=context.attempt + 1,
                        expected_root_identity=context.expected_root_identity,
                        expected_storage_identity=context.expected_storage_identity,
                    )
                else:
                    # Processor skips and writer publications may finish out of
                    # input order.  Retry only paths lacking an exact callback;
                    # snapshot.processed is a count, not a prefix watermark.
                    remaining = context.unfinished_image_rels()
                    if not remaining:
                        continue
                    retry_context = PostMoveReconcileContext(
                        context.root,
                        image_rels=remaining,
                        image_dir_rels=frozenset(
                            rel_path.rpartition("/")[0]
                            for rel_path in remaining
                        ),
                        attempt=context.attempt + 1,
                        expected_root_identity=context.expected_root_identity,
                        expected_storage_identity=context.expected_storage_identity,
                    )
                if not self._closing:
                    delay_ms = min(
                        30_000,
                        250 * (2 ** min(retry_context.attempt - 1, 7)),
                    )
                    QTimer.singleShot(
                        delay_ms,
                        partial(
                            self._retry_post_move_reconcile_context,
                            retry_context,
                        ),
                    )
                continue
            if context.subtree_rel is not None:
                rebuild_roots.add(context.root)
            if (
                self.current_catalog is not None
                and self.current_catalog.root == context.root
                and self.current_virtual_kind is None
                and context.contains_directory(self.current_dir_rel)
            ):
                reload_current = True
        for root in rebuild_roots:
            catalog = self.workspace.catalog_for_root(root)
            if catalog is not None:
                self._request_incremental_tree_rebuild(
                    catalog,
                    reason="moved_subtree_reconcile",
                )
        if reload_current:
            self.load_current_directory(preserve_selection=True)

    def _active_post_move_reconcile_context(
        self,
        snapshot: IndexProgressSnapshot,
    ) -> PostMoveReconcileContext | None:
        for task, context in self._post_move_reconcile_tasks.items():
            if self._task_is_running(task, (snapshot,)):
                return context
        return None

    def _refresh_after_move_payload(self, affected_roots: set[Path]) -> None:
        for root in affected_roots:
            self._swept_catalog_roots.discard(root)
            self._pruned_catalog_roots.discard(root)
            self._drop_very_similar_cache(root)
        if self.current_catalog is not None and self.current_catalog.root in affected_roots:
            self.reload_tree_and_directory(preserve_tree_scroll=True)
        else:
            for root in affected_roots:
                catalog = self.workspace.catalog_for_root(root)
                if catalog is not None:
                    self._request_incremental_tree_rebuild(catalog, reason="file_move")

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
        self._mark_initial_config_controls_interaction()
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
        self._mark_initial_config_controls_interaction()
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
        row = self.model.first_image_row
        if row is not None:
            if not self.model.ensure_row_loaded(row):
                return QModelIndex()
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
        row = self.current_selected_row()
        total = self.model.image_count
        if row is None:
            text = str(total)
        else:
            record = self.model.images[row]
            if isinstance(record, DirectoryRecord):
                text = str(total)
            else:
                ordinal = self.model.image_ordinal(row)
                text = (
                    f"{ordinal or row + 1} / {total} "
                    f"[{record.width}x{record.height} - {format_bytes(record.size_bytes)}]"
                )
        self.status_left_label.setText(text)

    def copy_selected_files(self) -> None:
        if self.current_catalog is None:
            return
        paths = [
            self.current_catalog.root.joinpath(*Path(rel_path).parts)
            for rel_path in self.selected_rel_paths()
        ]
        copy_files_to_clipboard(paths)

    def delete_selected(
        self,
        *,
        catalog: Catalog | None = None,
        rel_paths: Sequence[str] | None = None,
        expected_incarnations: Mapping[str, ImageRecord] | None = None,
    ) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None:
            return
        selected_rel_paths = list(rel_paths) if rel_paths is not None else self.selected_rel_paths()
        if not selected_rel_paths:
            return
        if self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        selected_incarnations: dict[str, ImageRecord] = dict(
            expected_incarnations or {}
        )
        if rel_paths is None:
            selected_incarnations = {
                record.rel_path: record
                for record in self.selected_records()
                if isinstance(record, ImageRecord)
            }
            if any(
                rel_path not in selected_incarnations
                for rel_path in selected_rel_paths
            ):
                self.progress_label.setText(
                    "Delete canceled because the selected thumbnail changed"
                )
                return
        self._start_delete_confirmation(
            catalog,
            kind="images-main-selection" if rel_paths is None else "images-main-explicit",
            rel_paths=selected_rel_paths,
            owner=self,
            wipe=self.wipe_on_delete_enabled(),
            remove_from_current_view=True,
            expected_incarnations=selected_incarnations,
        )

    def _capture_delete_identities(
        self,
        catalog: Catalog,
        rel_paths: Sequence[str],
    ) -> dict[str, object] | None:
        unique_rel_paths = tuple(dict.fromkeys(rel_paths))
        try:
            identities = dict(catalog.file_identities(unique_rel_paths))
        except (OSError, ValueError) as error:
            show_error(self, "Delete", str(error))
            if self.current_catalog is catalog:
                self.load_current_directory(preserve_selection=True)
            return None
        missing = [rel_path for rel_path in unique_rel_paths if rel_path not in identities]
        if missing:
            names = ", ".join(Path(rel_path).name for rel_path in missing[:3])
            if len(missing) > 3:
                names = f"{names}, and {len(missing) - 3} more"
            show_error(
                self,
                "Delete",
                f"The selected image(s) changed or disappeared before confirmation: {names}",
            )
            if self.current_catalog is catalog:
                self.load_current_directory(preserve_selection=True)
            return None
        return identities

    def queue_delete_images(
        self,
        catalog: Catalog,
        rel_paths: Sequence[str],
        *,
        expected_identities: Mapping[str, object] | None = None,
        expected_proofs: Mapping[str, object] | None = None,
        wipe: bool,
        remove_from_current_view: bool,
        viewer: FullscreenViewer | None = None,
    ) -> bool:
        unique_rel_paths = list(dict.fromkeys(rel_paths))
        if not unique_rel_paths:
            return False
        if viewer is not None and len(unique_rel_paths) != 1:
            raise ValueError("a viewer delete must contain exactly one image")
        expected_proof_map = {
            rel_path: expected_proofs[rel_path]
            for rel_path in unique_rel_paths
            if expected_proofs is not None and rel_path in expected_proofs
        }
        if expected_identities is None:
            rels_needing_identity = [
                rel_path for rel_path in unique_rel_paths if rel_path not in expected_proof_map
            ]
            if rels_needing_identity:
                self.progress_label.setText(
                    "Delete canceled because confirmation identities were not supplied"
                )
                return False
            expected_identity_map = {}
        else:
            expected_identity_map = {
                rel_path: expected_identities[rel_path]
                for rel_path in unique_rel_paths
                if rel_path in expected_identities
            }
        missing_authorization = [
            rel_path
            for rel_path in unique_rel_paths
            if rel_path not in expected_identity_map and rel_path not in expected_proof_map
        ]
        if missing_authorization:
            self.progress_label.setText("Delete canceled because an image proof was unavailable")
            if self.current_catalog is catalog:
                self.load_current_directory(preserve_selection=True)
            return False
        root = catalog.root
        selected_rel_paths = set(unique_rel_paths)
        dependencies = tuple(
            mutation
            for mutation in self._move_payload_tasks
            if any(
                target_root == root and target_rel_path in selected_rel_paths
                for target_root, target_rel_path in mutation.target_images
            )
        )
        reconciliation_contexts = tuple(
            (task, context)
            for task, context in self._image_reconcile_tasks.items()
            if context.root == root and context.rel_path in selected_rel_paths
        )
        retry_reconciliation_contexts = tuple(
            (context.replace_task, context)
            for (task_root, task_rel_path), context in self._image_reconcile_retries.items()
            if task_root == root
            and task_rel_path in selected_rel_paths
            and context.replace_task is not None
        )
        # Once an edit has committed, its byte proof supersedes the cheap
        # pre-save identity captured by an overlapping delete confirmation.
        # This also covers the window after the save future settled but while
        # catalog reconciliation is still active or waiting to retry.
        for _task, context in (*reconciliation_contexts, *retry_reconciliation_contexts):
            if context.expected_proof is None:
                continue
            expected_identity_map.pop(context.rel_path, None)
            expected_proof_map[context.rel_path] = context.expected_proof
        reconciliation_tasks = tuple(task for task, _context in reconciliation_contexts)
        retry_reconciliation_tasks = tuple(
            task for task, _context in retry_reconciliation_contexts if task is not None
        )
        reconciliation_tasks = tuple(dict.fromkeys((*reconciliation_tasks, *retry_reconciliation_tasks)))
        if dependencies or reconciliation_tasks:
            self._deferred_delete_requests.append(
                DeferredDeleteRequest(
                    catalog=catalog,
                    rel_paths=tuple(unique_rel_paths),
                    expected_identities=expected_identity_map,
                    expected_proofs=expected_proof_map,
                    wipe=wipe,
                    remove_from_current_view=remove_from_current_view,
                    dependencies=dependencies,
                    reconciliation_tasks=reconciliation_tasks,
                    viewer=viewer,
                )
            )
            if viewer is not None:
                viewer.image_delete_started(unique_rel_paths[0])
            if remove_from_current_view:
                self._remove_records_from_current_view(root, image_rels=selected_rel_paths)
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText("Saving image before delete")
            return True
        self.indexer.cancel_idle_tasks(root)
        self.indexer.cancel_directory_tasks(root)
        self._swept_catalog_roots.discard(root)
        self._pruned_catalog_roots.discard(root)
        self._drop_very_similar_cache(root)
        outcome = DeletePayloadOutcome(tuple(unique_rel_paths))
        task, future = self.indexer.submit_action(
            "Deleting images",
            root,
            None,
            priority=ActionPriority.FILE_DELETE,
            worker=lambda action_task: self._delete_images_worker(
                root,
                unique_rel_paths,
                wipe,
                expected_identity_map,
                expected_proof_map,
                action_task,
                outcome,
            ),
            key=f"delete:{root}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        delete_task = DeletePayloadTask(
            root=root,
            rel_paths=tuple(unique_rel_paths),
            expected_identities=expected_identity_map,
            expected_proofs=expected_proof_map,
            task=task,
            future=future,
            started_at=monotonic(),
            outcome=outcome,
            viewer=viewer,
        )
        self._delete_payload_tasks.append(delete_task)
        self._refresh_active_delete_payload_task()
        if viewer is not None:
            viewer.image_delete_started(unique_rel_paths[0])
        if remove_from_current_view:
            self._remove_records_from_current_view(root, image_rels=set(unique_rel_paths))
        if self._delete_payload_task is not None:
            self._show_delete_payload_status(self._delete_payload_task.task.snapshot())
        self._update_tools_menu_actions()
        return True

    def _delete_images_worker(
        self,
        root: Path,
        rel_paths: Sequence[str],
        wipe: bool,
        expected_identities: Mapping[str, object],
        expected_proofs: Mapping[str, object],
        task: IndexTask,
        outcome: DeletePayloadOutcome,
    ) -> DeletePayloadResult:
        def remaining_paths(catalog: Catalog) -> tuple[str, ...] | None:
            remaining: list[str] = []
            for rel_path in rel_paths:
                try:
                    catalog.file_identity(rel_path)
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                remaining.append(rel_path)
            return tuple(remaining)

        try:
            with Catalog.open_writer(
                root,
                expected_root_identity=task.expected_root_identity,
                expected_storage_identity=task.expected_storage_identity,
            ) as catalog:
                try:
                    deleted = catalog.delete_images(
                        rel_paths,
                        wipe=wipe,
                        expected_identities=expected_identities,
                        expected_proofs=expected_proofs,
                        progress_callback=task.update,
                        cancel_check=task.check_canceled,
                    )
                except IndexTaskCancelled as error:
                    remaining_rel_paths = remaining_paths(catalog)
                    outcome.remaining_rel_paths = remaining_rel_paths
                    task.mark_canceled()
                    return DeletePayloadResult(
                        requested=len(rel_paths),
                        deleted=(
                            0
                            if remaining_rel_paths is None
                            else len(rel_paths) - len(remaining_rel_paths)
                        ),
                        affected_roots={root},
                        remaining_rel_paths=remaining_rel_paths,
                        error=error,
                        canceled=True,
                    )
                except Exception as error:
                    remaining_rel_paths = remaining_paths(catalog)
                    outcome.remaining_rel_paths = remaining_rel_paths
                    task.mark_failed(error)
                    return DeletePayloadResult(
                        requested=len(rel_paths),
                        deleted=(
                            0
                            if remaining_rel_paths is None
                            else len(rel_paths) - len(remaining_rel_paths)
                        ),
                        affected_roots={root},
                        remaining_rel_paths=remaining_rel_paths,
                        error=error,
                    )
                remaining_rel_paths = remaining_paths(catalog)
                outcome.remaining_rel_paths = remaining_rel_paths
            task.mark_done()
            return DeletePayloadResult(
                requested=len(rel_paths),
                deleted=deleted,
                affected_roots={root},
                remaining_rel_paths=remaining_rel_paths,
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
            self.delete_selected(
                catalog=catalog,
                rel_paths=selected_rel_paths,
                expected_incarnations={
                    item.rel_path: item
                    for item in selected_records
                    if isinstance(item, ImageRecord)
                },
            )
        elif selected == actions.get("metadata") and isinstance(record, ImageRecord):
            dialog = MetadataDialog(record.absolute_path, self)
            dialog.exec()
            dialog.deleteLater()

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
        dialog = DuplicateListDialog(
            catalog,
            record,
            None,
            lambda rel_path: self.navigate_to_image(rel_path, catalog=catalog),
            self,
        )
        dialog.exec()
        dialog.deleteLater()

    def open_tag_dialog_for_selection(self) -> None:
        if self.current_catalog is None:
            return
        rel_paths = self.selected_rel_paths()
        if len(rel_paths) != 1:
            return
        catalog = self.current_catalog
        rel_path = rel_paths[0]
        dialog = TagDialog(catalog, rel_path, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        selected_tags = dialog.selected_tags() if accepted else []
        expected_identity = getattr(dialog, "loaded_file_identity", None)
        dialog.deleteLater()
        if accepted:
            if self.workspace.catalog_for_root(catalog.root) is not catalog:
                return
            if expected_identity is None:
                self.progress_label.setText(
                    "Image tags were not changed because the image identity was unavailable"
                )
                return
            self.queue_image_tags(
                catalog,
                rel_path,
                selected_tags,
                expected_identity=expected_identity,
            )

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
        complete_order = self.model.complete_image_order
        if complete_order is not None:
            order = complete_order
            first_image_row = self.model.first_image_row or 0
            start_index = index.row() - first_image_row
        else:
            order = [
                record.rel_path
                for record in self.model.images
                if isinstance(record, ImageRecord)
            ]
            start_index = order.index(selected_record.rel_path)
        start = selected_record.rel_path
        catalog = self.current_catalog
        if self.model.is_paged and self.current_virtual_kind in {
            None,
            VIRTUAL_KIND_TAG,
            VIRTUAL_KIND_DUPLICATES,
        }:
            initial = (
                ImageNavigator.random(order, start).order
                if random_mode
                else list(order)
            )
            kind = self.current_virtual_kind or VIRTUAL_KIND_PHYSICAL
            value = (
                self.current_dir_rel
                if kind == VIRTUAL_KIND_PHYSICAL
                else self.current_virtual_value
            )
            navigator: ImageNavigator | PagedImageNavigator = PagedImageNavigator(
                order=initial,
                index=0 if random_mode else initial.index(start),
                next_offset=self.model.next_page_offset,
                has_more=self.model.may_have_more_pages,
                total_count=self.model.image_count,
                page_loader=partial(
                    self._viewer_navigation_page_worker,
                    catalog.root,
                    catalog.root_identity,
                    catalog.storage_identity,
                    kind,
                    value,
                    self.current_sort,
                ),
                random_mode=random_mode,
                view_kind=kind,
            )
        else:
            navigator = ImageNavigator.random(order, start) if random_mode else ImageNavigator(
                order=order,
                index=start_index,
            )
        tree_scroll_position = self._tree_scroll_position()
        viewer = FullscreenViewer(
            catalog,
            navigator,
            self,
            wipe_on_delete=self.wipe_on_delete_enabled(),
        )
        last_viewed: str | None = None
        try:
            viewer.exec_fullscreen()
            last_viewed = viewer.last_viewed_rel_path
        finally:
            viewer.deleteLater()
            if last_viewed is not None and self.current_catalog is catalog:
                self.select_rel_path(last_viewed)
            # Modal activation and any discovery publication that completed
            # behind the viewer must not repurpose the user's tree viewport.
            self._restore_tree_scroll_position(tree_scroll_position)
            QTimer.singleShot(
                0,
                lambda position=tree_scroll_position: self._restore_tree_scroll_position(
                    position
                ),
            )

    def navigate_to_directory(self, dir_rel: str, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        intent_sequence = self._record_catalog_intent(catalog.root)
        self.current_catalog = catalog
        self._current_catalog_intent_sequence = intent_sequence
        self.current_dir_rel = dir_rel
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self._ensure_current_tree_path(catalog, dir_rel)
        self.load_current_directory()
        self.queue_directory_index(catalog, dir_rel)

    def navigate_to_image(self, rel_path: str, *, catalog: Catalog | None = None) -> None:
        catalog = catalog or self.current_catalog
        if catalog is None or self.workspace.catalog_for_root(catalog.root) is not catalog:
            return
        if self.current_catalog is not None:
            self._cancel_virtual_view_tasks(self.current_catalog.root)
        intent_sequence = self._record_catalog_intent(catalog.root)
        self.current_catalog = catalog
        self._current_catalog_intent_sequence = intent_sequence
        dir_rel = Path(rel_path).parent.as_posix()
        self.current_dir_rel = "" if dir_rel == "." else dir_rel
        self.current_virtual_kind = None
        self.current_virtual_value = ""
        self._ensure_current_tree_path(catalog, self.current_dir_rel)
        self.load_current_directory()
        self.select_rel_path(rel_path)

    def move_payload_to_directory(self, payload: object, dest_root: Path, dest_dir_rel: str) -> None:
        self._settle_move_payload_task()
        dest_catalog = self.workspace.catalog_for_exact_root(dest_root.expanduser().absolute())
        if dest_catalog is None or not self._valid_payload_rel_path(dest_dir_rel, allow_root=True):
            return
        if not isinstance(payload, list):
            return
        image_groups: dict[Path, list[str]] = defaultdict(list)
        directory_groups: dict[Path, list[str]] = defaultdict(list)
        source_catalogs: dict[Path, Catalog] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                source_root = Path(item["catalog_root"]).expanduser().absolute()
                rel_path = item["rel_path"]
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if not isinstance(rel_path, str) or not self._valid_payload_rel_path(rel_path):
                continue
            source_catalog = self.workspace.catalog_for_exact_root(source_root)
            if source_catalog is None:
                continue
            source_root = source_catalog.root
            source_catalogs[source_root] = source_catalog
            kind = item.get("kind", "image")
            if kind not in {"image", "directory"}:
                continue
            if kind == "directory":
                if not rel_path:
                    continue
                group = directory_groups
            else:
                if not is_image_name(Path(rel_path).name):
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
        for source_root, pending_target in (
            (source_root, pending_target)
            for source_root in {*image_payload, *directory_payload}
            for pending_target in self._pending_image_save_targets(source_root)
        ):
            if pending_target in image_payload.get(source_root, ()) or any(
                pending_target == directory_rel or pending_target.startswith(f"{directory_rel}/")
                for directory_rel in directory_payload.get(source_root, ())
            ):
                self.progress_label.setText("Wait for the pending image save before moving it")
                return
        if is_trash_rel_path(dest_dir_rel):
            source_roots = {*directory_payload.keys(), *image_payload.keys()}
            if any(source_root != dest_catalog.root for source_root in source_roots):
                show_error(self, "Move", "Cannot move items into another catalog's trash.")
                return
        affected_roots = {dest_catalog.root, *directory_payload.keys(), *image_payload.keys()}
        catalog_context = {
            root: source_catalogs[root]
            for root in {*directory_payload, *image_payload}
        }
        catalog_context[dest_catalog.root] = dest_catalog
        try:
            future = self.identity_executor.submit(
                self._capture_move_identities_worker,
                image_payload,
                directory_payload,
                dest_catalog.root,
                dest_dir_rel,
                {
                    root: context_catalog.root_identity
                    for root, context_catalog in catalog_context.items()
                },
                {
                    root: context_catalog.storage_identity
                    for root, context_catalog in catalog_context.items()
                },
            )
        except RuntimeError:
            self._show_identity_preflight_busy("move")
            return
        preflight = MoveIdentityPreflightTask(
            generation=self._next_mutation_identity_generation(),
            dest_catalog=dest_catalog,
            catalog_context=catalog_context,
            dest_dir_rel=dest_dir_rel,
            image_payload=image_payload,
            directory_payload=directory_payload,
            affected_roots=set(affected_roots),
            wipe_on_delete=self.wipe_on_delete_enabled(),
            future=future,
        )
        self._move_identity_preflights[future] = preflight
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("Checking items before move")

    @staticmethod
    def _valid_payload_rel_path(rel_path: str, *, allow_root: bool = False) -> bool:
        if not rel_path:
            return allow_root
        path = Path(rel_path)
        if path.is_absolute() or path.as_posix() != rel_path:
            return False
        if any(part in {"", ".", ".."} for part in path.parts):
            return False
        return not path.parts or path.parts[0] != ".marnwick"

    def _settle_move_identity_preflights(self) -> None:
        for future, preflight in list(self._move_identity_preflights.items()):
            if not future.done():
                continue
            self._move_identity_preflights.pop(future, None)
            if future.cancelled() or not self._move_identity_preflight_is_current(preflight):
                continue
            try:
                captured = future.result()
            except Exception as error:
                self._mutation_identity_preflight_failed(
                    "Move",
                    error,
                    preflight.affected_roots,
                )
                continue
            self._enqueue_preflighted_move(preflight, captured)

    def _move_identity_preflight_is_current(
        self,
        preflight: MoveIdentityPreflightTask,
    ) -> bool:
        if preflight.generation > self._mutation_identity_generation:
            return False
        return all(
            self.workspace.catalog_for_exact_root(root) is catalog
            for root, catalog in preflight.catalog_context.items()
        )

    def _enqueue_preflighted_move(
        self,
        preflight: MoveIdentityPreflightTask,
        captured: MutationIdentityResult,
    ) -> None:
        image_payload = preflight.image_payload
        directory_payload = preflight.directory_payload
        dest_catalog = preflight.dest_catalog
        dest_dir_rel = preflight.dest_dir_rel
        affected_roots = preflight.affected_roots
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
                captured.image_identities,
                captured.directory_identities,
                captured.destination_identity,
                preflight.wipe_on_delete,
                {
                    root: context_catalog.root_identity
                    for root, context_catalog in preflight.catalog_context.items()
                },
                {
                    root: context_catalog.storage_identity
                    for root, context_catalog in preflight.catalog_context.items()
                },
                action_task,
            ),
            key=f"move:{dest_catalog.root}:{dest_dir_rel}:{monotonic()}",
            interactive=True,
            force_refresh=True,
            preemptible=False,
            expected_root_identity=dest_catalog.root_identity,
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
            expected_image_identities=captured.image_identities,
            expected_directory_identities=captured.directory_identities,
            expected_destination_identity=captured.destination_identity,
        )
        self._move_payload_tasks.append(move_task)
        self._refresh_active_move_payload_task()
        navigated_from_moved_directory = False
        if self.current_catalog is not None:
            current_root = self.current_catalog.root
            for source_dir_rel in directory_payload.get(current_root, ()):
                if self.current_dir_rel == source_dir_rel or self.current_dir_rel.startswith(f"{source_dir_rel}/"):
                    parent_rel = Path(source_dir_rel).parent.as_posix()
                    self.current_dir_rel = "" if parent_rel == "." else parent_rel
                    self.current_virtual_kind = None
                    self.current_virtual_value = ""
                    navigated_from_moved_directory = True
                    break
        if navigated_from_moved_directory:
            self.load_current_directory(preserve_selection=True)
        self._remove_queued_move_records_from_current_view(image_payload, directory_payload)
        if self._move_payload_task is not None:
            self._show_move_payload_status(self._move_payload_task.task.snapshot())
        self._update_tools_menu_actions()

    def _mutation_identity_preflight_failed(
        self,
        title: str,
        error: BaseException,
        affected_roots: set[Path],
    ) -> None:
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Ready")
        show_error(self, title, str(error))
        if (
            self.current_catalog is not None
            and self.current_catalog.root in affected_roots
        ):
            self.load_current_directory(preserve_selection=True)

    def _cancel_mutation_identity_preflights(self, root: Path | None = None) -> None:
        for future, preflight in list(self._move_identity_preflights.items()):
            if root is not None and root not in preflight.affected_roots:
                continue
            self._move_identity_preflights.pop(future, None)
            future.cancel()
        for future, preflight in list(self._restore_identity_preflights.items()):
            if root is not None and preflight.catalog.root != root:
                continue
            self._restore_identity_preflights.pop(future, None)
            future.cancel()

    def _pending_image_save_targets(self, root: Path) -> set[str]:
        resolved = root
        targets = {
            rel_path
            for mutation in self._move_payload_tasks
            for target_root, rel_path in mutation.target_images
            if target_root == resolved
        }
        targets.update(
            context.rel_path
            for context in self._image_reconcile_tasks.values()
            if context.root == resolved
        )
        targets.update(
            context.rel_path
            for (target_root, _), context in self._image_reconcile_retries.items()
            if target_root == resolved
        )
        return targets

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
        expected_image_identities: Mapping[Path, Mapping[str, object]],
        expected_directory_identities: Mapping[Path, Mapping[str, object]],
        expected_destination_identity: object | None,
        wipe_on_delete: bool,
        expected_root_identities: Mapping[Path, tuple[int, int]],
        expected_storage_identities: Mapping[Path, CatalogStorageIdentity],
        task: IndexTask,
    ) -> MovePayloadResult:
        affected_roots = {dest_root, *directory_groups.keys(), *image_groups.keys()}
        requested = sum(len(items) for items in directory_groups.values()) + sum(
            len(items) for items in image_groups.values()
        )
        processed = 0
        moved = 0
        catalogs: dict[Path, Catalog] = {}
        reconcile_subtrees: list[tuple[Path, str]] = []
        reconcile_images: list[tuple[Path, str]] = []

        def catalog_for(root: Path) -> Catalog:
            resolved = root.expanduser().absolute()
            catalog = catalogs.get(resolved)
            if catalog is None:
                catalog = Catalog.open_writer(
                    resolved,
                    expected_root_identity=expected_root_identities.get(resolved),
                    expected_storage_identity=expected_storage_identities.get(resolved),
                )
                catalogs[resolved] = catalog
            return catalog

        task.update(0, requested, dest_dir_rel or ".")
        try:
            dest_catalog = catalog_for(dest_root)
            if (
                not isinstance(expected_destination_identity, tuple)
                or len(expected_destination_identity) < 2
                or not all(isinstance(value, int) for value in expected_destination_identity[:2])
            ):
                raise OSError("destination identity was not captured before move")
            # Directory mtimes/ctimes change whenever one admitted move adds a
            # child.  Pin the destination object itself, not its mutable
            # contents, so two independently confirmed moves to the same
            # directory can serialize successfully while replacement of that
            # directory is still rejected.
            expected_destination_object = expected_destination_identity[:2]
            for source_root in {*directory_groups, *image_groups}:
                source_catalog = catalog_for(source_root)
                missing_images = [
                    rel_path
                    for rel_path in image_groups.get(source_root, ())
                    if rel_path not in expected_image_identities.get(source_root, {})
                ]
                missing_directories = [
                    rel_path
                    for rel_path in directory_groups.get(source_root, ())
                    if rel_path not in expected_directory_identities.get(source_root, {})
                ]
                if missing_images or missing_directories:
                    missing = [*missing_images, *missing_directories]
                    raise OSError(
                        "identity was not captured before move: "
                        + ", ".join(missing[:3])
                    )
                self._verify_expected_mutation_identities(
                    source_catalog,
                    expected_image_identities.get(source_root, {}),
                    expected_directory_identities.get(source_root, {}),
                    verb="move",
                )
            for source_root, dir_rels in directory_groups.items():
                source_catalog = catalog_for(source_root)
                if dest_catalog.directory_identity(dest_dir_rel)[:2] != expected_destination_object:
                    raise OSError(
                        f"destination changed after move was requested: {dest_dir_rel or '.'}"
                    )
                base_processed = processed

                def directory_progress(local_processed: int, _total: int | None, current: str) -> None:
                    task.update(min(base_processed + local_processed, requested), requested, current)

                results = source_catalog.move_directories(
                    dir_rels,
                    dest_catalog,
                    dest_dir_rel,
                    wipe_on_delete=wipe_on_delete,
                    expected_identities=expected_directory_identities.get(source_root, {}),
                    progress_callback=directory_progress,
                    cancel_check=task.check_canceled,
                )
                processed += len(dir_rels)
                moved += len(results)
                reconcile_subtrees.extend(
                    (result.dest_catalog_root, result.dest_rel_path)
                    for result in results
                )
                task.update(min(processed, requested), requested, dest_dir_rel or ".")
            for source_root, rel_paths in image_groups.items():
                source_catalog = catalog_for(source_root)
                if dest_catalog.directory_identity(dest_dir_rel)[:2] != expected_destination_object:
                    raise OSError(
                        f"destination changed after move was requested: {dest_dir_rel or '.'}"
                    )
                base_processed = processed

                def image_progress(local_processed: int, _total: int | None, current: str) -> None:
                    task.update(min(base_processed + local_processed, requested), requested, current)

                results = source_catalog.move_images(
                    rel_paths,
                    dest_catalog,
                    dest_dir_rel,
                    wipe_on_delete=wipe_on_delete,
                    expected_identities=expected_image_identities.get(source_root, {}),
                    progress_callback=image_progress,
                    cancel_check=task.check_canceled,
                )
                processed += len(rel_paths)
                moved += len(results)
                reconcile_images.extend(
                    (result.dest_catalog_root, result.dest_rel_path)
                    for result in results
                )
                task.update(min(processed, requested), requested, dest_dir_rel or ".")
            task.mark_done()
            return MovePayloadResult(
                requested=requested,
                moved=moved,
                affected_roots=affected_roots,
                reconcile_subtrees=tuple(dict.fromkeys(reconcile_subtrees)),
                reconcile_images=tuple(dict.fromkeys(reconcile_images)),
            )
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

    @staticmethod
    def _verify_expected_mutation_identities(
        catalog: Catalog,
        expected_images: Mapping[str, object],
        expected_directories: Mapping[str, object],
        *,
        verb: str,
    ) -> None:
        if expected_images:
            current_images = catalog.file_identities(expected_images)
            for rel_path, expected_identity in expected_images.items():
                if current_images.get(rel_path) != expected_identity:
                    raise OSError(f"image changed after {verb} was requested: {rel_path}")
        for dir_rel, expected_identity in expected_directories.items():
            if catalog.directory_identity(dir_rel) != expected_identity:
                raise OSError(f"directory changed after {verb} was requested: {dir_rel}")

    def _remove_records_from_current_view(
        self,
        root: Path,
        *,
        image_rels: set[str] | None = None,
        directory_rels: set[str] | None = None,
    ) -> None:
        if self.current_catalog is None or self.current_catalog.root != root:
            return
        image_rels = image_rels or set()
        directory_rels = directory_rels or set()
        if not image_rels and not directory_rels:
            return
        if (
            self.current_virtual_kind is None
            and self.model.has_complete_row_map
            and len(self.model.images) > THUMBNAIL_MODEL_BATCH_SIZE
        ):
            # The mutation task/deferred request is registered before this
            # method runs, so a fresh worker snapshot captures its exclusion
            # set. Filter the already-owned snapshot off Qt so the accepted
            # source disappears promptly without another slow filesystem scan.
            fingerprint = self._physical_pane_generation
            active_preview = self._matching_virtual_view_task(
                root,
                VIRTUAL_KIND_PHYSICAL_PREVIEW,
                self.current_dir_rel,
                self.current_sort,
                fingerprint,
            )
            if active_preview is not None:
                active_preview.cancel_event.set()
                active_preview.future.cancel()
                self._virtual_view_tasks.pop(active_preview.future, None)
            self._submit_filtered_physical_snapshot_task(
                self.current_catalog,
                self.current_dir_rel,
                self.current_sort,
                fingerprint,
                self._thumbnail_selection_keys()
                - {("image", rel_path) for rel_path in image_rels}
                - {("directory", rel_path) for rel_path in directory_rels},
                None,
                self._current_thumbnail_scroll_key(),
            )
            return
        records = list(self.model.images)
        remove_flags: list[bool] = []
        filtered: list[PaneRecord] = []
        removed_rows: list[int] = []

        def inside_removed_directory(rel_path: str) -> bool:
            candidate = rel_path
            while candidate:
                if candidate in directory_rels:
                    return True
                parent = candidate.rpartition("/")[0]
                if parent == candidate:
                    break
                candidate = parent
            return "" in directory_rels

        for row, record in enumerate(records):
            rel_path = record.rel_path
            remove = (
                (isinstance(record, ImageRecord) and rel_path in image_rels)
                or inside_removed_directory(rel_path)
            )
            remove_flags.append(remove)
            if remove:
                removed_rows.append(row)
                continue
            filtered.append(record)
        if not removed_rows:
            return
        anchor_key = self._thumbnail_anchor_key_after_removal(records, remove_flags, removed_rows)
        self.model.replace_loaded_records(filtered)
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
        row = self.model.row_for_key(key)
        if row is None:
            return False
        if not self.model.ensure_row_loaded(row):
            return False
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

    def select_rel_path(self, rel_path: str) -> None:
        selected = self._select_thumbnail_record_key(("image", rel_path))
        catalog = self.current_catalog
        pane_load_pending = any(
            task.kind == VIRTUAL_KIND_PHYSICAL
            and self._is_current_virtual_task(task)
            and task.fingerprint == self._physical_pane_generation
            and not task.cancel_event.is_set()
            and not task.future.cancelled()
            for task in self._virtual_view_tasks.values()
        )
        if catalog is not None and (not selected or pane_load_pending):
            self._pending_rel_path_selection = (
                catalog.root,
                rel_path,
                self._physical_pane_generation,
            )
            if not pane_load_pending and self.model.canFetchMore():
                QTimer.singleShot(0, self.model.fetchMore)
            return
        self._pending_rel_path_selection = None

    def _apply_pending_rel_path_selection(
        self,
        pane_generation: int,
        *,
        clear_on_success: bool = True,
    ) -> None:
        pending = self._pending_rel_path_selection
        if pending is None:
            return
        root, rel_path, generation = pending
        if generation != pane_generation:
            if generation < pane_generation:
                self._pending_rel_path_selection = None
            return
        if self.current_catalog is None or self.current_catalog.root != root:
            self._pending_rel_path_selection = None
            return
        if self._select_thumbnail_record_key(("image", rel_path)) and clear_on_success:
            self._pending_rel_path_selection = None

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
        task = self.indexer.refresh_directory(
            catalog.root,
            dir_rel,
            interactive=interactive,
            force=force,
            expected_root_identity=catalog.root_identity,
            expected_storage_identity=catalog.storage_identity,
        )
        self._directory_index_tasks[(catalog.root, dir_rel)] = task
        if interactive:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(f"Indexing {dir_rel or catalog.root.name or catalog.root}")
        QTimer.singleShot(0, self._poll_indexer)

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
        if self._move_identity_preflights or self._restore_identity_preflights:
            return
        if self._active_virtual_view_task() is not None:
            return
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_post_move_reconcile_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        self._settle_thumbnail_repair_tasks()
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
                expected_root_identity=catalog.root_identity,
                expected_storage_identity=catalog.storage_identity,
            )
            return
        for catalog in self.workspace.catalogs:
            if catalog.root not in self._swept_catalog_roots:
                self._idle_index_tasks[catalog.root] = self.indexer.refresh_catalog(
                    catalog.root,
                    interactive=False,
                    expected_root_identity=catalog.root_identity,
                    expected_storage_identity=catalog.storage_identity,
                )
                return
        for catalog in self.workspace.catalogs:
            if catalog.root not in self._pruned_catalog_roots:
                self._thumbnail_prune_tasks[catalog.root] = self.indexer.prune_thumbnails(
                    catalog.root,
                    interactive=False,
                    expected_root_identity=catalog.root_identity,
                    expected_storage_identity=catalog.storage_identity,
                )
                return

    def _poll_indexer(self) -> None:
        if self.thumbnail_view.manual_drag_active():
            # The drag payload is already captured, but both thumbnail-folder
            # drop targets and tree rows must stay geometrically stable until
            # the release position has been resolved.
            return
        self._settle_initial_config_load(self._initial_config_load_generation)
        self._settle_config_saves()
        if not self._tree_publication_blocked():
            self._settle_tree_children_tasks()
            self._settle_tree_tags_tasks()
        if (
            self.current_catalog is not None
            and self.workspace.catalog_for_root(self.current_catalog.root) is not self.current_catalog
        ):
            self.current_catalog = None
            self.current_dir_rel = ""
            self.current_virtual_kind = None
            self.current_virtual_value = ""
            self.model.set_images(None, [])
        drain_completed = getattr(self.indexer, "drain_completed_snapshots", None)
        if callable(drain_completed):
            drain_completed()
        self._settle_catalog_open_tasks()
        self._settle_delete_confirmations()
        self._settle_move_identity_preflights()
        self._settle_restore_identity_preflights()
        self._settle_duplicate_delete_task()
        self._settle_delete_payload_task()
        self._settle_move_payload_task()
        self._settle_image_reconcile_tasks()
        self._settle_post_move_reconcile_tasks()
        self._flush_deferred_delete_requests()
        self._settle_virtual_view_tasks()
        self._settle_directory_discovery_tasks()
        self._settle_directory_index_tasks()
        self._settle_idle_tasks()
        self._settle_thumbnail_prune_tasks()
        self._settle_thumbnail_repair_tasks()
        active_open_task = self._active_catalog_open_task()
        if active_open_task is not None:
            self._show_catalog_open_status(active_open_task.root)
            return
        if self._move_identity_preflights or self._restore_identity_preflights:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(
                "Checking items before move"
                if self._move_identity_preflights
                else "Checking items before restore"
            )
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
        if active_move_task is not None and (
            active_move_task.dedicated_executor
            or self._task_is_running(active_move_task.task, snapshots)
        ):
            self._show_move_payload_status(active_move_task.task.snapshot())
            return
        active_virtual_task = self._active_virtual_view_task()
        visible_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot.interactive or not snapshot.label.startswith("Pruning thumbnails")
        ]
        if active_virtual_task is not None and active_virtual_task.kind != VIRTUAL_KIND_PHYSICAL:
            self._show_virtual_view_status(active_virtual_task)
            return
        if not visible_snapshots:
            self._indexing_was_active = False
            self._schedule_idle_indexing()
            active_virtual_task = self._active_virtual_view_task()
            if active_virtual_task is not None:
                self._show_virtual_view_status(active_virtual_task)
                return
            if self._physical_preview_retry_generation == self._physical_pane_generation:
                self.progress_bar.setRange(0, 0)
                self.progress_label.setText("Listing folder (waiting for a reader)")
                return
            if self._tree_build_task is not None:
                self._show_tree_build_status(self._tree_build_task)
                return
            if snapshots:
                return
            if self._unavailable_catalog_paths:
                self.progress_bar.setRange(0, 1)
                self.progress_bar.setValue(0)
                if len(self._unavailable_catalog_paths) == 1:
                    unavailable = Path(self._unavailable_catalog_paths[0]).name or self._unavailable_catalog_paths[0]
                    self.progress_label.setText(f"Could not restore catalog {unavailable}")
                else:
                    self.progress_label.setText(
                        f"Could not restore {len(self._unavailable_catalog_paths)} catalogs"
                    )
                return
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            self.progress_label.setText("Ready")
            self.statusBar().clearMessage()
            return

        self._indexing_was_active = True
        snapshot = sorted(visible_snapshots, key=lambda item: (item.interactive, item.started_at), reverse=True)[0]
        post_move_context = self._active_post_move_reconcile_context(snapshot)
        post_move_affects_current = (
            post_move_context is not None
            and self.current_catalog is not None
            and post_move_context.root == self.current_catalog.root
            and self.current_virtual_kind is None
            and post_move_context.contains_directory(self.current_dir_rel)
        )
        if (
            self.current_catalog is not None
            and self.current_virtual_kind is None
            and snapshot.root == self.current_catalog.root
            and (
                snapshot.dir_rel == self.current_dir_rel
                or post_move_affects_current
            )
        ):
            self.model.refresh_pending_thumbnails()
            if post_move_affects_current:
                self._request_physical_progress_reload(
                    self.current_catalog.root,
                    self.current_dir_rel,
                )
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
                indexed_row = self.model.row_for_key(("image", snapshot.current))
                if indexed_row is not None:
                    self.model.refresh_thumbnail(snapshot.current)
                # Thumbnail workers perform an exact rel-path lookup, so each
                # newly committed thumbnail can repaint its existing row. Do
                # not requery/rebuild a sorted database prefix on every 200 ms
                # progress tick; one bounded metadata overlay runs at task
                # completion instead.
        self.progress_label.setText(detail)

    def _task_is_running(self, task: IndexTask, snapshots: Sequence[IndexProgressSnapshot]) -> bool:
        return any(
            snapshot.started_at == task.started_at
            and snapshot.label == task.label
            and snapshot.root == task.root
            for snapshot in snapshots
        )

    def _show_tree_build_status(self, task: TreeBuildTask) -> None:
        if task.page_future is not None and not task.page_future.done():
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(
                f"Loading folder tree: {task.processed} folders ready"
            )
            return
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
                self._drop_very_similar_cache(root)
                self._swept_catalog_roots.add(root)
                if self.current_catalog is not None and self.current_catalog.root == root:
                    self.load_current_directory(preserve_selection=True)
                if snapshot.interactive or task.force_refresh:
                    catalog = self.workspace.catalog_for_root(root)
                    if catalog is not None:
                        self._request_incremental_tree_rebuild(catalog, reason="catalog_refresh")

    def _settle_directory_index_tasks(self) -> None:
        completed_roots: set[Path] = set()
        changed_roots: set[Path] = set()
        reload_current = False
        for key, task in list(self._directory_index_tasks.items()):
            snapshot = task.snapshot()
            if not snapshot.done:
                continue
            self._directory_index_tasks.pop(key, None)
            completed_roots.add(key[0])
            if snapshot.error is None and not snapshot.canceled:
                changed_roots.add(key[0])
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
        for root in changed_roots:
            self._drop_very_similar_cache(root)
        if reload_current and self.current_catalog is not None:
            self.model.refresh_pending_thumbnails()
            self._request_physical_reconcile(
                self.current_catalog.root,
                self.current_dir_rel,
            )
            self._request_physical_progress_reload(
                self.current_catalog.root,
                self.current_dir_rel,
            )

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
    MAX_VISIBLE_TAGS = 500

    def __init__(self, catalog: Catalog, rel_path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.rel_path = rel_path
        self._load_closed = False
        self._load_finished = False
        self._accept_pending = False
        self._hidden_selected_tags: list[str] = []
        self.loaded_file_identity: object | None = None
        self._load_cancel_event = Event()
        self._load_executor = shared_dialog_executor()
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
        self.tag_layout = QVBoxLayout(tag_container)
        self.loading_label = QLabel("Loading tags…")
        self.tag_layout.addWidget(self.loading_label)
        self.checkboxes: list[QCheckBox] = []
        scroll.setWidget(tag_container)
        layout.addWidget(scroll)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Comma-separated tags")
        self.entry.returnPressed.connect(self._accept_when_ready)
        self.setFocusProxy(self.entry)
        layout.addWidget(self.entry)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_when_ready)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(False)
        layout.addWidget(buttons)
        self._load_future = self._load_executor.submit(
            self._load_tag_state,
            catalog.root,
            catalog.root_identity,
            catalog.storage_identity,
            rel_path,
            self._load_cancel_event,
        )
        self._load_timer = QTimer(self)
        self._load_timer.setInterval(25)
        self._load_timer.timeout.connect(self._settle_tag_state)
        self._load_timer.start()
        self.resize(360, 420)

    @classmethod
    def _load_tag_state(
        cls,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        rel_path: str,
        cancel_event: Event,
    ) -> tuple[list[str], list[str], bool, object | None]:
        """Read a bounded tag editor snapshot without using the UI connection."""

        if cancel_event.is_set():
            return [], [], False, None
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as reader:
            image_identity = reader.file_identity(rel_path)
            rows = reader._conn.execute(  # noqa: SLF001 - bounded read-only dialog query
                "SELECT name FROM tags ORDER BY name COLLATE NOCASE ASC LIMIT ?",
                (cls.MAX_VISIBLE_TAGS + 1,),
            ).fetchall()
            if cancel_event.is_set():
                return [], [], False, None
            selected_rows = reader._conn.execute(  # noqa: SLF001 - bounded read-only dialog query
                """
                SELECT tags.name
                FROM image_tags
                JOIN tags ON tags.id = image_tags.tag_id
                JOIN images ON images.id = image_tags.image_id
                WHERE images.rel_path = ?
                ORDER BY tags.name COLLATE NOCASE ASC
                LIMIT ?
                """,
                (rel_path, cls.MAX_VISIBLE_TAGS + 1),
            ).fetchall()
            reader._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
            if reader.file_identity(rel_path) != image_identity:
                raise OSError(f"image changed while tags were loading: {rel_path}")
        tags = [str(row["name"]) for row in rows[: cls.MAX_VISIBLE_TAGS]]
        selected = [str(row["name"]) for row in selected_rows[: cls.MAX_VISIBLE_TAGS]]
        selected_overflow = len(selected_rows) > cls.MAX_VISIBLE_TAGS
        return tags, selected, selected_overflow, image_identity

    def _settle_tag_state(self) -> None:
        if not self._load_future.done():
            return
        self._load_timer.stop()
        if self._load_closed or self._load_future.cancelled():
            return
        try:
            tags, selected, selected_overflow, image_identity = self._load_future.result()
        except Exception as error:
            self.loading_label.setText(f"Unable to load tags: {error}")
            self._accept_pending = False
            return
        if image_identity is None:
            self.loading_label.setText("Unable to verify the selected image")
            self._accept_pending = False
            return
        self.loaded_file_identity = image_identity
        selected_by_key = {name.casefold(): name for name in selected}
        visible_by_key = {name.casefold(): name for name in tags}
        # Always show selected tags first. This preserves a selection even when
        # it falls beyond the bounded alphabetical catalog page.
        visible_names = list(selected)
        visible_names.extend(
            name
            for name in tags
            if name.casefold() not in selected_by_key
        )
        visible_names = visible_names[: self.MAX_VISIBLE_TAGS]
        visible_keys = {name.casefold() for name in visible_names}
        self._hidden_selected_tags = [
            name for key, name in selected_by_key.items() if key not in visible_keys
        ]
        self.loading_label.deleteLater()
        for tag in visible_names:
            checkbox = QCheckBox(tag)
            checkbox.setStyleSheet("background: transparent; color: #202124;")
            checkbox.setChecked(tag.casefold() in selected_by_key)
            self.checkboxes.append(checkbox)
            self.tag_layout.addWidget(checkbox)
        if len(visible_by_key) >= self.MAX_VISIBLE_TAGS:
            self.tag_layout.addWidget(QLabel(f"Showing the first {self.MAX_VISIBLE_TAGS:,} tags."))
        if selected_overflow:
            # Replacing a tag set we could not read completely would silently
            # remove hidden assignments, so keep the dialog fail-closed.
            self.tag_layout.addWidget(QLabel("This image has too many tags to edit safely."))
            self._accept_pending = False
            return
        self.tag_layout.addStretch(1)
        self._load_finished = True
        self.ok_button.setEnabled(True)
        if self._accept_pending:
            self.accept()

    def _accept_when_ready(self) -> None:
        if self._load_finished:
            self.accept()
        else:
            self._accept_pending = True

    def showEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().showEvent(event)
        QTimer.singleShot(0, self.focus_entry)

    def focus_entry(self) -> None:
        self.entry.setFocus(Qt.FocusReason.OtherFocusReason)

    def selected_tags(self) -> list[str]:
        names = list(self._hidden_selected_tags)
        names.extend(checkbox.text() for checkbox in self.checkboxes if checkbox.isChecked())
        names.extend(parse_tag_entry(self.entry.text()))
        seen: set[str] = set()
        result: list[str] = []
        for name in names:
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def _shutdown_tag_load(self) -> None:
        if self._load_closed:
            return
        self._load_closed = True
        self._load_cancel_event.set()
        self._load_timer.stop()
        self._load_future.cancel()
        self._load_executor.shutdown(wait=False, cancel_futures=True)

    def done(self, result: int) -> None:
        self._shutdown_tag_load()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_tag_load()
        super().closeEvent(event)


class DuplicateListDialog(QDialog):
    MAX_MATCHES_PER_SECTION = 500
    POPULATE_BATCH_SIZE = 100

    def __init__(
        self,
        catalog: Catalog,
        source: ImageRecord,
        matches: DuplicateMatchGroups | None,
        navigate_callback: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.source = source
        self.matches = matches
        self.navigate_callback = navigate_callback
        self._load_future: Future[DuplicateMatchGroups] | None = None
        self._load_executor: SharedExecutorLease | None = None
        self._load_cancel_event = Event()
        self._load_closed = False
        self._match_specs: deque[tuple[str, str | None, str | None, bool]] = deque()
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

        if matches is None:
            loading = QListWidgetItem("Loading duplicate details…")
            loading.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(loading)
            self._load_executor = shared_dialog_executor()
            self._load_future = self._load_executor.submit(
                self._load_matches,
                catalog.root,
                catalog.root_identity,
                catalog.storage_identity,
                source.rel_path,
                self._load_cancel_event,
            )
            self._load_timer = QTimer(self)
            self._load_timer.setInterval(25)
            self._load_timer.timeout.connect(self._settle_matches)
            self._load_timer.start()
        else:
            self._populate_matches(matches)

        self._populate_timer = QTimer(self)
        self._populate_timer.setInterval(0)
        self._populate_timer.timeout.connect(self._populate_match_batch)
        if self._match_specs:
            self._populate_match_batch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(720, 420)

    @staticmethod
    def _load_matches(
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        rel_path: str,
        cancel_event: Event,
    ) -> DuplicateMatchGroups:
        # Use a worker-owned connection so a long duplicate query cannot hold
        # the UI catalog's database lock and stall unrelated navigation.
        def check_cancel() -> None:
            if cancel_event.is_set():
                raise IndexTaskCancelled("duplicate query canceled")

        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as catalog:
            matches = catalog.duplicate_matches_for_image(
                rel_path,
                SortOrder.NAME_ASC,
                include_blobs=False,
                cancel_check=check_cancel,
            )
            catalog._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
            return matches

    def _populate_matches(self, matches: DuplicateMatchGroups) -> None:
        exact_count = len(matches.exact)
        similar_count = len(matches.very_similar)
        exact = tuple(matches.exact[: self.MAX_MATCHES_PER_SECTION])
        similar = tuple(matches.very_similar[: self.MAX_MATCHES_PER_SECTION])
        self.matches = DuplicateMatchGroups(exact=exact, very_similar=similar)
        self.list_widget.clear()
        self._match_specs.clear()
        self._queue_section("Exact Duplicates", exact, exact_count)
        self._queue_section("Very Similar", similar, similar_count)
        if hasattr(self, "_populate_timer"):
            self._populate_match_batch()

    def _settle_matches(self) -> None:
        future = self._load_future
        if future is None or not future.done():
            return
        self._load_future = None
        self._load_timer.stop()
        if self._load_closed or future.cancelled():
            return
        try:
            matches = future.result()
        except Exception as error:
            self.list_widget.clear()
            item = QListWidgetItem(f"Unable to load duplicate details: {error}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
            return
        self._populate_matches(matches)

    def _shutdown_match_load(self) -> None:
        if self._load_closed:
            return
        self._load_closed = True
        self._load_cancel_event.set()
        timer = getattr(self, "_load_timer", None)
        if timer is not None:
            timer.stop()
        if self._load_future is not None:
            self._load_future.cancel()
            self._load_future = None
        if self._load_executor is not None:
            self._load_executor.shutdown(wait=False, cancel_futures=True)
            self._load_executor = None
        populate_timer = getattr(self, "_populate_timer", None)
        if populate_timer is not None:
            populate_timer.stop()
        self._match_specs.clear()

    def done(self, result: int) -> None:
        self._shutdown_match_load()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_match_load()
        super().closeEvent(event)

    def _queue_section(
        self,
        title: str,
        records: Sequence[ImageRecord],
        total_count: int,
    ) -> None:
        self._match_specs.append((f"{title} ({total_count:,})", None, None, True))
        if not records:
            self._match_specs.append(("  (none)", None, None, False))
            return
        for record in records:
            self._match_specs.append(
                (f"  {record.rel_path}", str(record.absolute_path), record.rel_path, False)
            )
        if total_count > len(records):
            self._match_specs.append(
                (f"  Showing the first {len(records):,} of {total_count:,} matches.", None, None, False)
            )

    def _populate_match_batch(self) -> None:
        if self._load_closed:
            return
        for _ in range(self.POPULATE_BATCH_SIZE):
            if not self._match_specs:
                self._populate_timer.stop()
                return
            text, tooltip, rel_path, is_header = self._match_specs.popleft()
            item = QListWidgetItem(text)
            if is_header:
                header_font = item.font()
                header_font.setBold(True)
                item.setFont(header_font)
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            elif rel_path is None:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                item.setToolTip(tooltip or "")
                item.setData(Qt.ItemDataRole.UserRole, rel_path)
            self.list_widget.addItem(item)
        if self._match_specs:
            self._populate_timer.start()

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
        root = Path(str(self.catalog_combo.currentData()))
        for catalog in self.catalogs:
            if catalog.root == root:
                return catalog
        return self.catalogs[0]


class CatalogTagsDialog(QDialog):
    MAX_VISIBLE_TAGS = 500

    def __init__(self, catalog: Catalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self._read_closed = False
        self._read_executor: SharedExecutorLease | None = None
        self._read_future: Future[tuple[list[str], bool]] | None = None
        self._read_cancel_event: Event | None = None
        self._loaded_tags: list[str] | None = None
        self._loaded_tags_truncated = False
        self._requested_tag_names: list[str] = []
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
        self._read_timer = QTimer(self)
        self._read_timer.setInterval(25)
        self._read_timer.timeout.connect(self._settle_refresh)
        self.refresh()
        self.resize(420, 500)

    def refresh(self) -> None:
        if self._read_closed:
            return
        self._cancel_current_refresh()
        self._loaded_tags = None
        self._loaded_tags_truncated = False
        self.list_widget.clear()
        loading = QListWidgetItem("Loading tags…")
        loading.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list_widget.addItem(loading)
        self._read_cancel_event = Event()
        self._read_executor = shared_dialog_executor()
        self._read_future = self._read_executor.submit(
            self._load_catalog_tags,
            self.catalog.root,
            self.catalog.root_identity,
            self.catalog.storage_identity,
            self._read_cancel_event,
        )
        self._read_timer.start()

    @classmethod
    def _load_catalog_tags(
        cls,
        root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        cancel_event: Event,
    ) -> tuple[list[str], bool]:
        if cancel_event.is_set():
            return [], False
        with Catalog.open_reader(
            root,
            expected_root_identity=expected_root_identity,
            expected_storage_identity=expected_storage_identity,
        ) as reader:
            rows = reader._conn.execute(  # noqa: SLF001 - bounded read-only dialog query
                "SELECT name FROM tags ORDER BY name COLLATE NOCASE ASC LIMIT ?",
                (cls.MAX_VISIBLE_TAGS + 1,),
            ).fetchall()
            reader._assert_catalog_storage_identity()  # noqa: SLF001 - stale-result guard
        return (
            [str(row["name"]) for row in rows[: cls.MAX_VISIBLE_TAGS]],
            len(rows) > cls.MAX_VISIBLE_TAGS,
        )

    def _settle_refresh(self) -> None:
        future = self._read_future
        if future is None or not future.done():
            return
        self._read_future = None
        self._read_timer.stop()
        if self._read_closed or future.cancelled():
            return
        try:
            tags, truncated = future.result()
        except Exception as error:
            self.list_widget.clear()
            item = QListWidgetItem(f"Unable to load tags: {error}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
            return
        self._loaded_tags = tags
        self._loaded_tags_truncated = truncated
        self._render_visible_tags()

    def add_tags(self) -> None:
        names = parse_tag_entry(self.entry.text())
        if not names:
            return
        requested_keys = {normalize_tag(name) for name in self._requested_tag_names}
        for name in names:
            if normalize_tag(name) not in requested_keys:
                self._requested_tag_names.append(name)
                requested_keys.add(normalize_tag(name))
        self.entry.clear()
        self._render_visible_tags()

    def requested_tags(self) -> tuple[str, ...]:
        return tuple(self._requested_tag_names)

    def _render_visible_tags(self) -> None:
        loaded_tags = self._loaded_tags or []
        combined: dict[str, str] = {}
        for name in (*loaded_tags, *self._requested_tag_names):
            combined.setdefault(normalize_tag(name), name)
        tags = sorted(combined.values(), key=str.casefold)
        truncated = self._loaded_tags_truncated or len(tags) > self.MAX_VISIBLE_TAGS
        visible_tags = tags[: self.MAX_VISIBLE_TAGS]
        self.list_widget.clear()
        for tag in visible_tags:
            self.list_widget.addItem(tag)
        if self._loaded_tags is None:
            item = QListWidgetItem("Loading existing tags…")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
        elif truncated:
            item = QListWidgetItem(f"Showing the first {self.MAX_VISIBLE_TAGS:,} tags.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)
        elif not visible_tags:
            item = QListWidgetItem("No tags")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_widget.addItem(item)

    def _cancel_current_refresh(self) -> None:
        if self._read_cancel_event is not None:
            self._read_cancel_event.set()
            self._read_cancel_event = None
        if self._read_future is not None:
            self._read_future.cancel()
            self._read_future = None
        if self._read_executor is not None:
            self._read_executor.shutdown(wait=False, cancel_futures=True)
            self._read_executor = None

    def _shutdown_tag_reads(self) -> None:
        if self._read_closed:
            return
        self._read_closed = True
        self._read_timer.stop()
        self._cancel_current_refresh()

    def done(self, result: int) -> None:
        self._shutdown_tag_reads()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_tag_reads()
        super().closeEvent(event)


class LogsDialog(QDialog):
    MAX_DISPLAY_LINES = 2000
    MAX_LINE_CHARS = 4096
    MAX_DISPLAY_CHARS = 1024 * 1024

    def __init__(self, catalogs: list[Catalog], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._load_closed = False
        self._load_cancel_event = Event()
        self._load_executor = shared_dialog_executor()
        self.setWindowTitle("Logs")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(self.MAX_DISPLAY_LINES + 2)
        self.text.setPlainText("Loading logs…")
        layout.addWidget(self.text, 1)

        copy_button = QPushButton("Copy selected line")
        copy_button.clicked.connect(self.copy_selected_line)
        self.copy_buttons: list[QPushButton] = [copy_button]
        layout.addWidget(copy_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._load_future = self._load_executor.submit(
            self._load_logs,
            tuple(
                (
                    catalog.root,
                    catalog.root_identity,
                    catalog.storage_identity,
                )
                for catalog in catalogs
            ),
            self._load_cancel_event,
        )
        self._load_timer = QTimer(self)
        self._load_timer.setInterval(25)
        self._load_timer.timeout.connect(self._settle_logs)
        self._load_timer.start()
        self.resize(900, 520)

    @classmethod
    def _load_logs(
        cls,
        catalogs: tuple[
            tuple[Path, tuple[int, int], CatalogStorageIdentity],
            ...,
        ],
        cancel_event: Event,
    ) -> str:
        entries: list[tuple[str, str]] = []
        total_lines = 0
        for root, expected_root_identity, expected_storage_identity in catalogs:
            if cancel_event.is_set():
                return ""
            catalog_name = root.name or str(root)
            Catalog.assert_storage_identity(
                root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
            with Catalog.open_filesystem_handle(
                root,
                expected_root_identity=expected_root_identity,
            ) as reader:
                lines = reader.read_log_lines()
            Catalog.assert_storage_identity(
                root,
                expected_root_identity=expected_root_identity,
                expected_storage_identity=expected_storage_identity,
            )
            for line in lines:
                if cancel_event.is_set():
                    return ""
                total_lines += 1
                bounded_line = line[: cls.MAX_LINE_CHARS]
                display_line = f"{catalog_name}: {bounded_line}"
                entries.append((line[:64], display_line))
                # Periodically compact so many catalogs cannot create an
                # unbounded intermediate list before the UI cap is applied.
                if len(entries) >= cls.MAX_DISPLAY_LINES * 2:
                    entries.sort(key=lambda item: item[0])
                    del entries[: len(entries) - cls.MAX_DISPLAY_LINES]
        entries.sort(key=lambda item: item[0])
        if len(entries) > cls.MAX_DISPLAY_LINES:
            del entries[: len(entries) - cls.MAX_DISPLAY_LINES]
        display_lines = [entry[1] for entry in entries]
        omitted = max(0, total_lines - len(display_lines))
        if omitted:
            display_lines.insert(0, f"[{omitted:,} older log lines omitted]")
        if not display_lines:
            return "No log entries"
        result = "\n".join(display_lines)
        if len(result) > cls.MAX_DISPLAY_CHARS:
            result = "[Earlier log text omitted]\n" + result[-cls.MAX_DISPLAY_CHARS :]
        return result

    def _settle_logs(self) -> None:
        if not self._load_future.done():
            return
        self._load_timer.stop()
        if self._load_closed or self._load_future.cancelled():
            return
        try:
            text = self._load_future.result()
        except Exception as error:
            text = f"Unable to load logs: {error}"
        if not self._load_closed:
            if len(text) > self.MAX_DISPLAY_CHARS:
                suffix = "\n[log output truncated]"
                text = text[: self.MAX_DISPLAY_CHARS - len(suffix)] + suffix
            self.text.setPlainText(text)

    def copy_selected_line(self) -> None:
        cursor = self.text.textCursor()
        if not cursor.hasSelection():
            cursor.select(cursor.SelectionType.LineUnderCursor)
        line = cursor.selectedText().replace("\u2029", "\n").strip("\n")
        if line:
            self.copy_line(line)

    def copy_line(self, line: str) -> None:
        QApplication.clipboard().setText(line)

    def _shutdown_log_load(self) -> None:
        if self._load_closed:
            return
        self._load_closed = True
        self._load_cancel_event.set()
        self._load_timer.stop()
        self._load_future.cancel()
        self._load_executor.shutdown(wait=False, cancel_futures=True)

    def done(self, result: int) -> None:
        self._shutdown_log_load()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_log_load()
        super().closeEvent(event)


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
    PROGRESS_INTERVAL_SECONDS = 0.05
    PROGRESS_ENTRY_INTERVAL = 128

    def __init__(self, catalog: Catalog, dir_rel: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.dir_rel = dir_rel
        relative = Path(dir_rel)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("directory properties path must stay inside the catalog")
        catalog._validate_catalog_entry_parts(relative.parts)  # noqa: SLF001 - lexical validation only
        # Do not call Path.resolve()/stat on the UI thread. The worker performs
        # all potentially blocking filesystem access against this lexical path.
        self.path = catalog.root / relative if dir_rel else catalog.root
        self.image_count = 0
        self.other_file_count = 0
        self.image_size_bytes = 0
        self.other_file_size_bytes = 0
        self.thumbnail_repository_size_bytes = 0
        self.database_size_bytes = 0
        self._status_text = "Counting..."
        self._scan_closed = False
        self._scan_complete = False
        self._scan_cancel_event = Event()
        # The worker publishes immutable snapshots into a one-element deque.
        # CPython deque append/popleft operations are atomic, and maxlen=1
        # guarantees a slow UI never accumulates a directory-sized queue.
        self._scan_snapshots: deque[
            tuple[int, int, int, int, int, int, str, bool]
        ] = deque(maxlen=1)
        self._scan_executor = shared_dialog_executor()

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
        self.database_size_label = QLabel("Counting...")
        self.thumbnail_repository_size_label = QLabel("Counting...")
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
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._poll_scan)
        self._scan_future = self._scan_executor.submit(
            self._scan_directory,
            self.path,
            catalog.root,
            catalog.root_identity,
            catalog.storage_identity,
            catalog.thumbnail_dir if not dir_rel else None,
            tuple(catalog.catalog_database_paths()) if not dir_rel else (),
            self._scan_cancel_event,
            self._scan_snapshots,
        )
        self.timer.start()
        self.resize(700, 240)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_scan()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._shutdown_scan()
        super().done(result)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._refresh_status_label_text()

    def copy_path(self) -> None:
        QApplication.clipboard().setText(str(self.path))

    def is_counting(self) -> bool:
        return not self._scan_closed and not self._scan_complete

    @classmethod
    def _scan_directory(
        cls,
        path: Path,
        catalog_root: Path,
        expected_root_identity: tuple[int, int],
        expected_storage_identity: CatalogStorageIdentity,
        thumbnail_dir: Path | None,
        database_paths: tuple[Path, ...],
        cancel_event: Event,
        snapshots: deque[tuple[int, int, int, int, int, int, str, bool]],
    ) -> tuple[int, int, int, int, int, int, str, bool]:
        image_count = 0
        other_count = 0
        image_bytes = 0
        other_bytes = 0
        thumbnail_bytes = 0
        database_bytes = 0
        processed = 0
        last_publish = 0.0
        errors = 0

        def catalog_is_current() -> bool:
            try:
                Catalog.assert_storage_identity(
                    catalog_root,
                    expected_root_identity=expected_root_identity,
                    expected_storage_identity=expected_storage_identity,
                )
            except OSError:
                return False
            return True

        def snapshot(status: str, done: bool = False) -> tuple[int, int, int, int, int, int, str, bool]:
            value = (
                image_count,
                other_count,
                image_bytes,
                other_bytes,
                database_bytes,
                thumbnail_bytes,
                status,
                done,
            )
            snapshots.append(value)
            return value

        def publish(status: str, *, force: bool = False) -> None:
            nonlocal last_publish
            now = monotonic()
            if force or (
                processed % cls.PROGRESS_ENTRY_INTERVAL == 0
                and now - last_publish >= cls.PROGRESS_INTERVAL_SECONDS
            ):
                snapshot(status)
                last_publish = now

        if not catalog_is_current():
            return snapshot("Catalog is no longer available", True)

        pending_dirs = [path]
        while pending_dirs and not cancel_event.is_set():
            current = pending_dirs.pop()
            publish(f"Counting {current}", force=True)
            try:
                if not catalog_is_current():
                    return snapshot("Catalog changed while counting", True)
                current_stat = current.lstat()
                if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                    errors += 1
                    continue
                iterator = os.scandir(current)
            except OSError:
                errors += 1
                continue
            with iterator:
                for entry in iterator:
                    if cancel_event.is_set():
                        return snapshot("Canceled", True)
                    processed += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != ".marnwick":
                                pending_dirs.append(Path(entry.path))
                            publish(f"Counting {current}")
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            publish(f"Counting {current}")
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        errors += 1
                        continue
                    if is_image_name(entry.name):
                        image_count += 1
                        image_bytes += size
                    else:
                        other_count += 1
                        other_bytes += size
                    publish(f"Counting {current}")

        if cancel_event.is_set():
            return snapshot("Canceled", True)

        if thumbnail_dir is not None:
            pending_dirs = [thumbnail_dir]
            while pending_dirs and not cancel_event.is_set():
                current = pending_dirs.pop()
                publish(f"Counting thumbnails in {current}", force=True)
                try:
                    if not catalog_is_current():
                        return snapshot("Catalog changed while counting", True)
                    current_stat = current.lstat()
                    if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                        errors += 1
                        continue
                    iterator = os.scandir(current)
                except FileNotFoundError:
                    # A thumbnail repository is optional until the first
                    # thumbnail has been published.
                    continue
                except OSError:
                    errors += 1
                    continue
                with iterator:
                    for entry in iterator:
                        if cancel_event.is_set():
                            return snapshot("Canceled", True)
                        processed += 1
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending_dirs.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                thumbnail_bytes += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            errors += 1
                        publish(f"Counting thumbnails in {current}")

        for database_path in database_paths:
            if cancel_event.is_set():
                return snapshot("Canceled", True)
            if not catalog_is_current():
                return snapshot("Catalog changed while counting", True)
            try:
                database_stat = database_path.lstat()
                if stat.S_ISLNK(database_stat.st_mode) or not stat.S_ISREG(database_stat.st_mode):
                    raise OSError("unsafe catalog database entry")
                database_bytes += database_stat.st_size
            except OSError:
                # WAL/SHM files are optional and commonly absent. Only count
                # an error for the primary database.
                if database_path == database_paths[0]:
                    errors += 1
        if not catalog_is_current():
            return snapshot("Catalog changed while counting", True)
        status = "Ready" if not errors else f"Ready ({errors:,} entries unavailable)"
        return snapshot(status, True)

    def _poll_scan(self) -> None:
        newest: tuple[int, int, int, int, int, int, str, bool] | None = None
        while self._scan_snapshots:
            newest = self._scan_snapshots.popleft()
        if newest is not None and not self._scan_closed:
            (
                self.image_count,
                self.other_file_count,
                self.image_size_bytes,
                self.other_file_size_bytes,
                self.database_size_bytes,
                self.thumbnail_repository_size_bytes,
                status,
                done,
            ) = newest
            self._set_status_text(status)
            self._update_labels()
            if done:
                self._scan_complete = True
                self.timer.stop()
        if self._scan_future.done() and not self._scan_complete and not self._scan_closed:
            try:
                final = self._scan_future.result()
            except Exception as error:
                self._set_status_text(f"Unable to count directory: {error}")
                self._scan_complete = True
                self.timer.stop()
            else:
                self._scan_snapshots.append(final)
                self._poll_scan()

    def _update_labels(self) -> None:
        self.image_count_label.setText(str(self.image_count))
        self.other_count_label.setText(str(self.other_file_count))
        self.image_size_label.setText(format_bytes(self.image_size_bytes))
        self.other_size_label.setText(format_bytes(self.other_file_size_bytes))
        if not self.dir_rel:
            self.database_size_label.setText(format_bytes(self.database_size_bytes))
            self.thumbnail_repository_size_label.setText(
                format_bytes(self.thumbnail_repository_size_bytes)
            )

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

    def _shutdown_scan(self) -> None:
        if self._scan_closed:
            return
        self._scan_closed = True
        self._scan_cancel_event.set()
        self.timer.stop()
        self._scan_future.cancel()
        self._scan_snapshots.clear()
        self._scan_executor.shutdown(wait=False, cancel_futures=True)


class MetadataDialog(QDialog):
    MAX_METADATA_TEXT_CHARS = 256 * 1024
    MAX_METADATA_VALUE_CHARS = 4096
    MAX_METADATA_ITEMS_PER_SECTION = 1024

    def __init__(self, image_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Metadata")
        self.setWindowIcon(load_app_icon())
        self.setStyleSheet(DIALOG_STYLESHEET)

        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(self.MAX_METADATA_ITEMS_PER_SECTION * 2 + 32)
        self.text.setPlainText("Loading metadata…")
        layout.addWidget(self.text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(720, 560)
        self._load_closed = False
        self._load_executor = shared_dialog_executor()
        self._load_future = self._load_executor.submit(metadata_text, image_path)
        self._load_timer = QTimer(self)
        self._load_timer.setInterval(25)
        self._load_timer.timeout.connect(self._settle_metadata)
        self._load_timer.start()

    def _settle_metadata(self) -> None:
        if not self._load_future.done():
            return
        future = self._load_future
        self._load_timer.stop()
        if self._load_closed or future.cancelled():
            return
        try:
            text = future.result()
        except Exception as error:
            text = f"Metadata read error: {error}"
        if not self._load_closed:
            self.text.setPlainText(self.bound_text(text, self.MAX_METADATA_TEXT_CHARS))

    def _shutdown_metadata_load(self) -> None:
        if self._load_closed:
            return
        self._load_closed = True
        self._load_timer.stop()
        self._load_future.cancel()
        self._load_executor.shutdown(wait=False, cancel_futures=True)

    def done(self, result: int) -> None:
        self._shutdown_metadata_load()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._shutdown_metadata_load()
        super().closeEvent(event)

    @staticmethod
    def bound_text(value: object, limit: int) -> str:
        if isinstance(value, str):
            text = value
        elif isinstance(value, (bytes, bytearray, memoryview)):
            text = f"<{len(value):,} bytes>"
        else:
            text = str(value)
        if len(text) <= limit:
            return text
        suffix = "\n[metadata output truncated]"
        return text[: max(0, limit - len(suffix))] + suffix

    @classmethod
    def format_metadata_value(cls, value: object) -> str:
        if isinstance(value, str):
            return cls.bound_text(value, cls.MAX_METADATA_VALUE_CHARS)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<{len(value):,} bytes>"
        if value is None or isinstance(value, (bool, int, float)):
            return str(value)
        if isinstance(value, (tuple, list)):
            parts = [cls.format_metadata_value(item) for item in value[:32]]
            if len(value) > 32:
                parts.append(f"… {len(value) - 32:,} more")
            opener, closer = ("(", ")") if isinstance(value, tuple) else ("[", "]")
            return cls.bound_text(opener + ", ".join(parts) + closer, cls.MAX_METADATA_VALUE_CHARS)
        if isinstance(value, Mapping):
            parts: list[str] = []
            for index, (key, item) in enumerate(value.items()):
                if index >= 32:
                    parts.append(f"… {len(value) - 32:,} more")
                    break
                parts.append(
                    f"{cls.format_metadata_value(key)}: {cls.format_metadata_value(item)}"
                )
            return cls.bound_text("{" + ", ".join(parts) + "}", cls.MAX_METADATA_VALUE_CHARS)
        numerator = getattr(value, "numerator", None)
        denominator = getattr(value, "denominator", None)
        if isinstance(numerator, int) and isinstance(denominator, int):
            return f"{numerator}/{denominator}"
        return f"<{type(value).__name__} metadata value>"


def metadata_text(image_path: Path) -> str:
    lines: list[str] = []
    missing = object()

    def append_line(label: str, value: object = missing) -> None:
        if value is missing:
            line = label
        else:
            line = f"{label}: {MetadataDialog.format_metadata_value(value)}"
        lines.append(MetadataDialog.bound_text(line, MetadataDialog.MAX_METADATA_VALUE_CHARS))

    append_line("Path", str(image_path))
    try:
        stat = image_path.stat()
        append_line("File size", f"{stat.st_size} bytes")
        append_line("Modified", datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"))
    except OSError as error:
        append_line("File stat error", str(error))

    try:
        with open_catalog_image(image_path) as image:
            append_line("Format", image.format or "unknown")
            append_line("Dimensions", f"{image.width} x {image.height}")
            append_line("Mode", image.mode)
            if image.info:
                lines.append("")
                lines.append("Image Info")
                for index, key in enumerate(image.info):
                    if index >= MetadataDialog.MAX_METADATA_ITEMS_PER_SECTION:
                        lines.append("[additional image metadata omitted]")
                        break
                    if key == "exif":
                        continue
                    append_line(str(key), image.info[key])
            exif = image.getexif()
            if exif:
                lines.append("")
                lines.append("EXIF")
                for index, tag_id in enumerate(exif):
                    if index >= MetadataDialog.MAX_METADATA_ITEMS_PER_SECTION:
                        lines.append("[additional EXIF metadata omitted]")
                        break
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    value = exif.get(tag_id)
                    append_line(str(tag_name), value)
    except Exception as error:
        append_line("Metadata read error", str(error))
    return MetadataDialog.bound_text(
        "\n".join(lines),
        MetadataDialog.MAX_METADATA_TEXT_CHARS,
    )


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
        # A zoomed target can be many times larger than the viewport. Keep
        # the source bounded and transform only the invalidated visible area;
        # never manufacture a target-sized raster on the GUI thread.
        painter.setClipRegion(event.region())
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
        painter.drawPixmap(target, self._display_pixmap, self._display_pixmap.rect())


class FullscreenViewer(QDialog):
    ZOOM_STEP = 1.25
    MAX_ZOOM = 16.0
    PAN_KEY_STEP = 80

    def __init__(
        self,
        catalog: Catalog,
        navigator: ImageNavigator | PagedImageNavigator,
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
        self.clone_alignment_target: tuple[int, int] | None = None
        self.clone_last_target: tuple[int, int] | None = None
        self.drag_origin: QPoint | None = None
        self.preview_path: Path | None = None
        self.preview_image: Image.Image | None = None
        self.preview_image_current = False
        self.display_preview_image: Image.Image | None = None
        self.display_preview_size: tuple[int, int] | None = None
        self.base_pixmap = QPixmap()
        self.image_coordinate_size: tuple[int, int] = (0, 0)
        self.load_error: str | None = None
        self.loaded_file_identity: ImageFileIdentity | None = None
        self.loaded_file_dates: FileDateSnapshot | None = None
        self.movie: QMovie | None = None
        self._movie_data: QByteArray | None = None
        self._movie_buffer: QBuffer | None = None
        self._movie_validation_future: Future[bool] | None = None
        self._movie_validation_generation = 0
        self._pending_movie_bytes: bytes | None = None
        self.zoom_level = 1.0
        self.pan_offset = QPoint(0, 0)
        self.pan_drag_start: QPoint | None = None
        self.pan_offset_at_drag_start = QPoint(0, 0)
        self.info_overlay_enabled = False
        self._load_generation = 0
        self._load_future: Future[ViewerLoadResult] | None = None
        self._load_future_generation = 0
        self._pending_load_request: tuple[int, str] | None = None
        self._navigation_executor: SharedExecutorLease = shared_viewer_load_executor()
        self._preview_executor: SharedExecutorLease = shared_viewer_preview_executor()
        self._navigation_page_executor: SharedExecutorLease = shared_viewer_page_executor()
        self._navigation_page_future: Future[ViewerNavigationPage] | None = None
        self._navigation_page_cancel_event: Event | None = None
        self._pending_navigation_step = 0
        self._navigation_page_select_first = False
        self._navigation_page_replace_all = False
        self._navigation_rebuild_required = False
        self._navigation_rebuild_preferred_rel_path: str | None = None
        self._navigation_rebuild_fallback_index = 0
        self._navigation_rebuild_rel_paths: list[str] = []
        self._navigation_rebuild_seen: set[str] = set()
        self._navigation_rebuild_next_offset: int | None = None
        self._navigation_rebuild_total_count = 0
        self._pending_image_delete: ViewerDeletePending | None = None
        self._load_closed = False
        self._pending_save_rels: set[str] = set()
        self._preview_generation = 0
        self._preview_future: Future[PreviewRenderResult] | None = None
        self._preview_future_generation = 0
        self._preview_cancel_event: Event | None = None
        self._pending_preview_request: tuple[
            int,
            Path,
            str,
            tuple[EditOperation, ...],
            tuple[int, int],
            Event,
        ] | None = None
        # MainWindow keeps failed-save operations until their restored
        # preview has actually rendered. These fields identify that exact
        # safety copy so a later failure cannot be consumed accidentally.
        self._retained_preview_rel: str | None = None
        self._retained_preview_operations: tuple[EditOperation, ...] = ()
        self._load_timer = QTimer(self)
        self._load_timer.setInterval(20)
        self._load_timer.timeout.connect(self._settle_async_load)
        self._movie_validation_timer = QTimer(self)
        self._movie_validation_timer.setInterval(20)
        self._movie_validation_timer.timeout.connect(self._settle_movie_validation)
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(20)
        self._preview_timer.timeout.connect(self._settle_preview_render)
        self._navigation_page_timer = QTimer(self)
        self._navigation_page_timer.setInterval(20)
        self._navigation_page_timer.timeout.connect(self._settle_navigation_page)
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

    def exec_fullscreen(self) -> int:
        """Enter the modal viewer loop without first exposing a nonmodal window.

        Calling ``showFullScreen()`` before ``exec()`` briefly registers the
        viewer as an ordinary top-level window.  Some window managers then
        restore focus to the main window when a nested message box closes,
        even though the fullscreen viewer still covers it.  Applying the
        state before ``exec()`` lets Qt establish the correct modal owner from
        the viewer's first visible frame.
        """

        self.setWindowState(self.windowState() | Qt.WindowState.WindowFullScreen)
        return int(self.exec())

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        key = event.key()
        modifiers = event.modifiers()
        if not self.navigator.order and key not in {
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Escape,
        }:
            # A successful delete can leave a paged navigator briefly empty
            # while its rebased first page is loading. Current-image actions
            # must remain inert during that bounded gap.
            return
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
                self.clone_alignment_target = None
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
            if self.clone_alignment_target is None:
                self.clone_alignment_target = image_point
            self.clone_last_target = None
            self.paint_clone_to(image_point)
            return True
        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:  # type: ignore[attr-defined]
            self.clone_painting = False
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
        self._shutdown_async_load()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self.restore_cursor_visibility()
        self.stop_movie()
        self.cleanup_preview()
        self._shutdown_async_load()
        super().done(result)

    @property
    def current_path(self) -> Path:
        if not self.navigator.order:
            raise RuntimeError("no viewer image is currently available")
        relative = Path(self.navigator.current)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("viewer image path must be relative and normalized")
        self.catalog._validate_catalog_entry_parts(relative.parts)  # noqa: SLF001
        return self.catalog.root.joinpath(*relative.parts)

    def navigate(self, step: int) -> None:
        if not self.confirm_pending_edits():
            return
        if self._navigation_rebuild_required:
            # Keep the displayed row stable while a fresh OFFSET prefix is
            # assembled. Navigating the old prefix here could select a row
            # whose membership or position has already changed underneath us.
            self.setWindowTitle("Marnwick — refreshing image order…")
            return
        if self._pending_image_delete is not None and not self.navigator.order:
            if (
                step > 0
                and isinstance(self.navigator, PagedImageNavigator)
                and self.navigator.has_more
            ):
                self._pending_navigation_step = step
            return
        if self._navigation_page_future is not None and step < 0:
            # Let the useful page finish, but do not unexpectedly jump forward
            # after the user has already navigated away from its boundary.
            self._pending_navigation_step = 0
        next_rel_path = self.navigator.next() if step > 0 else self.navigator.previous()
        if next_rel_path is None:
            if (
                step > 0
                and isinstance(self.navigator, PagedImageNavigator)
                and self.navigator.has_more
            ):
                self._start_navigation_page(step)
                return
            self.accept()
            return
        self.load_current()

    def _start_navigation_page(
        self,
        step: int,
        *,
        select_first: bool = False,
        replace_all: bool = False,
    ) -> None:
        navigator = self.navigator
        if not isinstance(navigator, PagedImageNavigator) or self._load_closed:
            return
        if self._pending_image_delete is not None:
            self._pending_navigation_step = step
            self._navigation_page_select_first |= select_first
            self._navigation_page_replace_all |= replace_all
            if replace_all:
                self._navigation_rebuild_required = True
            self.setWindowTitle("Marnwick — deleting image…")
            return
        if self._navigation_page_future is not None:
            self._pending_navigation_step = step
            self._navigation_page_select_first |= select_first
            self._navigation_page_replace_all |= replace_all
            return
        cancel_event = Event()
        page_offset = (
            0
            if replace_all and self._navigation_rebuild_next_offset is None
            else (
                self._navigation_rebuild_next_offset
                if replace_all
                else navigator.next_offset
            )
        )
        try:
            future = self._navigation_page_executor.submit(
                navigator.page_loader,
                page_offset,
                VIEWER_REBUILD_PAGE_SIZE if replace_all else PANE_QUERY_PAGE_SIZE,
                cancel_event,
            )
        except RuntimeError:
            if select_first or replace_all:
                self._navigation_page_select_first = select_first
                self._navigation_page_replace_all = replace_all
                self.setWindowTitle("Marnwick — waiting for catalog reader…")
                QTimer.singleShot(
                    50,
                    partial(
                        self._retry_required_navigation_page,
                        step,
                        select_first,
                        replace_all,
                    ),
                )
            else:
                self.setWindowTitle("Marnwick — catalog reader busy")
            return
        self._navigation_page_cancel_event = cancel_event
        self._navigation_page_future = future
        self._pending_navigation_step = step
        self._navigation_page_select_first = select_first
        self._navigation_page_replace_all = replace_all
        self.setWindowTitle("Marnwick — loading more images…")
        self._navigation_page_timer.start()

    def _retry_required_navigation_page(
        self,
        step: int,
        select_first: bool,
        replace_all: bool,
    ) -> None:
        if (
            self._load_closed
            or self._navigation_page_future is not None
            or self._pending_image_delete is not None
            or self._navigation_page_select_first != select_first
            or self._navigation_page_replace_all != replace_all
        ):
            return
        self._start_navigation_page(
            step,
            select_first=select_first,
            replace_all=replace_all,
        )

    def _settle_navigation_page(self) -> None:
        future = self._navigation_page_future
        if future is None or not future.done():
            return
        self._navigation_page_future = None
        self._navigation_page_cancel_event = None
        self._navigation_page_timer.stop()
        self.setWindowTitle("Marnwick")
        if future.cancelled() or self._load_closed:
            return
        navigator = self.navigator
        if not isinstance(navigator, PagedImageNavigator):
            return
        pending_step = self._pending_navigation_step
        self._pending_navigation_step = 0
        select_first = self._navigation_page_select_first
        replace_all = self._navigation_page_replace_all
        old_offset = (
            (self._navigation_rebuild_next_offset or 0)
            if replace_all
            else navigator.next_offset
        )
        try:
            page = future.result()
        except ExecutorSaturatedError:
            if (
                replace_all
                or (
                    (pending_step > 0 or select_first)
                    and navigator.has_more
                )
            ) and not self._load_closed:
                self.setWindowTitle("Marnwick — waiting for catalog reader…")
                self._start_navigation_page(
                    pending_step,
                    select_first=select_first,
                    replace_all=replace_all,
                )
            return
        except Exception as error:
            self._navigation_page_select_first = False
            self._navigation_page_replace_all = False
            if replace_all:
                self._reset_navigation_rebuild()
                self.accept()
                return
            navigator.has_more = False
            if select_first and not navigator.order:
                self.accept()
                return
            self.load_error = str(error)
            self.update_info_overlay()
            return
        if replace_all:
            rel_paths = list(dict.fromkeys(page.rel_paths))
            if navigator.random_mode and rel_paths:
                rel_paths = ImageNavigator.random(rel_paths, rel_paths[0]).order
            for rel_path in rel_paths:
                if rel_path in self._navigation_rebuild_seen:
                    continue
                self._navigation_rebuild_seen.add(rel_path)
                self._navigation_rebuild_rel_paths.append(rel_path)
            self._navigation_rebuild_next_offset = page.next_offset
            self._navigation_rebuild_total_count = max(
                self._navigation_rebuild_total_count,
                len(self._navigation_rebuild_rel_paths),
                page.total_images,
            )
            preferred = self._navigation_rebuild_preferred_rel_path
            self._navigation_page_select_first = False
            self._navigation_page_replace_all = False
            if page.has_more and page.next_offset <= old_offset:
                self._reset_navigation_rebuild()
                self.accept()
                return
            preferred_found = (
                preferred is not None and preferred in self._navigation_rebuild_seen
            )
            usable_without_preference = (
                preferred is None and bool(self._navigation_rebuild_rel_paths)
            )
            if page.has_more and not preferred_found and not usable_without_preference:
                checked = len(self._navigation_rebuild_rel_paths)
                self._start_navigation_page(0, replace_all=True)
                if self._navigation_page_future is not None:
                    self.setWindowTitle(
                        f"Marnwick — refreshing image order ({checked} checked)…"
                    )
                return
            rebuilt_order = list(self._navigation_rebuild_rel_paths)
            fallback_index = self._navigation_rebuild_fallback_index
            next_offset = self._navigation_rebuild_next_offset or 0
            total_count = self._navigation_rebuild_total_count
            navigator.order = rebuilt_order
            navigator._seen = set(rebuilt_order)
            navigator.next_offset = next_offset
            navigator.has_more = page.has_more
            navigator.total_count = max(len(rebuilt_order), total_count)
            self._reset_navigation_rebuild()
            if navigator.order:
                navigator.index = (
                    navigator.order.index(preferred)
                    if preferred in navigator.order
                    else min(fallback_index, len(navigator.order) - 1)
                )
                self.load_current()
            elif navigator.has_more:
                self._start_navigation_page(0, select_first=True)
            else:
                self.accept()
            return
        added = navigator.append_page(page)
        self._navigation_page_select_first = False
        self._navigation_page_replace_all = False
        if page.has_more and page.next_offset <= old_offset:
            navigator.has_more = False
            self.load_error = "Catalog navigation page did not advance"
            self.update_info_overlay()
            return
        if select_first:
            if added:
                navigator.index = 0
                self.load_current()
                return
            if navigator.has_more:
                self._start_navigation_page(0, select_first=True)
            else:
                self.accept()
            return
        if self.operations:
            return
        if added and pending_step:
            next_rel_path = (
                navigator.next()
                if pending_step > 0
                else navigator.previous()
            )
            if next_rel_path is not None:
                self.load_current()
                return
        if navigator.has_more and pending_step > 0:
            self._start_navigation_page(pending_step)
        elif pending_step > 0:
            self.accept()

    def _reset_navigation_rebuild(self) -> None:
        self._navigation_rebuild_required = False
        self._navigation_rebuild_preferred_rel_path = None
        self._navigation_rebuild_fallback_index = 0
        self._navigation_rebuild_rel_paths.clear()
        self._navigation_rebuild_seen.clear()
        self._navigation_rebuild_next_offset = None
        self._navigation_rebuild_total_count = 0

    def invalidate_paged_navigation(self) -> None:
        """Rebuild a live OFFSET navigator after membership/order mutation.

        The old navigator remains published while bounded pages are read from
        page zero. If the displayed image moved beyond page zero, pages keep
        accumulating asynchronously until that exact image is found. This
        avoids both a visible jump and a discontinuous prefix that would break
        backward navigation.
        """

        navigator = self.navigator
        if self._load_closed or not isinstance(navigator, PagedImageNavigator):
            return
        preferred = navigator.current if navigator.order else None
        fallback_index = navigator.index if navigator.order else 0
        self._cancel_navigation_page_for_delete(preserve_rebuild=False)
        self._pending_navigation_step = 0
        self._navigation_rebuild_required = True
        self._navigation_rebuild_preferred_rel_path = preferred
        self._navigation_rebuild_fallback_index = fallback_index
        self._start_navigation_page(0, replace_all=True)

    def delete_current_image(self) -> None:
        if not self.confirm_pending_edits():
            return
        if not self.navigator.order:
            return
        if self._pending_image_delete is not None:
            self.setWindowTitle("Marnwick — delete already in progress…")
            return
        rel_path = self.navigator.current
        displayed_identity = self.loaded_file_identity
        if displayed_identity is None:
            show_error(
                self,
                "Delete",
                "The image is still loading or could not be verified; it was not deleted.",
            )
            return
        parent = self.parent()
        if isinstance(parent, MainWindow):
            parent._start_delete_confirmation(
                self.catalog,
                kind="image-viewer",
                rel_paths=(rel_path,),
                owner=self,
                wipe=self.wipe_on_delete,
                remove_from_current_view=True,
                expected_incarnations={rel_path: displayed_identity},
            )
            return
        show_error(
            self,
            "Delete",
            "This standalone viewer cannot safely mutate files without its catalog window.",
        )

    def _cancel_navigation_page_for_delete(
        self,
        *,
        preserve_rebuild: bool = False,
    ) -> None:
        self._navigation_page_timer.stop()
        if self._navigation_page_cancel_event is not None:
            self._navigation_page_cancel_event.set()
            self._navigation_page_cancel_event = None
        if self._navigation_page_future is not None:
            self._navigation_page_future.cancel()
            self._navigation_page_future = None
        self._navigation_page_select_first = False
        self._navigation_page_replace_all = False
        if preserve_rebuild and self._navigation_rebuild_required:
            # A delete changes the live query while this snapshot is being
            # assembled. Keep the sticky requirement, but restart from page
            # zero after the worker proves success or failure.
            self._navigation_rebuild_rel_paths.clear()
            self._navigation_rebuild_seen.clear()
            self._navigation_rebuild_next_offset = None
            self._navigation_rebuild_total_count = 0
        else:
            self._reset_navigation_rebuild()

    def image_delete_started(self, rel_path: str) -> None:
        if self._load_closed:
            return
        pending = self._pending_image_delete
        if pending is not None:
            if pending.rel_path != rel_path:
                raise RuntimeError("viewer already has a different pending delete")
            return
        try:
            removed_index = self.navigator.order.index(rel_path)
        except ValueError:
            return
        self._cancel_navigation_page_for_delete(preserve_rebuild=True)
        self._pending_image_delete = ViewerDeletePending(rel_path, removed_index)
        self.navigator.order.pop(removed_index)
        if not self.navigator.order:
            self.navigator.index = 0
            self.setWindowTitle("Marnwick — deleting image…")
            return
        self.navigator.index = min(removed_index, len(self.navigator.order) - 1)
        self.load_current()
        self.setWindowTitle("Marnwick — deleting image…")

    def image_delete_finished(self, rel_path: str, *, path_removed: bool) -> None:
        pending = self._pending_image_delete
        if self._load_closed or pending is None or pending.rel_path != rel_path:
            return
        self._pending_image_delete = None
        pending_step = self._pending_navigation_step
        self._pending_navigation_step = 0
        navigator = self.navigator
        self.setWindowTitle("Marnwick")
        if not path_removed:
            current_rel_path = navigator.current if navigator.order else None
            restored_index = min(pending.removed_index, len(navigator.order))
            navigator.order.insert(restored_index, rel_path)
            if isinstance(navigator, PagedImageNavigator):
                navigator._seen.add(rel_path)
            if current_rel_path is None:
                navigator.index = restored_index
                self.load_current()
            else:
                navigator.index = navigator.order.index(current_rel_path)
                self.update_info_overlay()
            if self._navigation_rebuild_required and isinstance(
                navigator,
                PagedImageNavigator,
            ):
                self.invalidate_paged_navigation()
                return
            if pending_step > 0 and isinstance(navigator, PagedImageNavigator):
                self._start_navigation_page(pending_step)
            return

        if (
            isinstance(navigator, PagedImageNavigator)
            and navigator.view_kind == VIRTUAL_KIND_DUPLICATES
        ):
            # Removing one duplicate can make several surviving singletons
            # disappear from this query. Its membership shrink is not a
            # one-row OFFSET adjustment, so rebuild from a fresh page zero.
            navigator._seen.discard(rel_path)
            self.invalidate_paged_navigation()
            return
        if isinstance(navigator, PagedImageNavigator):
            # The removed image occupied one row inside the already consumed
            # database prefix. Rebase OFFSET only after the worker confirms
            # that the path is gone; failure restoration must retain the old
            # cursor unchanged.
            navigator.next_offset = max(0, navigator.next_offset - 1)
            navigator.total_count = max(
                len(navigator.order),
                navigator.total_count - 1,
            )
            navigator._seen.discard(rel_path)
            navigator.has_more = bool(
                navigator.has_more and navigator.total_count > len(navigator.order)
            )
            if self._navigation_rebuild_required:
                self.invalidate_paged_navigation()
                return
        if navigator.order:
            self.update_info_overlay()
            if pending_step > 0 and isinstance(navigator, PagedImageNavigator):
                self._start_navigation_page(pending_step)
            return
        if isinstance(navigator, PagedImageNavigator) and navigator.has_more:
            self._start_navigation_page(0, select_first=True)
            return
        self.accept()

    def image_delete_postcondition_unknown(self, rel_path: str) -> None:
        """Close a viewer whose filesystem could not prove either outcome.

        The main pane performs its normal background refresh. Keeping an
        optimistic or restored OFFSET cursor here would assert a fact the
        mutation worker could not verify after an I/O failure.
        """

        pending = self._pending_image_delete
        if self._load_closed or pending is None or pending.rel_path != rel_path:
            return
        self._pending_image_delete = None
        self.accept()

    def image_delete_queued(self, rel_path: str) -> None:
        """Compatibility alias for callers that only report queue admission."""

        self.image_delete_started(rel_path)

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
        self.loaded_file_dates = None
        self.image_coordinate_size = (0, 0)
        if isinstance(parent, MainWindow):
            self.base_pixmap = QPixmap()
            self.label.clear_display_pixmap()
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.label.setText(f"Loading {Path(self.navigator.current).name}…")
            self._start_async_load(self.navigator.current)
            return
        try:
            path = self.catalog.mutation_path(self.navigator.current)
            if not path.is_file():
                raise FileNotFoundError(path)
            identity, original_file_dates = snapshot_image_file_identity_with_dates(path)
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
            self.loaded_file_dates = original_file_dates
            self.image_coordinate_size = (self.base_pixmap.width(), self.base_pixmap.height())
            retained_operations: tuple[EditOperation, ...] = ()
            if isinstance(parent, MainWindow):
                retained_operations = parent.take_failed_image_edit(
                    self.catalog,
                    self.navigator.current,
                )
            if retained_operations:
                self.operations.extend(retained_operations)
                self.render_preview()
                if not self.operations and isinstance(parent, MainWindow):
                    parent.retain_failed_image_edit(
                        self.catalog,
                        self.navigator.current,
                        retained_operations,
                    )
                self.update_info_overlay()
                return
            movie_bytes = (
                self._read_verified_movie_bytes(path, identity)
                if path.suffix.casefold() == ".gif"
                else None
            )
            if movie_bytes is not None and self.start_movie(movie_bytes):
                self.update_info_overlay()
                return
            if self.base_pixmap.isNull():
                raise ValueError(f"Unable to decode {path.name}")
        except Exception as error:
            self.load_error = str(error)
            self.base_pixmap = QPixmap()
            self.label.clear_display_pixmap()
            self.label.setText(f"Unable to display image\n{error}")
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.update_info_overlay()
            return
        self._fit_pixmap()
        self.update_info_overlay()

    @staticmethod
    def _stat_matches_image_identity(
        file_stat: os.stat_result,
        identity: ImageFileIdentity,
    ) -> bool:
        return stat.S_ISREG(file_stat.st_mode) and (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_nlink),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
            int(getattr(file_stat, "st_ctime_ns", 0)),
        ) == (
            identity.device,
            identity.inode,
            identity.link_count,
            identity.size,
            identity.modified_ns,
            identity.changed_ns,
        )

    @classmethod
    def _read_verified_movie_bytes(
        cls,
        path: Path,
        identity: ImageFileIdentity,
    ) -> bytes | None:
        """Read a bounded animation snapshot from the already-verified inode."""

        if identity.size <= 0 or identity.size > MAX_ANIMATED_IMAGE_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not cls._stat_matches_image_identity(os.fstat(fd), identity):
                raise OSError(f"{path.name} changed before animation loading")
            remaining = identity.size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError(f"{path.name} ended while animation was loading")
                chunks.append(chunk)
                remaining -= len(chunk)
            if not cls._stat_matches_image_identity(os.fstat(fd), identity):
                raise OSError(f"{path.name} changed while animation was loading")
            return b"".join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _load_viewer_image(catalog: Catalog, rel_path: str) -> ViewerLoadResult:
        catalog._assert_catalog_root_identity()  # noqa: SLF001 - stale worker guard
        path = catalog.mutation_path(rel_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        identity, original_file_dates = snapshot_image_file_identity_with_dates(path)
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        source_size = validate_qimage_reader_size(reader, path)
        decode_size = QSize(source_size)
        if (
            decode_size.width() > MAX_INTERACTIVE_PREVIEW_DIMENSION
            or decode_size.height() > MAX_INTERACTIVE_PREVIEW_DIMENSION
        ):
            decode_size.scale(
                QSize(
                    MAX_INTERACTIVE_PREVIEW_DIMENSION,
                    MAX_INTERACTIVE_PREVIEW_DIMENSION,
                ),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            reader.setScaledSize(decode_size)
        image = reader.read()
        movie_bytes = (
            FullscreenViewer._read_verified_movie_bytes(path, identity)
            if path.suffix.casefold() == ".gif"
            else None
        )
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
        catalog._assert_catalog_root_identity()  # noqa: SLF001 - reject replaced catalog
        if image.isNull():
            raise ValueError(f"Unable to decode {path.name}")
        # QImage is the cross-thread display payload, so annotate it with the
        # full oriented coordinate size before returning the bounded decode.
        # This keeps crop/clone coordinates exact without retaining a huge
        # GUI-side pixmap merely to remember the original dimensions.
        direct_error = abs(
            image.width() * source_size.height()
            - image.height() * source_size.width()
        )
        swapped_error = abs(
            image.width() * source_size.width()
            - image.height() * source_size.height()
        )
        if swapped_error < direct_error:
            coordinate_size = (source_size.height(), source_size.width())
        else:
            coordinate_size = (source_size.width(), source_size.height())
        image.setText("marnwick-coordinate-width", str(coordinate_size[0]))
        image.setText("marnwick-coordinate-height", str(coordinate_size[1]))
        return ViewerLoadResult(
            rel_path,
            path,
            identity,
            original_file_dates,
            image,
            movie_bytes,
        )

    def _start_async_load(self, rel_path: str) -> None:
        if self._load_closed:
            return
        self._load_generation += 1
        generation = self._load_generation
        previous = self._load_future
        if previous is not None and not previous.done():
            previous.cancel()
        self._load_future = None
        self._pending_load_request = (generation, rel_path)
        self._try_submit_pending_load()

    def _try_submit_pending_load(self) -> None:
        request = self._pending_load_request
        if self._load_closed or request is None or self._load_future is not None:
            return
        generation, rel_path = request
        try:
            self._load_future = self._navigation_executor.submit(
                self._load_viewer_image,
                self.catalog,
                rel_path,
            )
        except RuntimeError:
            # A closed lease is terminal only during viewer shutdown. Retain
            # the request otherwise so the normal timer path remains explicit
            # and recoverable for custom lease implementations.
            self.load_error = "Viewer image worker is unavailable; reload to retry"
            return
        self._load_future_generation = generation
        self._load_timer.start()

    def _settle_async_load(self) -> None:
        future = self._load_future
        if future is None or not future.done():
            return
        generation = self._load_future_generation
        self._load_future = None
        if future.cancelled() or generation != self._load_generation or self._load_closed:
            if not self._load_closed:
                self._try_submit_pending_load()
            if self._load_future is None and self._pending_load_request is None:
                self._load_timer.stop()
            return
        try:
            result = future.result()
        except ExecutorSaturatedError:
            request = self._pending_load_request
            if (
                request is not None
                and request[0] == generation
                and request[1] == self.navigator.current
                and not self._load_closed
            ):
                self.label.setText(
                    f"Waiting to load {Path(request[1]).name}…"
                )
                self._try_submit_pending_load()
                self._load_timer.start()
            return
        except Exception as error:
            if generation != self._load_generation:
                return
            if self._pending_load_request == (generation, self.navigator.current):
                self._pending_load_request = None
            self._load_timer.stop()
            self.load_error = str(error)
            self.base_pixmap = QPixmap()
            self.label.clear_display_pixmap()
            self.label.setText(f"Unable to display image\n{error}")
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.update_info_overlay()
            return
        if result.rel_path != self.navigator.current or generation != self._load_generation:
            return
        if self._pending_load_request == (generation, result.rel_path):
            self._pending_load_request = None
        self._load_timer.stop()
        self.base_pixmap = QPixmap.fromImage(result.image)
        try:
            coordinate_width = int(result.image.text("marnwick-coordinate-width"))
            coordinate_height = int(result.image.text("marnwick-coordinate-height"))
        except (TypeError, ValueError):
            coordinate_width = result.image.width()
            coordinate_height = result.image.height()
        self.image_coordinate_size = (
            max(1, coordinate_width),
            max(1, coordinate_height),
        )
        self.loaded_file_identity = result.identity
        self.loaded_file_dates = result.original_file_dates
        parent = self.parent()
        retained_operations: tuple[EditOperation, ...] = ()
        if isinstance(parent, MainWindow):
            retained_operations = parent.take_failed_image_edit(self.catalog, result.rel_path)
        if retained_operations:
            # ``take`` used to make a preview failure permanently lose the
            # user's restored edits. Put the safety copy straight back and
            # consume it only after the matching preview succeeds.
            assert isinstance(parent, MainWindow)
            parent.retain_failed_image_edit(self.catalog, result.rel_path, retained_operations)
            self._retained_preview_rel = result.rel_path
            self._retained_preview_operations = retained_operations
            self.operations.extend(retained_operations)
            self.render_preview()
            self.update_info_overlay()
            return
        self._fit_pixmap()
        if result.movie_bytes is not None:
            self._queue_movie_validation(result, generation)
        self.update_info_overlay()

    @classmethod
    def _movie_identity_is_current(
        cls,
        catalog: Catalog,
        rel_path: str,
        expected_identity: ImageFileIdentity,
    ) -> bool:
        """Revalidate an animation snapshot without filesystem work on Qt."""

        try:
            catalog._assert_catalog_root_identity()  # noqa: SLF001 - worker guard
            path = catalog.mutation_path(rel_path)
            current_stat = path.stat(follow_symlinks=False)
            catalog._assert_catalog_root_identity()  # noqa: SLF001 - worker guard
        except OSError:
            return False
        return cls._stat_matches_image_identity(current_stat, expected_identity)

    def _queue_movie_validation(
        self,
        result: ViewerLoadResult,
        generation: int,
    ) -> None:
        movie_bytes = result.movie_bytes
        if movie_bytes is None or self._load_closed:
            return
        self._cancel_movie_validation()
        try:
            future = self._navigation_executor.submit(
                self._movie_identity_is_current,
                self.catalog,
                result.rel_path,
                result.identity,
            )
        except RuntimeError:
            return
        self._pending_movie_bytes = movie_bytes
        self._movie_validation_future = future
        self._movie_validation_generation = generation
        self._movie_validation_timer.start()

    def _settle_movie_validation(self) -> None:
        future = self._movie_validation_future
        if future is None or not future.done():
            return
        generation = self._movie_validation_generation
        movie_bytes = self._pending_movie_bytes
        self._movie_validation_future = None
        self._pending_movie_bytes = None
        self._movie_validation_timer.stop()
        if (
            future.cancelled()
            or movie_bytes is None
            or self._load_closed
            or generation != self._load_generation
        ):
            return
        try:
            identity_is_current = future.result()
        except Exception:
            identity_is_current = False
        if identity_is_current and self.start_movie(movie_bytes):
            self.update_info_overlay()

    def _cancel_movie_validation(self) -> None:
        self._movie_validation_timer.stop()
        future = self._movie_validation_future
        if future is not None:
            future.cancel()
        self._movie_validation_future = None
        self._pending_movie_bytes = None

    def _shutdown_async_load(self) -> None:
        if self._load_closed:
            return
        self._load_closed = True
        self._load_generation += 1
        self._pending_load_request = None
        self._load_timer.stop()
        self._navigation_page_timer.stop()
        if self._navigation_page_cancel_event is not None:
            self._navigation_page_cancel_event.set()
            self._navigation_page_cancel_event = None
        if self._navigation_page_future is not None:
            self._navigation_page_future.cancel()
            self._navigation_page_future = None
        self._navigation_page_select_first = False
        self._navigation_page_replace_all = False
        self._reset_navigation_rebuild()
        self._pending_image_delete = None
        self._cancel_movie_validation()
        self._cancel_preview_render()
        if self._load_future is not None:
            self._load_future.cancel()
            self._load_future = None
        self._navigation_executor.shutdown(wait=False, cancel_futures=True)
        self._navigation_page_executor.shutdown(wait=False, cancel_futures=True)
        self._preview_executor.shutdown(wait=False, cancel_futures=True)

    def restore_failed_edit_operations(
        self,
        rel_path: str,
        operations: tuple[EditOperation, ...],
    ) -> bool:
        if (
            not self.navigator.order
            or self.navigator.current != rel_path
            or self.operations
        ):
            return False
        self.operations.extend(operations)
        self.render_preview()
        return bool(self.operations)

    def _commit_retained_preview_backup(
        self,
        rel_path: str,
        rendered_operations: tuple[EditOperation, ...],
    ) -> None:
        retained = self._retained_preview_operations
        if (
            not retained
            or self._retained_preview_rel != rel_path
            or rendered_operations[: len(retained)] != retained
        ):
            return
        self._release_retained_preview_backup(rel_path)

    def _release_retained_preview_backup(self, rel_path: str) -> None:
        retained = self._retained_preview_operations
        if not retained or self._retained_preview_rel != rel_path:
            return
        parent = self.parent()
        if isinstance(parent, MainWindow):
            stored = parent.take_failed_image_edit(self.catalog, rel_path)
            # Do not erase a newer queued-save failure that happened to land
            # while this preview was rendering.
            if stored and stored != retained:
                parent.retain_failed_image_edit(self.catalog, rel_path, stored)
        self._retained_preview_rel = None
        self._retained_preview_operations = ()

    def image_save_started(self, rel_path: str) -> None:
        self._pending_save_rels.add(rel_path)
        if self.navigator.order and self.navigator.current == rel_path:
            self.exit_region_edit()

    def image_save_failed(self, rel_path: str) -> None:
        self._pending_save_rels.discard(rel_path)

    def image_save_reconciled(self, rel_path: str) -> None:
        self._pending_save_rels.discard(rel_path)
        self.invalidate_paged_navigation()
        if (
            not self._load_closed
            and self.navigator.order
            and self.navigator.current == rel_path
        ):
            self.load_current()

    def image_save_reconcile_failed(self, rel_path: str) -> None:
        self._pending_save_rels.discard(rel_path)
        if (
            not self._load_closed
            and self.navigator.order
            and self.navigator.current == rel_path
        ):
            self.load_current()

    def can_edit_current(self) -> bool:
        if not self.navigator.order:
            return False
        if self._navigation_rebuild_required:
            return False
        if self.navigator.current in self._pending_save_rels:
            return False
        if self._load_future is not None and not self._load_future.done():
            return False
        return self._preview_future is None

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
        if not self.navigator.order:
            return "Loading the next image…"
        identity = self.loaded_file_identity
        file_date = (
            datetime.fromtimestamp(identity.modified_ns / 1_000_000_000).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if identity is not None
            else "Unavailable"
        )
        total = (
            self.navigator.total_count
            if isinstance(self.navigator, PagedImageNavigator)
            else len(self.navigator.order)
        )
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

    def start_movie(self, movie_bytes: bytes) -> bool:
        """Start a worker-captured animation without opening its path on Qt."""

        if not movie_bytes or len(movie_bytes) > MAX_ANIMATED_IMAGE_BYTES:
            return False
        self._dispose_movie()
        movie_data = QByteArray(movie_bytes)
        buffer = QBuffer(movie_data, self)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            buffer.deleteLater()
            return False
        movie = QMovie(buffer, b"gif", self)
        if not movie.isValid():
            buffer.close()
            buffer.deleteLater()
            movie.deleteLater()
            return False
        # CacheAll retains every decoded animation frame and can exhaust memory
        # on long GIFs. CacheNone plus a viewport-bounded decoder size keeps
        # both the streaming frame and the displayed source pixmap bounded.
        movie.setCacheMode(QMovie.CacheMode.CacheNone)
        movie.setScaledSize(self._bounded_movie_decode_size())
        self.label.clear_display_pixmap()
        self._movie_data = movie_data
        self._movie_buffer = buffer
        self.movie = movie
        movie.frameChanged.connect(self._display_movie_frame)
        movie.start()
        self._display_movie_frame()
        return True

    def _bounded_movie_decode_size(self) -> QSize:
        source_size = self.base_pixmap.size()
        if source_size.isEmpty():
            source_size = QSize(1, 1)
        device_pixel_ratio = self.image_device_pixel_ratio()
        bounds = physical_size_for_logical(self.label.size(), device_pixel_ratio)
        bounds.setWidth(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.width()))
        bounds.setHeight(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.height()))
        source_size.scale(bounds, Qt.AspectRatioMode.KeepAspectRatio)
        return QSize(max(1, source_size.width()), max(1, source_size.height()))

    def _display_movie_frame(self, _frame_number: int = -1) -> None:
        movie = self.movie
        if movie is None or self.display_preview_image is not None:
            return
        frame = movie.currentPixmap()
        if frame.isNull():
            return
        self.label.set_display_pixmap(frame, self.displayed_image_rect())

    def stop_movie(self) -> None:
        self._cancel_movie_validation()
        self._dispose_movie()
        self.label.clear_display_pixmap()

    def _dispose_movie(self) -> None:
        movie = self.movie
        movie_data = self._movie_data
        buffer = self._movie_buffer
        self.movie = None
        self._movie_data = None
        self._movie_buffer = None
        if movie is not None:
            with suppress(RuntimeError, TypeError):
                movie.frameChanged.disconnect(self._display_movie_frame)
            movie.stop()
            movie.deleteLater()
        if buffer is not None:
            buffer.close()
            buffer.deleteLater()
        del movie_data

    def _fit_pixmap(self) -> None:
        if self.movie is not None and self.display_preview_image is None:
            bounded_size = self._bounded_movie_decode_size()
            if self.movie.scaledSize() != bounded_size:
                self.movie.setScaledSize(bounded_size)
            self._display_movie_frame()
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
        # QPainter scales this bounded source directly into the clipped target
        # rect. In particular, MAX_ZOOM cannot allocate a 16x pixmap here.
        display = QPixmap(self.base_pixmap)
        display.setDevicePixelRatio(self.image_device_pixel_ratio())
        self.label.set_display_pixmap(display, display_rect)

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
        if not self.navigator.order:
            return
        target_rel_path = self.navigator.current
        if self.operations:
            if not self.confirm_pending_edits():
                return
            # A nested save/discard prompt can process mutation settlements.
            # Do not silently retarget the tag command if that changed the
            # displayed row while the prompt was open.
            if (
                not self.navigator.order
                or self.navigator.current != target_rel_path
            ):
                return
        if target_rel_path in self._pending_save_rels:
            # Saving is asynchronous. Opening a tag writer concurrently would
            # either race the encoded replacement or be rejected by the
            # catalog mutation lane, so leave the viewer responsive and let
            # the user invoke Tags again after reconciliation completes.
            self.setWindowTitle("Marnwick — finish saving before editing tags…")
            return
        dialog: TagDialog | None = None

        def exec_dialog() -> int:
            nonlocal dialog
            dialog = TagDialog(self.catalog, target_rel_path, self)
            return int(dialog.exec())

        accepted = self.run_with_visible_cursor(exec_dialog) == int(QDialog.DialogCode.Accepted)
        selected_tags = dialog.selected_tags() if accepted and dialog is not None else []
        expected_identity = (
            dialog.loaded_file_identity
            if accepted and dialog is not None
            else None
        )
        if dialog is not None:
            dialog.deleteLater()
        if accepted:
            parent = self.parent()
            if isinstance(parent, MainWindow):
                if target_rel_path in self._pending_save_rels:
                    self.setWindowTitle("Marnwick — finish saving before editing tags…")
                    return
                if expected_identity is None:
                    show_error(
                        self,
                        "Tags",
                        "The image identity was unavailable; tags were not changed.",
                    )
                    return
                parent.queue_image_tags(
                    self.catalog,
                    target_rel_path,
                    selected_tags,
                    expected_identity=expected_identity,
                    owner=self,
                )
            else:
                show_error(
                    self,
                    "Tags",
                    "This standalone viewer cannot safely change tags without its catalog window.",
                )

    def open_edit_tools(self) -> None:
        if not self.navigator.order:
            return
        if not self.can_edit_current():
            return
        if self.base_pixmap.isNull() or self.load_error is not None:
            show_error(self, "Edit Image", self.load_error or "The image could not be decoded.")
            return
        dialog: EditCommandDialog | None = None

        def exec_dialog() -> int:
            nonlocal dialog
            dialog = EditCommandDialog(self)
            return int(dialog.exec())

        accepted = self.run_with_visible_cursor(exec_dialog) == int(QDialog.DialogCode.Accepted)
        command = dialog.selected_command() if accepted and dialog is not None else None
        if dialog is not None:
            dialog.deleteLater()
        if not accepted or command is None:
            return
        if command in {"rotate_left", "rotate_right", "flip_horizontal", "flip_vertical"}:
            self.apply_instant_operation(command)
        elif command == "crop":
            self.start_region_edit("crop")
        elif command == "red_eye":
            self.start_region_edit("red_eye")
        elif command == "clone_heal":
            self.start_region_edit("clone_heal")

    def apply_instant_operation(self, name: str) -> None:
        if not self.can_edit_current():
            return
        self.exit_region_edit()
        self.operations.append(EditOperation(name))
        self.render_preview()

    def start_region_edit(self, mode: str) -> None:
        if not self.can_edit_current():
            return
        if self.movie is not None:
            self.stop_movie()
            self._fit_pixmap()
        self.edit_mode = mode
        self.drag_origin = None
        self.rubber_band.hide()
        self.clone_source_center = None
        self.clone_painting = False
        self.clone_alignment_target = None
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
        self.clone_alignment_target = None
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
        focus_widget = QApplication.focusWidget()
        self.restore_cursor_visibility()
        try:
            return callback()
        finally:
            self.update_cursor_visibility()
            restore_window_after_modal(self, focus_widget=focus_widget)

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
        if not self.can_edit_current():
            return
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
        image_width, image_height = self.image_coordinate_size
        if image_width <= 0 or image_height <= 0:
            image_width, image_height = self.base_pixmap.width(), self.base_pixmap.height()
        display_rect = self.displayed_image_rect()
        clipped = rect.intersected(display_rect)
        if clipped.width() < 2 or clipped.height() < 2:
            return None
        scale_x = image_width / display_rect.width()
        scale_y = image_height / display_rect.height()
        left = int((clipped.left() - display_rect.left()) * scale_x)
        top = int((clipped.top() - display_rect.top()) * scale_y)
        right = int((clipped.right() + 1 - display_rect.left()) * scale_x)
        bottom = int((clipped.bottom() + 1 - display_rect.top()) * scale_y)
        left = max(0, min(left, image_width - 1))
        top = max(0, min(top, image_height - 1))
        right = max(left + 1, min(right, image_width))
        bottom = max(top + 1, min(bottom, image_height))
        return left, top, right, bottom

    def image_point_from_label_point(self, point: QPoint) -> tuple[int, int] | None:
        if self.base_pixmap.isNull():
            return None
        image_width, image_height = self.image_coordinate_size
        if image_width <= 0 or image_height <= 0:
            image_width, image_height = self.base_pixmap.width(), self.base_pixmap.height()
        display_rect = self.displayed_image_rect()
        if not display_rect.contains(point) or display_rect.width() <= 0 or display_rect.height() <= 0:
            return None
        scale_x = image_width / display_rect.width()
        scale_y = image_height / display_rect.height()
        x = int((point.x() - display_rect.left()) * scale_x)
        y = int((point.y() - display_rect.top()) * scale_y)
        x = max(0, min(x, image_width - 1))
        y = max(0, min(y, image_height - 1))
        return x, y

    def image_radius_from_label_radius(self) -> int:
        if self.base_pixmap.isNull():
            return 1
        display_rect = self.displayed_image_rect()
        if display_rect.width() <= 0:
            return max(1, self.clone_brush_radius_label)
        image_width = self.image_coordinate_size[0] or self.base_pixmap.width()
        scale = image_width / display_rect.width()
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
        if not self.can_edit_current():
            return
        if self.clone_source_center is None:
            return
        if self.clone_alignment_target is None:
            self.clone_alignment_target = target
        image_radius = self.image_radius_from_label_radius()
        operations: list[EditOperation] = []
        for sample in self.clone_stroke_samples(self.clone_last_target, target, image_radius):
            delta_x = sample[0] - self.clone_alignment_target[0]
            delta_y = sample[1] - self.clone_alignment_target[1]
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
        self._cancel_preview_render()
        self.preview_image = None
        self.preview_image_current = False
        self.display_preview_image = None
        self.display_preview_size = None
        if not self.operations or self._load_closed or not self.navigator.order:
            return
        self._preview_generation += 1
        generation = self._preview_generation
        cancel_event = Event()
        self._preview_cancel_event = cancel_event
        operations = tuple(self.operations)
        rel_path = self.navigator.current
        device_pixel_ratio = self.image_device_pixel_ratio()
        bounds = physical_size_for_logical(self.label.size(), device_pixel_ratio)
        bounds.setWidth(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.width()))
        bounds.setHeight(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.height()))
        max_size = (max(1, bounds.width()), max(1, bounds.height()))
        self._pending_preview_request = (
            generation,
            self.current_path,
            rel_path,
            operations,
            max_size,
            cancel_event,
        )
        self._try_submit_pending_preview()

    def _try_submit_pending_preview(self) -> None:
        request = self._pending_preview_request
        if self._load_closed or request is None or self._preview_future is not None:
            return
        generation, path, rel_path, operations, max_size, cancel_event = request
        try:
            self._preview_future = self._preview_executor.submit(
                self._render_preview_worker,
                path,
                rel_path,
                operations,
                max_size,
                cancel_event,
            )
        except RuntimeError:
            self.load_error = "Edit preview worker is unavailable; edit again to retry"
            return
        self._preview_future_generation = generation
        self._preview_timer.start()

    @staticmethod
    def _render_preview_worker(
        path: Path,
        rel_path: str,
        operations: tuple[EditOperation, ...],
        max_size: tuple[int, int],
        cancel_event: Event,
    ) -> PreviewRenderResult:
        with open_catalog_image(path) as image:
            edited = ImageOps.exif_transpose(image).copy()
            for operation in operations:
                if cancel_event.is_set():
                    raise IndexTaskCancelled()
                edited = apply_operation_to_image(edited, operation)
            image_size = edited.size
            edited.thumbnail(max_size, Image.Resampling.LANCZOS)
            display = edited.convert("RGBA" if "A" in edited.getbands() else "RGB")
            return PreviewRenderResult(rel_path, operations, display.copy(), image_size)

    def _settle_preview_render(self) -> None:
        future = self._preview_future
        if future is None or not future.done():
            return
        generation = self._preview_future_generation
        self._preview_future = None
        if future.cancelled() or generation != self._preview_generation or self._load_closed:
            if not self._load_closed:
                self._try_submit_pending_preview()
            if self._preview_future is None and self._pending_preview_request is None:
                self._preview_timer.stop()
            return
        try:
            result = future.result()
        except ExecutorSaturatedError:
            request = self._pending_preview_request
            if (
                request is not None
                and request[0] == generation
                and request[2] == self.navigator.current
                and request[3] == tuple(self.operations)
                and not self._load_closed
            ):
                self._try_submit_pending_preview()
                self._preview_timer.start()
            return
        except IndexTaskCancelled:
            self._pending_preview_request = None
            self._preview_cancel_event = None
            self._preview_timer.stop()
            return
        except Exception as error:
            # The source pixels remain usable even when an edit preview could
            # not be rendered. Keep every operation (and MainWindow's safety
            # copy, when this was a restored failed save) so retry/save/discard
            # remains an explicit user decision.
            self.preview_image_current = False
            self.display_preview_image = None
            self.display_preview_size = None
            self._pending_preview_request = None
            self._preview_cancel_event = None
            self._preview_timer.stop()
            self._fit_pixmap()
            show_error(self, "Edit Image", str(error))
            return
        if (
            result.rel_path != self.navigator.current
            or result.operations != tuple(self.operations)
            or generation != self._preview_generation
        ):
            return
        self._pending_preview_request = None
        self._preview_cancel_event = None
        self._preview_timer.stop()
        self.image_coordinate_size = result.image_size
        self.preview_image_current = True
        self.display_preview_image = result.image
        self.display_preview_size = result.image.size
        self.base_pixmap = pixmap_from_pil_image(
            result.image,
            device_pixel_ratio=self.image_device_pixel_ratio(),
        )
        self._commit_retained_preview_backup(result.rel_path, result.operations)
        self._fit_pixmap()

    def _cancel_preview_render(self) -> None:
        self._preview_generation += 1
        self._preview_timer.stop()
        if self._preview_cancel_event is not None:
            self._preview_cancel_event.set()
            self._preview_cancel_event = None
        if self._preview_future is not None:
            self._preview_future.cancel()
            self._preview_future = None
        self._pending_preview_request = None

    def apply_operations_to_preview(self, operations: list[EditOperation]) -> None:
        if not self.can_edit_current():
            return
        self.operations.extend(operations)
        self.render_preview()

    def display_preview_target_size(self) -> tuple[int, int]:
        display_rect = self.displayed_image_rect()
        device_pixel_ratio = self.image_device_pixel_ratio()
        target = QSize(
            max(1, int(round(display_rect.width() * device_pixel_ratio))),
            max(1, int(round(display_rect.height() * device_pixel_ratio))),
        )
        bounds = physical_size_for_logical(self.label.size(), device_pixel_ratio)
        bounds.setWidth(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.width()))
        bounds.setHeight(min(MAX_INTERACTIVE_PREVIEW_DIMENSION, bounds.height()))
        if target.width() > bounds.width() or target.height() > bounds.height():
            target.scale(bounds, Qt.AspectRatioMode.KeepAspectRatio)
        return target.width(), target.height()

    def ensure_display_preview_image(self) -> Image.Image:
        target_size = self.display_preview_target_size()
        if self.display_preview_image is not None and self.display_preview_size == target_size:
            return self.display_preview_image
        if self.display_preview_image is not None:
            display = self.display_preview_image.resize(target_size, Image.Resampling.BILINEAR)
        else:
            display = pil_image_from_pixmap(self.base_pixmap, target_size)
        self.display_preview_image = display
        self.display_preview_size = target_size
        return display

    def rebuild_display_preview(self) -> None:
        if self.display_preview_image is None:
            return
        target_size = self.display_preview_target_size()
        self.display_preview_image = self.display_preview_image.resize(
            target_size,
            Image.Resampling.BILINEAR,
        )
        self.display_preview_size = target_size

    def apply_clone_operations_to_display(self, operations: list[EditOperation]) -> None:
        display = self.ensure_display_preview_image()
        width, height = self.display_preview_target_size()
        image_width = self.image_coordinate_size[0] or self.base_pixmap.width()
        image_height = self.image_coordinate_size[1] or self.base_pixmap.height()
        scale_x = width / max(1, image_width)
        scale_y = height / max(1, image_height)
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
        if not self.navigator.order:
            return False
        # Bind the prompt to the exact displayed incarnation. Modal dialogs
        # run a nested event loop, so background mutation settlement must not
        # be able to retarget these operations to a different image.
        rel_path = self.navigator.current
        operations = tuple(self.operations)
        expected_identity = self.loaded_file_identity
        original_file_dates = self.loaded_file_dates
        response = self.run_with_visible_cursor(lambda: ask_save_edits(self))
        if response == "cancel":
            return False
        committed_warning: str | None = None
        if response in {"save", "save_preserve_date"}:
            try:
                parent = self.parent()
                if isinstance(parent, MainWindow):
                    parent.queue_image_edit(
                        self.catalog,
                        rel_path,
                        operations,
                        preserve_file_dates=response == "save_preserve_date",
                        expected_identity=expected_identity,
                        original_file_dates=original_file_dates,
                        owner=self,
                    )
                else:
                    try:
                        apply_operations_to_file(
                            self.catalog.mutation_path(rel_path),
                            operations,
                            preserve_timestamp=False,
                            preserve_file_dates=response == "save_preserve_date",
                            expected_identity=expected_identity,
                            original_file_dates=original_file_dates,
                        )
                    except ImageSaveCommittedError as error:
                        committed_warning = str(error)
                    with suppress(Exception):
                        self.catalog.rebuild_thumbnail(rel_path)
            except (OSError, ValueError, UnsafeImageSaveError) as error:
                show_error(self, "Save Image", str(error))
                return False
        # Saving has transferred the operations to the protected mutation
        # lane; discarding is an equally explicit user decision. In either
        # case the preview-restoration safety copy is no longer needed.
        self._release_retained_preview_backup(rel_path)
        self.operations.clear()
        self.cleanup_preview()
        if committed_warning is not None:
            show_error(
                self,
                "Save Image Warning",
                "The image edit was saved, but a later cleanup or durability step reported a warning:\n\n"
                + committed_warning,
            )
        return True

    def cleanup_preview(self) -> None:
        self._cancel_preview_render()
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


def validate_qimage_reader_size(reader: QImageReader, path: Path) -> QSize:
    size = reader.size()
    width = size.width()
    height = size.height()
    if width <= 0 or height <= 0:
        raise ValueError(f"Unable to determine image dimensions for {path.name}")
    pixel_count = width * height
    if pixel_count > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"image exceeds configured pixel limit: {pixel_count} > {MAX_IMAGE_PIXELS}"
        )
    return size


def load_oriented_pixmap(path: Path) -> QPixmap:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    validate_qimage_reader_size(reader, path)
    image = reader.read()
    if not image.isNull():
        return QPixmap.fromImage(image)
    return QPixmap()


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


def pil_image_from_pixmap(pixmap: QPixmap, target_size: tuple[int, int]) -> Image.Image:
    if pixmap.isNull():
        return Image.new("RGB", target_size, "black")
    width, height = target_size
    scaled = pixmap.scaled(
        QSize(max(1, width), max(1, height)),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    image = scaled.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    data = image.constBits().tobytes()
    return Image.frombuffer(
        "RGBA",
        (image.width(), image.height()),
        data,
        "raw",
        "RGBA",
        image.bytesPerLine(),
        1,
    ).copy()


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
    confirmed = box.clickedButton() == delete_button
    box.deleteLater()
    return confirmed


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
    confirmed = box.clickedButton() == delete_button
    box.deleteLater()
    return confirmed


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
    confirmed = box.clickedButton() == delete_button
    box.deleteLater()
    return confirmed


def ask_save_edits(parent: QWidget) -> str:
    owner = visible_modal_dialog_owner(parent)
    focus_widget = QApplication.focusWidget()
    box, save_button, preserve_button, discard_button = create_save_edits_message_box(owner)
    box.exec()
    clicked = box.clickedButton()
    box.hide()
    box.deleteLater()
    restore_window_after_modal(owner, focus_widget=focus_widget)
    if clicked == save_button:
        return "save"
    if clicked == preserve_button:
        return "save_preserve_date"
    if clicked == discard_button:
        return "discard"
    return "cancel"


def ask_discard_failed_edits_on_exit(parent: QWidget, count: int) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle("Unsaved Image Edits")
    box.setIcon(QMessageBox.Icon.Warning)
    noun = "image" if count == 1 else "images"
    box.setText(f"Edits for {count} {noun} could not be saved.")
    box.setInformativeText(
        "Keep Marnwick open to retry them, or explicitly discard those edits and quit."
    )
    box.setStyleSheet(DIALOG_STYLESHEET)
    discard_button = box.addButton(
        "Discard Failed Edits and Quit",
        QMessageBox.ButtonRole.DestructiveRole,
    )
    keep_button = box.addButton("Keep Marnwick Open", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep_button)
    box.setEscapeButton(keep_button)
    style_message_box_buttons(box)
    box.exec()
    confirmed = box.clickedButton() == discard_button
    box.deleteLater()
    return confirmed


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


def visible_modal_dialog_owner(parent: QWidget) -> QWidget:
    """Return a visible owner that cannot be trapped behind an active modal."""

    modal = QApplication.activeModalWidget()
    if modal is not None:
        try:
            if modal.isVisible():
                return modal
        except RuntimeError:
            pass
    try:
        if QWidget.window(parent).isVisible():
            return parent
    except RuntimeError:
        pass
    active = QApplication.activeWindow()
    if active is not None:
        try:
            if active.isVisible():
                return active
        except RuntimeError:
            pass
    return parent


def restore_window_after_modal(
    owner: QWidget,
    *,
    focus_widget: QWidget | None = None,
) -> None:
    """Restore an owned window after a nested dialog releases its focus.

    The immediate activation handles ordinary Qt behavior.  The queued retry
    runs after the closed dialog's native focus events, which is important for
    fullscreen viewers on window managers that otherwise activate the covered
    main window.
    """

    try:
        window = QWidget.window(owner)
    except RuntimeError:
        return

    def reactivate() -> None:
        try:
            if not window.isVisible() or window.isMinimized():
                return
            active_modal = QApplication.activeModalWidget()
            if active_modal is not None and active_modal is not window:
                try:
                    if active_modal.isVisible():
                        return
                except RuntimeError:
                    pass
            window.raise_()
            window.activateWindow()
            target = focus_widget
            if (
                target is None
                or not target.isVisible()
                or not (target is window or window.isAncestorOf(target))
            ):
                target = window
            target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        except RuntimeError:
            # A queued activation may race owner destruction after the dialog
            # accepted a close request.
            return

    reactivate()
    QTimer.singleShot(0, reactivate)


def show_error(parent: QWidget, title: str, message: str) -> None:
    owner = visible_modal_dialog_owner(parent)
    focus_widget = QApplication.focusWidget()
    box = QMessageBox(owner)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(message)
    box.setStyleSheet(DIALOG_STYLESHEET)
    box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    style_message_box_buttons(box)
    box.exec()
    box.hide()
    box.deleteLater()
    restore_window_after_modal(owner, focus_widget=focus_widget)


def copy_files_to_clipboard(paths: list[Path]) -> None:
    absolute_paths = [path.expanduser().absolute() for path in paths]
    if not absolute_paths:
        return
    urls = [QUrl.fromLocalFile(str(path)) for path in absolute_paths]
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
