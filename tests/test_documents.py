"""Each kind of document: nothing personal survives, and every dollar amount does."""
import pytest

from conftest import leaks, missing

KINDS = ["irs_1040", "irs_k1", "irs_k1_fillable", "software_return", "fund_k1_package",
         "brokerage_1099", "state_return", "wording"]


def test_the_leak_check_itself_works(corpus):
    """Run the same check on the originals: it must find plenty, or the tests above prove nothing."""
    from conftest import original_text
    for kind in ("irs_1040", "software_return", "fund_k1_package", "brokerage_1099", "state_return"):
        doc = corpus["docs"][kind]
        original = original_text(doc.path).lower()
        present = [n for n in doc.gone if n.lower() in original]
        assert len(present) >= 8, (kind, present)


@pytest.mark.parametrize("kind", KINDS)
def test_document_is_written(corpus, kind):
    result = corpus["results"][kind]
    assert result.status == "ok", result.notes


@pytest.mark.parametrize("kind", KINDS)
def test_nothing_personal_survives(corpus, kind):
    found = leaks(corpus["out"], corpus["results"][kind], corpus["docs"][kind].gone)
    assert not found, found


@pytest.mark.parametrize("kind", KINDS)
def test_amounts_and_wording_survive(corpus, kind):
    lost = missing(corpus["out"], corpus["results"][kind], corpus["docs"][kind])
    assert not lost, lost


def test_same_person_gets_the_same_tag_everywhere(corpus):
    """The taxpayer's SSN appears in five documents and must carry one tag, so they can be read together."""
    session = corpus["session"]
    tags = {v for (kind, norm), v in session.tokens.map.items() if norm == "900124087"}
    assert len(tags) == 1, tags
    tag = tags.pop()
    from conftest import text_of
    with_tag = [k for k in ("irs_k1", "software_return", "fund_k1_package", "state_return")
                if tag in text_of(corpus["out"] / corpus["results"][k].output)]
    assert len(with_tag) == 4, with_tag


def test_output_names_do_not_carry_the_original_names(corpus):
    for result in corpus["results"].values():
        assert result.output.startswith("doc_") and "eldspar" not in result.output.lower()


def test_report_holds_no_personal_details(corpus):
    report = (corpus["out"] / "scrub_report.json").read_text(encoding="utf-8")
    for needle in ("Feldspar", "900-12-4087", "Tidewater", "K-1"):
        assert needle not in report


@pytest.mark.parametrize("form,allowed", [("f1040sd", 4), ("f1040se", 6), ("f1041sk1", 8), ("f1120ssk", 8)])
def test_only_name_and_id_boxes_are_removed_from_other_forms(tmp_path, form, allowed):
    """Fill every field of a form with an amount: only the handful of name/ID boxes may be blanked."""
    import builders
    from conftest import scrub
    doc = builders.blank_form_filled_with_amounts(tmp_path, form)
    _, results = scrub([doc.path], tmp_path / "out")
    result = results[doc.path.name]
    assert result.status == "ok", result.notes
    lost = missing(tmp_path / "out", result, doc)
    assert len(lost) <= allowed, f"{len(lost)} of {len(doc.kept)} amounts removed: {lost}"
