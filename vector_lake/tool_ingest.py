import json
import os
import hashlib
import logging
import re
import time

import yaml
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

from vector_lake import get_extension_root
from vector_lake.db_store import mark_file_processed
from vector_lake import governance_store
from vector_lake.skeleton_parser import parse_static_skeleton
from vector_lake.wiki_utils import (
    flush_durable,
    split_frontmatter,
    get_raw_dir,
    get_wiki_dir,
    get_index_path,
    is_private_raw_source,
    load_config,
    read_frontmatter_only,
    validate_wiki_filename,
)
from vector_lake.schema_validator import VALID_PREDICATES
from vector_lake.purpose_contract import (
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    validate_ingest_payload,
)

log = logging.getLogger("vector-lake-ingest")

def list_ingest_tasks(limit: int = 20, include_queued: bool = True) -> str:
    """List ingest jobs that require operator or host-subagent action."""
    from vector_lake.db_store import get_jobs_by_status

    statuses = ["awaiting_subagent"]
    if include_queued:
        statuses.insert(0, "queued")
    rows = get_jobs_by_status(statuses, limit=limit)
    if not rows:
        return "No queued or awaiting-subagent ingest jobs."
    lines = ["=== Ingest Task Queue ==="]
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.get("payload") or "{}")
        except Exception:
            payload = {}
        lines.append(
            "- "
            f"{row.get('job_id')} "
            f"status={row.get('status')} retries={row.get('retries')} "
            f"file={payload.get('filepath', '<unknown>')} "
            f"task_packet={row.get('task_packet_path') or '<not-created>'}"
        )
    return "\n".join(lines)


def claim_ingest_tasks(limit: int = 5, lease_seconds: int = 3600) -> str:
    """Lease task packets to the current host runtime and return structured work."""
    from vector_lake.db_store import claim_subagent_jobs

    claimed = claim_subagent_jobs(limit=limit, lease_seconds=lease_seconds)
    tasks = []
    for row in claimed:
        task_packet = None
        packet_path = row.get("task_packet_path")
        if packet_path:
            try:
                task_packet = json.loads(Path(packet_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                # An unreadable packet is unusable work.  Retiring it here (rather than
                # handing it back on every claim) stops it consuming a batch slot forever;
                # with the write gate no longer hard-failing on job health, the terminal
                # state stays visible without blocking wiki writes.
                from vector_lake.db_store import update_job_status

                update_job_status(
                    row.get("job_id"),
                    "failed",
                    f"Unreadable task packet ({packet_path}): {exc}",
                )
                continue
        if isinstance(task_packet, dict) and "error" not in task_packet:
            metadata = task_packet.setdefault("metadata", {})
            processed = metadata.setdefault("processed_data", {})
            processed.update({
                "job_id": row.get("job_id"),
                "lease_owner": row.get("lease_owner"),
                "lease_token": row.get("lease_token"),
                "lease_generation": row.get("lease_generation"),
            })
        tasks.append({
            "job_id": row.get("job_id"),
            "status": row.get("status"),
            "lease_until": row.get("lease_until"),
            "lease_owner": row.get("lease_owner"),
            "lease_token": row.get("lease_token"),
            "lease_generation": row.get("lease_generation"),
            "task_packet_path": packet_path,
            "task_packet": task_packet,
        })
    return json.dumps(tasks, ensure_ascii=False, indent=2)


#: Failure reasons that a later attempt can fix, so they must not spend the attempt budget.
#:
#: Matched on the message the finalize gates produce, deliberately narrow: a conflict means the
#: page moved under the model and the next attempt reads the new version, whereas a schema or
#: naming rejection will reject the same payload every time.
TRANSIENT_FAILURE_MARKERS = (
    "canonical version conflict",
    "version conflict",
    "is no longer finalizable",
)


def is_transient_failure(reason: str) -> bool:
    """Whether ``reason`` describes something a retry can resolve."""
    lowered = str(reason or "").lower()
    return any(marker in lowered for marker in TRANSIENT_FAILURE_MARKERS)


def record_ingest_failure(job_id: str, reason: str) -> str:
    """Consume one attempt of a job's bounded budget; report whether it is now terminal.

    The runner's failure branches used to increment only a local counter.  The job stayed in
    ``subagent_processing`` holding a one-hour lease, ``claim_ingest_tasks`` re-claims the
    oldest job first, and the same deterministically-rejecting task was therefore re-attempted
    every hour forever -- one model call each time, with no attempt cap and nothing visible in
    the queue.  The contract in ``scripts/ingest_runner.py`` already promised "C3 bounded
    attempts; never re-claim a job to try once more"; this is what makes that true.

    A transient failure (:func:`is_transient_failure`) is released for retry without consuming
    the budget, because terminalizing it would abandon a source a later attempt would ingest.
    """
    from vector_lake.db_store import (
        MAX_INGEST_ATTEMPTS,
        get_connection,
        release_job_for_retry,
        update_job_status,
    )

    reason = str(reason or "unspecified failure")[:500]
    row = get_connection().execute(
        "SELECT retries FROM jobs WHERE job_id = ?", (str(job_id),)
    ).fetchone()
    if row is None:
        return f"job {job_id} is not in the jobs table; nothing recorded"
    if is_transient_failure(reason):
        release_job_for_retry(str(job_id), reason)
        return f"job {job_id} released for retry (transient: {reason[:80]}); attempt budget untouched"
    attempt = int(row["retries"] or 0) + 1
    update_job_status(str(job_id), "failed", reason)
    if attempt < MAX_INGEST_ATTEMPTS:
        return f"job {job_id} failed attempt {attempt}/{MAX_INGEST_ATTEMPTS}: will be re-dispatched"

    # Terminal: remember the *content* so the scan stops re-dispatching it.  Without this the
    # source cycles forever -- each new job gets a fresh attempt budget, so "bounded attempts"
    # only bounds one job, not the source (measured: ``DHWB-20260913.md``, six days of a
    # deterministic ``categories`` rejection at three model calls per round).
    payload = get_connection().execute(
        "SELECT payload FROM jobs WHERE job_id = ?", (str(job_id),)
    ).fetchone()
    abandoned = 0
    if payload:
        try:
            fields = json.loads(payload["payload"] or "{}")
        except (TypeError, ValueError):
            fields = {}
        filepath = str(fields.get("filepath") or "")
        file_hash = str(fields.get("hash") or "")
        if filepath and file_hash:
            from vector_lake.db_store import record_abandoned_source

            abandoned = record_abandoned_source(filepath, file_hash, reason)
    suffix = (
        f"; source abandoned after {abandoned} terminal job(s) on this content "
        "(clear with `cli.py ingest-tasks --clear-abandoned`)"
        if abandoned
        else ""
    )
    return (
        f"job {job_id} failed attempt {attempt}/{MAX_INGEST_ATTEMPTS}: attempt budget "
        f"spent, no further dispatch ({reason}){suffix}"
    )


def reconcile_ingest_in_flight(grace_seconds: float = 120.0) -> dict:
    """Release in-flight markers whose source has no dispatchable job behind it.

    The ledger exists to stop two scans scheduling the same source twice, so a marker is only
    meaningful while a job is actually queued, leased or awaiting a subagent.  A marker left
    without one makes the source invisible: ``prepare_ingest_batch`` skips it, so it never gets
    enqueued, and the marker is refreshed on every later scan that sees it expire.

    Measured on the live corpus 2026-09-19 07:41: 24 sources marked in one batch while only 2
    jobs were created, three of them with no job row at all and no key match either.  The
    producer of that particular batch was not identified, which is exactly why this is written
    as a reconciliation rather than a fix to one caller: any path that leaves a marker behind
    becomes a transient state instead of a stuck one.

    ``grace_seconds`` keeps a marker that a scan is legitimately holding right now (the batch
    is marked before it is enqueued) from being reaped out from under it.
    """
    import time

    from vector_lake.db_store import get_connection

    ledger = _load_ingest_in_flight()
    if not ledger:
        return {"checked": 0, "released": []}

    conn = get_connection()
    # Statuses from which the pipeline will still act on the job.  ``failed`` only counts while
    # its attempt budget is unspent: at the cap ``claim_pending_jobs`` refuses to dispatch it, so
    # a marker pointing at such a job is exactly the orphan this function exists to release.
    # Getting that wrong is not theoretical -- measured on 2026-09-19, 7 of the 11 terminal
    # failures held in-flight markers, and treating them as active kept every one of those sources
    # invisible to the scan that would have re-enqueued them.
    from vector_lake.db_store import MAX_INGEST_ATTEMPTS

    dispatchable = ("queued", "dispatched", "awaiting_subagent", "subagent_processing", "completed")
    placeholders = ", ".join("?" for _ in dispatchable)
    active: set[str] = set()
    rows = conn.execute(
        f"SELECT payload FROM jobs WHERE status IN ({placeholders}) "
        f"OR (status = 'failed' AND retries < ?)",
        (*dispatchable, MAX_INGEST_ATTEMPTS),
    )
    for (payload,) in rows:
        try:
            filepath = json.loads(payload or "{}").get("filepath")
        except (TypeError, ValueError):
            continue
        if filepath:
            active.add(_ingest_ref(filepath))

    now = time.time()
    released = [
        ref
        for ref, entry in ledger.items()
        if now - float(entry.get("since", 0) or 0) >= grace_seconds and ref not in active
    ]
    for ref in released:
        _clear_ingest_in_flight(ref)
    return {"checked": len(ledger), "released": released}


def list_terminal_failed_ingest_jobs() -> str:
    """Jobs that spent their attempt budget, with the source each names."""
    from vector_lake.db_store import list_terminal_failed_jobs

    jobs = list_terminal_failed_jobs()
    if not jobs:
        return "No terminal-failed ingest jobs."
    lines = [f"{len(jobs)} terminal-failed ingest job(s):"]
    for job in jobs:
        name = os.path.basename(job["filepath"]) or "(no filepath)"
        lines.append(
            f"- {job['job_id']} retries={job['retries']} at {job['updated_at'][:19]} "
            f"{name}: {job['error_msg'][:150]}"
        )
    lines.append(
        "Close those whose source is now ingested with `cli.py ingest-tasks --close-terminal-failed`."
    )
    return "\n".join(lines)


def close_terminal_failed_ingest_jobs(source_ingested_only: bool = True) -> str:
    """Mark terminal-failed jobs superseded once their source is ingested."""
    from vector_lake.db_store import close_terminal_failed_jobs

    result = close_terminal_failed_jobs(source_ingested_only=source_ingested_only)
    kept = len(result["kept"])
    message = f"Closed {len(result['closed'])} terminal-failed job(s) as superseded."
    if kept:
        message += (
            f" Kept {kept} whose source has no processed_files row: those are still unfinished, "
            "and the abandonment rule decides their fate."
        )
    return message


def list_abandoned_ingest_sources() -> str:
    """Sources withheld from dispatch after repeated deterministic failures."""
    from vector_lake.db_store import list_abandoned_sources

    rows = list_abandoned_sources()
    if not rows:
        return "No abandoned ingest sources."
    lines = [f"{len(rows)} abandoned ingest source(s):"]
    for row in rows:
        lines.append(
            f"- {row['filepath']} (terminal_jobs={row['terminal_jobs']}, "
            f"since {str(row['abandoned_at'])[:19]}): {str(row['reason'])[:160]}"
        )
    lines.append("Re-dispatch with `cli.py ingest-tasks --clear-abandoned [FILE]`, or fix the source.")
    return "\n".join(lines)


def clear_abandoned_ingest_sources(filepath: str | None = None) -> str:
    """Allow abandoned source(s) to be dispatched again."""
    from vector_lake.db_store import clear_abandoned_sources

    cleared = clear_abandoned_sources(filepath)
    scope = f"'{filepath}'" if filepath else "all sources"
    return (
        f"Cleared {cleared} abandoned ingest source record(s) for {scope}. "
        "They are eligible for dispatch on the next scan."
    )


def expire_ingest_tasks(max_age_seconds: int = 86400) -> str:
    """Mark stale awaiting-subagent ingest jobs as failed so they can be retried explicitly."""
    from vector_lake.db_store import expire_stale_subagent_jobs

    expired = expire_stale_subagent_jobs(max_age_seconds=max_age_seconds)
    return f"Expired {expired} awaiting-subagent ingest job(s)."

def canonical_source_name(raw_path: str) -> str:
    """The Source page name a task packet mandates for this raw file.

    Normalised, because the packet's name must satisfy ``validate_wiki_filename`` or the
    task can never be finalised: an arXiv-style stem (``2608.19880v1``) contains dots,
    which the strict ``[Type]_[MainName]-[SubName]`` rule rejects, and ``finalize_ingest``
    refuses the write.  Those papers sat un-ingested for exactly this reason.
    """
    basename = Path(raw_path).stem
    safe = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", basename).strip("-")
    return f"Source_{safe or 'unnamed'}.md"


_NAME_KEY = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _name_key(value: str) -> str:
    """Alphanumeric-only comparison key, so ``a_b`` matches ``a-b``."""
    return _NAME_KEY.sub("", str(value)).lower()


def _raw_key(raw_path: str) -> str:
    """Comparison key for a raw source: its stem, alphanumerics only."""
    return _name_key(Path(str(raw_path)).stem)


def _declared_raw_sources() -> dict[str, dict]:
    """``{stem_key: {"page", "created", "source_hash"}}`` for every declared raw source.

    Same scan as :func:`_declared_raw_keys`, carrying the three facts the reconciliation below
    needs: which page declares the source, when that page was written, and -- for pages written
    since :func:`_stamp_source_hash` exists -- the raw content hash the page was compiled from.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return {}
    declared: dict[str, dict] = {}
    for entry in os.scandir(wiki_dir):
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        try:
            frontmatter = read_frontmatter_only(entry.path)
        except Exception:
            continue
        record = {
            "page": entry.name,
            "created": str(frontmatter.get("created") or ""),
            "source_hash": str(frontmatter.get("source_hash") or ""),
        }
        for source in frontmatter.get("sources") or []:
            text = str(source).replace("\\", "/")
            if "raw/" not in text:
                continue
            key = _raw_key(text)
            if key:
                declared.setdefault(key, record)
    return declared


def _declared_raw_keys() -> set:
    """Stems of every raw source that some wiki page declares in its ``sources:``.

    This is the authoritative "already ingested" signal, and it is path-form tolerant
    once normalised (``a_b`` == ``a-b``, spaces and punctuation dropped).  Matching on
    published Source *page names* cannot work for short stems (``AI.md``, ``品牌.md``):
    the page is named ``Source_<dir>-<stem>-<hash8>``, so a short stem never lands at
    the start and substring matching over-matches unrelated pages.

    Built from frontmatter only (one cheap head-read per page) and only when the caller
    actually needs it, so an idle scan pays nothing.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return set()
    keys = set()
    for entry in os.scandir(wiki_dir):
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        try:
            frontmatter = read_frontmatter_only(entry.path)
        except Exception:
            continue
        for source in frontmatter.get("sources") or []:
            text = str(source).replace("\\", "/")
            if "raw/" not in text:
                continue
            key = _raw_key(text)
            if key:
                keys.add(key)
    return keys


def _published_source_keys() -> list:
    """Normalised names of every existing ``Source_*`` page.

    Fallback signal for pages that declare no ``sources:`` path at all.  Used together
    with :func:`_declared_raw_keys` - the skip decision is their *union*, because for
    duplicate prevention an over-eager skip only delays an ingest while an under-eager
    one writes a second copy of a page that already exists.
    """
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return []
    return [
        _name_key(name[:-3])
        for name in os.listdir(wiki_dir)
        if name.startswith("Source_") and name.endswith(".md")
    ]


def _already_published(filepath: str, name_keys: list, declared_keys: set, min_key_len: int = 4) -> bool:
    """True when this raw source is already published, by either signal.

    A raw file that has been published must not be prepared again: the
    ``processed_files`` row is written on the finalize path, so a source ingested while
    the ingest pipeline was stalled has a page but no row, and the ledger check below
    therefore keeps treating it as new - the queue refilled with such files indefinitely.
    Short stems are only matched by prefix against page names (a stem like ``AI`` would
    otherwise match unrelated pages) but are matched exactly against declared sources,
    which is why the declaration signal is needed in the union.
    """
    key = _raw_key(filepath)
    if not key:
        return False
    if key in declared_keys:
        return True
    if len(key) < min_key_len:
        return any(page.startswith(key) for page in name_keys)
    return any(key in page for page in name_keys)

def raw_publication_index() -> dict:
    """Public: both signals used to decide whether a raw source is already published.

    Returns ``{"name_keys": [...], "declared_keys": set()}`` so a caller can build the
    index once and reuse it across a batch.  ``prepare_ingest_batch`` and the host-side
    ingest runner must share this single implementation - when they did not, their
    verdicts drifted (a name-only predicate called 88 raw files un-ingested where the
    union calls 73, and would have re-ingested 15 that were already published).
    """
    return {
        "name_keys": _published_source_keys(),
        "declared_keys": _declared_raw_keys(),
        "declared_pages": _declared_raw_sources(),
    }


def raw_is_published(filepath: str, index: dict | None = None) -> bool:
    """Public: is this raw source already published?  Union of both signals."""
    if index is None:
        index = raw_publication_index()
    return _already_published(filepath, index["name_keys"], index["declared_keys"])


def _stamp_source_hash(content: str, raw_hash: str) -> str:
    """Record the raw content hash in the Source page's frontmatter.

    This is the durable form of "which content was this page compiled from".  Nothing else
    records it: the page carries ``sources: [raw/…]`` and a creation date, so the reconciliation
    in :func:`_published_source_verdict` has to *infer* whether the raw file changed since.
    Stamping the hash at finalize -- where the packet already carries it -- turns that inference
    into a comparison.

    Best effort by contract: content without frontmatter, or unparsable frontmatter, is returned
    unchanged rather than made to fail.  The page's own validation is the authority on whether it
    may be written.
    """
    if not raw_hash:
        return content
    try:
        frontmatter, body = split_frontmatter(content)
    except Exception:  # noqa: BLE001 - a page whose frontmatter will not parse is not ours to fix
        return content
    if not frontmatter or frontmatter.get("source_hash") == raw_hash:
        return content
    frontmatter["source_hash"] = raw_hash
    try:
        rendered = yaml.safe_dump(
            frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False
        )
    except Exception:  # noqa: BLE001 - same
        return content
    return "---\n" + rendered + "---\n" + body


def _published_source_verdict(filepath: str, index: dict) -> str:
    """What to do with a source the publication check calls already published.

    ``"record"`` -- a page **declares** this raw path and the file cannot have changed since that
    page was written, so the missing ``processed_files`` row is written and the source is skipped.
    ``"stale"`` -- declared, but the raw file is newer than the page, so the page predates the
    current content and the source must stay pending: re-ingesting is the only way the edit is ever
    seen.  ``"skip"`` -- only the loose name signal matched (over-eager by design, so it may not
    retire a source permanently), or the page records no usable date.

    The state this exists for: a source ingested while the pipeline was stalled has a page but no
    ledger row, and the published branch runs *before* the hash comparison a row would enable.  So
    without this the ledger is permanently wrong **and** a later edit to that source is silently
    ignored -- measured on 2026-09-19, one source in exactly that state
    (``raw/医疗信息化/推动健康中国建设取得决定性进展有关情况.md``).  No raw hash is stored on the
    page, so the evidence is its ``created`` date against the file's mtime, and the ambiguous
    direction resolves to re-ingesting rather than retiring.
    """
    declared = index.get("declared_pages", {}).get(_raw_key(filepath))
    if not declared:
        return "skip"

    # A stamped hash is proof; prefer it over any inference.
    recorded_hash = str(declared.get("source_hash") or "").strip()
    if recorded_hash:
        current_hash = calculate_hash(filepath)
        if not current_hash:
            return "skip"
        return "record" if current_hash == recorded_hash else "stale"

    # Legacy pages carry no hash, so fall back to dates: compared as YYYY-MM-DD strings because
    # this module rebinds ``datetime`` to the class on the ``from datetime import datetime``
    # line, which makes ``datetime.date`` a method descriptor here.
    created_date = str(declared.get("created") or "")[:10]
    if len(created_date) != 10 or created_date[4] != "-" or created_date[7] != "-":
        return "skip"
    try:
        raw_date = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(filepath)))
    except OSError:
        return "skip"
    return "record" if raw_date <= created_date else "stale"


def calculate_hash(filepath: str) -> str:
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        log.error(f"Error calculating hash for {filepath}: {e}")
        return ""

def _read_purpose() -> str:
    try:
        return render_strategy_directive()
    except PurposeContractError as exc:
        log.error("Strategic purpose contract is unavailable: %s", exc)
        return "[STRATEGIC PURPOSE CONTRACT UNAVAILABLE: halt and repair purpose.md before ingesting.]"

INTEGRATION_DISPOSITIONS = {"integrated", "standalone", "rejected"}
INTEGRATION_PREDICATES = {"validates", "falsifies", "depends-on", "mentions", "related_to"}
INTEGRATION_EVENT_TAGS = {
    "Release", "Pivot", "Conflict", "Validation", "Observation", "Decision", "Execution", "Outcome"
}
INGEST_CANDIDATE_TYPES = {
    "concept", "vendor", "institution", "product", "person", "event", "policy", "standard",
    "synthesis",
}
INGEST_SEARCH_STOP_WORDS = {
    "about", "after", "ai", "also", "an", "and", "api", "are", "as", "at", "based", "be", "been", "before",
    "between", "by", "can", "compiled", "consensus", "directive", "for", "from", "generated", "has",
    "have", "here", "if", "in", "into", "is", "it", "its", "latest", "model", "more", "no", "not",
    "of", "on", "only", "or", "other", "our", "read", "should", "source", "sources", "system", "than",
    "that", "the", "their", "then", "there", "this", "through", "to", "truth", "using", "was", "were",
    "which", "while", "who", "will", "with",
}
INGEST_CHINESE_STOP_TERMS = {
    "体系", "机制", "医疗", "系统", "平台", "医院", "数据", "管理", "治理", "国家", "智能", "人工智能",
    "评估", "实验", "资本", "架构", "推理", "物理", "生成式", "临床", "模型", "技术", "应用", "方案",
    "服务", "项目", "流程",
}


def _normalise_search_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _read_relevant_index_context(filepath: str, max_nodes: int = 40) -> str:
    """Return deterministic source-relevant candidates from the complete index.

    A cold knowledge base (no index yet, no wiki pages) has nothing to link to,
    so an empty candidate list is the correct answer.  A knowledge base whose
    wiki is populated but whose index is missing is a real projection fault and
    stays a hard error, because ingesting without the existing-node context is
    how duplicate entities are created.
    """
    index_path = get_index_path()
    if not index_path.exists():
        wiki_pages = [
            name
            for name in os.listdir(get_wiki_dir())
            if name.endswith(".md") and name not in ("index.md", "log.md", "overview.md")
        ] if get_wiki_dir().exists() else []
        if wiki_pages:
            raise RuntimeError(
                f"Cannot build ingest context for {filepath}: {index_path.name} is missing while "
                f"{len(wiki_pages)} wiki page(s) exist. Rebuild the index projection "
                "(`projection-rebuild-index`) before ingesting new sources."
            )
        return "- (no existing nodes: cold knowledge base)"
    try:
        source_text = Path(filepath).read_text(encoding="utf-8", errors="replace")[:200_000]
        source_norm = _normalise_search_text(f"{Path(filepath).stem} {source_text}")
        source_words = Counter(
            word for word in re.findall(r"\b[a-z0-9]{2,}\b", source_text.lower())
            if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
        )
        source_acronyms = {
            token.lower() for token in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", source_text)
        }
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        nodes = index_data.get("nodes", {})
        if not nodes:
            return ""
        canonical_versions = governance_store.canonical_page_versions(set(nodes))
        scored = []
        wiki_dir = get_wiki_dir()
        for key, node in nodes.items():
            if str(node.get("type") or "").lower() not in INGEST_CANDIDATE_TYPES:
                continue
            filename = f"{key}.md"
            try:
                validate_wiki_filename(filename)
            except ValueError:
                continue
            target_path = wiki_dir / filename
            target_hash = canonical_versions.get(key)
            if not target_path.exists() or not target_hash:
                continue
            aliases = node.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            labels = [key.split("_", 1)[-1], node.get("title", ""), *aliases]
            score = 0
            match_reasons = set()
            for label in labels:
                chinese_label = "".join(re.findall(r"[\u4e00-\u9fff]", str(label)))
                if (
                    len(chinese_label) >= 3
                    and chinese_label not in INGEST_CHINESE_STOP_TERMS
                    and chinese_label in source_norm
                ):
                    score = max(score, 180 + len(chinese_label))
                    match_reasons.add(f"exact_chinese:{chinese_label}")
                label_words = [
                    word for word in re.findall(r"\b[a-z0-9]{2,}\b", str(label).lower())
                    if word not in INGEST_SEARCH_STOP_WORDS and word not in {"concept", "vendor", "institution"}
                ]
                if not chinese_label and label_words and all(word in source_words for word in label_words):
                    score = max(
                        score,
                        160 + sum(min(len(word), 12) for word in label_words)
                        + sum(min(source_words[word], 5) for word in label_words),
                    )
                    match_reasons.add(f"exact_terms:{'+'.join(label_words)}")
            candidate_words = {
                word for word in re.findall(
                    r"\b[a-z0-9]{2,}\b",
                    f"{node.get('title', '')} {(node.get('summary', '') or '')[:500]}".lower(),
                )
                if word not in INGEST_SEARCH_STOP_WORDS and not word.isdigit()
            }
            for word in source_words.keys() & candidate_words:
                score += 45 if word in source_acronyms else min(len(word), 12)
                match_reasons.add(f"overlap:{word}")
            if score <= 0:
                continue
            scored.append((score, key, node, target_hash, sorted(match_reasons)))
        scored.sort(key=lambda item: (-item[0], item[1]))
        lines = []
        candidate_limit = max(1, int(max_nodes))
        scan_limit = max(100, candidate_limit * 5)
        for score, key, node, target_hash, match_reasons in scored[:scan_limit]:
            try:
                _read_canonical_target_content(f"{key}.md", target_hash)
            except ValueError:
                continue
            lines.append(json.dumps({
                "target": f"{key}.md",
                "target_hash": target_hash,
                "type": node.get("type", "unknown"),
                "title": node.get("title", key),
                "summary": (node.get("summary", "") or "")[:160],
                "match_score": score,
                "match_reasons": match_reasons[:8],
            }, ensure_ascii=False))
            if len(lines) >= candidate_limit:
                break
        return "\n".join(f"- {line}" for line in lines)
    except Exception as exc:
        raise RuntimeError(
            f"Could not build source-relevant ingest context for {filepath}: {exc}"
        ) from exc


def _updated_now(content: str) -> str:
    updated = datetime.now(timezone.utc).isoformat()
    return re.sub(r"(?m)^updated:\s*.*$", f"updated: {updated}", content, count=1)


def _read_canonical_target_content(filename: str, expected_version: str) -> str:
    """Read Markdown whose extracted entity state matches the canonical version."""
    from vector_lake.db_store import get_connection, init_db

    candidates = []
    target_path = get_wiki_dir() / filename
    if target_path.exists():
        candidates.append(("markdown projection", target_path.read_text(encoding="utf-8")))
    init_db()
    rows = get_connection().execute(
        "SELECT payload_text FROM mutation_outbox "
        "WHERE filename = ? AND mutation_type = 'update' AND payload_text IS NOT NULL "
        "ORDER BY id DESC LIMIT 20",
        (filename,),
    ).fetchall()
    candidates.extend(("mutation outbox", str(row["payload_text"])) for row in rows)
    seen = set()
    for _origin, content in candidates:
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            content_version = governance_store.canonical_page_version_from_content(filename, content)
        except Exception:
            continue
        if content_version == expected_version:
            return content
    raise ValueError(
        f"No canonical-aligned Markdown snapshot is available for {filename}; "
        "replay or repair its latest mutation outbox projection before integrating"
    )


def _upsert_section_relation(
    content: str,
    heading: str,
    marker: str,
    line: str,
    legacy_tokens: tuple[str, ...] = (),
) -> str:
    start = content.find(heading)
    if start < 0:
        raise ValueError(f"Integration target is missing the required section: {heading}")
    section_start = start + len(heading)
    next_heading = re.search(r"(?m)^##\s+", content[section_start:])
    section_end = section_start + (next_heading.start() if next_heading else len(content[section_start:]))
    section = content[section_start:section_end]
    matches = []
    for relation_match in re.finditer(r"(?m)^-[^\n]*$", section):
        relation_line = relation_match.group(0)
        if marker in relation_line or (
            legacy_tokens and all(token in relation_line for token in legacy_tokens)
        ):
            matches.append(relation_match)
    if matches:
        chunks = [section[:matches[0].start()], line]
        cursor = matches[0].end()
        for duplicate in matches[1:]:
            chunks.append(section[cursor:duplicate.start()])
            cursor = duplicate.end()
        chunks.append(section[cursor:])
        merged = "".join(chunks)
        return _updated_now(content[:section_start] + merged + content[section_end:])
    insert_at = section_end
    prefix = content[:insert_at].rstrip()
    suffix = content[insert_at:].lstrip("\n")
    merged = f"{prefix}\n\n{line}\n"
    if suffix:
        merged += f"\n{suffix}"
    return _updated_now(merged)


def _apply_integration_disposition(files_written: list, processed_data: dict) -> tuple[list, str]:
    """Validate semantic completion and materialize bounded source/target updates."""
    integration = processed_data.get("integration")
    if not isinstance(integration, dict):
        raise ValueError("finalize_ingest requires an integration disposition")
    disposition = str(integration.get("disposition") or "").strip().lower()
    if disposition not in INTEGRATION_DISPOSITIONS:
        raise ValueError(f"integration disposition must be one of {sorted(INTEGRATION_DISPOSITIONS)}")

    files = []
    for item in files_written:
        record = dict(item)
        if "filepath" in record and not record.get("content"):
            record["content"] = Path(record["filepath"]).read_text(encoding="utf-8")
        files.append(record)

    reason = str(integration.get("reason") or "").strip()
    if disposition == "rejected":
        if files:
            raise ValueError("rejected ingest disposition must not include wiki files")
        if len(reason) < 12:
            raise ValueError("rejected ingest disposition requires an auditable reason")
        return [], disposition

    canonical_name = str(processed_data.get("canonical_name") or "").strip()
    source_items = [item for item in files if os.path.basename(str(item.get("filename", ""))) == canonical_name]
    if len(source_items) != 1:
        raise ValueError(f"{disposition} ingest disposition requires exactly one canonical source page: {canonical_name}")
    source_item = source_items[0]
    source_item["expected_version"] = str(processed_data.get("source_hash") or "")
    for item in files:
        if item is not source_item:
            item.setdefault("expected_version", "")
    relations = integration.get("relations") or []
    if disposition == "standalone":
        if relations:
            raise ValueError("standalone ingest disposition cannot include integration relations")
        if len(reason) < 12:
            raise ValueError("standalone ingest disposition requires an auditable reason")
        return files, disposition
    if not isinstance(relations, list) or not relations:
        raise ValueError("integrated ingest disposition requires at least one relation")

    submitted_names = {os.path.basename(str(item.get("filename", ""))) for item in files}
    source_content = str(source_item.get("content") or "").rstrip()
    source_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
    graph_heading = "## Graph Integration"
    if graph_heading not in source_content:
        source_content += f"\n\n{graph_heading}\n"
    target_mutations = []
    seen_targets = set()
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("integration relations must be objects")
        target = os.path.basename(str(relation.get("target") or ""))
        if target != str(relation.get("target") or ""):
            raise ValueError("integration relation target must be a wiki basename")
        validate_wiki_filename(target)
        if target == canonical_name or target in submitted_names or target in seen_targets:
            raise ValueError(f"integration target is duplicated or conflicts with submitted files: {target}")
        seen_targets.add(target)
        target_path = get_wiki_dir() / target
        if not target_path.exists():
            raise ValueError(f"integration target does not exist: {target}")
        target_key = target[:-3]
        actual_hash = governance_store.canonical_page_versions({target_key}).get(target_key)
        expected_hash = str(relation.get("target_hash") or "")
        if not expected_hash or expected_hash != actual_hash:
            raise ValueError(f"integration target_hash is stale or missing for {target}")
        target_content = _read_canonical_target_content(target, expected_hash)
        predicate = str(relation.get("predicate") or "").strip()
        if predicate not in INTEGRATION_PREDICATES:
            raise ValueError(f"unsupported integration predicate: {predicate}")
        evidence = " ".join(str(relation.get("evidence") or "").split())
        if len(evidence) < 12:
            raise ValueError(f"integration evidence is too short for {target}")
        try:
            confidence = float(relation.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"integration confidence must be numeric for {target}") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"integration confidence must be in [0, 1] for {target}")
        event_date = str(relation.get("event_date") or "")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"integration event_date must be YYYY-MM-DD for {target}") from exc
        event_tag = str(relation.get("event_tag") or "").strip().strip("[]")
        if event_tag not in INTEGRATION_EVENT_TAGS:
            raise ValueError(f"unsupported integration event_tag for {target}: {event_tag}")
        relation_id = hashlib.sha256(f"{source_key}\x00{target_key}".encode("utf-8")).hexdigest()[:16]
        marker = f"<!-- vector-lake-relation:{relation_id} -->"
        source_content = _upsert_section_relation(
            source_content,
            graph_heading,
            marker,
            f"- [{predicate}:: [[{target_key}]]] {evidence} "
            f"(confidence: {confidence:.2f}) {marker}",
            legacy_tokens=(f"[[{target_key}]]",),
        )
        source_anchor = f"(Source: [[{source_key}]])"
        target_heading = (
            "## 支撑拓扑 (Supporting Topology)"
            if target.startswith("Synthesis_")
            else "## 2. 证据时间线"
        )
        target_line = (
            f"- [depends-on:: [[{source_key}]]] {evidence} {source_anchor} {marker}"
            if target.startswith("Synthesis_")
            else f"- [{event_date}] [{event_tag}] {evidence} {source_anchor} {marker}"
        )
        target_content = _upsert_section_relation(
            target_content,
            target_heading,
            marker,
            target_line,
            legacy_tokens=(source_anchor,),
        )
        target_mutations.append({
            "filename": target,
            "content": target_content,
            "expected_version": expected_hash,
        })

    source_item["content"] = _updated_now(source_content)
    return files + target_mutations, disposition


def _build_ingest_instructions(filepath: str, file_hash: str, canonical_name: str) -> str:
    schema_content = ""
    try:
        schema_content = (get_extension_root() / "schema.md").read_text(encoding="utf-8")
        category_path = get_extension_root() / "SCHEMA_CATEGORIES.md"
        if category_path.exists():
            schema_content += "\n\n" + category_path.read_text(encoding="utf-8")
    except OSError:
        pass
    prompt_path = get_extension_root() / "templates" / "ingest_prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError("templates/ingest_prompt.md not found")
    return (
        prompt_path.read_text(encoding="utf-8")
        .replace("{{filepath}}", str(filepath))
        .replace("{{file_hash}}", file_hash)
        .replace("{{canonical_name}}", canonical_name)
        .replace("{{skeleton_block}}", parse_static_skeleton(filepath))
        .replace("{{schema_content}}", schema_content)
        .replace("{{index_summary}}", _read_relevant_index_context(filepath))
        .replace("{{purpose_content}}", _read_purpose())
        .replace("{{valid_predicates}}", ", ".join(sorted(VALID_PREDICATES)))
    )


def requeue_legacy_ingest_jobs() -> int:
    """Rebuild pre-integration-contract awaiting packets before they are claimed."""
    from vector_lake import db_store

    db_store.init_db()
    conn = db_store.get_connection()
    rows = conn.execute(
        "SELECT job_id, payload, task_packet_path FROM jobs "
        "WHERE task_type = 'ingest' AND status = 'awaiting_subagent'"
    ).fetchall()
    migrations = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if "source_hash" in payload:
            continue
        filepath = str(payload.get("filepath") or "")
        file_hash = str(payload.get("hash") or "")
        canonical_name = str(payload.get("canonical_name") or "")
        if not filepath or not file_hash or not canonical_name or not Path(filepath).exists():
            continue
        canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
        payload["source_hash"] = governance_store.canonical_page_versions({canonical_key}).get(
            canonical_key,
            "",
        )
        payload["instructions"] = _build_ingest_instructions(filepath, file_hash, canonical_name)
        migrations.append((str(row["job_id"]), payload, str(row["task_packet_path"] or "")))

    if not migrations:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with db_store.transaction():
        for job_id, payload, _packet_path in migrations:
            conn.execute(
                "UPDATE jobs SET payload = ?, status = 'queued', retries = 0, "
                "error_msg = 'Legacy ingest packet rebuilt for the integration contract', "
                "available_at = ?, updated_at = ?, task_packet_path = NULL, "
                "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                "WHERE job_id = ? AND status = 'awaiting_subagent'",
                (json.dumps(payload, ensure_ascii=False), now, now, job_id),
            )
    from vector_lake.native_llm import remove_subagent_task

    for _job_id, _payload, packet_path in migrations:
        if packet_path:
            try:
                remove_subagent_task(packet_path)
            except OSError:
                log.warning("Could not remove superseded ingest packet: %s", packet_path)
    return len(migrations)

INGEST_IN_FLIGHT_TTL_SECONDS = 3600


def _ingest_processing_path() -> Path:
    from vector_lake.wiki_utils import get_ingest_processing_path

    return get_ingest_processing_path()


def _ingest_ref(filepath) -> str:
    """Stable per-source key: the resolved path, not the content hash.

    Keying in-flight state by content hash silently dropped a second source whose
    content happened to match an earlier file, and reported "fully synced" while
    that source was never compiled.
    """
    try:
        return str(Path(filepath).resolve())
    except OSError:
        return str(filepath)


def _read_ingest_in_flight_unlocked() -> dict:
    processing_file = _ingest_processing_path()
    try:
        with open(processing_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Discarding unreadable ingest in-flight state %s: %s", processing_file, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    now_ts = datetime.now(timezone.utc).timestamp()
    return {
        key: entry
        for key, entry in data.items()
        if isinstance(entry, dict) and now_ts - float(entry.get("since", 0)) < INGEST_IN_FLIGHT_TTL_SECONDS
    }


def _write_ingest_in_flight_unlocked(data: dict) -> None:
    processing_file = _ingest_processing_path()
    tmp_path = processing_file.with_name(processing_file.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        flush_durable(handle)
    os.replace(tmp_path, processing_file)


def _load_ingest_in_flight() -> dict:
    from filelock import FileLock

    processing_file = _ingest_processing_path()
    with FileLock(str(processing_file) + ".lock", timeout=10):
        return _read_ingest_in_flight_unlocked()


def _mark_ingest_in_flight(filepaths: list) -> None:
    from filelock import FileLock

    if not filepaths:
        return
    processing_file = _ingest_processing_path()
    now_ts = datetime.now(timezone.utc).timestamp()
    with FileLock(str(processing_file) + ".lock", timeout=10):
        # ``_read_ingest_in_flight_unlocked`` already drops TTL-expired entries.
        state = _read_ingest_in_flight_unlocked()
        for path in filepaths:
            state[_ingest_ref(path)] = {"since": now_ts, "filepath": str(path)}
        _write_ingest_in_flight_unlocked(state)


def _clear_ingest_in_flight(filepath) -> None:
    """Release the in-flight claim for one source (best effort, never fatal)."""
    from filelock import FileLock

    processing_file = _ingest_processing_path()
    try:
        with FileLock(str(processing_file) + ".lock", timeout=10):
            state = _read_ingest_in_flight_unlocked()
            if state.pop(_ingest_ref(filepath), None) is not None:
                _write_ingest_in_flight_unlocked(state)
    except Exception as exc:
        # Only affects duplicate-scheduling suppression, never correctness.
        log.warning("Could not release ingest in-flight claim for %s: %s", filepath, exc)


def _load_scan_config() -> dict:
    """Load the extension config without silently dropping the exclusion list.

    This used to swallow every failure into ``{}``, which quietly disabled
    ``exclude_paths`` and therefore re-ingested privacy-excluded raw sources.
    ``config.json`` is per-machine and untracked, so a missing file is a
    supported state -- but it must still yield the shipped defaults rather than
    an empty exclusion list.  An unreadable or malformed file stays a hard
    error (see ``wiki_utils.load_config``).
    """
    return load_config()


def prepare_ingest_batch(batch_size: int = 5) -> str:
    """Native Antigravity Agentic subagent orchestration."""
    from vector_lake.db_store import init_db

    config = _load_scan_config()

    target_dirs = [str((get_extension_root() / d).resolve()) for d in config.get("target_directories", [])]
    if not target_dirs:
        # An empty or missing list means "the active MEMORY root's raw/ directory".
        target_dirs = [str(get_raw_dir())]
    exclude_paths = config.get("exclude_paths", [])
    supported_exts = set(config.get("supported_extensions", [".md", ".txt"]))
    
    files_to_process = []
    for target_dir in target_dirs:
        folder = Path(target_dir)
        if not folder.exists(): continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.startswith('~') or file.startswith('.'): continue
                
                filepath = os.path.join(root, file)
                path_str = filepath.replace("\\", "/")
                if any(exclude in path_str for exclude in exclude_paths):
                    continue
                # The same privacy decision the raw event handler makes.  Without it this
                # scan enqueued the diary text that handler refused to trigger on.
                if is_private_raw_source(path_str):
                    continue

                if os.path.splitext(file)[1].lower() in supported_exts:
                    files_to_process.append(filepath)
                    
    from vector_lake.db_store import get_connection
    init_db()
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash, processed_at FROM processed_files")
    processed = {row["filepath"]: {"hash": row["file_hash"], "processed_at": row["processed_at"]} for row in cur.fetchall()}
    
    in_flight = _load_ingest_in_flight()
    from vector_lake.db_store import abandoned_source_keys

    abandoned = abandoned_source_keys()
    pending_files = []
    skipped_abandoned: list[str] = []
    reconciled: list[str] = []
    skipped_name_only: list[str] = []
    skipped_undated: list[str] = []
    publication_index = None  # built lazily: one frontmatter scan per call, only when needed

    for filepath in files_to_process:
        try:
            mtime = os.stat(filepath).st_mtime
            # A file already carrying an in-flight ingest is skipped so two workers
            # cannot compile the same source concurrently.
            if _ingest_ref(filepath) in in_flight:
                continue

            if filepath in processed:
                processed_at_str = processed[filepath]["processed_at"]
                if processed_at_str:
                    processed_at_dt = datetime.fromisoformat(processed_at_str.replace("Z", "+00:00"))
                    if processed_at_dt.tzinfo is None:
                        processed_at_dt = processed_at_dt.replace(tzinfo=timezone.utc)
                    if mtime <= processed_at_dt.timestamp():
                        continue

                file_hash = calculate_hash(filepath)
                if file_hash == processed[filepath]["hash"]:
                    continue
            else:
                # No ledger row.  Normally that means a genuinely new file - but a source
                # already published under a different raw path (or ingested on a path that
                # never recorded a row) would only be duplicated by preparing it again.
                if publication_index is None:
                    publication_index = raw_publication_index()
                if raw_is_published(filepath, publication_index):
                    verdict = _published_source_verdict(filepath, publication_index)
                    if verdict == "record":
                        # The page exists, declares this raw path, and the file has not been
                        # touched since: record the missing ledger row so the accounting is
                        # accurate and a *future* edit is detected as a changed hash.
                        mark_file_processed(filepath, calculate_hash(filepath))
                        reconciled.append(filepath)
                        continue
                    if verdict == "skip":
                        # Two very different reasons share this branch, so they are counted apart:
                        # a page that *declares* the source but records no usable date, and a
                        # match on the loose name signal only.  The second is the over-eager
                        # duplicate-prevention trade from ``_already_published`` -- deliberate,
                        # but it skips the source on every scan, so it must not be silent
                        # (independent review, 2026-09-19).
                        if _raw_key(filepath) not in publication_index.get("declared_pages", {}):
                            skipped_name_only.append(filepath)
                        else:
                            skipped_undated.append(filepath)
                        log.info("Skipping raw source already published: %s", filepath)
                        continue
                    # "stale": the page predates the file's current content.  Fall through and
                    # treat the source as pending, so the edit is ingested rather than ignored.
                    log.info(
                        "Raw source changed since it was published; re-ingesting: %s", filepath
                    )
                file_hash = calculate_hash(filepath)

            if file_hash:
                # Keyed on content: a corrected source has a new hash and is dispatchable again
                # without an operator clearing anything.
                if (str(filepath), str(file_hash)) in abandoned:
                    skipped_abandoned.append(filepath)
                    continue
                pending_files.append((filepath, file_hash))
        except OSError:
            log.warning("Skipping unreadable raw source: %s", filepath)

    if not pending_files:
        notes: list[str] = []
        if skipped_name_only:
            log.info(
                "Skipped %d source(s) matched only by the loose page-name signal: %s",
                len(skipped_name_only),
                ", ".join(sorted(os.path.basename(p) for p in skipped_name_only)[:3]),
            )
            notes.append(
                f"{len(skipped_name_only)} source(s) skipped on the page-name signal alone "
                "(no page declares them); see `vector_lake.tool_ingest._already_published`."
            )
        if skipped_undated:
            notes.append(
                f"{len(skipped_undated)} declared source(s) whose page records no usable date."
            )
        if reconciled:
            log.info(
                "Recorded %d published source(s) that had a page but no processed_files row: %s",
                len(reconciled), ", ".join(sorted(os.path.basename(p) for p in reconciled)[:3]),
            )
            notes.append(
                f"Recorded {len(reconciled)} already-published source(s) that had no "
                "processed_files row."
            )
        if skipped_abandoned:
            log.info(
                "Not dispatching %d abandoned source(s) whose content keeps failing: %s",
                len(skipped_abandoned), ", ".join(sorted(os.path.basename(p) for p in skipped_abandoned)[:3]),
            )
            notes.append(
                f"{len(skipped_abandoned)} source(s) abandoned after repeated deterministic "
                "failures (inspect with `cli.py ingest-tasks --abandoned`, re-dispatch with "
                "`--clear-abandoned`)."
            )
        if notes:
            return "No new files to ingest. " + " ".join(notes)
        return "No new files to ingest. System is fully synced."

    pending_files = pending_files[:batch_size]
    _mark_ingest_in_flight([filepath for filepath, _ in pending_files])

    from vector_lake.db_store import enqueue_job
    enqueued_count = 0
    last_payload = None
    # The batch is marked in-flight up front so a concurrent scan cannot schedule the same
    # source twice.  That leaves a window: if one file's enqueue raises, the loop aborts and
    # every file marked *after* it keeps a marker with no job behind it, so the source is
    # skipped for the whole TTL while looking scheduled.  Measured on 2026-09-19: 5 sources in
    # that state.  Release the markers of everything that never got enqueued.
    enqueued_paths: set = set()

    try:
        for filepath, file_hash in pending_files:
            try:
                canonical_name = canonical_source_name(filepath)
                canonical_key = canonical_name[:-3] if canonical_name.endswith(".md") else canonical_name
                source_hash = governance_store.canonical_page_versions({canonical_key}).get(canonical_key, "")
                instructions = _build_ingest_instructions(filepath, file_hash, canonical_name)

                payload = {
                    "filepath": str(filepath),
                    "hash": file_hash,
                    "canonical_name": canonical_name,
                    "source_hash": source_hash,
                    "instructions": instructions,
                }

                # ``supersede_finished`` is safe here and only here: this point in the scan is
                # reached after ``raw_is_published`` said the source has no page, so a finished
                # job for it is a contradiction (page and ledger disagree) rather than work to
                # preserve.  Without it such a source loops forever: enqueue returns the finished
                # job's id, the file is marked in-flight, nothing is dispatched, and the next
                # sweep releases the marker and tries again.
                enqueue_job("ingest", payload, replace_terminal=True, supersede_finished=True)
                enqueued_paths.add(_ingest_ref(filepath))
                enqueued_count += 1
                last_payload = payload
            except Exception:
                _clear_ingest_in_flight(filepath)
                log.exception("Failed to enqueue ingest work for %s", filepath)
                raise
    finally:
        unenqueued = [
            _ingest_ref(filepath)
            for filepath, _ in pending_files
            if _ingest_ref(filepath) not in enqueued_paths
        ]
        for ref in unenqueued:
            _clear_ingest_in_flight(ref)

    if batch_size == 1 and enqueued_count == 1:
        return json.dumps(last_payload)

    abandoned_note = (
        f" {len(skipped_abandoned)} abandoned source(s) were not dispatched."
        if skipped_abandoned else ""
    )
    reconciled_note = (
        f" Recorded {len(reconciled)} already-published source(s) that had no processed_files row."
        if reconciled else ""
    )
    return (
        f"Successfully enqueued {enqueued_count} files for ingestion."
        f"{abandoned_note}{reconciled_note}"
    )

def finalize_ingest(files_written: list, processed_data: dict) -> str:
    """Finalizes an ingest operation from a subagent using direct data."""
    from vector_lake.wiki_utils import SafeWriteError
    from vector_lake.db_store import finalize_ingest_job, validate_ingest_job_finalization
    from vector_lake.schema_validator import SchemaViolationException
    from vector_lake.defense_hook import DefenseHookException

    try:
        files = files_written
        job_id = processed_data.get("job_id")
        if not job_id:
            raise ValueError("finalize_ingest requires a claimed job_id")
        job_row = validate_ingest_job_finalization(str(job_id), processed_data)
        lease_owner = str(processed_data.get("lease_owner") or "")
        lease_token = str(processed_data.get("lease_token") or "")
        lease_generation = int(processed_data.get("lease_generation"))
        files, integration_disposition = _apply_integration_disposition(files, processed_data)
        contract = load_purpose_contract()
        node_records = validate_ingest_payload(files, contract)
                
        wiki_dir = get_wiki_dir()

        # Stamp the raw content hash onto the canonical Source page.  Done here rather than by
        # the model: the value has to be the packet's own hash, and a model-authored hash would
        # be one more thing to verify.
        canonical_name = str(processed_data.get("canonical_name") or "")
        raw_hash = str(processed_data.get("hash") or "")
        if canonical_name and raw_hash:
            for item in files:
                if os.path.basename(str(item.get("filename") or "")) != canonical_name:
                    continue
                if "filepath" in item and not item.get("content"):
                    try:
                        with open(item["filepath"], "r", encoding="utf-8") as handle:
                            item["content"] = handle.read()
                    except OSError:
                        break
                if item.get("content"):
                    item["content"] = _stamp_source_hash(str(item["content"]), raw_hash)
                break

        written_paths = []
        mutations = []
        for item in files:
            fname = os.path.basename(item["filename"])
            if "filepath" in item and not item.get("content"):
                with open(item["filepath"], "r", encoding="utf-8") as f:
                    item["content"] = f.read()
            fcontent = item["content"]
            
            if "Concept_Decision_" in fname:
                lower_content = fcontent.lower()
                if not all(k in lower_content for k in ["context", "alternatives", "justification"]):
                    raise SafeWriteError(f"Decision nodes like {fname} MUST contain 'context', 'alternatives', and 'justification'.")

            mutation = {"filename": fname, "content": fcontent}
            if "expected_version" in item:
                mutation["expected_version"] = item["expected_version"]
            mutations.append(mutation)
            written_paths.append(str(wiki_dir / fname))

        filepath = processed_data["filepath"]
        file_hash = processed_data["hash"]
        if mutations:
            from vector_lake.mutation_coordinator import execute_mutation_batch

            def mark_ingest_processed():
                mark_file_processed(filepath, file_hash)
                finalize_ingest_job(
                    str(job_id),
                    lease_owner,
                    lease_token,
                    lease_generation,
                    result_data={"integration": processed_data.get("integration")},
                )

            execute_mutation_batch(
                mutations,
                canonical_callback=mark_ingest_processed,
            )
        else:
            from vector_lake.db_store import transaction

            with transaction():
                mark_file_processed(filepath, file_hash)
                finalize_ingest_job(
                    str(job_id),
                    lease_owner,
                    lease_token,
                    lease_generation,
                    result_data={"integration": processed_data.get("integration")},
                )

        task_packet_path = job_row.get("task_packet_path") if job_row else None
        if task_packet_path:
            try:
                from vector_lake.native_llm import remove_subagent_task

                remove_subagent_task(task_packet_path)
            except Exception as exc:
                log.warning("Ingest finalized, but task packet cleanup failed: %s", exc)
        
        _clear_ingest_in_flight(filepath)
        
        proposal_count = 0
        if written_paths:
            try:
                # Include existing nodes sharing the newly observed tension target,
                # so independently ingested sources can converge on one proposal.
                candidate_records = list(node_records)
                target_names = {
                    str(edge.get("target", "")).strip()
                    for record in node_records
                    for edge in record.get("tension_edges", [])
                    if isinstance(edge, dict) and str(edge.get("target", "")).strip()
                }
                if target_names:
                    with open(get_index_path(), "r", encoding="utf-8") as handle:
                        index_nodes = json.load(handle).get("nodes", {}).values()
                    for node in index_nodes:
                        edges = node.get("tension_edges", [])
                        if any(isinstance(edge, dict) and edge.get("target") in target_names for edge in edges):
                            candidate_records.append({
                                "filename": node.get("id", ""),
                                "sources": node.get("sources", []),
                                "tension_edges": edges,
                            })
                for proposal in build_synthesis_proposals(candidate_records, contract):
                    governance_store.enqueue_governance_item(
                        proposal["type"], proposal["title"], proposal["description"],
                        ", ".join(proposal["sources"]), proposal["search_queries"], proposal["affected_pages"],
                    )
                    proposal_count += 1
            except Exception as exc:
                log.warning("Ingest completed, but Synthesis-Proposal evaluation failed: %s", exc)

        suffix = f" Queued {proposal_count} Synthesis-Proposal(s)." if proposal_count else ""
        return (
            f"Successfully finalized ingestion for {filepath}. "
            f"Integration disposition: {integration_disposition}.{suffix}"
        )
    except (ValueError, SafeWriteError, PurposeContractError, SchemaViolationException, DefenseHookException) as e:
        # Caller-actionable rejection: the payload is wrong and can be resubmitted.
        return f"Error finalizing ingestion: {e}"
    except Exception:
        # Infrastructure fault (database, outbox, projection, health gate).  These
        # used to be flattened into the same "Error finalizing ingestion" string,
        # so a broken store looked like a rejected payload.  Propagate instead.
        log.exception("finalize_ingest infrastructure failure")
        raise
