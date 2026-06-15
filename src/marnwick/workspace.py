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
        catalog = Catalog(resolved)
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
        resolved = root.expanduser().resolve()
        catalog = self._catalogs.pop(resolved, None)
        if catalog is not None:
            catalog.close()

    def catalog_for_root(self, root: Path) -> Catalog | None:
        return self._catalogs.get(root.expanduser().resolve())

    def close(self) -> None:
        for catalog in list(self._catalogs.values()):
            catalog.close()
        self._catalogs.clear()
