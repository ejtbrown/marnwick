from __future__ import annotations

from pathlib import Path

from marnwick.workspace import Workspace


def test_catalog_for_known_canonical_root_does_not_resolve(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = Workspace()
    root = tmp_path / "catalog"
    sentinel = object()
    workspace._catalogs[root] = sentinel  # type: ignore[assignment]

    def unexpected_resolve(_path: Path, *_args, **_kwargs) -> Path:  # type: ignore[no-untyped-def]
        raise AssertionError("known canonical catalog root was resolved again")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)

    assert workspace.catalog_for_root(root) is sentinel
    assert workspace.catalog_for_exact_root(root) is sentinel


def test_close_known_canonical_root_does_not_resolve(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = Workspace()
    root = tmp_path / "catalog"

    class FakeCatalog:
        closed = False

        def close(self) -> None:
            self.closed = True

    catalog = FakeCatalog()
    workspace._catalogs[root] = catalog  # type: ignore[assignment]

    def unexpected_resolve(_path: Path, *_args, **_kwargs) -> Path:  # type: ignore[no-untyped-def]
        raise AssertionError("known canonical catalog root was resolved again")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)

    workspace.close_catalog(root)

    assert catalog.closed
    assert root not in workspace._catalogs
