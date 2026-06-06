from django.db import migrations, models


_DEFAULT_METHODS = [
    {'name': 'Cash',        'is_cash': True,  'is_agel': False},
    {'name': 'InstaPay',    'is_cash': False, 'is_agel': False},
    {'name': 'E-Wallet',    'is_cash': False, 'is_agel': False},
    {'name': 'Credit Card', 'is_cash': False, 'is_agel': False},
    {'name': 'Ajel',        'is_cash': False, 'is_agel': True},
]


def seed_payment_methods(apps, schema_editor):
    """Seed default payment methods for every store that has none."""
    Store = apps.get_model('core', 'Store')
    PaymentMethod = apps.get_model('finance', 'PaymentMethod')
    for store in Store.objects.filter(is_deleted=False):
        if PaymentMethod.objects.filter(store=store, is_deleted=False).exists():
            continue
        for m in _DEFAULT_METHODS:
            PaymentMethod.objects.create(store=store, **m)


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentmethod',
            name='is_agel',
            field=models.BooleanField(
                default=False,
                help_text='Agel (credit) sales only. Enforces credit limit and tracks customer debt.',
            ),
        ),
        migrations.RunPython(seed_payment_methods, migrations.RunPython.noop),
    ]
