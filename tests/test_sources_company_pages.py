"""Company careers pages (source ``company_ats``): JSON-LD postings, linked ATS, safe third-party page reading,
RSS safety. Offline (respx), fictitious employers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from jobbot.models import ContractType
from jobbot.sources import ats_fetch, ats_mapping, jsonld, public_page
from jobbot.sources.ats_detect import AtsBoard

FIX = Path(__file__).parent / "fixtures" / "sources_new"
NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
CAREERS = "https://www.thionville-soft.example/carrieres"


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def fetcher() -> ats_fetch.AtsFetcher:
    return ats_fetch.AtsFetcher(httpx.Client(), countries=frozenset({"FR", "LU"}))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(ats_fetch, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr("jobbot.clock.now", lambda: NOW)
    monkeypatch.setattr(public_page, "resolves_public", lambda host: True)  # fictitious .example hosts


@respx.mock
def test_careers_page_jsonld_and_linked_ats():
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text=text("careers_page.html")))
    result = fetcher().fetch_page(CAREERS, "Thionville Soft")
    dotnet, accountant = result.offers
    assert dotnet.title == "Développeur C# / .NET junior" and dotnet.external_id.endswith(":TS-2026-12")
    assert dotnet.location == "Thionville (57)" and dotnet.postal_code == "57100" and dotnet.raw["ats"] == "jsonld"
    assert dotnet.url == "https://www.thionville-soft.example/carrieres/developpeur-dotnet"
    assert "SQL Server" in dotnet.description and dotnet.contract_type == ContractType.CDI
    assert dotnet.raw["needs_details"] is True  # short listing text with its own offer page
    assert accountant.company == "Thionville Soft" and accountant.raw["country_code"] == "FR"
    assert result.linked_boards == [AtsBoard("teamtailor", "thionville-soft")]


@respx.mock
def test_third_party_pages_stay_on_public_addresses(monkeypatch):
    respx.get(CAREERS).mock(return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1:8501/"}))
    with pytest.raises(public_page.UnsafeUrl):
        fetcher().fetch_page(CAREERS)
    moved = "https://www.thionville-soft.example/nous-rejoindre"
    respx.get(CAREERS).mock(return_value=httpx.Response(301, headers={"Location": "/nous-rejoindre"}))
    respx.get(moved).mock(return_value=httpx.Response(200, text=text("careers_page.html")))
    assert len(fetcher().fetch_page(CAREERS).offers) == 2  # a public redirect is followed
    respx.get(moved).mock(return_value=httpx.Response(410))
    with pytest.raises(ats_fetch.BoardNotFound):
        fetcher().fetch_page(CAREERS)
    monkeypatch.undo()  # the real DNS check, with a fake resolver
    answers = {"prive.example": "10.0.0.5", "public.example": "93.184.216.34"}

    def getaddrinfo(host, *args, **kw):
        if host not in answers:
            raise OSError("introuvable")
        return [(2, 1, 6, "", (answers[host], 0))]

    monkeypatch.setattr(public_page.socket, "getaddrinfo", getaddrinfo)
    assert public_page.resolves_public("public.example") and not public_page.resolves_public("prive.example")
    assert not public_page.resolves_public("inconnu.example")
    for host in ("127.1", "0x7f.0.0.1", "10.0.0.5", "[::1]", "intranet", "nas.local"):
        assert not public_page.is_safe_url(f"http://{host}/carrieres")


@respx.mock
def test_page_body_is_capped(monkeypatch):
    monkeypatch.setattr(public_page, "MAX_PAGE_BYTES", 1000)
    respx.get(CAREERS).mock(return_value=httpx.Response(200, text="é" * 5000))
    final_url, page = public_page.fetch_public_page(httpx.Client(), CAREERS)
    assert final_url == CAREERS and len(page.encode("utf-8")) <= 1000


def test_jsonld_relative_url_and_hybrid_remote():
    page = "https://www.acme.example/carrieres"
    relative = ats_mapping.jsonld_posting(jsonld.PostingFields(title="Dev", url="/offres/42"), page)
    assert relative.url == "https://www.acme.example/offres/42" and relative.posting_id == relative.url
    script = ats_mapping.jsonld_posting(jsonld.PostingFields(title="Dev", url="javascript:alert(1)"), page)
    assert script.url == page and script.posting_id != page
    assert ats_mapping.jsonld_needs_details(relative, page) and not ats_mapping.jsonld_needs_details(script, page)
    assert ats_mapping._recruitee_remote({"remote": True, "hybrid": True}) is None
    hybrid = {"@type": "JobPosting", "title": "Dev", "jobLocationType": "TELECOMMUTE",
              "jobLocation": {"address": "12 rue de l'Exemple, 57000 Metz"}}
    fields = jsonld.posting_fields(hybrid)
    assert fields.remote is None and (fields.city, fields.postal_code) == ("Metz", "57000")
    assert jsonld.posting_fields({"title": "Dev", "jobLocationType": "TELECOMMUTE"}).remote is True
    deep = '<script type="application/ld+json">' + "[" * 50000 + "]" * 50000 + "</script>"
    assert jsonld.job_postings(deep) == []  # absurd nesting ignored, never a crash


def test_teamtailor_feed_with_dtd_is_refused():
    evil = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
    with pytest.raises(ats_mapping.ET.ParseError):
        ats_mapping.teamtailor_postings(evil)
    latin1 = '<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel><title>Société</title></channel></rss>'
    company, postings = ats_mapping.teamtailor_postings(latin1.encode("latin-1"))
    assert company == "Société" and postings == []  # bytes parsed with their declared encoding
