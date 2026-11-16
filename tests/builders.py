"""Made-up tax documents for the tests.

Every person, business, address and number here is invented. The ID numbers are
in ranges that are never issued (SSNs starting 900 with a non-ITIN group, EINs
starting 00), phone numbers use the reserved 555-01xx block and e-mail addresses
use the reserved .example domain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

import scrub_pdfs as core

FORMS = Path(__file__).resolve().parent / "forms"

P = {
    "first": "Marcus T", "last": "Feldspar", "full": "Marcus T Feldspar",
    "spouse_first": "Odalys", "spouse": "Odalys Feldspar", "child": "Wrenna",
    "ssn": "900-12-4087", "ssn_bare": "900124087", "spouse_ssn": "900-34-1198", "spouse_ssn_bare": "900341198",
    "street": "4821 Quarry Ridge Road", "city": "Bellhaven", "state": "OR", "zip": "97999",
    "phone_bare": "5415550142", "phone": "(541) 555-0142", "email": "marcus.feldspar@quarrymail.example",
    "routing": "000456780", "account": "00098765432155", "ip_pin": "731946", "spouse_pin": "268503",
    "fund": "Tidewater Granite Partners LP", "fund_ein": "00-3317265",
    "fund_street": "900 Harbor Point Blvd Suite 400", "fund_city": "Portland", "fund_zip": "97998",
    "preparer": "Philippa Stonebridge", "ptin": "P01234567", "firm": "Stonebridge & Vance CPAs LLP",
    "firm_ein": "00-1122334", "firm_phone": "(541) 555-0199", "firm_street": "77 Ledger Lane",
    "designee": "Ignatius Calloway", "designee_phone": "5415550188",
    "broker": "Granite Peak Securities LLC", "broker_ein": "00-9988776", "broker_acct": "4471-88213",
    "dob": "03/14/1971",
}

# The names list a careful person would type in. Numbers and addresses are found without it.
TERMS = ["name: Marcus T Feldspar", "name: Odalys Feldspar", "name: Wrenna Feldspar",
         "entity: Tidewater Granite Partners", "entity: Stonebridge & Vance", "entity: Granite Peak Securities",
         "name: Philippa Stonebridge", "name: Ignatius Calloway", "place: Bellhaven"]

# Things that must never be readable in any output, whatever the document.
ALWAYS_GONE = [P["ssn"], P["ssn_bare"], P["spouse_ssn"], P["spouse_ssn_bare"], P["fund_ein"], P["firm_ein"],
               P["broker_ein"], P["routing"], P["account"], P["phone_bare"], P["email"], P["ptin"],
               "555-0142", "555-0199", P["designee_phone"], P["broker_acct"]]
NAMES_GONE = ["Feldspar", "Marcus", "Odalys", "Wrenna", "Tidewater", "Stonebridge", "Philippa", "Calloway",
              "Ignatius", "Bellhaven", "Quarry Ridge", "Ledger Lane", "Harbor Point", "Granite Peak"]


@dataclass
class Doc:
    path: Path
    gone: list = field(default_factory=list)        # strings that must not survive
    kept: list = field(default_factory=list)        # strings (amounts, wording) that must survive
    scanned: bool = False


def cusip(first8: str) -> str:
    return next(first8 + d for d in "0123456789" if core.cusip_valid(first8 + d))


def _fill(doc, values: dict, amounts=None) -> list[str]:
    planted, n = [], 0
    for page in doc:
        for w in page.widgets():
            if w.field_type_string != "Text":
                continue
            key = w.field_name.split(".")[-1]
            if key in values:
                w.field_value = values[key]
                w.update()
            elif amounts and amounts(page.number, w):
                n += 1
                value = f"{(n * 7919) % 90000 + 1000:,}"
                w.field_value = value
                w.update()
                planted.append(value)
    return planted


def put(page, x, y, text, size=9, bold=False):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv")


def box(page, x0, y0, x1, y1):
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0, 0, 0), width=0.6)


# ---- IRS forms -------------------------------------------------------------

def irs_1040(folder: Path) -> Doc:
    values = {
        "f1_14[0]": P["first"], "f1_15[0]": P["last"], "f1_16[0]": P["ssn_bare"],
        "f1_17[0]": P["spouse_first"], "f1_18[0]": P["last"], "f1_19[0]": P["spouse_ssn_bare"],
        "f1_20[0]": P["street"], "f1_21[0]": "2B", "f1_22[0]": P["city"], "f1_23[0]": P["state"],
        "f1_24[0]": P["zip"], "f1_31[0]": P["child"], "f1_35[0]": P["last"],
        "f2_32[0]": P["routing"], "f2_33[0]": P["account"], "f2_37[0]": P["designee"],
        "f2_38[0]": P["designee_phone"], "f2_39[0]": "48213", "f2_40[0]": "Geologist",
        "f2_41[0]": P["ip_pin"], "f2_42[0]": "Nurse", "f2_43[0]": P["spouse_pin"],
        "f2_44[0]": P["phone_bare"], "f2_45[0]": P["email"], "f2_46[0]": P["preparer"],
        "f2_47[0]": P["ptin"], "f2_48[0]": P["firm"], "f2_49[0]": P["firm_phone"],
        "f2_50[0]": f"{P['firm_street']}, {P['city']}, {P['state']} {P['zip']}", "f2_51[0]": P["firm_ein"],
    }
    doc = pymupdf.open(FORMS / "f1040.pdf")
    kept = _fill(doc, values, lambda pn, w: w.rect.x0 > 400 and w.rect.width > 60
                 and (pn == 1 or w.rect.y0 > 340) and w.text_maxlen == 0)
    doc.set_metadata({"author": P["full"], "title": f"{P['last']} 2025 return"})
    doc.set_toc([[1, f"{P['last']} 2025 Form 1040", 1]])
    path = folder / f"{P['last']} 2025 Form 1040.pdf"
    doc.save(path)
    return Doc(path, ALWAYS_GONE + NAMES_GONE + [P["ip_pin"], P["spouse_pin"]], kept + ["Geologist", "Nurse"])


def irs_k1(folder: Path, flatten: bool = True, name: str = "K-1 Tidewater") -> Doc:
    values = {
        "f1_6[0]": P["fund_ein"],
        "f1_7[0]": f"{P['fund']}\n{P['fund_street']}\n{P['fund_city']}, OR {P['fund_zip']}",
        "f1_8[0]": "Ogden", "f1_9[0]": P["ssn"],
        "f1_10[0]": f"{P['full']}\n{P['street']}\n{P['city']}, {P['state']} {P['zip']}",
        "f1_14[0]": "2.5", "f1_15[0]": "2.5",
    }
    doc = pymupdf.open(FORMS / "f1065sk1.pdf")
    kept = _fill(doc, values, lambda pn, w: w.rect.x0 > 320 and w.rect.width > 40)
    if flatten:
        doc.bake()
    doc.set_metadata({"author": P["full"], "subject": P["ssn"]})
    path = folder / f"{name}.pdf"
    doc.save(path)
    return Doc(path, ALWAYS_GONE + NAMES_GONE + [P["fund_zip"]], kept + ["Ogden"])


def blank_form_filled_with_amounts(folder: Path, form: str) -> Doc:
    """Every field holds a dollar amount: shows how much the name/ID boxes cost, and nothing else."""
    doc = pymupdf.open(FORMS / f"{form}.pdf")
    kept = _fill(doc, {}, lambda pn, w: True)
    path = folder / f"{form}_amounts.pdf"
    doc.save(path)
    return Doc(path, [], kept)


# ---- documents in other layouts ---------------------------------------------

def software_return(folder: Path) -> Doc:
    """A return as printed by tax software: its own layout, labels beside values, worksheets."""
    doc = pymupdf.open()
    kept = []

    page = doc.new_page()
    put(page, 40, 44, "Form 1040   U.S. Individual Income Tax Return   2025", 12, True)
    box(page, 36, 54, 300, 82); box(page, 300, 54, 450, 82); box(page, 450, 54, 576, 82)
    put(page, 40, 63, "Your first name and middle initial", 7); put(page, 40, 77, P["first"].upper(), 10)
    put(page, 304, 63, "Last name", 7); put(page, 304, 77, P["last"].upper(), 10)
    put(page, 454, 63, "Your social security number", 7); put(page, 454, 77, P["ssn"], 10)
    box(page, 36, 82, 300, 110); box(page, 300, 82, 450, 110); box(page, 450, 82, 576, 110)
    put(page, 40, 91, "If joint return, spouse's first name and middle initial", 7)
    put(page, 40, 105, P["spouse_first"].upper(), 10)
    put(page, 304, 91, "Last name", 7); put(page, 304, 105, P["last"].upper(), 10)
    put(page, 454, 91, "Spouse's social security number", 7); put(page, 454, 105, P["spouse_ssn"], 10)
    box(page, 36, 110, 576, 138)
    put(page, 40, 119, "Home address (number and street). If you have a P.O. box, see instructions.", 7)
    put(page, 40, 133, P["street"].upper(), 10)
    box(page, 36, 138, 380, 166); box(page, 380, 138, 450, 166); box(page, 450, 138, 576, 166)
    put(page, 40, 147, "City, town, or post office", 7); put(page, 40, 161, P["city"].upper(), 10)
    put(page, 384, 147, "State", 7); put(page, 384, 161, P["state"], 10)
    put(page, 454, 147, "ZIP code", 7); put(page, 454, 161, P["zip"], 10)
    y = 196
    for line, text, amount in [("1a", "Total amount from Form(s) W-2, box 1", "185,400."),
                               ("2b", "Taxable interest", "3,212."), ("3a", "Qualified dividends", "9,871."),
                               ("7", "Capital gain or (loss)", "(3,000.)"), ("8", "Additional income from Schedule 1", "48,250."),
                               ("11", "Adjusted gross income", "243,733."), ("12", "Standard deduction", "30,000."),
                               ("15", "Taxable income", "213,733."), ("24", "Total tax", "38,917."),
                               ("33", "Total payments", "41,200."), ("34", "Overpaid", "2,283.")]:
        put(page, 40, y, line); put(page, 70, y, text); put(page, 500, y, amount)
        kept.append(amount)
        y += 16
    put(page, 40, y + 14, f"Routing number {P['routing']}     Account number {P['account']}")
    put(page, 40, y + 30, f"Phone no. {P['phone']}     Email address {P['email']}")
    put(page, 40, y + 46, f"Identity Protection PIN {P['ip_pin']}")

    page = doc.new_page()
    put(page, 40, 44, "SCHEDULE D   Capital Gains and Losses   2025", 12, True)
    put(page, 40, 64, f"Name(s) shown on return   {P['first'].upper()} & {P['spouse'].upper()}")
    put(page, 400, 64, f"Your social security number   {P['ssn']}")
    y = 96
    for text, a, b in [("1a Short-term totals from Form 8949", "12,480.", "11,902."),
                       ("6  Short-term capital loss carryover", "(4,115.)", ""),
                       ("8a Long-term totals from Form 8949", "88,300.", "61,744."),
                       ("14 Long-term capital loss carryover", "(22,907.)", ""),
                       ("16 Combine lines 7 and 15", "(3,000.)", ""),
                       ("   Unrecaptured section 1250 gain", "456.", ""),
                       ("   28% rate gain", "0.", "")]:
        put(page, 40, y, text); put(page, 400, y, a); put(page, 490, y, b)
        kept += [v for v in (a, b) if v and v != "0."]
        y += 16

    page = doc.new_page()
    put(page, 40, 44, "Federal Carryover Worksheet   2025", 12, True)
    put(page, 40, 62, f"Taxpayer: {P['full']}        SSN: XXX-XX-4087")
    y = 92
    for text, a, b in [("Passive loss carryover (Form 8582)", "14,221", "9,650"),
                       ("Section 179 carryover", "28,900", "0"),
                       ("AMT credit carryforward", "1,205", "1,205"),
                       ("Charitable contribution carryover", "6,000", "2,500"),
                       ("Net operating loss", "0", "0"),
                       ("QBI loss carryforward", "(7,318)", "(7,318)")]:
        put(page, 40, y, text); put(page, 380, y, a); put(page, 480, y, b)
        kept += [v for v in (a, b) if v != "0"]
        y += 16
    put(page, 40, y + 20, "Paid Preparer Use Only", 10, True)
    put(page, 40, y + 36, f"Preparer's name  {P['preparer']}      PTIN  {P['ptin']}")
    put(page, 40, y + 52, f"Firm's name  {P['firm']}      Firm's EIN  {P['firm_ein']}")
    put(page, 40, y + 68, f"Firm's address  {P['firm_street']}, {P['city']}, {P['state']} {P['zip']}      Phone no. {P['firm_phone']}")
    doc.set_metadata({"author": P["preparer"], "title": f"{P['last']} tax return"})
    path = folder / f"2025 Tax Return - {P['first']} and {P['spouse']}.pdf"
    doc.save(path)
    kept += ["Your social security number", "Spouse's social security number", "Name(s) shown on return",
             "Home address", "Paid Preparer Use Only", "Federal Carryover Worksheet"]
    return Doc(path, ALWAYS_GONE + NAMES_GONE + [P["ip_pin"], "FELDSPAR", "ODALYS", "XXX-XX-4087"], kept)


def fund_k1_package(folder: Path) -> Doc:
    """What a fund administrator sends: letter, K-1, footnotes, state schedule, foreign page."""
    kept = []
    doc = pymupdf.open()
    header = f"{P['fund']}   EIN {P['fund_ein']}   Partner: {P['full']}"

    page = doc.new_page()
    y = 72
    for line in [P["fund"], P["fund_street"], f"{P['fund_city']}, OR {P['fund_zip']}", "",
                 f"Dear {P['first'].split()[0]},", "",
                 f"Enclosed is your 2025 Schedule K-1 from {P['fund']}.",
                 f"Partner: {P['full']}", "Partner ID: 100482      Investor account number: TGP-00417",
                 f"Taxpayer ID: {P['ssn']}        Date of birth: {P['dob']}", "",
                 "Please give this package to your tax adviser.", f"Questions: {P['firm_phone']}"]:
        put(page, 72, y, line, 10)
        y += 16

    k1 = pymupdf.open(irs_k1(folder, flatten=True, name="_k1_inner").path)
    doc.insert_pdf(k1)
    k1.close()
    (folder / "_k1_inner.pdf").unlink()

    page = doc.new_page()
    put(page, 40, 40, header, 8)
    put(page, 40, 64, "Supplemental Information - Footnotes to Schedule K-1", 11, True)
    y = 90
    for text, amount in [("Line 20, code Z - Section 199A qualified business income", "48,250"),
                         ("Line 20, code Z - W-2 wages", "112,400"),
                         ("Line 20, code Z - UBIA of qualified property", "1,250,000"),
                         ("Line 20, code AH - Excess business interest expense", "1,112"),
                         ("Line 13, code W - Portfolio deductions", "2,044"),
                         ("Line 21 - Foreign taxes paid or accrued", "1,234"),
                         ("Capital account - beginning", "215,000"),
                         ("Capital account - contributions", "25,000"),
                         ("Capital account - ending", "262,480")]:
        put(page, 40, y, text); put(page, 480, y, amount)
        kept.append(amount)
        y += 16
    put(page, 40, y + 12, "H1  Domestic partner      Foreign partner")
    put(page, 40, y + 28, "Partner: Limited Partner      Type of partner: Individual")
    kept += ["Foreign partner", "Limited Partner", "Foreign taxes paid or accrued"]

    page = doc.new_page()
    put(page, 40, 40, header, 8)
    put(page, 40, 64, "State Schedule - Partner's share of income by state", 11, True)
    put(page, 40, 86, "State      Apportioned income      Withholding")
    y = 104
    for state, income, tax in [("CA", "12,345", "310"), ("NY", "85000.00", "0"), ("IN", "12345", "55"),
                               ("OR", "20000", "0"), ("ME", "31000", "12"), ("PA", "7,719", "237")]:
        put(page, 40, y, state); put(page, 110, y, income); put(page, 260, y, tax)
        kept.append(income)
        y += 16
    put(page, 40, y + 12, "Total IN 12345      Wages NY 85000      Allocated OR 20000")

    page = doc.new_page()
    put(page, 40, 40, header, 8)
    put(page, 40, 64, "Schedule K-3 - Partner's Share of Income, Deductions, Credits - International", 11, True)
    y = 90
    for text, amount in [("Foreign source gross income - passive category", "4,410"),
                         ("Foreign source gross income - general category", "9,087"),
                         ("Foreign taxes paid - passive category", "1,234"),
                         ("Foreign tax credit limitation", "771")]:
        put(page, 40, y, text); put(page, 480, y, amount)
        kept.append(amount)
        y += 16
    kept.append("Foreign tax credit limitation")
    doc.set_metadata({"author": P["fund"], "keywords": P["full"]})
    path = folder / f"K-1s - {P['fund'].upper()} ({P['full'].upper()}).pdf"
    doc.save(path)
    return Doc(path, ALWAYS_GONE + NAMES_GONE + [P["dob"], "TGP-00417", P["fund_zip"]], kept)


def brokerage_1099(folder: Path) -> Doc:
    """A consolidated 1099: payer and recipient blocks, account number, a table with CUSIPs."""
    kept = []
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 40, 44, "2025 Consolidated Form 1099   Tax Reporting Statement", 12, True)
    box(page, 36, 56, 300, 130); box(page, 300, 56, 576, 130)
    put(page, 40, 66, "PAYER'S name, street address, city or town, state or province, country, ZIP", 6.5)
    put(page, 40, 82, P["broker"], 10); put(page, 40, 96, "400 Summit Avenue", 10)
    put(page, 40, 110, f"{P['fund_city']}, OR {P['fund_zip']}", 10)
    put(page, 304, 66, "RECIPIENT'S name, street address, city or town, state or province", 6.5)
    put(page, 304, 82, P["full"].upper(), 10); put(page, 304, 96, P["street"].upper(), 10)
    put(page, 304, 110, f"{P['city'].upper()} {P['state']} {P['zip']}", 10)
    put(page, 40, 146, f"PAYER'S TIN: {P['broker_ein']}      RECIPIENT'S TIN: XXX-XX-4087      "
                       f"Account number: {P['broker_acct']}")
    put(page, 40, 174, "Form 1099-DIV   Dividends and Distributions", 10, True)
    y = 192
    for text, amount in [("1a Total ordinary dividends", "9,871.24"), ("1b Qualified dividends", "8,402.10"),
                         ("2a Total capital gain distributions", "1,330.00"), ("5  Section 199A dividends", "212.45"),
                         ("7  Foreign tax paid", "96.18"), ("12 Exempt-interest dividends", "0.00")]:
        put(page, 40, y, text); put(page, 480, y, amount)
        if amount != "0.00":
            kept.append(amount)
        y += 15
    put(page, 40, y + 14, "Form 1099-B   Proceeds from Broker Transactions", 10, True)
    y += 32
    put(page, 40, y, "Description"); put(page, 230, y, "CUSIP"); put(page, 310, y, "Date sold")
    put(page, 390, y, "Proceeds"); put(page, 480, y, "Cost basis")
    y += 15
    for name, first8, sold, proceeds, basis in [
            ("NORTHWIND INDUSTRIES INC", "03783310", "03/14/2025", "12,480.00", "9,112.50"),
            ("SUMMIT TOTAL MARKET ETF", "92290876", "06/02/2025", "40,118.75", "33,006.20"),
            ("HARBORLIGHT MUNI BOND FD", "46428728", "11/21/2025", "8,250.00", "8,611.39")]:
        code = cusip(first8)
        put(page, 40, y, name); put(page, 230, y, code); put(page, 310, y, sold)
        put(page, 390, y, proceeds); put(page, 480, y, basis)
        kept += [code, proceeds, basis, sold, name]
        y += 15
    put(page, 40, 760, f"Account {P['broker_acct']}      {P['full']}      Page 1 of 1", 8)
    doc.set_metadata({"author": P["broker"], "title": f"1099 {P['full']}"})
    path = folder / f"1099 - {P['broker']} - {P['last']}.pdf"
    doc.save(path)
    return Doc(path, [P["broker_ein"], P["broker_acct"], "XXX-XX-4087", P["fund_zip"], "Summit Avenue"]
               + NAMES_GONE + ["FELDSPAR", "QUARRY RIDGE"], kept)


def state_return(folder: Path) -> Doc:
    kept = []
    doc = pymupdf.open()
    page = doc.new_page()
    put(page, 40, 44, "Oregon Individual Income Tax Return   Form OR-40   2025", 12, True)
    put(page, 40, 70, f"First name and initial  {P['first']}      Last name  {P['last']}      "
                      f"Social Security no. (SSN)  {P['ssn']}")
    put(page, 40, 86, f"Spouse's first name and initial  {P['spouse_first']}      Spouse's last name  {P['last']}      "
                      f"Spouse's SSN  {P['spouse_ssn']}")
    put(page, 40, 102, f"Current mailing address  {P['street']}")
    put(page, 40, 118, f"{P['city']}, {P['state']} {P['zip']}      Phone  {P['phone']}")
    put(page, 40, 134, f"Date of birth (mm/dd/yyyy)  {P['dob']}")
    y = 166
    for line, text, amount in [("7", "Federal adjusted gross income", "243,733"), ("19", "Oregon taxable income", "198,405"),
                               ("20", "Tax", "17,362"), ("32", "Oregon income tax withheld", "16,900"),
                               ("42", "Tax to pay", "462"), ("", "State wages   OR 185400.00", "")]:
        put(page, 40, y, line); put(page, 70, y, text); put(page, 480, y, amount)
        if amount:
            kept.append(amount)
        y += 16
    kept += ["185400.00", "Current mailing address", "Oregon taxable income"]
    path = folder / f"OR-40 {P['last']}.pdf"
    doc.save(path)
    return Doc(path, ALWAYS_GONE[:4] + NAMES_GONE + [P["dob"], "555-0142"], kept)


def wording_page(folder: Path) -> Doc:
    """Ordinary tax wording and number formats that must come through untouched."""
    doc = pymupdf.open()
    page = doc.new_page()
    rows = ["1  Wages 85000.00   82,500.00", "2  Interest 1,234.56   (987.65)",
            "3  Capital loss carryover (12,345)   -3,000", "4  Basis 1234567   250000",
            "5  Rate 37.0%   23.8%   3.8%", "6  Dates 01/15/2025   12/31/2025   2025-04-15",
            "7  Units 100 200 3000   45 678", "8  Tax years 2021 2022 2023 2024 2025",
            "9  State wages NY 85000.00   CA 12345.67", "10 Total IN 12345   Wages NY 85000",
            "11 Line 21  Foreign taxes paid or accrued   1,234", "12 Partner: Limited Partner   Owner: Individual",
            "13 Was born before January 2, 1961      Are blind", "14 Shares 1,000.000 @ 42.1875 = 42,187.50",
            "15 Section 1231 gain 3,120   Unrecaptured 1250 gain 456"]
    y = 60
    for row in rows:
        page.insert_text((50, y), row, fontsize=10, fontname="cour")
        y += 16
    path = folder / "wording.pdf"
    doc.save(path)
    return Doc(path, [], rows)


def scan_of(source: Path, folder: Path, name: str, dpi: int = 170, quality: int = 55, crop_left: float = 0) -> Doc:
    """A picture-only copy, as a scanner or phone would make."""
    src = pymupdf.open(source)
    try:
        src.bake()
    except Exception:
        pass
    out = pymupdf.open()
    for page in src:
        clip = pymupdf.Rect(crop_left, 0, page.rect.width, page.rect.height)
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=clip)
        new = out.new_page(width=clip.width, height=clip.height)
        new.insert_image(new.rect, stream=pix.tobytes("jpg", jpg_quality=quality))
    path = folder / f"{name}.pdf"
    out.save(path)
    return Doc(path, scanned=True)
