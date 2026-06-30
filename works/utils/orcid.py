# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import re

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_ORCID_PREFIXES = (
    "https://orcid.org/",
    "http://orcid.org/",
    "orcid.org/",
)


def normalize_orcid(value) -> str | None:
    """Return the bare ORCID iD (e.g. '0000-0002-1825-0097') or None.

    Strips common URL prefixes, upper-cases the check digit 'X', and validates
    the 16-digit hyphenated format. Returns None for blank or malformed input.
    """
    if not value:
        return None
    s = str(value).strip()
    for prefix in _ORCID_PREFIXES:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix) :]
            break
    s = s.upper()
    if _ORCID_RE.match(s):
        return s
    return None
