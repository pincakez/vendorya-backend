from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_storesettings_pos_double_print_default_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storesettings',
            name='receipt_copies',
            field=models.PositiveSmallIntegerField(default=1, verbose_name='Receipt copies (1–5)'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='receipt_auto_cut',
            field=models.BooleanField(default=True, verbose_name='Auto-cut after print'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='receipt_cut_feed',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Cut feed distance mm (0–20)'),
        ),
    ]
