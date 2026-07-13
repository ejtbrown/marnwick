# Marnwick

Marnwick is a local desktop photo viewer and organizer for browsing large directory trees as portable catalogs. It combines a multi-catalog folder tree, a thumbnail browser, fullscreen viewing and editing, tags, duplicate discovery, and file organization in one PySide6 application.

Each catalog keeps its metadata beside the photos in a `.marnwick` directory. Move the photo tree and `.marnwick` together while Marnwick is closed to retain thumbnails, tags, catalog settings, trash restore paths, and cached directory information.

## Features

- Open and remember multiple photo catalogs.
- Discover folder trees before all images finish indexing.
- Browse folders and images in a sortable thumbnail grid with configurable columns.
- Preview child folders using up to four indexed image thumbnails.
- Sort thumbnail views by name, file size, modification date, or aspect ratio.
- View images fullscreen in display order or a randomized order.
- Zoom, pan, show file information, copy files to the desktop clipboard, and inspect image metadata.
- Rotate, flip, crop, reduce red eye, and clone/heal from the fullscreen viewer.
- Save edits normally or restore the original filesystem access, modification, and creation dates where the platform supports them.
- Define catalog tags, assign them to images, and browse tag-based virtual directories.
- Find exact duplicates using SHA-256 content hashes.
- Find visually similar images using perceptual hashes, aspect ratio, and color distribution.
- Inspect matches for an individual image or automatically move duplicate candidates into the catalog's `T-r-a-s-h` directory.
- Restore images and directories from `T-r-a-s-h`, including collision-safe restore names.
- Drag images and directories within one catalog or between open catalogs.
- Create and delete directories, delete selected images, and optionally request wipe-on-delete through GNU `shred`.
- View per-catalog logs, directory statistics, database size, and thumbnail repository size.
- Rebuild stale thumbnails and prune unreferenced cache files.
- Generate deterministic large test catalogs and drive performance runs through an authenticated localhost debug interface.

Marnwick recognizes AVIF, BMP, GIF, HEIC, HEIF, JPEG, PNG, TIFF, and WebP filenames. Actual decoding depends on the codecs available to Pillow and Qt; HEIC, HEIF, and AVIF commonly require additional platform or Pillow plugins. GIF animation is played in the fullscreen viewer. Marnwick does not index or play video.

## Requirements

- Python 3.11 or newer
- Linux or Windows for the supplied launcher setup
- A graphical desktop and a working Qt platform plugin for normal use
- Optional GNU `find` and `md5sum` for faster catalog discovery and freshness checks
- Optional GNU `shred` for wipe-on-delete

The runtime dependencies are Pillow and PySide6. Development dependencies are hash-locked in `requirements-dev.lock`.

## Quick start

### Linux

From a fresh clone:

```bash
./setup.sh
./start.sh
```

The setup script creates a virtual environment (by default `.venv`), installs the locked dependencies and Marnwick in editable mode, writes `start.sh`, and installs a per-user desktop entry under `${XDG_DATA_HOME:-$HOME/.local/share}/applications`.

### Windows PowerShell

```powershell
.\setup.ps1
.\start.cmd
```

The setup script creates a virtual environment (by default `.venv`), installs Marnwick, writes `start.ps1` and `start.cmd`, generates a Windows icon, and creates a Start Menu shortcut. `start.cmd` works without changing PowerShell's script policy; `start.ps1` is also available when local scripts are allowed. If PowerShell blocks `setup.ps1`, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

### Editable installation without launcher setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m marnwick
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

After installation, the console entry point is available as `.venv/bin/marnwick` on Linux or `.venv\Scripts\marnwick.exe` on Windows. Activate the virtual environment first if you want to invoke it as bare `marnwick`.

Verify the command-line entry point without opening the GUI:

```bash
.venv/bin/python -m marnwick --help
```

## First use

1. Choose **File > Open** and select an existing photo directory.
2. Marnwick creates `<catalog>/.marnwick` and shows the root and any cached or immediately visible folders.
3. Folder discovery and thumbnail indexing continue in the background. Large known folder trees are added in bounded pages so the window remains responsive. The status bar shows the active phase.
4. Select a folder in the left tree. Child folders appear before indexed images in the right pane.
5. Double-click an image for fullscreen viewing, or double-click a folder tile to enter it.

You can open more catalogs with **File > Open** or manage remembered catalogs under **Tools > Preferences**.

## Main controls

### Thumbnail browser

| Input | Action |
| --- | --- |
| Double-click or `Enter` | Open the selected image or folder |
| `S` | Open the selected image in randomized navigation mode |
| `T` | Edit tags for one selected image |
| `Ctrl+C` | Copy selected image files to the desktop clipboard |
| `Delete` or `Backspace` | Permanently delete selected image files after confirmation |
| Drag | Move selected images or folders to a physical folder tile or folder-tree item |

Use the slider to choose the number of thumbnail columns and the sort menu to change ordering. Scroll positions and selections are remembered separately for each physical or virtual directory during the session.

Right-click an image tile for duplicate matches, deletion, or metadata. Right-click a folder tile for open, properties, deletion, or trash restore. The folder-tree context menu also provides directory creation and, at a catalog root, catalog preferences, tag definitions, and close.

Directory tiles remain grouped before image tiles. Directory “size” is the total byte size of currently indexed images below that directory, not total filesystem usage. Directory aspect ratio is the mean aspect ratio of indexed descendant images. Both aggregates are calculated in one batched database query.

### Fullscreen viewer

| Input | Action |
| --- | --- |
| `Left` / `Right` | Previous or next image; at an edge, close the viewer |
| `+` / `-` | Zoom in or out |
| Arrow keys while zoomed | Pan the image |
| `Escape` | Exit a region tool, reset zoom, or close the viewer |
| `Z` | Toggle path, file date, and position information |
| `T` | Edit tags |
| `E` | Open edit tools |
| `Ctrl+C` | Copy the current file to the desktop clipboard |
| `Delete` or `Backspace` | Delete the current image and advance |

The edit menu provides rotate-left, rotate-right, vertical flip, horizontal flip, red-eye selection, crop selection, and clone/heal tools. Drag over the image to select a crop or red-eye region. In clone/heal mode, right-click to set the source, left-drag to paint, and use the mouse wheel to resize the brush. Navigation or closing prompts you to save, save while preserving filesystem dates, discard, or cancel pending edits. Saves preserve supported EXIF/GPS, ICC profiles, permissions, ownership, and extended attributes; edits are applied to every GIF frame or TIFF page instead of flattening the file. Marnwick refuses the save if the original changed after it was opened or if the encoder cannot preserve a multi-frame file's structure and timing.

## Catalog organization and deletion

Drag-and-drop moves, directory creation, deletion, restoration, duplicate cleanup, and edit saves are asynchronous and serialized through one protected action pipeline. Pending sources remain hidden when you navigate away and back. Successful moves preserve indexed metadata and tags; cross-catalog moves rebuild thumbnails when catalog thumbnail settings differ. Name collisions receive a numbered suffix such as `photo (1).jpg`, while a drop onto the existing parent is treated as a no-op.

Runtime move failures are compensated. Same-filesystem renames are rolled back when metadata updates fail. Cross-filesystem cleanup failures retain a complete destination recovery copy and refresh any remaining source instead of deleting the good copy.

Destructive targets are atomically isolated under a private no-replace name before removal. Indexed images are content-verified, confirmed directories are identity-checked, and a new filesystem entry that reuses the original path is left untouched.

There are two distinct deletion flows:

- **Delete** removes selected files or directories from disk after confirmation. Normal deletion does not use the catalog trash.
- **Automatically Delete Duplicates** keeps a preferred image from each exact-duplicate group and moves the others into the physical `T-r-a-s-h` directory. In Very Similar mode it moves only candidates directly similar to the preferred image, so a transitive similarity cluster can retain more than one image. Trashed items can be restored from the folder tree or thumbnail context menu.

`T-r-a-s-h` is reserved for Marnwick's recoverable duplicate and drag-to-trash workflow. Do not use that name for an unrelated photo folder.

Wipe-on-delete uses `shred -u` when available and otherwise logs a warning before falling back to ordinary deletion. If a file has multiple hard links, Marnwick unlinks only the selected name without shredding the shared content. Secure erasure remains filesystem- and hardware-dependent.

## Catalog state

A catalog may contain:

```text
<catalog>/
├── photos and folders...
└── .marnwick/
    ├── catalog.sqlite3
    ├── catalog.sqlite3-wal
    ├── catalog.sqlite3-shm
    ├── catalog.lock
    ├── directory-tree.json
    ├── marnwick.log
    ├── timings.json
    └── thumbnails/<size>/<hash-prefix>/<sha256>.jpg
```

SQLite stores image records, dimensions, hashes, similarity features, tags, catalog settings, directory freshness state, indexing failures, and trash restore paths. JPEG thumbnails are content-addressed files outside the database. Legacy database thumbnail blobs are migrated lazily.

Do not edit `.marnwick`, hard-link its state files, replace state entries with symlinks, or nest one Marnwick catalog inside another. Marnwick holds `catalog.lock` while a catalog is open and fails fast if another local process owns the lock. Advisory lock behavior depends on the filesystem, so avoid concurrent access over filesystems that do not provide reliable locks and avoid live cloud-sync conflict resolution.

Global window and catalog-list preferences default to `~/.config/marnwick/config.json`, or `$XDG_CONFIG_HOME/marnwick/config.json` when `XDG_CONFIG_HOME` is set. Saves use an adjacent `config.json.lock`, an fsynced atomic replacement, and a three-way catalog-list merge so separate Marnwick processes do not discard independent additions or resurrect a path removed by another process.

## Configuration and environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `MARNWICK_CONFIG_PATH` | Override the global JSON configuration path | XDG path described above |
| `MARNWICK_DISABLE_CONFIG=1` | Disable global configuration loading and saving | Configuration enabled |
| `MARNWICK_MAX_IMAGE_PIXELS` | Maximum decoded Pillow image area | `50000000` |
| `MARNWICK_VENV` | Override the virtual environment used by setup and launch scripts | `<repo>/.venv` |
| `PYTHON` | Override the interpreter used by setup | `python3` on Linux; discovered Python on Windows |
| `MARNWICK_DEBUG_TOKEN` | Authenticate the optional localhost debug protocol | Random token printed to stderr |

Application preferences include window geometry, remembered catalogs, thumbnail columns, sort order, and normal versus wipe deletion. Per-catalog preferences include saved thumbnail size and thumbnail-prune parallelism.

## Current limitations and safety notes

- Image and directory deletion is destructive; only items explicitly moved into `T-r-a-s-h` are restorable through Marnwick.
- Edits atomically replace the original file after an explicit save. Marnwick refuses to replace a hard-linked image because doing so cannot preserve hard-link identity; copy or unlink it explicitly before editing. If an extremely rare rollback itself fails, the error identifies the retained recovery file rather than silently deleting displaced bytes.
- Filesystem operations and one or two independent SQLite databases cannot form a single crash-atomic transaction. Runtime failures are compensated, but abrupt process or power loss can require **Tools > Refresh Catalog** to reconcile filesystem and catalog state. Keep independent backups of irreplaceable images.
- Large physical and all virtual result builds run away from the UI thread, and thumbnail rows are exposed in batches, but a completed view still retains its records in memory. Exceptionally large result sets can therefore use substantial memory.
- Background freshness uses path, size, modification time, and metadata change time (including Win32 `ChangeTime`) so ordinary same-size edits are detected without rehashing every image. Filesystems that do not expose reliable change fields may require **Tools > Refresh Catalog**, which forces reindexing.
- Catalog locking is advisory and relies on the underlying filesystem. Some network or synchronization filesystems may not provide reliable mutual exclusion.

## Troubleshooting

### A format is not indexed

Open **Tools > Logs** and look for the file's indexing error. Confirm that Pillow can decode the format and that the image is below `MARNWICK_MAX_IMAGE_PIXELS`. Marnwick memoizes unchanged failures by path, size, modification/change time, and thumbnail setting. **Tools > Refresh Catalog** forces a retry after a transient decoder or permission problem is fixed.

### Thumbnails are missing or damaged

Run **Tools > Prune Thumbnails**. Missing or corrupt cache files are validated and rebuilt from the originals; orphan files are removed after referenced rows are checked.

### The folder tree or image list is stale

Run **Tools > Refresh Catalog**. Initial discovery and indexing are asynchronous, so also check the status bar and logs. If a move or delete is still running, allow it to finish before retrying an action.

### The application cannot start on Linux

Qt may be missing a usable platform plugin or display connection. Run from a terminal with `./start.sh` to see the Qt error. Headless test runs use the offscreen Qt platform automatically.

### Wipe-on-delete logs a warning

Install GNU `shred` or switch to normal deletion. Even with `shred`, copy-on-write filesystems, SSD wear leveling, snapshots, backups, and hard links can prevent reliable isolated erasure.

### Configuration prevents startup

Set `MARNWICK_CONFIG_PATH` to a new file or temporarily set `MARNWICK_DISABLE_CONFIG=1`. The loader safely falls back to defaults for missing files, malformed JSON or text encoding, and unexpected value types. A missing, damaged, or currently locked remembered catalog is retained for a later retry without preventing other catalogs from opening.

## Development

Run the complete test suite:

```bash
.venv/bin/python -m pytest
```

Run focused suites:

```bash
.venv/bin/python -m pytest tests/test_catalog.py tests/test_indexer.py
.venv/bin/python -m pytest tests/test_ui.py -k "delete or move or fullscreen"
```

The repository currently has no configured formatter, general-purpose linter, static type checker, coverage threshold, or CI workflow. `bandit.yaml` supports an optional security scan, but Bandit is not included in the development extra:

```bash
.venv/bin/python -m pip install bandit
.venv/bin/python -m bandit -q -c bandit.yaml -r src tools
```

Regenerate the Python 3.12 development lock after changing dependencies:

```bash
.venv/bin/pip-compile --allow-unsafe --extra=dev --generate-hashes \
  --output-file=requirements-dev.lock pyproject.toml
```

## Performance tooling

Generate a deterministic nested catalog fixture. The default seed's tree has a minimum feasible aggregate size a little above 2.04 decimal GB, so allow at least that much free space; omitting `--target-gb` generates 50 GB.

```bash
.venv/bin/python tools/generate_test_catalog.py catalog /tmp/marnwick-catalog --target-gb 2.1
```

Start an authenticated debug session in one terminal. Disabling normal configuration prevents remembered catalogs from contaminating the run:

```bash
export MARNWICK_DEBUG_TOKEN="replace-with-a-private-token"
MARNWICK_DISABLE_CONFIG=1 .venv/bin/python -m marnwick --codex-debug
```

While Marnwick remains open, use the same token in a second terminal to drive catalog-open and navigation timings:

```bash
export MARNWICK_DEBUG_TOKEN="replace-with-a-private-token"
.venv/bin/python tools/drive_debug_catalog.py /tmp/marnwick-catalog
```

The debug server listens only on localhost, requires a token, and caps connection count, request size, and response page or tail sizes. For a custom port, pass `--codex-debug-port` to Marnwick and the same value as `--port` to the driver. For a private token file, pair Marnwick's `--codex-debug-token-file` with the driver's `--token-file`.

## Architecture

| Area | Source | Responsibility |
| --- | --- | --- |
| Application and UI | [`src/marnwick/ui.py`](src/marnwick/ui.py) | Qt models, views, dialogs, fullscreen editing, drag-and-drop, task settlement, and configuration UI |
| Catalog engine | [`src/marnwick/catalog.py`](src/marnwick/catalog.py) | SQLite schema, discovery, indexing, thumbnails, tags, duplicate detection, moves, deletion, trash, and repair |
| Background actions | [`src/marnwick/indexer.py`](src/marnwick/indexer.py) | Prioritized serialized queue, cancellation, progress snapshots, and idle work |
| Image editing | [`src/marnwick/image_ops.py`](src/marnwick/image_ops.py) | Edit operations, atomic saves, and filesystem-date handling |
| Image safety | [`src/marnwick/safe_image.py`](src/marnwick/safe_image.py) | Pillow pixel-limit enforcement |
| Domain records | [`src/marnwick/models.py`](src/marnwick/models.py) | Sort orders, image/folder records, settings, and result objects |
| Workspace | [`src/marnwick/workspace.py`](src/marnwick/workspace.py) | Identity and lifetime of open catalogs |
| Global configuration | [`src/marnwick/config.py`](src/marnwick/config.py) | JSON configuration defaults, validation, loading, and saving |
| Debug automation | [`src/marnwick/debug.py`](src/marnwick/debug.py) | Authenticated localhost JSON-lines protocol for performance runs |

The UI owns one long-lived `Catalog` connection per open root. Worker-local catalog connections share the process's reentrant catalog lock. Expensive scans, large physical views, virtual queries, and file mutations run away from the UI thread; mutations pass through a single prioritized protected action pipeline so selected-folder indexing and user file operations take precedence over idle refresh and thumbnail pruning. SQLite uses WAL mode, foreign keys, a five-second contention timeout, and content-addressed files for large thumbnail payloads.
