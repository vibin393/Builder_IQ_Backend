"""Address → coordinates, so a typed address becomes a pin on the map.

Uses OpenStreetMap's Nominatim — free, no API key. Nominatim asks callers to send a
real User-Agent and to keep bulk use to about one request a second; the backfill
script spaces its calls, and a single interactive lookup here is well within that.

Everything is best-effort: geocoding a lead must never stop the lead being saved, so
every failure (no network, no result, a timeout) returns None and the caller carries
on without coordinates.
"""

from decimal import Decimal

import httpx

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "BuilderIQ/1.0 (builder CRM geocoding)"
#: This tenant is a UK builder; appending the country disambiguates town names like
#: "Christchurch" (Dorset, not New Zealand) and "Poole".
_DEFAULT_REGION = "United Kingdom"
_SIX_DP = Decimal("0.000001")


def build_query(
    address: str | None = None,
    city: str | None = None,
    postcode: str | None = None,
    *,
    region: str | None = _DEFAULT_REGION,
) -> str | None:
    """Assemble a geocodable string from whatever address parts are known."""
    parts = [p.strip() for p in (address, city, postcode) if p and p.strip()]
    if not parts:
        return None
    query = ", ".join(parts)
    if region and region.lower() not in query.lower():
        query = f"{query}, {region}"
    return query


async def geocode(query: str | None, *, timeout: float = 6.0) -> tuple[Decimal, Decimal] | None:
    """Return (latitude, longitude) for an address string, or None if it can't be found."""
    if not query:
        return None
    try:
        params = {"q": query, "format": "json", "limit": 1}
        # This tenant is a UK builder; biasing to GB keeps an ambiguous town name in
        # the right country instead of matching a same-named place abroad.
        if _DEFAULT_REGION and "united kingdom" in _DEFAULT_REGION.lower():
            params["countrycodes"] = "gb"
        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": _USER_AGENT}
        ) as client:
            resp = await client.get(_NOMINATIM, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        lat = Decimal(str(data[0]["lat"])).quantize(_SIX_DP)
        lon = Decimal(str(data[0]["lon"])).quantize(_SIX_DP)
        return lat, lon
    except Exception:
        # Network down, rate-limited, bad payload — never let geocoding break a save.
        return None
