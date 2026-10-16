"""Parses a Craft app ROADMAP.md into a structured dict.

The files are structured differently per app, so the parser does not look for
fixed line numbers, but maps table rows by their label
(e.g. "Full target (ready for real work)", "Mainstream practitioner", "Essentials user").
"""
import re
from datetime import date

PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
PCT_RANGE = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*[–-]\s*(\d+(?:\.\d+)?)\s*%\s*\)")
NUM_RANGE = re.compile(r"(\d[\d.,]*)\s*[–-]\s*(\d[\d.,]*)")
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SEP_CELL = re.compile(r"^:?-{2,}:?$")


MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def clean(text: str) -> str:
    """Strip Markdown emphasis, code marks and links, keep the link text."""
    text = MD_LINK.sub(r"\1", text)
    return re.sub(r"[*_`]", "", text).strip()


def to_int(s: str) -> int:
    return int(round(float(s.replace(",", ""))))


def split_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def iter_sections(lines):
    """Yields (lowercase title, lines) for each '## ' section."""
    title, buf = "", []
    for line in lines:
        if line.startswith("## "):
            yield title, buf
            title, buf = line[3:].strip().lower(), []
        else:
            buf.append(line)
    yield title, buf


def table_rows(lines):
    """All table rows without separator rows, as lists of cells."""
    rows = []
    for line in lines:
        if line.strip().startswith("|"):
            cells = split_row(line)
            if cells and not all(SEP_CELL.match(c) for c in cells if c):
                rows.append(cells)
    return rows


def classify_label(label: str):
    """Maps a table label to a metric, or None."""
    l = clean(label).lower()
    if "remaining" in l or "to beta" in l or "parity" in l:
        return None  # hour estimates, not percentages
    if "breadth" in l and "feature" in l:
        return "breadth"
    if "mainstream" in l:
        return "mainstream"
    if "essentials" in l:
        return "essentials"
    if "feature" in l or "dimension" in l:
        return None  # individual areas such as "Features: ready for real work"
    if "full" in l:
        return "full"
    if "ready for real work" in l or l.startswith("ready"):
        return "ready"
    return None


def first_pct(cells):
    for i, c in enumerate(cells):
        if PCT.search(clean(c)):
            return float(PCT.search(clean(c)).group(1)), i
    return None, None


def parse_roadmap(text: str) -> dict:
    lines = text.splitlines()
    head = "\n".join(lines[:40])

    stage = None
    m = re.search(r"Stage:\s*\**\s*([A-Za-z-]+(?:\s[A-Za-z-]+)?)", head)
    if m:
        stage = clean(m.group(1)).lower()
    nxt = re.search(r"next:\s*([a-z-]+)", head, re.I)
    next_stage = nxt.group(1).lower() if nxt else None

    dates = [date.fromisoformat(d) for d in re.findall(r"Last updated:\**\s*(\d{4}-\d{2}-\d{2})", head)]
    last_updated = max(dates).isoformat() if dates else None
    tgt = re.search(r"Target:\**\s*([^·\n]+)", head)
    target = clean(tgt.group(1)).rstrip(".") if tgt else None

    sections = list(iter_sections(lines))

    metrics = {"full": None, "mainstream": None, "essentials": None, "ready": None, "breadth": None}
    candidates = {k: [] for k in metrics}
    audiences = []
    for _title, body in sections:
        for cells in table_rows(body):
            if len(cells) < 2:
                continue
            kind = classify_label(cells[0])
            if not kind:
                continue
            pct, idx = first_pct(cells[1:])
            if pct is None:
                continue
            pct_cell = cells[1 + idx]
            rng = PCT_RANGE.search(pct_cell)
            hours = None
            for c in cells[2 + idx:]:
                hm = NUM_RANGE.search(clean(c))
                if hm:
                    hours = [to_int(hm.group(1)), to_int(hm.group(2))]
                    break
            entry = {
                "label": clean(cells[0]),
                "pct": pct,
                "range": [float(rng.group(1)), float(rng.group(2))] if rng else None,
                "hours": hours,
            }
            candidates[kind].append(entry)
            if kind in ("full", "mainstream", "essentials"):
                audiences.append({"audience": kind, **entry})
    for kind, cands in candidates.items():
        if cands:
            with_hours = [c for c in cands if c["hours"]]
            metrics[kind] = with_hours[0] if with_hours else cands[0]

    if metrics["full"] is None and metrics["ready"] is not None:
        metrics["full"] = metrics["ready"]

    dimensions, features, upcoming, progress = [], [], [], []
    for title, body in sections:
        if title.startswith("by dimension"):
            for cells in table_rows(body)[1:]:
                pct, _ = first_pct(cells[1:])
                if pct is not None:
                    dimensions.append({"label": clean(cells[0]), "pct": pct})
        elif title.startswith("features"):
            for cells in table_rows(body)[1:]:
                pct, _ = first_pct(cells[1:])
                if pct is not None:
                    features.append({"label": clean(cells[0]), "pct": pct})
        elif title.startswith("upcoming"):
            for cells in table_rows(body)[1:]:
                upcoming.append(" · ".join(clean(c) for c in cells if c))
            for line in body:
                nm = re.match(r"^\s*\d+\.\s+(.*)$", line)
                if nm:
                    upcoming.append(clean(nm.group(1)))
        elif title.startswith("progress log"):
            for line in body:
                s = line.strip()
                if s.startswith("|"):
                    cells = split_row(s)
                    dm = DATE.search(clean(cells[0])) if cells else None
                    if dm:
                        progress.append({"date": dm.group(1), "text": clean(" · ".join(c for c in cells[1:] if c))})
                elif re.match(r"^[-*]\s*\**\s*\d{4}-\d{2}-\d{2}", s):
                    mm = re.match(r"^[-*]\s*\**\s*(\d{4}-\d{2}-\d{2})(.*)$", s)
                    progress.append({"date": mm.group(1), "text": clean(mm.group(2)).lstrip(" :*")})
    progress = [p for p in progress if p["text"]]
    progress.sort(key=lambda p: p["date"], reverse=True)

    return {
        "stage": stage,
        "next_stage": next_stage,
        "target": target,
        "last_updated": last_updated,
        "metrics": metrics,
        "audiences": audiences,
        "dimensions": dimensions,
        "features": features,
        "upcoming": upcoming[:8],
        "progress": progress[:40],
    }
