"""Replay the November 2026 EPS rollover reports from point-in-time inputs."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

from . import STRATEGY_VERSION
from .analysis import recommendations, require_complete_financials
from .cli import load_history
from .engine import MIN_HISTORY_SESSIONS
from .eps_archive import restore_archived_eps
from .eps_facts import extract_comparison, identify_filing
from .eps_improvement import SCHEMA, full_c5
from .official import SourceError, TAIPEI
from .report import render
from .storage import digest, read, write


BATCH = "eps-rollover-20261104"
DAYS = ("2026-11-02", "2026-11-03", "2026-11-04")
ARCHIVE_SHA = "761e840f3127a40245c59c29d10e9d857335d3699ea6da59e0e823a1d0f25e47"
EVIDENCE_HASH = "1683f5e064a426d02b12d6bd875740fbdc4198f9e8806aaabf77f7460631eeeb"
ARCHIVE_OBSERVED = "2026-08-31T21:14:19.231110+08:00"
ARCHIVE_URL = ("https://mopsov.twse.com.tw/server-java/FileDownLoad?step=9&functionName=show_file2"
               "&fileName=tifrs-2026Q2.zip&filePath=%2Fifrs%2F2026%2F")


def inputs(root, day):
    sessions, prices, institutions = load_history(root, through=day)
    if day not in sessions or len(sessions) < MIN_HISTORY_SESSIONS:
        raise SourceError(f"Missing archived market data for {day}")
    financial = read(root / "financials" / f"{day}.json.gz")
    if not financial or financial.get("observed_date") != day:
        raise SourceError(f"Missing same-day financial snapshot for {day}")
    return sessions, prices, institutions, financial


def extract_evidence(root, archive):
    """Extract only the required issuer comparisons from the pinned original archive."""
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVE_SHA:
            raise SourceError("Quarterly archive hash differs from the dated receipt")
    wanted = set()
    for day in DAYS:
        sessions, prices, institutions, financial = inputs(root, day)
        financial = restore_archived_eps(financial, root, day)
        for row in full_c5(day, sessions, prices, institutions, financial)["candidates"]:
            if row["fiscal_period"] != "2026-Q2":
                raise SourceError(f"No authorized archived filing for {row['symbol']} {row['fiscal_period']}")
            wanted.add(row["symbol"])
    evidence = {}
    with ZipFile(archive) as zipped:
        for member in zipped.namelist():
            parts = member.rsplit("-", 3)
            if len(parts) != 4 or parts[2] not in wanted or parts[3] != "2026Q2.html":
                continue
            scope = {"cr": "consolidated", "ir": "individual"}.get(parts[1])
            if scope is None:
                continue
            symbol = parts[2]
            raw = zipped.read(member)
            if identify_filing(raw) != {"symbol": symbol, "year": 2026, "quarter": 2,
                                        "statement_scope": scope}:
                raise SourceError(f"Archived filing identity mismatch for {symbol}")
            fact = extract_comparison(raw, symbol=symbol, year=2026, quarter=2, member_name=member)
            if fact["status"] != "KNOWN" or symbol in evidence:
                raise SourceError(f"Missing or ambiguous archived comparison for {symbol}")
            evidence[symbol] = {**fact, "member_name": member}
    if evidence.keys() != wanted:
        raise SourceError(f"Missing archived comparisons for {sorted(wanted - evidence.keys())}")
    return evidence


def prepare(root, reports, evidence):
    revision = root / "corrections" / BATCH
    output = reports / "corrections" / BATCH
    manifest_path = revision / "manifest.json"
    existing = read(manifest_path)
    if existing is not None:
        if digest(evidence) != existing["evidence_hash"]:
            raise SourceError("Correction already prepared with different evidence")
        for day in DAYS:
            entry = existing["reports"][day]
            if (digest((output / f"{day}.md").read_text(encoding="utf-8")) != entry["report_hash"]
                    or digest(read(revision / "recommendations" / f"{day}.json")) != entry["result_hash"]):
                raise SourceError(f"Prepared correction changed for {day}")
        return existing
    receipts = {}
    prepared = {}
    observed = datetime.fromisoformat(ARCHIVE_OBSERVED).astimezone(TAIPEI).date().isoformat()
    for day in DAYS:
        original = read(root / "recommendations" / f"{day}.json")
        delivery = read(root / "delivery" / f"{day}.json")
        original_text = (reports / f"{day}.md").read_text(encoding="utf-8")
        original_hash = digest(original_text)
        if (not original or original["eligible_count"] != 0 or original["strategy_version"] != STRATEGY_VERSION
                or not delivery or delivery["status"] != "SENT" or delivery["payload_hash"] != original_hash):
            raise SourceError(f"Original report or delivery did not match expected incident for {day}")
        sessions, prices, institutions, financial = inputs(root, day)
        original_financial = read(root / "financials" / f"{day}-{day}-eps.json.gz")
        if (not original_financial
                or original_financial.get("eps_input_hash") != digest({"financial": financial, "pool": []})
                or recommendations(day, sessions, prices, institutions,
                                   original_financial)["data_hash"] != original["data_hash"]):
            raise SourceError(f"Archived original screening inputs do not reproduce {day}")
        financial = restore_archived_eps(financial, root, day)
        pool = full_c5(day, sessions, prices, institutions, financial)["candidates"]
        wanted = {(row["market"], row["symbol"]): row for row in pool}
        for row in financial["fundamentals"]:
            symbol = row["symbol"]
            if (row["market"], symbol) not in wanted:
                continue
            fact = evidence.get(symbol, {})
            if (observed > day or row["fiscal_period"] != "2026-Q2"
                    or fact.get("status") != "KNOWN" or row["eps"] != fact.get("eps_ytd")):
                raise SourceError(f"Archived comparison does not match the target-day EPS for {day} {symbol}")
            row.update(eps_comparison_status="KNOWN", eps_prior_ytd=fact["eps_prior_ytd"],
                       eps_comparison=fact, eps_comparison_observed_date=observed)
        financial.update(eps_comparison_schema_version=SCHEMA, eps_target_date=day)
        result = recommendations(day, sessions, prices, institutions, financial)
        require_complete_financials(result)
        result.update(correction_batch=BATCH, supersedes_report_hash=original_hash,
                      retrospective=True, excluded_from_performance=True)
        result["signals"] = []
        text = "\n".join([
            "【正式更正版｜歷史重篩】",
            f"交易日：{day}",
            "修正項目：財報換季時漏讀已封存的 EPS。",
            "採用當日行情、法人與營收快照，以及交易日前已封存的財報。",
            "重篩結果不回填歷史績效。",
            "",
            render(day, result, [], STRATEGY_VERSION, include_performance=False),
        ])
        prepared[day] = (result, text)
        receipts[day] = {"report_hash": digest(text), "result_hash": digest(result),
                         "eligible": result["eligible_count"], "selected": len(result["candidates"]),
                         "c5": len(pool), "supersedes_report_hash": original_hash}
    output.mkdir(parents=True, exist_ok=True)
    for day, (result, text) in prepared.items():
        write(revision / "recommendations" / f"{day}.json", result)
        (output / f"{day}.md").write_text(text, encoding="utf-8")
    manifest = {"batch": BATCH, "reports": receipts, "evidence_hash": digest(evidence),
                "archive_sha256": ARCHIVE_SHA, "archive_observed_at": ARCHIVE_OBSERVED,
                "archive_url": ARCHIVE_URL, "eps_evidence": evidence}
    write(manifest_path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--extract-from", type=Path)
    args = parser.parse_args()
    if args.extract_from:
        evidence = extract_evidence(args.data_dir, args.extract_from)
        args.evidence.write_text(json.dumps(evidence, separators=(",", ":")), encoding="utf-8")
        return
    evidence = read(args.evidence)
    if not evidence or digest(evidence) != EVIDENCE_HASH:
        raise SourceError("Evidence does not match the verified incident archive extraction")
    manifest = prepare(args.data_dir, args.report_dir, evidence)
    print(json.dumps(manifest["reports"], ensure_ascii=False))


if __name__ == "__main__":
    main()
