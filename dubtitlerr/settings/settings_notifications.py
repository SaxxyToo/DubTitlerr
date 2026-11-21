"""Notifications: named outbound providers, validated the way Connections are.

A provider is {name, kind, enabled, triggers[]} plus its kind's visible fields in settings.json, and its write-only
secrets in secrets.json. That is exactly the shape `dubtitlerr/core/notify.py` reads, so the panel and the sender never
disagree about the file layout:

  settings.json   {"notifications": [{"name", "kind", "enabled", "triggers": [...], <kind fields>}]}
  secrets.json    {"notifications": {"<name>": {"url"|"token": "<secret>"}}}

Kinds and events are imported from notify rather than copied, so a new event or kind is added in one place. notify is a
stdlib-only leaf, so importing it here drags nothing heavy into the settings stack.
"""

import re

from dubtitlerr.core import notify

KINDS = notify.KINDS
EVENTS = notify.EVENTS
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
COMMON_FIELDS = ("name", "kind", "enabled", "triggers")
# Visible per kind: what the page shows and the operator edits. Discord's link_url is the review UI's base
# URL (optional); notify turns it into each embed's click-through.
VISIBLE_FIELDS = {"discord": ("link_url",), "ntfy": ("server", "topic"), "webhook": ()}
# Write-only per kind: never returned, only reported as set or not set.
SECRET_FIELDS = {"discord": ("url",), "ntfy": ("token",), "webhook": ("url",)}
KIND_LABELS = {"discord": "Discord", "ntfy": "ntfy", "webhook": "Webhook"}
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SECRET_MAX = 512


def known_fields():
    """Every field any kind accepts; the set an unknown-field message is measured against."""
    out: set[str] = set(COMMON_FIELDS)
    for kind in KINDS:
        out.update(VISIBLE_FIELDS[kind])
        out.update(SECRET_FIELDS[kind])
    return out


def _trigger_problem(triggers):
    if not isinstance(triggers, list) or not all(isinstance(t, str) for t in triggers):
        return "must be a list of trigger names"
    unknown = [t for t in triggers if t not in EVENTS]
    if unknown:
        return f"unknown trigger: {unknown[0]}"
    if len(set(triggers)) != len(triggers):
        return "lists a trigger twice"
    return ""


def validate_notification(item):
    """(clean provider dict, {field: message}); the secrets are handled by the caller."""
    errors, clean = {}, {}
    if not isinstance(item, dict):
        return {}, {"notification": "must be an object"}
    unknown = sorted(set(item) - known_fields())
    if unknown:
        errors["notification"] = f"unknown field: {unknown[0]}"
    name = item.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        errors["name"] = "use letters, digits, . _ - (up to 64 characters)"
    kind = item.get("kind")
    if kind not in KINDS:
        errors["kind"] = "must be discord, ntfy or webhook"
    enabled = item.get("enabled", True)
    if type(enabled) is not bool:
        errors["enabled"] = "must be true or false"
    triggers = item.get("triggers", [])
    why = _trigger_problem(triggers)
    if why:
        errors["triggers"] = why
    if kind == "ntfy":
        server = item.get("server")
        if not isinstance(server, str) or not server.strip():
            errors["server"] = "is required"
        else:
            problem = notify.url_problem(server.strip())
            if problem:
                errors["server"] = problem
        topic = item.get("topic")
        if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic.strip()):
            errors["topic"] = "use letters, digits, _ or - (up to 64 characters)"
    if kind == "discord":
        link = item.get("link_url")
        if isinstance(link, str):
            if link.strip():
                problem = notify.url_problem(link.strip())
                if problem:
                    errors["link_url"] = problem
        elif link is not None:
            errors["link_url"] = "must be text"
    # A secret field sent for a kind that has none is an unknown field: known_fields() already names the field, but the
    # per-kind set is what actually decides, so say so on that field.
    if isinstance(kind, str) and kind in KINDS:
        allowed = set(COMMON_FIELDS) | set(VISIBLE_FIELDS[kind]) | set(SECRET_FIELDS[kind])
        for field in sorted(known_fields() - allowed):
            if field in item:
                errors[field] = f"is not used by a {kind} notification"
    if not errors:
        clean.update(name=name, kind=kind, enabled=enabled, triggers=list(triggers))
        if kind == "ntfy":
            clean["server"] = item["server"].strip()
            clean["topic"] = item["topic"].strip()
        elif kind == "discord" and isinstance(item.get("link_url"), str) and item["link_url"].strip():
            clean["link_url"] = item["link_url"].strip()
    return clean, errors


def secret_problem(kind, field, value):
    """Why ``value`` may not be stored as the ``field`` secret of a ``kind`` provider, or \"\"."""
    if not isinstance(value, str):
        return "must be text"
    if field == "token":
        if any(ch.isspace() or ord(ch) < 32 for ch in value):
            return "must be one line without spaces or control characters"
    elif any(ch in value for ch in "\r\n\x00"):
        return "must be one line without control characters"
    if not value.strip() or len(value) > SECRET_MAX:
        return f"must be one line of 1 to {SECRET_MAX} characters"
    if field == "url":
        return notify.url_problem(value)
    return ""


def rules():
    """What the page needs to build the rows without copying these rules."""
    return {
        "kinds": list(KINDS),
        "kind_labels": dict(KIND_LABELS),
        "triggers": list(EVENTS),
        "fields": {
            kind: {"visible": list(VISIBLE_FIELDS[kind]), "secret": list(SECRET_FIELDS[kind])} for kind in KINDS
        },
    }
