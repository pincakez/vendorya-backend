from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('public_api', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='apikey',
            name='owner_name',
            field=models.CharField(blank=True, default='', help_text='Person or org this key is issued to.', max_length=120, verbose_name='Owner name'),
        ),
        migrations.AddField(
            model_name='apikey',
            name='max_rpd',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='Max requests/day'),
        ),
        migrations.AddField(
            model_name='apikey',
            name='max_rpm',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='Max requests/min'),
        ),
        migrations.AddField(
            model_name='apikey',
            name='penalty_minutes',
            field=models.PositiveIntegerField(default=0, help_text='Lockout duration after rate-limit hit.', verbose_name='Penalty (minutes)'),
        ),
    ]
