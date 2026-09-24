# HelloWork (hellowork.com) — research for jobbot

> Researched live on **2026-09-23** (curl 8.17 + httpx 0.28.1 + Playwright 1.58 headless Chromium, from a French residential IP, no account).
> Site build observed: static assets `https://f.hellowork.com/hellowork/ressources/2.54.2/...`, Stimulus + Hotwire Turbo front-end, ASP.NET Core back-end (`CfDJ8…` Data-Protection tokens), fronted by **Microsoft Azure Application Gateway v2** (WAF).
> Nothing was submitted: no form POSTs, no account, no email sent. The only calls were read-only GETs (plus one anonymous read-only `POST /fr-fr/compte/accountdata`, which is what the page itself does on load).

---

## 1. TL;DR + recommendation for jobbot

**What HelloWork is technically**
- Pages are rendered on the server (HTML), with **no JSON search API** and **no `__NEXT_DATA__`**. Search results come only as HTML cards (30 per page). Each job detail page embeds a complete **JSON-LD `JobPosting`** plus a `window.dataLayer.push({...})` analytics object. Together these give you everything you need: full HTML description, skills, salary, dates, apply mode.
- Search URL: `GET https://www.hellowork.com/fr-fr/emploi/recherche.html?k=<kw>&l=<loc>&c=Alternance&c=CDI&ray=50&st=date&p=1`. Every parameter is documented below and was checked live.
- **Apply modes** (read from `product_response_type` / dataLayer `response_type` / JSON-LD `directApply`):
  - `ATS`, `Smartapply` / `Smartapply_v2` → **in-site application** ("Postuler"). You apply without leaving HelloWork (first name, last name, email, CV, optional message of 2,900 characters or less). Recruiters with the "Suivi de candidature" badge share status updates.
  - `UrlWithForm`, `Url` → **external**: the button reads "Postuler sur le site du partenaire" (or "…du recruteur"). HelloWork collects your identity and CV first, then redirects you to the employer's or partner's ATS. The status becomes "À finaliser".
- **An account is required** in practice. Anonymous applications must tick "J'accepte de créer un compte…" and then enter a **6-digit security code sent by email (OTP)**. Logged-in applications come pre-filled and pre-select the default CV, so they work as a one-click "candidature simplifiée". HelloWork itself never uses that phrase.
- **Anti-bot measures are light.** There is no Cloudflare, DataDome or Akamai. The Azure WAF returns 403 for the user-agent strings `python-requests/*` and `Python-urllib/*`, while browser UAs, curl, httpx and even an empty UA get 200. A burst of 12 requests was not rate-limited, and headless Chromium renders everything. Login uses **Friendly Captcha** plus Google/LinkedIn OAuth.
- **Legal and ToS risk is high on paper.** The CGU (art. 1 and 8.2) state that automated systems are "utilisateurs non légitimes" and that scraping is "strictement interdit… commerciales ou non". The same text sits in an HTML comment at the top of every page. robots.txt has `Disallow: /*?`, which covers every search URL. CGU art. 6.2 says "un nombre très significatif d'envois de candidatures" can bring a **6-month or permanent suspension**.

**Recommendation (what jobbot should do)**
1. **Discovery:** do not crawl `recherche.html` at scale.
   - Preferred channel: have the candidate create **HelloWork email alerts** once, by hand, then parse the alert emails in their own inbox with IMAP and extract offer IDs with the regex `/emplois/(\d+)\.html`.
   - Complement with the official France Travail API (covered in another research file).
   - If you do query HelloWork search directly, keep it tiny: a handful of saved queries, 1–2 runs a day, page 1 only, and at least 5 s between requests. Know that this goes against robots.txt and CGU 8.2.
2. **Enrichment:** fetch individual offer pages `/fr-fr/emplois/{id}.html`. These are allowed by robots.txt and carry `meta robots=index,follow`. Parse the JSON-LD `JobPosting` for ATS keyword scoring and CV/letter tailoring. Cache in SQLite and never re-fetch an ID within 24 h. HTTP 410 means the offer expired.
3. **Apply:** use **Playwright sync with the candidate's persistent, manually logged-in profile**. Only handle in-site modes (`ATS`/`Smartapply*`) automatically:
   - open the offer, click `a[data-cy=applyButtonHeader]`, wait for `#offer-detail-main-step-form`;
   - optionally upload the tailored CV into `input[data-cy=cv-uploader-input]`;
   - fill `textarea[name=MotivationLetter]`;
   - take a **screenshot and require the human to confirm**, then click `button[data-cy=submitButton]`.
   - Cap volume (10/day or fewer, 20–60 s apart, only offers above the score threshold).
   - Never automate login, the captcha or the OTP.
   - For `Url`/`UrlWithForm` offers, hand off to the user and record them as "À finaliser".
4. **Tracking:** read the candidate's own `/fr-fr/candidat/mes-candidatures.html` (login required) with Playwright. Official statuses are: En cours d'envoi, Envoyée, Consultée, Entretien, Refusée, Acceptée, Erreur, À finaliser. There is a PDF export of the last 3 months (500 applications at most, 10 downloads a day), which is useful as proof for France Travail. You cannot apply twice to the same offer, and you cannot edit or withdraw an application.
5. **Follow-ups ("relances"):** HelloWork does **not** give out recruiter contact details and does not send follow-ups for you (help centre). Company pages contain no emails. For follow-ups and spontaneous applications, resolve the company name (JSON-LD `hiringOrganization.name` / `sameAs`) through other sources (SIRENE / company website).

**Market reality check (live, 2026-09-23):** `l=Metz&ray=50&c=Alternance&f=Informatique_dev` returns only **4 offers**. `k=développeur&l=Metz&ray=50&c=Alternance` returns 112, but most are "business developer" / "développeur commercial". Relevant live IDs: 79006307 (Chausséa, Alternant Développeur Web Fullstack, Metz, ATS), 83292557 and 83292364 (MEWO campus, Batilly / Thionville, ATS), 81303268 (Chausséa, Coordinateur projet digital, Url). Client-side relevance filtering is mandatory.

---

## 2. Endpoints / URLs & auth

Base: `https://www.hellowork.com`. All paths are prefixed `/fr-fr/`. HTTP/1.1 and TLS work with plain httpx. Send a browser UA and `Accept-Language: fr-FR`.

### 2.1 Search results (HTML)
`GET /fr-fr/emploi/recherche.html` (robots: **Disallowed** twice, by `Disallow: /*?` and `Disallow: /fr-fr/emploi/recherche.html`)

| Param | Meaning | Allowed values (live from form) | Notes |
|---|---|---|---|
| `k` | keywords ("Quoi") | free text | Matching is broad: `développeur` also matches "Business Developer" and "Développeur commercial" |
| `k_autocomplete` | job-taxonomy URI (optional) | e.g. `http://www.rj.com/Commun/Post/Developpeur_fullstack`, `http://www.rj.com/Commun/Post/Developpeur`, `http://www.rj.com/Commun/Post/Developpeur_web` | from job autocomplete (§2.3) |
| `l` | location text ("Où") | free text: `Metz`, `Metz 57000`, `Moselle`, `Nancy`, `Luxembourg`… | Works **without** `l_autocomplete` (server resolves it) |
| `l_autocomplete` | location URI (optional, more precise) | `http://www.rj.com/commun/localite/commune/57463` (Metz), `/commune/54395` (Nancy), `/commune/57672` (Thionville), `/departement/57` (Moselle), `/region/44` (Grand Est), `/pays/LU` (Luxembourg country), `/commune/57999` ("Luxembourg" 2nd suggestion) | from location autocomplete (§2.3) |
| `c` | contract type, **repeatable** (`c=Alternance&c=CDI`) | `CDI`, `CDD`, `Travail_temp` (Intérim), `Stage`, `Alternance`, `Independant`, `Franchise`, `Associe`, `Fonctionnaire`, `Freelance`, `Stage_de_lycee` | |
| `ray` | radius km | UI slider: `0,5,10,20,50` (`data-range-available-values-value="[0,5,10,20,50]"`), **default 20** | server also accepted `ray=100` (139 results vs 112 at 50) |
| `st` | sort | `relevance` (default), `date` | see gotcha §5: sorting is applied inside two blocks |
| `p` | page, **1-based** | `1..n` | 30 cards/page; past last page → HTTP 200 with 0 cards |
| `d` | publication date | `all`, `h` (24 h), `d` (3 days), `w` (1 week), `m` (1 month) | |
| `cod` | contract duration ("Durée de contrat") | `all`, `1m`, `1-3m`, `3-6m`, `6-12m`, `1-2y`, `2y` | 24-month alternance → probably `1-2y`/`2y` (boundary UNCERTAIN) |
| `e` | experience, repeatable | `deb` (Débutants acceptés), `0-1y`, `2-3y`, `4-5y`, `6-10y`, `gt10y`, `unknown` | |
| `t` | remote, repeatable | `Complet`, `Partiel`, `Occasionnel`, `Pas_teletravail` | `k=développeur&c=Alternance&t=Complet` (no `l`) → 2 offers nationwide |
| `et` | company type, repeatable | `Cabinet_recr`, `Centre_formation`, `SSII` (ESN), `Entreprises`, `Travail_temporaire` | `et=Centre_formation` is useful to exclude school-posted "fake" alternance ads |
| `ctt` | working time, repeatable | `ft`, `pt`, `np` | |
| `f` | function, repeatable | e.g. `Informatique_dev`, `Informatique_dev_hard`, `Informatique_syst_info`, `Informatique_syst_reseaux`, `Ingenierie_telecom`, … (33 values, full list in §3.1) | `f=Informatique_dev` is the best precision filter |
| `s` | sector, repeatable | e.g. `Inform_SSII`, `Media_internet_com`, `Ind_hightech_telecom`, … | |
| `msa` | minimum salary (range input) | integer, default `0` | |
| `so` | only offers with salary | `true` | |
| `sr` | only "Super recruteur" | `true` | |
| `rse` | CSR labels | `_10556393` (B Corp), `_9ffe5c83` (EcoVadis), … | |

Example (live 200, "112 offres"):
`https://www.hellowork.com/fr-fr/emploi/recherche.html?k=d%C3%A9veloppeur&l=Metz&c=Alternance&ray=50&st=date&p=1`

Sending `Turbo-Frame: turboSerp` does **not** shrink the response: the full page (~117 KB) comes back either way.

### 2.2 Job detail (HTML + JSON-LD)
`GET /fr-fr/emplois/{offerId}.html`. Allowed by robots, with `<meta name="robots" content="index, follow">` and `<link rel="canonical" href="https://www.hellowork.com/fr-fr/emplois/{id}.html">`.
- `offerId`: 8-digit integer, e.g. `79006307`.
- Expired offer → **HTTP 410** ("Erreur 410 - La page n'existe plus", or the old title with a 410 status).
- Lazy Turbo frames on the page:
  - `GET /fr-fr/offres/getinitialformframeview?offerId={id}`: apply funnel step 1 (robots-disallowed).
  - `GET /fr-fr/offres/getoffercandidateskillsframeview?offerId={id}`: skills chips.
  - `GET /fr-fr/getcompanypageofferframeview/{companyId}/{offerId}`: company block.

### 2.3 Autocomplete (Turbo-frame HTML fragments)
The Stimulus `autocomplete` controller appends `term=<text>` to the URL. Send the header `Turbo-Frame: <id>-frame`. Without `term` the endpoint returns 400.
- Location: `GET /fr-fr/search/getsearchenginelocationsuggestionsframeview?displayMissingResultMessage=False&term=Moselle`, header `Turbo-Frame: Locality-frame`. Returns `<button data-autocomplete-id-param="http://www.rj.com/commun/localite/departement/57" data-autocomplete-label-param="Moselle">`.
  - Alternative: `/fr-fr/search/getlocationsuggestionsframeview?displayMissingResultMessage=True`.
- Job: `GET /fr-fr/searchoffers/getsearchenginegroupedjobsuggestionsframeview?displayMissingResultMessage=False&term=développeur`, header `Turbo-Frame: Job-frame`.
  - Alternative: `/fr-fr/searchoffers/getjobsuggestionsframeview?...`.

`/fr-fr/search/` is robots-disallowed. Hardcode the few URIs you need (table in §2.1) instead of calling these endpoints.

### 2.4 Apply funnel (read-only inspection; POSTs were not called)
| Step | Method + URL | Fields |
|---|---|---|
| Load step-1 form (Turbo frame `offer-detail-step-frame`) | `GET /fr-fr/offres/getinitialformframeview?offerId={id}` | Sets cookie `hw.emploi.web.session` |
| Step-1 form | `POST /fr-fr/offres/postcandidateinformationfromstepframeview` (form `#offer-detail-main-step-form`, `novalidate`) | `FunnelId` (hidden, ASP.NET protected token `CfDJ8…`, session-bound), `Firstname` (required, pattern `^(?=.*\S)[a-zA-ZÀ-ÿ\s'\-]{1,50}$`), `LastName` (same), `Email` (required), `JweHashResume` (required; filled by the CV upload), `MotivationLetter` (textarea, `maxlength=2900`; only for `ATS`/`Smartapply*`), `HasAcceptedCGU=true` (checkbox, required when not logged in) |
| CV upload (Turbo frame `funnel-resume-uploader-frame`, loaded from `GET /fr-fr/GetUploaderCvFrameView?formId=offer-detail-main-step-form&isRequired=true&turboFrameId=funnel-resume-uploader-frame`) | `POST /fr-fr/uploadcv?formId=offer-detail-main-step-form&isRequired=True`, `multipart/form-data`, auto-submitted on file change | `upload` (file; accept `.rtf,.dot,.doc,.docx,.pdf,.odt,.wpd,.ods,.gif,.png,.jpg,.jpeg,.bmp`; `max-size=2097152` = 2 MiB), `uploaderApp=41`, `uploaderType=RO`. The response re-renders the frame and fills `JweHashResume` |
| Skip (only `Url` type) | `POST /fr-fr/offres/postskipinformationfromstepframeview` | `FunnelId`. Button "Passer cette étape" (presumably redirects to the partner site; UNCERTAIN) |
| Next steps | (not observed) | Anonymous users get the OTP step "Est-ce bien votre email ?" (6-digit code by email). Either user may get recruiter extra questions (salary expectations, phone, pre-qualification select). Final "Postuler". Confirmation email. |

Do **not** replicate these POSTs with raw httpx. `FunnelId` is tied to the session, the anonymous flow needs an OTP, and the logged-in flow needs the auth cookies. Drive it with Playwright.

### 2.5 Account / auth
- Login page: `GET /fr-fr/candidat/connexion-inscription.html#connexion` (with `?returnUrl=/fr-fr/emplois/{id}.html`). It sets cookies `hw.antiforgery` and `hw.emploi.web.session`.
  - The login JS posts to `/fr-fr/compte/loginprofil`, from config in the `data-sign-in` JSON attribute.
  - Fields: `email2`, password, and `remember` ("Rester connecté·e").
  - Captcha: **Friendly Captcha** (`<div class="frc-captcha">`, `"captchaSiteKey":"FCMVVIRROC6FLDRK","captchaIsNeeded":"true"`; the JS loads `WidgetInstance` with `solutionFieldName`).
  - OAuth: Google (`https://accounts.google.com/o/oauth2/v2/auth?redirect_uri=https://www.hellowork.com/fr-fr/candidat/googlecallback&…`) and LinkedIn (`…/candidat/linkedincallback`). Facebook login is no longer supported.
  - Google One Tap: `/fr-fr/candidat/onetapturbo`, `/fr-fr/candidat/onetapturbocustom`.
- **Auth probe** (verified live, anonymous):
  `POST /fr-fr/compte/accountdata`, headers `Accept: application/json`, `Content-Type: application/json`, body `{"shouldFetchBookmarks":false}`
  → 200 `{"DisplayName":null,"FirstName":null,"LastName":null,"Initials":null,"Email":null,"Id":0,"IsAuthenticated":false,"Bookmarks":[],"HasServiceAdaptation":false}`.
  A plain GET returns 404 and a POST without a JSON body returns 415. In the DOM, the logged-in state adds the class `isConnected` to `<body>` (from the JS source).
- Candidate area (all need login; anonymous → **302** to `/fr-fr/candidat/connexion-inscription.html?ReturnUrl=…#connexion`):
  - `/fr-fr/candidat/mes-candidatures.html` (applications and statuses)
  - `/fr-fr/candidat/mon-espace.html`
  - `/fr-fr/candidat/mon-profil.html`
  - `/fr-fr/candidat/mes-alertes.html`, `/fr-fr/candidat/creation-alerte.html`
  - `/fr-fr/candidat/mes-cv-vus.html`
  - `/fr-fr/candidat/parametres.html`
  - `/fr-fr/candidat/logoff`
- Bookmark (logged in): `GET /fr-fr/candidat/tooglebookmark?idOffre={id}&turboFrameId=bookmark-frame-card{id}&isFromToolbar=False` (sic, "toogle").
- Alert creation from the SERP: `POST /fr-fr/alerte/create` (Turbo frame `otp-input`) with `Job.DisplayLabel`, `Job.JobCriterionUri`, `Location.Uri`, `OtpData.Email`, `Contracts` (repeatable). An **email OTP** is required.

### 2.6 SEO landing pages (robots-allowed, no query string)
- `/fr-fr/alternance/metier_developpeur-informatique-ville_metz-57000.html` (200, 1 card)
- `/fr-fr/alternance/metier_developpeur-web-ville_metz-57000.html` (1 card)
- `/fr-fr/alternance/domaine_informatique-ville_metz-57000.html` (5 cards)
- `/fr-fr/emploi/metier_developpeur-informatique-ville_metz-57000.html` (20 cards)

These are exact city only (no radius) and a guessed slug returned 410. Pagination needs `?p=`, which robots also disallows. They are a very limited option that stays inside robots.txt. The card markup is identical to search.

### 2.7 Not available
- There is no public candidate API. `ats-partner.hellowork.com` is for ATS vendors and recruiters (receiving applications, pushing statuses) and is not usable by a candidate.
- The sitemap `https://www.hellowork.com/fr-fr/horizon.xml` returns **403** to a browser UA.

---

## 3. Response / page shape

### 3.1 Search page
- Total count: `<h1 class="typo-l-bold sm:typo-xl-bold"> 112 offres </h1>` (also the button `[data-cy=offerNumberButton]` "Afficher 112 offres"). With no results: `0 offre`.
- JSON-LD on the SERP: only `WebSite`, `Organization` and `BreadcrumbList`. **No `JobPosting`, no `ItemList`, no `__NEXT_DATA__`.**
- Card container: `li[data-id-storage-item-id]`, 30 per page. Card anatomy (trimmed, real, id 82502103):

```html
<li data-id-storage-target="item" data-hide-offer-target="item"
    data-id-storage-item-id="82502103" data-hide-offer-item-id-value="82502103">
  ...
  <form action="/fr-fr/searchoffers/hideoffermodalframeview" method="GET" data-turbo-frame="exclude-criteria-frame">
    <input type="hidden" name="offerId" value="82502103">
    <input type="hidden" name="title" value="Alternance - Business Developer H/F">
    <input type="hidden" name="company" value="Mbway Strasbourg">
  </form>
  <turbo-frame id="bookmark-frame-card82502103"> ... </turbo-frame>
  <img src="https://f.hellowork.com/img/entreprises/207061.png" alt="Mbway Strasbourg recrutement">
  <div data-controller="intersect" data-cy="serpCard"
       data-analytics-values-param="{&quot;event&quot;:&quot;generic_product&quot;,&quot;event_name&quot;:&quot;product.display&quot;,
         &quot;product_data&quot;:[{&quot;product_id&quot;:&quot;82502103&quot;,&quot;product_response_type&quot;:&quot;ATS&quot;,
         &quot;product_list&quot;:&quot;LO-Moteur-Exact&quot;,&quot;product_position&quot;:1,&quot;product_category&quot;:&quot;1&quot;,
         &quot;product_status&quot;:&quot;Suivi de candidature&quot;}]}">
    <span class="badge-m badge-purple ..." data-cy="followUpTag"
          title="Ce recruteur partage l’avancement des candidatures.">Suivi de candidature</span>
    <a href="/fr-fr/emplois/82502103.html" data-cy="offerTitle" data-turbo="false"
       title="Alternance - Business Developer H/F - Mbway Strasbourg"
       aria-label="Voir offre de Alternance - Business Developer H/F à Metz - 57, chez Mbway Strasbourg, pour un Alternance, avec un salaire de 800 - 1 867,02 € / mois, en temps plein, 2 ans">
      <h3 class="inline">
        <p class="typo-l sm:small-group:typo-l sm:typo-xl">Alternance - Business Developer H/F</p>
        <p class="typo-s inline">Mbway Strasbourg</p>
      </h3>
    </a>
    <div class="readonly tag-secondary-s w-fit border-0" data-cy="localisationCard">Metz - 57</div>
    <div class="readonly tag-secondary-s w-fit border-0" data-cy="contractCard">Alternance</div>
    <div class="readonly tag-secondary-s typo-s-bold w-fit border-0">800 - 1 867,02 € / mois</div>   <!-- salary: no data-cy -->
    <div class="readonly tag-secondary-s border-0 w-fit" data-cy="contractTag">2 ans</div>           <!-- duration / remote tags -->
    <span class="btn-outline-m ...">Voir l’offre</span>
    <div class="typo-s text-grey-500 pl-1 pt-1">il y a 3 jours</div>                              <!-- relative date -->
  </div>
</li>
```

| Field | Selector (inside `li[data-id-storage-item-id]`) | Notes |
|---|---|---|
| offer id | attr `data-id-storage-item-id` | also in `href` |
| link | `a[data-cy=offerTitle]` → `href` = `/fr-fr/emplois/{id}.html` | |
| title | `a[data-cy=offerTitle] p` (1st) | with lxml, **don't** use `h3 p`, because lxml moves `<p>` out of `<h3>`. `a[...] p` works with both lxml and html.parser (verified) |
| company | `a[data-cy=offerTitle] p` (2nd) | |
| location | `[data-cy=localisationCard]` | "Metz - 57", "Saint-Julien-lès-Metz - 57" |
| contract | `[data-cy=contractCard]` | |
| salary | `div.tag-secondary-s.typo-s-bold` | optional |
| extra tags | `[data-cy=contractTag]`, other `div.tag-secondary-s` | "2 ans", "24 mois", "Télétravail occasionnel", "+ 1" |
| relative date | `div.typo-s.text-grey-500` | seen: "il y a 11 heures", "il y a 1 jour", "il y a 29 jours" |
| apply mode | JSON in `[data-cy=serpCard]@data-analytics-values-param` → `product_data[0].product_response_type` | `ATS`, `Smartapply`, `UrlWithForm`, `Url` (counts across all fetched pages: 483 / 22 / 71 / 2) |
| match block | `product_list` | `LO-Moteur-Exact` (exact match), `LO-Moteur-Approchant` (broadened match), and on SEO pages `LO-Metier-Ville`, `LO-Alternance-Metier-Ville`, `LO-Alternance-Domaine-Ville` |
| follow-up badge | `[data-cy=followUpTag]` / `product_status` | `Suivi de candidature` vs `NotDefined` |
| super recruiter | `[data-cy=superRecruiterBadge]` | optional |

The full `f` (Fonctions) value list from the live form is: `Achat, Logistique_metiers_transport, Assistanat_admin_accueil, Compta_gestion_finance_audit, Dir_resp_centre_profit, Juridique_droit, Metiers_fonction_publique, RH_Personnel_Formation, BTP_gros_second_oeuvre, Bureau_etude_R_D, Commercial_technico_com, Commercial_particulier, Commercial_professionnel, Commercial_vendeur, Import_export_inter, Dir_management_resp, Negociation_gest_immo, Informatique_dev_hard, Informatique_dev, Informatique_syst_info, Informatique_syst_reseaux, Ingenierie_agro_agri, Ingenierie_chimie_pharma_bio, Ingenierie_electro_tech, Ingenierie_meca_aero, Ingenierie_telecom, Marketing_com_graphisme, Production_gestion, Production_operateur, Qualite_securite_environnement, Restauration_hotellerie_tourisme, Sante_social, Services_pers_entreprises, SAV_Hotline`.

Pagination buttons: `button[name=p][form=searchForm][data-turbo-frame=turboSerp]` with `value` = page number. Disabled prev/next have `aria-disabled="true"` and `value="0"`.

### 3.2 Job detail page
**JSON-LD `JobPosting`** (4th `application/ld+json` block, after WebSite, Organization and BreadcrumbList). Real sample for id 79006307, trimmed:

```json
{
 "@context": "https://schema.org", "@type": "JobPosting",
 "title": "Alternant - Developpeur Web Fullstack H/F",
 "description": "<h2>Les missions du poste</h2><p>Tu cherches chaussure à ton pied ? ...</p><h2>Le profil recherché</h2>...<h2>Bienvenue chez Chausséa</h2>...",
 "identifier": {"@type": "PropertyValue", "name": "Chausséa", "value": "8wvzi9yi36"},
 "url": "https://www.hellowork.com/fr-fr/emplois/79006307.html",
 "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {"@type": "QuantitativeValue"}},
 "datePosted": "2026-09-14T00:05:40Z",
 "directApply": true,
 "educationRequirements": {"@type": "EducationalOccupationalCredential", "credentialCategory": "postgraduate degree"},
 "employmentType": ["INTERN", "FULL_TIME"],
 "experienceRequirements": "no requirements",
 "hiringOrganization": {"@type": "Organization", "name": "Chausséa",
    "sameAs": "https://www.hellowork.com/fr-fr/entreprises/chaussea-18417.html",
    "logo": "https://f.hellowork.com/img/entreprises/160_160/194225.png"},
 "industry": ["Distribution", "Commerce de gros"],
 "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "FR",
    "addressLocality": "Metz", "addressRegion": "Grand Est", "postalCode": "57000"}},
 "occupationalCategory": "Informatique",
 "qualifications": "Qui es-tu ?<br /><br />Tu es en recherche d'une alternance (Master) en développement informatique pour la rentrée 2026 ... Vue.js, Node.js, POO, SQL, GIT, API REST...",
 "salaryCurrency": "EUR",
 "skills": ["Force de proposition","SQL","Assistance technique","Analyse des besoins utilisateur","Git","Veille technologique","Vue JS","Node.js","API REST","Flutter","Programmation","Esprit d'analyse"],
 "validThrough": "2026-10-14T00:05:40Z"
}
```
- `description` is **complete HTML** (h2 sections "Les missions du poste", "Le profil recherché", "Bienvenue chez …", optionally "Mais aussi..."). Use it for ATS keyword extraction.
- Salary with values: `{"maxValue": 1867.02, "minValue": 802.82, "unitText": "MONTH"}` or `"YEAR"`.
- Optional keys: `applicantLocationRequirements`, `jobLocationType: "TELECOMMUTE"`.
- `employmentType` for alternance = `["INTERN","FULL_TIME"]`. Use the dataLayer `contrat` field for the French label.
- `validThrough` = `datePosted` + 30 days on every sample.
- `directApply`: `true` for ATS/Smartapply_v2, `false` for Url/UrlWithForm (verified on 79006307, 83292557, 80294383, 79199308, 81298493).

**dataLayer** (`<script>window.dataLayer = window.dataLayer || [];window.dataLayer.push({...});</script>`), real, trimmed:
```json
{"categoryPage":"Detail","sujetPage":"Offre","typePage":"DO-Augmentee","idOffre":"79006307",
 "Metier":"developpeur-web","Domaine":"informatique","Tags":"api, as, belgique, equipe, js, veille-technologique, ...",
 "Fonction":"informatique","Region":"grand-est","Departement":"moselle","Ville":"metz-57000",
 "Nom-Entreprise":"chaussea","typeEntreprise":"entreprises","contrat":"Alternance","datePublicationOffre":"20260914",
 "idCategory":1,"ecommerce":{"currencyCode":"EUR","detail":{"products":[{"name":"none","id":"79006307","price":"1",
 "brand":"none","category":"1","response_type":"ATS","dimension77":"False"}]}},
 "page_component":["salaire-estime","competences-requises","entreprise","profil-recherché","infos-entreprise"]}
```
`typeEntreprise` values seen: `entreprises`, `centre-formation`, `""`. `response_type` values: `ATS`, `Smartapply_v2`, `UrlWithForm`, `Url`.

**HTML selectors (detail)**

| Field | Selector |
|---|---|
| title | `[data-cy=jobTitle]` (inside `h1#main-content`) |
| company (+ company page link) | `h1#main-content a` (absent for some partner offers) |
| location / contract | `h1#main-content + ul li` → `["Metz - 57","Alternance"]` |
| tags (salary estimate, duration, education, sector) | `#offer-panel ul.flex.flex-wrap.gap-3 > li`; salary button `[data-cy=salary-tag-button]` ("Estimation Hellowork → 504,10 - 1 867,02 € / mois") |
| missions | `#offer-panel section` whose `h2 span` = "Les missions du poste" → `[data-truncate-text-target=content]` |
| profile | `details:has(summary[data-cy=profileCollapseButton])` |
| company blurb | `details:has(summary[data-cy=companyCollapseButton])` |
| extra info | `details:has(summary[data-cy=informationCollapseButton])` |
| skills chips | `turbo-frame#candidate-skills-{id} li` |
| footer | `#offer-panel p.typo-xs.text-grey-500` → "Publiée le 14/09/2026 - Réf : 8wvzi9yi36" |
| apply button (header) | `a[data-cy=applyButtonHeader]` (href `#postuler`). Text "**Postuler**" = in-site; "**Postuler sur le site du partenaire**" (or "…du recruteur", per help centre) = external |
| mobile sticky apply | `a#mobile-sticky-button` (contains an `external-link.svg` icon when external) |
| apply section | `div#postuler` → lazy `turbo-frame#offer-detail-step-frame[src="/fr-fr/offres/getinitialformframeview?offerId={id}"][loading=lazy]` |
| section heading | anonymous in-site: "Créez votre compte Hellowork et envoyez votre candidature !"; logged-in in-site: "Envoyez votre candidature dès maintenant !"; external: "Finalisez votre candidature sur le site du partenaire" |

### 3.3 Apply form (step 1). Live DOM from Playwright on 79006307 (ATS) and 81298493 (Url), anonymous
```text
INPUT  FunnelId            hidden
INPUT  Firstname           text      required   #Answer_Firstname_Funnel   (placeholder "Votre prénom")
INPUT  LastName            text      required   #Answer_LastName_Funnel
INPUT  (none)              checkbox             #emailReadonly  (UI state only)
INPUT  Email               email     required   #Answer_Email_Funnel  autocomplete=email
INPUT  JweHashResume       text      required   (visually hidden; random id e.g. "89c")
INPUT  uploaderApp=41 / uploaderType=RO         (belong to the nested upload form, random id e.g. "025")
INPUT  upload              file      required   data-cy=cv-uploader-input (random id e.g. "204")
INPUT  cover-letter-collapse-funnel checkbox    (toggle; label data-cy=motivationFieldButton "Personnaliser mon message au recruteur")
TEXTAREA MotivationLetter  maxlength=2900       #Answer_MotivationLetter_Funnel   (ATS / Smartapply only)
INPUT  HasAcceptedCGU=true checkbox  required   #Answer_CGU  data-cy=cgu-box
BUTTON submit  data-cy=submitButton  "Continuer ma candidature"
(Url type only) FORM POST /fr-fr/offres/postskipinformationfromstepframeview  button "Passer cette étape"
```
Hint text: "Poids max. 2Mo — Formats .pdf, .doc, .docx, .odt, .rtf, .jpg ou .png". The motivation hint reads "Maximum 2900 caractères : au-delà, votre message sera coupé." The analytics payload on the submit button contains `"event_info":"[OTP] CTA click Continuer"` and `"app_version_array":{"one-time-password-V1.0":"Version_B"}`, meaning the anonymous flow continues to an email OTP step.

### 3.4 Location autocomplete fragment (real)
```html
<turbo-frame id="Locality-frame"><ul ... role="listbox"><li class="sm:px-2 ">
  <button type="button" data-autocomplete-id-param="http://www.rj.com/commun/localite/departement/57"
          data-autocomplete-label-param="Moselle" data-autocomplete-decoded-label-param="Moselle"
          data-action="autocomplete#selectSuggestion">
    <span class="font-bold">Moselle</span></button></li></ul></turbo-frame>
```

### 3.5 WAF block (real)
`User-Agent: python-requests/2.32.3` or `Python-urllib/3.12` →
```text
HTTP/1.1 403 Forbidden
Server: Microsoft-Azure-Application-Gateway/v2
<html><head><title>403 Forbidden</title></head><body><center><h1>403 Forbidden</h1></center><hr><center>Microsoft-Azure-Application-Gateway/v2</center></body></html>
```

---

## 4. Limits, ToS, legal (France / GDPR / CNIL)

### 4.1 robots.txt (live, `Last-Modified: Thu, 17 Sep 2026`)
- `User-Agent: *` → `Disallow: /*?`. **Every URL with a query string is disallowed**, which includes all search and pagination URLs and the apply frames. Exceptions are `Allow: /*utm_source=` and `Allow: /*xtor=`, both tracking-only.
- Also disallowed:
  - `/fr-fr/emploi/recherche.html`, `/fr-fr/candidat/`, `/fr-fr/compte/`, `/fr-fr/api/`, `/fr-fr/search/`
  - `/fr-fr/offres/postuler`, `/fr-fr/GetApplyFormFrameView`, `/fr-fr/offres/getinitialformframeview*`, `/fr-fr/emplois/candidature.html`
  - `/fr-fr/offer/redirect`, `/fr-fr/emplois/ext/`, `/fr-fr/rebond/`, `/fr-fr/rss/`, `/fr-fr/jobalerte`, `/fr-fr/unleash/`
- **Allowed:** job detail `/fr-fr/emplois/{id}.html`, SEO landings (`/fr-fr/alternance/metier_…-ville_….html`), company pages.
- A long bad-bot list gets `Disallow: /`. It includes `Python-urllib`, `httplib`, `Wget`, `HTTrack`, `AhrefsBot`, `DataForSeoBot`, `stepstoneCrawlBot`, … `python-httpx` and `Playwright` are not listed.

### 4.2 CGU HelloWork (https://www.hellowork-group.com/fr/legal/cgu-hellowork/, no version date shown)
- **Art. 1:** "Les systèmes automatisés ou un logiciel accédant au Site sont expressément considérés comme des utilisateurs non légitimes de la base de données…"
- **Art. 8.2:** "l'utilisation de systèmes automatisés ou de logiciels pour extraire des données du Site, en particulier le Contenu et la CVthèque (« screen scraping / web scraping » notamment), à quelques fins que ce soit, commerciales ou non, est strictement interdite, à moins que HELLOWORK n'ait conclu une convention de licence écrite…". Usage is limited to "fins privées et non commerciales". This text is **repeated in an HTML comment at the top of every page's source** ("En visualisant le code source vous acceptez les CGU…").
- **Art. 8.3:** sui generis database right (CPI L.342-1 et s.). It prohibits extracting a substantial part, and also "extraire ou réutiliser à répétition ou de manière systématique des parties… non substantielles… lorsque ces opérations excèdent manifestement les conditions d'utilisation normales".
- **Art. 6.2 (candidate obligations):**
  - Information must be truthful and concern the candidate personally.
  - "L'Utilisateur Candidat s'engage à faire une utilisation raisonnable du Site… **Un nombre très significatif d'envois de candidatures, sur une durée limitée, et/ou sur des secteurs d'activité divers peut être apparentée à une utilisation anormale**… HELLOWORK se réserve le droit de suspendre l'accès… **de manière temporaire, d'une durée de six (6) mois, ou définitive**."
- **Art. 5.3:** the account can be closed without notice for CGU breach.
- **Art. 8.2:** deep links are formally subject to authorization. This is irrelevant for private use.
- **Art. 12:** French law applies.
- Privacy policy: https://www.hellowork-group.com/fr/legal/pdc-hellowork/

### 4.3 Practical legal reading for jobbot (not legal advice)
- Automated **search scraping** clearly breaches CGU 8.2 and robots.txt. The realistic sanction for a single candidate is account suspension or IP blocking, not litigation. Still, the tool should make it opt-in, keep it low-volume, and never republish data (art. 8.3).
- **Automated applying** hits CGU 6.2 (abnormal volume). Keep it small, targeted to one sector and human-confirmed. That also matches HelloWork's own guidance that mass applying is counter-productive.
- Bypassing technical measures (captcha, OTP) is a stronger legal signal. See also Code pénal art. 323-1 on fraudulent access to an automated data processing system. **Never automate captcha or OTP solving**: the human logs in once in the persistent profile.
- **GDPR:** offer data is mostly company data, but some ads contain recruiter names. The candidate's own processing for a personal job search falls under the household exemption (GDPR art. 2(2)(c)), provided data is not published or shared. Keep data local (SQLite), store only what is needed, and purge expired offers (for example `validThrough` + 90 days).
- **CNIL:** the June 2025 guidance on "moissonnage" (web scraping) says robots.txt and CAPTCHA are signals of opposition that a controller relying on legitimate interest must respect (https://www.cnil.fr/fr/focus-interet-legitime-collecte-par-moissonnage). It targets AI and legitimate-interest processing, but it is a useful benchmark: respect robots.txt where possible.
- **Your own data:** reading your own `mes-candidatures` page is your own account data (GDPR right of access, art. 15). It is low-risk if done at human pace, although automated access is still formally covered by CGU art. 1.

### 4.4 Operational limits
- No rate limit was observed in 12 back-to-back search requests (0.08–0.39 s each, all 200). Use a polite budget anyway: **≥ 5 s between requests, ≤ 50 HelloWork requests a day** for discovery and detail, and cache aggressively.
- 30 results per page. Offers live about 30 days (`validThrough`).
- The same offer cannot be applied to twice. The status shows "Erreur" if you already applied via HelloWork or another platform. An application cannot be modified or cancelled.
- History PDF export: last 3 months, at most 500 applications, **10 downloads a day**.
- CV file at most 2 MiB. Message at most 2,900 characters (truncated beyond).

---

## 5. Gotchas & anti-bot measures

1. **WAF user-agent filter.** Azure Application Gateway returns 403 for `python-requests/*` and `Python-urllib/*` UAs. httpx's default UA currently passes, but always send a real Chrome UA plus `Accept-Language: fr-FR`.
2. **No JS challenge was seen.** There is no Cloudflare, DataDome, PerimeterX or Akamai (headers only show `X-Cache`, `X-Cache-Hits` and HSTS; HTML has no captcha markers). Headless Chromium through Playwright 1.58 rendered search, detail and the apply form with no challenge. Do **not** add stealth or evasion flags. They aren't needed and would make a ToS breach worse.
3. **Captcha and OTP.** Login uses Friendly Captcha. Anonymous apply and alert creation use an email OTP (6 digits; the `otp` Stimulus controller distributes the pasted code across inputs). Use a persistent logged-in profile and let the human do login and OTP.
4. **Two-block result ordering.** Results come as `LO-Moteur-Exact` cards followed by `LO-Moteur-Approchant` (broadened) cards. With `st=date` each block is sorted by date separately: the live sequence was "1 jour … 28 jours, then 11 heures, 11 heures, … 4 jours". Never stop pagination at the first "old" card. Read all pages, or sort client-side.
5. **Broad keyword matching.** `k=développeur` pulls "Business Developer", "Développeur commercial" and "Chargé de développement". Filter client-side: JSON-LD `occupationalCategory == "Informatique"`, dataLayer `Domaine == "informatique"`, or a negative title regex `commercial|business|chargé de développement|développement commercial`. Also run queries with `f=Informatique_dev`.
6. **`ray` default is 20 km.** Always pass it explicitly (50 for Metz, to reach Thionville and Pont-à-Mousson). Nancy sits at the edge of 50 km, so run a separate `l=Nancy` query. Luxembourg: `l=Luxembourg&l_autocomplete=http://www.rj.com/commun/localite/pays/LU` (33 offers, all contracts).
7. **Free-text `l` can silently return 0.** `k=développeur web&l=Moselle&c=CDI&c=Alternance&d=w` returned "0 offre", while `l=Moselle` + Alternance returned 21. Prefer `l_autocomplete` URIs for departments and regions, and alert when a normally non-empty query returns 0.
8. **Relative dates only on cards.** "il y a N heures/jours". The exact `datePosted` is in the detail JSON-LD, and `datePublicationOffre` (YYYYMMDD) in the dataLayer.
9. **lxml tree fix-up.** `<p>` inside `<h3>` is invalid, so lxml restructures it. Select `a[data-cy=offerTitle] p`, not `h3 p`.
10. **Random element IDs** in the CV uploader (`id="89c"`, `"025"`, `"204"`) change on every render. Select by `name`, `data-cy` or `form` attributes only.
11. **Lazy Turbo frames.** The apply form frame has `loading="lazy"` and only loads when `#postuler` scrolls into view (clicking the header button does this). Wait for `#offer-detail-main-step-form`, not for `networkidle`.
12. **Overlays.** The cookie banner (`#hw-cc-notice-continue-without-accepting-btn`, `#hw-cc-notice-accept-btn`, `#hw-cc-consent-modal-accept-btn`) and a **custom Google One Tap panel** (top right, "Se connecter à Hellowork avec Google", inside `turbo-frame#one-tap-custom`) can cover elements. Dismiss the banner first. One Tap should not appear when logged in.
13. **Google OAuth in automated browsers.** Google often refuses sign-in from automation-controlled Chromium ("This browser or app may not be secure"). Recommend an email and password login in the persistent profile. If the account was created via Google, set a password through "Mot de passe oublié ?" (`https://www.hellowork.com/fr-fr/candidat/mot-de-passe-oublie.html`).
14. **CV upload auto-submits** (`change->file-validator#check file-validator:valid->auto-submittable#submit`) and replaces the uploader frame. Wait until `input[name=JweHashResume]` **changes value**. When logged in it may already hold the default CV token.
15. **External offers** (`Url`/`UrlWithForm`) do not expose the partner URL before step 1. It is revealed only after submitting identity and CV (or "Passer cette étape"), then HelloWork redirects. The application is not complete until the external ATS form is done (status "À finaliser").
16. **Expired offers** return **410**, not 404. Treat 410 as a terminal "expired" state.
17. **Multi-apply bounce.** The JS bundle contains a `multi_apply_bounce` controller (checkboxes plus an "N offres" counter and a `multiApplyForm`). After an application, HelloWork likely proposes a one-click apply to several similar offers (robots path `/fr-fr/rebond/`). jobbot must **not** blindly accept it. Detect it and let the human decide. (UNCERTAIN: seen only in the JS source.)
18. **Pre-qualification questions.** A `prequalification` controller shows a `select` question and checks it against `expectedAnswers`. A wrong answer shows a refusal message instead of the form. Handle it as a human step.
19. **Brotli.** The server answers `Content-Encoding: br` when asked. httpx only decodes br if the `brotli` package is installed. Either install it or let httpx negotiate gzip (the default without brotli; verified 200, 628 KB uncompressed).

---

## 6. Concrete Python implementation notes

Dependencies: `httpx`, `beautifulsoup4` + `lxml` (or selectolax), `playwright` (sync). All code is sync.

### 6.1 Constants
```python
# jobbot/sources/hellowork/const.py
BASE = "https://www.hellowork.com"
SEARCH_PATH = "/fr-fr/emploi/recherche.html"
OFFER_PATH = "/fr-fr/emplois/{id}.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,  # never python-requests/urllib UA -> WAF 403
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
PAGE_SIZE = 30
MIN_DELAY_S = 5.0
CONTRACTS = {"CDI", "CDD", "Travail_temp", "Stage", "Alternance", "Independant",
             "Franchise", "Associe", "Fonctionnaire", "Freelance", "Stage_de_lycee"}
RADII = {0, 5, 10, 20, 50}          # UI values; server also accepted 100
SORTS = {"relevance", "date"}
DATE_FILTERS = {"all", "h", "d", "w", "m"}
LOC = {  # l_autocomplete URIs (live-verified)
    "Metz": "http://www.rj.com/commun/localite/commune/57463",
    "Nancy": "http://www.rj.com/commun/localite/commune/54395",
    "Thionville": "http://www.rj.com/commun/localite/commune/57672",
    "Moselle": "http://www.rj.com/commun/localite/departement/57",
    "Grand Est": "http://www.rj.com/commun/localite/region/44",
    "Luxembourg": "http://www.rj.com/commun/localite/pays/LU",
}
IN_SITE_TYPES = {"ATS", "Smartapply", "Smartapply_v2"}
EXTERNAL_TYPES = {"Url", "UrlWithForm"}
```

### 6.2 Search (httpx + bs4). Use sparingly, see §1 and §4
```python
import json, re, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import httpx
from bs4 import BeautifulSoup

_REL = re.compile(r"il y a\s+(\d+)\s+(minute|heure|jour|mois)", re.I)
_TOTAL = re.compile(r"(\d[\d\s \xa0]*)\s*offres?", re.I)

@dataclass(frozen=True)
class Card:
    offer_id: str
    url: str
    title: str
    company: str | None
    location: str | None
    contract: str | None
    tags: tuple[str, ...]
    posted_approx: datetime | None
    response_type: str | None      # ATS | Smartapply | UrlWithForm | Url
    match_block: str | None        # LO-Moteur-Exact | LO-Moteur-Approchant | ...
    follow_up: bool

def _rel_to_dt(text: str, now: datetime) -> datetime | None:
    m = _REL.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"minute": timedelta(minutes=n), "heure": timedelta(hours=n),
             "jour": timedelta(days=n), "mois": timedelta(days=30 * n)}[unit]
    return now - delta

def build_params(k: str, l: str | None, contracts=("Alternance",), ray=50, sort="date",
                 page=1, d="all", functions=(), l_uri: str | None = None) -> list[tuple[str, str]]:
    if not set(contracts) <= CONTRACTS: raise ValueError(f"bad contract {contracts}")
    if sort not in SORTS or d not in DATE_FILTERS or page < 1: raise ValueError("bad params")
    params = [("k", k), ("ray", str(ray)), ("st", sort), ("p", str(page)), ("d", d)]
    if l: params.append(("l", l))
    if l_uri: params.append(("l_autocomplete", l_uri))
    params += [("c", c) for c in contracts] + [("f", f) for f in functions]
    return params

def parse_serp(html: str, now: datetime | None = None) -> tuple[int | None, list[Card]]:
    now = now or datetime.now(timezone.utc)
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.select_one("h1")
    m = _TOTAL.search(h1.get_text(" ", strip=True)) if h1 else None
    total = int(re.sub(r"\D", "", m.group(1))) if m else None
    cards: list[Card] = []
    for li in soup.select("li[data-id-storage-item-id]"):
        a = li.select_one("a[data-cy=offerTitle]")
        if a is None:
            continue
        ps = a.select("p")
        serp = li.select_one("[data-cy=serpCard]")
        pd = {}
        if serp and serp.has_attr("data-analytics-values-param"):
            try:
                pd = (json.loads(serp["data-analytics-values-param"]).get("product_data") or [{}])[0]
            except json.JSONDecodeError:
                pd = {}
        sel = lambda css: (li.select_one(css).get_text(strip=True) if li.select_one(css) else None)
        cards.append(Card(
            offer_id=li["data-id-storage-item-id"],
            url=BASE + a["href"],
            title=ps[0].get_text(strip=True) if ps else a.get("title", ""),
            company=ps[1].get_text(strip=True) if len(ps) > 1 else None,
            location=sel("[data-cy=localisationCard]"),
            contract=sel("[data-cy=contractCard]"),
            tags=tuple(t.get_text(strip=True) for t in li.select("div.tag-secondary-s")),
            posted_approx=_rel_to_dt(sel("div.typo-s.text-grey-500") or "", now),
            response_type=pd.get("product_response_type"),
            match_block=pd.get("product_list"),
            follow_up=li.select_one("[data-cy=followUpTag]") is not None,
        ))
    return total, cards

def search_all(client: httpx.Client, max_pages: int = 5, **kw) -> list[Card]:
    out: list[Card] = []
    for page in range(1, max_pages + 1):
        r = client.get(BASE + SEARCH_PATH, params=build_params(page=page, **kw))
        if r.status_code == 403:
            raise RuntimeError("HelloWork WAF 403 - check User-Agent / slow down")
        r.raise_for_status()
        total, cards = parse_serp(r.text)
        out.extend(cards)
        if not cards or (total is not None and page * PAGE_SIZE >= total):
            break
        time.sleep(MIN_DELAY_S)
    return out  # dedupe by offer_id downstream; sort by detail datePosted, not card order

# usage
# with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as c:
#     cards = search_all(c, k="développeur", l="Metz", l_uri=LOC["Metz"],
#                        contracts=("Alternance", "CDI"), ray=50, sort="date")
```

### 6.3 Offer detail (robots-allowed)
```python
_DL = re.compile(r'window\.dataLayer\.push\((\{"categoryPage":"Detail".*?\})\);', re.S)

@dataclass(frozen=True)
class OfferDetail:
    offer_id: str
    status: str                    # "ok" | "expired"
    jobposting: dict | None
    datalayer: dict | None
    response_type: str | None
    apply_mode: str | None         # "in_site" | "external" | None
    apply_label: str | None

def fetch_offer(client: httpx.Client, offer_id: str) -> OfferDetail:
    if not offer_id.isdigit():
        raise ValueError(offer_id)
    r = client.get(BASE + OFFER_PATH.format(id=offer_id))
    if r.status_code == 410:
        return OfferDetail(offer_id, "expired", None, None, None, None, None)
    r.raise_for_status()
    return parse_offer(offer_id, r.text)

def parse_offer(offer_id: str, html: str) -> OfferDetail:
    soup = BeautifulSoup(html, "lxml")
    jp = None
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            d = json.loads(s.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            jp = d
            break
    m = _DL.search(html)
    dl = json.loads(m.group(1)) if m else None
    rt = None
    if dl:
        prods = (((dl.get("ecommerce") or {}).get("detail") or {}).get("products") or [{}])
        rt = prods[0].get("response_type")
    btn = soup.select_one("a[data-cy=applyButtonHeader]")
    label = btn.get_text(" ", strip=True) if btn else None
    if rt in IN_SITE_TYPES or (jp and jp.get("directApply") is True):
        mode = "in_site"
    elif rt in EXTERNAL_TYPES or (label and "site du" in label.lower()):
        mode = "external"
    else:
        mode = None
    return OfferDetail(offer_id, "ok", jp, dl, rt, mode, label)

# ATS text for scoring: BeautifulSoup(jp["description"], "lxml").get_text("\n")
#   + jp.get("qualifications", "") + ", ".join(jp.get("skills", []))
```

Suggested SQLite (fits the rest of jobbot):
```sql
CREATE TABLE IF NOT EXISTS hw_offer (
  offer_id TEXT PRIMARY KEY, title TEXT, company TEXT, city TEXT, postal_code TEXT,
  contract TEXT, date_posted TEXT, valid_through TEXT, response_type TEXT, apply_mode TEXT,
  direct_apply INTEGER, occupational_category TEXT, jsonld TEXT, fetched_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS hw_application (
  offer_id TEXT PRIMARY KEY REFERENCES hw_offer(offer_id), prepared_at TEXT, submitted_at TEXT,
  cv_path TEXT, message TEXT, screenshot_path TEXT, hw_status TEXT, hw_status_checked_at TEXT
);
```

### 6.4 Apply with the logged-in persistent profile (Playwright sync)
Strategy:
- **One-time setup:** `jobbot hellowork login` opens a headed persistent context on the login page. The human logs in with email and password (Friendly Captcha if shown) and ticks "Rester connecté·e". jobbot then verifies `IsAuthenticated`.
- **Per application:** open the offer, dismiss the banner, check the apply mode, click the header button, wait for the form, verify the prefilled identity, upload the tailored CV (optional), fill the message, take a screenshot, **ask the human**, submit, loop over follow-up steps (human for any unknown step), then verify success.
- **Guards:** daily cap, 20–60 s between applications, skip if already in `hw_application`, abort on OTP, captcha or unknown required fields.

```python
# assumes BASE, OFFER_PATH from 6.1 and `re`, `datetime` imports from 6.2
from pathlib import Path
import random, re, time
from datetime import datetime
from playwright.sync_api import sync_playwright, BrowserContext, Page, TimeoutError as PWTimeout

PROFILE_DIR = Path.home() / ".jobbot" / "profiles" / "hellowork"
FORM = "#offer-detail-main-step-form"
DAILY_CAP = 10

def open_context(p, headless: bool = False) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR), headless=headless, locale="fr-FR",
        timezone_id="Europe/Paris", viewport={"width": 1366, "height": 900},
        accept_downloads=True)  # optionally channel="chrome" to use installed Chrome

def is_logged_in(page: Page) -> bool:
    data = page.evaluate("""async () => {
        const r = await fetch('/fr-fr/compte/accountdata', {method: 'POST',
          headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
          body: JSON.stringify({shouldFetchBookmarks: false})});
        return r.ok ? await r.json() : null; }""")
    return bool(data and data.get("IsAuthenticated"))

def interactive_login() -> None:
    with sync_playwright() as p:
        ctx = open_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/fr-fr/candidat/connexion-inscription.html#connexion")
        input("Connecte-toi dans la fenêtre (cocher 'Rester connecté·e'), puis Entrée… ")
        page.goto(BASE + "/fr-fr/")
        if not is_logged_in(page):
            raise RuntimeError("Login not detected (IsAuthenticated=false)")
        ctx.close()

def dismiss_overlays(page: Page) -> None:
    btn = page.locator("#hw-cc-notice-continue-without-accepting-btn")
    try:
        if btn.count() and btn.first.is_visible():
            btn.first.click()
    except PWTimeout:
        pass

def prepare_application(page: Page, offer_id: str, cv_path: Path | None,
                        message: str | None, shots_dir: Path) -> dict:
    resp = page.goto(BASE + OFFER_PATH.format(id=offer_id), wait_until="domcontentloaded")
    if resp and resp.status == 410:
        return {"status": "expired"}
    dismiss_overlays(page)
    header_btn = page.locator("a[data-cy=applyButtonHeader]")
    label = header_btn.inner_text().strip()
    if "site du" in label.lower():                     # external ATS: hand off to the human
        return {"status": "external", "label": label}
    header_btn.click()                                  # scrolls to #postuler -> lazy frame loads
    page.locator(FORM).wait_for(state="visible", timeout=20_000)
    # identity: prefilled when logged in; verify instead of overwriting
    ident = {n: page.locator(f'[form="offer-detail-main-step-form"][name="{n}"]').input_value()
             for n in ("Firstname", "LastName", "Email")}
    if not all(ident.values()):
        return {"status": "needs_human", "reason": f"identity not prefilled: {ident}"}
    if cv_path is not None:
        if cv_path.stat().st_size > 2_097_152 or cv_path.suffix.lower() not in {".pdf", ".docx", ".doc", ".odt", ".rtf"}:
            raise ValueError("CV must be <= 2 MiB and pdf/doc/docx/odt/rtf")
        token = page.locator("input[name=JweHashResume]")
        before = token.input_value() if token.count() else ""
        page.set_input_files("input[data-cy=cv-uploader-input]", str(cv_path))
        page.wait_for_function(
            "(b) => { const e = document.querySelector('input[name=JweHashResume]');"
            " return e && e.value && e.value !== b; }", arg=before, timeout=30_000)
    if message:
        ta = page.locator("textarea[name=MotivationLetter]")
        if ta.count():                                  # only ATS / Smartapply
            if not ta.is_visible():
                page.locator("label[data-cy=motivationFieldButton]").click()
            ta.fill(message[:2900])
    cgu = page.locator("input[data-cy=cgu-box]")
    if cgu.count() and not cgu.is_checked():          # normally absent when logged in (UNCERTAIN)
        return {"status": "needs_human", "reason": "CGU/account-creation checkbox shown -> not logged in?"}
    shot = shots_dir / f"hw_{offer_id}_ready.png"
    page.locator("#postuler").screenshot(path=str(shot))
    return {"status": "ready", "screenshot": str(shot)}

def submit_application(page: Page, offer_id: str, shots_dir: Path, max_steps: int = 4) -> str:
    """Click through the funnel. Stops on anything unknown. Human confirmation must happen BEFORE calling."""
    page.locator("button[data-cy=submitButton]").click()
    for step in range(max_steps):
        page.wait_for_timeout(1500)
        frame = page.locator("turbo-frame#offer-detail-step-frame")
        text = frame.inner_text() if frame.count() else page.inner_text("body")
        page.locator("#postuler").screenshot(path=str(shots_dir / f"hw_{offer_id}_step{step}.png"))
        if page.locator("[data-controller~=otp]").count():
            return "needs_human:otp"                    # should not happen when logged in
        if re.search(r"candidature (a bien été )?envoy|Merci|bien été transmise", text, re.I):
            return "sent"                               # UNCERTAIN wording -> confirm via mes-candidatures
        nxt = frame.locator("button[type=submit]")
        required_empty = frame.locator("input[required]:not([type=hidden]), select[required], textarea[required]")
        if required_empty.count():
            return "needs_human:extra_questions"        # salary / phone / prequalification
        if nxt.count() == 1:
            nxt.click()
            continue
        return "unknown_state"
    return "unknown_state"

# Orchestration sketch:
# with sync_playwright() as p:
#     ctx = open_context(p, headless=False); page = ctx.new_page()
#     page.goto(BASE + "/fr-fr/"); assert is_logged_in(page)
#     for offer in queue[:DAILY_CAP]:
#         res = prepare_application(page, offer.id, offer.cv_pdf, offer.message, shots)
#         if res["status"] == "ready" and input(f"Envoyer {offer.id} ? [o/N] ").lower() == "o":
#             status = submit_application(page, offer.id, shots)
#             db.record(offer.id, status)
#         time.sleep(random.uniform(20, 60))
```

### 6.5 Tracking your own applications
```python
STATUSES = ["En cours d'envoi", "Envoyée", "Consultée", "Entretien", "Refusée",
            "Acceptée", "Erreur", "À finaliser"]   # official list (help centre)

def snapshot_applications(ctx: BrowserContext, out_dir: Path) -> Path:
    page = ctx.new_page()
    page.goto(BASE + "/fr-fr/candidat/mes-candidatures.html", wait_until="domcontentloaded")
    if "connexion-inscription" in page.url:
        raise RuntimeError("Session expired - run interactive_login()")
    page.wait_for_timeout(2000)
    path = out_dir / f"mes-candidatures-{datetime.now():%Y%m%d}.html"
    path.write_text(page.content(), encoding="utf-8")   # 1st run: inspect and write selectors
    return path
# Parsing: selectors NOT verified (needs a logged-in session). Start with: for each element containing
# a link a[href*="/fr-fr/emplois/"], read the offer id and the nearest status text in STATUSES.
# PDF export: button "top right of the list" -> `with page.expect_download() as d: <click>`; 10/day limit.
```
Poll once a day at most. HelloWork also emails you when a recruiter opens your CV (status "Consultée").

### 6.6 Discovery via HelloWork alert emails (preferred, ToS-friendly)
- The candidate creates alerts once by hand (logged in: `/fr-fr/candidat/creation-alerte.html`, or "Créer mon alerte" on a search page; an email OTP applies). Suggested alerts: "Développeur" / Metz 57000 / Alternance+CDI, and "Développeur full-stack" / Moselle / Alternance. Add Nancy and Luxembourg.
- jobbot reads the candidate's mailbox with IMAP. Sender domain is probably `@hellowork.com` (UNCERTAIN; check one email). Extract every URL, resolve tracking redirects with a HEAD/GET that follows redirects, and match `r"/fr-fr/emplois/(\d+)\.html"`. Then call `fetch_offer()`.

---

## 7. Claims table

| # | Claim | Status | Source / evidence |
|---|---|---|---|
| 1 | robots.txt `User-Agent: *` has `Disallow: /*?` (all query URLs) and disallows `/fr-fr/emploi/recherche.html`, `/fr-fr/offres/postuler`, `/fr-fr/offres/getinitialformframeview*`, `/fr-fr/candidat/`, `/fr-fr/api/`, `/fr-fr/search/` | LIVE-VERIFIED | GET https://www.hellowork.com/robots.txt (200, Last-Modified 17 Sep 2026) |
| 2 | robots bad-bot list includes `Python-urllib`, `httplib`, `Wget`, `HTTrack` → `Disallow: /` | LIVE-VERIFIED | robots.txt |
| 3 | Job detail `/fr-fr/emplois/{id}.html` is robots-allowed and has `meta robots index, follow` + canonical | LIVE-VERIFIED | GET 79006307 |
| 4 | Search = `GET /fr-fr/emploi/recherche.html` with params `k,l,c(repeat),ray,st,p,d,cod,e,t,et,ctt,f,s,rse,msa,so,sr,k_autocomplete,l_autocomplete` | LIVE-VERIFIED | form `#searchForm` inputs + several live queries |
| 5 | Contract values `CDI,CDD,Travail_temp,Stage,Alternance,Independant,Franchise,Associe,Fonctionnaire,Freelance,Stage_de_lycee` | LIVE-VERIFIED | search form checkboxes |
| 6 | Radius UI values 0/5/10/20/50, default 20; server accepts `ray=100` | LIVE-VERIFIED | `data-range-available-values-value`, hidden `ray=20`; query ray=100 → 139 results |
| 7 | 30 cards per page; `p` is 1-based; past last page = 200 with 0 cards | LIVE-VERIFIED | 112 results → pages 1–4; p=9 → 0 cards |
| 8 | `st=date` sorts inside the Exact block, then the Approchant block | LIVE-VERIFIED | date sequence on st=date page 1 |
| 9 | `d` values all/h/d/w/m; `cod` values; `e`,`t`,`et`,`ctt`,`f`,`s` values as listed | LIVE-VERIFIED | form + facet labels |
| 10 | `l` free text works without `l_autocomplete` (Moselle → 21 in both cases) | LIVE-VERIFIED | q1 vs q3 |
| 11 | Location autocomplete endpoint uses `term`, returns `http://www.rj.com/commun/localite/...` URIs (Metz 57463, Nancy 54395, Thionville 57672, Moselle departement/57, Grand Est region/44, Luxembourg pays/LU) | LIVE-VERIFIED | GET …getsearchenginelocationsuggestionsframeview?term= |
| 12 | Job autocomplete returns `http://www.rj.com/Commun/Post/...` URIs | LIVE-VERIFIED | GET …getsearchenginegroupedjobsuggestionsframeview?term=développeur |
| 13 | SERP has no JobPosting / `__NEXT_DATA__`; only WebSite/Organization/BreadcrumbList JSON-LD | LIVE-VERIFIED | search1.html |
| 14 | Card selectors (`li[data-id-storage-item-id]`, `a[data-cy=offerTitle]`, `[data-cy=localisationCard]`, `[data-cy=contractCard]`, `[data-cy=contractTag]`, `[data-cy=followUpTag]`, date div) | LIVE-VERIFIED | curl HTML + Playwright locators |
| 15 | `product_response_type` values ATS / Smartapply / UrlWithForm / Url; `product_list` Exact / Approchant | LIVE-VERIFIED | analytics JSON in cards |
| 16 | Total count in `<h1>` "N offres" | LIVE-VERIFIED | multiple queries |
| 17 | Detail JSON-LD JobPosting with full HTML description, skills, salary, datePosted, validThrough (=+30 d), directApply | LIVE-VERIFIED | 79006307, 79199308, 83292557 |
| 18 | `directApply` true for ATS / Smartapply_v2, false for Url / UrlWithForm | LIVE-VERIFIED | 5 offers compared |
| 19 | dataLayer push contains `response_type`, `datePublicationOffre`, `contrat`, `typeEntreprise`, `Domaine` | LIVE-VERIFIED | detail HTML |
| 20 | Header apply button `a[data-cy=applyButtonHeader]` text "Postuler" (in-site) vs "Postuler sur le site du partenaire" (external) | LIVE-VERIFIED | Playwright on 79006307 / 81298493 |
| 21 | Apply step-1 frame `GET /fr-fr/offres/getinitialformframeview?offerId=` → form POST `/fr-fr/offres/postcandidateinformationfromstepframeview` with Firstname, LastName, Email, JweHashResume, MotivationLetter (≤2900), HasAcceptedCGU, FunnelId | LIVE-VERIFIED | curl + Playwright DOM (not submitted) |
| 22 | CV upload `POST /fr-fr/uploadcv?...` multipart `upload`, max 2 MiB, formats list, uploaderApp=41, uploaderType=RO | LIVE-VERIFIED | uploader frame markup (not submitted) |
| 23 | MotivationLetter is absent for Url / UrlWithForm offers; `Url` offers have "Passer cette étape" skip form | LIVE-VERIFIED | form frames of 79199308 / 81298493 |
| 24 | Anonymous apply requires account creation (CGU checkbox) + email security code (OTP) | LIVE-VERIFIED (markup "[OTP] CTA click Continuer") + DOC-SOURCED | https://hellowork-candidat.zendesk.com/hc/fr/articles/32867869934993 |
| 25 | Logged-in apply: identity prefilled, default CV preselected (dropdown to change or upload), optional message, possible extra questions, confirmation email | DOC-SOURCED | same help article (updated 2026-05-27) |
| 26 | External offers need completing the external form; status "À finaliser" | DOC-SOURCED | help article + https://hellowork-candidat.zendesk.com/hc/fr/articles/39095236765457 |
| 27 | Application statuses: En cours d'envoi, Envoyée, Consultée, Entretien, Refusée, Acceptée, Erreur, À finaliser | DOC-SOURCED | https://hellowork-candidat.zendesk.com/hc/fr/articles/39095236765457 |
| 28 | No double application to the same offer ("Erreur"); no modification or cancellation after sending | DOC-SOURCED | https://hellowork-candidat.zendesk.com/hc/fr/articles/42172416781329 , …/39099350085905 |
| 29 | HelloWork does not send follow-ups or give out recruiter contacts | DOC-SOURCED | https://hellowork-candidat.zendesk.com/hc/fr/articles/39095434260881 |
| 30 | PDF history export: 3 months, ≤500 applications, ≤10 downloads/day | DOC-SOURCED | https://hellowork-candidat.zendesk.com/hc/fr/articles/46649188249617 |
| 31 | `/fr-fr/candidat/mes-candidatures.html` → 302 to login when anonymous | LIVE-VERIFIED | curl headers |
| 32 | `POST /fr-fr/compte/accountdata` (JSON body) returns `IsAuthenticated` etc.; GET → 404, no-JSON POST → 415 | LIVE-VERIFIED | curl + Playwright fetch |
| 33 | Logged-in state adds `body.isConnected` class | LIVE-VERIFIED (JS source) | stimulus.js module 7765 |
| 34 | Login: `/fr-fr/compte/loginprofil`, Friendly Captcha (`frc-captcha`, sitekey FCMVVIRROC6FLDRK, captchaIsNeeded true), Google + LinkedIn OAuth, Facebook dropped | LIVE-VERIFIED (markup/JS; vendor inferred from `WidgetInstance`/`solutionFieldName` API) | login page HTML |
| 35 | WAF = Azure Application Gateway v2; `python-requests` & `Python-urllib` UA → 403; browser, curl, httpx and empty UA → 200 | LIVE-VERIFIED | curl UA tests |
| 36 | No Cloudflare / DataDome / captcha on public pages; no 429 in 12 rapid requests | LIVE-VERIFIED | headers/HTML scan, burst test |
| 37 | Headless Chromium (Playwright 1.58) renders SERP, detail and apply form without challenge | LIVE-VERIFIED | pw_probe / pw_detail runs + screenshot |
| 38 | Cookie banner button IDs `#hw-cc-notice-continue-without-accepting-btn`, `#hw-cc-notice-accept-btn`; custom Google One Tap panel overlays page | LIVE-VERIFIED | Playwright DOM + screenshot |
| 39 | Expired offer → HTTP 410 | LIVE-VERIFIED | ids 50000000, 70000000 |
| 40 | Sitemap horizon.xml → 403 | LIVE-VERIFIED | curl |
| 41 | SEO landing pages (`/fr-fr/alternance/metier_…-ville_metz-57000.html`) exist with the same card markup | LIVE-VERIFIED | 4 pages fetched |
| 42 | Company pages expose no email / mailto | LIVE-VERIFIED (1 page) | chaussea-18417 |
| 43 | Every page's HTML source starts with a comment quoting CGU art. 8.2 | LIVE-VERIFIED | search/detail HTML |
| 44 | CGU art. 1 (bots = illegitimate users), 8.2 (scraping banned even non-commercial), 8.3 (sui generis DB right), 6.2 (mass applying → 6-month or permanent suspension), 5.3 (account closure) | DOC-SOURCED | https://www.hellowork-group.com/fr/legal/cgu-hellowork/ |
| 45 | CNIL (June 2025): robots.txt/CAPTCHA are opposition signals to respect for legitimate-interest scraping | DOC-SOURCED | https://www.cnil.fr/fr/focus-interet-legitime-collecte-par-moissonnage |
| 46 | HelloWork ATS Partner API exists for ATS vendors/recruiters, not candidates | DOC-SOURCED | https://ats-partner.hellowork.com/v1/ |
| 47 | Alert creation form `POST /fr-fr/alerte/create` needs email OTP; fields Job.DisplayLabel, Job.JobCriterionUri, Location.Uri, OtpData.Email, Contracts | LIVE-VERIFIED (markup, not submitted) | search page `#alert-form` |
| 48 | Metz ±50 km, Alternance, `f=Informatique_dev` = 4 offers (79006307, 83292557, 83292364, 81303268) | LIVE-VERIFIED (2026-09-23 snapshot) | query g2 |
| 49 | Logged-in form: exact selector of the CV dropdown, whether CGU box is hidden, wording of the success screen | UNCERTAIN | no account available; discover on first run |
| 50 | Partner URL for Url/UrlWithForm is revealed only after step-1 submit or "Passer cette étape" | UNCERTAIN | inferred: no external URL in pre-submit HTML |
| 51 | Post-apply "multi-apply bounce" (apply to similar offers in bulk) | UNCERTAIN | only the `multi_apply_bounce` Stimulus controller seen + robots `/fr-fr/rebond/` |
| 52 | `cod` boundary for a 24-month contract (`1-2y` vs `2y`) | UNCERTAIN | labels only |
| 53 | HelloWork alert email sender domain and link format | UNCERTAIN | not observed |
| 54 | HelloWork offers appear in the France Travail "Offres d'emploi" API partner feed | UNCERTAIN | not verified |
| 55 | Google OAuth may refuse login from automation-controlled Chromium | UNCERTAIN (known general Google behaviour, not tested here) | — |
