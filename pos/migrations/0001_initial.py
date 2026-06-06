import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('core', '0009_storesettings_pos_top_selling'),
        ('inventory', '0004_product_delete_note_product_delete_reason_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='POSFavoriteItem',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('order', models.PositiveSmallIntegerField(default=0)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='inventory.product')),
                ('store', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pos_favorites', to='core.store')),
            ],
            options={
                'ordering': ['order'],
                'unique_together': {('store', 'product')},
            },
        ),
    ]
