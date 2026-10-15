"""TREASURY.md § 3.4 (Yakot's pick Q6): the DATABASE itself refuses to edit or delete a money line — even a bug or a script
can't rewrite history. A mistake = a REVERSAL line (treasury.ledger.reverse). A future data fix must DROP + re-create this
trigger on purpose, in its own migration."""
from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION treasury_moneymove_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'treasury_moneymove is append-only: write a reversal, never edit or delete a money line';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER treasury_moneymove_append_only
    BEFORE UPDATE OR DELETE ON treasury_moneymove
    FOR EACH ROW EXECUTE FUNCTION treasury_moneymove_append_only();
"""
REVERSE_SQL = """
DROP TRIGGER IF EXISTS treasury_moneymove_append_only ON treasury_moneymove;
DROP FUNCTION IF EXISTS treasury_moneymove_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [('treasury', '0001_initial')]
    operations = [migrations.RunSQL(SQL, REVERSE_SQL)]
