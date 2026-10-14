from django.db import migrations


def restore_walk_in_customers(apps, schema_editor):
    """Every shop needs ONE live Walk-in customer — the till opens every sale on it (s167).
    GATES' was found soft-deleted on prod (deleted by hand from the Customers list), so the till
    started with no customer. Restore a deleted one, or create one, for every shop that has none live."""
    Store = apps.get_model('core', 'Store')
    Customer = apps.get_model('users', 'Customer')
    for store in Store.objects.all():
        walk_ins = Customer._base_manager.filter(store=store, is_walk_in=True)   # unfiltered: sees deleted rows
        if walk_ins.filter(is_deleted=False).exists():
            continue
        dead = walk_ins.order_by('created_at').first()
        if dead is not None:
            dead.is_deleted = False
            dead.deleted_at = None
            dead.save(update_fields=['is_deleted', 'deleted_at'])
            continue
        cust, created = Customer._base_manager.get_or_create(
            store=store, phone_number='0000000000',
            defaults={'name': 'Walk-in', 'is_walk_in': True},
        )
        changed = []
        if not cust.is_walk_in:
            cust.is_walk_in = True
            changed.append('is_walk_in')
        if cust.is_deleted:
            cust.is_deleted, cust.deleted_at = False, None
            changed += ['is_deleted', 'deleted_at']
        if changed:
            cust.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0013_superadmin_storeless'),
        ('core', '0031_storesettings_default_weight_unit'),
    ]
    operations = [
        migrations.RunPython(restore_walk_in_customers, migrations.RunPython.noop),
    ]
