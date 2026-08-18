"""Compiles a resume's LaTeX source into a one-page PDF using pdflatex -
the same toolchain used for the master resume (profile/resume.tex). Never
invents or alters content: compiles the .tex file exactly as it exists on
disk - a rendering step only, never a second content pass or Claude call.

pdflatex is a system dependency (via BasicTeX) that may not always be on
PATH depending on how the process was launched - _find_pdflatex() checks
common install locations rather than assuming shell PATH inheritance.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from pypdf import PdfReader

_PDFLATEX_CANDIDATES = [
    "pdflatex",  # already on PATH, if the process was launched from a shell that has it
    "/Library/TeX/texbin/pdflatex",  # BasicTeX/MacTeX default symlink location on macOS
]


class PdfRenderError(Exception):
    """A .tex file could not be compiled into a valid one-page PDF - missing
    pdflatex, a LaTeX compilation error, or a result that isn't exactly one
    page. Callers must catch this and proceed without a PDF (the .tex/DB
    draft remain the source of truth) - never fabricate or guess a PDF."""


def _find_pdflatex() -> str | None:
    for candidate in _PDFLATEX_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _pdf_page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def compile_tex_to_pdf(tex_path: Path, pdf_path: Path, timeout: int = 60) -> None:
    """Compiles tex_path (read-only - never modified) into pdf_path. Raises
    PdfRenderError on any failure. pdf_path is only ever written on complete
    success (valid compile AND exactly one page) - never a partial or
    multi-page file, so a caller can safely treat "no exception" as "a
    correct one-page PDF now exists at pdf_path"."""
    pdflatex = _find_pdflatex()
    if pdflatex is None:
        raise PdfRenderError("pdflatex not found (checked PATH and /Library/TeX/texbin)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_tex = Path(tmpdir) / tex_path.name
        shutil.copy(tex_path, tmp_tex)

        try:
            proc = subprocess.run(
                [pdflatex, "-interaction=nonstopmode", "-halt-on-error", tmp_tex.name],
                cwd=tmpdir, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfRenderError(f"pdflatex timed out after {timeout}s") from exc
        except OSError as exc:
            raise PdfRenderError(f"failed to launch pdflatex: {exc}") from exc

        log_text = proc.stdout + proc.stderr
        tmp_pdf = tmp_tex.with_suffix(".pdf")
        if not tmp_pdf.exists() or "Emergency stop" in log_text or "Fatal error" in log_text:
            raise PdfRenderError(f"pdflatex compilation failed:\n{log_text[-800:]}")

        try:
            page_count = _pdf_page_count(tmp_pdf)
        except Exception as exc:
            raise PdfRenderError(f"compiled PDF could not be read back: {exc}") from exc
        if page_count != 1:
            raise PdfRenderError(f"compiled PDF has {page_count} page(s), required exactly 1")

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_pdf, pdf_path)
