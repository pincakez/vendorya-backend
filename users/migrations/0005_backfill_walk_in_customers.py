from django.db import migrations


def create_walk_in_customers(apps, schema_editor):
    """Seed a default Walk-in customer for every existing store that lacks one.
    New stores get theirs via the Store post_save signal."""
    Store = apps.get_model('core', 'Store')
    Customer = apps.get_model('users', 'Customer')
    for store in Store.objects.all():
        if Customer.objects.filter(store=store, is_walk_in=True).exists():
            continue
        # Avoid colliding with an existing customer on the reserved phone.
        cust, created = Customer.objects.get_or_create(
            store=store, phone_number='0000000000',
            defaults={'name': 'Walk-in', 'is_walk_in': True},
        )
        if not created and not cust.is_walk_in:
            cust.is_walk_in = True
            cust.save(update_fields=['is_walk_in'])


def reverse(apps, schema_editor):
    Customer = apps.get_model('users', 'Customer')
    Customer.objects.filter(is_walk_in=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0004_customer_is_walk_in'),
        ('core', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(create_walk_in_customers, reverse),
    ]
