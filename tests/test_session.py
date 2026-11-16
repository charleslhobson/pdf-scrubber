"""Review decisions, the keep list, tags that survive between runs, cancelling."""
import pymupdf
import pytest

import builders
import scrub_pdfs as core
from conftest import parse, scrub, text_of, write_terms


@pytest.fixture
def k1(tmp_path):
    return builders.irs_k1(tmp_path)


def session_for(out, *flags):
    return core.Session(parse("x", "-o", out, "--no-terms", *flags))


def test_boxes_are_recorded_for_review(tmp_path, k1):
    _, results = scrub([k1.path], tmp_path / "out")
    boxes = results[k1.path.name].boxes[0]
    assert len(boxes) >= 8
    assert all(len(b["rect"]) == 4 and b["token"].startswith("[") and b["source"] == "auto" for b in boxes)


def test_rejecting_a_box_keeps_that_text_and_drawing_one_removes_more(tmp_path, k1):
    session, results = scrub([k1.path], tmp_path / "out")
    first = results[k1.path.name]
    assert "Ogden" in text_of(tmp_path / "out" / first.output)
    ein_box = next(b for b in first.boxes[0] if b["token"].startswith("[ID"))
    with session.scrubber.open(k1.path) as doc:
        ogden = doc[0].search_for("Ogden")[0] + (-2, -2, 2, 2)
    second = session.scrub(k1.path, {"pages": {0: {"skip": [ein_box["rect"]], "add": [list(ogden)]}}})
    assert second.status == "ok", second.notes
    text = text_of(tmp_path / "out" / second.output)
    assert "Ogden" not in text
    assert builders.P["fund_ein"] in text or builders.P["ssn"] in text      # the rejected box is visible again
    assert [b for b in second.boxes[0] if b["source"] == "manual"]
    assert second.output == first.output


def test_review_decisions_are_remembered(tmp_path, k1):
    session, _ = scrub([k1.path], tmp_path / "out")
    with session.scrubber.open(k1.path) as doc:
        ogden = list(doc[0].search_for("Ogden")[0] + (-2, -2, 2, 2))
    session.scrub(k1.path, {"pages": {0: {"skip": [], "add": [ogden]}}})
    session.finish()
    later = session_for(tmp_path / "out")
    again = later.scrub(k1.path)                           # no decisions passed in
    assert "Ogden" not in text_of(tmp_path / "out" / again.output)


def test_keep_list_leaves_a_business_name_visible_everywhere(tmp_path, k1):
    session = session_for(tmp_path / "out")
    session.scan([k1.path])
    entities = [r["phrase"] for r in session.terms.learned if r["kind"] == "ENTITY"]
    assert any("Tidewater" in e for e in entities)
    for phrase in entities:
        session.terms.keep_visible(phrase)
    result = session.scrub(k1.path)
    text = text_of(tmp_path / "out" / result.output)
    assert result.status == "ok" and "Tidewater Granite Partners" in text
    assert "Feldspar" not in text and builders.P["fund_ein"] not in text
    for phrase in entities:                                # a change of mind: remove them after all
        session.terms.remove_again(phrase, "ENTITY")
    assert "Tidewater" not in text_of(tmp_path / "out" / session.scrub(k1.path).output)


def test_adding_a_file_later_keeps_tags_and_numbering(tmp_path, k1):
    session, results = scrub([k1.path], tmp_path / "out", terms=write_terms(tmp_path))
    tags = dict(session.tokens.map)
    later = core.Session(parse("x", "-o", tmp_path / "out", "--terms", tmp_path / "names.txt"))
    assert later.project.resumed
    state = builders.state_return(tmp_path)
    later.scan([state.path])
    added = later.scrub(state.path)
    later.finish()
    assert added.output == "doc_002.redacted.pdf"
    assert all(later.tokens.map[k] == v for k, v in tags.items())
    ssn_tag = later.tokens.map[("TIN", "900124087")]
    assert ssn_tag in text_of(tmp_path / "out" / results[k1.path.name].output)
    assert ssn_tag in text_of(tmp_path / "out" / added.output)
    assert len(later.report()["files"]) == 2


def test_fresh_starts_over(tmp_path, k1):
    scrub([k1.path], tmp_path / "out")
    assert not session_for(tmp_path / "out", "--fresh").project.resumed
    assert session_for(tmp_path / "out").project.resumed


def test_memory_is_dropped_when_the_results_are_deleted(tmp_path, k1):
    scrub([k1.path], tmp_path / "out")
    for f in (tmp_path / "out").iterdir():
        f.unlink()
    assert not session_for(tmp_path / "out").project.resumed


def test_an_older_copy_is_removed_when_a_rescrub_is_held_back(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Wages 85,000   SSN 900-12-4087", fontsize=11)
    path = tmp_path / "doc.pdf"
    doc.save(path)
    session, results = scrub([path], tmp_path / "out")
    assert (tmp_path / "out" / results["doc.pdf"].output).is_file()
    doc = pymupdf.open(path)
    doc[0].insert_text((-400, 100), "900-12-4087", fontsize=11)        # now it cannot be verified
    doc.saveIncr()
    doc.close()
    assert session.scrub(path).status == "FAILED"
    assert not list((tmp_path / "out").glob("*.redacted.pdf"))


def test_cancel_stops_before_the_next_page_and_writes_nothing(tmp_path, k1):
    session = session_for(tmp_path / "out")
    session.cancel()
    with pytest.raises(core.Cancelled):
        session.scrub(k1.path)
    assert not list((tmp_path / "out").glob("*.redacted.pdf"))


def test_progress_is_reported_page_by_page(tmp_path):
    doc = builders.software_return(tmp_path)
    seen = []
    session = core.Session(parse("x", "-o", tmp_path / "out", "--no-terms"), lambda kind, **info: seen.append((kind, info)))
    session.scan([doc.path])
    session.scrub(doc.path)
    pages = [info["page"] for kind, info in seen if kind == "page"]
    assert pages == [1, 2, 3] and any(kind == "scan_file" for kind, _ in seen)


@pytest.mark.parametrize("status,notes,expect", [
    ("skipped", ["password-protected (supply the password)"], "password"),
    ("FAILED", ["a value redacted elsewhere is still stored inside this PDF (object 9)"], "Flatten"),
    ("partial", [], "scan reader"),
    ("ok", [], "before sharing"),
])
def test_every_outcome_has_a_plain_next_step(status, notes, expect):
    what, todo = core.explain(status, notes, 0, [2])
    assert what and expect.lower() in (what + " " + todo).lower()
