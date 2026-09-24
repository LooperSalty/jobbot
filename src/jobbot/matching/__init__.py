"""Relevance & ATS: skill lexicon, geocoding, job keywords, ATS document score, job matching.

Public modules (see docs/ARCHITECTURE.md "matching/"):
- ``lexicon``  — ``SKILLS``, ``SOFT_SKILLS``, ``canonicalize``, ``find_skills``
- ``geo``      — ``haversine_km``, ``geocode``
- ``keywords`` — ``extract_keywords``
- ``ats``      — ``score_document``, ``keyword_coverage``
- ``match``    — ``match_job``, ``MatchReport``, ``score_new_jobs``

Submodules are imported explicitly by callers (``from jobbot.matching.ats import score_document``) so that
importing the package stays cheap.
"""
