"""Reading an inbound payload, one adapter per channel.

Every channel says the same handful of things in a different way. Rightmove calls
it `telephone`, Meta calls it `phone_number`, a website form calls it whatever the
person who built the form called it. An adapter's whole job is to turn one of
those into a `LeadDraft`, and to say clearly when it cannot.

On the field names below
-----------------------
The portal mappings list several candidate keys per field rather than one. That is
not indecision - it is how these integrations actually behave. A portal's
documented payload and the payload it sends are not always the same shape, and
they change it without telling anybody. Trying a few spellings costs nothing and
survives the common case where `phone` becomes `telephone` in a minor release.

The mappings still need confirming against one real payload per channel before
going live. That is exactly what `IntakeEvent.payload` is for: point the channel
at the endpoint, look at what actually arrived, and correct the list. Nothing is
lost while the mapping is wrong, which is the only reason it is safe to ship a
best guess.

UK conventions
--------------
Phone numbers are normalised to national format (`07700 900123`, `01202 555111`)
and `+44` is stripped, because a portal sends `+447700900123` and a salesperson
reading the screen wants the number they would dial. Postcodes are upper-cased and
spaced. Both are formatting, not validation: a badly formed number is kept as it
arrived rather than rejected, since a real enquiry with a mistyped number is still
a real enquiry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models.intake import IntakeKind

# --------------------------------------------------------------- the draft ---


@dataclass(slots=True)
class LeadDraft:
    """What an adapter managed to understand, before any business rule applies."""

    first_name: str
    last_name: str
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    postcode: str | None = None
    city: str | None = None
    address: str | None = None
    notes: str | None = None
    bedrooms_needed: int | None = None
    price_min: str | None = None
    price_max: str | None = None
    desired_move_in: date | None = None
    has_home_to_sell: bool | None = None
    is_prequalified: bool | None = None
    #: The channel's own reference, which makes a resend idempotent.
    external_ref: str | None = None
    #: Anything the adapter recognised but we have no column for. Folded into the
    #: lead's notes rather than dropped - a portal asking "how did you hear about
    #: us" is worth keeping even though there is nowhere structured to put it.
    extra: dict[str, Any] = field(default_factory=dict)


class UnreadablePayload(Exception):
    """The payload cannot be turned into a lead, and says why.

    Raised rather than returned so the caller cannot accidentally carry on with a
    half-built draft. The message goes straight onto the failed event, so it has
    to be worth reading six weeks later.
    """


class SpamRejected(Exception):
    """A bot, not a person. Rejected rather than captured and archived later."""


# ------------------------------------------------------------- UK tidying ---

_UK_MOBILE = re.compile(r"^7\d{9}$")


def clean_phone(value: Any) -> str | None:
    """Normalise a UK number to the form somebody would dial.

    `+447700900123` and `00447700900123` both become `07700 900123`. A number we
    do not recognise is returned trimmed rather than discarded: a mistyped number
    on a genuine enquiry is still a lead, and a salesperson can see the typo.
    """
    if not value:
        return None
    raw = str(value).strip()
    digits = re.sub(r"\D", "", raw)

    for prefix in ("0044", "44"):
        if digits.startswith(prefix) and len(digits) > len(prefix) + 8:
            digits = digits[len(prefix) :]
            break

    if _UK_MOBILE.match(digits):
        digits = "0" + digits
    if len(digits) == 11 and digits.startswith("07"):
        return f"{digits[:5]} {digits[5:]}"
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:5]} {digits[5:]}"
    if len(digits) == 11 and digits.startswith("02"):
        return f"{digits[:3]} {digits[3:7]} {digits[7:]}"
    return raw or None


def clean_postcode(value: Any) -> str | None:
    """`bh151aa` becomes `BH15 1AA`. Anything unrecognised is passed through."""
    if not value:
        return None
    raw = str(value).strip().upper()
    squashed = raw.replace(" ", "")
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", squashed):
        return f"{squashed[:-3]} {squashed[-3:]}"
    return raw or None


def clean_email(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    return text if "@" in text and "." in text.split("@")[-1] else None


def _split_name(value: str) -> tuple[str, str]:
    """A single name field into two.

    Plenty of channels collect one "name" box. Everything before the last space
    is the forename, because "Mary Anne Okonkwo" is far more likely than a
    two-word surname, and a salesperson can correct it in one edit.
    """
    parts = [p for p in str(value).replace(",", " ").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        # One word is all we have. Better a lead called "Dana" with a placeholder
        # surname than no lead: the name is required, the accuracy is not.
        return parts[0], "(no surname given)"
    return " ".join(parts[:-1]), parts[-1]


def _first(payload: dict, *keys: str) -> Any:
    """The first of several spellings that is actually present.

    Case- and separator-insensitive, so `firstName`, `first_name` and `First Name`
    all match one entry in the candidate list.
    """
    flat = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in payload.items()}
    for key in keys:
        value = flat.get(re.sub(r"[^a-z0-9]", "", key.lower()))
        if value not in (None, "", []):
            return value
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> str | None:
    """`"£450,000"` becomes `"450000"`. Kept as a string for Decimal to parse."""
    if value in (None, ""):
        return None
    digits = re.sub(r"[^\d.]", "", str(value))
    return digits or None


def _yes(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def is_mobile(number: str | None) -> bool:
    """A UK mobile, by the number rather than by which box it was typed into."""
    return bool(number) and re.sub(r"\D", "", number).startswith("07")


def _assign_numbers(draft: LeadDraft, *numbers: str | None) -> None:
    """Sort whatever numbers arrived into landline and mobile.

    Channels are careless about this. A form with one "telephone" box collects
    mobiles in it, and a portal will put the same number in both fields. Routing by
    the number itself rather than by the field name is what makes "prefers text" an
    instruction somebody can act on instead of a preference with nothing to send to.

    Duplicates collapse: the same number in `phone` and `mobile` is one number, and
    showing it twice on the row just makes the salesperson check whether they are
    different.
    """
    seen: list[str] = []
    for number in numbers:
        if number and number not in seen:
            seen.append(number)
    for number in seen:
        if is_mobile(number):
            if draft.mobile is None:
                draft.mobile = number
        elif draft.phone is None:
            draft.phone = number
    # Two mobiles, or two landlines: the second one still belongs on the record
    # rather than being thrown away.
    for number in seen:
        if number not in (draft.phone, draft.mobile):
            if draft.phone is None:
                draft.phone = number
            elif draft.mobile is None:
                draft.mobile = number


# ------------------------------------------------------------ the adapters ---


def _common(payload: dict, draft: LeadDraft) -> LeadDraft:
    """The fields nearly every channel sends under one name or another."""
    draft.email = clean_email(_first(payload, "email", "email_address", "emailAddress"))
    _assign_numbers(
        draft,
        clean_phone(
            _first(payload, "phone", "telephone", "phone_number", "daytime_phone", "landline")
        ),
        clean_phone(_first(payload, "mobile", "mobile_phone", "cell", "cellphone")),
    )
    draft.postcode = clean_postcode(_first(payload, "postcode", "post_code", "zip", "zip_code"))
    draft.city = _first(payload, "city", "town", "locality") or None
    draft.address = _first(payload, "address", "address1", "street_address") or None
    draft.notes = _first(payload, "message", "comments", "enquiry", "notes", "body") or None
    draft.bedrooms_needed = _int(_first(payload, "bedrooms", "bedrooms_needed", "beds"))
    draft.price_min = _money(_first(payload, "price_min", "budget_from", "min_price"))
    draft.price_max = _money(_first(payload, "price_max", "budget_to", "max_price", "budget"))
    draft.has_home_to_sell = _yes(
        _first(payload, "has_home_to_sell", "property_to_sell", "own_home")
    )
    draft.external_ref = (
        _first(payload, "id", "lead_id", "leadId", "enquiry_id", "reference", "ref") or None
    )
    if draft.external_ref is not None:
        draft.external_ref = str(draft.external_ref)[:200]
    return draft


def _named(payload: dict) -> tuple[str, str]:
    """Forename and surname, however this channel chose to send them."""
    first = _first(payload, "first_name", "firstname", "forename", "given_name")
    last = _first(payload, "last_name", "lastname", "surname", "family_name")
    if first and last:
        return str(first).strip(), str(last).strip()

    whole = _first(payload, "name", "full_name", "fullname", "contact_name", "customer_name")
    if whole:
        return _split_name(str(whole))
    if first:
        return str(first).strip(), "(no surname given)"
    raise UnreadablePayload(
        "No name in the payload. Looked for first_name/last_name and name. "
        "Check the field names against what this channel actually sends."
    )


def parse_web_form(payload: dict) -> LeadDraft:
    """Your own form. The fields are whatever you called them, so this is the
    most forgiving adapter - it tries every common spelling."""
    first, last = _named(payload)
    draft = _common(payload, LeadDraft(first_name=first, last_name=last))
    draft.is_prequalified = _yes(
        _first(payload, "prequalified", "is_prequalified", "mortgage_agreed")
    )
    return draft


def parse_rightmove(payload: dict) -> LeadDraft:
    """Rightmove enquiry.

    Rightmove sends the property reference rather than anything about our
    communities, so the reference is kept in notes and the source record's default
    community fills the gap.
    """
    first, last = _named(payload)
    draft = _common(payload, LeadDraft(first_name=first, last_name=last))
    ref = _first(payload, "property_reference", "propertyId", "property_id")
    if ref:
        draft.extra["Rightmove property"] = str(ref)
    if not draft.external_ref:
        draft.external_ref = str(_first(payload, "enquiryId", "enquiry_reference") or "") or None
    return draft


def parse_zoopla(payload: dict) -> LeadDraft:
    first, last = _named(payload)
    draft = _common(payload, LeadDraft(first_name=first, last_name=last))
    ref = _first(payload, "listing_id", "listingId", "property_reference")
    if ref:
        draft.extra["Zoopla listing"] = str(ref)
    return draft


def parse_onthemarket(payload: dict) -> LeadDraft:
    first, last = _named(payload)
    draft = _common(payload, LeadDraft(first_name=first, last_name=last))
    ref = _first(payload, "property_ref", "propertyReference")
    if ref:
        draft.extra["OnTheMarket property"] = str(ref)
    return draft


def parse_meta_lead_ads(payload: dict) -> LeadDraft:
    """Meta lead ads.

    Meta does not send fields - it sends a list of questions and answers, where
    the question names are whatever whoever built the ad typed. So the list is
    flattened into a normal dict first, and then read like anything else.
    """
    flat: dict[str, Any] = dict(payload)
    answers = payload.get("field_data") or payload.get("fieldData") or []
    if isinstance(answers, list):
        for item in answers:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key")
            values = item.get("values") or item.get("value")
            if name:
                flat[str(name)] = values[0] if isinstance(values, list) and values else values

    first, last = _named(flat)
    draft = _common(flat, LeadDraft(first_name=first, last_name=last))
    draft.external_ref = str(payload.get("leadgen_id") or draft.external_ref or "") or None
    campaign = flat.get("campaign_name") or flat.get("ad_name")
    if campaign:
        draft.extra["Ad"] = str(campaign)
    return draft


def parse_google_lead_form(payload: dict) -> LeadDraft:
    """Google lead form extension. Same shape problem as Meta, different names."""
    flat: dict[str, Any] = dict(payload)
    answers = payload.get("user_column_data") or payload.get("userColumnData") or []
    if isinstance(answers, list):
        for item in answers:
            if isinstance(item, dict):
                name = item.get("column_id") or item.get("columnId") or item.get("column_name")
                if name:
                    flat[str(name)] = item.get("string_value") or item.get("stringValue")

    first, last = _named(flat)
    draft = _common(flat, LeadDraft(first_name=first, last_name=last))
    draft.external_ref = str(payload.get("lead_id") or draft.external_ref or "") or None
    campaign = flat.get("campaign_id") or flat.get("campaignId")
    if campaign:
        draft.extra["Google campaign"] = str(campaign)
    return draft


def parse_email(payload: dict) -> LeadDraft:
    """An enquiry that only ever arrives as an email.

    The poller hands us `{subject, from, body}`. The body is scanned for
    `Label: value` lines, which is how essentially every portal notification
    email is laid out, and the sender address is the fallback for the email field.

    This is the most fragile adapter by a distance, and it is included because
    some channels leave no alternative. When it fails, the email is on the failed
    queue with its text intact and somebody can read it in ten seconds - which is
    the difference between fragile and dangerous.
    """
    body = str(payload.get("body") or payload.get("text") or "")
    fields: dict[str, Any] = {}
    for line in body.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label, value = label.strip(), value.strip()
        if label and value and len(label) < 60:
            fields[label] = value

    merged = {**fields, **{k: v for k, v in payload.items() if k not in ("body", "text")}}
    try:
        first, last = _named(merged)
    except UnreadablePayload as exc:
        raise UnreadablePayload(
            f"{exc} The email body was scanned for 'Label: value' lines and "
            f"{len(fields)} were found. Read the message and add a field map to "
            "this source if the layout is different."
        ) from exc

    draft = _common(merged, LeadDraft(first_name=first, last_name=last))
    draft.email = draft.email or clean_email(payload.get("from"))
    if payload.get("subject"):
        draft.extra["Subject"] = str(payload["subject"])
    if not draft.notes:
        draft.notes = body[:1800] or None
    return draft


def parse_generic(payload: dict, field_map: dict[str, str] | None) -> LeadDraft:
    """Anything else, mapped by the field map configured on the source.

    `{"their_field": "our_field"}`. Used for a channel nobody has written an
    adapter for, so a new integration needs configuration rather than a release.
    """
    if not field_map:
        raise UnreadablePayload(
            "This source is set to Generic but has no field map. Add one that "
            "says which incoming field means which of ours, for example "
            '{"customer_email": "email"}.'
        )
    renamed = {field_map.get(k, k): v for k, v in payload.items()}
    first, last = _named(renamed)
    return _common(renamed, LeadDraft(first_name=first, last_name=last))


#: One adapter per kind. Generic takes the source's field map, so it is called
#: separately rather than sitting in this table.
ADAPTERS = {
    IntakeKind.WEB_FORM: parse_web_form,
    IntakeKind.RIGHTMOVE: parse_rightmove,
    IntakeKind.ZOOPLA: parse_zoopla,
    IntakeKind.ONTHEMARKET: parse_onthemarket,
    IntakeKind.META_LEAD_ADS: parse_meta_lead_ads,
    IntakeKind.GOOGLE_LEAD_FORM: parse_google_lead_form,
    IntakeKind.EMAIL: parse_email,
}


def parse(kind: IntakeKind, payload: dict, field_map: dict | None = None) -> LeadDraft:
    """Read a payload with the adapter for its channel."""
    if kind is IntakeKind.GENERIC:
        return parse_generic(payload, field_map)
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        raise UnreadablePayload(f"No adapter for {kind}.")
    return adapter(payload)
