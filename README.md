# jobbot — ta recherche d'alternance (et de CDI) en pilote automatique

jobbot automatise la partie répétitive de ta recherche. Il trouve les offres, les trie, prépare un CV et une lettre
ciblés pour chacune, postule quand c'est possible et relance au bon moment. Il lit aussi les réponses et contacte les
entreprises qui recrutent des alternants sans avoir publié d'offre. Quand un entretien arrive, il te prépare une fiche,
et chaque semaine il fait le bilan. Tu gardes la main : rien ne part sans respecter tes quotas, et tu peux tout valider
avant envoi.

> Tout part de **ton** profil, `config/profile.yaml`, qui reste sur ta machine (il est ignoré par git). Le dépôt
> fournit un modèle fictif, `config/profile.example.yaml` (Jean DUPONT, développeur full-stack junior à Metz qui
> cherche une alternance Bac+5 ou un CDI junior à Metz, Nancy, au Luxembourg ou en télétravail) : remplace-le par
> le tien.

---

## 1. Ce que fait jobbot, étape par étape

| Étape | Ce qui se passe |
|---|---|
| **Offres de l'école** (`classroom watch`) | Le flux Google Classroom de ton école est relu toutes les 15 minutes : chaque nouvelle offre adaptée reçoit tout de suite un CV et une lettre ciblés, et la candidature part aussitôt (voir § 3.6). |
| **Collecte** (`search`) | Offres de La Bonne Alternance, France Travail, LinkedIn (sans connexion), HelloWork, Indeed, Welcome to the Jungle, Free-Work, APEC, Adzuna, Jooble et des **pages carrières des entreprises** (Lever, Greenhouse, Teamtailor…, voir § 3.7), à Metz, Nancy, **au Luxembourg** et en télétravail. Les doublons entre sites sont détectés. |
| **Tri** (`score`) | Chaque offre reçoit une note de 0 à 100 avec ses raisons (intitulé, compétences, distance, contrat, pays, expérience demandée, salaire). Les fausses alternances d'écoles et les postes commerciaux sont écartés. |
| **CV + lettre** (`prepare`) | CV d'une page et lettre adaptés aux mots-clés de l'offre, avec un **score ATS expliqué** (mots-clés présents ou manquants). Tout est tiré de `config/profile.yaml` : **rien n'est inventé**. |
| **Candidature** (`apply`) | Envoi par LinkedIn (candidature simplifiée), HelloWork, La Bonne Alternance ou e-mail. Sur le site d'une entreprise, jobbot pré-remplit le formulaire et c'est **toi** qui cliques « Envoyer » (`assist`). |
| **Spontanées** (`discover`, `enrich`, `spontaneous`) | Recherche d'entreprises du numérique dans les données publiques et parmi les recruteurs de La Bonne Alternance, puis des bonnes personnes à contacter (adresse de recrutement, RH, responsable technique, dirigeant d'une petite entreprise) et envoi d'une candidature spontanée personnalisée (relayée par La Bonne Alternance ou par e-mail). |
| **Boîte mail** (`inbox`) | Lecture des réponses sur Gmail, y compris dans les spams : entretien, refus, retour d'e-mail. Les candidatures sont mises à jour et tu es prévenu en cas d'entretien. |
| **Entretiens** (`prep`) | Dès qu'une invitation arrive, une **fiche de préparation** est écrite : date, heure, lien ou adresse, l'entreprise, ce que le poste demande, tes exemples (méthode STAR), les questions probables et les questions à poser. |
| **Relances** (`followup`) | Relance polie dans le même fil de discussion, en jours ouvrés (2 au maximum, jamais après une réponse). |
| **Rapport de la semaine** (`report`) | Chaque lundi, bilan de la semaine écoulée : offres, candidatures, réponses, actions à faire, et un tableau prêt à recopier pour ton **justificatif de recherche d'emploi France Travail**. |

Tout est enregistré dans une base locale (`data/jobbot.db`) et visible dans le **tableau de bord**.

---

## 2. Installation (Windows, une seule fois)

Il te faut Python 3.12, Google Chrome et, pour les textes rédigés par l'IA, Claude Code (commande `claude`).
Pour faire tourner jobbot jour et nuit sur un petit serveur Linux sans écran, installe-le d'abord sur le PC, puis
suis le § 12 (déploiement en une commande).

```powershell
cd C:\chemin\vers\jobbot
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dashboard,dev]"
.venv\Scripts\python -m playwright install chromium
# optionnel, pour la source Indeed : python-jobspy impose pandas 2 / numpy 1.26, donc dans un venv séparé
py -3.12 -m venv .venv-jobspy
.venv-jobspy\Scripts\python -m pip install python-jobspy
```

jobbot détecte `.venv-jobspy` tout seul (ou le Python indiqué par la variable d'environnement `JOBSPY_PYTHON`).
Sur un serveur Linux, voir § 3.7 (`.venv-jobspy/bin/python`).

La commande `jobbot` est ensuite disponible dans `.venv\Scripts\` : active l'environnement avec
`.venv\Scripts\Activate.ps1`, ou lance `.venv\Scripts\jobbot.exe ...` directement.

**IA :** par défaut jobbot utilise Claude Code, c'est-à-dire ton abonnement (`llm.provider: claude_cli`). Si la
commande `claude` est absente, jobbot passe à des modèles de texte intégrés, moins personnalisés. Pour utiliser une clé
API à la place : `jobbot config set llm.provider anthropic`, puis ajoute `ANTHROPIC_API_KEY` dans `.env`.

---

## 3. Configuration

### 3.1 Préparer le dossier

```powershell
jobbot init     # crée data/, output/, .env (copie de .env.example), config/profile.yaml (copie du modèle
                # fictif config/profile.example.yaml) et la base de données — n'écrase rien
jobbot setup    # assistant : zone, contrats, quotas, relances, notifications, comptes
```

L'assistant pose une quarantaine de questions, en sept sections :

| Section | Ce qu'on y règle |
|---|---|
| **Recherche** | Contrats (alternance, CDI…), rayon autour de Metz, Nancy oui/non, Luxembourg oui/non et contrats acceptés là-bas, télétravail complet, déménagement et régions acceptées, expérience maximum exigée, prétentions salariales (France et Luxembourg). |
| **Automatisation** | Mode `review` ou `auto`, jours de rodage, seuils de score, quotas LinkedIn / HelloWork / La Bonne Alternance / e-mail. |
| **Candidatures spontanées** | Quota par jour, écrire ou non à des personnes nommées, chercher les RH et responsables techniques, adresses devinées autorisées ou non et leur maximum par jour. |
| **Relances** | Délais des deux relances et leur nombre. |
| **Notifications** | Annonces vocales Jarvis, rapport de la semaine (et son envoi par e-mail), fiche d'entretien automatique, notifications importantes par e-mail, sur le téléphone (ntfy) ou par Telegram (voir § 12.6). |
| **Comptes** | Mot de passe d'application Gmail, identifiants France Travail, clés La Bonne Alternance / Hunter / Brave / Adzuna / Jooble. |
| **Offres de l'école (Classroom)** | Surveillance du Classroom de l'école oui/non, candidature immédiate oui/non, fréquence de vérification, lien du cours. |

**Entrée** garde la valeur affichée entre parenthèses. Les mots de passe sont saisis masqués ; laisse vide pour garder
celui qui est déjà enregistré. Tu peux refaire une seule section, par exemple `jobbot setup --section Comptes`. Tant
que l'assistant n'a pas été terminé, jobbot le propose au lancement de n'importe quelle commande (sauf dans une tâche
planifiée : il continue alors avec les réglages actuels et l'écrit dans le journal).

Pour lire ou changer un réglage précis (n'importe quelle clé de `settings.yaml`, ou de `profile.yaml` avec le préfixe
`profile.`) :

```powershell
jobbot config show                    # tout (mots de passe : ✓ présent / ✗ absent, jamais la valeur)
jobbot config show Automatisation
jobbot config get limits.linkedin_per_day
jobbot config set limits.linkedin_per_day 10
jobbot config set mode review         # review : tu valides tout / auto : envoi automatique au-dessus du seuil
jobbot config set loc.luxembourg oui  # ajoute la zone Luxembourg (CDI seulement par défaut)
jobbot config set profile.target.min_salary_eur 32000
jobbot config set outreach.allow_unverified_guesses false
jobbot config set reports.weekly_email true
jobbot config set SMTP_PASSWORD "abcd efgh ijkl mnop"   # secret : écrit dans .env, jamais affiché
```

Les réglages sont vérifiés avant d'être écrits dans `config/settings.yaml` ou `config/profile.yaml` (les commentaires
sont gardés, et l'ancienne version est sauvegardée en `.bak`) : une valeur refusée (`reports.weekly_report_day 9`,
`mode yolo`…) affiche une erreur en français et rien n'est modifié. Les secrets vont dans `.env`.

**Réglages propres à une machine.** Un fichier optionnel `config/settings.local.yaml` (ignoré par git) est fusionné
par-dessus `config/settings.yaml`, clé par clé : c'est là qu'un serveur met `headless: true`,
`browser_channel: null`, ses notifications ou son fournisseur d'IA, sans toucher au fichier versionné.
`jobbot config set` écrit dans `settings.yaml`, sauf si la clé existe déjà dans `settings.local.yaml` : elle y est
alors modifiée (sinon le changement n'aurait aucun effet, le fichier local étant prioritaire).
`jobbot config get` affiche toujours la valeur effective.

### 3.2 Les clés du fichier `.env`

| Clé | À quoi elle sert | Où l'obtenir |
|---|---|---|
| `SMTP_USER`, `SMTP_PASSWORD` | Envoi des e-mails et lecture des réponses (Gmail) | `SMTP_USER` = ton adresse Gmail. `SMTP_PASSWORD` = un **mot de passe d'application** de 16 caractères, pas ton mot de passe Google : https://myaccount.google.com/apppasswords (la validation en 2 étapes doit être activée). |
| `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | Offres France Travail | https://francetravail.io → Espace développeur → créer une application → s'abonner à l'API « Offres d'emploi v2 ». |
| `LBA_API_TOKEN` | Optionnel : API officielle de La Bonne Alternance, en plus du site | Compte gratuit sur https://api.apprentissage.beta.gouv.fr. Il n'est **pas** nécessaire pour postuler via La Bonne Alternance. |
| `HUNTER_API_KEY`, `BRAVE_API_KEY` | Optionnel : vérifier des adresses e-mail, trouver le site d'une entreprise | hunter.io, brave.com/search/api |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Optionnel : offres de l'agrégateur Adzuna (§ 3.7) | Gratuit : https://developer.adzuna.com → Register, puis le tableau de bord (« Application ID », « Application Key »). |
| `JOOBLE_API_KEY` | Optionnel : offres de l'agrégateur Jooble (§ 3.7) | Gratuit sur demande : https://fr.jooble.org/api/about (clé française). |
| `CLASSROOM_COURSE_URLS` | Optionnel : le(s) cours Google Classroom où ton école publie des offres (liens séparés par des virgules) | Ouvre le cours dans ton navigateur et copie l'adresse `https://classroom.google.com/c/…` (voir § 3.6). |
| `CLAUDE_CODE_OAUTH_TOKEN` | Optionnel : l'IA de ton abonnement Claude sur un serveur sans écran | Sur le PC : `claude setup-token` (voir § 12.2). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optionnel : notifications Telegram | Jeton donné par @BotFather et identifiant de ta conversation avec le bot. |
| `SERVER_ROOT_HOST_ALIAS`, `PROJECT_SSH_HOST_ALIAS`, `PROJECT_USER`, `SERVER_IP_TAILSCALE` | Déploiement sur un serveur Linux (jamais copiées sur le serveur) | Voir § 12.1. |

Sans une clé, la fonction concernée est désactivée avec un message ; le reste continue.

### 3.3 Ton profil : la seule source de vérité

`config/profile.yaml` contient ton identité, tes expériences, tes projets, tes compétences, ton alternance et les
réponses types aux formulaires (disponibilité, prétentions…). **Les CV et lettres ne contiennent que ce qui est
écrit ici.** Ils sont reformulés et réordonnés, jamais inventés. Plus ce fichier est complet, meilleur est le ciblage.

Sur une installation neuve, `config/profile.yaml` n'existe pas : `jobbot init` y copie le **modèle fictif**
`config/profile.example.yaml` (commenté section par section). Remplace tout son contenu par le tien, à la main
(`copy config\profile.example.yaml config\profile.yaml` si tu ne passes pas par `init`) ou en partant d'un CV
existant :

```powershell
jobbot import-cv C:\chemin\mon_cv.pdf   # crée config/profile.imported.yaml : relis-le, puis copie-le en config/profile.yaml
```

`config/profile.yaml` et `config/profile.imported.yaml` sont ignorés par git : tes données personnelles ne sont
jamais publiées.

### 3.4 Où chercher : Metz, Nancy, Luxembourg, déménagement, salaire

Les zones de recherche sont dans `profile.target.locations`. Chaque zone a son rayon et, si besoin, **ses propres
contrats** :

```yaml
locations:
  - {city: Metz, postal_code: "57000", radius_km: 40, latitude: 49.1193, longitude: 6.1757}
  - {city: Nancy, postal_code: "54000", radius_km: 25, latitude: 48.6921, longitude: 6.1844}
  - {city: Luxembourg, radius_km: 25, latitude: 49.6116, longitude: 6.1319, country: LU, contract_types: [cdi]}
```

- **Luxembourg.** Une alternance française au Luxembourg est quasi impossible (contrat, école et OPCO français) :
  par défaut on n'y cherche que des **CDI**. Chaque source interroge le Luxembourg à sa façon (France Travail par
  pays, Indeed Luxembourg, Free-Work, LinkedIn, WTTJ) ; La Bonne Alternance et l'APEC restent en France. Une
  alternance au Luxembourg est écartée avec la raison « contrats recherchés dans ce pays : CDI ». Pour y accepter
  aussi un CDD : `jobbot config set loc.luxembourg_contracts "[cdi, cdd]"` — ce contrat n'est alors cherché que là.
- **Déménagement.** `profile.target.willing_to_relocate: true` garde aussi les offres situées en France hors de tes
  zones, avec une note plus faible. `relocation_regions` limite aux régions voulues (ex. `[Grand Est, Alsace,
  Île-de-France]` ; vide = toute la France). Jamais à l'étranger.
- **Salaire minimum.** `profile.target.min_salary_eur` (brut annuel) écarte les offres dont le haut de la fourchette
  est plus bas. Les formats « 35 k€ », « 2 800 € par mois sur 13 mois », « 15 €/h » sont convertis. Ne s'applique
  jamais à une alternance, ni quand le salaire est absent ou illisible.

### 3.5 Connexion à LinkedIn et HelloWork (à la main, une fois)

```powershell
jobbot login linkedin
jobbot login hellowork
```

Une fenêtre Chrome s'ouvre et tu te connectes **toi-même**. jobbot ne tape jamais ton mot de passe, ne résout aucun
captcha et ne saisit aucun code reçu. La session est gardée dans `data/browser-profile/`. Si LinkedIn demande plus tard
une vérification, jobbot s'arrête, fait une capture d'écran et te demande de relancer `jobbot login`.

### 3.6 Offres de l'école (Google Classroom)

Ton école publie des offres d'alternance dans un cours Google Classroom ? jobbot relit le flux du cours toutes les
15 minutes et postule **tout de suite** aux offres adaptées, pour que tu sois parmi les premiers candidats.

**Mise en place (une fois)**

```powershell
jobbot setup --section classroom   # active la surveillance, colle le lien du cours (https://classroom.google.com/c/…)
jobbot login classroom             # une fenêtre Chrome s'ouvre : connecte-toi à ton compte Google de l'école
jobbot classroom test              # lit les dernières publications et montre ce que jobbot ferait (n'envoie rien)
jobbot schedule install            # ajoute la tâche « jobbot-classroom » (toutes les classroom.poll_minutes minutes)
```

- Le lien du cours est gardé dans `.env` (`CLASSROOM_COURSE_URLS`, plusieurs liens séparés par des virgules) et
  n'est jamais affiché (`jobbot doctor` indique seulement ✓ / ✗).
- La connexion Google se fait **à la main**, dans un profil de navigateur séparé (`data/browser-profile-classroom/`,
  jamais celui de LinkedIn / HelloWork). jobbot ne tape ni mot de passe ni code de validation. Si Google redemande
  la connexion, tu reçois une notification « reconnecte-toi : jobbot login classroom ».
- Sur une machine sans écran, la lecture du flux se fait en mode invisible ; la connexion initiale doit être faite
  sur un poste avec écran.

**Comment ça marche**

1. Chaque nouvelle publication (ou publication modifiée) est lue en entier : texte (« Voir plus » compris), liens,
   pièces jointes Google Docs / Drive (PDF, Word). Les liens vers HelloWork, LinkedIn, La Bonne Alternance, Indeed,
   Welcome to the Jungle, France Travail… sont lus par la source correspondante pour avoir l'annonce complète.
2. L'IA repère les offres (une publication peut en contenir plusieurs) ; rappels, réunions et informations
   générales sont ignorés. Une adresse e-mail ou un lien absent du texte n'est jamais inventé.
3. Chaque offre est notée comme les autres, avec trois différences : le filtre « fausse offre d'école » ne
   s'applique pas, l'offre reçoit un bonus (« offre relayée par l'école », `classroom.school_bonus`, +5), et une
   offre qui ne vise que des niveaux inférieurs à ta formation (BTS / Bac+2, CAP…) ou dont la date limite est
   passée est écartée avec la raison.
4. Offre adaptée : CV et lettre ciblés — la lettre et l'e-mail précisent « J'ai pris connaissance de votre offre par
   l'intermédiaire de <ton école> » — puis candidature immédiate : par e-mail si une adresse est donnée (y compris
   celle de l'école quand elle centralise les CV), sinon par LinkedIn / HelloWork / La Bonne Alternance si le lien y
   mène, sinon « à faire à la main » avec le lien.
5. Une notification te dit pour chaque offre : candidature envoyée, à valider, à faire toi-même (avec le lien) ou
   écartée (raison courte). Une publication n'est traitée qu'une fois.

**Réglages** (`jobbot config set classroom.<clé> <valeur>`)

| Clé | Par défaut | Effet |
|---|---|---|
| `classroom.enabled` | `true` | Surveillance active. |
| `classroom.auto_apply` | `true` | Postuler sans attendre, **même pendant les jours de rodage**. Restent obligatoires : le score minimum, les quotas, la liste noire, « pas deux candidatures à la même entreprise en 60 jours » et le contrôle de sincérité du CV. `mode: review` (choix explicite) fait tout passer « à valider ». |
| `classroom.min_score` | vide | Score minimum pour postuler seul (vide = `thresholds.auto_apply_min_score`). |
| `classroom.poll_minutes` | 15 | Fréquence de la tâche planifiée (relance `jobbot schedule install` après un changement). |
| `classroom.active_hours` | `07:30-21:00` | En dehors de cette plage, la vérification s'arrête tout de suite. |
| `classroom.school_bonus` | 5 | Points ajoutés au score d'une offre relayée par l'école. |
| `classroom.max_posts` | 30 | Publications lues dans le flux à chaque passage. |
| `classroom.backend` | `browser` | `api` : API Google Classroom officielle (optionnelle, voir ci-dessous). |

Commandes : `jobbot classroom watch` (surveillance continue, Ctrl+C pour arrêter ; `--once` : un seul passage ;
`--interval 5` : toutes les 5 minutes), `jobbot classroom test [-n 10]` (aperçu, ne postule jamais). `jobbot run`
commence aussi par une vérification du Classroom.

**Mode API (optionnel).** Si tu préfères l'API officielle au navigateur : crée dans Google Cloud un identifiant OAuth
« Application de bureau » avec les API Classroom et Drive activées, enregistre son JSON sous
`config/google_oauth_client.json`, installe `google-auth-oauthlib` (`.venv\Scripts\pip install -e ".[classroom]"`),
puis `jobbot config set classroom.backend api` et `jobbot login classroom` (autorisation dans le navigateur ; le
jeton va dans `data/classroom_token.json`). Ces deux fichiers sont ignorés par git.

### 3.7 Sources d'offres en plus : Adzuna, Jooble, pages carrières, Indeed

Ces sources sont dans `search.sources` par défaut. Sans leur clé, une source est simplement ignorée ;
`jobbot doctor` affiche pour chacune « disponible » ou la raison (clé absente, module non installé…).

**Adzuna** (`adzuna`) — agrégateur d'offres, API officielle gratuite. Il faut `ADZUNA_APP_ID` et `ADZUNA_APP_KEY` :
crée un compte sur https://developer.adzuna.com (« Register »), puis copie l'« Application ID » et
l'« Application Key » du tableau de bord (`jobbot setup --section Comptes` ou directement dans `.env`). jobbot
cherche autour de chacune de tes villes (rayon du profil), 24 recherches au plus par exécution (l'offre gratuite
permet environ 250 requêtes par jour). Adzuna ne couvre **pas** le Luxembourg : ces recherches-là sont sautées.
Adzuna ne publie qu'un extrait de l'annonce ; le lien mène à l'offre d'origine, où tu postules toi-même.

**Jooble** (`jooble`) — autre agrégateur, API officielle gratuite sur demande. Il faut `JOOBLE_API_KEY` : remplis
le formulaire « Get API key » sur https://fr.jooble.org/api/about (voir aussi https://jooble.org/api/about). Chaque
pays a sa propre clé : demande la clé **française** (seules les recherches en France sont faites). 10 recherches au
plus par exécution ; comme pour Adzuna, l'annonce est un extrait et la candidature se fait sur le site d'origine.

**Pages carrières des entreprises** (`company_ats`) — aucune clé. jobbot lit les offres publiées par les
employeurs qu'il connaît déjà : les entreprises de la base (page carrières trouvée pendant `enrich`) et celles des
offres collectées dont le lien de candidature mène à un logiciel de recrutement connu (Lever, Greenhouse, Recruitee,
SmartRecruiters, Workable, Teamtailor, page entreprise Welcome to the Jungle). Une page carrières sans logiciel
connu est lue pour ses offres au format schema.org (JSON-LD). Seuls les postes de développeur (ni commerciaux ni
seniors) près de tes villes, au Luxembourg si tu l'as choisi, ou en télétravail complet sont gardés. Politesse :
80 pages au plus par exécution (`search.max_ats_boards_per_run`), chaque page au plus une fois toutes les 20 h (et
de plus en plus rarement si elle reste en erreur), une requête par seconde au plus par logiciel ; seules les
adresses publiques sont lues (jamais une adresse interne, même après une redirection). La candidature se fait sur la page de l'offre (« à finir à la main »,
`jobbot assist` pré-remplit le formulaire).

**Indeed** (`indeed`) — lu avec `python-jobspy`, installé dans un environnement séparé `.venv-jobspy` à la racine du
projet (jobbot le trouve seul, sous Windows comme sous Linux, ou via la variable `JOBSPY_PYTHON`). Sur un serveur
Linux : `uv venv .venv-jobspy --python 3.12 && uv pip install --python .venv-jobspy/bin/python python-jobspy`.

---

## 4. Premier lancement : la checklist

1. **`jobbot doctor`** vérifie Python, les dépendances, Claude, le navigateur, la présence des clés `.env` (jamais leur
   valeur) et les sources. `jobbot doctor --smtp` teste aussi la connexion Gmail sans envoyer d'e-mail.
2. **`jobbot --dry-run run`** fait un essai à blanc de toute la journée. Les formulaires sont remplis sans être envoyés,
   et les e-mails sont écrits dans `output/outbox/*.eml`. Tu peux les ouvrir avec Outlook ou Thunderbird pour les
   relire. Les CV et lettres générés sont dans `output/<date>/…`.
3. **Mode validation.** Pendant les `warmup_days` premiers jours après l'assistant (3 par défaut), jobbot reste en
   mode **review**, même si tu as choisi `auto`. Tout part dans la file « À valider ». Valide dans le tableau de bord
   (`jobbot dashboard`, page « À valider ») ou avec `jobbot approve <id>` / `jobbot approve --all` /
   `jobbot reject <id>`. Les offres validées partent au prochain `jobbot apply` ou `jobbot run`.
4. **Mode auto.** Après la période de rodage, les offres notées au moins `thresholds.auto_apply_min_score` (60) partent
   seules. Les autres restent « à valider ». Tu peux revenir en arrière à tout moment : `jobbot config set mode review`.
   Même en mode auto, un CV qui porte une « alerte de vérité » (voir § 9) n'est **jamais** envoyé sans ta validation.

---

## 5. Utilisation au quotidien

```powershell
jobbot run                 # journée complète : collecte → tri → CV/lettres → candidatures → spontanées → mails → relances
jobbot night               # nuit : entreprises + adresses, priorités, dossiers du lendemain (rien n'est envoyé)
jobbot status              # résumé : offres, candidatures, quotas du jour, files d'attente, dernière nuit
jobbot assist              # offres « à finir à la main » : ouvertes et pré-remplies, tu cliques « Envoyer »
jobbot prep 12             # fiche de préparation de l'entretien de la candidature n° 12
jobbot report              # rapport de la semaine en cours (--precedente : semaine dernière complète)
jobbot dashboard           # tableau de bord dans le navigateur
jobbot export              # Excel (ou --format csv) de tout le suivi
```

Pour que jobbot tourne tout seul chaque jour :

```powershell
jobbot schedule install    # 09:30 : jobbot run · 18:00 : réponses + relances · Classroom toutes les 15 min · 00:30 : nuit
jobbot schedule show
jobbot schedule remove
```

La tâche `jobbot-classroom` (si `classroom.enabled`) lance `jobbot classroom watch --once` toutes les
`classroom.poll_minutes` minutes, sans fenêtre ; son journal est dans `data\logs\jobbot.log`.

Sous Linux (serveur), les mêmes commandes gèrent ta **crontab** : quatre lignes marquées `# jobbot:run`,
`# jobbot:soir`, `# jobbot:classroom` et `# jobbot:night` (les autres lignes ne sont jamais touchées), chacune
protégée par `flock` contre les doubles lancements, sortie dans `data/logs/cron.log` (voir § 12.7). La nuit tourne
avec une priorité basse (`nice -n 10 ionice -c3`) ; `--nuit 01:00` change son heure, `night.enabled: false` la
retire. Sous Windows, la tâche s'appelle `jobbot-nuit`.

### La nuit : `jobbot night`

Le serveur ne fait rien la nuit : `jobbot night` (planifiée à 0 h 30) s'en sert pour remplir la file des candidatures
spontanées. **Rien n'est jamais envoyé la nuit.** Dans l'ordre, chaque étape isolée (une erreur n'arrête pas les
suivantes) et l'ensemble arrêté proprement à `night.until` (06:30) ou après `--budget MINUTES` :

1. **Rétention RGPD** : les contacts nominatifs des entreprises jamais contactées sont supprimés au bout de
   `night.retention_days` (365) jours ; la source de chaque adresse reste notée (`raw.source_urls`).
2. **Collecte** : toutes les entreprises du numérique autour de chacune de tes zones (rayon + `night.extra_radius_km`,
   50 km au plus), les « recruteurs » La Bonne Alternance, et **les employeurs des offres déjà en base** (même
   écartées pour l'expérience : une entreprise qui recrute des développeurs est une excellente cible pour une
   alternance), identifiés dans le registre avec une correspondance de nom sûre ; au Luxembourg (pas de SIREN), par
   leur nom. Tout passe par le filtre « hors cible ».
3. **Contacts** : sites, pages carrières, personnes, format d'adresse, sur `night.workers` (4) fils, jamais deux à la
   fois sur le même site ; résumés du site par le modèle rapide, `night.max_llm_summaries` (80) au plus.
4. **Priorités** (0-100, raisons en français) : numérique cœur +30 / proche +15 / grande structure +10, offre de
   développeur publiée depuis moins de 60 jours +25, adresse recrutement/RH +20, RH ou responsable technique nommé
   +15, adresse devinée d'après le format appris +5, devinée non vérifiée -10 (sans adresse : hors file), à 20 km
   +10 / 40 km +5, 10 à 249 salariés (tuteur possible) +10, tes compétences sur son site jusqu'à +10. La campagne
   du jour prend les entreprises **par priorité décroissante**.
5. **Dossiers du lendemain** : CV + lettre pour les premières de la file (plafond du jour + `night.prepare_margin`) ;
   la campagne du matin les réutilise pendant 7 jours au lieu de les régénérer.

Au réveil, une notification (Telegram / e-mail si activés) : « Nuit : +X entreprises (Y pertinentes, N nouvelles),
+Z adresses (dont N nominatives), D dossiers prêts, file d'attente : Q entreprises ≈ J jours d'envoi ». `jobbot
status`, `jobbot doctor` et la page « Entreprises » du tableau de bord montrent la file et la dernière nuit.
`jobbot night --dry-run --budget 30` : essai sans génération de dossiers.

**Montée progressive des envois spontanés** (`limits.spontaneous_ramp`, la vraie limite est la délivrabilité) :
20 par jour au départ, +5 par semaine complète depuis le premier envoi réel, 40 au plus ; si plus de 5 % des envois
spontanés des 7 derniers jours rebondissent, retour à 20 (tu es prévenu) et la montée reprend une semaine plus tard.
`limits.spontaneous_ramp.enabled: false` revient au quota fixe `limits.spontaneous_per_day`.

Le script `scripts\install_task.ps1` fait la même chose ; il accepte `-Matin 08:45 -Soir 18:30` et `-Supprimer`. Les
tâches tournent quand ta session Windows est ouverte, parce que LinkedIn et HelloWork ont besoin du navigateur. Si le PC
était éteint à l'heure prévue, elles démarrent dès que possible. La sortie de chaque lancement est dans
`data\logs\tache-*.log`. Le lundi, le premier lancement écrit aussi le rapport de la semaine (voir § 6) ; une fiche
d'entretien est écrite dès qu'une invitation est lue.

### Toutes les commandes

| Commande | Rôle |
|---|---|
| `init` · `doctor [--smtp]` · `setup [--section X]` | Installation, diagnostic, assistant |
| `config show [section]` · `config get <clé>` · `config set <clé> <valeur>` | Réglages |
| `import-cv <pdf>` · `login linkedin\|hellowork\|wttj\|classroom` | Profil depuis un CV, connexion manuelle |
| `session export [site\|all]` · `session import [chemin]` | Sessions de connexion : export depuis le PC, import sur un serveur sans écran (§ 12) |
| `classroom watch [--once] [--interval N]` · `classroom test [-n 10]` | Offres de l'école (Google Classroom) : surveillance et candidature immédiate, aperçu sans envoi |
| `search [-s source]` · `score` · `prepare [-n 10] [-j id]` | Collecte, tri, CV et lettres |
| `ats <id> [--cv autre.pdf]` | Explique le score ATS : mots-clés présents ou manquants, alertes |
| `apply [-c canal] [-n N] [-j id]` | Candidatures. Canaux : `linkedin`, `hellowork`, `lba`, `email`, `external`. `-j id` vaut validation. |
| `assist [-n 10]` | Candidatures externes pré-remplies. Réponds `o` (envoyée), `n` (abandon), `passer` ou `q`. |
| `approve [ids] [--all]` · `reject <ids> [--blacklist]` | File de validation (offres et entreprises) |
| `discover` · `enrich` · `spontaneous` | Candidatures spontanées |
| `inbox` · `followup` | Réponses et relances |
| `prep <id candidature>` | Fiche de préparation d'entretien (régénérée si elle existe) |
| `report [--precedente] [--date AAAA-MM-JJ] [--email]` | Rapport de la semaine ; `--email` te l'envoie aussi par e-mail |
| `run [-s étape]` | Journée complète, ou seulement certaines étapes : `classroom`, `collect`, `score`, `prepare`, `apply`, `discover`, `enrich`, `spontaneous`, `inbox`, `followup` |
| `night [--budget MINUTES] [--dry-run]` | Nuit : collecte d'entreprises et d'adresses, priorités, dossiers du lendemain ; rien n'est envoyé |
| `status` · `export` · `dashboard [--host IP] [--port N]` · `schedule install\|remove\|show` | Suivi |

Options communes : `--dry-run` (simulation), `-v` (journal détaillé), `--root <dossier>`.

---

## 6. Entretiens et rapport de la semaine

### Fiche de préparation d'entretien

Quand `inbox` reconnaît une invitation à un entretien (réponse d'un recruteur ou notification HelloWork / LinkedIn /
La Bonne Alternance), jobbot te prévient et écrit `output/entretiens/<date>-<entreprise>.md` :

- **logistique** relevée dans l'e-mail : date, heure, lien Meet / Teams / Zoom, adresse, téléphone du recruteur.
  Elle n'est jamais devinée : si l'e-mail ne la donne pas, la fiche te dit de vérifier l'original ;
- l'entreprise en quelques lignes, ce que le poste demande : ce que ton profil couvre, et ce qui est **« à réviser »**
  (compétences de l'offre absentes de ton profil, jamais présentées comme acquises) ;
- tes exemples au format **STAR**, tirés uniquement de tes expériences et projets réels ;
- les questions probables (technique, alternance, motivation) avec des pistes de réponse, les questions à poser et
  une check-list de la veille.

Pour la générer ou la refaire à la main : `jobbot prep <id>` ou le bouton de la page « Entretiens » du tableau de
bord. Pour désactiver la fiche automatique : `jobbot config set reports.interview_prep false`.

### Rapport de la semaine

Chaque lundi (réglage `reports.weekly_report_day`, 0 = lundi … 6 = dimanche), le premier `jobbot run` écrit
`output/rapports/semaine-AAAA-SS.md` sur la semaine écoulée ; s'il n'a pas tourné le lundi, il est rattrapé les deux
jours suivants, une seule fois par semaine. Le rapport contient :

- les offres trouvées et gardées par source, les candidatures par canal, les taux de réponse ;
- les réponses de la semaine et les entretiens ;
- ce qui t'attend : candidatures à finir à la main, à valider, relances manuelles, relances des 7 prochains jours ;
- un tableau **« Justificatif de recherche d'emploi (France Travail) »** avec la date, l'entreprise, le poste et le
  canal de chaque candidature.

`jobbot report` le produit à la demande (`--precedente`, `--date 2026-09-27`). Pour le recevoir aussi par e-mail :
`jobbot config set reports.weekly_email true` (ou `jobbot report --email`). Pour l'arrêter :
`jobbot config set reports.weekly_report false`.

---

## 7. Le tableau de bord

`jobbot dashboard` ouvre http://localhost:8501 (`--port 8600` pour un autre port). Il écoute sur `127.0.0.1` :
`--host` permet de le mettre sur l'IP d'un VPN, jamais sur `0.0.0.0` (refusé : le tableau de bord n'a pas de mot de
passe). Sur un serveur, voir § 12.5. Les pages :

- **Vue d'ensemble** : chiffres clés (offres, candidatures, réponses, entretiens, relances dues), offres par statut,
  derniers événements.
- **Offres** : filtres par statut, source, score ou texte ; liens vers l'offre et la page de candidature ; détail du
  score ATS ; **téléchargement du CV et de la lettre**.
- **À valider** : boutons Valider / Écarter pour les offres et les candidatures spontanées. Pour une spontanée, tu
  vois l'adresse choisie, son type (recrutement, RH, responsable technique, dirigeant…), la personne et si l'adresse
  est **devinée**, ainsi que le résumé de l'entreprise.
- **Actions manuelles** : candidatures externes à envoyer toi-même (lien, documents, bouton « J'ai postulé ») et
  relances à faire à la main (candidatures sans e-mail, par exemple via LinkedIn ou La Bonne Alternance).
- **Candidatures** : suivi complet et historique (y compris « Nouvel essai (autre adresse) » après un rebond).
- **Entretiens** : les fiches de préparation, à lire ou télécharger, et le bouton « Générer / Régénérer la fiche ».
- **Entreprises** : entreprises trouvées pour les candidatures spontanées, leur statut, la meilleure adresse et son
  type, le nombre de personnes repérées.
- **Sources** : volume, offres retenues et candidatures par site.
- **Réglages** : les mêmes questions que `jobbot setup`, section par section. Les mots de passe sont masqués ; laisse
  le champ vide pour ne pas les changer.

---

## 8. Comment chaque offre est traitée

| Canal | Fonctionnement | Quota par défaut |
|---|---|---|
| **LinkedIn — candidature simplifiée** | Navigateur connecté : envoi du CV, réponses aux questions à partir de ton profil, décoche « suivre l'entreprise ». Si une question est inconnue, l'offre passe « à finir à la main ». | 15 / jour |
| **HelloWork** | Candidature sur le site quand tu es connecté : CV et message tiré de la lettre (2 900 caractères au plus). Si l'offre renvoie vers le site du partenaire, elle passe « à la main ». | 10 / jour |
| **La Bonne Alternance** | « Candidature simplifiée » : CV et lettre transmis au recruteur par La Bonne Alternance, **une seule fois par offre et par entreprise (SIRET)**. Pour les « recruteurs LBA » sans offre, c'est aussi le canal des candidatures spontanées. | 20 / jour |
| **E-mail** | Pour les offres qui donnent une adresse : message court, CV et lettre en pièces jointes. | 30 / jour |
| **Site de l'employeur** | Workday, Teamtailor, WTTJ, Indeed… Documents prêts, formulaire pré-rempli par `jobbot assist`, **envoi par toi**. | — |
| **Spontanées par e-mail** | Une adresse par entreprise, choisie dans cet ordre : adresse de recrutement (`recrutement@`, `rh@`) > RH nommé > responsable technique > dirigeant (petite entreprise seulement) > `contact@` > autre personne. Le message commence par « Bonjour Prénom Nom, » pour une personne, sinon « Madame, Monsieur, », et indique où l'adresse a été trouvée et comment ne plus être contacté. Voir ci-dessous. | 20 / jour |
| **Relances** | 7 jours ouvrés après la candidature, puis 8 jours ouvrés plus tard, dans le même fil. Sans réponse après 30 jours, la candidature est close. | 10 / jour |

Autres règles :

- jamais deux candidatures à la même entreprise en moins de 60 jours ;
- pause de 40 à 140 s entre deux envois ;
- les entreprises en liste noire (réponse « STOP », adresse invalide, `reject --blacklist`) ne sont plus jamais
  contactées.

### Candidatures spontanées nominatives : les garde-fous

Pour trouver les bonnes personnes, `enrich` lit le registre public des entreprises (dirigeants personnes physiques,
jamais le commissaire aux comptes), les pages « équipe » / « à propos » du site, Hunter (si tu as une clé) et les
**titres** des résultats de recherche publics « Prénom Nom - Poste - Entreprise ». Les profils LinkedIn ne sont
jamais ouverts. Le format d'adresse du domaine (`prenom.nom@`, `pnom@`…) est appris d'Hunter ou des adresses
publiées sur le site.

| Réglage | Par défaut | Effet |
|---|---|---|
| `outreach.allow_nominative` | `true` | `false` : uniquement des adresses de fonction (`recrutement@`, `rh@`, `contact@`), jamais celle d'une personne. |
| `outreach.allow_employee_search` | `true` | `false` : pas de recherche de salariés (pages équipe, moteurs de recherche, personnes Hunter) ; restent les adresses publiées sur le site et les dirigeants du registre. Cette recherche n'a lieu que si aucune adresse de recrutement n'a été trouvée. |
| `outreach.allow_unverified_guesses` | `true` | `false` : une adresse devinée n'est utilisée que si Hunter l'a vérifiée. |
| `outreach.max_unverified_per_day` | 8 | Nombre maximum d'envois par jour à des adresses devinées non vérifiées. |
| `outreach.max_bounce_rate` | 0,08 | Au-delà de 8 % de rebonds sur 30 jours, les adresses devinées sont **mises en pause** (l'adresse vérifiée suivante est utilisée, sinon l'entreprise attend). |
| `outreach.max_attempts_per_company` | 2 | Après un rebond, l'adresse suivante est essayée (nouvelle candidature, mêmes documents), dans cette limite. Une adresse en échec n'est jamais réutilisée. |
| `outreach.preferred_roles` | rh, recrutement, cto… | Ordre de préférence des fonctions à contacter. |

Une adresse devinée est signalée comme telle dans le suivi (« devinée, non vérifiée ») et dans le message (« J'ai
déduit votre adresse de votre nom, relevé sur …, et du format d'adresse de votre domaine »). Les dirigeants
d'entreprises de 50 salariés ou plus passent en dernier.

---

## 9. Sécurité, quotas et cadre légal — à lire

- **LinkedIn.** Les conditions d'utilisation (§ 8.2) interdisent les robots, et automatiser la candidature simplifiée
  les enfreint quel que soit le volume. Le risque réel est une **restriction de ton compte**, avec vérification
  d'identité. jobbot limite ce risque sans le supprimer :
  - quota bas (15/jour, alors que LinkedIn tolère environ 50) et pauses « humaines » ;
  - arrêt immédiat en cas de vérification ou de limite ;
  - aucune connexion automatisée ;
  - aucune collecte de profils ni d'e-mails.

  Pour désactiver ce canal : `jobbot config set limits.linkedin_per_day 0`.
- **HelloWork.** Le fichier robots.txt et les CGU interdisent l'aspiration du site : la recherche se limite à quelques
  requêtes par jour, avec au moins 5 secondes entre chacune. La meilleure source reste **les alertes e-mail
  HelloWork**, que jobbot lit dans ta boîte (`inbox`). La candidature ne se fait que connecté ; les captchas et codes
  restent pour toi.
- **Indeed et Welcome to the Jungle.** Leurs conditions interdisent les robots dans le parcours de candidature :
  lecture seule, puis candidature par toi avec `assist`, qui pré-remplit sans jamais envoyer.
- **La Bonne Alternance.** jobbot utilise les échanges internes du site, qui peuvent changer sans préavis : dans ce
  cas, l'offre passe « à la main ». Une seule candidature par offre et par entreprise, 20 par jour au plus. Les CGU
  exigent des informations exactes : une fausse déclaration relève de l'article 441-1 du Code pénal.
- **Gmail.** jobbot utilise un mot de passe d'application ; si tu changes ton mot de passe Google, il faut en recréer
  un. Pendant les 2 premières semaines, reste vers **20 e-mails spontanés par jour** : c'est la réputation de ton
  adresse qui compte, pas la limite de 500 par jour. jobbot n'utilise ni pixel de suivi ni lien raccourci, et joint un
  seul PDF (1 Mo maximum conseillé).
- **RGPD / CNIL (candidatures spontanées).**
  - Chaque e-mail indique la source de l'adresse (article 14 du RGPD) et comment ne plus être contacté (« STOP »).
    Une réponse de refus ou de désinscription ajoute automatiquement l'adresse à la liste noire.
  - L'adresse de recrutement passe toujours avant une personne ; une seule personne par entreprise et par essai.
  - jobbot suit les règles B2B de la CNIL : on peut écrire sans consentement préalable à l'adresse professionnelle
    d'une personne si le message est lié à sa fonction, dit d'où vient l'adresse et permet de refuser simplement.
    Une adresse nominative reste une donnée personnelle : pour t'en tenir aux adresses de fonction,
    `jobbot config set outreach.allow_nominative false`.
  - jobbot ne récupère **jamais** d'e-mails sur LinkedIn et n'ouvre aucun profil : seuls les titres des résultats de
    recherche publics sont lus. La CNIL a infligé 240 000 € d'amende à KASPR en 2024 pour la collecte d'e-mails sur
    LinkedIn.
  - Garde la base de données pour toi. Supprime les contacts inutilisés après 6 mois, et ceux restés sans réponse
    après 12 mois.
- **Sincérité.** Le CV et la lettre sont vérifiés contre ton profil. Les « alertes de vérité » (compétence, chiffre,
  diplôme ou date absent du profil) s'affichent dans `jobbot ats` et dans le tableau de bord. En mode auto, une
  candidature dont le CV porte une alerte n'est **jamais** envoyée seule : elle passe « à valider » (offres,
  spontanées par e-mail et relais La Bonne Alternance). Pour ajouter une compétence, ajoute-la **d'abord** dans
  `profile.yaml` si elle est vraie. Les fiches d'entretien suivent la même règle.

---

## 10. Dépannage

| Problème | Solution |
|---|---|
| `doctor` : « N caractère(s) au lieu de 16 lettres » | Le mot de passe d'application Gmail fait toujours 16 lettres (les espaces ne comptent pas). Recopie-le ou recrée-le, puis `jobbot setup --section Comptes` et `jobbot doctor --smtp`. |
| « Info : adresses devinées en pause » | Trop de rebonds récents sur les adresses devinées : jobbot n'utilise plus que les adresses vérifiées jusqu'à ce que le taux redescende sous `outreach.max_bounce_rate`. |
| Pas d'offres au Luxembourg via France Travail | Il faut les clés France Travail ; le code pays est lu dans le référentiel de l'API. Sans lui, la recherche Luxembourg est sautée (jamais remplacée par une recherche en France). |
| `claude` introuvable | Installe ou connecte Claude Code, ou `jobbot config set llm.provider none` (textes modèles). |
| « Chromium (Playwright) absent » | `.venv\Scripts\python -m playwright install chromium` |
| Chrome ne démarre pas / « profil déjà utilisé » | Une autre exécution de jobbot tourne déjà (tâche planifiée ?). Attends ou ferme sa fenêtre. |
| « Une exécution de jobbot est déjà en cours » | Un verrou `data/jobbot.lock` est pris. Il est ignoré après 6 h ; supprime-le seulement si aucun jobbot ne tourne. |
| « session LinkedIn absente » / vérification de sécurité | `jobbot login linkedin`, puis connecte-toi à la main. En attendant, les offres LinkedIn restent en file. |
| Gmail `534 5.7.9` ou « identifiants refusés » | Crée un nouveau mot de passe d'application, puis `jobbot setup --section Comptes`. |
| Gmail `550 5.4.5` / `5.7.1` | Limite ou blocage anti-spam : les envois s'arrêtent pour la journée. Baisse les quotas. |
| France Travail : source désactivée | Clés absentes ou API « Offres d'emploi v2 » non souscrite sur francetravail.io. |
| Une offre est restée « Échec » | `jobbot apply -j <id>` la retente. `jobbot reject <id>` l'écarte. |
| « Google demande de te reconnecter à Classroom » | `jobbot login classroom` et connecte-toi à la main. `jobbot doctor` montre le résultat de la dernière vérification. |
| `classroom test` : « pas une offre » partout | Normal pour des rappels ou des informations. Sinon vérifie le lien du cours (`jobbot setup --section classroom`) et que le compte connecté est bien inscrit au cours. |
| La tâche planifiée ne tourne pas | `jobbot schedule show`, puis regarde `data\logs\tache-*.log`. La session Windows doit être ouverte. |
| Caractères bizarres dans la console | Utilise Windows Terminal : jobbot remplace les caractères non affichables au lieu de planter. |
| Le tableau de bord ne s'ouvre pas | `.venv\Scripts\pip install -e ".[dashboard]"`, ou change de port : `jobbot dashboard --port 8600`. |
| Serveur : « Connexion SSH impossible » | Voir § 12.8 (serveur allumé et joignable, alias du `~/.ssh/config`, clé autorisée avec `ssh-copy-id`). |
| Notification « Session à renouveler » | Une session LinkedIn / HelloWork a expiré : `jobbot login …` sur le PC, `jobbot session export all`, puis `python scripts/deploy_server.py --only private`. |

Le journal détaillé est dans `data/logs/jobbot.log`. Pour plus de détails dans la console, ajoute `-v`.

---

## 11. Où sont tes données ?

| Emplacement | Contenu |
|---|---|
| `config/profile.yaml` | Ton profil (source de vérité). Ignoré par git : **ne jamais le publier.** |
| `config/profile.example.yaml` | Modèle de profil fictif (Jean DUPONT), copié par `jobbot init` |
| `config/settings.yaml` | Réglages : mode, quotas, seuils, relances, sources |
| `config/settings.local.yaml` | Réglages propres à cette machine (serveur), prioritaires. Ignoré par git. |
| `config/deploy.local.yaml` | Détails de ton serveur pour le déploiement (§ 12.1). Ignoré par git. |
| `data/sessions/` | Sessions exportées (`jobbot session export`). **Ne jamais les partager.** |
| `.env` | Secrets : mot de passe Gmail, clés d'API. **Ne jamais le partager.** |
| `data/jobbot.db` | Offres, entreprises, candidatures, historique, liste noire |
| `data/browser-profile/` | Sessions LinkedIn / HelloWork. **Ne jamais le partager.** |
| `data/browser-profile-classroom/` | Session Google (Classroom de l'école), séparée. **Ne jamais la partager.** |
| `data/logs/` | Journaux et captures d'écran (`screens/`) |
| `output/<date>/<entreprise>-<poste>/` | CV (PDF + DOCX) et lettre de chaque candidature (`…-candidature-spontanee/` pour une spontanée) |
| `output/entretiens/` | Fiches de préparation d'entretien (Markdown) |
| `output/rapports/` | Rapports de la semaine (`semaine-AAAA-SS.md`) |
| `output/outbox/` | E-mails simulés (`--dry-run`) |

Pour développer : `.venv\Scripts\python -m pytest -q -m "not live"`. Les tests n'utilisent ni le réseau ni ton profil
(ils lisent le profil fictif `tests/fixtures/profile.yaml`) ; ceux qui interrogent les vrais sites sont marqués
`live`. L'architecture et le contrat des modules sont décrits dans
`docs/ARCHITECTURE.md`, et les recherches (sites, limites, droit) dans `docs/research/`.

---

## 12. Installation sur un serveur Linux (Debian)

jobbot peut tourner jour et nuit sur un petit serveur Debian sans écran : collecte, candidatures, Classroom,
boîte mail et relances continuent quand le PC est éteint. Tout se pilote **depuis le PC Windows**, en une
commande, dès que l'accès SSH par clé fonctionne.

### 12.1 Ce qu'il faut

- Un serveur Debian (12 ou 13) joignable en SSH depuis le PC, et deux **alias** dans ton `~/.ssh/config`
  (authentification par clé, jamais de mot de passe) : un pour `root`, un pour le compte ordinaire qui fera
  tourner jobbot.
- Dans le `.env` du PC (noms déjà présents dans `.env.example`) :

| Clé | Rôle |
|---|---|
| `SERVER_ROOT_HOST_ALIAS` | Alias SSH de root : paquets système, bibliothèques de Chromium, fuseau horaire, démarrage des services. |
| `PROJECT_SSH_HOST_ALIAS` | Alias SSH du compte qui fait tourner jobbot. |
| `PROJECT_USER` | Nom de ce compte (jamais root). |
| `SERVER_IP_TAILSCALE` | Optionnel : IP du serveur sur ton VPN, seulement avec `--dashboard-vpn`. |
| `CLAUDE_CODE_OAUTH_TOKEN` | L'IA de ton abonnement Claude sur le serveur (voir § 12.2). |

  Ces clés d'accès ne partent **jamais** sur le serveur : le déploiement y copie un `.env` filtré, sans commentaire,
  sans aucune clé `SERVER_*`, `PROJECT_*`, `WEBHOOK_*`, ni clé contenant `ROOT` ou `PASSWORD` (sauf
  `SMTP_PASSWORD`). Les clés ajoutées directement sur le serveur y sont conservées.
- Optionnel : `config/deploy.local.yaml` (ignoré par git) pour ce qui est propre à ton serveur :

```yaml
settings_local:            # imposé dans config/settings.local.yaml du serveur
  search_engine_url: http://127.0.0.1:8888      # une instance SearXNG locale (format JSON activé)
telegram:                  # fichiers lus en root SUR le serveur : les valeurs ne passent jamais par le PC
  token_file: /chemin/vers/jeton-du-bot
  chat_id_file: /chemin/vers/id-conversation
openclaw:                  # remplacement de l'ancienne application (§ 12.4)
  backup_extra: [scripts, .env]                 # chemins (relatifs au dossier du compte) à sauvegarder en plus
  keep: [mon-service]                            # services à ne jamais arrêter
```

### 12.2 Sur le PC d'abord : les connexions

Le serveur n'a pas d'écran : les connexions manuelles se font sur le PC, puis leurs cookies sont envoyés.

```powershell
jobbot login linkedin
jobbot login hellowork
jobbot login classroom
jobbot session export all      # data/sessions/<site>.json (secrets : jamais partagés, ignorés par git)
claude setup-token             # abonnement Claude : copie le jeton dans .env → CLAUDE_CODE_OAUTH_TOKEN=…
```

Sur le serveur, chaque ouverture du navigateur importe automatiquement une session plus récente que la dernière
importée (seulement les cookies du site concerné). `jobbot session import <dossier>` fait la même copie à la main.

### 12.3 Déployer

```powershell
.venv\Scripts\python scripts\deploy_server.py --dry-run   # le plan et les commandes : rien n'est modifié
.venv\Scripts\python scripts\deploy_server.py             # installation complète
```

| Étape | Ce qui est fait |
|---|---|
| 1 `check` | Connexion aux deux comptes, système, architecture, Python, espace disque, port du tableau de bord. |
| 2 `system` | (root) `git curl xvfb ca-certificates cron`, fuseau horaire Europe/Paris (heures de cron). |
| 3 `toolchain` | `uv`, Python 3.12, Claude Code (installateur officiel, sinon `npm` en root). |
| 4 `code` | Dépôt `~/jobbot.git` + copie `~/jobbot`, remote git `serveur`, `git push` (un hook met ensuite `~/jobbot` à jour à chaque `git push serveur`). Seul le dernier commit part. |
| 5 `deps` | `.venv` (`uv`), `pip install -e ".[dashboard]"`, Chromium de Playwright et ses bibliothèques (root). |
| 6 `private` | Profil, `.env` filtré, sessions exportées, `config/settings.local.yaml` (`headless: true`, `browser_channel: null`, `notify_email: true`) — envoyés seulement s'ils ont changé (sha256), droits 600. Jeton Telegram lu en root sur le serveur si configuré. |
| 7 `llm` | Petit appel à Claude sur le serveur ; en cas d'échec, les deux solutions sont affichées. |
| 8 `init` | `jobbot init` puis `jobbot doctor` (le tableau est affiché). |
| 9 `schedule` | `jobbot schedule install` : lignes cron (09:30, 18:00, Classroom). |
| 10 `dashboard` | Service systemd utilisateur `jobbot-dashboard` (démarré avec le serveur). |
| 11 `summary` | Résumé et prochaines actions. |

Chaque étape peut être relancée sans risque. `--only <étape>` en lance une seule (ex. `--only private` après un
nouvel export de sessions) ; `--update` = code, dépendances, fichiers privés, cron (et redémarrage du tableau de
bord). Mise à jour la plus rapide du code seul : `git push serveur`.

### 12.4 Remplacer l'ancienne application du serveur

Si une ancienne application tourne sur le serveur, `--replace-openclaw` l'arrête proprement avant l'installation
(jamais pendant un `--update`) :

```powershell
.venv\Scripts\python scripts\deploy_server.py --replace-openclaw --dry-run   # listes ARRÊTER / CONSERVER, rien ne change
.venv\Scripts\python scripts\deploy_server.py --replace-openclaw             # installation complète avec remplacement
```

- **Découverte automatique** : services systemd (système et du compte), minuteries, conteneurs docker, processus
  pm2, lignes crontab et processus qui écoutent sur son port (lu dans `WEBHOOK_BASE_URL`, 3000 par défaut). Les
  services de la liste « à conserver » (SearXNG, surveillance du serveur, synchronisation de fichiers, jobbot et
  `keep` de `config/deploy.local.yaml`) ne sont jamais arrêtés.
- **Sauvegarde d'abord** : `~/backups/openclaw-<date>.tar.gz` dans le dossier du compte projet (propriétaire root,
  droits 600) avec ses dossiers, fichiers de services, crontabs, `docker inspect` et état pm2. Sans sauvegarde,
  rien n'est arrêté.
- **Puis arrêt réversible** : services arrêtés et désactivés, conteneurs arrêtés (sans redémarrage automatique),
  processus pm2 retirés, seules ses lignes crontab retirées, port vérifié. Rien n'est supprimé : ni le compte, ni ses
  clés SSH, ni ses dossiers.
- **Revenir en arrière** : `--restore-openclaw` (dernière sauvegarde, ou `--restore-openclaw openclaw-AAAAMMJJ-HHMMSS.tar.gz`)
  remet dossiers, services, conteneurs, pm2 et lignes crontab.
- **Supprimer pour de bon** (optionnel, séparé) : `--purge-openclaw` supprime ses dossiers et fichiers de services ;
  refusé tant qu'aucune sauvegarde n'existe, et une nouvelle sauvegarde est faite juste avant. Un dossier qui
  contient un service conservé (ex. SearXNG) n'est jamais supprimé, pas plus que le dossier personnel, `.ssh`,
  `backups` ou jobbot. Les sauvegardes, elles, sont gardées.
- Les lignes crontab sont filtrées sur le serveur : leur contenu ne passe jamais par le PC (seul un aperçu masqué
  est affiché).

### 12.5 Le tableau de bord à distance

Sur le serveur, le tableau de bord écoute seulement sur `127.0.0.1:8501` : il n'est jamais exposé au réseau.
Depuis le PC :

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dashboard_serveur.ps1   # tunnel SSH, puis http://localhost:8501
```

Avec un VPN (Tailscale) : `--dashboard-vpn` le met sur l'IP VPN du serveur (`SERVER_IP_TAILSCALE`), jamais sur
`0.0.0.0`.

### 12.6 Être prévenu quand le serveur travaille seul

Chaque notification a un niveau : `info` (bilans), `important` (réponse, offre de l'école à traiter, fiche
d'entretien) ou `urgent` (entretien, session à renouveler).

| Réglage | Effet |
|---|---|
| `notify_email: true` | Les notifications importantes et urgentes t'arrivent par e-mail (« [jobbot] … », au plus `notify_email_max_per_day` par jour, 10 par défaut). Activé par le déploiement. |
| `notify_telegram: true` | Par ton bot Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) ; `notify_telegram_all: true` pour recevoir aussi les bilans. |
| `notify_ntfy_topic: <sujet>` | Sur le téléphone avec l'application ntfy (sujet long et difficile à deviner). |

En `--dry-run`, rien n'est envoyé (les e-mails sont écrits dans `output/outbox`).

### 12.7 Sur le serveur

- Les heures de cron suivent l'heure du serveur (réglée sur Europe/Paris par le déploiement). Journal des tâches :
  `~/jobbot/data/logs/cron.log` ; journal détaillé : `data/logs/jobbot.log`.
- `jobbot doctor` ajoute sous Linux : écran virtuel (`xvfb-run`), lignes cron, sessions importées (âge),
  `config/settings.local.yaml`, fuseau horaire.
- Sur le serveur, change tes réglages dans `config/settings.local.yaml` (`jobbot config set` y écrit quand la clé y
  est déjà) : `config/settings.yaml` est remplacé par la version du PC à chaque déploiement.
- Moteur de recherche : avec `search_engine_url` (SearXNG), la recherche des sites d'entreprises et des personnes
  l'utilise avant Brave et DuckDuckGo, sans l'attente de 25 s. Chaque requête ne demande que
  `search_engine_engines` (`[bing]` : le seul moteur qui répond encore depuis l'IP du serveur), une à la fois, au
  moins `night.search_min_interval_s` (6 s) d'écart, `night.max_search_queries` (300) par jour. Si l'instance
  signale ses moteurs bloqués (CAPTCHA, trop de requêtes, accès refusé), SearXNG est laissé tranquille
  `night.search_backoff_hours` (6 h) : Clearbit, noms de domaine devinés + DNS + SIREN, Wikidata et Brave (si
  `BRAVE_API_KEY`) prennent le relais. `jobbot doctor` montre l'état (« SearXNG ») et la dernière nuit.

### 12.8 Dépannage du serveur

| Problème | Solution |
|---|---|
| « Connexion SSH impossible avec l'alias … » | Le serveur est-il allumé et joignable (même réseau, VPN actif) ? Essaie `ssh <alias>`. Vérifie l'alias dans `~/.ssh/config`, puis autorise ta clé : `ssh-copy-id -i ~/.ssh/<clé>.pub <compte>@<serveur>` (sous Windows : `type $env:USERPROFILE\.ssh\<clé>.pub \| ssh <compte>@<serveur> "cat >> ~/.ssh/authorized_keys"`). Le script n'utilise jamais de mot de passe. |
| « IA indisponible » (étape 7) | Abonnement : `claude setup-token` sur le PC, jeton dans `CLAUDE_CODE_OAUTH_TOKEN`, puis `--only private`. Ou clé API payante : `ANTHROPIC_API_KEY` + `llm: {provider: anthropic}` dans `settings_local` de `config/deploy.local.yaml`. |
| Session LinkedIn / HelloWork / Google expirée | `jobbot login …` sur le PC, `jobbot session export all`, `--only private`. |
| Le tableau de bord ne répond pas | `ssh <alias> systemctl --user status jobbot-dashboard`, puis relance `scripts\dashboard_serveur.ps1`. |
| Les tâches ne tournent pas | `ssh <alias> "cd jobbot && .venv/bin/jobbot schedule show"` et `data/logs/cron.log`. |
