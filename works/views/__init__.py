# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Works app views package.

This package organizes views into logical modules:
- auth: Authentication and user account management
- work_views: Work-specific views (landing pages, lists)
- data: Data export endpoints

For backward compatibility, all views are re-exported from this __init__.py
"""

# Import from submodules
from .auth import (
    add_subscriptions,
    authenticate_via_magic_link,
    change_useremail,
    confirm_account_deletion,
    confirm_email_change,
    confirmation_login,
    customlogout,
    finalize_account_deletion,
    loginres,
    random_recognition_username,
    recognition_board,
    request_delete,
    unsubscribe,
    user_settings,
    user_subscriptions,
)
from .data import (
    download_collection_csv,
    download_collection_geojson,
    download_collection_gpkg,
    download_country_csv,
    download_country_geojson,
    download_country_gpkg,
    download_csv,
    download_geojson,
    download_geopackage,
    generate_geopackage,
)
from .work_views import (
    contribute,
    contribute_next,
    statistics_page,
    work_landing,
    work_preview_png,
    works_list,
)

# Re-export all for backward compatibility
__all__ = [
    # Auth views
    "loginres",
    "confirmation_login",
    "authenticate_via_magic_link",
    "customlogout",
    "user_settings",
    "random_recognition_username",
    "recognition_board",
    "user_subscriptions",
    "add_subscriptions",
    "unsubscribe",
    "change_useremail",
    "confirm_email_change",
    "request_delete",
    "confirm_account_deletion",
    "finalize_account_deletion",
    # Work views
    "contribute",
    "contribute_next",
    "statistics_page",
    "works_list",
    "work_landing",
    "work_preview_png",
    # Data exports
    "download_geojson",
    "download_geopackage",
    "download_csv",
    "generate_geopackage",
    "download_collection_geojson",
    "download_collection_gpkg",
    "download_collection_csv",
    "download_country_geojson",
    "download_country_gpkg",
    "download_country_csv",
]
