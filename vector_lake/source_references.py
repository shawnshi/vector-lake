import hashlib
import re


_SOURCE_PAGE_RE = re.compile(r"^Source_[^/\\\[\]|#\s]+$")
STRUCTURAL_SOURCE_PAGE = "Source_Auto_Fixed"


def normalize_explicit_source_page_ref(value: str) -> str:
    """Return a canonical Source page key only for an explicit Wiki reference."""
    source_ref = str(value or "").strip()
    if source_ref.startswith("[[") and source_ref.endswith("]]" ):
        source_ref = source_ref[2:-2].split("|", 1)[0].strip()
    if source_ref.endswith(".md"):
        source_ref = source_ref[:-3]
    return source_ref if _SOURCE_PAGE_RE.fullmatch(source_ref) else ""


def source_id_for_raw_ref(raw_ref: str) -> str:
    digest = hashlib.blake2b(str(raw_ref).encode("utf-8"), digest_size=12).hexdigest()
    return f"source_{digest}"


def canonical_source_page_for_ref(raw_ref: str, *, owning_page: str = "") -> str:
    """Map explicit Source refs without changing the raw reference identity."""
    if owning_page:
        return owning_page
    page_key = normalize_explicit_source_page_ref(raw_ref)
    return f"{page_key}.md" if page_key else ""
