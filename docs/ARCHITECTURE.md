# jobbot — architecture & module contracts

Personal automation that finds offers (alternance / CDI), scores them, tailors CV + cover letter with an
explainable ATS score, applies (LinkedIn Easy Apply, HelloWork, La Bonne Alternance, e-mail), sends
spontaneous applications to companies found via open data, tracks everything in SQLite, follows up and
reads replies.

**This file is the contract.** Every module must expose exactly the public API below (names, parameters,
return types). Private helpers are free. If you need something from a module that another agent writes,
code against the signature here — do not re-implement it.

## Ground rules (all modules)

- Python 3.12, **synchronous** code (httpx sync client, Playwright **sync** API, sqlite3). No asyncio.
- Models are immutable pydantic (`frozen=True`). Never mutate: `obj.model_copy(update={...})`.
- Persistence only through `jobbot.db.Repository` (already written — read it). No SQL elsewhere.
- LLM only through `jobbot.llm.LLMClient` (`complete`, `complete_json(prompt, PydanticModel)`, `fast=True`
  for cheap tasks). Always have a deterministic fallback path when the LLM raises `LLMError`.
- HTTP through `jobbot.http.make_client()` / `request_with_retry(...)` (+ `HostThrottle` for scraping).
- Anything acting on the user's behalf (apply, send e-mail) goes through `jobbot.pacing.Pacer`
  (`ensure_available(channel)`, `record(channel, ok)`, `between_applications()`), and honours `ctx.dry_run`.
- Logging: `logging.getLogger(__name__)`. User-facing messages in **French**. Never log secrets.
- Handle errors explicitly; a failing source/company/job must never abort the whole run (catch, log, continue).
- Files ≤ 400 lines, functions ≤ 50 lines, no deep nesting.
- Tests in `tests/` with pytest, **no network** (use `respx` for httpx, fake objects for Playwright/SMTP/IMAP,
  `FakeLLM` for the LLM). Put HTML/JSON samples in `tests/fixtures/<module>/`. Run with
  `.venv\Scripts\python.exe -m pytest tests/<your files> -q`.
- Shared fixtures in `tests/conftest.py`: `profile`, `settings`, `repo`, `fake_llm`, `ctx` (AppContext with
  FakeLLM, dry_run=True, tmp project root), `make_job(**overrides)`, `make_company(**overrides)`.
- Personal data never lives in tracked files: the real profile is `config/profile.yaml` (gitignored);
  `config/profile.example.yaml` is a fictitious template (Jean DUPONT) copied by `jobbot init`, and tests only
  read its twin `tests/fixtures/profile.yaml` (they pass on a fresh clone without `config/profile.yaml`).
- Research notes with real endpoints/selectors: `docs/research/*.md` — READ the ones relevant to you.

## Already written (read, don't rewrite)

| File | Content |
|---|---|
| `models.py` | `JobOffer`, `Company`, `ContactEmail`, `KeywordHit`, `ATSReport`, `TailoredDocuments`, `Application`, `ApplicationEvent`, `SentEmail`, enums `ContractType`, `ApplyMethod`, `JobStatus`, `ApplicationStatus` |
| `config.py` | `Profile` (+ `all_skills()`, `full_text()`), `Settings`, `Secrets`, `LocationPref`, `Experience`, `SkillGroup`, `Project`, `Education`, `Language`, loaders |
| `context.py` | `AppContext(root, settings, secrets, profile, repo, llm, dry_run)`, `.path(rel)`, `.output_dir`, `AppContext.build()` |
| `db.py` | `Repository` (jobs, companies, documents, applications, events, sent_emails, action_log, blacklist, kv, stats) |
| `llm.py` | `LLMClient`, `ClaudeCLIClient`, `AnthropicClient`, `FakeLLM`, `LLMError`, `extract_json` |
| `http.py` | `make_client`, `request_with_retry`, `HostThrottle`, `BROWSER_UA` |
| `pacing.py` | `Pacer`, `QuotaExceeded`, channels `linkedin hellowork lba email spontaneous followup` |
| `textutil.py` | `normalize`, `normalize_company`, `dedup_key`, `stable_id`, `html_to_text`, `extract_emails`, `slugify`, `truncate` |
| `clock.py` | `now()`, `parse_dt`, `iso`, `to_paris`, `french_date()` |
| `sources/base.py` | `SearchQuery`, `JobSource` protocol, `SourceUnavailable` |
| `apply/base.py` | `ApplyResult`, `Applier` protocol |

## Module contracts

### sources/ — job collection
- `sources/france_travail.py`: `class FranceTravailSource(JobSource)` — `__init__(self, secrets: Secrets, client: httpx.Client | None = None)`, `name = "france_travail"`. OAuth2 client_credentials, token cached until expiry. Maps contract (natureContrat apprentissage → `ALTERNANCE`), `contact.courriel` → `contact_email` + `apply_method=EMAIL`, `urlPostulation`/`origineOffre.urlOrigine` → `apply_url` + `EXTERNAL`.
  France Travail NEVER exposes recruiter e-mails (`contact.courriel` is a sentence + link) — do not map it to `contact_email` unless it is a real address. Alternance = `natureContrat=E2,FS`; never combine with `typeContrat` in one query. Use the ROME 4.0 code list from the research. Remote detected from description text.
- `sources/lba.py`: `class LaBonneAlternanceSource(JobSource)` — `__init__(self, secrets, client=None)`, `name = "lba"`; keyless website JSON API (research "path B") by default, official API (path A) additionally when `LBA_API_TOKEN` is set; `search()` returns offers with `apply_method=LBA` when `smart_apply`/`hasEmail`/`recipient_id` allow the in-site application (store in `raw` what the applier needs: source, id, recipient_id…), partner offers routed to HELLOWORK/EXTERNAL; `raw["application_count"]` kept. Also `def search_recruiters(self, latitude: float, longitude: float, radius_km: int, romes: list[str]) -> list[Company]` (companies likely to hire apprentices, source=`"lba_recruteur"`, `raw` keeps what is needed to apply through LBA when `hasEmail`).
- `sources/hellowork.py`: `class HelloWorkSource(JobSource)` — `__init__(self, client=None)`, `name="hellowork"`; search is kept tiny (page 1 only, ≥ 5 s between requests — robots/CGU forbid crawling); `def job_from_id(self, offer_id: str) -> JobOffer | None` (offer page `/fr-fr/emplois/{id}.html`, JSON-LD JobPosting + dataLayer; 410 → None); `apply_method=HELLOWORK` for in-site modes (`ATS`, `Smartapply*`), else EXTERNAL.
- `sources/alerts.py`: `def jobs_from_alert_email(from_addr: str, subject: str, body_html: str, body_text: str) -> list[JobOffer]` — recognises job-alert e-mails (HelloWork, LinkedIn, Indeed, WTTJ, France Travail, LBA…) and returns minimal offers (id, source, external_id, url, title/company when present; description filled later by `fetch_details`). Called by `mail.inbox.process_inbox`. This is the ToS-friendly discovery channel for HelloWork/LinkedIn.
- `sources/linkedin.py`: `class LinkedInSource(JobSource)` — logged-out guest endpoints for search + detail; `__init__(self, client=None, easy_apply_only: bool = True)`, `name="linkedin"`, `apply_method=LINKEDIN_EASY_APPLY` when Easy Apply.
- `sources/wttj.py`: `class WelcomeToTheJungleSource(JobSource)` — `__init__(self, client=None)`, `name="wttj"`, Algolia search, apply → EXTERNAL (never auto-submit).
- `sources/jobspy_source.py`: `class JobSpySource(JobSource)` — `__init__(self, countries: tuple[str, ...] = ("france",))`, optional (`python-jobspy` may be absent → `available()` False), `name="indeed"`, read-only, apply → EXTERNAL (Indeed ToS forbids bots in apply).
- `sources/free_work.py`: `class FreeWorkSource(JobSource)` — `__init__(self, client=None)`, `name="free_work"`, public JSON API (junior CDI + Luxembourg track), apply → EXTERNAL.
- `sources/apec.py`: `class ApecSource(JobSource)` — `__init__(self, client=None)`, `name="apec"`, search endpoint only (CDI track), apply → EXTERNAL.
- `sources/registry.py`:
  - `def build_sources(ctx: AppContext, names: list[str] | None = None) -> list[JobSource]`
  - `def build_queries(profile: Profile, settings: Settings) -> list[SearchQuery]` (job_titles × locations × contract types; dedupe; remote query when `remote_ok`)
  - `class CollectReport(frozen)`: `new: int, updated: int, per_source: dict[str, int], errors: dict[str, str]`
  - `def collect_jobs(ctx, sources: list[JobSource] | None = None, queries: list[SearchQuery] | None = None) -> CollectReport` — upserts via `repo.upsert_job`; skips jobs whose company is in `profile.target.exclude_companies` or blacklisted; one source failing never stops the others.
  - `def ensure_details(ctx, job: JobOffer) -> JobOffer` — if the description is short (< 400 chars), call the owning source's `fetch_details`, persist with `repo.save_job`, return the new job (errors → return job unchanged).

### matching/ — relevance & ATS
- `matching/lexicon.py`: `SKILLS: dict[str, list[str]]` canonical → variants (FR/EN, ≥150 tech entries), `SOFT_SKILLS: dict[str, list[str]]`; `def canonicalize(term: str) -> str`; `def find_skills(text: str) -> set[str]` (canonical names found; word-boundary aware; handles `C#`, `C++`, `.NET`, `Node.js`, `CI/CD`).
- `matching/geo.py`: `def haversine_km(lat1, lon1, lat2, lon2) -> float`; `def geocode(query: str, client: httpx.Client | None = None) -> tuple[float, float] | None` (French geocoding API, cached in-process).
- `matching/keywords.py`: `def extract_keywords(job: JobOffer, llm: LLMClient | None = None, *, max_keywords: int = 30) -> list[KeywordHit]` — LLM extraction (fast model) of hard skills/tools/soft skills/title variants with weights (required=1.0, nice-to-have=0.5), merged with deterministic lexicon hits; pure-lexicon fallback on `LLMError`/None. Weights in (0, 1]. No duplicates (canonicalized).
- `matching/ats.py`: `def score_document(text: str, keywords: list[KeywordHit], job_title: str, *, kind: Literal["cv", "letter"] = "cv") -> ATSReport` — deterministic, explainable. Weights for CV: keywords 60, title 15, sections 15, format 10 (letter: keywords 70, title 10, structure 20). Fuzzy/synonym-aware matching via lexicon + rapidfuzz. Also `def keyword_coverage(text: str, keywords: list[KeywordHit]) -> tuple[list[KeywordHit], list[KeywordHit]]` (matched, missing).
- `matching/match.py`:
  - `def match_job(job: JobOffer, profile: Profile) -> tuple[float, list[str]]` — deterministic 0-100 relevance + French reasons; hard filters (excluded title/description words, contract type not wanted, too far, experience years required > max, **school/CFA "fake" alternance offers** — training organisations recruiting students: ISCOD, Studi, OpenClassrooms, IPAC, MyDigitalSchool, Digital Campus, "formation 100 % financée", "trouve ton entreprise", "rejoins notre école"…, **"business developer"/"développeur commercial" sales jobs**) return score 0 with the reason.
  - `class MatchReport(frozen)`: `scored: int, shortlisted: int, filtered_out: int`
  - `def score_new_jobs(ctx: AppContext) -> MatchReport` — scores jobs in status NEW; SHORTLISTED if ≥ `thresholds.min_match_score` else FILTERED_OUT (`repo.set_job_match`). Marks cross-board duplicates of an already shortlisted/applied job as SKIPPED.

### documents/ — tailored CV & letter
- `documents/models.py`: `class TailoredCV(frozen)`: `identity: Identity, headline: str, summary: str, experiences: list[Experience], projects: list[Project], education: list[Education], skills: list[SkillGroup], languages: list[Language], interests: list[str], alternance: AlternanceInfo | None`; `def cv_to_text(cv) -> str`. `class CoverLetter(frozen)`: `subject: str, greeting: str, paragraphs: list[str], closing: str, email_subject: str, email_body: str`; `def letter_to_text(letter) -> str`.
- `documents/tailor.py`:
  - `def tailor_cv(profile: Profile, keywords: list[KeywordHit], llm: LLMClient, *, job: JobOffer | None = None, company: Company | None = None, target_score: float = 80, max_iterations: int = 3) -> tuple[TailoredCV, ATSReport, list[str]]` — LLM rewrites headline/summary/bullets, reorders & selects skills/experiences, **never invents** (only facts from `profile`); iterates with missing-but-truthful keywords until target or max iterations; returns best CV, its ATS report and truth warnings. Fallback without LLM: deterministic reordering of skills/bullets by keyword relevance.
  - `def check_truthfulness(cv: TailoredCV, profile: Profile) -> list[str]` — every skill/tool/company/date/degree in the CV must exist in the profile (lexicon-canonical comparison); returns French warnings.
  - `def truthful_missing(missing: list[KeywordHit], profile: Profile) -> list[KeywordHit]`.
- `documents/letter.py`: `def write_cover_letter(profile: Profile, llm: LLMClient, *, job: JobOffer | None = None, company: Company | None = None, keywords: list[KeywordHit] | None = None) -> CoverLetter` — French, vous/moi/nous, 250-350 words, alternance details (school, rhythm, 24 months, start date) when relevant, spontaneous variant when `job is None`. Also produces a short e-mail subject/body (e-mail body ≤ 150 words, letter attached). Includes the opt-out sentence for spontaneous e-mails. Fallback template without LLM.
- `documents/render.py`: `def render_cv_html(cv: TailoredCV) -> str`; `def render_cv_pdf(cv: TailoredCV, out_path: Path) -> Path` (Jinja2 `templates/cv.html.j2` → Playwright Chromium `page.pdf`, A4, 1 page target, single column, real text, standard fonts, French headings "Expérience professionnelle", "Formation", "Compétences"...); `def render_cv_docx(cv, out_path) -> Path`; `def render_letter_pdf(letter: CoverLetter, profile: Profile, out_path: Path, *, recipient: str | None = None) -> Path`; `def pdf_text(path: Path) -> str` (pypdf).
- `documents/service.py`: `def prepare_documents(ctx: AppContext, *, job: JobOffer | None = None, company: Company | None = None) -> TailoredDocuments` — extract keywords (job) or derive them from company NAF/description + profile target (spontaneous) → tailor → render PDF/DOCX into `output/<yyyy-mm-dd>/<slug-company>-<slug-title>/CV_<First>_<LAST>.pdf` and `Lettre_de_motivation_<First>_<LAST>.pdf` (never put the company name in attachment file names) → **parse the PDF back** and compute the final ATS on the extracted text → letter → render → `repo.save_documents` → return. Sets job status DOCS_READY.
- `documents/importer.py`: `def import_cv(pdf_path: Path, llm: LLMClient) -> dict` (profile YAML dict extracted from an existing CV PDF) and `def write_profile_yaml(data: dict, out: Path) -> Path`.

### apply/ — submitting applications
- `apply/browser.py`: `@contextmanager def open_browser(ctx: AppContext, *, headless: bool | None = None) -> Iterator[BrowserContext]` (Playwright `launch_persistent_context(user_data_dir=settings.paths.browser_profile, channel=settings.browser_channel, locale="fr-FR")`, falls back to bundled Chromium if channel missing); `def login(ctx, site: Literal["linkedin", "hellowork", "wttj"], timeout_s: int = 600) -> bool` (opens login page, waits until the user is logged in); `def is_logged_in(page, site) -> bool`; `def human_pause(pacer: Pacer)`.
- `apply/answers.py`: `class FormQuestion(frozen)`: `label: str, kind: Literal["text","number","textarea","select","radio","checkbox","typeahead","file"], options: list[str] = [], required: bool = False`; `class FormAnswer(frozen)`: `value: str | list[str] | None, confidence: float, source: Literal["rule","profile","memory","llm","none"]`; `class FormAnswerer`: `__init__(self, profile: Profile, llm: LLMClient | None, repo: Repository, job: JobOffer | None = None)`, `def answer(self, q: FormQuestion) -> FormAnswer` (rules for common FR/EN questions → `profile.answers` / `years_experience` → remembered answers (repo kv, key `answer:<normalized label>`) → LLM fast with profile context; numeric answers are integers as strings; select/radio values must be one of `options`); `def remember(self, label: str, value: str) -> None`.
- `apply/linkedin.py`: `class LinkedInApplier(Applier)`: `channel="linkedin"`, `__init__(self, ctx: AppContext, context: BrowserContext, pacer: Pacer, answerer_factory: Callable[[JobOffer], FormAnswerer])`, `can_apply(job)` (source linkedin & method LINKEDIN_EASY_APPLY), `apply(job, docs)` — Easy Apply modal loop (upload `docs.cv_pdf`, fill fields, next/review/submit, uncheck "follow company", detect errors, max 12 steps); unknown required field with low confidence → discard application, return NEEDS_MANUAL. `dry_run` → fill everything but do not click submit (discard).
- `apply/hellowork.py`: `class HelloWorkApplier(Applier)`: same constructor shape, `channel="hellowork"`. Logged-in profile only (never automate login/captcha/OTP); in-site modes only; upload tailored CV, fill `MotivationLetter` (≤ 2 900 chars) from `docs.letter_text`; screenshot before submit into `data/logs/screens/`; `dry_run` → stop before submit. External modes → NEEDS_MANUAL.
- `apply/assisted.py`: `def assisted_apply(ctx: AppContext, context: BrowserContext, job: JobOffer, docs: TailoredDocuments, answerer: FormAnswerer) -> ApplyResult` — for EXTERNAL offers (company ATS, WTTJ, Indeed, APEC…): open `job.apply_url or job.url`, click an obvious "Postuler/Apply" button if present, **pre-fill** generic fields (by `autocomplete`, `name`, `id`, `placeholder`, `<label>` text, FR/EN: prénom, nom, e-mail, téléphone, ville, LinkedIn, portfolio, message/lettre), upload the CV into file inputs, answer other questions via the answerer, then **never submits**: it waits for the human to submit in the visible window and returns NEEDS_MANUAL with detail "pré-rempli". `def autofill_page(page, profile, docs, answerer) -> int` (number of fields filled) is reusable. Known ATS (Greenhouse, Lever, Teamtailor, SmartRecruiters, Workday…) get specific selectors when the research gives them.
- `apply/email_apply.py`: `class EmailApplier(Applier)`: `__init__(self, ctx, mailer: Mailer)`, `channel="email"`, `can_apply(job)` → has `contact_email` and not blacklisted; sends letter as body + CV/letter PDF attachments; returns `email_message_id`.
- `apply/lba.py`: `class LbaApplier(Applier)`: `__init__(self, ctx: AppContext, client: httpx.Client | None = None)`, `channel="lba"` — La Bonne Alternance "Candidature simplifiée" (research: fetch the offer detail to get the per-offer token, then the same JSON POST the website makes, with the CV PDF base64 + message from `docs.letter_text`); never apply twice to the same offer/SIRET; `dry_run` → build payload, don't POST. Also `def apply_to_recruiter(self, company: Company, docs: TailoredDocuments) -> ApplyResult` for LBA recruiters with `hasEmail` (spontaneous application relayed by LBA).
- `apply/service.py`:
  - `class ApplyReport(frozen)`: `applied: int, needs_manual: int, failed: int, skipped: int, pending_review: int, details: list[str]`
  - `def select_jobs_to_apply(ctx, limit: int | None = None) -> list[JobOffer]` — DOCS_READY/APPROVED jobs, mode rules (auto: score ≥ `auto_apply_min_score` or APPROVED; others → PENDING_REVIEW), not already applied, company not applied in the last 60 days.
  - `def apply_to_jobs(ctx: AppContext, *, channels: list[str] | None = None, limit: int | None = None, job_ids: list[str] | None = None) -> ApplyReport` — routes each job to its applier by `apply_method`; quota per channel; creates `Application` (+ `next_followup_at` from `followup.compute_next_followup`) and an `applied` event; job status from `ApplyResult.status`; EXTERNAL jobs → NEEDS_MANUAL (docs ready, link in dashboard). Browser opened lazily once per run.

### mail/ — sending & reading
- `mail/mailer.py`: `class OutgoingEmail(frozen)`: `to: str, subject: str, body: str, attachments: list[str] = [], in_reply_to: str | None = None, references: list[str] = [], kind: Literal["application","spontaneous","followup"], application_id: int | None = None`; `class Mailer`: `__init__(self, secrets: Secrets, repo: Repository, profile: Profile, *, dry_run: bool = False, outbox_dir: Path | None = None, smtp_factory: Callable[..., smtplib.SMTP] | None = None)`, `def send(self, email: OutgoingEmail) -> SentEmail` (Message-ID via `make_msgid`, threading headers, `Re:` subject for follow-ups, UTF-8 plain text (+ minimal HTML alternative), attachments; refuses blacklisted recipients with `MailError`; dry-run writes `.eml` to outbox instead of sending; logs `SentEmail`); `class MailError(RuntimeError)`; `def build_mailer(ctx: AppContext) -> Mailer | None` (None + warning when SMTP creds missing and not dry-run).
- `mail/inbox.py`: `class InboundMessage(frozen)`: `uid: int, folder: str, message_id: str, from_addr: str, from_name: str, subject: str, date: datetime | None, in_reply_to: str | None, references: list[str], body_text: str, body_html: str, auto_submitted: bool, is_bounce: bool, bounced_recipient: str | None, gm_thread_id: str | None`; scan Gmail `\All` **and** `\Junk` special-use folders incrementally by UID (per-folder last UID + UIDVALIDITY in kv); job-alert e-mails → `sources.alerts.jobs_from_alert_email` → `repo.upsert_job` (lazy import); platform notification e-mails (LBA "Suite donnée à votre candidature - {company}", "Réponse négative de {company}…", HelloWork/LinkedIn status mails) update the matching application by company name; `class ReplyClassification(frozen)`: `category: Literal["interview","rejection","positive","info_request","auto_reply","bounce","unsubscribe","other"], summary: str, confidence: float`; `def fetch_new_messages(secrets: Secrets, since_uid: int, *, imap_factory=None, limit: int = 200) -> list[InboundMessage]`; `def classify_reply(msg: InboundMessage, llm: LLMClient | None) -> ReplyClassification` (rules first, LLM fast fallback); `class InboxReport(frozen)`: `scanned: int, matched: int, updates: list[str]`; `def process_inbox(ctx: AppContext, *, imap_factory=None) -> InboxReport` (match by In-Reply-To/References → `repo.find_application_by_message_id`, fallback sender address/domain; update Application status + event; unsubscribe → blacklist; bounce → BOUNCED + blacklist address; stores last UID in `kv["imap_last_uid"]`; notifies on interview/positive).

### followup & notifications
- `followup.py`: `def compute_next_followup(app: Application, cfg: FollowupSettings, *, after: datetime | None = None) -> datetime | None`; `def write_followup(profile: Profile, app: Application, llm: LLMClient | None, *, number: int) -> tuple[str, str]` (subject, body; short, polite, French; template fallback); `class FollowupReport(frozen)`: `sent: int, manual: int, closed: int, details: list[str]`; `def run_followups(ctx: AppContext, mailer: Mailer | None) -> FollowupReport` — due applications: with `email_to` → send in-thread (`in_reply_to` = last thread id), update `followup_count`, `email_thread_ids`, `next_followup_at`, status FOLLOWED_UP; without email → event `manual_followup` (listed in dashboard); past `give_up_after_days` → NO_RESPONSE.
- `notify.py`: `def notify(ctx: AppContext, title: str, message: str, *, level: str = "info") -> None` (levels and
  server channels: see v4 below) — log + Windows toast (PowerShell, best effort, never raises) + optional Jarvis TTS when `settings.notify_jarvis` and `settings.jarvis_script` exists: `subprocess.run([sys.executable, jarvis_script, "--type", "tell", message], timeout=15)`.

### outreach/ — spontaneous applications
- `outreach/companies.py`: `class DiscoverReport(frozen)`: `found: int, new: int, errors: list[str]`; `def search_companies(naf_code: str, department: str, *, min_headcount_code: str | None, client=None, max_pages: int = 5) -> list[Company]` (recherche-entreprises.api.gouv.fr; active companies only; leaders' names from `dirigeants`); `def discover_companies(ctx: AppContext, *, naf_codes: list[str] | None = None, departments: list[str] | None = None, limit: int = 300) -> DiscoverReport` (+ LBA recruiters when available; skips blacklisted/excluded; distance filter from profile locations when coordinates exist).
- `outreach/domains.py`: `def find_domain(company: Company, *, client=None, secrets: Secrets | None = None) -> str | None` — candidates from Clearbit autocomplete → slug guessing (+ DNS) → Wikidata by SIREN → Brave (if key) → DuckDuckGo html last resort (≥ 25 s between queries, stop on bot-flag); **accept only if the homepage or /mentions-legales contains the SIREN (or TVA FRxx+SIREN)**, else a strong name match flagged lower confidence. Geocoding via `data.geopf.fr/geocodage` (api-adresse is sunset).
- `outreach/emails.py`: `def decode_cfemail(hex_str: str) -> str`; `def deobfuscate(text: str) -> str`; `def scrape_emails(html: str, domain: str) -> list[str]`; `def crawl_site_emails(domain: str, *, client=None, max_pages: int = 8) -> list[ContactEmail]` (home, /contact, /mentions-legales, /recrutement, /carrieres, /nous-rejoindre, /jobs, /equipe…); `def guess_patterns(first: str, last: str, domain: str) -> list[str]`; `def has_mx(domain: str) -> bool`; `def hunter_domain_search(domain: str, api_key: str, *, client=None) -> list[ContactEmail]`; `def rank_contacts(contacts: list[ContactEmail]) -> list[ContactEmail]` (recruitment/rh/jobs > named HR > leader (small companies) > contact/info; drop noreply, webmaster, rgpd/dpo, factures/compta, support, web-agency/host addresses, placeholder addresses); keep only same-domain or webmail addresses; also parse JSON-LD `email`/`contactPoint`; detect careers page / company ATS link (teamtailor, jobtoolz, welcometothejungle, lever, recruitee, workable, flatchr, taleez, jobaffinity, digitalrecruiters…) → `company.careers_url`; guessed nominative addresses (dirigeants personnes physiques, excluding "Commissaire aux comptes") only with MX + Hunter verification, confidence ≤ 0.4, never sent unverified; `def enrich_company(ctx: AppContext, company: Company) -> Company`; `class EnrichReport(frozen)`: `processed: int, with_email: int, without_email: int`; `def enrich_companies(ctx, *, limit: int = 50) -> EnrichReport`.
- `outreach/spontaneous.py`: `class CampaignReport(frozen)`: `sent: int, skipped: int, failed: int, details: list[str]`; `def run_spontaneous_campaign(ctx: AppContext, mailer: Mailer | None, *, limit: int | None = None) -> CampaignReport` — enriched companies not contacted, not blacklisted, not applied to recently → `prepare_documents(ctx, company=c)` → e-mail (body = short e-mail, attachments CV + letter) → `Application(method=EMAIL, company_id=…)` + event + `next_followup_at` → company status `contacted`; quota channel `spontaneous`; review mode → only prepares docs and marks company `pending_review`.

### orchestration & UI
- `pipeline.py`: `class DailyReport(frozen)` (one field per step with that step's report or error string); `def run_daily(ctx: AppContext, *, steps: list[str] | None = None) -> DailyReport` — steps in order: `collect`, `score`, `prepare` (docs for up to N shortlisted jobs, best score first), `apply`, `discover`, `enrich`, `spontaneous`, `inbox`, `followup`; each step isolated (exception → recorded, next step runs); final `notify` summary.
- `cli.py` (Typer `app`): `init`, `doctor`, `import-cv`, `login`, `search`, `score`, `prepare`, `ats`, `apply`, `assist` (loops over NEEDS_MANUAL jobs: opens each pre-filled, asks "Candidature envoyée ? [o/n/passer]" and records the Application), `approve`, `reject`, `discover`, `enrich`, `spontaneous`, `inbox`, `followup`, `run`, `status`, `export`, `dashboard`, `schedule`. Global `--dry-run`. Rich tables. French help texts.
- `dashboard/app.py`: Streamlit — KPIs, pipeline by status, jobs table (score, ATS, links, download CV/LM), review queue (approve/reject), manual-actions queue (external applications, manual follow-ups), applications timeline, companies (spontaneous) table, settings summary.
- `scripts/install_task.ps1`: Windows Task Scheduler daily run (`jobbot run`).

## User preferences & first-run wizard (already written: `preferences.py` — read it)

- `preferences.py` (done, tested): `SETUP_QUESTIONS` (sections Recherche / Automatisation / Candidatures spontanées /
  Relances / Notifications / Comptes), `current_answer(root, q)`, `apply_answer(root, q, value)` (validated, comment-preserving,
  atomic writes to `config/settings.yaml`, `config/profile.yaml`, secrets to `.env`), `get_value/set_value/set_many(root, "dotted.key", v)`
  (`profile.` prefix → profile file, else settings), `is_setup_completed`, `mark_setup_completed`, `effective_mode(settings)` (review during
  `warmup_days` after setup, then `settings.mode`).
- **Mode everywhere = `preferences.effective_mode(ctx.settings)`**, never `ctx.settings.mode` directly (apply service, spontaneous campaign).
- CLI:
  - `jobbot setup` — interactive wizard over `SETUP_QUESTIONS` grouped by section (rich prompts; current value as default; bool → confirm,
    int/float → typed prompt, choice/list → choices shown, secret → hidden input where empty = keep current; each answer applied with
    `apply_answer`, errors shown and re-asked), `--section <name>` to redo one section; ends with `mark_setup_completed`.
  - **First run**: any command other than `setup`, `init`, `doctor`, `--help` checks `is_setup_completed(root)`; if false and stdin is a TTY,
    runs the wizard first (non-TTY, e.g. scheduled task: log a warning and continue with current settings).
  - `jobbot config show [section]` (rich table of every question with current value; secrets shown as ✓/✗ only), `jobbot config set <key> <value>`
    (any dotted key, validated), `jobbot config get <key>`.
- Dashboard: a "Réglages" page rendering `SETUP_QUESTIONS` as a form (per section) that saves with `apply_answer` (secrets: password inputs,
  empty = unchanged), shows validation errors in French.
- New settings (already in `config.py`, with defaults): `setup_completed`, `setup_date`, `warmup_days`, `outreach.*` (`allow_nominative`,
  `allow_employee_search`, `allow_unverified_guesses`, `max_unverified_per_day`, `max_bounce_rate`, `max_attempts_per_company`,
  `preferred_roles`), `reports.*` (`weekly_report`, `weekly_report_day`, `interview_prep`), `LocationPref.country` / `LocationPref.contract_types`
  (e.g. Luxembourg → CDI only), `TargetPrefs.willing_to_relocate` / `relocation_regions`.

## Integration notes (round 1) — what the code actually does on top of the contracts above

All contract signatures above are kept. Additive behaviour and shared conventions every module relies on:

- **Helper modules**: files were split to stay ≤ 400 lines (`matching/_text.py`, `_skills_data.py`, `_soft_data.py`,
  `_doccheck.py`, `filters.py`, `location.py`; `sources/france_travail_mapping.py`, `lba_mapping.py`; `documents/_*.py`;
  `apply/answer_rules*.py`, `coerce.py`, `formscan.py`, `formfill.py`, `lba_recruiters.py`; `mail/messages.py`, `parsing.py`,
  `classify.py`, `imap_client.py`, `matching.py`, `platforms.py`; `outreach/_registry.py`, `_extract.py`, `_mailcheck.py`;
  `cli_*.py`, `review.py`, `dashboard/data.py`). Contract names are re-exported from the contract modules.
- **Search queries**: `build_queries` honours `LocationPref.contract_types` (Luxembourg = CDI only). `collect_jobs` skips
  foreign-location queries for France-only sources (`FOREIGN_LOCATION_SOURCES` = linkedin, hellowork, wttj, free_work) and caps
  scraped boards per run (`SOURCE_QUERY_BUDGET`).
- **Matching**: `location.assess_location(job, profile, contract=None)` ignores areas restricted to other contracts; foreign
  places are never sent to the (France-only) Géoplateforme geocoder. School/CFA detection reads `raw["is_delegated"]` (LBA).
- **Details**: board/alert offers are stored with an empty description and `raw["needs_details"]`. The `prepare` step calls
  `registry.ensure_details` and, when the description changed, **re-scores** the job (`FILTERED_OUT` if it no longer qualifies)
  before generating documents. A SHORTLISTED job that fails dedup (already applied / company contacted < 60 days /
  blacklisted) gets the dedup status right away, and one whose documents fail `PREPARE_MAX_FAILURES` (3) times becomes
  FAILED (`raw["prepare_failures"]`), so the best-first batch can never be blocked.
- **`Repository.upsert_job`** (refresh of a known job): empty values never overwrite stored ones, `raw` is merged (non-null new
  values win), and an alert offer (`raw["from_alert"]`) only adds missing `raw` keys. `dedup_key` follows title/company changes.
- **Company.status**: `new | enriched | no_email | pending_review | approved | rejected | contacted | blacklisted`
  (`pending_review` = documents ready in review mode; `approved`/`rejected` set by `jobbot approve|reject` or the dashboard).
- **Appliers**: dry-run returns `ApplyResult(ok=True, status=SKIPPED, detail="[dry-run] …")` and no `Application` is created.
  An applier may expose `fatal: str | None` (channel must stop for this run: SMTP refused, LBA down); a quota
  SKIPPED or a NEEDS_MANUAL asking for « jobbot login » / reporting LinkedIn's « limite quotidienne » also stops the
  channel and leaves the job untouched for the next run. Only `apply/service.py`
  (and `lba_recruiters`) call `Pacer.record()`; appliers only call `ensure_available`. `EmailApplier` sends `docs.email_body`
  (fallback `letter_text`) with the CV + letter PDFs attached. `FormAnswerer.answer(q, *, allow_llm=True)`.
- **Spontaneous**: `pipeline.spontaneous_applications` first relays applications to « recruteurs LBA » with `hasEmail`
  (`apply/lba_recruiters.run_lba_recruiters`, channel `lba`, one per SIRET ever), then runs the e-mail campaign.
- **Mail**: `MailFatalError(MailError)` = stop the e-mail channel (bad credentials, daily cap, spam block). kv keys:
  `imap_last_uid`, `imap:{all|junk}:{last_uid|uidvalidity}`, `inbox:done:<message-id>`, `gmthread:<thread-id>`,
  `mail:subject:<message-id>` (original subject reused by follow-ups). Other kv keys: `answer:<label>` (form answers),
  `lba:applied:{item|siret|recipient}:…`, `hunter_search:YYYY-MM`, `hunter_verify:YYYY-MM`.
- **Documents**: `CoverLetter.signature: str = ""` (extra field); output folder for spontaneous applications is
  `output/<date>/<slug-company>-candidature-spontanee/`.
- **Doctor**: `jobbot doctor` also checks offline that a Gmail app password is 16 letters (value never shown);
  `--smtp` adds a real login test (no e-mail sent).
- **Core**: `Secrets` treats blank values and `KEY=   # comment` lines as unset; `http.RETRY_STATUSES` includes 419
  (API Apprentissage rate limit); tests marked `live` are skipped unless `-m live` selects them (`tests/conftest.py`).

## v2 — Luxembourg, per-location contracts, relocation, salary floor

- `sources/countries.py` (new helper): `location_country(loc) -> str` (ISO code of a `LocationPref`, "FR" for
  None; a city named "Luxembourg" is LU even in older profiles without `country`), `is_foreign(loc)`,
  `country_name(code)` (French name), `COUNTRY_NAMES`, `INDEED_COUNTRIES` (ISO → JobSpy `country_indeed`).
- `registry.build_queries`: `LocationPref.contract_types` **replaces** the target contracts for that location
  (Luxembourg → CDI only); a contract listed there but absent from `target.contract_types` is searched at that
  location only (never in the national remote query). `FOREIGN_LOCATION_SOURCES` now also contains
  `france_travail` and `indeed` (LBA and APEC stay France-only).
- Luxembourg track per source: France Travail `paysContinent` (code resolved once per source instance from
  `/referentiel/pays` by libellé, `pick_country`; unknown country / referential down → the query is skipped with a
  warning, never searched in France); JobSpy searches a foreign place on that country's Indeed only
  (`country_indeed="luxembourg"`, place + radius); Free-Work `<iso>~~~` key (`lu~~~`, no implicit LU pass from Metz
  any more); LinkedIn `geoId` of the country + `location="Luxembourg"`; WTTJ `aroundLatLng` (without coordinates:
  `offices.country_code:LU` filter).
- `matching/target_rules.py` (new, used by `match_job`; `match.CONTRACT_LABELS` re-exported from it):
  `offer_country(job)` (raw `office_country`, country words / ", LU" suffix in `location`, France Travail
  "99 - <pays>" → "ZZ", French department → "FR"); `contract_reason` (contract wanted nowhere; offer abroad with no
  search area in that country; country whose areas refuse the contract: « Écartée : offre en alternance au
  Luxembourg — contrats recherchés dans ce pays : CDI »; contract accepted only abroad); `location_fit`
  (`assess_location` + foreign place without coordinates in an accepted country, area contract restriction
  « offre en alternance dans la zone de Nancy — contrats recherchés dans cette zone : CDI », then relocation);
  `relocation_fit` (`willing_to_relocate`: French offers only — department or "France" in the location —, inside
  `relocation_regions` = current regions, pre-2016 regions, short names, department codes/names; empty list =
  anywhere in France; score 0.6); `annual_salary(text)` / `salary_reason` (`min_salary_eur`, gross per year; FR/EN
  formats, k€, monthly × "sur N mois", hourly × 1607, daily × 218; non-euro or unparsable → never filters; top of the
  range compared; never for an alternance or an unknown contract).

## v2 — Interview prep sheets & weekly report (`reports/`)

- `reports/interview.py`: `build_interview_prep(ctx, application, *, message: InboundMessage | None = None) -> Path`
  → `output/entretiens/<YYYY-MM-DD>-<slug-entreprise>.md` (French Markdown): logistics, company summary, what the role
  needs (offer skills the profile backs / « à réviser » = absent from the profile, never claimed), STAR talking points,
  likely questions (technique, alternance, comportement/motivation) with answer leads, questions to ask, check-list.
  Quality LLM (`complete_json`, `_prep_llm.PrepDraft`) section by section, template fallback for every missing or
  rejected section; an LLM STAR point is kept only when it names a real profile experience/project and
  `documents._truth.unsupported_claims` finds nothing; answers may name the offer's skills (honest gaps) but no other
  unbacked skill/number/degree. Logistics are **rules only** (`_logistics.extract_logistics`: date, heure, visio
  link, adresse, téléphone du recruteur — the candidate's own phone excluded), never guessed. `notify()`; **no**
  `ApplicationEvent` (the inbox already logs the interview). Also `prep_on_interview(ctx, application, message=None)
  -> Path | None` (inbox hook: honours `settings.reports.interview_prep`, never raises), `generate_prep(ctx,
  application_id) -> str`, `prep_path(ctx, application_id) -> Path | None`, `interview_rows(ctx) -> list[dict]`.
  kv: `reports:prep:<app id>` (JSON path/at/logistics), `reports:interview_mail:<app id>` (invitation text + sender,
  reused when the sheet is regenerated without the e-mail).
- `reports/weekly.py`: `class WeeklyReport(frozen)` (`week` "2026-W39", `start`, `end`, `path`, `offers`,
  `offers_per_source`, `applications`, `applications_per_channel`, `responses`, `interviews`, `pending_manual`,
  `upcoming_followups`, `emailed`, `details`; `summary()`); `build_weekly_report(ctx, week_end: date | datetime | None
  = None, *, send_email: bool | None = None, mailer=None) -> WeeklyReport` → `output/rapports/semaine-YYYY-WW.md`
  (ISO week). Window = Monday 00:00 (Paris) of `week_end`'s week → end of `week_end`, capped at now (default: today =
  current week so far; future week → ValueError). Sections: en bref, offers per source (found/kept), applications per
  channel and response rate per channel and per source (cumulative, bounces excluded), answers of the week, manual
  actions (NEEDS_MANUAL, to validate, manual follow-ups), follow-ups due in 7 days, « Justificatif de recherche
  d'emploi (France Travail) » table of the week's applications. `notify()` summary; optional e-mail to
  `profile.identity.email` (`OutgoingEmail(kind="report")`) when `send_email` (None → `settings.reports.weekly_email`
  if that setting exists, else off). Schedule: `weekly_report_due(ctx, today=None) -> date | None`
  (`reports.weekly_report_day`; Monday = previous full week, other days = current week to that day; missed run caught
  up for 2 days), `scheduled_weekly_report(ctx, *, mailer_factory=None, today=None) -> WeeklyReport | None` (once
  per week, kv `reports:weekly:done:<YYYY-Www>`); helpers `week_bounds`, `week_label`, `report_path_name`,
  `previous_week_end`, `parse_day`.
- Hooks: `mail/inbox.py` — a reply classified `interview` and a platform notice with status INTERVIEW call
  `prep_on_interview` (update line gets « — fiche d'entretien : <fichier> »); `mail/mailer.py` — `OutgoingEmail.kind`
  also accepts `"report"`; `pipeline.py` — `DailyReport.weekly: WeeklyReport | str | None` is **not** a step (so
  `STEPS`, `summary()` and the CLI step table are unchanged): after the daily summary notification, `run_daily`
  writes the due weekly report (error → French string, never fails the run); `cli.py` — `jobbot prep
  <application_id>`, `jobbot report [--precedente] [--date AAAA-MM-JJ] [--email]`; dashboard page « Entretiens »
  (sheets rendered, download, « Générer / Régénérer la fiche »).
## Named contacts for spontaneous applications (v2 — outreach)

Goal: reach HR / recruiters, tech managers and (small companies) leaders by name, not only `contact@`. Contracts
above are kept; additions:

- `outreach/people.py` (new): `class Person(frozen)` (`name, first, last, role, category: hr|tech|leader|other,
  source: registry|website|hunter|search, source_url, email, verified, confidence`);
  `find_people(company, *, client=None, secrets=None, **options) -> list[Person]` (best first) and
  `find_people_detailed(..., domain=None, pages=None, preferred_roles=None, use_hunter=True, use_search=True) ->
  PeopleSearch(people, contacts, pattern, invalid, sources)`. Sources: registry dirigeants (`raw["leaders_detail"]`,
  else `recherche-entreprises /search?q=<siren>`; natural persons, never "Commissaire aux comptes"), team/about pages
  (cards, "Nom – Poste" lines, JSON-LD `Person` with `jobTitle`; never home-page testimonials), Hunter
  `domain-search` (people + the domain `pattern`), public search-result **titles** "Prénom Nom - Poste - Entreprise |
  LinkedIn" via Brave (key) else DuckDuckGo HTML (shares `domains._DDG`: ≥ 25 s apart, stopped for the run on a
  bot-flag); profile pages are never fetched. `rank_people(people, preferred_roles, *, small_company)` follows
  `settings.outreach.preferred_roles` (aliases: rh/drh/ressources humaines…, cto/directeur technique…), leaders of
  companies ≥ 50 employees or ETI/GE last; `is_small_company(company)`.
- Address patterns (`outreach/_patterns.py`): Hunter syntax (`{first}.{last}`, `{f}{last}`…); learned from Hunter,
  else from named addresses (site/JSON-LD/team cards) matched to known people, stored in **kv
  `email_pattern:<mail domain>`**. `guess_patterns(first, last, domain, *, pattern=None, repo=None)` puts the learned
  pattern first. The mail domain can differ from the website (`trustteam.eu` → `@trustteam.fr`).
- Enrichment (`emails.enrich_company`, via `outreach/_nominative.py`): named site addresses get `person_name` + kind;
  top HR/tech people (and the leader of a small company) without an address get guesses (learned pattern → 1 guess,
  conf 0.35; priors → 2 guesses, conf 0.3/0.2; never an address Hunter reports invalid). Hunter verifies when a
  key/budget exists and no recruitment address was found (`valid` → verified 0.4, `claimed` → nobody). Employee
  search (team pages, search engine, Hunter people) only when `allow_employee_search` and no recruitment address.
  `ContactEmail.kind` gains **`tech`**; `raw["email_sources"][email] = "pattern:<person source>"` for guesses;
  company `raw` gains `people` (name/role/category/source, ≤ 10), `email_pattern`, `mail_domain`. Hunter budget
  helpers moved to `_mailcheck.take_hunter_credit/hunter_month_key` (same kv keys). `SiteCrawl` gains
  `meta_description`, `site_text`, `page_html` (not serialised). **`Company.description`** = 2-3 French sentences
  from the home/about pages (LLM fast; fallback meta description) when the company has a usable address.
  `is_sendable(contact, *, allow_unverified=False)`; status `enriched` = at least one address `selection.usable`.
- `outreach/selection.py` (new): order recruitment@ > named HR > named tech manager > leader (small companies) >
  contact@ > other named people (> leaders of larger companies); verified before guessed. `allow_nominative` off →
  role addresses only. Unverified guesses need `allow_unverified_guesses`, are capped by `max_unverified_per_day`
  (**action_log `spontaneous/unverified_guess`**, written only for real sends) and paused while the spontaneous
  bounce rate over 30 days exceeds `max_bounce_rate` (report line « Info : adresses devinées en pause… »). A guess
  blocked by the cap/pause falls back to the next verified address, else the company waits (status unchanged).
- `outreach/relevance.py` (new): **one relevance gate for every spontaneous target** (registry + « recruteurs LBA »),
  `is_relevant_employer(company, profile) -> (bool, French reason)`. Relevant: NAF in `target.naf_codes` or digital
  (62, 63.1, 58.2, 61, 72.19Z; LBA site hits have only the NAF *label*), ≥ 250 employees or ETI/GE (never for
  field sectors: farming, extraction, construction 41-43, catering, temp work, cleaning), 70.22Z/71.12B under Syntec
  (or 70.22Z ≥ 50), a developer job in LBA `rome_labels`/`keywords`, an IT name (or name/description when the
  sector is unknown). Applied at discovery (not stored, `DiscoverReport.irrelevant`), enrichment, the LBA relay (stored
  data, then the recruiter detail's `nafs`/`company.size`) and the e-mail campaign; waiting companies (and the
  `pending_review` queue) get **status `irrelevant`** + `raw.irrelevant_reason`; counted in `CampaignReport.irrelevant`
  and the daily summary (« N entreprise(s) hors cible écartée(s) »).
- Bounce retry (`spontaneous`): candidates also include **`contacted` companies whose latest application is
  BOUNCED** and whose spontaneous attempts < `max_attempts_per_company` (60-day dedup otherwise unchanged). The next
  best address is used (bounced/blacklisted/already-used ones never), documents are reused, the new Application gets
  events `applied` + **`retry_after_bounce`**; review mode queues the retry as `pending_review`.
- Greeting set at send time: "Bonjour Prénom Nom," (display case) for a person's address, else "Madame, Monsieur,"
  (replaces the documents' greeting line). Source line for guesses: « J'ai déduit votre adresse de votre nom, relevé
  sur … » + mandatory opt-out (`_compose.ensure_opt_out`, re-exported by `spontaneous`).

## Integration notes (v2 round)

- **Settings**: `ReportSettings.weekly_email: bool = False` (asked by `reports.weekly.wants_email`, question « Recevoir
  aussi le rapport hebdomadaire par e-mail ? » in the Notifications section); `reports.weekly_report_day` is validated
  0–6; `outreach.max_unverified_per_day ≥ 0`, `outreach.max_bounce_rate` in [0, 1], `outreach.max_attempts_per_company
  ≥ 1` (an out-of-range `jobbot config set` is refused, nothing written).
- **Truthfulness gate**: in auto mode nothing whose latest CV has `truth_warnings` is sent without the user:
  `apply.service` puts such a job in PENDING_REVIEW (detail « CV à vérifier (…) », helper `truth_warnings(ctx, job)`),
  the spontaneous e-mail campaign and the LBA relay put the company in `pending_review`. APPROVED jobs / `approved`
  companies / explicit `jobbot apply -j` are the user's decision and go through.
- **Labels**: `cli_support.EVENT_FR` (incl. `retry_after_bounce`, `manual_followup`) and `CONTACT_KIND_FR` (incl.
  `tech`), used by the dashboard timeline and companies table (`dashboard.data.company_row`: chosen address = the
  review contact, else `selection.sort_contacts` order; kind, person, guessed flag, people count, description).
- **CLI**: `jobbot run` prints the weekly report line (summary + file) when `DailyReport.weekly` is set.

## v3 — Offers relayed by the school (Google Classroom, `classroom/`)

The school posts apprenticeship offers in a Google Classroom course (private URL(s) in `.env`
`CLASSROOM_COURSE_URLS`, comma-separated). A frequent watcher applies at once to the adapted ones.

- **Config**: `Secrets.classroom_course_urls`; `Settings.classroom: ClassroomSettings` (`enabled`, `backend:
  browser|api`, `poll_minutes` 1–1439, `active_hours` "HH:MM-HH:MM" (`config.parse_active_hours`), `auto_apply`,
  `min_score` (None → `thresholds.auto_apply_min_score`), `headless`, `browser_profile`
  (`data/browser-profile-classroom`, never the LinkedIn/HelloWork profile), `max_posts`, `school_bonus`). Wizard
  section « Offres de l'école (Classroom) » (`enabled`, `auto_apply`, `poll_minutes`, secret `CLASSROOM_COURSE_URLS`
  validated by its `Question.setter` — `apply_answer` now honours a setter on secrets; `config set` too).
- `classroom/models.py`: `Course`, `Attachment`, `ClassroomPost`, `CourseFetch`, `LinkContent`, `Wants`; ids from
  URLs (`/c/<b64>`, `/p|sa|a|m/<b64>` → `p-<numeric>`…, `decode_id`/`encode_id`), `parse_course_urls` → (courses,
  invalid count), `stream_fingerprint` (dates / comment counts ignored), `text_hash`.
- `classroom/dom.py` (pure + JS): posts found by permalink (card = largest ancestor holding no other post),
  heuristic split + `h-<hash>` ids without permalinks, `login_wall`, `no_access`, `parse_post_text` (author / date /
  body, nav tabs, icon ligatures and comments dropped), `parse_date_text`, `classify_links` (Google redirects
  unwrapped; Docs/Slides/Sheets/Drive → `Attachment`; forms and other sites → links), `build_post`.
- `classroom/browser.py` (default backend): `open_classroom_browser(ctx, *, headless)` is the **only** place that
  creates a Classroom browser context (watcher and `jobbot login classroom`; `apply.browser.open_browser` gained
  `profile_dir` / `launch_options`, `GOOGLE_LAUNCH` drops the automation flags so the human can sign in) — extend it
  for a future session import (`storage_state`). `fetch_course` (never raises): stream → scroll → open wanted posts
  (« Voir plus » clicked) → Google files via `ContextDownloader` (`context.request`, shares cookies; Docs
  `export?format=txt`, Drive `uc?export=download`). `BrowserBackend`: headless first, one headful retry on a login
  wall only when `display_available()` (Linux: `DISPLAY`/`WAYLAND_DISPLAY`); a failed retry keeps `login_required`.
  No OS-specific code in `classroom/` (runs on Windows and on a headless Linux server).
- `classroom/api.py` (optional, `pip install -e .[classroom]`): `courses.announcements.list` +
  `courses.courseWorkMaterials.list` (`orderBy=updateTime desc`), Drive API export/download; OAuth desktop flow
  (`google-auth-oauthlib` imported lazily, French `ClassroomAuthError`), client `config/google_oauth_client.json`,
  token `data/classroom_token.json` (both gitignored); `authorize(ctx)` behind `jobbot login classroom`.
- `classroom/content.py`: `Downloader` protocol, bytes → text (PDF via pypdf, DOCX, HTML, text; 8 MB cap);
  `link_contents` — board URLs (`alerts.match_offer_url`) read through their source with the new
  `registry.fetch_details_only(ctx, job)` (never stored), other pages via httpx + `html_to_text`.
- `classroom/extract.py`: `PostExtraction{is_offer_post, offers[ExtractedOffer]}` via `llm.complete_json(fast=True)`,
  then `ground()` drops e-mails / links absent from the texts and blanks unknown companies; fallback on `LLMError`
  (offer wording + regex e-mails/URLs, first line as title).
- `classroom/mapping.py`: `to_job` → `JobOffer(source="classroom", external_id=f"{post_id}-{n}")`, `raw` = `post_id,
  course_id, post_url, post_text, apply_instructions, deadline, target_level, relayed_by_school, apply_via_school,
  school_bonus, board…`. Route: EMAIL (valid address, school's included) > board in-site (LinkedIn Easy Apply /
  HelloWork / LBA: `raw["board"] = {source, external_id, url, raw}`, title/company from the board for dedup) >
  EXTERNAL. `apply/board.py: board_view(job)` gives appliers the board's own view (called in `apply.service`).
- `matching/relayed.py` (used by `match_job`): relayed offers skip `school_offer_reason`, get `raw["school_bonus"]`
  (« Offre relayée par l'école (+5 points) »), are filtered on a target level well below the programme (RNCP level
  of `profile.alternance`, BTS/Bac+2, CAP, Bac for a Bac+5), a passed deadline or an expired linked board offer.
  `match.score_one(ctx, job)` scores one job like `score_new_jobs`.
- Documents: `_letter_rules.relay_sentence` / `with_relay` — letter paragraph 1 and application e-mail start with
  « J'ai pris connaissance de votre offre par l'intermédiaire de <profile.alternance.school> » (code-inserted).
- `classroom/process.py`: `review_reason` (auto_apply off, `mode: review`, score < threshold, truth warnings →
  PENDING_REVIEW; the warm-up is ignored), `process_offer` (score_one → dedup → `prepare_documents` → `submit`),
  `submit` (APPROVED « Validée automatiquement » then `apply_to_jobs(job_ids=[id])`: single job, no long pause;
  exhausted quota → `deferred`), `retry_approved` (≤ `MAX_ATTEMPTS`), `notify_outcome` (one notification per offer).
- `classroom/state.py`: kv `classroom:posts:<course>` (post → stream fingerprint / text hash / failures, 500 kept),
  `classroom:last_check` (doctor), `classroom:login_notified_at` (alert ≤ every 6 h), lock `data/classroom.lock`,
  `in_active_hours`.
- `classroom/watch.py`: `ClassroomReport`, `run_classroom(ctx, *, respect_hours=True, backend=None)` (disabled /
  unconfigured / outside hours / busy → returns at once; retries, fetch, per-post extraction, per-offer processing;
  a post is processed again only when its text changed — then offers filtered before are re-scored; failing posts
  retried 3 times). `classroom/preview.py`: `preview_classroom` (read-only, `jobbot classroom test`).
- Pipeline: step `classroom` first in `STEPS` (`run_classroom(respect_hours=False)`), `DailyReport.classroom`.
- CLI (`cli_classroom.py`): `jobbot classroom watch [--once] [--interval N]`, `jobbot classroom test [-n N]`,
  `jobbot login classroom` (browser, or OAuth when `backend: api`). `jobbot schedule install|remove|show` also
  manages the Windows task `jobbot-classroom` (`schtasks /SC MINUTE /MO <poll_minutes>`, `pythonw -m jobbot.cli
  --root … classroom watch --once`). `jobbot doctor`: course link ✓/✗ (never shown) + last check.

## v4 — Linux server (headless) & one-command deployment

- **Settings override**: `config.settings_data(root, include_local=True)` deep-merges the gitignored
  `config/settings.local.yaml` over `config/settings.yaml` (`config.deep_merge`, pure); `load_settings(root,
  include_local=True)`. `preferences` reads the effective value; `set_*` write `settings.yaml` unless the key is
  already set in the local file (`config_files.settings_target`, it is then updated there). YAML round-trip, `.env`
  helpers and targeting moved to `config_files.py` (re-exported by `preferences`); `OutreachSettings`,
  `ReportSettings`, `ClassroomSettings`, `parse_active_hours`, `Frozen` moved to `config_sections.py` (re-exported by
  `config`). The `settings` test fixture ignores a local file.
- **New settings / secrets**: `notify_email`, `notify_email_max_per_day` (10), `notify_ntfy_topic` (validated topic),
  `notify_telegram`, `notify_telegram_all`, `search_engine_url` (SearXNG base URL); `Secrets.claude_code_oauth_token`
  (passed as `CLAUDE_CODE_OAUTH_TOKEN` to the `claude -p` subprocess by `ClaudeCLIClient(settings, oauth_token)`),
  `telegram_bot_token`, `telegram_chat_id`. Wizard « Notifications »: `notify_email`, `notify_ntfy_topic`,
  `notify_telegram`.
- **Notifications** (`notify.notify(ctx, title, message, *, level="info")`, levels `info | important | urgent`):
  log + toast + Jarvis as before, then `notify_remote`: ntfy (`POST https://ntfy.sh/<topic>`, title/priority/tags
  as query params), Telegram (`sendMessage`, important/urgent unless `notify_telegram_all`; token never logged, httpx
  request logs redacted), e-mail to `profile.identity.email` (important/urgent, action_log `notify/email`, cap per
  day, `OutgoingEmail(kind="notification")`, dry-run → `.eml`). Each channel isolated, none raises; ntfy/Telegram
  skipped in dry-run. Levels used: inbox interview → urgent, positive/other reply → important; Classroom applied /
  to validate / manual / failed → important; Classroom login lost → urgent; prep sheet → important; pipeline
  `_alert_expired_session` (apply details containing « jobbot login ») → urgent, once a day (kv
  `notify:login_alert:<date>`).
- **Sessions** (`sessions.py`, CLI `cli_session.py`: `jobbot session export [site|all]`, `jobbot session import
  [path]`): Playwright `storage_state` reduced to the site's domains (`SITE_DOMAINS`) → `data/sessions/<site>.json`
  (0600). `apply.browser.open_browser(..., import_sessions=True)` imports a file newer than kv
  `session_imported:<site>` (`context.add_cookies`, that site's cookies only) for the sites of the opened profile
  (`sites_for_profile`: main profile → linkedin/hellowork/wttj, Classroom profile → classroom), so
  `classroom.browser.open_classroom_browser(ctx, headless=, import_session=True)` (still the single Classroom
  factory) benefits too. Export marks the kv so the PC never re-imports its own export.
- **Scheduling on Linux** (`cron.py`): `jobbot schedule install|remove|show` manage the user crontab lines marked
  `# jobbot:run|soir|classroom` (other lines untouched, rewritten only when changed); line = `cd <root> && PATH=…
  flock -n /tmp/jobbot-<name>.lock [xvfb-run -a] <root>/.venv/bin/jobbot <cmd> >> <root>/data/logs/cron.log 2>&1`.
  `cron.scheduler_kind()` picks Windows (unchanged `install_task.ps1` + schtasks) or cron.
- **Dashboard**: `jobbot dashboard --host 127.0.0.1 --port 8501` (`cli_tools.dashboard_host` refuses 0.0.0.0 / ::),
  `--server.headless true` on Linux.
- **Doctor on Linux** (`server_checks.py`): xvfb-run, crontab lines, `data/sessions` freshness (30 days), presence of
  `config/settings.local.yaml`, time zone.
- **SearXNG** (`outreach/_searxng.py`): `searxng_search(client, base_url, query)` (JSON, 1-2 s politeness delay, never
  raises); `domains.find_domain_verified(..., search_url=)` tries it before Brave / DuckDuckGo;
  `people.find_people_detailed(..., search_url=)` takes result titles from it first. Geocoding moved to
  `outreach/_geocode.py` (re-exported by `domains`).
- **Deployment kit** (`scripts/deploy_server.py` → `scripts/deploy/`, stdlib + PyYAML): `config.py` (.env keys,
  `filter_env`, `config/deploy.local.yaml`), `runner.py` (every command through an injectable executor: `probe` =
  read-only, also in dry-run; `run` = printed only in dry-run; ssh `BatchMode=yes`, `ConnectTimeout=15`, remote
  scripts base64-encoded as `bash -c` argument; exit 255 → French SSH help), `templates.py` (remote scripts, hook,
  systemd unit, server settings), `steps_setup.py` / `steps_app.py` (steps 1-11), `replace_plan.py` (pure discovery
  parsing / STOP-KEEP plan / backup and purge lists / redaction), `replace_scripts.py` + `replace_steps.py` (backup →
  stop → crontab → port; purge after an existing backup and a fresh one; restore from the manifest), `cli.py` (step
  selection, exit codes 0/2/3). The `.env` reaches the server as `.env.pc` merged there (server-only keys such as
  Telegram kept, no secret travels back to the PC). Trust boundary: root never executes or blindly reads / writes
  what the project account controls (its discovery output is limited to units / pm2 lines in the « user » scope,
  its files are handled through `runuser`, Chromium's apt packages are validated names from Playwright's
  `--dry-run`); crontab lines are filtered on the server by an awk program (`CRON_AWK`) and only masked previews
  reach the PC; the purge list excludes any folder holding a kept service. Tests: `tests/test_deploy_kit.py`,
  `tests/test_deploy_replace.py` with `tests/fixtures/deploy/fakes.py` (fake executor, placeholders only).
- `scripts/dashboard_serveur.ps1`: SSH tunnel `8501 → 127.0.0.1:8501` of `PROJECT_SSH_HOST_ALIAS`, opens the browser.

## v5 — Matching precision (region-level places, thin descriptions)

- `matching/regions.py` (new; `REGIONS`, `OLD_REGIONS`, `region_of`, `key` moved here from `target_rules`, still
  re-exported there): `vague_place(location) -> VaguePlace | None` (label as written, departments, city centre) for a
  whole region (13 current regions, pre-2016 regions, « Région parisienne », « Paris et périphérie », « Région X »)
  or a big-city area (« Lyon et périphérie », « Grand Lyon », « Toulouse Métropole », « Région lyonnaise »…,
  `METRO_DEPARTMENTS` + `geo.KNOWN_PLACES`); `place_label` (location without its trailing ", France");
  `is_country_only` (« France », « France entière », empty).
- `matching/location.py`: a big-city area gets the city's coordinates (`job_coordinates`); a whole region with no own
  coordinates / postal code (`job_region`) is kept with `LocationFit.penalty` = 10 (« Localisation : lieu imprécis
  (Grand Est), -10 points ») when it contains a French search area's department (or an area's department is
  unknown), else filtered. Every out-of-zone filter reads « Écartée : trop loin : <lieu> (hors de tes zones) » (+
  « — N km de <ville>, rayon R km » / « — département NN »). Country-only place, not remote → penalty 15
  (« Localisation : lieu non précisé (France), -15 points »). For a vague place, `JobOffer.remote=True` counts as
  full remote only from `FULL_REMOTE_FLAG_SOURCES` (France Travail / LBA / Indeed also flag hybrid work).
  `target_rules`: foreign regions in `_COUNTRY_WORDS` (Wallonie, Bruxelles, Saarland, Bade-Wurtemberg…);
  `offer_country` = FR for a French region / area; relocation needs the whole region in `relocation_regions`.
  `match._locate` never geocodes a region.
- `matching/match.py`: description < `THIN_DESCRIPTION_CHARS` (400 = `registry.DETAILS_MIN_CHARS`) → skills
  component × min(1, skills found / 3) and « Annonce incomplète, descriptif court : score provisoire »;
  `match_jobs(jobs, profile)`; `MatchReport.completed`.
- `registry.complete_new_offers(ctx)`: NEW offers with a short description from `DETAIL_SOURCES` (every source but
  Indeed) read through `ensure_details`, best provisional score first, at most
  `settings.search.max_detail_fetches_per_run` (60) per run; `pipeline.score_new_offers` (step « score » and
  `jobbot score`) runs it before `score_new_jobs` (a failure never blocks the scoring). `prepare` scores every
  SHORTLISTED offer again after `ensure_details` and drops it (FILTERED_OUT) below `thresholds.min_match_score`.
  Tests: `tests/test_matching_regions.py`, `tests/test_matching_details.py`.
