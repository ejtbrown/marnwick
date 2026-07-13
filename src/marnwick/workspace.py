from __future__ import annotations

from pathlib import Path

from .catalog import Catalog


class Workspace:
    def __init__(self) -> None:
        self._catalogs: dict[Path, Catalog] = {}

    @property
    def catalogs(self) -> list[Catalog]:
        return list(self._catalogs.values())

    def open_catalog(self, root: Path) -> Catalog:
        resolved = root.expanduser().resolve()
        catalog = self._catalogs.get(resolved)
        if catalog is not None:
            return catalog
        # Opening is never permission to recreate a catalog directory that
        # disappeared or was disconnected while the request was queued.
        catalog = Catalog(resolved, create_root=False)
        self._catalogs[resolved] = catalog
        return catalog

    def adopt_catalog(self, catalog: Catalog) -> tuple[Catalog, bool]:
        existing = self._catalogs.get(catalog.root)
        if existing is not None:
            if existing is not catalog:
                catalog.close()
            return existing, True
        self._catalogs[catalog.root] = catalog
        return catalog, False

    def close_catalog(self, root: Path) -> None:
        expanded = root.expanduser()
        # UI callers normally pass the canonical Catalog.root. Avoid touching
        # a slow or disconnected filesystem merely to remove that known key.
        catalog = self._catalogs.pop(expanded, None)
        if catalog is None:
            catalog = self._catalogs.pop(expanded.resolve(), None)
        if catalog is not None:
            catalog.close()

    def catalog_for_root(self, root: Path) -> Catalog | None:
        expanded = root.expanduser()
        # Internal callers normally already hold Catalog.root, which is
        # canonical.  Avoid another filesystem lookup on every UI poll; a
        # disconnected or slow catalog mount must not stall the event loop.
        catalog = self._catalogs.get(expanded)
        if catalog is not None:
            return catalog
        return self._catalogs.get(expanded.resolve())

    def catalog_for_exact_root(self, root: Path) -> Catalog | None:
        """Return an already-canonical catalog without touching the filesystem."""

        return self._catalogs.get(root.expanduser())

    def close(self) -> None:
        for catalog in list(self._catalogs.values()):
            catalog.close()
        self._catalogs.clear()
