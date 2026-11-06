"""Duplicate reports: CSV (the editable plan), HTML (visual review) and console summary."""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
import urllib.parse
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .config import Config
from .duplicates import KEEP, REMOVE, REVIEW, SKIP, DuplicateResult, Group, Member
from .filetypes import DECODABLE_IMAGE_EXT
from .utils import human_size

DUP_CSV_FIELDS = [
    "group_id", "tier", "action", "path", "size_bytes", "size", "width", "height",
    "date", "distance", "in_selected_folders", "kept_because_or_note", "hash",
]
TIER_DESC = {
    "exact": "Byte-for-byte identical",
    "pixel": "Identical pixels, different file bytes (e.g. metadata differs)",
    "similar": "Visually similar (resized / recompressed) - review carefully",
}


def write_duplicates_csv(result: DuplicateResult, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DUP_CSV_FIELDS)
        w.writeheader()
        for g in result.groups:
            keeper = g.keeper
            for m in g.members:
                if m.action == KEEP:
                    note = "; ".join(m.reasons) or "best candidate by tie-breakers"
                elif m.action == SKIP:
                    note = f"outside the selected folders - not touched in this run (copy of {keeper.path})"
                else:
                    note = f"duplicate of {keeper.path}"
                w.writerow({
                    "group_id": g.id,
                    "tier": g.tier,
                    "action": m.action,
                    "path": m.path,
                    "size_bytes": m.row.size,
                    "size": human_size(m.row.size),
                    "width": m.row.width or "",
                    "height": m.row.height or "",
                    "date": m.row.meta_date or "",
                    "distance": "" if m.distance is None else m.distance,
                    "in_selected_folders": "yes" if m.in_scope else "no",
                    "kept_because_or_note": note,
                    "hash": (m.row.full_hash if g.tier == "exact" else m.row.pixel_hash if g.tier == "pixel" else m.row.dhash) or "",
                })


def summarize(result: DuplicateResult) -> dict:
    out = {}
    for tier in ("exact", "pixel", "similar"):
        gs = result.by_tier(tier)
        out[tier] = {
            "groups": len(gs),
            "remove_files": sum(1 for g in gs for m in g.members if m.action == REMOVE),
            "review_files": sum(1 for g in gs for m in g.members if m.action == REVIEW),
            "remove_bytes": sum(g.removable_bytes for g in gs),
        }
    return out


def print_summary(result: DuplicateResult) -> None:
    s = summarize(result)
    print(f"\nDuplicate summary  (selected: {result.scope.label()})")
    print("-" * 72)
    print(f"{'Tier':<10}{'Groups':>8}{'To remove':>12}{'To review':>12}{'Space freed':>16}")
    for tier, d in s.items():
        print(f"{tier:<10}{d['groups']:>8}{d['remove_files']:>12}{d['review_files']:>12}{human_size(d['remove_bytes']):>16}")
    print("-" * 72)
    tot_files = sum(d["remove_files"] for d in s.values())
    tot_bytes = sum(d["remove_bytes"] for d in s.values())
    print(f"Proposed for removal: {tot_files} files, {human_size(tot_bytes)}")
    bad = unreadable_paths(result)
    if bad:
        print(f"Unreadable/corrupted files (skipped for image comparison): {len(bad)}")
    folders = folder_summary(result)
    if folders:
        print(f"\nBy folder (files in the selected folders that have a copy elsewhere):")
        print(f"  {'Folder':<52}{'Files':>7}{'Remove':>8}{'Review':>8}{'Frees':>11}")
        top = sorted(folders.items(), key=lambda kv: (-kv[1]["remove_files"], kv[0]))
        for folder, d in top[:25]:
            name = folder if len(folder) <= 50 else "…" + folder[-49:]
            print(f"  {name:<52}{d['files_with_copies']:>7}{d['remove_files']:>8}{d['review_files']:>8}"
                  f"{human_size(d['remove_bytes']):>11}")
        if len(top) > 25:
            print(f"  ... {len(top) - 25} more folders in the JSON report")


# ---------------------------------------------------------------------------
# Per-file view: "does this file exist elsewhere, and where?"
# ---------------------------------------------------------------------------
MATCH_LABEL = {"exact": "identical file (same bytes)", "pixel": "identical picture (same pixels)",
               "similar": "looks similar"}
_ACTION_RANK = {REMOVE: 3, REVIEW: 2, KEEP: 1, SKIP: 0}


def _copy_info(o: Member, g: Group) -> dict:
    d = {
        "path": o.path,
        "match": MATCH_LABEL[g.tier] + (f" (distance {o.distance})" if g.tier == "similar" and o.distance is not None else ""),
        "action": o.action,
        "in_selected_folders": o.in_scope,
        "size": human_size(o.row.size),
        "group_id": g.id,
    }
    if o.row.width:
        d["resolution"] = f"{o.row.width}x{o.row.height}"
    if o.row.meta_date:
        d["date_taken"] = o.row.meta_date
    return d


def per_file_view(result: DuplicateResult) -> dict[str, dict]:
    """One entry per file inside the selected folders that has at least one other copy."""
    files: dict[str, dict] = {}
    for g in result.groups:
        keeper = g.keeper
        for m in g.members:
            if not m.in_scope:
                continue
            e = files.get(m.path)
            if e is None:
                e = files[m.path] = {
                    "file": PurePosixPath(m.path).name,
                    "path": m.path,
                    "action": m.action,
                    "size_bytes": m.row.size,
                    "size": human_size(m.row.size),
                    "resolution": f"{m.row.width}x{m.row.height}" if m.row.width else None,
                    "date_taken": m.row.meta_date,
                    "kept_copy": None,
                    "why": None,
                    "other_copies": [],
                    "_groups": [],
                }
            if _ACTION_RANK[m.action] > _ACTION_RANK[e["action"]]:
                e["action"] = m.action
            e["_groups"].append((g, m))
            seen = {c["path"] for c in e["other_copies"]}
            e["other_copies"] += [_copy_info(o, g) for o in g.members if o is not m and o.path not in seen]
    for e in files.values():
        g, m = max(e["_groups"], key=lambda gm: _ACTION_RANK[gm[1].action])
        if e["action"] == KEEP:
            e["kept_copy"] = e["path"]
            e["why"] = "this is the copy being kept" + (f": {'; '.join(m.reasons)}" if m.reasons else "")
        else:
            e["kept_copy"] = g.keeper.path
            e["why"] = {
                REMOVE: f"{MATCH_LABEL[g.tier]} of the kept copy - will be moved to quarantine",
                REVIEW: f"{MATCH_LABEL[g.tier]} to the kept copy - left alone unless you change it to REMOVE",
            }.get(e["action"], "")
        e["exists_elsewhere"] = bool(e["other_copies"])
        e["exists_outside_selected_folders"] = any(not c["in_selected_folders"] for c in e["other_copies"])
        e["group_ids"] = sorted({gg.id for gg, _ in e["_groups"]})
        del e["_groups"]
    return files


def folder_summary(result: DuplicateResult, view: dict[str, dict] | None = None) -> dict[str, dict]:
    view = view if view is not None else per_file_view(result)
    out: dict[str, dict] = defaultdict(lambda: {"files_with_copies": 0, "remove_files": 0, "review_files": 0,
                                                "keep_files": 0, "remove_bytes": 0})
    for e in view.values():
        d = out[str(PurePosixPath(e["path"]).parent)]
        d["files_with_copies"] += 1
        if e["action"] == REMOVE:
            d["remove_files"] += 1
            d["remove_bytes"] += e["size_bytes"]
        elif e["action"] == REVIEW:
            d["review_files"] += 1
        else:
            d["keep_files"] += 1
    return dict(out)


def write_duplicates_json(result: DuplicateResult, cfg: Config, path: Path, csv_path: Path) -> None:
    """Review file: for every file in the selected folders that also exists elsewhere,
    list where all the other copies are and what will happen to each."""
    view = per_file_view(result)
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(view):
        by_folder[str(PurePosixPath(p).parent)].append(view[p])
    fsum = folder_summary(result, view)
    for d in fsum.values():
        d["frees"] = human_size(d["remove_bytes"])
    tiers = summarize(result)
    doc = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "disk_root": str(cfg.root),
        "selected_folders": result.scope.as_list(),
        "how_to_read": {
            "action": {
                KEEP: "this copy stays where it is",
                REMOVE: "will be moved to the quarantine folder (not deleted) when you run dedupe --apply",
                REVIEW: "looks similar - left alone unless you change it to REMOVE in the CSV",
                SKIP: "a copy outside the selected folders - not touched in this run",
            },
            "other_copies": "every other place on the disk where the same (or a similar) file exists",
            "to_change_a_decision": f"edit the 'action' column in {csv_path} (this JSON is for reading only)",
        },
        "summary": {
            "files_with_copies": len(view),
            "to_remove": sum(1 for e in view.values() if e["action"] == REMOVE),
            "to_review": sum(1 for e in view.values() if e["action"] == REVIEW),
            "kept": sum(1 for e in view.values() if e["action"] == KEEP),
            "space_freed": human_size(sum(e["size_bytes"] for e in view.values() if e["action"] == REMOVE)),
            "by_tier": tiers,
            "by_folder": dict(sorted(fsum.items())),
        },
        "folders": dict(sorted(by_folder.items())),
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def unreadable_paths(result: DuplicateResult) -> list[str]:
    """Unique paths from read errors ('path: Error: message')."""
    seen: dict[str, str] = {}
    for e in result.read_errors:
        path, _, why = e.partition(": ")
        seen.setdefault(path, why)
    return [f"{p}\t{w}" for p, w in sorted(seen.items())]


def _file_url(p: Path) -> str:
    return "file://" + urllib.parse.quote(str(p))


def write_duplicates_html(result: DuplicateResult, cfg: Config, path: Path, csv_path: Path,
                          max_groups: int = 3000) -> None:
    s = summarize(result)
    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate Report</title>
<style>
:root {{ --bg:#fafafa; --fg:#1d1d1f; --muted:#6e6e73; --card:#fff; --line:#e3e3e8;
        --keep:#1f8a3b; --remove:#c62828; --review:#b26a00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161617; --fg:#f5f5f7; --muted:#a1a1a6;
        --card:#1f1f21; --line:#333336; --keep:#4cc26b; --remove:#ff6b6b; --review:#ffb340; }} }}
body {{ font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin:0;
       background:var(--bg); color:var(--fg); }}
main {{ max-width:1200px; margin:0 auto; padding:24px 16px 80px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.muted {{ color:var(--muted); }}
table.sum {{ border-collapse:collapse; margin:16px 0 24px; }}
table.sum td, table.sum th {{ padding:6px 14px; border-bottom:1px solid var(--line); text-align:right; }}
table.sum td:first-child, table.sum th:first-child {{ text-align:left; }}
.group {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin:12px 0; }}
.ghead {{ display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; margin-bottom:8px; }}
.ghead b {{ font-size:15px; }}
.members {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(190px, 1fr)); gap:10px; }}
.m {{ border:2px solid var(--line); border-radius:8px; padding:6px; overflow:hidden; }}
.m.KEEP {{ border-color:var(--keep); }} .m.REMOVE {{ border-color:var(--remove); opacity:.85; }}
.m.REVIEW {{ border-color:var(--review); }} .m.SKIP {{ border-style:dashed; opacity:.75; }}
.m img {{ width:100%; height:150px; object-fit:contain; background:#0001; border-radius:4px; }}
.tag {{ font-weight:600; font-size:12px; }}
.KEEP .tag {{ color:var(--keep); }} .REMOVE .tag {{ color:var(--remove); }} .REVIEW .tag {{ color:var(--review); }}
.path {{ word-break:break-all; font-size:12px; }}
.small {{ font-size:12px; color:var(--muted); }}
code {{ background:#0001; padding:1px 4px; border-radius:4px; }}
</style></head><body><main>
<h1>Duplicate Report</h1>
<div class="muted">Disk: {html.escape(str(cfg.root))}</div>
<div class="muted">Selected folders: {html.escape(result.scope.label())}</div>
<p>Green = kept, red = proposed for removal (moved to quarantine, not deleted), amber = needs your review,
dashed = a copy outside the selected folders (shown so you know where it is, not touched).
To change a decision, edit the <code>action</code> column in<br><code>{html.escape(str(csv_path))}</code><br>
then run <code>python -m diskcleaner dedupe --plan "{html.escape(str(csv_path))}"</code>.</p>
<table class="sum"><tr><th>Tier</th><th>Groups</th><th>Remove</th><th>Review</th><th>Space freed</th></tr>"""]
    for tier, d in s.items():
        parts.append(f"<tr><td>{tier} <span class='small'>{html.escape(TIER_DESC[tier])}</span></td>"
                     f"<td>{d['groups']}</td><td>{d['remove_files']}</td><td>{d['review_files']}</td>"
                     f"<td>{human_size(d['remove_bytes'])}</td></tr>")
    parts.append("</table>")
    if len(result.groups) > max_groups:
        parts.append(f"<p class='muted'>Showing the first {max_groups} of {len(result.groups)} groups; "
                     "the CSV contains all of them.</p>")
    for g in result.groups[:max_groups]:
        parts.append(f"<section class='group'><div class='ghead'><b>{g.id}</b><span>{g.tier}</span>"
                     f"<span class='muted'>{len(g.members)} files · frees {human_size(g.removable_bytes)}</span></div>"
                     "<div class='members'>")
        for m in g.members:
            full = cfg.root / m.path
            thumb = ""
            if m.row.ext in DECODABLE_IMAGE_EXT and m.row.ext not in (".heic", ".heif", ".tif", ".tiff"):
                thumb = f"<a href='{_file_url(full)}'><img loading='lazy' src='{_file_url(full)}' alt=''></a>"
            dims = f"{m.row.width}×{m.row.height} · " if m.row.width else ""
            dist = f" · distance {m.distance}" if m.distance is not None else ""
            note = "; ".join(m.reasons) if m.action == KEEP else (
                "outside selected folders" if m.action == SKIP else "")
            parts.append(
                f"<div class='m {m.action}'>{thumb}<div class='tag'>{m.action}</div>"
                f"<div class='path'><a href='{_file_url(full)}'>{html.escape(m.path)}</a></div>"
                f"<div class='small'>{dims}{human_size(m.row.size)}{dist}</div>"
                f"<div class='small'>{html.escape(note)}</div></div>"
            )
        parts.append("</div></section>")
    parts.append("</main></body></html>")
    path.write_text("".join(parts), encoding="utf-8")
