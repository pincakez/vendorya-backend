from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0021_variant_sku_backfill'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='productvariant',
            constraint=models.UniqueConstraint(condition=models.Q(('sku__isnull', False)), fields=('store', 'sku'), name='uniq_variant_sku_per_store'),
        ),
        migrations.AddConstraint(
            model_name='productvariant',
            constraint=models.UniqueConstraint(condition=models.Q(('sku2__isnull', False)), fields=('store', 'sku2'), name='uniq_variant_sku2_per_store'),
        ),
    ]
