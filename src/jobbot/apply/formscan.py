"""Read the controls of a form on a Playwright Page, Frame or Locator (shadow-DOM aware).

Every scanned control is tagged ``data-jobbot-key`` so ``jobbot.apply.formfill`` can find it again.
Radio buttons and same-name checkboxes are merged into one question with options.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict

from jobbot.apply.answer_rules import clean_label
from jobbot.apply.answers import FormQuestion
from jobbot.apply.coerce import is_placeholder

log = logging.getLogger(__name__)

CONTROL_SEL = (
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), "
    "select, textarea"
)
UNSUPPORTED_TYPES = frozenset({"date", "datetime-local", "month", "week", "time", "color", "range", "password", "search"})

_SCAN_IDS = itertools.count(1)

_DESCRIBE_JS = """(els, arg) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const txt = (n) => (n ? clean(n.innerText || n.textContent) : '');
  const vis = (n) => { if (!n) return false; const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = (el, root) => { if (el.id) { try { const l = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) return l; } catch (e) {} } return el.closest('label'); };
  const byIds = (el, root) => clean((el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)
    .map((i) => txt(root.getElementById ? root.getElementById(i) : document.getElementById(i))).join(' '));
  const groupTitle = (el) => {
    const legendSel = 'legend, [data-test-form-builder-radio-button-form-component__title]';
    let node = el.parentElement;
    for (let d = 0; node && d < 6; d += 1, node = node.parentElement) {
      const lg = node.querySelector(legendSel);
      if (node.matches('fieldset, [role=radiogroup], [role=group]')) {
        if (lg) return txt(lg);
        if (node.getAttribute('aria-label')) return clean(node.getAttribute('aria-label'));
      }
      const same = el.name ? node.querySelectorAll('input[name="' + CSS.escape(el.name) + '"]').length : 0;
      if (same >= 2) {
        if (lg) return txt(lg);
        const c = node.cloneNode(true);
        c.querySelectorAll('label, input, select, textarea, button, option').forEach((x) => x.remove());
        const t = clean(c.textContent); if (t) return t.slice(0, 200);
      }
    }
    return '';
  };
  const wrapText = (el) => { const w = el.closest('[data-test-form-element], .fb-dash-form-element, .form-group, ' +
    '.form-field, .field, .question, .application-question'); return w ? txt(w).slice(0, 200) : ''; };
  return els.map((el, i) => {
    const key = arg.scan + '-' + i; el.setAttribute('data-jobbot-key', key);
    const root = el.getRootNode(); const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase(); const lab = labelOf(el, root);
    const label = txt(lab) || byIds(el, root) || clean(el.getAttribute('aria-label'));
    const choice = type === 'radio' || type === 'checkbox';
    const selected = tag === 'select' && el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
    return { key, tag, type, label, group: choice ? groupTitle(el) : '', context: label ? '' : wrapText(el),
      name: el.getAttribute('name') || '', id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
      autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(), role: el.getAttribute('role') || '',
      ariaAuto: el.getAttribute('aria-autocomplete') || '',
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      value: tag === 'select' ? selected : (el.value || ''), checked: !!el.checked,
      visible: vis(el) || ((choice || type === 'file') && vis(lab)), disabled: !!el.disabled || el.readOnly === true,
      accept: el.getAttribute('accept') || '', files: el.files ? el.files.length : 0,
      options: tag === 'select' ? Array.from(el.options).map((o) => clean(o.text)) : [],
      maxlength: el.maxLength > 0 ? el.maxLength : 0, excluded: !!(arg.exclude && el.closest(arg.exclude)) };
  });
}"""


class FieldInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    keys: tuple[str, ...]
    kind: str
    label: str
    options: tuple[str, ...] = ()
    required: bool = False
    value: str = ""
    checked: tuple[bool, ...] = ()
    hints: str = ""
    autocomplete: str = ""
    accept: str = ""
    maxlength: int = 0
    visible: bool = True
    disabled: bool = False
    supported: bool = True
    files: int = 0

    @property
    def is_filled(self) -> bool:
        if self.kind in ("radio", "checkbox"):
            return any(self.checked)
        if self.kind == "select":
            return bool(self.value) and not is_placeholder(self.value)
        if self.kind == "file":
            return self.files > 0
        return bool(self.value.strip())

    def question(self) -> FormQuestion:
        options = [o for o in self.options if not is_placeholder(o)]
        return FormQuestion(label=self.label, kind=self.kind, options=options, required=self.required)  # type: ignore[arg-type]


def scan_fields(scope: Any, *, selector: str = CONTROL_SEL, exclude: str = "") -> list[FieldInfo]:
    """Describe every form control under ``scope`` (radios/checkbox groups merged into one field)."""
    try:
        raw = scope.locator(selector).evaluate_all(_DESCRIBE_JS, {"scan": f"s{next(_SCAN_IDS)}", "exclude": exclude})
    except PlaywrightError as exc:
        log.warning("Lecture du formulaire impossible : %s", exc)
        return []
    return build_fields([r for r in raw if not r["excluded"]])


def locate(scope: Any, key: str) -> Any:
    """The control tagged ``key`` by the last scan (a Playwright Locator)."""
    return scope.locator(f"[data-jobbot-key='{key}']").first


def _kind(d: dict) -> str:
    if d["tag"] in ("select", "textarea"):
        return d["tag"]
    if d["type"] in ("file", "radio", "checkbox", "number"):
        return d["type"]
    if d["role"] == "combobox" or d["ariaAuto"] in ("list", "both"):
        return "typeahead"
    return "text"


def _group_key(d: dict, checkbox_names: dict[str, int]) -> tuple[str, str] | None:
    if d["type"] == "radio":
        return ("radio", d["name"] or d["group"] or d["key"])
    if d["type"] == "checkbox" and d["name"] and checkbox_names.get(d["name"], 0) > 1:
        return ("checkbox", d["name"])
    return None


def build_fields(raw: list[dict]) -> list[FieldInfo]:
    checkbox_names: dict[str, int] = {}
    for d in raw:
        if d["type"] == "checkbox" and d["name"]:
            checkbox_names[d["name"]] = checkbox_names.get(d["name"], 0) + 1
    entries: list[list[dict]] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for d in raw:
        gkey = _group_key(d, checkbox_names)
        if gkey is None:
            entries.append([d])
        elif gkey in groups:
            groups[gkey].append(d)
        else:
            groups[gkey] = [d]
            entries.append(groups[gkey])
    return [_to_field(items) for items in entries]


def _to_field(items: list[dict]) -> FieldInfo:
    first = items[0]
    kind = _kind(first)
    grouped = kind == "radio" or len(items) > 1
    raw_label = (first["group"] or first["name"]) if grouped else (
        first["label"] or first["placeholder"] or first["context"] or first["name"]
    )
    options = tuple(clean_label(i["label"]) or i["value"] for i in items) if grouped else tuple(first["options"])
    return FieldInfo(
        keys=tuple(i["key"] for i in items), kind=kind, label=clean_label(raw_label), options=options,
        required=any(i["required"] for i in items) or "*" in raw_label,
        value=first["value"] if not grouped else "", checked=tuple(i["checked"] for i in items),
        hints=" ".join(first[k] for k in ("name", "id", "placeholder")), autocomplete=first["autocomplete"],
        accept=first["accept"], maxlength=first["maxlength"], visible=any(i["visible"] for i in items),
        disabled=all(i["disabled"] for i in items), supported=first["type"] not in UNSUPPORTED_TYPES,
        files=first["files"],
    )
