# Company discovery and email finding for spontaneous applications (candidatures spontanées)

Research date: **2026-09-23**. Every endpoint below was probed live from a French residential connection on that date unless it is tagged otherwise.
Target: **jobbot** (Python 3.12, sync `httpx`, Playwright sync, sqlite). Candidate: junior full-stack developer in Metz (57), also Nancy (54), Thionville, Luxembourg and remote, looking for a 24-month alternance or a junior CDI.

---

## 1. TL;DR and recommendation

**Pipeline to implement.** Every step is free and keyless except the optional ones.

1. **Company discovery: `recherche-entreprises.api.gouv.fr`.** It is keyless and allows 7 req/s per IP.
   - Call `GET /search?activite_principale=<NAF list>&departement=57,54&etat_administratif=A&est_entrepreneur_individuel=false&per_page=25&page=N`.
   - For a radius around Metz, call `GET /near_point?lat=49.1048&long=6.1962&radius=<=50&activite_principale=...` instead.
   - Live counts on 2026-09-23 with the recommended IT NAF list:
     - dept 57: 1,834 active companies
     - dept 57+54: 3,314
     - after excluding sole traders (entrepreneurs individuels): 1,601
     - with at least 6 employees (tranche ≥ 03): 274
   - The API returns **no website, email or phone field**. It does return `dirigeants[]` (names), the headquarters and the matching local establishments.
2. **Geocoding: `https://data.geopf.fr/geocodage/search`** (the Géoplateforme, run by IGN).
   - `api-adresse.data.gouv.fr` still answers, but it sends `Deprecation/Sunset: Sat, 31 Jan 2026` headers and `x-api-new-host: https://data.geopf.fr/geocodage/`. Do not use it.
   - `geo.api.gouv.fr/communes` is a good alternative for city → INSEE code + centre.
3. **Website domain.** Build a candidate list, then verify it. Never trust a candidate without verification.
   1. **Clearbit Autocomplete** (`https://autocomplete.clearbit.com/v1/companies/suggest?query=`). It is keyless and still live on 2026-09-23, but `logo` is now always `null`. It found `yaakadev.com`, `le16solutions.com` and `trustteam.fr/.be` for small French SMEs.
   2. **Slug guessing**: `{slug}.fr/.com/.eu/.io/.tech/.dev` built from `nom_raison_sociale`, `nom_commercial`, `liste_enseignes` and `sigle`, then a DNS A/MX check.
   3. **Wikidata SPARQL** by SIREN (P1616 → P856 official website). Only about 13k companies have both, so it is useful for large ESNs (Sopra Steria, Altran…).
   4. **Search engine fallback**:
      - DuckDuckGo `html.duckduckgo.com/html/` works without a key, **but the IP was bot-flagged (HTTP 202 + `anomaly.js?cc=botnet`) after about 5 queries in about 2 minutes and was still blocked 15 minutes later (11:52 → 12:07 UTC)**. Allow at most 1 query every 20–30 s, with jitter, and only as the last resort.
      - A paid-but-free-credit option for robustness: **Brave Search API** ($5 of free credit per month ≈ 1,000 queries; a card is required) or **Tavily** (1,000 credits/month, no card).
      - Google CSE is closed to new customers and ends on 2027-01-01. The Bing Search API was retired on 2025-08-11. Qwant is behind DataDome.
   5. **Verification (mandatory).** Fetch the homepage and `/mentions-legales`, then accept the domain only if the page contains the company's **SIREN** (9 digits, spaces allowed) or TVA `FR..{SIREN}`. French professional sites must show their RCS/SIREN in the legal notice (LCEN). This was verified live on yaakadev.com and trustteam.eu.
4. **Email extraction.**
   - Parse JSON-LD `email` fields (skipping Person nodes). trustteam.eu exposes its email only in `contactPoint`.
   - Crawl at most 6 pages per domain: the home page, then links whose href or text matches `contact|mentions|legal|recrut|carri|job|rejoindre|join|equipe|team`, then fixed fallback paths.
   - Decode Cloudflare `data-cfemail` / `/cdn-cgi/l/email-protection#hex` (XOR with the first byte; verified live).
   - Normalise `[at]`, `(at)`, `&#64;`, `%40` and `(dot)`.
   - Filter out false positives: `logo@2x.png`, `votre@email.com` (live), Sentry DSNs, CMS author emails in JSON-LD (live: `prenom.nom@agence-exemple.fr` on recrutement.metz.fr), and host or web-agency emails in the legal notice.
   - Keep only same-domain or webmail addresses.
   - Rank the addresses: `recrutement@/rh@/jobs@/carrieres@` > `contact@/bonjour@/hello@` > a named person (dirigeant) > the rest.
   - **If an ATS link is found on the careers page** (jobtoolz, welcometothejungle, teamtailor, lever, recruitee, workable, flatchr, taleez, jobaffinity, digitalrecruiters…), prefer applying through the ATS. It was live-verified that `trustteam.eu` links to `trustteam.jobtoolz.com`.
5. **Nominative email guessing (optional, last resort).**
   - Use `dirigeants[]` where `type_dirigeant == "personne physique"`, **excluding `qualite` containing "Commissaire aux comptes"**. In the live TRUSTTEAM sample, the only natural person was the statutory auditor.
   - Generate patterns in this order: `prenom.nom` > `p.nom` > `pnom` > `prenom` > `prenomnom` > ….
   - **Cannot be verified with SMTP from home.** Outbound TCP/25 timed out from this French residential line (live), while port 587 to smtp.gmail.com answered in 70 ms. All four French ISPs (Orange, Free, SFR, Bouygues) filter port 25 by default.
   - Verify with MX (dnspython 2.8.0 is installed) plus provider classification, and optionally the **Hunter.io Email Verifier**: the free plan gives 50 credits/month and a verification costs 0.5 credit, so about 100 verifications/month.
   - Do not self-host SMTP RCPT probing.

**Paid or freemium email finders, ranked for jobbot:**
1. Hunter (free: 50 credits/month, API included).
2. Dropcontact (50 trial credits; €79/month; GDPR "no database").
3. Snov.io (trial: 50 credits/month, **no API** on trial).
4. Kaspr (free: 15 B2B email credits/month, **no API**, LinkedIn Chrome extension; **fined €240k by the CNIL in Dec 2024 for scraping LinkedIn contact data**).

Only Hunter is worth wiring. The others are UI-only on free plans.

**Legal stance (France).**
- Generic addresses (`contact@`, `rh@`) are not personal data, so they are the safe default.
- Named addresses are personal data under the GDPR. Keep the volume low, send personalised messages with a clear opt-out line, keep minimal data and delete it after 6–12 months.
- Never scrape LinkedIn, societe.com, pappers or pagesjaunes (ToS and database rights).

---

## 2. Endpoints, URLs and auth

### 2.1 API Recherche d'entreprises (DINUM / annuaire-entreprises)

- Base URL: `https://recherche-entreprises.api.gouv.fr` (**no auth, no key**, CORS `*`).
- OpenAPI: `GET https://recherche-entreprises.api.gouv.fr/openapi.json` (200, 52 KB). The UI is at `/docs/`.
- Endpoints: `GET /search` and `GET /near_point`. There are no others.
- Send an explicit `User-Agent`. The docs recommend it: *"Il est recommandé d'inclure un en-tête User-Agent explicite et descriptif"*.

#### `GET /search`: parameters (from the live openapi.json)

**Search mode (`q`):**
- `q` (string): free text over the name, address, dirigeants and elected officials.
- It needs at least 3 characters, otherwise a filter is required. Live error: `"3 caractères minimum pour les termes de la requête (ou utilisez au moins un filtre)"`.
- **If `q` is exactly 9 digits (SIREN) or 14 digits (SIRET), the API does a direct lookup and silently ignores all other filters.**

**Filters:**
- `activite_principale`: NAF code(s), comma-separated. **It applies to the unité légale only, not its establishments.**
  - Only **NAF rév. 2** codes are accepted today. `62.10Y` (NAF 2025) → HTTP 400 with the full list of valid codes (live).
- `departement`: comma list, regex `\b([013-8]\d?|2[aAbB1-9]?|9[0-59]?|97[12346])\b`. It **filters establishments**: an HQ elsewhere matches if any establishment is in 57.
- `code_postal`: 5 digits, comma list. It filters establishments.
- `code_commune`: INSEE code, 5 chars, comma list (Metz = `57463`, Nancy = `54395`, Thionville = `57672`).
- `region`: 2 digits (Grand Est = `44`).
- `epci`: EPCI SIREN, comma list. Metz Métropole = `200039865`, as seen in the live data.
- `etat_administratif`: `A` (active) | `C` (ceased). It applies to the unité légale.
- `tranche_effectif_salarie`: comma list of codes (table below). It applies to the unité légale.
- `categorie_entreprise`: `PME`, `ETI`, `GE` (comma list).
- `nature_juridique`: INSEE legal category codes, comma list (for example `5710` SAS, `5499` SARL, `5720` SASU).
- `section_activite_principale`: `A`..`U`. IT is `J` in NAF rév. 2 and becomes `K` in NAF 2025.
- Boolean filters (`true`/`false`): `est_entrepreneur_individuel`, `est_association`, `est_ess`, `est_organisme_formation`, `est_qualiopi`, `est_societe_mission`, `est_siae`, `est_rge`, `est_bio`, `est_finess`, `est_uai`, `est_patrimoine_vivant`, `est_achats_responsables`, `est_alim_confiance`, `est_administration`, `est_collectivite_territoriale`, `egapro_renseignee`, `convention_collective_renseignee`, `est_entrepreneur_spectacle`.
- `id_convention_collective`: collective agreement ID. **IDCC `1486` = Syntec**, the typical ESN / software agreement, which is a good extra filter.
- `id_finess`, `id_rge`, `id_uai`, `code_collectivite_territoriale`.
- `nom_personne`, `prenoms_personne`, `type_personne` (`dirigeant`|`elu`), `date_naissance_personne_min/max` (`aaaa-mm[-jj]`).
- `ca_min`, `ca_max`, `resultat_net_min`, `resultat_net_max` (int, €).

**Paging and output shape:**
- `page` (default 1), `per_page` (default 10, **max 25**; 26 → 400 `"Veuillez indiquer un paramètre per_page entre 1 et 25"`).
- **`page*per_page` must be ≤ 10,000.** Page 401 × 25 → 400 `"Le nombre total de résultats est restreint à 10 000…"`. To get more, split by `departement`, NAF or `tranche`.
- `limite_matching_etablissements`: 1..100, default 10. `page_etablissements` pages through them.
- `minimal=true` plus `include=complements,dirigeants,finances,matching_etablissements,siege,score,tva`. **`include` only works with `minimal=true`.**
- `sort_by_size=true` sorts by number of establishments. It puts Capgemini, Sopra Steria, Inetum, Altran and CGI first for 57 (live).

#### `GET /near_point`

- `lat` (required), `long` (required, note: **`long`** and not `lon`), `radius` in km (default 5, **0.001–50**; 51 → 400).
- Also accepts `activite_principale`, `section_activite_principale`, `limite_matching_etablissements`, `minimal`, `include`, `page`, `per_page` (≤ 25), `page_etablissements` and `sort_by_size`.
- Live check: `lat=49.1048&long=6.1962&radius=20&activite_principale=<IT list>` → `total_results: 1937`.

#### Tranche d'effectif codes

Source: live GitHub `app/labels/tranches-effectifs.json`.

| code | meaning | code | meaning |
|---|---|---|---|
| `NN` | non-employer unit | `22` | 100–199 |
| `00` | 0 employees at 31/12 (but had some in the year) | `31` | 200–249 |
| `01` | 1–2 | `32` | 250–499 |
| `02` | 3–5 | `41` | 500–999 |
| `03` | 6–9 | `42` | 1,000–1,999 |
| `11` | 10–19 | `51` | 2,000–4,999 |
| `12` | 20–49 | `52` | 5,000–9,999 |
| `21` | 50–99 | `53` | 10,000+ |

Live distribution for `62.01Z` + `departement=57` + `etat_administratif=A` (890 companies):

| tranche | NN | 00 | 01 | 02 | 03 | 11 | 12 | 21 | ≥22 |
|---|---|---|---|---|---|---|---|---|---|
| companies | 796 | 5 | 45 | 12 | 11 | 11 | 8 | 2 | 0 |

**89% are `NN`**, so do not hard-filter on tranche. Use it as a scoring signal. The value is also stale: `annee_tranche_effectif_salarie` is `"2023"`.

#### NAF codes to target (NAF rév. 2 → NAF 2025 observed in live `activite_principale_naf25`)

The companies live in the `activite_principale` field in 57, `etat_administratif=A`.

| NAF rév.2 | label (INSEE) | NAF 2025 seen | dept 57 active | 57+54 with ≥1 employee | use |
|---|---|---|---|---|---|
| **62.01Z** | Programmation informatique | 62.10Y | 890 | 175 | core |
| **62.02A** | Conseil en systèmes et logiciels informatiques (ESN: Capgemini, Sopra Steria, CGI, Inetum, Altran are here, **not** in 62.01Z) | 62.20G | 509 | 177 | core |
| **62.02B** | Tierce maintenance de systèmes et d'applications informatiques | 62.20H | 40 | 9 | core |
| **62.03Z** | Gestion d'installations informatiques | 62.20H | 26 | 17 | core |
| **62.09Z** | Autres activités informatiques | 62.90Y | 84 | 14 | core |
| **63.11Z** | Traitement de données, hébergement et activités connexes | 63.10Y | 90 | 36 | core |
| **63.12Z** | Portails Internet | 63.91Y | 70 | 17 | core |
| **58.21Z** | Édition de jeux électroniques | 58.21Y | 20 | 8 | core |
| **58.29A** | Édition de logiciels système et de réseau | 58.29Y | 17 | 12 | core |
| **58.29B** | Édition de logiciels outils de développement et de langages | 58.29Y | 2 | 3 | core |
| **58.29C** | Édition de logiciels applicatifs | 58.29Y | 86 | 68 | core (SaaS vendors) |
| 73.11Z | Activités des agences de publicité (many web agencies) | 73.11Y | 382 | 122 | secondary |
| 72.19Z | R&D autres sciences physiques et naturelles (deeptech) | 72.10H | 50 | 51 | secondary |
| 70.22Z | Conseil pour les affaires et autres conseils de gestion | 70.20Y | 2,266 | 433 | **noisy**: only with `id_convention_collective=1486` or a keyword `q` |
| 71.12B | Ingénierie, études techniques | 71.12Y | 1,074 | 567 | **noisy**: engineering firms, only with Syntec IDCC or keyword |
| 74.10Z | Activités spécialisées de design | 74.11Y/74.12Y | 572 | 28 | weak |

Recommended default list: `62.01Z,62.02A,62.02B,62.03Z,62.09Z,58.21Z,58.29A,58.29B,58.29C,63.11Z,63.12Z`.

Add `73.11Z,72.19Z` as an option, and `70.22Z,71.12B` only with `id_convention_collective=1486`.

**NAF 2025 becomes official on 2027-01-01** (décret n° 2025-736). The API currently rejects NAF 2025 codes in the filter but already returns `activite_principale_naf25`. **Keep the code list in config**, and on HTTP 400 parse the `erreur` message, which lists the valid values.

### 2.2 Geocoding

**Géoplateforme (IGN), the current service**
- Base: `https://data.geopf.fr/geocodage`. No auth.
- Rate limit: documented as **50 req/s per IP**. The live headers were `ratelimit-limit: 50` and `x-ratelimit-limit-second: 1`.
- `GET /search?q=<text>&type=municipality|street|housenumber|locality&limit=N&postcode=&citycode=&depcode=&city=&lat=&lon=&index=address|poi|parcel&autocomplete=true|false`
- `GET /reverse?lat=&lon=&limit=&type=&index=`
- `GET /getCapabilities`: the full parameter list (JSON, 16.9 KB, live).
- `GET /search/csv` is for batch geocoding.
- Use `autocomplete=0` for backend calls. It defaults to `true`.

**Old API (deprecated)**
- `https://api-adresse.data.gouv.fr/search/` still returns 200 today, with headers:
  - `deprecation: Sat, 31 Jan 2026 22:59:59 GMT`
  - `sunset: …`
  - `x-api-new-host: https://data.geopf.fr/geocodage/`
  - `x-api-notice: L'URL api-adresse.data.gouv.fr va etre decommissionnee…`
- It can disappear at any time.

**geo.api.gouv.fr: commune lookup**
- `GET https://geo.api.gouv.fr/communes?nom=Thionville&fields=code,nom,codesPostaux,centre,codeDepartement,population&boost=population&limit=5`
- `GET https://geo.api.gouv.fr/communes?codePostal=57000&fields=code,nom,centre`
- Live: Metz `57463` centre `[6.1962, 49.1048]`. Thionville `57672` `[6.1443, 49.3718]`.

**Luxembourg** is not covered by SIRENE or the Géoplateforme (see §5).

### 2.3 Domain discovery

| Source | URL / method | Auth | Status 2026-09-23 |
|---|---|---|---|
| **Clearbit Autocomplete** | `GET https://autocomplete.clearbit.com/v1/companies/suggest?query=<name>` | none | **200 live**, `logo:null` since 2025-09-09. `logo.clearbit.com` no longer resolves (curl exit 6) |
| **DuckDuckGo HTML** | `GET https://html.duckduckgo.com/html/?q=<q>` (or POST form `q=…&kl=fr-fr`); `https://lite.duckduckgo.com/lite/` | none | 200 at first, then **202 + anomaly challenge** after a small burst |
| **Brave Search API** | `GET https://api.search.brave.com/res/v1/web/search?q=&country=FR&search_lang=fr&count=20&offset=0..9` | header `X-Subscription-Token` | live 422 `{"error":{"code":"VALIDATION"…"loc":["header","x-subscription-token"],"msg":"Field required"}}`; bad key → 422 `SUBSCRIPTION_TOKEN_INVALID` |
| **Tavily** | `POST https://api.tavily.com/search` JSON `{"query":…,"max_results":5}` | `Authorization: Bearer tvly-…` | live 401 `{"detail":{"error":"Unauthorized: missing or invalid API key."}}` |
| Serper (Google SERP) | `POST https://google.serper.dev/search` JSON `{"q":…,"gl":"fr","hl":"fr"}` | `X-API-KEY` | live 403 `{"message":"Unauthorized.","statusCode":403}`; 2,500 free queries one-time |
| Google Programmable Search JSON | `GET https://www.googleapis.com/customsearch/v1?key=&cx=&q=` | key | live 400 `API_KEY_INVALID`; **closed to new customers, discontinued 2027-01-01** |
| Bing Web Search v7 | `GET https://api.bing.microsoft.com/v7.0/search` | `Ocp-Apim-Subscription-Key` | live 401; **retired 2025-08-11** |
| Qwant | `GET https://api.qwant.com/v3/search/web?...` | — | live: DataDome "Please enable JS" page, unusable |
| **Wikidata SPARQL** | `GET https://query.wikidata.org/sparql?query=…` + `Accept: application/sparql-results+json` | none (UA required) | live; P1616 = SIREN (9 digits, no spaces), P856 = website; about 12,995 items have both |
| OSM Overpass | `POST https://overpass-api.de/api/interpreter` `data=[out:json];…` | none | live 200 (another instance returned 504); in dept 57, 1,869 objects with `ref:FR:SIRET` + `website` (1,480 SIRENs), mostly shops |
| annuaire-entreprises / recherche-entreprises | — | — | **no website/email/phone field** in the OpenAPI schema (live) |

### 2.4 Email-finder APIs

**Hunter.io v2**
- Base: `https://api.hunter.io/v2`.
- Auth, any one of:
  - query `api_key=`
  - header `X-API-KEY:`
  - header `Authorization: Bearer <key>`
- Live without a key → **401** `{"errors":[{"id":"authentication_failed","code":401,"details":"No user found for the API key supplied"}]}`. This applies to all endpoints, **including `email-count`, which used to be keyless**.

| Endpoint | Params | Cost | Rate limit |
|---|---|---|---|
| `GET /domain-search` | `domain` or `company`, `limit` (≤100; **Free plan: first 10 only**), `offset`, `type=personal|generic`, `seniority=junior,senior,executive`, `department=…`, `required_field=full_name,position,phone_number`, `verification_status=valid,accept_all,unknown`, `decision_maker`, `job_titles`, `location` | pricing page: "1 credit per email found" (**conflicting**; another source says 1 credit per 1–10 emails) | 15 req/s, 500/min |
| `GET /email-finder` | `domain` or `company` or `linkedin_handle`; `first_name`+`last_name` or `full_name`; `max_duration=3..20` (default 10) | 1 credit, only if found | 15 req/s, 500/min |
| `GET /email-verifier` | `email` | 0.5 credit, free if it fails | 10 req/s, 300/min; **HTTP 202 = still in progress, poll again**; 222 = remote SMTP error; 451 = claimed_email |
| `GET /email-count` | `domain` or `company`, `type` | free (but a key is needed) | 15 req/s |
| `GET /account` | — | free | — |

- Other status codes: 403 = rate limit, 429 = usage (credits) exhausted, 451 = the person asked Hunter to stop processing their data.
- Free plan: **50 credits/month**. Repeated identical searches in the same period count once.
- Paid plans:

  | Plan | €/month | credits/month |
  |---|---|---|
  | Starter | 49 | 2,000 |
  | Growth | 149 | 10,000 |
  | Scale | 299 | 25,000 |

**Other finders**

| Service | Free tier | API on free tier? | Notes |
|---|---|---|---|
| Snov.io | Trial: 50 credits + 100 recipients / 30 days | **No** ("API & webhooks access … not available in Trial") | Starter $39/month for 1,000 credits |
| Dropcontact | 50 credits on signup | API & MCP included in paid plans | Starter €79/month for 500 credits; "does not use any contact databases" (algorithmic, French) |
| Kaspr | 15 B2B email + 5 phone + 5 direct-email credits/month | **No** (API on request from Starter, €45–59/month) | Works through a LinkedIn Chrome extension. **CNIL fine €240,000 (Dec 2024)** |
| Tomba.io | 25 searches/month | apparently paid only | $89/month for 10k credits |
| Reacher (check-if-email-exists) | self-host (AGPL-3.0) or SaaS | — | **Requires outbound port 25**; recommends SMTP proxies for volume |

---

## 3. Response and page shapes (real, trimmed)

### 3.1 `/search` (live, `activite_principale=62.01Z&departement=57&per_page=2`, trimmed)

```json
{
  "results": [{
    "siren": "519037253",
    "nom_complet": "TRUSTTEAM (TRUSTTEAM)",
    "nom_raison_sociale": "TRUSTTEAM",
    "sigle": null,
    "nombre_etablissements": 14,
    "nombre_etablissements_ouverts": 12,
    "activite_principale": "62.01Z",
    "activite_principale_naf25": "62.20H",
    "section_activite_principale": "J",
    "categorie_entreprise": "PME",
    "annee_categorie_entreprise": "2023",
    "tranche_effectif_salarie": "21",
    "annee_tranche_effectif_salarie": "2023",
    "nature_juridique": "5710",
    "etat_administratif": "A",
    "statut_diffusion": "O",
    "date_creation": "2009-12-23",
    "date_mise_a_jour": "2026-09-22T09:29:27",
    "siege": {
      "siret": "51903725300043",
      "activite_principale": "46.51Z",
      "adresse": "PARC D'ACTIVITES SAINT JACQUES 41 RUE ALBERT EINSTEIN 54320 MAXEVILLE",
      "code_postal": "54320", "commune": "54357", "libelle_commune": "MAXEVILLE",
      "departement": "54", "region": "44", "epci": "245400676",
      "latitude": "48.7006371", "longitude": "6.1223250588",
      "coordonnees": "48.7006371,6.1223250588",
      "liste_enseignes": ["DSOFT"], "nom_commercial": "TRUSTTEAM",
      "liste_idcc": ["1486"], "tranche_effectif_salarie": "21",
      "caractere_employeur": "N", "est_siege": true, "etat_administratif": "A"
    },
    "dirigeants": [
      {"nom": "GARNIER", "prenoms": "YANN", "annee_de_naissance": "1970", "date_de_naissance": "1970-01",
       "qualite": "Commissaire aux comptes suppléant", "nationalite": null, "type_dirigeant": "personne physique"},
      {"siren": "399166222", "denomination": "YZICO AUDIT", "qualite": "Commissaire aux comptes titulaire", "type_dirigeant": "personne morale"},
      {"siren": null, "denomination": "TRUSTTEAM NV", "qualite": "Président de SAS", "type_dirigeant": "personne morale"}
    ],
    "matching_etablissements": [{
      "siret": "51903725300084", "adresse": "ZAC DE MERCY 1 ALLEE DE LA CHAPELLE 57530 ARS LAQUENEXY",
      "code_postal": "57530", "commune": "57031", "libelle_commune": "ARS LAQUENEXY",
      "latitude": "49.0850910441539", "longitude": "6.24474447888129",
      "activite_principale": "46.51Z", "est_siege": false, "etat_administratif": "A",
      "tranche_effectif_salarie": "NN", "liste_idcc": ["1486"], "nom_commercial": null
    }],
    "finances": {"2022": {"ca": 8904988, "resultat_net": 1603916}},
    "complements": {"convention_collective_renseignee": true, "liste_idcc": ["0804","1486"], "egapro_renseignee": true,
                    "est_entrepreneur_individuel": false, "est_ess": false, "est_association": false, "est_qualiopi": false,
                    "est_organisme_formation": false, "est_societe_mission": false, "est_service_public": null},
    "tva": ["FR65519037253"]
  }],
  "total_results": 1815, "page": 1, "per_page": 2, "total_pages": 908
}
```

**Field reference (live keys)**

- Top level: `activite_principale, activite_principale_naf25, annee_categorie_entreprise, annee_tranche_effectif_salarie, caractere_employeur, categorie_entreprise, complements, date_creation, date_fermeture, date_mise_a_jour, date_mise_a_jour_insee, date_mise_a_jour_rne, dirigeants, etat_administratif, finances, matching_etablissements, nature_juridique, nom_complet, nom_raison_sociale, nombre_etablissements, nombre_etablissements_ouverts, section_activite_principale, siege, sigle, siren, siren_conserve, statut_diffusion, tranche_effectif_salarie, tva`.
- `siege`: `activite_principale, activite_principale_naf25, activite_principale_registre_metier, adresse, annee_tranche_effectif_salarie, caractere_employeur, cedex, code_pays_etranger, code_postal, commune, complement_adresse, coordonnees, date_creation, date_debut_activite, date_fermeture, date_mise_a_jour, date_mise_a_jour_insee, departement, dernier_numero_voie, distribution_speciale, epci, est_siege, etat_administratif, geo_adresse, geo_id, indice_repetition, latitude, libelle_cedex, libelle_commune, libelle_commune_etranger, libelle_pays_etranger, libelle_voie, liste_enseignes, liste_finess, liste_id_bio, liste_id_organisme_formation, liste_idcc, liste_rge, liste_uai, longitude, nom_commercial, numero_voie, region, siret, statut_diffusion_etablissement, tranche_effectif_salarie, type_voie`.
- `matching_etablissements[]`: the same as `siege` minus a few fields, plus `ancien_siege`.
  - **It includes closed establishments** (`etat_administratif: "F"`, with `date_fermeture` set; seen live for NANCY SERVICES).
- `dirigeants[]` comes in two forms:
  - natural person: `{nom, prenoms, annee_de_naissance, date_de_naissance (YYYY-MM), qualite, nationalite, type_dirigeant:"personne physique"}`
  - legal entity: `{siren, denomination, qualite, type_dirigeant:"personne morale"}`
- Latitude and longitude are **strings**. Cast them to float.

**Errors (live):**
- HTTP 400 with JSON `{"erreur": "..."}`.
- **HTTP 429 is nginx HTML**, not JSON: `<html><head><title>429 Too Many Requests</title>…`.
  - In the live test, 15 parallel requests gave 13×200 and 2×429.
  - **No `Retry-After` header was present**, although the docs say one is sent.

### 3.2 `/near_point` (live, trimmed)

```json
{"results":[{"siren":"479766842","nom_complet":"CAPGEMINI TECHNOLOGY SERVICES","nombre_etablissements":112,
 "nombre_etablissements_ouverts":58,"activite_principale":"62.02A","activite_principale_naf25":"62.20G",
 "categorie_entreprise":"GE","tranche_effectif_salarie":"53",
 "matching_etablissements":[{"activite_principale":"62.02A","adresse":"IMMEUBLE FIRST PLAZA 92 Q BOULEVARD SOLIDARITE 57070 METZ",
   "code_postal":"57070","commune":"57463","date_creation":"2026-05-19", "...": "..."}]}], "total_results": "...", "...": "..."}
```

### 3.3 Géoplateforme `/search` (live)

```json
{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[6.194891,49.108385]},
 "properties":{"label":"Metz","score":0.9573536363636362,"id":"57463","type":"municipality","name":"Metz","postcode":"57000",
 "citycode":"57463","x":933224.54,"y":6894559.67,"population":122572,"city":"Metz","context":"57, Moselle, Grand Est",
 "importance":0.53089,"depcode":"57","municipality":"Metz","_type":"address"}}],"query":"Metz"}
```

- Coordinates are GeoJSON **`[lon, lat]`**.
- `/reverse?lat=49.1193&lon=6.1757&limit=1` → `"label":"18 Place d'Armes Jacques-François Blondel 57000 Metz"`, `"distance":10`.

### 3.4 Clearbit Autocomplete (live)

```json
[{"name":"Trustteam","domain":"trustteam.be","logo":null},{"name":"Trustteam","domain":"trustteam.fr","logo":null},
 {"name":"Trust1Team","domain":"trust1team.com","logo":null},{"name":"Trust Team","domain":"trustteam.com","logo":null}]
```

- `query=yaakadev` → `[{"name":"YAAKADEV","domain":"yaakadev.com","logo":null}]`
- `query=le16solutions` → the 4th result is `{"name":"16Solutions","domain":"le16solutions.com"}`
- `query=nancy services` → `[]`
- Response headers: `Server: Clearbit`, CloudFront, `Cache-Control: public, max-age=2629746`.

### 3.5 DuckDuckGo HTML (live)

**Result markup.** Results are `<a class="result__a" href="//duckduckgo.com/l/?uddg=<urlencoded target>&rut=…">Title</a>` and `<… class="result__url">www.trustteam.eu/fr-fr</…>`.

**Real result list for `q=Trustteam Maxeville`:**
- trustteam.eu/fr-fr ✅
- cylex-locale.fr
- annuaire-entreprises.data.gouv.fr/entreprise/trustteam-trustteam-519037253
- societe.com
- pappers.fr
- dnb.com
- trustteam.eu/… ✅
- datalegal.fr
- agenceliste.com
- agences.io

Directories dominate, so a blocklist is needed.

**Blocked response:** HTTP **202**, about 14 KB, containing `anomaly.js?sv=html&cc=botnet…`, `challenge-form` and `anomaly-modal__mask`, with zero `result__a`. The page also contains a decoy `error-lite+xxxx@duckduckgo.com` address, which the email extractor must ignore.

### 3.6 Emails found on real French SME pages (live)

| Page | Result |
|---|---|
| `https://www.yaakadev.com/` (and `/contact`, `/mentions-legales`) | `secretariat@yaakadev.com`. The contact form placeholder `votre@email.com` is a false positive. `/mentions-legales` contains `812 991 172` (the SIREN) |
| `https://www.trustteam.eu/fr-fr` | `contact@trustteam.fr` (a **different TLD from the site**). Careers link `https://trustteam.jobtoolz.com/fr`. `/fr-fr/mentions-legales` contains `Numéro Siret : 519 037 253 00043 - APE 4651Z RCS NANCY : B 519 037 253` |
| `https://le16solutions.com/` | `support@le16solutions.com` |
| `https://www.lesjoiesducode.fr/contact` | Cloudflare: `<a href="/cdn-cgi/l/email-protection#61020e0f15000215210d04120b0e0804120514020e05044f0713">` → key `0x61` → `contact@lesjoiesducode.fr`. The page also loads `/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js`. **The key changes on every response** (it was `0x9a` on the first fetch) |
| `https://recrutement.metz.fr/contact/` | Yoast JSON-LD `{"@type":"Person","name":"prenom.nom@agence-exemple.fr"}`: the web agency's WordPress author, a **false positive** |

### 3.7 MX (live, dnspython 2.8.0)

| Domain | MX | Provider class |
|---|---|---|
| yaakadev.com | aspmx.l.google.com (+alt1–4) | Google Workspace |
| le16solutions.com | le16solutions-com.mail.protection.outlook.com | Microsoft 365 |
| trustteam.eu | axsse.axsmail.be, backup.axsmail.be | other |
| capgemini.com | mx1.hc674-87.eu.iphmx.com … | Cisco IronPort (gateway, typically accept-then-bounce) |
| nonexistent-domain-xyz-123456.fr | — | NXDOMAIN |

---

## 4. Limits, ToS and legal (France / GDPR / CNIL)

### 4.1 Rate limits

| Service | Limit | Source |
|---|---|---|
| recherche-entreprises | **7 req/s per IP, 30 req/s per ASN**, "limite maximale … pas un débit garanti"; priority goes to public services under load; 429 on excess | openapi.json (live) |
| recherche-entreprises paging | per_page ≤ 25, page×per_page ≤ 10,000 | live 400s |
| Géoplateforme geocodage | 50 req/s per IP | data.gouv.fr dataservice page + live `ratelimit-limit: 50` |
| Clearbit Autocomplete | undocumented; responses are cached (max-age ≈ 30 days) | live headers |
| DuckDuckGo HTML | undocumented; bot-flagged after about 5 requests in 2 min, still blocked 15 min later | live |
| Brave Search | $5 per 1,000 requests, $5 free credit per month (≈ 1,000 req), 50 req/s; the free plan ended in Feb 2026 for new users; card required | brave pricing page |
| Tavily | 1,000 credits/month free, no card; basic search = 1 credit, advanced = 2 | tavily pricing + docs |
| Hunter | 50 credits/month free; domain-search/email-finder 15 req/s and 500/min; verifier 10 req/s and 300/min | hunter.io |
| Wikidata SPARQL | 60 s query timeout; UA required; be gentle (≤ 1 req/s) | UNCERTAIN (general WDQS policy) |
| Overpass (overpass-api.de) | shared, returns 504 when busy; about 10k queries/day and 1 GB/day are fair-use guidelines | UNCERTAIN |

### 4.2 Data licences and ToS

- **recherche-entreprises**:
  - The data is public SIRENE/RNE, published by DINUM. The OpenAPI `info.license` is MIT (that covers the API spec). SIRENE data is open data under Licence Ouverte (UNCERTAIN for the exact licence version).
  - Only companies marked "diffusible" are returned. Non-diffusible companies and companies refused RCS registration are excluded (openapi description).
- **Directory sites** (societe.com, pappers.fr, verif.com, pagesjaunes.fr, kompass, dnb.com, cylex, datalegal):
  - Do not crawl them. They are protected by the *droit sui generis des bases de données* (CPI art. L.342-1) and by their ToS.
  - Use them only as URLs to skip in search results.
- **LinkedIn**: the User Agreement forbids scraping. The CNIL fined **KASPR €240,000** (decision published Dec 2024) for collecting LinkedIn members' contact details even when members had limited their visibility. It ordered deletion, with a compliance deadline of 2025-06-18. **Do not scrape emails from LinkedIn.**
- **Company websites**: respect `robots.txt`, crawl at most 6 pages per domain at ≤ 1 req/s per host, and identify yourself politely.

### 4.3 Legal notice obligation (useful for verification)

- Service-Public says professional websites must display the company name, address, **RCS number**, **phone and email address**, VAT number and host details. It cites LCEN (loi n° 2004-575) art. 6 (page last checked 2023-07-31).
- The penalty is 1 year of imprisonment and a €75,000 fine.
- This is why `/mentions-legales` is the best page to find **both** the SIREN (domain verification) and a contact email.
- Article numbering may have moved after the 2024 SREN law (UNCERTAIN). The obligation itself remains.

### 4.4 GDPR and CNIL for spontaneous applications

- **Generic addresses** (`contact@`, `rh@`, `recrutement@`) identify a legal entity and not a natural person. The CNIL's email-prospecting guidance says these are **not subject** to the personal-data prospecting rules.
- **Named professional addresses** (`prenom.nom@`) are personal data under the GDPR.
  - A job application is not strictly "prospection commerciale" (CPCE L.34-5 targets the promotion of goods and services), but apply the CNIL B2B conditions anyway:
    - the message is relevant to the person's professional role
    - the sender is clearly identified
    - there is an easy opt-out ("Si vous ne souhaitez plus être contacté, répondez STOP")
    - the email says where the address was obtained (GDPR art. 14 transparency)
  - The household exemption (GDPR art. 2(2)(c)) for a private person's own job search is **arguable but not settled**. Mark it UNCERTAIN and design as if the GDPR applies.
- **Data minimisation**:
  - Store only the company, the email and its source URL, the date and the status.
  - Do not store dirigeants' birth dates. The API returns `date_de_naissance`, so drop it at ingestion.
  - Purge contacts that are not used after 6–12 months.
  - Keep a suppression list so anyone who replies "stop" is never contacted again.
- **Volume**: keep sends low and personalised (for example ≤ 20–30 per day) to stay clearly outside "mass prospecting" and to protect the candidate's mailbox reputation.

---

## 5. Gotchas and anti-bot measures

### 5.1 recherche-entreprises

1. **`departement`/`code_postal` filter establishments, not the HQ.** The HQ of a result can be in Montpellier (live: NANCY SERVICES) or Lorient (YAAKADEV). Use `matching_etablissements[]` for the local address, and **drop entries with `etat_administratif != "A"`** because closed establishments are returned.
2. **`activite_principale` filters the unité légale**, and the local establishment can have another NAF (live: TRUSTTEAM is 62.01Z while its HQ and 57 establishment are 46.51Z).
3. **Large ESNs are in 62.02A, not 62.01Z.** With 62.01Z alone, `tranche_effectif_salarie ≥ 22` returned 0 companies in 57, and `tranche=53` returned 0 nationwide.
4. **About 89% of results have tranche `NN`** and the tranche year is 2023. Score on it instead of filtering.
5. **`q` with 9 or 14 digits ignores all filters.**
6. **The 429 response is HTML** and has no `Retry-After`. Throttle client-side to ≤ 5 req/s and back off exponentially (1 s, 2 s, 4 s…).
7. **The page cap is 10,000 results.** Shard by `departement` × NAF code.
8. **`dirigeants[]` includes statutory auditors** (`qualite` = "Commissaire aux comptes titulaire/suppléant"). Never treat them as contacts. Often the only "dirigeant" is a legal entity (a holding company) with no person name.
9. `nom_complet` looks like `"TRUSTTEAM (TRUSTTEAM)"` or `"NANCY SERVICES (LE16SOLUTIONS)"`. The part in brackets is the trade name or sign. Use `nom_raison_sociale`, `siege.nom_commercial`, `siege.liste_enseignes` and `sigle` as separate name candidates for domain search.
10. `activite_principale_naf25` is already populated. The NAF 2025 switch happens on 2027-01-01, and the filter may start requiring NAF 2025 codes then (UNCERTAIN). Parse the 400 error text.
11. `latitude`/`longitude` are strings and can be null.

### 5.2 Geocoding

12. `api-adresse.data.gouv.fr` is past its announced sunset. Migrate to `data.geopf.fr/geocodage` now.
13. GeoJSON is `[lon, lat]`, and `near_point` wants `lat` and `long`.
14. **Luxembourg**: SIRENE, the Géoplateforme and recherche-entreprises cover France only.
    - For Luxembourg employers, use job boards or the search engine plus the site's legal notice ("RCS Luxembourg B123456").
    - Luxembourg Business Registers has no free JSON API (UNCERTAIN).

### 5.3 Domain discovery

15. **Clearbit** returns look-alikes (`trust1team.com`, `konexus-consulting.com` for "Capgemini Deutschland"), and returns nothing for generic names ("nancy services"). Always verify.
16. **DuckDuckGo** flags the IP quickly: HTTP 202 with an `anomaly.js` challenge, and `lite.duckduckgo.com` is blocked at the same time.
    - Treat a 202 as "blocked", stop, and wait at least 30–60 min.
    - Never retry in a loop.
    - Use one query per company at most, only after the free options fail.
17. **Result blocklist** (skip these hosts):
    - French company directories: `societe.com, pappers.fr, annuaire-entreprises.data.gouv.fr, infogreffe.fr, verif.com, manageo.fr, societeinfo.com, datalegal.fr, dnb.com, kompass.com, pagesjaunes.fr, cylex-locale.fr, cylex.fr, agenceliste.com, agences.io, sortlist.fr, europages.fr, bilansgratuits.fr, entreprises.lefigaro.fr, corporama.com, score3.fr, lagazettefrance.fr, bodacc.fr, journal-officiel.gouv.fr`
    - Social networks, job boards and review sites: `linkedin.com, facebook.com, instagram.com, x.com, twitter.com, youtube.com, indeed.com, fr.indeed.com, welcometothejungle.com, hellowork.com, glassdoor.fr, glassdoor.com, jobteaser.com, francetravail.fr, apec.fr, trustpilot.com, google.com, wikipedia.org`
18. **The company email domain can differ from the website** (live: site `trustteam.eu`, email `@trustteam.fr`). When you filter "same domain", accept emails whose domain is any Clearbit or verified candidate domain for that SIREN, or whose MX matches the site's MX.
19. **Verify with the SIREN.** Strip spaces, NBSP (` `), narrow NBSP (` `), dots and hyphens, then search for the 9 digits.
    - The SIRET (14 digits, starting with the SIREN) and the TVA number (`FR` + 2-digit key + SIREN) both contain it.
    - Some tiny sites omit the SIREN. Fall back to name similarity (rapidfuzz ≥ 85) plus the postcode or town appearing on the page.
20. **Parked or sold domains** resolve but show "domaine à vendre" (Sedo/Afternic). Reject pages with no SIREN, no name match, or parking keywords.
20b. **Wikidata Query Service rejects generic User-Agents.** It returned HTTP 403 with "Please respect our robot policy" for `jobbot/0.1` (live). Include a contact URL or email in the UA.
20c. **Clearbit fuzzy match.** rapidfuzz scorers are case-sensitive unless you pass `processor=default_process`. Even at a threshold of 80, look-alikes such as `trust1team.com` get through, so SIREN verification is still required.

### 5.4 Website crawling

21. **Cloudflare bot protection returns 403** on some sites (live: siecledigital.fr 403 `server: cloudflare`, usine-digitale.fr 403). Fall back to Playwright (Chromium, real UA, `locale="fr-FR"`), and give up if the page is a challenge ("Just a moment…").
22. **The Cloudflare email-protection key is random per response.** Decode it and never deduplicate on the hex.
23. **JS-built emails** (`'contact' + '@' + 'x.fr'`, React SPAs such as Wix and Webflow forms) only appear after rendering. Use Playwright only when httpx finds zero emails **and** the page has little text or `<noscript>`.
24. Many SMEs expose **only a contact form**, with no email. Record `contact_form_url` and let the user decide. Do not auto-submit forms.
24b. **JSON-LD can be the only place an email appears.**
    - Live: trustteam.eu exposes `contact@trustteam.fr` only in `Organization.contactPoint.email`.
    - Parse JSON-LD, keep `email` values from every node except `@type: Person`, then strip JSON-LD before running the regex.
25. False-positive sources:
    - image retina names (`logo@2x.png`)
    - npm specifiers (`lodash@4.17.21`)
    - Sentry DSNs (`<hex>@o123.ingest.sentry.io`, `sentry.wixpress.com`)
    - form placeholders (`votre@email.com`, `prenom.nom@domaine.fr`)
    - **JSON-LD/Yoast author emails** (a web agency developer)
    - the **host's or agency's email in the legal notice** (`support@ovh.com`, `contact@<agence-web>.fr`)
    - the DDG `error-lite+…@duckduckgo.com`
26. Email role ranking for an application:
    - rank 1: `recrutement|recrute|rh|drh|jobs?|emploi|carrieres?|careers?|candidature|talents?|hr|stage|alternance`
    - rank 2: `contact|bonjour|hello|info|accueil|secretariat|direction|agence`
    - rank 3: a person (`prenom.nom`)
    - exclude: `dpo|rgpd|privacy|gdpr|compta|comptabilite|facturation|invoice|billing|support|sav|technique|abuse|postmaster|webmaster|noreply|no-reply|newsletter|presse|press`
27. **An ATS on the careers page** means you should apply there. Known hosts:
    - `jobtoolz.com` (live)
    - `welcometothejungle.com/fr/companies/`
    - `*.teamtailor.com`
    - `jobs.lever.co`
    - `*.recruitee.com`
    - `apply.workable.com`
    - `*.flatchr.io`
    - `*.taleez.com`
    - `*.jobaffinity.fr`
    - `*.digitalrecruiters.com`
    - `jobs.smartrecruiters.com`
    - `*.myworkdayjobs.com`
    - `career*.successfactors.*`
    - `*.softy.pro`
    - `werecruit.io`
    - `*.join.com`
    - `hellowork.com` company pages
    - `*.gohire.io`

### 5.5 Email verification

28. **Port 25 is blocked from residential French lines.**
    - Live from this machine: `gmail-smtp-in.l.google.com:25` and `mx1.free.fr:25` both **timed out after 8 s**, while `smtp.gmail.com:587` returned `220 smtp.gmail.com ESMTP` in 0.07 s.
    - **Orange** blocks port 25 except towards its own SMTP (community.orange.fr).
    - **Bouygues** has no customer toggle on residential lines.
    - **Free**: "Blocage SMTP sortant" can be unchecked in the subscriber console on older boxes, but it is reported impossible on the Delta.
    - **SFR**: some boxes have a toggle ("Blocage du protocole SMTP sortant").
    - Hetzner Cloud blocks port 25 until the first invoice is paid plus a request. Scaleway blocks 25/465/587 by default, with a security-group option.
    - A fresh VPS IP probing RCPT will be greylisted or blocklisted. **Do not build SMTP probing.**
29. **Catch-all and gateway domains.**
    - Microsoft 365 with Directory-Based Edge Blocking rejects unknown recipients.
    - Cisco IronPort (`iphmx.com`), Proofpoint (`pphosted.com`) and Mimecast gateways often accept at RCPT and bounce later.
    - Google Workspace is usually honest but rate-limits unknown IPs.
    - Even Hunter returns `accept_all`, so treat `accept_all` as "unverifiable". Send to the generic address instead.
30. **Null MX** (`MX 0 .`, RFC 7505) means the domain accepts no mail. No MX but an A record means an implicit MX (RFC 5321 §5.1).
31. **Hunter** returns 451 `claimed_email` when the person opted out. Never use that email. On `/email-verifier`, **HTTP 202 means "in progress"**, so poll again after a few seconds.
32. The **Clearbit Logo API is dead** (`logo.clearbit.com` does not resolve). Do not use it for UI logos; use the site favicon instead.

---

## 6. Python implementation notes (sync httpx / Playwright sync / sqlite)

### 6.1 Module layout

- `jobbot/companies/sirene_client.py`: recherche-entreprises (search + near_point), throttle, shard.
- `jobbot/companies/geocode.py`: Géoplateforme + geo.api.gouv.fr.
- `jobbot/companies/domain_finder.py`: Clearbit, slug guessing, Wikidata, DDG/Brave; `verify_domain()`.
- `jobbot/companies/site_crawler.py`: httpx crawl with a Playwright fallback; link discovery; ATS detection.
- `jobbot/companies/email_extract.py`: regex, deobfuscation, Cloudflare decode, filtering, ranking.
- `jobbot/companies/email_guess.py`: dirigeant → pattern candidates; MX info; optional Hunter.
- `jobbot/companies/models.py`: frozen dataclasses (immutable).
- sqlite tables:
  - `companies(siren PK, name, naf, naf25, tranche, lat, lon, local_address, source_json, fetched_at)`
  - `domains(siren, domain, method, verified_by, score, checked_at)`
  - `emails(siren, email, kind, rank, source_url, found_at, status)`
  - `suppression(email PK, reason, at)`

### 6.2 Throttled recherche-entreprises client

```python
from __future__ import annotations
import time, random, httpx
from dataclasses import dataclass
from typing import Iterator

BASE = "https://recherche-entreprises.api.gouv.fr"
UA = "jobbot/0.1 (recherche d'emploi personnelle; contact: <your-email>)"
IT_NAF = "62.01Z,62.02A,62.02B,62.03Z,62.09Z,58.21Z,58.29A,58.29B,58.29C,63.11Z,63.12Z"

class Throttle:
    def __init__(self, per_sec: float) -> None:
        self._min_gap = 1.0 / per_sec
        self._last = 0.0
    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
        self._last = time.monotonic()

class SireneError(RuntimeError): ...

class SireneClient:
    def __init__(self, client: httpx.Client | None = None, per_sec: float = 5.0) -> None:
        self._c = client or httpx.Client(base_url=BASE, timeout=20, headers={"User-Agent": UA})
        self._t = Throttle(per_sec)

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(6):
            self._t.wait()
            r = self._c.get(path, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:   # 429 body is nginx HTML, no Retry-After
                time.sleep(min(60, 2 ** attempt) + random.random())
                continue
            try:
                msg = r.json().get("erreur", r.text[:300])
            except ValueError:
                msg = r.text[:300]
            raise SireneError(f"{r.status_code} {path} {params}: {msg}")
        raise SireneError(f"gave up after retries: {path} {params}")

    def search(self, **filters) -> Iterator[dict]:
        """Yield unité légale dicts; shards must keep total_results <= 10_000."""
        params = {"per_page": 25, "etat_administratif": "A", "limite_matching_etablissements": 20, **filters}
        page = 1
        while True:
            data = self._get("/search", {**params, "page": page})
            yield from data.get("results", [])
            if page >= data.get("total_pages", 0) or page * 25 >= 10_000:
                return
            page += 1

    def near_point(self, lat: float, lon: float, radius_km: float, naf: str = IT_NAF) -> Iterator[dict]:
        radius_km = max(0.001, min(radius_km, 50))
        page = 1
        while True:
            data = self._get("/near_point", {"lat": lat, "long": lon, "radius": radius_km,
                                             "activite_principale": naf, "per_page": 25, "page": page})
            yield from data.get("results", [])
            if page >= data.get("total_pages", 0) or page * 25 >= 10_000:
                return
            page += 1

# usage: shard per departement to stay < 10k
# for dep in ("57", "54"):
#     for u in client.search(activite_principale=IT_NAF, departement=dep, est_entrepreneur_individuel="false"): ...
```

**Normalising a result.** Keep the result immutable and drop the personal data you do not need:

```python
def local_establishments(u: dict, deps: set[str]) -> list[dict]:
    return [e for e in u.get("matching_etablissements", [])
            if e.get("etat_administratif") == "A" and (e.get("code_postal") or "")[:2] in deps]

def person_dirigeants(u: dict) -> list[tuple[str, str, str]]:
    out = []
    for d in u.get("dirigeants", []):
        q = (d.get("qualite") or "").lower()
        if d.get("type_dirigeant") != "personne physique" or "commissaire aux comptes" in q:
            continue
        out.append((d.get("prenoms") or "", d.get("nom") or "", d.get("qualite") or ""))
    return out   # never persist date_de_naissance

def name_candidates(u: dict) -> list[str]:
    s = u.get("siege") or {}
    names = [u.get("nom_raison_sociale"), s.get("nom_commercial"), u.get("sigle"), *(s.get("liste_enseignes") or [])]
    seen, out = set(), []
    for n in names:
        if n and n.upper() not in seen:
            seen.add(n.upper()); out.append(n)
    return out
```

**Scoring suggestion** (add up the points):

| Signal | Points |
|---|---|
| NAF core | +3 |
| Syntec IDCC `1486` in `siege.liste_idcc` or `complements.liste_idcc` | +2 |
| tranche ≥ `03` | +2 |
| tranche `NN` | 0 |
| `caractere_employeur == "O"` on a local establishment | +1 |
| `categorie_entreprise` in (`ETI`, `GE`) | +1 |
| distance to Metz < 15 km | +2 |
| distance to Metz < 30 km | +1 |
| `est_entrepreneur_individuel` | −5 (or exclude) |

### 6.3 Geocoding

```python
GEO = "https://data.geopf.fr/geocodage/search"

def geocode_city(c: httpx.Client, q: str, postcode: str | None = None) -> tuple[float, float, str] | None:
    params = {"q": q, "type": "municipality", "limit": 1, "autocomplete": 0}
    if postcode:
        params["postcode"] = postcode
    r = c.get(GEO, params=params, timeout=10)
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        return None
    lon, lat = feats[0]["geometry"]["coordinates"]          # GeoJSON order!
    return lat, lon, feats[0]["properties"]["citycode"]
# geocode_city(c, "Metz") -> (49.108385, 6.194891, "57463")
```

Hard-code these defaults in config so geocoding is not needed at runtime:

| City | lat | lon |
|---|---|---|
| Metz (geo.api centre; Géoplateforme gives 49.108385, 6.194891) | 49.1048 | 6.1962 |
| Nancy (Géoplateforme, live, INSEE 54395) | 48.688135 | 6.171263 |
| Thionville (geo.api centre) | 49.3718 | 6.1443 |

### 6.4 Domain finder with verification

```python
import re, socket, unicodedata, urllib.parse, html as htmlmod
from rapidfuzz import fuzz   # pip install rapidfuzz
from rapidfuzz.utils import default_process

DIRECTORY_HOSTS = {"societe.com","pappers.fr","annuaire-entreprises.data.gouv.fr","infogreffe.fr","verif.com","manageo.fr",
  "societeinfo.com","datalegal.fr","dnb.com","kompass.com","pagesjaunes.fr","cylex-locale.fr","cylex.fr","agenceliste.com",
  "agences.io","sortlist.fr","europages.fr","linkedin.com","facebook.com","instagram.com","x.com","twitter.com","youtube.com",
  "indeed.com","welcometothejungle.com","hellowork.com","glassdoor.fr","jobteaser.com","francetravail.fr","apec.fr",
  "trustpilot.com","wikipedia.org","bodacc.fr","corporama.com","lefigaro.fr"}
LEGAL_FORMS = r"\b(sas|sasu|sarl|eurl|sa|sci|scop|selarl|groupe|group|france|holding)\b"

def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(LEGAL_FORMS, " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)

def slug_candidates(names: list[str]) -> list[str]:
    out = []
    for n in names:
        base = slugify(n)
        if len(base) < 3:
            continue
        dashed = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", n).encode("ascii","ignore").decode().lower()).strip("-")
        for b in dict.fromkeys([base, dashed]):
            out += [f"{b}.fr", f"{b}.com", f"{b}.eu", f"{b}.io", f"{b}.tech", f"{b}.dev", f"{b}.net"]
    return list(dict.fromkeys(out))

def resolves(domain: str) -> bool:
    try:
        socket.getaddrinfo(domain, 443)
        return True
    except socket.gaierror:
        return False

def clearbit_candidates(c: httpx.Client, name: str) -> list[str]:
    r = c.get("https://autocomplete.clearbit.com/v1/companies/suggest", params={"query": name}, timeout=10)
    if r.status_code != 200:
        return []
    # rapidfuzz is case-sensitive by default -> pass processor (live bug: "Yaakadev" vs "YAAKADEV" scored < 80)
    return [x["domain"] for x in r.json()
            if x.get("domain") and fuzz.token_set_ratio(name, x.get("name", ""), processor=default_process) >= 80]

def wikidata_site(c: httpx.Client, siren: str) -> str | None:
    q = f'SELECT ?site WHERE {{ ?i wdt:P1616 "{siren}" . ?i wdt:P856 ?site }} LIMIT 1'
    # WDQS returns 403 "Please respect our robot policy https://w.wiki/4wJS" for a bare UA like "jobbot/0.1" (live);
    # the UA MUST contain a contact URL or email.
    r = c.get("https://query.wikidata.org/sparql", params={"query": q},
              headers={"Accept": "application/sparql-results+json",
                       "User-Agent": "jobbot/0.1 (personal job search; https://github.com/<you>/jobbot)"}, timeout=30)
    b = r.json()["results"]["bindings"] if r.status_code == 200 else []
    return urllib.parse.urlparse(b[0]["site"]["value"]).hostname if b else None

def ddg_candidates(c: httpx.Client, query: str) -> list[str] | None:
    """Returns None when DDG is blocking (HTTP 202 anomaly) -> caller must back off >= 30 min."""
    r = c.get("https://html.duckduckgo.com/html/", params={"q": query, "kl": "fr-fr"}, timeout=15)
    if r.status_code == 202 or "anomaly" in r.text[:20000]:
        return None
    hosts = []
    for href in re.findall(r'class="result__a"[^>]+href="([^"]+)"', r.text):
        target = urllib.parse.parse_qs(urllib.parse.urlparse(htmlmod.unescape(href)).query).get("uddg", [""])[0]
        host = (urllib.parse.urlparse(target).hostname or "").removeprefix("www.")
        if host and not any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS):
            hosts.append(host)
    return list(dict.fromkeys(hosts))

SIREN_STRIP = re.compile(r"[\s  .\-]")
def page_has_siren(html: str, siren: str) -> bool:
    return siren in SIREN_STRIP.sub("", re.sub(r"<[^>]+>", " ", html))

LEGAL_PATHS = ["/mentions-legales", "/mentions-legales/", "/mentions", "/informations-legales", "/legal", "/fr/mentions-legales"]

def verify_domain(c: httpx.Client, domain: str, siren: str, names: list[str], postcode: str | None) -> float:
    """0..1 confidence. 1.0 == SIREN found on site."""
    try:
        home = c.get(f"https://{domain}/", timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        return 0.0
    if home.status_code >= 400:
        return 0.0
    pages = [home.text]
    links = re.findall(r'href="([^"]*(?:mention|legal|cgu|cgv)[^"]*)"', home.text, re.I)
    for p in list(dict.fromkeys(links))[:2] or LEGAL_PATHS[:3]:
        try:
            r = c.get(urllib.parse.urljoin(str(home.url), p), timeout=15, follow_redirects=True)
            if r.status_code == 200:
                pages.append(r.text)
        except httpx.HTTPError:
            pass
    if any(page_has_siren(p, siren) for p in pages):
        return 1.0
    text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in pages)[:200_000]
    name_score = max((fuzz.partial_ratio(n.lower(), text.lower()) for n in names), default=0) / 100
    pc = 0.2 if postcode and postcode in text else 0.0
    parked = any(k in text.lower() for k in ("domaine à vendre", "domain is for sale", "sedo", "afternic"))
    return 0.0 if parked else min(0.9, 0.7 * name_score + pc)
```

**Order of candidate sources:**
1. Wikidata, only when `categorie_entreprise` is ETI or GE.
2. Clearbit for each `name_candidates()` entry.
3. Slug guesses that pass `resolves()`.
4. DDG `"{name} {libelle_commune}"`, at most one per company, at least 20–30 s apart, with a circuit breaker on 202.
5. Brave or Tavily if a key is configured.

Stop at the first domain with confidence 1.0. Otherwise keep the best domain with a score of at least 0.75, and flag it "à vérifier" in the UI.

### 6.5 Site crawl and email extraction

This code was tested on live pages; see §3.6.

```python
EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63})@((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.I)
CF_ATTR_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
OBFUSCATIONS = [
    (re.compile(r"\s*[\[\(\{<]\s*(?:at|arobase|chez|@)\s*[\]\)\}>]\s*", re.I), "@"),
    (re.compile(r"\s+(?:at|arobase)\s+", re.I), "@"),
    (re.compile(r"\s*[\[\(\{<]\s*(?:dot|point|\.)\s*[\]\)\}>]\s*", re.I), "."),
    (re.compile(r"\s+(?:dot|point)\s+(?=[a-z]{2,24}\b)", re.I), "."),
]
BAD_TLDS = {"png","jpg","jpeg","gif","svg","webp","avif","ico","css","js","json","map","woff","woff2","ttf","mp4","pdf"}
BAD_DOMAINS = {"example.com","example.org","exemple.fr","exemple.com","domain.com","domaine.fr","email.com","votredomaine.fr",
               "votre-domaine.fr","yourdomain.com","test.com","sentry.io","wixpress.com","duckduckgo.com"}
BAD_LOCAL = {"votre","your","you","nom","prenom","prenom.nom","name","email","mail","exemple","example","john.doe",
             "jean.dupont","test","xxx","user","username","noreply","no-reply","donotreply"}

def cf_decode(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i+2], 16) ^ key) for i in range(2, len(hexstr), 2))

def deobfuscate(text: str) -> str:
    text = urllib.parse.unquote(htmlmod.unescape(text))   # &#64; &#x40; &commat; %40
    for rx, rep in OBFUSCATIONS:
        text = rx.sub(rep, text)
    return text

def _plausible(local: str, domain: str) -> bool:
    d = domain.lower()
    return (d.rsplit(".", 1)[-1] not in BAD_TLDS
            and not any(d == b or d.endswith("." + b) for b in BAD_DOMAINS)
            and local.lower() not in BAD_LOCAL
            and not re.fullmatch(r"[0-9a-f]{16,}", local, re.I))

JSONLD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)

def strip_jsonld_authors(html: str) -> str:
    # Drop JSON-LD blocks (Yoast "Person" author = web-agency developer -> false positive, seen live)...
    return JSONLD_RE.sub(" ", html)

def jsonld_org_emails(html: str) -> set[str]:
    # ...but keep every JSON-LD "email" that is NOT inside a Person node (Organization, LocalBusiness,
    # ContactPoint...). Live: trustteam.eu only exposes contact@trustteam.fr in Organization.contactPoint.email.
    import json
    out: set[str] = set()
    def walk(o):
        if isinstance(o, dict):
            t = o.get("@type"); t = t if isinstance(t, list) else [t]
            if "Person" in t:
                return
            if isinstance(o.get("email"), str):
                out.add(o["email"].removeprefix("mailto:").strip().lower())
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    for block in JSONLD_RE.findall(html):
        try: walk(json.loads(block))
        except ValueError: pass
    return out

def extract_emails(html: str) -> set[str]:
    found = {cf_decode(h).lower() for h in CF_ATTR_RE.findall(html) + CF_HREF_RE.findall(html)}
    found |= jsonld_org_emails(html)
    for local, domain in EMAIL_RE.findall(deobfuscate(strip_jsonld_authors(html))):
        local = local.strip(".")
        if _plausible(local, domain):
            found.add(f"{local}@{domain}".lower())
    return found

WEBMAIL = {"gmail.com","orange.fr","wanadoo.fr","free.fr","sfr.fr","neuf.fr","laposte.net","outlook.fr","outlook.com",
           "hotmail.fr","hotmail.com","yahoo.fr","yahoo.com","icloud.com","bbox.fr","numericable.fr","proton.me","protonmail.com"}
ROLE_APPLY = re.compile(r"^(recrutement|recrute|rh|drh|jobs?|emploi|carrieres?|careers?|candidatures?|talents?|hr|stages?|alternance)\b")
ROLE_GENERIC = re.compile(r"^(contact|bonjour|hello|info|infos|accueil|secretariat|direction|agence|commercial)\b")
ROLE_EXCLUDE = re.compile(r"^(dpo|rgpd|privacy|gdpr|compta|comptabilite|facturation|invoice|billing|support|sav|technique|"
                          r"abuse|postmaster|webmaster|noreply|no-reply|newsletter|presse|press)\b")

def rank_email(email: str, company_domains: set[str]) -> int | None:
    local, domain = email.split("@", 1)
    if domain not in company_domains and domain not in WEBMAIL:
        return None                           # host / agency / third party
    if ROLE_EXCLUDE.match(local):
        return None
    if ROLE_APPLY.match(local):
        return 1
    if ROLE_GENERIC.match(local):
        return 2
    return 3                                  # named person or unknown role
```

#### Crawl plan per verified domain (≤ 6 fetches, ≤ 1 req/s per host)

1. **Fetch** `/` with httpx:
   - UA: a browser UA
   - headers: `Accept-Language: fr-FR,fr;q=0.9`
   - `follow_redirects=True`
   - timeout: 15 s
   - maximum size: 2 MB
2. **Collect links** whose href or anchor text matches `contact|nous-contacter|contactez|mentions|legal|recrut|carri|career|jobs?|emploi|rejoindre|rejoignez|join|equipe|team|a-propos|qui-sommes`, same host only.
3. **Fetch the top 5**, in this priority: recrutement/carrieres → contact → mentions-legales → equipe.
   - If no links were found, try fixed paths: `/contact`, `/contact/`, `/nous-contacter`, `/contactez-nous`, `/mentions-legales`, `/recrutement`, `/carrieres`, `/nous-rejoindre`, `/rejoignez-nous`, `/jobs`, `/careers`.
4. **Detect an ATS** by searching all hrefs for these hosts:
   ```python
   ATS_RE = re.compile(r"https?://[^\"' ]*(jobtoolz\.com|welcometothejungle\.com/(?:fr|en)/companies/|teamtailor\.com|jobs\.lever\.co|recruitee\.com|apply\.workable\.com|flatchr\.io|taleez\.com|jobaffinity\.fr|digitalrecruiters\.com|smartrecruiters\.com|myworkdayjobs\.com|successfactors\.|softy\.pro|werecruit\.io|join\.com|gohire\.io)[^\"' ]*", re.I)
   ```
5. **Playwright fallback**, used only when one of these holds:
   - a status in (403, 503) with `server: cloudflare`
   - the HTML is under 5 KB of text
   - zero emails were found and the page looks JS-rendered (`<div id="root"></div>`, `__NEXT_DATA__`, `wix`)

   ```python
   from playwright.sync_api import sync_playwright
   def render(urls: list[str]) -> dict[str, str]:
       out = {}
       with sync_playwright() as p:
           b = p.chromium.launch(headless=True)
           ctx = b.new_context(locale="fr-FR", user_agent=UA_BROWSER)
           page = ctx.new_page()
           for u in urls:
               try:
                   page.goto(u, wait_until="domcontentloaded", timeout=20000)
                   page.wait_for_timeout(1500)            # let email-decode.min.js / SPA run
                   if "Just a moment" in page.title():     # Cloudflare challenge -> give up
                       continue
                   out[u] = page.content()
                   # mailto links after JS: page.eval_on_selector_all("a[href^='mailto:']", "els => els.map(e => e.href)")
               except Exception as e:                      # log, never swallow silently
                   out[u] = ""
                   print(f"render failed {u}: {e}")
           b.close()
       return out
   ```
6. **Also record** the `contact_form_url`: the first page containing a `<form>` with an email input and a textarea. **Never submit it.**

### 6.6 Pattern guessing and MX (tested)

```python
import dns.resolver, unicodedata
from dataclasses import dataclass

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z\- ']", "", s).strip()

def guess_patterns(prenoms: str, nom: str, domain: str) -> list[str]:
    first, last = _norm(prenoms.split()[0]) if prenoms else "", _norm(nom)
    if not first or not last:
        return []
    f = re.sub(r"[\s'\-]", "", first); l = re.sub(r"[\s'\-]", "", last)
    fd = re.sub(r"[\s']", "-", first); ld = re.sub(r"[\s']", "-", last)
    locals_ = [f"{f}.{l}", f"{fd}.{ld}", f"{f[0]}.{l}", f"{f[0]}{l}", f, f"{f}{l}", f"{f}-{l}", f"{f}_{l}",
               f"{l}.{f}", l, f"{l}{f[0]}", f"{f}.{l[0]}"]
    return list(dict.fromkeys(f"{x}@{domain}" for x in locals_))
# guess_patterns("JEAN PIERRE MARIE", "DE LA FONTAINE", "acme.fr")[:4]
# -> ['jean.delafontaine@acme.fr', 'jean.de-la-fontaine@acme.fr', 'j.delafontaine@acme.fr', 'jdelafontaine@acme.fr']
```

**Priors for patterns** (UNCERTAIN; send-burg.com study of 336k B2B emails):
- first.last ≈ 48% overall
- first.last ≈ 74% at enterprises vs 38% at startups
- small teams often use first-name-only addresses
- mid-size companies often use f.last or flast

If the site shows **any** named address (for example `sylvie.martin@acme.fr`), infer the pattern from it and apply it to the dirigeant. That is far more reliable than the priors.

```python
MX_PROVIDERS = [("google", ("google.com","googlemail.com")), ("microsoft365", ("mail.protection.outlook.com",)),
  ("ovh", ("ovh.net",)), ("gandi", ("gandi.net",)), ("ionos", ("ionos.fr","ionos.com","kundenserver.de")),
  ("o2switch", ("o2switch.net",)), ("proofpoint", ("pphosted.com","ppe-hosted.com")), ("mimecast", ("mimecast.com",)),
  ("cisco", ("iphmx.com",)), ("zoho", ("zoho.eu","zoho.com")), ("infomaniak", ("infomaniak.ch",))]

@dataclass(frozen=True)
class MxInfo:
    domain: str
    hosts: tuple[str, ...]
    provider: str        # google | microsoft365 | ... | other | implicit-a | null-mx | nxdomain | none
    accepts_mail: bool

def mx_info(domain: str) -> MxInfo:
    r = dns.resolver.Resolver(); r.lifetime = 5.0
    try:
        ans = r.resolve(domain, "MX")
        hosts = tuple(str(a.exchange).rstrip(".").lower() for a in sorted(ans, key=lambda a: a.preference))
    except dns.resolver.NXDOMAIN:
        return MxInfo(domain, (), "nxdomain", False)
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        try:
            r.resolve(domain, "A"); return MxInfo(domain, (domain,), "implicit-a", True)
        except Exception:
            return MxInfo(domain, (), "none", False)
    if hosts in (("",), (".",)):
        return MxInfo(domain, hosts, "null-mx", False)                     # RFC 7505
    prov = next((n for n, suf in MX_PROVIDERS if any(h.endswith(s) for h in hosts for s in suf)), "other")
    return MxInfo(domain, hosts, prov, True)
```

### 6.7 Optional Hunter client (key in env `HUNTER_API_KEY`; never hard-code it)

```python
import os
class HunterClient:
    BASE = "https://api.hunter.io/v2"
    def __init__(self, c: httpx.Client) -> None:
        key = os.environ.get("HUNTER_API_KEY")
        if not key:
            raise RuntimeError("HUNTER_API_KEY missing")
        self._c, self._h = c, {"X-API-KEY": key}
    def _get(self, path: str, params: dict) -> dict:
        for _ in range(5):
            r = self._c.get(self.BASE + path, params=params, headers=self._h, timeout=30)
            if r.status_code == 202:        # verifier still running
                time.sleep(3); continue
            if r.status_code in (222, 403):  # smtp error / rate limit
                time.sleep(5); continue
            if r.status_code == 451:         # claimed_email -> must not use
                return {"claimed": True}
            if r.status_code == 429:
                raise RuntimeError("Hunter credits exhausted")
            r.raise_for_status()
            return r.json()["data"]
        raise RuntimeError(f"Hunter {path} retries exhausted")
    def account(self) -> dict:              return self._get("/account", {})
    def domain_search(self, domain: str) -> dict:
        return self._get("/domain-search", {"domain": domain, "limit": 10, "type": "generic"})
    def find(self, domain: str, first: str, last: str) -> dict:
        return self._get("/email-finder", {"domain": domain, "first_name": first, "last_name": last})
    def verify(self, email: str) -> dict:   return self._get("/email-verifier", {"email": email})
```

**Budget**: 50 credits per month. Spend them like this:
- `email-verifier`, 0.5 credit each, only on the final chosen named address when no generic address exists.
- `domain-search?type=generic`, only for high-score companies with no email found on the site.
- Cache every Hunter answer in sqlite. Repeated identical lookups count once per period anyway.

### 6.8 End-to-end per company (pseudo)

1. Get the unité légale from `SireneClient.search`.
2. Compute `local_establishments()`, `name_candidates()` and the score.
3. Skip the company if it is in the suppression list or was already applied to.
4. Find the domain: Wikidata (ETI/GE), then Clearbit, then slug + DNS, then (budgeted) DDG, Brave or Tavily. Keep domains verified by SIREN.
5. Crawl and extract emails, rank them, and detect the ATS and the contact form.
6. If there is no rank-1 or rank-2 email:
   - guess patterns from the person dirigeants (excluding statutory auditors), using a pattern seen on the site if available
   - check the MX; optionally run the Hunter verifier; mark `status=unverified` unless the result is `valid`
7. Persist `emails(source_url, method, rank, confidence)`. The sending module uses a rank-1 or rank-2 address first, and never more than one address per company per campaign.

---

## 7. Claims table

| # | Claim | Status | Source / evidence |
|---|---|---|---|
| 1 | recherche-entreprises `/search` and `/near_point` are keyless and return JSON; CORS `*` | LIVE-VERIFIED | curl 200, headers `access-control-allow-origin: *` |
| 2 | Rate limit is 7 req/s per IP and 30 req/s per ASN, 429 on excess, UA recommended | DOC-SOURCED | https://recherche-entreprises.api.gouv.fr/openapi.json (fetched live) |
| 3 | The 429 body is nginx HTML with no `Retry-After` header | LIVE-VERIFIED | 15 parallel requests → 13×200, 2×429 |
| 4 | `per_page` max is 25; 26 → 400 | LIVE-VERIFIED | error JSON `erreur` |
| 5 | `page*per_page` ≤ 10,000, else 400 | LIVE-VERIFIED | page=401, per_page=25 → 400 |
| 6 | `activite_principale` accepts only NAF rév. 2 codes; `62.10Y` → 400 with the list of valid codes | LIVE-VERIFIED | curl |
| 7 | The response already includes `activite_principale_naf25` (e.g. 62.01Z→62.10Y, 62.02A→62.20G, 62.09Z→62.90Y, 63.11Z→63.10Y, 58.29C→58.29Y) | LIVE-VERIFIED | samples of 25 per NAF in dept 57 |
| 8 | NAF 2025 enters into force on 2027-01-01 (décret 2025-736) | DOC-SOURCED | https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000052019213 , https://www.insee.fr/fr/information/8617910 |
| 9 | Whether the API filter will switch to NAF 2025 codes in 2027 | UNCERTAIN | — |
| 10 | `departement`/`code_postal` filter establishments; `activite_principale`/`tranche`/`etat_administratif` apply to the unité légale | DOC-SOURCED + LIVE-VERIFIED | openapi descriptions; TRUSTTEAM HQ in 54 matched departement=57 |
| 11 | `matching_etablissements` includes closed establishments (`etat_administratif: "F"`) | LIVE-VERIFIED | NANCY SERVICES sample |
| 12 | `dirigeants[]` includes statutory auditors (Commissaire aux comptes) as `personne physique` | LIVE-VERIFIED | TRUSTTEAM sample |
| 13 | 62.01Z dept 57 active = 890; 89% tranche `NN`; 0 companies with tranche ≥ 22 | LIVE-VERIFIED | per-tranche counts |
| 14 | Large ESNs (Capgemini TS, Sopra Steria, CGI, Inetum, Altran) are in 62.02A | LIVE-VERIFIED | `sort_by_size=true` query |
| 15 | Recommended IT NAF list: 1,834 active companies in dept 57; 3,314 in 57+54; 1,601 without EI; 274 with tranche ≥ 03 | LIVE-VERIFIED | counts 2026-09-23 |
| 16 | `near_point` radius range is 0.001–50 km, parameter `long` | LIVE-VERIFIED | radius=51 → 400 |
| 17 | `include=` only works with `minimal=true`; values: complements, dirigeants, finances, matching_etablissements, siege, score, tva | DOC-SOURCED | openapi.json |
| 18 | recherche-entreprises exposes no website/email/phone field | LIVE-VERIFIED | openapi schema grep, response keys |
| 19 | Tranche code table (NN, 00, 01…53) | LIVE-VERIFIED | https://raw.githubusercontent.com/annuaire-entreprises-data-gouv-fr/search-api/main/app/labels/tranches-effectifs.json |
| 20 | `api-adresse.data.gouv.fr` still answers but sends Deprecation/Sunset 2026-01-31 headers and points to data.geopf.fr/geocodage | LIVE-VERIFIED | response headers |
| 21 | Géoplateforme `/search`, `/reverse`, `/getCapabilities` work keyless; coordinates are `[lon, lat]` | LIVE-VERIFIED | curl |
| 22 | Géoplateforme limit is 50 req/s per IP | DOC-SOURCED + LIVE header | https://www.data.gouv.fr/dataservices/api-geoplateforme-geocodage ; `ratelimit-limit: 50` |
| 23 | geo.api.gouv.fr/communes returns code + centre (Metz 57463, Thionville 57672) | LIVE-VERIFIED | curl |
| 24 | Clearbit Autocomplete still works keyless; `logo` is null | LIVE-VERIFIED | curl (capgemini, trustteam, yaakadev, tiime, le16solutions) |
| 25 | The Clearbit Logo API was sunset on 2025-12-01; the logo field has been null since 2025-09-09 | DOC-SOURCED + LIVE | https://developers.hubspot.com/changelog/upcoming-sunset-of-clearbits-free-logo-api ; logo.clearbit.com DNS fail |
| 26 | Clearbit Autocomplete might be sunset later | UNCERTAIN | no announcement found; build a fallback |
| 27 | DuckDuckGo HTML works keyless but switches to HTTP 202 `anomaly.js?cc=botnet` after a small burst, lasting at least 15 min (11:52 → 12:07 UTC); lite endpoint blocked too | LIVE-VERIFIED | curl/httpx at 11:48–12:00 UTC |
| 28 | Brave Search API: no free plan for new users since Feb 2026; $5/month credit, $5 per 1k requests, 50 req/s; header `X-Subscription-Token` | DOC-SOURCED + LIVE 422 | https://api-dashboard.search.brave.com/documentation/pricing ; https://www.implicator.ai/brave-drops-free-search-api-tier-puts-all-developers-on-metered-billing/ |
| 29 | Google Custom Search JSON API is closed to new customers and discontinued 2027-01-01 | DOC-SOURCED | https://developers.google.com/custom-search/v1/overview |
| 30 | Bing Search APIs retired 2025-08-11 | DOC-SOURCED + LIVE 401 | https://learn.microsoft.com/en-us/lifecycle/announcements/bing-search-api-retirement |
| 31 | Tavily: 1,000 free credits/month, no card; basic search = 1 credit | DOC-SOURCED + LIVE 401 | https://www.tavily.com/pricing ; https://docs.tavily.com/documentation/api-credits |
| 32 | Serper: 2,500 free queries, no card | DOC-SOURCED + LIVE 403 | https://serper.dev/ |
| 33 | Qwant API is blocked by DataDome for scripts | LIVE-VERIFIED | curl HTML "Please enable JS" |
| 34 | Wikidata P1616 (SIREN, 9 digits) + P856 (website): about 12,995 items; Sopra Steria and Altran resolved | LIVE-VERIFIED | SPARQL |
| 35 | OSM: 1,869 objects with `ref:FR:SIRET` + `website` in Moselle | LIVE-VERIFIED | Overpass (overpass-api.de 200; private.coffee 504) |
| 36 | The legal notice must show RCS number, phone, email, VAT and host (LCEN art. 6); penalty 1 year / €75k | DOC-SOURCED | https://entreprendre.service-public.gouv.fr/vosdroits/F31228 |
| 37 | LCEN article numbering after the SREN law 2024 | UNCERTAIN | — |
| 38 | The SIREN appears in the legal notice and can verify domain ownership | LIVE-VERIFIED | yaakadev.com (812 991 172), trustteam.eu (519 037 253) |
| 39 | Cloudflare email protection: `data-cfemail` or `/cdn-cgi/l/email-protection#hex`; first byte = XOR key; key random per response | LIVE-VERIFIED | lesjoiesducode.fr decoded to contact@lesjoiesducode.fr with keys 0x9a and 0x61 |
| 40 | Real false positives: `votre@email.com` placeholder; Yoast JSON-LD author email; DDG `error-lite+…@duckduckgo.com` | LIVE-VERIFIED | yaakadev.com/contact, recrutement.metz.fr/contact, DDG 202 page |
| 41 | Company email domain can differ from the website domain (trustteam.eu → contact@trustteam.fr) | LIVE-VERIFIED | trustteam.eu homepage |
| 42 | Careers pages link to ATS (trustteam → trustteam.jobtoolz.com) | LIVE-VERIFIED | trustteam.eu homepage |
| 43 | Some sites return 403 from Cloudflare to httpx (siecledigital.fr, usine-digitale.fr) | LIVE-VERIFIED | httpx scan |
| 44 | Outbound TCP/25 is blocked from this French residential line; 587 works | LIVE-VERIFIED | socket test (gmail MX:25 and mx1.free.fr:25 timeout; smtp.gmail.com:587 banner) |
| 45 | Orange, Bouygues, SFR and Free filter port 25 by default; Free/SFR toggles exist on some boxes; Bouygues has none | DOC-SOURCED (forums, not official) | https://communaute.orange.fr/t5/ma-connexion/Port-25-brid%C3%A9/td-p/979850 ; https://forum.yunohost.org/t/comment-debloquer-le-port-25-sur-box-bouygues/22800 ; https://www.learnup.fr/tutoriels/internet-reseaux/47-desactiver-le-blocage-du-port-smtp-chez-free/ ; https://homeprotection.fr/content-65-comment-desactiver-le-blocage-smtp-sortant-sur-ma-box-internet-ou-modifier-le-filtre-smtp-port-25/ |
| 46 | Hetzner blocks 25/465 until the first invoice plus a request; Scaleway blocks 25/465/587 by default | DOC-SOURCED | https://www.scaleway.com/en/docs/instances/how-to/send-emails-from-your-instance/ ; Hetzner community posts |
| 47 | dnspython 2.8.0 is installed; MX lookups classify Google/M365/Cisco correctly | LIVE-VERIFIED | local run |
| 48 | Hunter API v2 endpoints need a key (401 JSON), including email-count | LIVE-VERIFIED | curl |
| 49 | Hunter free plan: 50 credits/month; verifier 0.5 credit; finder 1 credit if found; limits 15 req/s + 500/min (verifier 10/s, 300/min); 202 = in progress; 451 = claimed | DOC-SOURCED | https://hunter.io/pricing ; https://hunter.io/api-documentation/v2 |
| 50 | Hunter domain-search cost: per email vs per 1–10 emails; free plan capped at the first 10 results | UNCERTAIN (sources conflict) | hunter.io/pricing vs third-party |
| 51 | Snov.io trial: 50 credits / 30 days, no API | DOC-SOURCED | https://snov.io/pricing |
| 52 | Kaspr free: 15 B2B email, 5 phone, 5 direct credits/month, no API | DOC-SOURCED | https://www.kaspr.io/pricing |
| 53 | Dropcontact: 50 trial credits; €79/month for 500 credits; API in paid plans; no contact database | DOC-SOURCED | https://www.dropcontact.com/pricing |
| 54 | Tomba free: 25 searches/month | DOC-SOURCED | https://tomba.io/pricing |
| 55 | Reacher/check-if-email-exists needs outbound port 25; AGPL-3.0 or commercial | DOC-SOURCED | https://github.com/reacherhq/check-if-email-exists |
| 56 | CNIL fined KASPR €240,000 for scraping LinkedIn contact details | DOC-SOURCED | https://www.cnil.fr/fr/aspiration-de-donnees-sanction-de-240-000-euros-lencontre-de-la-societe-kaspr |
| 57 | Generic addresses (contact@) are outside the personal-data prospecting rules; named B2B addresses need a professional link, information and opt-out | DOC-SOURCED | https://www.cnil.fr/fr/la-prospection-commerciale-par-courrier-electronique |
| 58 | The GDPR household exemption covers a private individual's job-search mailing | UNCERTAIN | legal interpretation, not settled |
| 59 | Email pattern priors (first.last ≈ 48%, 74% at enterprises) | UNCERTAIN (single vendor study) | https://send-burg.com/research/b2b-email-formats |
| 60 | Syntec collective agreement IDCC = 1486 (usable as `id_convention_collective`) | LIVE-VERIFIED (appears on IT companies) / DOC common knowledge | TRUSTTEAM `liste_idcc` |
| 61 | Wikidata/Overpass rate-limit specifics | UNCERTAIN | general policies |
| 62 | WDQS returns 403 "Please respect our robot policy https://w.wiki/4wJS" for UA `jobbot/0.1`; 200 once the UA contains a contact URL | LIVE-VERIFIED | httpx 2026-09-23 |
| 63 | Some SMEs expose their email only in JSON-LD `Organization.contactPoint.email` (trustteam.eu → contact@trustteam.fr); Person nodes hold CMS author emails | LIVE-VERIFIED | trustteam.eu, recrutement.metz.fr |
| 64 | The §6 code sketches (extract_emails, rank_email, guess_patterns, mx_info, page_has_siren, slug_candidates, ATS_RE, geocode_city, wikidata_site, clearbit_candidates, SireneClient.search, verify_domain) run without errors against live pages and APIs | LIVE-VERIFIED | executed from this doc; verify_domain("www.yaakadev.com","812991172") = 1.0 |
