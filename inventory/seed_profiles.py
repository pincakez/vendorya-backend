"""Store seed profiles — broad starter categories applied at store creation.

A *seed profile* is a one-time, creation-time action (like the capability
presets): pick one when onboarding a store and it lays down a small set of
BROAD top-level categories so the owner isn't staring at an empty tree. It is
NOT a stored field and never locks anything — the owner edits/deletes freely
afterwards in Inventory → Categories.

To add a vertical later, append one entry to SEED_PROFILES. Keep the buckets
BROAD (a handful, top-level) — the whole point of Phase 2 was killing the
garbage deep auto-category tree.
"""
from .models import Category

# profile key -> ordered list of broad top-level category names.
SEED_PROFILES = {
    'none': [],
    'pharmacy': ['Drugs', 'Cosmetics', 'Kids', 'Medical Tools'],
}


def apply_seed_profile(store, profile):
    """Create the profile's broad buckets for `store` (idempotent on name).

    Returns the list of Category rows that were freshly created. A no-op for an
    unknown profile or 'none'. Safe to re-run: existing active categories with
    the same top-level name are left untouched.
    """
    names = SEED_PROFILES.get(profile or 'none', [])
    created = []
    for name in names:
        cat, was_created = Category.objects.get_or_create(
            store=store, name=name, parent=None,
        )
        if was_created:
            created.append(cat)
    return created
