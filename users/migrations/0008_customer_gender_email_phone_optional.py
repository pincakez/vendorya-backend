from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_customer_store_credit'),
    ]

    operations = [
        # 1. Add gender
        migrations.AddField(
            model_name='customer',
            name='gender',
            field=models.CharField(
                choices=[('MALE', 'Male'), ('FEMALE', 'Female')],
                default='MALE',
                max_length=6,
            ),
        ),
        # 2. Add email
        migrations.AddField(
            model_name='customer',
            name='email',
            field=models.EmailField(blank=True, default=''),
        ),
        # 3. Make phone_number nullable (allows name-only customers)
        migrations.AlterField(
            model_name='customer',
            name='phone_number',
            field=models.CharField(
                blank=True,
                default=None,
                help_text='Phone number must be unique per store (if provided).',
                max_length=20,
                null=True,
                verbose_name='Phone Number',
            ),
        ),
        # 4. Drop the old unique_together
        migrations.AlterUniqueTogether(
            name='customer',
            unique_together=set(),
        ),
        # 5. Add partial unique constraint (only enforced when phone is non-null)
        migrations.AddConstraint(
            model_name='customer',
            constraint=models.UniqueConstraint(
                condition=Q(phone_number__isnull=False),
                fields=['store', 'phone_number'],
                name='customer_store_phone_unique',
            ),
        ),
    ]
