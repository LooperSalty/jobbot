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
| **Collecte** (`search`) | Offres de La Bonne Alternance, France Travail, LinkedIn (sans connexion), HelloWork, Indeed, Welcome to the Jungle, Free-Work et APEC, à Metz, Nancy, **au Luxembourg** et en télétravail. Les doublons entre sites sont détectés. |
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

L'assistant pose une quarantaine de questions, en six sections :

| Section | Ce qu'on y règle |
|---|---|
| **Recherche** | Contrats (alternance, CDI…), rayon autour de Metz, Nancy oui/non, Luxembourg oui/non et contrats acceptés là-bas, télétravail complet, déménagement et régions acceptées, expérience maximum exigée, prétentions salariales (France et Luxembourg). |
| **Automatisation** | Mode `review` ou `auto`, jours de rodage, seuils de score, quotas LinkedIn / HelloWork / La Bonne Alternance / e-mail. |
| **Candidatures spontanées** | Quota par jour, écrire ou non à des personnes nommées, chercher les RH et responsables techniques, adresses devinées autorisées ou non et leur maximum par jour. |
| **Relances** | Délais des deux relances et leur nombre. |
| **Notifications** | Annonces vocales Jarvis, rapport de la semaine (et son envoi par e-mail), fiche d'entretien automatique. |
| **Comptes** | Mot de passe d'application Gmail, identifiants France Travail, clés La Bonne Alternance / Hunter / Brave. |

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

### 3.2 Les clés du fichier `.env`

| Clé | À quoi elle sert | Où l'obtenir |
|---|---|---|
| `SMTP_USER`, `SMTP_PASSWORD` | Envoi des e-mails et lecture des réponses (Gmail) | `SMTP_USER` = ton adresse Gmail. `SMTP_PASSWORD` = un **mot de passe d'application** de 16 caractères, pas ton mot de passe Google : https://myaccount.google.com/apppasswords (la validation en 2 étapes doit être activée). |
| `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | Offres France Travail | https://francetravail.io → Espace développeur → créer une application → s'abonner à l'API « Offres d'emploi v2 ». |
| `LBA_API_TOKEN` | Optionnel : API officielle de La Bonne Alternance, en plus du site | Compte gratuit sur https://api.apprentissage.beta.gouv.fr. Il n'est **pas** nécessaire pour postuler via La Bonne Alternance. |
| `HUNTER_API_KEY`, `BRAVE_API_KEY` | Optionnel : vérifier des adresses e-mail, trouver le site d'une entreprise | hunter.io, brave.com/search/api |

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
jobbot status              # résumé : offres, candidatures, quotas du jour, files d'attente
jobbot assist              # offres « à finir à la main » : ouvertes et pré-remplies, tu cliques « Envoyer »
jobbot prep 12             # fiche de préparation de l'entretien de la candidature n° 12
jobbot report              # rapport de la semaine en cours (--precedente : semaine dernière complète)
jobbot dashboard           # tableau de bord dans le navigateur
jobbot export              # Excel (ou --format csv) de tout le suivi
```

Pour que jobbot tourne tout seul chaque jour :

```powershell
jobbot schedule install    # 09:30 : jobbot run · 18:00 : lecture des réponses + relances
jobbot schedule show
jobbot schedule remove
```

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
| `import-cv <pdf>` · `login linkedin\|hellowork\|wttj` | Profil depuis un CV, connexion manuelle |
| `search [-s source]` · `score` · `prepare [-n 10] [-j id]` | Collecte, tri, CV et lettres |
| `ats <id> [--cv autre.pdf]` | Explique le score ATS : mots-clés présents ou manquants, alertes |
| `apply [-c canal] [-n N] [-j id]` | Candidatures. Canaux : `linkedin`, `hellowork`, `lba`, `email`, `external`. `-j id` vaut validation. |
| `assist [-n 10]` | Candidatures externes pré-remplies. Réponds `o` (envoyée), `n` (abandon), `passer` ou `q`. |
| `approve [ids] [--all]` · `reject <ids> [--blacklist]` | File de validation (offres et entreprises) |
| `discover` · `enrich` · `spontaneous` | Candidatures spontanées |
| `inbox` · `followup` | Réponses et relances |
| `prep <id candidature>` | Fiche de préparation d'entretien (régénérée si elle existe) |
| `report [--precedente] [--date AAAA-MM-JJ] [--email]` | Rapport de la semaine ; `--email` te l'envoie aussi par e-mail |
| `run [-s étape]` | Journée complète, ou seulement certaines étapes : `collect`, `score`, `prepare`, `apply`, `discover`, `enrich`, `spontaneous`, `inbox`, `followup` |
| `status` · `export` · `dashboard` · `schedule install\|remove\|show` | Suivi |

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

`jobbot dashboard` ouvre http://localhost:8501. Les pages :

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
| La tâche planifiée ne tourne pas | `jobbot schedule show`, puis regarde `data\logs\tache-*.log`. La session Windows doit être ouverte. |
| Caractères bizarres dans la console | Utilise Windows Terminal : jobbot remplace les caractères non affichables au lieu de planter. |
| Le tableau de bord ne s'ouvre pas | `.venv\Scripts\pip install -e ".[dashboard]"`, ou change de port : `jobbot dashboard --port 8600`. |

Le journal détaillé est dans `data/logs/jobbot.log`. Pour plus de détails dans la console, ajoute `-v`.

---

## 11. Où sont tes données ?

| Emplacement | Contenu |
|---|---|
| `config/profile.yaml` | Ton profil (source de vérité). Ignoré par git : **ne jamais le publier.** |
| `config/profile.example.yaml` | Modèle de profil fictif (Jean DUPONT), copié par `jobbot init` |
| `config/settings.yaml` | Réglages : mode, quotas, seuils, relances, sources |
| `.env` | Secrets : mot de passe Gmail, clés d'API. **Ne jamais le partager.** |
| `data/jobbot.db` | Offres, entreprises, candidatures, historique, liste noire |
| `data/browser-profile/` | Sessions LinkedIn / HelloWork. **Ne jamais le partager.** |
| `data/logs/` | Journaux et captures d'écran (`screens/`) |
| `output/<date>/<entreprise>-<poste>/` | CV (PDF + DOCX) et lettre de chaque candidature (`…-candidature-spontanee/` pour une spontanée) |
| `output/entretiens/` | Fiches de préparation d'entretien (Markdown) |
| `output/rapports/` | Rapports de la semaine (`semaine-AAAA-SS.md`) |
| `output/outbox/` | E-mails simulés (`--dry-run`) |

Pour développer : `.venv\Scripts\python -m pytest -q -m "not live"`. Les tests n'utilisent ni le réseau ni ton profil
(ils lisent le profil fictif `tests/fixtures/profile.yaml`) ; ceux qui interrogent les vrais sites sont marqués
`live`. L'architecture et le contrat des modules sont décrits dans
`docs/ARCHITECTURE.md`, et les recherches (sites, limites, droit) dans `docs/research/`.
