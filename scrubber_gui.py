#!/usr/bin/env python3
"""
PDF Scrubber - the desktop window.

Four steps: choose PDFs, say which names to remove, scrub, then review every
page and fix anything by clicking. All the work is done by scrub_pdfs.py; this
file is only the window. Nothing here touches the network.

    python scrubber_gui.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import scrub_pdfs as core

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal
except ImportError:  # pragma: no cover - source installs without the window toolkit
    QtWidgets = None

APP_NAME = "PDF Scrubber"
ACCENT = "#2F6FED"

# Every colour is spelled out. Windows in dark mode hands Qt white text; a stylesheet that sets
# only backgrounds then draws white on white, and file names, labels and typed text vanish.
STYLE = f"""
* {{ font-size: 10.5pt; }}
QWidget {{ color: #1F2937; }}
QMainWindow, QDialog, QMessageBox, QWidget#page {{ background: #F5F6F8; }}
QScrollArea, QScrollArea > QWidget > QWidget#page {{ border: none; }}
QToolTip {{ color: #111827; background: #FFFFFF; border: 1px solid #CBD2DC; padding: 4px 6px; }}
QLabel {{ background: transparent; }}
QFrame#sidebar {{ background: #18212F; }}
QLabel#brand {{ color: white; font-size: 14pt; font-weight: 600; padding: 18px 18px 2px 18px; }}
QLabel#brandsub {{ color: #8FA0B8; padding: 0 18px 14px 18px; font-size: 9pt; }}
QListWidget#steps {{ background: transparent; border: none; color: #B9C4D4; outline: none; }}
QListWidget#steps::item {{ padding: 11px 18px; border-left: 3px solid transparent; }}
QListWidget#steps::item:selected {{ background: #223047; color: white; border-left: 3px solid {ACCENT}; }}
QListWidget#steps::item:disabled {{ color: #5B6B82; }}
QLabel#private {{ color: #8FA0B8; padding: 14px 18px; font-size: 9pt; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 600; color: #111827; }}
QLabel#sub {{ color: #5B6676; }}
QLabel#muted {{ color: #6B7686; font-size: 9.5pt; }}
QLabel#warn {{ color: #92400E; background: #FEF3C7; border: 1px solid #FCD34D; border-radius: 8px; padding: 9px 12px; }}
QLabel#good {{ color: #065F46; background: #D1FAE5; border: 1px solid #6EE7B7; border-radius: 8px; padding: 9px 12px; }}
QLabel#bad {{ color: #991B1B; background: #FEE2E2; border: 1px solid #FCA5A5; border-radius: 8px; padding: 9px 12px; }}
QFrame#card {{ background: white; border: 1px solid #E2E6EC; border-radius: 12px; }}
QLabel#cardtitle {{ font-weight: 600; color: #111827; font-size: 11.5pt; }}
QFrame#drop {{ background: #F8FAFF; border: 2px dashed #B7C6E6; border-radius: 12px; }}
QFrame#drop[active="true"] {{ background: #EAF1FF; border-color: {ACCENT}; }}
QPushButton {{ background: white; border: 1px solid #CBD2DC; border-radius: 8px; padding: 7px 14px; color: #1F2937; }}
QPushButton:hover {{ background: #F1F4F8; }}
QPushButton:disabled {{ color: #A3ACB9; background: #F3F4F6; border-color: #E2E6EC; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600; padding: 8px 20px; }}
QPushButton#primary:hover {{ background: #245FD6; }}
QPushButton#primary:disabled {{ background: #A9C0F5; border-color: #A9C0F5; color: white; }}
QPushButton#danger {{ color: #B91C1C; }}
QLineEdit, QComboBox {{ background: white; color: #111827; border: 1px solid #CBD2DC; border-radius: 8px;
    padding: 6px 9px; selection-background-color: {ACCENT}; selection-color: white; }}
QComboBox QAbstractItemView {{ background: white; color: #111827; border: 1px solid #CBD2DC;
    selection-background-color: #DCE8FF; selection-color: #111827; outline: none; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QTableWidget, QTreeWidget, QListWidget#docs, QListWidget#hits {{
    background: white; color: #1F2937; border: 1px solid #E2E6EC; border-radius: 8px; gridline-color: #EEF1F5;
    alternate-background-color: #F8FAFC; selection-background-color: #DCE8FF; selection-color: #111827; }}
QListWidget#docs::item:selected, QListWidget#hits::item:selected, QTableWidget::item:selected,
QTreeWidget::item:selected {{ background: #DCE8FF; color: #111827; }}
QTableCornerButton::section {{ background: #F5F6F8; border: none; }}
QHeaderView::section {{ background: #F5F6F8; border: none; border-bottom: 1px solid #E2E6EC; padding: 6px 8px;
    font-weight: 600; color: #4B5563; }}
QProgressBar {{ border: none; background: #E5E9F0; border-radius: 5px; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QGraphicsView {{ background: #DADFE7; border: 1px solid #CBD2DC; border-radius: 8px; }}
"""

TYPES = [("Person", "name"), ("Business or trust", "entity"), ("Place", "place"), ("Other", "")]
GROUPS = [("People", ("NAME",)), ("Businesses, funds and employers", ("ENTITY",)),
          ("Tax preparers and their firms", ("PREPARER",)),
          ("Addresses", ("ADDRESS",)), ("E-mail addresses", ("EMAIL",))]
ALWAYS = ("SSN", "EIN", "TIN", "PTIN", "ACCT", "PHONE", "PIN", "NUMBER", "ID", "DOB", "LICENSE")


# --------------------------------------------------------------------------
# start-up: look the same on every PC, and never fail silently
# --------------------------------------------------------------------------

def light_palette() -> "QtGui.QPalette":
    palette = QtGui.QPalette()
    role = QtGui.QPalette.ColorRole
    for name, colour in (("Window", "#F5F6F8"), ("WindowText", "#1F2937"), ("Base", "#FFFFFF"),
                         ("AlternateBase", "#F8FAFC"), ("Text", "#1F2937"), ("Button", "#FFFFFF"),
                         ("ButtonText", "#1F2937"), ("BrightText", "#FFFFFF"), ("Highlight", ACCENT),
                         ("HighlightedText", "#FFFFFF"), ("ToolTipBase", "#FFFFFF"), ("ToolTipText", "#111827"),
                         ("PlaceholderText", "#9AA3AF"), ("Link", ACCENT), ("Light", "#FFFFFF"),
                         ("Midlight", "#E9ECF1"), ("Mid", "#C4CAD4"), ("Dark", "#9AA3AF"), ("Shadow", "#6B7686")):
        palette.setColor(getattr(role, name), QtGui.QColor(colour))
    disabled = QtGui.QPalette.ColorGroup.Disabled
    for name in ("WindowText", "Text", "ButtonText"):
        palette.setColor(disabled, getattr(role, name), QtGui.QColor("#A3ACB9"))
    return palette


def prepare_app(app) -> None:
    """One light look everywhere, whatever theme the PC is set to."""
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    try:                                        # Qt 6.8+: stop following Windows dark mode
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except Exception:
        pass
    app.setPalette(light_palette())
    app.setStyleSheet(STYLE)


def error_log() -> Path:
    return core.data_dir() / "error.log"


def note_error(text: str) -> None:
    try:
        with error_log().open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- PDF Scrubber {core.VERSION} ---\n{text}\n")
    except OSError:
        pass


def install_error_reporting() -> None:
    """A packaged Windows app has no console, so an unhandled error would simply look like a button
    that does nothing. Show it and keep the details in a log instead."""
    showing = []

    def hook(kind, value, tb):
        note_error("".join(traceback.format_exception(kind, value, tb)))
        if (QtWidgets.QApplication.instance() is None or showing
                or threading.current_thread() is not threading.main_thread()):
            return
        showing.append(1)
        try:
            QtWidgets.QMessageBox.critical(
                None, APP_NAME, f"Something went wrong:\n\n{kind.__name__}: {value}\n\n"
                                f"Nothing unsafe was saved. The details are in:\n{error_log()}")
        finally:
            showing.clear()

    sys.excepthook = hook


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def label(text: str, name: str = "", wrap: bool = True) -> "QtWidgets.QLabel":
    w = QtWidgets.QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def button(text: str, primary: bool = False, name: str = "") -> "QtWidgets.QPushButton":
    w = QtWidgets.QPushButton(text)
    if primary:
        w.setObjectName("primary")
    elif name:
        w.setObjectName(name)
    w.setCursor(Qt.CursorShape.PointingHandCursor)
    return w


def card(title: str = "") -> tuple["QtWidgets.QFrame", "QtWidgets.QVBoxLayout"]:
    frame = QtWidgets.QFrame()
    frame.setObjectName("card")
    box = QtWidgets.QVBoxLayout(frame)
    box.setContentsMargins(16, 14, 16, 16)
    box.setSpacing(10)
    if title:
        box.addWidget(label(title, "cardtitle"))
    return frame, box


def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))                      # noqa: S606 - opens the user's own folder
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def read_user_terms(path: Path) -> list[tuple[str, str]]:
    """The names list as (type label, text) rows."""
    rows = []
    for line in core.read_terms(path):
        mm = re.match(r"^([A-Za-z]{2,12}):\s*(.+)$", line)
        if not mm:
            rows.append(("Other", line))
        elif mm.group(1).lower() == "name":
            rows.append(("Person", mm.group(2)))
        elif mm.group(1).lower() == "place":
            rows.append(("Place", mm.group(2)))
        else:
            rows.append(("Business or trust", mm.group(2)))
    return rows


def write_user_terms(path: Path, rows: list[tuple[str, str]]) -> None:
    prefix = dict(TYPES)
    lines = ["# Names and words to remove from every document. Written by PDF Scrubber.",
             "# KEEP THIS FILE PRIVATE - never upload or publish it.", ""]
    for kind, text in rows:
        tag = prefix.get(kind, "")
        lines.append(f"{tag}: {text}" if tag else text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Bridge(QtCore.QObject if QtWidgets else object):
    """Carries progress from the worker thread to the window."""
    event = Signal(str, dict) if QtWidgets else None


class Worker(QtCore.QThread if QtWidgets else object):
    """Runs one job off the window's thread. PyMuPDF is not thread-safe, so the window never
    renders pages while a job is running."""
    finished_ok = Signal(object) if QtWidgets else None
    crashed = Signal(str) if QtWidgets else None

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self.job = job

    def run(self):
        try:
            self.finished_ok.emit(self.job())
        except core.Cancelled:
            self.finished_ok.emit(None)
        except Exception:
            self.crashed.emit(traceback.format_exc())


# --------------------------------------------------------------------------
# Step 1 - files
# --------------------------------------------------------------------------

class DropArea(QtWidgets.QFrame if QtWidgets else object):
    dropped = Signal(list) if QtWidgets else None

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(118)
        box = QtWidgets.QVBoxLayout(self)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = label("Drop a folder or PDF files here", "cardtitle", wrap=False)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(title)
        hint = label("or use the buttons below. Your originals are never changed.", "muted", wrap=False)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(hint)

    def _mark(self, on: bool):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._mark(True)

    def dragLeaveEvent(self, e):
        self._mark(False)

    def dropEvent(self, e):
        self._mark(False)
        self.dropped.emit([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


class FilesPage(QtWidgets.QWidget if QtWidgets else object):
    changed = Signal() if QtWidgets else None
    go_next = Signal() if QtWidgets else None
    go_review = Signal() if QtWidgets else None

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.files: list[Path] = []
        self.passwords: list[str] = []
        self.ticks: dict[str, bool] = {}        # the person's own ticks survive every refresh
        self.locks: dict[str, bool] = {}        # which files need a password (checked once)
        self.last_dir = ""
        self.setAcceptDrops(True)               # dropping anywhere on the page works, not just the box
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)
        root.addWidget(label("Choose your PDFs", "h1"))
        root.addWidget(label("Add the tax documents you want to share. Copies are made with personal details "
                             "blacked out; nothing is uploaded.", "sub"))

        self.drop = DropArea()
        self.drop.dropped.connect(self.add_paths)
        root.addWidget(self.drop)
        row = QtWidgets.QHBoxLayout()
        self.btn_folder = button("Choose folder...")
        self.btn_files = button("Add PDF files...")
        self.btn_clear = button("Clear list", name="danger")
        self.btn_folder.clicked.connect(self.pick_folder)
        self.btn_files.clicked.connect(self.pick_files)
        self.btn_clear.clicked.connect(self.clear)
        for b in (self.btn_folder, self.btn_files):
            row.addWidget(b)
        self.count_note = label("", "muted", wrap=False)
        self.count_note.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        row.addWidget(self.count_note, 1)
        row.addWidget(self.btn_clear)
        root.addLayout(row)

        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setMinimumHeight(150)        # never squeezed out of sight on a small screen
        self.table.setHorizontalHeaderLabels(["Document", "Note"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.itemChanged.connect(self._ticked)
        root.addWidget(self.table, 1)

        frame, box = card()
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.addWidget(label("Save scrubbed copies to", wrap=False), 0, 0)
        self.out = QtWidgets.QLineEdit()
        self.out.setPlaceholderText("A new folder next to your originals")
        self.out.textChanged.connect(self._out_changed)
        grid.addWidget(self.out, 0, 1)
        self.btn_out = button("Browse...")
        self.btn_out.clicked.connect(self.pick_out)
        grid.addWidget(self.btn_out, 0, 2)
        grid.addWidget(label("Passwords for locked PDFs", wrap=False), 1, 0)
        self.pw = QtWidgets.QLineEdit()
        self.pw.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText("Type a password and press Add (only needed for locked files)")
        self.pw.returnPressed.connect(self.add_password)
        grid.addWidget(self.pw, 1, 1)
        self.btn_pw = button("Add")
        self.btn_pw.clicked.connect(self.add_password)
        grid.addWidget(self.btn_pw, 1, 2)
        self.pw_note = label("", "muted")
        grid.addWidget(self.pw_note, 2, 1)
        box.addLayout(grid)
        self.thorough = QtWidgets.QCheckBox("Check scanned pages thoroughly (slower, recommended)")
        self.thorough.setChecked(True)
        box.addWidget(self.thorough)
        self.fresh = QtWidgets.QCheckBox("Start over in this folder (delete the earlier results there)")
        self.fresh.setVisible(False)
        box.addWidget(self.fresh)
        self.resume_note = label("", "good")
        self.resume_note.setVisible(False)
        box.addWidget(self.resume_note)
        root.addWidget(frame)

        self.ocr_note = label("", "warn")
        root.addWidget(self.ocr_note)
        nav = QtWidgets.QHBoxLayout()
        self.btn_review = button("Review earlier results")
        self.btn_review.setVisible(False)
        self.btn_review.clicked.connect(self.go_review.emit)
        nav.addWidget(self.btn_review)
        nav.addStretch(1)
        self.btn_next = button("Next: names to remove", primary=True)
        self.btn_next.clicked.connect(self.go_next.emit)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)
        self.refresh()

    # ---- file list
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def add_paths(self, paths: list[str]) -> None:
        paths = [os.path.normpath(p) for p in paths if p]
        QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)     # a big folder takes a moment
        try:
            found = core.collect(paths)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        known = {str(f.resolve()) for f in self.files}
        fresh = [f for f in found if str(f.resolve()) not in known]
        self.files += fresh
        if paths:
            first = Path(paths[0])
            self.last_dir = str(first if first.is_dir() else first.parent)
        if fresh and not self.out.text().strip():
            base = Path(self.last_dir)
            self.out.setText(str(base.parent / f"{base.name or 'PDFs'} Scrubbed"))
        self.refresh()
        if not found:
            self.count_note.setText("No PDF files were found in what you chose.")
        elif not fresh:
            self.count_note.setText("Those files are already in the list.")

    def pick_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder with your PDFs", self.last_dir)
        if folder:
            self.add_paths([folder])

    def pick_files(self):
        names, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "PDF files", self.last_dir, "PDF files (*.pdf)")
        if names:
            self.add_paths(names)

    def pick_out(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Where to save the scrubbed copies",
                                                            self.out.text().strip() or self.last_dir)
        if folder:
            self.out.setText(os.path.normpath(folder))

    def clear(self):
        self.files, self.ticks = [], {}
        self.refresh()

    def _ticked(self, item):
        if item.column() == 0 and 0 <= item.row() < len(self.files):
            self.ticks[str(self.files[item.row()].resolve())] = item.checkState() == Qt.CheckState.Checked
        self._update_next()
        self.changed.emit()

    def _update_next(self):
        chosen = len(self.selected())
        self.btn_next.setEnabled(bool(self.out.text().strip()) and chosen > 0)
        if self.files:
            self.count_note.setText(f"{chosen} of {len(self.files)} PDF(s) will be scrubbed.")
        else:
            self.count_note.setText("")

    def add_password(self):
        text = self.pw.text()
        if text and text not in self.passwords:
            self.passwords.append(text)
        self.pw.clear()
        self.refresh()

    def selected(self) -> list[Path]:
        return [f for row, f in enumerate(self.files)
                if self.table.item(row, 0) and self.table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def _lock_note(self, path: Path) -> str:
        """'' for an ordinary PDF, otherwise what to tell the person about the lock."""
        key = str(path.resolve())
        try:
            if key not in self.locks:
                with core.pymupdf.open(str(path)) as doc:
                    self.locks[key] = bool(doc.needs_pass)
            if not self.locks[key]:
                return ""
            with core.pymupdf.open(str(path)) as doc:
                if any(doc.authenticate(pw) for pw in self.passwords):
                    return "Locked - password accepted"
            return "Locked - none of the passwords fit" if self.passwords else "Locked - needs its password"
        except Exception:
            return "Could not be opened - it will be skipped"

    def refresh(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.files))
        stems = {f.stem for f in self.files}
        for row, f in enumerate(self.files):
            item = QtWidgets.QTableWidgetItem(f.name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            copy = re.sub(r" \(\d+\)$| - Copy$", "", f.stem)
            duplicate = copy != f.stem and copy in stems
            note = "Looks like a duplicate download - left out unless you tick it" if duplicate \
                else (self._lock_note(f) if core.pymupdf is not None else "")
            ticked = self.ticks.setdefault(str(f.resolve()), not duplicate)
            item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
            item.setToolTip(str(f))
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(note))
        self.table.blockSignals(False)
        self.pw_note.setText(f"{len(self.passwords)} password(s) added. They are kept only while the window is open."
                             if self.passwords else "")
        found = core.find_tesseract()
        self.ocr_note.setVisible(not found)
        self.ocr_note.setText("The scan reader (Tesseract) is not installed, so scanned pages will be left out of the "
                              "results rather than passed through unchecked. PDFs downloaded from tax software or a "
                              "portal are not affected.")
        self.thorough.setVisible(bool(found))
        self._out_changed()
        self.changed.emit()

    def _out_changed(self):
        text = self.out.text().strip()
        existing = 0
        if text:
            try:                                # half-typed paths can be invalid on Windows
                project = core.Project.open(Path(text))
                existing = len(project.done()) if project.resumed else 0
            except (OSError, ValueError):
                existing = 0
        self.fresh.setVisible(existing > 0)
        self.resume_note.setVisible(existing > 0)
        self.btn_review.setVisible(existing > 0)
        if existing:
            self.resume_note.setText(f"This folder already holds {existing} scrubbed document(s). New files will be "
                                     "added to them and use the same tags, so everything can still be read together.")
        self._update_next()
        self.changed.emit()


# --------------------------------------------------------------------------
# Step 2 - names
# --------------------------------------------------------------------------

class NamesPage(QtWidgets.QWidget if QtWidgets else object):
    go_back = Signal() if QtWidgets else None
    scan = Signal() if QtWidgets else None
    scrub = Signal() if QtWidgets else None
    cancel = Signal() if QtWidgets else None
    shrunk = Signal() if QtWidgets else None     # a name was taken off the list

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.records: list[dict] = []
        self.scanned = False
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)
        root.addWidget(label("Names to remove", "h1"))
        root.addWidget(label("ID numbers, accounts, phone numbers and e-mails are found automatically. Names are "
                             "not - a name looks like any other word - so tell the tool which ones to remove.", "sub"))
        cols = QtWidgets.QHBoxLayout()
        cols.setSpacing(14)

        left, lbox = card("Your list")
        lbox.addWidget(label("Everyone and everything that identifies you: people, businesses, trusts, your town "
                             "and street name. Saved privately on this computer for next time.", "muted"))
        entry = QtWidgets.QHBoxLayout()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems([t for t, _ in TYPES])
        self.text = QtWidgets.QLineEdit()
        self.text.setPlaceholderText("e.g. Jane Q. Sample")
        self.text.returnPressed.connect(self.add_row)
        self.btn_add = button("Add")
        self.btn_add.clicked.connect(self.add_row)
        entry.addWidget(self.kind)
        entry.addWidget(self.text, 1)
        entry.addWidget(self.btn_add)
        lbox.addLayout(entry)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Type", "Remove this everywhere"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        lbox.addWidget(self.table, 1)
        self.word_note = label("", "muted")
        lbox.addWidget(self.word_note)
        self.btn_remove = button("Remove selected", name="danger")
        self.btn_remove.clicked.connect(self.remove_rows)
        lbox.addWidget(self.btn_remove, 0, Qt.AlignmentFlag.AlignLeft)
        cols.addWidget(left, 1)

        right, rbox = card("Found in your documents")
        self.found_hint = label("Press 'Scan my documents' and the tool will list the names, businesses and addresses "
                                "it found in the forms. Untick anything you would rather keep visible.", "muted")
        rbox.addWidget(self.found_hint)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        rbox.addWidget(self.tree, 1)
        quick = QtWidgets.QHBoxLayout()
        self.btn_all = button("Tick all")
        self.btn_biz = button("Keep business names")
        self.btn_all.clicked.connect(lambda: self.set_all(True))
        self.btn_biz.clicked.connect(lambda: self.set_group(1, False))
        quick.addWidget(self.btn_all)
        quick.addWidget(self.btn_biz)
        quick.addStretch(1)
        rbox.addLayout(quick)
        self.always = label("", "muted")
        rbox.addWidget(self.always)
        cols.addWidget(right, 1)
        root.addLayout(cols, 1)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        self.status = label("", "muted")
        root.addWidget(self.status)
        nav = QtWidgets.QHBoxLayout()
        self.btn_back = button("Back")
        self.btn_back.clicked.connect(self.go_back.emit)
        self.btn_cancel = button("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.btn_go = button("Scan my documents", primary=True)
        self.btn_go.clicked.connect(self._go)
        nav.addWidget(self.btn_back)
        nav.addStretch(1)
        nav.addWidget(self.btn_cancel)
        nav.addWidget(self.btn_go)
        root.addLayout(nav)
        for kind, text in read_user_terms(core.default_terms_path()):
            self._append(kind, text)
        self.set_scanned(False)

    # ---- the user's own list
    def _append(self, kind: str, text: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(kind))
        self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(text))

    def rows(self) -> list[tuple[str, str]]:
        return [(self.table.item(r, 0).text(), self.table.item(r, 1).text()) for r in range(self.table.rowCount())]

    def add_row(self):
        text = " ".join(self.text.text().split())
        if not text:
            return
        kind = self.kind.currentText()
        if (kind, text.lower()) not in {(k, t.lower()) for k, t in self.rows()}:
            self._append(kind, text)
        self.text.clear()
        everyday = [w for w in re.findall(r"[^\W\d_]{3,}", text) if w.lower() in core.STOPWORDS] \
            if kind == "Person" else []
        self.word_note.setText(
            f"'{everyday[0]}' is also an everyday word, so it is only removed as part of the full name. "
            f"To remove it wherever it stands alone, add it again as type 'Other'." if everyday else "")
        self.save()

    def remove_rows(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            self.word_note.setText("Click a row in the list first, then press 'Remove selected'.")
            return
        for row in rows:
            self.table.removeRow(row)
        self.word_note.setText("")
        self.save()
        self.shrunk.emit()

    def save(self):
        write_user_terms(core.default_terms_path(), self.rows())

    # ---- what the scan found
    def set_scanned(self, scanned: bool) -> None:
        self.scanned = scanned
        self.btn_go.setText("Scrub now" if scanned else "Scan my documents")
        for b in (self.btn_all, self.btn_biz):
            b.setEnabled(scanned)
        if not scanned:
            self.tree.clear()
            self.records = []
            self.always.setText("")
            self.status.setText("")

    def show_found(self, learned: list[dict], kept: list[dict]) -> None:
        self.tree.clear()
        self.records = [dict(r, tick=True) for r in learned] + [dict(r, tick=False) for r in kept]
        shown = 0
        for title, kinds in GROUPS:
            rows = [r for r in self.records if r["kind"] in kinds]
            if not rows:
                continue
            top = QtWidgets.QTreeWidgetItem([f"{title}  ({len(rows)})"])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            self.tree.addTopLevelItem(top)
            for r in sorted(rows, key=lambda r: r["phrase"].lower()):
                item = QtWidgets.QTreeWidgetItem([r["phrase"]])
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if r["tick"] else Qt.CheckState.Unchecked)
                item.setData(0, Qt.ItemDataRole.UserRole, r)
                top.addChild(item)
                shown += 1
            top.setExpanded(True)
        numbers = sum(1 for r in self.records if r["kind"] in ALWAYS)
        self.found_hint.setText("Ticked items are removed from every document. Untick anything you would rather "
                                "keep visible." if shown else
                                "No names were found inside form boxes. Anything on your own list is still removed.")
        self.always.setText(f"{numbers} ID, account and phone number(s) were also found. Those are always removed."
                            if numbers else "")
        self.set_scanned(True)

    def _leaves(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                yield i, top.child(j)

    def set_all(self, on: bool):
        for _, leaf in self._leaves():
            leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def set_group(self, index: int, on: bool):
        wanted = GROUPS[index][1]
        for _, leaf in self._leaves():
            if leaf.data(0, Qt.ItemDataRole.UserRole)["kind"] in wanted:
                leaf.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def choices(self) -> list[tuple[dict, bool]]:
        """(record, remove?) for every listed item."""
        return [(leaf.data(0, Qt.ItemDataRole.UserRole), leaf.checkState(0) == Qt.CheckState.Checked)
                for _, leaf in self._leaves()]

    def set_busy(self, busy: bool, text: str = ""):
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        for w in (self.btn_go, self.btn_back, self.btn_add, self.btn_remove):
            w.setEnabled(not busy)
        self.status.setText(text)

    def _go(self):
        self.add_row()
        self.save()
        (self.scrub if self.scanned else self.scan).emit()


# --------------------------------------------------------------------------
# Step 3 - scrubbing
# --------------------------------------------------------------------------

class ScrubPage(QtWidgets.QWidget if QtWidgets else object):
    cancel = Signal() if QtWidgets else None
    go_review = Signal() if QtWidgets else None
    go_back = Signal() if QtWidgets else None

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.out_dir: Path | None = None
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(14)
        self.title = label("Scrubbing", "h1")
        root.addWidget(self.title)
        self.sub = label("", "sub")
        root.addWidget(self.sub)
        frame, box = card()
        self.file_label = label("", "cardtitle")
        box.addWidget(self.file_label)
        self.files_bar = QtWidgets.QProgressBar()
        self.files_bar.setTextVisible(False)
        box.addWidget(self.files_bar)
        self.page_label = label("", "muted")
        box.addWidget(self.page_label)
        self.page_bar = QtWidgets.QProgressBar()
        self.page_bar.setTextVisible(False)
        box.addWidget(self.page_bar)
        root.addWidget(frame)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Document", "What happened", "What to do"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        root.addWidget(self.table, 1)
        nav = QtWidgets.QHBoxLayout()
        self.btn_back = button("Back")
        self.btn_back.clicked.connect(self.go_back.emit)
        self.btn_open = button("Open results folder")
        self.btn_open.clicked.connect(lambda: self.out_dir and open_folder(self.out_dir))
        self.btn_cancel = button("Cancel")
        self.btn_cancel.clicked.connect(self.cancel.emit)
        self.btn_review = button("Next: review every page", primary=True)
        self.btn_review.clicked.connect(self.go_review.emit)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_open)
        nav.addStretch(1)
        nav.addWidget(self.btn_cancel)
        nav.addWidget(self.btn_review)
        root.addLayout(nav)

    def start(self, total: int, out_dir: Path):
        self.out_dir = out_dir
        self.title.setText("Scrubbing")
        self.sub.setText("Text PDFs take a second or two each. Scanned pages take around half a minute per page.")
        self.table.setRowCount(0)
        self.files_bar.setRange(0, max(total, 1))
        self.files_bar.setValue(0)
        self.page_bar.setRange(0, 1)
        self.page_bar.setValue(0)
        self.file_label.setText("Starting...")
        self.page_label.setText("")
        for w, on in ((self.btn_cancel, True), (self.btn_review, False), (self.btn_back, False), (self.btn_open, False)):
            w.setEnabled(on)

    def on_event(self, kind: str, info: dict):
        if kind == "file_start":
            self.file_label.setText(f"Document {info['index']} of {info['total']}:  {info['name']}")
            self.page_label.setText("")
            self.page_bar.setRange(0, 1)
            self.page_bar.setValue(0)
        elif kind == "page":
            self.page_bar.setRange(0, info["pages"])
            self.page_bar.setValue(info["page"] - 1)
            self.page_label.setText(f"Page {info['page']} of {info['pages']}"
                                    + ("  -  scanned page, reading it takes a while" if info["scanned"] else ""))
        elif kind == "file_done":
            self.files_bar.setValue(info["index"])
            self.page_bar.setValue(self.page_bar.maximum())
            self.add_result(info["name"], info["status"], info["what"], info["todo"])

    def add_result(self, name: str, status: str, what: str, todo: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        colour = {"ok": "#065F46", "partial": "#92400E"}.get(status, "#991B1B")
        for col, text in enumerate((name, what, todo)):
            item = QtWidgets.QTableWidgetItem(text)
            if col == 1:
                item.setForeground(QtGui.QBrush(QtGui.QColor(colour)))
            self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()
        self.table.scrollToBottom()

    def finish(self, cancelled: bool, summary: str):
        self.title.setText("Stopped" if cancelled else "Scrubbing finished")
        self.sub.setText(summary)
        self.file_label.setText("Stopped - documents already finished are saved." if cancelled else "All done.")
        self.page_label.setText("")
        for w, on in ((self.btn_cancel, False), (self.btn_review, True), (self.btn_back, True), (self.btn_open, True)):
            w.setEnabled(on)


# --------------------------------------------------------------------------
# Step 4 - review
# --------------------------------------------------------------------------

class BoxItem(QtWidgets.QGraphicsRectItem if QtWidgets else object):
    """One black-out box on the page. state: 'auto' (will be removed), 'kept' (the person said
    leave it visible) or 'drawn' (the person added it)."""

    def __init__(self, rect_pt: list, token: str, state: str, scale: float):
        super().__init__(rect_pt[0] * scale, rect_pt[1] * scale,
                         (rect_pt[2] - rect_pt[0]) * scale, (rect_pt[3] - rect_pt[1]) * scale)
        self.rect_pt, self.token, self.state = [float(v) for v in rect_pt], token, state
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restyle()

    def restyle(self):
        if self.state == "auto":
            pen, fill = QtGui.QPen(QtGui.QColor(185, 28, 28), 1.4), QtGui.QColor(220, 38, 38, 95)
            tip = f"{self.token}  -  will be removed. Click to keep it visible instead."
        elif self.state == "kept":
            pen, fill = QtGui.QPen(QtGui.QColor(5, 150, 105), 1.6, Qt.PenStyle.DashLine), QtGui.QColor(16, 185, 129, 30)
            tip = "Kept visible. Click to remove it after all."
        else:
            pen, fill = QtGui.QPen(QtGui.QColor(29, 78, 216), 1.4), QtGui.QColor(37, 99, 235, 110)
            tip = "You added this box. Click to take it away."
        self.setPen(pen)
        self.setBrush(QtGui.QBrush(fill))
        self.setToolTip(tip)


class PageView(QtWidgets.QGraphicsView if QtWidgets else object):
    box_clicked = Signal(object) if QtWidgets else None
    box_drawn = Signal(list) if QtWidgets else None

    def __init__(self):
        super().__init__()
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scale_pt = 1.0
        self.editable = False
        self._start = None
        self._band = None

    def show_page(self, image: "QtGui.QImage | None", scale: float, boxes: list[BoxItem], editable: bool):
        scene = self.scene()
        scene.clear()
        self.scale_pt, self.editable, self._band = scale, editable, None
        if image is None:
            return
        scene.addPixmap(QtGui.QPixmap.fromImage(image))
        ratio = image.devicePixelRatio() or 1.0
        scene.setSceneRect(0, 0, image.width() / ratio, image.height() / ratio)
        for item in boxes:
            scene.addItem(item)

    def mousePressEvent(self, e):
        if not self.editable or e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        pos = self.mapToScene(e.position().toPoint())
        hit = next((i for i in self.scene().items(pos) if isinstance(i, BoxItem)), None)
        if hit:
            self.box_clicked.emit(hit)
            return
        self._start = pos
        self._band = self.scene().addRect(QtCore.QRectF(pos, pos), QtGui.QPen(QtGui.QColor(29, 78, 216), 1.2,
                                                                                Qt.PenStyle.DashLine),
                                          QtGui.QBrush(QtGui.QColor(37, 99, 235, 60)))

    def mouseMoveEvent(self, e):
        if self._band is not None and self._start is not None:
            self._band.setRect(QtCore.QRectF(self._start, self.mapToScene(e.position().toPoint())).normalized())
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._band is None:
            return super().mouseReleaseEvent(e)
        rect = self._band.rect().intersected(self.scene().sceneRect())
        self.scene().removeItem(self._band)
        self._band, self._start = None, None
        s = self.scale_pt
        if rect.width() / s >= 4 and rect.height() / s >= 4:
            self.box_drawn.emit([rect.left() / s, rect.top() / s, rect.right() / s, rect.bottom() / s])


class ReviewPage(QtWidgets.QWidget if QtWidgets else object):
    apply_changes = Signal(dict) if QtWidgets else None      # {path: overrides}
    go_back = Signal() if QtWidgets else None

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.session: core.Session | None = None
        self.entries: list[tuple[str, dict]] = []       # (original path, project entry)
        self.edits: dict[str, dict] = {}                # path -> overrides being edited
        self.key = ""
        self.pno = 0
        self.zoom = 1.25
        self.fit = True                         # follow the view's width until the person zooms
        self.doc = None
        self.doc_key = None
        self.working = False                    # a job is running: PyMuPDF must not be touched here
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(12)
        root.addWidget(label("Review every page", "h1"))
        root.addWidget(label("Red boxes will be removed. Click a box to keep that text visible. Drag anywhere on the "
                             "page to black out something the tool missed - a signature, handwriting, a name.", "sub"))
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)

        side = QtWidgets.QVBoxLayout()
        self.docs = QtWidgets.QListWidget()
        self.docs.setObjectName("docs")
        self.docs.setFixedWidth(250)
        self.docs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.docs.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.docs.currentRowChanged.connect(self._doc_changed)
        side.addWidget(self.docs, 1)
        self.checked = QtWidgets.QCheckBox("I have looked through this document")
        self.checked.toggled.connect(self._mark_checked)
        side.addWidget(self.checked)
        self.tally = label("", "muted")
        side.addWidget(self.tally)
        body.addLayout(side)

        mid = QtWidgets.QVBoxLayout()
        self.banner = label("", "good")
        mid.addWidget(self.banner)
        fix = QtWidgets.QHBoxLayout()
        self.btn_flatten = button("Flatten and scrub again")
        self.btn_flatten.clicked.connect(self._flatten)
        self.unlock = QtWidgets.QLineEdit()
        self.unlock.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.unlock.setPlaceholderText("Password for this document")
        self.btn_unlock = button("Unlock and scrub")
        self.btn_unlock.clicked.connect(self._unlock)
        fix.addWidget(self.btn_flatten)
        fix.addWidget(self.unlock)
        fix.addWidget(self.btn_unlock)
        fix.addStretch(1)
        mid.addLayout(fix)
        bar = QtWidgets.QHBoxLayout()
        self.btn_prev = button("< Page")
        self.btn_nextp = button("Page >")
        self.page_label = label("", wrap=False)
        self.btn_prev.clicked.connect(lambda: self._turn(-1))
        self.btn_nextp.clicked.connect(lambda: self._turn(1))
        self.btn_zoom_out = button("-")
        self.btn_zoom_in = button("+")
        self.btn_zoom_out.clicked.connect(lambda: self._zoom(1 / 1.2))
        self.btn_zoom_in.clicked.connect(lambda: self._zoom(1.2))
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["Original with boxes", "Scrubbed result"])
        self.mode.currentIndexChanged.connect(lambda *_: self.render())
        for w in (self.btn_prev, self.page_label, self.btn_nextp):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(label("Zoom", wrap=False))
        bar.addWidget(self.btn_zoom_out)
        bar.addWidget(self.btn_zoom_in)
        bar.addWidget(self.mode)
        mid.addLayout(bar)
        self.view = PageView()
        self.view.setMinimumSize(320, 260)
        self.view.box_clicked.connect(self._box_clicked)
        self.view.box_drawn.connect(self._box_drawn)
        mid.addWidget(self.view, 1)
        body.addLayout(mid, 1)

        right = QtWidgets.QVBoxLayout()
        frame, box = card("Your changes")
        self.pending = label("No changes yet.", "muted")
        box.addWidget(self.pending)
        self.btn_apply = button("Apply changes", primary=True)
        self.btn_apply.clicked.connect(self._apply)
        box.addWidget(self.btn_apply)
        self.btn_undo = button("Undo changes on this page")
        self.btn_undo.clicked.connect(self._undo_page)
        box.addWidget(self.btn_undo)
        right.addWidget(frame)
        frame, box = card("Double-check")
        box.addWidget(label("Search the scrubbed text for anything that should be gone: a surname, the last four "
                            "digits of an SSN, a street.", "muted"))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Word or number")
        self.search.returnPressed.connect(self._search)
        box.addWidget(self.search)
        self.hits = QtWidgets.QListWidget()
        self.hits.setObjectName("hits")
        self.hits.itemDoubleClicked.connect(self._open_hit)
        box.addWidget(self.hits, 1)
        self.search_note = label("", "muted")
        box.addWidget(self.search_note)
        right.addWidget(frame, 1)
        holder = QtWidgets.QWidget()
        holder.setLayout(right)
        holder.setFixedWidth(270)
        right.setContentsMargins(0, 0, 0, 0)
        body.addWidget(holder)
        root.addLayout(body, 1)

        nav = QtWidgets.QHBoxLayout()
        self.btn_back = button("Back")
        self.btn_back.clicked.connect(self.go_back.emit)
        self.btn_open = button("Open results folder", primary=True)
        self.btn_open.clicked.connect(lambda: self.session and open_folder(self.session.out_dir))
        nav.addWidget(self.btn_back)
        nav.addStretch(1)
        nav.addWidget(self.btn_open)
        root.addLayout(nav)

    # ---- loading
    def load(self, session: core.Session, keep_position: bool = False):
        self.session = session
        previous = self.key if keep_position else ""
        files = session.project.data["files"]
        self.entries = sorted(((k, v) for k, v in files.items() if v.get("status")), key=lambda kv: kv[1]["n"])
        if not keep_position:
            self.edits = {}
        self._close_doc()
        self.docs.blockSignals(True)
        self.docs.clear()
        for key, entry in self.entries:
            self.docs.addItem(self._doc_title(entry))
        self.docs.blockSignals(False)
        self._retitle()
        row = next((i for i, (k, _) in enumerate(self.entries) if k == previous), 0)
        if self.entries:
            self.docs.setCurrentRow(row)
            self._doc_changed(row, keep_page=keep_position)
        else:
            self.view.show_page(None, 1, [], False)
        self._update_pending()

    def _doc_title(self, entry: dict) -> str:
        mark = {"ok": "", "partial": "  (pages left out)", "FAILED": "  (held back)",
                "skipped": "  (not scrubbed)"}.get(entry["status"], "")
        scan = "  - scanned" if entry.get("flattened") else ""
        tick = "[x] " if entry.get("reviewed") else "[  ] "
        return f"{tick}doc_{entry['n']:03d}{mark}{scan}\n      {entry['name']}"

    def _retitle(self):
        for row, (key, entry) in enumerate(self.entries):
            self.docs.item(row).setText(self._doc_title(entry))
            self.docs.item(row).setToolTip(key)
            colour = {"ok": "#111827", "partial": "#92400E"}.get(entry["status"], "#991B1B")
            self.docs.item(row).setForeground(QtGui.QBrush(QtGui.QColor(colour)))
        written = [e for _, e in self.entries if e.get("output")]
        done = sum(1 for e in written if e.get("reviewed"))
        self.tally.setText(f"{done} of {len(written)} looked through" if written else "")

    def _entry(self) -> dict | None:
        return next((e for k, e in self.entries if k == self.key), None)

    def _close_doc(self):
        if self.doc is not None:
            try:
                self.doc.close()
            except Exception:
                pass
        self.doc, self.doc_key = None, None

    def _doc_changed(self, row: int, keep_page: bool = False):
        if row < 0 or row >= len(self.entries):
            return
        self.key = self.entries[row][0]
        entry = self.entries[row][1]
        if not keep_page:
            self.pno = 0
        self.checked.blockSignals(True)
        self.checked.setChecked(bool(entry.get("reviewed")))
        self.checked.setEnabled(bool(entry.get("output")))
        self.checked.blockSignals(False)
        what, todo = core.explain(entry["status"], entry.get("notes", []), entry.get("flattened", 0),
                                  entry.get("withheld", []))
        self.banner.setObjectName({"ok": "good", "partial": "warn"}.get(entry["status"], "bad"))
        self.banner.setStyleSheet("")           # re-read the object name
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setText(f"{what}  {todo}")
        locked = entry["status"] == "skipped" and "password" in " ".join(entry.get("notes", []))
        self.btn_flatten.setVisible(entry["status"] == "FAILED")
        self.unlock.setVisible(locked)
        self.btn_unlock.setVisible(locked)
        self.mode.setEnabled(bool(entry.get("output")))
        if not entry.get("output"):
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(0)
            self.mode.blockSignals(False)
        self.render()

    # ---- the override being edited for the current document
    def _overrides(self, key: str | None = None) -> dict:
        key = key or self.key
        if key not in self.edits:
            entry = next(e for k, e in self.entries if k == key)
            saved = entry.get("overrides") or {}
            self.edits[key] = {"rasterize": bool(saved.get("rasterize")),
                               "pages": {int(p): {"skip": [list(r) for r in v.get("skip", [])],
                                                  "add": [list(r) for r in v.get("add", [])]}
                                         for p, v in (saved.get("pages") or {}).items()}}
        return self.edits[key]

    def _page_edit(self) -> dict:
        return self._overrides()["pages"].setdefault(self.pno, {"skip": [], "add": []})

    @staticmethod
    def _norm(ov: dict) -> tuple:
        pages = tuple(sorted((int(p), tuple(sorted(map(tuple, v.get("skip", [])))),
                              tuple(sorted(map(tuple, v.get("add", [])))))
                             for p, v in (ov.get("pages") or {}).items() if v.get("skip") or v.get("add")))
        return (bool(ov.get("rasterize")), pages)

    def changed_docs(self) -> dict:
        out = {}
        for key, entry in self.entries:
            if key in self.edits and self._norm(self.edits[key]) != self._norm(entry.get("overrides") or {}):
                out[key] = self.edits[key]
        return out

    def _update_pending(self):
        changed = self.changed_docs()
        self.btn_apply.setEnabled(bool(changed))
        self.pending.setText(f"{len(changed)} document(s) have changes waiting. Apply them to rebuild those copies."
                             if changed else "No changes waiting.")

    # ---- drawing the page
    def render(self):
        entry = self._entry()
        if entry is None or self.session is None or self.working:
            return
        result_mode = self.mode.currentIndex() == 1 and entry.get("output")
        try:
            if result_mode:
                doc = core.pymupdf.open(str(self.session.out_dir / entry["output"]))
            else:
                if self.doc_key != self.key:
                    self._close_doc()
                    self.doc = self.session.scrubber.open(Path(self.key))
                    self.doc_key = self.key
                doc = self.doc
        except Exception:
            self.view.show_page(None, 1, [], False)
            self.page_label.setText("This document could not be opened for preview.")
            for w in (self.btn_prev, self.btn_nextp, self.btn_undo):
                w.setEnabled(False)
            return
        pages = doc.page_count
        self.pno = max(0, min(self.pno, pages - 1))
        if self.fit:
            room = max(self.view.viewport().width() - 26, 300)
            self.zoom = max(0.5, min(2.2, room / max(doc[self.pno].rect.width, 1)))
        ratio = max(1.0, float(self.devicePixelRatioF()))       # 1.5 or 2 on most laptops
        pix = doc[self.pno].get_pixmap(matrix=core.pymupdf.Matrix(self.zoom * ratio, self.zoom * ratio), alpha=False)
        image = QtGui.QImage(pix.samples, pix.width, pix.height, pix.stride,
                             QtGui.QImage.Format.Format_RGB888).copy()
        image.setDevicePixelRatio(ratio)
        if result_mode:
            doc.close()
        boxes = []
        if not result_mode:
            edit = self._overrides()["pages"].get(self.pno, {"skip": [], "add": []})
            for b in (entry.get("boxes") or {}).get(str(self.pno), []):
                if b.get("source") == "manual":
                    continue
                boxes.append(BoxItem(b["rect"], b["token"], "auto", self.zoom))
            for r in edit["skip"]:
                # a box the person rejected earlier no longer comes back from the scrubber
                same = next((x for x in boxes if x.state == "auto" and self._same(x.rect_pt, r)), None)
                if same:
                    same.state = "kept"
                    same.restyle()
                else:
                    boxes.append(BoxItem(r, "", "kept", self.zoom))
            for r in edit["add"]:
                boxes.append(BoxItem(r, "[REDACTED]", "drawn", self.zoom))
        self.view.show_page(image, self.zoom, boxes, editable=not result_mode)
        scanned = (self.pno + 1) in (entry.get("scanned_pages") or [])
        self.page_label.setText(f"Page {self.pno + 1} of {pages}" + ("   (scanned page - look closely)" if scanned else ""))
        self.btn_prev.setEnabled(self.pno > 0)
        self.btn_nextp.setEnabled(self.pno < pages - 1)
        self.btn_undo.setEnabled(not result_mode)

    @staticmethod
    def _same(a, b) -> bool:
        return all(abs(x - y) < 0.6 for x, y in zip(a, b))

    def _turn(self, step: int):
        self.pno += step
        self.render()

    def _zoom(self, factor: float):
        self.fit = False
        self.zoom = max(0.5, min(3.0, self.zoom * factor))
        self.render()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fit and self.session is not None and self.isVisible():
            QtCore.QTimer.singleShot(0, self.render)

    # ---- editing
    def _box_clicked(self, item: BoxItem):
        edit = self._page_edit()
        if item.state == "auto":
            edit["skip"].append(list(item.rect_pt))
        elif item.state == "kept":
            edit["skip"] = [r for r in edit["skip"] if not self._same(r, item.rect_pt)]
        else:
            edit["add"] = [r for r in edit["add"] if not self._same(r, item.rect_pt)]
        self.render()
        self._update_pending()

    def _box_drawn(self, rect: list):
        self._page_edit()["add"].append([round(v, 2) for v in rect])
        self.render()
        self._update_pending()

    def _undo_page(self):
        entry = self._entry()
        saved = ((entry.get("overrides") or {}).get("pages") or {})
        page = saved.get(self.pno) or saved.get(str(self.pno)) or {}
        self._overrides()["pages"][self.pno] = {"skip": [list(r) for r in page.get("skip", [])],
                                                "add": [list(r) for r in page.get("add", [])]}
        self.render()
        self._update_pending()

    def _apply(self):
        changed = self.changed_docs()
        if changed:
            self.apply_changes.emit(changed)

    def _flatten(self):
        ov = dict(self._overrides())
        ov["rasterize"] = True
        self.edits[self.key] = ov
        self.apply_changes.emit({self.key: ov})

    def _unlock(self):
        text = self.unlock.text()
        if text and self.session is not None:
            if text not in self.session.args.password:
                self.session.args.password.append(text)
            self.unlock.clear()
            self.apply_changes.emit({self.key: self._overrides()})

    def _mark_checked(self, on: bool):
        entry = self._entry()
        if entry is not None and self.session is not None:
            entry["reviewed"] = bool(on)
            self.session.project.save()
            self._retitle()

    # ---- searching the results
    def _search(self):
        self.hits.clear()
        needle = self.search.text().strip()
        if not needle or self.session is None:
            return
        squashed = re.sub(r"[\s.\-]", "", needle).lower()
        count = 0
        for key, entry in self.entries:
            if not entry.get("output"):
                continue
            path = self.session.out_dir / entry["output"].replace(".redacted.pdf", ".redacted.txt")
            if not path.is_file():
                continue
            page = 0
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                mm = re.match(r"^===== page (\d+) =====$", line)
                if mm:
                    page = int(mm.group(1))
                    continue
                plain = line.lower()
                if needle.lower() in plain or (len(squashed) >= 4 and squashed in re.sub(r"[\s.\-]", "", plain)):
                    count += 1
                    item = QtWidgets.QListWidgetItem(f"doc_{entry['n']:03d}, page {page}: {line.strip()[:60]}")
                    item.setData(Qt.ItemDataRole.UserRole, (key, page))
                    self.hits.addItem(item)
        self.search_note.setText(f"Found {count} time(s). Double-click one to go there, then black it out or add it "
                                 "to your names list and scrub again." if count else
                                 "Not found in the scrubbed text. Scanned page images are not searched - look at "
                                 "those yourself.")

    def _open_hit(self, item):
        key, page = item.data(Qt.ItemDataRole.UserRole)
        row = next((i for i, (k, _) in enumerate(self.entries) if k == key), -1)
        if row >= 0:
            self.docs.setCurrentRow(row)
            self.pno = max(0, page - 1)
            self.render()

    def set_busy(self, busy: bool, text: str = ""):
        self.working = busy
        for w in (self.docs, self.view, self.btn_apply, self.btn_flatten, self.btn_unlock, self.btn_back,
                  self.btn_prev, self.btn_nextp, self.mode, self.btn_undo):
            w.setEnabled(not busy)
        if busy:
            self._close_doc()
            self.pending.setText(text)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow if QtWidgets else object):
    STEPS = ["1   Choose PDFs", "2   Names to remove", "3   Scrub", "4   Review"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        screen = QtGui.QGuiApplication.primaryScreen()
        room = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 1280, 800)
        self.resize(max(820, min(1180, room.width() - 60)), max(560, min(780, room.height() - 70)))
        self.session: core.Session | None = None
        self.signature = None
        self.worker: Worker | None = None
        self.bridge = Bridge()
        self.bridge.event.connect(self._on_event)

        shell = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QtWidgets.QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        sbox = QtWidgets.QVBoxLayout(side)
        sbox.setContentsMargins(0, 0, 0, 0)
        sbox.setSpacing(0)
        sbox.addWidget(label(APP_NAME, "brand", wrap=False))
        sbox.addWidget(label(f"version {core.VERSION}", "brandsub", wrap=False))
        self.steps = QtWidgets.QListWidget()
        self.steps.setObjectName("steps")
        self.steps.addItems(self.STEPS)
        self.steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.steps.itemClicked.connect(lambda item: self.goto(self.steps.row(item)))
        sbox.addWidget(self.steps, 1)
        sbox.addWidget(label("Everything happens on this computer. Nothing is uploaded.", "private"))
        outer.addWidget(side)

        self.stack = QtWidgets.QStackedWidget()
        self.files = FilesPage()
        self.names = NamesPage()
        self.scrub = ScrubPage()
        self.review = ReviewPage()
        for page in (self.files, self.names, self.scrub, self.review):
            holder = QtWidgets.QScrollArea()    # on a small screen the page scrolls instead of hiding parts
            holder.setWidgetResizable(True)
            holder.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            holder.setWidget(page)
            self.stack.addWidget(holder)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(shell)

        self.files.go_next.connect(lambda: self.goto(1))
        self.files.go_review.connect(self.review_existing)
        self.files.changed.connect(self._settings_changed)
        self.names.go_back.connect(lambda: self.goto(0))
        self.names.scan.connect(self.start_scan)
        self.names.scrub.connect(self.start_scrub)
        self.names.cancel.connect(self.cancel)
        self.names.shrunk.connect(self._names_shrunk)
        self.scrub.cancel.connect(self.cancel)
        self.scrub.go_review.connect(lambda: self.goto(3))
        self.scrub.go_back.connect(lambda: self.goto(1))
        self.review.go_back.connect(lambda: self.goto(2 if self.scrub.table.rowCount() else 0))
        self.review.apply_changes.connect(self.apply_review)
        self.reached = 0
        self.goto(0)

    # ---- navigation
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def goto(self, index: int):
        if self.busy() or index > self.reached + 1:
            self.steps.setCurrentRow(self.stack.currentIndex())
            return
        if index == 1 and not self.files.btn_next.isEnabled():
            index = 0
        if index >= 2 and self.session is None:
            index = min(self.stack.currentIndex(), 1)
        if index == 3 and self.session is not None:
            self.review.load(self.session, keep_position=self.stack.currentIndex() == 3)
        self.reached = max(self.reached, index)
        self.stack.setCurrentIndex(index)
        self.steps.setCurrentRow(index)
        for row in range(self.steps.count()):
            item = self.steps.item(row)
            reachable = row <= self.reached + 1 and (row < 2 or self.session is not None)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable if reachable
                          else Qt.ItemFlag.NoItemFlags)

    # ---- the session behind the window
    def _signature(self):
        return (tuple(str(f) for f in self.files.selected()), self.files.out.text().strip(),
                tuple(self.files.passwords), self.files.thorough.isChecked(), self.files.fresh.isChecked())

    def _settings_changed(self):
        if self.session is not None and self._signature() != self.signature and not self.busy():
            self.session = None                 # what was scanned no longer matches what is selected
            self.names.set_scanned(False)
            self.reached = min(self.reached, 1)

    def _names_shrunk(self):
        if self.session is not None and not self.busy():
            self.session = None                 # the scan still holds the name that was just removed
            self.names.set_scanned(False)
            self.names.status.setText("You took a name off the list, so the documents need scanning again.")
            self.reached = min(self.reached, 1)

    def make_session(self, files: list[Path]) -> core.Session:
        out = Path(self.files.out.text().strip()).expanduser()
        fresh = self.files.fresh.isChecked() and self.files.fresh.isVisible()
        if fresh:
            for old in core.previous_outputs(out):
                try:
                    old.unlink()
                except OSError:
                    pass
        terms = core.default_terms_path()
        write_user_terms(terms, self.names.rows())
        argv = [str(f) for f in files] or ["-"]
        argv += ["-o", str(out)]
        argv += ["--terms", str(terms)] if self.names.rows() else ["--no-terms"]
        for secret in self.files.passwords:
            argv += ["--password", secret]
        if not self.files.thorough.isChecked():
            argv.append("--fast-ocr")
        if fresh:
            argv.append("--fresh")
        session = core.Session(core.build_parser().parse_args(argv),
                               lambda kind, **info: self.bridge.event.emit(kind, info))
        self.signature = self._signature()
        return session

    def _run(self, job, done):
        self.worker = Worker(job, self)
        self.worker.finished_ok.connect(done)
        self.worker.crashed.connect(self._crashed)
        self.worker.start()

    def _crashed(self, trace: str):
        note_error(trace)
        self.names.set_busy(False)
        self.review.set_busy(False)
        self.scrub.finish(True, "Something went wrong. Nothing unsafe was saved.")
        QtWidgets.QMessageBox.critical(self, APP_NAME, "Something went wrong and the job stopped.\n\n"
                                       + trace.strip().splitlines()[-1]
                                       + f"\n\nNothing unsafe was saved. The details are in:\n{error_log()}")

    def cancel(self):
        if self.session is not None and self.busy():
            self.session.cancel()

    # ---- step 2: scan
    def start_scan(self):
        files = self.files.selected()
        if not files or self.busy():
            return
        try:
            self.session = self.make_session(files)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, APP_NAME, f"Could not start: {exc}")
            return
        session = self.session
        self.names.progress.setRange(0, len(files))
        self.names.progress.setValue(0)
        self.names.set_busy(True, "Reading your documents...")

        def job():
            session.scrubber.cancelled = False
            session.scan(files)
            return True

        self._run(job, self._scan_done)

    def _scan_done(self, ok):
        self.names.set_busy(False)
        if not ok or self.session is None:
            self.session = None
            self.names.status.setText("Scan cancelled.")
            return
        terms = self.session.terms
        kept = [{"phrase": key, "kind": "ENTITY", "token": None, "key": key} for _, key in terms.keep]
        self.names.show_found(list(terms.learned), kept)
        self.names.status.setText("Scan finished. Check the list on the right, then press 'Scrub now'.")

    # ---- step 3: scrub
    def start_scrub(self):
        if self.session is None or self.busy():
            return
        session, files = self.session, self.files.selected()
        if self._signature() != self.signature:
            self._settings_changed()
            return
        terms = session.terms
        for record, remove in self.names.choices():
            if remove:
                terms.remove_again(record["phrase"], record["kind"], record.get("token"))
            else:
                terms.keep_visible(record["phrase"])
        if self.names.rows():
            terms.load(core.default_terms_path())      # picks up anything typed after the scan
        self.reached = max(self.reached, 2)
        self.goto(2)
        self.scrub.start(len(files), session.out_dir)

        def job():
            session.scrubber.cancelled = False
            cancelled = False
            try:
                for n, f in enumerate(files, 1):
                    self.bridge.event.emit("file_start", {"index": n, "total": len(files), "name": f.name})
                    res = session.scrub(f)
                    what, todo = core.explain(res.status, res.notes, res.raster_pages, res.withheld_pages)
                    self.bridge.event.emit("file_done", {"index": n, "name": f.name, "status": res.status,
                                                         "what": what, "todo": todo})
            except core.Cancelled:
                cancelled = True
            session.finish()
            return {"cancelled": cancelled}

        self._run(job, self._scrub_done)

    def _scrub_done(self, outcome):
        if self.session is None:
            return
        done = self.session.project.done()
        clean = sum(1 for f in done if f["status"] == "ok")
        other = len(done) - clean
        cancelled = bool(outcome and outcome.get("cancelled")) or outcome is None
        summary = f"{clean} document(s) scrubbed" + (f", {other} need attention (see the table)." if other else ".")
        self.scrub.finish(cancelled, summary + "  Next, look through every page before you share anything.")
        self.reached = 3
        self.goto(2)

    def _on_event(self, kind: str, info: dict):
        if kind == "scan_file":
            self.names.progress.setValue(info["index"] - 1)
            self.names.status.setText(f"Reading document {info['index']} of {info['total']}...")
        elif kind == "scan_page" and self.stack.currentIndex() == 1:
            base = self.names.status.text().split("  (")[0]
            self.names.status.setText(f"{base}  (page {info['page']} of {info['pages']})")
        elif self.stack.currentIndex() == 2:
            self.scrub.on_event(kind, info)
        elif kind == "page" and self.stack.currentIndex() == 3:
            self.review.pending.setText(f"Rebuilding... page {info['page']} of {info['pages']}")

    # ---- step 4: review
    def review_existing(self):
        """Open the review screen for a results folder scrubbed earlier."""
        if self.busy():
            return
        try:
            self.session = self.make_session([])
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, APP_NAME, f"Could not open that folder: {exc}")
            return
        self.reached = 3
        self.goto(3)

    def apply_review(self, changes: dict):
        if self.session is None or self.busy() or not changes:
            return
        session = self.session
        self.review.set_busy(True, "Rebuilding the changed documents...")

        def job():
            session.scrubber.cancelled = False
            for path, overrides in changes.items():
                session.scrub(Path(path), overrides)
            session.finish()
            return True

        self._run(job, self._review_applied)

    def _review_applied(self, _):
        self.review.set_busy(False)
        if self.session is not None:
            self.review.edits = {}
            self.review.load(self.session, keep_position=True)

    def closeEvent(self, e):
        if self.busy():
            self.cancel()
            self.worker.wait(15000)
        self.review._close_doc()
        e.accept()


def self_test() -> int:
    """Used by the release build: does the packaged app start, and does its scan reader work?"""
    log = core.data_dir() / "selftest.log"
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        prepare_app(app)
        window = MainWindow()
        app.processEvents()
        doc = core.pymupdf.open()
        doc.new_page(width=300, height=120).insert_text((30, 70), "SCRUB TEST 2468", fontsize=20)
        exe = core.find_tesseract()
        if not exe:
            raise RuntimeError("the scan reader (Tesseract) was not found")
        words = core.ocr_words(doc[0].get_pixmap(dpi=200), 200, 11, exe)
        read = " ".join(text for _, text in words)
        if "2468" not in read:
            raise RuntimeError(f"the scan reader misread the test page: {read!r}")
        window.close()
        log.write_text(f"ok - PDF Scrubber {core.VERSION}, scan reader at {exe}\n", encoding="utf-8")
        return 0
    except Exception:
        log.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


def install_missing() -> bool:
    """Running from source without the two packages: offer to fetch them."""
    wanted = Path(__file__).resolve().parent / "requirements.txt"
    print("The window needs two Python packages that are not installed yet (PyMuPDF and PySide6).")
    try:
        if not core.ask_yes("Download and install them now? (needs an internet connection, about 250 MB)"):
            return False
    except core.Quit:
        return False
    cmd = [sys.executable, "-m", "pip", "install"] + (["-r", str(wanted)] if wanted.is_file()
                                                       else ["pymupdf", "PySide6-Essentials"])
    return subprocess.call(cmd) == 0


def main() -> int:
    if QtWidgets is None or core.pymupdf is None:
        if install_missing():                   # start again in a fresh process (os.execv mangles
            return subprocess.call([sys.executable, str(Path(__file__).resolve())] + sys.argv[1:])  # spaces on Windows)
        print("\nOpening the text menu instead.\n")
        return core.interactive()
    if "--self-test" in sys.argv:
        return self_test()
    app = QtWidgets.QApplication(sys.argv)
    prepare_app(app)
    install_error_reporting()
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
