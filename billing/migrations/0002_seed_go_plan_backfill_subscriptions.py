from decimal import Decimal

from django.db import migrations


def seed_go_and_backfill(apps, schema_editor):
    SubscriptionPlan = apps.get_model('billing', 'SubscriptionPlan')
    Subscription     = apps.get_model('billing', 'Subscription')
    Store            = apps.get_model('core', 'Store')

    plan, _ = SubscriptionPlan.objects.get_or_create(
        name='GO',
        defaults={
            'description': 'Default plan — unlimited usage during early access. Pricing TBD.',
            'monthly_price': Decimal('0.00'),
            'annual_price':  Decimal('0.00'),
            'currency': 'EGP',
            'max_users':    None,
            'max_branches': None,
            'max_products': None,
            'max_invoices_per_month': None,
            'is_active': True,
        },
    )

    for store in Store.objects.filter(is_deleted=False):
        Subscription.objects.get_or_create(
            store=store,
            defaults={'plan': plan, 'status': 'ACTIVE'},
        )


def reverse(apps, schema_editor):
    Subscription     = apps.get_model('billing', 'Subscription')
    SubscriptionPlan = apps.get_model('billing', 'SubscriptionPlan')
    Subscription.objects.all().delete()
    SubscriptionPlan.objects.filter(name='GO').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0001_initial'),
        ('core',    '0006_activitylog_operation_type_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_go_and_backfill, reverse),
    ]
