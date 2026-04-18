from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
EXTENSION_ROOT = PACKAGE_ROOT.parent


def get_extension_root() -> Path:
    return EXTENSION_ROOT
