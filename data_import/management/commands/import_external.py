"""Import another system's CSV export into ONE shop — master data (categories, products, customers, suppliers).

    manage.py import_external proline --store <store code or id> --dir <folder with the CSVs>      (dry run)
    manage.py import_external proline --store <…> --dir <…> --commit                                (save)

DRY RUN IS THE DEFAULT: the whole import runs inside a transaction that is rolled back, so the report
is exactly what --commit would do, and nothing is saved. --commit saves everything or nothing.
RUN DRY RUNS (and commits) OUTSIDE OPENING HOURS: a dry run really writes before rolling back, so it holds
the same row locks as a commit for the whole run (it gives up after 5 s waiting for a lock instead of
making the till wait).

Re-runnable: every imported row carries a hidden tag (system + the source's own key). Running again
with a fresher export UPDATES what the last run made — it never adds a second copy. Products the shop
typed by hand earlier are found by SKU2 (or barcode) = the source's item code and LINKED when the names
match (or with --link-by-code): prices follow the source, the shop's name / category / barcode stay. An
empty price in the source never overwrites a price. Stock is never touched.
"""
from django.core.management.base import BaseCommand, CommandError

from core.models import Store
from data_import.adapters import ADAPTERS, get_adapter
from data_import.core import run_import

BALANCES_HELP = (
    "none (default) = do NOT import tb_Cust.Balance. opening = write it as each customer's OPENING balance. "
    "RISK: in the source Balance is most likely the CURRENT balance (a running Debit − Credit). If the old "
    "sales / payments are imported later as history, the same debt would be counted twice. Use 'opening' only "
    "when that history will not be imported.")


def _codes(raw):
    return [c.strip() for c in (raw or '').split(',') if c.strip()]


class Command(BaseCommand):
    help = 'Import master data from another system\'s CSV export into one shop (dry run unless --commit).'

    def add_arguments(self, parser):
        parser.add_argument('system', choices=sorted(ADAPTERS), help='the source system')
        parser.add_argument('--store', required=True, help='the shop: its store code or its id')
        parser.add_argument('--dir', required=True, help='folder with the exported CSV files')
        parser.add_argument('--commit', action='store_true',
                            help='SAVE (default is a dry run that saves nothing). Run dry runs and commits '
                                 'outside opening hours — the run locks the rows it touches until it ends.')
        parser.add_argument('--link-by-code', action='store_true',
                            help='link a product the shop typed by hand to the source item with the same code '
                                 '(SKU2 or barcode) EVEN WHEN THE NAMES DIFFER. Without it such rows are skipped '
                                 'and listed in the report, so a code typed on the wrong product is caught.')
        parser.add_argument('--balances', choices=['none', 'opening'], default='none', help=BALANCES_HELP)
        parser.add_argument('--balance-sign', choices=['as-is', 'flip'], default='as-is',
                            help='with --balances=opening: "flip" if the source stores what a customer owes '
                                 'as a negative number (Vendorya: positive = the customer owes the shop)')
        parser.add_argument('--sell-price', choices=['auto', 'PriceOut1', 'PriceOut2'], default='auto',
                            help='the item column with the real sell price ("auto" = the one recent sales match)')
        parser.add_argument('--type-column', default='auto',
                            help='customer/supplier split: the source column that says which is which '
                                 '("auto" = detect, "none" = ignore types and use invoices only)')
        parser.add_argument('--party-key', choices=['ID', 'AccountCode'], default=None,
                            help='the customer/supplier column invoices point at (default: detected)')
        parser.add_argument('--supplier-codes', default='', help='comma list of party codes to force as suppliers')
        parser.add_argument('--customer-codes', default='', help='comma list of party codes to force as customers')
        parser.add_argument('--default-supplier', default='',
                            help='supplier code (or name) for items whose source supplier is unknown '
                                 '(default: the shop\'s default supplier)')

    def handle(self, *args, **o):
        key = o['store'].strip()
        stores = Store.objects.filter(is_deleted=False)
        store = stores.filter(store_code=key).first()
        if store is None:
            try:
                store = stores.filter(pk=key).first()
            except Exception:   # noqa: BLE001 — not a UUID
                store = None
        if store is None:
            raise CommandError(f'No shop with store code or id {key!r}.')
        options = {
            'balances': o['balances'], 'balance_sign': o['balance_sign'], 'type_column': o['type_column'],
            'party_key': o['party_key'], 'supplier_codes': _codes(o['supplier_codes']),
            'customer_codes': _codes(o['customer_codes']), 'default_supplier': o['default_supplier'],
            'link_by_code': o['link_by_code'],
            'sell_price': o['sell_price'],
        }
        report = run_import(get_adapter(o['system']), store, o['dir'], options, commit=o['commit'])
        self.stdout.write(report.render(dry_run=not o['commit']))
        if o['commit'] and not report.committed:
            raise CommandError('Nothing was saved — fix the blockers above first.')
