from django.db import migrations


def create_default_storage(apps, schema_editor):
    """Give every existing store one default 'Storage' location so the feature
    works out of the box (move-to-storage needs a target)."""
    Store = apps.get_model('core', 'Store')
    StorageLocation = apps.get_model('inventory', 'StorageLocation')
    for store in Store.objects.all():
        StorageLocation.objects.get_or_create(
            store=store,
            name='Storage',
            defaults={'description': 'Default storage location', 'is_active': True},
        )


def remove_default_storage(apps, schema_editor):
    StorageLocation = apps.get_model('inventory', 'StorageLocation')
    StorageLocation.objects.filter(name='Storage', description='Default storage location').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0007_storagelocation_storagemovement_storagestock'),
    ]

    operations = [
        migrations.RunPython(create_default_storage, remove_default_storage),
    ]
