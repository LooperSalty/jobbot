# ATS, CV & cover-letter optimisation (France, 2026): jobbot research

> Researched on 2026-09-23. Evidence comes from:
> - live probes (curl + Playwright, read-only, no form ever submitted) of Greenhouse, Lever, SmartRecruiters, Workday CXS, Teamtailor, HelloWork, Talentsoft, Jobaffinity, Taleez and the ESCO API;
> - a local PDF/DOCX round-trip experiment (Playwright 1.58 `page.pdf` → pypdf 6.x + pdfminer.six, and python-docx 1.2);
> - official sources (Légifrance, code.travail.gouv.fr, entreprendre.service-public.gouv.fr, France Travail, Apec, Oracle/Workday/Greenhouse/LinkedIn docs);
> - vendor pages from Jobscan, Teal, SkillSyncer, Resume Worded and CVDesignR.
>
> Scratch scripts and raw responses were kept out of the repo on purpose.
>
> Candidate context: junior full-stack developer (BUT Informatique, IUT Exemple). Target: a 24-month Bac+5 alternance at **Exemple Digital School** (programme "Expert en Ingénierie du développement et en architecture logicielle", RNCP 36469, level 7, starting October 2026, rhythm **1 week at school / 3 weeks at the company**), or a junior CDI. Locations: Metz, Nancy, Thionville, Luxembourg, remote.

---

## 1. TL;DR and recommendation for jobbot

**Things no one publishes:**
- No vendor publishes its scoring formula. Jobscan, Teal, SkillSyncer and Resume Worded all say only that hard skills weigh most, followed by job title, education, soft skills and other keywords. All four recommend a target of **about 80 %**; Jobscan says 75 % is often enough.
- No public source gives exact ATS market shares for France.

**What is known about the market and the parsers:**
- **Market segments:**
  - Large groups (and the ESN / IT consultancies that hire many juniors and alternants around Metz and Luxembourg) mostly use **Workday, SAP SuccessFactors (plus SmartRecruiters, which SAP bought in September 2025), Oracle Taleo / Oracle Recruiting, Cegid Talentsoft / Digitalrecruiters, Avature and iCIMS**.
  - SMEs and start-ups mostly use **Teamtailor, Flatchr, Beetween, Welcome to the Jungle ATS (ex-Welcome Kit), Taleez, Jobaffinity, Lever, Recruitee and Factorial**.
- **Parser engines:**
  - SuccessFactors (and iCIMS) license **Textkernel** (which also absorbed Sovren).
  - Oracle Taleo uses a third-party parser. It rates **French (France) as "Limited"**.
  - Workday uses its own ML parser. Its docs say results "vary based on resume format and order of words". The parser **does not auto-fill the Skills and Languages fields**; skills are suggested by ML from the whole resume.
- **AI scoring is now the norm:**
  - Workday HiredScore grades candidates A to D.
  - Workday Skills Cloud produces match scores.
  - The WTTJ ATS scores each candidate against the recruiter's criteria and explains why.
  - Flatchr offers IA matching.
  - Greenhouse flags hidden-text prompt injection.

  Keyword stuffing and hidden text are therefore **dangerous**, not merely useless.
- **Layout (LIVE-VERIFIED in our round-trip test):**
  - Two-column PDFs change reading order depending on the extractor. pdfminer attached the sidebar (contact details, skills) *under* "Expérience professionnelle"; pypdf put the sidebar first.
  - Contact details placed in a PDF or DOCX **header** end up at the end of the text, at the start, or nowhere, depending on the parser. In DOCX they live in `word/header1.xml`, not in `document.xml`.
  - Icons survive extraction as noise glyphs (✉ ☎ 📍).
- **Format rules:**
  - Use **one column, no tables or text boxes, and contact details in the body**.
  - Use standard French headings.
  - Use Arial/Calibri at 10–12 pt.
  - Write dates as "Septembre 2023 – Juin 2026", or MM/YYYY, and keep them consistent.
  - A junior CV fits on **one page**.
  - Keep files **≤ 2 MB**. This is the limit on LinkedIn Easy Apply and HelloWork, and Taleo defaults to 1 MB.

**Recommendation (to implement):**
1. **Single source of truth:** keep a YAML "master profile" holding the true skills inventory, experiences and projects, with each bullet tagged by the skills it evidences. Render it through two renderers:
   - **PDF:** Jinja2 HTML → Playwright `page.pdf(tagged=True)`, one column.
   - **DOCX:** python-docx using built-in `Heading 1` styles, with no tables, headers or text boxes.
2. **Choose the format per ATS** from the apply URL (see the §2 detection table):
   - Taleo (`*.taleo.net`), SuccessFactors, and any form asking for Word → **DOCX**.
   - Everything else → text **PDF**.
   - Always respect the `<input type=file accept>` value read with Playwright before uploading.
3. **Deterministic `ats_score` (0–100)** as specified in §6.3. The main weights are:
   - hard skills 45 %
   - job title 15 %
   - soft skills 10 %
   - education/contract fit 10 %
   - other keywords 5 %
   - language 5 %
   - file searchability 10 %

   Also apply stuffing penalties and hard blocks for hidden text or skills that are not true. Thresholds:
   - **≥ 80** → apply.
   - **70–79** → auto-tailor again.
   - **60–69** → apply only to priority targets.
   - **< 60** → skip, or send a spontaneous application only.
   - **Stop optimising at 85–90** to avoid stuffing.
4. **Deterministic `lm_score` (0–100)** for the lettre de motivation, as specified in §6.6. It checks:
   - the vous/moi/nous structure;
   - 250–400 words;
   - the company name and one specific fact about the company;
   - the top-3 JD hard skills;
   - the alternance block (school, diploma, rhythm, start date, duration);
   - the formal closing;
   - no placeholders and no clichés.
5. **Tailoring is constrained:** tailoring (LLM or rules) may only **reorder, rephrase and mirror JD spelling** of skills that exist in the master profile. Every new skill token that is not in the inventory is a hard block.
6. **After every upload, verify what the ATS parsed.** Workday (`includeResumeParsing: true` in the CXS job JSON), Lever ("Couldn't auto-read resume.") and Teamtailor pre-fill fields from the CV. Read those fields back with Playwright and fix them before submitting.
7. **Use legal facts as the urgency engine in alternance messaging:**
   - A candidate with no employer may start the school cycle as a *stagiaire de la formation professionnelle* for at most **3 months** (art. L6222-12-1). For an October 2026 rentrée, the realistic signing deadline is **early January 2027**, and the contract is shortened by the months already elapsed.
   - State hiring aid for contracts **starting before 1 January 2027** at level 7 (Bac+5) is **€2,000 for companies under 250 employees and €750 for 250 or more** (decree 2026-168). A mandatory €750 employer participation applies to level 6–7 contracts (decree 2025-585).
   - The 2027 aid is only *announced* as maintained (Prime Minister, 9 September 2026). No decree yet.

**Open questions for the candidate (to put in jobbot's onboarding):**
1. What is the exact EDS rentrée date, and does the school confirm the 3-month "sans employeur" window, or does it apply a stricter internal deadline?
2. Date of birth. Apprenticeship requires being 29 or under at the start; the L6222-12-1 stagiaire status covers ages 16–29.
3. Do you hold a driving licence (permis B) and have a car? This matters for Moselle and Thionville SMEs.
4. What is your English level (CECRL)? It matters for Luxembourg offers.
5. What are your GitHub and portfolio URLs, and which 2–3 projects should be showcased?
6. Do you prefer a photo? Recommendation: no photo in the ATS version, optional in the email version.
7. For the CDI track: salary expectation and date of availability.
8. Optional and sensitive (never required): an RQTH raises the aid to €6,000. Mention it only if you choose to.
9. Alternance with a Luxembourg employer seems unavailable for Bac+5 through the Luxembourg cross-border scheme (§4). Should Luxembourg be CDI-only?

---

## 2. Endpoints / URLs & auth

None of these need credentials. They are the read-only sources jobbot uses to fetch the job description (JD) text and the upload constraints, and to detect the ATS.

### 2.1 Public JD / form endpoints (LIVE-VERIFIED unless noted)

| ATS | Method + URL | Params | Notes |
|---|---|---|---|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs` | `content=true` (optional) | 200 JSON `{"jobs":[...]}`. An unknown board returns `404 {"status":404,"error":"Job not found"}`. |
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true` | `questions=true` | Returns `questions[]`, including `{"label":"Resume/CV","fields":[{"name":"resume","type":"input_file"},{"name":"resume_text","type":"textarea"}]}`, plus `language`, `data_compliance`, `ai_disclaimer`, `demographic_questions`, `location_questions`. |
| Greenhouse | `GET https://job-boards.greenhouse.io/embed/job_app?for={token}&token={id}` | – | The HTML form has `accept=".pdf,.doc,.docx,.txt,.rtf"` and `allowed_filetypes":["pdf","doc","docx","txt","rtf"]`. |
| Lever | `GET https://api.lever.co/v0/postings/{company}?mode=json&limit=N` | `mode=json`, `limit`, `skip`, `location`, `commitment`, `team` (per Lever docs; only `mode`/`limit` probed) | 200 JSON array. An unknown company returns `404 {"ok":false,"error":"Document not found"}` (probed with `mirakl`, `ledger`, `backmarket`, `spendesk`). `qonto`, `contentsquare` and `swile` return 200. |
| Lever | `GET https://api.lever.co/v0/postings/{company}/{id}` | – | Keys: `text, categories{commitment,department,location,team,allLocations}, descriptionPlain, lists[], additionalPlain, hostedUrl, applyUrl, workplaceType, country`. |
| Lever | `GET https://jobs.lever.co/{company}/{id}/apply` | – | The HTML has `input[name=resume][type=file]` with **no `accept`**, and the text "Couldn't auto-read resume." Lever parses the file on upload to pre-fill fields. |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings` | `limit`, `offset`, `country=fr` (probed); `q`, `city` (Posting API docs, UNCERTAIN) | 200 `{"offset","limit","totalFound","content":[...]}`. An unknown company returns **200 with `totalFound:0`**, not 404. |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings/{postingId}` | – | Keys include `language{code:"fr"}`, `applyUrl`, `experienceLevel`, `typeOfEmployment`, and `jobAd.sections{companyDescription, jobDescription, qualifications, additionalInformation}`. **`qualifications` is the "requirements" block**, which is useful for required vs nice-to-have. |
| SmartRecruiters | Apply UI: `https://www.smartr.me/oneclick-ui/company/{companyId}/publication/{uuid}?dcr_ci={companyId}` | – | This is the "Je suis intéressé(e)" link on `jobs.smartrecruiters.com/{companyId}/{postingId}`. The form is a JS app. |
| Workday | `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | JSON body `{"appliedFacets":{},"limit":20,"offset":0,"searchText":"alternance"}`, headers `Content-Type: application/json`, `Accept: application/json` | 200 `{"total", "jobPostings":[{title, externalPath, locationsText, postedOn, startDate, bulletFields}], "facets"}` (probed on `sanofi/SanofiCareers`). A wrong site returns `404 {"errorCode":"S21",...,"message":"not found: Job_Posting_Site_ID=NoSuchSite"}`. |
| Workday | `GET https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}` | – | `jobPostingInfo{title, jobDescription(HTML), location, startDate, timeType, jobReqId, country, canApply, includeResumeParsing, questionnaireId, timeLeftToApply, externalUrl}`, plus `hiringOrganization`. **`includeResumeParsing: true`** tells jobbot that the form will be pre-filled from the CV. Applying requires a Workday candidate account per tenant (not probed). |
| Teamtailor | `https://{company}.teamtailor.com/jobs/{id}-{slug}` → click "Postuler" | – | The rendered form has `accept=".doc,.docx,.pptx,.pdf,.pages,.txt,.rtf"` (`groupeoci.teamtailor.com`). `.../applications/new` returned 404; the form opens inline or as a modal. |
| HelloWork (CV upload) | `https://www.hellowork.com/fr-fr/candidat/depot-cv-etape1.html` | – | Page text: "Fichiers acceptés : .pdf, .doc, .docx, .odt, .rtf, .jpg ou .png (max 2Mo)". `accept=".rtf,.dot,.doc,.docx,.pdf,.odt,.wpd,.ods,.gif,.png,.jpg,.jpeg,.bmp"`. The help centre says the same: "Format : PDF, DOC, DOCX, ODT, RTF, JPG, PNG – Taille : ≤ 2 Mo". |
| Talentsoft (Cegid) | `https://{client}-career.talent-soft.com/offre-de-emploi/emploi-{slug}_{id}.aspx` (also `-recrute`, `-cand` subdomains) | `LCID=1036` (FR) | Applying requires a candidate account ("Je me crée un espace candidat" → `/my-account/log-in.aspx`). Spontaneous application: `/mon-compte/mon-fichier-cv.aspx`. The probed offer (id 682) had been closed and redirected to an error page. |
| Jobaffinity | `https://jobaffinity.fr/apply/{token}` | – | The form needs JavaScript ("Vous devez activez JavaScript pour afficher le formulaire de candidature"). Closed offers show "Ce poste n'est plus ouvert." |
| Flatchr | `https://careers.flatchr.io/vacancy/{id}-{slug}` | – | The page is 200 while the offer is open and redirects to `/fr/404-not-found/` when closed. Help centre: "formats CV acceptés : Word ou PDF". |
| Taleez | `https://taleez.com/apply/{slug}` and `https://{company}.taleez.com/` | – | 200. Clicking the "POSTULER" button timed out in headless mode. |
| LinkedIn Easy Apply | – (other research file) | – | LinkedIn Help: "the file format must be either Microsoft Word or PDF", "We recommend a file size less than 2MB", "We'll store your four most recently uploaded resumes". |

### 2.2 ATS detection (URL/host patterns) and format policy

Only the rows marked LIVE were seen in probes. The others are DOC-SOURCED or UNCERTAIN; match them loosely.

| Host / URL regex | ATS | Upload format jobbot should use | Evidence |
|---|---|---|---|
| `\.myworkdayjobs\.com` / `/wday/cxs/` | Workday | PDF (text), or DOCX if the `accept` value lacks pdf. Fill the Skills field manually. | LIVE (Sanofi) |
| `\.taleo\.net` (`/careersection/`) | Oracle Taleo | **DOCX**, ≤ 1 MB, plainest layout | DOC (Oracle) |
| `successfactors\.(eu|com)`, `career\d+\.sapsf\.`, `jobs\.sap\.com`, `rmk-map` | SAP SuccessFactors | **DOCX** (PDF parse support unclear) | UNCERTAIN |
| `jobs\.smartrecruiters\.com`, `smartr\.me/oneclick-ui` | SmartRecruiters | PDF | LIVE |
| `boards\.greenhouse\.io`, `job-boards\.greenhouse\.io`, `gh_jid=` | Greenhouse | PDF | LIVE |
| `jobs\.lever\.co` | Lever | PDF | LIVE |
| `\.teamtailor\.com` (plus custom career domains that serve Teamtailor assets) | Teamtailor | PDF | LIVE |
| `careers\.flatchr\.io` | Flatchr | PDF | LIVE |
| `welcometothejungle\.com/.+/jobs/`, `welcomekit\.co` | WTTJ ATS | PDF (reportedly ≤ 5 MB) | UNCERTAIN (help page returned 403) |
| `jobaffinity\.fr/apply/` | Jobaffinity | PDF | LIVE |
| `talent-soft\.com` | Cegid Talentsoft | PDF or DOCX (account needed) | LIVE |
| `\.digitalrecruiters\.com` | Cegid Digitalrecruiters | PDF | DOC (URL pattern from search results) |
| `taleez\.com/apply/` | Taleez | PDF | LIVE |
| `beetween` | Beetween | PDF | UNCERTAIN |
| `apply\.workable\.com`, `\.recruitee\.com`, `boondmanager` | Workable / Recruitee / BoondManager (ESN) | PDF | UNCERTAIN |
| `hellowork\.com` | HelloWork (job board + "Buddi" ATS) | PDF ≤ 2 MB | LIVE |

**Always override the table** with the actual `accept` attribute and any "Mo/MB" hint text found in the form (Playwright, before upload).

### 2.3 Skill-label source (optional): ESCO API (LIVE-VERIFIED, no auth)

- `GET https://ec.europa.eu/esco/api/search?text=JavaScript&language=fr&type=skill&limit=3&full=false` → 200. Keys: `total, offset, limit, text, language, type, _embedded.results[{uri,title,preferredLabel{..},className}]`.
- `GET https://ec.europa.eu/esco/api/resource/skill?uri={uri}&language=fr` → 200. Keys: `preferredLabel{fr,en,...}`, `alternativeLabel{fr:[...]}`, `description`.
- **Do not trust the alternative labels blindly.** For JavaScript, ESCO's French alt labels are `JavaScript 1.7, Escript, CSJS, Live Script, Server-side JavaScript, Mocha, ...`. "Mocha" is also a JS test framework, and "JS" is missing. Use the hand-curated lexicon in §6.2 as the primary source; ESCO is at most a fallback.

### 2.4 Legal / official references used (for messaging content)

- Art. L6222-12, L6222-12-1, L6222-7-1 Code du travail:
  - `https://code.travail.gouv.fr/code-du-travail/l6222-12`
  - `https://code.travail.gouv.fr/code-du-travail/l6222-12-1`
  - `https://code.travail.gouv.fr/code-du-travail/l6222-7-1`
- Décret n° 2026-168 du 6 mars 2026 (aide exceptionnelle): `https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000053634597`
- Service-public fiche F23556 (aides apprentissage; verified 22/07/2026): `https://entreprendre.service-public.gouv.fr/vosdroits/F23556`
- Décret n° 2025-585 du 27 juin 2025 (participation employeur 750 €): `https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000051808276`

---

## 3. Response / page shape (trimmed real samples)

**Greenhouse `?questions=true`** (datadog / 7194969):
```json
{"id":7194969,"title":"...","language":"en","location":{"name":"Paris, France"},
 "data_compliance":[{"type":"gdpr","requires_consent":false,"demographic_data_consent_applies":true}],
 "questions":[
  {"label":"First Name","required":true,"fields":[{"name":"first_name","type":"input_text","values":[]}]},
  {"label":"Resume/CV","required":true,"fields":[{"name":"resume","type":"input_file","values":[]},
                                                {"name":"resume_text","type":"textarea","values":[]}]},
  {"label":"LinkedIn Profile","required":false,"fields":[{"name":"question_59674544","type":"input_text","values":[]}]}],
 "ai_disclaimer":null,"include_ai_disclaimer":null,"ai_opt_out_request_url":null}
```

**SmartRecruiters list item** (Ubisoft2). Note the gender marker "(F/H/NB)" in the title:
```json
{"id":"744000151346470","name":"Stage 6 mois - Vegetation Artist - (F/H/NB) [AAA project]",
 "uuid":"9b2acf08-50f4-4479-b454-781eb8c8f345","refNumber":"REF32004W",
 "company":{"identifier":"Ubisoft2","name":"Ubisoft"},"releasedDate":"2026-09-23T10:30:54.347Z",
 "location":{"city":"Annecy","country":"fr","remote":false,"hybrid":false,"fullLocation":"Annecy, Auvergne-Rhône-Alpes, France"},
 "typeOfEmployment":{"id":"permanent","label":"Full-time"},"experienceLevel":{"id":"internship","label":"Internship"}}
```
The detail endpoint adds `language:{"code":"fr","label":"French","labelNative":"français"}` and `jobAd.sections.qualifications`.

**Workday CXS job detail** (sanofi / R2868669):
```json
{"jobPostingInfo":{"title":"alternance 12 ou 24 mois gestion de projet packaging et dispositifs",
  "location":"Val-de-Reuil","startDate":"2026-09-22","timeType":"Full time","jobReqId":"R2868669",
  "country":{"descriptor":"France"},"canApply":true,"includeResumeParsing":true,
  "questionnaireId":"495fb9a8287e1012c220f1fed4fb0000","timeLeftToApply":"30+ days left to apply",
  "jobDescription":"<p>... 6287 chars of HTML ...</p>"},
 "hiringOrganization":{"name":"(0122) Sanofi Winthrop Industrie","url":""},"userAuthenticated":false}
```

**PDF round-trip experiment** (Playwright 1.58 Chromium, `page.pdf(format="A4", tagged=True)`; the same content rendered 3 ways):

| Case | pypdf text order | pdfminer.six text order |
|---|---|---|
| 1 column, contact in the body | Name → title → contact → Expérience → Formation → Compétences (correct) | identical (correct) |
| 2 columns (CSS grid, 32 % sidebar with icons and skills) | sidebar first: `Jean Exemple\n✉  jean...\n☎  06...\n📍  Metz\nCompétences\n...\nExpérience professionnelle...` | **`Jean Exemple\n\nExpérience professionnelle\n\n✉ jean...\n☎ 06...\n📍 Metz\n\nCompétences...`** The contact details and skills land *inside* the Experience section. |
| Contact only in the Chromium `header_template` | contact line is the **last** line | contact line is the **first** line |

**DOCX experiment** (python-docx 1.2):
- The contact line was written into `section.header`, and a Skills table was added to the body.
- Result: `doc.paragraphs` contains **neither** the email nor "React" (tables and headers are not paragraphs).
- The email appears **only in `word/header1.xml`**, not in `word/document.xml`.
- `React` is in `document.xml` (inside `w:tbl`).
- The heading style name is `Heading 1`.
- Conclusion: a naive parser that reads body paragraphs only loses both the contact details and the table content.

**French stemming check** (Snowball "french", snowballstemmer 3.1):
```
développeur→développeur  développeuse→développ  développement→développ
rigueur→rigueur  rigoureux→rigour   curieux→curieux  curiosité→curios
autonomie→autonom autonome→autonom  communication→commun (!)  réactivité→réactiv  react→react
```
Stemming does **not** unify many French soft-skill pairs and over-stems "communication" to "commun" (which collides with the adjective *commun*). Use explicit synonym groups instead.

**Fuzzy title check** (rapidfuzz 3.14, `token_set_ratio` on accent-stripped lowercase):
- "developpeur full stack" vs "developpeur fullstack" = **97.7**
- vs "full stack developer" = **95.2**
- vs "concepteur developpeur d applications" = **66.7**

---

## 4. Limits, ToS, legal

### 4.1 Upload limits (per platform)

| Platform | Formats | Size | Source |
|---|---|---|---|
| LinkedIn Easy Apply | Word or PDF | "recommend < 2 MB" | LinkedIn Help a510363 |
| HelloWork | pdf, doc, docx, odt, rtf, jpg, png | ≤ 2 Mo | LIVE |
| Greenhouse | pdf, doc, docx, txt, rtf | upload ≤ 100 MB; reportedly **not parsed above 2.5 MB** | LIVE (types); support doc (100 MB); 2.5 MB UNCERTAIN |
| Teamtailor | doc, docx, pptx, pdf, pages, txt, rtf | ? | LIVE |
| Oracle Taleo | configurable filter (doc/docx/pdf/rtf/txt/htm/odt/wpd…) | **default 1 MB per file**, 10 files, 5 MB per entity (Basic package) | Oracle docs |
| Workday | employer-configurable, commonly DOC/DOCX/PDF/TXT/HTML | commonly 5 MB | UNCERTAIN |
| Flatchr | Word or PDF | ? | Flatchr help |
| WTTJ | PDF | 5 MB | UNCERTAIN |

**jobbot rule:** target **< 500 KB** for the PDF (text only, no photo, fonts subset by Chromium) and never exceed **1 MB**. That satisfies every limit above, Taleo included.

### 4.2 Parser language support

- **Oracle Taleo:** "Resume parsing is delivered using a third party partner service… delivered as-is". French (France) and French (Canada) are rated **"Limited"**. The career-section parsing default is English. For a French CV on Taleo, expect weak auto-fill and fix the fields manually.
- **Greenhouse:** full parsing in many languages, French included (support article "Resume parsing with non-English languages").
- **SuccessFactors / Textkernel:** French is supported (the language list quoted by SAP documentation includes French).

### 4.3 Honesty, anti-cheating, AI rules

- **Hidden text / prompt injection:**
  - Greenhouse reports that about **1 %** of roughly 300 M applications per year contain hidden prompt-injection attempts, and it alerts recruiters.
  - ManpowerGroup finds hidden text in about 10 % of the CVs it scans and does not move those candidates forward (press, as cited by several sources).
  - jobbot must **never** emit white or tiny text, off-page text, or metadata keyword lists. The lint in §6.4 blocks them.
- **Truthfulness:** a CV listing skills the candidate does not have is a misrepresentation. It can also justify breaking a trial period or a contract obtained by fraud (general French labour-law principle, not researched in depth: UNCERTAIN). Enforce the "inventory-only" rule.
- **EU AI Act:**
  - Recruitment/selection AI systems are **high-risk (Annex III)**.
  - The Digital Omnibus (**Regulation (EU) 2026/1744**, in force 27 July 2026) postponed Annex III obligations **from 2 August 2026 to 2 December 2027**.
  - The obligations fall on employers and ATS vendors, not on the candidate.
  - A person using AI for a "personal non-professional activity" is not a "deployer" (art. 3(4)). Whether job-seeking counts as personal is UNCERTAIN, but no labelling duty on AI-assisted letters is known.
- **GDPR, candidate-side levers** (not researched in depth; DOC-SOURCED general law):
  - Art. 15 gives a right of access to the data and scores an employer or ATS holds about the candidate.
  - Art. 22 restricts decisions based *solely* on automated processing.
  - jobbot could generate a polite art. 15 request after an instant rejection (optional feature).
  - Processing the candidate's own data inside jobbot is personal use.
  - Scraped recruiter emails are third-party data; see the other research file on email discovery.

### 4.4 Alternance: timing rules (official texts)

- **L6222-12:**
  - "La date de début de la formation pratique chez l'employeur ne peut être postérieure de plus de trois mois au début d'exécution du contrat."
  - "La date de début de la période de formation en centre de formation d'apprentis ne peut être postérieure de plus de trois mois au début d'exécution du contrat."
  - So the contract may start up to 3 months **before** the CFA period.
- **L6222-12-1:**
  - "toute personne âgée de seize à vingt-neuf ans révolus […] peut, à sa demande, si elle n'a pas été engagée par un employeur, débuter un cycle de formation en apprentissage dans la limite d'une durée de trois mois."
  - The person has *stagiaire de la formation professionnelle* status, and "le CFA […] l'assiste dans la recherche d'un employeur."
  - "A tout moment, le bénéficiaire […] peut signer un contrat d'apprentissage. Dans ce cas, la durée du contrat ou de la période d'apprentissage est réduite du nombre de mois écoulés depuis le début du cycle de formation."
- **L6222-7-1:** the duration "varie entre six mois et trois ans" and should equal the length of the training cycle.
- Centre Inffo (citing 2021 administrative guidance): the contract may end **up to 2 months after** the end of the cycle or the exam. At least 25 % of the time must be spent in training at the CFA.

**What this means for the candidate** (rentrée in October 2026, 24-month programme):

| Period | Legal state | Message angle |
|---|---|---|
| now → rentrée (early Oct) | Contract may start now, since the CFA period begins within 3 months | "Disponible immédiatement ; rentrée EDS en octobre 2026 ; rythme 3 semaines entreprise / 1 semaine école" |
| rentrée → rentrée + 3 months (≈ early Jan 2027) | Candidate may attend as a *stagiaire* without an employer; each month elapsed shortens the contract | "Je peux démarrer immédiatement ; la loi me laisse jusqu'à début janvier pour signer (art. L6222-12-1)" |
| before 31 Dec 2026 | Current aid applies only if the contract **starts before 1 Jan 2027** | "Un contrat qui démarre avant le 1er janvier 2027 ouvre droit à l'aide de l'État (jusqu'à 2 000 € pour une entreprise de moins de 250 salariés, niveau Bac+5)" |
| after ≈ early Jan 2027 | Outside the 3-month window, so continuing depends on the CFA (UNCERTAIN) | Switch to the junior CDI / stage track |

**Ask EDS** for the exact rentrée date and its internal rule. Store both as config: `alternance.cycle_start`, `alternance.no_employer_deadline`.

### 4.5 Alternance: 2026 employer aid and costs (for LM and email arguments)

Contracts concluded **from 8 March 2026** whose **execution begins before 1 January 2027** (decree 2026-168; service-public F23556 verified 22/07/2026):

| Company size | Level 3–4 (≤ Bac) | Level 5 (Bac+2) | **Level 6–7 (Bac+3 to Bac+5), which is the candidate's case (RNCP 36469, level 7)** | Apprentice with RQTH |
|---|---|---|---|---|
| < 250 employees | 5 000 € (*aide unique*) | 4 500 € (*aide exceptionnelle*) | **2 000 €** | 6 000 € |
| ≥ 250 employees | 2 000 € | 1 500 € | **750 €** | 6 000 € |

- Paid for the first year only, monthly and automatically by the ASP after the contract is sent to the OPCO and declared in the DSN. Prorated for contracts shorter than 12 months.
- Companies with 250 or more employees must meet a quota by 31 December of the following year (for 2026 contracts, 31/12/2027): alternants plus related contracts ≥ 5 % of headcount, or ≥ 3 % with 10 % growth or a branch agreement.
- Contracts signed between 1 January and 7 March 2026 received aid only for level ≤ 4 in companies under 250 employees (€5,000). Irrelevant now, but shows the rules change mid-year.
- **Employer participation:** €750 flat per apprenticeship contract at level ≥ 6, for contracts concluded from 1 July 2025 (art. R6332-25-1, decree 2025-585). The CFA invoices it after the probationary period. It is €200 when a new contract follows a rupture.
- **Net for an SME hiring the candidate:** €2,000 aid − €750 participation = **+€1,250 in year 1**.
- **Contrat de professionnalisation:** the 2026 exceptional aid no longer covers it. Only the France Travail aids for job seekers aged 26+ remain.
- **2027:** Prime Minister Lecornu (letter of 9 September 2026) said the hiring aid will be "sanctuarisées" in the PLF 2027. It is a political commitment only; no decree. Messages must not promise 2027 amounts.
- **EDS says:** "Apprenticeship/Professional contract: 100% employer-financed, €0 apprentice cost". The OPCO funds the CFA through the NPEC, and the employer pays the €750 participation plus any excess above the NPEC.

**Safe LM sentence** (all facts sourced):

> « Ma formation est financée via votre OPCO (une participation forfaitaire de 750 € reste à la charge de l'employeur pour un diplôme Bac+5), et un contrat débutant avant le 1er janvier 2027 ouvre droit à l'aide de l'État de 2 000 € pour une entreprise de moins de 250 salariés. »

### 4.6 Luxembourg

- The Luxembourg cross-border apprenticeship scheme (MENJE, `men.public.lu`) covers **vocational initial training and adult apprenticeship trades** on an official list. For 2026/27, requests were due by **31/10/2026** and signing ran **16/07 to 01/11/2026**.
- Post-bac and Bac+5 programmes do not appear to be covered. Frontaliers Grand Est says a France–Luxembourg agreement "n'a à ce jour pas encore été signé" and that the scheme applies "si le diplôme visé ne se situe pas au niveau post-bac".
- **Treat Luxembourg as CDI-only** unless EDS confirms otherwise (UNCERTAIN).

---

## 5. Gotchas & anti-bot / anti-cheat measures

**Layout and parsing**
1. **Columns:** extractors disagree (LIVE). Never produce two-column or sidebar layouts, even though French templates (Canva, CVDesignR) love them. France Travail: "Une seule colonne", "Pas de tableaux ni zones de texte (l'ATS les ignore ou les lit mal)".
2. **Headers/footers:** extractors disagree (LIVE). Taleo and Workday reportedly ignore them. Keep all content, and especially contact details, in the **body**. Set `display_header_footer=False` in `page.pdf`. Put nothing in DOCX `section.header` or `section.footer`.
3. **Tables:** python-docx `doc.paragraphs` skips them (LIVE), and many parsers read cells in the wrong order. Use none.
4. **Icons and skill bars:**
   - Unicode icons become noise tokens (LIVE: ✉ ☎ 📍).
   - Icon fonts (Font Awesome) produce Private Use Area code points (U+E000–U+F8FF) or "[NULL]" per Jobscan.
   - Skill bars and charts are unreadable. France Travail says write the level instead: "Anglais : courant (C1)".
   - Lint for PUA characters, emoji and U+FFFD.
5. **Ligatures:** some fonts (Calibri, Garamond) make Chromium emit `ﬁ`/`ﬂ` ligature glyphs, which extract as U+FB01/U+FB02 and break words like "certiﬁcation". Add CSS `font-variant-ligatures: none; font-feature-settings: "liga" 0;` and lint for U+FB00–U+FB06. (Precaution; not reproduced with Arial.)
6. **French typography:** non-breaking spaces (U+00A0, U+202F) appear before `: ; ! ?`, in "85 %" and in numbers. Normalise them to spaces before matching. Also normalise the apostrophes `’`/`'` and the dashes `–`/`—`/`-`.
7. **Scanned or image PDFs** are not parsed. Workday: works best with "resumes that don't have images or image-based styles". Apec: "Si vous ne pouvez sélectionner le texte d'un PDF, le document est probablement enregistré sous forme d'image".

**Workday and Taleo specifics**
8. **Workday Skills:** the parser does **not** auto-fill Skills or Languages. Workday suggests skills using ML over the *entire* resume (certificates, company, degree, description, education, experience, summary…). In a Workday form, jobbot must fill or accept the Skills widget with the JD's hard skills that the candidate really has. HiredScore then grades A–D against the requisition.
9. **Taleo:** French parsing is "Limited" and attachments default to ≤ 1 MB. Upload a DOCX, then re-type the key fields.

**Keyword matching**
10. **Ambiguous tokens** that must never be matched by naive substring or case-insensitive search:
    - "Vue" (the French word *vue*: "en vue de", "point de vue");
    - "Go" (the verb);
    - "C" and "R";
    - "Java" inside "JavaScript";
    - "React" vs "réactif"/"réactivité";
    - "Express", "Spring" and "Rails" (common words);
    - "Oracle" (the company vs the database);
    - "Mocha" (ESCO lists it as a JS alias);
    - "SQL" inside "MySQL"/"PostgreSQL";
    - "Angular" vs "AngularJS" (different products);
    - "Next" and "Nest" (common words);
    - "TS", which also means "Tout Service" and similar abbreviations.

    §6.2 flags these as `cs` (case-sensitive) or `ctx` (require a tech context).
11. **Exact vs synonym matching:** Jobscan says an ATS "doesn't always recognize synonyms" and advises writing the acronym *and* its long form. Apec says "retour sur investissement (ROI)"; France Travail says "Écrivez l'acronyme ET sa signification complète au moins une fois".

    jobbot uses synonyms to **score**, but when **rendering** it mirrors the JD's exact spelling (JD "Postgres" → CV "PostgreSQL (Postgres)").

**Titles and dates**
12. **Gender and contract markers in titles:** "(H/F)", "F/H", "H/F/X", "(F/H/NB)" (LIVE Ubisoft), "F/H H/F" (LIVE AGIRC-ARRCO/Talentsoft), "développeur·se", "développeur(se)", "développeur.euse", "développeuse", "Alternant(e)". Strip them before comparing titles.
13. **Dates:**
    - Use "Septembre 2023 – Juin 2026" (France Travail prefers this to "01/20 – 12/23") or MM/YYYY.
    - Never use year-only or day-month-year (Jobscan).
    - For ongoing roles use "Septembre 2025 – aujourd'hui" (Jobscan's "Present").
    - Use one style everywhere.
    - Avoid abbreviated months with dots ("sept.", "janv."). Parser support for them is UNCERTAIN.

**Scoring pitfalls**
14. **Over-optimisation:**
    - Jobscan warns against chasing 100 %: "An 80% is really the sweet spot between optimizing your resume without overstuffing it".
    - It also flags clichés like "team player".
    - French equivalents to penalise: "dynamique et motivé", "esprit d'équipe" without evidence, "force de proposition" repeated.
15. **Market-score tools are not ATSs:** Jobscan, Teal and similar tools approximate keyword coverage; real ATSs rank with proprietary ML or recruiter criteria. Treat the 80 % target as a heuristic.
16. **WTTJ "no auto-reject" claims** (some French blogs) conflict with WTTJ's own ATS page: "Each application is reviewed against your scoring criteria… the AI explains exactly how it scored them". Assume AI ranking exists everywhere.

**Application flow**
17. **Account walls:** Workday (per tenant), Talentsoft and Taleo require candidate accounts. The account-creation strategy is out of scope here; see the application-flow research. Playwright must then run the per-tenant flow.
18. **Pre-fill errors after parsing** (Workday `includeResumeParsing`, Lever, Teamtailor) are common. Always read back the name, email, phone, last title/company and dates, then correct them.
19. **Apply forms:** Teamtailor, Taleez, Jobaffinity and the SmartRecruiters oneclick UI are JS apps. Upload inputs can be hidden (`invisible-resume-upload` on Lever). Use `page.set_input_files()` on the `input[type=file]` directly rather than clicking the visible button.

---

## 6. Concrete Python implementation notes

Suggested package layout (small files):

```
jobbot/documents/
  profile.yaml            # master profile (truth)
  lexicon.py              # §6.2 skills / soft skills / titles / headings
  normalize.py            # text normalisation + tokenisation
  jd_analyzer.py          # JD -> weighted requirements
  cv_scorer.py            # §6.3 ats_score
  cv_tailor.py            # select/reorder bullets, mirror spellings (no new facts)
  render_pdf.py           # Jinja2 HTML -> Playwright PDF (tagged)
  render_docx.py          # python-docx, Heading 1, List Bullet
  doc_lint.py             # §6.4 round-trip + safety checks
  lm_builder.py / lm_scorer.py  # §6.5 / §6.6
  ats_detect.py           # §2.2 patterns + accept-attr reader
```

Dependencies:
- **Mandatory:** `playwright` (≥ 1.42 for `tagged`/`outline`; 1.58 tested), `python-docx` (1.2 tested), `pypdf` (6.x), `pdfminer.six` (20260107 tested), `rapidfuzz` (3.14 tested), `jinja2`, `pyyaml`.
- **Optional:** `snowballstemmer`, only for non-lexicon "other keywords".

### 6.1 Normalisation

```python
import re, unicodedata

_NBSP = dict.fromkeys(map(ord, "    "), " ")
_QUOTES = {ord("’"): "'", ord("‘"): "'", ord("–"): "-", ord("—"): "-", ord("‐"): "-"}
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}

def normalize(text: str, keep_case: bool = False) -> str:
    """Accent-strip, unify spaces/quotes/dashes, expand ligatures. Returns a new string."""
    t = text.translate(_NBSP).translate(_QUOTES)
    for lig, rep in _LIGATURES.items():
        t = t.replace(lig, rep)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[ \t]+", " ", t)
    return t if keep_case else t.lower()

GENDER_MARKERS = re.compile(
    r"\(?\b(?:h\s*/\s*f|f\s*/\s*h)(?:\s*/\s*(?:x|nb))?\b\)?|\((?:e|ne|se|euse|trice)\)|[·.](?:e|se|euse|trice)\b",
    re.I)
CONTRACT_NOISE = re.compile(
    r"\b(en\s+)?(alternance|apprentissage|alternant(e)?|apprenti(e)?|stage|stagiaire|cdi|cdd|"
    r"interim|freelance|junior|confirme|senior|work[- ]study|apprenticeship)\b", re.I)

def normalize_title(title: str) -> str:
    """'Stage 6 mois - Vegetation Artist - (F/H/NB) [AAA project]' -> 'vegetation artist'."""
    t = GENDER_MARKERS.sub(" ", title)
    t = normalize(t)
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)          # [AAA project], (full remote)
    t = t.replace("developpeuse", "developpeur").replace("full-stack", "full stack").replace("fullstack", "full stack")
    t = t.replace("back-end", "backend").replace("front-end", "frontend")
    parts = re.split(r"\s[-|/]\s", t)               # "... - Metz", "Stage 6 mois - X", "Python / Java"
    parts = [re.sub(r"\b\d+\s*mois\b", " ", CONTRACT_NOISE.sub(" ", p)) for p in parts]
    parts = [re.sub(r"\s+", " ", p).strip() for p in parts if p.strip()]
    family_kw = [k for ks in TITLE_FAMILIES.values() for k in ks]   # TITLE_FAMILIES: see 6.2
    with_family = [p for p in parts if any(k in p for k in family_kw)]
    return (with_family or sorted(parts, key=len, reverse=True) or [""])[0]
```

### 6.2 Lexicon: FR/EN tech-skill synonyms, soft skills, headings, titles

Conventions:
- The first element of `variants` is the rendering label.
- Matching runs on `normalize()` output (lowercase, no accents), except variants listed in `cs` (case-sensitive, matched on the raw text).
- `ctx=True` means: count only if the match is (a) in a Skills section, (b) inside a comma/slash-separated list containing at least one other lexicon skill, or (c) followed or preceded by "langage", "framework", "développement", "stack", "environnement technique" or "technos".
- `parent` means that having this skill satisfies a JD asking for the parent.

```python
# (canonical_id): dict(label, cat, variants, cs=(), ctx=False, parent=None, related=())
SKILLS = {
 # --- languages
 "javascript": dict(label="JavaScript", cat="lang", variants=["javascript", "js", "ecmascript", "es6", "es2015", "es2020", "vanilla js", "vanillajs"]),
 "typescript": dict(label="TypeScript", cat="lang", variants=["typescript"], cs=("TS",), parent="javascript"),
 "python":     dict(label="Python", cat="lang", variants=["python", "python3", "python 3"]),
 "java":       dict(label="Java", cat="lang", variants=["java", "java 17", "java 21", "java se"]),  # word-boundary => never inside "javascript"
 "java_ee":    dict(label="Java EE / Jakarta EE", cat="fw", variants=["java ee", "jakarta ee", "j2ee", "jee"], parent="java"),
 "kotlin":     dict(label="Kotlin", cat="lang", variants=["kotlin"]),
 "csharp":     dict(label="C#", cat="lang", variants=["c#", "csharp", "c sharp"]),
 "cpp":        dict(label="C++", cat="lang", variants=["c++", "cpp"]),
 "c":          dict(label="C", cat="lang", variants=["langage c"], cs=("C",), ctx=True),
 "php":        dict(label="PHP", cat="lang", variants=["php", "php 8", "php8"]),
 "go":         dict(label="Go", cat="lang", variants=["golang"], cs=("Go",), ctx=True),
 "rust":       dict(label="Rust", cat="lang", variants=[], cs=("Rust",), ctx=True),
 "ruby":       dict(label="Ruby", cat="lang", variants=["ruby"]),
 "swift":      dict(label="Swift", cat="lang", variants=[], cs=("Swift",), ctx=True),
 "dart":       dict(label="Dart", cat="lang", variants=["dart"]),
 "r":          dict(label="R", cat="lang", variants=["langage r"], cs=("R",), ctx=True),
 "sql":        dict(label="SQL", cat="data", variants=["sql", "langage sql", "requetes sql", "requetage sql"]),
 "plsql":      dict(label="PL/SQL", cat="data", variants=["pl/sql", "plsql"], parent="sql"),
 "bash":       dict(label="Bash / Shell", cat="lang", variants=["bash", "shell", "script shell", "scripting shell", "shell scripting", "zsh"]),
 "powershell": dict(label="PowerShell", cat="lang", variants=["powershell"]),
 "html":       dict(label="HTML5", cat="front", variants=["html", "html5", "html 5"]),
 "css":        dict(label="CSS3", cat="front", variants=["css", "css3", "css 3"]),
 "sass":       dict(label="Sass/SCSS", cat="front", variants=["sass", "scss"], parent="css"),
 # --- front-end
 "react":      dict(label="React", cat="front", variants=["react", "react.js", "reactjs", "react js"], parent="javascript"),  # never inside "react native" (check longest-match first)
 "react_native": dict(label="React Native", cat="mobile", variants=["react native", "react-native"], related=("react",)),
 "nextjs":     dict(label="Next.js", cat="front", variants=["next.js", "nextjs", "next js"], parent="react"),
 "vue":        dict(label="Vue.js", cat="front", variants=["vue.js", "vuejs", "vue js", "vue 3", "vue3", "vue 2"], cs=("Vue",), ctx=True, parent="javascript"),
 "nuxt":       dict(label="Nuxt", cat="front", variants=["nuxt", "nuxt.js", "nuxtjs", "nuxt 3"], parent="vue"),
 "angular":    dict(label="Angular", cat="front", variants=["angular", "angular 2+", "angular 17", "angular 18", "angular 19", "angular 20"], parent="typescript"),
 "angularjs":  dict(label="AngularJS", cat="front", variants=["angularjs", "angular.js", "angular 1"], related=("angular",)),
 "svelte":     dict(label="Svelte", cat="front", variants=["svelte", "sveltekit"]),
 "jquery":     dict(label="jQuery", cat="front", variants=["jquery"]),
 "tailwind":   dict(label="Tailwind CSS", cat="front", variants=["tailwind", "tailwindcss", "tailwind css"], parent="css"),
 "bootstrap":  dict(label="Bootstrap", cat="front", variants=["bootstrap"], parent="css"),
 "redux":      dict(label="Redux", cat="front", variants=["redux", "redux toolkit"]),
 "vite":       dict(label="Vite", cat="tool", variants=["vite", "vitejs"]),
 "webpack":    dict(label="Webpack", cat="tool", variants=["webpack"]),
 "flutter":    dict(label="Flutter", cat="mobile", variants=["flutter"], related=("dart",)),
 "ionic":      dict(label="Ionic", cat="mobile", variants=["ionic"]),
 "android":    dict(label="Android", cat="mobile", variants=["android"]),
 "ios":        dict(label="iOS", cat="mobile", variants=[], cs=("iOS",)),
 # --- back-end
 "nodejs":     dict(label="Node.js", cat="back", variants=["node.js", "nodejs", "node js", "node"], parent="javascript"),
 "express":    dict(label="Express.js", cat="back", variants=["express.js", "expressjs"], cs=("Express",), ctx=True, parent="nodejs"),
 "nestjs":     dict(label="NestJS", cat="back", variants=["nestjs", "nest.js"], cs=("Nest",), ctx=True, parent="nodejs"),
 "symfony":    dict(label="Symfony", cat="back", variants=["symfony"], parent="php"),
 "laravel":    dict(label="Laravel", cat="back", variants=["laravel"], parent="php"),
 "django":     dict(label="Django", cat="back", variants=["django", "django rest framework", "drf"], parent="python"),
 "flask":      dict(label="Flask", cat="back", variants=["flask"], parent="python"),
 "fastapi":    dict(label="FastAPI", cat="back", variants=["fastapi"], parent="python"),
 "spring":     dict(label="Spring Boot", cat="back", variants=["spring boot", "springboot", "spring framework", "spring mvc"], cs=("Spring",), ctx=True, parent="java"),
 "hibernate":  dict(label="Hibernate / JPA", cat="back", variants=["hibernate", "jpa"], parent="java"),
 "dotnet":     dict(label=".NET", cat="back", variants=[".net", "dotnet", "dot net", ".net core", "asp.net", "asp.net core", ".net 8", "entity framework"], parent="csharp"),
 "rails":      dict(label="Ruby on Rails", cat="back", variants=["ruby on rails", "ror"], cs=("Rails",), ctx=True, parent="ruby"),
 "graphql":    dict(label="GraphQL", cat="api", variants=["graphql"]),
 "rest":       dict(label="API REST", cat="api", variants=["api rest", "apis rest", "rest api", "rest apis", "restful", "api restful", "web services rest", "services rest"]),
 "soap":       dict(label="SOAP", cat="api", variants=["soap"]),
 "openapi":    dict(label="OpenAPI / Swagger", cat="api", variants=["openapi", "swagger"]),
 "websocket":  dict(label="WebSocket", cat="api", variants=["websocket", "websockets", "socket.io"]),
 "grpc":       dict(label="gRPC", cat="api", variants=["grpc"]),
 "microservices": dict(label="Microservices", cat="archi", variants=["microservices", "micro-services", "micro services", "architecture microservices", "architecture micro-services"]),
 "ddd":        dict(label="DDD (Domain-Driven Design)", cat="archi", variants=["ddd", "domain driven design", "domain-driven design"]),
 "mvc":        dict(label="MVC", cat="archi", variants=["mvc", "mvvm"]),
 "oop":        dict(label="POO (programmation orientée objet)", cat="archi", variants=["poo", "programmation orientee objet", "oriente objet", "oop", "object-oriented programming", "object oriented"]),
 "design_patterns": dict(label="Design patterns", cat="archi", variants=["design patterns", "design pattern", "patrons de conception", "patterns de conception"]),
 "solid":      dict(label="SOLID / Clean Code", cat="quality", variants=["clean code", "clean architecture", "architecture hexagonale", "hexagonal architecture"], cs=("SOLID",), ctx=False),  # lowercase "solid" = English adjective
 # --- data
 "postgresql": dict(label="PostgreSQL", cat="data", variants=["postgresql", "postgres", "postgre", "postgre sql", "psql", "pgsql"], parent="sql"),
 "mysql":      dict(label="MySQL", cat="data", variants=["mysql"], parent="sql", related=("mariadb",)),
 "mariadb":    dict(label="MariaDB", cat="data", variants=["mariadb"], parent="sql", related=("mysql",)),
 "sqlite":     dict(label="SQLite", cat="data", variants=["sqlite"], parent="sql"),
 "oracle_db":  dict(label="Oracle Database", cat="data", variants=["oracle database", "oracle db", "base oracle", "bdd oracle"], cs=("Oracle",), ctx=True, parent="sql"),
 "sqlserver":  dict(label="SQL Server", cat="data", variants=["sql server", "mssql", "ms sql", "microsoft sql server", "t-sql", "tsql"], parent="sql"),
 "mongodb":    dict(label="MongoDB", cat="data", variants=["mongodb", "mongo", "mongoose"]),
 "redis":      dict(label="Redis", cat="data", variants=["redis"]),
 "elasticsearch": dict(label="Elasticsearch", cat="data", variants=["elasticsearch", "elastic search", "elk", "opensearch"]),
 "firebase":   dict(label="Firebase", cat="data", variants=["firebase", "firestore"]),
 "supabase":   dict(label="Supabase", cat="data", variants=["supabase"], related=("postgresql",)),
 "orm":        dict(label="ORM", cat="data", variants=["orm", "prisma", "typeorm", "sequelize", "doctrine", "eloquent", "sqlalchemy"]),
 "merise":     dict(label="Merise (MCD/MLD)", cat="data", variants=["merise", "mcd", "mld", "modelisation de donnees", "modelisation des donnees", "data modeling"]),
 "uml":        dict(label="UML", cat="archi", variants=["uml", "diagrammes uml"]),
 "power_bi":   dict(label="Power BI", cat="data", variants=["power bi", "powerbi"]),
 "pandas":     dict(label="pandas / NumPy", cat="data", variants=["pandas", "numpy"], parent="python"),
 "ml":         dict(label="Machine learning", cat="data", variants=["machine learning", "apprentissage automatique", "deep learning", "scikit-learn", "tensorflow", "pytorch"]),
 "llm":        dict(label="IA générative / LLM", cat="data", variants=["llm", "ia generative", "genai", "generative ai", "rag", "openai api", "langchain"]),
 # --- devops / cloud
 "git":        dict(label="Git", cat="tool", variants=["git", "gestion de versions", "gestion de version", "controle de version", "version control"]),
 "github":     dict(label="GitHub", cat="tool", variants=["github"], parent="git"),
 "gitlab":     dict(label="GitLab", cat="tool", variants=["gitlab"], parent="git"),
 "bitbucket":  dict(label="Bitbucket", cat="tool", variants=["bitbucket"], parent="git"),
 "cicd":       dict(label="CI/CD (intégration et déploiement continus)", cat="devops", variants=["ci/cd", "ci cd", "cicd", "ci-cd", "integration continue", "deploiement continu", "livraison continue", "continuous integration", "continuous delivery", "continuous deployment", "pipeline ci", "pipelines ci", "pipeline ci/cd", "pipelines ci/cd"]),
 "github_actions": dict(label="GitHub Actions", cat="devops", variants=["github actions", "github action"], parent="cicd"),
 "gitlab_ci":  dict(label="GitLab CI", cat="devops", variants=["gitlab ci", "gitlab-ci", "gitlab ci/cd"], parent="cicd"),
 "jenkins":    dict(label="Jenkins", cat="devops", variants=["jenkins"], parent="cicd"),
 "azure_devops": dict(label="Azure DevOps", cat="devops", variants=["azure devops", "azure pipelines"], parent="cicd"),
 "docker":     dict(label="Docker", cat="devops", variants=["docker", "dockerfile", "docker-compose", "docker compose", "conteneurisation", "containerisation", "containerization", "conteneurs docker"]),
 "kubernetes": dict(label="Kubernetes", cat="devops", variants=["kubernetes", "k8s", "openshift", "helm"], related=("docker",)),
 "linux":      dict(label="Linux", cat="devops", variants=["linux", "unix", "debian", "ubuntu", "centos", "red hat", "rhel"]),
 "nginx":      dict(label="Nginx", cat="devops", variants=["nginx"]),
 "apache_http": dict(label="Apache HTTP Server", cat="devops", variants=["apache http", "apache2", "httpd"]),
 "kafka":      dict(label="Kafka", cat="devops", variants=["kafka", "apache kafka"]),
 "rabbitmq":   dict(label="RabbitMQ", cat="devops", variants=["rabbitmq"]),
 "terraform":  dict(label="Terraform", cat="devops", variants=["terraform"]),
 "ansible":    dict(label="Ansible", cat="devops", variants=["ansible"]),
 "aws":        dict(label="AWS", cat="cloud", variants=["aws", "amazon web services", "ec2", "amazon s3", "aws lambda"]),
 "azure":      dict(label="Microsoft Azure", cat="cloud", variants=["azure", "microsoft azure"]),
 "gcp":        dict(label="Google Cloud (GCP)", cat="cloud", variants=["gcp", "google cloud", "google cloud platform"]),
 "monitoring": dict(label="Monitoring (Grafana/Prometheus)", cat="devops", variants=["grafana", "prometheus", "datadog", "monitoring", "observabilite", "observability"]),
 # --- tests / quality
 "unit_tests": dict(label="Tests unitaires", cat="quality", variants=["tests unitaires", "test unitaire", "unit tests", "unit testing", "unit test"]),
 "tdd":        dict(label="TDD", cat="quality", variants=["tdd", "test driven development", "test-driven development", "developpement pilote par les tests"], related=("unit_tests",)),
 "bdd":        dict(label="BDD", cat="quality", variants=["bdd", "behavior driven development", "behaviour driven development", "cucumber", "gherkin"]),
 "e2e":        dict(label="Tests end-to-end", cat="quality", variants=["tests e2e", "test e2e", "e2e", "end-to-end", "end to end", "tests de bout en bout"]),
 "jest":       dict(label="Jest", cat="quality", variants=["jest", "vitest"], parent="unit_tests"),
 "cypress":    dict(label="Cypress", cat="quality", variants=["cypress"], parent="e2e"),
 "playwright": dict(label="Playwright", cat="quality", variants=["playwright"], parent="e2e"),
 "selenium":   dict(label="Selenium", cat="quality", variants=["selenium"], parent="e2e"),
 "phpunit":    dict(label="PHPUnit", cat="quality", variants=["phpunit"], parent="unit_tests"),
 "junit":      dict(label="JUnit", cat="quality", variants=["junit", "mockito"], parent="unit_tests"),
 "pytest":     dict(label="pytest", cat="quality", variants=["pytest", "unittest"], parent="unit_tests"),
 "sonarqube":  dict(label="SonarQube", cat="quality", variants=["sonarqube", "sonar", "sonarcloud"]),
 "code_review": dict(label="Revue de code", cat="quality", variants=["revue de code", "revues de code", "code review", "code reviews", "pull request", "merge request"]),
 # --- methods / tools
 "agile":      dict(label="Méthodes agiles", cat="method", variants=["agile", "agilite", "methode agile", "methodes agiles", "methodologie agile", "methodologies agiles"]),
 "scrum":      dict(label="Scrum", cat="method", variants=["scrum", "sprint", "sprints", "daily meeting", "daily scrum"], parent="agile"),
 "kanban":     dict(label="Kanban", cat="method", variants=["kanban"], parent="agile"),
 "jira":       dict(label="Jira", cat="tool", variants=["jira", "confluence"]),
 "figma":      dict(label="Figma", cat="design", variants=["figma", "maquettage", "maquettes", "wireframes", "prototypage"]),
 "ux_ui":      dict(label="UX/UI", cat="design", variants=["ux/ui", "ui/ux", "ux", "ui", "experience utilisateur", "interface utilisateur", "user experience", "user interface"]),
 "responsive": dict(label="Responsive design", cat="front", variants=["responsive", "responsive design", "mobile first", "mobile-first"]),
 "a11y":       dict(label="Accessibilité (RGAA/WCAG)", cat="front", variants=["accessibilite", "a11y", "rgaa", "wcag"]),
 "seo":        dict(label="SEO", cat="front", variants=["seo", "referencement naturel"]),
 "security":   dict(label="Sécurité applicative (OWASP)", cat="security", variants=["owasp", "securite applicative", "securite des applications", "application security", "cybersecurite", "cybersecurity"]),
 "auth":       dict(label="Authentification (JWT/OAuth2)", cat="security", variants=["jwt", "oauth", "oauth2", "oauth 2.0", "openid connect", "oidc", "keycloak", "sso"]),
 "wordpress":  dict(label="WordPress", cat="cms", variants=["wordpress", "woocommerce"]),
 "prestashop": dict(label="PrestaShop", cat="cms", variants=["prestashop"]),
 "excel":      dict(label="Excel", cat="office", variants=["excel", "vba"]),
 "windows_server": dict(label="Windows Server / Active Directory", cat="sys", variants=["windows server", "active directory"]),
 "networking": dict(label="Réseaux (TCP/IP)", cat="sys", variants=["tcp/ip", "reseaux informatiques", "reseau informatique", "networking", "dns", "dhcp"]),
}

SOFT_SKILLS = {  # match on normalize() output; group = one requirement
 "teamwork":      ["travail en equipe", "esprit d'equipe", "travailler en equipe", "collaboratif", "collaboration", "teamwork", "team player", "esprit collectif"],
 "autonomy":      ["autonomie", "autonome", "autonomous", "self-starter", "independance"],
 "rigor":         ["rigueur", "rigoureux", "rigoureuse", "sens du detail", "minutie", "minutieux", "attention to detail", "detail-oriented", "meticulous"],
 "curiosity":     ["curiosite", "curieux", "curieuse", "curious", "veille technologique", "envie d'apprendre", "eager to learn", "appetence"],
 "communication": ["communication", "aisance relationnelle", "relationnel", "bon relationnel", "communicant", "communication skills", "pedagogie", "pedagogue"],
 "adaptability":  ["adaptabilite", "capacite d'adaptation", "adaptable", "flexibilite", "polyvalence", "polyvalent", "polyvalente", "adaptability"],
 "proactivity":   ["force de proposition", "proactif", "proactive", "proactivite", "prise d'initiative", "sens de l'initiative", "initiative"],
 "problem_solving": ["resolution de problemes", "resoudre des problemes", "problem solving", "problem-solving", "esprit d'analyse", "esprit analytique", "capacite d'analyse", "analytical", "esprit de synthese"],
 "organization":  ["organisation", "organise", "organisee", "gestion du temps", "gestion des priorites", "time management", "organized"],
 "responsibility": ["sens des responsabilites", "fiabilite", "fiable", "reliable", "implication", "implique", "impliquee"],
 "customer":      ["sens du service", "orientation client", "sens client", "customer focus", "customer-oriented"],
 "stress":        ["gestion du stress", "resistance au stress", "sang-froid", "sang froid"],
 "english":       ["anglais", "english"],  # handled by language component, listed so it is not double counted
}

TITLE_FAMILIES = {  # normalized-title substrings -> family
 "fullstack": ["developpeur full stack", "developpeur web full stack", "full stack developer", "full stack engineer", "ingenieur full stack", "concepteur developpeur full stack"],
 "backend":   ["developpeur backend", "backend developer", "developpeur java", "developpeur php", "developpeur python", "developpeur node", "developpeur .net", "developpeur c#"],
 "frontend":  ["developpeur frontend", "frontend developer", "integrateur web", "developpeur react", "developpeur angular", "developpeur vue"],
 "mobile":    ["developpeur mobile", "developpeur flutter", "developpeur android", "developpeur ios", "mobile developer"],
 "software":  ["ingenieur logiciel", "ingenieur developpement", "ingenieur etudes et developpement", "software engineer", "developpeur logiciel",
               "developpeur d'applications", "concepteur developpeur d'applications", "concepteur developpeur", "analyste programmeur",
               "analyste developpeur", "developpeur informatique", "developpeur web", "programmeur", "developer", "developpeur"],
 "devops":    ["ingenieur devops", "devops"],
}

LEVELS = {  # JD requirement detection (normalized)
 7: ["bac+5", "bac +5", "bac 5", "master", "mastere", "m2", "niveau 7", "diplome d'ingenieur", "ecole d'ingenieur", "cycle ingenieur"],
 6: ["bac+3", "bac +3", "licence", "bachelor", "but ", "b.u.t", "niveau 6", "licence pro", "bac+4", "m1"],
 5: ["bac+2", "bac +2", "bts", "dut", "niveau 5"],
}
CONTRACTS = {
 "alternance": ["alternance", "alternant", "apprentissage", "apprenti", "contrat pro", "contrat de professionnalisation", "work-study", "apprenticeship"],
 "cdi": ["cdi", "contrat a duree indeterminee", "permanent"],
 "stage": ["stage", "stagiaire", "internship", "intern"],
 "freelance": ["freelance", "independant", "portage"],
}

CV_HEADINGS = {  # canonical -> accepted variants (normalized). Render with the first one.
 "profile":     ["profil", "resume", "a propos", "objectif professionnel", "summary", "profile"],
 "experience":  ["experience professionnelle", "experiences professionnelles", "parcours professionnel", "experiences", "experience", "work experience", "professional experience"],
 "projects":    ["projets", "projets personnels", "projets academiques", "realisations", "projects"],
 "education":   ["formation", "formations", "etudes", "diplomes", "parcours academique", "education"],
 "skills":      ["competences", "competences techniques", "competences informatiques", "savoir-faire", "stack technique", "skills", "technical skills"],
 "languages":   ["langues", "languages"],
 "certifications": ["certifications", "certificats", "certifications"],
 "interests":   ["centres d'interet", "loisirs", "interets", "interests", "engagement associatif", "benevolat"],
}

REQUIRED_CUES = ["requis", "indispensable", "obligatoire", "exige", "maitrise", "vous maitrisez", "maitriser", "necessaire",
                 "imperatif", "must", "required", "you have", "solide", "solides connaissances", "experience significative"]
NICE_CUES = ["serait un plus", "un plus", "est un plus", "apprecie", "appreciee", "idealement", "bonus", "souhaite", "souhaitee",
             "nice to have", "a plus", "plus appreciated", "optionnel", "atout"]
JD_SECTION_CUES = {"missions": ["missions", "vos missions", "descriptif du poste", "le poste", "responsabilites", "what you'll do"],
                   "profile":  ["profil recherche", "votre profil", "competences requises", "qualifications", "requirements", "vous etes", "ce que nous recherchons"],
                   "company":  ["qui sommes-nous", "l'entreprise", "a propos", "about us"],
                   "perks":    ["avantages", "les + du poste", "ce que nous offrons", "benefits"]}
```

**Matcher construction.** Handle the longest variants first ("react native" before "react", "gitlab ci" before "gitlab", "java ee" before "java"). Use tech-safe boundaries so `c++`, `c#`, `.net`, `node.js` and `ci/cd` work:

```python
import re
from collections import Counter

_LB, _RB = r"(?<![\w+#.])", r"(?![\w+#]|\.[a-z])"   # 'JavaScript/TypeScript' ok, 'java' not in 'javascript'

def build_matchers(skills: dict) -> list[tuple[str, re.Pattern, bool, bool]]:
    """Lowercase variants are unambiguous (no context needed); `cs` variants need context when ctx=True."""
    rows = []
    for cid, s in skills.items():
        for v in s["variants"]:
            rows.append((len(v), cid, re.compile(_LB + re.escape(v) + _RB), False, False))
        for v in s.get("cs", ()):
            rows.append((len(v), cid, re.compile(_LB + re.escape(v) + _RB), True, s.get("ctx", True)))
    rows.sort(key=lambda r: -r[0])
    return [(cid, rx, cs, ctx) for _, cid, rx, cs, ctx in rows]

CTX_RX = re.compile(r"(langage|framework|stack|techno|environnement technique|developpement|librairie|library)", re.I)

def find_skills(raw: str, matchers, in_skills_section: bool = False) -> Counter:
    """Count canonical skills; consumes matched spans so shorter variants don't double count."""
    norm = normalize(raw)
    masked_norm, masked_raw = list(norm), list(normalize(raw, keep_case=True))
    hits = Counter()
    for cid, rx, cs, ctx in matchers:
        hay = "".join(masked_raw if cs else masked_norm)
        for m in rx.finditer(hay):
            if ctx and not in_skills_section:
                window = hay[max(0, m.start() - 60): m.end() + 60]
                if not (CTX_RX.search(window) or window.count(",") + window.count("/") >= 2):
                    continue
            hits[cid] += 1
            for i in range(m.start(), m.end()):
                masked_norm[i] = masked_raw[i] = " "
    return hits
```

(Because `normalize()` and `normalize(keep_case=True)` both apply NFKD, they give same-length strings for the characters involved: the accent strip is applied identically. Assert `len(norm) == len(raw_norm)` in tests.)

Soft skills, `TITLE_FAMILIES`, `LEVELS` and `CONTRACTS` use the same `_LB`/`_RB` boundary regex on `normalize()` output, with no case sensitivity and no context rule.

A sanity run of these sketches (this research, snowballstemmer venv, Python 3.12) gave:
- "Stack : React, Node.js, PostgreSQL, Docker, CI/CD (GitHub Actions), JavaScript/TypeScript" → react, nodejs, postgresql, docker, cicd, github_actions, javascript, typescript.
- "En vue de renforcer l'équipe, point de vue client, Vue.js 3 et Nuxt" → vue ×1 and nuxt only. The French word *vue* is ignored.
- "Réactivité et rigueur ; réactif" → no tech skill.
- "Base Oracle, PL/SQL, MySQL et C# / .NET Core, C++" → oracle_db, plsql, mysql, csharp, dotnet, cpp.
- `normalize_title("Stage 6 mois - Vegetation Artist - (F/H/NB) [AAA project]")` → "vegetation artist".

### 6.3 Deterministic ATS match score (the formula)

Inputs:
- `jd`: title plus full text. Use the SmartRecruiters `qualifications` or Lever `lists` blocks when available.
- `cv`: sections of the *rendered* CV, re-extracted from the file (see §6.4). Score what the ATS will see, not the YAML.
- `profile`: truth inventory.

**Step A: JD requirements.** For each skill `s` found in the JD:

```
base(s)    = 1.0 if s appears in a sentence/line with a REQUIRED cue or in the "profile/qualifications" block
             0.4 if in a sentence with a NICE cue
             0.7 otherwise
freq(s)    = 1 + 0.25 * min(count_in_JD(s) - 1, 2)          # 1.00 / 1.25 / 1.50
title(s)   = 1.5 if s appears in the JD title else 1.0
w(s)       = base * freq * title
```
Soft skills: the same scheme per group, with `base` capped at 0.7.

**Step B: CV credit per JD skill** (take the max over the rules that apply):

```
1.00  s found in Experience/Projects bullets AND in Skills section
0.85  s found in Experience/Projects only
0.70  s found in Skills section only (listed, no evidence)
1.00×(above)  a CHILD of s is found (JD "SQL", CV "PostgreSQL"; JD "CI/CD", CV "GitHub Actions")
0.50  s is the child and only the PARENT is found (JD "PostgreSQL", CV "SQL")
0.30  only a RELATED/sibling skill found (JD "MySQL", CV "MariaDB"; JD "Vue.js", CV "React")
0.00  otherwise
```

**Step C: components (each 0..1):**

```
Hard   = Σ_s w(s)·credit(s) / Σ_s w(s)                       (hard skills only; if JD has 0 skills -> 0.5 neutral)
Soft   = Σ_g w(g)·[g present anywhere in CV or LM-excluded] / Σ_g w(g)   (if JD has none -> 1.0)
Title  = max over {CV headline, CV profile first line, most recent job title}:
           1.0  normalize_title equal to JD normalize_title
           0.9  token_set_ratio ≥ 90
           0.7  token_set_ratio ≥ 80
           0.5  same TITLE_FAMILY (e.g. both "fullstack")
           0.3  both in any developer family
           0.0  otherwise
EduContract = mean of the applicable checks:
           level:     1.0 if JD level ≤ candidate level (for alternance postings use the level being PREPARED = 7);
                      0.5 if JD level = candidate+1; 0 if JD asks ≥ 2 levels above
           contract:  1.0 if JD contract ∈ user targets; 0 → HARD GATE (skip) if JD is stage-only and user excluded stage
           years_exp: 1.0 if JD asks ≤ 1 year or nothing; 0.6 for 2 years; 0.3 for ≥ 3 years  (regex below)
           rhythm:    (alternance only) 1.0 if JD rhythm unspecified or compatible with "1 semaine école / 3 semaines entreprise";
                      0.6 if JD states an incompatible rhythm (e.g. "2 jours école / 3 jours entreprise")
           duration:  (alternance only) 1.0 if JD says 24 mois / 2 ans or unspecified; 0.6 if 12 mois only
Keywords = share of the JD's top-15 "other" keywords (nouns ≥ 4 chars, not stopwords, not in lexicon;
           ranked by count, snowball-stemmed for comparison) present in CV                     (0..1)
Language = 1.0 if every JD language requirement ("anglais", "english", "allemand", "B2/C1…") is present in CV
           Langues section at ≥ the required level; 0.5 if present without level; 0 if missing; 1.0 if none required
Search   = file searchability from §6.4 lint (0..1)
```

**Step D: final score:**

```
ats_score = 100 × ( 0.45·Hard + 0.15·Title + 0.10·Soft + 0.10·EduContract
                  + 0.05·Keywords + 0.05·Language + 0.10·Search )  − penalties
penalties:
  −2 per hard skill whose CV count > max(3, 2 × JD count)                       (cap −10)
  −3 if > 40 % of Experience-bullet tokens are lexicon skills (stuffed bullets)
  −2 per cliché in CV (see CLICHES in §6.6)                                      (cap −6)
hard blocks (score := 0, do not send):
  hidden/tiny/white text, off-page text, PUA/emoji icons in contact block,
  any lexicon skill in the CV that is NOT in profile.skills (fabrication guard),
  text layer missing (scanned/image PDF), > 1 page for junior, file > 1 MB
bands: ≥ 80 apply | 70–79 re-tailor | 60–69 priority targets only | < 60 skip/spontaneous
stop tailoring when ≥ 85 or when no truthful move increases the score by ≥ 1 point
```

Years of experience regex (on normalised JD text):

```python
YEARS_RX = re.compile(r"(\d{1,2})\s*(?:\+|a\s*\d+)?\s*(?:ans|annees?)\s+(?:minimum\s+)?d['e ]?\s*experience|(\d{1,2})\+?\s*years?", re.I)
RHYTHM_RX = re.compile(r"(\d)\s*(jours?|semaines?)\s*(?:en\s*)?(?:ecole|cours|formation|cfa)\s*[/,-]\s*(\d)\s*(jours?|semaines?)\s*(?:en\s*)?entreprise|"
                       r"(\d)\s*(jours?|semaines?)\s*(?:en\s*)?entreprise\s*[/,-]\s*(\d)\s*(jours?|semaines?)", re.I)
DURATION_RX = re.compile(r"(12|18|24|36)\s*mois|(1|2|3)\s*ans?\b", re.I)
```

**Why these weights.**
- Every vendor ranks the factors hard skills >> job title > education > soft skills > other keywords (Jobscan: "weighs hard skills much more heavily than soft skills and one-word keywords"; "exact job title match had an interview rate 3.5 times higher").
- SkillSyncer's report shows the same four buckets (Hard / Soft / Other keywords / Degree & Title).
- Searchability is included because Jobscan and CVDesignR both score file, sections, contact details and dates separately.
- The weights are a design choice (UNCERTAIN by nature). Keep them in config and tune them on real outcomes: log `(ats_score, components, response/no-response)` per application.

### 6.4 Rendering and round-trip lint (the "ATS simulation")

**PDF renderer** (Playwright sync). One column, A4, body contact, no header/footer:

```python
from pathlib import Path
from playwright.sync_api import sync_playwright

CV_CSS = """
@page { size: A4; margin: 14mm 14mm 12mm 14mm; }
body { font-family: Arial, Helvetica, 'Liberation Sans', sans-serif; font-size: 10.5pt; line-height: 1.3;
       color: #000; font-variant-ligatures: none; font-feature-settings: "liga" 0; }
h1 { font-size: 16pt; margin: 0 0 2pt; } h2 { font-size: 12pt; margin: 8pt 0 3pt; text-transform: none; }
ul { margin: 2pt 0 4pt 14pt; padding: 0; } li { margin: 0 0 1pt; }
/* NO grid/flex columns, NO position:absolute, NO ::before icons, NO background images */
"""

def render_pdf(html: str, out: Path) -> Path:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(path=str(out), format="A4", print_background=False, tagged=True, outline=False,
                     display_header_footer=False, prefer_css_page_size=True)
        finally:
            browser.close()
    return out
```

**DOCX renderer** (python-docx):
- Use `doc.add_heading(text, level=1)` for section titles and `doc.add_paragraph(text, style="List Bullet")` for bullets.
- Write the name, title and contact line as normal paragraphs at the top.
- Set margins through `section.left_margin`, etc.
- Never touch `section.header`/`footer`. No tables, no shapes, no images.
- Set the font to Arial or Calibri, 10.5 pt (`doc.styles["Normal"].font`).
- Set core properties: `doc.core_properties.title = "CV – Prénom NOM – Développeur full-stack"`, `author = "Prénom NOM"`. Keep them honest and short; never put a keyword list there.

**Lint** (score `Search` in 0..1, plus hard blocks):

```python
import io, re, zipfile
from pypdf import PdfReader
from pdfminer.high_level import extract_text as pdfminer_text

EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"(?:\+33\s?|0)[1-9](?:[\s.-]?\d{2}){4}|\+352\s?\d[\d\s]{5,}")
MONTHS = r"(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)"
DATE_RANGE_RX = re.compile(rf"(?:{MONTHS}\s+\d{{4}}|\d{{2}}/\d{{4}})\s*-\s*(?:{MONTHS}\s+\d{{4}}|\d{{2}}/\d{{4}}|aujourd'hui|present|en cours|ce jour)")
BAD_CHARS_RX = re.compile("[-�ﬀ-ﬆ\U0001F300-\U0001FAFF☀-➿]")

def lint_pdf(pdf: bytes, expected_headings: list[str], expected_skills: list[str], max_bytes=1_000_000) -> dict:
    reader = PdfReader(io.BytesIO(pdf))
    t1 = "\n".join((p.extract_text() or "") for p in reader.pages)
    t2 = pdfminer_text(io.BytesIO(pdf))
    n1, n2 = normalize(t1), normalize(t2)
    blocks = []
    if len(n1.strip()) < 200: blocks.append("no_text_layer")
    if len(reader.pages) > 1: blocks.append("more_than_one_page")
    if len(pdf) > max_bytes: blocks.append("file_too_big")
    h1, h2 = _head(n1), _head(n2)                            # contact must be in the first lines for BOTH extractors
    checks = {
        "contact_top": all(EMAIL_RX.search(h) and PHONE_RX.search(h) for h in (h1, h2)),
        "headings": sum(h in n1 and h in n2 for h in expected_headings) >= min(3, len(expected_headings)),
        "heading_order_stable": _order(n1, expected_headings) == _order(n2, expected_headings)
                                == [h for h in expected_headings if h in n1],
        "dates": len(DATE_RANGE_RX.findall(n1)) >= 2,
        "skills_present": all(normalize(s) in n1 for s in expected_skills),
        "clean_glyphs": not BAD_CHARS_RX.search(t1 + t2),
        "size_ok": len(pdf) <= 500_000,
        "one_page": len(reader.pages) == 1,
    }
    weights = {"contact_top": .20, "headings": .20, "heading_order_stable": .15, "dates": .15,
               "skills_present": .10, "clean_glyphs": .05, "size_ok": .10, "one_page": .05}
    return {"search": sum(weights[k] for k, ok in checks.items() if ok), "checks": checks, "blocks": blocks}

def _head(text: str, n_lines: int = 6) -> str:
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][:n_lines])

def _order(text: str, headings: list[str]) -> list[str]:
    """Headings sorted by first position; compare sequences (never raw offsets: extractors differ in whitespace)."""
    return sorted((h for h in headings if h in text), key=text.find)

def lint_docx(path: str) -> list[str]:
    z = zipfile.ZipFile(path)
    names = z.namelist()
    problems = []
    body = z.read("word/document.xml").decode("utf8")
    if "<w:tbl>" in body or "<w:tbl " in body: problems.append("table")
    if "w:txbxContent" in body: problems.append("text_box")
    if any(n.startswith("word/media/") for n in names): problems.append("image")
    for n in names:
        if re.match(r"word/(header|footer)\d*\.xml", n) and re.search(r"<w:t[ >][^<]+</w:t>", z.read(n).decode("utf8")):
            problems.append(f"text_in_{n}")
    return problems
```

**Validated on the three experiment PDFs and the DOCX:**
- 1-column PDF → `search = 1.0`.
- 2-column PDF with icons → `0.8`, with `heading_order_stable=False` and `clean_glyphs=False`.
- Contact only in the PDF header → `0.8`, with `contact_top=False`.
- Header/table DOCX → `['table', 'text_in_word/header1.xml']`.

pdfminer logs harmless warnings ("Could not get FontBBox…") on Chromium PDFs. Silence them with `logging.getLogger("pdfminer").setLevel(logging.ERROR)`.

**Hidden-text guard** (PDF). This is a static check, because jobbot controls the HTML:
- Refuse any CSS with `color` equal to the background, `font-size` < 6pt, `opacity` < 1, `display:none` text, or `position` off-page.
- Also verify with pdfminer layout analysis: iterate `LTChar` objects and flag `size < 6` or `non_stroking_color` close to white.

### 6.5 Tailoring algorithm (deterministic, truth-preserving)

1. Build `jd_req = analyze(jd)` (weights from §6.3 step A) and detect the JD language. Greenhouse `language`, SmartRecruiters `language.code`, or a simple FR/EN stopword ratio all work. Use an English CV for English JDs (Luxembourg).
2. **Headline** (first line under the name). Use the JD title after `normalize_title`, re-cased, *if* its family matches the candidate's target families. Otherwise use the default "Développeur full-stack".
   - Alternance postings: "Développeur Full-Stack en alternance – Bac+5 (Exemple Digital School) – dès octobre 2026".
   - CDI postings: "Développeur Full-Stack junior – disponible immédiatement".
3. **Profil** (2–3 lines, ≤ 350 characters):
   - Include the 3–5 highest-weight JD skills the candidate has, using the JD's spelling.
   - Include one quantified fact (Apec: "gestion d'une équipe de 10 personnes, augmentation… de 15 % en 6 mois"; Jobscan "Measurable results").
   - Alternance: rhythm "3 semaines entreprise / 1 semaine école" and "contrat de 24 mois".
4. **Compétences:**
   - Group into 3–5 lines ("Langages", "Front-end", "Back-end", "Données", "DevOps & outils").
   - Put the JD-matched skills first on each line.
   - Render each skill with the JD's variant. When the JD uses an abbreviation, render "abbr (long form)" once (CI/CD, POO, TDD).
5. **Expérience professionnelle / Projets:**
   - For each item, rank its bullets by `Σ w(s)` of the skills they evidence.
   - Keep the top 2–4 bullets so the page stays at one page. Loop: render → count pages → drop the lowest-ranked bullet.
6. **Formation:**
   - Put the programme being prepared first: "Expert en Ingénierie du développement et en architecture logicielle (Bac+5, RNCP 36469) – Exemple Digital School – Octobre 2026 – Septembre 2028 (alternance)". Confirm the end date with the school.
   - Then "BUT Informatique – IUT Exemple – [dates]".
7. Re-score. Iterate until the score is ≥ 80, or ≥ 85 as the stop point, or no move helps.
8. **Fabrication guard:** diff the lexicon hits in the output against `profile.skills`. If there is any extra hit, discard the output.

**Recommended section order:**
- Alternance: **Profil → Compétences → Expérience professionnelle → Projets → Formation → Langues → Centres d'intérêt**.
- For a candidate with little experience, France Travail suggests putting Formation first. Make the order configurable per posting: if the JD's profile block starts with a diploma requirement, move Formation up.

**Contact block** (body, first 3 lines, plain text):
- Name.
- Headline.
- `Metz (57) · 06 12 34 56 78 · prenom.nom@email.fr · linkedin.com/in/… · github.com/…`, plus "Permis B" if true.
- No full postal address, date of birth, marital status or photo in the ATS version.

**File naming** (ASCII only, no spaces or accents):
- `CV_Prenom_NOM_Developpeur_FullStack_Alternance.pdf`
- `LM_Prenom_NOM_<Entreprise>.pdf`

CVDesignR checks file names. French guides recommend `CV_NOM_Prenom_2026.pdf` or `Lettre_NOM_Prenom.pdf`.

### 6.6 Lettre de motivation (LM): norms, templates, deterministic score

**Norms** (France Travail "10 astuces"; Welcome to the Jungle on alternance):
- One page, Arial 12 pt.
- Structure **vous / moi / nous**, in three roughly equal paragraphs.
- Opening: "Madame, Monsieur," or, if the name is known, "Madame [Nom]," / "Monsieur [Nom],".
- Closing: "Veuillez agréer, Madame, Monsieur, l'expression de mes salutations distinguées." The closing must repeat the opening form of address exactly (e.g. "Madame,").
- Short sentences, no conditional mood, one letter per application, and only the competencies the job asks for.
- **Alternance:** the first paragraph states the diploma title, the start date, the rhythm and the duration. WTTJ lists "La date de début de la formation", "L'intitulé de la formation", "La fréquence cours/entreprise", "La durée de la formation", with a maximum of three sentences.
- **Length target: 250–400 words.**

**Spontaneous application by email:**
- The body is a short pitch of 5–8 lines (about 80–150 words). Do not paste the whole LM.
- Attach the CV PDF and, optionally, the LM PDF.
- Subject: "Candidature spontanée – Alternance Développeur full-stack (Bac+5) – dès octobre 2026 – Prénom NOM".
- Send on Tuesday–Wednesday morning (several guides agree; UNCERTAIN evidence).
- Address a named person where possible: the head of the company in a small firm, HR otherwise (France Travail).

**Relances:**
- Job offers: 7–10 business days after applying. Spontaneous applications: 10–15 days (Apec: processing takes 2–3 weeks).
- At most 2 follow-ups.
- The December follow-up uses the aid deadline (§4.5).

**Template A: LM for an alternance offer (FR).** `{…}` are variables and `[[…]]` are optional blocks. The linter blocks any leftover brace.

```
{prenom} {nom}
{ville} · {telephone} · {email}
{linkedin} · {github}

{entreprise}
[[{destinataire_nom}]]
{ville_entreprise}, le {date_longue}

Objet : Candidature – {intitule_poste} en alternance [[– réf. {reference}]]

{appel},

À partir d'octobre 2026, je prépare le titre « Expert en ingénierie du développement et en architecture logicielle » (Bac+5, RNCP 36469) à Exemple Digital School, en alternance sur 24 mois au rythme de trois semaines en entreprise et une semaine en formation. Je souhaite effectuer cette alternance au sein de {entreprise} en tant que {intitule_poste}.

{vous_paragraphe}   # 2–3 phrases : fait précis sur l'entreprise (produit, client, actualité, valeur) + lien avec le poste

{moi_paragraphe}    # 3–4 phrases : BUT Informatique (IUT Exemple), 1–2 réalisations chiffrées, 3 compétences clés de l'annonce

{nous_paragraphe}   # 2–3 phrases : ce que j'apporterai / ce que je viens apprendre ; disponibilité ; [[argument aide si PME et date ≤ 31/12/2026]]

Je serais heureux de vous présenter mon parcours lors d'un entretien. {formule_finale}

{prenom} {nom}
```

- `appel` = "Madame, Monsieur" or "Madame {Nom}" / "Monsieur {Nom}".
- `formule_finale` = "Je vous prie d'agréer, {appel_reprise}, l'expression de mes salutations distinguées.", where `appel_reprise` is "Madame, Monsieur", "Madame" or "Monsieur".
- **Aid sentence** (use only if `company_size < 250` is known or likely, and the start date is ≤ 31/12/2026): "Pour information, un contrat débutant avant le 1er janvier 2027 ouvre droit à l'aide de l'État à l'embauche d'un apprenti (2 000 € pour un Bac+5 dans une entreprise de moins de 250 salariés)."
- Never promise 2027 amounts.

**Template B: spontaneous email body (FR, alternance):**

```
Objet : Candidature spontanée – Alternance Développeur full-stack (Bac+5) – dès octobre 2026 – {prenom} {nom}

{appel},

Titulaire d'un BUT Informatique (IUT Exemple), je prépare à partir d'octobre 2026 un Bac+5 en ingénierie du développement à Exemple Digital School, en alternance sur 24 mois (3 semaines en entreprise / 1 semaine en formation). {fait_entreprise_1_phrase} J'aimerais mettre mes compétences en {skill_1}, {skill_2} et {skill_3} au service de vos projets.
[[Je peux démarrer immédiatement ; un contrat débutant avant le 1er janvier 2027 ouvre droit à l'aide de l'État à l'embauche d'un apprenti.]]

Vous trouverez mon CV en pièce jointe. Je serais ravi d'échanger avec vous lors d'un appel de 15 minutes.

Bien cordialement,
{prenom} {nom} – {telephone} – {linkedin}
```

**Template C: follow-up at D+7/10 (reply in the same thread):**

```
Objet : RE: {objet_initial}

{appel},

Je me permets de revenir vers vous concernant ma candidature au poste de {intitule_poste} envoyée le {date_envoi}. Toujours très motivé par {fait_entreprise_court}, je reste disponible pour un échange à votre convenance[[, sachant que ma rentrée à Exemple Digital School a lieu le {date_rentree}]].

Bien cordialement,
{prenom} {nom} – {telephone}
```

**Template D: follow-up in December** (legal urgency). Use it only when today is between 1 and 20 December 2026:

```
… Je me permets de vous relancer : un contrat d'apprentissage qui débute avant le 1er janvier 2027 permet encore de bénéficier de l'aide de l'État actuelle, et la réglementation me laisse jusqu'à début janvier pour signer (art. L6222-12-1 du Code du travail). …
```

**Template E: junior CDI (FR).** Same vous/moi/nous structure. Replace the alternance paragraph with: availability ("disponible immédiatement"), mobility ("Metz, Nancy, Thionville, Luxembourg ou télétravail"), and the latest diploma.

**LM deterministic score** (0–100):

```
coverage   25  = 25 × min(1, (#top-5 JD hard skills the candidate HAS that appear in LM) / 3)
personal   30  = 10·[company name ≥ 2×] + 10·[≥ 1 "company fact" token found in company page/JD 'company' block]
                 + 10·[JD title (normalize_title) appears in objet or 1st paragraph]  (+ref number if JD has one, else ignore)
alternance 20  = alternance JD: 5·school + 5·diploma level/title + 5·rhythm + 5·(start date AND duration)
                 CDI JD: 10·availability + 10·mobility/location
structure  15  = 5·[3–4 body paragraphs] + 5·[opening appel present] + 5·[formule finale present AND reprise == appel]
length     10  = 10 if 250 ≤ words ≤ 400, linearly → 0 at 150 or 550
penalties      −10 if ≥ 40 % of LM sentences have ≥ 0.8 rapidfuzz ratio with a CV bullet (copy-paste of CV)
               −2 per cliché (cap −6); −3 if > 2 conditional verbs ("serais|aimerais|pourrais|souhaiterais")
               (France Travail: "évitez le conditionnel"), except in the fixed closing sentence
blocks         leftover "{", "}", "[[", "]]", "XXX", "Lorem"; wrong company name (≠ current JD company) ; > 1 page
target         ≥ 80 send ; 65–79 regenerate paragraph(s) with lowest sub-score ; < 65 regenerate all
```

```python
CLICHES = ["dynamique et motive", "motive et dynamique", "je suis quelqu'un de", "team player", "passionne depuis toujours",
           "force de proposition", "rigoureux et organise", "sens du relationnel", "je me permets de vous adresser",
           "votre entreprise leader", "n'hesitez pas a me contacter"]   # 1 use of "force de proposition" tolerated
CLOSINGS = [r"je vous prie d'agreer, (madame|monsieur|madame, monsieur), l'expression de mes (salutations distinguees|sinceres salutations|respectueuses salutations)",
            r"veuillez agreer, (madame|monsieur|madame, monsieur), l'expression de mes (salutations distinguees|sinceres salutations)"]
```

### 6.7 Upload step (Playwright sync), generic pattern

```python
def upload_cv(page, pdf_path: str, docx_path: str) -> str:
    """Pick the file type the form accepts, upload via the (possibly hidden) input, return used path."""
    inp = page.locator("input[type=file]").first
    accept = (inp.get_attribute("accept") or "").lower()
    use_docx = accept and ".pdf" not in accept and (".docx" in accept or ".doc" in accept)
    path = docx_path if use_docx else pdf_path
    inp.set_input_files(path)          # works on hidden inputs (Lever: class invisible-resume-upload)
    page.wait_for_timeout(2500)        # let ATS parse & pre-fill
    return path
```

After uploading, read back the pre-filled `input[name*=name]`, `input[type=email]`, `input[type=tel]`, the experience blocks and the dates. Diff them against the profile, overwrite any mismatch, and only then continue to the questions and submit. The submit policy belongs to the application-flow research file.

---

## 7. Claims table

| # | Claim | Status | Source |
|---|---|---|---|
| 1 | Greenhouse Job Board API `?questions=true` exposes `resume` (input_file) **and** `resume_text` (textarea) | LIVE-VERIFIED | `boards-api.greenhouse.io/v1/boards/datadog/jobs/7194969?questions=true` |
| 2 | Greenhouse hosted form accepts `.pdf,.doc,.docx,.txt,.rtf` | LIVE-VERIFIED | `job-boards.greenhouse.io/embed/job_app?for=datadog&token=7194969` |
| 3 | Greenhouse unknown board → `404 {"status":404,"error":"Job not found"}` | LIVE-VERIFIED | curl |
| 4 | Lever postings API 200 for qonto/contentsquare/swile; unknown company → `404 {"ok":false,"error":"Document not found"}` | LIVE-VERIFIED | `api.lever.co/v0/postings/{company}` |
| 5 | Lever apply form has a hidden resume input without `accept`, and the text "Couldn't auto-read resume." (it parses on upload) | LIVE-VERIFIED | `jobs.lever.co/qonto/ee6d81ef-…/apply` |
| 6 | SmartRecruiters postings API 200 (Ubisoft2); unknown company → 200 `totalFound:0` | LIVE-VERIFIED | `api.smartrecruiters.com/v1/companies/Ubisoft2/postings` |
| 7 | SmartRecruiters posting detail includes `language.code` and `jobAd.sections.qualifications`; apply UI at `www.smartr.me/oneclick-ui/...` | LIVE-VERIFIED | curl + Playwright |
| 8 | Workday CXS search `POST /wday/cxs/{tenant}/{site}/jobs` works without auth; wrong site → 404 `errorCode:"S21"` | LIVE-VERIFIED | sanofi.wd3 |
| 9 | Workday CXS job detail contains `includeResumeParsing: true` | LIVE-VERIFIED | sanofi R2868669 |
| 10 | Teamtailor apply form accepts `.doc,.docx,.pptx,.pdf,.pages,.txt,.rtf` | LIVE-VERIFIED | groupeoci.teamtailor.com job 8015797 |
| 11 | HelloWork CV upload: pdf/doc/docx/odt/rtf/jpg/png, max 2 Mo | LIVE-VERIFIED | hellowork.com depot-cv page + zendesk help |
| 12 | Talentsoft career sites require a candidate account; the spontaneous application path is `/mon-compte/mon-fichier-cv.aspx` | LIVE-VERIFIED | cdcinformatique-career.talent-soft.com |
| 13 | The Jobaffinity apply form needs JavaScript | LIVE-VERIFIED | jobaffinity.fr/apply/… |
| 14 | In a 2-column PDF, pdfminer puts sidebar contact/skills under "Expérience professionnelle"; pypdf puts the sidebar first | LIVE-VERIFIED | local experiment (Playwright 1.58, pypdf 6.x, pdfminer.six 20260107) |
| 15 | Contact details in a Chromium PDF header come last (pypdf) or first (pdfminer) | LIVE-VERIFIED | local experiment |
| 16 | Unicode icons (✉ ☎ 📍) survive as glyph noise in extracted text | LIVE-VERIFIED | local experiment |
| 17 | DOCX header text is stored in `word/header1.xml`, not `document.xml`; python-docx `doc.paragraphs` skips tables | LIVE-VERIFIED | local experiment |
| 18 | Snowball French does not unify rigueur/rigoureux, curieux/curiosité, développeur/développeuse; communication→"commun" | LIVE-VERIFIED | snowballstemmer 3.1 |
| 19 | rapidfuzz token_set_ratio: "developpeur full stack" vs "developpeur fullstack" 97.7; vs "concepteur developpeur d applications" 66.7 | LIVE-VERIFIED | rapidfuzz 3.14 |
| 20 | ESCO API works without auth; the JavaScript French alt labels include "Mocha" and lack "JS" | LIVE-VERIFIED | `ec.europa.eu/esco/api/search` + `/resource/skill` |
| 21 | Playwright Python 1.58 `Page.pdf` supports `tagged` and `outline` | LIVE-VERIFIED | `inspect.signature` |
| 22 | Titles carry gender markers such as "(F/H/NB)" and "F/H H/F" | LIVE-VERIFIED | SmartRecruiters Ubisoft; Talentsoft AGIRC-ARRCO search result |
| 23 | Jobscan recommends an 80 % match rate; 75 % often works; "80% is really the sweet spot" | DOC-SOURCED | jobscan.co/blog/what-jobscan-match-rate-should-i-aim-for/ |
| 24 | Jobscan: hard skills weigh "much more heavily than soft skills"; priority order hard skills, education, job title, soft skills, other keywords; exact title match → 3.5× interview rate | DOC-SOURCED | jobscan.co/jobscan-tutorial |
| 25 | Jobscan: use acronym + long form; ATS "doesn't always recognize synonyms" | DOC-SOURCED | jobscan.co/blog/top-resume-keywords-boost-resume/ |
| 26 | Jobscan: dates MM/YYYY or Month YYYY, consistent; avoid year-only and day-month-year | DOC-SOURCED | jobscan.co/blog/resume-dates/ |
| 27 | Jobscan (April 2026): PDF safest unless .doc requested; headers/footers/text boxes often ignored; icons become gibberish/[NULL] | DOC-SOURCED | jobscan.co/blog/ats-formatting-mistakes/ |
| 28 | SkillSyncer report buckets Hard / Soft / Other keywords / Degree & Title with job vs resume counts; recommended score 80 | DOC-SOURCED | skillsyncer.com/scans/sample |
| 29 | Resume Worded: "A score lower than 80 suggests that your resume is missing important skills" | DOC-SOURCED | resumeworded.com/targeted-resume |
| 30 | Teal match score = share of JD key terms present; 80 %+ strong | DOC-SOURCED (third-party summary; Teal KB returned 403) | tealhq.com tool pages |
| 31 | CVDesignR scanner checks four areas: file (PDF, orientation, size, file name), content, readability, sections; weights not published | DOC-SOURCED | cvdesignr.com/en/ats-scanner |
| 32 | France Travail: DOCX > text PDF; one column; no tables or text boxes; Arial/Calibri 10–12 pt; standard headings; acronym + meaning; "Janvier 2020 – Décembre 2023"; no skill bars | DOC-SOURCED | francetravail.fr …/logiciels-de-recrutement-ats-ada.html |
| 33 | Apec (17/07/2026): standard headings "Expérience professionnelle", "Compétences", "Formation"; spell out acronyms; quantify | DOC-SOURCED | apec.fr …/optimisez-votre-cv-pour-passer-le-filtre-des-ats-.html |
| 34 | Workday parsing "results can vary based on resume format and order of words"; best without images; Languages and Skills not auto-filled | DOC-SOURCED | doc.workday.com admin guide "Concept: Resume Parsing" |
| 35 | Workday suggests skills by ML from the entire resume (certificates, company, degree, education, experience, summary…) | DOC-SOURCED | doc.workday.com "Setup Considerations: Skills in Recruiting" |
| 36 | Workday HiredScore grades candidates A–D | DOC-SOURCED | workday.com HiredScore pages (search results) |
| 37 | Oracle Taleo: resume parsing by a third-party partner "as-is"; French (France) "Limited" | DOC-SOURCED | docs.oracle.com/en/cloud/saas/taleo-enterprise/25a/ottcg/r-supportedlanguages.html |
| 38 | Oracle Taleo attachments default max 1 048 576 bytes; Basic package 10 files / 5 MB per entity | DOC-SOURCED | docs.oracle.com …/c-attachmentpermissionsandsettings.html |
| 39 | SAP SuccessFactors uses Textkernel for resume parsing | DOC-SOURCED (SAP page not fetchable; cited by resumap.io and a Textkernel integration page) | resumap.io/articles/textkernel-parsing-test |
| 40 | SuccessFactors parsing accepts DOCX/PPT/TXT and TIFF/JPG/BMP/GIF; PDF status unclear | UNCERTAIN | help.sap.com (search snippet only) |
| 41 | Greenhouse upload ≤ 100 MB; formats doc/docx/pdf/rtf/txt; French parsing supported | DOC-SOURCED | support.greenhouse.io articles 360052218132 and 205019689 |
| 42 | Greenhouse does not parse files > 2.5 MB | UNCERTAIN | search snippet only |
| 43 | Greenhouse: ~1 % of about 300 M applications per year contain hidden prompt injection; recruiters alerted | DOC-SOURCED (press, via search) | multiple articles (search results) |
| 44 | WTTJ ATS scores each application against recruiter criteria and explains the score | DOC-SOURCED | solutions.welcometothejungle.com/en/ats |
| 45 | WTTJ candidate upload: PDF ≤ 5 MB | UNCERTAIN | help centre returned 403; third-party blog |
| 46 | Flatchr accepts Word or PDF; offers "matching par IA" | DOC-SOURCED | help.flatchr.io; flatchr.io/matching-tri-cv-offre-emploi |
| 47 | Teamtailor Resume Parser pre-fills name/email/phone/LinkedIn | DOC-SOURCED | teamtailor.com product news |
| 48 | French ATS market: >150 vendors; large accounts on Avature/Cegid/Oracle/SAP/Workday/iCIMS/Greenhouse; SMEs on Teamtailor/Flatchr/Beetween/Welcome Kit/Factorial/Lever/Recruitee; ESN on BoondManager; SAP bought SmartRecruiters in September 2025 | DOC-SOURCED | auxinepartners.com insights (French ATS market map) |
| 49 | About 147 ATS in France; 80 % of recruiters have or plan an ATS (HelloWork 2024 study) | DOC-SOURCED | rhmatin.com (2025) |
| 50 | No public market-share figures per ATS in France | UNCERTAIN (absence of evidence) | – |
| 51 | LinkedIn Easy Apply: Word or PDF, recommend < 2 MB, stores the 4 latest resumes | DOC-SOURCED | linkedin.com/help/linkedin/answer/a510363 |
| 52 | L6222-12: the CFA period and the employer practical period must start ≤ 3 months after the contract starts | DOC-SOURCED | code.travail.gouv.fr/code-du-travail/l6222-12 |
| 53 | L6222-12-1: without an employer, a person aged 16–29 may start the cycle for ≤ 3 months as a *stagiaire*; the contract is shortened by the elapsed months | DOC-SOURCED | code.travail.gouv.fr/code-du-travail/l6222-12-1 |
| 54 | L6222-7-1: contract duration from 6 months to 3 years | DOC-SOURCED | code.travail.gouv.fr/code-du-travail/l6222-7-1 |
| 55 | The contract may end up to 2 months after the end of the cycle; ≥ 25 % of time at the CFA | DOC-SOURCED | centre-inffo.fr FAQ |
| 56 | Aid for contracts concluded from 08/03/2026 and starting before 01/01/2027: <250 → 5 000/4 500/2 000 € (levels 3–4/5/6–7); ≥250 → 2 000/1 500/750 €; RQTH 6 000 € | DOC-SOURCED | Décret 2026-168 (Légifrance), service-public F23556 (verified 22/07/2026), Opco EP |
| 57 | Aid paid monthly by the ASP in year 1; ≥250 quota 5 % (or 3 % + growth) by 31/12 of the following year | DOC-SOURCED | service-public F23556; Décret 2026-168 |
| 58 | Employer participation of €750 per contract at level ≥ 6, for contracts from 01/07/2025 (€200 after a rupture) | DOC-SOURCED | Décret 2025-585 (Légifrance) |
| 59 | The 2027 aid is announced as maintained (Prime Minister, 9/09/2026) but no decree is published | DOC-SOURCED (political statement) / 2027 amounts UNCERTAIN | editions-tissot.fr; tpeactu.fr |
| 60 | The 2026 exceptional aid no longer covers the contrat de professionnalisation | DOC-SOURCED (secondary) | alternance-professionnelle.fr; France Travail aid for 26+ |
| 61 | EDS Bac+5 "Expert en Ingénierie du développement et en architecture logicielle": RNCP 36469, level 7, October 2026, 2 years / 910 h, 1 week per month at school | DOC-SOURCED | metz-numeric-school.fr programme page |
| 62 | RNCP 36469 is currently active on France Compétences | UNCERTAIN | francecompetences.fr page not parseable |
| 63 | Luxembourg cross-border apprenticeship covers vocational trades only, not Bac+5 | UNCERTAIN (strong indication) | men.public.lu; frontaliers-grandest.eu |
| 64 | AI Act Digital Omnibus (Reg. 2026/1744, in force 27/07/2026) moved Annex III obligations (recruitment) to 02/12/2027 | DOC-SOURCED (secondary) | alpacax.com; Gibson Dunn; Travers Smith (search) |
| 65 | France Travail LM: one page, Arial 12, vous/moi/nous, "Madame, Monsieur" … "Veuillez agréer…", avoid the conditional | DOC-SOURCED | francetravail.fr …/10-astuces-pour-ecrire-votre-let.html |
| 66 | Alternance LM must state the start date, diploma title, rhythm and duration in the first paragraph | DOC-SOURCED | welcometothejungle.com/fr/articles/alternance-lettre-de-motivation-… |
| 67 | Spontaneous application: address the head of a small company or HR otherwise; follow up if there is no answer | DOC-SOURCED | francetravail.fr …/la-candidature-spontanee--mode-d.html |
| 68 | Follow-up timing 7–10 business days (offers), 10–15 days (spontaneous) | DOC-SOURCED (secondary, consensus of guides) | onlinecv.fr, jobteaser.com, Apec (search) |
| 69 | The Chromium `font-variant-ligatures:none` precaution is needed for Calibri/Garamond | UNCERTAIN (not reproduced; Arial showed no ligatures) | – |
| 70 | Scoring weights 45/15/10/10/5/5/10 and thresholds 80/70/60 | Design choice derived from rows 23–31 (not a vendor formula) | this document |
| 71 | The §6.4 `lint_pdf`/`lint_docx` sketches flag the 2-column layout (unstable heading order, icon glyphs), a header-only contact, and a DOCX header/table, while a clean 1-column PDF scores 1.0 | LIVE-VERIFIED | local run on the experiment files |
| 72 | The §6.1/§6.2 code sketches run as-is and disambiguate *vue*/Vue.js, réactif/React, Java/JavaScript, "Solid"/SOLID, and slash lists like "JavaScript/TypeScript" | LIVE-VERIFIED | local sanity run (Python 3.12, rapidfuzz 3.14) |
