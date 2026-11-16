"""The window, driven off-screen: all four steps, a review edit, and unlocking a file."""
import time

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

import pymupdf  # noqa: E402

import builders  # noqa: E402
import scrubber_gui as gui  # noqa: E402
from conftest import text_of  # noqa: E402


from PySide6 import QtGui  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402


def dark_palette():
    """What Qt is handed on a Windows PC set to dark mode."""
    palette = QtGui.QPalette()
    for role, colour in (("Window", "#202020"), ("WindowText", "#FFFFFF"), ("Base", "#2B2B2B"), ("Text", "#FFFFFF"),
                         ("Button", "#333333"), ("ButtonText", "#FFFFFF"), ("PlaceholderText", "#AAAAAA"),
                         ("Highlight", "#0078D4"), ("HighlightedText", "#FFFFFF"), ("ToolTipText", "#FFFFFF")):
        palette.setColor(getattr(QtGui.QPalette.ColorRole, role), QtGui.QColor(colour))
    return palette


@pytest.fixture(scope="module")
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    instance.setPalette(dark_palette())         # every test runs as if the PC were in dark mode
    gui.prepare_app(instance)
    return instance


def ink(widget, x0, y0, x1, y1) -> int:
    """How many clearly dark pixels a region of a widget shows - i.e. is there readable text."""
    image = widget.grab().toImage()
    return sum(1 for x in range(x0, min(x1, image.width())) for y in range(y0, min(y1, image.height()))
               if image.pixelColor(x, y).lightness() < 110)


def test_file_names_are_readable_when_the_pc_is_in_dark_mode(app, tmp_path):
    """Chosen files once seemed not to appear at all: white text on the app's white list."""
    builders.state_return(tmp_path)
    builders.wording_page(tmp_path)
    window = gui.MainWindow()
    window.show()
    window.files.add_paths([str(tmp_path)])
    app.processEvents()
    table = window.files.table
    assert table.rowCount() == 2
    top = table.horizontalHeader().height() + table.rowViewportPosition(0)
    assert ink(table, 28, top, 320, top + table.rowHeight(0)) > 60, "the file name is not visible"
    for widget in (table, window.files.out, window.names.table, window.names.text, window.names.tree,
                   window.review.docs, window.review.search):
        assert widget.palette().color(QtGui.QPalette.ColorRole.Text).lightness() < 110, widget
    window.files.out.setText("C:/Users/someone/Scrubbed")
    app.processEvents()
    assert ink(window.files.out, 8, 4, 260, window.files.out.height() - 4) > 60, "typed text is not visible"
    assert "2 of 2" in window.files.count_note.text()
    window.close()


def test_ticks_survive_adding_a_password_and_more_files(app, tmp_path):
    a = builders.state_return(tmp_path)
    builders.wording_page(tmp_path)
    window = gui.MainWindow()
    window.files.add_paths([str(a.path)])
    window.files.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not window.files.btn_next.isEnabled()                # nothing left to scrub
    window.files.pw.setText("secret")
    window.files.add_password()
    window.files.add_paths([str(tmp_path)])
    states = {window.files.table.item(r, 0).text(): window.files.table.item(r, 0).checkState()
              for r in range(window.files.table.rowCount())}
    assert states[a.path.name] == Qt.CheckState.Unchecked and states["wording.pdf"] == Qt.CheckState.Checked
    assert window.files.btn_next.isEnabled()
    window.files.add_paths([str(tmp_path / "nothing here")])
    assert "No PDF files" in window.files.count_note.text()
    window.close()


def test_locked_files_say_whether_the_password_fits(app, tmp_path):
    with pymupdf.open(builders.wording_page(tmp_path).path) as doc:
        doc.save(tmp_path / "locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw123", owner_pw="o")
    window = gui.MainWindow()
    window.files.add_paths([str(tmp_path / "locked.pdf")])
    assert "needs its password" in window.files.table.item(0, 1).text()
    window.files.pw.setText("wrong")
    window.files.add_password()
    assert "none of the passwords fit" in window.files.table.item(0, 1).text()
    window.files.pw.setText("pw123")
    window.files.add_password()
    assert "password accepted" in window.files.table.item(0, 1).text()
    window.close()


def test_errors_are_written_to_a_log_instead_of_vanishing(app):
    gui.note_error("Traceback: example failure")
    assert "example failure" in gui.error_log().read_text(encoding="utf-8")


def wait(app, window, limit=600):
    start = time.time()
    while True:
        end = time.time() + 0.2
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)
        if not window.busy():
            break
        assert time.time() - start < limit, "the job did not finish"
    app.processEvents()


def test_window_runs_all_four_steps(app, tmp_path):
    src = tmp_path / "taxes"
    src.mkdir()
    k1 = builders.irs_k1(src)
    builders.state_return(src)
    (src / "K-1 Tidewater (1).pdf").write_bytes(k1.path.read_bytes())          # a duplicate download
    with pymupdf.open(builders.wording_page(tmp_path).path) as doc:
        doc.save(src / "locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw123", owner_pw="o")

    window = gui.MainWindow()
    window.show()
    # step 1
    window.files.add_paths([str(src)])
    assert len(window.files.files) == 4 and len(window.files.selected()) == 3  # the duplicate is left out
    assert window.files.out.text().endswith("taxes Scrubbed")
    notes = [window.files.table.item(r, 1).text() for r in range(4)]
    assert any("duplicate" in n for n in notes) and any("Locked" in n for n in notes)
    window.files.btn_next.click()
    # step 2
    while window.names.table.rowCount():
        window.names.table.removeRow(0)
    for kind, text in (("Person", "Odalys Feldspar"), ("Place", "Bellhaven")):
        window.names.kind.setCurrentText(kind)
        window.names.text.setText(text)
        window.names.btn_add.click()
    window.names.btn_go.click()                                                # scan
    wait(app, window)
    found = {record["phrase"]: record["kind"] for record, _ in window.names.choices()}
    assert found.get("Marcus T Feldspar") == "NAME" and "Tidewater Granite Partners LP" in found
    # taking a name off the list means the earlier scan no longer matches: scan again
    window.names.table.selectRow(0)
    window.names.btn_remove.click()
    assert window.session is None and window.names.btn_go.text() == "Scan my documents"
    window.names.kind.setCurrentText("Person")
    window.names.text.setText("Odalys Feldspar")
    window.names.btn_go.click()                                                # adds the name and scans
    wait(app, window)
    window.names.btn_biz.click()                                               # keep business names
    window.names.btn_go.click()                                                # scrub
    wait(app, window)
    # step 3
    rows = {window.scrub.table.item(r, 0).text(): window.scrub.table.item(r, 1).text()
            for r in range(window.scrub.table.rowCount())}
    assert rows["K-1 Tidewater.pdf"].startswith("Scrubbed") and "password" in rows["locked.pdf"]
    out = window.session.out_dir
    k1_text = text_of(out / "doc_001.redacted.pdf")
    assert "Tidewater Granite Partners" in k1_text and "Feldspar" not in k1_text
    # step 4
    window.scrub.btn_review.click()
    wait(app, window)
    review = window.review
    assert review.docs.count() == 3
    row = next(i for i, (_, e) in enumerate(review.entries) if e["name"] == "K-1 Tidewater.pdf")
    review.docs.setCurrentRow(row)
    app.processEvents()
    boxes = [i for i in review.view.scene().items() if isinstance(i, gui.BoxItem)]
    assert len(boxes) >= 6
    with window.session.scrubber.open(k1.path) as doc:
        ogden = doc[0].search_for("Ogden")[0] + (-2, -2, 2, 2)
    review._box_drawn(list(ogden))
    assert review.btn_apply.isEnabled()
    review.btn_apply.click()
    wait(app, window)
    assert "Ogden" not in text_of(out / "doc_001.redacted.pdf")
    review.search.setText("Feldspar")
    review._search()
    assert review.hits.count() == 0
    # unlock the password-protected file from the review screen
    locked = next(i for i, (_, e) in enumerate(review.entries) if e["name"] == "locked.pdf")
    review.docs.setCurrentRow(locked)
    app.processEvents()
    assert review.btn_unlock.isVisible()
    review.unlock.setText("pw123")
    review.btn_unlock.click()
    wait(app, window)
    assert review.entries[locked][1]["status"] == "ok"
    window.close()


def test_names_list_is_saved_privately(app, tmp_path):
    import os
    from pathlib import Path
    window = gui.MainWindow()
    window.names.kind.setCurrentText("Person")
    window.names.text.setText("Jane Q. Sample")
    window.names.btn_add.click()
    saved = (Path(os.environ["PDF_SCRUBBER_HOME"]) / "pii_terms.txt").read_text(encoding="utf-8")
    assert "name: Jane Q. Sample" in saved
    window.close()
