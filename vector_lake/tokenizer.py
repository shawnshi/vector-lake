"""Single tokenizer entry point for FTS pre-processing.

Backends
--------
``rjieba`` (the only backend)
    Rust implementation of jieba (``jieba-rs``) exposed through the official
    PyO3 binding by the same author (messense).  Ships ``cp38-abi3`` wheels for
    Windows, macOS, manylinux and musllinux, so it installs on CPython 3.8+
    without a compiler, and it measured ~7-15x faster than the pure-Python
    implementation this project used to fall back to.

The pure-Python ``jieba`` fallback was removed on 2026-09-18: the abi3 wheels
cover every platform this project supports, so the fallback was only reachable
outside that set, while carrying a second segmentation whose token stream differs
from ``rjieba``'s -- the one thing the search-index cache key exists to keep out of
a single FTS index.  On a platform with no ``rjieba`` wheel, tokenization is now
``unavailable``: CJK pre-tokenization is skipped and CJK queries match less,
which ``doctor`` and :func:`backend_name` report rather than hide.

Why the layering is observable
------------------------------
Every tokenization call goes through this module so that:

* the active backend is inspectable (:func:`backend_name`, :func:`backend_version`),
* the search-index cache key includes the backend identity (see
  ``indexer._node_content_digest``), because two backends produce different
  token streams and mixing them inside one FTS index would silently corrupt it,
* ``VECTOR_LAKE_TOKENIZER`` is still accepted (the single valid value is
  ``rjieba``), so an existing environment file states its intent explicitly; an
  unavailable forced backend warns and tokenization goes ``unavailable``.

Known limitation
----------------
``rjieba`` does **not** expose ``add_word`` / ``load_userdict`` (neither at module
level nor on its ``Jieba`` class), and jieba-rs embeds its own dictionary, so
``QUERY_EXPANSION_DICT`` terms cannot be registered at all now that the pure-Python
backend is gone.  :func:`add_word` reports that instead of pretending success; see
:func:`supports_add_word`.  Indexing and querying use the same tokenizer, so recall
is unaffected apart from the exact phrase forms of those terms.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

log = logging.getLogger("vector-lake-tokenizer")

# The Rust implementation is the only backend; see the module docstring for why the
# pure-Python fallback was removed.  Kept as a tuple because the selection logic and
# ``VECTOR_LAKE_TOKENIZER`` validation read it as the set of valid names.
VALID_BACKENDS = ("rjieba",)

# jieba-rs release that the installed rjieba binding was built against.
#
# Recorded because the crate version is NOT discoverable at runtime: rjieba
# exposes no __version__, and its Cargo.toml pins `jieba-rs = "0.9.0"` (verified
# from the rjieba 0.2.1 sdist).  Update this constant when the ACTIVE backend's crate moves.
#
# 2026-09-25: the pin is no longer stuck on upstream's release schedule --
# `crates/vector_lake_core` now depends on `jieba-rs = "0.11"` itself and exposes
# `cut` / `cut_joined` with rjieba's call shape, so this constant still describes
# the backend that is in force (rjieba) but 0.11 is buildable in-tree.  Measured
# parity over 250 pages / 500 strings: the CJK token stream is identical
# (500/500, 0 differing positions), while punctuation/ASCII runs change
# (`Concept_1 - 0` -> `Concept_1-0`).  Switching the backend is therefore a
# punctuation-level change that still rebuilds every lexical index (this module's
# version is part of the FTS cache key) and still needs the eval gate.
JIEBA_RS_PINNED = "0.9.x"
RJIEBA_TESTED_VERSION = "0.2.1"

_MODULE_CACHE: dict[str, object] = {}
_LOCK = threading.Lock()
_RESOLVED: dict[str, str] = {}
_ADD_WORD_WARNED = False


def _load(module_name: str, *, expected: bool = False):
    """Import a backend, tolerating a broken or partially built extension."""
    if module_name in _MODULE_CACHE:
        return _MODULE_CACHE[module_name]
    try:
        module = importlib.import_module(module_name)
    except (ImportError, OSError) as exc:
        # A missing optional backend is expected on some platforms; only an
        # explicitly requested backend is worth a warning.
        log_fn = log.warning if expected else log.debug
        log_fn("Tokenizer backend %r unavailable: %s: %s", module_name, type(exc).__name__, exc)
        _MODULE_CACHE[module_name] = None
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Tokenizer backend %r failed to initialise: %s: %s", module_name, type(exc).__name__, exc)
        _MODULE_CACHE[module_name] = None
        return None
    _MODULE_CACHE[module_name] = module
    return module


def _select():
    """Resolve the active backend once per process."""
    cached_name = _RESOLVED.get("name")
    if cached_name:
        return cached_name, _MODULE_CACHE.get(cached_name)
    with _LOCK:
        cached_name = _RESOLVED.get("name")
        if cached_name:
            return cached_name, _MODULE_CACHE.get(cached_name)
        forced = (os.environ.get("VECTOR_LAKE_TOKENIZER") or "").strip().lower()
        if forced and forced not in VALID_BACKENDS:
            log.warning(
                "VECTOR_LAKE_TOKENIZER=%r is not one of %s; falling back to auto-selection.",
                forced,
                list(VALID_BACKENDS),
            )
            forced = ""
        order = ([forced] if forced else []) + [name for name in VALID_BACKENDS if name != forced]
        for name in order:
            module = _load(name, expected=bool(forced) and name == forced)
            if module is not None:
                if name == "rjieba":
                    log.info("Tokenizer backend: rjieba (jieba-rs %s via Rust extension)", JIEBA_RS_PINNED)
                _RESOLVED["name"] = name
                return name, module
        log.warning(
            "No CJK tokenizer backend is importable (rjieba has no wheel for this platform?); "
            "FTS pre-tokenization is disabled, so CJK queries will match less. "
            "Install rjieba, or run without CJK full-text search."
        )
        _RESOLVED["name"] = "unavailable"
        return "unavailable", None


def reset_backend_cache() -> None:
    """Drop cached backend selection (used by tests and after env changes)."""
    global _ADD_WORD_WARNED
    with _LOCK:
        _MODULE_CACHE.clear()
        _RESOLVED.clear()
        _ADD_WORD_WARNED = False


def backend_name() -> str:
    """Active backend: ``rjieba``, or ``unavailable`` when it cannot be imported."""
    name, _ = _select()
    return name


def backend_version() -> str:
    """Human-readable active backend including the jieba-rs crate version."""
    name, module = _select()
    if module is None:
        return "unavailable"
    if name == "rjieba":
        importlib_version = getattr(module, "__version__", None) or RJIEBA_TESTED_VERSION
        return f"rjieba {importlib_version} (jieba-rs {JIEBA_RS_PINNED})"
    return f"{name} {getattr(module, '__version__', 'unknown')}"


def supports_add_word() -> bool:
    """Whether the active backend can register custom dictionary terms."""
    _, module = _select()
    return module is not None and callable(getattr(module, "add_word", None))


def cut(text: str) -> list[str]:
    """Segment ``text``; empty input or no backend yields an empty list."""
    if not text:
        return []
    _, module = _select()
    if module is None:
        return []
    return list(module.cut(text))


def tokenize_joined(text: str, separator: str = " ") -> str:
    """Segment and rejoin, the form the FTS5 pre-tokenization step needs."""
    return separator.join(cut(text))


def split(text: str) -> list[str]:
    """Like :func:`cut` but using the backend's ``lcut`` when available."""
    if not text:
        return []
    _, module = _select()
    if module is None:
        return []
    lcut = getattr(module, "lcut", None)
    if callable(lcut):
        return list(lcut(text))
    return list(module.cut(text))


def add_word(term: str) -> bool:
    """Register a domain term where the backend supports it.

    Returns ``False``: no backend in this tree exposes a dictionary API (``rjieba``
    does not, and the pure-Python backend that did was removed).  Callers must not
    assume this succeeded -- segmentation stays consistent between indexing and
    querying, so recall is unaffected apart from the exact phrase forms of those terms.
    """
    global _ADD_WORD_WARNED
    if not term:
        return False
    _, module = _select()
    add = getattr(module, "add_word", None)
    if module is None or not callable(add):
        if not _ADD_WORD_WARNED:
            log.warning(
                "Active tokenizer backend %r exposes no add_word(); custom dictionary terms are ignored "
                "(jieba-rs embeds its own dictionary and the pure-Python backend was removed). "
                "Segmentation stays consistent between indexing and querying, so recall is unaffected "
                "except for the exact phrase forms of those terms.",
                backend_name(),
            )
            _ADD_WORD_WARNED = True
        return False
    add(term)
    return True
