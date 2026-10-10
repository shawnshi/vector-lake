"""Lexical link detection using lstat fields available in Python3.10.

Do not use Path.is_junction (Python3.12+) or resolve a source before checking
its lexical ancestors. Mount-point reparse tags cover Windows junctions.
"""
import os
import stat
from pathlib import Path


def is_link_or_junction(path: Path) -> bool:
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False  # callers separately require existing, bounded regular inputs
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name != 'nt':
        return False
    if not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(info, 'st_reparse_tag', None)
    # Unknown Windows reparse metadata fails closed; known non-link cloud tags
    # retain the former is_symlink/is_junction behavior.
    return tag is None or tag in (stat.IO_REPARSE_TAG_MOUNT_POINT, stat.IO_REPARSE_TAG_SYMLINK)
