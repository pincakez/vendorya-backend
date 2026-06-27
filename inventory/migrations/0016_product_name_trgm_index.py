from django.db import migrations


class Migration(migrations.Migration):
    """
    Enable pg_trgm extension and create a GIN index on inventory_product.name.
    This makes icontains lookups over 27k Memory Base rows fast (~300ms vs 3-4s).
    No model changes — DB-side performance only.
    """

    dependencies = [
        ('inventory', '0015_product_source_alter_productvariant_sku'),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                "CREATE INDEX IF NOT EXISTS inventory_product_name_trgm_gin "
                "ON inventory_product USING GIN (name gin_trgm_ops);",
            ],
            reverse_sql=[
                "DROP INDEX IF EXISTS inventory_product_name_trgm_gin;",
            ],
        ),
    ]
