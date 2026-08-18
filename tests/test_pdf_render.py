"""Tests for src/resume/pdf_render.py - .tex -> one-page PDF compilation.
Never touches resume content generation; these tests only exercise the
rendering step itself. Real-compilation tests are skipped (not failed) on a
machine without pdflatex installed, rather than faking a compile.
"""

import shutil

import pytest

from src.resume.pdf_render import PdfRenderError, _find_pdflatex, compile_tex_to_pdf

pdflatex_available = _find_pdflatex() is not None
requires_pdflatex = pytest.mark.skipif(not pdflatex_available, reason="pdflatex not installed on this machine")

_MINIMAL_ONE_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Hello, one-page resume.
\end{document}
"""

_TWO_PAGE_TEX = r"""
\documentclass{article}
\begin{document}
Page one.
\newpage
Page two.
\end{document}
"""

_INVALID_TEX = r"""
\documentclass{article}
\begin{document}
\undefinedcommandthatdoesnotexist{broken}
\end{document}
"""


def test_find_pdflatex_returns_none_when_no_candidate_exists(monkeypatch):
    monkeypatch.setattr("src.resume.pdf_render._PDFLATEX_CANDIDATES", ["/nonexistent/pdflatex", "totally-fake-binary-xyz"])
    assert _find_pdflatex() is None


def test_compile_tex_to_pdf_raises_when_pdflatex_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.resume.pdf_render._find_pdflatex", lambda: None)
    tex = tmp_path / "job_1_draft_1.tex"
    tex.write_text(_MINIMAL_ONE_PAGE_TEX)
    pdf = tmp_path / "pdf" / "job_1_draft_1.pdf"

    with pytest.raises(PdfRenderError, match="pdflatex not found"):
        compile_tex_to_pdf(tex, pdf)
    assert not pdf.exists()


@requires_pdflatex
def test_compile_tex_to_pdf_produces_one_page_pdf_with_correct_content(tmp_path):
    tex = tmp_path / "job_1_draft_1.tex"
    tex.write_text(_MINIMAL_ONE_PAGE_TEX)
    pdf = tmp_path / "pdf" / "job_1_draft_1.pdf"

    compile_tex_to_pdf(tex, pdf)

    assert pdf.exists()
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    assert len(reader.pages) == 1
    assert "Hello, one-page resume." in reader.pages[0].extract_text()
    # The source .tex must remain completely unchanged by compilation.
    assert tex.read_text() == _MINIMAL_ONE_PAGE_TEX


@requires_pdflatex
def test_compile_tex_to_pdf_rejects_multi_page_output(tmp_path):
    tex = tmp_path / "job_2_draft_1.tex"
    tex.write_text(_TWO_PAGE_TEX)
    pdf = tmp_path / "pdf" / "job_2_draft_1.pdf"

    with pytest.raises(PdfRenderError, match="2 page"):
        compile_tex_to_pdf(tex, pdf)
    # A failing compile must never leave a (wrong) PDF behind.
    assert not pdf.exists()


@requires_pdflatex
def test_compile_tex_to_pdf_raises_on_invalid_latex(tmp_path):
    tex = tmp_path / "job_3_draft_1.tex"
    tex.write_text(_INVALID_TEX)
    pdf = tmp_path / "pdf" / "job_3_draft_1.pdf"

    with pytest.raises(PdfRenderError):
        compile_tex_to_pdf(tex, pdf)
    assert not pdf.exists()


@requires_pdflatex
def test_compile_tex_to_pdf_creates_output_directory(tmp_path):
    tex = tmp_path / "job_4_draft_1.tex"
    tex.write_text(_MINIMAL_ONE_PAGE_TEX)
    pdf = tmp_path / "nested" / "does" / "not" / "exist" / "job_4_draft_1.pdf"

    compile_tex_to_pdf(tex, pdf)

    assert pdf.exists()
