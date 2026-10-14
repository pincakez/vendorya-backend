# 2026-10-14: one OPEN shift per person — a double-click on "Open shift" made two open drawers that both
# counted the same sales as cash in (→ a false "cash short" on close).

from django.db import migrations, models


def refuse_if_duplicates(apps, schema_editor):
    """The constraint can't be added while someone already has 2+ open shifts. Fail loudly with the list
    instead of closing a drawer behind anyone's back — a person decides which one to close (Shifts page)."""
    WorkShift = apps.get_model('finance', 'WorkShift')
    dupes = (WorkShift.objects.filter(status='OPEN').values('user', 'user__username', 'store__name')
             .annotate(n=models.Count('id')).filter(n__gt=1))
    if dupes:
        lines = ', '.join(f"{d['user__username']} @ {d['store__name']} ({d['n']} open)" for d in dupes)
        raise RuntimeError(
            'Cannot add one_open_shift_per_user: these users have more than one OPEN shift — '
            f'close the extra ones first, then migrate again: {lines}')


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0019_sale_purchase_refund_serials_warranty'),
    ]

    operations = [
        # Read-only check; nothing to undo.
        migrations.RunPython(refuse_if_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='workshift',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'OPEN')), fields=('user',),
                                               name='one_open_shift_per_user'),
        ),
    ]
