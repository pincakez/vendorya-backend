from django.db import migrations


SEED_CURRENCIES = [
    # (code, symbol, name, position)
    ('EGP', 'EGP', 'Egyptian Pound',   'SUFFIX'),
    ('LE',  'LE',  'Egyptian Pound (local)', 'SUFFIX'),
    ('USD', '$',   'US Dollar',        'PREFIX'),
    ('EUR', '€',   'Euro',             'PREFIX'),
    ('SAR', 'SAR', 'Saudi Riyal',      'SUFFIX'),
    ('AED', 'AED', 'UAE Dirham',       'SUFFIX'),
]


def seed_and_backfill(apps, schema_editor):
    Currency = apps.get_model('core', 'Currency')
    Store    = apps.get_model('core', 'Store')

    seeded = {}
    for code, symbol, name, position in SEED_CURRENCIES:
        obj, _ = Currency.objects.get_or_create(
            code=code,
            defaults={'symbol': symbol, 'name': name, 'position': position, 'is_active': True},
        )
        seeded[code] = obj

    default_egp = seeded['EGP']

    for store in Store.objects.all():
        if store.currency_id is not None:
            continue
        sym = (store.currency_symbol or '').strip()
        # Try exact code match, then symbol match (case-insensitive), then fall back.
        match = (Currency.objects.filter(code__iexact=sym).first()
                 if sym else None)
        if match is None and sym:
            match = Currency.objects.filter(symbol__iexact=sym).first()
        if match is None and sym:
            # Free-form symbol the user typed (e.g. "د.ع").  Create on the fly so
            # we preserve their intent rather than silently rewriting to EGP.
            match = Currency.objects.create(
                code=sym[:10],
                symbol=sym[:10],
                name=f"Imported: {sym}",
                position='SUFFIX',
                is_active=True,
            )
        store.currency = match or default_egp
        store.save(update_fields=['currency'])


def reverse(apps, schema_editor):
    Store    = apps.get_model('core', 'Store')
    Currency = apps.get_model('core', 'Currency')
    Store.objects.update(currency=None)
    Currency.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_currency_timezone_format_additive'),
    ]

    operations = [
        migrations.RunPython(seed_and_backfill, reverse),
    ]
