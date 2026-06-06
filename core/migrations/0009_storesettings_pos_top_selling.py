from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_storesettings_tax_enabled'),
        ('inventory', '0004_product_delete_note_product_delete_reason_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='storesettings',
            name='pos_top_selling_period',
            field=models.CharField(
                choices=[('today', 'Today'), ('week', 'This Week'), ('month', 'This Month'), ('all', 'All Time')],
                default='month',
                max_length=10,
                verbose_name='Top Selling Period',
            ),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='pos_top_selling_category',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='inventory.category',
                verbose_name='Top Selling Category Filter',
            ),
        ),
        migrations.AddField(
            model_name='storesettings',
            name='pos_top_selling_limit',
            field=models.PositiveSmallIntegerField(
                default=8,
                help_text='Max items shown in the POS Top Selling panel (4–10).',
                verbose_name='Top Selling Limit',
            ),
        ),
    ]
