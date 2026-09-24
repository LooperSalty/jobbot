"""Spontaneous applications: company discovery (open data + LBA recruiters), website/e-mail enrichment,
named people (HR, tech managers, leaders), and the e-mail campaign.

Modules: :mod:`companies` (discover), :mod:`domains` (website + verification, geocoding), :mod:`emails`
(crawl, rank, enrich), :mod:`people` (named people + ranking), :mod:`selection` (who receives the e-mail under
``settings.outreach``), :mod:`spontaneous` (campaign, bounce retry). Import them directly, e.g.
``from jobbot.outreach.spontaneous import run_spontaneous_campaign``.
"""
