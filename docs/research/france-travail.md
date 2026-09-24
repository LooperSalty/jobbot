# France Travail APIs (francetravail.io): Offres d'emploi v2, La Bonne Boite v2, ROME 4.0

> Research date: **2026-09-23**. Everything tagged LIVE-VERIFIED was observed today over HTTP (curl) without credentials.
> The official machine-readable OpenAPI specs were fetched live from the francetravail.io docs backend and saved in
> `docs/research/specs/` (`francetravail-offres-emploi-v2.openapi.json`, `francetravail-labonneboite-v2.openapi.json`,
> `francetravail-rome-metiers-v1.openapi.json`). **Treat those JSON files as the source of truth for field names.**

---

## 1. TL;DR + recommendation for jobbot

- **Offres d'emploi v2 is the main source**: it is free, legal and structured, and covers France Travail offers plus the partner offers whose publishers agreed to the API (HelloWork, Indeed-like aggregators, etc. appear as `origineOffre.partenaires[]`).
  - Auth is OAuth2 `client_credentials`: `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire` with the scope `api_offresdemploiv2 o2dsoffre`. The token lives about 1499 s (~25 min).
  - Search with `GET https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search`.
  - Page with `range=p-d`: at most 150 results per call, `p ≤ 3000`, `d ≤ 3149`. That is **at most 3150 results per query**, so split by date or department. A 206 means more results exist. Parse `Content-Range: offres p-d/total`.
  - The rate limit is **10 calls/second per application** (as displayed by the portal for the default tier). A 429 comes with `Retry-After`.
- **Alternance filter**: `natureContrat=E2,FS` (E2 = *Contrat apprentissage*, FS = *Cont. professionnalisation*, LIVE-VERIFIED on the public site).
  - **Never combine `typeContrat` and `natureContrat` in one query**: the API ORs them. Alternance offers usually carry `typeContrat=CDD` (sometimes CDI) plus a nature of E2/FS.
  - Run separate queries: one for alternance (`natureContrat=E2,FS`) and one for CDI (`typeContrat=CDI`, then drop offers with `alternance==true` locally).
- **ROME codes (ROME 4.0 v61, June 2026)**: developer jobs are now split across many fine-grained codes.
  - Offers are tagged with the new codes: LIVE-VERIFIED today in Moselle, with 8 offers on **M1855** "Développeur web" (which holds full-stack, front-end and back-end) and 11 on **M1805** "Développeur informatique".
  - **Always send a list**: `codeROME=M1805,M1855,M1861,M1818,M1827,M1841,M1848,M1852,M1836,M1813,M1821,M1837` (≤ 20 values, see §2.3).
- **The volume in Metz is tiny.** The public site shows 1 alternance offer matching "developpeur" within 10 km of Metz today, and it came from a school (ISCOD).
  - Search `departement=57,54` (+ `inclureLimitrophes=true`), then national (to catch remote work), then Luxembourg (`paysContinent`, resolve the code at runtime).
  - **Filter out school/CFA "fake" alternance offers** (ISCOD, Studi, OpenClassrooms…). The candidate already has a school.
  - The API has **no remote-work filter**. Detect remote by searching the description text for `télétravail|remote|full remote|100 %`.
- **No recruiter emails from France Travail.**
  - In Offres v2, `contact.courriel` and `agence.courriel` now contain a sentence plus a link to the francetravail.fr offer page, not an email.
  - Use `contact.urlPostulation`, then `origineOffre.partenaires[].url`, then `origineOffre.urlOrigine` as the apply target.
- **La Bonne Boite v2** (`https://api.francetravail.io/partenaire/labonneboite/v2/recherche`) gives companies likely to hire within 6 months: SIRET, name, NAF, headcount range, lat/lon and a `hiring_potential` score from 0 to 100.
  - Use the scope `api_labonneboitev2 search office`. The limit is **2 calls/second** for the default tier. Data is refreshed monthly.
  - **It does not return email, phone or website.** For spontaneous applications, use it only as a target list. Email discovery must come from another module (website crawl, Hunter, pattern + MX check).
- **Registration**: create an account on francetravail.io, then create an "application" (name, description, URL). Click "Utiliser l'API" on **Offres d'emploi** and on **La Bonne Boite** (`/produits-partages/catalogue/bonne-boite-v2`) to attach both to that app.
  - Access is free and immediate ("accès libre"). You get one `client_id` (format `PAR_<app>_<hash>`) and one `client_secret` per application.
  - One token can carry the scopes of every API subscribed by the app.
  - Env vars already reserved in `.env.example`: `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET`.
- **Suggestions (outside this topic, worth a separate module)**:
  - **La bonne alternance API** (`api.apprentissage.beta.gouv.fr`, `LBA_API_TOKEN` already in `.env.example`). It covers alternance offers plus companies that hire apprentices, and exposes an endpoint to **send an application through the API** without ever revealing the email. It is DOC-SOURCED from search results and not verified here.
  - **ROMEO** (francetravail.io) maps free text to ROME appellations. It is useful for scoring a CV against an offer.
  - The **ROME open-data CSV** needs no auth (see §6.7).

---

## 2. Endpoints / URLs & auth

### 2.1 Hosts (LIVE-VERIFIED 2026-09-23)

| Purpose | URL | Probe result today |
|---|---|---|
| Token (documented) | `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire` | fake creds → **400** `{"error_description":"Client authentication failed","error":"invalid_client"}` |
| Token alias (OIDC issuer host) | `POST https://authentification-pro.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire` | same 400 invalid_client |
| Token alias (used in LBB spec) | `POST https://authentification-partenaire.francetravail.io/connexion/oauth2/access_token?realm=%2Fpartenaire` | same 400 invalid_client |
| Legacy token host | `POST https://entreprise.pole-emploi.fr/connexion/oauth2/access_token?realm=%2Fpartenaire` | still answers 400 (don't rely on it) |
| OIDC discovery | `GET https://authentification-pro.francetravail.fr/connexion/oauth2/realms/root/realms/partenaire/.well-known/openid-configuration` | 200 JSON. `token_endpoint` = `https://authentification-pro.francetravail.fr/connexion/oauth2/realms/root/realms/partenaire/access_token`; `grant_types_supported` includes `client_credentials`; `token_endpoint_auth_methods_supported` includes `client_secret_post`, `client_secret_basic`. `entreprise.francetravail.fr/.../.well-known` → **308** to that host |
| API gateway | `https://api.francetravail.io/partenaire/...` | no token → **401**, empty body, `WWW-Authenticate: Bearer` |
| Legacy API hosts | `api.pole-emploi.io`, `api.emploi-store.fr` | **connection refused / not reachable**: do not use |
| Docs backend (no auth) | `https://francetravail.io/api-peio/v2/api/{apiId}/openapi` (84 = Offres, 360 = LBB, 273 = ROME Métiers, 272 = Fiches ROME) | 200 OpenAPI 3.0.1 JSON |
| API status (no auth) | `GET https://francetravail.io/api-peio/v2/api/status` | 200: `[{"apiId":84,"apiName":"Offres d'emploi","status":"up","uptime":99.93,...},{"apiId":360,"apiName":"La Bonne Boite","status":"up",...}]` |
| Incidents (no auth) | `GET https://francetravail.io/api-peio/v2/incident` | 200 JSON list (latest: 2026-06-09 Offres outage; 2025-12-03 communes referential broken → `commune` search unavailable) |

Other token-endpoint probes (LIVE-VERIFIED):
- `GET` on the token URL → **405** `{"error_description":"Required Method: POST found: GET","error":"method_not_allowed"}`
- POST without `grant_type` → **400** `{"error_description":"Grant type is not set","error":"invalid_request"}`
- POST without `realm` → 400 invalid_client. Always send `realm=/partenaire` **as a query-string parameter**.

**Important gotcha (LIVE-VERIFIED):** the gateway answers **401 to any `/partenaire/*` path, even a non-existent one** (`/partenaire/doesnotexist/v9/foo` → 401; `/foo` → 404). A 401 does **not** prove that an endpoint exists. Endpoint existence below comes from the official OpenAPI specs.

### 2.2 OAuth2 client_credentials (DOC-SOURCED: client-credentials doc page + spec)

```
POST /connexion/oauth2/access_token?realm=%2Fpartenaire HTTP/1.1
Host: entreprise.francetravail.fr
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=PAR_xxx&client_secret=yyy&scope=api_offresdemploiv2 o2dsoffre api_labonneboitev2 search office
```
Response 200:
```json
{"scope":"api_offresdemploiv2 o2dsoffre","expires_in":1499,"token_type":"Bearer","access_token":"..."}
```

| API | Required scopes (all "Obligatoire" in spec) | Base URL |
|---|---|---|
| Offres d'emploi v2 (spec v2.01) | `api_offresdemploiv2` `o2dsoffre` | `https://api.francetravail.io/partenaire/offresdemploi` |
| La Bonne Boite v2 (spec v2.27.3) | `api_labonneboitev2` `search` `office` | `https://api.francetravail.io/partenaire/labonneboite/v2` |
| ROME 4.0 – Métiers v1 | `api_rome-metiersv1` `nomenclatureRome` | `https://api.francetravail.io/partenaire/rome-metiers` |
| ROME 4.0 – Fiches métiers v1 | `api_rome-fiches-metiersv1` `nomenclatureRome` | `https://api.francetravail.io/partenaire/rome-fiches-metiers` |

- Scopes are space-separated. You can combine scopes of several APIs **if the app is subscribed to all of them**. Otherwise you get 400 `{"error":"invalid_scope","error_description":"Unknown/invalid scope(s): [...]"}`.
- Old clients (e.g. community code) also add `application_<CLIENT_ID>` to the scope. That is legacy and not in the current docs, so don't add it unless you get `invalid_scope`.
- Every API call: `Authorization: Bearer <access_token>` (+ `Accept: application/json`).

### 2.3 Offres d'emploi v2: endpoints

| Method | Path (prefix `https://api.francetravail.io/partenaire/offresdemploi`) | Notes |
|---|---|---|
| GET | `/v2/offres/search` | search; 200 / **206** / **204** / 400 / 500 |
| GET | `/v2/offres/{id}` | detail; 200, **204 = offer no longer exists** |
| GET | `/v2/referentiel/{name}` | `appellations`, `nafs`, `communes`, `continents`, `departements`, `domaines`, `langues`, `metiers`, `naturesContrats`, `niveauxFormations`, `pays`, `permis`, `regions`, `secteursActivites`, `themes`, `typesContrats`. Returns `[{"code":"E1","libelle":"Contrat travail"}, ...]`, with `Content-Range: <name> */<size>` |

**All query params of `/v2/offres/search`** (names are case-sensitive; from spec v2.01 + the HTML doc's "cardinalité" column):

| Param | Type | Allowed values / format | Max values |
|---|---|---|---|
| `range` | string | `p-d`, 0-based, `d-p+1 ≤ 150`, `p ≤ 3000`, `d ≤ 3149`. e.g. `0-149`, `150-299` | 1 |
| `sort` | int | `0` relevance desc, distance, date desc · `1` **date desc**, relevance, distance · `2` distance asc, relevance, date | 1 |
| `motsCles` | string | comma-separated keywords, each 2..99 chars, **AND** logic; matches `intitule`, `romeCode`, `romeLibelle`, `competences.libelle`, `formations.domaineLibelle`, `permis.libelle`, `langues.libelle`, (`description` only if also found in title/ROME). Allowed chars: `[aA-zZ]+[0-9]+[space]+[@#$%^&+./-"]`, so **strip accents** | 7 |
| `codeROME` | string | comma-separated ROME codes | spec says 200, HTML doc says **20** → keep ≤ 20 |
| `appellation` | string | ROME appellation code (5-6 digits, e.g. `38976` = Développeur full-stack); also returns "approaching" jobs | 1 |
| `domaine` | string | professional domain code (3 chars, referential `domaines`) | 1 |
| `grandDomaine` | string | `A,B,C,C15,D,E,F,G,H,I,J,K,L,L14,M,M13,M14,M15,M16,M17,`**`M18` = Informatique / Télécommunication**`,N` | n |
| `theme` | string | referential `themes` | 1 |
| `secteurActivite` | string | NAF division, 2 digits (e.g. `62` = programmation/conseil informatique) | 2 |
| `codeNAF` | string | `99.99X` format | 200 (spec) |
| `experience` | string | `0` non précisé, `1` < 1 an, `2` 1–3 ans, `3` > 3 ans | 1 |
| `experienceExigence` | string | `D` débutant accepté · `S` expérience souhaitée · `E` exigée | 1 |
| `typeContrat` | string | codes from `referentiel/typesContrats` (e.g. `CDI`, `CDD`, `MIS`, `LIB`…; `DDT` not usable) | 10 |
| `natureContrat` | string | codes from `referentiel/naturesContrats`: **`E2` = Contrat apprentissage, `FS` = Cont. professionnalisation**, `E1` = Contrat travail, `NS` seen in facets | n |
| `origineOffre` | int | `1` France Travail · `2` Partenaire | 1 |
| `partenaires` / `modeSelectionPartenaires` | string / enum | partner codes + `INCLUS`/`EXCLU` (only applies to partner offers) | 20 |
| `qualification` | string/int | `0` non-cadre · `9` cadre | 1 |
| `tempsPlein` | bool | `true`/`false` (prefer `dureeHebdo`) | 1 |
| `dureeHebdo` | string | `0` non précisé · `1` temps plein · `2` temps partiel. **Spec has a typo: the param name is `"dureeHebdo "` with a trailing space; send `dureeHebdo`** | 1 |
| `dureeHebdoMin` / `dureeHebdoMax` | HHMM | e.g. `3500` | 1 |
| `dureeContratMin` / `dureeContratMax` | decimal months 0–99 | e.g. `12.0` | 1 |
| `commune` | string | INSEE commune codes (Metz `57463`, Nancy `54395`, Thionville `57672`) | 5 |
| `distance` | int (km) | used only with `commune`; **default 10**; results go up to +30 % of the distance; `distance=0` = only that commune | 1 |
| `departement` | string | dept codes (`57`, `54`, `55`, `67`, `88`, `2A`…) | 5 |
| `inclureLimitrophes` | bool | include neighbouring departments | 1 |
| `region` | string | region code (Grand Est = `44`) | 1 |
| `paysContinent` | string | code from `referentiel/pays` / `continents` (Luxembourg: resolve by `libelle` at runtime) | 1 |
| `niveauFormation` | string | referential `niveauxFormations` | 1 |
| `permis` | string | referential `permis` | 1 |
| `salaireMin` + `periodeSalaire` | decimal + `M`/`A`/`H`/`C` | both mandatory together | 1 |
| `publieeDepuis` | int | days; documented values **1, 3, 7, 14, 31** | 1 |
| `minCreationDate` + `maxCreationDate` | `yyyy-MM-ddTHH:mm:ssZ` | **both mandatory together** | 1 |
| `accesTravailleurHandicape`, `offresMRS`, `offresManqueCandidats`, `entreprisesAdaptees`, `employeursHandiEngages` | bool | filters | 1 |

The **`typeContrat` × `natureContrat` = OR** semantics come from the spec: *"typeContrat=CDI,CDD avec natureContrat=E1 → toutes les offres CDI ou CDD … mais également toutes les offres de nature E1"*.

### 2.4 La Bonne Boite v2: endpoints (spec v2.27.3)

Prefix `https://api.francetravail.io/partenaire/labonneboite/v2`. Internally the gateway rewrites to `/api/v2/search`, `/api/v2/search/count` and `/api/v2/company/scores` (from the technical sheet). It is a FastAPI backend, hence the 422 validation errors.

| Method | Path | Purpose |
|---|---|---|
| GET | `/recherche` | companies sorted by hiring potential (paginated) |
| GET | `/nombreEntreprise` | only the count (`hits`) |
| GET | `/potentielEmbauche?siret=...` | score for given SIRET(s) (`siret` required, array) |

Query params for `/recherche` (and `/nombreEntreprise`, which has no paging or sort). **Array params are repeated keys** (`rome=M1805&rome=M1855`).

| Param | Type | Notes |
|---|---|---|
| `job` | string | free text matched against rome/domain/granddomain/naf labels |
| `rome` | array[str] | ROME codes |
| `domain`, `granddomain` | array[str] | professional (grand) domain codes |
| `naf` | array[str] | NAF codes (e.g. `6201Z`) |
| `location` | string | free text (city/department/region names from geo.api.gouv.fr) |
| `city` / `citycode` / `postcode` | array[str] | exact name / INSEE code / postal code |
| `department` / `department_number` | array[str] / array[int] | name / number |
| `region` / `region_number` | array[str] / array[int] | name / number |
| `latitude`, `longitude`, `distance` | number, number, int | all 3 together; `distance` in km, **0 < distance < 200**; `distance` also works with `citycode` (radius around the town hall) |
| `bbox` | string | `left|bottom|right|top` |
| `sort_by` | string | `hiring_potential` (default) or `romes.hiring_potential` |
| `sort_direction` | string | `desc` (default) / `asc` |
| `page` | int | from 1 (default 1) |
| `page_size` | int | default 10 (description says 20), **max 100** |

---

## 3. Response / page shape

### 3.1 Search response (Offres v2)

Headers:
- `Content-Type: application/json;charset=UTF-8`
- `Content-Range: offres 0-149/1234` (p-d/total). On 204 the value is `*/0`.
- `Accept-Range: 150` (max per call)
- On 429: `Retry-After: <seconds>`, `X-Ratelimit-Burst-Capacity-Clientidlimiter`, `X-Ratelimit-Remaining-Clientidlimiter`

Status codes:
- **200**: every matching result fits in this response.
- **206 Partial Content**: more results exist. Continue with the next `range`. **206 is a success, so don't `raise_for_status()`-then-fail and don't retry it.**
- **204**: no result (empty body).
- **400**: bad params. Body: `{"codeErreur":"1493122545781","codeHttp":400,"message":"La donnée keywords doit être une chaîne comprise entre 2 et 99 caractères."}`. Range too large: `{"codeHttp":400,"message":"La plage de résultats demandée est trop importante."}` with `Accept-Range: 150`.

Body:
```json
{
  "resultats": [ { /* Offre */ } ],
  "filtresPossibles": [
    {"filtre": "typeContrat",   "agregation": [{"valeurPossible": "LIB", "nbResultats": 3}]},
    {"filtre": "experience",    "agregation": [{"valeurPossible": "0",   "nbResultats": 3}]},
    {"filtre": "qualification", "agregation": [{"valeurPossible": "0", "nbResultats": 1}, {"valeurPossible": "X", "nbResultats": 2}]},
    {"filtre": "natureContrat", "agregation": [{"valeurPossible": "E1", "nbResultats": 1}, {"valeurPossible": "NS", "nbResultats": 2}]}
  ]
}
```
`filtresPossibles` is a free facet count, handy to show "N offres en apprentissage".

### 3.2 `Offre` object (exact field names from the live-fetched official spec; example values are the spec's own, **not a live offer**, trimmed)

```json
{
  "id": "048KLTP",
  "intitule": "Boulanger / Boulangère (H/F)",
  "description": "…up to 5000 chars…",
  "dateCreation": "2022-10-23T08:15:42Z",
  "dateActualisation": "2022-10-23T08:15:42Z",
  "lieuTravail": {"libelle": "74 - ANNECY", "latitude": 45.901584, "longitude": 6.125296, "codePostal": "74000", "commune": "74010"},
  "romeCode": "D1102",
  "romeLibelle": "Boulanger / Boulangère",
  "appellationlibelle": "Boulanger / Boulangère",
  "entreprise": {"nom": "…", "description": "…", "logo": "https://…", "url": "https://…", "entrepriseAdaptee": true},
  "typeContrat": "CDD",
  "typeContratLibelle": "CDD - 6 Mois",
  "natureContrat": "Contrat travail",
  "experienceExige": "D",
  "experienceLibelle": "Débutant accepté",
  "experienceCommentaire": "…",
  "formations": [{"codeFormation": "21538", "domaineLibelle": "…", "niveauLibelle": "…", "commentaire": "…", "exigence": "E"}],
  "langues": [{"libelle": "Anglais", "exigence": "E"}],
  "permis": [{"libelle": "B - Véhicule léger", "exigence": "S"}],
  "outilsBureautiques": ["Traitement de texte"],
  "competences": [{"code": "483320", "libelle": "…", "exigence": "E"}],
  "salaire": {"libelle": "Mensuel de 1923.00 Euros sur 12 mois", "commentaire": "Selon expérience", "complement1": "…", "complement2": "…",
              "listeComplements": [{"code": "14", "libelle": "Véhicule de fonction"}]},
  "dureeTravailLibelle": "35H",
  "dureeTravailLibelleConverti": "Temps plein",
  "complementExercice": "…",
  "conditionExercice": "…",
  "alternance": false,
  "contact": {
    "nom": "Etienne Dupont",
    "coordonnees1": "12 impasse du caillou", "coordonnees2": null, "coordonnees3": null,
    "telephone": "06 12 34 56 78",
    "courriel": "Pour postuler, utiliser le lien suivant : https://candidat.francetravail.fr/offres/recherche/detail/XXXXXXX",
    "commentaire": "A contacter après 19h",
    "urlRecruteur": "https://…",
    "urlPostulation": "https://…/carrieres"
  },
  "agence": {"telephone": "…", "courriel": "Pour postuler, utiliser le lien suivant : https://candidat.francetravail.fr/offres/recherche/detail/XXXXXXX"},
  "nombrePostes": 3,
  "accessibleTH": true,
  "deplacementCode": "1", "deplacementLibelle": "Jamais",
  "qualificationCode": "6", "qualificationLibelle": "Employé qualifié",
  "codeNAF": "10.71C", "secteurActivite": "10", "secteurActiviteLibelle": "…",
  "qualitesProfessionnelles": [{"libelle": "Faire preuve d'autonomie", "description": "…"}],
  "trancheEffectifEtab": "1 à 5 employés",
  "origineOffre": {
    "origine": "2",
    "urlOrigine": "https://…",
    "partenaires": [{"nom": "PARTENAIRE1", "url": "https://…/offre-46841", "logo": "https://…/logo.png"}]
  },
  "offresManqueCandidats": true,
  "contexteTravail": {"horaires": ["35H Travail le samedi"], "conditionsExercice": ["…"]},
  "entrepriseAdaptee": false,
  "employeurHandiEngage": false
}
```

Field semantics to remember:
- **In responses, `natureContrat` is the label** (`"Contrat apprentissage"`, `"Cont. professionnalisation"`, `"Contrat travail"`), **not the code** used in queries.
- `typeContrat` is a code, and `typeContratLibelle` is the label (e.g. `"CDD - 12 Mois"`).
- Name casing differs between the spec and the HTML doc: `appellationlibelle` (spec) vs `appellationLibelle` (HTML doc), and `lieuTravail.codePostal` (spec) vs `codepostal` (HTML doc). **Read both spellings.**
- `origineOffre.origine`: `"1"` France Travail, `"2"` partner. The spec types it as a string, the doc as an int, so normalise with `str()`.
- Public offer page: `https://candidat.francetravail.fr/offres/recherche/detail/{id}` (LIVE-VERIFIED 200 for id `6927986`). Ids are 7 chars, alphanumeric (`048KLTP`) or numeric (`6927986`).
- `description` is plain text with `\n`, up to 5000 chars (enough for ATS keyword scoring). Only about 20 % of offers carry `trancheEffectifEtab`.

Live snapshot today (public site, the same data behind the API; LIVE-VERIFIED):
- `motsCles=M1855&lieux=57D` → **8** offers. Samples: "Tech Lead Java / Expert Technique Java (H/F)" ATOS FRANCE; "Développeur Full Stack Java - Vue.Js Senior H/F" Geser Best; "Développeur C# Confirmé H/F" Open.
- `motsCles=M1805&lieux=57D` → **11** offers.
- `motsCles=M1861&lieux=57D` → **0**.
- `motsCles=M1855&lieux=57D&natureOffre=E2,FS` → **1**: "Alternance DEVELOPPEUR WEB FULLSTACK - Metz (H/F)" by **ISCOD** (a training company). Its detail page shows "CDD - 12 Mois / Contrat apprentissage".

### 3.3 La Bonne Boite v2 `/recherche` response (spec example; values are the spec's own, around Metz)

```json
{
  "hits": 1,
  "params": {"citycode": ["57463"], "rome": ["D1102"], "page": 1, "page_size": 2, "sort_by": "", "sort_direction": ""},
  "resolved_params": {
    "jobs": [{"type": "rome", "value": "D1102", "display": "Boulangerie - viennoiserie", "selection": ""}],
    "locations": [{"type": "city", "value": "57463", "display": "Metz", "geo": {"latitude": 49.1196, "longitude": 6.1764}, "selection": ""}]
  },
  "items": [{
    "id": 4636621, "siret": "30509416100034", "company_name": "MAISON NICOLAS", "office_name": "",
    "headcount_min": "3", "headcount_max": "5",
    "naf": "4781Z", "naf_label": "Commerce de détail alimentaire sur éventaires et marchés",
    "rome": "D1102",
    "location": {"lat": 49.1196, "lon": 6.17373},
    "city": "Metz", "citycode": "57463", "postcode": "57000",
    "department": "Moselle", "department_number": 57, "region": "Grand Est",
    "hiring_potential": 2.42, "is_high_potential": false
  }]
}
```
- `/nombreEntreprise` returns `{hits, params, resolved_params}`.
- `/potentielEmbauche` returns `{hits, items:[{id, siret, company_name, office_name, headcount_min, headcount_max, naf, naf_label, location{lat,lon}, city, citycode, postcode, department, region, department_number, hiring_potential, is_high_potential}]}`.
- Errors: 403 `{"error":"..."}`, 422 `{"errors":[{"param":["query","distance"],"message":"..."}]}`, 500 `{"error":"..."}`.
- **There are no email, phone or website fields anywhere in the LBB v2 schema.**

---

## 4. Limits, ToS, legal

### 4.1 Rate limits and quotas
- Each API page's technical sheet has a `nombreAppelMax`: **Offres = 100**, **LBB = 20** (LIVE-VERIFIED in the docs JSON).
- The portal's "Mon espace" computes and displays `floor(nombreAppelMax × tier)` **"appels / seconde"**, with tiers `bronze = 0.1`, `argent = 0.2`, `or = 0.4` (LIVE-VERIFIED in the francetravail.io JS bundle). That gives **Offres = 10 / 20 / 40 req/s** and **LBB = 2 / 4 / 8 req/s**.
- The default tier is very likely *bronze*, which matches the long-known "10 appels/s" for Offres. Confirm on your app page after subscribing.
- You can request a higher quota from the app page; support then contacts you.
- Overrun → **429** `text/plain` with `Retry-After`. The headers `X-Ratelimit-Burst-Capacity-Clientidlimiter` (max calls/s) and `X-Ratelimit-Remaining-Clientidlimiter` (calls left in the current second) are documented.
- Official resilience guidance ("Consommer une API de manière résiliente"):
  - Retry on 429, 502, 503 and 504 (500 at most twice), with 3 attempts, 1 s initial delay, ×2 factor, 30 s cap and ±20 % jitter.
  - On 401, refresh the token and retry once.
  - Never retry 400, 403 or 404.
  - Circuit breaker: open after 5 failures in 60 s, half-open after 30 s.
  - Stale cache: 5 min nominal, up to 24 h degraded.
  - Request timeout 5 s, and a dedicated connection pool per API family.
- Offres volume: about 300k+ active offers; the platform reports 40M calls/month (2023).

### 4.2 Licences
- **Offres d'emploi** falls under the *"Licence de réutilisation de la base de données des offres d'emploi de France Travail"* (licence spécifique, `produits-partages/documentation/conditions-dutilisation-api/licence-offres-emploi`). Key articles for jobbot:
  - **Art. 3**: the data may be accessed only through francetravail.io with an account. It may not be re-served or sub-licensed to third parties, and users must be prevented from bulk-extracting it. This is fine for a single-user personal tool. Don't publish a public mirror.
  - **Art. 4**: any display must mention the source (France Travail) and the last update date, plus a link to the licence. This only matters if jobbot's dashboard is shown to others; add a footer anyway.
  - **Art. 5.1**: never charge job seekers, and never sell offers.
  - **Art. 5.2**: if you republish, keep the original publication/update date and **poll the API at least once every 24 h** so that deleted or changed offers are reflected.
  - **Art. 5.3**: when displaying an offer, show its full content, logo included. This matters only for display to others.
  - **Art. 7**: once an offer has been **deleted** from the base, you may keep/use its content **only anonymised**. Remove the company name/description/URL, the contact name and details, the phone numbers, the offer URL at the company, the contact-method URL, and the postcode/INSEE/commune.
    - Practical jobbot rule: when `GET /v2/offres/{id}` returns 204, mark the offer `expired` and null the raw FT JSON blob.
    - Keep the candidate's own application record: date sent, generated documents, and the company name the candidate typed or sent to. That is the candidate's own data. **This nuance is not legally confirmed, see §7.**
  - **Art. 8 (GDPR)**: contact names and details are personal data. Use them only for the job-matching purpose, no marketing file, minimal retention, and EU storage.
  - **Art. 10**: the licence ends automatically after 12 consecutive months without API calls.
- **La Bonne Boite**: *Licence Ouverte / Etalab* (`idConditionUtilisation=1`).
  - The page asks that the France Travail logo appears when presenting search results.
  - The data is company-level (SIRET, name, NAF), not personal. Refreshed **monthly**.
- **Applying through francetravail.fr**: "Postuler" on the FT site requires a candidate login (`authentification-candidat.francetravail.fr`).
  - Automating that site with Playwright is **not** covered by the API licence and is risky under the site's CGU. The site returns a generic "Service indisponible" page on unexpected request patterns (observed today on a malformed autocomplete call).
  - Recommendation: automate only external `urlPostulation` / partner pages. For FT-only offers, open the page for a manual click or apply by email when the description contains an address.

### 4.3 CNIL / GDPR notes for the spontaneous-application module (context for the email-scraping part)
- LBB gives only company-level data. Emails must come from elsewhere.
- Sending an unsolicited application to a **generic professional address** (`rh@`, `recrutement@`, `contact@`) is ordinary correspondence.
- Harvesting **named individuals'** emails (e.g. `prenom.nom@`) makes jobbot a controller under GDPR. Art. 14 requires informing the person (a one-line notice in the email is enough), keeping only what is needed, and deleting after a set retention period (e.g. 6–12 months).
- Always offer an opt-out line and honour it (store an opt-out list).
- Keep volumes human-scale (no bulk mailing) to stay clearly outside "prospection".

---

## 5. Gotchas & anti-bot measures

1. **401 ≠ endpoint exists.** The gateway 401s any `/partenaire/*` path. Validate your path against the spec.
2. **`realm` goes in the query string** (`?realm=%2Fpartenaire`), not in the form body.
3. **206 is a success** that means "more pages". 204 means an empty body, so don't call `.json()` on it.
4. **Hard ceiling of 3150 results per query** (`range` start ≤ 3000, end ≤ 3149). Beyond that you get a 400. Partition by `departement` (≤ 5 per query), by `minCreationDate`/`maxCreationDate` windows (both required), or by ROME code.
5. **`typeContrat` OR `natureContrat`**: never mix them in one request if you want AND semantics. Alternance = `natureContrat=E2,FS`. The response's `alternance: true` is the easiest local check.
6. **Response `natureContrat` is a label, request `natureContrat` is a code.** Map them with `referentiel/naturesContrats`, cached.
7. **ROME 4.0 split.** Full-stack, front-end and back-end are appellations of **M1855**, not M1805. Offers are tagged with the new codes. Old code lists (M1805 only) miss most web-dev offers.
8. **`motsCles`**:
   - Keywords are ANDed. Each must be 2–99 chars. At most 7 keywords.
   - The documented charset has no accents, so send `developpeur`, not `développeur`, and use `unidecode`/`unicodedata`.
   - A ROME code also works as a keyword (the FT site does `motsCles=M1855`).
9. **`distance` defaults to 10 km** and silently adds a 30 % margin. Use `distance=0` for an exact commune.
10. **`publieeDepuis` is only safe with 1/3/7/14/31.** `minCreationDate` requires `maxCreationDate`.
11. **Spec typo**: `"dureeHebdo "` has a trailing space in the OpenAPI. Send `dureeHebdo`. Casing also differs: `appellationlibelle` vs `appellationLibelle`, `codePostal` vs `codepostal`.
12. **No email.** `contact.courriel` and `agence.courriel` hold `"Pour postuler, utiliser le lien suivant : https://candidat.francetravail.fr/offres/recherche/detail/<id>"`. Parse the URL out of that string if needed. `contact.coordonnees1/3` are also scrubbed if they contained an email.
13. **Partner offers vs API**: the API only includes partner offers whose publisher consented, so counts can be lower than on francetravail.fr.
14. **School/CFA spam in alternance.** Many E2/FS offers are posted by training organisations recruiting students (e.g. ISCOD today). Filter them with an `entreprise.nom` blocklist (ISCOD, Studi, OpenClassrooms, IFOCOP, "CFA", "École", "Academy"…) and description patterns ("formation financée", "rejoignez notre école", "titre RNCP", "trouver votre entreprise").
15. **No remote-work filter** exists. Regex the `description` and `intitule`.
16. **Referentiel incidents happen.** On 2025-12-03 the communes referential broke and `commune` searches were unavailable. Fall back to `departement` if `commune` queries start returning 400/500.
17. **Legacy hosts are dead** (`api.emploi-store.fr`, `api.pole-emploi.io`). `entreprise.pole-emploi.fr` still answers but is legacy. Use `entreprise.francetravail.fr` for the token.
18. **Token lifetime is ~1499 s.** Cache it with a margin of about 60 s. The 401 → refresh → retry-once pattern is the official guidance.
19. **LBB array params** must be repeated keys (`rome=A&rome=B`), unlike Offres, which uses comma-separated strings. httpx does the right thing with list values.
20. **LBB `distance` must be in the open interval (0, 200).** LBB `page_size` ≤ 100. `hiring_potential` is a 0–100 transformed score; `is_high_potential` is the boolean shortlist flag.
21. **Public-site scraping**: candidat.francetravail.fr is Tapestry server-rendered HTML with form tokens and "Service indisponible" fallbacks. Don't scrape it; use the API.

---

## 6. Concrete Python implementation notes (sync httpx)

### 6.1 Constants

```python
# src/jobbot/sources/francetravail/constants.py
TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
TOKEN_PARAMS = {"realm": "/partenaire"}          # query string, httpx encodes to %2F
API_BASE = "https://api.francetravail.io/partenaire"
OFFRES_SEARCH = f"{API_BASE}/offresdemploi/v2/offres/search"
OFFRES_DETAIL = f"{API_BASE}/offresdemploi/v2/offres/{{id}}"
OFFRES_REF = f"{API_BASE}/offresdemploi/v2/referentiel/{{name}}"
LBB_BASE = f"{API_BASE}/labonneboite/v2"
SCOPES_OFFRES = ("api_offresdemploiv2", "o2dsoffre")
SCOPES_LBB = ("api_labonneboitev2", "search", "office")

PAGE_SIZE = 150
MAX_START = 3000          # range start <= 3000
MAX_END = 3149            # range end   <= 3149
OFFRES_RPS = 8            # official 10/s (bronze) -> keep headroom
LBB_RPS = 1.5             # official 2/s (bronze)
TOKEN_SAFETY_S = 60

NATURE_ALTERNANCE = ("E2", "FS")   # E2 apprentissage, FS professionnalisation (verified)

# ROME 4.0 v61 (2026-06) developer family; keep <= 20 per request
ROME_DEV_PRIMARY = (
    "M1805",  # Développeur informatique (historic umbrella)
    "M1855",  # Développeur web  (appellations: full-stack 38976, front-end 200150, back-end 200151, web mobile 38745)
    "M1861",  # Développeur logiciel ou d'application
    "M1818",  # Ingénieur d'étude informatique
    "M1827",  # Ingénieur DevOps (also "Ingénieur développement logiciel")
    "M1841",  # Ingénieur informaticien
    "M1848",  # Analyste concepteur informatique
    "M1852",  # Analyste d'étude informatique (analyste-programmeur)
    "M1836",  # Ingénieur concepteur informatique
    "M1813",  # Intégrateur logiciels métiers
    "M1821",  # Analyste d'application informatique
    "M1837",  # Développeur multimédia
)
ROME_DEV_SECONDARY = ("M1815", "M1824", "M1811", "M1889", "M1831", "M1892", "M1887", "M1842", "M1832")
DEPTS_PRIMARY = ("57", "54")           # Moselle, Meurthe-et-Moselle
DEPTS_EXTENDED = ("55", "67", "88")    # Meuse, Bas-Rhin, Vosges
COMMUNE_METZ, COMMUNE_NANCY, COMMUNE_THIONVILLE = "57463", "54395", "57672"
REGION_GRAND_EST = "44"
```

### 6.2 Token cache + rate-limited client with retries

```python
import random, re, threading, time
from dataclasses import dataclass
import httpx

@dataclass(frozen=True)
class Token:
    value: str
    expires_at: float

class RateLimiter:
    """Simple token bucket, thread-safe, sync."""
    def __init__(self, rate_per_s: float, burst: int | None = None):
        self.rate, self.capacity = rate_per_s, burst or max(1, int(rate_per_s))
        self._tokens, self._last, self._lock = float(self.capacity), time.monotonic(), threading.Lock()
    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(wait)

class FranceTravailClient:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self, client_id: str, client_secret: str, scopes: tuple[str, ...],
                 http: httpx.Client | None = None, rps: float = 8):
        if not client_id or not client_secret:
            raise ValueError("FRANCE_TRAVAIL_CLIENT_ID / FRANCE_TRAVAIL_CLIENT_SECRET missing")
        self._cid, self._secret, self._scope = client_id, client_secret, " ".join(scopes)
        self._http = http or httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0),
                                          headers={"Accept": "application/json"})
        self._token: Token | None = None
        self._limiter = RateLimiter(rps)

    def _fetch_token(self) -> Token:
        r = self._http.post(TOKEN_URL, params=TOKEN_PARAMS, data={
            "grant_type": "client_credentials", "client_id": self._cid,
            "client_secret": self._secret, "scope": self._scope})
        if r.status_code != 200:
            # 400 invalid_client / invalid_scope -> config error, do not retry
            raise RuntimeError(f"FT token error {r.status_code}: {r.text[:300]}")
        body = r.json()
        return Token(body["access_token"], time.time() + int(body.get("expires_in", 1499)) - TOKEN_SAFETY_S)

    def _auth_header(self, force: bool = False) -> dict[str, str]:
        if force or self._token is None or time.time() >= self._token.expires_at:
            self._token = self._fetch_token()
        return {"Authorization": f"Bearer {self._token.value}"}

    def get(self, url: str, params=None) -> httpx.Response:
        refreshed = False
        for attempt in range(4):                       # 1 try + 3 retries
            self._limiter.acquire()
            try:
                r = self._http.get(url, params=params, headers=self._auth_header())
            except (httpx.ConnectError, httpx.ReadTimeout):
                if attempt == 3:
                    raise                                   # caller logs + marks source degraded
                self._sleep_backoff(attempt); continue
            if r.status_code == 401 and not refreshed:
                self._auth_header(force=True); refreshed = True; continue
            if r.status_code in self.RETRYABLE and attempt < 3:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.isdigit() else self._backoff(attempt)); continue
            return r                                    # 200/204/206/400/403/404 handled by caller
        return r

    @staticmethod
    def _backoff(attempt: int) -> float:
        base = min(30.0, 1.0 * (2 ** attempt))
        return base * random.uniform(0.8, 1.2)
    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self._backoff(attempt))
```

### 6.3 Paginated search (206 / Content-Range / 3150 cap)

```python
CONTENT_RANGE_RE = re.compile(r"offres\s+(\d+)-(\d+)/(\d+)")

def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    m = CONTENT_RANGE_RE.search(value)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None

def search_all(ft: FranceTravailClient, criteria: dict[str, str]) -> tuple[list[dict], int]:
    """Returns (offers, total_reported). Stops at the 3150 API ceiling."""
    offers: list[dict] = []
    start, total = 0, None
    while start <= MAX_START:
        end = min(start + PAGE_SIZE - 1, MAX_END)
        r = ft.get(OFFRES_SEARCH, params={**criteria, "range": f"{start}-{end}"})
        if r.status_code == 204:
            return offers, 0
        if r.status_code not in (200, 206):
            raise RuntimeError(f"FT search {r.status_code}: {r.text[:300]} params={criteria}")
        offers.extend(r.json().get("resultats", []))
        cr = parse_content_range(r.headers.get("Content-Range"))
        total = cr[2] if cr else total
        if r.status_code == 200 or cr is None or cr[1] + 1 >= cr[2]:
            break
        start = cr[1] + 1
    return offers, total or len(offers)
```
If `total > 3150`, split the criteria: per department (`departement` one at a time), or by day windows:
`minCreationDate=2026-09-01T00:00:00Z&maxCreationDate=2026-09-08T00:00:00Z`.

### 6.4 Query plan for this candidate (sync, daily cron)

```python
from unicodedata import normalize

def strip_accents(s: str) -> str:
    return normalize("NFKD", s).encode("ascii", "ignore").decode()

def queries(first_run: bool) -> list[dict[str, str]]:
    since = {"publieeDepuis": "31"} if first_run else {"publieeDepuis": "3"}   # or min/maxCreationDate
    rome = ",".join(ROME_DEV_PRIMARY)
    common = {"sort": "1", "codeROME": rome, **since}
    return [
        # A. Alternance in Lorraine (apprentissage + pro), incl. neighbouring depts
        {**common, "natureContrat": "E2,FS", "departement": "57,54", "inclureLimitrophes": "true"},
        # B. Alternance nationwide (to catch remote / hybrid) -> filter description locally
        {**common, "natureContrat": "E2,FS"},
        # C. Junior CDI in Lorraine
        {**common, "typeContrat": "CDI", "departement": "57,54", "experienceExigence": "D"},
        # D. Keyword fallback around Metz (catches badly-coded offers)
        {"sort": "1", **since, "motsCles": strip_accents("développeur"), "commune": COMMUNE_METZ, "distance": "60"},
        # E. Luxembourg: resolve code at runtime from /referentiel/pays (libelle == "Luxembourg")
        # {**common, "paysContinent": pays_code("Luxembourg")},
    ]
```
Post-filters (local, pure functions):
- `is_alternance = offer.get("alternance") or offer.get("natureContrat") in {"Contrat apprentissage", "Cont. professionnalisation"}`
- `is_school_spam`: company name or description matches the school/CFA blocklist.
- `is_remote`: regex `r"t[ée]l[ée]travail|full[\s-]?remote|100\s?%\s?remote|remote"` on `intitule + description`.
- `is_junior_ok = offer.get("experienceExige") == "D" or "junior" in title.lower()`.
- Dedupe by `id` across queries.
- Cross-source dedupe (HelloWork/Indeed scrapers): use `origineOffre.partenaires[].url` or a normalized (`entreprise.nom`, `intitule`, `lieuTravail.commune`).

### 6.5 Apply-target resolution

```python
_FT_LINK_RE = re.compile(r"https://candidat\.francetravail\.fr/offres/recherche/detail/\w+")

def apply_target(offer: dict) -> tuple[str, str]:
    """Returns (kind, url). kind in {'external_form','partner','francetravail'}."""
    contact = offer.get("contact") or {}
    if url := contact.get("urlPostulation"):
        return "external_form", url                       # ATS / career page -> Playwright
    partners = (offer.get("origineOffre") or {}).get("partenaires") or []
    if partners and partners[0].get("url"):
        return "partner", partners[0]["url"]              # HelloWork, etc. -> that site's module
    return "francetravail", f"https://candidat.francetravail.fr/offres/recherche/detail/{offer['id']}"
```
- Note: `origineOffre.urlOrigine` is the partner URL for partner offers and the FT page for FT offers.
- Also mine `description` for emails (`[\w.+-]+@[\w-]+\.[\w.]+`). Some recruiters put their address in the free text; that is legitimate to use.

### 6.6 Offer lifecycle (licence Art. 5.2 / 7)

- Store in sqlite: `offers(source TEXT, id TEXT, raw_json TEXT, date_creation TEXT, date_actualisation TEXT, first_seen TEXT, last_seen TEXT, status TEXT CHECK(status IN ('active','expired')), apply_kind TEXT, apply_url TEXT, PRIMARY KEY(source,id))`.
- Once a day, for offers still tracked, call `GET /v2/offres/{id}`:
  - `200` → update `last_seen` and `date_actualisation`.
  - `204` → set `status='expired'`, `raw_json=NULL`, and clear the company/contact columns (Art. 7). Keep the `applications` row the candidate created.
- Cache referentiels (`naturesContrats`, `typesContrats`, `pays`, `departements`, `metiers`) in a `referentiels(name, code, libelle, fetched_at)` table and refresh them weekly.

### 6.7 La Bonne Boite client (spontaneous-application targets)

```python
def lbb_search(ft: FranceTravailClient, citycode: str, romes: tuple[str, ...],
               distance_km: int = 30, page: int = 1, page_size: int = 100) -> dict:
    if not 0 < distance_km < 200:
        raise ValueError("LBB distance must be in ]0, 200[")
    params: list[tuple[str, str | int]] = [("citycode", citycode), ("distance", distance_km),
                                           ("page", page), ("page_size", min(page_size, 100)),
                                           ("sort_by", "hiring_potential"), ("sort_direction", "desc")]
    params += [("rome", r) for r in romes]               # repeated keys
    r = ft.get(f"{LBB_BASE}/recherche", params=params)
    if r.status_code == 422:
        raise ValueError(f"LBB validation: {r.json().get('errors')}")
    r.raise_for_status()
    return r.json()          # {"hits", "params", "resolved_params", "items":[...]}
```
- Use a separate `FranceTravailClient(..., scopes=SCOPES_LBB, rps=LBB_RPS)`, or one client with all scopes and two limiters.
- Iterate `page` until `page * page_size >= hits`.
- Output → `companies(siret PRIMARY KEY, name, naf, naf_label, headcount_min, headcount_max, lat, lon, city, postcode, hiring_potential, is_high_potential, source='lbb', fetched_at)`.
- Then hand `siret`/`company_name`/`city` to the enrichment module (website discovery → contact page crawl → generic email; optional Hunter; SMTP/MX validation). LBB itself returns **no** email.
- Good ROME inputs for LBB: `M1805, M1855, M1861` (the dataset is monthly and scored per ROME×NAF). Also try `naf=6201Z` (programmation informatique), `6202A` (conseil en SI) and `6312Z` with the `naf` param.

### 6.8 ROME referential without auth (for scoring/mapping)
- `GET https://api.francetravail.fr/api-nomenclatureemploi/v1/open-data/csv` returns `RefRomeCsv.zip` (4.5 MB, **no auth**, ROME 4.0 **v61, published 2026-06-15**). LIVE-VERIFIED.
- Useful files: `unix_referentiel_code_rome_v461_utf8.csv` (`code_rome`, `libelle_rome`, `code_rome_parent`…) and `unix_referentiel_appellation_v461_utf8.csv` (`code_ogr` = appellation code, `libelle_appellation_long`, `code_rome`…).
- The skills and savoirs files (`unix_referentiel_competence…`, `unix_referentiel_savoir…`) can feed the ATS keyword extractor with official skill labels. The CSV also exists as `…/open-data/json` and `…/open-data/xml`.

### 6.9 Tests
- Use `respx` (already in dev deps) to mock:
  - the token POST (200 JSON, 400 invalid_client);
  - search returning a 206 with `Content-Range: offres 0-149/320`, then a 206 with `offres 150-299/320`, then a 200 with `offres 300-319/320`;
  - a 204 (no body);
  - a 429 with `Retry-After: 1`;
  - a 401, then success after token refresh.
- Mark real calls with `@pytest.mark.live`.

---

## 7. Claims table

Legend:
- **LIVE-VERIFIED**: observed today (2026-09-23) over HTTP.
- **DOC-SOURCED**: stated by official docs or specs (fetched live today unless noted).
- **UNCERTAIN**: inferred, conflicting or not checkable without credentials.

| # | Claim | Status | Source / evidence |
|---|---|---|---|
| 1 | Token endpoint `POST https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire` exists; bad creds → 400 `invalid_client` JSON | LIVE-VERIFIED | curl today |
| 2 | Token endpoint GET → 405 `method_not_allowed`; missing grant_type → 400 `invalid_request` "Grant type is not set" | LIVE-VERIFIED | curl today |
| 3 | `authentification-pro.francetravail.fr` and `authentification-partenaire.francetravail.io` serve the same token endpoint (400 invalid_client) | LIVE-VERIFIED | curl today |
| 4 | OIDC discovery exists; supports `client_credentials`, `client_secret_post` and `client_secret_basic`; `entreprise.*` .well-known → 308 to `authentification-pro` | LIVE-VERIFIED | https://authentification-pro.francetravail.fr/connexion/oauth2/realms/root/realms/partenaire/.well-known/openid-configuration |
| 5 | `api.francetravail.io` returns 401 + `WWW-Authenticate: Bearer` with no or invalid token, **including for non-existent `/partenaire/*` paths** | LIVE-VERIFIED | curl today (`/partenaire/doesnotexist/v9/foo` → 401, `/foo` → 404) |
| 6 | `api.pole-emploi.io` and `api.emploi-store.fr` are unreachable | LIVE-VERIFIED | curl today (connection failed) |
| 7 | Offres API id 84 and LBB id 360 are "up" today | LIVE-VERIFIED | https://francetravail.io/api-peio/v2/api/status |
| 8 | Official Offres spec v2.01: server `https://api.francetravail.io/partenaire/offresdemploi`, scopes `o2dsoffre` + `api_offresdemploiv2` | DOC-SOURCED (live-fetched) | https://francetravail.io/api-peio/v2/api/84/openapi |
| 9 | Token request format and response (`expires_in` 1499, `token_type` Bearer, scope list space-separated) | DOC-SOURCED | https://francetravail.io/produits-partages/documentation/utilisation-api-france-travail/client-credentials |
| 10 | `range`: ≤ 150 per call, start ≤ 3000, end ≤ 3149 | DOC-SOURCED | spec + HTML doc (page https://francetravail.io/produits-partages/catalogue/offres-emploi; raw JSON incl. `ficheTechniqueApi.documentationApi.ressources[]`: https://francetravail.io/api-peio/v2/pages/page?slug=produits-partages/catalogue/offres-emploi) |
| 11 | 200 = all results; 206 = more available; 204 = none with `Content-Range: */0`; `Content-Range: offres p-d/t`; `Accept-Range: 150` | DOC-SOURCED | spec 84 + HTML doc resource "Rechercher par critères" |
| 12 | `typeContrat` and `natureContrat` are combined with **OR** | DOC-SOURCED | spec 84 param descriptions |
| 13 | `natureContrat` E2 = "Contrat apprentissage", FS = "Cont. professionnalisation" | LIVE-VERIFIED | candidat.francetravail.fr search `natureOffre=E2,FS` filter tags today |
| 14 | E1 = "Contrat travail"; `NS` appears as a facet value | DOC-SOURCED | referentiel example + search example in HTML doc |
| 15 | Other nature codes (FA, FJ, FT, FU, I1, PS…) and their labels | UNCERTAIN | not verified; fetch `/v2/referentiel/naturesContrats` at runtime |
| 16 | An alternance offer shows `typeContrat` CDD ("CDD - 12 Mois") plus nature "Contrat apprentissage" | LIVE-VERIFIED | https://candidat.francetravail.fr/offres/recherche/detail/6927986 |
| 17 | Response `natureContrat` is a label string (e.g. "Contrat travail") | DOC-SOURCED | spec 84 schema `Offre.natureContrat` example |
| 18 | `contact.courriel` and `agence.courriel` no longer contain emails, only "Pour postuler, utiliser le lien suivant : https://candidat.francetravail.fr/offres/recherche/detail/XXXXXXX" | DOC-SOURCED | spec 84 schemas `Contact`, `Agence` |
| 19 | `contact.urlPostulation` = application form URL (partner offers) | DOC-SOURCED | spec 84 + HTML doc |
| 20 | Full list of search params (§2.3) incl. `experienceExigence`, `grandDomaine` M18, `inclureLimitrophes`, `minCreationDate`/`maxCreationDate` pair | DOC-SOURCED | spec 84 + HTML doc |
| 21 | `publieeDepuis` allowed values 1/3/7/14/31 | DOC-SOURCED | HTML doc (spec says only "integer") |
| 22 | `codeROME` max values: 200 (spec) vs 20 (HTML doc) | UNCERTAIN (conflict) | keep ≤ 20 |
| 23 | `motsCles`: AND logic, 2–99 chars each, ≤ 7 keywords, ASCII charset | DOC-SOURCED | spec 84 + HTML doc + 400 example message |
| 24 | Accented keywords are rejected | UNCERTAIN | inferred from the documented charset; strip accents anyway |
| 25 | `distance` default 10 km, +30 % margin, `distance=0` = exact commune | DOC-SOURCED | spec 84 + HTML doc |
| 26 | Spec param name `"dureeHebdo "` has a trailing space (spec typo) | DOC-SOURCED | spec 84 file |
| 27 | Field casing differs: `appellationlibelle`/`codePostal` (spec) vs `appellationLibelle`/`codepostal` (HTML doc) | DOC-SOURCED | both docs; which one the live API sends is UNCERTAIN |
| 28 | Offres technical sheet `nombreAppelMax=100`; LBB `nombreAppelMax=20` | LIVE-VERIFIED | https://francetravail.io/api-peio/v2/pages/page?slug=produits-partages/catalogue/offres-emploi and `?slug=produits-partages/catalogue/bonne-boite-v2` |
| 29 | Portal shows quota = floor(nombreAppelMax × {bronze 0.1, argent 0.2, or 0.4}) "appels / seconde" | LIVE-VERIFIED | francetravail.io JS chunk (`getApiQuota`, `ee={bronze:.1,argent:.2,or:.4}`) |
| 30 | Default tier is bronze → Offres 10 req/s, LBB 2 req/s | UNCERTAIN (very likely) | consistent with the long-documented "10 appels/s"; check the app page after signup |
| 31 | 429 + `Retry-After`; headers `X-Ratelimit-Burst-Capacity-Clientidlimiter`, `X-Ratelimit-Remaining-Clientidlimiter` | DOC-SOURCED | https://francetravail.io/produits-partages/documentation/utilisation-api-france-travail/erreurs-frequentes |
| 32 | Official resilience parameters (3 retries, 1 s ×2 up to 30 s, ±20 % jitter, 401 refresh once, no retry on 400/403/404) | DOC-SOURCED | https://francetravail.io/produits-partages/documentation/consommation-resiliente |
| 33 | ROME 4.0 v61 published 2026-06-15; M1855 "Développeur / Développeuse web" (parent M1805) holds full-stack 38976, front-end 200150, back-end 200151; the other M18xx codes as listed in §6.1 | LIVE-VERIFIED | https://api.francetravail.fr/api-nomenclatureemploi/v1/open-data/csv (RefRomeCsv.zip) |
| 34 | Offers are tagged with new ROME 4.0 codes (Moselle today: M1855 = 8, M1805 = 11, M1861 = 0) | LIVE-VERIFIED | candidat.francetravail.fr `motsCles=<code>&lieux=57D` |
| 35 | API `codeROME=M1855` returns the same set as the public site | UNCERTAIN | same backend likely; not testable without a token |
| 36 | INSEE: Metz 57463, Nancy 54395, Thionville 57672; Grand Est region 44 | LIVE-VERIFIED | https://geo.api.gouv.fr/communes?nom=… |
| 37 | Luxembourg code for `paysContinent` | UNCERTAIN | resolve from `/v2/referentiel/pays` at runtime |
| 38 | LBB v2 spec 2.27.3: base `https://api.francetravail.io/partenaire/labonneboite/v2`, endpoints `/recherche`, `/nombreEntreprise`, `/potentielEmbauche`; scopes `api_labonneboitev2 search office` | DOC-SOURCED (live-fetched) | https://francetravail.io/api-peio/v2/api/360/openapi |
| 39 | LBB returns **no email/phone/website** (fields: siret, company_name, office_name, headcount_min/max, naf, naf_label, location, city, citycode, postcode, department(_number), region, hiring_potential, is_high_potential, rome) | DOC-SOURCED | spec 360 schemas |
| 40 | LBB `page_size` max 100, `distance` in ]0, 200[, array params, 422 validation errors | DOC-SOURCED | spec 360 |
| 41 | LBB data refreshed monthly; Licence Ouverte (Etalab); FT logo required when presenting results | DOC-SOURCED | https://francetravail.io/produits-partages/catalogue/bonne-boite-v2 |
| 42 | LBB v1 (`/labonneboite/v1/company/`) is retired (the docs error example shows `api_labonneboitev1` as an invalid scope) | UNCERTAIN | inferred; the gateway 401s every path, so it can't be probed |
| 43 | Registration: account → application (name, description, URL) → "Utiliser l'API" on each API → one client_id/secret per app; Offres & LBB are "accès libre" | DOC-SOURCED | https://francetravail.io/produits-partages/documentation/gestion-compte-applications, /conditions-dutilisation-api |
| 44 | Offres licence: no third-party redistribution (Art. 3), source attribution (Art. 4), refresh ≥ every 24 h if republishing (Art. 5.2), anonymise deleted offers (Art. 7), GDPR on contact data (Art. 8), auto-termination after 12 months without calls (Art. 10) | DOC-SOURCED | https://francetravail.io/produits-partages/documentation/conditions-dutilisation-api/licence-offres-emploi |
| 45 | Keeping the candidate's own application record (company name) after offer deletion is compatible with Art. 7 | UNCERTAIN | legal interpretation; minimise to be safe |
| 46 | `GET /v2/offres/{id}` returns 204 when the offer no longer exists | DOC-SOURCED | spec 84 + HTML doc error cases |
| 47 | Schools/training companies post "alternance" offers (e.g. ISCOD in Metz today) | LIVE-VERIFIED | candidat.francetravail.fr search today |
| 48 | Incidents: 2025-12-03 communes referential outage (no `commune` search); 2026-06-09 Offres outage; 2026-03-30 auth incident | LIVE-VERIFIED | https://francetravail.io/api-peio/v2/incident |
| 49 | ROME Métiers API v1: base `https://api.francetravail.io/partenaire/rome-metiers`, scopes `api_rome-metiersv1 nomenclatureRome` | DOC-SOURCED (live-fetched) | https://francetravail.io/api-peio/v2/api/273/openapi |
| 50 | La bonne alternance API: alternance offers, 60 req/60 s, API endpoint to send applications, non-commercial use | UNCERTAIN (search-result summary only) | https://api.apprentissage.beta.gouv.fr/en/explorer/recherche-offre, https://www.data.gouv.fr/dataservices/api-la-bonne-alternance |
| 51 | Legacy scope `application_<client_id>` is optional today | UNCERTAIN | current docs omit it; community code still sends it |
