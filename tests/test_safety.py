"""The promises that matter most: originals untouched, hidden data stripped, unsure means not written."""
import hashlib
import json

import pymupdf

import builders
from conftest import leaks, raw_of, scrub, text_of, write_terms


def test_originals_are_never_modified(tmp_path):
    doc = builders.irs_k1(tmp_path, flatten=False)
    before = hashlib.sha256(doc.path.read_bytes()).hexdigest()
    scrub([doc.path], tmp_path / "out", terms=write_terms(tmp_path))
    assert hashlib.sha256(doc.path.read_bytes()).hexdigest() == before


def test_hidden_document_data_is_stripped(tmp_path):
    src = builders.irs_1040(tmp_path)                      # carries author, title and a bookmark
    with pymupdf.open(src.path) as doc:
        doc.embfile_add("notes.txt", b"Marcus Feldspar SSN 900-12-4087")
        doc.save(tmp_path / "with_attachment.pdf")
    _, results = scrub([tmp_path / "with_attachment.pdf"], tmp_path / "out", terms=write_terms(tmp_path))
    result = results["with_attachment.pdf"]
    assert result.status == "ok", result.notes
    with pymupdf.open(tmp_path / "out" / result.output) as out:
        assert not any(v for k, v in (out.metadata or {}).items() if v and k not in ("format", "encryption"))
        assert out.get_toc() == [] and out.embfile_count() == 0
        assert sum(len(list(page.widgets())) for page in out) == 0
    assert "Feldspar" not in raw_of(tmp_path / "out" / result.output)


def test_a_file_that_cannot_be_verified_is_not_written(tmp_path):
    """An SSN parked outside the visible page cannot be reached, so the whole file is held back."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Taxpayer SSN 900-12-4087   Wages 85,000", fontsize=11)
    page.insert_text((-400, 100), "900-12-4087", fontsize=11)
    path = tmp_path / "offpage.pdf"
    doc.save(path)
    session, results = scrub([path], tmp_path / "out")
    result = results["offpage.pdf"]
    assert result.status == "FAILED" and result.output == ""
    assert not list((tmp_path / "out").glob("*.redacted.pdf"))
    what, todo = __import__("scrub_pdfs").explain(result.status, result.notes)
    assert "Held back" in what and "Flatten" in todo


def test_flattening_rescues_a_held_back_file(tmp_path):
    from conftest import TESSERACT
    if not TESSERACT:
        import pytest
        pytest.skip("Tesseract is not installed")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Taxpayer SSN 900-12-4087   Wages 85,000", fontsize=11)
    page.insert_text((-400, 100), "900-12-4087", fontsize=11)
    path = tmp_path / "offpage.pdf"
    doc.save(path)
    session, results = scrub([path], tmp_path / "out")
    result = session.scrub(path, {"rasterize": True})
    assert result.status == "ok", result.notes
    assert "900-12-4087" not in raw_of(tmp_path / "out" / result.output)


def test_locked_pdf_needs_its_password(tmp_path):
    plain = builders.state_return(tmp_path)
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(plain.path) as doc:
        doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="open sesame", owner_pw="owner")
    _, results = scrub([locked], tmp_path / "out1", terms=write_terms(tmp_path))
    assert results["locked.pdf"].status == "skipped" and "password" in results["locked.pdf"].notes[0]
    _, results = scrub([locked], tmp_path / "out2", "--password", "wrong", "--password", "open sesame",
                       terms=write_terms(tmp_path))
    assert results["locked.pdf"].status == "ok"
    assert not leaks(tmp_path / "out2", results["locked.pdf"], plain.gone)


def test_ids_are_removed_even_without_a_names_list(tmp_path):
    doc = builders.software_return(tmp_path)
    _, results = scrub([doc.path], tmp_path / "out")
    found = leaks(tmp_path / "out", results[doc.path.name], builders.ALWAYS_GONE)
    assert not found, found


def test_scanned_pages_are_left_out_when_they_cannot_be_read(tmp_path):
    form = builders.irs_k1(tmp_path)
    scan = builders.scan_of(form.path, tmp_path, "scan")
    _, results = scrub([scan.path], tmp_path / "out", "--no-ocr")
    result = results["scan.pdf"]
    assert result.status == "partial" and result.withheld_pages == [1]
    assert "WITHHELD" in text_of(tmp_path / "out" / result.output)
    with pymupdf.open(tmp_path / "out" / result.output) as out:
        assert not out[0].get_images()                     # the unread page image is gone, not passed through


def test_report_and_private_files_stay_apart(tmp_path):
    import os
    from pathlib import Path
    doc = builders.state_return(tmp_path)
    scrub([doc.path], tmp_path / "out", terms=write_terms(tmp_path))
    report = json.loads((tmp_path / "out" / "scrub_report.json").read_text(encoding="utf-8"))
    assert report["files"][0]["status"] == "ok" and "Feldspar" not in json.dumps(report)
    private = Path(os.environ["PDF_SCRUBBER_HOME"])
    assert (private / "scrub_last_run.log").is_file()
    assert not list((tmp_path / "out").glob("*.log")) and not list((tmp_path / "out").glob("*names*"))


def test_a_copy_that_cannot_be_saved_is_reported_not_crashed(tmp_path):
    """On Windows a scrubbed PDF left open in a reader is locked. The tool must say so and carry on."""
    import scrub_pdfs as core
    doc = builders.state_return(tmp_path)
    out = tmp_path / "out"
    (out / "doc_001.redacted.pdf").mkdir(parents=True)       # something in the way that cannot be overwritten
    _, results = scrub([doc.path], out, terms=write_terms(tmp_path))
    result = results[doc.path.name]
    assert result.status == "FAILED" and result.output == ""
    what, todo = core.explain(result.status, result.notes)
    assert "Not saved" in what and "close it" in todo.lower()


def test_folders_are_searched_all_the_way_down_whatever_the_file_case(tmp_path):
    import scrub_pdfs as core
    deep = tmp_path / "2025" / "K-1s"
    deep.mkdir(parents=True)
    for name in ("a.pdf", "B.PDF", "notes.txt", "old.redacted.pdf"):
        (deep / name).write_bytes(b"%PDF-1.4")
    found = core.collect([str(tmp_path), str(tmp_path / "missing")])
    assert sorted(f.name.lower() for f in found) == ["a.pdf", "b.pdf"]
