from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_dashboardlayout'),
    ]

    operations = [
        migrations.AddField(
            model_name='storesettings',
            name='autocomplete_source',
            field=models.CharField(
                choices=[('memory_base', 'Memory Base'), ('store_history', 'Store History')],
                default='memory_base',
                help_text='Product pool the name-search autocomplete draws from.',
                max_length=20,
                verbose_name='Autocomplete source',
            ),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='mb_auto_register',
            field=models.BooleanField(
                default=True,
                help_text='Automatically add every new product to the shared Memory Base reference pool when it is created.',
                verbose_name='Auto-register new items in Memory Base',
            ),
        ),
    ]
