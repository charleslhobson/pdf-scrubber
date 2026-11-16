#!/usr/bin/env python3
"""
scrub_pdfs.py - redact personal information from tax PDFs (K-1s, 1040s, W-2s,
1099s, statements) entirely on this machine, so the results can be shared with
an AI assistant for tax analysis.

    python scrub_pdfs.py                  (guided menu)
    python scrub_pdfs.py ~/taxes -o ~/taxes_scrubbed --terms pii_terms.txt

Nothing here touches the network. Dollar amounts, form line numbers, dates and
state codes are kept; identifiers become stable placeholders such as [ID-1] or
[ENTITY-2], so the same person or partnership can still be followed from one
document to the next.

What gets removed
  * SSNs, EINs, ITINs, PTINs (dashed, bare 9-digit, masked, one-digit-per-box)
  * bank routing / account numbers, phone numbers, e-mail addresses, IP PINs
  * street addresses, PO boxes, city and ZIP (the state code is kept)
  * dates of birth, driver's licence numbers
  * the name / address / ID boxes on IRS forms, located from their labels
  * every word or phrase listed in the --terms file
  * anything found by the rules above, wherever else it appears in the batch
  * metadata, bookmarks, attachments, form-field data, comments, hidden text,
    and images on text pages (logos, signatures, 2-D barcodes)
  * the input file names (outputs are doc_001, doc_002, ...)

How it fails safe
  * scanned pages are OCR'd, blacked out on the image itself, and re-OCR'd
    until nothing more is found; without Tesseract they are withheld
  * every output is re-opened and re-checked before it is saved; a file that
    still trips a detector, or still contains a redacted value anywhere in its
    raw bytes, is not written at all
  * a .redacted.txt next to each PDF shows the text a reader can still extract

What it cannot promise
  Pattern matching cannot know a name it was never told. List yours in the
  terms file, and look over the output - especially scanned pages - before
  sharing it.

Requires Python 3.9+, `pip install pymupdf`, and the Tesseract program for
scanned pages.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:  # the guided menu offers to install it
    pymupdf = None

VERSION = "2.1"


def _quiet_pdf_library() -> None:
    if pymupdf is not None:
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)


_quiet_pdf_library()

# --------------------------------------------------------------------------
# Detectors: regexes run over the page text.  Words on one visual row are
# joined with a space when they sit close together and with a TAB when they
# are far apart, so a pattern written with a literal space can never glue two
# table columns together into something that looks like an SSN or a phone.
# --------------------------------------------------------------------------

STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    "MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|PR|RI|SC|SD|TN|TX|UT|VT|VA|"
    "VI|GU|WA|WV|WI|WY"
)
STATE_CODES = set(STATES.split("|"))
_SUFFIXES = (
    "Street St Avenue Ave Road Rd Boulevard Blvd Drive Dr Lane Ln Court Ct Way "
    "Place Pl Parkway Pkwy Circle Cir Terrace Ter Highway Hwy Trail Trl Square "
    "Sq Loop Turnpike Tpke Plaza Alley Crossing Xing Broadway"
).split()
SUFFIX = "|".join(sorted({s for w in _SUFFIXES for s in (w, w.upper())}, key=len, reverse=True))
UNITS = "Apt|APT|Suite|SUITE|Ste|STE|Unit|UNIT|Floor|FLOOR|Fl|FL|Room|ROOM|Rm|RM|Bldg|BLDG|#"


@dataclass
class Detector:
    kind: str
    rx: re.Pattern
    groups: tuple = (0,)
    kinds: tuple | None = None      # one kind per group, if they differ


def build_detectors(ocr: bool) -> list[Detector]:
    # On OCR text, allow the usual digit look-alikes inside dashed IDs.
    d = r"[0-9OoIl|SBZ]" if ocr else r"\d"
    m = r"[0-9Xx*#]" if not ocr else r"[0-9Xx*#OoIl|SBZ]"
    pre, post = r"(?<![\w.,$#/-])", r"(?![\w/-]|[.,]\d)"

    def D(kind, pattern, groups=(0,), flags=0, kinds=None):
        return Detector(kind, re.compile(pattern, flags), groups, kinds)

    return [
        # 123-45-6789, 123 45 6789, XXX-XX-6789, ***-**-6789
        D("SSN", rf"{pre}{m}{{3}}[- ]{m}{{2}}[- ]{d}{{4}}{post}"),
        D("EIN", rf"{pre}{d}{{2}}-{d}{{7}}{post}"),
        D("PTIN", rf"{pre}P{d}{{8}}{post}"),
        # label + value pairs (value may be one digit per box)
        D("ACCT", r"\b(?:routing|account|acct)\.?[ \t]*(?:numbers?|num|nos?|#)[.:#]*[ \t]*"
                  r"((?:[A-Z]{0,3}[0-9Xx*][0-9Xx*-]{2,24})|(?:[0-9Xx*][ \t]){3,24}[0-9Xx*])", (1,), re.I),
        D("ACCT", r"\b(?:routing|account|acct)\.?[ \t]*:?[ \t]*"
                  r"([A-Z]{0,3}[0-9Xx*][0-9Xx*-]{4,24})(?![\w-]|[.,]\d)", (1,), re.I),
        D("PHONE", r"\b(?:phone|telephone|tel|fax|cell|mobile)\b[^\n\d]{0,25}(\d{10})(?!\d)", (1,), re.I),
        D("DOB", r"\b(?:date of birth|birth ?date|d\.?o\.?b\.?|born(?! before| after| in\b))\b[^\n\d]{0,25}"
                 r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|[A-Z][a-z]+\.? \d{1,2},? \d{4})", (1,), re.I),
        D("PIN", r"\b(?:identity protection pin|ip ?pin)\b[^\n\d]{0,60}(\d{6}|\d(?:[ \t]\d){5})(?!\d)", (1,), re.I),
        D("LICENSE", r"\bdriver'?s?[ ]licen[sc]e\b[^\n]{0,30}?(?<![\w-])((?=[A-Z0-9-]*\d)[A-Z0-9-]{5,20})(?![\w-])",
          (1,), re.I),
        D("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        D("PHONE", rf"{pre}(?:\+?1[ .-])?(?:\(\d{{3}}\)[ ]?\d{{3}}[ .-]|\d{{3}}([.-])\d{{3}}\1)\d{{4}}{post}"),
        # bare nine digits: SSN / EIN / routing number / ZIP+4 without dashes
        D("TIN", rf"{pre}\d{{9}}{post}"),
        D("NUMBER", rf"{pre}\d{{10,17}}{post}"),
        *([D("ID", r"(?<!\S)(?!\d{1,2}/\d{1,2}/\d{2,4}(?!\S))(?=(?:\S*?\d){4})(?=\S*[';:|\]\[}{!])"
                   r"[\dOolIiTtSs';:|/\\\]\[}{!$.]{6,}(?!\S)")] if ocr else []),
        # nine single digits, one per comb box
        D("TIN", r"(?<![\w.,$-])(?<!\d[ \t])\d(?:[ \t]\d){8}(?![ \t]?[\w.,])"),
        D("ADDRESS", rf"{pre}\d{{1,6}}(?:-\d{{1,4}})?[A-Z]?[ ]+(?:(?:[NSEW]|NE|NW|SE|SW|North|South|East|West)\.?[ ]+)?"
                     rf"(?:[A-Z0-9][\w.'-]*[ ]+){{0,4}}?(?:{SUFFIX})\b\.?"
                     rf"(?:,?[ ]+(?:{UNITS})\.?[ ]*#?[\w-]+)?"),
        D("ADDRESS", r"\bP\.?[ ]?O\.?[ ]?Box[ ]+[\w-]+", (0,), re.I),
        D("ADDRESS", rf"(?<![\w-])(?:{UNITS})\.?[ ]*#?\d[\w-]*(?![\w-])"),
        # City, ST 12345  ->  city and ZIP go, the state stays
        D("CITYZIP", rf"(?<![\w-])([A-Z][A-Za-z.'-]*(?:[ ][A-Z][A-Za-z.'-]*){{0,2}}),?[ \t]+(?:{STATES})[ \t,]+"
                     rf"(\d{{5}}(?:-\d{{4}})?)(?![\w-]|[.,]\d)", (1, 2), 0, ("CITY", "ZIP")),
        # without a city in front, "NY 85000" is state wages, not an address; only ZIP+4 is unambiguous
        D("ZIP", rf"(?<![\w-])(?:{STATES})[ \t,]+(\d{{5}}-\d{{4}})(?![\w-]|[.,]\d)", (1,)),
        # free-text "Label: value" lines on cover letters and statements
        D("NAME", r"(?:^|[ \t])(?:names?|taxpayer|spouse|client|partner|shareholder|beneficiary|member|investor|"
                  r"owner|account holder|prepared for|prepared by|attention|attn)(?:'s)?(?: names?)?[ ]*:[ \t]*"
                  r"((?-i:[A-Z])[^\t\n]{1,59})", (1,), re.I | re.M),
        D("NAME", r"\bdear[ ]+((?-i:[A-Z])[^\n\t,:]{1,59})[,:]", (1,), re.I),
    ]


# --------------------------------------------------------------------------
# Form boxes: a label that announces a name/address box.  Everything in the
# box under the label (bounded by the form's ruled lines) is redacted.
#   depth    - most we will ever go below the label, in points
#   fallback - how far to go when the form has no ruled lines (scans)
#   learn    - "person": also redact each name word everywhere else
#              "phrase": also redact each full line everywhere else
# --------------------------------------------------------------------------

@dataclass
class Anchor:
    kind: str
    rx: re.Pattern
    depth: float
    fallback: float
    width: float
    learn: str = ""
    side: bool = False      # value is always on the label's own row, to its right
    rx_ocr: re.Pattern = None   # same label, tolerant of OCR dropping spaces and apostrophes


def _A(kind, pattern, depth, fallback, width, learn="", side=False):
    # loosen spaces and apostrophes everywhere except inside (fixed-width) lookbehinds
    parts = re.split(r"(\(\?<[!=](?:\\.|[^()\\])*\))", pattern)
    loose = "".join(x if n % 2 else x.replace("'", "'?").replace(" ", " ?") for n, x in enumerate(parts))
    return Anchor(kind, re.compile(pattern, re.I), depth, fallback, width, learn, side, re.compile(loose, re.I))


LINE, BLOCK = (24, 18), (84, 40)
W_BLOCK = 255      # name/address blocks live in a half-page column
# Wording that continues a name/address label ("..., city, state, and ZIP code").  Spelled out
# rather than wild-carded so that a value printed on the label's own line is never taken for label.
_TAIL = (r"(?:,? (?:and |or )?(?:street address|address|city or town|city|state or province|state|country|"
         r"ZIP code|ZIP|foreign postal code|telephone no\.?))*"
         r"(?: for partner entered in E\.?)?(?: \(?see instructions\.?\)?\.?)?")
_WHO_ENTITY = r"(?:partnership|corporation|employer|payer|lender|filer|trustee|issuer|transferor|creditor)"
_WHO_PERSON = (r"(?:partner|shareholder|beneficiary|fiduciary|recipient|borrower|student|participant|policyholder|"
               r"transferee|debtor)")
_WHO_ANY = (r"(?:partner|shareholder|beneficiary|recipient|payer|employee|employer|filer|borrower|lender|student|"
            r"partnership|corporation|estate|trust)")

ANCHORS = [
    # ---- Form 1040 header and every "Name(s) shown on return" strip
    _A("NAME", r"(?<!\(1\) )(?<![\w])(?:(?:your|spouse's) )?first name(?: and (?:mid\w+ )?initial)?", *LINE, 240,
       "person"),
    _A("NAME", r"(?<!\(2\) )(?<![\w])(?:(?:your|spouse's) )?last name\b", *LINE, 230, "person"),
    _A("NAME", r"name\(s\) shown on (?:(?:your )?(?:tax |income tax )?return|form [\w-]+(?: or [\w-]+)*|page \d)"
               r"(?:\. Do not enter name and social security number if shown on (?:other side|page \d)\.?)?",
       *LINE, 420, "auto"),
    _A("NAME", r"name of (?:proprietor|person with self-employment income|taxpayer|decedent)", *LINE, 420, "auto"),
    _A("NAME", r"employee's (?:first )?name(?: and initial)?", 60, 26, 250, "person"),
    _A("ADDRESS", r"home address(?: \(number and street\))?(?:\. If you have a P\.?O\.? box, see instructions\.?)?",
       *LINE, 390),
    _A("ADDRESS", r"(?<![\w])apt\.? no\.?(?![\w])", *LINE, 60),
    _A("ADDRESS", r"city, town,? or post office(?:\. If you have a foreign address, also complete spaces below\.?)?",
       *LINE, 300),
    _A("ADDRESS", r"foreign (?:country name|province/state/county|postal code)", *LINE, 150),
    _A("ADDRESS", r"street address(?: \(including apt\.? no\.?\))?", *LINE, 260),
    _A("ADDRESS", r"city or town, state or province(?:, country,? and ZIP or foreign postal code)?", *LINE, 260),
    _A("ADDRESS", r"address \(number,? street(?:,? and (?:room|apt\.?|suite)(?: or suite)? no\.?)?\)?", *LINE, 390),
    _A("ADDRESS", r"number,? street,? and (?:room|apt\.?) or suite no\.?", *LINE, 390),
    _A("ADDRESS", r"employee's address(?: and ZIP code)?", *BLOCK, W_BLOCK),
    _A("ADDRESS", r"(?<!and )ZIP code(?! for)", *LINE, 75),
    # ---- name-and-address blocks on K-1s, W-2s, 1099s, 1098s
    _A("ENTITY", _WHO_ENTITY + r"'s name" + _TAIL, *BLOCK, W_BLOCK, "phrase"),
    _A("ENTITY", r"estate's or trust's name", *BLOCK, W_BLOCK, "phrase"),
    _A("NAME", _WHO_PERSON + r"'s name" + _TAIL, *BLOCK, W_BLOCK, "auto"),
    _A("NAME", r"(?<!'s )name, (?:street )?address" + _TAIL, *BLOCK, W_BLOCK, "auto"),
    # ---- signature / preparer area
    _A("PREPARER", r"(?:print/type )?preparer's name", *LINE, 160, "phrase"),
    _A("PREPARER", r"preparer's signature", *LINE, 160),
    _A("PREPARER", r"firm's(?! address| EIN)(?: name)?", *LINE, 300, "phrase"),
    _A("PREPARER", r"firm's address", *LINE, 300),
    _A("PREPARER", r"designee's(?: name)?", *LINE, 180, "phrase"),
    _A("PHONE", r"(?<![\w])phone (?:no\.?|number)", *LINE, 110),
    # ---- identifier boxes: on text pages only digit-bearing words go; on scans the whole box is blacked out
    _A("ID", r"(?:your |spouse's )?social security number", *LINE, 125),
    _A("ID", r"identifying number", *LINE, 130),
    _A("ID", r"(?:employer|taxpayer|federal) identification number(?: \(EIN\))?", *LINE, 150),
    _A("ID", _WHO_ANY + r"(?:'s)? (?:SSN or TIN|SSN|TIN|EIN|social security (?:number|no\.?)|"
             r"(?:federal |taxpayer |employer )?identif\w+ (?:number|no\.?))", *LINE, 200),
    _A("ID", r"firm's EIN", *LINE, 100),
    _A("ID", r"(?<![\w])PTIN(?![\w])", *LINE, 80),
    _A("ID", r"protection PIN(?:, enter it here)?", *LINE, 120),
    _A("ID", r"number \(PIN\)", 0, 0, 115, "", True),
    _A("ID", r"routing number", 0, 0, 200, "", True),
    _A("ID", r"account number", 0, 0, 270, "", True),
    # ---- dependants: 2025 layout (one row per field) and the older table (one row per person)
    _A("NAME", r"\(1\) First name(?![ \t]+Last name)", 0, 0, 470, "person", True),
    _A("NAME", r"\(2\) Last name", 0, 0, 470, "person", True),
    _A("ID", r"\(3\) SSN", 0, 0, 470, "", True),
    _A("NAME", r"\(1\) First name[ \t]+Last name", 64, 52, 200, "person"),
    _A("ID", r"\(2\) Social security(?: number)?", 64, 52, 110),
]
ENTITY_KINDS = {"ENTITY", "PREPARER"}

# Labels that end a box on the same row without being boxes we redact.
BOUNDARIES = re.compile(
    r"\bEIN\b|\bSSN\b|relationship|"
    r"occupation|date\b|check if|filing status|ZIP code\b(?! for)", re.I)

# Words that mark a name as a business/trust rather than a person.
ENTITY_WORDS = set(
    "llc lp llp lllp inc corp corporation company co ltd plc trust estate fund funds partners partnership "
    "holdings capital associates group bank foundation ventures investments investment properties realty "
    "management advisors advisers services enterprises international global equity credit energy "
    "securities financial insurance association society church university college school".split())

# Never learned as a stand-alone name word (too common in tax text).
STOPWORDS = ENTITY_WORDS | set(
    "and the for not see none same various attached statement statements yes no single married joint "
    "jointly separately head household widow widower deceased minor child son daughter parent other "
    "name names first last middle initial your spouse spouses address city state zip code number "
    "street apt suite unit box post office town income loss gain gains tax taxes return returns form "
    "schedule part line total amount year interest dividend dividends ordinary qualified general limited "
    "partner member manager domestic foreign individual self employed applicable available "
    "ssn tin ein itin relationship dependent dependents lived with you and date signature title "
    "may will grant grants mark rose bill social security current mailing former maiden suffix "
    # everyday tax vocabulary: never a person or a place, even when Capitalised or next to a state code
    "wages salary salaries tips compensation federal local county country source sourced resident nonresident "
    "apportioned apportionment allocated allocation sales payroll property taxable net gross withholding withheld "
    "credit credits share shares balance beginning ending account contributions distributions distribution basis "
    "section box units class series type entity exempt passive active rental royalty royalties business trade "
    "expense expenses deduction deductions depreciation amortization description adjustment adjustments "
    "summary detail details page copy filed final amended revised estimated payment payments paid due refund "
    "owner owners holder investor investors taxpayer client beneficiary shareholder trustee fiduciary grantor "
    "managing general special profit profits percentage percent ownership liabilities liability recourse "
    "nonrecourse short long term current prior carryover carryforward subtotal totals composite "
    "unknown none various other all any each per see note notes information instructions".split())


# --------------------------------------------------------------------------
# Tokens and term lists
# --------------------------------------------------------------------------

class Tokens:
    """Maps each distinct sensitive value to a stable placeholder such as [ID-3]."""

    NUMERIC = {"SSN", "EIN", "TIN", "PTIN", "ACCT", "PHONE", "PIN", "ZIP", "NUMBER"}
    SHARED = {"SSN": "TIN", "EIN": "TIN"}  # same digits -> same token whatever the format

    def __init__(self):
        self.map: dict[tuple, str] = {}
        self.count: Counter = Counter()
        self.literals: set[str] = set()     # long identifiers seen in the document being scrubbed

    def get(self, kind: str, value: str) -> str:
        value = value.strip()
        if kind in self.NUMERIC:
            key = (self.SHARED.get(kind, kind), re.sub(r"[^0-9A-Za-z*]", "", value).lower())
        else:
            key = (kind, re.sub(r"[\W_]+", " ", value).strip().lower())
        if key not in self.map:
            label = "ID" if key[0] == "TIN" else key[0]
            self.count[label] += 1
            self.map[key] = f"[{label}-{self.count[label]}]"
        return self.map[key]

    def register(self, kind: str, value: str) -> None:
        """Note a long identifier that was removed, for the raw leak check on this document."""
        digits = re.sub(r"\D", "", value)
        if kind in self.NUMERIC and len(digits) >= 7:
            self.literals.update({value.strip().lower(), digits})

    def dump(self) -> dict:
        return {"map": [[k[0], k[1], v] for k, v in self.map.items()], "count": dict(self.count)}

    def load(self, data: dict) -> None:
        for kind, norm, token in (data or {}).get("map", []):
            self.map[(kind, norm)] = token
        self.count.update((data or {}).get("count", {}))


def phrase_regex(phrase: str) -> re.Pattern | None:
    parts = re.findall(r"[^\W_]+", phrase)
    if not parts:
        return None
    body = r"[\W_]{0,3}".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.I)


def is_vocabulary(text: str) -> bool:
    """True when every word is ordinary form/tax wording ("Foreign", "Total", "Limited Partner")."""
    words = re.findall(r"[^\W\d_]{2,}", text)
    return bool(words) and all(w.lower() in STOPWORDS for w in words)


def looks_like_name(text: str) -> bool:
    """Names are Capitalised or ALL CAPS; stray label text ("do not enter name and") is not."""
    words = re.findall(r"[^\W\d_]{2,}", text)
    return bool(words) and sum(w[0].isupper() for w in words) * 2 > len(words)


class Terms:
    """Words and phrases removed everywhere: the user's own list plus what pass 1 learned."""

    def __init__(self, tokens: Tokens):
        self.tokens = tokens
        self.items: list[tuple] = []        # (pattern, token, key)
        self.frozen = False                 # set after pass 1 so every page is judged against the same list
        self.seen: set[str] = set()
        self.learned: list[dict] = []       # {"phrase", "kind", "token", "key", "count"}
        self.literals: set[str] = set()     # for the raw leak check
        self.keep: list[tuple] = []         # (pattern, key): phrases the user chose to leave visible
        self.keep_keys: set[str] = set()
        self._by_key: dict[str, dict] = {}
        self._literal: dict[str, str] = {}

    @staticmethod
    def key_of(phrase: str) -> str:
        return re.sub(r"[\W_]+", " ", phrase).strip().lower()

    def add(self, phrase: str, kind: str, split_words: bool, token: str | None = None, learned=False) -> None:
        phrase = " ".join(phrase.split())
        key = self.key_of(phrase)
        if len(key) < 2 or key in self.keep_keys:
            return
        token = token or self.tokens.get(kind, phrase)
        if key in self._by_key and learned:
            self._by_key[key]["count"] += 1
        if key not in self.seen:
            rx = phrase_regex(phrase)
            if rx:
                self.seen.add(key)
                self.items.append((rx, token, key))
                if len(key) >= 5:
                    self.literals.add(phrase.lower())
                    self._literal[key] = phrase.lower()
                if learned:
                    record = {"phrase": phrase, "kind": kind, "token": token, "key": key, "count": 1}
                    self.learned.append(record)
                    self._by_key[key] = record
        if split_words:
            for w in re.findall(r"[^\W\d_]{3,}", phrase):
                if w.lower() not in STOPWORDS:
                    self.add(w, kind, False, token, learned)

    def keep_visible(self, phrase: str) -> None:
        """The user wants this left in the documents: stop removing it, wherever it was found."""
        key = self.key_of(phrase)
        rx = phrase_regex(phrase)
        if not key or not rx or key in self.keep_keys:
            return
        self.keep_keys.add(key)
        self.keep.append((rx, key))
        self.items = [it for it in self.items if it[2] != key]
        self.learned = [r for r in self.learned if r["key"] != key]
        self._by_key.pop(key, None)
        self.seen.discard(key)
        self.literals.discard(self._literal.pop(key, ""))

    def remove_again(self, phrase: str, kind: str, token: str | None = None) -> None:
        """Undo keep_visible: the person changed their mind and wants this removed after all."""
        key = self.key_of(phrase)
        if key not in self.keep_keys:
            return
        self.keep_keys.discard(key)
        self.keep = [k for k in self.keep if k[1] != key]
        self.add(phrase, kind, False, token, learned=True)

    def load(self, path: Path) -> int:
        n = 0
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            kind, phrase = "PII", line
            mm = re.match(r"^([A-Za-z]{2,12}):\s*(.+)$", line)
            if mm:
                kind, phrase = mm.group(1).upper(), mm.group(2)
            self.add(phrase, kind, split_words=(kind == "NAME"))
            n += 1
        return n

    def dump(self) -> dict:
        return {"learned": [{k: r[k] for k in ("phrase", "kind", "token", "count")} for r in self.learned],
                "keep": [rx_key[1] for rx_key in self.keep]}

    def restore(self, data: dict) -> None:
        """Bring back what earlier runs into the same results folder learned."""
        for key in (data or {}).get("keep", []):
            self.keep_visible(key)
        for r in (data or {}).get("learned", []):
            self.add(r["phrase"], r["kind"], False, r.get("token"), learned=True)
            if self.key_of(r["phrase"]) in self._by_key:
                self._by_key[self.key_of(r["phrase"])]["count"] = r.get("count", 1)

    def learn_value(self, kind: str, value: str, token: str) -> None:
        """Remember something a detector found (an account number, an address, an e-mail address) so the
        same value is also removed where it appears without its label or context."""
        if self.frozen:
            return
        value = value.strip()
        digits = len(re.sub(r"\D", "", value))
        if kind in Tokens.NUMERIC or kind == "ID":
            if digits < 7:                      # short numbers collide with dollar amounts
                return
        elif kind == "CITY" or len(re.sub(r"[\W_]", "", value)) < 5 or is_vocabulary(value):
            return                              # a lone town name is too easily an ordinary word: use --terms
        elif kind == "NAME" and (len(re.findall(r"[^\W\d_]{2,}", value)) < 2 or not looks_like_name(value)):
            return                              # "Label: Word" is not reliably a name; two or more words are
        self.add(value, kind, False, token, learned=True)

    def learn(self, anchor: Anchor, lines: list[str], token: str) -> None:
        """Remember what a name/entity box contained so it is caught elsewhere."""
        if not anchor.learn or self.frozen:
            return
        for i, line in enumerate(lines):
            letters = re.findall(r"[^\W\d_]{2,}", line)
            if not letters or len(line) > 70 or re.match(r"\(?\w{1,2}\)", line):
                continue
            if re.search(r"\d", line) and i > 0:      # address lines: detectors handle those
                continue
            low = [w.lower() for w in letters]
            if all(w in STOPWORDS for w in low) or not looks_like_name(line):
                continue
            is_entity = any(w in ENTITY_WORDS for w in low)
            person = anchor.learn == "person" or (anchor.learn == "auto" and not is_entity and len(letters) <= 6)
            if len(letters) >= 2 or len(letters[0]) >= 4 or person:
                self.add(line, anchor.kind, split_words=person and i == 0, token=token, learned=True)
            if i == 0 and anchor.learn in ("phrase", "auto") and is_entity:
                # "Acme Partners LP" is also written "Acme Partners" elsewhere
                core = re.sub(r"[\s,.]+(?:l\.?l\.?c|l\.?p|l\.?l\.?p|inc|corp|co|ltd)\.?$", "", line, flags=re.I)
                if core != line and len(re.findall(r"[^\W\d_]{2,}", core)) >= 2:
                    self.add(core, anchor.kind, False, token, learned=True)


# --------------------------------------------------------------------------
# Page text model
# --------------------------------------------------------------------------

_NORMALISE = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"', " ": " ",
})


@dataclass
class PageText:
    words: list            # (Rect, text)
    row_of: list           # word index -> row number
    pos_of: list           # word index -> position within its row
    text: str              # rows joined by \n, words by space or TAB
    cmap: list             # char offset -> word index, or -1


def read_words(page) -> PageText:
    raw = page.get_text("words")
    return build_page_text([(pymupdf.Rect(w[:4]), w[4]) for w in raw])


def build_page_text(items) -> PageText:
    words = [(r, t.translate(_NORMALISE)) for r, t in items if t.strip()]
    order = sorted(range(len(words)), key=lambda i: ((words[i][0].y0 + words[i][0].y1) / 2, words[i][0].x0))
    rows: list[list[int]] = []
    ref = None
    for i in order:
        r = words[i][0]
        if ref is not None and min(r.y1, ref.y1) - max(r.y0, ref.y0) >= 0.5 * min(r.height, ref.height):
            rows[-1].append(i)
        else:
            rows.append([i])
            ref = r
    row_of, pos_of = [0] * len(words), [0] * len(words)
    parts, cmap = [], []
    for rn, row in enumerate(rows):
        row.sort(key=lambda i: words[i][0].x0)
        prev = None
        for pn, i in enumerate(row):
            r, t = words[i]
            row_of[i], pos_of[i] = rn, pn
            if prev is not None:
                gap = r.x0 - prev.x1
                parts.append(" " if gap <= 1.0 * max(r.height, prev.height) else "\t")
                cmap.append(-1)
            parts.append(t)
            cmap.extend([i] * len(t))
            prev = r
        parts.append("\n")
        cmap.append(-1)
    return PageText(words, row_of, pos_of, "".join(parts), cmap)


PLACEHOLDER = re.compile(r"\[[A-Z]{2,12}(?:-\d+)?\]")


def words_in_span(pt: PageText, start: int, end: int) -> list[int]:
    """Words touched by a match, ignoring placeholders we wrote ourselves."""
    return sorted({pt.cmap[k] for k in range(start, end)
                   if pt.cmap[k] >= 0 and not PLACEHOLDER.search(pt.words[pt.cmap[k]][1])})


@dataclass
class Hit:
    kind: str
    token: str
    words: list
    value: str = ""
    learn: bool = True      # False for matches of the names list itself


ID_LABEL = re.compile(r"^\W*(account|acct|routing|pin|ssn|ein|tin|itin|ptin|security|identification|identifying|"
                      r"identity|protection|licen[sc]e|phone|zip)\b", re.I)
STRONG_LABEL = re.compile(r"^\W*(account|acct|routing|pin)\b", re.I)


def find_boxed_digits(pt: PageText, tokens: Tokens) -> list[Hit]:
    """Digits sitting next to or under an ID label, even when the label wraps
    over several lines: one-digit-per-box runs (routing, account, PIN), and
    plain 5-17 digit strings beside a routing/account/PIN label."""
    rows: dict[int, list[int]] = {}
    for i in range(len(pt.words)):
        rows.setdefault(pt.row_of[i], []).append(i)
    runs = []                       # (word indexes, is_boxed)
    for members in rows.values():
        members.sort(key=lambda i: pt.pos_of[i])
        cur: list[int] = []
        for i in members + [None]:
            t = pt.words[i][1] if i is not None else ""
            if i is not None and re.fullmatch(r"[0-9Xx*]", t):
                cur.append(i)
                continue
            if len(cur) >= 4:
                runs.append((cur, True))
            cur = []
            if i is not None and re.fullmatch(r"[0-9Xx*]{5,17}", t) and re.search(r"\d", t):
                runs.append(([i], False))
    hits = []
    for idx, boxed in runs:
        R = union([pt.words[i][0] for i in idx])
        h = pt.words[idx[0]][0].height
        rx = ID_LABEL if boxed else STRONG_LABEL
        reach = 260 if boxed else 150
        best = None
        for (W, t) in pt.words:
            mt = rx.match(t)
            if not mt:
                continue
            beside = W.x1 <= R.x0 + 5 and R.x0 - W.x1 <= reach and W.y1 >= R.y0 - 1.5 * h and W.y0 <= R.y1 + 1.5 * h
            above = W.y1 <= R.y0 + 2 and R.y0 - W.y1 <= 30 and W.x0 <= R.x1 and W.x1 >= R.x0 - (60 if boxed else 0)
            if beside or above:
                dist = abs(R.x0 - W.x1) + abs(R.y0 - W.y0)
                if best is None or dist < best[0]:
                    best = (dist, mt.group(1).lower())
        if best:
            word = best[1]
            kind = "ACCT" if word in ("account", "acct", "routing") else "PIN" if word in ("pin", "protection", "identity") \
                else "PHONE" if word == "phone" else "ZIP" if word == "zip" else "TIN"
            value = "".join(pt.words[i][1] for i in idx)
            hits.append(Hit(kind, tokens.get(kind, value), idx, value))
    return hits


def cusip_valid(code: str) -> bool:
    """CUSIP check digit (securities identifiers are nine characters, often all digits)."""
    if not re.fullmatch(r"[0-9A-Z*@#]{8}\d", code):
        return False
    total = 0
    for pos, ch in enumerate(code[:8], 1):
        v = int(ch) if ch.isdigit() else {"*": 36, "@": 37, "#": 38}.get(ch, ord(ch) - 55)
        if pos % 2 == 0:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(code[8])


def is_cusip(pt: PageText, i: int, labels: list) -> bool:
    """A nine-digit number beside or under a 'CUSIP' heading is a security, not a taxpayer ID."""
    r, text = pt.words[i]
    if not labels or not cusip_valid(text.upper()):
        return False
    cx = (r.x0 + r.x1) / 2
    for L in labels:
        beside = L.x1 <= r.x0 + 5 and r.x0 - L.x1 <= 220 and L.y1 > r.y0 - r.height and L.y0 < r.y1 + r.height
        under = L.y1 <= r.y0 + 2 and L.x0 - 30 <= cx <= L.x1 + 30
        if beside or under:
            return True
    return False


def find_hits(pt: PageText, detectors: list[Detector], terms: Terms, tokens: Tokens) -> list[Hit]:
    hits = []
    cusips = [r for r, t in pt.words if re.match(r"\W*cusip\b", t, re.I)]
    for det in detectors:
        for mt in det.rx.finditer(pt.text):
            if det.kinds == ("CITY", "ZIP") and is_vocabulary(mt.group(1)):
                continue                        # "Total IN 12345": a state column, not an address
            if det.kind == "NAME" and (is_vocabulary(mt.group(det.groups[0]))
                                       or not looks_like_name(mt.group(det.groups[0]))):
                continue                        # "Partner: Foreign", "Spouse: Was born before January 2, 1961"
            for n, g in enumerate(det.groups):
                s, e = mt.span(g)
                if s < 0 or s == e:
                    continue
                idx = words_in_span(pt, s, e)
                kind = det.kinds[n] if det.kinds else det.kind
                if kind == "TIN" and len(idx) == 1 and is_cusip(pt, idx[0], cusips):
                    continue
                if idx:
                    hits.append(Hit(kind, tokens.get(kind, mt.group(g)), idx, mt.group(g)))
    hits += find_boxed_digits(pt, tokens)
    for rx, token, _ in terms.items:
        for mt in rx.finditer(pt.text):
            idx = words_in_span(pt, *mt.span())
            if idx:
                hits.append(Hit(token.strip("[]").rsplit("-", 1)[0], token, idx, mt.group(0), learn=False))
    return hits


# --------------------------------------------------------------------------
# Form-box detection
# --------------------------------------------------------------------------

def ruled_lines(page):
    """Horizontal and vertical rules drawn on the page (form box borders)."""
    H, V = [], []
    try:
        drawings = page.get_drawings()
    except Exception:
        return H, V

    def add_rect(r):
        r = pymupdf.Rect(r).normalize()
        if r.height < 2.5 and r.width > 15:
            H.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
        elif r.width < 2.5 and r.height > 6:
            V.append((r.y0, r.y1, (r.x0 + r.x1) / 2))
        elif r.width > 15 and r.height > 6:
            H.extend([(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)])
            V.extend([(r.y0, r.y1, r.x0), (r.y0, r.y1, r.x1)])

    for d in drawings:
        for it in d.get("items", []):
            if it[0] == "l":
                p, q = it[1], it[2]
                if abs(p.y - q.y) < 1 and abs(p.x - q.x) > 15:
                    H.append((min(p.x, q.x), max(p.x, q.x), (p.y + q.y) / 2))
                elif abs(p.x - q.x) < 1 and abs(p.y - q.y) > 6:
                    V.append((min(p.y, q.y), max(p.y, q.y), (p.x + q.x) / 2))
            elif it[0] == "re":
                add_rect(it[1])
            elif it[0] == "qu":
                add_rect(it[1].rect)
    return H, V


def raster_rules(pix, dpi: int):
    """Find long straight dark runs in a scanned page: the form's box borders."""
    gray = pix if pix.n == 1 else pymupdf.Pixmap(pymupdf.csGRAY, pix)
    w, h = gray.width, gray.height
    bw = bytes(gray.samples).translate(bytes(1 if v < 110 else 0 for v in range(256)))
    k = 72.0 / dpi
    H, V = set(), set()
    hrun = re.compile(b"\x01{%d,}" % int(40 / k))        # 40 pt or longer
    vrun = re.compile(b"\x01{%d,}" % int(14 / k))        # taller than a comb-box divider
    for y in range(0, h, 2):
        for mt in hrun.finditer(bw, y * w, (y + 1) * w):
            H.add((round((mt.start() - y * w) * k), round((mt.end() - y * w) * k), round(y * k)))
    for x in range(0, w, 2):
        col = bw[x::w]
        for mt in vrun.finditer(col):
            V.add((round(mt.start() * k), round(mt.end() * k), round(x * k)))
    return list(H), list(V)


def union(rects):
    out = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out


def find_boxes(page_rect, pt: PageText, tokens: Tokens, terms: Terms, keep_entities: bool,
               rules, learn_only=False, regions: list | None = None, ocr=False) -> list[Hit]:
    """Redact the contents of labelled name/address/ID boxes.

    `rules` are the page's ruled lines (H, V).  If `regions` is a list, whole
    box areas are appended to it as (rect, token) for pixel blackout - used on
    scanned pages, where OCR cannot be trusted to have read the box contents."""
    labels = []          # (anchor or None, word indexes, rect)
    for a in ANCHORS:
        for mt in (a.rx_ocr if ocr else a.rx).finditer(pt.text):
            idx = words_in_span(pt, *mt.span())
            if idx:
                # a wrapped label: keep only the words on its first row for geometry
                labels.append((a, idx, union([pt.words[i][0] for i in idx if pt.row_of[i] == pt.row_of[idx[0]]])))
    if not labels:
        return []
    for mt in BOUNDARIES.finditer(pt.text):
        idx = words_in_span(pt, *mt.span())
        if idx:
            labels.append((None, idx, union([pt.words[i][0] for i in idx])))
    protected = {i for _, idx, _ in labels for i in idx}
    H, V = rules
    hits = []
    for a, idx, L in labels:
        if a is None or (keep_entities and a.kind in ENTITY_KINDS):
            continue
        probe = L.x0 + 4
        below = sorted(y for (x0, x1, y) in H if y >= L.y1 + 0.5 and x0 <= probe + 2 and x1 >= probe + 10)
        if below and below[0] < L.y1 + 4:
            # a rule hugging the label: either the label sits beside its value in a one-row box
            # (keep that rule) or it is a column header with the value in the next row (skip it)
            beside = any(i not in protected and 0 <= r.x0 - L.x1 < 60 and r.y0 < L.y1 and r.y1 > L.y0
                         for i, (r, _) in enumerate(pt.words))
            if not beside:
                below = [y for y in below if y >= L.y1 + 4]
        ruled = bool(below)
        bottom = min(below[0], L.y1 + a.depth) if below else L.y1 + a.fallback
        if not ruled and any(i not in protected and r.x0 >= L.x1 and r.y0 < L.y1 and r.y1 > L.y0
                             for i, (r, _) in enumerate(pt.words)):
            bottom = L.y1 + 0.6 * L.height
        mid = (L.y1 + bottom) / 2
        lefts = [x for (y0, y1, x) in V if L.x0 - 45 <= x <= L.x0 + 1 and y0 <= mid <= y1]
        left = max(lefts) if lefts else L.x0 - 20
        rights = sorted(x for (y0, y1, x) in V if x >= L.x1 - 2 and y0 <= mid <= y1)
        if len(rights) > 1 and rights[0] - L.x1 <= 14:
            rights = [x for x in rights if x > rights[0] + 5] or rights     # skip it, take the far edge
        right = min(rights[0] if rights else 1e9, left + a.width + 20, page_rect.x1)
        for _, _, other in labels:
            if other.x0 > L.x1 + 2 and other.y0 < L.y1 and other.y1 > L.y0:      # same row, to the right
                right = min(right, other.x0 - 2)
            elif other.y0 > L.y1 + 2 and abs(other.x0 - L.x0) < 60:              # next label underneath
                bottom = min(bottom, other.y0 - 1)
        if a.side:
            bottom = L.y1 + 0.6 * L.height
            right = min(left + a.width + 20, page_rect.x1)
        zone = pymupdf.Rect(left, L.y0 - 0.6 * L.height, right, bottom)
        if regions is not None and not learn_only:
            # everything under the label, and everything to the right of the label's own text
            end = L.x1
            for j in sorted((j for j in range(len(pt.words)) if pt.row_of[j] == pt.row_of[idx[0]]
                             and pt.words[j][0].x0 >= L.x1 - 1), key=lambda j: pt.pos_of[j]):
                if a.side or pt.words[j][0].x0 - end > 0.9 * L.height or j in protected:
                    break
                end = pt.words[j][0].x1
            under = pymupdf.Rect(zone.x0, L.y1 + 0.5, zone.x1, zone.y1)
            right_of = pymupdf.Rect(end + 2, zone.y0, zone.x1, zone.y1)
            for _, _, other in labels:            # never run over the next label to the right
                if other.x0 > end + 2 and other.y0 < zone.y1 and other.y1 > zone.y0:
                    right_of.x1 = min(right_of.x1, other.x0 - 2)
            for box in ((right_of,) if a.side else (under, right_of)):
                if box.width >= 6 and (a.side or box.height >= 4):
                    regions.append((box, f"[{a.kind}]"))
        inside = []
        for i, (r, t) in enumerate(pt.words):
            if i in protected or not re.search(r"[^\W_]", t) or PLACEHOLDER.search(t):
                continue
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            if (r.y0 < L.y1 + 1.2 * L.height and r.y1 > L.y1 and r.x0 < L.x1 and r.x1 > L.x0
                    and re.fullmatch(r"[a-z.,()]+", t)):
                continue
            if a.kind == "ID" and not re.search(r"[0-9Xx*#]{2}|^[0-9Xx*]$", t):
                continue                          # ID boxes: leave label text alone
            if t.strip(",.") in STATE_CODES and cy > L.y1:
                continue                          # "Portland, OR 97201": the state is kept
            if zone.x0 <= cx <= zone.x1 and zone.y0 <= cy <= zone.y1 and (cy > L.y1 - 1 or r.x0 >= L.x1):
                inside.append(i)
        if not inside:
            continue
        inside.sort(key=lambda i: (pt.row_of[i], pt.pos_of[i]))
        lines, cur, last_row = [], [], None
        for i in inside:
            if last_row is not None and pt.row_of[i] != last_row:
                lines.append(" ".join(cur))
                cur = []
            cur.append(pt.words[i][1])
            last_row = pt.row_of[i]
        lines.append(" ".join(cur))
        token = tokens.get(a.kind, lines[0])
        if ruled:
            terms.learn(a, lines, token)
        if not learn_only:
            hits.append(Hit(a.kind, token, inside))
    return hits


# --------------------------------------------------------------------------
# Applying redactions
# --------------------------------------------------------------------------

def hit_rects(pt: PageText, hits: list[Hit]) -> list[tuple]:
    """Turn hits into (rect, token) boxes, merging neighbouring words."""
    taken: dict[int, Hit] = {}
    for h in hits:
        for i in h.words:
            taken.setdefault(i, h)
    out = []
    ordered = sorted(taken, key=lambda i: (pt.row_of[i], pt.pos_of[i]))
    run: list[int] = []
    for i in ordered:
        if run and (taken[i].token == taken[run[-1]].token and pt.row_of[i] == pt.row_of[run[-1]]
                    and pt.pos_of[i] == pt.pos_of[run[-1]] + 1):
            run.append(i)
            continue
        if run:
            out.append((union([pt.words[k][0] for k in run]), taken[run[0]].token))
        run = [i]
    if run:
        out.append((union([pt.words[k][0] for k in run]), taken[run[0]].token))
    return out


def redact_text_page(page, boxes: list[tuple], labels: bool, manual: bool = False) -> None:
    """Remove what lies under each box. `manual` boxes are hand-drawn: used exactly as drawn, and
    line art they touch (a signature, a logo) is removed as well as text."""
    for rect, token in boxes:
        r = pymupdf.Rect(rect)
        if not manual:
            if r.height > 5:                   # stay off the lines above and below
                trim = r.height * 0.12
                r.y0 += trim
                r.y1 -= trim
            r.x0 -= 0.5
            r.x1 += 0.5
        size = max(3.5, min(7.0, r.height * 0.8, 1.9 * r.width / max(len(token), 1)))
        page.add_redact_annot(r, text=token if labels else None, fontname="helv", fontsize=size,
                              align=pymupdf.TEXT_ALIGN_CENTER, fill=(0, 0, 0), text_color=(1, 1, 1),
                              cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                          graphics=(pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED if manual
                                    else pymupdf.PDF_REDACT_LINE_ART_NONE),
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


class OCRUnavailable(Exception):
    pass


class Cancelled(Exception):
    """The person pressed Cancel."""


def ocr_words(pix, dpi: int, psm: int, exe: str) -> list:
    """OCR a rendered page with Tesseract; returns (Rect in points, text) per word.
    The image goes in over stdin and nothing is written to disk."""
    cmd = [exe, "stdin", "stdout", "--dpi", str(dpi), "--psm", str(psm)]
    tessdata = Path(exe).resolve().parent / "tessdata"
    if tessdata.is_dir():                       # a copy bundled with the app, or a Windows install
        cmd += ["--tessdata-dir", str(tessdata)]
    extra = {"creationflags": 0x08000000} if os.name == "nt" else {}    # no console window flashing up
    proc = subprocess.run(cmd + ["tsv"], input=pix.tobytes("png"), capture_output=True, timeout=600, **extra)
    if proc.returncode != 0:
        raise RuntimeError("tesseract failed: " + proc.stderr.decode("utf-8", "ignore")[:200])
    k = 72.0 / dpi
    out = []
    reader = csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", "ignore")), delimiter="\t",
                            quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        x, y, w, h = (int(row[c]) for c in ("left", "top", "width", "height"))
        out.append((pymupdf.Rect(x * k, y * k, (x + w) * k, (y + h) * k), text))
    return out


def needs_raster(page, pt: PageText, keep_images: bool = False) -> str:
    """Why this page cannot be trusted to its text layer ('' if it can)."""
    area = abs(page.rect) or 1.0
    img_area = 0.0
    for info in page.get_image_info():
        img_area += abs(pymupdf.Rect(info["bbox"]) & page.rect)
    # Small images on a text page are simply deleted; if the user wants them kept they must be
    # looked at, so anything bigger than a logo sends the page down the OCR route.
    if img_area / area >= (0.05 if keep_images else 0.30):
        return "scanned or image-based page"
    chars = [c for _, t in pt.words for c in t]
    if not chars:
        has_ink = bool(page.get_images()) or len(page.get_cdrawings()) > 3
        return "no text layer" if has_ink else ""
    bad = sum(1 for c in chars if ord(c) == 0xFFFD or 0xE000 <= ord(c) <= 0xF8FF or ord(c) < 32)
    if bad / len(chars) > 0.03:
        return "unreadable text encoding"
    return ""


# --------------------------------------------------------------------------
# One document
# --------------------------------------------------------------------------

@dataclass
class Result:
    source: str
    output: str = ""
    status: str = "ok"
    pages: int = 0
    raster_pages: int = 0
    withheld_pages: list = field(default_factory=list)
    redactions: Counter = field(default_factory=Counter)
    notes: list = field(default_factory=list)
    path: str = ""                                   # full path of the original
    boxes: dict = field(default_factory=dict)        # page index -> boxes applied, for the review screen
    raster_list: list = field(default_factory=list)  # page numbers that were flattened
    leftover: str = ""                               # a removed value found in the raw file (never saved)


class Scrubber:
    def __init__(self, args, tokens: Tokens, terms: Terms):
        self.args = args
        self.tokens = tokens
        self.terms = terms
        self.det_text = build_detectors(ocr=False)
        self.det_ocr = build_detectors(ocr=True)
        self.tesseract = find_tesseract(args.tesseract)
        self.has_ocr = bool(self.tesseract) and not args.no_ocr
        # sparse-text mode reads form labels and boxed digits best; automatic mode reads prose best
        self.psms = (11,) if args.fast_ocr else (11, 3)
        self.on_event = None                # progress callback: on_event(kind, **info)
        self.cancelled = False              # set from another thread to stop at the next page
        self.overrides: dict = {}           # the person's review decisions for the document in hand
        self.released: set[str] = set()

    # ---- loading -------------------------------------------------------
    def open(self, path: Path):
        doc = pymupdf.open(stream=path.read_bytes(), filetype="pdf")   # never the file itself
        if doc.needs_pass:
            passwords = self.args.password or []
            if isinstance(passwords, str):
                passwords = [passwords]
            if not any(doc.authenticate(pw) for pw in passwords):
                raise ValueError("password-protected (supply the password)")
        if doc.is_pdf is False:
            raise ValueError("not a PDF")
        try:
            doc.bake()         # form-field values and typed-on comments become page text we can redact
        except Exception:
            pass
        self.strip_catalog(doc)
        for page in doc:
            try:
                if page.rotation:
                    page.remove_rotation()
                if page.cropbox != page.mediabox:
                    page.set_cropbox(page.mediabox)
            except Exception:
                pass
        return doc

    @staticmethod
    def strip_catalog(doc) -> None:
        """Remove document-level stores that keep copies of field values, tags, scripts, bookmarks."""
        cat = doc.pdf_catalog()
        for key in ("AcroForm", "StructTreeRoot", "MarkInfo", "PieceInfo", "OpenAction", "AA", "Outlines",
                    "Metadata", "Names", "Perms", "Collection", "DSS", "Threads", "SpiderInfo", "Lang"):
            try:
                if doc.xref_get_key(cat, key)[0] != "null":
                    doc.xref_set_key(cat, key, "null")
            except Exception:
                pass
        for page in doc:
            for key in ("PieceInfo", "Metadata", "AA", "StructParents", "Thumb", "B"):
                try:
                    if doc.xref_get_key(page.xref, key)[0] != "null":
                        doc.xref_set_key(page.xref, key, "null")
                except Exception:
                    pass
            try:
                for annot in list(page.annots() or []):
                    page.delete_annot(annot)      # whatever bake() left behind
                for widget in list(page.widgets() or []):
                    page.delete_widget(widget)
            except Exception:
                pass

    def emit(self, kind: str, **info) -> None:
        if self.on_event:
            self.on_event(kind, **info)

    def check_cancel(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def learn_from(self, path: Path) -> None:
        """Pass 1: note every name and identifier found anywhere in the batch, so that pass 2 can
        remove each one wherever it appears - including pages where it has no label next to it."""
        try:
            doc = self.open(path)
        except Exception:
            return
        for page in doc:
            self.check_cancel()
            self.emit("scan_page", page=page.number + 1, pages=doc.page_count)
            pt = read_words(page)
            ocr, rules = False, None
            if not (self.args.rasterize or needs_raster(page, pt, self.args.keep_images)):
                rules = ruled_lines(page)
            elif self.has_ocr:
                try:
                    pix = page.get_pixmap(dpi=self.args.dpi, alpha=False, colorspace=pymupdf.csRGB)
                    pt = build_page_text(ocr_words(pix, self.args.dpi, 11, self.tesseract))
                    ocr, rules = True, raster_rules(pix, self.args.dpi)
                except Exception:
                    continue
            else:
                continue
            for hit in find_hits(pt, self.det_ocr if ocr else self.det_text, self.terms, self.tokens):
                if hit.value and hit.learn:
                    self.terms.learn_value(hit.kind, hit.value, hit.token)
            find_boxes(page.rect, pt, self.tokens, self.terms, self.args.keep_entities, rules,
                       learn_only=True, ocr=ocr)
        doc.close()

    # ---- pages ---------------------------------------------------------
    def filter(self, pt: PageText, hits: list[Hit], pno: int, register: bool = True):
        """Take out of the hits whatever the person asked to leave visible: phrases on the keep
        list, and boxes rejected in review. Returns (hits, word indexes that must stay)."""
        stay: set[int] = set()
        for rx, _ in self.terms.keep:
            for mt in rx.finditer(pt.text):
                stay.update(words_in_span(pt, *mt.span()))
        skips = self.overrides.get(pno, {}).get("skip", ())
        if skips:
            for i, (r, _) in enumerate(pt.words):
                centre = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                if any(s.contains(centre) for s in skips):
                    stay.add(i)
        out = []
        for h in hits:
            left = [i for i in h.words if i not in stay]
            if len(left) != len(h.words) and h.value:
                self.released.add(re.sub(r"[\W_]", "", h.value).lower())
            if left:
                h.words = left
                out.append(h)
        if register:
            for h in out:
                if h.value:
                    self.tokens.register(h.kind, h.value)
        return out, stay

    def page_hits(self, page_rect, pt: PageText, rules, ocr: bool, pno: int, regions=None) -> list[Hit]:
        hits, stay = self.filter(pt, find_hits(pt, self.det_ocr if ocr else self.det_text, self.terms, self.tokens),
                                 pno)
        found = [] if regions is not None else None
        boxed = find_boxes(page_rect, pt, self.tokens, self.terms, self.args.keep_entities, rules,
                           regions=found, ocr=ocr)
        boxed, _ = self.filter(pt, boxed, pno, register=False)
        if regions is not None:
            skips = self.overrides.get(pno, {}).get("skip", ())

            def mid(r):
                return pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)

            for rect, token in found:
                if any(s.contains(mid(rect)) or rect.contains(mid(s)) for s in skips):
                    continue                    # the person rejected this box in review
                if any(rect.contains(mid(pt.words[i][0])) for i in stay):
                    continue                    # it holds something they chose to keep
                regions.append((rect, token))
        return hits + boxed

    @staticmethod
    def note(res: Result, pno: int, rect, token: str, source: str = "auto") -> None:
        """Record a box for the review screen."""
        res.boxes.setdefault(pno, []).append(
            {"rect": [round(float(v), 2) for v in rect], "token": token, "source": source})

    def scrub_text_page(self, page, pno: int, res: Result) -> bool:
        """Redact a page that has a trustworthy text layer. False -> fall back to raster."""
        if not self.args.keep_images:
            for img in page.get_images():
                try:
                    page.delete_image(img[0])
                except Exception:
                    return False
        rules = ruled_lines(page)
        labels = not self.args.no_labels
        clean = False
        for attempt in range(3):
            pt = read_words(page)
            hits = self.page_hits(page.rect, pt, rules, ocr=False, pno=pno)
            if not hits:
                clean = True
                break
            if attempt == 2:
                break                           # text that will not go away: rasterise instead
            boxes = hit_rects(pt, hits)
            if attempt == 0:
                for h in {id(h): h for h in hits}.values():
                    res.redactions[h.kind] += 1
            for rect, token in boxes:
                self.note(res, pno, rect, token)
            redact_text_page(page, boxes, labels)
        if not clean:
            return False
        drawn = self.overrides.get(pno, {}).get("add", ())
        if drawn:
            redact_text_page(page, [(r, "[REDACTED]") for r in drawn], labels, manual=True)
            for r in drawn:
                self.note(res, pno, r, "[REDACTED]", "manual")
            res.redactions["MANUAL"] += len(drawn)
        return True

    def scrub_raster_page(self, doc, pno: int, res: Result) -> str:
        """Flatten the page to an image, OCR it, black out hits on the pixels. Returns its text."""
        if not self.has_ocr:
            raise OCRUnavailable()
        page = doc[pno]
        w, h, dpi = page.rect.width, page.rect.height, self.args.dpi
        # first remove whatever the (possibly hidden) text layer admits to
        pt = read_words(page)
        if pt.words:
            hits, _ = self.filter(pt, find_hits(pt, self.det_text, self.terms, self.tokens), pno)
            if hits:
                for hh in {id(x): x for x in hits}.values():
                    res.redactions[hh.kind] += 1
                boxes = hit_rects(pt, hits)
                for rect, token in boxes:
                    self.note(res, pno, rect, token)
                redact_text_page(page, boxes, False)
        pix = page.get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csRGB)
        scale = dpi / 72.0
        rules = raster_rules(pix, dpi)
        marks, text = [], ""
        page_rect = pymupdf.Rect(0, 0, w, h)

        def blackout(rect, token, pad):
            ir = pymupdf.IRect(int((rect.x0 - pad) * scale), int((rect.y0 - pad) * scale),
                               int((rect.x1 + pad) * scale) + 1, int((rect.y1 + pad) * scale) + 1)
            ir &= pix.irect
            if not ir.is_empty:
                pix.set_rect(ir, (0, 0, 0))
                marks.append((pymupdf.Rect(rect) & page_rect, token))

        drawn = list(self.overrides.get(pno, {}).get("add", ()))
        for attempt in range(5):
            self.check_cancel()
            reads = [build_page_text(ocr_words(pix, dpi, psm, self.tesseract)) for psm in self.psms]
            done = [m[0] + (-2, -2, 2, 2) for m in marks]
            todo = []
            for opt in reads:
                regions = [] if attempt == 0 else None
                hits = self.page_hits(page_rect, opt, rules, ocr=True, pno=pno, regions=regions)
                # OCR "reads" our own black boxes as stray glyphs; those are not new findings
                for hh in hits:
                    hh.words = [i for i in hh.words if not any(
                        d.contains(pymupdf.Point((opt.words[i][0].x0 + opt.words[i][0].x1) / 2,
                                                 (opt.words[i][0].y0 + opt.words[i][0].y1) / 2)) for d in done)]
                hits = [hh for hh in hits if hh.words]
                if attempt == 0:
                    for hh in {id(x): x for x in hits}.values():
                        res.redactions[hh.kind] += 1
                todo += [(rect, token, 0) for rect, token in regions or []]
                todo += [(rect, token, 1.5 + attempt) for rect, token in hit_rects(opt, hits)]
            if attempt == 0 and drawn:
                todo += [(r, "[REDACTED]", 0) for r in drawn]
                res.redactions["MANUAL"] += len(drawn)
            text = reads[-1].text
            if not todo:
                break                           # a full round of OCR found nothing left
            for rect, token, pad in todo:
                blackout(rect, token, pad)      # later rounds grow the box in case a sliver was left
        else:
            raise RuntimeError(f"page {pno + 1}: OCR keeps finding sensitive text")
        # swap the original page for the flattened, blacked-out image
        img = pymupdf.open()
        ip = img.new_page(width=w, height=h)
        ip.insert_image(ip.rect, stream=pix.tobytes("jpg", jpg_quality=88))
        for rect, token in marks:
            if rect.is_empty:
                continue
            self.note(res, pno, rect, token, "manual" if token == "[REDACTED]" else "auto")
            if not self.args.no_labels:
                size = max(3.5, min(7.0, rect.height * 0.8, 1.9 * rect.width / max(len(token), 1)))
                ip.insert_text((max(rect.x0, 1.0) + 1, rect.y1 - max(1.0, (rect.height - size) / 2)), token,
                               fontsize=size, color=(1, 1, 1))
        doc.delete_page(pno)
        doc.insert_pdf(img, from_page=0, to_page=0, start_at=pno)
        img.close()
        res.raster_pages += 1
        res.raster_list.append(pno + 1)
        if not self.args.no_labels:
            for _, token in marks:
                text += f"{token}\n"
        return text

    def withhold(self, doc, pno: int, why: str) -> None:
        w, h = doc[pno].rect.width, doc[pno].rect.height
        doc.delete_page(pno)
        page = doc.new_page(pno, width=w, height=h)
        page.insert_textbox(pymupdf.Rect(60, 80, w - 60, 200),
                            f"[PAGE WITHHELD BY SCRUBBER: {why}]", fontsize=11)

    # ---- whole document -----------------------------------------------
    def scrub(self, path: Path, out_dir: Path, out_stem: str, overrides: dict | None = None) -> Result:
        """Scrub one document. `overrides` carries the person's review decisions:
        {"rasterize": bool, "pages": {page_index: {"skip": [rect, ...], "add": [rect, ...]}}}"""
        res = Result(source=path.name, path=str(path))
        ov = overrides or {}
        self.overrides = {
            int(p): {"skip": [pymupdf.Rect(r) + (-1, -1, 1, 1) for r in v.get("skip", [])],
                     "add": [pymupdf.Rect(r) for r in v.get("add", [])]}
            for p, v in (ov.get("pages") or {}).items()}
        flatten = bool(self.args.rasterize or ov.get("rasterize"))
        self.released = set()                   # values the person chose to leave visible in this document
        self.tokens.literals = set()            # identifiers seen in THIS document
        try:
            doc = self.open(path)
        except Exception as exc:
            res.status, res.notes = "skipped", [str(exc)]
            return res
        res.pages = doc.page_count
        page_text: dict[int, str] = {}
        raster: set[int] = set()
        for pno in range(doc.page_count):
            self.check_cancel()
            page = doc[pno]
            pt = read_words(page)
            why = "flatten" if flatten else needs_raster(page, pt, self.args.keep_images)
            self.emit("page", page=pno + 1, pages=doc.page_count, scanned=bool(why))
            if not why and not self.scrub_text_page(doc[pno], pno, res):
                why = "text layer could not be cleaned"
                res.boxes.pop(pno, None)
                self.emit("page", page=pno + 1, pages=doc.page_count, scanned=True)
            if why:
                try:
                    page_text[pno] = self.scrub_raster_page(doc, pno, res)
                    raster.add(pno)
                except Cancelled:
                    raise
                except OCRUnavailable:
                    self.withhold(doc, pno, "image page and no OCR engine is installed")
                    res.withheld_pages.append(pno + 1)
                except Exception as exc:
                    self.withhold(doc, pno, "could not be verified clean")
                    res.withheld_pages.append(pno + 1)
                    res.notes.append(str(exc))

        # strip everything that is not page content
        self.strip_catalog(doc)
        try:
            doc.scrub(redact_images=0)
        except Exception:
            doc.set_metadata({})
            doc.del_xml_metadata()
        data = doc.tobytes(garbage=4, deflate=True, clean=True)
        doc.close()

        # ---- verify the bytes we are about to write --------------------
        problems = []
        chk = pymupdf.open(stream=data, filetype="pdf")
        for pno in range(chk.page_count):
            page = chk[pno]
            if pno in raster:
                stray = [w[4] for w in page.get_text("words") if not re.fullmatch(r"[\[\]A-Z0-9-]+", w[4])]
                if stray:
                    problems.append(f"page {pno + 1}: unexpected text layer on flattened page")
                continue
            pt = read_words(page)
            left, _ = self.filter(pt, find_hits(pt, self.det_text, self.terms, self.tokens), pno, register=False)
            if left:
                problems.append(f"page {pno + 1}: {len(left)} detector hit(s) remain "
                                f"({', '.join(sorted({h.kind for h in left}))})")
            page_text[pno] = page.get_text("text", sort=True)
        if any(v for v in (chk.metadata or {}).values() if v and v not in ("PDF 1.7", "PDF 1.6", "PDF 1.5", "PDF 1.4")
               and not str(v).startswith("PDF ")) or chk.get_toc() or chk.embfile_count():
            problems.append("metadata, bookmarks or attachments survived")
        needles = sorted({n.encode("utf-8", "ignore") for n in self.tokens.literals | self.terms.literals
                          if len(n) >= 5 and re.sub(r"[\W_]", "", n) not in self.released},
                         key=len, reverse=True)
        needle_rx = re.compile(rb"(?<![a-z0-9])(?:" + b"|".join(re.escape(n) for n in needles) +
                               rb")(?![a-z0-9])") if needles else None
        for xref in range(1, chk.xref_length() if needle_rx else 1):
            try:
                obj = chk.xref_object(xref, compressed=True)
                if "/Image" in obj or "FontFile" in obj or "/Length1" in obj:
                    blob = obj.encode("latin-1", "ignore")
                else:
                    blob = obj.encode("latin-1", "ignore") + (chk.xref_stream(xref) or b"")
            except Exception:
                continue
            found = needle_rx.search(blob.lower())
            if found:
                problems.append(f"a value redacted elsewhere is still stored inside this PDF (object {xref})")
                res.leftover = found.group(0).decode("utf-8", "ignore")
                break
        chk.close()
        if problems:
            res.status, res.notes = "FAILED", res.notes + problems
            return res                          # nothing is written

        out_pdf = out_dir / f"{out_stem}.redacted.pdf"
        try:
            out_pdf.write_bytes(data)
            if not self.args.no_text:
                body = []
                for pno in range(res.pages):
                    body.append(f"===== page {pno + 1} =====\n{page_text.get(pno, '').strip()}\n")
                (out_dir / f"{out_stem}.redacted.txt").write_text("\n".join(body), encoding="utf-8")
        except OSError as exc:
            res.status = "FAILED"
            res.notes.append(f"could not be saved ({type(exc).__name__})")
            return res
        res.output = out_pdf.name
        if res.withheld_pages:
            res.status = "partial"
        return res


# --------------------------------------------------------------------------
# Sessions: one results folder, remembered between runs
# --------------------------------------------------------------------------

def safe_stem(stem: str, scrubber: Scrubber, index: int) -> str:
    """File names usually carry a surname or fund name, so by default outputs are just numbered."""
    if not scrubber.args.keep_names:
        return f"doc_{index:03d}"
    probe = re.sub(r"[_]+", " ", stem)
    for det in scrubber.det_text[:3]:
        probe = det.rx.sub("ID", probe)
    for rx, _, _ in scrubber.terms.items:
        probe = rx.sub("X", probe)
    probe = re.sub(r"\d{9,}", "ID", probe)
    probe = re.sub(r"[^\w.-]+", "_", probe).strip("_.") or "document"
    return f"{index:03d}_{probe}"


def collect(inputs: list[str]) -> list[Path]:
    """The PDFs named by `inputs` (files, or folders searched all the way down). Folders that
    cannot be read - common on Windows for system and cloud-sync folders - are passed over."""
    files: list[Path] = []
    for item in inputs:
        p = Path(item).expanduser()
        try:
            if p.is_dir():
                found = []
                for folder, _, names in os.walk(p):         # os.walk skips what it cannot open
                    found += [Path(folder) / n for n in names if n.lower().endswith(".pdf")]
                files += sorted(found)
            elif p.is_file():
                files.append(p)
            else:
                print(f"  ! not found: {item}", file=sys.stderr)
        except OSError:
            print(f"  ! could not read: {item}", file=sys.stderr)
    return [f for f in files if not f.name.lower().endswith(".redacted.pdf")]


def bundled_dir() -> Path:
    """Where files shipped with the packaged app live (the script's own folder when run from source)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_tesseract(explicit: str | None = None) -> str | None:
    """Locate the Tesseract OCR program: an explicit path, the copy bundled with the app, PATH,
    then the usual install folders."""
    if explicit:
        return shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
    bundled = bundled_dir() / "tesseract" / ("tesseract.exe" if os.name == "nt" else "tesseract")
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("tesseract")
    if found:
        return found
    guesses = ["/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            guesses.append(str(Path(base) / "Tesseract-OCR" / "tesseract.exe"))
            guesses.append(str(Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    return next((g for g in guesses if Path(g).is_file()), None)


RUN_LOG = "scrub_last_run.log"
TERMS_FILE = "pii_terms.txt"


def data_dir() -> Path:
    """The private folder for the names list, run log and project memory. Never the results folder."""
    override = os.environ.get("PDF_SCRUBBER_HOME")
    if override:
        folder = Path(override)
    elif os.name == "nt":
        folder = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "PDF Scrubber"
    elif sys.platform == "darwin":
        folder = Path.home() / "Library" / "Application Support" / "PDF Scrubber"
    else:
        folder = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pdf-scrubber"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def default_terms_path() -> Path:
    """The names list: beside the script if one is already there, otherwise in the private folder."""
    legacy = Path(__file__).resolve().parent / TERMS_FILE
    return legacy if legacy.is_file() else data_dir() / TERMS_FILE


def explain(status: str, notes=(), flattened: int = 0, withheld=()) -> tuple[str, str]:
    """What happened to a document and what to do about it, in plain words."""
    text = " ".join(notes)
    if status == "ok":
        if flattened:
            return ("Scrubbed. It has scanned pages.",
                    "Look through the scanned pages yourself before sharing: scans are where things get missed.")
        return ("Scrubbed.", "Skim it before sharing.")
    if status == "partial":
        return (f"Scrubbed, but {len(withheld)} page(s) were left out because they could not be checked.",
                "Install the scan reader (Tesseract) and scrub again, or share it without those pages.")
    if status == "cancelled":
        return ("Not finished - you cancelled.", "Scrub again when you are ready.")
    if status == "skipped":
        if "password" in text:
            return ("Not scrubbed - it is locked with a password.", "Enter its password and scrub again.")
        return ("Not scrubbed - it could not be opened as a PDF.",
                "Check that it opens in a PDF reader. If it does not, download or export it again.")
    if "could not be saved" in text:
        return ("Not saved - the computer would not let the tool write the scrubbed copy.",
                "If that copy is open in a PDF reader, close it. Check the results folder is not read-only, "
                "then scrub again.")
    if "stored inside" in text or "detector hit" in text:
        return ("Held back - something that was removed is still stored inside the file, out of reach.",
                "Scrub it again with 'Flatten' turned on. That rebuilds every page as a picture.")
    if "metadata" in text:
        return ("Held back - hidden document details could not be stripped.",
                "Scrub it again with 'Flatten' turned on, or re-save the original from a PDF reader first.")
    return ("Held back - the tool could not confirm this file is clean, so nothing was saved.",
            "Scrub it again with 'Flatten' turned on. If that fails too, cover the page by hand.")


class Project:
    """Remembers a results folder between runs, so tags and document numbers stay the same when
    files are added or scrubbed again. Kept in the private folder: it contains personal details."""

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir).expanduser().resolve()
        digest = hashlib.sha1(str(self.out_dir).lower().encode("utf-8")).hexdigest()[:16]
        self.path = data_dir() / "projects" / f"{digest}.json"
        self.data = {"version": 1, "out_dir": str(self.out_dir), "tokens": {}, "terms": {}, "files": {}}
        self.resumed = False

    @classmethod
    def open(cls, out_dir: Path, fresh: bool = False) -> "Project":
        project = cls(out_dir)
        # only trust the memory while the results it describes are still there
        if not fresh and project.path.is_file() and (project.out_dir / "scrub_report.json").is_file():
            try:
                data = json.loads(project.path.read_text(encoding="utf-8"))
                if data.get("version") == 1:
                    project.data, project.resumed = data, True
            except (OSError, ValueError):
                pass
        return project

    def entry(self, source: Path) -> dict:
        key = str(Path(source).resolve())
        files = self.data["files"]
        if key not in files:
            files[key] = {"n": max((f["n"] for f in files.values()), default=0) + 1, "name": Path(source).name}
        return files[key]

    def done(self) -> list[dict]:
        return sorted((f for f in self.data["files"].values() if f.get("status")), key=lambda f: f["n"])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def forget(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class Session:
    """One results folder being worked on: options, names list, tags and documents.
    The command line, the text menu and the window all drive the tool through this."""

    def __init__(self, args, on_event=None):
        self.args = args
        self.out_dir = Path(args.out).expanduser()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.project = Project.open(self.out_dir, fresh=getattr(args, "fresh", False))
        self.tokens = Tokens()
        self.tokens.load(self.project.data.get("tokens"))
        self.terms = Terms(self.tokens)
        self.terms.restore(self.project.data.get("terms"))
        self.user_terms = self.terms.load(Path(args.terms).expanduser()) if args.terms else 0
        self.scrubber = Scrubber(args, self.tokens, self.terms)
        self.scrubber.on_event = on_event
        self.on_event = on_event
        self.results: dict[str, Result] = {}

    def cancel(self) -> None:
        self.scrubber.cancelled = True

    def scan(self, files: list[Path]) -> None:
        """Pass 1: learn names and identifiers from every document."""
        self.terms.frozen = False
        try:
            for n, f in enumerate(files, 1):
                self.scrubber.check_cancel()
                if self.on_event:
                    self.on_event("scan_file", index=n, total=len(files), name=f.name)
                self.scrubber.learn_from(f)
        finally:
            self.terms.frozen = True

    def scrub(self, path: Path, overrides: dict | None = None) -> Result:
        """Pass 2 for one document. Review decisions saved earlier are reused unless new ones are given."""
        self.terms.frozen = True
        entry = self.project.entry(path)
        if overrides is None:
            overrides = entry.get("overrides") or {}
        stem = safe_stem(Path(path).stem, self.scrubber, entry["n"])
        res = self.scrubber.scrub(Path(path), self.out_dir, stem, overrides)
        if not res.output and not any("could not be saved" in n for n in res.notes):
            for suffix in (".redacted.pdf", ".redacted.txt"):   # never leave an older copy looking current
                try:
                    (self.out_dir / f"{stem}{suffix}").unlink()
                except OSError:
                    pass
        entry.update(output=res.output, status=res.status, pages=res.pages, flattened=res.raster_pages,
                     scanned_pages=list(res.raster_list), withheld=list(res.withheld_pages),
                     redactions=dict(res.redactions), notes=list(res.notes), overrides=overrides,
                     boxes={str(k): v for k, v in res.boxes.items()})
        self.results[str(Path(path).resolve())] = res
        return res

    def report(self) -> dict:
        done = self.project.done()
        return {
            "files": [{
                "output": f.get("output", ""), "status": f["status"], "pages": f.get("pages", 0),
                "flattened_pages": f.get("flattened", 0), "withheld_pages": f.get("withheld", []),
                "redactions": f.get("redactions", {}),
                "notes": [] if f["status"] == "skipped" else f.get("notes", []),
            } for f in done],
            "totals": dict(sum((Counter(f.get("redactions", {})) for f in done), Counter())),
        }

    def finish(self) -> Path | None:
        """Write the report, the private file list and the project memory. Returns the list's path."""
        (self.out_dir / "scrub_report.json").write_text(json.dumps(self.report(), indent=2), encoding="utf-8")
        self.project.data["tokens"] = self.tokens.dump()
        self.project.data["terms"] = self.terms.dump()
        self.project.save()
        # Which original became which number. Original file names are personal, so this list lives
        # in the private folder and never in the results folder.
        try:
            log = data_dir() / RUN_LOG
            lines = [f"scrub_pdfs {VERSION} - results folder: {self.out_dir}", ""]
            for f in self.project.done():
                lines.append(f"{f.get('output') or '(not written)':<24} <- {f['name']}   [{f['status']}]")
                lines.append(f"{'':<24}    {explain(f['status'], f.get('notes', []), f.get('flattened', 0), f.get('withheld', []))[0]}")
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return log
        except OSError:
            return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Redact personal information from tax PDFs, locally. "
                    "Run with no arguments for a guided menu.",
        epilog="Always skim the .redacted.txt files (and the PDFs) before sharing anything.")
    ap.add_argument("inputs", nargs="*", help="PDF files and/or folders (searched recursively)")
    ap.add_argument("-o", "--out", help="output folder")
    ap.add_argument("--menu", action="store_true", help="open the guided menu (the default when no inputs are given)")
    ap.add_argument("--terms", help="text file of names/words to redact everywhere (see pii_terms.example.txt)")
    ap.add_argument("--no-terms", action="store_true", help="run without a terms file (not recommended)")
    ap.add_argument("--fresh", action="store_true",
                    help="forget earlier runs into this output folder (tags and numbering start again)")
    ap.add_argument("--rasterize", action="store_true",
                    help="flatten EVERY page to an image and OCR-check it (slowest, most conservative)")
    ap.add_argument("--keep-entities", action="store_true",
                    help="keep partnership / employer / payer / preparer names (their EINs still go)")
    ap.add_argument("--keep-images", action="store_true", help="keep logos and other images on text pages")
    ap.add_argument("--keep-names", action="store_true",
                    help="reuse (cleaned-up) input file names instead of doc_001, doc_002, ...")
    ap.add_argument("--no-learn", action="store_true",
                    help="do not carry names/identifiers found in one place over to the rest of the batch")
    ap.add_argument("--no-labels", action="store_true", help="plain black boxes instead of [SSN-1] style tokens")
    ap.add_argument("--no-text", action="store_true", help="do not write .redacted.txt review files")
    ap.add_argument("--no-ocr", action="store_true", help="withhold scanned pages instead of OCR'ing them")
    ap.add_argument("--dpi", type=int, default=300, help="resolution for scanned pages (default 300)")
    ap.add_argument("--fast-ocr", action="store_true", help="one OCR pass per round instead of two (less thorough)")
    ap.add_argument("--tesseract", help="path to the tesseract program if it is not found automatically")
    ap.add_argument("--password", action="append", default=[],
                    help="password for encrypted PDFs (repeat the option to try several)")
    ap.add_argument("--show-learned", action="store_true",
                    help="print what pass 1 learned (contains PII - shown on screen only, never saved)")
    return ap


def execute(args) -> tuple[int, list]:
    """Run one scrub with parsed options. Returns (exit code, per-file results)."""
    print(f"scrub_pdfs version {VERSION}")
    files = collect(args.inputs)
    if not files:
        print("No PDFs found.", file=sys.stderr)
        return 2, []
    if not args.out:
        print("An output folder is required (-o FOLDER).", file=sys.stderr)
        return 2, []
    if not args.terms and not args.no_terms:
        print("A --terms file is required: names can only be found automatically inside recognised\n"
              "IRS form boxes, so everywhere else the tool needs to be told what to look for.\n"
              "Copy pii_terms.example.txt, list your names / dependants / businesses, and pass it\n"
              "with --terms.  (To run without one anyway, add --no-terms.)", file=sys.stderr)
        return 2, []

    live = sys.stdout.isatty()

    def on_event(kind, **info):
        if not live:
            return
        if kind == "scan_file":
            print(f"\r          file {info['index']} of {info['total']}   ", end="", flush=True)
        elif kind == "page" and info["scanned"]:
            print(f"\r            scanned page {info['page']} of {info['pages']}...   ", end="", flush=True)

    session = Session(args, on_event)
    out_dir, terms, scrubber = session.out_dir, session.terms, session.scrubber
    if session.project.resumed:
        print(f"Continuing in {out_dir}: {len(session.project.done())} document(s) already there, so tags and\n"
              "numbering carry on from them. (Add --fresh to start over.)")
    if args.terms:
        print(f"Loaded {session.user_terms} term(s) from {args.terms}")
    else:
        print("WARNING: running without a terms file - names outside recognised form boxes will remain.")
    if args.no_ocr:
        print("NOTE: --no-ocr given - scanned pages will be WITHHELD, not redacted.")
    elif not scrubber.has_ocr:
        print("NOTE: Tesseract OCR was not found - scanned pages will be WITHHELD, not redacted.\n"
              "      Install it (macOS: brew install tesseract; Ubuntu: apt install tesseract-ocr;\n"
              "      Windows: UB-Mannheim installer) or point to it with --tesseract.")

    results = []
    try:
        if not args.no_learn:
            print(f"Pass 1/2: reading {len(files)} file(s) to learn names and identifiers...")
            session.scan(files)
            print(f"{chr(13) if live else ''}          learned {len(terms.learned)} value(s) to remove "
                  "wherever they appear")
            if args.show_learned:
                for record in terms.learned:
                    print(f"            {record['phrase']}")
        terms.frozen = True

        print(f"Pass 2/2: scrubbing -> {out_dir}")
        for f in files:
            res = session.scrub(f)
            results.append(res)
            total = sum(res.redactions.values())
            extra = ""
            if res.raster_pages:
                extra += f", {res.raster_pages} page(s) flattened+OCR"
            if res.withheld_pages:
                extra += f", pages withheld: {res.withheld_pages}"
            target = f" -> {res.output}" if res.output else ""
            print(f"{chr(13) if live else ''}  [{res.status:^7}] {f.name}{target}: {res.pages} page(s), "
                  f"{total} redaction(s){extra}")
            for note in res.notes:
                print(f"            - {note}")
            if res.leftover:
                print(f"            ! still present: {res.leftover!r}", file=sys.stderr)
            if res.status not in ("ok",):
                print(f"            What to do: {explain(res.status, res.notes, res.raster_pages, res.withheld_pages)[1]}")
    except KeyboardInterrupt:
        print("\nStopped. Documents already finished are saved; your originals are untouched.")
    finally:
        log = session.finish()

    if log:
        print(f"\nWhich original became which number is listed in {log} (keep that file private).")
    report = session.report()
    bad = [r for r in results if r.status in ("FAILED", "skipped")]
    part = [r for r in results if r.status == "partial"]
    print(f"\nDone: {len(results) - len(bad) - len(part)} clean, {len(part)} partial, {len(bad)} not written.")
    print("Redactions by type:", ", ".join(f"{k} {v}" for k, v in sorted(report["totals"].items())) or "none")
    scans = [r for r in results if r.raster_pages and r.output]
    if scans:
        print("\nLOOK AT THESE BEFORE SHARING - they contain scanned pages, where redaction depends on OCR\n"
              "and handwriting, stamps or faint digits can be missed:")
        for r in scans:
            print(f"  {r.output}  ({r.raster_pages} scanned page(s))")
    print("\nBefore sharing anything: skim each .redacted.txt (it is exactly the text a reader can still\n"
          "extract) and flip through the PDFs. Add anything you spot to the terms file and re-run.")
    return (1 if bad else 0), results


# --------------------------------------------------------------------------
# Guided menu (what you get when the script is started with no arguments)
# --------------------------------------------------------------------------

RULE = "=" * 66


class Quit(Exception):
    """Raised when the person closes the input (Ctrl+C / Ctrl+Z / end of input)."""


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{hint}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit()
    return answer or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    while True:
        answer = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str = "") -> str:
    """options: (key, label). Returns the chosen key."""
    for key, label in options:
        print(f"  {key}. {label}")
    keys = [k for k, _ in options]
    while True:
        answer = ask(prompt, default)
        if answer in keys:
            return answer
        print(f"  Please type one of: {', '.join(keys)}")


def ask_path(prompt: str, default: str = "") -> Path | None:
    """Accepts a typed or dragged-in path (dragging adds quotes on most systems). None if left empty."""
    raw = ask(prompt, default).strip()
    if raw.startswith("& "):                    # PowerShell puts this in front of a dragged path
        raw = raw[2:].strip()
    raw = raw.strip('"').strip("'").strip()
    return Path(os.path.expandvars(raw)).expanduser() if raw else None


def ask_secret(prompt: str) -> str:
    try:
        if sys.stdin.isatty():
            return getpass.getpass(f"{prompt}: ")
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit()


def ask_lines(heading: str) -> list[str]:
    print(heading)
    print("  One per line. Press Enter on an empty line when you are done.")
    out = []
    while True:
        line = ask("   >")
        if not line:
            return out
        out.append(line)


def pause() -> None:
    try:
        input("\nPress Enter to continue...")
    except (EOFError, KeyboardInterrupt):
        raise Quit()


def read_terms(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def ensure_pdf_library() -> bool:
    """Offer to install PyMuPDF if it is missing. True when it is ready to use."""
    global pymupdf
    if pymupdf is not None:
        return True
    print("\nThe PDF library this tool needs (PyMuPDF) is not installed yet.")
    if not ask_yes("Download and install it now? (needs an internet connection, about 20 MB)"):
        print(f"\nTo install it yourself:   {Path(sys.executable).name} -m pip install pymupdf")
        return False
    code = subprocess.call([sys.executable, "-m", "pip", "install", "pymupdf"])
    if code == 0:
        try:
            pymupdf = importlib.import_module("pymupdf")
            _quiet_pdf_library()
            print("Installed.")
            return True
        except ImportError:
            pass
    print("\nThe install did not work. Try this in a terminal, then start the tool again:\n"
          f"    {Path(sys.executable).name} -m pip install pymupdf")
    return False


def ocr_help() -> None:
    print("\nScanned PDFs (photos or scans of paper) need a free program called Tesseract so the tool\n"
          "can read them. Without it, scanned pages are left out of the results rather than passed\n"
          "through unchecked. PDFs downloaded from tax software or a portal do not need it.\n"
          "\n"
          "  Windows: search for 'UB Mannheim Tesseract', download the 64-bit installer, keep the defaults\n"
          "  macOS:   brew install tesseract\n"
          "  Linux:   sudo apt install tesseract-ocr     (or your distribution's equivalent)\n"
          "\n"
          "Start this tool again afterwards and it will find Tesseract by itself.")


def edit_terms(path: Path) -> None:
    """Build or extend the private list of names and words to remove everywhere."""
    existing = read_terms(path)
    print(f"\n{RULE}\n Your list of names and words to remove\n{RULE}")
    print("The tool finds SSNs, account numbers, addresses, phone numbers and e-mails by itself.\n"
          "What it cannot guess is names - so list them here. This list stays on your computer,\n"
          f"in {path}\nNever share or publish that file.\n")
    if existing:
        print(f"The list has {len(existing)} entries:")
        for line in existing:
            print(f"    {line}")
        choice = ask_choice("\nWhat would you like to do?",
                            [("1", "Add more entries"), ("2", "Start the list over"), ("3", "Leave it as it is")], "1")
        if choice == "3":
            return
        if choice == "2":
            existing = []
    new = list(existing)
    people = ask_lines("\nPEOPLE - everyone named in the documents: the taxpayer, spouse, children,\n"
                       "former names. Full names, e.g.  Jane Q. Sample")
    for person in people:
        new.append(f"name: {person}")
        for word in re.findall(r"[^\W\d_]{3,}", person):
            if word.lower() in STOPWORDS:
                print(f"\n  '{word}' is also an everyday word, so it is only removed as part of the full name.")
                if ask_yes(f"  Remove '{word}' everywhere it appears as a separate word too?", False):
                    new.append(word)
    for item in ask_lines("\nBUSINESSES, TRUSTS, EMPLOYERS, FUNDS, your accountant and their firm.\n"
                          "Exact names, e.g.  Sample Family Revocable Trust"):
        new.append(f"entity: {item}")
    for item in ask_lines("\nPLACES - your town(s) and street name(s), without the house number,\n"
                          "e.g.  Springfield   or   Maple Hollow"):
        new.append(item)
    for item in ask_lines("\nANYTHING ELSE that would identify you (nicknames, a boat name, a property name)."):
        new.append(item)
    seen, unique = set(), []
    for line in new:
        if line.lower() not in seen:
            seen.add(line.lower())
            unique.append(line)
    header = ("# Names and words to remove from every document. One per line.\n"
              "# 'name:' in front of a person removes the full name and each name word.\n"
              "# KEEP THIS FILE PRIVATE - never upload or publish it.\n\n")
    path.write_text(header + "\n".join(unique) + "\n", encoding="utf-8")
    print(f"\nSaved {len(unique)} entries to {path}")


def previous_outputs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [p for p in folder.iterdir() if p.is_file() and
            (re.fullmatch(r".*\.redacted\.(pdf|txt)", p.name, re.I) or p.name == "scrub_report.json")]


def guided_scrub(state: dict) -> None:
    print(f"\n{RULE}\n Scrub PDFs - five short questions\n{RULE}")
    if not ensure_pdf_library():
        return

    # 1. where the PDFs are
    print("\nStep 1 of 5 - Where are the PDFs?")
    print("  Type the folder's location, or drag the folder into this window and press Enter.")
    while True:
        source = ask_path("  Folder", state.get("source", ""))
        files = collect([str(source)]) if source else []
        if files:
            break
        print("  No PDFs found there. Check the location and try again (Ctrl+C goes back to the menu).")
    state["source"] = str(source)
    copies = [f for f in files if re.search(r" \(\d+\)$| - Copy$", f.stem)
              and any(g.stem == re.sub(r" \(\d+\)$| - Copy$", "", f.stem) for g in files)]
    print(f"  Found {len(files)} PDF(s).")
    if copies:
        print(f"  {len(copies)} of them look like duplicate downloads (same name with '(1)' or '- Copy').")
        if ask_yes("  Skip the duplicates?", True):
            files = [f for f in files if f not in copies]

    # 2. where the results go
    print("\nStep 2 of 5 - Where should the scrubbed copies go?")
    print("  Your originals are never changed. Press Enter to accept the suggestion.")
    suggestion = state.get("out") or str(source.parent / f"{source.name} Scrubbed" if source.is_dir()
                                         else source.parent / "Scrubbed")
    while True:
        out = ask_path("  Folder", suggestion)
        if out is None:
            continue
        if out.resolve() == source.resolve():
            print("  That is the folder holding your originals - please choose a different one.")
            continue
        break
    state["out"] = str(out)
    old = previous_outputs(out)
    if old:
        print(f"  That folder holds {len(old)} file(s) from an earlier run. Mixing runs causes confusion.")
        if ask_yes("  Delete those earlier results first? (only the tool's own output files)", True):
            for p in old:
                try:
                    p.unlink()
                except OSError:
                    pass

    # 3. the names list
    terms_path = Path(state.get("terms") or default_terms_path())
    print("\nStep 3 of 5 - Names to remove")
    entries = read_terms(terms_path)
    if entries:
        print(f"  Using your list of {len(entries)} entries ({terms_path.name}).")
        if ask_yes("  Do you want to view or add to it first?", False):
            edit_terms(terms_path)
    else:
        print("  There is no list yet. The tool cannot guess names, so let's make one now.")
        edit_terms(terms_path)
    entries = read_terms(terms_path)
    state["terms"] = str(terms_path)
    if not entries:
        print("\n  The list is empty. Names will only be removed inside recognised IRS form boxes.")
        if not ask_yes("  Continue without a names list?", False):
            return

    # 4. passwords
    print("\nStep 4 of 5 - Password-protected PDFs")
    passwords = list(state.get("passwords", []))
    if passwords:
        print(f"  {len(passwords)} password(s) remembered from earlier in this session.")
    if ask_yes("  Do any of the PDFs need a password to open?" if not passwords
               else "  Add another password?", False):
        print("  Type each password and press Enter (typing is hidden). Empty line when done.")
        while True:
            secret = ask_secret("   password")
            if not secret:
                break
            passwords.append(secret)
    state["passwords"] = passwords

    # 5. options
    print("\nStep 5 of 5 - Two choices")
    keep_entities = ask_yes("  Keep company names (partnerships, employers, banks, your accountant)?\n"
                            "  They can help the analysis but also hint at who you are. Keep them?", False)
    tesseract = find_tesseract(state.get("tesseract"))
    fast = False
    if tesseract:
        fast = not ask_yes("  Scanned pages: use the thorough check? (slower, recommended)", True)
    else:
        print("\n  NOTE: the scan reader (Tesseract) was not found. Scanned pages will be LEFT OUT of the\n"
              "  results. Choose 'Setup check' in the main menu for install steps.")
        if not ask_yes("  Continue anyway?", True):
            return

    # review
    print(f"\n{RULE}\n Ready\n{RULE}")
    print(f"  PDFs to scrub : {len(files)} file(s) from {source}")
    print(f"  Results go to : {out}")
    print(f"  Names list    : {len(entries)} entries")
    print(f"  Passwords     : {len(passwords)}")
    print(f"  Company names : {'kept' if keep_entities else 'removed'}")
    print(f"  Scanned pages : {('thorough check' if not fast else 'fast check') if tesseract else 'left out (no Tesseract)'}")
    print("\n  Text PDFs take a second or two each. Scanned pages take around half a minute per page.")
    if not ask_yes("\nStart now?", True):
        return

    argv = [str(f) for f in files] + ["-o", str(out)]
    argv += ["--terms", str(terms_path)] if entries else ["--no-terms"]
    for secret in passwords:
        argv += ["--password", secret]
    if keep_entities:
        argv.append("--keep-entities")
    if fast:
        argv.append("--fast-ocr")
    if tesseract:
        argv += ["--tesseract", tesseract]
    print()
    try:
        _, results = execute(build_parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\n\nStopped. Files already finished are in the results folder; your originals are untouched.")
        return
    state["results"] = results

    locked = [r for r in results if r.status == "skipped" and any("password" in n for n in r.notes)]
    failed = [r for r in results if r.status == "FAILED"]
    print(f"\n{RULE}\n What to do next\n{RULE}")
    if locked:
        print(f"  * {len(locked)} file(s) were locked with a password that was not supplied (or did not match).\n"
              "    Choose 'Scrub PDFs' again and enter the password at step 4.")
    if failed:
        print(f"  * {len(failed)} file(s) were held back because the tool could not confirm they were clean.\n"
              "    Nothing was written for them. Add any name it missed to your list and run again.")
    print("  * Use 'Check the results for a name or number' in the main menu to search for anything\n"
          "    you know should be gone: a surname, the last four digits of an SSN, a street.\n"
          "  * Open a few of the PDFs and look at them, especially any listed above as scanned.")


def check_results(state: dict) -> None:
    print(f"\n{RULE}\n Check the results for a name or number\n{RULE}")
    print("Searches the text of every scrubbed file. No matches is what you want to see.\n"
          "Note: this reads text only. It cannot see inside scanned page images - look at those yourself.")
    folder = ask_path("\nResults folder", state.get("out", ""))
    texts = sorted(folder.glob("*.redacted.txt")) if folder and folder.is_dir() else []
    if not texts:
        print("No scrubbed text files (*.redacted.txt) found there.")
        return
    while True:
        needle = ask("\nWord or number to look for (empty line to stop)")
        if not needle:
            return
        squashed = re.sub(r"[\s.\-]", "", needle).lower()
        hits = 0
        for path in texts:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                plain = line.lower()
                if needle.lower() in plain or (len(squashed) >= 4 and squashed in re.sub(r"[\s.\-]", "", plain)):
                    hits += 1
                    if hits <= 20:
                        print(f"  {path.name}, line {number}: {line.strip()[:90]}")
        if hits:
            print(f"  FOUND {hits} time(s). Add it to your names list and scrub again.")
        else:
            print(f"  Not found in {len(texts)} file(s).")


def setup_check(state: dict) -> None:
    print(f"\n{RULE}\n Setup check\n{RULE}")
    print(f"  Python          : {sys.version.split()[0]}  OK")
    if pymupdf is None:
        print("  PDF library     : NOT INSTALLED")
        ensure_pdf_library()
    else:
        print(f"  PDF library     : PyMuPDF {getattr(pymupdf, '__version__', '')}  OK")
    tesseract = find_tesseract(state.get("tesseract"))
    if tesseract:
        print(f"  Scan reader     : Tesseract found at {tesseract}  OK")
    else:
        print("  Scan reader     : Tesseract NOT FOUND - scanned pages will be left out")
        ocr_help()
        if ask_yes("\nIs Tesseract installed somewhere this tool did not look?", False):
            where = ask_path("  Location of the tesseract program")
            if where and find_tesseract(str(where)):
                state["tesseract"] = str(where)
                print("  Found it. It will be used for this session.")
            else:
                print("  No program found at that location.")
    terms_path = default_terms_path()
    count = len(read_terms(terms_path))
    print(f"  Names list      : {f'{count} entries' if count else 'not created yet'}  ({terms_path})")


def about() -> None:
    print(f"""
{RULE}
 What this tool does - and does not do
{RULE}
It makes copies of your tax PDFs with personal details blacked out, so the copies can be
shared (for example with an AI assistant) for analysis. Everything happens on this computer;
nothing is uploaded and your originals are never changed.

REMOVED  SSNs and EINs, bank routing and account numbers, phone numbers, e-mail addresses,
         street addresses, city and ZIP, dates of birth, IP PINs, the name / address / ID boxes
         on IRS forms, everything on your names list, hidden data inside the PDF (metadata,
         bookmarks, attachments, form-field values) and the original file names.
KEPT     Dollar amounts, line numbers, dates, state codes, occupations.
REPLACED Each removed item becomes a tag such as [ID-1] or [NAME-2]. The same person or number
         gets the same tag in every document, so the documents can still be read together.

LIMITS - please read
  * It cannot recognise a name it was never told. Fill in your names list.
  * Scanned pages are the weak spot. Handwriting, stamps, signatures and faint print can be
    missed. Always look at scanned pages yourself before sharing.
  * It is pattern matching, not a guarantee. Skim the results before you share them.
  * Not legal or tax advice. Use at your own risk.""")


def interactive() -> int:
    state: dict = {}
    try:
        while True:
            terms_count = len(read_terms(default_terms_path()))
            print(f"\n{RULE}\n PDF Scrubber {VERSION} - black out personal details in tax PDFs\n"
                  f" Runs entirely on this computer. Nothing is uploaded.\n{RULE}")
            print(f"  PDF library: {'ready' if pymupdf is not None else 'NOT INSTALLED (option 4)'}"
                  f"   |   Scan reader: {'ready' if find_tesseract(state.get('tesseract')) else 'not found (option 4)'}"
                  f"   |   Names list: {f'{terms_count} entries' if terms_count else 'none yet'}\n")
            choice = ask_choice("Choose", [
                ("1", "Scrub PDFs  (guided, five questions)"),
                ("2", "Edit my list of names and words to remove"),
                ("3", "Check the results for a name or number"),
                ("4", "Setup check"),
                ("5", "What this tool does and does not do"),
                ("0", "Quit"),
            ], "1")
            try:
                if choice == "0":
                    return 0
                if choice == "1":
                    guided_scrub(state)
                elif choice == "2":
                    edit_terms(default_terms_path())
                elif choice == "3":
                    check_results(state)
                elif choice == "4":
                    setup_check(state)
                elif choice == "5":
                    about()
                pause()
            except KeyboardInterrupt:
                print("\n(back to the menu)")
    except Quit:
        return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.menu or not args.inputs:
        return interactive()
    if pymupdf is None:
        print("PyMuPDF is required:  pip install pymupdf", file=sys.stderr)
        return 2
    return execute(args)[0]


if __name__ == "__main__":
    sys.exit(main())
