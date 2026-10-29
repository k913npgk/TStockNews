"""Conservative extraction of comparable basic EPS from MOPS iXBRL members."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import hashlib
import math
import os
import re
from collections import defaultdict
from typing import Any


_BASIC_EPS = "BasicEarningsLossPerShare"
_FILING_METADATA_NAMES = {"CompanyID", "Year", "Quarter", "ReportCategory"}


def _decode_body(body: bytes) -> str:
    """Decode UTF-8 first; allow CP950 only with an explicit early Big5 label."""
    try:
        return body.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        header = body[:1024].decode("ascii", errors="ignore")
        if not re.search(r"charset\s*=\s*[\"']?\s*(?:big5|cp950)(?:[\"';\s]|$)", header, re.IGNORECASE):
            raise
        return body.decode("cp950", errors="strict")


class _FilingMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = defaultdict(list)
        self._active: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if _local(tag.casefold()) != "nonnumeric":
            return
        attrs = {key.casefold(): value for key, value in attrs}
        name = (attrs.get("name") or "").split(":")[-1]
        if name in _FILING_METADATA_NAMES:
            self._active.append({"name": name, "text": []})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for fact in self._active:
            fact["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if _local(tag.casefold()) == "nonnumeric" and self._active:
            fact = self._active.pop()
            self.values[fact["name"]].append("".join(fact["text"]).strip())


def identify_filing(body: bytes) -> dict[str, Any] | None:
    """Read unique filing identity and report scope from exact iXBRL metadata facts."""
    try:
        parser = _FilingMetadataParser()
        parser.feed(_decode_body(body))
        parser.close()
    except (UnicodeDecodeError, ValueError):
        return None
    values: dict[str, str] = {}
    for name in _FILING_METADATA_NAMES:
        found = parser.values.get(name, [])
        unique = set(found)
        if not unique or "" in unique or len(unique) != 1:
            return None
        values[name] = next(iter(unique))
    if not re.fullmatch(r"\d+", values["Year"]) or not re.fullmatch(r"\d+", values["Quarter"]):
        return None
    category = values["ReportCategory"]
    if category not in ("Consolidated report", "Individual report"):
        return None
    return {"symbol": values["CompanyID"], "year": int(values["Year"]),
            "quarter": int(values["Quarter"]),
            "statement_scope": "consolidated" if category == "Consolidated report" else "individual"}
def _quarter_end(year: int, quarter: int) -> date:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1, 2, 3, or 4")
    return date(year, quarter * 3, (date(year, quarter * 3 + 1, 1) - date.resolution).day) if quarter < 4 else date(year, 12, 31)


def _scope_from_member(member_name: str, symbol: str, year: int, quarter: int) -> str | None:
    """Require the exact MOPS member suffix: -cr|ir-SYMBOL-YYYYQn.html."""
    base = os.path.basename(member_name.replace("\\", "/"))
    match = re.search(rf"-(cr|ir)-{re.escape(symbol)}-{year}Q{quarter}\.html$", base, re.IGNORECASE)
    if not match or len(re.findall(r'-(?:cr|ir)-',base,re.IGNORECASE))!=1:
        return None
    return "consolidated" if match.group(1).casefold() == "cr" else "individual"


def _local(tag: str) -> str:
    return tag.rsplit(":", 1)[-1].casefold()


class _IxbrlParser(HTMLParser):
    """Small HTML parser that retains facts, contexts and unit declarations."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.contexts: dict[str, dict[str, Any]] = {}
        self.conflicted_contexts: set[str] = set()
        self.units: dict[str, dict[str, Any]] = {}
        self.conflicted_units: set[str] = set()
        self.facts: list[dict[str, Any]] = []
        self._context: dict[str, Any] | None = None
        self._active_dimension: dict[str, Any] | None = None
        self._unit_id: str | None = None
        self._unit_target: str | None = None
        self._unit_stack: list[str] = []
        self._unit_current: dict[str, Any] | None = None
        self._fact: dict[str, Any] | None = None
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attrs = [(k.casefold(), v) for k, v in attrs]
        a = {k: v for k, v in attrs}
        loc = _local(tag)
        self._stack.append(tag)
        if loc == "context":
            self._active_dimension = None
            self._context = {"id": a.get("id"), "start": None, "end": None,
                             "instant": None, "dimensional": False, "dimensions": []}
        elif self._context is not None:
            if loc in ("explicitmember", "typedmember"):
                self._context["dimensional"] = True
                self._active_dimension = {"tag": loc, "dimension": a.get("dimension"), "text": []}
            elif loc == "startdate":
                self._context["_date_target"] = "start"
            elif loc == "enddate":
                self._context["_date_target"] = "end"
            elif loc == "instant":
                self._context["_date_target"] = "instant"
        if loc == "unit":
            self._unit_id = a.get("id")
            if self._unit_id:
                self._unit_current = {"numerator": [], "denominator": [], "other": [], "divide": False}
            self._unit_stack = [loc]
        elif self._unit_id is not None:
            self._unit_stack.append(loc)
            if loc == "divide":
                self._unit_current["divide"] = True
            elif loc == "unitnumerator":
                self._unit_target = "numerator"
            elif loc == "unitdenominator":
                self._unit_target = "denominator"
            elif loc == "measure" and self._unit_target is None:
                self._unit_target = "other"
        if loc in ("nonfraction", "fraction") and a.get("name", "").split(":")[-1] == _BASIC_EPS:
            self._fact = {"attrs": a, "text": []}
        if tag in ("ix:nonfraction", "ix:context", "ix:unit", "xbrli:context", "xbrli:unit") and tag.endswith(":"):
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._active_dimension is not None:
            self._active_dimension["text"].append(data.strip())
        if self._context is not None and self._context.get("_date_target"):
            key = self._context.pop("_date_target")
            try:
                self._context[key] = date.fromisoformat(data.strip()).isoformat()
            except ValueError:
                self._context[key] = None
        if self._unit_id is not None and self._unit_target and data.strip():
            self._unit_current[self._unit_target].append(data.strip())
        if self._fact is not None:
            self._fact["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        loc = _local(tag)
        if self._fact is not None and loc in ("nonfraction", "fraction"):
            self.facts.append(self._fact)
            self._fact = None
        if loc == "measure" and self._unit_target == "other":
            self._unit_target = None
        if loc == "unitnumerator":
            self._unit_target = None
        elif loc == "unitdenominator":
            self._unit_target = None
        if loc in ("explicitmember", "typedmember") and self._active_dimension is not None:
            self._active_dimension["text"] = "".join(self._active_dimension["text"])
            self._context["dimensions"].append(self._active_dimension)
            self._active_dimension = None
        if loc == "unit" and self._unit_id is not None:
            previous = self.units.get(self._unit_id)
            if previous is not None and previous != self._unit_current:
                self.conflicted_units.add(self._unit_id)
            elif previous is None:
                self.units[self._unit_id] = self._unit_current
            self._unit_id = None
            self._unit_target = None
            self._unit_stack = []
            self._unit_current = None
        if loc == "context" and self._context is not None:
            context = self._context
            context.pop("_date_target", None)
            context.pop("_dimension_active", None)
            if context.get("id"):
                context_id = context["id"]
                previous = self.contexts.get(context_id)
                if previous is not None and previous != context:
                    self.conflicted_contexts.add(context_id)
                elif previous is None:
                    self.contexts[context_id] = context
            self._context = None
        # HTML is often not XML-balanced; pop through the matching open tag.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i] == tag:
                del self._stack[i:]
                break


def _unit_is_twd_per_share(unit: dict[str, Any]) -> bool:
    numerator = [re.sub(r"\s+", "", m).casefold() for m in unit.get("numerator", []) if m]
    denominator = [re.sub(r"\s+", "", m).casefold() for m in unit.get("denominator", []) if m]
    # MOPS declares this unit as a divide whose numerator is TWD and denominator share.
    # Merely naming a unitRef TWD_PER_SHARE is not sufficient.
    return (unit.get("divide") is True and not unit.get("other") and numerator == ["iso4217:twd"]
            and denominator == ["xbrli:shares"])


def _decode_fact(fact: dict[str, Any], unit: dict[str, Any]) -> Decimal | None:
    attrs = fact["attrs"]
    if (attrs.get("xsi:nil") or attrs.get("nil") or "").strip().casefold() in ("true", "1"):
        return None
    if not _unit_is_twd_per_share(unit):
        return None
    sign = attrs.get("sign")
    if sign not in (None, "", "+", "-"):
        return None
    raw = "".join(fact["text"]).replace(",", "").strip()
    if not raw:
        return None
    parenthesized = len(raw) >= 2 and raw.startswith("(") and raw.endswith(")")
    if parenthesized:
        if sign:
            return None
        raw = raw[1:-1].strip()
    elif "(" in raw or ")" in raw:
        return None
    try:
        value = Decimal(raw)
        if "scale" in attrs:
            if not re.fullmatch(r"[+-]?\d{1,3}", attrs["scale"]):
                return None
            scale = int(attrs["scale"])
            value *= Decimal(10) ** scale
        if sign == "-" and value < 0:
            return None
        if sign == "+" and value < 0:
            return None
        if parenthesized:
            value = -value
        if sign == "-":
            value = -value
        if not value.is_finite():
            return None
        if not math.isfinite(float(value)):
            return None
        return value
    except (InvalidOperation, ValueError, OverflowError):
        return None


def extract_comparison(body: bytes, *, symbol: str, year: int, quarter: int, member_name: str) -> dict[str, Any]:
    """Extract current and prior-year YTD basic EPS from one cr/ir iXBRL member.

    Missing, malformed, dimensional, nil, invalid-unit, or conflicting facts remain
    unknown. A quarter's EPS is never derived by subtracting another quarter.
    """
    end = _quarter_end(year, quarter)
    prior_end = _quarter_end(year - 1, quarter)
    start = date(year, 1, 1)
    prior_start = date(year - 1, 1, 1)
    result: dict[str, Any] = {
        "eps_ytd": None, "eps_prior_ytd": None, "statement_scope": _scope_from_member(member_name, symbol, year, quarter),
        "period_end": end.isoformat(), "prior_period_end": prior_end.isoformat(),
        "period_start": start.isoformat(), "prior_period_start": prior_start.isoformat(),
        "status": "UNKNOWN", "facts": {"eps_ytd": None, "eps_prior_ytd": None},
    }
    digest = hashlib.sha256(body).hexdigest()
    if result["statement_scope"] is None:
        result["reason"] = "MEMBER_NAME_MISMATCH"
        return result
    try:
        parser = _IxbrlParser()
        parser.feed(_decode_body(body))
        parser.close()
    except (UnicodeDecodeError, ValueError):
        result["reason"] = "INVALID_DOCUMENT"
        return result

    periods = {
        "eps_ytd": (start.isoformat(), end.isoformat()),
        "eps_prior_ytd": (prior_start.isoformat(), prior_end.isoformat()),
    }
    candidate_values: dict[str, list[tuple[Decimal, dict[str, Any]]]] = {key: [] for key in periods}
    invalid_period_fact = {key: False for key in periods}
    for fact in parser.facts:
        attrs = fact["attrs"]
        context_ref = attrs.get("contextref")
        unit_ref = attrs.get("unitref")
        if context_ref in parser.conflicted_contexts:
            result["reason"] = "CONFLICTING_CONTEXT_DEFINITIONS"
            continue
        context = parser.contexts.get(context_ref or "")
        if not context or context.get("dimensional"):
            continue
        for key, (wanted_start, wanted_end) in periods.items():
            if context.get("start") != wanted_start or context.get("end") != wanted_end:
                continue
            if unit_ref in parser.conflicted_units:
                invalid_period_fact[key] = True
                continue
            unit = parser.units.get(unit_ref or "", {})
            value = _decode_fact(fact, unit)
            if value is None:
                invalid_period_fact[key] = True
                continue
            metadata = {"contextRef": context_ref, "unitRef": unit_ref, "source_member_sha256": digest}
            candidate_values[key].append((value, metadata))

    for key in periods:
        values = candidate_values[key]
        if values and not invalid_period_fact[key] and len({value for value, _ in values}) == 1:
            result[key] = float(values[0][0])
            result["facts"][key] = values[0][1]
    if result.get("reason") == "CONFLICTING_CONTEXT_DEFINITIONS":
        pass
    elif all(result[key] is not None for key in periods):
        result["status"] = "KNOWN"
        result["reason"] = None
    elif any(len({value for value, _ in candidate_values[key]}) > 1 for key in periods):
        result["reason"] = "CONFLICTING_FACTS"
    elif any(invalid_period_fact.values()):
        result["reason"] = "INVALID_FACT"
    else:
        result["reason"] = "FACT_NOT_FOUND"
    return result
