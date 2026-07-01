# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

from django.utils.text import slugify


def unique_slug(name, max_length, exists, fallback):
    """Slugify ``name`` (truncated to ``max_length``), appending ``-N`` on
    collision. ``exists(candidate)`` returns whether the candidate is taken.
    Pure/model-agnostic so it can be shared between ``Source._generate_unique_slug()``
    and the data migration that backfills legacy slug-less rows (which can't call
    instance methods on the frozen historical model)."""
    base = (slugify(name)[:max_length].rstrip("-") or fallback)[:max_length]
    candidate = base
    n = 2
    while exists(candidate):
        suffix = f"-{n}"
        candidate = f"{base[: max_length - len(suffix)].rstrip('-')}{suffix}"
        n += 1
    return candidate
