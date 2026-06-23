from __future__ import annotations

from importlib.resources import files


DESKTOP_FILE_ID = "marnwick"
ICON_RESOURCE = "marnwick-icon.png"
FOLDER_ICON_RESOURCE = "folder-icon.png"
VIRTUAL_FOLDER_ICON_RESOURCE = "virtual-folder-icon.png"


def app_icon_bytes() -> bytes:
    return files("marnwick.assets").joinpath(ICON_RESOURCE).read_bytes()


def folder_icon_bytes() -> bytes:
    return files("marnwick.assets").joinpath(FOLDER_ICON_RESOURCE).read_bytes()


def virtual_folder_icon_bytes() -> bytes:
    return files("marnwick.assets").joinpath(VIRTUAL_FOLDER_ICON_RESOURCE).read_bytes()
