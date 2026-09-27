"""mail.classify — answers to spontaneous applications: « no opening » refusals, vague refusals, and what must NOT
be read as a refusal (interview rescheduling, conditional acknowledgements, absences, questions)."""

from __future__ import annotations

import pytest

from jobbot.llm import FakeLLM
from jobbot.mail.classify import classify_reply, is_rejection
from jobbot.mail.messages import InboundMessage

EXEMPLE = (
    "Bonjour Jean,\n\nMerci pour l'intérêt que vous portez à notre société. Nous vous invitons à consulter les "
    "plateformes dédiées à l'alternance (La Bonne Alternance, Indeed…) pour trouver une entreprise d'accueil.\n\n"
    "Bonne continuation,\nL'équipe Exemple"
)
RESCHEDULE = (
    "Bonjour Jean,\n\nMalheureusement la date du 3 octobre n'est plus possible de notre côté. Pouvons-nous fixer un "
    "entretien le mardi 6 octobre à 10h ?\n\nCordialement,\nAmélie"
)


def _msg(body: str, *, auto: bool = False) -> InboundMessage:
    return InboundMessage(uid=1, folder="[Gmail]/Tous les messages", message_id="<r1@example.fr>",
                          from_addr="rh@example.fr", subject="RE: Candidature spontanée – alternance développeur",
                          body_text=body, auto_submitted=auto)


@pytest.mark.parametrize("body", [
    EXEMPLE,
    "Bonjour, nous ne recrutons pas d'alternant cette année. Cordialement.",
    "Nous n'avons pas de besoin en alternance pour le moment, bonne recherche.",
    "Il n'y a pas de poste à pourvoir actuellement dans notre équipe.",
    "Nous ne prenons pas d'alternants en développement.",
    "Nous ne donnerons pas suite à votre candidature spontanée.",
    "Notre agence n'est pas en recrutement.",
    "Malheureusement, nous ne pouvons pas vous accueillir.",
    "Nous ne sommes pas en mesure de vous proposer un contrat d'apprentissage.",
    "Nous n'avons aucune offre d'alternance à ce jour.",
    "Suite à notre entretien, malheureusement nous ne pourrons pas vous accueillir.",
    "Nous avons bien reçu votre candidature, mais nous ne recrutons pas d'apprentis.",
])
def test_refusals_of_spontaneous_applications(body):
    llm = FakeLLM('{"category": "other", "summary": "Réponse", "confidence": 0.9}')
    result = classify_reply(_msg(body), llm)
    assert result.category == "rejection", body
    assert not llm.calls  # decided by the rules, the LLM is not asked


@pytest.mark.parametrize(("body", "category"), [
    (RESCHEDULE, "interview"),
    (("Nous ne recrutons pas d'alternant pour le moment, mais seriez-vous disponible pour un échange "
      "téléphonique jeudi ?"), "interview"),
    (("Nous avons bien reçu votre candidature. Si aucun poste ne correspond à votre profil, nous conserverons "
      "votre CV."), "auto_reply"),
    ("Je suis actuellement absente et malheureusement sans accès à mes e-mails jusqu'au 12 octobre.", "auto_reply"),
    ("Malheureusement votre CV était illisible : pourriez-vous nous transmettre une nouvelle version ?",
     "info_request"),
    ("Votre profil nous intéresse, malheureusement notre responsable est absent cette semaine.", "positive"),
])
def test_what_is_not_a_refusal(body, category):
    assert classify_reply(_msg(body), None).category == category


def test_platform_refusal_wording_is_unchanged():
    assert is_rejection("Malheureusement, votre candidature n'a pas été retenue.")
    assert is_rejection("Unfortunately your application was not successful for this position.")
    assert not is_rejection("Votre candidature a bien été envoyée.")
