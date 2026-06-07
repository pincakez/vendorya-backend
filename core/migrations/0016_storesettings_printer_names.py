from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_storesettings_service_notify_hours_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storesettings',
            name='label_printer_name',
            field=models.CharField(blank=True, max_length=120, verbose_name='Label Printer Name'),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='receipt_printer_name',
            field=models.CharField(blank=True, max_length=120, verbose_name='Receipt Printer Name'),
        ),
    ]
