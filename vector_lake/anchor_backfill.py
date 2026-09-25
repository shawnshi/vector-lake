"""Draft, review and apply the anchor a multi-source block never wrote.

The 926 ``ambiguous_source`` claims sit on pages declaring two or more sources: the block has to say
*which* one it came from, and the extractor reads that as ``(Source: [[<the declared file>]])`` on
the block.  Nothing mechanical finds it -- measured: 0 of the 926 carry such an anchor, 843 of their
texts do not occur in any declared file (the compiler paraphrases), and 102 declared files are not
on disk -- so this drafts the attribution from *discriminating* evidence and hands it to a human:

* a candidate earns a proposal only when the block's terms that occur in **some but not all** of the
  declared sources cover >= 0.75 of that mass on it, it owns >= 2 terms no other candidate has, and
  it leads the runner-up by >= 0.20;
* the proposal carries the line of that source holding the most matched terms, so one look settles
  it;
* everything else abstains with its reason (``no_citable_basis`` / ``cannot_discriminate`` /
  ``no_readable_source`` / ``page_scaffolding``) and is named as such.  Topic similarity is never
  used: for a general statement it would attach false provenance.

Applying is safe by construction: ``claim_id`` is minted from the *cleaned* block text and
``_clean_claim_text`` strips ``(Source: ...)``, so the anchor adds an ``inline_sources`` entry to the
existing claim instead of re-minting it -- and because the extractor compares declared sources and
anchors through ``_source_key``, the anchor actually matches.
"""

from __future__ import annotations

import collections
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import db_store, governance_store, mutation_coordinator
from vector_lake.claim_extractor import _clean_claim_text
from vector_lake.wiki_utils import get_meta_dir, get_wiki_dir, normalize_raw_ref, read_markdown_file

log = logging.getLogger(__name__)

MIN_TERMS = 4
MIN_COVERAGE = 0.75
MIN_DISTINGUISHING = 2
MIN_MARGIN = 0.20

#: Sentences that describe the *page* rather than its subject.  No source is the thing they came
#: from -- the first run proposed a source for ``本页只记录证据中明确出现的定义、机制、适用范围或限制。``
#: on the strength of generic shared words, which is exactly the fabricated citation this rule must
#: not produce.  (The scope sentence is deliberately kept as a claim by the extractor; it is simply
#: not a claim about the subject, so it is not anchorable.)
PAGE_SELF_DESCRIPTION = (
    "本页只记录证据中明确出现的",
    "未使用冻结证据范围之外的材料",
    "该页面编译自",
    "内容仅概括原始文件中的",
    "provenance-only standalone",
)

#: Bookkeeping that is not a claim about anything.  These rows should not exist; the extractor's
#: ``_is_page_boilerplate`` now refuses to re-mint them, so they are named apart from the
#: self-description sentences above, which are claims the page is entitled to make.
NOT_A_CLAIM = (
    "[system directive:",
    "node auto-migrated to v11 schema",
)


def _is_scaffolding(text: str) -> str | None:
    """``page_scaffolding`` / ``not_a_claim`` when the block is not about the subject, else None.

    A ``Last Reshaped`` footer sits at the end of *real* claims (``ACE引擎… (Last Reshaped:
    2026-06-28)``), so matching that marker classified 107 substantive claims as scaffolding and
    kept them out of the draft.  Markers are only read here when they describe the page itself or
    are pure bookkeeping.
    """
    normalized = str(text or "").lower()
    if any(marker in normalized for marker in NOT_A_CLAIM):
        return "not_a_claim"
    if any(marker in text for marker in PAGE_SELF_DESCRIPTION):
        return "page_scaffolding"
    return None

_ANCHOR = re.compile(r"\(Source[s]?:\s*(.*?)\)", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _squeeze(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _terms(text: str) -> set[str]:
    compact = _squeeze(text)
    found: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", compact):
        for size in (2, 3):
            for index in range(len(run) - size + 1):
                found.add(run[index : index + size])
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._%/-]{2,}", compact):
        found.add(token.lower())
    return found


def drafts_path() -> Path:
    return get_meta_dir() / "anchor_drafts.jsonl"


def review_path() -> Path:
    return get_meta_dir() / "anchor_review.md"


def rollback_path() -> Path:
    directory = get_meta_dir() / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-anchor-backfill.rollback.jsonl"


def _gap_blocks() -> dict[str, list[dict]]:
    db_store.init_db()
    rows = db_store.get_connection().execute(
        """SELECT json_extract(data_json,'$.claim_id') AS claim_id,
                  json_extract(data_json,'$.locator.page_key') AS page_key,
                  json_extract(data_json,'$.claim_text') AS claim_text
           FROM claims WHERE json_extract(data_json,'$.evidence_gap')='ambiguous_source'"""
    ).fetchall()
    pages: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        if row["page_key"]:
            pages[str(row["page_key"])].append(
                {"claim_id": row["claim_id"], "page_key": str(row["page_key"]), "text": row["claim_text"] or ""}
            )
    return pages


def draft_anchors(write: bool = True) -> str:
    """Attribute what can be attributed with discriminating evidence; abstain explicitly otherwise."""
    root = get_wiki_dir().parent
    pages = _gap_blocks()
    summary: collections.Counter = collections.Counter()
    drafts: list[dict] = []

    for page_key, blocks in sorted(pages.items()):
        page_path = get_wiki_dir() / f"{page_key}.md"
        record_base = {"page_key": page_key, "declared_sources": [], "readable_sources": []}
        if not page_path.exists():
            summary["page missing"] += len(blocks)
            continue
        try:
            frontmatter, _body, _ = read_markdown_file(page_path)
        except Exception:
            summary["page unreadable"] += len(blocks)
            continue
        declared = frontmatter.get("sources") or []
        if isinstance(declared, str):
            declared = [declared]
        declared = [normalize_raw_ref(item) for item in declared]
        sources: dict[str, dict] = {}
        for ref in declared:
            path = root / ref
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="ignore")
                sources[ref] = {"lines": text.splitlines(), "compact": _squeeze(text)}
        record_base["declared_sources"] = declared
        record_base["readable_sources"] = sorted(sources)

        for block in blocks:
            record = {
                **record_base,
                "claim_id": block["claim_id"],
                "block_text": block["text"][:200],
                "verdict": "no_citable_basis",
                "chosen": None,
                "score": 0.0,
                "margin": 0.0,
                "distinguishing_terms": [],
                "citation": None,
                "reason": "",
            }
            classification = _is_scaffolding(block["text"])
            if classification:
                record["verdict"] = classification
            elif not sources:
                record["verdict"] = "no_readable_source"
            elif len(sources) < 2:
                record["verdict"] = "cannot_discriminate"
            else:
                record.update(_attribute(block["text"], sources))
            summary[record["verdict"]] += 1
            drafts.append(record)

    if write:
        drafts_path().write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in drafts) + "\n", encoding="utf-8"
        )
        review_path().write_text(_render_review(drafts), encoding="utf-8")

    proposed = [item for item in drafts if item["verdict"] == "proposed"]
    lines = [
        "=== Anchor Drafts ===",
        f"blocks with an ambiguous source: {len(drafts)} over {len(pages)} page(s)",
        f"proposed for review: {len(proposed)}",
    ]
    for name, count in summary.most_common():
        lines.append(f"  {name}: {count}")
    if write:
        lines.append(f"drafts: {drafts_path()}")
        lines.append(f"review: {review_path()}")
    return "\n".join(lines)


def _attribute(text: str, sources: dict[str, dict]) -> dict:
    """Score each candidate by the discriminator mass it covers, and require a clear winner."""
    block_terms = _terms(text)
    present = {
        ref: {term for term in block_terms if term in payload["compact"]}
        for ref, payload in sources.items()
    }
    document_frequency: collections.Counter = collections.Counter()
    for ref in sources:
        for term in present[ref]:
            document_frequency[term] += 1
    weight = {
        term: (3 if df == 1 else 2 if df == 2 else 0)
        for term, df in document_frequency.items()
        if df < len(sources)
    }
    total = sum(weight.values())
    if not total:
        return {"reason": "no_discriminating_terms"}
    scored = {
        ref: sum(weight.get(term, 0) for term in present[ref]) / total for ref in sources
    }
    ranked = sorted(scored.items(), key=lambda item: -item[1])
    winner, best = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    distinguishing = sorted(term for term in present[winner] if document_frequency[term] == 1)

    reasons = []
    if total < MIN_TERMS:
        reasons.append("too_few_discriminating_terms")
    if best < MIN_COVERAGE:
        reasons.append("no_dominant_source")
    if len(distinguishing) < MIN_DISTINGUISHING:
        reasons.append("only_one_distinguishing_term")
    if best - runner_up < MIN_MARGIN:
        reasons.append("sources_indistinguishable")
    if reasons:
        return {
            "score": round(best, 3),
            "margin": round(best - runner_up, 3),
            "distinguishing_terms": distinguishing[:8],
            "term_mass": total,
            "reason": ",".join(reasons),
        }

    hits = [term for term in present[winner] if weight.get(term)]
    lines = sources[winner]["lines"]
    best_line, best_hits = 0, 0
    for number, line in enumerate(lines):
        compact = _squeeze(line)
        count = sum(1 for term in hits if term in compact)
        if count > best_hits:
            best_line, best_hits = number, count
    return {
        "verdict": "proposed",
        "chosen": winner,
        "score": round(best, 3),
        "margin": round(best - runner_up, 3),
        "distinguishing_terms": distinguishing[:8],
        "term_mass": total,
        "citation": {
            "line": best_line + 1,
            "matched_terms": best_hits,
            "text": lines[best_line][:200] if lines else "",
        },
    }


def _render_review(drafts: list[dict], batch: int = 60) -> str:
    proposed = [item for item in drafts if item["verdict"] == "proposed"]
    abstained = [item for item in drafts if item["verdict"] != "proposed"]
    counts = collections.Counter(item["verdict"] for item in drafts)
    lines = [
        "# Ambiguous-source blocks: proposed anchors (for review)",
        "",
        "A proposal means: the block's discriminating terms cover >= 0.75 of that mass on the named",
        "source, it owns >= 2 terms no other candidate has, and it leads by >= 0.20. Each entry",
        "shows the line of the source carrying the most matched terms. Strike out the numbers you",
        "reject, then run `python cli.py anchor-backfill --only 1,2,5,9-14 --apply`.",
        "",
        f"| verdict | blocks |",
        "|---|---|",
        f"| proposed | {counts['proposed']} |",
        f"| page_scaffolding | {counts['page_scaffolding']} |",
        f"| cannot_discriminate (fewer than two readable declared sources) | {counts['cannot_discriminate']} |",
        f"| no_citable_basis (no dominant source) | {counts['no_citable_basis']} |",
        f"| no_readable_source (none of the declared files is on disk) | {counts['no_readable_source']} |",
        "",
    ]
    for start in range(0, len(proposed), batch):
        chunk = proposed[start : start + batch]
        lines.append(f"\n## Batch {start // batch + 1} ({start + 1}-{start + len(chunk)})\n")
        for number, item in enumerate(chunk, start=start + 1):
            citation = item.get("citation") or {}
            lines.append(f"### {number}. {item['page_key']}")
            lines.append(f"- block: `{item['block_text'].strip()[:170]}`")
            lines.append(
                f"- proposed source: `{item['chosen']}`  (coverage {item['score']:.2f} / lead {item['margin']:.2f})"
            )
            lines.append(f"- line {citation.get('line')} ({citation.get('matched_terms')} matched terms):")
            lines.append(f"  > {str(citation.get('text', '')).strip()[:220]}")
            lines.append(f"- distinguishing terms: {', '.join(item['distinguishing_terms'][:6])}")
            lines.append("")
    lines.append("\n## Abstentions, by page\n")
    per_page: dict[str, list[dict]] = collections.defaultdict(list)
    for item in abstained:
        per_page[item["page_key"]].append(item)
    for page_key, items in sorted(per_page.items()):
        reasons = collections.Counter(item["verdict"] for item in items)
        detail = ", ".join(f"{name}x{count}" for name, count in reasons.most_common())
        missing = [ref for ref in items[0]["declared_sources"] if ref not in items[0]["readable_sources"]]
        extra = f"; not on disk {len(missing)}/{len(items[0]['declared_sources'])}" if missing else ""
        lines.append(f"- `{page_key}`: {detail}{extra}")
    return "\n".join(lines) + "\n"


def parse_only(spec: str | None, proposed: list[dict]) -> list[dict]:
    """``"1,2,5,9-14"`` -> those proposals (1-based, in review order).  None -> all of them."""
    if not spec:
        return proposed
    wanted: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            wanted.update(range(int(low), int(high) + 1))
        else:
            wanted.add(int(part))
    return [item for index, item in enumerate(proposed, start=1) if index in wanted]


#: Markdown that wraps a block without being part of it: the list marker, the heading hashes.
_LIST_MARKER = re.compile(r"^\s*(?:#+\s*|[-*+]\s+|\d+\.\s+)+")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def _locate_line(lines: list[str], block_text: str) -> int | None:
    """The line holding the block, matched on the same cleaned text the claim was minted from.

    Only body lines are eligible: a block's text can collide with a frontmatter line (appending an
    anchor there corrupts the YAML, which the write gate refused with a mapping error) and a
    heading is not a block that can carry an anchor (the schema allows only the template's own
    headings, which refused the first attempt the same way).

    Two shapes made a single fixed needle ambiguous -- a list of same-day timeline entries
    (``- [2026-06-01] [Observation] `` on every line) and a page repeating its own name at the head
    of each bullet -- so the match starts at the block's first 24 characters and grows until
    exactly one line answers.  Ambiguity that survives the full text is reported, never guessed.
    """
    needle_full = _squeeze(_clean_claim_text(block_text, limit=10_000))
    if not needle_full:
        return None
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                body_start = index + 1
                break
    forms: dict[int, str] = {}
    for index in range(body_start, len(lines)):
        line = lines[index]
        if _HEADING.match(line):
            continue
        forms[index] = _squeeze(_LIST_MARKER.sub("", _clean_claim_text(line, limit=10_000)))
    for length in (24, 40, 60, 90, 140, 220, len(needle_full)):
        needle = needle_full[:length]
        if not needle:
            continue
        matches = [index for index, form in forms.items() if form.startswith(needle)]
        if len(matches) == 1:
            return matches[0]
    matches = [index for index, form in forms.items() if needle_full in form]
    return matches[0] if len(matches) == 1 else None


def _unparseable_fragment(line: str) -> bool:
    """Does the line already carry a ``(Source: ...)`` fragment the extractor cannot read?

    An anchor is read as ``(Source: [[...]])``.  A bare ``(Source: raw/x.md)`` is dead weight but
    harmless -- the extractor strips it, and stripping it leaves the same trailing space the claim
    id was minted with, so appending the readable anchor beside it keeps the claim.  A path that
    itself contains parentheses is different: ``_clean_claim_text`` matches non-greedily to the
    first ``)``, so the fragment is only partly removed, the cleaned text changes, the claim is
    re-minted, and the leftover junk still does not anchor anything.  Those lines need a human.
    """
    for match in _ANCHOR.finditer(line):
        content = match.group(1)
        if "(" in content or ")" in content:
            return True
    return False


def _paren_safe(path: str) -> bool:
    r"""Can this path be written as ``(Source: [[<path>]])`` at all?

    ``_clean_claim_text`` removes the fragment with ``\((Source[s]?:\s*(.*?)\)`` -- non-greedy to
    the first ``)`` -- and ``_parse_inline_sources`` reads only ``[[...]]`` inside it.  A path that
    itself contains parentheses is therefore cut in half: the anchor is not read, the residue
    (``.md]])``) lands in the stored claim text, and the claim is re-minted.  Measured on the first
    application of this rule: 4 pages / 16 blocks churned their claim ids this way.  Those lines
    need the anchor written in a bracket-safe form by a human, so they are skipped, not forced.
    """
    return "(" not in path and ")" not in path


def backfill_anchors(
    only: str | None = None,
    apply: bool = False,
    batch: int = 10,
) -> str:
    """Append ``(Source: [[<declared file>]])`` for the confirmed proposals, then re-extract."""
    path = drafts_path()
    if not path.exists():
        return f"No drafts at {path}; run `anchor-draft` first."
    drafts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    proposed = [item for item in drafts if item["verdict"] == "proposed"]
    selected = parse_only(only, proposed)

    edits: dict[str, list[dict]] = collections.defaultdict(list)
    unmatched: list[str] = []
    needs_repair: list[str] = []
    paren_paths: list[str] = []
    for item in selected:
        if not _paren_safe(item["chosen"]):
            paren_paths.append(f"{item['page_key']}: {item['chosen']}")
            continue
        page_path = get_wiki_dir() / f"{item['page_key']}.md"
        if not page_path.exists():
            unmatched.append(f"{item['page_key']}: page missing")
            continue
        text = page_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")
        index = _locate_line(lines, item["block_text"])
        if index is None:
            unmatched.append(f"{item['page_key']}: block not located uniquely")
            continue
        if f"[[{item['chosen']}]]" in lines[index]:
            continue  # already anchored
        if _unparseable_fragment(lines[index]):
            needs_repair.append(f"{item['page_key']}: line carries a (Source: ...) the parser truncates")
            continue
        edits[item["page_key"]].append({"claim_id": item["claim_id"], "line": index, "source": item["chosen"]})

    total = sum(len(value) for value in edits.values())
    lines = [
        "=== Anchor Backfill ===",
        f"proposals: {len(proposed)}  |  selected: {len(selected)}",
        f"anchors to write: {total} over {len(edits)} page(s)",
    ]
    if unmatched:
        lines.append(f"skipped, block not locatable: {len(unmatched)}")
        lines.extend(f"  {item}" for item in unmatched[:5])
    if needs_repair:
        lines.append(f"skipped, the line's existing (Source: ...) needs a human: {len(needs_repair)}")
        lines.extend(f"  {item}" for item in needs_repair[:5])
    if paren_paths:
        lines.append(
            f"skipped, the source path contains parentheses (the parser truncates it): {len(paren_paths)}"
        )
        lines.extend(f"  {item}" for item in paren_paths[:5])
    if not apply:
        lines.append("[DRY RUN] nothing written.")
        return "\n".join(lines)

    rollback = rollback_path()
    written = 0
    refused: list[str] = []
    with rollback.open("a", encoding="utf-8") as sink:
        pages = sorted(edits.items())
        for start in range(0, len(pages), batch):
            chunk = pages[start : start + batch]
            mutations = []
            for page_key, page_edits in chunk:
                page_path = get_wiki_dir() / f"{page_key}.md"
                before = page_path.read_text(encoding="utf-8", errors="ignore")
                after = before.split("\n")
                for edit in page_edits:
                    # No space before the anchor, deliberately: ``_clean_claim_text`` drops the
                    # ``(Source: ...)`` fragment *after* collapsing whitespace, so
                    # ``...职责 (Source: ...)`` cleans to ``...职责 `` -- a trailing space, which
                    # is a different claim text and therefore a different ``claim_id``.  Adding
                    # it flush leaves the cleaned text byte-identical, so the existing claim
                    # gains its ``inline_sources`` instead of being replaced: measured on the
                    # first canary, the spaced form re-minted three ids and left the timeline
                    # projection pointing at the old ones.
                    after[edit["line"]] = after[edit["line"]].rstrip() + f"(Source: [[{edit['source']}]])"
                after_text = "\n".join(after)
                sink.write(
                    json.dumps(
                        {
                            "page_key": page_key,
                            "claim_ids": [edit["claim_id"] for edit in page_edits],
                            "anchors": [edit["source"] for edit in page_edits],
                            "written_at": _now(),
                            "before": before,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                mutations.append(
                    {
                        "filename": f"{page_key}.md",
                        "content": after_text,
                        "expected_version": governance_store.canonical_page_version_from_content(
                            f"{page_key}.md", before
                        ),
                    }
                )
            sink.flush()  # recovery information lands before the write it guards
            try:
                mutation_coordinator.execute_mutation_batch(mutations)
                written += sum(len(page_edits) for _page, page_edits in chunk)
            except Exception as exc:  # noqa: BLE001 - one refusing page must not hold back the batch
                log.warning("Anchor batch refused (%s); retrying page by page", exc)
                for mutation in mutations:
                    try:
                        mutation_coordinator.execute_mutation_batch([mutation])
                        written += next(len(v) for k, v in edits.items() if f"{k}.md" == mutation["filename"])
                    except Exception as inner:  # noqa: BLE001
                        refused.append(f"{mutation['filename']}: {inner}")

    lines.append(f"wrote {written} anchor(s); rollback file: {rollback}")
    if refused:
        lines.append(f"refused by the write gate: {len(refused)}")
        lines.extend(f"  {item}" for item in refused[:10])
    lines.append(_verify(edits))
    lines.append(f"ambiguous_source claims still open: {_open_gap_count()}")
    return "\n".join(lines)


def _open_gap_count() -> int:
    return db_store.get_connection().execute(
        "SELECT COUNT(*) FROM claims WHERE json_extract(data_json,'$.evidence_gap')='ambiguous_source'"
    ).fetchone()[0]


def _verify(edits: dict[str, list[dict]]) -> str:
    """Every targeted claim must now carry evidence; the gap must not merely have moved."""
    ids = [edit["claim_id"] for page_edits in edits.values() for edit in page_edits]
    if not ids:
        return "nothing to verify."
    conn = db_store.get_connection()
    with_evidence = 0
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" * len(chunk))
        with_evidence += conn.execute(
            f"""SELECT COUNT(*) FROM claims WHERE claim_id IN ({placeholders})
                AND json_extract(data_json,'$.evidence_ids') <> '[]'""",
            chunk,
        ).fetchone()[0]
    remaining = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE json_extract(data_json,'$.evidence_gap')='ambiguous_source'"
    ).fetchone()[0]
    return (
        f"verified: {with_evidence}/{len(ids)} targeted claim(s) now carry evidence; "
        f"ambiguous_source claims remaining: {remaining}"
    )
