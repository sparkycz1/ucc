"""Which community.ui.com release posts belong to which synced thing.

A release is identified in ucc by (kind, product, version):

- kind=os: product is the console model (`UDM-Pro`, `UNVR`, ...) — UniFi
  OS ships per console family ("UniFi OS - Dream Machines", "UniFi OS -
  Network Video Recorders", ...), and the same version number can mean a
  different build for another family.
- kind=app: product is Site Manager's application key (`network`,
  `protect`, `innerspace`, ...).
- kind=firmware: product is the device model (`G5 Flex`, `USW-24-PoE`,
  ...); community.ui.com posts firmware per family ("UniFi Protect
  Cameras", "UniFi Switch", ...).
"""

from __future__ import annotations

from app.db.models.release import ReleaseKind

# Application key → the exact community.ui.com release title.
APP_TITLES = {
    "network": "UniFi Network Application",
    "protect": "UniFi Protect Application",
    "access": "UniFi Access Application",
    "talk": "UniFi Talk Application",
    "connect": "UniFi Connect Application",
    "innerspace": "UniFi InnerSpace",
    "drive": "UniFi Drive Application",
    "identity": "UniFi Identity Enterprise",
}

# Console model prefix → words in its "UniFi OS - ..." release title. The
# first match wins, so more specific prefixes come first.
_OS_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("ENVR",), "enterprise network video recorder"),
    (("UNVR", "UNVR-PRO", "UNVRPRO", "NVR", "UNVR4"), "network video recorder"),
    (("EFG",), "enterprise firewall"),
    (("UDW",), "dream wall"),
    # UDRULT is the Cloud Gateway Ultra's model code, not a Dream Router.
    (("UDRULT", "UCG", "UXG"), "cloud gateway"),
    (("UDR",), "dream router"),
    (("UDM", "UDMPRO", "UDMSE"), "dream machine"),
    (("UX", "UXMAX"), "express"),
    (("UCK", "CK", "UCKP", "UCKG2"), "cloud key"),
)

# Device model prefix → words in its firmware release title.
_FIRMWARE_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("UVC", "G3", "G4", "G5", "G6", "AI ", "AI-"), "protect camera"),
    (("UP-", "UPV", "VIEWPORT"), "viewport"),
    (("UFP", "UP CHIME", "UP-CHIME"), "protect"),
    (("U6", "U7", "UAP", "E7", "UWB", "AC-", "NANOHD", "FLEXHD", "BEACONHD"), "access point"),
    (("USW", "US-", "US8", "US16", "US24", "US48", "USL", "USP", "ECS", "USM"), "switch"),
    (("UXG", "UGW", "USG"), "gateway"),
    (("UA-", "UAH", "UA "), "access"),
    (("UBB", "UMR", "ULTE", "UDB"), "device"),
)

# Release titles that are never firmware/OS/app releases we track.
_NEVER = ("ios", "android", "advisory", "community update", "desktop", "mobile")


def _family(product: str, families: tuple[tuple[tuple[str, ...], str], ...]) -> str | None:
    upper = product.upper().strip()
    compact = upper.replace("-", "").replace(" ", "")
    for prefixes, words in families:
        for prefix in prefixes:
            if upper.startswith(prefix) or compact.startswith(prefix.replace("-", "")):
                return words
    return None


def os_family(model: str) -> str | None:
    return _family(model, _OS_FAMILIES)


def firmware_family(model: str) -> str | None:
    return _family(model, _FIRMWARE_FAMILIES)


def search_term(kind: str, product: str) -> str:
    """What to type into community.ui.com's release search to find this
    product's posts (the newest first)."""
    if kind == ReleaseKind.APP:
        return APP_TITLES.get(product.lower(), f"UniFi {product.capitalize()}")
    if kind == ReleaseKind.OS:
        family = os_family(product)
        return f"UniFi OS {family}" if family else "UniFi OS"
    family = firmware_family(product)
    return f"UniFi {family}" if family else f"UniFi {product}"


def title_matches(kind: str, product: str, title: str) -> bool:
    """Whether a community release `title` is about `kind`/`product`."""
    lowered = title.lower().strip()
    if any(word in lowered for word in _NEVER):
        return False
    is_os = lowered.startswith("unifi os")
    if kind == ReleaseKind.OS:
        if not is_os:
            return False
        family = os_family(product)
        if family is None:
            return True
        if family == "network video recorder" and "enterprise" in lowered:
            return False
        return family in lowered
    if is_os:
        return False
    if kind == ReleaseKind.APP:
        expected = APP_TITLES.get(product.lower())
        if expected is not None:
            return lowered == expected.lower()
        return product.lower() in lowered and "application" in lowered
    if "application" in lowered:
        return False
    family = firmware_family(product)
    if family is None:
        return True
    return all(word.rstrip("s") in lowered for word in family.split())


def community_search_url(kind: str, product: str, version: str | None) -> str:
    """community.ui.com's own release search, for when no post matches."""
    from urllib.parse import urlencode

    query = search_term(kind, product) + (f" {version}" if version else "")
    return "https://community.ui.com/releases?" + urlencode({"q": query})


def version_key(version: str | None) -> tuple[int, ...] | None:
    """`10.0.160` → (10, 0, 160), comparable; `None` if not a version."""
    from app.releases.versions import normalize_version

    normalized = normalize_version(version)
    if normalized is None:
        return None
    try:
        return tuple(int(part) for part in normalized.split("."))
    except ValueError:
        return None


def is_newer(candidate: str | None, current: str | None) -> bool:
    new, old = version_key(candidate), version_key(current)
    if new is None:
        return False
    if old is None:
        return True
    return new > old
