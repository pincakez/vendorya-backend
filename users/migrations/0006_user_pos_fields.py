from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_storesettings_tax_enabled'),
        ('users', '0005_backfill_walk_in_customers'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='default_branch',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='default_for_users',
                to='core.branch',
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='pos_settings',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
