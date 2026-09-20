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
    {"match": r"rewe|edeka|lidl|aldi|kaufland|penny|netto|albert heijn|carrefour|mercadona",
     "merchant": "", "category": "Groceries"},
    {"match": r"\bdm\b|rossmann|müller|muller drogerie", "merchant": "", "category": "Shopping"},
    {"match": r"db vertrieb|deutsche bahn|\bbvg\b|\bhvv\b|\bmvg\b|trainline|flixbus|\bsncf\b",
     "merchant": "", "category": "Travel"},
    {"match": r"uber|bolt\.eu|free now|lyft|taxi", "merchant": "", "category": "Taxi & Ride Shares"},
    {"match": r"netflix|spotify|disney|youtube premium|apple\.com/bill|patreon",
     "merchant": "", "category": "Entertainment & Recreation"},
    {"match": r"amazon|amzn|zalando|ikea|mediamarkt|saturn", "merchant": "", "category": "Shopping"},
    {"match": r"vodafone|telekom|o2|congstar|1und1|1&1", "merchant": "", "category": "Phone"},
    {"match": r"github|openai|anthropic|adobe|figma|notion|linear\.app|vercel|hetzner|digitalocean",
     "merchant": "", "category": "Software & Tech"},
    {"match": r"\batm\b|geldautomat|cash withdrawal|bargeldauszahlung",
     "merchant": "ATM Withdrawal", "category": "Cash & ATM"},
    {"match": r"\bmiete\b|\brent\b|hausverwaltung", "merchant": "", "category": "Rent"},
    {"match": r"stadtwerke|vattenfall|eon|e\.on|strom|gasag", "merchant": "", "category": "Utilities"},
    {"match": r"krankenversicherung|tk\b|aok|barmer|health insurance",
     "merchant": "", "category": "Medical"},
    {"match": r"finanzamt|steuer|tax office", "merchant": "", "category": "Taxes"},
]


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
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
        if not match:
            continue
        try:
            pattern = re.compile(match, re.IGNORECASE)
        except re.error as exc:
            log.warning("Skipping rule with bad regex %r: %s", match, exc)
            continue
        rules.append(
            Rule(
                pattern=pattern,
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

    def apply(self, description: str, counterparty: str | None) -> tuple[str, str | None]:
        """Return (merchant_name, category_name_or_None).

        Matching runs against the raw text, before cleanup, because the noise
        we strip sometimes carries the only identifying substring.
        """
        haystack = " ".join(filter(None, [counterparty or "", description or ""]))
        merchant = clean_merchant(counterparty or description)

        for rule in self.rules:
            if rule.pattern.search(haystack):
                return (rule.merchant or merchant), rule.category
        return merchant, None
