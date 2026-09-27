"""jobbot dashboard (Streamlit, French). Run with « jobbot dashboard » (or streamlit run this file).

Pages: vue d'ensemble, offres, à valider, actions manuelles, candidatures, entretiens (fiches de préparation),
entreprises (+ section « Nuit » : dernier rapport, file d'attente par priorité), sources, réglages.
All queries/actions live in ``jobbot.dashboard.data`` (tested without Streamlit).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from jobbot.dashboard import data
from jobbot.models import JobStatus

PAGES = ("Vue d'ensemble", "Offres", "À valider", "Actions manuelles", "Candidatures", "Entretiens", "Entreprises",
         "Sources", "Réglages")
LINK = st.column_config.LinkColumn
SCORE = st.column_config.NumberColumn
JOB_COLUMNS = ["score", "ats_cv", "offre", "entreprise", "lieu", "canal", "statut", "trouvee_le", "lien", "postuler"]


@st.cache_resource(show_spinner=False)
def _context(root: str) -> Any:
    return data.load_context(Path(root))


def ctx() -> Any:
    return _context(str(data.dashboard_root()))


def _frame(rows: list[dict[str, Any]], columns: list[str] | None = None) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if columns and not frame.empty:
        frame = frame[[c for c in columns if c in frame.columns]]
    return frame


def _flash() -> None:
    message = st.session_state.pop("flash", None)
    if message:
        st.success(message)


def _act(message: str) -> None:
    st.session_state["flash"] = message
    st.rerun()


def _downloads(row: dict[str, Any], key: str) -> None:
    cols = st.columns(2)
    for col, (field, text) in zip(cols, (("cv_pdf", "Télécharger le CV"), ("lettre_pdf", "Télécharger la lettre")),
                                  strict=True):
        content = data.file_bytes(row.get(field))
        if content:
            col.download_button(text, content, file_name=Path(row[field]).name, mime="application/pdf",
                                key=f"{field}-{key}")


# --------------------------------------------------------------------------- pages


def page_overview() -> None:
    k = data.kpis(ctx())
    cols = st.columns(5)
    for col, (name, value) in zip(cols, (("Offres", k["offres"]), ("Retenues", k["retenues"]),
                                         ("Candidatures", k["candidatures"]), ("Réponses", k["reponses"]),
                                         ("Entretiens", k["entretiens"])), strict=True):
        col.metric(name, value)
    cols = st.columns(5)
    for col, (name, value) in zip(cols, (("Cette semaine", k["cette_semaine"]), ("À valider", k["a_valider"]),
                                         ("À finir à la main", k["a_finir"]), ("Relances dues", k["relances_dues"]),
                                         ("E-mails envoyés", k["emails_envoyes"])), strict=True):
        col.metric(name, value)
    counts = data.pipeline_counts(ctx())
    st.subheader("Offres par statut")
    if counts:
        st.bar_chart(_frame(counts).set_index("statut"))
    else:
        st.info("Aucune offre pour l'instant : lance « jobbot run » ou « jobbot search ».")
    st.subheader("Derniers événements")
    st.dataframe(_frame(data.timeline(ctx(), limit=15)), hide_index=True)


def page_jobs() -> None:
    rows = data.job_rows(ctx())
    if not rows:
        st.info("Aucune offre collectée.")
        return
    frame = _frame(rows)
    c1, c2, c3 = st.columns(3)
    statuses = c1.multiselect("Statut", sorted(frame["statut"].unique()))
    sources = c2.multiselect("Source", sorted(frame["source"].unique()))
    minimum = c3.slider("Score minimum", 0, 100, 0)
    text = st.text_input("Recherche (intitulé, entreprise, lieu)")
    view = frame[frame["score"].fillna(0) >= minimum]
    view = view[view["statut"].isin(statuses)] if statuses else view
    view = view[view["source"].isin(sources)] if sources else view
    if text:
        blob = view["offre"] + " " + view["entreprise"] + " " + view["lieu"]
        view = view[blob.str.contains(text, case=False, regex=False)]
    st.dataframe(view[[c for c in JOB_COLUMNS if c in view.columns]], hide_index=True,
                 column_config={"lien": LINK("Offre"), "postuler": LINK("Postuler"),
                                "score": SCORE("Score", format="%.0f"), "ats_cv": SCORE("ATS CV", format="%.0f")})
    _job_detail({r["id"]: r for r in rows}, list(view["id"]))


def _job_detail(by_id: dict[str, dict[str, Any]], ids: list[str]) -> None:
    if not ids:
        return
    choice = st.selectbox("Détail d'une offre", ids, format_func=lambda i: f"{by_id[i]['entreprise']} — "
                                                                           f"{by_id[i]['offre']}")
    row = by_id[choice]
    st.markdown(f"**{row['offre']}** — {row['entreprise']} ({row['lieu']}) · {row['statut']} · {row['canal']}")
    if row["detail"]:
        st.caption(row["detail"])
    ats = data.ats_summary(ctx(), choice)
    if ats:
        st.write(f"Score ATS du CV : **{ats['score']:.0f}/100** (mots-clés {ats['mots_cles']:.0f}, "
                 f"intitulé {ats['intitule']:.0f}, sections {ats['sections']:.0f}, format {ats['format']:.0f})")
        st.write("Présents : " + (", ".join(ats["presents"]) or "—"))
        st.write("Manquants : " + (", ".join(ats["manquants"]) or "—"))
        for alert in ats["alertes"]:
            st.warning(alert)
    _downloads(row, choice)


def page_review() -> None:
    _flash()
    jobs = data.jobs_in(ctx(), JobStatus.PENDING_REVIEW)
    st.subheader(f"Offres à valider ({len(jobs)})")
    if not jobs:
        st.info("Rien à valider.")
    for job in jobs:
        _review_job(job)
    companies = data.pending_companies(ctx())
    st.subheader(f"Candidatures spontanées à valider ({len(companies)})")
    for company in companies:
        _review_company(company)


def _review_job(job: Any) -> None:
    row = data.job_row(ctx(), job)
    with st.expander(f"{job.match_score or 0:.0f} · {job.company} — {job.title}"):
        st.write(f"{row['lieu']} · {row['canal']} · ATS CV : {row['ats_cv'] or '—'}")
        if row["lien"]:
            st.markdown(f"[Voir l'offre]({row['lien']})")
        _downloads(row, job.id)
        c1, c2 = st.columns(2)
        if c1.button("Valider", key=f"ok-{job.id}", type="primary"):
            _act(data.approve(ctx(), job.id))
        if c2.button("Écarter", key=f"ko-{job.id}"):
            _act(data.reject(ctx(), job.id))


def _review_company(company: Any) -> None:
    row = data.company_row(company)
    with st.expander(f"{company.name} — {company.city}"):
        st.write(f"{row['activite']} · contact : {data.contact_label(row)}")
        if row["description"]:
            st.caption(row["description"])
        c1, c2 = st.columns(2)
        if c1.button("Valider", key=f"cok-{company.id}", type="primary"):
            _act(data.review_company(ctx(), company.id, True))
        if c2.button("Écarter", key=f"cko-{company.id}"):
            _act(data.review_company(ctx(), company.id, False))


def page_manual() -> None:
    _flash()
    jobs = data.jobs_in(ctx(), JobStatus.NEEDS_MANUAL)
    st.subheader(f"Candidatures à finir à la main ({len(jobs)})")
    st.caption("Astuce : « jobbot assist » ouvre chaque offre pré-remplie dans le navigateur.")
    for job in jobs:
        row = data.job_row(ctx(), job)
        with st.expander(f"{job.company} — {job.title}"):
            if row["detail"]:
                st.caption(row["detail"])
            if row["postuler"]:
                st.markdown(f"[Postuler sur le site]({row['postuler']})")
            _downloads(row, job.id)
            c1, c2 = st.columns(2)
            if c1.button("J'ai postulé", key=f"done-{job.id}", type="primary"):
                _act(data.mark_applied(ctx(), job.id))
            if c2.button("Abandonner", key=f"drop-{job.id}"):
                _act(data.reject(ctx(), job.id))
    st.subheader("Relances à faire à la main")
    followups = data.manual_followups(ctx())
    if followups:
        st.dataframe(_frame(followups), hide_index=True)
    else:
        st.info("Aucune relance manuelle en attente.")


def page_applications() -> None:
    st.subheader("Candidatures")
    st.dataframe(_frame(data.application_rows(ctx())), hide_index=True)
    st.subheader("Historique")
    st.dataframe(_frame(data.timeline(ctx())), hide_index=True)


def page_interviews() -> None:
    from jobbot.reports.interview import generate_prep, interview_rows

    _flash()
    rows = interview_rows(ctx())
    st.subheader(f"Entretiens ({len(rows)})")
    if not rows:
        st.info("Aucun entretien pour l'instant : chaque invitation reçue par e-mail arrive ici avec sa fiche de "
                "préparation (« jobbot prep <id> » pour la générer à la main).")
    for row in rows:
        with st.expander(f"{row['entreprise']} — {row['poste'] or 'candidature spontanée'} · {row['statut']}"):
            if row["logistique"]:
                st.caption(row["logistique"])
            content = data.file_bytes(row["fiche"])
            if content:
                st.download_button("Télécharger la fiche", content, file_name=Path(row["fiche"]).name,
                                   mime="text/markdown", key=f"dl-prep-{row['id']}")
                st.markdown(content.decode("utf-8", "replace"))
            if st.button("Régénérer la fiche" if content else "Générer la fiche", key=f"prep-{row['id']}"):
                _act(generate_prep(ctx(), row["id"]))


def _night_section() -> None:
    """Last night report + ready queue by priority (« jobbot night »)."""
    from jobbot.night.status import last_report, queue_rows, status_lines

    st.subheader("Nuit")
    last = last_report(ctx())
    st.caption(f"{last['at'][:16].replace('T', ' ')} UTC — {last['summary']}" if last
               else "Aucune nuit encore : « jobbot night » (planifiée à 0 h 30 par « jobbot schedule install »).")
    for line in status_lines(ctx())[:3]:
        st.write(line)
    rows = queue_rows(ctx())
    if rows:
        st.dataframe(_frame(rows), hide_index=True,
                     column_config={"priorite": SCORE("Priorité", format="%.0f")})


def page_companies() -> None:
    try:
        _night_section()
    except Exception as exc:  # the companies table stays available
        st.warning(f"Section « Nuit » indisponible : {exc}")
    rows = data.company_rows(ctx())
    if not rows:
        st.info("Aucune entreprise : lance « jobbot discover ».")
        return
    frame = _frame(rows)
    statuses = st.multiselect("Statut", sorted(frame["statut"].unique()))
    view = frame[frame["statut"].isin(statuses)] if statuses else frame
    st.dataframe(view.drop(columns=["id"]), hide_index=True,
                 column_config={"site": LINK("Site"), "carrieres": LINK("Carrières")})


def page_sources() -> None:
    stats = data.source_stats(ctx())
    if not stats:
        st.info("Aucune offre collectée.")
        return
    frame = _frame(stats)
    st.dataframe(frame, hide_index=True)
    st.bar_chart(frame.set_index("source")[["offres", "retenues", "candidatures"]])


# --------------------------------------------------------------------------- settings


def _widget(q: Any, value: Any) -> Any:
    key = f"setting-{q.key}"
    label = q.label
    if q.kind == "bool":
        return st.checkbox(label, value=bool(value), help=q.help or None, key=key)
    if q.kind == "int":
        return int(st.number_input(label, value=int(value or 0), step=1, help=q.help or None, key=key))
    if q.kind == "float":
        return float(st.number_input(label, value=float(value or 0), step=1.0, help=q.help or None, key=key))
    if q.kind == "choice":
        index = list(q.choices).index(value) if value in q.choices else 0
        return st.selectbox(label, list(q.choices), index=index, help=q.help or None, key=key)
    if q.kind == "list" and q.choices:
        chosen = [v for v in (value or []) if v in q.choices]
        return st.multiselect(label, list(q.choices), default=chosen, help=q.help or None, key=key)
    if q.kind == "list":
        text = st.text_input(label, value=", ".join(value or []), help="Valeurs séparées par des virgules", key=key)
        return [v.strip() for v in text.split(",") if v.strip()]
    if q.kind == "secret":
        placeholder = "déjà renseigné — laisser vide pour garder" if value else "non renseigné"
        return st.text_input(label, value="", type="password", placeholder=placeholder, help=q.help or None, key=key)
    return st.text_input(label, value="" if value is None else str(value), help=q.help or None, key=key)


def page_settings() -> None:
    _flash()
    root = data.dashboard_root()
    st.caption("Les modifications sont validées puis écrites dans config/ (commentaires conservés) ou .env.")
    for section, questions in data.settings_sections().items():
        current = data.current_values(root, questions)
        with st.form(f"form-{section}"):
            st.subheader(section)
            answers = {q.key: _widget(q, current.get(q.key)) for q in questions}
            if st.form_submit_button("Enregistrer"):
                saved, errors = data.save_answers(root, questions, answers)
                for error in errors:
                    st.error(error)
                if not errors:
                    _context.clear()
                    _act(f"{section} : {saved} réglage(s) enregistré(s).")


def main() -> None:
    st.set_page_config(page_title="jobbot", layout="wide")
    st.sidebar.title("jobbot")
    page = st.sidebar.radio("Page", PAGES)
    try:
        dict(zip(PAGES, (page_overview, page_jobs, page_review, page_manual, page_applications, page_interviews,
                         page_companies, page_sources, page_settings), strict=True))[page]()
    except Exception as exc:  # show a French message instead of a raw traceback
        st.error(f"Erreur d'affichage : {exc}")


main()
