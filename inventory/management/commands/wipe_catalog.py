"""
wipe_catalog — hard-wipe all product / catalog / invoice data across ALL stores,
in preparation for a clean reseed (superfix §1).

What it WIPES (hard TRUNCATE ... RESTART IDENTITY CASCADE — real row removal,
NOT soft delete, sequences reset to 1):
  • inventory: Product, ProductVariant, ProductAttribute, ProductUnit,
    StockLevel, StockBatch, BundleItem, StockAdjustment, StockTransfer(+Item),
    StorageStock, StorageMovement, Supplier, Category
  • finance:   SalesInvoice(+Item), SaleBatchConsumption, RefundInvoice(+Item),
    PurchaseInvoice(+Item), SupplierPayment, Payment, InvoiceSequence,
    PurchaseSequence
  • pos:       POSFavoriteItem

What it KEEPS (store structure / config — never touched):
  • Stores, users, settings
  • inventory: Tax, AttributeDefinition (BRAND_AR / ACTIVE_ING keys), StorageLocation
  • finance:   Expense(+Category), PaymentMethod, WorkShift

SAFETY: dry-run by default — prints the row counts it WOULD delete and exits.
Pass --execute to actually wipe. Yakot pre-authorised the wipe (superfix decision #1);
the flag only prevents an accidental run. Back up the dev DB first (backup_db.sh).

Usage:
    python manage.py wipe_catalog            # dry run — shows counts only
    python manage.py wipe_catalog --execute  # actually wipe
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.apps import apps

# (app_label, ModelName) — order is irrelevant for TRUNCATE CASCADE, but we list
# every table explicitly so the dry-run report is complete and auditable.
WIPE_TARGETS = [
    ("inventory", "Product"),
    ("inventory", "ProductVariant"),
    ("inventory", "ProductAttribute"),
    ("inventory", "ProductUnit"),
    ("inventory", "StockLevel"),
    ("inventory", "StockBatch"),
    ("inventory", "BundleItem"),
    ("inventory", "StockAdjustment"),
    ("inventory", "StockTransfer"),
    ("inventory", "StockTransferItem"),
    ("inventory", "StorageStock"),
    ("inventory", "StorageMovement"),
    ("inventory", "Supplier"),
    ("inventory", "Category"),
    ("finance", "SalesInvoice"),
    ("finance", "SalesInvoiceItem"),
    ("finance", "SaleBatchConsumption"),
    ("finance", "RefundInvoice"),
    ("finance", "RefundItem"),
    ("finance", "PurchaseInvoice"),
    ("finance", "PurchaseItem"),
    ("finance", "SupplierPayment"),
    ("finance", "Payment"),
    ("finance", "InvoiceSequence"),
    ("finance", "PurchaseSequence"),
    ("pos", "POSFavoriteItem"),
]


class Command(BaseCommand):
    help = "Hard-wipe all product/catalog/invoice data across all stores (superfix §1). Dry-run unless --execute."

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually perform the wipe. Without this flag the command only reports counts.",
        )

    def handle(self, *args, **options):
        execute = options["execute"]

        # Resolve tables + current row counts.
        rows = []
        total = 0
        for app_label, model_name in WIPE_TARGETS:
            model = apps.get_model(app_label, model_name)
            count = model._base_manager.count()
            total += count
            rows.append((model._meta.db_table, count))

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\nwipe_catalog — {len(rows)} tables, {total:,} rows total"))
        for table, count in rows:
            self.stdout.write(f"  {table:38s} {count:>10,}")

        if not execute:
            self.stdout.write(self.style.WARNING(
                "\nDRY RUN — nothing deleted. Re-run with --execute to wipe.\n"))
            return

        tables = [t for t, _ in rows]
        table_sql = ", ".join(f'"{t}"' for t in tables)
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(f"TRUNCATE {table_sql} RESTART IDENTITY CASCADE;")

        self.stdout.write(self.style.SUCCESS(
            f"\n✓ Wiped {total:,} rows across {len(tables)} tables (sequences reset).\n"))
