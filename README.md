# Marnwick

Marnwick is a local desktop photo viewer and organizer for browsing large directory trees as portable catalogs. It combines a multi-catalog folder tree, a thumbnail browser, fullscreen viewing and editing, tags, duplicate discovery, and file organization in one PySide6 application.

Each catalog keeps its metadata beside the photos in a `.marnwick` directory. Move the photo tree and `.marnwick` together while Marnwick is closed to retain thumbnails, tags, catalog settings, trash restore paths, and cached directory information.

## Features

- Become interactive with safe defaults while global configuration loads in the background; newer choices made in the open window take precedence over a late load.
- Open and restore multiple photo catalogs without blocking the application window.
- Paint current indexed rows immediately when available; otherwise enumerate the selected folder away from the UI thread, publish one complete sorted placeholder layout, and replace placeholders with thumbnails individually.
- Discover deeply nested folder trees independently of the currently selected folder, read the known inventory in bounded database pages, and build Qt tree items in short event-loop slices.
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

1. Choose **File > Open** and select an existing photo directory. Catalog initialization runs in the background, and the status bar reports the open request without freezing the application window.
2. Marnwick creates `<catalog>/.marnwick` and shows the root and any cached or immediately visible folders.
3. Select a folder in the left tree. Marnwick gives that folder's work priority over deep discovery and idle maintenance, starts a catalog-page read and a complete direct-child filesystem inventory in parallel, and reports the active phase in the status bar. A nonempty current catalog page can paint immediately. For an unindexed or changed folder, the filesystem worker publishes every recognized direct image and child folder in one stable sorted layout as soon as enumeration finishes; it does not wait for image decoding or recursive descendant discovery.
4. The thumbnail model exposes that layout to Qt in 400-row batches. Thumbnail files are read away from the UI thread and replace placeholders at their existing rows as they become available. Metadata updates also apply in place. An aspect-ratio or directory-aggregate sort can require one final reconciliation after indexing supplies the previously unknown values, but it does not rebuild the pane for every completed thumbnail. Tag and exact-duplicate panes remain database-paged, while folder-tree database reads are paged and Qt item construction is time-sliced into bounded batches.
5. Deep folder discovery continues independently in the background. The status bar reports folders found, images checked, the current path, and other active phases.
6. Double-click an image for fullscreen viewing, or double-click a folder tile to enter it.

You can open more catalogs with **File > Open** or manage remembered catalogs under **Tools > Preferences**. Global configuration is read on a bounded background lane, so the window can open with safe defaults even if the configuration path is slow. A late configuration result does not overwrite newer window geometry, controls, or catalog choices made in that window. Remembered catalogs are then restored asynchronously. If several open requests overlap, every catalog that opens successfully is retained in the workspace, while the most recently requested successful catalog becomes active. A slow earlier request cannot take focus back from a newer successful request; if the newest request fails, Marnwick falls back to the newest earlier success. An unavailable remembered path remains configured for a later retry.

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

Physical-folder navigation is progressive. Marnwick checks and lists only the selected folder's direct entries, so a slow descendant tree does not hold up the selected pane. Once an unindexed folder's complete direct-child list is ready, neutral image placeholders remain at fixed rows while indexing runs and completed thumbnails repaint in place. Rapid navigation assigns each pane load a new generation, cancels stale preemptible scans and queries, and ignores any obsolete result that completes late. Returning to a previously interrupted folder queues a current direct-folder scan instead of reusing the canceled task.

Deep discovery walks descendants separately from the selected-pane load and commits its directory inventory in batches. The folder tree reads that inventory from SQLite in bounded pages and performs only a short batch of Qt item work per event-loop turn. If tree work for an older catalog is still pending, the current catalog takes priority; selecting an already visible directory does not wait for the full descendant tree to finish.

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

The edit menu provides rotate-left, rotate-right, vertical flip, horizontal flip, red-eye selection, crop selection, and clone/heal tools. Drag over the image to select a crop or red-eye region. In clone/heal mode, right-click to set the source, left-drag to paint, and use the mouse wheel to resize the brush. Navigation, tagging, or closing resolves pending edits by asking you to save, save while preserving filesystem dates, discard, or cancel; a tag dialog never races an asynchronous save. Save, warning, and error prompts remain owned by the fullscreen modal viewer, and focus returns to that viewer when a nested prompt closes. In the main application, choosing save queues image decoding, editing, encoding, validation, and atomic replacement on a dedicated background worker; you can continue navigating while the status bar reports the save. A static PNG uses the same non-modal worker path, shares metadata inspection and editing in one traversal, and uses fast lossless compression. It no longer opens an indeterminate “preserving frames and metadata” progress dialog. After a successful replacement, Marnwick submits a targeted reindex through the catalog action queue. That reindex decodes and hashes one stable open file descriptor, compares the resulting filesystem identity and SHA-256 hash with the proof of the exact committed object, and publishes the new record and thumbnail only if they match. It does not perform a separate preliminary full-file proof hash. If a save or tag edit can change the membership or ordering of a database-paged fullscreen view, that navigator reloads from page zero asynchronously instead of continuing from a stale SQL offset. The currently displayed image stays published while bounded background pages locate its fresh position; editing pauses during that reconciliation, visible progress is reported, and an overlapping delete restarts the fresh query after its outcome is known. The main thumbnail pane also refreshes a visible physical or virtual query after save reconciliation.

Saves preserve supported EXIF/GPS with orientation normalized, ICC profiles, PNG text, JPEG/WebP/AVIF XMP, and—where the platform supports them—permissions, ownership, and extended attributes. Preserved PNG text and XMP are each limited to 4 MiB; JPEG's single-marker XMP limit is 65,504 bytes. Edits are applied to every GIF, APNG, animated WebP/AVIF, or TIFF frame/page instead of flattening the file, with supported timing, loop, disposal, and blend metadata checked after encoding. Marnwick refuses the save if the original changed after it was opened, the complete edit sequence exceeds the aggregate pixel budget, embedded metadata exceeds a preservation limit, the destination format cannot carry that metadata or sequence, or the encoder cannot reproduce supported metadata and multi-frame structure.

## Catalog organization and deletion

Drag-and-drop moves, directory creation, deletion, restoration, and duplicate cleanup are asynchronous and serialized through one prioritized, protected catalog-action pipeline. User mutations take precedence over selected-folder indexing, which in turn takes precedence over deep discovery and idle refresh or pruning. Pending sources remain hidden when you navigate away and back. Successful moves preserve tags, invalidate content-derived metadata and thumbnail references, and queue a targeted background reconciliation that restores those fields from the object at its new path. Name collisions receive a numbered suffix such as `photo (1).jpg`, while a drop onto the existing parent is treated as a no-op.

Edit saves use a fixed four-worker encoding pool, with at most eight active or queued saves globally and at most one admitted save per catalog. A stalled codec or filesystem can therefore occupy one lane without blocking navigation or a save in another catalog. A second save in the same catalog is not admitted concurrently. Moves, directory deletion, and restoration are rejected while they overlap an image being saved, and duplicate cleanup waits for every save in that catalog; Marnwick asks you to retry those operations later. Catalog reconciliation returns to the protected action pipeline after atomic replacement. Deleting the same image is the exception: that intent is retained and deferred until a successful save and proof-aware reconciliation finish.

Runtime move failures are compensated without overwriting a path another program created during the operation. A failed same-filesystem move restores the original name when it remains free; if a successor has already claimed that name, Marnwick retains the moved object at its destination and reconciles both visible paths. Cross-filesystem moves publish a verified destination copy before isolating and removing the source; cleanup failures leave that destination in place and restore any remaining isolated source when possible.

Destructive targets are atomically isolated under a private no-replace name before removal. Image, source-directory, and destination-directory identities are captured before a confirmed or queued mutation and checked again by the worker that performs it. A delete from the fullscreen viewer is additionally bound to the exact content identity that produced the displayed pixels; a thumbnail delete is bound to the direct-inventory filesystem identity or indexed content hash that produced that row. A replacement appearing at the same path before or after confirmation is therefore preserved. Indexed images are content-verified, confirmed directories are identity-checked, and a new filesystem entry that reuses the original path is left untouched. Edit saving also verifies the identity captured when the viewer loaded the image, then carries a content-and-filesystem proof tied to the exact encoded object through atomic replacement and catalog reconciliation.

Some Linux network and userspace filesystems reject the kernel's atomic no-replace rename operation even though ordinary file operations work. Marnwick falls back to no-clobber hard-link/private isolation for regular files and exclusive reservations for directories on the same filesystem; verified cross-filesystem copies use private destination-side staging before no-clobber publication. These paths refuse to overwrite a raced source or destination. This includes NFS mounts that report the unsupported operation as `EINVAL`.

Once an image delete is queued, its path remains filtered from thumbnail results even if you change directories or catalogs and later return. The fullscreen viewer hides a queued row immediately, restores it if the worker proves the path remained, and rebases a paged cursor only after the worker proves the path disappeared. Exact-duplicate membership is rebuilt from page zero because deleting one member can also remove a surviving singleton from that virtual view. Settlement refreshes the currently visible pane only when its catalog was affected; a later visit reads current catalog and filesystem state. If an identity check rejects the delete because the path was replaced, the replacement is preserved and a refreshed view can show it.

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

Do not edit `.marnwick`, hard-link its state files, replace state entries with symlinks, use a directory name beginning with `.marnwick-private-`, or nest one Marnwick catalog inside another. Marnwick reserves `.marnwick-private-*` for temporary and recovery data and excludes that namespace from catalog discovery. Marnwick holds `catalog.lock` while a catalog is open and fails fast if another local process owns the lock. Advisory lock behavior depends on the filesystem, so avoid concurrent access over filesystems that do not provide reliable locks and avoid live cloud-sync conflict resolution.

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

## Responsiveness and resource bounds

Marnwick treats UI responsiveness as part of correctness. Configuration and catalog loading, selected-folder and virtual queries, recursive discovery, thumbnail reads, viewer decoding and preview rendering, edit encoding, identity preflights, timing writes, and debug file reads run outside the Qt UI thread. Catalog-action writes remain serialized even when read-only work is concurrent. Edit encoding uses a bounded multi-lane pool but remains serialized per catalog.

Work that feeds the interface has explicit bounds:

- Initial configuration uses one read lane. Catalog opening normally uses two daemon read lanes and can retire a bounded number of saturated generations, for a maximum of eight opening threads and eight admitted requests; superseded results cannot retake focus, and quitting does not wait for a read trapped on an unavailable mount.
- Selected physical folders use two bounded worker pools: a current catalog page can paint first, while a filesystem worker enumerates, stats, and sorts the complete set of direct child folders and recognized image files. This full inventory is necessary to establish one stable membership and order for an unindexed or changed folder; it has no entry-count or 12-millisecond cutoff. The status bar reports the listing phase and elapsed time, stale generations cannot publish, and the Qt model exposes the completed inventory in 400-row batches rather than creating every visible item in one event-loop turn.
- Folder-tree database reads use pages of at most 400 paths, and Qt tree construction yields after at most 400 items or roughly eight milliseconds of work. An automatic rebuild creates at most 4,096 directory items; child branches and long tag lists expose explicit load-more rows rather than allocating the entire known tree. Tree rebuilds, page publication, deep-path selection, and scroll restoration wait until either a directory drag or a thumbnail-to-tree file drag ends so drop targets cannot move under the pointer.
- The thumbnail model exposes records in 400-row batches, limits pending reads, and applies only a small number of completed thumbnails per UI tick. A newer thumbnail generation can start while old reads unwind.
- Decoded thumbnail inputs are limited to 32 MiB and 4096 pixels per dimension. The primary thumbnail pixmap cache is limited to 512 entries or 256 MiB, the delegate's scaled-pixmap cache to 512 entries or 128 MiB, and remembered pane state and Very Similar result caches have fixed entry limits.
- Fullscreen decode, edit-preview rendering, and paged navigation use three process-wide pools rather than creating threads per viewer. Decode and preview each allow eight workers and 16 admitted tasks; paging allows four workers and eight admitted tasks. Closing a viewer cancels its queued work without shutting down the shared pools.
- Pillow source decoding is limited by `MARNWICK_MAX_IMAGE_PIXELS`; the complete set of detached frames in one edit has the same aggregate pixel budget. Interactive edit-preview rasters are capped at 4096 pixels per dimension, and GIF movie input is capped at 128 MiB.
- Catalog logs retain and read at most a 1 MiB tail, timing history retains 1,000 events, and the optional debug server caps connections, request work per event-loop turn, page and tail sizes, pending reads, file-read size, and queued response bytes.

## Current limitations and safety notes

- Image and directory deletion is destructive; only items explicitly moved into `T-r-a-s-h` are restorable through Marnwick.
- Edits atomically replace the original file after an explicit save. Marnwick refuses to replace a hard-linked image because doing so cannot preserve hard-link identity; copy or unlink it explicitly before editing. If an extremely rare rollback itself fails, the error identifies the retained recovery file rather than silently deleting displaced bytes.
- Filesystem operations and one or two independent SQLite databases cannot form a single crash-atomic transaction. Runtime failures are compensated, but abrupt process or power loss can require **Tools > Refresh Catalog** to reconcile filesystem and catalog state. Keep independent backups of irreplaceable images.
- Cross-filesystem directory moves revalidate the published destination immediately before recursively removing the isolated source, but no portable filesystem operation makes those steps atomic across mounts. An external program that replaces or removes the destination during that cleanup window can defeat compensation; do not externally mutate paths participating in a move. Marnwick's catalog lock does not control unrelated filesystem tools.
- Current physical folders can paint from bounded database pages, but an unindexed or changed physical folder materializes and sorts its complete direct-child filesystem inventory on a worker so it can publish one stable placeholder layout. A directory with an exceptionally large number of direct entries can therefore require substantial worker memory and listing time, although it does not block Qt. Tag and exact-duplicate views are database-paged. The Very Similar model exposes rows progressively, but its worker currently materializes the complete global similarity result before first publication.
- Automatic folder-tree construction is capped and child/tag reads are paged. Expanding many branches or repeatedly choosing load-more can still create many Qt items during a long session, but initial discovery no longer requires one item per known directory.
- Cancellation is cooperative. Marnwick can cancel queued work, interrupt long SQLite queries, and ignore stale generations, but it cannot forcibly interrupt an operating-system filesystem call or native decoder already in progress. Generation-guarded catalog, selected-folder, tree, identity, and thumbnail read pools can retire only a bounded number of saturated worker epochs, giving newer work finite escape capacity without allowing thread growth per navigation; fullscreen pools reserve fixed lanes instead. If every permitted epoch or lane is occupied by independently stuck native calls, later work is rejected or retried until capacity returns. Obsolete calls cannot publish stale UI state, total worker counts remain capped, and read-only daemon pools do not hold up process exit.
- Image saves are mutually excluded within each catalog and share four encoding lanes globally. Four independently stalled codecs or filesystems can occupy all lanes and delay later saves, but the admission queue remains bounded and the status bar continues to report active work. Normal in-application close waits for admitted saves, proof-aware reconciliation, and dependent deletes to settle; forcibly terminating the process can interrupt that workflow.
- Background freshness uses path, size, modification time, and metadata change time (including Win32 `ChangeTime`) so ordinary same-size edits are detected without rehashing every image. Filesystems that do not expose reliable change fields may require **Tools > Refresh Catalog**, which forces reindexing.
- Catalog locking is advisory and relies on the underlying filesystem. Some network or synchronization filesystems may not provide reliable mutual exclusion.

## Troubleshooting

### A format is not indexed

Open **Tools > Logs** and look for the file's indexing error. Confirm that Pillow can decode the format and that the image is below `MARNWICK_MAX_IMAGE_PIXELS`. Marnwick memoizes unchanged failures by path, size, modification/change time, and thumbnail setting. **Tools > Refresh Catalog** forces a retry after a transient decoder or permission problem is fixed.

### Thumbnails are missing or damaged

Run **Tools > Prune Thumbnails**. Missing or corrupt cache files are validated and rebuilt from the originals; orphan files are removed after referenced rows are checked.

### A newly opened folder shows placeholder tiles

This is expected briefly for an unindexed folder. If current indexed rows exist, they can paint while Marnwick validates the folder. Otherwise the status bar reports that Marnwick is listing the folder; once the worker has enumerated and sorted all direct entries, every recognized filename appears in one stable placeholder layout. Thumbnails and indexed metadata then replace those placeholders at their existing rows. Aspect-ratio and directory-aggregate sorts can reconcile the order once after indexing completes because those values are not available from filenames and file stats alone. Navigating away cancels stale work; navigating back starts a current direct-folder scan.

If no progress appears, check **Tools > Logs** for decoder or permission errors, confirm the directory is still readable, and run **Tools > Refresh Catalog** to force reconciliation.

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
| Background actions | [`src/marnwick/indexer.py`](src/marnwick/indexer.py) | Prioritized bounded action workers, serialized protected mutations, cancellation, progress snapshots, selected-folder indexing, discovery, and idle work |
| Image editing | [`src/marnwick/image_ops.py`](src/marnwick/image_ops.py) | Edit operations, format-aware encoding, atomic saves, and filesystem-date handling |
| Image safety | [`src/marnwick/safe_image.py`](src/marnwick/safe_image.py) | Pillow pixel-limit enforcement |
| Domain records | [`src/marnwick/models.py`](src/marnwick/models.py) | Sort orders, image/folder records, settings, and result objects |
| Workspace | [`src/marnwick/workspace.py`](src/marnwick/workspace.py) | Identity and lifetime of open catalogs |
| Global configuration | [`src/marnwick/config.py`](src/marnwick/config.py) | JSON configuration defaults, validation, loading, and saving |
| Asynchronous utilities | [`src/marnwick/async_utils.py`](src/marnwick/async_utils.py) | Bounded daemon executors for abandonable reads, latest-only snapshots, atomic saves, and process-wide shared dialog/viewer pools |
| Debug automation | [`src/marnwick/debug.py`](src/marnwick/debug.py) | Authenticated localhost JSON-lines protocol for performance runs |

The UI owns one long-lived writable `Catalog` connection per open root. Writable worker-local connections share the process's reentrant catalog lock; query-only workers can use short-lived read-only connections with bounded contention waits. Configuration and catalog initialization, thumbnail reads, expensive scans, tree pages, and physical or virtual page queries run away from the UI thread. The `BackgroundIndexer` has bounded read lanes plus a dedicated serialized lane for protected mutations; queued work is prioritized so explicit mutations outrank selected-folder indexing, which outranks deep discovery and idle refresh or pruning. Image encoding uses a bounded four-worker pool with per-catalog mutual exclusion at admission, then queues a targeted reconciliation on the protected catalog lane after atomic replacement. Reconciliation verifies the committed proof during its single stable decode/hash pass before publishing catalog state. SQLite uses WAL mode, foreign keys, bounded busy timeouts, and content-addressed files for large thumbnail payloads.
