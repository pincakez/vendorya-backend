from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_storesettings_pos_clock_24h'),
    ]

    operations = [
        migrations.AddField(
            model_name='storesettings',
            name='lock_timeout_minutes',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Lock Screen Timeout (minutes)'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='lock_pin_hash',
            field=models.CharField(blank=True, max_length=128, verbose_name='Lock Screen PIN (hashed)'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='lock_logo',
            field=models.ImageField(blank=True, null=True, upload_to='lock_logos/', verbose_name='Lock Screen Logo'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='lock_facts_bank',
            field=models.JSONField(blank=True, default=list, verbose_name='Lock Screen Facts Bank'),
        ),
    ]
