"""Pre-ship tenant-isolation check.

    python manage.py check_isolation

Runs the same self-contained audit that backs the admin "Isolation Check"
button: builds a throwaway two-store world, verifies no endpoint leaks across
stores, then rolls it all back. Exits non-zero if a leak is found, so it can
gate a deploy / run in CI.
"""
from django.core.management.base import BaseCommand

from core.isolation_audit import run_self_contained_audit


class Command(BaseCommand):
    help = "Verify every tenant endpoint isolates store data (exits 1 on a leak)."

    def handle(self, *args, **options):
        r = run_self_contained_audit()
        self.stdout.write(
            f"\nTenant Isolation: {r['status']}  "
            f"(checked {r.get('endpoints_checked')}, isolated {r.get('isolated')}, "
            f"leaks {r.get('leaks')}, errors {r.get('errors')})\n"
        )
        for e in r.get('endpoints', []):
            if e['status'] == 'leak':
                self.stdout.write(self.style.ERROR(
                    f"  LEAK  {e['endpoint']} — returned {e['leaked']} foreign row(s)"))
            elif e['status'] == 'error':
                self.stdout.write(self.style.WARNING(
                    f"  ERR   {e['endpoint']} — {e.get('error')}"))

        if r['status'] == 'FAIL':
            self.stderr.write(self.style.ERROR("\nFAILED — cross-store leak detected.\n"))
            raise SystemExit(1)
        if r['status'] == 'INCONCLUSIVE':
            self.stdout.write(self.style.WARNING(
                "\nINCONCLUSIVE — could not build foreign data to test against.\n"))
            return
        self.stdout.write(self.style.SUCCESS("\nPASS — all stores isolated.\n"))
