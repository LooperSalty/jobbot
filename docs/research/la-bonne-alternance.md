# La Bonne Alternance (LBA) + API Apprentissage — jobbot research

> Researched 2026-09-23 with live probes (curl) against production, the published OpenAPI spec
> (`https://api.apprentissage.beta.gouv.fr/api/documentation/json`, version `256bb22`),
> and the open-source code at `github.com/mission-apprentissage/labonnealternance` (LBA app, version `1.905.0` live)
> and `github.com/mission-apprentissage/api-apprentissage`.
> Scratch evidence (raw responses) was kept out of the repo on purpose.

---

## 1. TL;DR + recommendation for jobbot

**Two access paths exist:**

| Path | Auth | What you get | Apply? |
|---|---|---|---|
| **A. Official "API Apprentissage"** `https://api.apprentissage.beta.gouv.fr/api/job/v1/*` | `Authorization: Bearer <API key>` (free self-service account, key valid 1 year) | Structured `jobs[]` (LBA offers + France Travail real-time + partners like Hellowork, Météojob…) and `recruiters[]` (companies that hire apprentices = spontaneous applications). ROME/RNCP/geo/diploma filters. Max 150 results per source, no pagination. Daily full JSON export. | `POST /job/v1/apply` exists, **but production access is refused for individuals** ("Les demandes d'habilitation pour un usage individuel ne seront pas accordées"). A sandbox key only sends to LBA's staging site. **Not usable for real applications.** |
| **B. LBA website internal JSON API** `https://labonnealternance.apprentissage.beta.gouv.fr/api/*` (what the Next.js front calls) | none for search/detail; per-offer JWT (returned by the detail call) for applications | `GET /api/v1/search`: free text `q`, geo, `admin_area=departement:57`, `smart_apply`, `is_algo_company`, `level`, `sort=date`, **deep pagination** (100/page, tested to 15 000th hit), facets, `application_count`. `GET /api/_private/jobs/:source/:id`: detail + `token` + `recipient_id` + `contact.hasEmail`. | `POST /api/v2/_private/application` with `Authorization: Bearer <token from detail>`. This is exactly what the site's "Candidature simplifiée" form sends. Undocumented/internal, so it can change without notice. |

**Recommendation:**

1. **Source (search):** implement `LbaSource` on **path B `/api/v1/search`**. It needs no key, is paginated, returns `smart_apply` (one-click apply available) and `application_count` (competition), and supports `admin_area`.
   - Also implement **path A** as an optional, higher-fidelity source once the user creates a free key: search is self-service, and path A gives ROME filtering plus real-time France Travail offers.
   - Merge and dedupe the two.
   - Run separate queries for **Metz / Thionville / Nancy** (Moselle `departement:57`, Meurthe-et-Moselle `departement:54`, or radius searches). **LBA does not cover Luxembourg** (French contracts only).
2. **Apply:** implement `LbaApplier` (channel `lba`) for items where `smart_apply == true` / `contact.hasEmail == true` / `recipient_id` is not null.
   - **Preferred:** Playwright on the public detail page. Click `button[data-testid="postuler-button"]`, fill the form, upload the CV, submit, then wait for the text "Votre candidature a bien été envoyée à". This is the most defensible option because it is the normal candidate journey.
   - **Faster alternative:** the same JSON POST the browser makes (`/api/v2/_private/application`) through httpx, reusing the fresh per-offer JWT.
   - Either way:
     - keep the volume low (≤ 20–30/day, randomized pauses; the server hard cap is ~100/day/applicant);
     - have a human validate each application (review mode);
     - **never re-apply to the same offer or SIRET** (the server allows only 3 applications per offer/company per applicant, ever).
3. **Other offer types:**
   - `offres_emploi_partenaires` with `hasEmail=false` → send to the matching applier using `contact.url` / `apply.url` (Hellowork → Hellowork applier; France Travail → FT/external).
   - `recruteurs_lba` without email → push into the **spontaneous-application pipeline** as a `Company` (source `lba_recruteur`) with SIRET, NAF, size, address and sometimes phone/website. **LBA never exposes recruiter emails** (`hasEmail` boolean only). An email must come from the separate email-finder module, or you use the LBA relay when `hasEmail=true`.
4. **Tracking:** LBA emails the candidate at every step. Parse these subjects over IMAP: "Votre candidature chez {company}", "Suite donnée à votre candidature - {company}", "Réponse négative de {company} à la candidature de …", "Votre candidature n'a pas pu être envoyée à {company}", "Echec d'envoi de votre candidature".
5. **ROME codes changed (ROME 4.0).** For a full-stack dev use `M1855` (Développeur web / full-stack), `M1805` (Développeur informatique), `M1827` (DevOps / ingénieur dev logiciel), `M1818` (Ingénieur d'étude informatique), `M1836` (Ingénieur concepteur informatique), `M1852` (Analyste d'étude informatique), `M1853` (Chef de projet études & dev), and optionally `M1811` (Data engineer). These were live-verified via `/api/_private/metiers/intitule`.
6. **1jeune1solution:** there is nothing to integrate. `https://www.1jeune1solution.gouv.fr/apprentissage` now **308-redirects to LBA** (`/1jeune1solution` landing), and its alternance data comes from LBA / API Apprentissage anyway.
7. **Legacy:** `labonnealternance…/api/V1/jobs`, `/api/v1/jobs`, `/api/V1/jobsEtFormations`, `/api/V1/formations` and `/api/V1/application` all return **404**, so they are retired. Don't implement them. The old francetravail.io "API La Bonne Alternance v1" listing is obsolete.

---

## 2. Endpoints / URLs & auth

### 2.A Official API Apprentissage (path A)

- **Base URL:** `https://api.apprentissage.beta.gouv.fr/api`. It is the same base URL for sandbox and production; the key type decides the environment.
- **OpenAPI 3.1 spec (JSON):** `GET https://api.apprentissage.beta.gouv.fr/api/documentation/json` (also served at `/api/swagger.json`), 117 660 bytes, `info.version = "256bb22"` — LIVE.
- **Human docs:**
  - `https://api.apprentissage.beta.gouv.fr/fr/documentation-technique`
  - `https://api.apprentissage.beta.gouv.fr/fr/explorer/recherche-offre`
  - `https://api.apprentissage.beta.gouv.fr/fr/explorer/candidature-offre`
- **Healthcheck (no auth):** `GET /api/healthcheck` → `200 {"name":"Espace développeurs La bonne alternance","version":"256bb22","env":"production"}` — LIVE.
- **Status page:** `https://mission-apprentissage.github.io/upptime/history/api-apprentissage-api`

#### Getting a key (manual, one-time — do NOT automate)

1. Go to `https://api.apprentissage.beta.gouv.fr/fr` and click "Se connecter / S'inscrire". Login is an **email magic link** (server route `POST /_private/auth/login-request {email}`, then `POST /_private/auth/login`).
2. First login asks you to register: `prenom`, `nom`, `description` (describe the usage), `cgu: true`, and `type` ∈ `operateur_public | organisme_formation | entreprise | editeur_logiciel | apprenant | autre` (with `other_type` when `autre`). **`apprenant` fits the candidate.** Source: `api-apprentissage/shared/src/models/user.model.ts`.
3. Go to `https://api.apprentissage.beta.gouv.fr/fr/compte/profil` and click "Générer un jeton". Choose a name and `env`:
   - `sandbox`: write habilitations granted automatically, but LBA exchanges (apply) go to `https://labonnealternance-recette.apprentissage.beta.gouv.fr`.
   - `production`: read endpoints work at once; write habilitations (`applications:write`, `jobs:write`, `appointments:write`) only "on request to support".
4. The key is a **JWT valid 365 days** (`config.api_key.expiresIn = 365*24*60*60*1000`). Warning emails are sent at 30 and 15 days before expiry. Store it in `.env` as `JOBBOT_LBA_API_KEY`.

#### Auth header

```
Authorization: Bearer <api_key>
```

The regex is `/^bearer\s+(\S+)$/i`. Error bodies observed live:

- no header → `401 {"statusCode":401,"name":"Unauthorized","message":"Vous devez fournir une clé d'API valide pour accéder à cette ressource"}`
- `Authorization: abc` → `401 {"statusCode":401,"name":"Unauthorized","message":"Le header Authorization doit être de la forme 'Bearer <token>'"}`
- `Authorization: Bearer abc.def.ghi` → `401 {"statusCode":401,"name":"Unauthorized","message":"Impossible de déchiffrer la clé d'API"}`

#### Job endpoints (from OpenAPI; each probed live without a key → 401 JSON, which proves the route exists)

| Method | Path | operationId | Scope needed | Rate limit (per key) |
|---|---|---|---|---|
| GET | `/job/v1/search` | jobSearch | none (any key) | **60/min** |
| GET | `/job/v1/offer/{id}` | jobSearchById | none | 120/min (shared with next) |
| GET | `/job/v1/offer/{id}/publishing-informations` | jobPublisingInfos | none | 120/min |
| GET | `/job/v1/export` | jobsExport | none | **2/min** |
| POST | `/job/v1/apply` | jobApply | `applications:write` | **10/min** |
| POST | `/job/v1/offer` | jobOfferCreate | `jobs:write` | (employer side, irrelevant) |
| PUT | `/job/v1/offer/{id}` | jobOfferUpdate | `jobs:write` | (irrelevant) |

The other routes are also key-protected:

- `GET /certification/v1` (RNCP lookup)
- `GET /geographie/v1/commune/search`, `/geographie/v1/departement`, `/geographie/v1/mission-locale`
- `GET /formation/v1/search`, `/formation/v1/{id}`
- `GET /organisme/v1/recherche`, `/organisme/v1/export`
- `POST /formation/v1/appointment/generate-link`

#### `GET /job/v1/search` query params (exact, from spec)

| Param | Type | Allowed / notes |
|---|---|---|
| `latitude` | number −90..90 | Must be paired with `longitude`. If both are omitted, the search covers the whole of France. |
| `longitude` | number −180..180 | ⚠ The spec *examples* swap lat/lon (`longitude` example `48.8566`). Metz = `latitude=49.1193&longitude=6.1757`. |
| `radius` | number 0..200, default 30 | km |
| `target_diploma_level` | string enum `"3"`,`"4"`,`"5"`,`"6"`,`"7"` | European level at the end of studies. Bac+5 = `7`, Bac+3 = `6`, Bac+2 = `5`. It returns the matching level **plus unknown/unspecified levels**. |
| `romes` | string, comma-separated | e.g. `M1855,M1805,M1827`; each must match `^[A-Z]\d{4}$` |
| `rncp` | string `^RNCP\d{3,5}$` | one RNCP code |
| `opco` | enum | `AFDAS`, `AKTO / Opco entreprises et salariés des services à forte intensité de main d'oeuvre`, `ATLAS`, `Constructys`, `L'Opcommerce`, `OCAPIAT`, `OPCO 2i`, `Opco entreprises de proximité`, `Opco Mobilités`, `Opco Santé`, `Uniformation, l'Opco de la Cohésion sociale` |
| `departements` | array (repeat the key) | `departements=57&departements=54` |
| `partners_to_exclude` | array (repeat the key) | partner labels, e.g. `Hellowork`, `RH Alternance`. The list changes; the spec links to a public Metabase question that returned "Introuvable" (404) on 2026-09-23. |

Sorting is fixed: source priority (LBA > France Travail > other partners), then distance ascending, then creation date descending. **Limit: 150 per source (≤450 jobs) + ≤150 recruiters, no pagination.**

#### `GET /job/v1/export`

Returns `{ "url": "<presigned S3/OVH URL, valid 2 minutes>", "lastUpdate": "<ISO date>" }`. The file is JSON "identical to the search route" structure and is regenerated **once a day at 03:00 Paris time**. Its size is unknown.

#### `POST /job/v1/apply` (documented for completeness — production habilitation refused for individual use)

- Headers: `Authorization: Bearer <key with applications:write>`, `Content-Type: application/json`
- Body (`JobApplicationWrite`, `additionalProperties: false`, all required except `applicant_message`):

```json
{
  "applicant_first_name": "Prénom",            // 1..50 chars, no URLs
  "applicant_last_name": "Nom",                // 1..50 chars, no URLs
  "applicant_email": "prenom.nom@gmail.com",   // format email; disposable ("burner") domains rejected
  "applicant_phone": "+33612345678",           // European mobile or landline only
  "applicant_attachment_name": "CV_Prenom_Nom.pdf",  // regex ((.*?))(\.)+([Dd][Oo][Cc][Xx]|[Pp][Dd][Ff])$
  "applicant_attachment_content": "data:application/pdf;base64,JVBERi0xLjcK...", // maxLength 4 215 276 chars, file ≤ 3 MB
  "applicant_message": "Lettre de motivation / message (nullable, no max length in schema)",
  "recipient_id": "partners_6a003c349a6be614e48aa5db"  // from apply.recipient_id
}
```

- Success: **`202 {"id": "<application id>"}`**. Errors: 400/401/403/404/409, 419/429 (rate limit), 500/502/503. All share the shape `{statusCode, name, message, data?}`.

### 2.B LBA website internal API (path B) — base `https://labonnealternance.apprentissage.beta.gouv.fr/api`

- **Version and health:**
  - `GET /api/version` → `{"version":"1.905.0"}`
  - `GET /api/healthcheck` → `{"name":"La bonne alternance","version":"1.905.0",…,"env":"production","mongo":true,…}` — LIVE.
- The routes are defined with zod in `shared/src/routes/*.ts`. Query objects are **`strictObject`**, so **any unknown query key → 400** (live: `{"statusCode":400,"error":"Bad Request","message":"querystring.: Unrecognized key: \"foo\"",…}`).

#### `GET /api/v1/search` — no auth, rate limit **30 req / 1 s** (header `x-ratelimit-limit: 30`, `x-ratelimit-reset: 1`) — LIVE

| Param | Values |
|---|---|
| `q` | free text ≤200 chars, fuzzy, French analysis (matches title, company, ROME labels, keywords) |
| `mode` | `emplois` (offers **excluding** CFA/GEIQ-delegated offers + companies), `formations`, `emplois_formation` (CFA/GEIQ offers "formation incluse"). If `mode` is omitted, formations are mixed in. |
| `is_algo_company` | `true` = only `recruteurs_lba` (companies to contact spontaneously); `false` = only job offers |
| `smart_apply` | `true` = only items with "candidature simplifiée" (LBA relays the application by email) |
| `level` | **label strings, repeatable** (NOT digits): `CAP, BEP (Infrabac)`, `Bac, Bac Pro, BP (Bac)`, `BTS, DEUST (Bac+2)`, `Licence, BUT, Licence Pro (Bac+3)`, `Master, titre ingénieur, grande école (Bac+5)`. Items with an unknown level are always included. |
| `contract_type` | repeatable: `Apprentissage`, `Professionnalisation` |
| `activity_sector` | repeatable, exact facet label (case-sensitive, e.g. `PROGRAMMATION INFORMATIQUE`, `Conseil en systèmes et logiciels informatiques`, `EDITION DE LOGICIELS APPLICATIFS`) |
| `type_filter_label` | repeatable facet label (`Candidatures spontanées`, `Offres d'emploi partenaires`, …) |
| `organization_name` | exact company name |
| `is_disabled_elligible` | `true`/`false` |
| `start_type` | `des_que_possible` \| `precise_date` |
| `start_date` | ISO date. Returns offers starting ≤ that date, flexible, or with no date (e.g. `2026-10-31`). |
| `sort` | `proximity` \| `date` \| `applications` (ascending application count) \| `start_date`. Default is relevance. |
| `latitude`, `longitude`, `radius` | radius 0..200, default 30 |
| `admin_area` | `departement:57`, `departement:54`, `region:44` (Grand Est). Takes priority over the radius. Must match `^(?:(departement):(0[1-9]\|1[0-9]\|2[AB1-9]\|[3-8][0-9]\|9[0-5]\|97[1-46])\|(region):(0[1-6]\|11\|24\|27\|28\|32\|44\|52\|53\|75\|76\|84\|93\|94))$` |
| `page` | 0-based, default 0 |
| `hitsPerPage` | 1..100, default 20 (101 → 400 "Too big") |
| `search_source` | `suggestion`\|`free_text`\|`training_links`\|`external_sites` (telemetry only). Deprecated alias: `source`. |
| `internal` | `true` = excluded from LBA's search analytics (used by their SSR prefetch) |

Header accepted: `referer` (optional).

#### `GET /api/v1/search/suggest?q=<≥3 chars>&limit=<1..20>` — autocomplete, 60 req/s — LIVE

Response: `{"suggestions":[…]}`.

#### `GET /api/_private/jobs/:source/:id` — detail, no auth, rate limit **5 req / 1 s** (header `x-ratelimit-limit: 5`) — LIVE

- `source` ∈ `recruteurs_lba` | `offres_emploi_lba` | `offres_emploi_partenaires`
- For `recruteurs_lba`, `:id` = **SIRET** (= `url_id` in search hits). The Mongo `_id` gives `404 {"message":"Company not found"}`.
- For offers, `:id` = the `_id` (24-hex ObjectId).
- `recipient_id` `partners_<id>` can also be fetched as `offres_emploi_partenaires/<id>`. This is what the `/postuler` widget does.

#### `GET /api/_private/metiers/intitule?label=<text>` — label → ROME 4.0 codes, no auth — LIVE

Response: `{"coupleAppellationRomeMetier":[{"code_rome","intitule","appellation"}]}`.

#### `POST /api/v2/_private/application` — the site's "Candidature simplifiée" — rate limit **5 / 5 s**, body limit 5 MB

- Headers: `Authorization: Bearer <token>`, where `token` comes from the detail response of **that** item, and `Content-Type: application/json`.
  - Probed live without a token → `401 {"statusCode":401,"error":"Unauthorized","message":"Unauthorized"}`.
  - The token is an HS256 JWT. Decoded payload (live):
    `{"identity":{"type":"application","company_siret":"78887652200038","email":""},"scopes":[{"options":{},"path":"/v2/_private/application","method":"post"}],"iat":1790164142,"exp":1790250542,"iss":"https://labonnealternance.apprentissage.beta.gouv.fr"}`
    It is **valid 24 h**, so fetch the detail right before applying.
- Body = `ZApplicationApiPrivate` (source `shared/src/models/applications.model.ts`, built by `ui/components/ItemDetail/CandidatureLba/services/submit-candidature.ts`):

```json
{
  "applicant_first_name": "Prénom",
  "applicant_last_name": "Nom",
  "applicant_email": "prenom.nom@gmail.com",
  "applicant_phone": "0612345678",
  "applicant_message": "Message au responsable du recrutement (= lettre de motivation)",
  "applicant_attachment_name": "CV_Prenom_Nom.pdf",
  "applicant_attachment_content": "data:application/pdf;base64,....",
  "recipient_id": "partners_6a003c349a6be614e48aa5db",
  "job_searched_by_user": "Développeur / Développeuse web",
  "application_url": "https://labonnealternance.apprentissage.beta.gouv.fr/emploi/recruteurs_lba/78887652200038/efluid",
  "applicant_inscription_formation": true,
  "applicant_contract_duration": "24 mois",
  "applicant_contract_start": ["Octobre 2026"],
  "applicant_formation_description": "Exemple Digital School — Bac+5 … — Metz (≤200 chars)",
  "applicant_rythm_description": "ex: 1 semaine école / 3 semaines entreprise (≤200 chars)"
}
```

Field notes:

- `caller` is optional. The UI sets it only inside the `/postuler` widget, and it triggers a limit of 20 applications per SIRET per caller per day. **Omit it.**
- `applicant_contract_duration` options in the UI: `6 mois`, `12 mois`, `18 mois`, `24 mois`, `36 mois`.
- `applicant_contract_start` options: `Dès que possible` plus the next 12 months formatted `"<Mois> YYYY"` in French with a capital letter (e.g. `Octobre 2026`).
- `applicant_formation_description` and `applicant_rythm_description` are only sent when `applicant_inscription_formation === true`.

Success: `200 {}`. The v2 public route returns `202 {"id"}`.

#### Public pages (for Playwright / human links) — LIVE (HTTP 200)

- Detail: `https://labonnealternance.apprentissage.beta.gouv.fr/emploi/{type}/{id}/{slug}`
  - e.g. `/emploi/recruteurs_lba/78887652200038/efluid` (id = SIRET)
  - e.g. `/emploi/offres_emploi_partenaires/6a064773687b65f199a02f5f/alternant-developpeur-web-fullstack`
  - The slug seems free-form (any slug returned 200).
- Search UI: `/recherche?q=…&latitude=…&longitude=…&radius=…&mode=emplois&…` (same param names as the API). The old `/recherche-apprentissage?romes=…` → 308 to `/recherche?romes=…`.
- Apply widget (intended for partner-site iframes): `/postuler?caller=<name>&recipient_id=partners_<id>` (alternatively `type=` + `itemId=`). `caller` is mandatory there.
- robots.txt disallows only: `/test-widget`, `/recherche-formation`, `/recherche-emploi`, `/espace-pro`, `/1jeune1solution`, `/1jeune1solution-recruteurs`. `/api/*`, `/recherche` and `/emploi/*` are not disallowed.

#### LBA-host routes you should NOT call directly

`/api/v3/jobs/search` on the LBA host exists (live `401 {"message":"Unable to parse token missing-bearer"}`). It is the internal backend behind API Apprentissage and needs an api-apprentissage-issued token.

### 2.C 1jeune1solution

- `https://www.1jeune1solution.gouv.fr/apprentissage` and `/apprentissage/recherche` → **308** to `https://labonnealternance.apprentissage.beta.gouv.fr/1jeune1solution?utm_source=1j1s&utm_medium=website&utm_campaign=landinglba1j1s` — LIVE.
- `/api/alternances?...` → 404. `/api/alternances/{id}` exists but returned `404 {"error":"CONTENU_INDISPONIBLE"}` for an LBA id — LIVE.
- The 1j1s-front code (`DNUM-SocialGouv/1j1s-front`) consumes LBA / API Apprentissage (`/v1/jobs?caller=…`, `/v1/jobs/matcha/{id}`, plus a newer `apiAlternance` repository).
- `/emplois`, `/stages`, `/jobs-etudiants` are 200 but are not alternance-specific (France Travail-based).

**Conclusion:** there is no independent alternance source there, so skip it.

---

## 3. Response / page shape

### 3.1 Official `GET /job/v1/search` → `{ jobs: JobOfferRead[], recruiters: JobRecruiter[], warnings: {message, code}[] }` (all 3 keys required)

`JobOfferRead` (all keys present, nullable where marked):

```
identifier.id                 string|null   LBA id; null for France Travail (fetched on the fly)
identifier.partner_job_id     string        id in the partner system
identifier.partner_label      string        "offres_emploi_lba" | "recruteurs_lba" | partner name ("France Travail", "Hellowork", ...)
workplace.name|brand|legal_name  string|null
workplace.description         string|null
workplace.website             uri|null
workplace.siret               string(14)|null  (school's SIRET if is_delegated)
workplace.size                string|null   e.g. "100-199"
workplace.location.address    string
workplace.location.geopoint   {type:"Point", coordinates:[lon, lat]}   ← GeoJSON order
workplace.domain.idcc         number|null
workplace.domain.opco         string|null
workplace.domain.naf          {code, label|null}|null
apply.url                     uri   redirect URL (LBA detail page, partner page, FT page)
apply.phone                   string|null
apply.recipient_id            string|null   (optional key) → POST /job/v1/apply; null = cannot apply via LBA
contract.start                date-time|null
contract.duration             integer|null (months)
contract.type                 ["Apprentissage"|"Professionnalisation"]
contract.remote               "onsite"|"remote"|"hybrid"|null
offer.title                   string
offer.description             string
offer.desired_skills          string[]
offer.to_be_acquired_skills   string[]
offer.access_conditions       string[]
offer.opening_count           number
offer.publication.creation    date-time|null
offer.publication.expiration  date-time|null
offer.rome_codes              string[]
offer.target_diploma          {european:"3".."7", label}|null
offer.status                  "Active"|"Filled"|"Cancelled"  (search returns Active only)
is_delegated                  boolean  true = offer posted by a CFA (school) on behalf of companies
```

`JobRecruiter` = `identifier.id` + the same `workplace.*` + `apply.{url, phone, recipient_id}`. There is **no email field anywhere.**

Schema-derived example (not live — no key available):

```json
{
  "jobs": [{
    "identifier": {"id": "6a064773687b65f199a02f5f", "partner_label": "Hellowork", "partner_job_id": "79006307"},
    "workplace": {"name": "Chausséa", "siret": "33026769100377", "size": "2 000 à 4 999 salariés", "website": null,
      "location": {"address": "57000 Metz", "geopoint": {"type": "Point", "coordinates": [6.194897, 49.108385]}},
      "domain": {"idcc": null, "opco": "L'Opcommerce", "naf": {"code": "47.72A", "label": "Commerce de détail de la chaussure"}},
      "brand": null, "legal_name": null, "description": "…"},
    "apply": {"url": "https://www.hellowork.com/fr-fr/emplois/79006307.html?...", "phone": null, "recipient_id": null},
    "contract": {"start": null, "duration": 24, "type": ["Apprentissage"], "remote": "onsite"},
    "offer": {"title": "Alternant - Developpeur Web Fullstack H/F", "rome_codes": ["M1855"], "status": "Active",
      "target_diploma": {"european": "7", "label": "Master, titre ingénieur, grande école (Bac+5)"},
      "publication": {"creation": "2026-09-13T20:05:40.000Z", "expiration": "2026-11-13T20:05:40.000Z"},
      "description": "…", "desired_skills": [], "to_be_acquired_skills": [], "access_conditions": [], "opening_count": 1},
    "is_delegated": false
  }],
  "recruiters": [{ "identifier": {"id": "…"}, "workplace": { "siret": "78887652200038", "name": "EFLUID", "…": "…" },
                   "apply": {"url": "https://labonnealternance…", "phone": null, "recipient_id": "partners_6a003c349a6be614e48aa5db"} }],
  "warnings": []
}
```

(The values come from the live path-B data for the same items; the `partner_job_id`/`recipient_id` mapping for Hellowork items is UNCERTAIN.)

### 3.2 Path B `GET /api/v1/search` → LIVE sample (trimmed)

`GET /api/v1/search?q=developpeur&latitude=49.1193&longitude=6.1757&radius=30&hitsPerPage=20` →

```json
{
  "hits": [
    {
      "_id": "6a003c199a6be614e489356f",
      "url_id": "85190096900011",
      "type": "offre",
      "type_filter_label": "Candidatures spontanées",
      "sub_type": "recruteurs_lba",
      "contract_type": ["Apprentissage", "Professionnalisation"],
      "publication_date": "2026-09-20T08:00:50.157Z",
      "is_disabled_elligible": false, "start_date": null, "start_type": null, "is_start_flexible": null,
      "is_algo_company": true, "is_formation_included": false,
      "smart_apply": true, "application_count": 38,
      "title": "LORCA SERVICES", "description": "",
      "address": "rue de Metz 57580 LEMUD",
      "location": {"type": "Point", "coordinates": [6.363160999999999, 49.038056999986715]},
      "departement_code": "57", "region_code": "44",
      "organization_name": "LORCA SERVICES", "level": "",
      "activity_sector": "EDITION DE LOGICIELS APPLICATIFS",
      "keywords": ["Responsable programmatique", "Développeur web", "Développeuse web"],
      "rome_labels": ["Responsable programmatique", "Développeur / Développeuse web", "…"],
      "preview": [{"type": "text", "value": "[...]"}, {"type": "hit", "value": "Développeur"}],
      "matched_words": [{"word": "développeur", "count": 2}],
      "distance": 17
    },
    {
      "_id": "6a064773687b65f199a02f5f", "url_id": "6a064773687b65f199a02f5f",
      "type": "offre", "type_filter_label": "Offres d'emploi partenaires", "sub_type": "offres_emploi_partenaires",
      "contract_type": ["Apprentissage", "Professionnalisation"], "publication_date": "2026-09-13T20:05:40.000Z",
      "is_algo_company": false, "smart_apply": false, "application_count": 0,
      "title": "Alternant - Developpeur Web Fullstack H/F",
      "description": "Tu es passionné(e) par le développement web …",
      "address": "57000 Metz", "location": {"type": "Point", "coordinates": [6.194897, 49.108385]},
      "departement_code": "57", "region_code": "44", "organization_name": "Chausséa",
      "level": "Master, titre ingénieur, grande école (Bac+5)",
      "activity_sector": "Commerce de détail de la chaussure",
      "keywords": ["alternant", "développeur web", "fullstack", "…"],
      "rome_labels": ["Développeur / Développeuse web"], "distance": 2
    }
  ],
  "nbHits": 37, "page": 0, "nbPages": 2,
  "facets": {
    "type": {"offre": 20, "formation": 17},
    "sub_type": {"recruteurs_lba": 19, "formation": 17, "offres_emploi_partenaires": 1},
    "type_filter_label": {"Candidatures spontanées": 19, "Formation en présentiel": 17, "Offres d'emploi partenaires": 1},
    "contract_type": {"Apprentissage": 20, "Professionnalisation": 20},
    "level": {"Licence, BUT, Licence Pro (Bac+3)": 9, "BTS, DEUST (Bac+2)": 5, "Master, titre ingénieur, grande école (Bac+5)": 4},
    "activity_sector": {"Conseil en systèmes et logiciels informatiques": 7, "PROGRAMMATION INFORMATIQUE": 6, "…": 0},
    "organization_name": {"…": 0}
  },
  "counts": {"is_disabled_elligible": 0, "urgent": 0, "smart_apply": 3}
}
```

Formation hits (`type:"formation"`, `sub_type:"formation"`) appear unless `mode=emplois`. The union of hit keys is:
`_id, activity_sector, address, application_count, contract_type, departement_code, description, distance, is_algo_company, is_disabled_elligible, is_formation_included, is_start_flexible, keywords, level, location, matched_words, organization_name, preview, publication_date, region_code, rome_labels, smart_apply, start_date, start_type, sub_type, title, type, type_filter_label, url_id`.

**Volumes observed on 2026-09-23 (for sizing):**

- `mode=emplois`, Metz, radius 30 → **2 839 hits** (`recruteurs_lba` 2 668, `offres_emploi_partenaires` 144, `offres_emploi_lba` 27); 1 559 of them `smart_apply`.
- `mode=emplois&is_algo_company=false`, Metz, radius 30 → **171 job offers** (all sectors). Only ~2 are clearly dev jobs.
- `mode=emplois&is_algo_company=false&admin_area=region:44` → 979 offers (805 partner, 174 LBA).
- `q=developpeur&mode=emplois&admin_area=departement:57` → 22 (21 companies, 1 offer).
- `q=informatique&mode=emplois&smart_apply=true&is_algo_company=true&admin_area=departement:57` → 16 companies.
- Deep pagination: `admin_area=region:44&hitsPerPage=100&page=150` still returns 100 hits (nbHits 23 243).

⇒ For a Metz dev apprentice, **LBA's value is mostly `recruteurs_lba` (spontaneous applications, many with one-click relay)**. Actual dev job offers are few and often duplicated from Hellowork / France Travail.

### 3.3 Path B detail `GET /api/_private/jobs/{source}/{id}` — LIVE samples (token redacted)

`recruteurs_lba/78887652200038`:

```json
{
  "ideaType": "recruteurs_lba", "status": "Active", "id": "78887652200038", "title": "EFLUID",
  "contact": {"hasEmail": true, "phone": null},
  "place": {"distance": null, "fullAddress": "2B RUE ARDANT DU PICQ 57000 METZ", "latitude": 49.1288939999869,
            "longitude": 6.174426, "city": "METZ", "address": "2B RUE ARDANT DU PICQ 57000 METZ"},
  "company": {"name": "EFLUID", "siret": "78887652200038", "size": "250-499", "url": null,
              "opco": {"label": "OPCO 2i", "url": null}, "elligibleHandicap": false, "hiringCount3Years": 4},
  "url": null,
  "nafs": [{"code": "58.29C", "label": "Edition de logiciels applicatifs"}],
  "applicationCount": 67,
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.<redacted>",
  "recipient_id": "partners_6a003c349a6be614e48aa5db"
}
```

(`recruteurs_lba/88787449300018` ATIXYS → `"contact":{"hasEmail":false,"phone":null}`, `smart_apply:false` in search. The UI shows no apply form in that case.)

`offres_emploi_partenaires/6a064773687b65f199a02f5f` (Hellowork-sourced) — flattened keys:

```
ideaType="offres_emploi_partenaires"; title; contact.{email:"", hasEmail:false, phone:null, url:"https://www.hellowork.com/fr-fr/emplois/79006307.html?utm_…"}
place.{distance, fullAddress:"57000 Metz", latitude, longitude, numberAndStreet, city:"Metz", zipCode:"57000", remoteOnly:false}
company.{name:"Chausséa", siret:"33026769100377", size:"2 000 à 4 999 salariés", url, opco.{label,url}, elligibleHandicap, isGeiq:false, hiringCount3Years:62}
id; target_diploma_level:"Master, titre ingénieur, grande école (Bac+5)"
job.{description, employeurDescription, creationDate, id:"202a76a3-…", jobStartDate:null, jobExpirationDate:"2026-11-13T20:05:40.000Z",
     elligibleHandicap, dureeContrat:"24 mois", quantiteContrat:1, status:"Active", type:["Apprentissage"], partner_label:"Hellowork",
     offer_desired_skills[], offer_to_be_acquired_skills[], offer_access_conditions[], contract_rythm, startType, startDateFlexible, isCfaEntreprise}
romes[{code:"M1855", label:null}]; nafs[{code:"47.72A", label}]; token; recipient_id:"partners_6a064773687b65f199a02f5f"
```

Note: `contact.email` is always `""`. The real address is never exposed. `hasEmail:false` together with `contact.url` means "apply on the partner site".

### 3.4 Detail page HTML (LIVE, SSR) — apply button

```html
<div data-testid="candidater-recruteurs_lba"> … <button aria-label="Ouvrir le formulaire d'envoi de candidature"
     data-testid="postuler-button">Candidature simplifiée</button>
```

The partner offer page without email renders an `<a href="https://www.hellowork.com/...">` instead, with no `postuler-button`.

Application modal (source `ui/components/ItemDetail/CandidatureLba/CandidatureLbaModalBody.tsx`):

- Inputs use `id` = `name` = `data-testid` = the field name: `applicant_last_name`, `applicant_first_name`, `applicant_email`, `applicant_phone`, `applicant_message` (textarea "Message au responsable du recrutement").
- Radio `applicant_inscription_formation`: "Je suis déjà inscrit(e) en formation" / "Je ne suis pas encore inscrit(e) en formation".
- Select `applicant_contract_duration`, multiselect `applicant_contract_start`, textareas `applicant_formation_description` and `applicant_rythm_description`.
- Possible recruiter questions render as extra required textareas.
- File: `[data-testid="fileDropzone"] input[type=file]` (accepts pdf/docx, `maxSize: 3145728`).
- Submit: `button[type=submit][data-testid="candidature-not-sent"]`, aria-label "Envoyer la candidature" (offers) or "Envoyer la candidature spontanée" (companies).
- While sending: `[data-testid="candidature-currently-sending"]`.
- Success: heading **"Votre candidature a bien été envoyée à {company}"**.

---

## 4. Limits, ToS, legal

**API Apprentissage (path A)**

- **Rate limits per key:** search 60/min, offer detail 120/min, export 2/min, apply 10/min.
  - Headers: `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset`, and `retry-after` on overflow.
  - The doc text says **429**, but the OpenAPI `TooManyRequests` schema has `statusCode` enum **419**. Treat both as rate-limit errors.
  - Higher limits are available on request: contact-api@labonnealternance.apprentissage.beta.gouv.fr.
- **Usage terms (explorer page):**
  - "L'utilisation de cette API est gratuite et réservée à des usages non lucratifs… toute utilisation de ces données à des fins commerciales, telles que la revente… est interdite." A personal job search is fine.
  - Apply API: "Cette API est réservée aux services traitant un volume important de candidatures. **Les demandes d'habilitation pour un usage individuel ne seront pas accordées.**"
- **CGU** (`/cgu`):
  - Registration is "gratuite et ouverte à tous".
  - Keep the token secret. Do not share it.
  - Do not commercialise the data or pass it to third parties.
  - Accounts can be suspended.
  - False declarations fall under art. 441-1 Code pénal.
- **Licence of docs/data:** Etalab-2.0 (per the OpenAPI `info.license`).

**LBA website (path B)**

- **Server-side rate limits (fastify-rate-limit, per client IP by default):**
  - `/api/v1/search` 30/s
  - `/api/v1/search/suggest` 60/s
  - `/api/_private/jobs/*` 5/s
  - `/api/v2/_private/application` 5 per 5 s
- **Business limits in `server/src/services/application.service.ts`:**
  - `MAX_CANDIDATURES_PAR_CANDIDAT_PAR_JOUR = 100`. The check is `count > 100` for the applicant email per calendar day, so it throws 429 `TOO_MANY_APPLICATIONS_PER_DAY`.
  - `MAX_MESSAGES_PAR_OFFRE_PAR_CANDIDAT = 3`. It counts **all-time** applications by the same applicant to the same `job_id`, or to the same SIRET for `recruteurs_lba`. At ≥3 → 429 `TOO_MANY_APPLICATIONS_PER_OFFER`.
  - `MAX_MESSAGES_PAR_SIRET_PAR_CALLER = 20` per day, only when `caller` is set.
  - `MAX_APPLICATIONS_PER_OFFER = 80`. When an offer (not a recruteurs_lba company) reaches 80 applications, it is **auto-cancelled**.
  - Disposable email domains → 400 `BURNER`.
  - Offer not Active or expired → 400 `EXPIRED`.
  - No `apply_email` on the job (not a Taleez API partner) → 400 `INTERNAL_EMAIL`.
  - Unknown job → 400 `NOTFOUND`.
  - Wrong file → 400 `FILE_TYPE_NOT_SUPPORTED`.
- **CGU LBA** (`/conditions-generales-utilisation`):
  - The user must comply with law and third-party rights, "Ne créer qu'un seul compte", and only send "informations, fichiers et autres contenus conformes à la réalité, honnêtes et loyaux", with nothing contrary to the purpose of the platform.
  - DGEFP may suspend access.
  - **There is no explicit anti-automation or anti-scraping clause.** The internal `_private` routes are not a public contract.
  - Operator: DGEFP (Ministère du Travail).
- **Privacy policy** (`/politique-de-confidentialite`):
  - Candidate data is kept **24 months** from collection.
  - "Les curriculums vitae (CV) sont toutefois supprimés immédiatement après la transmission de la candidature au recruteur."
  - ⚠ The code has a job purging CVs after 1 year (`purge-application-cv-files.ts`) and a field `applicant_attachment_deleted_at`, so the policy and the code differ.
- **GDPR / CNIL for jobbot:**
  - Storing recruiter data locally (company names, SIRET, sometimes a phone number for sole traders = personal data) for your own job search falls under the **household exemption** (GDPR art. 2(2)(c)) as long as it is not redistributed. Keep the SQLite DB private.
  - Applications sent through LBA are e-mailed **by LBA** to the recruiter. This is not the candidate doing email prospecting, so no LCEN/B2B-prospecting issue on jobbot's side.
  - Only send truthful information. Tailoring the CV is fine; inventing experience conflicts with the "loyaux" clause and with Code pénal 441-1 in the API CGU.

---

## 5. Gotchas & anti-bot measures

1. **No Cloudflare, captcha or JS challenge observed.** Plain `curl` with a UA got JSON at once from the site API and SSR HTML from pages. The only protection is the per-IP rate limit (429 from fastify-rate-limit), with `x-ratelimit-*` headers on every response.
2. **Unknown query params → 400** on path B (`strictObject`). Don't send `caller`, `romes`, `insee`… to `/api/v1/search`; use only the listed params.
3. **`level` filter in path B takes label strings.** `level=7` silently returns only "unknown level" items (live: 489 vs 515 with the label).
4. **Lat/lon:**
   - The OpenAPI examples swap latitude and longitude.
   - Every `geopoint` / `location` is GeoJSON `[lon, lat]`.
   - The path-B detail exposes `place.latitude` / `place.longitude` explicitly.
5. **ROME 4.0:** the old `M1805` is now "Développeur informatique", and web/full-stack is **`M1855`**. Search with the whole set (§1.5).
6. **Official search is capped:** 150 per source, 150 recruiters, no paging. Split by city (Metz, Thionville, Nancy; radius 20–30) and by ROME, then dedupe on `identifier.id` / SIRET.
7. **France Travail offers in path A:**
   - `identifier.id = null`, and (probably) `recipient_id = null`, so apply via `apply.url` on the FT site.
   - They also appear in the FT API. Dedupe with the FT source on `partner_job_id` (7-char FT id).
8. **Hellowork (and other partner) offers are re-published inside LBA.** Detail: `job.partner_label = "Hellowork"`, `contact.url` = hellowork URL with UTM, id `79006307` inside the URL. Dedupe against jobbot's own Hellowork source by the numeric id in the URL. Optionally pass `partners_to_exclude=Hellowork` on path A.
9. **`recipient_id` format:**
   - It is `"partners_<24-hex ObjectId>"` or `"recruiters_<id>"`. The server splits on `_` and validates the ObjectId.
   - For `recruteurs_lba` the recipient is `partners_<_id>`, NOT the SIRET.
   - Path-B detail for companies is addressed by SIRET (`url_id`). The `_id` gives 404.
10. **Apply token lifetime:** 24 h (`exp - iat = 86400`). Always fetch the detail → token → POST in the same run.
11. **Attachment validation (server):**
    - The string MUST start with exactly `data:application/pdf;base64,` or `data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,`, and the prefix must match the file-name extension.
    - The decoded bytes must be detected as the same type (`file-type` magic bytes).
    - `.doc`, `.odt` and images are rejected.
    - The whole string (prefix included) must be ≤ 4 215 276 chars, so the raw file should be ≤ ~3.0 MB. The UI caps at 3 145 728 bytes.
12. **Phone:** only European mobile or landline numbers. The UI error says to use "format international (+33XXX...) ou national (06XXX...)".
13. **Names cannot contain URLs** (`withoutUrls`). Max 50 chars.
14. **Applications are asynchronous.** A 200/202 only means "accepted and stored". A worker then runs the ClamAV scan and sends the emails. Failures reach the candidate by email ("Echec d'envoi de votre candidature", "Votre candidature n'a pas pu être envoyée à …" = recruiter hard bounce).
15. **`mode=emplois` excludes CFA/GEIQ "formation incluse" offers.** Those offers (`mode=emplois_formation`, `is_delegated=true` in path A) usually require enrolling in *that* school. The candidate is already enrolled at Exemple Digital School, so they are generally a poor fit. Keep them only as low-priority.
16. **Luxembourg is not covered.** Use other sources for LU.
17. **Encoding:** all responses are UTF-8 JSON. Garbled accents seen in a Windows console are a terminal artefact.
18. **Undocumented routes (`/api/v1/search`, `/api/_private/*`, `/api/v2/_private/application`) can change at any release.** LBA deploys often (v1.905.0). Wrap the parsing in pydantic models with `extra="ignore"`. Fail soft with `SourceUnavailable` / `NEEDS_MANUAL`.
19. **httpx 419:** jobbot's `RETRY_STATUSES` (`src/jobbot/http.py`) = `{429,500,502,503,504}`. Add **419** for API Apprentissage.
20. **Empty descriptions:** `recruteurs_lba` hits have `description: ""` and `level: ""`. Use `rome_labels`, `activity_sector` (NAF label), `company.size` and `hiringCount3Years` for scoring and letter personalisation.

---

## 6. Concrete Python implementation notes (sync httpx / Playwright sync)

### 6.1 Constants

```python
LBA_SITE = "https://labonnealternance.apprentissage.beta.gouv.fr"
LBA_SITE_API = f"{LBA_SITE}/api"
API_APPRENTISSAGE = "https://api.apprentissage.beta.gouv.fr/api"

CITIES = {  # name: (lat, lon)
    "Metz": (49.1193, 6.1757),
    "Thionville": (49.3579, 6.1683),
    "Nancy": (48.6921, 6.1844),
}
ADMIN_AREAS = ["departement:57", "departement:54"]  # region:44 = Grand Est
ROMES_DEV = ["M1855", "M1805", "M1827", "M1818", "M1836", "M1852", "M1853"]  # + "M1811" data
LEVEL_BAC5 = "Master, titre ingénieur, grande école (Bac+5)"
PDF_PREFIX = "data:application/pdf;base64,"
DOCX_PREFIX = "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
MAX_ATTACHMENT_BYTES = 3_000_000          # safe under 3 145 728 (UI) and 4 215 276 b64 chars (API)
RATE_LIMIT_STATUSES = {419, 429}
```

### 6.2 Path B source (no key) — `jobbot/sources/lba.py`

```python
from collections.abc import Iterator
import httpx
from jobbot.http import HostThrottle, make_client, request_with_retry

_throttle = HostThrottle(min_interval_s=0.5)   # well under 30/s search, 5/s detail

def site_search(client: httpx.Client, *, q: str | None = None, admin_area: str | None = None,
                lat: float | None = None, lon: float | None = None, radius: int = 30,
                is_algo_company: bool | None = None, smart_apply: bool | None = None,
                levels: list[str] | None = None, sort: str = "date",
                hits_per_page: int = 100, max_pages: int = 20) -> Iterator[dict]:
    params: list[tuple[str, str]] = [("mode", "emplois"), ("sort", sort), ("hitsPerPage", str(hits_per_page))]
    if q: params.append(("q", q[:200]))
    if admin_area: params.append(("admin_area", admin_area))
    if lat is not None and lon is not None:
        params += [("latitude", f"{lat}"), ("longitude", f"{lon}"), ("radius", str(radius))]
    if is_algo_company is not None: params.append(("is_algo_company", str(is_algo_company).lower()))
    if smart_apply is not None: params.append(("smart_apply", str(smart_apply).lower()))
    for lvl in levels or []: params.append(("level", lvl))          # repeat key, label strings
    for page in range(max_pages):
        r = request_with_retry(client, "GET", f"{LBA_SITE_API}/v1/search",
                               params=params + [("page", str(page))], throttle=_throttle)
        r.raise_for_status()
        data = r.json()
        yield from data["hits"]
        if page + 1 >= data["nbPages"]:
            break

def site_detail(client: httpx.Client, sub_type: str, url_id: str) -> dict | None:
    # sub_type in {"recruteurs_lba","offres_emploi_lba","offres_emploi_partenaires"}; url_id = SIRET for recruteurs_lba
    r = request_with_retry(client, "GET", f"{LBA_SITE_API}/_private/jobs/{sub_type}/{url_id}", throttle=_throttle)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()
```

Mapping a search hit to `JobOffer` / `Company`:

- `sub_type == "recruteurs_lba"` → `Company(source="lba_recruteur", siren=url_id[:9], name=organization_name, address=address, naf_label=activity_sector, latitude=location.coordinates[1], longitude=location.coordinates[0], raw=hit)`.
  - If `smart_apply`, also create a `JobOffer(source="lba", external_id=url_id, apply_method=ApplyMethod.LBA, contract_type=ALTERNANCE, url=f"{LBA_SITE}/emploi/recruteurs_lba/{url_id}/{slug(title)}")`.
- Otherwise → `JobOffer(source="lba", external_id=_id, title, company=organization_name, description, published_at=publication_date, url=f"{LBA_SITE}/emploi/{sub_type}/{_id}/{slug(title)}", apply_method=LBA if smart_apply else EXTERNAL, contract_type=ALTERNANCE, raw=hit)`.
  - In `fetch_details`, fill `apply_url` from `detail["contact"]["url"]`, the full description from `detail["job"]["description"]` and `detail["job"]["employeurDescription"]`, and the partner from `detail["job"]["partner_label"]`.
- Scoring signals: `application_count` (lower is better), `distance`, `level`, `rome_labels`, and `hiringCount3Years` / `company.size` from the detail.

### 6.3 Path A source (key) — `jobbot/sources/api_apprentissage.py`

```python
def api_search(api_key: str, *, lat: float, lon: float, radius: int = 30, romes: list[str],
               target_diploma_level: str | None = "7", departements: list[str] | None = None,
               partners_to_exclude: list[str] | None = None) -> dict:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    params: list[tuple[str, str]] = [("latitude", str(lat)), ("longitude", str(lon)), ("radius", str(radius)),
                                     ("romes", ",".join(romes))]
    if target_diploma_level: params.append(("target_diploma_level", target_diploma_level))
    for d in departements or []: params.append(("departements", d))
    for p in partners_to_exclude or []: params.append(("partners_to_exclude", p))
    with make_client(headers=headers) as c:
        r = request_with_retry(c, "GET", f"{API_APPRENTISSAGE}/job/v1/search", params=params,
                               throttle=HostThrottle(1.1))            # 60/min
        if r.status_code in (401, 403):
            raise SourceUnavailable("Clé API Apprentissage invalide/expirée (401/403) — régénérer sur /fr/compte/profil")
        r.raise_for_status()
        body = r.json()   # {"jobs": [...], "recruiters": [...], "warnings": [...]}
        for w in body.get("warnings", []):
            log.warning("API apprentissage warning %s: %s", w.get("code"), w.get("message"))
        return body
```

- `available()` → `(bool(settings.lba_api_key), "Clé JOBBOT_LBA_API_KEY absente — créer un compte 'apprenant' sur api.apprentissage.beta.gouv.fr")`.
- Daily bulk mode (optional): `GET /job/v1/export` → download `url` **within 2 minutes** (stream to disk with `client.stream("GET", url)`) → filter locally by `rome_codes` and distance. Call it at most once a day, after 03:30 Paris.
- Keep the rate-limit headers: log `x-ratelimit-remaining`. Sleep `retry-after` on 419/429.

### 6.4 Apply — option 1: Playwright (preferred)

```python
import re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

def lba_apply_playwright(detail_url: str, cand: dict, cv_path: str, message: str, *, headless=False, dry_run=True) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(locale="fr-FR")
        page.goto(detail_url, wait_until="domcontentloaded")
        btn = page.locator('button[data-testid="postuler-button"]')
        if btn.count() == 0:
            return "NO_SMART_APPLY"                      # partner/external offer → other applier
        btn.first.click()
        page.locator("#applicant_last_name").fill(cand["last_name"])
        page.locator("#applicant_first_name").fill(cand["first_name"])
        page.locator("#applicant_email").fill(cand["email"])
        page.locator("#applicant_phone").fill(cand["phone"])
        page.locator('[data-testid="fileDropzone"] input[type="file"]').set_input_files(cv_path)
        page.locator('textarea[name="applicant_message"]').fill(message)
        page.get_by_label("Je suis déjà inscrit(e) en formation").check()
        page.locator("#applicant_contract_duration").select_option("24 mois")      # native <select> assumed
        # recruiter questions (if any) = extra required textareas → return NEEDS_MANUAL if present and unanswered
        if dry_run:
            page.screenshot(path="lba_preview.png", full_page=True)
            return "DRY_RUN"
        page.locator('button[type="submit"][data-testid="candidature-not-sent"]').click()
        try:
            page.get_by_text(re.compile(r"Votre candidature a bien été envoyée à")).wait_for(timeout=30_000)
            return "APPLIED"
        except PWTimeout:
            return "FAILED"
```

Notes:

- `applicant_contract_start` is a custom MultiSelect, not a native select. Skip it, or click the option text "Octobre 2026" after opening it.
- The `applicant_inscription_formation` radio reveals the extra fields, so click it before filling them.
- Selectors come from the source code plus the live SSR HTML. Verify on the first real run in headed mode.

### 6.5 Apply — option 2: direct JSON POST (what the browser sends)

```python
import base64
from pathlib import Path

def build_attachment(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"CV trop lourd ({len(data)} o > 3 Mo)")
    ext = path.suffix.lower()
    if ext == ".pdf" and data[:4] == b"%PDF":
        prefix = PDF_PREFIX
    elif ext == ".docx" and data[:2] == b"PK":
        prefix = DOCX_PREFIX
    else:
        raise ValueError("Seuls .pdf/.docx valides sont acceptés par LBA")
    return path.name, prefix + base64.b64encode(data).decode("ascii")

def lba_apply_http(client: httpx.Client, sub_type: str, url_id: str, cand: dict, cv: Path, message: str,
                   *, formation: str, rythme: str) -> tuple[bool, str]:
    detail = site_detail(client, sub_type, url_id)                       # fresh token (24 h)
    if not detail or not detail.get("recipient_id") or not detail.get("contact", {}).get("hasEmail"):
        return False, "NO_SMART_APPLY"
    name, content = build_attachment(cv)
    payload = {
        "applicant_first_name": cand["first_name"][:50],
        "applicant_last_name": cand["last_name"][:50],
        "applicant_email": cand["email"],
        "applicant_phone": cand["phone"],               # "+33612345678" or "0612345678"
        "applicant_message": message,
        "applicant_attachment_name": name,
        "applicant_attachment_content": content,
        "recipient_id": detail["recipient_id"],
        "application_url": f"{LBA_SITE}/emploi/{sub_type}/{url_id}/candidature",
        "applicant_inscription_formation": True,
        "applicant_contract_duration": "24 mois",
        "applicant_contract_start": ["Octobre 2026"],
        "applicant_formation_description": formation[:200],
        "applicant_rythm_description": rythme[:200],
    }
    r = client.post(f"{LBA_SITE_API}/v2/_private/application", json=payload,
                    headers={"Authorization": f"Bearer {detail['token']}"}, timeout=60)
    if r.status_code == 200:
        return True, "APPLIED"
    # 400 body: {"statusCode":400,"error":"Bad Request","message":"<BusinessErrorCode or zod message>"}
    # 429: TOO_MANY_APPLICATIONS_PER_DAY / _PER_OFFER, or the 5-per-5s route limit
    return False, f"HTTP {r.status_code}: {r.text[:300]}"
```

- Pace it with `Pacer` channel `"lba"`: quota ≤ 25/day, ≥ 60–180 s random pause between applications.
- Before applying, check the SQLite applications table for the same SIRET / `recipient_id`. **Never send a 2nd application** (server cap 3, all-time).
- `applicant_message` doubles as the cover letter (plain text, keep < ~3 000 chars; there is no server max). There is no separate letter attachment: the LBA flow takes **one file only** (the CV). Put the tailored letter in the message body.

### 6.6 Tracking (IMAP parser rules)

| Email subject (from LBA) | jobbot status |
|---|---|
| `Votre candidature chez {company}` | confirm APPLIED (the email includes `reminderDate` = application date + 10 days) |
| `Suite donnée à votre candidature - {company}` (template `mail-candidat-entretien`) | INTERVIEW (the recruiter left a message, optionally with email/phone) |
| `Réponse négative de {company} à la candidature de {first} {last}` (template `mail-candidat-refus`) | REJECTED |
| `Votre candidature n'a pas pu être envoyée à {company}` (template `mail-candidat-hardbounce`) | BOUNCED |
| `Echec d'envoi de votre candidature` | FAILED (virus scan / technical) → retry once manually |

Follow-up ("relance"):

- LBA does not relay candidate follow-ups.
- After ~10 days without an answer, for `recruteurs_lba` use the company phone (if `apply.phone` or `contact.phone` is set) or the email found by the email-finder module.
- LBA itself has recruiter-side reminder jobs (`relance-*`), but they don't expose a candidate hook.

### 6.7 Tests (respx)

- Mock `GET …/api/v1/search` with the live sample in §3.2. Mock `GET …/_private/jobs/recruteurs_lba/78887652200038` with §3.3.
- Assert that:
  - params are repeated for `level`;
  - no unknown keys are sent;
  - 419/429 trigger a retry;
  - the attachment prefix matches the extension;
  - `.doc` is rejected.

---

## 7. Claims table

| # | Claim | Status | Source |
|---|---|---|---|
| 1 | API base `https://api.apprentissage.beta.gouv.fr/api`; `/api/healthcheck` 200 with version `256bb22` | LIVE-VERIFIED | curl 2026-09-23 |
| 2 | OpenAPI JSON at `/api/documentation/json` and `/api/swagger.json` (117 660 B, OpenAPI 3.1.0) | LIVE-VERIFIED | curl |
| 3 | `GET /api/job/v1/search`, `/job/v1/export`, `/job/v1/offer/{id}`, `POST /job/v1/apply`, `/geographie/v1/commune/search` → 401 "Vous devez fournir une clé d'API valide…" without a key | LIVE-VERIFIED | curl |
| 4 | Auth = `Authorization: Bearer <key>`; malformed header and invalid JWT error messages as quoted | LIVE-VERIFIED | curl + `api-apprentissage/server/src/services/security/authenticationService.ts` |
| 5 | Keys are JWTs valid 365 days; env `sandbox`/`production` chosen at creation; expiry warnings at 30/15 days | DOC-SOURCED | github.com/mission-apprentissage/api-apprentissage (`server/src/config.ts`, `users.actions.ts`, `apiKeyExpiration.notifier.ts`) |
| 6 | Account via email magic link; user type `apprenant` allowed; registration is free and open to all | DOC-SOURCED | api-apprentissage `shared/src/routes/_private/auth.routes.ts`, `user.model.ts`; https://api.apprentissage.beta.gouv.fr/cgu |
| 7 | Search params, enums and limits (150/source, 150 recruiters, no pagination), sort order | DOC-SOURCED | OpenAPI `/api/documentation/json` |
| 8 | Rate limits: search 60/min, offer 120/min, export 2/min, apply 10/min; headers `x-ratelimit-*`, `retry-after` | DOC-SOURCED | OpenAPI descriptions |
| 9 | Rate-limit status is 429 in the text but 419 in the schema enum | DOC-SOURCED | OpenAPI (`components.responses.TooManyRequests`) |
| 10 | `JobOfferRead` / `JobRecruiter` field names; no email field; `recipient_id` nullable | DOC-SOURCED | OpenAPI `components.schemas` |
| 11 | Apply payload fields, `additionalProperties:false`, data-URL base64, pdf/docx only, ≤3 MB, 202 `{id}` | DOC-SOURCED | OpenAPI `JobApplicationWrite`; https://api.apprentissage.beta.gouv.fr/fr/explorer/candidature-offre |
| 12 | Production apply habilitation refused for individual use | DOC-SOURCED (quoted) | https://api.apprentissage.beta.gouv.fr/fr/explorer/candidature-offre |
| 13 | Sandbox key routes apply/jobs to `labonnealternance-recette.apprentissage.beta.gouv.fr` | DOC-SOURCED | OpenAPI `info.description` |
| 14 | API usage free, non-commercial only | DOC-SOURCED | https://api.apprentissage.beta.gouv.fr/fr/explorer/recherche-offre |
| 15 | Export = presigned URL valid 2 min, refreshed daily 03:00 Paris | DOC-SOURCED | OpenAPI `JobOfferExport` |
| 16 | Legacy `labonnealternance…/api/V1/jobs`, `/v1/jobs`, `/V1/jobsEtFormations`, `/V1/formations`, `/V1/application` → 404 | LIVE-VERIFIED | curl |
| 17 | LBA host `/api/v3/jobs/search` → 401 "Unable to parse token missing-bearer" (internal backend) | LIVE-VERIFIED | curl |
| 18 | LBA site `/api/v1/search` works without auth, returns hits/nbHits/page/nbPages/facets/counts; rate limit 30/s | LIVE-VERIFIED | curl (headers `x-ratelimit-limit: 30`) + `server/src/http/controllers/search.controller.ts` |
| 19 | `/api/v1/search` rejects unknown params and `hitsPerPage>100` with 400 | LIVE-VERIFIED | curl |
| 20 | `level` filter needs label strings; digit gives unknown-level only | LIVE-VERIFIED | curl (515 vs 489 hits) |
| 21 | `admin_area=departement:57` / `region:44` works; deep pagination to page 150 works | LIVE-VERIFIED | curl |
| 22 | Volumes around Metz (2 839 opportunities / 171 offers within 30 km; few dev offers) | LIVE-VERIFIED | curl 2026-09-23 (changes daily) |
| 23 | Detail `/api/_private/jobs/recruteurs_lba/{SIRET}` returns `contact.hasEmail`, `token`, `recipient_id`; `_id` → 404; rate limit 5/s | LIVE-VERIFIED | curl + `jobs.controller.ts` |
| 24 | Recruiter emails never exposed (`contact.email` = "" / absent) | LIVE-VERIFIED | curl (3 items) + OpenAPI |
| 25 | Apply token = HS256 JWT scoped to `POST /v2/_private/application`, valid 24 h | LIVE-VERIFIED | decoded live token |
| 26 | `POST /api/v2/_private/application` exists (401 without token), rate limit 5/5 s, body limit 5 MB, 200 `{}` | LIVE-VERIFIED (existence) / DOC-SOURCED (limits, payload) | curl + `server/src/http/controllers/v2/application.controller.v2.ts`, `ui/.../submit-candidature.ts` |
| 27 | Server checks data-URL prefix, extension and magic bytes | DOC-SOURCED | `server/src/services/application.service.ts` (`validateApplicationFileType`) |
| 28 | Business limits: 100/day/applicant, 3 per offer/SIRET per applicant (all-time), 20/SIRET/caller/day, offer auto-cancelled at 80 applications, burner emails rejected | DOC-SOURCED | `application.service.ts` constants and `checkUserApplicationCountV2` |
| 29 | Apply button `button[data-testid="postuler-button"]` "Candidature simplifiée" on smart-apply detail pages; partner pages link to the partner site | LIVE-VERIFIED | curl SSR HTML |
| 30 | Modal field ids/names, submit test-id, success text, 3 145 728-byte UI max, contract options | DOC-SOURCED | `ui/components/ItemDetail/CandidatureLba/*.tsx` (GitHub main) |
| 31 | Candidate email subjects for tracking | DOC-SOURCED | `application.service.ts` |
| 32 | ROME 4.0: web/full-stack = M1855, dev informatique = M1805, DevOps = M1827, ingé étude = M1818, ingé concepteur = M1836, analyste = M1852, chef de projet dev = M1853, data engineer = M1811 | LIVE-VERIFIED | `GET /api/_private/metiers/intitule?label=…` |
| 33 | Hellowork offers are republished in LBA (`job.partner_label:"Hellowork"`, `contact.url` hellowork) | LIVE-VERIFIED | curl detail `6a064773687b65f199a02f5f` |
| 34 | France Travail offers fetched on the fly in path A (`identifier.id` null), natureContrat `E2,FS` | DOC-SOURCED | OpenAPI + `server/src/services/ftjob.service.ts` |
| 35 | FT offers have `recipient_id=null` and `apply.url` pointing to francetravail.fr | UNCERTAIN | inferred |
| 36 | 1jeune1solution `/apprentissage` → 308 to LBA; `/api/alternances/{id}` → 404 CONTENU_INDISPONIBLE | LIVE-VERIFIED | curl |
| 37 | robots.txt content (no `/api` disallow) | LIVE-VERIFIED | curl |
| 38 | Privacy: data kept 24 months, CV deleted after transmission (code purges at 1 year) | DOC-SOURCED | https://labonnealternance.apprentissage.beta.gouv.fr/politique-de-confidentialite + `purge-application-cv-files.ts` |
| 39 | LBA CGU has no explicit anti-automation clause; requires truthful, "loyaux" content | DOC-SOURCED | https://labonnealternance.apprentissage.beta.gouv.fr/conditions-generales-utilisation |
| 40 | Partner-label exclusion list (Metabase link) currently 404 "Introuvable" | LIVE-VERIFIED | curl |
| 41 | Whether a production read key is issued instantly without manual review | UNCERTAIN | UI allows choosing `production`; the docs say only *write* habilitations need support |
| 42 | Export file size / whether it includes recruiters | UNCERTAIN | doc says "job postings and companies" |
| 43 | Playwright selectors work unchanged in a real browser session (`#applicant_contract_duration` is a native select) | UNCERTAIN | source code only; verify headed on first run |
| 44 | No bot protection (Cloudflare/captcha) on LBA | LIVE-VERIFIED (for curl at low volume) | curl |
