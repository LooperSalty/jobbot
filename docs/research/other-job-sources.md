# Other job sources for jobbot (WTTJ, Indeed/JobSpy, APEC, Free-Work, Meteojob, JobTeaser, L'Étudiant, Glassdoor, Choose Your Boss, and La bonne alternance)

Research date: **2026-09-23**. Every probe below was run live from a residential French IP (Windows, curl 8.17, Python 3.12.10, httpx 0.28.1, Playwright sync) unless the row says otherwise.
Candidate profile: junior full-stack developer (BUT Info, IUT Exemple), looking for a 24-month Bac+5 alternance (Exemple Digital School, start Oct 2026) or a junior CDI. Target area: Metz (57), Nancy (54), Thionville, Luxembourg, or remote.

---

## 1. TL;DR and recommendation for jobbot

**Main finding:** on 2026-09-23, very few **developer alternance** offers exist around Metz on any board. Two examples:
- Indeed returns 276 "alternance" offers within 50 km of Metz. Only about 2 have dev titles.
- WTTJ has 29 apprenticeships within 50 km of Metz. None is in tech.

Late September is the end of the alternance hiring season. For this candidate, finding companies for **spontaneous applications** matters more than scraping more boards. The official **La bonne alternance (LBA) API** does both jobs:
- it returns offers and also "recruteurs" (companies likely to hire apprentices);
- it has an official `POST /job/v1/apply` endpoint that emails the CV to the recruiter.

With LBA, jobbot does not need to scrape email addresses for these companies.

### Ranking: (volume for Metz / Grand Est dev alternance or junior dev) × (ease + legality of automated access)

| # | Source | Volume (Metz/GE dev), 1-5 | Access ease/legality, 1-5 | Score | Verdict |
|---|--------|---------------------------|---------------------------|-------|---------|
| 1 | **La bonne alternance API** (`api.apprentissage.beta.gouv.fr`) | 4 (aggregates France Travail + partner boards + "recruteurs LBA"; exact counts not verified, needs a key) | 5 (official, free, documented, has an apply endpoint) | **20** | **MUST** (get a key now) |
| 2 | **Indeed FR** via python-jobspy (internal mobile GraphQL) | 4 (largest raw volume: 47 "développeur" + 276 "alternance" within 50 km of Metz; LU works via `indeed-co: LU`) | 2.5 (works without proxy today; ToS explicitly forbids scrapers and automated apply) | **10** | **YES**, read-only, low rate, never auto-apply |
| 3 | **Free-Work** public API (`api.free-work.com`) | 1.5 (IT only: 17 CDI within 50 km of Metz incl. Esch-sur-Alzette; 94 in Luxembourg; ~0 alternance) | 4.5 (open JSON API, robots allow, no anti-bot seen) | **7** | **YES** for the junior CDI and Luxembourg track |
| 4 | LinkedIn (via JobSpy guest endpoints) | 3 (15 hits "alternance développeur" Metz, some relevant) | 2 | 6 | Covered by the LinkedIn research slug; JobSpy works without login |
| 5 | **Welcome to the Jungle** (Algolia) | 1.5 (Metz 50 km: 29 apprenticeships, 0 tech; 13 software jobs within 100 km, mostly senior) | 3.5 (public Algolia key, clean JSON, but ToS §10 forbids robots) | **5** | **YES**, cheap to add, low volume, very clean data (apply_url, ATS questions) |
| 6 | **APEC** (`/cms/webservices/rechercheOffre`) | 1.5 (28 "développeur" offers in 57+54, all CDI, mostly experienced; 0 dev alternance) | 3.5 (search open, no auth; detail endpoint behind DataDome) | **5** | **YES** for the CDI track, search only |
| 7 | Meteojob (SSR JSON) | 1.5 (25 "developpeur" Metz, noisy; 11 "alternance developpeur" Grand Est) | 3 (JSON embedded in HTML; robots disallow `/*?`) | 4.5 | OPTIONAL |
| 8 | L'Étudiant jobboard (Piloty) | 1 (15 alternance Metz, all sectors; aggregates LBA offers) | 2 (client-rendered) | 2 | SKIP: duplicates LBA |
| 9 | JobTeaser | 1 (Metz alternance: 9, 0 IT) | 1.5 (Cloudflare challenge after ~8 requests; apply needs a school/student account) | 1.5 | SKIP (the candidate can use the school's JobTeaser manually) |
| 10 | Glassdoor FR | 2 | 0.5 (Cloudflare `cf-mitigated: challenge`; JobSpy returns 400/403) | 1 | SKIP |
| 11 | Choose Your Boss | ? | 0.5 (Cloudflare block for both curl and headless Chrome) | ~0 | SKIP |
| 12 | Google Jobs (JobSpy `google`) | ? | 0 (JobSpy returns 0 rows: "initial cursor not found") | 0 | SKIP |

### Recommended set for jobbot

Wire in these sources:
- **Tier 1:** LBA API (search + recruiters + apply), Indeed via JobSpy (`site_name=["indeed"]`, `country_indeed="france"` and a second pass with `"luxembourg"`), and WTTJ Algolia.
- **Tier 2:** Free-Work API (junior CDI + Luxembourg), APEC search (CDI track).
- **Optional:** Meteojob.

**Apply policy:**
- **Allowed:** fully automatic apply only through the **LBA apply API**. This is its official purpose, and it is rate limited to 10 calls/min.
- **Not allowed:** never auto-submit on Indeed (ToS A.3 explicitly prohibits bots in the apply process), WTTJ (account + ToS §10), APEC or JobTeaser.
- **External ATS links:** for `apply_url` values from WTTJ, Indeed `viewJobUrl`, Meteojob `url.redirect` or Free-Work `applicationUrl`, hand off to jobbot's Playwright "assisted apply" step. It pre-fills the form and a human confirms the submit.

### Questions and suggestions for the user
1. Which RNCP code and rhythm does the Exemple Digital School Bac+5 use? LBA can filter by `rncp`, and WTTJ in-site forms often ask for "rythme + code RNCP" (seen live in `ats_questions`).
2. Can the candidate create a free LBA developer account (https://api.apprentissage.beta.gouv.fr/fr/compte/profil)? It gives a sandbox key at once. The production key (needed for real apply) must be requested by email at contact-api@labonnealternance.apprentissage.beta.gouv.fr.
3. For Luxembourg, is a French apprenticeship contract with a Luxembourg employer acceptable? That is rare. Or should jobbot keep Luxembourg for the CDI track only?
4. Given the date (23 Sept for an October start), switch jobbot's priority to spontaneous applications: LBA `recruiters[]`, plus the email-finding module in another slug.

---

## 2. Endpoints / URLs and auth

### 2.1 Welcome to the Jungle (WTTJ): Algolia search + public REST detail

**Credential discovery (runtime, no login).**
- The site is now Next.js (turbopack). The HTML inlines `self.__next_s.push(["/api/env",{"id":"runtime-env"}])`.
- `GET https://www.welcometothejungle.com/api/env` → `200 application/javascript`, body `window.env = {...};`. Relevant keys (LIVE):
  - `PUBLIC_ALGOLIA_APPLICATION_ID`: `CSEKHVMS53`
  - `PUBLIC_ALGOLIA_API_KEY_CLIENT`: `4bd8f6215d0cc52b26430765769e65a0`
  - `PUBLIC_ALGOLIA_ARTICLES_INDEX`: `wk_cms_articles_production`, `PUBLIC_ALGOLIA_ORGANIZATIONS_INDEX`: `wk_cms_organizations_production`
  - `PUBLIC_WTTJ_API_URL`: `https://api.welcometothejungle.com`
  - The jobs index name is **not** in env. It is `wttj_jobs_production_{locale}` (verified below).
- Parse this at startup and fall back to the constants above.

**Key restrictions.** `GET https://csekhvms53-dsn.algolia.net/1/keys/4bd8f6215d0cc52b26430765769e65a0` (sent with the key and a Referer) returned:
```json
{"value":"4bd8f6215d0cc52b26430765769e65a0","createdAt":1613058482,"acl":["search","browse"],"validity":0,
 "indexes":["wk_cms_articles_production*","wk_cms_organizations_production*","wk_cms_jobs_production*","wk_meetings_production*","wttj_jobs_production*"],
 "referers":["*.wk.test","https://wttj.test","https://www.welcometothejungle.com"],"description":"<redacted>"}
```
- The key has **no** `maxQueriesPerIPPerHour` and **no** `maxHitsPerQuery`.
- ACL includes **`browse`**, so the Browse API (cursor paging past the 1000-hit limit) is allowed.
- A **Referer header is mandatory.** Without it Algolia answers `403 {"message":"Method not allowed with this referer","status":403}`. `Referer: https://www.welcometothejungle.com/` alone is enough; Origin is optional.

**Endpoints**

| Purpose | Method | URL |
|---|---|---|
| Search | POST | `https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_fr/query` (fallback hosts: `csekhvms53-1.algolianet.com`, `-2`, `-3`) |
| Multi-query | POST | `https://csekhvms53-dsn.algolia.net/1/indexes/*/queries` body `{"requests":[{"indexName":"...","params":"query=...&hitsPerPage=..."}]}` |
| Full export | POST | `https://csekhvms53-dsn.algolia.net/1/indexes/wttj_jobs_production_fr/browse` body `{"query":"","filters":"...","hitsPerPage":1000}`, then `{"cursor":"..."}` |
| Job detail (no auth) | GET | `https://api.welcometothejungle.com/api/v1/organizations/{organization.slug}/jobs/{slug}` |
| Organization (no auth) | GET | `https://api.welcometothejungle.com/api/v1/organizations/{organization.slug}` |
| Public job page | GET | `https://www.welcometothejungle.com/fr/companies/{organization.slug}/jobs/{slug}` (200, has JSON-LD `JobPosting`) |

Headers for Algolia: `X-Algolia-Application-Id: CSEKHVMS53`, `X-Algolia-API-Key: 4bd8f6215d0cc52b26430765769e65a0`, `Referer: https://www.welcometothejungle.com/`, `Content-Type: application/json`.

**Index names (LIVE):**
- `wttj_jobs_production_fr`: 200. This is the main index; it holds every job language (`language` facet: fr, en, es, …).
- `wttj_jobs_production_fr_published_at_desc`: 200 (sort replica).
- `wttj_jobs_production_en`: 200 (EN locale).
- `wttj_jobs_production`, `…_fr_published_at_asc`, `…_fr_salary_desc`: **404** "Index … does not exist".
- `wk_cms_jobs_production`: 200 (another, CMS-side index; ids are numeric; do not use).
- `GET /1/indexes/…/settings`: 403 "Method not allowed with this API key".

**Filterable attributes and values (LIVE facet dump, 2026-09-23):**
- `contract_type`: `full_time`, `internship`, `apprenticeship`, `freelance`, `temporary`, `other`, `vie`, `part_time`, `graduate_program`, `idv`
- `remote`: `unknown`, `partial`, `no`, `punctual`, `fulltime`
- `education_level`: `bac_5`, `bac_2`, `bac_3`, `no_diploma`, `bac_4`, `bac`, `phd`, `cap`, `bac_1`, `bep`
- `offices.country_code`: `FR`, `LU`, …; `offices.state`: `"Grand Est"`; `offices.district`: `Moselle`, `Meurthe-et-Moselle`; `offices.city`: `Metz`, `Nancy`, …
- `new_profession.category_reference`: `tech-engineering-3NjUy` (Tech & Engineering)
- `new_profession.sub_category_reference`:
  - `software-web-development-iMzA4`
  - `data-business-intelligence-yZjY1`
  - `cloud-computing-and-devops-mZTM2`
  - `cybersecurity-2MDhm`
  - `network-engineering-and-administration-yZjhm`
  - `it-support-and-services-3Njcy`
  - `engineering-disciplines-4YWVi` (mechanics etc. Exclude it.)
- `new_profession.pivot_reference`, e.g. `full-stack-developer-5YTYy`
- Numeric: `published_at_timestamp` (unix seconds), `experience_level_minimum` (float years), `salary_yearly_minimum`
- `language`, `is_boosted`, `organization.slug`, `organization.name`

**Query params that work (LIVE):**
- `query`, `hitsPerPage`, `page`
- `filters` (SQL-like: `contract_type:apprenticeship AND offices.country_code:FR AND new_profession.category_reference:tech-engineering-3NjUy`; quote values with spaces: `offices.state:'Grand Est'`)
- `facetFilters` (`[["offices.district:Moselle","offices.district:Meurthe-et-Moselle"]]` = OR inside the inner list)
- `numericFilters` (`["published_at_timestamp>=1790121663"]`)
- `aroundLatLng` (`"49.1193,6.1757"` = Metz), `aroundRadius` (**metres**, e.g. `50000`)
- `facets` (`["*"]` or a list), `attributesToRetrieve`

**Apply flow:**
- The detail JSON has `ats: "external"` + `apply_url` (SmartRecruiters, aplitrak, contactrh, the company's own site, …). This means an external ATS.
- Or it has `ats: "wkit"` + `apply_url: null`. This means the in-site Welcome Kit form, which **requires a WTTJ account**: clicking "Postuler" shows the sign-up form (DOC-SOURCED help center).
- `application_fields` lists `resume` (always mandatory), `cover_letter` (optional/mandatory), `media_linkedin`, `media_github`, `portfolio`, `picture`, … each with `mode: mandatory|optional|disabled`.
- `ats_questions` holds custom questions, e.g. "For apprenticeships: please provide your apprenticeship rhythm and the RNCP code of your programme." (required TEXT).

**Front-end URL, for humans only.** Robots disallow query URLs. Example: `https://www.welcometothejungle.com/fr/jobs?query=developpeur&refinementList[contract_type][]=apprenticeship` (200). The `aroundRadius` unit in the front-end URL is not verified.

### 2.2 Indeed France: python-jobspy (and the raw endpoint it uses)

**Library (LIVE PyPI JSON, `https://pypi.org/pypi/python-jobspy/json`):**
- Latest `python-jobspy` **1.1.82**, uploaded **2025-07-28**. No newer PyPI release in over a year.
- `requires_python >=3.10,<4.0`.
- Deps: `NUMPY==1.26.3` (**hard pin**), `pandas>=2.1,<3`, `pydantic>=2.3,<3`, `requests>=2.31,<3`, `tls-client>=1.0.1,<2`, `markdownify>=0.13.1,<0.14`, `regex>=2024.4.28,<2025`, `beautifulsoup4>=4.12.2,<5`.
- Installed fine in a fresh Py 3.12.10 venv (numpy 1.26.3, pandas 2.3.3, tls-client 1.0.1).

**Repository:**
- The repo **moved**: `github.com/cullenwatson/JobSpy` returns 301 to **`github.com/speedyapply/JobSpy`** (4.3k stars, 66 open issues, last push 2026-02-18, commit `fda080a373e8226f3fd60635323f5da9af9892b1`).
- `main` has fixes that are **not on PyPI**: numpy relaxed to `>=1.26.0` (#337), `user_agent` kwarg fix (#295), LinkedIn date-parsing fallback (#343). `pyproject.toml` on main still says `1.1.82` (poetry-core build).
- Recommended install: `pip install "python-jobspy @ git+https://github.com/speedyapply/JobSpy@fda080a373e8226f3fd60635323f5da9af9892b1"`. Fallback: `python-jobspy==1.1.82` in an **isolated venv**, because of the numpy pin.

**Supported sites** (`Site` enum, `jobspy/__init__.py` on main): `linkedin`, `indeed`, `zip_recruiter`, `glassdoor`, `google`, `bayt`, `naukri`, `bdjobs`.

**`scrape_jobs()` signature (main):**
```python
scrape_jobs(site_name=None, search_term=None, google_search_term=None, location=None,
            distance=50,            # MILES (Indeed GraphQL is called with radiusUnit: MILES)
            is_remote=False, job_type=None,   # "fulltime" | "parttime" | "internship" | "contract" (no apprenticeship value!)
            easy_apply=None, results_wanted=15, country_indeed="usa",   # use "france" / "luxembourg"
            proxies=None, ca_cert=None, description_format="markdown",  # or "html"
            linkedin_fetch_description=False, linkedin_company_ids=None,
            offset=0, hours_old=None, enforce_annual_salary=False, verbose=0, user_agent=None, **kwargs) -> pd.DataFrame
```

**Indeed limits:**
- Only **one** of these per search: `hours_old` | (`job_type` and/or `is_remote`) | `easy_apply`. Issue #353: `is_remote` is silently ignored when `hours_old` is set.
- `country_indeed` values relevant here: `france` → `fr.indeed.com`, `indeed-co: FR`, Glassdoor `fr`; `luxembourg` → `lu`, no Glassdoor; `belgium` → `fr:be`; `germany`.

**Returned DataFrame columns** (`desired_order`, exact order, LIVE from a real run):
```
id, site, job_url, job_url_direct, title, company, location, date_posted, job_type, salary_source,
interval, min_amount, max_amount, currency, is_remote, job_level, job_function, listing_type, emails,
description, company_industry, company_url, company_logo, company_url_direct, company_addresses,
company_num_employees, company_revenue, company_description, skills, experience_range, company_rating,
company_reviews_count, vacancy_count, work_from_home_type
```
- `id`: `in-<jobkey>` for Indeed.
- `location`: e.g. `"Metz, GE, FR"`.
- `emails`: comma-joined emails regex-extracted from the description. Useful for spontaneous-application contacts.
- The frame is sorted by `site`, then `date_posted` descending.

**Live run results (2026-09-23):**
- `indeed`, `search_term="alternance développeur"`, `location="Metz"`, `distance=30`, `country_indeed="france"`: **7 rows in 0.3 s**. Non-null: description 7/7, job_url_direct 7/7, company_addresses 4/7, job_type 2/7.
- `linkedin`, same term, `location="Metz, Grand Est, France"`: **15 rows in 3.9 s**. With `linkedin_fetch_description=True`, descriptions come back (2.7-4.4 k chars), `job_level` and `job_function` are filled, and `job_url_direct` is **None** (issue #370).
- `glassdoor` (FR): **0 rows**, logs "Glassdoor response status code 400 / location not parsed" and "bad response status code: 403".
- `google`: **0 rows**, "initial cursor not found".

**Underlying Indeed endpoint (what JobSpy calls; you can call it directly with httpx):**
- `POST https://apis.indeed.com/graphql`
- Headers:
  - `indeed-api-key: 161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8` (the Indeed iOS app key)
  - `indeed-co: FR` (or `LU`)
  - `indeed-locale: fr-FR`
  - `user-agent: Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) … Indeed App 193.1`
  - `indeed-app-info: appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone`
  - `content-type: application/json`
- Body `{"query": "query G { jobSearch( what: \"…\" location: {where: \"Metz (57)\", radius: 50, radiusUnit: KILOMETERS} limit: 100 cursor: \"…\" sort: RELEVANCE|DATE filters: {…} ) { pageInfo { nextCursor } results { job { … } } } }"}`.
- 200 JSON. No rate-limit headers were returned.
- Attribute keys seen on FR results (LIVE). Use them in `filters: { composite: { filters: [{ keyword: { field: "attributes", keys: ["QADT5"] } }] } }`:
  - `CPAHG` = Alternance
  - `QADT5` = Contrat d'apprentissage
  - `6UN82` = Contrat de professionnalisation
  - `5QWDV` = CDI
  - `T9BXE` = CDD
  - `VDTG7` = Stage
  - `CF3CP` = Temps plein
  - `75GKK` = Temps partiel
  - `DSQF7` = Télétravail
  - `PAXZC` = Télétravail partiel
  - `ZG59D` = Indépendant / freelance
- **Several keys in one `keys: [...]` behave as AND, not OR** (LIVE): `["CPAHG","QADT5"]` returned 0, while `["QADT5"]` alone returned 2. Run one query per contract key and merge the results.
- Date filter: `filters: { date: { field: "dateOnIndeed", start: "72h" } }`.
- Remote-only keyword key: `DSQF7`.

**Volume probes (LIVE, radius 50 km):**

| what | where | Results |
|---|---|---|
| "alternance" | Metz (57) | **276** over 3 pages; dev-like titles ≈ 2 |
| "alternance" | Nancy (54) | 230 |
| "développeur" | Metz | 47 |
| "développeur" + CDI key | Metz | 24 |
| "développeur" (with `indeed-co: LU`) | Luxembourg | ≥100 (has next cursor) |
| "développeur" + CPAHG | Metz | **0**, because many alternance dev offers are tagged `QADT5`, not `CPAHG` |

**Job URLs:**
- `https://fr.indeed.com/viewjob?jk={key}`.
- `recruit.viewJobUrl` is the employer/ATS URL. Example: `https://jobs.chaussea.com/html/job/8wvzi9yi36`.

### 2.3 APEC (apec.fr): cadre / junior-cadre CDI offers

| Purpose | Method | URL | Auth / anti-bot |
|---|---|---|---|
| Search | POST | `https://www.apec.fr/cms/webservices/rechercheOffre` | none (LIVE 200 with curl/httpx) |
| Count | POST | `/cms/webservices/rechercheOffre/count` (same body) | not probed |
| Place autocomplete | GET | `https://www.apec.fr/cms/webservices/autocompletion/lieuautocomplete?q=Metz&max=5&byLeftAndRight=false` | none (LIVE 200) |
| Offer detail | GET | `https://www.apec.fr/cms/webservices/offre/public?numeroOffre=179469236W` | **DataDome 403** (`X-DataDome: protected`, body `{"url":"https://geo.captcha-delivery.com/…"}`) for curl and for headless Playwright (bundled Chromium and `channel="chrome"`) |
| Public offer page | GET | `https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{numeroOffre}` | SPA; loads the detail via the endpoint above |

Other endpoints seen in the JS bundle `/modules/apec-jahia-module/javascript/angular-output/offres/dist/main-WSTTYCDC.js`:
- `/cms/webservices/offre/public/liste`
- `/cms/webservices/autocompletion/metierAutocomplete`
- `/cms/webservices/candidature/avecCompte`
- `/cms/webservices/candidature/sansCompte/{token}`
- `/cms/webservices/pushOffre/abonnement/cadre/…` (email alerts)
- `/cms/webservices/panierOffre` (saved offers)

**Search body.** Headers: `Content-Type: application/json`, `Accept: application/json`, a browser UA; Origin/Referer `https://www.apec.fr` are optional. The server is Jackson-strict: unknown fields give **500** with a stack trace listing the allowed fields.
```json
{"motsCles":"developpeur",
 "lieux":["57","54"],                     // dept codes, region id "20074" (Grand Est) or commune lieuId e.g. "582374" (Metz)
 "typesContrat":["20053","597137","597139","597138","597140"],   // alternance family; CDI = "101888"
 "typesConvention":["143684","143685","143686","143687"],          // entreprise, cabinet, agence, SSII/ESN (143706 = partenaire)
 "niveauxExperience":[], "fonctions":[], "statutPoste":[], "secteursActivite":[], "typesTeletravail":[],
 "idsEtablissement":[], "idNomZonesDeplacement":[], "positionNumbersExcluded":[],
 "typeClient":"CADRE",
 "sorts":[{"type":"DATE","direction":"DESCENDING"}],         // or {"type":"SCORE",...}
 "pagination":{"range":20,"startIndex":0},
 "activeFiltre":true,
 "pointGeolocDeReference":{"distance":0}}                    // radius: {"distance":50,"latitude":49.108,"longitude":6.196} + a commune lieu
```

**The 34 accepted fields** (from the 500 error, LIVE):
`typesConvention, pointGeolocDeReference, idNomSalon, idNomZonesDeplacement, secteursActivite, dureeContrat, dateExecDernierPush, salaireMinimum, motsCles, datePriseDePoste, pagination, id, dureeStage, idsEtablissement, fonctions, niveauxExperience, typeClient, numeroOffre, salaireMaximum, anciennetePublication, niveauEtude, lieux, localisable, typesContrat, offreSalon, confidentielle, activeSurbrillance, positionNumbersExcluded, typesTeletravail, statutPoste, sorts, activeFiltre, idsNomOrigine, sirenExcluded`
- There is **no** top-level `distance`. Sending one gives 500 "Unrecognized field distance".

**Contract type enum** (from the JS bundle, LIVE):

| Code | Contract |
|---|---|
| 101887 | CDD |
| 101888 | CDI |
| 597171 | STAGE |
| 101930 | INTERIM |
| 20053 | ALTERNANCE |
| 101889 | MISSION_INTERIM |
| 597141 | CDI_INTERIMAIRE |
| 597137 | CDD_ALTERNANCE_CONTRAT_APPRENTISSAGE |
| 597139 | CDI_ALTERNANCE_CONTRAT_APPRENTISSAGE |
| 597138 | CDD_ALTERNANCE_CONTRAT_PROFESSIONNALISATION |
| 597140 | CDI_ALTERNANCE_CONTRAT_PROFESSIONNALISATION |

The front's alternance family is `[20053, 597137, 597139, 597138, 597140]`.

**Place ids** (from `lieuautocomplete`, LIVE):
- Metz: `lieuId 582374`, type `FR_COMMUNE`, lat 49.1082122, lon 6.1960061, `lieuxParentsIds ["57","20074"]`
- Grand Est: `lieuId 20074`, `FR_REGION`
- Departments are passed as plain codes: `"57"`, `"54"`

**Radius behaviour (LIVE):**
- `lieux=["582374"]` + `pointGeolocDeReference={"distance":30}` **without lat/lon** returned 2735 results (all France). This is a trap.
- With `latitude`/`longitude` added, it returned 28 (same as 57+54).
- Always send lat/lon with a radius.

**Volume (LIVE, 2026-09-23):**
- "developpeur", lieux 57: **12**; lieux 57+54: **28**. All `typeContrat 101888` (CDI); titles are mostly confirmed/senior plus some non-IT "développeur foncier/commercial".
- Alternance family, lieux 57+54, no keyword: **5**, 0 IT.
- "developpeur" + alternance, whole France: **6**.

### 2.4 Free-Work (IT freelance + CDI board): public API Platform JSON API

- `GET https://api.free-work.com/job_postings`: no auth, `200`. The same API is also served at `https://www.free-work.com/api/job_postings`.
  - `Accept: application/ld+json` returns a Hydra envelope with `hydra:totalItems`.
  - `Accept: application/json` returns a bare array.
  - The front also sends `X-Varnish-Public: 1` and `X-Jobs-Filtered: 1` (optional).
- **Params** (mapping from the Nuxt bundle `/_nuxt/YoxSCc3f.js`, `Ehe` object; LIVE-tested where noted):

| Param | Values / notes |
|---|---|
| `page`, `itemsPerPage` | `itemsPerPage=500` accepted (LIVE) |
| `searchKeywords` | free text (LIVE) |
| `contracts` | comma list: `permanent`, `fixed-term`, `contractor`, `apprenticeship`, `internship` (LIVE) |
| `locationKeys` | `fr~grand-est~~` (region), `fr~grand-est~moselle~` (dept), `fr~grand-est~moselle~metz` (city), `lu~~~` (Luxembourg) (all LIVE) |
| `locationRadius` | km, used with a city key (LIVE: Metz + 50 includes Nancy and Esch-sur-Alzette) |
| `publishedSince` | `less_than_24_hours`, `less_than_7_days`, … |
| `remoteMode` | e.g. `partial`, `full`, `none`. Only `partial` was observed live; `full` and `none` are guesses. |
| `experienceLevels` | `junior`, `intermediate`, `senior`, `expert` (values observed) |
| `minAnnualSalary`, `minDailySalary`, `minDuration`, `maxDuration`, `skills`, `companies`, `employmentTime`, `order` | other filters from the bundle |

- The Hydra template also exposes `order[publishedAt]`, `createdAt[after]`, `updatedAt[after]`, `q` (title/reference).
- **Detail:** `GET https://api.free-work.com/job_postings/{slug}?jobSlug={job.slug}` → 200 (LIVE). Get-by-numeric-id `/job_postings/{id}` → 404.
- **Public URL:** `https://www.free-work.com/fr/tech-it/job-mission/{job.slug}/{slug}` (LIVE; the legacy `/fr/tech-it/{job.slug}/job-mission/{slug}` 301s to it).
- **Apply:**
  - `applicationType: "turnover"` (100/100 of a "developpeur" sample) = apply on Free-Work with an account (`POST /applications` in the bundle).
  - `applicationType: "url"` → external `applicationUrl`.
  - `applicationContact` / `applicationForm` exist but were null in the sample.
- **Volume (LIVE):**
  - All: 9052
  - "developpeur": 1051
  - "developpeur" + Grand Est: **24**
  - `permanent` + Moselle: **5**
  - `permanent` + Metz radius 50: **17** (includes 2 in Esch-sur-Alzette LU)
  - Luxembourg (all): **94**
  - `apprenticeship` in Grand Est: **0**; across all locations: 74, mostly UK "Trainee" posts

### 2.5 Meteojob (CleverConnect)

- `GET https://www.meteojob.com/jobs?what=developpeur&where=Metz%20(57)` → 200. The old `/jobsearch/offers?...` gives a 302 to `/jobs?...`.
- The page is Angular SSR. All data is in `<script id="candidate-front-state" type="application/json">` (plain JSON). Keys (LIVE):
  - `app:search:offers` → `{total, content[], facets}`
  - reference tables: `ref:contract-types`, `ref:experience-levels`, `ref:teleworks`, `ref:degrees`, …
- Contract refs (LIVE): `CDI`, `CDD`, `INTERIM`, `TRAINING` (Stage), **`APPRENTICE` (Alternance / Apprentissage)**, `INDEPEDENT` (sic), `SEASONAL`, `INTERMIT`, `CDIC`, `TFP`, `VIE`, `MILITARY`, `CTTEE`, `EXTRA`, `VOLUNTEERING`, `CDI_INTERIMAIRE`, `CIDD`.
- **No working contract URL param found.** `contractTypes=APPRENTICE`, `contract=APPRENTICE` and `contractTypes=6` all left the result set unchanged. Filter client-side on `content[].contractTypes`.
- Page size seen: 20 per page. `total` for Metz "developpeur" was **25** and is noisy (many "Business developer" and sales roles). Grand Est: 176. "alternance developpeur" in Grand Est: 11.
- Offer URL: `https://www.meteojob.com/jobs/{id}`. External apply: `content[].url.redirect`.

### 2.6 JobTeaser

- `GET https://www.jobteaser.com/fr/job-offers?contract=alternating&q=developpeur` → 200 for the first ~8 requests. After that: **403 Cloudflare "Security checkup: Enable JavaScript and cookies"** (LIVE).
- The data is in the Next.js RSC payload (`self.__next_f.push`). `totalCount: 439` (France), `countPerPage: 20`.
- `jobAd` fields (LIVE): `id`, `slug`, `title`, `activationTime`, `candidacyType` (`EXTERNAL`, …), `company{id,name,logoUrl}`, `contract{type,durationMin,durationMax}`, `location{city,state,subState,latitude,longitude,formattedAddress}`, `locations[]`, `publicationType`, `searchIndex:"job_ads_search"`.
- Contract values: `internship`, `alternating`, `cdd`, `cdi`, `graduate_program`, `vie`, `part_time`, `thesis`, `freelance`.
- The location URL param was not identified.
- SEO page `https://www.jobteaser.com/fr/job-search/alternance-metz` (WebFetch): **9 results, 0 IT** (VINCI, Demathieu Bard, Equans, Michelin…).
- Applying generally goes through a JobTeaser / school career-center account (UNCERTAIN in detail).

### 2.7 L'Étudiant jobboard (jobs-stages.letudiant.fr, Piloty white-label)

- `GET https://jobs-stages.letudiant.fr/offres/contrat-alternance-ville-metz` → 200. Title: "… Metz - **15** Offres Disponibles - Septembre 2026".
- `…/contrat-alternance-region-grand-est` → "**429** Offres Disponibles".
- Listings are **client-rendered**: the SSR HTML only has JSON-LD `WebSite`/`Organization`/`BreadcrumbList`. The site search template is `https://jobs-stages.letudiant.fr/offres?keyword={q}`.
- The board says it aggregates offers from **La bonne alternance** (DOC-SOURCED via search snippet), so use LBA directly.

### 2.8 Glassdoor, Choose Your Boss, Google Jobs

- **Glassdoor FR:** `GET https://www.glassdoor.fr/Emploi/metz-developpeur-emplois-SRCH_IL.0,4_IC2988507_KO5,16.htm` → **403**, `Cf-Mitigated: challenge`, `Server: cloudflare`, `<title>Security | Glassdoor`. `POST https://www.glassdoor.fr/graph` → Cloudflare "Attention Required!". JobSpy Glassdoor → 0 rows (400/403).
- **Choose Your Boss:** `https://www.chooseyourboss.com/offres/emploi-it` → **403 "Just a moment..."** (curl). Headless Chrome (Playwright `channel="chrome"`) gets "Attention Required! | Cloudflare". Volume unknown. robots.txt only disallows `/recruiters/`, `/users/`.
- **Google Jobs via JobSpy:** 0 rows ("initial cursor not found"), even with the README-style query.

### 2.9 La bonne alternance (LBA) API: official, the best source for this candidate

- OpenAPI 3.1 spec (LIVE): `GET https://api.apprentissage.beta.gouv.fr/api/documentation/json`. Server `https://api.apprentissage.beta.gouv.fr/api`.
- **Auth:** `Authorization: Bearer <api key>`.
  - Without a key: `GET /api/job/v1/search?...` → **401** `{"statusCode":401,"name":"Unauthorized","message":"Vous devez fournir une clé d'API valide pour accéder à cette ressource"}` (LIVE).
  - Get a key by creating an account at `https://api.apprentissage.beta.gouv.fr/fr/compte/profil`. The portal says the APIs are "ouvertes à tous".
  - Per the spec: a **sandbox** key gets the needed permissions automatically, but its exchanges go to a test environment. A **production** key must be requested at `contact-api@labonnealternance.apprentissage.beta.gouv.fr`.
- The legacy endpoints `https://labonnealternance.apprentissage.beta.gouv.fr/api/v1/jobs` and `/api/v1/jobsEtFormations` → **404** (LIVE). Old tutorials using them are dead.
- **`GET /job/v1/search`.** Rate limit **60 calls/min**. Params:
  - `latitude`, `longitude`, `radius` (km, 0-200, default 30; whole France if no coordinates)
  - `target_diploma_level` (`"3"|"4"|"5"|"6"|"7"`; use `7` for Bac+5)
  - `romes` (comma-separated ROME codes, e.g. `M1805,M1806,M1810`, UNCERTAIN)
  - `rncp` (`^RNCP\d{3,5}$`)
  - `opco` (enum, e.g. `ATLAS`)
  - `departements` (array, e.g. `departements=57&departements=54`)
  - `partners_to_exclude` (array)
- **Response:** `{jobs:[…], recruiters:[…], warnings:[{message,code}]}` (schema in §3.6).
- **`POST /job/v1/apply`.** Rate limit **10 calls/min**; needs the `applications:write` permission.
  - JSON body, required fields: `applicant_first_name`, `applicant_last_name`, `applicant_email`, `applicant_phone`, `applicant_attachment_name`, `applicant_attachment_content` (CV file; base64 assumed), `recipient_id` (from `jobs[].apply.recipient_id` or `recruiters[].apply.recipient_id`).
  - Optional: `applicant_message` (cover letter).
  - Responses: `202, 400, 401, 403, 404, 409, 419, 500, 502, 503`. The recruiter gets the application by email.
- Other routes: `GET /job/v1/offer/{id}`, `GET /job/v1/export`, `POST /job/v1/offer` (recruiter side).

---

## 3. Response and page shapes (real, trimmed samples)

### 3.1 WTTJ Algolia hit (`wttj_jobs_production_fr`, LIVE 2026-09-23)
Keys: `_geoloc, _highlightResult, benefits, contract_duration_maximum, contract_duration_minimum, contract_type, education_level, experience_level_minimum, has_benefits, has_contract_duration, has_education_level, has_experience_level_minimum, has_remote, has_salary_yearly_minimum, is_boosted, key_missions, language, name, new_profession, objectID, offices, organization, organization_score, profile, profile_ranking, published_at, published_at_date, published_at_timestamp, rank_group_1..3, reference, remote, salary_currency, salary_maximum, salary_minimum, salary_period, salary_yearly_minimum, sectors, slug, source_stage, summary, wk_reference`
```json
{"objectID":"dcb39ce0-c93c-4b56-b2ac-0ccad9bca8b0","reference":"dcb39ce0-c93c-4b56-b2ac-0ccad9bca8b0","wk_reference":"RCA_zkWA9dy",
 "name":"Développeur Python fullstack","slug":"developpeur-python-fullstack_saint-herblain",
 "contract_type":"full_time","contract_duration_minimum":null,"contract_duration_maximum":null,
 "remote":"partial","experience_level_minimum":4.0,"education_level":null,"language":"fr",
 "published_at":"2026-09-23T00:01:03Z","published_at_timestamp":1790121663,"published_at_date":"2026-09-23",
 "salary_minimum":null,"salary_maximum":null,"salary_currency":null,"salary_period":null,
 "summary":"Rejoins RCA, une entreprise innovante dans le secteur du logiciel. En tant que développeur Python fullstack, …",
 "key_missions":["Concevoir et mettre en place la stack Python/API web from scratch, …","Développer les APIs REST en Python / FastAPI et le frontend en Angular, …"],
 "profile":"<p><strong>Ton profil :</strong></p><p>Tu justifies d'au moins 5 années d'expérience …</p>…",
 "organization":{"name":"RCA","slug":"rca","reference":"5xRxGwr","nb_employees":300,"creation_year":1999,"summary":"Solutions logicielles pour experts-comptables.","logo":{"url":"https://cdn-images.welcometothejungle.com/…"}},
 "new_profession":{"category_reference":"tech-engineering-3NjUy","category_name":"Technologie et ingénierie","sub_category_reference":"software-web-development-iMzA4","sub_category_name":"Développement de logiciels et de sites Web","pivot_reference":"full-stack-developer-5YTYy","pivot_name":"Développeur full stack"},
 "offices":[{"city":"Saint-Herblain","district":"Loire-Atlantique","state":"Loire Region","country":"France","country_code":"FR","local_state":"Pays de la Loire"}],
 "_geoloc":[{"lat":47.22967,"lng":-1.6267}],
 "sectors":[{"name":"Logiciels","reference":"software-1","parent_name":"Tech","parent_reference":"tech-1"}]}
```
- The hit has **no full `description`**: only `summary` (AI-generated), `key_missions` and `profile`. Get `description` from the REST detail.
- `offices[].state` uses English names ("Grand Est", "Loire Region"). `local_state` holds the French name.
- The search response envelope has `hits, nbHits, page, nbPages, hitsPerPage, facets, facets_stats, exhaustiveNbHits, query, params, renderingContent, processingTimeMS`.
- **Pagination is capped at 1000 hits** (1044 hits at 5 per page returned nbPages=200). Use `/browse` to go past it.

### 3.2 WTTJ REST job detail (`GET api.welcometothejungle.com/api/v1/organizations/{org}/jobs/{slug}`, LIVE)
Top-level `{"job":{...}}`. Job keys: `application_fields, apply_url, archived_at, ats, ats_questions, benefits, company_description, company_summary, contract_duration_max, contract_duration_min, contract_type, cta_content, description, education_level, experience_level, featured_page, is_default, key_missions, language, name, office, offices, organization, profession, profile, published_at, recruiter_proposal, recruitment_process, reference, remote, salary_currency, salary_max, salary_min, salary_period, skills, slug, social_image, start_date, status, summary, team, tools, updated_at, urls, videos, wttj_reference`
```json
{"job":{"name":"Développeur Python fullstack","slug":"developpeur-python-fullstack_saint-herblain","reference":"RCA_zkWA9dy",
 "status":"published","published_at":"2026-09-23T00:01:03Z","updated_at":"2026-09-23T00:01:14.589632Z",
 "contract_type":"full_time","experience_level":"4_TO_5_YEARS","education_level":null,"remote":"partial","start_date":null,
 "apply_url":"https://jobs.smartrecruiters.com/RCA1/744000151073629-developpeur-python-fullstack?oga=true&sid=…",
 "ats":"external",
 "application_fields":[{"id":"908f…","mode":"optional","name":"cover_letter"},{"id":"df35…","mode":"mandatory","name":"resume"}],
 "ats_questions":[],
 "description":"<p><strong>Ta mission : </strong></p><p>Tu rejoins une <strong>nouveau projet</strong> …",
 "profile":"<p><strong>Ton profil : </strong></p>…",
 "office":{"address":"Rue Édith Piaf, 44800 Saint-Herblain, France","city":"Saint-Herblain","zip_code":"44800","latitude":47.22967,"longitude":-1.6267,"country_code":"FR","district":"Loire-Atlantique"}}}
```
`skills`, `tools` and `urls` shapes, taken from another live detail (carrefour / data-analyste-import-f-h-alternance_massy):
```json
{"skills":[{"name":{"fr":"Communication","en":"Communication skills","es":"…","cs":"…","sk":"…"},"reference":"7e7fb132-…"}],
 "tools":[{"name":"Excel","reference":"a1f51a6d-…"},{"name":"SAP","reference":"cf3b2257-…"}],
 "urls":[{"kind":"canonical","language":"fr","href":"https://www.welcometothejungle.com/fr/companies/carrefour/jobs/data-analyste-import-f-h-alternance_massy"}],
 "apply_url":"https://www.aplitrak.com/?adid=…","ats":"external"}
```
In-site (`ats:"wkit"`) example with questions, from beloy / junior-front-end-developer_paris:
```json
{"ats":"wkit","apply_url":null,"contract_duration_min":12,"contract_duration_max":24,"education_level":"bac_5","experience_level":"2_TO_3_YEARS",
 "application_fields":[{"name":"cover_letter","mode":"mandatory"},{"name":"resume","mode":"mandatory"},{"name":"media_linkedin","mode":"mandatory"},{"name":"media_github","mode":"optional"}],
 "ats_questions":[{"id":"15c5a1c58d5e05229ac98afdfdd8238f","required":true,"format":{"type":"TEXT","bounds":{"min":0,"max":null}},
   "question":{"title":"For apprenticeships: please provide your apprenticeship rhythm and the RNCP code of your programme.","description":null}}]}
```
In a 15-job sample of tech apprenticeships, 5 were `ats:"wkit"` and 10 `ats:"external"`. External targets included aplitrak (Carrefour), SmartRecruiters (Mousquetaires), contactrh (Capgemini), Ironhack landing pages, groupe-vyv and safran-group.

### 3.3 Indeed GraphQL result (LIVE, trimmed)
```json
{"trackingKey":"5-dub1-0-1k372fgqhio...",
 "job":{"source":{"name":"Chaussea"},"key":"55d686bf939a8e1b","title":"Alternant - developpeur web fullstack h/f",
  "datePublished":1778648400000,"dateOnIndeed":1778691987759,
  "description":{"html":"<div><h3 class=\"jobSectionHeader\"><b>&Agrave; propos de nous</b></h3>…"},
  "location":{"countryCode":"FR","admin1Code":"GE","city":"Metz","postalCode":"57000","formatted":{"short":"Metz (57)","long":"57000 Metz"}},
  "compensation":{"baseSalary":null,"currencyCode":null},
  "attributes":[{"key":"6M28R","label":"Node.js"},{"key":"8HSB4","label":"Développement web"},{"key":"EXSNN","label":"Master"},
                {"key":"QADT5","label":"Contrat d'apprentissage"},{"key":"TRYDN","label":"Vue.js"}],
  "employer":{"relativeCompanyPageUrl":"/cmp/Chaussea-1","name":"CHAUSSEA","dossier":{"links":{"corporateWebsite":"https://jobs.chaussea.com/"}}},
  "recruit":{"viewJobUrl":"https://jobs.chaussea.com/html/job/8wvzi9yi36","detailedSalary":null,"workSchedule":null}}}
```
- `datePublished` is epoch **ms**.
- Old postings resurface: a Perisoft job had `date_posted` 2019-04-18. Filter on `dateOnIndeed` / `date_posted`.
- `attributes` mixes skills, education (`Master`) and contract types.

### 3.4 APEC search response (LIVE)
Envelope keys: `resultats[]`, `offreFilters[]`, `totalCount`.
```json
{"resultats":[{"id":179437370,"numeroOffre":"179437370W","intitule":"Développeur F/H","intituleSurbrillance":"Développeur F/H",
  "nomCommercial":"NEXIIUM TALENT","lieuTexte":"Lyon 09 - 69","salaireTexte":"40 - 50 k€ brut annuel",
  "texteOffre":"Notre client intégrateur de systèmes de contrôle-commande … (≈250 chars, truncated)…",
  "urlLogo":"/media_entreprise/808530/logo_NEXIIUM_TALENT_808530_1603522.png",
  "dateValidation":"2026-09-17T14:38:37.000+0000","datePublication":"2026-09-17T14:38:37.000+0000",
  "indicateurOqa":true,"latitude":"45.7826243","longitude":"4.8087685","localisable":true,"score":37.556084,
  "offreConfidentielle":false,"secteurActivite":101606,"secteurActiviteParent":101762,"clientReel":true,
  "contractDuration":0,"typeContrat":101888,"origineCode":101859,"indicateurFaibleCandidature":false}],
 "offreFilters":[{"offreFiltering":"CONTRACT_TYPE_FILTERING","offreFilterItems":[{"key":101888,"count":2662},{"key":20053,"count":6},{"key":101930,"count":9},{"key":101887,"count":58}]},
                 {"offreFiltering":"CONTRACT_DURATION_FILTERING","offreFilterItems":[…]},{"offreFiltering":"COMPANY_TYPE_FILTERING",…},
                 {"offreFiltering":"POSITION_STATUS_FILTERING",…},{"offreFiltering":"EXPERIENCE_FILTERING","offreFilterItems":[{"key":101881,"count":836},{"key":20043,"count":1629},{"key":20044,"count":480},{"key":20045,"count":291}]},
                 {"offreFiltering":"GEOLOCALISABLE_FILTER…"}],
 "totalCount":2735}
```
- `texteOffre` is truncated. The full text is only in the DataDome-protected detail call.
- For CV/letter tailoring, use the search snippet plus a manual or headed-browser fetch. Or skip tailoring for APEC offers.
- `EXPERIENCE_FILTERING` keys `101881, 20043, 20044, 20045`: labels UNCERTAIN (probably "Tous / Débutant / Confirmé / Expérimenté"). Resolve them via the UI before using `niveauxExperience`.

### 3.5 Free-Work job posting (LIVE, trimmed)
Keys: `@id, @type, annualSalary, applicationContact, applicationForm, applicationType, applicationUrl, candidateProfile, company, companyDescription, contracts, createdAt, currency, dailySalary, description, duration, durationPeriod, durationValue, elasticHighlights, experienceLevel, expiredAt, external, externalSource, id, job, locale, location, maxAnnualSalary, maxDailySalary, minAnnualSalary, minDailySalary, modifiedByUserAt, noIndex, premium, published, publishedAt, remoteMode, renewable, skills, slug, softSkills, startsAt, status, title, updatedAt`
```json
{"@id":"/job_postings/developpeur-ios-confirme-senior-swift-5-ios-26","id":668802,
 "title":"Développeur iOS Confirmé/Senior  — Swift 5 / iOS 26","slug":"developpeur-ios-confirme-senior-swift-5-ios-26",
 "description":"<p>développeur iOS confirmé/senior pour faire évoluer une application mobile native …","candidateProfile":"<p>…",
 "experienceLevel":"senior","minAnnualSalary":40000,"maxAnnualSalary":45000,"minDailySalary":400,"maxDailySalary":520,"currency":"EUR",
 "duration":12,"durationPeriod":"month","remoteMode":"partial","contracts":["contractor","permanent"],
 "applicationType":"turnover","applicationUrl":null,"applicationContact":null,
 "location":{"adminLevel1":"Île-de-France","country":"France","countryCode":"FR","latitude":"48.6443057","longitude":"2.7537863","key":"fr~ile-de-france~~","label":"Île-de-France, France"},
 "company":{"id":22004,"name":"CAT-AMANIA","slug":"cat-amania-8","location":{"locality":"Saint-Herblain","postalCode":"44800","key":"fr~pays-de-la-loire~loire-atlantique~saint-herblain"}},
 "job":{"id":82,"name":"Développeur·euse mobile iOS (Swift, Objective-C...)","slug":"developpeur-mobile-ios-swift-objective-c"},
 "skills":[{"@id":"/skills/515","id":515,"displayed":true,"skillJobs":["…"]}],
 "publishedAt":"2026-09-23T11:45:52+02:00","expiredAt":"2026-11-08T00:00:00+01:00","annualSalary":"40k-45k €","dailySalary":"400-520 €"}
```
The Hydra envelope has `hydra:totalItems`, `hydra:member`, `hydra:view` (`hydra:first`/`hydra:last`/`hydra:next`) and `hydra:search`.

### 3.6 Meteojob offer (`candidate-front-state` → `app:search:offers.content[]`, LIVE, trimmed)
Keys: `anonymous, company, contentLanguage, contractTypes, description, drivingLicence, experienceInJobRequired, externalReference, focus, freeOffer, highlight, id, jobCategories, jobType, jobs, labels, lastModification, locality, locations, mainJob, metaDescription, metaTitle, mobility, offerPremium, permanent, publicationDate, publisher, pushedContractType, pushing, recruiter, reference, salary, score, smartFeedsTag, status, temperature, title, top, url`
```json
{"id":"56998954","publicationDate":"2026-09-22T23:18:43.994Z","title":"STAGE - Développeur F/H",
 "mainJob":{"label":"Ingénieur de Développement d'Applications Low-Code PowerApps (H/F)","id":202847},
 "contractTypes":["CDI"],"jobType":["FULL_TIME"],
 "locations":[{"name":"Metz (57)","admin1Label":"Grand Est","admin2Label":"Moselle","admin2Code":"57","admin3Code":"57000","countryCode":"FR","lat":49.11911,"lon":6.17269}],
 "company":{"id":32188,"name":"Groupe Talents Handicap","bypassJobApplicationProcess":false},
 "description":"<i>En tant qu'organisateur de forums de recrutement, …",
 "url":{"jobOffer":"/jobs/56998954","redirect":"https://www.talents-handicap.com/689-edf/337795-stage-developpeur-f-h?src=MeteoJobs"},
 "labels":{"contractTypeList":[{"id":1,"meteojobId":"CDI","value":"CDI"}],"degreeList":[{"meteojobId":"BAC+4","value":"Bac+4"}],"telework":{"meteojobId":"UNDEFINED"}}}
```
The sample shows a data-quality problem: the title says "STAGE" while the contract is "CDI" and `mainJob` is a different label. Classify from the title and the description.

### 3.7 LBA `/job/v1/search` response (from the live OpenAPI spec; no live data because there is no key)
```
jobs[]:
  identifier{partner_job_id, id|null, partner_label}
  workplace{name, description, website, siret, brand, legal_name, size,
            location{address, geopoint{type, coordinates[lon,lat]}}, domain{idcc, opco, naf{code,label}}}
  apply{url, phone|null, recipient_id|null}
  contract{start|null, duration|null (months), type[], remote: onsite|remote|hybrid|null}
  offer{title, description, desired_skills[], to_be_acquired_skills[], access_conditions[], opening_count,
        publication{creation, expiration}, rome_codes[], target_diploma{european: "3".."7", label}|null,
        status: Active|Filled|Cancelled}
  is_delegated: bool
recruiters[]:            <- companies likely to hire apprentices = spontaneous-application targets
  identifier{id}, workplace{…same…}, apply{url, phone, recipient_id}
warnings[]: {message, code}
```

### 3.8 JobTeaser `jobAd` (RSC payload, LIVE, trimmed)
```json
{"activationTime":"2026-08-31T11:49:36.000Z","candidacyType":"EXTERNAL",
 "company":{"id":"03541b55-…","name":"Veolia Vesa"},"contract":{"durationMax":12,"durationMin":10,"type":"alternating"},
 "id":"0b81468c-a541-4ada-8c52-dc283c7daf69",
 "location":{"city":"Aubervilliers","country":"France","state":"Île-de-France","subState":"Seine-Saint-Denis","latitude":48.9024,"longitude":2.3732},
 "slug":"0b81468c-a541-4ada-8c52-dc283c7daf69-veolia-vesa-alternant-e-developpeur-mobile-android-ios-h-f",
 "title":"Alternant(e) - Développeur Mobile Android/iOS - (H/F)","searchIndex":"job_ads_search"}
```

---

## 4. Limits, ToS, legal (France / GDPR / CNIL)

| Source | robots.txt (LIVE) | ToS on automation | Practical limit seen |
|---|---|---|---|
| WTTJ | `Disallow: /*?`, `disallow: */jobs?query=*`, `/me/*`, `/users/*`. The Algolia host is separate. | §10 "Your obligations" forbids "any software, device, script, robot … (spiders, browser plug-ins…)" and copying/republishing; §19.2 personal, non-commercial license (DOC-SOURCED https://www.welcometothejungle.com/en/pages/terms) | None hit. The Algolia key has no per-IP quota. Be polite anyway (≤1 req/s). |
| Indeed | allows search pages; disallows `/applystart`, `/alert`, `/api/getrecjobs`, … | **Explicit**: "Utiliser un système automatisé (bots, agrégateurs (« scrapers »), « spiders », IA ou IA agentique) pour accéder, extraire des données ou soumettre du contenu au Site … sans l'autorisation écrite expresse d'Indeed" and "L'utilisation de technologies d'automatisation … ou de bots pour automatiser le processus de candidature sur Indeed … est interdite" (LIVE text from https://fr.indeed.com/legal) | No 429 in about 20 calls. JobSpy README: "Indeed is the best scraper currently with no rate limiting". Main risk is account/IP blocking, not legal action, at personal scale. |
| APEC | `User-agent: *` with no Disallow (sitemaps listed) | Database protected (CPI art. L.343-1 and following): "Toute représentation, reproduction ou diffusion … de la base de données … sans autorisation expresse préalable de l'Apec est sanctionnée" (DOC-SOURCED https://corporate.apec.fr/home/informations-legales-et-conditio.html) | Search endpoint open. **DataDome** blocks detail GETs (captcha/interstitial). |
| Free-Work | allows everything except `/login`, `/logout`, `/fw-deals`; blocks named bad bots | not reviewed (UNCERTAIN) | none seen (itemsPerPage=500 OK) |
| Meteojob | `Disallow: /*?`, `/api/`, `/jobsearch/api/`, `/postuler/…`, `/candidate-application/widgets/application-form/` | not reviewed | none seen in 7 requests |
| JobTeaser | `Disallow: /*?*` (with allow exceptions `/*/job-offers?locale=*`), `Disallow: */apply_to_extern_url` | not reviewed | **Cloudflare challenge after ~8 requests** |
| Glassdoor / Choose Your Boss | n/a | n/a | Cloudflare hard block |
| LBA | n/a (API) | "APIs ouvertes à tous"; usage under key | **60/min search, 10/min apply** (OpenAPI) |

**Legal notes for a personal tool (France):**
- **Sui generis database right** (CPI L.341-1 / L.342-1): repeated, systematic extraction of substantial parts of a job board's database can infringe, even when each request is harmless. jobbot's use is personal: a few hundred offers a day, kept only for the candidate's own applications, never republished. That keeps it on the low-risk side, but it is still a ToS breach on WTTJ and Indeed. Prefer official APIs (LBA, France Travail) where they exist.
- **GDPR:** job offers mostly hold company data. Recruiter names and emails in descriptions are personal data. If jobbot stores them (for example JobSpy's `emails` column) for spontaneous applications, the candidate acts as data controller. Keep the data minimal, delete it after the campaign, and honour objections (for example "ne plus me contacter").
- **CNIL guidance on scraping:** https://www.cnil.fr/fr/focus-interet-legitime-collecte-par-moissonnage asks that sites opposing collection through technical measures (robots.txt, anti-bot) be excluded. This supports **not** bypassing DataDome or Cloudflare (APEC detail, JobTeaser, Glassdoor, Choose Your Boss).
- **Automated applications:** forbidden in writing on Indeed. The only clean automatic apply channel found is the **LBA `/job/v1/apply`** API, which exists for exactly this purpose.

---

## 5. Gotchas and anti-bot measures

1. **WTTJ:**
   - The Algolia key only works with `Referer: https://www.welcometothejungle.com/…`.
   - The key and app id come from `/api/env`. Re-read them at startup; the key was created in Feb 2021 and has never rotated, but it could.
   - The jobs index is `wttj_jobs_production_fr`, not `…_production`.
   - `aroundRadius` is in **metres**.
   - `has_remote` was `true` for all 7347 "developpeur" hits, so it is useless as a filter. Use `remote`.
   - `engineering-disciplines-4YWVi` sits under "tech" but is mechanics/electro. Exclude it.
   - A multi-country remote job appears once per country in `offices.country_code` facets.
   - Pagination is capped at 1000 hits. Use `/browse` + `cursor`.
   - `experience_level_minimum` is often `null`, and `<=1` filters drop nulls. Filter with `(experience_level_minimum<=1 OR has_experience_level_minimum:false)`, or post-filter.
   - `education_level` is often null.
2. **Indeed:**
   - JobSpy sends `radiusUnit: MILES`, so `distance=30` ≈ 48 km.
   - JobSpy has **no apprenticeship `job_type`**. Put "alternance"/"apprenti" in `search_term`, or call GraphQL directly with the `QADT5` / `CPAHG` / `6UN82` keys, **one per query**, because several keys are ANDed.
   - `"alternance informatique"` returns only 5 results. The `what` search is strict (AND of terms, also matched in descriptions). Use short queries plus a local regex.
   - Stale jobs (2019) come back. Filter on `date_posted`.
   - Issue #385 (2026-09-10): the Indeed scraper can **hang on page 2** despite `timeout=10`. Run each `scrape_jobs` call in a child process with a hard timeout.
   - The mobile-app API key is hard-coded in JobSpy (`indeed/constant.py`). If Indeed rotates it, JobSpy breaks until upstream updates it (check the GitHub issues).
   - `description_format="markdown"` is the default. Keep it for the LLM tailoring step.
3. **LinkedIn via JobSpy:**
   - Rate limits around page 10 per IP (README).
   - `job_url_direct` is always None (#370).
   - The description can be empty (#374); the live test on 2026-09-23 got descriptions.
   - `hours_old` and `easy_apply` are mutually exclusive.
4. **APEC:**
   - A top-level `distance` field returns a 500.
   - A radius without lat/lon silently returns all of France.
   - The keyword "developpeur" matches "Développeur foncier" and "développeur commercial". Post-filter on IT terms.
   - The detail endpoint is **DataDome-protected**: `X-DataDome: protected`, `Accept-CH` client hints, 403 JSON `{"url":"https://geo.captcha-delivery.com/..."}`. It blocks headless Chromium and headless Chrome too.
   - Do not try to beat it. Store `texteOffre` (≈250 chars) and link to the offer page; the human opens it.
   - The very first detail call was already blocked; it came after about 10 search calls from the same IP. The search endpoint kept answering 200 afterwards. It is unknown whether a fresh IP gets a few detail calls through.
5. **Free-Work:**
   - `apprenticeship` in France also returns UK "Trainee" posts. Always add a `locationKeys` filter.
   - The same job often lists several `contracts` (`["contractor","permanent"]`).
   - Get-by-id → 404. Detail needs the `slug` + `jobSlug` query param.
6. **Meteojob:**
   - Contract URL filters are ignored. Filter client-side.
   - Search is fuzzy ("developpeur" matches "Business developer").
   - Title and contract data can contradict each other.
7. **JobTeaser, Glassdoor, Choose Your Boss:** Cloudflare challenges or blocks. JobTeaser allows a few SSR hits first. Do not add them to the automated pipeline.
8. **L'Étudiant:** listings are client-rendered, and the source is LBA. It is redundant.
9. **Season:** late September is the end of alternance hiring. Expect small volumes and prioritise `recruiters[]` (LBA) plus spontaneous applications.
10. **Dedup across sources:**
    - Many offers are cross-posted (Open "Développeur Java / Angular confirmé" appears on WTTJ, Indeed and LinkedIn; CGI appears on Indeed and LinkedIn).
    - Dedup key: `normalize(company) + normalize(title) + city`. Also match on the external ATS URL (`apply_url` / `viewJobUrl` / `url.redirect`).

---

## 6. Concrete Python implementation notes (sync httpx / Playwright sync)

### 6.1 Common normalized record (sqlite `jobs` table)
```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass(frozen=True)
class JobRecord:
    source: str                 # "wttj" | "indeed" | "linkedin" | "apec" | "freework" | "meteojob" | "lba"
    source_id: str              # objectID / jk / numeroOffre / id / partner_job_id
    title: str
    company: str | None
    city: str | None
    postal_code: str | None
    country: str | None
    lat: float | None
    lon: float | None
    contract: str | None        # normalized: "apprenticeship" | "cdi" | "cdd" | "internship" | "freelance" | "other"
    remote: str | None          # "none" | "partial" | "full" | None
    published_at: datetime | None
    url: str                    # public page on the board
    apply_url: str | None       # external ATS or None
    apply_channel: str          # "external_ats" | "board_account" | "lba_api" | "email" | "unknown"
    description_html: str | None
    raw_json: str               # full raw payload for re-parsing
    extra: dict = field(default_factory=dict)
```
Dedup: `UNIQUE(source, source_id)`, plus a soft-dedup index on `(norm_company, norm_title, norm_city)`.

### 6.2 Welcome to the Jungle client
```python
import json, re, time, httpx

WTTJ_ENV_URL = "https://www.welcometothejungle.com/api/env"
WTTJ_FALLBACK = {"PUBLIC_ALGOLIA_APPLICATION_ID": "CSEKHVMS53",
                 "PUBLIC_ALGOLIA_API_KEY_CLIENT": "4bd8f6215d0cc52b26430765769e65a0"}
WTTJ_INDEX = "wttj_jobs_production_fr"
METZ = (49.1193, 6.1757)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def load_wttj_env(client: httpx.Client) -> dict:
    try:
        r = client.get(WTTJ_ENV_URL, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        m = re.search(r"window\.env\s*=\s*(\{.*\})\s*;?\s*$", r.text, re.S)
        return {**WTTJ_FALLBACK, **json.loads(m.group(1))} if m else dict(WTTJ_FALLBACK)
    except (httpx.HTTPError, ValueError):
        return dict(WTTJ_FALLBACK)

class WttjSource:
    def __init__(self, client: httpx.Client):
        env = load_wttj_env(client)
        app = env["PUBLIC_ALGOLIA_APPLICATION_ID"]
        self._client = client
        self._base = f"https://{app.lower()}-dsn.algolia.net/1/indexes"
        self._headers = {"X-Algolia-Application-Id": app,
                         "X-Algolia-API-Key": env["PUBLIC_ALGOLIA_API_KEY_CLIENT"],
                         "Referer": "https://www.welcometothejungle.com/",   # MANDATORY
                         "Content-Type": "application/json", "User-Agent": UA}

    def search(self, *, query: str = "", filters: str = "", around: tuple[float, float] | None = METZ,
               radius_m: int = 60_000, hits_per_page: int = 100, page: int = 0,
               since_ts: int | None = None) -> dict:
        body = {"query": query, "hitsPerPage": hits_per_page, "page": page, "filters": filters}
        if around:
            body |= {"aroundLatLng": f"{around[0]},{around[1]}", "aroundRadius": radius_m}
        if since_ts:
            body["numericFilters"] = [f"published_at_timestamp>={since_ts}"]
        r = self._client.post(f"{self._base}/{WTTJ_INDEX}/query", headers=self._headers, json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def browse_all(self, filters: str):
        body = {"query": "", "filters": filters, "hitsPerPage": 1000}
        while True:
            r = self._client.post(f"{self._base}/{WTTJ_INDEX}/browse", headers=self._headers, json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            yield from data.get("hits", [])
            cursor = data.get("cursor")
            if not cursor:
                return
            body = {"cursor": cursor}
            time.sleep(1)

    def detail(self, org_slug: str, job_slug: str) -> dict:
        r = self._client.get(f"https://api.welcometothejungle.com/api/v1/organizations/{org_slug}/jobs/{job_slug}",
                             headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        return r.json()["job"]

# Recommended jobbot queries (LIVE-validated syntax):
TECH_SUBCATS = ["software-web-development-iMzA4", "data-business-intelligence-yZjY1",
                "cloud-computing-and-devops-mZTM2", "cybersecurity-2MDhm"]
SUBCAT_OR = " OR ".join(f"new_profession.sub_category_reference:{s}" for s in TECH_SUBCATS)
Q_ALTERNANCE_LOCAL = f"contract_type:apprenticeship AND ({SUBCAT_OR})"          # + around Metz 100 km
Q_CDI_LOCAL = f"contract_type:full_time AND ({SUBCAT_OR})"                        # + around Metz 100 km (includes LU)
Q_ALTERNANCE_REMOTE = f"contract_type:apprenticeship AND remote:fulltime AND offices.country_code:FR AND ({SUBCAT_OR})"
```
Mapping:
- `url = f"https://www.welcometothejungle.com/fr/companies/{hit['organization']['slug']}/jobs/{hit['slug']}"`
- `apply_channel = "external_ats" if job["ats"] == "external" else "board_account"`
- `apply_url = job["apply_url"]`
- Keep `ats_questions` so the letter generator can pre-answer them (rhythm / RNCP).

### 6.3 Indeed through JobSpy (child process + hard timeout, because of issue #385)
```python
# requirements (isolated venv or pinned): python-jobspy @ git+https://github.com/speedyapply/JobSpy@fda080a373e8226f3fd60635323f5da9af9892b1
import multiprocessing as mp, re
import pandas as pd

DEV_RE = re.compile(r"d[ée]velopp|developer|full.?stack|front|back.?end|web|logiciel|software|devops|data|java|python|php|react|angular|vue|\.net|c#|node", re.I)
NOT_DEV_RE = re.compile(r"business dev|commercial|foncier|d[ée]veloppement commercial|charg[ée] de d[ée]veloppement", re.I)

def _worker(kwargs: dict, q: mp.Queue) -> None:
    from jobspy import scrape_jobs
    try:
        q.put(("ok", scrape_jobs(**kwargs).to_json(orient="records", date_format="iso")))
    except Exception as exc:          # surface, never swallow
        q.put(("err", f"{type(exc).__name__}: {exc}"))

def run_jobspy(kwargs: dict, timeout_s: int = 120) -> pd.DataFrame:
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_worker, args=(kwargs, q), daemon=True)
    p.start(); p.join(timeout_s)
    if p.is_alive():
        p.terminate(); p.join()
        raise TimeoutError(f"jobspy timed out: {kwargs.get('site_name')} {kwargs.get('search_term')}")
    status, payload = q.get_nowait()
    if status == "err":
        raise RuntimeError(payload)
    return pd.read_json(payload, orient="records")

INDEED_SEARCHES = [
    dict(site_name=["indeed"], search_term="alternance développeur", location="Metz", distance=35, country_indeed="france", results_wanted=100, hours_old=72),
    dict(site_name=["indeed"], search_term="apprenti développeur", location="Metz", distance=35, country_indeed="france", results_wanted=100, hours_old=72),
    dict(site_name=["indeed"], search_term="alternance informatique", location="Nancy", distance=25, country_indeed="france", results_wanted=100, hours_old=72),
    dict(site_name=["indeed"], search_term="développeur junior", location="Metz", distance=35, country_indeed="france", results_wanted=100, hours_old=72),
    dict(site_name=["indeed"], search_term="developer junior", location="Luxembourg", distance=15, country_indeed="luxembourg", results_wanted=100, hours_old=72),
]
# distance is MILES (35 mi ≈ 56 km). Only ONE of hours_old | job_type/is_remote | easy_apply per call.
# Then: df = df[df.title.str.contains(DEV_RE) & ~df.title.str.contains(NOT_DEV_RE)]
# Map: source_id=df.id ("in-<jk>"), url=df.job_url, apply_url=df.job_url_direct, description=df.description (markdown).
# Space calls ≥5-10 s apart; no proxy needed at this volume (LIVE); stop on 429.
```
**Direct GraphQL alternative** (if JobSpy lags): send the headers from §2.2 to `POST https://apis.indeed.com/graphql`. Use `radiusUnit: KILOMETERS` and, per contract, `filters: { composite: { filters: [{ keyword: { field: "attributes", keys: ["QADT5"] } }] } }`. Page with `cursor`. `limit` max is 100 (the value JobSpy uses).

### 6.4 APEC client (search only)
```python
APEC_SEARCH = "https://www.apec.fr/cms/webservices/rechercheOffre"
APEC_ALTERNANCE = ["20053", "597137", "597139", "597138", "597140"]
APEC_CDI = ["101888"]
APEC_CONVENTIONS = ["143684", "143685", "143686", "143687"]

def apec_body(keywords: str, lieux: list[str], contrats: list[str], start: int = 0, size: int = 100,
              geo: tuple[float, float, int] | None = None) -> dict:
    body = {"motsCles": keywords, "lieux": lieux, "typesContrat": contrats, "typesConvention": APEC_CONVENTIONS,
            "fonctions": [], "statutPoste": [], "niveauxExperience": [], "idsEtablissement": [], "secteursActivite": [],
            "typesTeletravail": [], "idNomZonesDeplacement": [], "positionNumbersExcluded": [],
            "typeClient": "CADRE", "sorts": [{"type": "DATE", "direction": "DESCENDING"}],
            "pagination": {"range": size, "startIndex": start}, "activeFiltre": True,
            "pointGeolocDeReference": {"distance": 0}}
    if geo:   # ALWAYS lat+lon with distance, else all-France results
        lat, lon, km = geo
        body["pointGeolocDeReference"] = {"distance": km, "latitude": lat, "longitude": lon}
    return body

def apec_search(client: httpx.Client, body: dict) -> dict:
    r = client.post(APEC_SEARCH, json=body, timeout=30,
                    headers={"User-Agent": UA, "Accept": "application/json", "Origin": "https://www.apec.fr",
                             "Referer": "https://www.apec.fr/candidat/recherche-emploi.html/emploi"})
    if r.status_code == 403 and "captcha-delivery" in r.text:
        raise PermissionError("APEC DataDome block - back off 24h, do not retry")
    r.raise_for_status()
    return r.json()          # {"resultats": [...], "offreFilters": [...], "totalCount": n}
# Usage: apec_search(c, apec_body("developpeur", ["57","54"], APEC_CDI))
# url = f"https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{r['numeroOffre']}"
# Do NOT call /cms/webservices/offre/public (DataDome). Tailoring text = intitule + texteOffre (truncated).
```
`range` of 100 was not tested; 20 is the front default. If 100 is rejected, fall back to 20 and page with `startIndex`.

### 6.5 Free-Work client
```python
FW_API = "https://api.free-work.com/job_postings"
FW_HEADERS = {"User-Agent": UA, "Accept": "application/ld+json", "X-Varnish-Public": "1"}

def freework_search(client: httpx.Client, **params) -> dict:
    base = {"page": 1, "itemsPerPage": 100}
    r = client.get(FW_API, params={**base, **params}, headers=FW_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()          # hydra:totalItems, hydra:member, hydra:view.hydra:next

# Queries:
#  freework_search(c, contracts="permanent,fixed-term", locationKeys="fr~grand-est~moselle~metz", locationRadius=60, publishedSince="less_than_7_days")
#  freework_search(c, contracts="permanent", locationKeys="lu~~~", searchKeywords="developpeur")
#  freework_search(c, contracts="apprenticeship", locationKeys="fr~grand-est~~")
# url = f"https://www.free-work.com/fr/tech-it/job-mission/{m['job']['slug']}/{m['slug']}"
# apply_channel = "external_ats" if m["applicationType"] == "url" else "board_account"
```

### 6.6 Meteojob (SSR JSON)
```python
from bs4 import BeautifulSoup   # or re

def meteojob_search(client: httpx.Client, what: str, where: str, page: int = 1) -> dict:
    r = client.get("https://www.meteojob.com/jobs", params={"what": what, "where": where, "page": page},
                   headers={"User-Agent": UA, "Accept-Language": "fr-FR"}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    tag = BeautifulSoup(r.text, "html.parser").find("script", id="candidate-front-state")
    state = json.loads(tag.string)
    return state.get("app:search:offers", {"total": 0, "content": []})
# where format: "Metz (57)", "Nancy (54)", "Grand Est". Filter contractTypes client-side ("APPRENTICE","CDI").
# url = "https://www.meteojob.com" + o["url"]["jobOffer"]; apply_url = o["url"].get("redirect")
```
The `page` param name is UNCERTAIN (not tested). Check `total` against the number collected.

### 6.7 La bonne alternance (search + recruiters + official apply)
```python
import base64, pathlib
LBA = "https://api.apprentissage.beta.gouv.fr/api"

class LbaSource:
    def __init__(self, client: httpx.Client, api_key: str):
        if not api_key:
            raise ValueError("LBA_API_KEY missing (create account at api.apprentissage.beta.gouv.fr/fr/compte/profil)")
        self._c = client
        self._h = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def search(self, lat=49.1193, lon=6.1757, radius_km=60, romes="M1805,M1806,M1810", diploma="7") -> dict:
        r = self._c.get(f"{LBA}/job/v1/search", headers=self._h, timeout=30,
                        params={"latitude": lat, "longitude": lon, "radius": radius_km,
                                "romes": romes, "target_diploma_level": diploma})
        r.raise_for_status()                 # 60 req/min
        return r.json()                      # {"jobs": [...], "recruiters": [...], "warnings": [...]}

    def apply(self, recipient_id: str, first: str, last: str, email: str, phone: str,
              cv_path: pathlib.Path, message: str | None) -> int:
        payload = {"applicant_first_name": first, "applicant_last_name": last, "applicant_email": email,
                   "applicant_phone": phone, "applicant_attachment_name": cv_path.name,
                   "applicant_attachment_content": base64.b64encode(cv_path.read_bytes()).decode(),
                   "applicant_message": message, "recipient_id": recipient_id}
        r = self._c.post(f"{LBA}/job/v1/apply", headers=self._h, json=payload, timeout=60)
        if r.status_code == 409:
            return 409                       # most likely "already applied"; record it, do not retry
        r.raise_for_status()                 # 202 accepted; max 10 req/min -> sleep >= 6 s between calls
        return r.status_code
```
Details not verified live (no key available):
- `applicant_attachment_content` encoding: base64 is assumed from the spec wording "Resume file". Check the spec's `format` or examples first.
- Whether `recipient_id` for `recruiters[]` is non-null for every company.
- Which ROME codes fit best. ROME 4.0 probably keeps `M1805` for "Études et développement informatique"; check against the France Travail ROME API.
- The meaning of 409 and 419.

### 6.8 Scheduler and politeness
- Suggested cadence: WTTJ, Free-Work, APEC and Meteojob **once per day** (they are small). Indeed **2×/day** with `hours_old=24`, 5-10 s between calls. LBA search **2×/day**.
- **Stop rules:**
  - A 403 carrying `captcha-delivery`, `cf-mitigated` or `Just a moment` pauses that source for 24 h and logs the event.
  - A 429 means exponential backoff, then a pause.
- Store raw payloads (`raw_json`) so parsers can be fixed without refetching.
- Playwright is only needed for the human-confirmed "assisted apply" on external ATS URLs. None of the search sources above needs a browser.

---

## 7. Claims table

| # | Claim | Status | Evidence / URL |
|---|---|---|---|
| 1 | WTTJ runtime env is served at `/api/env` with `PUBLIC_ALGOLIA_APPLICATION_ID=CSEKHVMS53`, `PUBLIC_ALGOLIA_API_KEY_CLIENT=4bd8f6215d0cc52b26430765769e65a0` | LIVE-VERIFIED | curl `https://www.welcometothejungle.com/api/env` → 200 |
| 2 | The WTTJ Algolia key has ACL `search`+`browse`, restricted indexes and referers, no per-IP quota | LIVE-VERIFIED | `GET /1/keys/{key}` response |
| 3 | Algolia without Referer → 403 "Method not allowed with this referer"; with Referer → 200 | LIVE-VERIFIED | curl |
| 4 | Jobs index `wttj_jobs_production_fr` (+ `_published_at_desc`, `wttj_jobs_production_en`) exists; `wttj_jobs_production` → 404 | LIVE-VERIFIED | probes |
| 5 | Contract facet values include `apprenticeship`; `aroundLatLng`+`aroundRadius` (m) work; pagination capped at 1000 | LIVE-VERIFIED | facet dump, nbPages=200 at hpp=5 |
| 6 | Browse endpoint returns a `cursor` (full export possible) | LIVE-VERIFIED | `/browse` response |
| 7 | WTTJ REST `api/v1/organizations/{org}/jobs/{slug}` needs no auth; returns `apply_url`, `ats` (`external`/`wkit`), `application_fields`, `ats_questions`, `description` | LIVE-VERIFIED | 15 detail calls |
| 8 | WTTJ volume 2026-09-23: 29 apprenticeships within 50 km of Metz (0 tech); 4 tech apprenticeships within 150 km; 13 software-web-dev jobs within 100 km | LIVE-VERIFIED | Algolia queries |
| 9 | WTTJ in-site apply requires a candidate account | DOC-SOURCED | https://welcometothejunglecandidate.zendesk.com/hc/fr/articles/4423376395666-Envoyer-une-candidature-via-le-formulaire |
| 10 | WTTJ ToS §10 prohibits scripts/robots/spiders; robots.txt disallows `/*?` | DOC-SOURCED / LIVE-VERIFIED | https://www.welcometothejungle.com/en/pages/terms ; robots.txt |
| 11 | python-jobspy latest on PyPI = 1.1.82 (2025-07-28), requires Python ≥3.10, pins `NUMPY==1.26.3` | LIVE-VERIFIED | https://pypi.org/pypi/python-jobspy/json |
| 12 | The JobSpy repo moved to speedyapply/JobSpy; main (2026-02-18, `fda080a`) has unreleased fixes (numpy relax, user_agent) | LIVE-VERIFIED | GitHub API |
| 13 | JobSpy sites: linkedin, indeed, zip_recruiter, glassdoor, google, bayt, naukri, bdjobs; params and the 34-column output order as listed | LIVE-VERIFIED | source `jobspy/__init__.py`, `util.py` + live DataFrame |
| 14 | JobSpy live: Indeed FR 7 rows, LinkedIn 15 rows (fetch_description OK, job_url_direct None), Glassdoor 0 (400/403), Google 0 | LIVE-VERIFIED | venv run 2026-09-23 |
| 15 | JobSpy `distance` is miles for Indeed (radiusUnit MILES) | LIVE-VERIFIED | source `indeed/__init__.py` |
| 16 | Indeed GraphQL `apis.indeed.com/graphql` works with the app key and `indeed-co: FR/LU`, no proxy | LIVE-VERIFIED | httpx probes |
| 17 | Indeed FR attribute keys (CPAHG, QADT5, 6UN82, 5QWDV, …); several keys in one filter = AND | LIVE-VERIFIED | probes |
| 18 | Indeed ToS forbids scrapers/bots and automated applications | LIVE-VERIFIED | text of https://fr.indeed.com/legal |
| 19 | JobSpy open issues: #385 Indeed hang page 2, #374 LinkedIn empty description, #370 job_url_direct missing, #353 is_remote ignored with hours_old | LIVE-VERIFIED | GitHub search API |
| 20 | APEC `POST /cms/webservices/rechercheOffre` open without auth; 34-field DTO; contract/convention code tables | LIVE-VERIFIED | probes + JS bundle `main-WSTTYCDC.js` |
| 21 | APEC radius needs lat/lon inside `pointGeolocDeReference` | LIVE-VERIFIED | 2735 vs 28 results |
| 22 | APEC detail `/cms/webservices/offre/public` is DataDome-protected (403 for curl and headless Chrome) | LIVE-VERIFIED | headers `X-DataDome: protected` |
| 23 | APEC database is protected under CPI L.343-1 | DOC-SOURCED | https://corporate.apec.fr/home/informations-legales-et-conditio.html |
| 24 | APEC `niveauxExperience` code labels (101881, 20043, 20044, 20045) | UNCERTAIN | only facet keys seen |
| 25 | Free-Work public API `api.free-work.com/job_postings` with params `searchKeywords`, `contracts`, `locationKeys`, `locationRadius`, `itemsPerPage≤500` | LIVE-VERIFIED | probes + Nuxt bundle |
| 26 | Free-Work volume: 17 CDI within 50 km of Metz (incl. LU), 94 Luxembourg, 0 apprenticeship Grand Est | LIVE-VERIFIED | probes |
| 27 | Free-Work `remoteMode` values other than `partial`; `publishedSince` values beyond the two listed | UNCERTAIN | from bundle labels only |
| 28 | Meteojob data in `<script id="candidate-front-state">`; contract URL filters ignored | LIVE-VERIFIED | probes |
| 29 | Meteojob `page` query param name | UNCERTAIN | not tested |
| 30 | JobTeaser: 439 alternance "developpeur" offers FR; Cloudflare challenge after ~8 requests; Metz alternance 9 (0 IT) | LIVE-VERIFIED | probes + WebFetch |
| 31 | JobTeaser application requires a student/school account | UNCERTAIN | general knowledge |
| 32 | L'Étudiant: 15 alternance offers Metz, 429 Grand Est; aggregates La bonne alternance | LIVE-VERIFIED (counts) / DOC-SOURCED (aggregation) | page titles; search snippet for jobs-stages.letudiant.fr |
| 33 | Glassdoor FR and Choose Your Boss blocked by Cloudflare | LIVE-VERIFIED | 403 `cf-mitigated: challenge`, "Attention Required!" |
| 34 | LBA API: Bearer key; `/job/v1/search` (60/min) returns `jobs` + `recruiters`; `/job/v1/apply` (10/min) emails the recruiter; sandbox key automatic, production key by email | LIVE-VERIFIED (spec + 401) | https://api.apprentissage.beta.gouv.fr/api/documentation/json |
| 35 | Legacy LBA `/api/v1/jobs` endpoints are gone (404) | LIVE-VERIFIED | curl |
| 36 | LBA sign-up at `/fr/compte/profil`, "ouvertes à tous" | DOC-SOURCED | https://api.apprentissage.beta.gouv.fr/fr |
| 37 | LBA apply attachment is base64; recruiters always have `recipient_id`; real volume around Metz | UNCERTAIN | no key available |
| 38 | ROME codes M1805/M1806/M1810 are right for LBA dev searches (ROME 4.0) | UNCERTAIN | check with the ROME API |
| 39 | CNIL scraping guidance: exclude sites that oppose collection by technical means | DOC-SOURCED | https://www.cnil.fr/fr/focus-interet-legitime-collecte-par-moissonnage |
| 40 | Choose Your Boss job volume for Metz | UNCERTAIN | blocked |
