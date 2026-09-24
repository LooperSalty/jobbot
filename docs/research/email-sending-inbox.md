# Sending & reading e-mail from a personal Gmail account (jobbot, 2026)

> Research date: **2026-09-23**. Account: the candidate's own Gmail address, written `ton.adresse@gmail.com` below (consumer Gmail, not Workspace).
> Live probes ran from the user's machine (Windows 11, Python 3.12.10) with a **fake** account
> (`jobbot.probe.nonexistent.q7x3k9@gmail.com`). No e-mail was sent and nobody logged in to the real account.
> Tags: **LIVE-VERIFIED** = observed today by a probe; **DOC-SOURCED** = read today in the linked doc;
> **UNCERTAIN** = inference, community report, or not testable without credentials.

---

## 1. TL;DR + recommendation for jobbot

**Transport. Use SMTP + IMAP with a Google App Password. It works today and needs only the stdlib.**

- `smtp.gmail.com:587` with STARTTLS (or `:465` with implicit TLS) still offers `AUTH LOGIN PLAIN XOAUTH2 ...` (LIVE-VERIFIED).
  `imap.gmail.com:993` still offers `AUTH=PLAIN` and the Gmail extensions `X-GM-EXT-1` (LIVE-VERIFIED).
- Your normal Google password no longer works over IMAP, SMTP or POP. You need a **16-character App Password**, which requires **2-Step Verification**. Create it at `https://myaccount.google.com/apppasswords`.
  - Google revokes all App Passwords when you change your Google password.
  - They are unavailable with Advanced Protection, with security-key-only 2SV, and on work or school accounts.
- Google labels App Passwords "not recommended". I found **no official end date for consumer accounts** as of 2026-09-23. The March 14, 2025 cutoff removed *plain-password* access. App Passwords were explicitly kept.
- It needs no Google Cloud project and no expiring token, and it already matches `.env.example` (`SMTP_USER/SMTP_PASSWORD/SMTP_HOST=smtp.gmail.com/SMTP_PORT=587/IMAP_*`).
- **Keep a transport interface** (`Mailer` / `inbox`) so a `GmailApiTransport` can be added later.
  - That fallback would use OAuth desktop flow and `google-api-python-client` 2.200.0.
  - Its big catch: if the OAuth consent screen is left in **"Testing"**, refresh tokens **expire after 7 days**.

**Threading (so a relance lands in the same thread for the recipient and in your Gmail):**
1. Generate the Message-ID yourself with `email.utils.make_msgid(domain="gmail.com")`. **Always pass `domain`.** Without it Python uses the Windows hostname (`...@DESKTOP-XXXXXXX`, LIVE-VERIFIED).
2. Store the Message-ID in SQLite **before** sending.
3. For a relance, set:
   - `Subject: Re: <original subject>` (normalise, so it never becomes `Re: Re:`),
   - `In-Reply-To: <last message id>`,
   - `References: <whole chain, oldest first>`.
4. After sending, find your own copy in `[Gmail]/All Mail` with `X-GM-RAW "rfc822msgid:<id>"`. Store its `X-GM-THRID` (thread id) and `X-GM-MSGID`. This also confirms that Gmail kept your Message-ID.

**Reading replies.** Poll every 15–30 min.
- Scan the **`\All` and `\Junk`** special-use folders incrementally by UID. Recruiter replies do land in Spam.
- Match incoming mail to an application in this order:
  1. `In-Reply-To`/`References` contains one of your Message-IDs,
  2. same `X-GM-THRID`,
  3. fallback: sender domain equals the company domain.
- Classify in this order: **bounce** (multipart/report or mailer-daemon) → **auto-reply** (`Auto-Submitted` ≠ `no`, `X-Autoreply`, `Return-Path: <>`, subject "Réponse automatique…") → **human**.
- On any human reply, cancel pending relances and notify the user.
- On a hard bounce or an opt-out, add the address to the blacklist.

**Deliverability (personal @gmail.com, low volume):**
- Volume: **≤ 20 new cold e-mails/day** for the first 2 weeks, then ≤ 30–40. **≤ 10 relances/day.** Google's hard cap is 500/day.
- Timing: Mon–Fri 08:30–11:30 and 14:00–17:00 Europe/Paris, with a **random 4–12 min gap** between sends. One recipient per message, reconnect SMTP for each send.
- Content:
  - Plain text first, with an optional minimal HTML alternative.
  - **No tracking pixel** (a CNIL recommendation of 2026 requires consent for tracking pixels).
  - No link shorteners, at most 2–3 links (LinkedIn, GitHub, portfolio).
  - One PDF CV of **≤ 1 MB**.
- Every message is personalised.
- Include a one-line opt-out plus where you found the address.

**Legal (France/EU):**
- A candidature spontanée from a private person is most likely **not** "prospection directe" under CPCE L34-5, which is about promoting goods or services (UNCERTAIN, no CNIL text on it).
- **GDPR still applies to the recruiters' personal data you scrape.** The "purely personal activity" exemption is doubtful for job hunting.
- So follow the CNIL **B2B** rules anyway:
  - the message relates to the recipient's job,
  - your identity is clear,
  - opting out is simple in every message,
  - you tell them where you got the address,
  - you prefer generic addresses (`recrutement@`, `rh@`, `jobs@`), which the CNIL says fall outside those rules.
- **Never harvest contact details from LinkedIn.** The CNIL fined KASPR €240,000 in Dec 2024 for exactly that.
- Retention: CNIL's maximum for prospect data is **3 years** after collection or the prospect's last contact. The opposition list must be kept **at least 3 years**.
  - For jobbot: purge contacts you never used after 6 months, and contacted-no-reply contacts after 12 months.
  - Keep a hashed suppression list for ≥ 3 years.

**Relances (French etiquette):**
- Job posting: J+7 working days. Spontaneous application: J+10 working days. Second and last relance: J+10 working days after the first.
- **Max 2 relances. Never after any human reply.**
- Send Tue–Thu, 9–11h preferred. Avoid Monday morning, Friday afternoon, and Moselle public holidays (incl. Good Friday and 26 Dec).
- Keep it ≤ 10 lines. Always add one new piece of information and ask one question.
- Given the October 2026 school start, shorten to J+5 / J+7 working days until mid-October.

**Suggested safety default.** Start in **review mode** for the first ~20 e-mails:
- either `--dry-run` writes `.eml` files to `output/outbox/`,
- or IMAP `APPEND` puts the message into `[Gmail]/Drafts`, so you can check it on your phone and send it yourself.

Switch to auto-send once the templates are validated.

---

## 2. Endpoints / URLs & auth

### 2.1 SMTP (sending)

| Item | Value |
|---|---|
| Host | `smtp.gmail.com` |
| Port 587 | plain → `STARTTLS` → `EHLO` again → `AUTH` (**recommended**, matches `.env.example`) |
| Port 465 | implicit TLS (`smtplib.SMTP_SSL`) |
| Auth mechanisms (after TLS) | `AUTH LOGIN PLAIN XOAUTH2 PLAIN-CLIENTTOKEN OAUTHBEARER XOAUTH` (LIVE) |
| Username | full address `ton.adresse@gmail.com` |
| Password | the 16-letter App Password. Google displays it as `abcd efgh ijkl mnop`. Strip the spaces before use. |
| Max message size advertised | `SIZE 35882577` bytes (~34.2 MiB, LIVE). The **25 MB attachment rule** is stricter. Base64 adds about 33%. |
| Extensions | `8BITMIME`, `SMTPUTF8`, `PIPELINING`, `CHUNKING`, `ENHANCEDSTATUSCODES` (LIVE) |
| OAuth variant | `AUTH XOAUTH2` with `base64("user=<addr>\x01auth=Bearer <access_token>\x01\x01")`. The token needs scope `https://mail.google.com/` (a *restricted* scope). |

Gmail automatically saves a copy of every message sent through smtp.gmail.com in **Sent Mail**. **Do not also IMAP-APPEND it to Sent.** It would upload twice and could trigger errors or duplicates.

SMTP replies jobbot must handle (from Google's SMTP error reference; 535 LIVE):

| Code | Text (abridged) | jobbot action |
|---|---|---|
| `535 5.7.8` | `Username and Password not accepted. ... https://support.google.com/mail/?p=BadCredentials` (LIVE) | Stop the whole channel and notify: App Password is wrong or was revoked (e.g. after a password change). |
| `534 5.7.9` | `Application-specific password required` | Someone put the normal password in `.env`. Tell the user to create an App Password. |
| `534 5.7.14` | `Please log in via your web browser...` | Security challenge. Notify, back off 24 h. |
| `530 5.7.0` | `Must issue a STARTTLS command first` | Bug: STARTTLS was skipped. |
| `454 4.7.0` | `Too many login attempts, please try again later` | Back off ≥ 1 h. |
| `421 4.7.0` | `Try again later, closing connection` | Retry later with exponential backoff (max 3 attempts). |
| `550 5.4.5` | `Daily user sending limit exceeded` | Freeze all sending for 24 h. Mark queue items `deferred`. |
| `552 5.3.4` | `Your message exceeded Google's message size limits` | Shrink the attachment. |
| `550 5.7.1` | `...likely unsolicited mail` / policy | Stop the campaign and notify: reputation problem. |
| `550 5.1.1` / `553 5.1.2` | recipient does not exist / domain not found (usually arrives later as a **bounce**, not synchronously) | Mark the contact invalid and blacklist it. |

### 2.2 IMAP (reading)

| Item | Value |
|---|---|
| Host / port | `imap.gmail.com:993`, TLS from the first byte (`imaplib.IMAP4_SSL`) |
| Greeting | `* OK Gimap ready for requests from <ip> <id>` (LIVE) |
| Pre-auth CAPABILITY | `IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 XYZZY SASL-IR AUTH=XOAUTH2 AUTH=PLAIN AUTH=PLAIN-CLIENTTOKEN AUTH=OAUTHBEARER` (LIVE) |
| Login | `LOGIN user app_password`. The bad-credentials reply is `[AUTHENTICATIONFAILED] Invalid credentials (Failure)` (LIVE). |
| IMAP enable toggle | Removed in January 2025: "IMAP access is always turned on". Nothing to enable. |
| Session length | IMAP sessions last ~24 h. OAuth sessions last about the access-token lifetime (~1 h). Then Gmail closes the connection, so reconnect. |
| Connections | At most 15 simultaneous IMAP connections per account (keep jobbot to 1). |
| Bandwidth | 2,500 MB/day download, 500 MB/day upload (Workspace doc; consumer assumed similar). jobbot uses a few MB. |

**Gmail IMAP extensions** (enabled when `X-GM-EXT-1` is present):

| Extension | Syntax | Use in jobbot |
|---|---|---|
| `X-GM-RAW` | `UID SEARCH X-GM-RAW "<gmail web search query>"` | Search with Gmail's operators: `rfc822msgid:`, `from:`, `to:`, `subject:`, `newer_than:3d`, `after:2026/09/01`, `in:anywhere`, `label:`, `has:attachment`, `filename:pdf`, `is:unread`, `deliveredto:`, `-from:me` |
| `X-GM-THRID` | `FETCH n (X-GM-THRID)` / `UID SEARCH X-GM-THRID <decimal>` | Gmail conversation id (64-bit, decimal form of the hex id in the web UI / API `threadId`) |
| `X-GM-MSGID` | `FETCH n (X-GM-MSGID)` / `UID SEARCH X-GM-MSGID <decimal>` | Stable message id, the same in every folder |
| `X-GM-LABELS` | `FETCH n (X-GM-LABELS)`, `UID STORE n +X-GM-LABELS (jobbot/candidature)` | Optional: label sent mail and replies so they are easy to see in Gmail |
| Special-use (RFC 6154) | `LIST "" "*"` returns `\All \Sent \Drafts \Junk \Trash \Flagged \Important` | **Always resolve folders through these flags.** Names are localised: with a French UI, Sent is `[Gmail]/Messages envoy&AOk-s` (= "Messages envoyés"). `XLIST` has been deprecated since 2013. |

### 2.3 One-time setup: App Password (manual, done by the user, never by jobbot)

1. Turn on 2-Step Verification: `https://myaccount.google.com/signinoptions/two-step-verification`. The URL exists and redirects to sign-in (LIVE). Use phone prompts or an authenticator app. **Do not** set up security-key-only 2SV, because that disables App Passwords.
2. Create the App Password at `https://myaccount.google.com/apppasswords` (LIVE: 302 → ServiceLogin). Name it `jobbot`.
3. Copy the 16 letters into Windows Credential Manager with `keyring` (preferred), or into `SMTP_PASSWORD` in `.env` (already gitignored).
4. It stops working when the Google password changes. `jobbot doctor` should test SMTP `login()` and IMAP `login()` and say exactly that.

### 2.4 Alternative: Gmail API + OAuth desktop flow

| Item | Value |
|---|---|
| Send | `POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`, body `{"raw": base64url(RFC 5322 bytes), "threadId": "<optional>"}`. For large payloads: `POST https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send`. |
| Read | `GET .../users/me/messages?q=<gmail query>` (5 units), `GET .../users/me/messages/{id}?format=metadata&metadataHeaders=Message-ID...` (20 units), `GET .../users/me/threads/{id}` (40 units), `GET .../users/me/history?startHistoryId=` (2 units) |
| Without a token | `401` + `WWW-Authenticate: Bearer realm="https://accounts.google.com/"`, JSON `{"error":{"code":401,"message":"Request is missing required authentication credential...","status":"UNAUTHENTICATED"}}` (LIVE) |
| Scopes | `gmail.send` = **sensitive**. `gmail.readonly`, `gmail.modify`, `gmail.compose`, `gmail.metadata`, `https://mail.google.com/` = **restricted**. `gmail.labels` = non-sensitive. |
| Quotas | 6,000 units/min/user and 1,200,000 units/min/project. `messages.send` costs 100 units. At most 500 recipients per message. New quota regime for projects created on or after 2026-05-01. |
| OAuth client | GCP project → enable Gmail API → Google Auth Platform: audience **External** (consumer accounts cannot use "Internal") → add yourself as test user → Clients → **Desktop app** → download `credentials.json`. |
| Flow | `InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)` (loopback redirect; the OOB flow is gone) → save `token.json`. |
| Token pitfalls | **"Testing" publishing status → refresh token expires in 7 days** (`invalid_grant`). Changing the Google password revokes tokens that carry Gmail scopes. A token unused for 6 months expires. Limit of 100 refresh tokens per account per client (the oldest is silently revoked). |
| Personal-use escape hatch | Google: "If the app is for your personal use (fewer than 100 users) ... can continue using the app without going through verification". You see an "unverified app" screen. Switching to **"In production"** without verification reportedly removes the 7-day expiry (UNCERTAIN, third-party reports). |
| Libs (pip, 2026-09-23) | `google-api-python-client==2.200.0`, `google-auth-oauthlib==1.4.1` (+ `google-auth-httplib2`). You can also call REST with `httpx` and only use `google-auth` to get and refresh tokens. |

**SMTP/IMAP + App Password vs Gmail API:**

| | SMTP/IMAP + App Password | Gmail API (OAuth) |
|---|---|---|
| Setup | 2 min, no GCP | GCP project, consent screen, test user, desktop client |
| Dependencies | stdlib | 3 Google packages (or httpx + google-auth) |
| Token lifetime | Until password change or revocation | 7 days in Testing; long-lived only if "In production" |
| Permission scope | **Full mailbox** (read/send/delete) | Can be narrowed to `gmail.send` + `gmail.readonly` |
| Threading | Via headers + `X-GM-THRID` lookup | Via headers + `threadId` field (documented) |
| Incremental sync | UID + UIDVALIDITY per folder | `history.list` (cheap, robust) |
| Future risk | Google says App Passwords are "not recommended"; no end date announced for consumer accounts | Officially recommended path |
| Verdict | **Primary** | Fallback behind the same interface |

### 2.5 DNS facts for gmail.com (LIVE, via 8.8.8.8)

- `_dmarc.gmail.com TXT "v=DMARC1; p=none; sp=quarantine; rua=mailto:mailauth-reports@google.com"`
- `gmail.com TXT "v=spf1 redirect=_spf.google.com"`
- `_spf.google.com` lists Google's IP ranges and ends with `~all`.

Mail sent through smtp.gmail.com is SPF-aligned and DKIM-signed by Google, so there is nothing to configure. Never send `From: ...@gmail.com` through any other server: it fails DMARC alignment and gets spam-foldered.

---

## 3. Response / message shapes (real samples)

### 3.1 SMTP (LIVE, 2026-09-23; client IP redacted)

```
S: 250-smtp.gmail.com at your service, [<client-ip>]
S: 250-SIZE 35882577
S: 250-8BITMIME
S: 250-STARTTLS
S: 250-ENHANCEDSTATUSCODES
S: 250-PIPELINING
S: 250-CHUNKING
S: 250 SMTPUTF8
C: STARTTLS  ...  EHLO
S: 250-AUTH LOGIN PLAIN XOAUTH2 PLAIN-CLIENTTOKEN OAUTHBEARER XOAUTH
C: AUTH PLAIN <fake creds>
S: 535-5.7.8 Username and Password not accepted. For more information, go to
S: 535 5.7.8  https://support.google.com/mail/?p=BadCredentials 5b1f17b1804b1-...9.3 - gsmtp
```
Python raises `smtplib.SMTPAuthenticationError` with `.smtp_code == 535`. Port 465 shows the same EHLO and AUTH lines.

### 3.2 IMAP (LIVE + documented shapes)

```
* OK Gimap ready for requests from <client-ip> 5b1f17b1804b1-...
a1 CAPABILITY
* CAPABILITY IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 XYZZY SASL-IR AUTH=XOAUTH2 AUTH=PLAIN AUTH=PLAIN-CLIENTTOKEN AUTH=OAUTHBEARER
a2 LOGIN <fake> <fake>
a2 NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)
```
Documented LIST shape (English UI; a French UI localises the names, but the flags stay the same):
```
* LIST (\HasNoChildren) "/" "INBOX"
* LIST (\Noselect \HasChildren) "/" "[Gmail]"
* LIST (\HasNoChildren \All) "/" "[Gmail]/All Mail"
* LIST (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
* LIST (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
* LIST (\HasNoChildren \Junk) "/" "[Gmail]/Spam"
* LIST (\HasNoChildren \Trash) "/" "[Gmail]/Trash"
```
FETCH shape as `imaplib` returns it (a list of `(meta_bytes, payload_bytes)` tuples separated by `b')'`):
```python
[(b'5 (X-GM-THRID 1266894439832287888 X-GM-MSGID 1278455344230334865 UID 812 '
  b'BODY[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO ...)] {95}',
  b'Message-ID: <x@y.fr>\r\nIn-Reply-To: <abc.jobbot@gmail.com>\r\n...\r\n\r\n'),
 b')']
```

### 3.3 Outgoing message produced by jobbot (LIVE, generated locally, not sent)

```
From: Jean Dupont <ton.adresse@gmail.com>
To: Mme =?utf-8?q?=C3=89milie?= Durand <e.durand@example.fr>
Subject: Candidature =?utf-8?q?spontan=C3=A9e_=E2=80=93_Alternance_d=C3=A9ve?=
 =?utf-8?q?loppeur?= full-stack (Bac+5, oct. 2026)
Date: Wed, 23 Sep 2026 13:52:35 +0200
Message-ID: <179016435516.26396.10265082071649868446.jobbot@gmail.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="===============0715122881600684553=="
  ├─ multipart/alternative
  │    ├─ text/plain; charset="utf-8"  (Content-Transfer-Encoding: quoted-printable)
  │    └─ text/html;  charset="utf-8"
  └─ application/pdf; Content-Disposition: attachment;
       filename*=utf-8''CV_Jean_Dupont_Alternance_D%C3%A9veloppeur.pdf
```
Relance in the same thread (LIVE, generated locally):
```
Subject: Re: Candidature =?utf-8?q?spontan=C3=A9e?= ...
In-Reply-To: <179016435516.26396.10265082071649868446.jobbot@gmail.com>
References: <179016435516.26396.10265082071649868446.jobbot@gmail.com>
Message-ID: <179016435517.26396.2937063629173836830@gmail.com>
```
`make_msgid()` **without** `domain=` produced `<179016435431.26396.16191474454837955166@DESKTOP-XXXXXXX>`, which leaks the PC name (LIVE).

### 3.4 Bounce (DSN) from Gmail: structure

- From: `Mail Delivery Subsystem <mailer-daemon@googlemail.com>`
- Subject: `Delivery Status Notification (Failure)` (or `(Delay)` for a temporary failure)
- It follows RFC 3464 (the shape below was tested offline with the classifier in §6.6):

```
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Auto-Submitted: auto-replied                 # typical, UNCERTAIN
X-Failed-Recipients: rh@example.fr           # typical, UNCERTAIN
In-Reply-To / References: <your Message-ID>  # typical → the bounce lands IN your thread, UNCERTAIN
Content-Type: multipart/report; report-type=delivery-status; boundary=...
  ├─ text/plain           human explanation ("Address not found")
  ├─ message/delivery-status
  │     Reporting-MTA: dns; googlemail.com
  │     <blank line>
  │     Final-Recipient: rfc822; rh@example.fr
  │     Action: failed            (delayed = temporary)
  │     Status: 5.1.1             (5.x.x = permanent/hard, 4.x.x = temporary/soft)
  │     Diagnostic-Code: smtp; 550 5.1.1 ... User unknown
  └─ message/rfc822 or text/rfc822-headers   ← the original, incl. Message-ID
```
**Consequence:** a bounce can appear in the same `X-GM-THRID` as your application. **Classify bounces before treating a thread message as a "reply".**

### 3.5 Auto-replies / out-of-office

- RFC 3834 `Auto-Submitted`:
  - `no` means a human,
  - `auto-generated` means cron-like mail,
  - `auto-replied` means an automatic response.
  - A responder "MUST NOT" reply to anything carrying a value other than `no`.
- Other signals:
  - `X-Autoreply: yes`, `X-Autorespond: ...`,
  - `Precedence: auto_reply|bulk|junk|list`,
  - Exchange OOF replies use an **empty return path** (`Return-Path: <>`) and are sent **once per sender**,
  - subject prefixes such as `Réponse automatique :`, `Automatic reply:`, `Absence du bureau`, `Out of Office`.
- **Do not** use `X-Auto-Response-Suppress` as an *incoming* auto-reply signal. Exchange puts it on normal mail, and filtering on it drops genuine replies.
- **Do not** put `Auto-Submitted`/`Precedence: bulk` on jobbot's own candidatures. They are human-authored one-to-one e-mails. Those headers would make them look like bulk mail and would suppress the OOF that tells you someone is away.

### 3.6 Gmail API `Message` resource (if the API fallback is used)

`{ "id", "threadId", "labelIds": [...], "snippet", "historyId", "internalDate" (epoch ms, string), "payload": { "partId", "mimeType", "filename", "headers": [{"name","value"}], "body": {...}, "parts": [...] }, "sizeEstimate", "raw" (base64url, only with format=raw) }`.
`messages.send` returns a `Message` (at least `id`, `threadId`, `labelIds` containing `SENT`).

---

## 4. Limits, ToS, legal

### 4.1 Gmail limits (consumer account)

| Limit | Value | Source |
|---|---|---|
| Messages/day | 500 sent per day, or 500 recipients in one message → "You have reached a limit for sending mail"; normal sending returns after **1–24 h** | support.google.com/mail/answer/22839 |
| Attachments | 25 MB total for personal accounts. Above that, the **web UI** turns them into Drive links; SMTP just rejects the message. Executables (.exe...) are blocked. | support.google.com/mail/answer/6584 |
| SMTP SIZE | 35,882,577 bytes | LIVE |
| IMAP connections | 15 | support.google.com/mail/answer/7126229 |
| IMAP bandwidth | 2,500 MB/day down, 500 MB/day up | support.google.com/a/answer/1071518 (Workspace page) |
| Gmail API | 6,000 units/min/user; send = 100 units | developers.google.com/workspace/gmail/api/reference/quota |

**jobbot's own caps.** They are far below Google's, because the real limit is reputation, not quota.

| Channel (`Pacer`) | Days 1–14 | After 14 days, if bounces < 3% and no spam signals |
|---|---|---|
| `spontaneous` | 10–15/day | 25/day |
| `email` (answering job posts) | 10/day | 15/day |
| `followup` | 10/day | 15/day |
| **Global total** | ≤ 30/day | ≤ 50/day |

- Plus a **minimum gap of 4 min** and a random 4–12 min gap between sends.
- **Circuit breakers:**
  - more than 3 hard bounces in 24 h, or a bounce rate over 5% for the day → pause `spontaneous` for 48 h and notify,
  - any `550 5.7.1` or `550 5.4.5` → pause everything for 24 h.

### 4.2 Google policies

- **Gmail Program Policies:**
  - "Don't use Gmail to distribute spam or unsolicited commercial mail."
  - "You are not allowed to automate the Gmail interface, whether to send, delete, or filter emails, in a manner that misleads or deceives users."
  - Accounts can be disabled.
  - jobbot stays compliant by sending low volumes of genuinely personalised, non-commercial mail under the candidate's real identity.
- **Sender guidelines (since 2024-02-01, all senders to Gmail):**
  - SPF or DKIM (Google handles this for @gmail.com),
  - TLS,
  - RFC 5322-valid messages,
  - spam rate kept below 0.3% (Postmaster Tools, which you cannot use for gmail.com).
- One-click unsubscribe (`List-Unsubscribe` + `List-Unsubscribe-Post`) is only required for bulk senders (5,000+/day). **Do not add it** to one-to-one candidatures: it marks the mail as bulk or marketing.
- App Password page: "App passwords aren't recommended and are unnecessary in most cases" (it is still available).

### 4.3 French / EU law applied to spontaneous applications

**(a) Is a candidature spontanée "prospection directe" (CPCE art. L34-5)?**
- L34-5 defines it as "l'envoi de tout message destiné à promouvoir, directement ou indirectement, des biens, des services ou l'image d'une personne vendant des biens ou fournissant des services".
- A private person applying for a salaried job or apprenticeship does not sell goods or services. It is **most likely outside L34-5** (UNCERTAIN: I found no CNIL statement specific to job seekers).
- If it were in scope, the B2B regime would apply. L34-5 also requires, in every message:
  - "coordonnées valables" to ask for the messages to stop,
  - no concealment of identity,
  - a subject line that matches the content.
- jobbot follows all three anyway.

**(b) GDPR.**
- Nominative professional addresses (`e.durand@entreprise.fr`) are personal data.
- Recital 18 excludes processing "by a natural person in the course of a purely personal or household activity and thus with no connection to a professional or commercial activity".
- Job hunting is arguably *connected to a professional activity*, and jobbot automates collection at scale. **Do not rely on the household exemption** (UNCERTAIN). Treat the candidate as a controller acting on **legitimate interest** (Art. 6.1.f).

**(c) CNIL B2B e-mail rules** (cnil.fr/fr/la-prospection-commerciale-par-courrier-electronique), used as the safe benchmark:
- The subject of the message relates to the recipient's profession. Target HR, recruiters, tech leads and founders of small companies. Never private addresses.
- The person is informed, and can object simply and for free in **every** message.
- **Generic addresses of legal persons (`contact@`, `info@`, `recrutement@`) are not subject to these principles.** Rank them first (this matches `outreach/emails.rank_contacts`).

**(d) GDPR Art. 14 (data not obtained from the person).**
- Tell them the source at the latest at the first communication.
- One line in the footer is enough: *"J'ai trouvé votre adresse sur {source : page Carrières de votre site / mentions légales / annonce France Travail}."*
- Honour access and erasure requests. `jobbot contacts forget <email>` should delete the contact, then add its hash to the suppression list.

**(e) Scraping.**
- CNIL deliberation of 2024-12-05: KASPR was fined €240,000 for:
  - collecting LinkedIn contact details that users had restricted,
  - keeping them 5 years, with the clock renewed on every update,
  - late information, given in English only,
  - poor answers to access requests.
- → **Never extract e-mails from LinkedIn** (extensions, scraping of the contact-info panel).
- Only collect from company-owned public pages (contact, mentions légales, careers), from job ads that give an e-mail, and from open data. Record `source_url` and `collected_at` for every address.
- Guessed patterns (`prenom.nom@`) are personal data too, and inaccurate ones break accuracy (Art. 5.1.d). Only send them after an MX check. Drop them on the first bounce.

**(f) Retention** (CNIL Q&A on commercial management referentials):
- prospect data can be kept "trois ans à compter de leur collecte ou du dernier contact venant du prospect";
- opposition list "au minimum 3 ans".

jobbot policy (stricter, easy to defend):

| Data | Deleted after |
|---|---|
| scraped contact never used | 6 months |
| contacted, no reply | 12 months after the last e-mail |
| contact who replied | 24 months after the last exchange (the application may still be alive) |
| suppression list (SHA-256 of the lower-cased address + date + reason) | kept ≥ 3 years, never used for sending |

**(g) Tracking pixels.**
- CNIL recommendation adopted 2026-03-12, published 2026-04-14.
- It applies to "tout recours à des traceurs dans les courriels, quel que soit le contexte". Consent is needed except for strictly limited cases such as deliverability of transactional mail.
- → **jobbot must not embed open-tracking pixels or tracked or redirected links.** Measure success with replies instead.

**(h) Useful labour-law fact for the relance text.**
- Code du travail L6222-12-1: a 16–29-year-old without an employer can begin the apprenticeship training cycle for up to 3 months with trainee status, and sign the contract later.
- → From October 2026 the relance can honestly say "je peux démarrer mon contrat jusqu'à {date}". Confirm the exact date with the school.

---

## 5. Gotchas & anti-bot measures

1. **App Password revoked on a Google password change.** `535 5.7.8` suddenly appears. Stop, notify, do not retry in a loop: repeated failures lead to `454 4.7.0 Too many login attempts`.
2. **Normal password in `.env`** → `534 5.7.9 Application-specific password required`. Show a clear message in French.
3. **Default `make_msgid()` leaks the PC hostname** (`@DESKTOP-XXXXXXX`, LIVE). Always pass `domain="gmail.com"`. Do not use `%` or spaces in `idstring`:
   - a space makes Gmail rewrite the id (the original is kept in `X-Google-Original-Message-ID`, community report),
   - a `%` in a Message-ID caused an IMAP SEARCH issue on Google's issue tracker (#183677218, not readable anonymously, UNCERTAIN).
4. **Check that Gmail kept your Message-ID.** Look up `rfc822msgid:` in All Mail after sending. If it is not found within ~30 s, search `in:sent to:<recipient> newer_than:1d` and store the *actual* Message-ID from the Sent copy.
5. **Double-send after a crash.**
   1. Insert the `sent_emails` row with `status='sending'` and the pre-generated Message-ID *before* `send_message()`.
   2. On restart, for rows stuck in `sending`, look up `rfc822msgid:` in All Mail.
   3. Found → mark `sent`. Not found → safe to retry once.
6. **Replies in Spam.** `[Gmail]/All Mail` does **not** contain Spam or Trash, and IMAP SEARCH only covers the selected mailbox. Scan the `\Junk` folder too. Notify the user if a matched reply is found there, and tell them to mark it "Not spam" (UNCERTAIN, standard Gmail behaviour, not re-verified today).
7. **Localised folder names** (French UI: `[Gmail]/Tous les messages`, `[Gmail]/Messages envoy&AOk-s`, in modified UTF-7). Resolve them with special-use flags from `LIST "" "*"`. `imaplib` does not quote mailbox names, so wrap them in `"..."` yourself.
8. **`UID n:*` always returns at least the last message**, even when n > max UID. Filter `uid > last_uid` in Python.
9. **UIDVALIDITY.** Store `(folder, uidvalidity, last_uid)`. If UIDVALIDITY changes, re-scan by date (`X-GM-RAW "newer_than:30d"`). The architecture's single `kv["imap_last_uid"]` is not enough: use one key per folder.
10. **Do not mark the user's mail as read.** Use `BODY.PEEK[...]`, never `RFC822` or `BODY[]`. Open folders with `select(..., readonly=True)` (EXAMINE).
11. **Your own messages are in All Mail.** Exclude `From: ton.adresse@gmail.com` (or `X-GM-RAW "-from:me"`) before matching replies.
12. **The bounce sits in your thread.** Run the classifier in this order: bounce → auto-reply → human.
13. **OOF is sent once per sender** (Exchange). Do not count it as a reply. Parse a return date ("absent jusqu'au 5 octobre") to push the relance after that date. If the OOF names an alternative contact, add it as a new *candidate* contact with source `auto-reply` (it still needs review).
14. **Idle SMTP connections get dropped** during 4–12 min gaps. Open a new SMTP session for each message (a login costs little and keeps the code simple).
15. **Account lock-out heuristics.** Many identical e-mails, bursts, or many bounces trigger `550 5.7.1`, a temporary block, or even account review. Vary the wording per company, keep the gaps, and verify MX before sending.
16. **Attachment hygiene.**
    - One PDF of ≤ 1 MB (compress images).
    - Name it like `CV_Jean_Dupont_Alternance_Dev_FullStack.pdf`: ASCII, no spaces.
    - No `.docx`, `.zip`, `.exe`, or password-protected files.
    - Put the letter **in the body** for spontaneous applications. Attach a PDF letter only when an ad asks for one.
17. **Gmail threading rules** (Google, 2019). Messages thread if one is "sent in reply to another", **or** if they have the same sender/recipients and the same subject **and** were sent within one week. If a `References` header is present, it must reference earlier ids. → Keep the exact subject with `Re: `, and a complete `References` chain.
18. **Outlook recipients.** Outlook groups conversations mainly by normalised subject (plus `Thread-Index` when present). Keeping the same subject with `Re: ` covers them too (UNCERTAIN for exact Outlook logic).
19. **The From address must be the authenticated account.** Gmail may rewrite a mismatching `From` unless it is a verified "Send mail as" alias (UNCERTAIN).
20. **No tracking pixels, no URL shorteners, no `List-Unsubscribe`, no `Precedence: bulk`.** All of them push a one-to-one e-mail towards "marketing".
21. **POP/Gmailify changes (Jan 2026)** only removed Gmail's fetching of *other* accounts. IMAP/SMTP access *to* Gmail is unaffected (LIVE: servers answer normally).

---

## 6. Concrete Python implementation notes (stdlib, sync)

Maps onto `docs/ARCHITECTURE.md`: `mail/mailer.py`, `mail/inbox.py`, `followup.py`, `pacing.py`, `db.py`.
The code below was checked **offline** (message building, LIST/FETCH parsing, classifier) with Python 3.12.10. Network calls with real credentials were not run.

### 6.1 Secrets

```python
# pip install keyring==25.7.0  (Windows Credential Manager backend)
import keyring, os
def app_password() -> str:
    pw = keyring.get_password("jobbot-gmail", "ton.adresse@gmail.com") or os.environ.get("SMTP_PASSWORD", "")
    pw = pw.replace(" ", "")
    if len(pw) != 16:
        raise MailError("Mot de passe d'application Gmail manquant ou invalide (16 lettres attendues). "
                        "Crée-le sur https://myaccount.google.com/apppasswords (validation en 2 étapes requise).")
    return pw
```

### 6.2 Building and sending (`mail/mailer.py`)

```python
from __future__ import annotations
import email.policy, email.utils, re, smtplib, ssl, time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
MSGID_DOMAIN = "gmail.com"
MAX_ATTACH_BYTES = 2 * 1024 * 1024
RE_PREFIX = re.compile(r"^\s*((re|tr|fw|fwd|aw|wg|sv|rif)\s*:\s*)+", re.I)   # handles "RE :", "TR:"

def reply_subject(subject: str) -> str:
    return f"Re: {RE_PREFIX.sub('', subject).strip()}"          # never "Re: Re:"

def build_message(sender_name: str, sender_addr: str, *, to_addr: str, to_name: str | None,
                  subject: str, body_text: str, body_html: str | None = None,
                  attachments: tuple[Path, ...] = (), in_reply_to: str | None = None,
                  references: tuple[str, ...] = ()) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.SMTP)                  # CRLF, RFC 2047/2231 encoding
    msg["From"] = email.utils.formataddr((sender_name, sender_addr))
    msg["To"] = email.utils.formataddr((to_name, to_addr)) if to_name else to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=MSGID_DOMAIN)
    if in_reply_to:
        chain = list(references) or [in_reply_to]
        if chain[-1] != in_reply_to:
            chain.append(in_reply_to)
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(chain[-20:])                 # keep header reasonable
    msg.set_content(body_text, cte="quoted-printable")
    if body_html:
        msg.add_alternative(body_html, subtype="html", cte="quoted-printable")
    for p in attachments:
        data = p.read_bytes()
        if len(data) > MAX_ATTACH_BYTES:
            raise MailError(f"Pièce jointe trop lourde ({len(data)//1024} Ko) : {p.name}")
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=p.name)
    return msg

def smtp_send(user: str, password: str, msg: EmailMessage) -> dict:
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo(); s.starttls(context=ctx); s.ehlo()
        s.login(user, password)
        return s.send_message(msg)          # {} = all recipients accepted; else {addr: (code, msg)}
```

Error mapping (wrap `smtp_send`):
```python
except smtplib.SMTPAuthenticationError as e:     # 534 / 535
    raise MailAuthError(...) from e             # stop channel, notify, NO retry
except smtplib.SMTPRecipientsRefused as e:      # invalid address at RCPT time
    blacklist(...)
except smtplib.SMTPDataError as e:              # 550 5.4.5 quota, 550 5.7.1 spam, 552 size
    if b"5.4.5" in e.smtp_error: pacer.freeze_all(hours=24)
    elif b"5.7.1" in e.smtp_error: pacer.freeze_all(hours=24); notify(...)
except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError):
    retry_later(backoff=[5*60, 30*60, 2*3600])
```

Send sequence with idempotency (inside `Mailer.send`):
1. `refuse` if the recipient's hash is in the suppression list or the recipient is blacklisted.
2. `pacer.acquire(channel)`. It raises `QuotaExceeded` when the daily cap is hit and sleeps the random gap.
3. `msg = build_message(...)`, then `repo.insert_sent_email(message_id=msg["Message-ID"], status="sending", ...)`.
4. `smtp_send(...)`, then `status="sent"`, `sent_at=now`.
5. `lookup_sent_copy(message_id)` (§6.4). Store `gm_thrid`, `gm_msgid`. Optionally label it `jobbot/candidature`.

Suggested `sent_emails` columns: `id, application_id, kind (application|spontaneous|followup), to_addr, subject, message_id UNIQUE, in_reply_to, references_json, gm_thrid, gm_msgid, status (sending|sent|failed|deferred), error, sent_at`.

### 6.3 IMAP connection & folder discovery (`mail/inbox.py`)

```python
import base64, email, imaplib, re, ssl, time
IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "(?P<delim>[^"]*)" (?P<name>.+)$')

def connect(user: str, password: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context(), timeout=30)
    imap.login(user, password)                   # imaplib.IMAP4.error on bad creds
    return imap

def special_folders(imap) -> dict[str, str]:
    typ, lines = imap.list('""', '"*"')
    found = {}
    for line in lines or []:
        m = LIST_RE.match(line)
        if not m: continue
        name = m.group("name").decode()
        name = name[1:-1] if name.startswith('"') else name
        for flag in m.group("flags").decode().split():
            if flag in ("\\All", "\\Sent", "\\Junk", "\\Drafts", "\\Trash"):
                found[flag] = name                # raw modified-UTF-7 name, usable as-is in SELECT
    return found

def q(name: str) -> str:                          # imaplib does not quote mailbox names
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
```
(Modified UTF-7 decoding, only for display: base64 of UTF-16-BE between `&` and `-`, with `,` in place of `/`. `&AOk-` → `é`. Tested.)

### 6.4 Gmail search (X-GM-RAW), post-send lookup, thread id

```python
def gm_search(imap, query: str) -> list[bytes]:
    imap.literal = query.encode("utf-8")                         # sent as {n} literal → accents OK
    typ, data = imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
    if typ != "OK": raise RuntimeError(data)
    return data[0].split() if data and data[0] else []

HEADER_FIELDS = ("MESSAGE-ID IN-REPLY-TO REFERENCES FROM TO SUBJECT DATE AUTO-SUBMITTED PRECEDENCE "
                 "X-AUTOREPLY X-AUTORESPOND X-FAILED-RECIPIENTS RETURN-PATH CONTENT-TYPE")
META_RE = re.compile(rb"X-GM-THRID (\d+)|X-GM-MSGID (\d+)|UID (\d+)")

def fetch_headers(imap, uids: list[bytes]) -> list[dict]:
    typ, data = imap.uid("FETCH", b",".join(uids).decode(),
                         f"(UID X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
    rows = []
    for item in data:
        if not isinstance(item, tuple): continue
        meta, payload = item
        thrid = msgid = uid = None
        for m in META_RE.finditer(meta):
            thrid, msgid, uid = thrid or m.group(1), msgid or m.group(2), uid or m.group(3)
        rows.append({"uid": int(uid), "gm_thrid": int(thrid), "gm_msgid": int(msgid),
                     "headers": email.message_from_bytes(payload, policy=email.policy.default)})
    return rows

def lookup_sent_copy(imap, all_mail: str, message_id: str, tries: int = 6, wait: float = 5.0):
    imap.select(q(all_mail), readonly=True)
    for _ in range(tries):
        uids = gm_search(imap, f"rfc822msgid:{message_id.strip('<>')}")
        if uids:
            return fetch_headers(imap, uids[-1:])[0]              # → gm_thrid, gm_msgid
        time.sleep(wait)
    return None                                                   # fallback: in:sent to:<addr> newer_than:1d
```

### 6.5 Inbox poll (every 15–30 min; `process_inbox`)

```
for flag in ("\\All", "\\Junk"):
    folder = folders[flag]
    typ, _ = imap.select(q(folder), readonly=True)                # EXAMINE: never changes \Seen
    uidvalidity = int(imap.response("UIDVALIDITY")[1][0])         # or: imap.status(q(folder), "(UIDVALIDITY UIDNEXT)")
    last = kv.get(f"imap:{flag}:{uidvalidity}:last_uid")          # None → first run / UIDVALIDITY changed
    uids = (gm_search(imap, "newer_than:14d -from:me") if last is None
            else [u for u in imap.uid("SEARCH", "UID", f"{last+1}:*")[1][0].split() if int(u) > last])
    for chunk in batched(uids, 100):
        for row in fetch_headers(imap, chunk):
            app = match_application(row)   # 1) In-Reply-To/References ∈ sent_emails.message_id
                                           # 2) row.gm_thrid ∈ sent_emails.gm_thrid
                                           # 3) sender domain == company.domain AND sent < 60 days ago (confidence 0.6)
            if not app: continue
            cls = classify(raw_headers_or_full)   # §6.6; full body fetched only for matched messages:
                                                  # UID FETCH <uid> (BODY.PEEK[])
            apply_outcome(app, cls)
    kv.set(f"imap:{flag}:{uidvalidity}:last_uid", max_uid_seen)
imap.logout()   # always in finally
```

Outcomes:

| Classification | Application status | Relances | Other actions |
|---|---|---|---|
| `bounce` hard (5.x.x) | `BOUNCED` | cancel | Blacklist the address. If the company has another contact, queue it for review. |
| `bounce` soft (4.x.x / "Delay") | unchanged | unchanged | Log only. Gmail keeps retrying on its own. |
| `auto_reply` | unchanged | keep, but postpone `next_followup_at` to max(original, return date + 2 working days) | — |
| `human` + rejection | `REJECTED` | cancel | Notify. |
| `human` + interview/positive/info_request | `REPLIED` / `INTERVIEW` | cancel | Notify immediately (toast + Jarvis). |
| `human` + unsubscribe/opt-out | — | cancel | Blacklist + suppression hash, **never reply**. |

### 6.6 Classifier (rules first; LLM fallback per ARCHITECTURE). Tested offline.

```python
BOUNCE_FROM = re.compile(r"(mailer-daemon|postmaster|mail delivery (subsystem|system))", re.I)
AUTO_SUBJ = re.compile(r"^\s*(r[ée]ponse automatique|automatic reply|auto(matic)?[- ]?reply|"
                       r"out of (the )?office|absence|absent|en cong[ée]s?|accus[ée] de r[ée]ception)", re.I)

def classify(raw: bytes) -> Classified:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if (msg.get_content_type() == "multipart/report"
            and (msg.get_param("report-type") or "").lower() == "delivery-status") \
            or BOUNCE_FROM.search(str(msg.get("From", ""))):
        rcpts, statuses, orig = [], [], None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "message/delivery-status":
                for block in part.get_payload():                 # list of header blocks
                    if block.get("Final-Recipient"):
                        rcpts.append(block["Final-Recipient"].split(";", 1)[-1].strip())
                    if block.get("Status"):
                        statuses.append(block["Status"].strip())
            elif ct == "message/rfc822" and orig is None:
                orig = part.get_payload()[0].get("Message-ID")
            elif ct == "text/rfc822-headers" and orig is None:
                orig = email.message_from_string(part.get_content()).get("Message-ID")
        if not rcpts and msg.get("X-Failed-Recipients"):
            rcpts = [a.strip() for a in str(msg["X-Failed-Recipients"]).split(",")]
        hard = any(s.startswith("5") for s in statuses) or not statuses
        return Classified("bounce", "hard" if hard else "soft", tuple(rcpts), orig or msg.get("In-Reply-To"))
    auto = str(msg.get("Auto-Submitted", "no")).strip().lower()
    prec = str(msg.get("Precedence", "")).strip().lower()
    if (auto not in ("", "no") or msg.get("X-Autoreply") or msg.get("X-Autorespond")
            or prec in ("auto_reply", "bulk", "junk", "list")
            or str(msg.get("Return-Path", "")).strip() == "<>"
            or AUTO_SUBJ.search(str(msg.get("Subject", "")))):
        return Classified("auto_reply")
    return Classified("human")
```
Offline test results:
- synthetic Gmail DSN → `bounce/hard, ('rh@example.fr',), '<abc.jobbot@gmail.com>'`
- Exchange-style OOF → `auto_reply`
- human "RE:" → `human`

Human-reply sub-classification, run on the **new** text only. Cut the quoted part at the first of:
- a line starting with `>`,
- `^Le .{5,80} a écrit\s?:`,
- `^On .{5,80} wrote:`,
- `^De\s?:` / `^From:`,
- `^-{2,}\s?(Message d'origine|Original Message)`,
- `^_{10,}`.

French keyword rules (then LLM if nothing matches):

| Category | Regex |
|---|---|
| `unsubscribe` | `d[ée]sinscri|ne plus (me|nous) (contacter|[ée]crire|solliciter)|retir(er|ez) (mon|notre) adresse|merci de ne plus|\bstop\b` |
| `rejection` | `malheureusement|ne (pas|pouvons pas) donner (une )?suite|pas de suite favorable|ne correspond pas|poste (a [ée]t[ée] )?pourvu|pas de besoin|pas en mesure|nous avons retenu (une autre|un autre)` |
| `interview` | `entretien|disponibilit[ée]s|cr[ée]neau|[ée]change t[ée]l[ée]phonique|visio|teams|google meet|rencontrer|vous rappeler` |
| `info_request` | `pourriez-vous (nous )?(envoyer|transmettre|pr[ée]ciser)|quel(le)? (est|sont) votre|rythme|date de d[ée]but` |

### 6.7 Follow-up scheduling (`followup.py`)

```python
# pip install holidays==0.105
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
import holidays, random
PARIS = ZoneInfo("Europe/Paris")
HOL = holidays.France(subdiv="57", years=range(2026, 2028))     # Moselle: + Good Friday, + 26 Dec

def add_working_days(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in HOL:
            n -= 1
    return d

def next_slot(d: date) -> datetime:
    while d.weekday() >= 5 or d in HOL:
        d += timedelta(days=1)
    if d.weekday() == 0: d += timedelta(days=1)                   # Monday → Tuesday
    if d.weekday() == 4: d -= timedelta(days=1)                   # Friday → Thursday (still ≥ due-1)
    start = random.choice([dtime(9, 0), dtime(14, 0)])
    return datetime.combine(d, start, PARIS) + timedelta(minutes=random.randint(0, 110))

DELAYS = {  # working days
    "offer":       (7, 10),    # (relance 1 after the application, relance 2 after relance 1)
    "spontaneous": (10, 10),
    "interview":   (5, None),  # or the date announced by the recruiter + 2 working days
}
URGENT_UNTIL = date(2026, 10, 15)   # alternance school start: offer (5, 7), spontaneous (7, 7)
MAX_FOLLOWUPS = 2                    # then NO_RESPONSE; never after any human reply
```
A relance is sent **in the same thread**:
- `in_reply_to` = the last message *we* sent in that thread,
- `references` = the stored chain,
- `subject` = `reply_subject(original_subject)`.

Quote the original below the new text, the Gmail way: `Le mer. 23 sept. 2026 à 10:12, Jean Dupont <ton.adresse@gmail.com> a écrit :` followed by `> ` lines. Re-attach the CV to relance 1 only.

### 6.8 Review mode: draft into Gmail instead of sending (optional)

```python
imap.append(q(folders["\\Drafts"]), r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
```
The draft appears in Gmail (web and phone) for the user to check and send. Whether Gmail keeps your `In-Reply-To`/`References` when the draft is sent from the UI is UNCERTAIN. Test it once, and use it for first contacts rather than relances.

### 6.9 Gmail API fallback (sketch)

```python
from pathlib import Path
import base64
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
SCOPES = ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"]

def gmail_service(token: Path, client_secret: Path):
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())            # google.auth.exceptions.RefreshError('invalid_grant') → re-consent + notify
        else:
            creds = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES).run_local_server(port=0)
        token.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)

def api_send(svc, msg, thread_id: str | None = None) -> dict:
    body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id: body["threadId"] = thread_id                  # + matching Subject + References/In-Reply-To
    return svc.users().messages().send(userId="me", body=body).execute()   # {'id','threadId','labelIds'}
```
Replies: `svc.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From","Subject","Message-ID","In-Reply-To","References","Auto-Submitted"]).execute()`, or incremental sync with `history.list(startHistoryId=...)`.

XOAUTH2 over smtplib/imaplib (needs scope `https://mail.google.com/`, restricted):
```python
auth = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
smtp.auth("XOAUTH2", lambda challenge=None: auth)          # smtplib base64-encodes
imap.authenticate("XOAUTH2", lambda _: auth.encode())
```

### 6.10 Deliverability checklist enforced in code

- `Pacer` caps (§4.1). Allowed window Mon–Fri 08:30–11:30 / 14:00–17:00 Europe/Paris. Random 4–12 min gap. The first-2-weeks ramp is stored in `kv["mail_ramp_start"]`.
- Before any first contact:
  - `has_mx(domain)` (dnspython),
  - drop `noreply|no-reply|donotreply|mailer-daemon|postmaster|abuse|rgpd|dpo|compta|factur`,
  - no role mismatch.
  - Never probe addresses with SMTP `RCPT TO` from a home connection (catch-all domains lie, outbound port 25 is usually blocked, and it hurts reputation).
- Content lint, blocking before send:
  - the subject has no ALL CAPS words and no `!!`,
  - no "gratuit/urgent/100%/€",
  - ≤ 3 links, no shorteners (`bit.ly|tinyurl|t.co|lnkd.in`),
  - body 80–220 words,
  - at least one company-specific sentence (company name + one fact),
  - the rapidfuzz similarity to the last 20 sent bodies is < 90,
  - the attachment is a PDF of ≤ 1 MB,
  - the footer has the source + opt-out line,
  - no `<img>` in the HTML.
- Headers: only `From/To/Subject/Date/Message-ID/MIME` (+ `In-Reply-To/References`). **No** `List-Unsubscribe`, `Precedence`, `Auto-Submitted`, `X-Mailer`.

### 6.11 Templates (French; the LLM personalises, the template is the fallback)

**Candidature spontanée (alternance)**, subject: `Candidature alternance développeur full-stack (Bac+5, 24 mois) – Jean Dupont`
```
Bonjour {Madame/Monsieur Nom | Bonjour,},

Diplômé d'un BUT Informatique ({IUT}) et admis en {intitulé} à {école}
(rentrée octobre 2026), je recherche une alternance de 24 mois en développement full-stack.
{Phrase spécifique : ce qui m'attire chez {Entreprise} — produit, stack, actualité.}

{1–2 phrases de preuve : projet/stage + techno alignée avec l'entreprise.}
Rythme : {rythme}. Basé à Metz, mobile sur Nancy, Thionville et le Luxembourg.

Mon CV est en pièce jointe. Seriez-vous disponible pour un court échange la semaine prochaine ?

Bien cordialement,
Jean Dupont
{téléphone} · {LinkedIn} · {GitHub/portfolio}

PS : j'ai trouvé votre adresse sur {source}. Un simple « non merci » en réponse et je ne vous
recontacterai pas.
```

**Relance 1** (same thread, `Re: <objet>`, J+10 working days, J+7 during the start-of-year rush)
```
Bonjour {Madame/Monsieur Nom},

Je me permets de revenir vers vous au sujet de ma candidature en alternance (développeur
full-stack, 24 mois, dès octobre 2026) envoyée le {date}.
{Élément nouveau : projet terminé / certification / date de démarrage possible.}

Auriez-vous quelques minutes pour en parler, ou pourriez-vous m'indiquer la personne à contacter ?
Je joins de nouveau mon CV pour plus de commodité.

Bien cordialement,
Jean Dupont — {téléphone}
```

**Relance 2, the last one** (J+10 working days after relance 1)
```
Bonjour,

Un dernier message au sujet de ma candidature en alternance : si le moment n'est pas opportun,
je le comprends tout à fait — une réponse, même négative, m'aiderait beaucoup, et je ne vous
relancerai plus. Je reste disponible pour démarrer jusqu'au {date limite confirmée par l'école}.

Belle journée,
Jean Dupont
```

**Relance after applying to a job posting** (J+7 working days), subject `Re: Candidature – {intitulé} (réf. {réf})`
```
Bonjour,

J'ai postulé le {date} à votre offre « {intitulé} » (réf. {réf}, {site}). Je souhaitais m'assurer
que mon dossier vous était bien parvenu et vous confirmer ma motivation : {1 exigence clé de l'offre
↔ 1 réalisation}. Avez-vous une visibilité sur les prochaines étapes ?

Bien cordialement,
Jean Dupont — {téléphone}
```

**After an interview:**
- Thank-you note the same day or J+1: 4–5 lines, recall one point discussed.
- Relance: the announced date + 2 working days, or J+5–7 working days if no date was given.

**Tone rules:**
- Use "vous".
- ≤ 10 lines.
- One question per message.
- Add a new element each time.
- Never guilt the reader or put pressure on them.
- Never mention salary.
- Never send from a phone-style one-liner.

### 6.12 Tests (no network, per ARCHITECTURE)

- Fake `smtplib.SMTP` via `smtp_factory`: assert `starttls` is called before `login`, that `Message-ID` ends with `@gmail.com`, and that relance headers carry `In-Reply-To`/`References` and `Re: ` (not `Re: Re:`).
- Fake IMAP returning the LIST/FETCH tuples from §3.2. Assert the special folder mapping with French names, the `UID n:*` filtering, and that UIDVALIDITY changes trigger a rescan.
- Classifier fixtures: Gmail DSN, Exchange OOF (`Return-Path: <>`, "Réponse automatique :"), human "RE :" reply with a quoted part, opt-out "merci de ne plus me contacter".
- A `live` marker (skipped by default) for `jobbot doctor --live`: runs SMTP login + IMAP login + LIST, and sends nothing.

---

## 7. Claims table

| # | Claim | Tag | Source |
|---|---|---|---|
| 1 | smtp.gmail.com:587 (STARTTLS) and :465 (TLS) reachable; after TLS they offer `AUTH LOGIN PLAIN XOAUTH2 PLAIN-CLIENTTOKEN OAUTHBEARER XOAUTH` | LIVE-VERIFIED | probe 2026-09-23 11:47 UTC |
| 2 | SMTP advertises `SIZE 35882577`, `8BITMIME`, `SMTPUTF8`, `CHUNKING` | LIVE-VERIFIED | probe |
| 3 | Bad credentials → `535 5.7.8 Username and Password not accepted ... ?p=BadCredentials` | LIVE-VERIFIED | probe (fake account) |
| 4 | imap.gmail.com:993 capabilities include `X-GM-EXT-1`, `AUTH=PLAIN`, `AUTH=XOAUTH2`, `IDLE`, `XLIST` | LIVE-VERIFIED | probe |
| 5 | IMAP bad login → `[AUTHENTICATIONFAILED] Invalid credentials (Failure)` | LIVE-VERIFIED | probe |
| 6 | App Passwords need 2SV; unavailable with security-key-only 2SV, work/school accounts, Advanced Protection; revoked on password change; "aren't recommended" | DOC-SOURCED | https://support.google.com/accounts/answer/185833 |
| 7 | `https://myaccount.google.com/apppasswords` exists (302 → sign-in) | LIVE-VERIFIED | curl |
| 8 | Since 2025-03-14 IMAP/SMTP/POP no longer work with legacy passwords ("with the exception of app passwords") | DOC-SOURCED | https://knowledge.workspace.google.com/admin/sync/transition-from-less-secure-apps-to-oauth |
| 9 | Consumer "less secure apps" ended May 2022 | DOC-SOURCED (third-party) | https://www.getmailbird.com/gmail-oauth-changes-app-password-phase-out/ |
| 10 | No official end date for consumer App Passwords as of 2026-09-23 (blog claims of a "2026 phase-out" are unsourced) | UNCERTAIN | absence of evidence; Google page above has no date |
| 11 | IMAP enable/disable toggle removed January 2025; IMAP always on | DOC-SOURCED | https://support.google.com/mail/answer/7126229 |
| 12 | Max 15 simultaneous IMAP client connections | DOC-SOURCED | same page |
| 13 | IMAP sessions ~24 h; OAuth sessions ~ token lifetime (~1 h); SASL XOAUTH2 | DOC-SOURCED | https://developers.google.com/workspace/gmail/imap/imap-smtp |
| 14 | Consumer limit 500 messages/day; lock lifts in 1–24 h | DOC-SOURCED | https://support.google.com/mail/answer/22839 |
| 15 | Personal attachment limit 25 MB; executables blocked | DOC-SOURCED | https://support.google.com/mail/answer/6584 |
| 16 | IMAP bandwidth 2,500 MB/day down, 500 MB/day up | DOC-SOURCED (Workspace page; consumer UNCERTAIN) | https://support.google.com/a/answer/1071518 |
| 17 | SMTP codes 534 5.7.9, 454 4.7.0, 421 4.7.0, 550 5.4.5, 552 5.3.4, 550 5.7.1, 550 5.1.1 and their meanings | DOC-SOURCED | https://knowledge.workspace.google.com/admin/support/troubleshooting/gmail-smtp-errors-and-codes |
| 18 | Gmail auto-saves SMTP-sent mail to Sent; client-side APPEND duplicates or errors | DOC-SOURCED (community) | https://bugzilla.mozilla.org/show_bug.cgi?id=1427619 |
| 19 | X-GM-RAW / X-GM-THRID / X-GM-MSGID / X-GM-LABELS syntax; special-use flags; XLIST deprecated | DOC-SOURCED | https://developers.google.com/workspace/gmail/imap/imap-extensions |
| 20 | `rfc822msgid:`, `newer_than:`, `in:anywhere`, `deliveredto:` etc. search operators | DOC-SOURCED | https://support.google.com/mail/answer/7190 |
| 21 | `UID SEARCH CHARSET UTF-8 X-GM-RAW` + imaplib `literal` works for accented queries | UNCERTAIN | common pattern; not run with credentials |
| 22 | French-UI folder names are localised (e.g. `[Gmail]/Messages envoy&AOk-s`); resolve by flags | UNCERTAIN (localisation) / UTF-7 decode LIVE-VERIFIED locally | local test |
| 23 | All Mail excludes Spam/Trash, so the Junk folder must be scanned separately | UNCERTAIN | standard Gmail behaviour, not re-checked |
| 24 | Gmail API scopes: `gmail.send` sensitive; `readonly/modify/compose/metadata/mail.google.com` restricted | DOC-SOURCED | https://developers.google.com/workspace/gmail/api/auth/scopes |
| 25 | "Testing" publishing status → refresh token expires in 7 days; 100 refresh tokens per account per client; 6-month inactivity; password change revokes Gmail-scoped tokens | DOC-SOURCED | https://developers.google.com/identity/protocols/oauth2 |
| 26 | Personal-use apps (< 100 users) need no verification but show the unverified screen | DOC-SOURCED | https://support.google.com/cloud/answer/13464323 |
| 27 | "In production" + unverified removes the 7-day expiry for the owner, restricted scopes included | UNCERTAIN | https://github.com/davistroy/open-brain/issues/282 (third party) |
| 28 | Gmail API quota 6,000 units/min/user; send = 100 units; new quotas for projects created from 2026-05-01 | DOC-SOURCED | https://developers.google.com/workspace/gmail/api/reference/quota |
| 29 | Gmail API without a token → 401 JSON `UNAUTHENTICATED` | LIVE-VERIFIED | curl |
| 30 | API threading needs `threadId` + RFC 2822 `References`/`In-Reply-To` + matching `Subject` | DOC-SOURCED | https://developers.google.com/workspace/gmail/api/guides/threads |
| 31 | Python quickstart: `InstalledAppFlow...run_local_server(port=0)`, Python ≥ 3.10.7 | DOC-SOURCED | https://developers.google.com/workspace/gmail/api/quickstart/python |
| 32 | Gmail threading rules (reply, or same subject + participants within 1 week; `References` must reference earlier ids) | DOC-SOURCED | https://workspaceupdates.googleblog.com/2019/03/threading-changes-in-gmail-conversation-view.html |
| 33 | Gmail overwrites malformed Message-IDs (keeps `X-Google-Original-Message-ID`) | UNCERTAIN | community reports (groups.google.com comp.mail.sendmail) |
| 34 | `make_msgid()` without `domain` uses the PC hostname; `EmailMessage(policy=SMTP)` encodes RFC 2047 subjects and RFC 2231 filenames | LIVE-VERIFIED (local Python 3.12.10) | scratch test |
| 35 | Outlook threads mainly by normalised subject / Thread-Index | UNCERTAIN | — |
| 36 | RFC 3834 `Auto-Submitted` semantics; responders must not reply to non-`no` values | DOC-SOURCED | https://www.rfc-editor.org/rfc/rfc3834 |
| 37 | Use `X-Autoreply`/`X-Autorespond`/`Auto-Submitted` to detect auto-replies; don't filter on `X-Auto-Response-Suppress` | DOC-SOURCED | https://www.jitbit.com/maxblog/18-detecting-outlook-autoreplyout-of-office-emails-and-x-auto-response-suppress-header/ |
| 38 | Exchange OOF uses a blank `<>` return path and replies once per sender | DOC-SOURCED | https://learn.microsoft.com/en-us/troubleshoot/exchange/email-delivery/understand-troubleshoot-oof-replies |
| 39 | Gmail bounces come from `mailer-daemon@googlemail.com`, subject "Delivery Status Notification (Failure)" | DOC-SOURCED | https://support.google.com/mail/answer/6596 ; lettr.com blog |
| 40 | Gmail DSN carries `X-Failed-Recipients`, `Auto-Submitted: auto-replied`, and In-Reply-To pointing at the original (so it threads) | UNCERTAIN | typical observation; classifier handles both cases |
| 41 | Google sender requirements (all senders: SPF or DKIM, TLS, RFC 5322, spam < 0.3%; bulk ≥ 5k/day: one-click unsubscribe) | DOC-SOURCED | https://support.google.com/a/answer/81126 |
| 42 | gmail.com DMARC `p=none; sp=quarantine`; SPF `redirect=_spf.google.com` | LIVE-VERIFIED | nslookup @8.8.8.8 |
| 43 | Gmail Program Policies forbid spam / unsolicited commercial mail and deceptive automation | DOC-SOURCED | https://support.google.com/mail/answer/16734397 |
| 44 | Safe cold volume for a warmed mailbox is ~20–50/day; ramp slowly | DOC-SOURCED (vendor blogs, opinion) | https://www.mailreach.co/blog/gmail-warmup ; https://mailtester.ninja/how-many-cold-emails-per-day/ |
| 45 | CNIL B2B e-mail: allowed without consent if related to the profession, with information and a simple opt-out; generic `contact@`/`info@` addresses are outside these rules | DOC-SOURCED | https://www.cnil.fr/fr/la-prospection-commerciale-par-courrier-electronique |
| 46 | L34-5 definition of "prospection directe"; identity and opt-out coordinates required | DOC-SOURCED | https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000042155961/ |
| 47 | A candidature spontanée is not "prospection directe" | UNCERTAIN | legal interpretation |
| 48 | GDPR household exemption (Recital 18) wording | DOC-SOURCED | https://gdpr-info.eu/recitals/no-18/ |
| 49 | The household exemption covers automated job-search outreach | UNCERTAIN (probably not) | — |
| 50 | CNIL fined KASPR €240,000 (2024-12-05) for LinkedIn contact scraping, 5-year rolling retention, and poor information/access | DOC-SOURCED | https://www.cnil.fr/fr/aspiration-de-donnees-sanction-de-240-000-euros-lencontre-de-la-societe-kaspr |
| 51 | CNIL: prospect data ≤ 3 years from collection or the prospect's last contact; opposition list ≥ 3 years | DOC-SOURCED | https://www.cnil.fr/fr/questions-reponses-sur-les-referentiels-relatifs-la-gestion-des-activites-commerciales-et-des |
| 52 | CNIL tracking-pixel recommendation adopted 2026-03-12, published 2026-04-14; applies to all e-mail contexts; consent required outside narrow exemptions | DOC-SOURCED | https://www.cnil.fr/fr/recommandation-pixel-suivi-courriels ; https://www.cnil.fr/fr/faq-recommandation-pixels-courriers-electroniques |
| 53 | Relance timing: spontaneous 10–15 days; after a job-post deadline 7–10 days; after an interview, announced date + 2 days or ~10 days; max 2 relances; avoid Friday PM / Monday AM | DOC-SOURCED | https://www.meteojob.com/blog-emploi/recherche-demploi/quand-et-comment-relancer-une-candidature ; https://www.randstadprofessional.fr/blog/carriere/relance-mail-apres-une-candidature-ce-qui-fonctionne-vraiment/ |
| 54 | L6222-12-1: start the apprenticeship training up to 3 months without an employer (16–29 y) | DOC-SOURCED | https://code.travail.gouv.fr/code-du-travail/l6222-12-1 |
| 55 | `holidays.France(subdiv="57")` adds Good Friday + 26 Dec (Moselle) | DOC-SOURCED | https://github.com/vacanza/holidays/blob/main/holidays/countries/france.py |
| 56 | PyPI latest: google-api-python-client 2.200.0, google-auth-oauthlib 1.4.1, imap-tools 1.15.0, holidays 0.105, keyring 25.7.0 | LIVE-VERIFIED | `pip index versions` 2026-09-23 |
| 57 | Gmailify + POP fetching of *other* accounts ended Jan 2026; Gmail's own IMAP/SMTP unaffected | DOC-SOURCED (news) + LIVE (servers answer) | https://www.theregister.com/2026/01/05/gmail_dropping_pop3/ |
| 58 | Classifier code handles a Gmail-style DSN, an Exchange-style OOF and a human reply correctly | LIVE-VERIFIED (offline synthetic fixtures) | scratch test |
| 59 | A draft APPENDed via IMAP keeps `In-Reply-To`/`References` when sent from the Gmail UI | UNCERTAIN | — |
| 60 | Gmail rewrites `From` when it is not the account or a verified alias | UNCERTAIN | — |
