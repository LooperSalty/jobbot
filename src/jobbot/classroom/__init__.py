"""Offers relayed by the candidate's school in Google Classroom.

``watch.run_classroom`` reads the course stream (logged-in browser profile, or the optional Classroom API),
turns each new post into offers (LLM + deterministic fallback), scores them (school-relayed rules) and applies
at once to the adapted ones (``classroom.auto_apply``), with the usual quotas, dedup and truth gate.
"""
