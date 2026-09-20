"""Merchant cleanup and rule-based categorization.

PSD2 remittance strings are noisy - card numbers, terminal ids, dates and
reference codes are all jammed into free text. Left alone they produce a
Monarch merchant list with thousands of unique one-off entries, which makes
reports useless. This module normalizes them into stable merchant names and
applies user-editable rules to assign categories.

Rules live in a JSON file so they can be edited without touching code:

    [
      {"match": "rewe|edeka|lidl", "merchant": "Groceries", "category": "Groceries"},
      {"match": "^db vertrieb", "merchant": "Deutsche Bahn", "category": "Travel"}
    ]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Noise fragments that appear in card-network remittance text across EU banks.
NOISE_PATTERNS = [
    re.compile(r"\b\d{2}[./-]\d{2}[./-]\d{2,4}\b"),          # embedded dates
    re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"),                  # embedded times
    re.compile(r"\bxxxx[\s-]?\d{4}\b", re.IGNORECASE),        # masked card numbers
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),          # IBANs
    re.compile(r"\bmandate\s*(ref(erence)?)?[:\s]*\S+", re.IGNORECASE),
    re.compile(r"\bcreditor\s*id[:\s]*\S+", re.IGNORECASE),
    re.compile(r"\bend[- ]?to[- ]?end[:\s]*\S+", re.IGNORECASE),
    re.compile(r"\bref(erence)?[:.\s]*[A-Z0-9]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(card|karte)\s*\d+\b", re.IGNORECASE),
    re.compile(r"//\S+"),                                      # acquirer routing tails
]

WHITESPACE = re.compile(r"\s+")

DEFAULT_RULES: list[dict[str, str]] = [
    # -- Transfers first: these must never be mistaken for income or spending.
    # Money moved in from another account you also track would otherwise be
    # counted twice - once as income here, once as spending there - and inflate
    # both sides of every cash-flow report.
    # The source's own transaction type is checked first: Wise reports a
    # top-up as MONEY_ADDED regardless of how the description is worded, and
    # the wording does vary ("Topped up account" defeats a /top[- ]?up/ match).
    {"code": r"^MONEY_ADDED$", "merchant": "Account Top-up", "category": "Transfer"},
    {"match": r"thank you for adding funds|adding funds|topp?ed up|top[- ]?up(?! fee)|"
              r"from your .*account|einzahlung|\bumbuchung\b",
     "merchant": "Account Top-up", "category": "Transfer"},
    {"match": r"\bwise\b|transferwise|revolut|\bn26\b.*transfer",
     "category": "Transfer"},

    # -- Schooling and childcare
    {"match": r"\bschool\b|\bschule\b|kita\b|kindergarten|tuition|\bcr(è|e)che\b",
     "category": "Child Care"},

    # -- Fees: the bank's own ISO 20022 code is authoritative where present.
    {"code": r"FEES", "category": "Financial & Legal Services"},
    {"match": r"membership|kontof(ü|ue)hrung|account fee|\bfee\b|geb(ü|ue)hr",
     "category": "Financial & Legal Services"},

    # -- Cash
    {"match": r"\batm\b|geldautomat|cash withdrawal|bargeldauszahlung",
     "merchant": "ATM Withdrawal", "category": "Cash & ATM"},

    # -- Everyday German/EU retail
    {"match": r"rewe|edeka|lidl|aldi|kaufland|penny|netto|albert heijn|carrefour|mercadona",
     "category": "Groceries"},
    {"match": r"\bdm\b|rossmann|m(ü|ue)ller drogerie", "category": "Shopping"},
    {"match": r"db vertrieb|deutsche bahn|\bbvg\b|\bhvv\b|\bmvg\b|trainline|flixbus|\bsncf\b",
     "category": "Public Transit"},
    {"match": r"uber|bolt\.eu|free now|taxi", "category": "Taxi & Ride Shares"},
    {"match": r"netflix|spotify|disney|youtube premium|apple\.com/bill|patreon",
     "category": "Entertainment & Recreation"},
    {"match": r"amazon|amzn|zalando|ikea|mediamarkt|saturn", "category": "Shopping"},
    {"match": r"vodafone|telekom|\bo2\b|congstar|1und1|1&1", "category": "Phone"},
    {"match": r"github|openai|anthropic|adobe|figma|notion|linear\.app|vercel|"
              r"hetzner|digitalocean", "category": "Software"},

    # -- Housing and bills
    {"match": r"\bmiete\b|\brent\b|hausverwaltung", "category": "Rent"},
    {"match": r"schliessf(ä|ae)ch|schlie(ß|ss)fach|storage|lagerung|self.?storage",
     "category": "Home Improvement"},
    {"match": r"stadtwerke|vattenfall|\be\.?on\b|strom|gasag", "category": "Utilities"},
    {"match": r"krankenversicherung|\btk\b|\baok\b|barmer|health insurance",
     "category": "Medical"},
    {"match": r"versicherung|insurance", "category": "Insurance"},

    # -- Immigration / relocation paperwork, which is a real line item right now
    {"match": r"(ü|ue)bersetzung|translation|beglaubig|certified translation|"
              r"notar|notary|aus(lä|lae)nderbeh(ö|oe)rde|visa|apostille",
     "category": "Financial & Legal Services"},
    {"match": r"finanzamt|steuer|tax office", "category": "Taxes"},
]


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str] | None
    code: re.Pattern[str] | None
    merchant: str | None
    category: str | None


def clean_merchant(text: str) -> str:
    """Strip transaction noise and normalize casing."""
    cleaned = text or ""
    for pattern in NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = cleaned.replace("*", " ").replace("|", " ")
    cleaned = WHITESPACE.sub(" ", cleaned).strip(" -,.;:/")

    if not cleaned:
        return "Unknown"

    # Bank statement text is usually all-caps; title-case it unless it looks
    # like a deliberate acronym or brand (IKEA, DM, BVG).
    if cleaned.isupper() and len(cleaned) > 4:
        cleaned = cleaned.title()

    return cleaned[:120]


def load_rules(path: Path | None) -> list[Rule]:
    raw: list[dict[str, str]]
    if path and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(f"{path} must contain a JSON array of rule objects.")
    else:
        raw = DEFAULT_RULES

    rules: list[Rule] = []
    for entry in raw:
        match = entry.get("match")
        code = entry.get("code")
        if not match and not code:
            continue
        try:
            pattern = re.compile(match, re.IGNORECASE) if match else None
            code_pattern = re.compile(code, re.IGNORECASE) if code else None
        except re.error as exc:
            log.warning("Skipping rule with bad regex %r/%r: %s", match, code, exc)
            continue
        rules.append(
            Rule(
                pattern=pattern,
                code=code_pattern,
                merchant=(entry.get("merchant") or "").strip() or None,
                category=(entry.get("category") or "").strip() or None,
            )
        )
    return rules


def write_default_rules(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_RULES, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class Categorizer:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules

    def referenced_categories(self) -> set[str]:
        """Every category name the rules can assign."""
        return {rule.category for rule in self.rules if rule.category}

    def unknown_categories(self, available: set[str]) -> set[str]:
        """Rule categories that do not exist in Monarch.

        A rule naming a missing category still matches, but the assignment
        falls back to Uncategorized - so the rule looks like it works while
        quietly doing nothing. Surfacing the mismatch is the difference
        between a typo you fix in a minute and months of miscategorized data.
        """
        return {c for c in self.referenced_categories() if c not in available}

    def apply(
        self,
        description: str,
        counterparty: str | None,
        code: str | None = None,
    ) -> tuple[str, str | None]:
        """Return (merchant_name, category_name_or_None).

        Matching runs against the raw text, before cleanup, because the noise
        we strip sometimes carries the only identifying substring. `code` is
        the bank transaction code (ISO 20022, e.g. "PMNT/MDOP/FEES"), which is
        structured and far more reliable than text when the bank supplies it.
        """
        haystack = " ".join(filter(None, [counterparty or "", description or ""]))
        merchant = clean_merchant(counterparty or description)

        for rule in self.rules:
            if rule.code is not None:
                if not code or not rule.code.search(code):
                    continue
            if rule.pattern is not None and not rule.pattern.search(haystack):
                continue
            return (rule.merchant or merchant), rule.category
        return merchant, None


def transaction_code(raw: dict) -> str | None:
    """Flatten a source's structured transaction type into a matchable string.

    Berlin Group (Enable Banking) gives bank_transaction_code; Wise gives
    details.type. Both are far more reliable than the free-text description,
    which varies by bank, language and payment rail.
    """
    block = raw.get("bank_transaction_code") or {}
    parts = [block.get("description"), block.get("code"), block.get("sub_code")]
    joined = "/".join(str(p) for p in parts if p)
    if joined:
        return joined

    wise_type = (raw.get("details") or {}).get("type")
    return str(wise_type) if wise_type else None
