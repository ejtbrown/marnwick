# Marnwick

Marnwick is a desktop photo viewer and organizer designed around fast catalog browsing.

The application stores each catalog's state in a `.marnwick` directory beside the photos, so a photo tree can be moved to another disk or a replicated cloud folder without losing thumbnails, tags, or catalog settings.

## Run

From a fresh clone on Linux:

```bash
./setup.sh
```

This creates `.venv`, installs Marnwick, writes `start.sh`, and adds a Marnwick app-menu launcher using `marnwick-icon.png`.

From a fresh clone on Windows PowerShell:

```powershell
.\setup.ps1
```

This creates `.venv`, installs Marnwick, writes `start.ps1` and `start.cmd`, generates a Windows icon from `marnwick-icon.png`, and adds a Marnwick Start Menu shortcut.
If PowerShell blocks local scripts on your machine, run `powershell -ExecutionPolicy Bypass -File .\setup.ps1` from the repo directory.

To run without the launcher setup:

```bash
python -m pip install -e ".[dev]"
python -m marnwick
```

or, after installation:

```bash
marnwick
```

## Development

```bash
python -m pytest
```

The core catalog engine is tested without opening a GUI. The Qt interface is intentionally thin and calls into that engine for catalog creation, thumbnail indexing, tagging, moves, deletes, ordering, and image edit persistence.
