"""Scanned pages: the output is a picture, so the check is what can still be read off that picture."""
import pymupdf
import pytest

import builders
from conftest import leaks, needs_ocr, ocr_of, scrub, write_terms

pytestmark = needs_ocr

# What matters most on a scan, including digits printed one per box that OCR struggles with.
SCAN_GONE = ["Feldspar", "Marcus", "Odalys", "Tidewater", "Stonebridge", "Calloway", "Quarry", "Bellhaven",
             "900-12-4087", "900124087", "900341198", "00-3317265", "5415550142", "quarrymail", "P01234567"]


@pytest.fixture(scope="module")
def scans(tmp_path_factory):
    src = tmp_path_factory.mktemp("scan_src")
    out = tmp_path_factory.mktemp("scan_out")
    k1 = builders.scan_of(builders.irs_k1(src).path, src, "scan_k1")
    f1040 = builders.scan_of(builders.irs_1040(src).path, src, "scan_1040")
    _, with_names = scrub([k1.path, f1040.path], out, terms=write_terms(src))
    out_bare = tmp_path_factory.mktemp("scan_out_bare")
    _, without = scrub([k1.path, f1040.path], out_bare)
    return {"out": out, "with": with_names, "out_bare": out_bare, "without": without}


@pytest.mark.parametrize("name", ["scan_k1.pdf", "scan_1040.pdf"])
def test_scan_is_flattened_and_nothing_can_be_read_off_it(scans, name):
    result = scans["with"][name]
    assert result.status == "ok", result.notes
    assert result.raster_pages == result.pages
    found = leaks(scans["out"], result, SCAN_GONE, ocr=True)
    assert not found, found


@pytest.mark.parametrize("name", ["scan_k1.pdf", "scan_1040.pdf"])
def test_scan_is_clean_even_without_a_names_list(scans, name):
    """The form boxes are found from their labels and blacked out whole."""
    result = scans["without"][name]
    assert result.status == "ok", result.notes
    found = leaks(scans["out_bare"], result, SCAN_GONE, ocr=True)
    assert not found, found


def test_boxed_digits_are_blacked_out_on_a_scan(scans):
    """Routing, account and PIN digits sit one per box, which OCR cannot read - the boxes go as a whole."""
    result = scans["without"]["scan_1040.pdf"]
    read = ocr_of(scans["out_bare"] / result.output)
    digits = "".join(ch for ch in read if ch.isdigit())
    for secret in (builders.P["account"], builders.P["routing"], builders.P["ip_pin"], builders.P["spouse_pin"]):
        assert secret not in digits


def test_amounts_on_a_scan_are_still_readable(scans):
    read = ocr_of(scans["out"] / scans["with"]["scan_k1.pdf"].output)
    amounts = [a for a in ("8,919", "16,838", "24,757", "32,676", "40,595", "48,514", "56,433", "64,352") if a in read]
    assert len(amounts) >= 6, amounts


def test_flattened_pages_have_no_hidden_text(scans):
    result = scans["with"]["scan_k1.pdf"]
    with pymupdf.open(scans["out"] / result.output) as doc:
        words = [w[4] for page in doc for w in page.get_text("words")]
    assert all(w.startswith("[") or w.endswith("]") for w in words), words     # only the tags drawn on boxes


def test_a_hidden_text_layer_under_a_scan_is_removed(tmp_path):
    """Scanners often add an invisible text layer. It must not ride along under the picture."""
    form = builders.irs_k1(tmp_path)
    scan = builders.scan_of(form.path, tmp_path, "scan_hidden")
    with pymupdf.open(scan.path) as doc:
        doc[0].insert_text((40, 300), "Marcus T Feldspar 900-12-4087", fontsize=9, render_mode=3)
        doc.save(tmp_path / "hidden.pdf")
    _, results = scrub([tmp_path / "hidden.pdf"], tmp_path / "out")
    result = results["hidden.pdf"]
    assert result.status == "ok", result.notes
    assert not leaks(tmp_path / "out", result, ["Feldspar", "900-12-4087"])
