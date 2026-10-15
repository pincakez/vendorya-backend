from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('services', '0004_service_desk_upgrade13'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicepart',
            name='serial_numbers',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
