from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0020_productvariant_sku2_productvariant_sku_number_and_more'),
    ]

    operations = [
        # s159 back-fill: every variant learns its shop, and every old fixed 10-digit SKU
        # (product 4 + supplier 3 + store 3) gets its parts stored, so the new maker counts on.
        migrations.RunSQL(
            sql=[
                "UPDATE inventory_productvariant v SET store_id = p.store_id "
                "FROM inventory_product p WHERE v.product_id = p.id",
                "UPDATE inventory_productvariant SET sku_number = CAST(substr(sku, 1, 4) AS integer), "
                "sku_prefix = substr(sku, 5, 3) WHERE sku ~ '^[0-9]{10}$'",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
