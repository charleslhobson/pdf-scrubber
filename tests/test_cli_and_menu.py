"""The command line and the text menu, run the way a person runs them."""
import os
import subprocess
import sys
from pathlib import Path

import builders
from conftest import ROOT, text_of, write_terms

SCRIPT = str(ROOT / "scrub_pdfs.py")


def run(*argv, stdin=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, SCRIPT, *[str(a) for a in argv]], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, timeout=600)


def test_command_line_scrubs_a_folder(tmp_path):
    src = tmp_path / "taxes"
    src.mkdir()
    builders.state_return(src)
    builders.wording_page(src)
    done = run(src, "-o", tmp_path / "out", "--terms", write_terms(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Done: 2 clean" in done.stdout and "scrub_pdfs version" in done.stdout
    assert sorted(p.name for p in (tmp_path / "out").glob("*.redacted.pdf")) == ["doc_001.redacted.pdf",
                                                                                  "doc_002.redacted.pdf"]


def test_command_line_refuses_to_run_without_a_names_list(tmp_path):
    builders.state_return(tmp_path)
    done = run(tmp_path, "-o", tmp_path / "out")
    assert done.returncode == 2 and "--terms file is required" in done.stderr


def test_command_line_explains_a_locked_file(tmp_path):
    import pymupdf
    plain = builders.state_return(tmp_path)
    with pymupdf.open(plain.path) as doc:
        doc.save(tmp_path / "locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="o")
    done = run(tmp_path / "locked.pdf", "-o", tmp_path / "out", "--no-terms")
    assert done.returncode == 1
    assert "skipped" in done.stdout and "Enter its password" in done.stdout


def test_text_menu_walks_through_a_scrub_and_a_search(tmp_path):
    src = tmp_path / "taxes"
    src.mkdir()
    builders.state_return(src)
    answers = ["1", str(src), str(tmp_path / "out"),           # scrub; where; where to
               "Marcus T Feldspar", "Odalys Feldspar", "",     # people
               "", "Bellhaven", "", "",                        # businesses, places, anything else
               "n", "n", "y", "y", "",                         # passwords, company names, thorough, start, pause
               "3", "", "Feldspar", "243,733", "", "",         # search the results
               "0"]
    if not __import__("scrub_pdfs").find_tesseract():
        answers[12:14] = ["y"]                                 # no thorough question; confirm 'continue anyway'
    done = run("--menu", stdin="\n".join(answers) + "\n")
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr
    assert "Done: 1 clean" in done.stdout
    assert "Not found in 1 file(s)" in done.stdout             # the surname
    assert "FOUND 1 time(s)" in done.stdout                    # an amount, which should still be there
    names = Path(os.environ["PDF_SCRUBBER_HOME"]) / "pii_terms.txt"
    assert "name: Marcus T Feldspar" in names.read_text(encoding="utf-8")
    assert "Feldspar" not in text_of(tmp_path / "out" / "doc_001.redacted.pdf")
