"""
Multi-File Data Porcessing Pipeline for Soko Fresh Supermarket 
 
Usage:  python pipeline.py --input data/ --output output/
"""
import csv
import logging
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
 
import pandas as pd
 
logger = logging.getLogger(__name__)
 
REQUIRED_COLUMNS: set[str] = {
    "transaction_id", "date", "branch", "customer_id",
    "product", "quantity", "unit_price", "payment_method",
}
 
 
COLUMN_ALIASES: dict[str, str] = {"qty": "quantity"}
 
 
ID_FIELD = "transaction_id"
QTY_FIELD = "quantity"
PRICE_FIELD = "unit_price"
NUMERIC_FIELDS = {QTY_FIELD, PRICE_FIELD} 
 
 
CONSOLIDATED_COLUMNS = [
    "transaction_id", "date", "branch", "customer_id", "product", "quantity",
    "unit_price", "line_total", "payment_method", "payment_ref", "source_file",
]
 
 
REJECTED_LEADING_COLUMNS = ["source_file", "row_number", "transaction_id", "reasons"]
 
 
def discover_files(folder: Path, pattern: str = "transactions_*.csv") -> list[Path]:
    """Return the matching CSV files in a stable (date) order using pathlib."""
    # Return sorts by date
    return sorted(Path(folder).glob(pattern), key=lambda p: p.name)
 
 
def read_transactions(path: Path) -> tuple[list[str], list[dict]]:
    """Read one file and return (header, rows). Every row should remember its source file."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            row["_source_file"] = path.name
            row["_line"] = reader.line_num
            rows.append(row)
    return header, rows
 
# This function cleans up a header name (trims it, lower-cases it, turns spaces and hyphens into underscores) 
# It then applies COLUMN_ALIASES, which is how qty becomes quantity
def canonical_name(name: str) -> str:
    """Normalise a header name (case, spaces, hyphens) and map known aliases."""
    key = re.sub(r"[\s\-]+", "_", name.strip().lower())
    return COLUMN_ALIASES.get(key, key)
 
 
def validate_columns(header: list[str], required: set[str] = REQUIRED_COLUMNS) -> set[str]:
    """Return the set of required columns missing from the header (empty set means valid)."""
    return set(required) - set(header)
 
 
def find_problems(row: dict, seen_ids: set[str]) -> list[str]:
    """Return a list of human-readable reasons this row is invalid (empty list means valid).
 
    Must cover: blank required values, non-numeric or non-positive quantity/price,
    and transaction IDs already seen in this run.
    """
    problems = []
 
    if None in row: 
        problems.append("malformed row: more fields than the header")
 
    # Blank required values.
    blanks = [c for c in sorted(REQUIRED_COLUMNS) if not (row.get(c) or "").strip()]
    problems += [f"blank field: {c}" for c in blanks]
 
    # Quantity and unit price must be positive whole numbers.
    for field in sorted(NUMERIC_FIELDS):
        if field in blanks:
            continue  
        text = row[field].strip()
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            problems.append(f"invalid {field}: not a whole number ('{text}')")
        elif int(text) <= 0:
            problems.append(f"invalid {field}: must be positive (got {text})")
 
    if ID_FIELD not in blanks and row[ID_FIELD].strip() in seen_ids:
        problems.append(f"duplicate {ID_FIELD}: {row[ID_FIELD].strip()}")
 
    return problems
 
 
def clean_row(row: dict) -> dict:
    """Return a normalised copy of a valid row (types converted, whitespace stripped)."""
    stripped = {k: v.strip() for k, v in row.items()
                if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")}
 
    cleaned = {k: int(v) if k in NUMERIC_FIELDS else v for k, v in stripped.items()}
    cleaned["line_total"] = cleaned[QTY_FIELD] * cleaned[PRICE_FIELD]
    return cleaned
 
 
def write_csv(rows: list[dict], path: Path, fieldnames: list[str]) -> None:
    """Write rows to CSV, creating parent folders as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d row(s) to %s", len(rows), path)
 
 
def category_of(reason: str) -> str:
    """Mapping a 'category: detail' reason to the summary bucket it belongs to."""
    category = reason.split(":")[0]
    if category == "blank field":
        return "missing values"
    if category.startswith("invalid "):
        return "invalid numbers"
    if category.startswith("duplicate "):
        return "duplicate IDs"
    if category == "malformed row":
        return "malformed rows"
    return "file column issues"
 
 
def build_summary(stats: dict) -> str:
    """Format the processing summary shown in the expected-output resource."""
    issues = "; ".join(stats["column_issues"])
    issues_text = f"  ({issues})" if issues else ""
    by_cat = stats["rejected_by_category"]
 
    lines = [
        "=== Processing summary ===",
        f"Files discovered:        {stats['files_discovered']}",
        f"Files processed:         {stats['files_processed']}",
        f"Files with column issues:{len(stats['column_issues'])}{issues_text}",
        f"Rows read:               {stats['rows_read']}",
        f"Rows accepted:           {stats['accepted']}",
        f"Rows rejected:           {stats['rejected']}",
        f"  - missing values:      {by_cat.get('missing values', 0)}",
        f"  - invalid numbers:     {by_cat.get('invalid numbers', 0)}",
        f"  - duplicate IDs:       {by_cat.get('duplicate IDs', 0)}",
    ]
    # This is only shown when they happen
    for extra in ("malformed rows", "file column issues"):
        if by_cat.get(extra):
            lines.append(f"  - {extra + ':':<21}{by_cat[extra]}")
    lines += [
        f"Total revenue (KES):     {stats['total_revenue']:,}",
        f"Run finished at:         {stats['finished_at']}",
    ]
    return "\n".join(lines)
 
 
def process_file(path: Path, seen_ids: set[str], first_seen: dict[str, str]) -> dict:
    """Read, validate and check one file, and return what happened to its rows."""
    result = {"rows_read": 0, "processed": False, "issue": None,
              "accepted": [], "rejected": []}
 
    # A file that can't be read is logged and skipped
    try:
        header, rows = read_transactions(path)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.error("Could not read %s: %s", path.name, exc)
        result["issue"] = f"{path.name}: unreadable - {exc}"
        return result
    result["rows_read"] = len(rows)
    logger.info("Read %s: %d row(s)", path.name, len(rows))
 
    # Standardise the header names and check for missing columns
    header_map = {h: canonical_name(h) for h in header}
    remapped = {h: c for h, c in header_map.items() if h.strip() != c}
    missing = validate_columns(list(header_map.values()))
    mapped_rows = [{header_map.get(k, k): v for k, v in row.items()} for row in rows]
 
    # Header can't be fixed, so every row in the file is rejected
    if missing:
        note = f"missing {', '.join(sorted(missing))}"
        logger.error("%s rejected as a whole file: %s", path.name, note)
        result["issue"] = f"{path.name}: {note}"
        result["rejected"] = [(row, [f"file rejected: {note}"]) for row in mapped_rows]
        return result
 
    result["processed"] = True
    if remapped:
        changes = ", ".join(f"'{old}' mapped to '{new}'" for old, new in remapped.items())
        logger.warning("%s: unexpected header, %s", path.name, changes)
        result["issue"] = f"{path.name}: {changes}"
 
    for row in mapped_rows:
        reasons = find_problems(row, seen_ids)
        if reasons:
            # Say which copy of a duplicate was kept
            reasons = [f"{r} (first accepted in {first_seen[row[ID_FIELD].strip()]})"
                       if r.startswith("duplicate ") else r for r in reasons]
            result["rejected"].append((row, reasons))
        else:
            clean = clean_row(row)
            clean["source_file"] = row["_source_file"]
            # Only accepted IDs are remembered, so the first valid copy wins
            seen_ids.add(clean[ID_FIELD])
            first_seen[clean[ID_FIELD]] = f"{row['_source_file']} line {row['_line']}"
            result["accepted"].append(clean)
    logger.info("%s: %d accepted, %d rejected", path.name,
                len(result["accepted"]), len(result["rejected"]))
    return result
 
 
def build_reject_record(row: dict, reasons: list[str]) -> dict:
    """Turn a rejected row and its reasons into one record for the rejected CSV."""
    record = {"source_file": row["_source_file"], "row_number": row["_line"],
              "transaction_id": (row.get(ID_FIELD) or "").strip(),
              "reasons": "; ".join(reasons)}
    # Keep the original columns, without the _ keys
    for key, value in row.items():
        if isinstance(key, str) and not key.startswith("_") and key != ID_FIELD:
            record[key] = value or ""
    return record
 
 
def run_pipeline(input_dir: Path, output_dir: Path) -> dict:
    """Run every step and return the statistics used by build_summary()."""
    files = discover_files(input_dir)
    logger.info("Discovered %d file(s) in %s", len(files), input_dir)
    seen_ids: set[str] = set()       
    first_seen: dict[str, str] = {} 
    accepted, rejected, by_category = [], [], {}
    stats = {"files_discovered": len(files), "files_processed": 0,
             "column_issues": [], "rows_read": 0}
 
    for path in files:
        result = process_file(path, seen_ids, first_seen)
        stats["rows_read"] += result["rows_read"]
        stats["files_processed"] += result["processed"]
        if result["issue"]:
            stats["column_issues"].append(result["issue"])
        accepted += result["accepted"]
        for row, reasons in result["rejected"]:
            rejected.append(build_reject_record(row, reasons))
 
            for category in {category_of(r) for r in reasons}:
                by_category[category] = by_category.get(category, 0) + 1
 
    # The rejected CSV's columns:
    extra_columns = list(dict.fromkeys(
        key for record in rejected for key in record if key not in REJECTED_LEADING_COLUMNS))
    write_csv(accepted, Path(output_dir) / "consolidated_transactions.csv", CONSOLIDATED_COLUMNS)
    write_csv(rejected, Path(output_dir) / "rejected_transactions.csv",
              REJECTED_LEADING_COLUMNS + extra_columns)
 
    stats.update(rejected_by_category=by_category, accepted=len(accepted), rejected=len(rejected),
                 total_revenue=sum(r["line_total"] for r in accepted),
                 finished_at=datetime.now().isoformat(timespec="seconds"))
    return stats
 
 
if __name__ == "__main__":
    """ parse arguments, call run_pipeline, print the summary"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Soko Fresh multi-file sales pipeline")
    parser.add_argument("--input", type=Path, default=Path("data"), help="folder with transactions_*.csv")
    parser.add_argument("--output", type=Path, default=Path("output"), help="folder for results")
    args = parser.parse_args()
 
    if not args.input.is_dir():
        logger.error("Input folder '%s' not found.", args.input)
        sys.exit(1)
    if not discover_files(args.input):
        logger.error("No transactions_*.csv files in '%s'.", args.input)
        sys.exit(1)
 
    try:
        stats = run_pipeline(args.input, args.output)
        summary = build_summary(stats)
        (args.output / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    except OSError as exc:
        logger.error("Could not write output: %s", exc)
        sys.exit(1)
 
    print()
    print(summary)
    if stats["accepted"] + stats["rejected"] != stats["rows_read"]:
        logger.error("accepted + rejected does not equal rows read.")
        sys.exit(1)
