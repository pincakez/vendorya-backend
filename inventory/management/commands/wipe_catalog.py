"""
wipe_catalog — hard-wipe all product / catalog / invoice data across ALL stores,
in preparation for a clean reseed (superfix §1).

What it WIPES (real row removal across all stores — NOT soft delete):
  • inventory: Product, ProductVariant, ProductAttribute, ProductUnit,
    StockLevel, StockBatch, BundleItem, StockAdjustment, StockTransfer(+Item),
    StorageStock, StorageMovement, Supplier, Category
  • finance:   SalesInvoice(+Item), SaleBatchConsumption, RefundInvoice(+Item),
    PurchaseInvoice(+Item), SupplierPayment, Payment, InvoiceSequence,
    PurchaseSequence   (deleting the *sequence* rows resets invoice numbering)
  • pos:       POSFavoriteItem

What it KEEPS (store structure / config — never touched):
  • Stores, StoreSettings, users, branches, billing/subscriptions
  • inventory: Tax, AttributeDefinition (BRAND_AR / ACTIVE_ING keys), StorageLocation
  • finance:   Expense(+Category), PaymentMethod, WorkShift
  • services:  Service rows (their invoice_id link is nulled, the service is kept)

────────────────────────────────────────────────────────────────────────────
HOW IT WORKS — and why NOT `TRUNCATE ... CASCADE`:
  An earlier version used `TRUNCATE <tables> CASCADE`. That is CATASTROPHIC here:
  `TRUNCATE ... CASCADE` is purely *constraint-based* — it truncates every table
  with a FK pointing at the named tables, regardless of whether any row actually
  references them, and it follows that chain transitively. Because `core_store`
  carries `default_category_id` / `default_supplier_id` FKs (and StoreSettings a
  `pos_top_selling_category_id`, Service an `invoice_id`), CASCADE walked from
  inventory_category/supplier → core_store → EVERY per-store table + users. It
  would have wiped the entire database (all stores, users, subscriptions,
  settings) — and the 26-table dry-run report hid that.

  This version instead:
    1. Auto-discovers "bridge" FKs — columns on KEPT tables that point INTO the
       wiped set — and sets them to NULL (mirrors Django's SET_NULL, which raw
       SQL would otherwise bypass).
    2. DELETEs the wiped tables in child→parent (topological) order. DELETE is
       *data-driven*: it only removes rows from the named tables and never
       touches anything outside the set.
  The dry-run now reports the bridges it will null AND asserts (via a rolled-back
  probe) that nothing outside the 26 tables would be affected.
────────────────────────────────────────────────────────────────────────────

SAFETY: dry-run by default — prints counts + bridges, runs a rollback probe, exits.
Pass --execute to actually wipe. Yakot pre-authorised the wipe (superfix decision #1);
the flag only prevents an accidental run. Back up the dev DB first (backup_db.sh).

Usage:
    python manage.py wipe_catalog            # dry run — counts, bridges, probe
    python manage.py wipe_catalog --execute  # actually wipe
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.apps import apps

# (app_label, ModelName) — the tables whose ROWS get deleted (across all stores).
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
    help = ("Hard-wipe all product/catalog/invoice data across all stores "
            "(superfix §1) via ordered DELETEs — never TRUNCATE CASCADE. "
            "Dry-run unless --execute.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute", action="store_true",
            help="Actually perform the wipe. Without this flag it only reports + probes.")

    # ── helpers ────────────────────────────────────────────────────────────
    def _tables(self):
        return [apps.get_model(a, m)._meta.db_table for a, m in WIPE_TARGETS]

    def _discover_bridges(self, cur, tableset):
        """FK columns on KEPT tables that point INTO the wiped set → (table, column).
        These must be nulled before the rows they point at can be deleted."""
        cur.execute("""
            SELECT con.conrelid::regclass::text AS referencing_table,
                   att.attname                  AS column_name,
                   con.confrelid::regclass::text AS referenced_table,
                   att.attnotnull
            FROM pg_constraint con
            JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
            JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
            WHERE con.contype = 'f'
              AND con.confrelid::regclass::text = ANY(%s)
              AND con.conrelid::regclass::text <> ALL(%s)
            ORDER BY 1, 2
        """, [list(tableset), list(tableset)])
        return cur.fetchall()

    def _delete_order(self, cur, tables):
        """Topological sort: a table that references another is deleted first.
        Returns the 26 tables ordered child→parent (Kahn's algorithm)."""
        tableset = set(tables)
        cur.execute("""
            SELECT con.conrelid::regclass::text, con.confrelid::regclass::text
            FROM pg_constraint con
            WHERE con.contype = 'f'
              AND con.conrelid::regclass::text = ANY(%s)
              AND con.confrelid::regclass::text = ANY(%s)
              AND con.conrelid <> con.confrelid          -- ignore self-refs (intra-statement)
        """, [tables, tables])
        # edge ref -> target  ⇒ ref must be deleted before target.
        referenced_by = {t: set() for t in tableset}   # target -> {tables referencing it}
        for ref, target in cur.fetchall():
            if ref != target:
                referenced_by[target].add(ref)
        order, remaining = [], set(tableset)
        while remaining:
            # safe to delete now = nothing remaining references it
            ready = sorted(t for t in remaining
                           if not (referenced_by[t] & remaining))
            if not ready:
                raise CommandError(f"FK cycle among wiped tables: {sorted(remaining)}")
            order.extend(ready)
            remaining -= set(ready)
        return order

    # ── main ───────────────────────────────────────────────────────────────
    def handle(self, *args, **options):
        execute = options["execute"]
        tables = self._tables()
        tableset = set(tables)

        with connection.cursor() as cur:
            # row counts (raw SQL → manager-agnostic, counts every row)
            counts, total = [], 0
            for t in tables:
                cur.execute(f'SELECT count(*) FROM "{t}"')
                c = cur.fetchone()[0]
                total += c
                counts.append((t, c))

            bridges = self._discover_bridges(cur, tableset)
            order = self._delete_order(cur, tables)

            # ── report ──
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nwipe_catalog — {len(tables)} tables, {total:,} rows total"))
            for t, c in counts:
                self.stdout.write(f"  {t:38s} {c:>10,}")

            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nBridge FKs to NULL (kept tables → wiped set): {len(bridges)}"))
            for ref_t, col, target_t, notnull in bridges:
                flag = "  ⚠ NOT NULL!" if notnull else ""
                self.stdout.write(f"  {ref_t}.{col} → {target_t}{flag}")
                if notnull:
                    raise CommandError(
                        f"{ref_t}.{col} is NOT NULL but points into the wiped set — "
                        f"cannot null it. Resolve before wiping.")

            if not execute:
                # rollback probe: prove the real plan touches ONLY the 26 tables.
                self._probe(cur, bridges, order, tableset)
                self.stdout.write(self.style.WARNING(
                    "\nDRY RUN — nothing deleted. Re-run with --execute to wipe.\n"))
                return

            # ── execute ──
            with transaction.atomic():
                for ref_t, col, _target, _nn in bridges:
                    cur.execute(f'UPDATE "{ref_t}" SET "{col}" = NULL WHERE "{col}" IS NOT NULL')
                for t in order:
                    cur.execute(f'DELETE FROM "{t}"')

            self.stdout.write(self.style.SUCCESS(
                f"\n✓ Wiped {total:,} rows across {len(tables)} tables "
                f"(ordered DELETE, {len(bridges)} bridges nulled, no CASCADE).\n"))

    # Critical KEPT tables whose row counts MUST be unchanged by the wipe.
    _GUARD_TABLES = [
        "users_user", "core_store", "core_storesettings", "core_branch",
        "billing_subscription", "services_service", "inventory_tax",
        "inventory_attributedefinition", "finance_paymentmethod",
    ]

    def _probe(self, cur, bridges, order, tableset):
        """Run the EXACT null+delete plan inside a real transaction, assert the
        guard (kept) tables keep their row counts, then roll back. Proves the plan
        touches only the 26 wiped tables — no CASCADE collateral."""
        class _Rollback(Exception):
            pass

        before = {}
        for t in self._GUARD_TABLES:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            before[t] = cur.fetchone()[0]

        ok = True
        try:
            with transaction.atomic():
                for ref_t, col, _t, _nn in bridges:
                    cur.execute(f'UPDATE "{ref_t}" SET "{col}" = NULL WHERE "{col}" IS NOT NULL')
                for t in order:
                    cur.execute(f'DELETE FROM "{t}"')
                for t in self._GUARD_TABLES:
                    cur.execute(f'SELECT count(*) FROM "{t}"')
                    after = cur.fetchone()[0]
                    if after != before[t]:
                        ok = False
                        self.stdout.write(self.style.ERROR(
                            f"  PROBE FAIL: {t} {before[t]} → {after} (collateral!)"))
                raise _Rollback   # never persist the probe
        except _Rollback:
            pass

        self.stdout.write(
            self.style.SUCCESS(
                f"\nPROBE: ordered DELETE runs clean; all {len(self._GUARD_TABLES)} "
                "guard tables (users/stores/settings/billing/…) unchanged.")
            if ok else
            self.style.ERROR("\nPROBE: collateral detected — DO NOT --execute."))
