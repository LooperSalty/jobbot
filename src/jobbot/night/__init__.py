"""Night scraper (« jobbot night »): while the server is idle, find many more relevant companies and contact
addresses, rank them and prepare the next day's documents — nothing is ever sent at night.

Modules: :mod:`run` (``run_night`` + steps), :mod:`budget` (time budget), :mod:`harvest` (registry around every
area, LBA recruiters), :mod:`hiring` (employers of the stored offers → SIREN, hiring signals), :mod:`store`
(dedupe / merge / relevance gate), :mod:`enrich` (parallel enrichment, one worker per domain),
:mod:`prioritize` (priority stored for the dashboard), :mod:`prepare` (documents ahead), :mod:`purge` (GDPR
retention), :mod:`report` (notification + last report), :mod:`status` (status / doctor / dashboard views).
Import them directly, e.g. ``from jobbot.night.run import run_night``.
"""
