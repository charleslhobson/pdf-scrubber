"""One test per bug found on real documents, so none of them can come back."""
import pymupdf

import builders
from conftest import needs_ocr, scrub, text_of, write_terms


def page_with(folder, name, lines, font="helv"):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((60, y), line, fontsize=11, fontname=font)
        y += 18
    path = folder / name
    doc.save(path)
    return path


def scrubbed_text(tmp_path, lines, *flags, terms=None):
    path = page_with(tmp_path, "page.pdf", lines)
    _, results = scrub([path], tmp_path / "out", *flags, terms=terms)
    result = results["page.pdf"]
    assert result.status == "ok", result.notes
    return text_of(tmp_path / "out" / result.output)


def test_everyday_tax_words_are_not_mistaken_for_names(tmp_path):
    """'Type of partner: Foreign' once made the tool blank the word Foreign in every document."""
    text = scrubbed_text(tmp_path, ["Type of partner: Foreign", "Owner: Individual", "Partner: Limited Partner",
                                    "Line 21  Foreign taxes paid or accrued   1,234", "Foreign tax credit   771"])
    for phrase in ("Foreign taxes paid", "Foreign tax credit", "Individual", "Limited Partner", "1,234", "771"):
        assert phrase in text


def test_a_word_learned_in_one_file_does_not_fail_the_next(tmp_path):
    """A value seen in one document must not make later documents fail the leak check."""
    first = page_with(tmp_path, "a.pdf", ["Type of partner: Foreign", "Member: Domestic"])
    second = page_with(tmp_path, "b.pdf", ["Foreign transactions - see Schedule K-3", "Gross income  48,250"])
    _, results = scrub([first, second], tmp_path / "out")
    assert [r.status for r in results.values()] == ["ok", "ok"]


def test_state_code_and_amount_is_not_an_address(tmp_path):
    """'NY 85000.00' is state wages. It was once taken for a ZIP code and removed."""
    text = scrubbed_text(tmp_path, ["State wages   NY 85000.00   CA 12345.67", "Total IN 12345   Wages NY 85000",
                                    "Allocated OR 20000   Income ME 31000"])
    for amount in ("85000.00", "12345.67", "12345", "85000", "20000", "31000"):
        assert amount in text


def test_a_real_address_still_loses_city_and_zip_but_keeps_the_state(tmp_path):
    text = scrubbed_text(tmp_path, ["Mail to: 400 Summit Avenue, Portland OR 97998", "Springfield, IL 62999-1234"])
    assert "Portland" not in text and "97998" not in text and "Springfield" not in text and "62999" not in text
    assert "Summit Avenue" not in text
    import re
    assert re.search(r"\bOR\b", text) and re.search(r"\bIL\b", text)


def test_birthday_checkbox_wording_is_kept_but_a_birth_date_is_not(tmp_path):
    text = scrubbed_text(tmp_path, ["Spouse: Was born before January 2, 1961      Are blind",
                                    "Date of birth: 03/14/1971"])
    assert "Was born before January 2, 1961" in text
    assert "03/14/1971" not in text


def test_rows_of_numbers_are_not_phone_numbers(tmp_path):
    text = scrubbed_text(tmp_path, ["Units 100 200 3000   45 678", "Basis 1234567   250000",
                                    "Call (541) 555-0142 or 541-555-0199"])
    assert "100 200 3000" in text and "1234567" in text and "250000" in text
    assert "555-0142" not in text and "555-0199" not in text


def test_security_identifiers_are_kept_but_bare_ssns_are_not(tmp_path):
    code = builders.cusip("03783310")
    text = scrubbed_text(tmp_path, ["Description              CUSIP         Proceeds",
                                    f"NORTHWIND INDUSTRIES     {code}     12,480.00",
                                    "Taxpayer identification number 900124087"])
    assert code in text and "12,480.00" in text
    assert "900124087" not in text


def test_matches_of_the_names_list_are_not_learned_again(tmp_path):
    """'Stonebridge & Vance' on the list matched inside an e-mail address and was learned as a new name."""
    path = page_with(tmp_path, "p.pdf", ["Contact philippa@stonebridgevance.example", "Stonebridge & Vance CPAs"])
    session, _ = scrub([path], tmp_path / "out", terms=write_terms(tmp_path, ["entity: Stonebridge & Vance"]))
    assert "stonebridgevance" not in [r["phrase"].lower() for r in session.terms.learned]


def test_label_words_are_not_learned_as_names_from_unruled_layouts(tmp_path):
    """'Last name  Feldspar  Social Security no.' once taught the tool that Social and Security are names."""
    path = page_with(tmp_path, "state.pdf", ["First name and initial  Marcus T      Last name  Feldspar      "
                                             "Social Security no. (SSN)  900-12-4087",
                                             "Current mailing address  4821 Quarry Ridge Road"])
    other = page_with(tmp_path, "other.pdf", ["Your social security number is on file.",
                                              "Social security benefits  21,400   Current year  2025"])
    session, results = scrub([path, other], tmp_path / "out")
    learned = " ".join(r["phrase"].lower() for r in session.terms.learned)
    assert "social" not in learned and "current" not in learned
    text = text_of(tmp_path / "out" / results["other.pdf"].output)
    assert "Social security benefits" in text and "21,400" in text


@needs_ocr
def test_a_label_at_the_page_edge_does_not_fail_the_self_check(tmp_path):
    """On a scan cropped to the edge, the tag drawn on a box lost its first letters and the file was refused."""
    form = builders.irs_1040(tmp_path)
    scan = builders.scan_of(form.path, tmp_path, "edge_scan", crop_left=34)
    _, results = scrub([scan.path], tmp_path / "out")
    assert results[scan.path.name].status == "ok", results[scan.path.name].notes
