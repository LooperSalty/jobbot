"""Tailored CV & cover letter (see docs/ARCHITECTURE.md "documents/").

Public modules (imported explicitly by callers so that importing the package stays cheap):
- ``models``   — ``TailoredCV``, ``CoverLetter``, ``cv_to_text``, ``letter_to_text``, ``DocumentError``
- ``tailor``   — ``tailor_cv``, ``check_truthfulness``, ``truthful_missing``
- ``letter``   — ``write_cover_letter``
- ``render``   — ``render_cv_html``, ``render_cv_pdf``, ``render_cv_docx``, ``render_letter_pdf``, ``pdf_text``
- ``service``  — ``prepare_documents``
- ``importer`` — ``import_cv``, ``write_profile_yaml``

Private helpers: ``_format`` (dates, contact line), ``_truth`` (profile truth base), ``_cv_rules`` /
``_cv_llm`` (deterministic and LLM tailoring), ``_letter_rules`` (letter template), ``_fit`` / ``_pdf`` /
``_docx`` (one-page fitting, Chromium, Word), ``_import_rules`` (CV text parser).
"""
