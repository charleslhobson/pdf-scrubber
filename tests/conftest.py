"""Shared test setup: a throwaway private folder, the made-up corpus, and leak-checking helpers."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402

import builders  # noqa: E402
import scrub_pdfs as core  # noqa: E402

TESSERACT = core.find_tesseract()
needs_ocr = pytest.mark.skipif(not TESSERACT, reason="Tesseract is not installed")


@pytest.fixture(autouse=True, scope="session")
def private_home(tmp_path_factory):
    """Keep names lists, run logs and project memory out of the real user folder."""
    os.environ["PDF_SCRUBBER_HOME"] = str(tmp_path_factory.mktemp("private"))
    yield


def parse(*argv) -> object:
    return core.build_parser().parse_args([str(a) for a in argv])


def write_terms(folder: Path, lines=builders.TERMS) -> Path:
    path = folder / "names.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def scrub(files, out: Path, *flags, terms: Path | None = None, scan: bool = True):
    """Run both passes over `files`. Returns (session, {file name: Result})."""
    argv = ["x", "-o", out, *flags] + (["--terms", terms] if terms else ["--no-terms"])
    session = core.Session(parse(*argv))
    files = [Path(f) for f in files]
    if scan:
        session.scan(files)
    results = {f.name: session.scrub(f) for f in files}
    session.finish()
    return session, results


def text_of(pdf: Path) -> str:
    with pymupdf.open(pdf) as doc:
        return "\n".join(page.get_text() for page in doc)


def original_text(pdf: Path) -> str:
    with pymupdf.open(pdf) as doc:
        try:
            doc.bake()
        except Exception:
            pass
        return "\n".join(page.get_text() for page in doc)


def raw_of(pdf: Path) -> str:
    """Everything stored in the file outside image and font data, decompressed."""
    parts = []
    with pymupdf.open(pdf) as doc:
        for xref in range(1, doc.xref_length()):
            try:
                obj = doc.xref_object(xref)
                parts.append(obj)
                if "/Image" not in obj and "FontFile" not in obj:
                    parts.append((doc.xref_stream(xref) or b"").decode("latin-1"))
            except Exception:
                pass
    return "\n".join(parts)


def ocr_of(pdf: Path, dpi: int = 250) -> str:
    """What a person - or a vision model - could read off the rendered pages."""
    words = []
    with pymupdf.open(pdf) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            for psm in ("11", "3"):
                out = subprocess.run([TESSERACT, "stdin", "stdout", "--dpi", str(dpi), "--psm", psm],
                                     input=pix.tobytes("png"), capture_output=True).stdout
                words.append(out.decode("utf-8", "ignore"))
    return "\n".join(words)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def leaks(out_dir: Path, result, needles, ocr: bool = False) -> list[str]:
    """Where any needle can still be found in one scrubbed document."""
    assert result.output, f"{result.source} was not written: {result.notes}"
    pdf = out_dir / result.output
    sidecar = pdf.with_name(pdf.name.replace(".redacted.pdf", ".redacted.txt"))
    places = {"text": text_of(pdf), "raw data": raw_of(pdf), "file name": pdf.name,
              "text file": sidecar.read_text(encoding="utf-8") if sidecar.is_file() else ""}
    if ocr and TESSERACT:
        places["rendered page"] = ocr_of(pdf)
    found = []
    for needle in needles:
        for place, hay in places.items():
            if needle.lower() in hay.lower() or squash(needle) in squash(hay):
                found.append(f"{needle!r} in {place}")
    return found


def missing(out_dir: Path, result, doc: "builders.Doc") -> list[str]:
    """Kept items (amounts, wording) that appear fewer times after scrubbing than before."""
    before, after = original_text(doc.path), text_of(out_dir / result.output)
    return [k for k in doc.kept if after.count(k) < before.count(k)]


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    """Every text document, scrubbed together in one batch with a names list - as a person would."""
    src = tmp_path_factory.mktemp("originals")
    out = tmp_path_factory.mktemp("scrubbed")
    docs = {
        "irs_1040": builders.irs_1040(src),
        "irs_k1": builders.irs_k1(src),
        "irs_k1_fillable": builders.irs_k1(src, flatten=False, name="K-1 fillable"),
        "software_return": builders.software_return(src),
        "fund_k1_package": builders.fund_k1_package(src),
        "brokerage_1099": builders.brokerage_1099(src),
        "state_return": builders.state_return(src),
        "wording": builders.wording_page(src),
    }
    session, results = scrub([d.path for d in docs.values()], out, terms=write_terms(src))
    return {"docs": docs, "out": out, "session": session,
            "results": {key: results[doc.path.name] for key, doc in docs.items()}}
