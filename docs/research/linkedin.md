# LinkedIn (linkedin.com) — research for jobbot

> Researched live on **2026-09-23** from a French residential IP (curl 8.x + httpx 0.28.1, Python 3.12.10, BeautifulSoup 4.14.3). **No account and no credentials were used.** I made about 170 read-only GETs to LinkedIn guest/public endpoints, plus a deliberate 30-request burst to measure rate limiting.
> Nothing was submitted: no login, no application, no message, no connection request.
> **The logged-in DOM (Easy Apply modal, job cards, top card, hiring team) could not be observed first-hand.** It comes from open-source bots that captured it from live sessions in September 2026:
> - GodsScion fixtures, captured 2026-09-09;
> - `YonatanVol/cv_sender`, verified live on 2026-09-14 and 2026-09-22;
> - `selcukgural/AfterApply`, verified live on 2026-09-06 and 2026-09-12.
>
> Each such claim is tagged DOC-SOURCED in §7.

---

## 1. TL;DR + recommendation for jobbot

**What is true today (2026-09-23, live-verified)**
- **Logged-out guest endpoints still work.** Three of them return HTML fragments or JSON with no cookies or auth:
  - `GET /jobs-guest/jobs/api/seeMoreJobPostings/search`: 10 cards per page, `start` must stay below 1000.
  - `GET /jobs-guest/jobs/api/jobPosting/{id}`: the full detail.
  - `GET /jobs-guest/api/typeaheadHits`: geoId and company-id lookup.
  - LinkedIn is now served through **Cloudflare** (`Server: cloudflare`, `CF-RAY …-CDG`, `__cf_bm` cookie).
- **The guest search only applies some filters.** It applies `keywords`, `location`, `geoId`, `distance`, `f_TPR` (any `r<seconds>`, `r3600` works), `f_AL=true` (Easy Apply), `f_EA=true` (<10 applicants), `f_C` (company id) and `start`.
- **It silently ignores** `f_JT`, `f_E`, `f_WT` and `sortBy`. With and without each of them, I got the same 20 job IDs in the same order, both with `geoId` and with `location=` queries. The public `/jobs/search` page ignores them too (its count did not change). **Job type, experience level, remote and sort must be done client-side in jobbot.**
- **"Alternance" is not a LinkedIn job type.** In French UI, `f_JT=I` is labelled **"Stage / Alternance"** and `f_E=1` is **"Stagiaire / Alternant"**. Of 16 live alternance postings in Grand Est, 11 were tagged `Temps plein` with level `Non pertinent`, 3 were `Stage / Alternance`, 1 was `Autre` and 1 was `Temps partiel`. **Detect alternance with a keyword classifier on title and description** (see §6.4), not with filters.
- **You can tell Easy Apply from external apply while logged out:**
  - Easy Apply detail pages contain `button[data-tracking-control-name="public_jobs_apply-link-onsite"]`.
  - External-apply pages contain `…public_jobs_apply-link-offsite…`.
  - The external ATS URL is **not** exposed logged-out.
- **The recruiter or job poster is sometimes public.** 6 of 14 sampled guest detail pages had a `.message-the-recruiter` block with name, headline and `/in/<slug>` profile URL. Detail pages also leak the numeric company id (`facetCurrentCompany=<id>` in the "see who was hired" link).
- **Rate limit:** about **10 requests fired within about 3 s → HTTP 429**. The body is a JS redirect to `/authwall`, and access comes back after about **6.5 s**. With 2–3 s spacing, about 60 sequential requests never got a 429.
- **Easy Apply is a minority channel for this candidate:**
  - `développeur` within 80 km of Metz: **193 jobs, 26 of them Easy Apply (13 %)**, and only **5 new Easy Apply jobs in the last 7 days**.
  - `alternance développeur` across France: "Plus de 2 000", of which **884 are Easy Apply**.
  - Most offers near Metz go to external ATSs (Workday, Taleo, SmartRecruiters, company sites and so on).
- **Official Easy Apply limits** (help article a8068422, fetched live):
  - a **daily limit** that "resets at the end of the day (UTC)";
  - a **speed limit**: "Rapidly submitting applications in a short time triggers a temporary pause";
  - a **lower daily limit for "inauthentic" activity**.
  - LinkedIn publishes no number. Many LinkedIn posts from August 2025 report **50 per 24 h**.
- **ToS:** the User Agreement (effective **2025-11-03**) §8.2 forbids:
  - "software, devices, scripts, robots … to scrape or copy the Services";
  - "bots or other unauthorized automated methods to access the Services, add or download contacts, send or redirect messages…".
  - The help article "Prohibited software and extensions" explicitly bans tools that "automate activity on LinkedIn's website".
  - The penalty is a **temporary or permanent account restriction**.
  - `robots.txt` has `User-agent: * / Disallow: /`, and `/jobs-guest/` is disallowed for every named crawler.
- **Logged-in Easy Apply exists in two DOM variants in September 2026**, and jobbot must support both:
  - **(A) Classic Ember modal:** `button#jobs-apply-button-id` opens `div[role=dialog].jobs-easy-apply-modal[data-test-modal]`, with buttons "Continue to next step" / "Review your application" / "Submit application".
  - **(B) New server-driven UI (SDUI):** Easy Apply is an `<a href="…/jobs/view/{id}/apply/?openSDUIApplyFlow=true">`. The form sits in a **shadow root**, has **no `role=dialog`**, and its controls have ids containing `easyApplyFormElement` inside `.fb-dash-form-element`.
  - Playwright CSS, text and role selectors pierce open shadow roots. **XPath does not.**

**Recommendation (what jobbot should do)**
1. **Discovery: use the guest endpoints, at low volume. No login and no cookies, so the account is never at risk.**
   - Queries: a fixed set of about 9 keywords × 3 geos (Metz 50 mi, Luxembourg, France for remote) with `f_TPR=r86400`, up to 3 pages each.
   - Pacing: at least 3 s between requests plus random jitter, and one run per day. That is about 80 search requests plus details for new IDs only, so under 200 requests/day.
   - Back off 10 minutes on the first 429.
   - This goes against robots.txt and UA §8.2. The practical exposure is IP-level throttling, not the account. **Never attach the logged-in `li_at` cookie to these calls.**
   - Zero-risk complement: have the candidate create LinkedIn **job alerts** by hand and parse the alert emails through IMAP (`/jobs/view/(\d+)` in the links). This works like the HelloWork alert approach.
2. **Enrichment:** fetch `jobPosting/{id}` once per new id and cache it in SQLite. Then:
   - classify alternance vs junior CDI vs out-of-scope (senior, stage-only, commercial "business developer");
   - detect the language (some postings are in PT, DE or EN);
   - detect Easy Apply vs external (`onsite` vs `offsite`);
   - extract the recruiter block when present;
   - then run the ATS scoring and CV/letter tailoring modules.
3. **Apply: assisted Easy Apply only, human-in-the-loop, low volume.**
   - Browser: Playwright **sync**, `launch_persistent_context(user_data_dir=<dedicated dir>, channel="chrome", headless=False, locale="fr-FR", timezone_id="Europe/Paris")`. The candidate logs in **by hand once**. Never automate login, 2FA or captcha.
   - Flow: the bot opens the job, detects variant A or B, fills known answers from an answer bank and uploads the tailored PDF. It **stops at the Review step ("Vérifier")** and shows a screenshot and summary.
   - Submission is irreversible. The FR help centre says: "Vous ne pouvez pas modifier ou retirer une candidature envoyée". So the human confirms in the jobbot UI and only then does the bot click "Envoyer la candidature" (or the human clicks it).
   - Governor:
     - at most **10–12 Easy Apply per day** (about 20–25 % of the reported 50 limit);
     - **90–240 s random gap** between submissions;
     - only 09:00–19:00 Europe/Paris, and at most 2 sessions a day;
     - **hard stop** on any `/checkpoint/`, `/authwall`, captcha, "limit" message or unexpected logout.
   - Never unanswered-guess: any required question that has no answer in the bank sends the job to a "needs input" queue.
4. **External-apply jobs:** store them as `status="external"` with the LinkedIn URL. Getting the ATS URL needs a logged-in click on "Postuler", which opens a new tab (`/jobs/view/externalApply/{id}` redirect). Hand them to the generic ATS module or to the human.
5. **Tracking:**
   - Primary source of truth: jobbot's own SQLite, keyed by LinkedIn job id. A submission counts only when the confirmation view ("Candidature envoyée" / "Application sent") is seen **and** the submit button is gone.
   - Secondary: LinkedIn's confirmation e-mails through the IMAP module.
   - Optional, at most once a day: read the logged-in tracker `/my-items/saved-jobs/?cardType=APPLIED`, which now redirects to `/jobs-tracker?cardType=APPLIED`.
6. **Recruiters and follow-ups ("relances"):**
   - Store only the hiring-team **name, headline, profile URL and job id**. Sources: the guest `.message-the-recruiter` block, or `.hirer-card__hirer-information` when logged in.
   - jobbot **drafts** a short message or InMail and the **human sends it by hand**.
   - **Do not automate LinkedIn messages or invitations.** They are explicitly named in UA §8.2 and are the fastest way to get restricted.
   - **Do not scrape or enrich e-mails from LinkedIn profiles.** The CNIL fined KASPR €240 k (2024-12-19) for exactly that. For spontaneous applications, get e-mails from company websites, SIRENE and similar sources in the email module.

---

## 2. Endpoints / URLs & auth

### 2.1 Guest job search (logged-out) — LIVE

```
GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
```

| Param | Values | Guest honours it? | Notes |
|---|---|---|---|
| `keywords` | free text, URL-encoded (`d%C3%A9veloppeur`) | **yes** | Loose OR-ish matching: "alternance" in Grand Est returns 4 000+ results. Use specific phrases. |
| `location` | free text, e.g. `Metz, Grand Est, France` | **yes** | Geocoded server-side. `location` alone (no geoId) returned Metz jobs. |
| `geoId` | numeric (see §2.4) | **yes** | More precise than `location`. When both are sent, geoId wins. |
| `distance` | `0` (exact), `5`, `10`, `25`, `50` (**miles**; FR UI labels: Lieu exact / 8 km / 16 km / 40 km / 80 km) | **yes** | Only meaningful with a city geoId. Other values are UNCERTAIN. |
| `f_TPR` | `r<seconds>`: `r86400` (24 h), `r604800` (week), `r2592000` (month); **`r3600` also works** | **yes** | Always set it, otherwise 1-year-old reposts show up (`datetime="2025-07-23"` seen). |
| `f_AL` | `true` | **yes** | Easy Apply only. FR pill label "Candidature simplifiée". |
| `f_EA` | `true` | yes (tested on the public page: 70 → 4) | "Moins de 10 candidatures". |
| `f_C` | company id(s), comma-separated | **yes** | Thales = `1951` returned only Thales jobs. |
| `start` | `0,10,20,…,990` | **yes** | 10 per page. Past the end you get **200 with a 26-byte body**. `start>=1000` gives **HTTP 400** with an empty body. |
| `f_JT` | `F,P,C,T,I,V,O` | **NO (ignored)** | The same 20 IDs came back for `I` and `F`. |
| `f_E` | `1..6` | **NO (ignored)** | The same 20 IDs came back. |
| `f_WT` | `1,2,3` | **NO (ignored)** | The same 20 IDs came back. |
| `sortBy` | `DD`, `R` | **NO (ignored)** | The same order came back. |
| `f_PP` | geoId | NO (ignored) | Tested with Metz. |

Headers used (these work; no auth, no cookies needed):
```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36
Accept-Language: fr-FR,fr;q=0.9          # → French labels ("il y a 1 semaine", "Recrutement actif") and short locations ("Metz")
```
Without `Accept-Language`, the response is in English: "1 week ago", "Actively Hiring", "Be an early applicant", and long locations such as "Bordeaux, Nouvelle-Aquitaine, France". **Pin one language** and write the parser for it.

Response: `200 text/html; charset=utf-8`, about 30 KB. It starts with `<!DOCTYPE html>` followed by up to 10 `<li>` cards. The server sets `JSESSIONID`, `bcookie`, `bscookie`, `lidc`, `li_gc`, `lang` and `__cf_bm`. Keep one `httpx.Client` so the cookies are reused.

### 2.2 Guest job detail — LIVE

```
GET https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jobId}
```
- `200 text/html`, 18–78 KB, for a live job.
- `200` with `figcaption.closed-job__flavor--closed` "**Les candidatures ne sont plus acceptées**" for a closed job (seen on id 3577461385).
- **`404` with an empty body** for an unknown id (`/jobPosting/1`).

### 2.3 Public (SEO) pages — LIVE
- `GET https://www.linkedin.com/jobs/search?keywords=…&location=…&geoId=…&distance=…&f_AL=true&f_TPR=…` returns a full page of about 290 KB. It includes `.results-context-header__job-count` (e.g. `70`, `Plus de 4 000`), the first cards (same markup as §3.1) and filter pills.
  - This is the only guest way to get a **count**.
  - Filter facets shown: `distance`, `f_TPR`, `f_C`, plus pills `f_AL` and `f_EA`.
  - There are no f_JT, f_E or f_WT facets any more.
- `GET https://www.linkedin.com/jobs/view/{jobId}/` returns 200 and about 320 KB, with the same top card and description classes as §3.2.
  - **No JSON-LD `JobPosting` in 2026** (checked: 0 `application/ld+json` scripts).
  - Canonical slug URLs look like `https://fr.linkedin.com/jobs/view/<slug>-at-<company>-<jobId>`. Always extract the trailing digits.

### 2.4 Geo and company typeahead (guest JSON) — LIVE
```
GET https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=GEO&geoTypes=ADMIN_DIVISION_1,POPULATED_PLACE,ADMIN_DIVISION_2,MARKET_AREA,COUNTRY_REGION&query=Metz
GET https://www.linkedin.com/jobs-guest/api/typeaheadHits?typeaheadType=COMPANY&query=Thales
```
The response is a JSON array of `{"id","type","displayName","trackingId"}`. You need `ADMIN_DIVISION_1` in `geoTypes` to get regions such as Grand Est. Leaving `geoTypes` out also works.

**GeoIds for this candidate (live):**

| Place | geoId |
|---|---|
| Metz, Grand Est, France | **103167155** |
| Greater Metz Area | 90009656 |
| Montigny-lès-Metz | 104937284 |
| Maizières-lès-Metz | 104840851 |
| Nancy, Grand Est, France | **106742268** |
| Greater Nancy Area | 90009658 |
| 54000, Nancy | 104610469 |
| Thionville, Grand Est, France | **103333173** |
| Moselle (dept 57) | **105424345** |
| Meurthe-et-Moselle (dept 54) | 104346793 |
| Grand Est, France (region) | **101735443** |
| Strasbourg | 105580607 |
| France | **105015875** |
| Luxembourg (country) | **104042105** |
| Luxembourg (city) | 100364719 |
| Esch-sur-Alzette | 101682471 / 103735401 (two entries) |

### 2.5 Logged-in URLs (not exercised with a session; existence probed logged-out)

| Purpose | URL | Logged-out probe |
|---|---|---|
| Search (classic) | `https://www.linkedin.com/jobs/search/?keywords=…&geoId=…&distance=25&f_AL=true&f_E=1%2C2&f_JT=F%2CI&f_WT=1%2C2%2C3&f_TPR=r86400&sortBy=DD&start=25` | 200 (serves the public page) |
| Search (new AI search) | `https://www.linkedin.com/jobs/search-results/?keywords=…` | **307** → `/uas/login?session_redirect=%2Fjobs%2Fsearch-results%2F…` |
| Job | `https://www.linkedin.com/jobs/view/{id}/` | 200 |
| SDUI apply flow | `https://www.linkedin.com/jobs/view/{id}/apply/?openSDUIApplyFlow=true` | **404** logged-out |
| Applied-jobs tracker | `https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED` | **307** → `/uas/login?session_redirect=%2Fjobs-tracker%3FcardType%3DAPPLIED` (the new tracker path) |
| Saved resumes / answers | `https://www.linkedin.com/jobs/application-settings/` | 302 → login |
| Recommended | `https://www.linkedin.com/jobs/collections/recommended/` | 302 → login |
| Internal API (Voyager) | `https://www.linkedin.com/voyager/api/...` | **403 `CSRF check failed`** without `csrf-token`; **401** with a fake `csrf-token` + `JSESSIONID`. **Do not use.** It needs `li_at` and is the most heavily policed surface. |

**Logged-in search parameter values.** These are DOC-SOURCED: wodsuz/EasyApplyJobsBot `utils.py` and the GodsScion `config/search.py` labels. The logged-in UI applies them; the guest API does not.

| Param | Value → meaning |
|---|---|
| `f_E` | `1` Internship (FR "Stagiaire / Alternant"), `2` Entry level (FR "Premier emploi"), `3` Associate, `4` Mid-Senior, `5` Director, `6` Executive. Multi: `1%2C2` |
| `f_JT` | `F` Full-time (Temps plein), `P` Part-time (Temps partiel), `C` Contract, `T` Temporary, `V` Volunteer, `I` Internship (FR **"Stage / Alternance"**), `O` Other (Autre). **There is no dedicated apprenticeship value.** For alternance, use `F,I,O` plus keywords. |
| `f_WT` | `1` On-site, `2` Remote, `3` Hybrid |
| `f_TPR` | `r86400`, `r604800`, `r2592000`, or any `r<seconds>` |
| `f_AL` | `true` = Easy Apply |
| `f_EA` | `true` = under 10 applicants |
| `f_SB2` | `1..9` = salary brackets ($40k+ … $200k+, US-centric) |
| `sortBy` | `DD` = most recent, `R` = most relevant |
| `distance` | miles: `0,5,10,25,50` (the UI also offers 100, UNCERTAIN) |
| `start` | step **25** logged-in, not 10 |

**Auth flow (logged-in):** manual login once in a persistent Chrome profile.
- Session cookie: `li_at`.
- `JSESSIONID="ajax:…"`, which is also the Voyager `csrf-token`.
- The login page is a React app with no `<form>`, hidden duplicate inputs and randomized ids. The GodsScion fixture of 2026-09-09 shows this. **Do not automate it.**
- A challenge page is `/checkpoint/challenge/...`. Unauthenticated access goes to `/authwall` or `/uas/login`.

---

## 3. Response / page shape

### 3.1 Guest search card — real sample (trimmed; Tailwind utility classes shortened)

```html
<!DOCTYPE html>
<li>
  <div class="base-card relative w-full … base-card--link base-search-card base-search-card--link job-search-card"
       data-entity-urn="urn:li:jobPosting:4303890241" data-impression-id="jobs-search-result-0"
       data-reference-id="l+HK6pewsjUFGhOM09TpOg==" data-tracking-id="E0OI48rK6et+O3ibQsSb0A==" data-column="1" data-row="1">
    <a class="base-card__full-link absolute …" data-tracking-control-name="public_jobs_jserp-result_search-card"
       href="https://fr.linkedin.com/jobs/view/d%C3%A9veloppeur-c%23-confirm%C3%A9-f-h-at-open-4303890241?position=1&amp;pageNum=0&amp;refId=…&amp;trackingId=…">
      <span class="sr-only">Développeur C# confirmé F/H</span>
    </a>
    <div class="search-entity-media">
      <img class="artdeco-entity-image artdeco-entity-image--square-4" data-delayed-url="https://media.licdn.com/dms/image/v2/…/company-logo_100_100/…" alt>
    </div>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Développeur C# confirmé F/H</h3>
      <h4 class="base-search-card__subtitle">
        <a class="hidden-nested-link" href="https://fr.linkedin.com/company/open-2?trk=public_jobs_jserp-result_job-search-card-subtitle">OPEN</a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Metz</span>
        <div class="job-posting-benefits text-sm">
          <icon class="job-posting-benefits__icon" …></icon>
          <span class="job-posting-benefits__text">Recrutement actif</span>
        </div>
        <time class="job-search-card__listdate" datetime="2026-09-11">il y a 1 semaine</time>
      </div>
    </div>
  </div>
</li>
```

| Field | Selector (inside each `li`) | Notes |
|---|---|---|
| job id | `div[data-entity-urn]` → `urn:li:jobPosting:(\d+)` | Primary key |
| url | `a.base-card__full-link[href]` | Strip the query string. Canonical form: `https://www.linkedin.com/jobs/view/{id}/` |
| title | `h3.base-search-card__title` | **Normalize whitespace**: it can contain `\n` ("Ingénieur Logiciel\nFull-Stack") |
| company | `h4.base-search-card__subtitle` (text) | |
| company page | `h4.base-search-card__subtitle a[href]` | Gives the `/company/<slug>` |
| location | `span.job-search-card__location` | FR: "Metz", "Nancy et périphérie", "Ville de Paris" |
| posted date | `time[datetime]` (ISO date) | Class `job-search-card__listdate--new` means less than 24 h |
| insight | `span.job-posting-benefits__text` | "Recrutement actif", "Soyez un des premiers candidats" (EN: "Actively Hiring", "Be an early applicant") |
| salary | `span.job-search-card__salary-info` | Rare in France |
| logo | `img[data-delayed-url]` | |

### 3.2 Guest job detail — real sample (trimmed; job 4410135372, an **Easy Apply** job; poster anonymized)

```html
<section class="top-card-layout container-lined overflow-hidden …">
  <div class="top-card-layout__card …">
    <a href="https://fr.linkedin.com/company/uti-group?trk=public_jobs_topcard_logo" data-tracking-control-name="public_jobs_topcard_logo">…</a>
    <div class="top-card-layout__entity-info-container …">
      <div class="top-card-layout__entity-info …">
        <a class="topcard__link" data-tracking-control-name="public_jobs_topcard-title"
           href="https://fr.linkedin.com/jobs/view/d%C3%A9veloppeur-c%2B%2B-at-uti-group-4410135372?trk=public_jobs_topcard-title">
          <h2 class="top-card-layout__title … topcard__title">Développeur C++</h2>
        </a>
        <h4 class="top-card-layout__second-subline …">
          <div class="topcard__flavor-row">
            <span class="topcard__flavor"><a class="topcard__org-name-link topcard__flavor--black-link"
               data-tracking-control-name="public_jobs_topcard-org-name"
               href="https://fr.linkedin.com/company/uti-group?trk=public_jobs_topcard-org-name">UTi GROUP.</a></span>
            <span class="topcard__flavor topcard__flavor--bullet">Strasbourg</span>
          </div>
          <div class="topcard__flavor-row">
            <span class="posted-time-ago__text topcard__flavor--metadata">il y a 5 jours</span>
            <figure class="num-applicants__figure topcard__flavor--metadata topcard__flavor--bullet">
              <figcaption class="num-applicants__caption">Plus de 200 candidats</figcaption>
            </figure>
          </div>
          <a class="face-pile … see-who-was-hired"
             href="https://www.linkedin.com/login?session_redirect=https%3A%2F%2Fwww%2Elinkedin%2Ecom%2Fsearch%2Fresults%2Fpeople%2F%3FfacetCurrentCompany%3D1245550&…">
             <p class="face-pile__text link-styled">Découvrez qui UTi GROUP. a recruté pour ce poste</p></a>
        </h4>
        <div class="top-card-layout__cta-container …">
          <!-- EASY APPLY job → onsite. External-apply jobs have data-tracking-control-name="public_jobs_apply-link-offsite_…" -->
          <button class="apply-button apply-button--default top-card-layout__cta … top-card-layout__cta--primary btn-md btn-primary"
                  data-tracking-control-name="public_jobs_apply-link-onsite">Postuler</button>
          <button class="… top-card-layout__cta--secondary btn-md btn-secondary sign-up-modal__outlet"
                  data-tracking-control-name="public_jobs_topcard-save-job">Enregistrer</button>
        </div>
      </div>
    </div>
  </div>
</section>

<div class="message-the-recruiter">                                   <!-- present on ~40 % of jobs -->
  <p>Envoyer un message directement à l’auteur de l’offre d’emploi</p>
  <div class="base-card … base-card--link base-main-card …">
    <a class="base-card__full-link …" href="https://fr.linkedin.com/in/prenom-nom-xxxxxxxx"><span class="sr-only">Prénom NOM</span></a>
    <div class="base-main-card__info …">
      <h3 class="base-main-card__title …">Prénom NOM</h3>
      <h4 class="base-main-card__subtitle …">Responsable Recrutement IT chez UTiGROUP</h4>
    </div>
    <div class="base-main-card__ctas …"><a class="message-the-recruiter__cta" href="https://www.linkedin.com/login">…</a></div>
  </div>
</div>

<section class="description">
  <div class="description__text description__text--rich">
    <section class="show-more-less-html">
      <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5 relative overflow-hidden">
        <p>Dans le cadre du renforcement des équipes IT de l’un de nos clients grands comptes du secteur bancaire, …</p> …
      </div>
    </section>
  </div>
  <ul class="description__job-criteria-list">
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Niveau hiérarchique</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">Cadre</span></li>
    <li …><h3 …>Type d’emploi</h3><span …>Temps plein</span></li>
    <li …><h3 …>Fonction</h3><span …>Ingénierie et Technologies de l’information</span></li>
    <li …><h3 …>Secteurs</h3><span …>Services et conseil en informatique</span></li>
  </ul>
</section>
<section class="job-assessment">…"Utilisez l’IA pour évaluer si votre profil correspond"… (login upsell, ignore)</section>
<code id="decoratedJobPostingId" style="display: none"><!--"4410135372"--></code>
<code id="referenceId" style="display: none"><!--"YEAnCg7rRPGadzX+4YCKFw=="--></code>
```

| Field | Selector | Notes |
|---|---|---|
| title | `h2.top-card-layout__title` | |
| company | `a.topcard__org-name-link` (text, href) | The href host can be `br.`, `be.` or `fr.linkedin.com` |
| company numeric id | regex `facetCurrentCompany%3D(\d+)` on `a.see-who-was-hired[href]` | Use it for `f_C` |
| location | `span.topcard__flavor--bullet` (first) | |
| posted (relative) | `span.posted-time-ago__text` | "il y a 5 jours", "Republié il y a …" means a repost |
| applicants | `.num-applicants__caption` | "Faites partie des 25 premiers candidats", "Plus de 200 candidats" |
| closed | `figcaption.closed-job__flavor--closed` | "Les candidatures ne sont plus acceptées" |
| description HTML | `div.show-more-less-html__markup` | Keep the HTML for ATS keyword extraction. **The language varies** (a PT "Descrição do Cargo" was seen) |
| criteria | `li.description__job-criteria-item` → `h3` label / `span` value | FR labels: `Niveau hiérarchique`, `Type d’emploi` (note U+2019 ’), `Fonction`, `Secteurs` |
| apply mode | `[data-tracking-control-name="public_jobs_apply-link-onsite"]` → **easy_apply**; `[data-tracking-control-name^="public_jobs_apply-link-offsite"]` → **external** | The external URL is not in the logged-out HTML |
| poster | `.message-the-recruiter a.base-card__full-link[href]`, `.base-main-card__title`, `.base-main-card__subtitle` | Personal data, see §4 |

Criteria values observed live (FR):
- `Type d’emploi`: `Temps plein`, `Temps partiel`, `Stage / Alternance`, `Autre`.
- `Niveau hiérarchique`: `Non pertinent`, `Stagiaire / Alternant`, `Premier emploi`, `Cadre`.
- The FR labels for Associate, Director and Executive were not observed.
- `Fonction` is often wrong. An apprentice developer at ArcelorMittal was tagged "Développement commercial et Ventes".

### 3.3 Typeahead JSON — real sample
```json
[{"id":"103167155","type":"GEO","displayName":"Metz, Grand Est, France","trackingId":"WNO5dGPKR+2F4DcFRKeJnQ=="},
 {"id":"90009656","type":"GEO","displayName":"Greater Metz Area","trackingId":"ZXHu976wR4G8UEw0E3XYlA=="}]
[{"id":"1951","type":"COMPANY","displayName":"Thales","trackingId":"Lf8BBPrsQJWw8x6QGvEs7g=="}]
```

### 3.4 429 body — real
```html
HTTP/1.1 429   content-type: text/html   content-length: 792   x-li-fabric: prod-ltx1
<html><head><script type="text/javascript">
    window.onload = function() {
      var trk = "qf"; …
      window.location.href = "https://www.linkedin.com/authwall?trk=" + trk +
          "&original_referer=" + document.referrer.substr(0, 200) +
          "&sessionRedirect=" + encodeURIComponent(window.location.href);
    }
</script></head></html>
```

### 3.5 Logged-in DOM (DOC-SOURCED: fixtures captured from a live session on 2026-09-09 in GodsScion `tests/fixtures/*.html`, English UI)

**Search result card** (virtualized list):
```html
<li id="ember220" data-occludable-job-id="4460606885">
  <div class="job-card-container job-card-list job-card-container--clickable …" data-job-id="4460606885">
    <div class="job-card-list__entity-lockup artdeco-entity-lockup artdeco-entity-lockup--size-4">
      <div class="artdeco-entity-lockup__title"><a class="job-card-container__link job-card-list__title--link"
           href="/jobs/view/4460606885/?eBP=…&refId=…&trackingId=…" aria-label="Forward Deployed Engineer with verification">…</a></div>
      <div class="artdeco-entity-lockup__subtitle"><span dir="ltr" class="pkXTSOjuybKaxMNVefkdwtUQOqEFHJfJW">TechNovaTime</span></div>
      <div class="artdeco-entity-lockup__caption">…</div>
      <ul class="job-card-container__metadata-wrapper"><li><span dir="ltr">New York, United States (Hybrid)</span></li></ul>
    </div>
    <div class="job-card-list__insight">…Actively reviewing applicants…</div>
    <ul class="job-card-list__footer-wrapper job-card-container__footer-wrapper">
      <li class="job-card-container__footer-item job-card-container__footer-job-state">Viewed</li>   <!-- or "Applied" -->
      <li class="job-card-container__footer-item"><span dir="ltr">Promoted</span></li>
      <li class="job-card-container__footer-item">…<span dir="ltr">Easy Apply</span></li>          <!-- FR: "Candidature simplifiée" -->
    </ul>
    <button class="job-card-container__action …" aria-label="Dismiss Forward Deployed Engineer job">…</button>
  </div>
</li>
```
- Dead classes: `job-card-container__primary-description`, `job-card-container__metadata-item`, `job-card-list__title`.
- Data-bearing spans have **randomized class names**. Use `data-occludable-job-id`, `artdeco-entity-lockup__*`, `aria-label` and `dir="ltr"`.

**Job top card (right pane):**
- `div.job-details-jobs-unified-top-card__container--two-pane` contains:
  - `.job-details-jobs-unified-top-card__company-name`;
  - `.job-details-jobs-unified-top-card__job-title a[href="/jobs/view/{id}/…"]`;
  - `.job-details-jobs-unified-top-card__tertiary-description-container span.tvm__text` ("New York, United States", "1 week ago", "Over 100 applicants", "Actively reviewing applicants");
  - `.job-details-fit-level-preferences` ("Hybrid", "Full-time");
  - `.jobs-s-apply .jobs-apply-button--top-card button#jobs-apply-button-id`.
- Description: `.jobs-box__html-content` (GodsScion `get_job_description`).
- About company: `.jobs-company__box`.
- Already applied: `.jobs-s-apply__application-link`.

**Apply button (variant A):**
```html
<button aria-label="Easy Apply to Forward Deployed Engineer at TechNovaTime" id="jobs-apply-button-id"
        class="jobs-apply-button artdeco-button artdeco-button--3 artdeco-button--primary ember-view"
        data-job-id="4460606885" data-live-test-job-apply-button="">
  <svg … data-test-icon="linkedin-bug-xxsmall">…</svg><span class="artdeco-button__text">Easy Apply</span></button>
```
- The same `#jobs-apply-button-id` is used for the **external "Apply" button**. Decide from the label ("Easy Apply" / "Candidature simplifiée") and by checking that a modal opens rather than a new tab.
- **Variant B (SDUI, seen for some accounts since about 2026-09-14):** `<a href="https://www.linkedin.com/jobs/view/{id}/apply/?openSDUIApplyFlow=true" aria-label="…Easy Apply…">`. External-apply links never carry `openSDUIApplyFlow`.

**Easy Apply modal, variant A** (single-page example; multi-step forms have Next/Review):
```html
<div data-test-modal role="dialog" class="artdeco-modal artdeco-modal--layer-default jobs-easy-apply-modal">
  <button aria-label="Dismiss" data-test-modal-close-btn class="artdeco-button artdeco-button--circle …">…</button>
  <div class="artdeco-modal__header"><h2 id="jobs-apply-header">Apply to TechNovaTime</h2></div>
  <div class="artdeco-modal__content jobs-easy-apply-modal__content p0">
    <div role="region" aria-label="Your job application progress is at 0 percent.">
      <form>
        <h3>Contact info</h3>
        <div class="fb-dash-form-element <random> mt1" data-test-form-element>
          <div data-test-text-entity-list-form-component>
            <label for="text-entity-list-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-…"
                   class="fb-dash-form-element__label fb-dash-form-element__label-title--is-required">Email</label>
            <span data-test-text-entity-list-form-required>Required</span>
            <select id="…" class="fb-dash-form-element__select-dropdown" aria-required="true" required data-test-text-entity-list-form-select>
              <option value="Select an option">Select an option</option><option value="me@example.com">…</option></select>
          </div></div>
        <div class="fb-dash-form-element …" data-test-form-element>… Phone country code <select>… "France (+33)" …</select></div>
        <div class="fb-dash-form-element …" data-test-form-element>
          <div data-test-single-line-text-form-component data-live-test-single-line-text-form-component>
            <label for="single-line-text-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-…">Phone</label>
            <input id="single-line-text-form-component-…" type="text" required></div>
          <!-- resume picker -->
          <div class="ui-attachment jobs-document-upload-redesign-card__container …" aria-label="Selected">
            <h3 class="jobs-document-upload-redesign-card__file-name">CV.pdf</h3>
            <input id="jobsDocumentCardToggle-ember357" type="radio">
            <label for="jobsDocumentCardToggle-ember357" class="jobs-document-upload-redesign-card__toggle-label">Deselect resume CV.pdf</label></div>
          <div class="ui-attachment jobs-document-upload-redesign-card__container …" aria-label="Select this resume">…</div>
          <label class="jobs-document-upload__upload-button artdeco-button …" for="jobs-document-upload-file-input-upload-resume-urn:li:fsu_jobApplicationFileUploadFormElement…">
            <span role="button" aria-label="Upload resume button. Only, DOC, DOCX, PDF formats are supported. Max file size is (5 MB).">Upload resume</span></label>
          <input name="file" id="jobs-document-upload-file-input-upload-resume-…" type="file">
          <button aria-label="Show 3 more resumes" type="button">Show 3 more resumes</button>
        </div>
      </form>
      <footer role="presentation">
        <input id="follow-company-checkbox" type="checkbox"> <!-- checked by default -->
        <label for="follow-company-checkbox">Follow TechNovaTime to stay up to date with their page.</label>
        <a data-control-name="application_settings_unify" data-test-easy-apply-footer-application-settings="true">…</a>
        <button aria-label="Submit application" data-live-test-easy-apply-submit-button type="button">Submit application</button>
      </footer>
```

**Easy Apply form, variant B (SDUI)** (DOC-SOURCED from cv_sender, 2026-09-22):
- There is **no `role="dialog"`** and **no `.jobs-easy-apply-content`**.
- The controls carry ids containing **`easyApplyFormElement`**, inside `.fb-dash-form-element` wrappers.
- The flow is rendered inside a **shadow root**. Page-level `document.querySelector` finds nothing, while Playwright CSS locators pierce it.
- Button labels are the same words (aria-label / text).
- Confirmation text: "Application sent" / "Your application was sent".

---

## 4. Limits, ToS, legal (France / GDPR / CNIL)

**LinkedIn limits**
- **Easy Apply limits.** Help article **a8068422**, fetched live, EN and FR, quoted:
  - "You can submit a set number of Easy Apply applications each day. If you reach this limit, you'll need to wait until it resets at the end of the day (UTC)."
  - "Rapidly submitting applications in a short time triggers a temporary pause on Easy Apply."
  - "we may lower a member's daily Easy Apply limit if we notice activity that seems inauthentic … you can verify your account to increase your Easy Apply limit."
  - The stated aim includes "ensure fairness by curbing automation and bots".
- **Daily number:** not published. Many LinkedIn posts from August–November 2025 report **50 Easy Apply per 24 h**.
  - GodsScion detects the limit through `.artdeco-inline-feedback__message` containing "exceeded the daily application limit" (EN).
  - The FR wording is unknown. Treat any inline feedback containing "limite" as a stop.
- **Invitations** (only relevant if jobbot ever touched connections, which it should not): about **100 per week** (help a550555 and community reports). Ignored or spam-marked invitations make the restriction worse.
- **Messages:** messaging non-connections needs InMail (Premium) or the poster's open-message CTA. This was not verified logged-in.

**User Agreement** (`https://www.linkedin.com/legal/user-agreement`, "Effective on November 3, 2025", fetched live). §8.2 "Don'ts" includes:
- "Develop, support or use software, devices, scripts, robots or any other means or processes (such as crawlers, browser plugins and add-ons or any other technology) to scrape or copy the Services, including profiles and other data from the Services;"
- "Override any security feature or bypass or circumvent any access controls or use limits of the Services (such as search results, profiles, or videos);"
- "Use bots or other unauthorized automated methods to access the Services, add or download contacts, send or redirect messages, create, comment on, like, share, or re-share posts, or otherwise drive inauthentic engagement;"
- FR version: "utiliser des bots ou d’autres méthodes automatisées non autorisées pour accéder aux Services, ajouter ou télécharger des contacts, envoyer ou rediriger des messages…"
- §3: "LinkedIn reserves the right to restrict, suspend, or terminate your account if you breach this Contract…".
- EU/EEA residents contract with **LinkedIn Ireland Unlimited Company**. Disputes go to Irish courts and Irish law.
- Help article **a1341387** ("Prohibited software and extensions"): "we don't permit the use of any third party software, including 'crawlers', bots, browser plug-ins, or browser extensions that scrape, modify the appearance of, or automate activity on LinkedIn's website."
- Help article **a1340522** ("Account restrictions", section "Automated tools violations"): "Automated inauthentic activity violates the LinkedIn User Agreement and can result in temporary or permanent restriction of your account."
- **Consequence for jobbot:** automating Easy Apply breaches the contract, whatever the volume. It is not a criminal matter in France for your own account. The realistic sanction is an **account restriction** and a verification (ID) flow, possibly permanent. Only the candidate can decide to accept this risk. jobbot should make the risk explicit in its settings: `linkedin.easy_apply.enabled=false` by default, and when enabled, `confirm_before_submit=true` by default.

**robots.txt** (live): the header says "The use of robots or other automated means to access LinkedIn without the express permission of LinkedIn is strictly prohibited". The file contains `User-agent: * / Disallow: /`, and `/jobs-guest/`, `/jobs/view/externalApply/` and `/jobs?runSearch*` are disallowed even for Googlebot.

**Enforcement history** (DOC-SOURCED):
- LinkedIn v. Nubela/Proxycurl, N.D. Cal. 3:25-cv-00828, filed 2025-01-24. It settled, and Proxycurl shut down on **2025-07-04**.
- Enforcement targets fake accounts and scraping at scale. Nothing public shows action against one person's low-volume job search, but account restrictions for Easy Apply bots are widely reported.

**GDPR / CNIL**
- Hiring-team names, headlines and profile URLs are **personal data**.
- A private person's own job search is arguably a "purely personal or household activity" (GDPR art. 2(2)(c)), which is outside GDPR. This is UNCERTAIN: the exemption is read narrowly.
- Minimize anyway:
  - store name, headline, profile URL, job id and date only;
  - no photos;
  - no enrichment;
  - automatic deletion after 6 months or when the application is closed;
  - no sharing;
  - local SQLite only.
- **CNIL, 2024-12-19: KASPR fined €240 000** for a Chrome extension that collected LinkedIn members' contact details, including those who had limited their visibility (sources: https://www.cnil.fr/fr/aspiration-de-donnees-sanction-de-240-000-euros-lencontre-de-la-societe-kaspr and https://www.cnil.fr/en/data-scraping-kaspr-fined-eu240000). **Do not harvest e-mails or phone numbers from LinkedIn profiles**, not even from 1st-degree connections' "Contact info".
- For spontaneous applications, get generic company addresses (`recrutement@`, `rh@`, `contact@`) from company websites or legal notices. That belongs to the email module.
- Sending one targeted, relevant e-mail to a professional address about a job is ordinary use. Still give the recipient a way to object.

---

## 5. Gotchas & anti-bot measures

**Guest endpoints**
1. **Burst limit:** requests 0–9 fired at about 0.3 s intervals returned 200, then request 10 returned **429**. With 3 s polling, access came back after about **6.5 s**.
   - The limit is per IP and short-window.
   - Use at least 3 s + jitter. On a 429, back off exponentially (30 s, 2 min, 10 min) and **abort the run** after 3 consecutive 429s.
   - Do not rotate proxies. That counts as circumventing access controls under §8.2.
2. The 429 body is a JS **authwall redirect** (§3.4), not JSON. Check `status_code`, not the body.
3. `start` must stay **below 1000**, and the end of results is a 26-byte body (`<!DOCTYPE html>\n\n<!---->`). Split big queries by geo or recency instead of paging deep.
4. Results **shift between pages** as new jobs arrive: duplicates across pages and queries are normal. Deduplicate on job id. A given job can appear under several keywords.
5. `f_JT`, `f_E`, `f_WT` and `sortBy` are **ignored on guest** (§2.1). Do not trust them for filtering. Default relevance order sometimes surfaces year-old jobs: always pass `f_TPR`.
6. **Language depends on `Accept-Language`.** FR locations are short ("Metz", "Nancy et périphérie"); EN ones are long. Pin `fr-FR` and test the parser against FR strings. Criteria labels use a typographic apostrophe, `Type d’emploi` (U+2019).
7. Keywords are loose. `développeur` matches "business developer" and "développeur commercial", and `alternance` matches every field. You must post-filter (§6.4).
8. The guest `Fonction` and `Secteurs` values are unreliable, and the description can be in PT, DE or EN (Ausbildung postings in Grand Est). Run a language detector before ATS scoring.
9. Cloudflare sits in front (`__cf_bm`, 30 min). Keep **one** `httpx.Client` per run so the cookies persist. Do not send `li_at`.
10. Whether a plain `python-httpx/x` UA is blocked is inconclusive: my one try fell inside a 429 window. Always send a real browser UA, as HelloWork research found for its WAF.

**Logged-in Easy Apply**
11. **Two UI variants at once** (A: Ember modal; B: SDUI anchor + shadow DOM). Detect both: `a[href*="openSDUIApplyFlow"]` first, then `button#jobs-apply-button-id` with an Easy Apply label.
12. **Use CSS, text or role locators, never XPath**, because XPath does not pierce shadow roots (Playwright docs). Also avoid `document.querySelector` inside `page.evaluate` for variant B. Evaluate on element handles instead (`el.getRootNode()`).
13. **Scope Next/Review/Submit lookups to the dialog** where it exists. A page-wide "Next"/"Suivant" matches the results **pagination** ("View next page") and pages away mid-application (GodsScion bug note). All modal buttons are `type="button"`: `button[type=submit]` finds nothing.
14. **Job cards are virtualized.** Off-screen `<li>` elements are not in the DOM and rendered ones go stale. Scroll the **results pane** (the nearest scrollable ancestor of a card), not the window, and re-query `li[data-occludable-job-id]` each iteration. The cv_sender old scan saw only about 7 of 25 jobs.
15. Card and top-card class names are **randomized hashes** (e.g. `pkXTSOjuybKaxMNVefkdwtUQOqEFHJfJW`). Anchor on `data-*`, `aria-label`, `artdeco-entity-lockup__*`, `dir="ltr"` and `id="jobs-apply-button-id"`. Never key off `artdeco-button--3` (a size token).
16. The **"Follow company" checkbox (`#follow-company-checkbox`) is pre-checked**. Uncheck it through `label[for=follow-company-checkbox]` if the candidate does not want to follow.
17. LinkedIn **pre-fills** from saved answers and resumes (`/jobs/application-settings/`). Do not overwrite a prefilled value unless configured. A `<select>` showing "Select an option" (FR probably "Sélectionner une option", UNCERTAIN) counts as unanswered.
18. Resume upload accepts **PDF / DOC / DOCX up to 5 MB** through `input[name="file"][type="file"]`. Each upload adds to the saved-resume list. Name files clearly (`CV_NomPrenom_<entreprise>.pdf`), because the recruiter sees the filename.
19. **Submission is irreversible**: "Vous ne pouvez pas modifier ou retirer une candidature envoyée" (FR help a512388). Never let the bot guess answers:
    - leave unknown radios and selects untouched;
    - **never auto-tick attestation or consent checkboxes** ("certify", "consent", "I agree"; FR: "certifie", "consens", "j’accepte");
    - route the job to "needs input". GodsScion learned this the hard way, and its comments list the wrong-data incidents.
20. Validation errors appear as `.artdeco-inline-feedback--error` / `.artdeco-inline-feedback__message`. When the error set does not change between two passes, you are **stuck**: discard and skip.
21. **Discard flow:**
    - click `button[data-test-modal-close-btn]` (aria "Dismiss"; FR probably "Ignorer", UNCERTAIN);
    - wait about 2 s for the animation;
    - click the discard confirm: `button[data-control-name="discard_application_confirm_btn"]`, or the text "Discard" / FR probably "Supprimer" (UNCERTAIN);
    - verify the modal is gone. Escape alone is unreliable.
    - A modal left open makes every later click fail with "Other element would receive the click".
22. **Login page** (React): no `<form>`, no `name` attributes, `useId()` ids, hidden duplicate inputs, and "Sign in with Apple" above "Sign in". **Log in manually** in the persistent profile. If the session is ever lost, stop and ask the human.
23. **Checkpoints:** any URL containing `/checkpoint/`, `/authwall`, `/uas/login` or `/login`, a captcha iframe, or a "restricted" or "verify your identity" page means **stop the whole LinkedIn module for 24 h** and notify the human. Never try to solve it automatically.
24. **Chrome 136+ blocks automation of the default user-data dir** (Playwright docs). Use a **dedicated** `user_data_dir` (e.g. `data/linkedin_profile/`). GodsScion's "use your default Chrome profile" default is obsolete.
25. **Browser fingerprint hygiene** (not evasion):
    - use the real installed Chrome (`channel="chrome"`), **headful**, at a normal window size;
    - **do not override the UA**. cv_sender hard-codes "Chrome/124" on a newer binary, which creates a UA versus Client-Hints mismatch;
    - use a stable residential IP (the candidate's own), with `locale` and `timezone` matching the account (fr-FR, Europe/Paris).
    - Playwright's default `navigator.webdriver=true` is visible to the page. The reference bots use undetected-chromedriver (GodsScion) or `--disable-blink-features=AutomationControlled` (cv_sender).
    - LinkedIn's own documentation says what triggers limits: **speed and volume**. Behaviour matters more than fingerprint. No flag makes automation ToS-compliant.
26. **Human-like pacing:**
    - scroll the description before applying;
    - 1–4 s between field fills;
    - type with small per-key delays (`press_sequentially(delay=60..140)`) only for free-text answers;
    - 90–240 s between applications;
    - no more than 60–120 job views per session.
27. The Easy Apply share is small and competition is high. Easy Apply jobs showed "Plus de 200 candidats" versus "25 premiers" for others. Prefer `f_EA=true` (<10 applicants) and fresh `f_TPR=r86400` jobs.

**Reference bots (for selectors). Repo metadata comes from the GitHub API on 2026-09-23.**

| Repo | Stack | Last push / last commit | Status |
|---|---|---|---|
| [GodsScion/Auto_job_applier_linkedIn](https://github.com/GodsScion/Auto_job_applier_linkedIn) | Selenium + undetected-chromedriver, EN UI only | pushed **2026-09-11**, commit 2026-09-10 (e0b2401a); ~2.9k★ | **Best maintained.** Has `tests/test_selectors_groundtruth.py` against live fixtures from 2026-09-09. Uses UI filter clicks, not URL params. |
| [YonatanVol/cv_sender](https://github.com/YonatanVol/cv_sender) | **Playwright** (async), persistent profile | pushed **2026-09-22** | Handles the **SDUI variant (B)**. Two-phase prepare/confirm. EN + Hebrew labels. |
| [selcukgural/AfterApply](https://github.com/selcukgural/AfterApply) | .NET + extension | PRs 2026-09-06 and 2026-09-12 | Guest endpoints; `hirer-card__hirer-information` / `hirer-card__job-poster`. SDUI hiring team only by heading text. |
| [wodsuz/EasyApplyJobsBot](https://github.com/wodsuz/EasyApplyJobsBot) | Selenium | pushed 2026-05-18, commit 2026-03-29; ~0.8k★ | Builds URLs with `f_AL/f_E/f_JT/f_WT/f_SB2/sortBy`. Selectors `button[aria-label='Continue to next step']`, `…'Review your application'`, `…'Submit application'`, `label[for='follow-company-checkbox']`, `//li[@data-occludable-job-id]`. Has a bug: `"Intership"` typo means `f_JT=I` is never emitted. |
| feder-cr/Jobs_Applier_AI_Agent_AIHawk and AIHawk-FOSS/Auto_Jobs_Applier_AI_Agent | — | both now **redirect to `feder-cr/invisible_playwright_mcp`** (an anti-detect browser MCP) | **The AIHawk LinkedIn applier is gone upstream.** Only forks remain. Do not base code on it. |
| NathanDuma/LinkedIn-Easy-Apply-Bot | Selenium | last push 2023-06-11 | Stale |
| madingess/EasyApplyBot | Selenium | last push 2025-03-12 | Stale |

---

## 6. Concrete Python implementation notes (sync httpx / Playwright sync)

### 6.1 Guest client (httpx, sync)

> This block and the §6.4 classifier were **executed verbatim against the live endpoints on 2026-09-23**. The query was `alternance développeur`, Metz, 50 mi, one month.
> - It returned 20 cards over 2 pages, and 8 details parsed correctly: company_id, apply_mode (`easy_apply`/`external`), poster and criteria.
> - The only defect found was an accent-less CAPS title slipping past `NOT_DEV_RE`. It is fixed below by accepting `[ée]`; accent-folding is recommended.

```python
# jobbot/sources/linkedin_guest.py
from __future__ import annotations
import random, re, time
from dataclasses import dataclass
import httpx
from bs4 import BeautifulSoup

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
TYPEAHEAD_URL = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept-Language": "fr-FR,fr;q=0.9",
}
PAGE_SIZE = 10
MAX_START = 990                 # start=1000 -> HTTP 400
MIN_GAP_S, JITTER_S = 3.0, 2.5  # 10 req in ~3 s triggers 429
BACKOFFS_S = (30, 120, 600)
JOB_ID_RE = re.compile(r"urn:li:jobPosting:(\d+)")
COMPANY_ID_RE = re.compile(r"facetCurrentCompany%3D(\d+)")

class RateLimited(RuntimeError): ...

@dataclass(frozen=True)
class Card:
    job_id: str; title: str; company: str | None; company_url: str | None
    location: str | None; posted_date: str | None; is_new: bool; insight: str | None; url: str

class GuestClient:
    def __init__(self) -> None:
        self._http = httpx.Client(headers=HEADERS, timeout=20, follow_redirects=False)
        self._last = 0.0

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        for attempt, backoff in enumerate((0, *BACKOFFS_S)):
            if backoff:
                time.sleep(backoff)
            wait = MIN_GAP_S + random.uniform(0, JITTER_S) - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            resp = self._http.get(url, params=params)
            self._last = time.monotonic()
            if resp.status_code != 429:
                return resp
        raise RateLimited(f"429 x{len(BACKOFFS_S)+1} on {url}")

    def search_page(self, *, keywords: str, geo_id: str | None = None, location: str | None = None,
                    distance_mi: int | None = None, tpr_seconds: int = 86_400,
                    easy_apply_only: bool = False, company_ids: list[str] | None = None,
                    start: int = 0) -> list[Card]:
        if start > MAX_START:
            return []
        params: dict[str, str] = {"keywords": keywords, "f_TPR": f"r{tpr_seconds}", "start": str(start)}
        if geo_id: params["geoId"] = geo_id
        if location: params["location"] = location
        if distance_mi is not None: params["distance"] = str(distance_mi)  # 0,5,10,25,50
        if easy_apply_only: params["f_AL"] = "true"
        if company_ids: params["f_C"] = ",".join(company_ids)
        # f_JT / f_E / f_WT / sortBy are IGNORED by this endpoint (verified 2026-09-23) - do not send.
        resp = self._get(SEARCH_URL, params)
        if resp.status_code == 400:           # start >= 1000
            return []
        resp.raise_for_status()
        return parse_cards(resp.text)

    def search_all(self, max_pages: int = 3, **kw) -> list[Card]:
        seen: dict[str, Card] = {}
        for page in range(max_pages):
            cards = self.search_page(start=page * PAGE_SIZE, **kw)
            fresh = [c for c in cards if c.job_id not in seen]
            seen.update({c.job_id: c for c in fresh})
            if len(cards) < PAGE_SIZE or not fresh:
                break
        return list(seen.values())

    def detail_html(self, job_id: str) -> str | None:
        if not job_id.isdigit():
            raise ValueError("LinkedIn job id must be numeric")
        resp = self._get(DETAIL_URL.format(job_id=job_id))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    def geo_lookup(self, query: str) -> list[dict]:
        resp = self._get(TYPEAHEAD_URL, {"typeaheadType": "GEO", "query": query,
                         "geoTypes": "ADMIN_DIVISION_1,POPULATED_PLACE,ADMIN_DIVISION_2,MARKET_AREA,COUNTRY_REGION"})
        resp.raise_for_status()
        return resp.json()


def _txt(el) -> str | None:
    return " ".join(el.get_text(" ", strip=True).split()) if el else None

def parse_cards(html: str) -> list[Card]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Card] = {}
    for card in soup.select("[data-entity-urn]"):
        m = JOB_ID_RE.search(card.get("data-entity-urn", ""))
        if not m:
            continue
        job_id = m.group(1)
        link = card.select_one("a.base-card__full-link")
        comp_a = card.select_one("h4.base-search-card__subtitle a")
        tm = card.select_one("time")
        out[job_id] = Card(
            job_id=job_id,
            title=_txt(card.select_one("h3.base-search-card__title")) or "",
            company=_txt(card.select_one("h4.base-search-card__subtitle")),
            company_url=(comp_a.get("href", "").split("?")[0] if comp_a else None),
            location=_txt(card.select_one("span.job-search-card__location")),
            posted_date=tm.get("datetime") if tm else None,
            is_new=bool(tm and "job-search-card__listdate--new" in (tm.get("class") or [])),
            insight=_txt(card.select_one("span.job-posting-benefits__text")),
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        )
    return list(out.values())

def parse_detail(html: str) -> dict:
    s = BeautifulSoup(html, "html.parser")
    crit = {}
    for li in s.select("li.description__job-criteria-item"):
        k, v = li.select_one("h3"), li.select_one("span")
        if k and v:
            crit[_txt(k).replace("’", "'")] = _txt(v)
    poster = None
    mtr = s.select_one(".message-the-recruiter")
    if mtr:
        a = mtr.select_one("a.base-card__full-link")
        poster = {"name": _txt(mtr.select_one(".base-main-card__title")),
                  "headline": _txt(mtr.select_one(".base-main-card__subtitle")),
                  "profile_url": (a.get("href", "").split("?")[0] if a else None)}
    who = s.select_one("a.see-who-was-hired")
    cid = COMPANY_ID_RE.search(who.get("href", "")) if who else None
    desc = s.select_one("div.show-more-less-html__markup")
    if s.select_one('[data-tracking-control-name="public_jobs_apply-link-onsite"]'):
        apply_mode = "easy_apply"
    elif s.select_one('[data-tracking-control-name^="public_jobs_apply-link-offsite"]'):
        apply_mode = "external"
    else:
        apply_mode = "unknown"
    org = s.select_one("a.topcard__org-name-link")
    return {
        "title": _txt(s.select_one("h2.top-card-layout__title")),
        "company": _txt(org),
        "company_url": org.get("href", "").split("?")[0] if org else None,
        "company_id": cid.group(1) if cid else None,
        "location": _txt(s.select_one("span.topcard__flavor--bullet")),
        "posted_relative": _txt(s.select_one("span.posted-time-ago__text")),
        "applicants": _txt(s.select_one(".num-applicants__caption")),
        "closed": bool(s.select_one(".closed-job__flavor--closed")),
        "description_html": str(desc) if desc else None,
        "description_text": _txt(desc),
        "seniority": crit.get("Niveau hiérarchique"),
        "employment_type": crit.get("Type d'emploi"),
        "job_function": crit.get("Fonction"),
        "industries": crit.get("Secteurs"),
        "apply_mode": apply_mode,
        "poster": poster,
    }
```

### 6.2 Query plan for this candidate (daily run, about 80 search + about 40 detail requests, about 10 min)

```python
GEOS = [
    {"geo_id": "103167155", "distance_mi": 50},   # Metz + 80 km: Nancy (~55 km), Thionville (~30 km), Luxembourg-ville (~65 km)
    {"geo_id": "104042105"},                       # Luxembourg (country)
    {"geo_id": "105015875"},                       # France, for remote only: keep jobs whose text matches REMOTE_RE
]
KEYWORDS = [
    "alternance développeur", "alternance full stack", "apprenti développeur",
    "alternance ingénieur logiciel", "alternance développeur web",
    "développeur junior", "développeur full stack", "développeur web", "ingénieur logiciel junior",
]
# First run: tpr_seconds=2_592_000 (month) for backfill; then daily tpr_seconds=86_400.
# France-wide queries: add "télétravail" or "remote" to keywords since f_WT is ignored by guest.
```

### 6.3 Easy Apply share check (optional, cheap)
`GET https://www.linkedin.com/jobs/search?keywords=…&geoId=…&distance=50&f_AL=true` → `.results-context-header__job-count`. Use it to decide whether a logged-in Easy Apply session is even worth opening today. On 2026-09-23, Metz had 5 new Easy Apply jobs per week.

### 6.4 Classification (client-side, since guest filters are ignored)

```python
import re
ALTERNANCE_RE = re.compile(
    r"\b(alternan(?:ce|t|te|ts|tes)|apprenti(?:e|s|es)?|apprentissage|contrat\s+(?:d['’]\s*)?apprentissage|"
    r"contrat\s+de\s+professionnalisation|contrat\s+pro|work[\s-]?study|apprenticeship|ausbildung|duales?\s+studium)\b",
    re.I)
INTERNSHIP_ONLY_RE = re.compile(r"\b(stage|stagiaire|internship|intern|pfe|stage\s+de\s+fin\s+d['’]études)\b", re.I)
JUNIOR_RE = re.compile(r"\b(junior|débutant|jeune\s+diplômé|premier\s+emploi|entry[\s-]level|graduate|0\s*[-à]\s*2\s*ans)\b", re.I)
SENIOR_RE = re.compile(r"\b(senior|sénior|confirmé|expérimenté|lead|expert|architecte|principal|staff|manager|head\s+of|"
                       r"(?:[4-9]|1\d)\s*\+?\s*ans\s+d['’]expérience)\b", re.I)
REMOTE_RE = re.compile(r"\b(télétravail|full[\s-]?remote|100\s*%\s*remote|à\s+distance|remote)\b", re.I)
NOT_DEV_RE = re.compile(r"\b(business\s+developer|d[ée]veloppeur\s+commercial|d[ée]veloppement\s+commercial|charg[ée]e?\s+de\s+d[ée]veloppement)\b", re.I)
# Titles are often in CAPS WITHOUT accents ("ALTERNANCE- CHARGE DE DEVELOPPEMENT METZ H/F", live 4460976990):
# best practice = fold accents first (unicodedata.normalize("NFKD", s).encode("ascii","ignore").decode()) and write ASCII regexes.
BAC5_RE = re.compile(r"\b(bac\s*\+\s*5|master|mastère|m1|m2|ingénieur|msc)\b", re.I)
RHYTHM_RE = re.compile(r"\b(\d\s*(?:jours?|semaines?)\s*/\s*\d\s*(?:jours?|semaines?)|24\s*mois|2\s*ans)\b", re.I)

def classify(title: str, description: str, employment_type: str | None, seniority: str | None) -> str:
    text = f"{title}\n{description}"
    if NOT_DEV_RE.search(title):
        return "out_of_scope"
    if ALTERNANCE_RE.search(text) or employment_type == "Stage / Alternance" or seniority == "Stagiaire / Alternant":
        return "alternance"
    if INTERNSHIP_ONLY_RE.search(title):
        return "internship"          # not wanted
    if SENIOR_RE.search(title):
        return "senior"              # not wanted
    return "junior_cdi" if JUNIOR_RE.search(text) or seniority == "Premier emploi" else "cdi_unknown_level"
```
Score `alternance` jobs higher when `BAC5_RE` and `RHYTHM_RE` match (a 24-month Bac+5 fits Exemple Digital School). Start date: look for "octobre 2026", "rentrée 2026" or "septembre 2026".

### 6.5 Playwright persistent context (sync)

```python
# jobbot/apply/linkedin_browser.py
from pathlib import Path
from playwright.sync_api import sync_playwright, BrowserContext, Page

PROFILE_DIR = Path("data") / "linkedin_profile"  # dedicated, never the default Chrome profile
CHECKPOINT_MARKERS = ("/checkpoint/", "/authwall", "/uas/login", "linkedin.com/login")

def open_linkedin(p) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",            # real installed Chrome (Chrome 136+ forbids automating the *default* profile dir)
        headless=False,              # headful only
        locale="fr-FR",
        timezone_id="Europe/Paris",
        viewport={"width": 1366, "height": 900},
        accept_downloads=False,
        # no user_agent override: keep UA and Client Hints consistent with the real binary
    )

def ensure_logged_in(page: Page) -> bool:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    return not any(m in page.url for m in CHECKPOINT_MARKERS)

# First run: open_linkedin(), page.goto("https://www.linkedin.com/login"), the HUMAN logs in, then close.
# Later runs: if ensure_logged_in() is False -> stop the LinkedIn module and notify the user. Never type credentials.
```

### 6.6 Easy Apply walker (sync, both variants, EN + FR labels)

Selector constants. Language-independent hooks come first; the FR strings marked (?) are unverified.

```python
import random, re
EASY_APPLY_LABELS = ("Candidature simplifiée", "Easy Apply")
OPENERS = (
    "a[href*='openSDUIApplyFlow=true']",                                   # variant B (SDUI)
    "button#jobs-apply-button-id",                                          # variant A (also used by external Apply!)
    "button.jobs-apply-button",
)
MODAL_A = "div.jobs-easy-apply-modal[role='dialog'], div[data-test-modal][role='dialog']"
CONTROLS = ("[data-test-form-element] input, [data-test-form-element] select, [data-test-form-element] textarea, "
            "[id*='easyApplyFormElement'], .fb-dash-form-element input, .fb-dash-form-element select, "
            ".fb-dash-form-element textarea")
NEXT_SEL = ("button[data-easy-apply-next-button]",                     # (?) seen in older bots
            "button[aria-label='Continue to next step']",
            "button[aria-label='Passer à l’étape suivante']",          # (?) FR aria
            "button:has-text('Suivant')", "button:has-text('Next')")
REVIEW_SEL = ("button[aria-label='Review your application']",
              "button[aria-label='Vérifiez votre candidature']",       # (?) FR aria
              "button:has-text('Vérifier')", "button:has-text('Review')")
SUBMIT_SEL = ("button[data-live-test-easy-apply-submit-button]",
              "button[aria-label='Submit application']",
              "button[aria-label='Envoyer la candidature']",            # FR text documented in help a512388
              "button:has-text('Envoyer la candidature')", "button:has-text('Submit application')")
SENT_TEXTS = ("Candidature envoyée", "Votre candidature a été envoyée", "Application sent", "Your application was sent")
ERROR_SEL = ".artdeco-inline-feedback--error, .artdeco-inline-feedback__message"
DISMISS_SEL = ("button[data-test-modal-close-btn]", "button[aria-label='Dismiss']", "button[aria-label='Ignorer']")  # FR (?)
DISCARD_SEL = ("button[data-control-name='discard_application_confirm_btn']",
               "button:has-text('Supprimer')", "button:has-text('Discard')")                                    # FR (?)
FILE_INPUT = "input[type='file'][name='file'], input[type='file']"
FOLLOW_CB = "input#follow-company-checkbox"
ATTESTATION_RE = re.compile(r"(certif|attest|consen|j['’]accepte|i agree|agree|acknowledg|reconnais|conditions|terms|politique|policy)", re.I)
```

Flow sketch. Step caps and stall detection are mandatory.

```python
def first_visible(scope, selectors, timeout_ms=1500):
    for sel in selectors:
        loc = scope.locator(sel).first
        try:
            if loc.is_visible(timeout=timeout_ms) and loc.is_enabled():
                return loc
        except Exception:
            continue
    return None

def open_easy_apply(page) -> str:
    """Return 'A', 'B', 'external' or 'none'. Never click an external Apply."""
    sdui = page.locator("a[href*='openSDUIApplyFlow=true']").first
    if sdui.count() and sdui.is_visible():
        sdui.click(); page.wait_for_timeout(1800); return "B"
    btn = first_visible(page, OPENERS[1:])
    if not btn:
        return "none"
    label = (btn.get_attribute("aria-label") or "") + " " + btn.inner_text()
    if not any(l in label for l in EASY_APPLY_LABELS):
        return "external"
    btn.click(); page.wait_for_timeout(1800)
    return "A" if page.locator(MODAL_A).first.is_visible() else "B"

def walk(page, variant, answers, cv_path, confirm_submit) -> str:
    scope = page.locator(MODAL_A).first if variant == "A" else page
    prev_errors = None
    for _ in range(12):                                  # hard step cap
        upload_cv_if_asked(scope, cv_path)               # scope.locator(FILE_INPUT).set_input_files(cv_path)
        missing = fill_step(scope, answers)              # returns list of unanswered REQUIRED labels
        if missing:
            return "needs_input:" + "; ".join(missing[:3])
        submit = first_visible(scope, SUBMIT_SEL, 800)
        if submit:
            set_follow(scope, want=False)
            if not confirm_submit(page.screenshot()):    # human-in-the-loop gate
                return "held_for_review"
            submit.click()
            return "sent" if wait_sent(page) else "unconfirmed"
        nxt = first_visible(scope, REVIEW_SEL, 800) or first_visible(scope, NEXT_SEL, 800)
        if not nxt:
            return "stuck_no_button"
        nxt.click(); page.wait_for_timeout(random.randint(1200, 2500))
        errs = tuple(t.strip() for t in scope.locator(ERROR_SEL).all_inner_texts())
        if errs and errs == prev_errors:
            return "stuck_validation:" + " | ".join(errs[:2])
        prev_errors = errs
    return "too_many_steps"

def wait_sent(page) -> bool:
    for _ in range(12):                                  # confirmation can take several seconds (cv_sender)
        page.wait_for_timeout(1000)
        if first_visible(page, SUBMIT_SEL, 300):
            continue
        if any(page.get_by_text(t).first.is_visible() for t in SENT_TEXTS if page.get_by_text(t).count()):
            return True
    return False
```

**Question field handling.** Read each `CONTROLS` element with its visible label: `label[for=id]` → `fieldset legend` / `span[data-test-form-builder-radio-button-form-component__title]` → `aria-label`.

| Type | DOM hook | How to answer |
|---|---|---|
| Select | `select` (`data-test-text-entity-list-form-select`) | `loc.select_option(label=answer)`, falling back to a whole-word option match. The placeholder "Select an option" (FR ?) counts as unanswered. Never pick a random option. |
| Radio (Yes/No etc.) | `fieldset[data-test-form-builder-radio-button-form-component="true"]` | The question text comes from the fieldset legend or title span, **not** the radio's own label ("Oui"). Click `label[for=<input id>]`. |
| Single-line text | `[data-test-single-line-text-form-component] input[type=text]` | `fill()`. Labels to map: téléphone/phone, ville/city, LinkedIn URL, portfolio/GitHub, salaire/salary, préavis/notice. |
| **Numeric** ("Combien d'années d'expérience avez-vous avec React ?") | text input with numeric validation (error like "Enter a whole number…", FR ?) | Take per-skill values from the answer bank; for a junior, the honest value is often 0–2. Never reuse the "total experience" answer for a skill-specific question (GodsScion bug note). Integer strings only. |
| **Typeahead city** ("Ville", "Location (city)") | text input with `role="combobox"` | `press_sequentially("Metz", delay=80)`, wait for `[role='listbox'] [role='option']`, then click the option whose text starts with "Metz, Grand Est". GodsScion's fallback is ArrowDown + Enter. |
| Textarea (cover letter, "Pourquoi…") | `textarea` | Use the per-job generated letter (the jobbot LLM module) **reviewed by the human**. Keep it under ~2,000 characters. |
| Checkbox | `input[type=checkbox]` (skip `#follow-company-checkbox`) | Tick only when the label matches an explicit bank entry. **Never** tick anything matching `ATTESTATION_RE`: send it to needs_input. |
| Date picker | `button[aria-label*='This is today']` (EN) | Rare. Otherwise send to needs_input. |
| File (CV / cover letter) | `input[type=file][name=file]` | `set_input_files(pdf_path)`. PDF/DOC/DOCX ≤ 5 MB. Or select an existing resume card: `.jobs-document-upload-redesign-card__container[aria-label='Select this resume']` → click its `label.jobs-document-upload-redesign-card__toggle-label`. |

**Answer bank** (SQLite table `answers(label_norm TEXT PRIMARY KEY, answer TEXT, kind TEXT, lang TEXT, updated_at)`):
- Normalize labels: lowercase, strip accents, collapse whitespace, remove "*", "Required", "Obligatoire".
- When a human answers a needs_input question, save it and reuse it next time (the cv_sender pattern).

### 6.7 Safety governor (config-driven; values are judgment calls, see §4)

```python
LINKEDIN_POLICY = {
    "easy_apply_enabled": False,          # opt-in: the user must accept the ToS/account risk explicitly
    "confirm_before_submit": True,        # human gate at Review
    "daily_easy_apply_cap": 12,           # reported hard limit ~50/24h (UTC reset), stay far below
    "gap_between_applies_s": (90, 240),
    "active_hours_local": (9, 19),        # Europe/Paris
    "max_sessions_per_day": 2,
    "max_job_views_per_session": 100,
    "stop_on": ["checkpoint", "authwall", "captcha", "limit_message", "logged_out", "3_consecutive_failures"],
    "cooldown_after_stop_h": 24,
    "automate_messages": False,           # never; UA 8.2
    "automate_invitations": False,        # never
}
```
Limit detection: after each submit or open, if `.artdeco-inline-feedback__message` (or any toast) contains "limit"/"limite", or "exceeded the daily application limit", set `limit_hit_until = next 00:00 UTC`.

### 6.8 Hiring team (logged-in, read-only)
- Variant A: `.hirer-card__hirer-information` → `a[href*='/in/']` (profile), `span`/`strong` (name). The companion class is `hirer-card__job-poster`. The section heading is "Meet the hiring team" (FR probably "Rencontrez l'équipe de recrutement", UNCERTAIN).
- Variant B (SDUI): there are no stable `hirer-*` classes. Find the heading text ("Meet the hiring team", FR (?)), then read `a[href*="/in/"]` inside that section only. Ignore "alumni" and "people you may know" cards (AfterApply's live check found unrelated alumni links on the page).
- Guest fallback (no login): `.message-the-recruiter` (§3.2).
- Output: `recruiters(job_id, name, headline, profile_url, source, seen_at)`. jobbot drafts the message and the human sends it. **No automated DMs or invites.**

### 6.9 Tracking (SQLite sketch)

```sql
CREATE TABLE IF NOT EXISTS li_jobs (
  job_id TEXT PRIMARY KEY, title TEXT, company TEXT, company_id TEXT, location TEXT,
  posted_date TEXT, apply_mode TEXT CHECK (apply_mode IN ('easy_apply','external','unknown')),
  category TEXT, applicants TEXT, closed INTEGER DEFAULT 0, description_html TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS li_applications (
  job_id TEXT PRIMARY KEY REFERENCES li_jobs(job_id),
  status TEXT CHECK (status IN ('queued','needs_input','held_for_review','sent','unconfirmed','external_todo','skipped','failed')),
  cv_file TEXT, letter_file TEXT, answers_json TEXT, screenshot TEXT, reason TEXT,
  sent_at TEXT, followup_due TEXT);
```
- Set `followup_due = sent_at + 7 days`. Follow-up drafts go through the email module or a manual LinkedIn message.
- Re-check `closed` through the guest detail before any follow-up.

---

## 7. Claims table

| # | Claim | Tag | Source / evidence |
|---|---|---|---|
| 1 | Guest search `…/jobs-guest/jobs/api/seeMoreJobPostings/search` returns 200 HTML with up to 10 `<li>` cards (2026-09-23) | LIVE-VERIFIED | curl/httpx probes, many queries |
| 2 | End of results is 200 with a 26-byte body; `start=1000` or `2000` is HTTP 400 | LIVE-VERIFIED | `start=70` on a 70-result query; `start=990/1000/2000` |
| 3 | Guest honours `keywords`, `location`, `geoId`, `distance`, `f_TPR` (incl. `r3600`), `f_AL`, `f_C`, `start` | LIVE-VERIFIED | ID-set comparisons (overlap 3/20 for f_AL, 1/20 for f_TPR) |
| 4 | Guest **ignores** `f_JT`, `f_E`, `f_WT`, `sortBy`, `f_PP` | LIVE-VERIFIED | Identical 20-ID sets with geoId and with location |
| 5 | The public `/jobs/search` page ignores f_JT/f_E/f_WT (count unchanged: 70) but applies f_AL (9), f_EA (4), f_TPR (4 / 12) | LIVE-VERIFIED | `.results-context-header__job-count` |
| 6 | The FR pill labels are f_AL "Candidature simplifiée" and f_EA "Moins de 10 candidatures"; distance labels are Lieu exact / 8 / 16 / 40 / 80 km for 0/5/10/25/50 | LIVE-VERIFIED | Public search page HTML |
| 7 | GeoIds: Metz 103167155, Nancy 106742268, Thionville 103333173, Moselle 105424345, Grand Est 101735443, France 105015875, Luxembourg 104042105 | LIVE-VERIFIED | `jobs-guest/api/typeaheadHits` JSON |
| 8 | Company typeahead works (Thales=1951) and `f_C=1951` restricts results | LIVE-VERIFIED | Probe |
| 9 | Guest detail `jobPosting/{id}` returns 200 HTML, 404 for unknown ids, and a closed-job marker "Les candidatures ne sont plus acceptées" | LIVE-VERIFIED | ids 4469506752, 1, 3577461385 |
| 10 | Easy Apply vs external is visible logged-out through `public_jobs_apply-link-onsite` / `…offsite…` | LIVE-VERIFIED | Easy Apply id 4410135372 vs external 4450139736 |
| 11 | The external apply URL is not in the logged-out HTML (no `code#applyUrl`) | LIVE-VERIFIED | grep of detail pages |
| 12 | The `.message-the-recruiter` block (poster name, headline, `/in/` URL) appears on some guest details (6/14) | LIVE-VERIFIED | Scan of 14 detail pages |
| 13 | The detail page leaks the numeric company id through `facetCurrentCompany=` | LIVE-VERIFIED | 4410135372 → 1245550 |
| 14 | 11 of 16 sampled alternance postings are tagged "Temps plein"; FR labels are "Stage / Alternance" and "Stagiaire / Alternant" | LIVE-VERIFIED | Criteria survey, Grand Est, `f_TPR=r2592000` |
| 15 | The public `/jobs/view/{id}/` has no JSON-LD in 2026 | LIVE-VERIFIED | 0 `application/ld+json` scripts |
| 16 | About 10 rapid requests give 429 with a JS authwall body, and access comes back after ~6.5 s; 2–3 s spacing avoided 429 | LIVE-VERIFIED | Burst test (30 requests) and recovery loop |
| 17 | `Accept-Language` switches card text and location format FR/EN | LIVE-VERIFIED | Same query with and without the header |
| 18 | LinkedIn web is served through Cloudflare (Server: cloudflare, CF-RAY …-CDG, `__cf_bm`) | LIVE-VERIFIED | Response headers |
| 19 | Easy Apply share: Metz+80 km "développeur" 26/193; 5 new per week; France "alternance développeur" 884 of 2000+ | LIVE-VERIFIED | Public page counts |
| 20 | robots.txt: `User-agent: * Disallow: /`; `/jobs-guest/` disallowed for named bots | LIVE-VERIFIED | https://www.linkedin.com/robots.txt |
| 21 | User Agreement effective 2025-11-03, §8.2 anti-scraping and anti-bot clauses (EN + FR) | LIVE-VERIFIED | https://www.linkedin.com/legal/user-agreement |
| 22 | Easy Apply daily limit (UTC reset), speed limit and lowered limit for inauthentic activity; no number published | LIVE-VERIFIED | https://www.linkedin.com/help/linkedin/answer/a8068422 |
| 23 | "Prohibited software" and "Automated tools violations" lead to temporary or permanent restriction | LIVE-VERIFIED | help a1341387, a1340522 |
| 24 | FR flow wording: "Candidature simplifiée" → "Vérifier" → "Envoyer la candidature"; applications cannot be edited or withdrawn | LIVE-VERIFIED | help a512388 (fr-FR) |
| 25 | `/my-items/saved-jobs/?cardType=APPLIED` redirects to login with `session_redirect=/jobs-tracker?cardType=APPLIED` | LIVE-VERIFIED | 307 probe |
| 26 | `/jobs/search-results/` exists (login-gated); `/jobs/view/{id}/apply/?openSDUIApplyFlow=true` gives 404 logged-out | LIVE-VERIFIED | Probes |
| 27 | Voyager API: 403 "CSRF check failed" without a token, 401 with a fake token | LIVE-VERIFIED | `/voyager/api/me` probes |
| 28 | Repo activity: GodsScion pushed 2026-09-11; wodsuz 2026-05-18; NathanDuma 2023-06-11; madingess 2025-03-12; cv_sender 2026-09-22 | LIVE-VERIFIED | GitHub REST API |
| 29 | The AIHawk repos now redirect to `feder-cr/invisible_playwright_mcp` (the LinkedIn applier is gone upstream) | LIVE-VERIFIED | GitHub API 301 → new full_name |
| 30 | Logged-in card: `li[data-occludable-job-id]`, `.artdeco-entity-lockup__title a[aria-label]`, randomized class names, footer "Easy Apply"/"Applied" | DOC-SOURCED | https://github.com/GodsScion/Auto_job_applier_linkedIn/blob/main/tests/test_selectors_groundtruth.py (fixtures 2026-09-09) |
| 31 | Apply button `button#jobs-apply-button-id.jobs-apply-button[aria-label="Easy Apply to … at …"][data-live-test-job-apply-button]`; the same id is used for external Apply | DOC-SOURCED | GodsScion fixture `04_apply_button.html`, runAiBot.py L976–1037 |
| 32 | Modal A: `div[data-test-modal][role=dialog].jobs-easy-apply-modal`, `[data-test-form-element]`, `.fb-dash-form-element`, `button[aria-label="Submit application"][data-live-test-easy-apply-submit-button][type=button]`, `#follow-company-checkbox`, `input[name=file]`, 5 MB PDF/DOC/DOCX | DOC-SOURCED | GodsScion fixture `06_easy_apply_modal.html` |
| 33 | Next/Review aria-labels "Continue to next step" / "Review your application" | DOC-SOURCED | GodsScion runAiBot.py L1021–1023; wodsuz linkedin.py L222, L482 |
| 34 | Variant B (SDUI): an `<a …/apply/?openSDUIApplyFlow=true>` opener, shadow root, no `role=dialog`, ids containing `easyApplyFormElement` | DOC-SOURCED | https://github.com/YonatanVol/cv_sender/pull/12 (2026-09-14); `cvsender/channels/linkedin.py` comments dated 2026-09-22 |
| 35 | Discard confirm `button[data-control-name="discard_application_confirm_btn"]`; close `button[data-test-modal-close-btn]` | DOC-SOURCED | GodsScion runAiBot.py L1119–1145 |
| 36 | Daily-limit text "exceeded the daily application limit" in `.artdeco-inline-feedback__message` (EN) | DOC-SOURCED | GodsScion runAiBot.py L987 |
| 37 | Hiring team: `.hirer-card__hirer-information`, `hirer-card__job-poster`; SDUI has no stable hirer classes | DOC-SOURCED | GodsScion runAiBot.py L1240; https://github.com/selcukgural/AfterApply/pull/5 |
| 38 | Logged-in URL filter codes f_E 1–6, f_JT F/P/C/T/V/I/O, f_WT 1–3, f_SB2 1–9, sortBy DD/R | DOC-SOURCED | https://github.com/wodsuz/EasyApplyJobsBot/blob/main/utils.py |
| 39 | Playwright: XPath does not pierce shadow roots; CSS does; closed shadow roots unsupported | DOC-SOURCED | https://playwright.dev/python/docs/locators#locate-in-shadow-dom |
| 40 | Chrome 136+: the default user data dir cannot be automated, so a separate dir is needed; `channel="chrome"` option | DOC-SOURCED | https://playwright.dev/python/docs/codegen (Use existing userDataDir), https://playwright.dev/python/docs/api/class-browsertype |
| 41 | Easy Apply ~50 applications per 24 h | DOC-SOURCED (community, not official) | https://www.linkedin.com/posts/rachelbicknell_linkedins-easy-apply-now-has-a-limit-this-activity-7358190082186518528-Agt7 |
| 42 | ~100 invitations per week | DOC-SOURCED | https://www.linkedin.com/help/linkedin/answer/a550555 plus community posts |
| 43 | CNIL fined KASPR €240 000 on 2024-12-19 for scraping LinkedIn contact details | DOC-SOURCED | https://www.cnil.fr/fr/aspiration-de-donnees-sanction-de-240-000-euros-lencontre-de-la-societe-kaspr |
| 44 | LinkedIn v. Proxycurl (3:25-cv-00828, filed 2025-01-24); Proxycurl shut down on 2025-07-04 | DOC-SOURCED | https://www.socialmediatoday.com/news/linkedin-wins-legal-case-data-scrapers-proxycurl/756101/ |
| 45 | FR aria-labels "Passer à l’étape suivante" / "Vérifiez votre candidature" and FR discard or dismiss texts ("Supprimer", "Ignorer") | UNCERTAIN | Not observable without login; use language-independent hooks first |
| 46 | `data-easy-apply-next-button`, `data-live-test-easy-apply-next-button` / `…review-button` still exist | UNCERTAIN | Only the submit data attribute appears in the 2026-09-09 fixture |
| 47 | Whether `/jobs/search/` redirects to `/jobs/search-results/` (AI search) for some accounts, and whether the URL f_* params still apply there | UNCERTAIN | cv_sender used `/jobs/search/?f_AL=true&f_E=1,2&sortBy=DD` successfully in 2026-09 |
| 48 | The exact Easy Apply speed-limit threshold, and the FR limit-reached message text | UNCERTAIN | Not published |
| 49 | Whether the default `python-httpx` UA is blocked on the guest endpoints | UNCERTAIN | The one test fell inside a 429 window |
| 50 | The GDPR household exemption covers a private individual's job-search CRM | UNCERTAIN | Legal interpretation; minimize data anyway |
| 51 | Free (non-Premium) members can message the job poster from the job page | UNCERTAIN | The guest page shows a "message the recruiter" CTA to login only |
| 52 | FR seniority labels for Associate, Director and Executive (only "Stagiaire / Alternant", "Premier emploi", "Cadre", "Non pertinent" were observed) | UNCERTAIN | Not observed |
