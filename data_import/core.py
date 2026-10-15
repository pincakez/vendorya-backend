"""Generic engine for importing another system's CSV export into ONE shop.

Nothing in here knows a particular source system: a source system plugs in through an adapter
(`data_import/adapters/<system>.py`) that maps its columns onto Vendorya rows. This module gives
every adapter the same building blocks:

- `read_table`   — read one exported CSV safely (UTF-8 with or without BOM, any column order,
                   extra/missing columns reported, Arabic text kept exactly);
- `parse_decimal`— "1,234.50", "", "NULL", Arabic-Indic digits → Decimal / None / BadNumber;
- `RefBook`      — the hidden tag (`ExternalRef`): find the object a key made last time, tag new ones;
- `apply_fields` — the update policy (an object the import CREATED follows the source; an object the
                   shop made by hand and the import only LINKED keeps the shop's values except the
                   fields the adapter says always follow the source);
- `Report`       — per section: added / updated / linked / unchanged / skipped + reasons, samples, notes;
- `run_import`   — dry run (default) = the real code path inside a transaction that is rolled back,
                   so the report is exactly what a commit would do and NOTHING is saved; commit =
                   the same, all or nothing, plus ONE activity-log entry for the run.

Iron rules every adapter keeps: every query is filtered by the one shop; soft deletes only (an
importer never deletes); stock quantities are never touched.
"""
import csv
import os
import re
from collections import Counter, OrderedDict
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from .models import ExternalRef

# Some exports carry pictures or long memos inside a cell — far past csv's 128 KB default.
csv.field_size_limit(2 ** 31 - 1)

_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789')
_ZEROS = {'0', '0.0', '0.00', '0.000', '0.0000', '-0', '-0.00', '-0.0000'}
_FALSE = {'false', 'no', 'n', 'f'}
_TRUE = {'1', '-1', 'true', 'yes', 'y', 't'}
SAMPLES = 5


class BadNumber(ValueError):
    def __init__(self, column, raw):
        super().__init__(f'bad number in {column}')
        self.column, self.raw = column, raw


def is_blank(raw):
    s = (raw or '').strip()
    return s == '' or s.upper() == 'NULL'


def has_value(raw):
    """A cell that really says something: not empty, not NULL, not 0 / False."""
    s = (raw or '').strip()
    return not (is_blank(s) or s in _ZEROS or s.lower() in _FALSE)


def truthy(raw):
    return (raw or '').strip().lower() in _TRUE


def text(raw):
    """A cell as clean text ('' for empty / NULL). The text itself (Arabic included) is kept exactly."""
    return '' if is_blank(raw) else raw.strip()


def parse_decimal(raw, column=''):
    """'1,234.50' → Decimal('1234.50'); '' / 'NULL' → None; junk → BadNumber."""
    s = (raw or '').strip().translate(_DIGITS)
    s = s.replace('٬', '').replace('٫', '.').replace(',', '').replace(' ', '')
    if s == '' or s.upper() == 'NULL':
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise BadNumber(column, raw)
    if not value.is_finite():
        raise BadNumber(column, raw)
    return value


def norm_key(raw):
    """A source key as one canonical string: '12', ' 12 ', '12.0' all → '12'."""
    s = (raw or '').strip().translate(_DIGITS)
    m = re.fullmatch(r'(-?\d+)\.0+', s)
    return m.group(1) if m else s


def norm_name(raw):
    """For matching by name only (never stored): spaces collapsed, case folded."""
    return ' '.join((raw or '').split()).casefold()


def norm_phone(raw):
    s = text(raw).translate(_DIGITS)
    plus = s.startswith('+')
    digits = re.sub(r'[\s\-\(\)\./]', '', s.lstrip('+'))
    return ('+' if plus else '') + digits if digits else ''


# ── reading a table ─────────────────────────────────────────────────────────────────────────

class Table:
    def __init__(self, name, path, headers, rows, expected, ragged):
        self.name, self.path, self.headers, self.rows = name, path, headers, rows
        self.missing = [c for c in expected if c not in headers]
        self.extra = [h for h in headers if expected and h not in expected]
        self.ragged = ragged      # rows whose cell count differs from the header

    def has(self, column):
        return column in self.headers


def find_file(folder, name):
    """`<name>.csv` in `folder`, any letter case."""
    want = f'{name}.csv'.lower()
    try:
        for entry in os.listdir(folder):
            if entry.lower() == want:
                return os.path.join(folder, entry)
    except FileNotFoundError:
        return None
    return None


def read_table(folder, name, expected=()):
    """→ Table, or None when the file isn't there. Blank lines are skipped; a short row is padded
    with '' and a long row's extra cells are dropped (both counted in `ragged`)."""
    path = find_file(folder, name)
    if path is None:
        return None
    with open(path, encoding='utf-8-sig', newline='') as fh:
        reader = csv.reader(fh)
        try:
            headers = [h.strip() for h in next(reader)]
        except StopIteration:
            return Table(name, path, [], [], expected, 0)
        rows, ragged = [], 0
        for raw in reader:
            if not any((c or '').strip() for c in raw):
                continue
            if len(raw) != len(headers):
                ragged += 1
            rows.append({h: (raw[i] if i < len(raw) else '') for i, h in enumerate(headers)})
    return Table(name, path, headers, rows, list(expected), ragged)


# ── the report ──────────────────────────────────────────────────────────────────────────────

class Section:
    ACTIONS = ('add', 'update', 'link', 'unchanged', 'skip')

    def __init__(self, key, title, info=False):
        self.key, self.title, self.info = key, title, info    # info = no rows of its own, only facts
        self.counts = Counter({a: 0 for a in self.ACTIONS})
        self.reasons = Counter()
        self.notes = OrderedDict()          # label → count (information, decides later work)
        self.lines = []                     # free text lines
        self.samples = {a: [] for a in self.ACTIONS}

    def _count(self, action, sample):
        self.counts[action] += 1
        if sample is not None and len(self.samples[action]) < SAMPLES:
            self.samples[action].append(sample)

    def add(self, sample=None):
        self._count('add', sample)

    def update(self, sample=None):
        self._count('update', sample)

    def link(self, sample=None):
        self._count('link', sample)

    def unchanged(self):
        self._count('unchanged', None)

    def skip(self, reason, sample=None):
        self.reasons[reason] += 1
        self._count('skip', f'{sample} — {reason}' if sample else reason)

    def note(self, label, n=1):
        self.notes[label] = self.notes.get(label, 0) + n

    def line(self, msg):
        self.lines.append(msg)


class Report:
    def __init__(self, system, store, folder):
        self.system, self.store, self.folder = system, store, folder
        self.sections = OrderedDict()
        self.files = []          # (table, rows, missing, extra, ragged) or (table, None, …) when absent
        self.blockers = []       # anything here refuses --commit
        self.warnings = []
        self.committed = False

    def section(self, key, title=None, info=False):
        if key not in self.sections:
            self.sections[key] = Section(key, title or key, info)
        return self.sections[key]

    def file(self, name, table):
        if table is None:
            self.files.append((name, None, [], [], 0))
        else:
            self.files.append((name, len(table.rows), table.missing, table.extra, table.ragged))

    def counts(self, key):
        return dict(self.sections[key].counts) if key in self.sections else {}

    def render(self, dry_run):
        out = []
        mode = 'DRY RUN — nothing was saved' if dry_run else (
            'COMMITTED — saved' if self.committed else 'NOT SAVED')
        out.append(f'Import from {self.system} into shop "{self.store.name}" '
                   f'(code {self.store.store_code}) — {mode}')
        out.append(f'Folder: {self.folder}')
        out.append('')
        out.append('Files:')
        for name, n, missing, extra, ragged in self.files:
            if n is None:
                out.append(f'  {name}.csv — NOT FOUND')
                continue
            bits = [f'{n} rows']
            if missing:
                bits.append(f'missing columns: {", ".join(missing)}')
            if extra:
                bits.append(f'extra columns (ignored): {", ".join(extra)}')
            if ragged:
                bits.append(f'{ragged} rows with a wrong number of cells')
            out.append(f'  {name}.csv — ' + ' · '.join(bits))
        if self.blockers:
            out.append('')
            out.append('BLOCKERS — --commit is refused until these are fixed:')
            out.extend(f'  ! {b}' for b in self.blockers)
        if self.warnings:
            out.append('')
            out.append('Warnings:')
            out.extend(f'  - {w}' for w in self.warnings)
        verb = {'add': 'would add' if dry_run else 'added', 'update': 'would update' if dry_run else 'updated',
                'link': 'would link' if dry_run else 'linked', 'unchanged': 'unchanged', 'skip': 'skipped'}
        for s in self.sections.values():
            out.append('')
            out.append(f'== {s.title} ==')
            if not s.info:
                out.append('  ' + ' · '.join(f'{verb[a]} {s.counts[a]}' for a in Section.ACTIONS))
            if s.reasons:
                out.append('  top skip reasons:')
                out.extend(f'    {n:>6} × {r}' for r, n in s.reasons.most_common(10))
            for msg in s.lines:
                out.append(f'  {msg}')
            if s.notes:
                out.append('  counts:')
                out.extend(f'    {n:>6} × {label}' for label, n in s.notes.items())
            for a in ('add', 'update', 'link', 'skip'):
                if s.samples[a]:
                    out.append(f'  sample ({verb[a]}):')
                    out.extend(f'    {x}' for x in s.samples[a])
        return '\n'.join(out)

    def summary(self):
        return {k: dict(s.counts) for k, s in self.sections.items() if not s.info}


# ── the hidden tag ──────────────────────────────────────────────────────────────────────────

class RefBook:
    """The tags one system left in ONE shop. Never reads another shop's tags."""

    def __init__(self, store, system):
        self.store, self.system = store, system
        self._refs = {}
        self._touched = set()
        for ref in ExternalRef.all_objects.filter(store=store, system=system):
            self._refs[(ref.kind, ref.external_key)] = ref

    def get(self, kind, key):
        return self._refs.get((kind, key))

    def target(self, kind, key, model):
        """→ (ref, object) — object None when the tag points at nothing of this shop any more.
        Soft-deleted objects ARE returned (the caller decides; an importer never brings one back)."""
        ref = self.get(kind, key)
        if ref is None:
            return None, None
        obj = model.all_objects.filter(pk=ref.object_id, store=self.store).first()
        return ref, obj

    def tagged_ids(self, kind, origin=None):
        return {r.object_id for (k, _), r in self._refs.items() if k == kind and origin in (None, r.origin)}

    def tag(self, kind, key, obj, origin=ExternalRef.Origin.CREATED):
        ref = ExternalRef.all_objects.create(
            store=self.store, system=self.system, kind=kind, external_key=key,
            content_type=ContentType.objects.get_for_model(type(obj)), object_id=obj.pk,
            origin=origin, last_run_at=timezone.now())
        self._refs[(kind, key)] = ref
        return ref

    def touch(self, ref):
        self._touched.add(ref.pk)

    def flush(self):
        if self._touched:
            ExternalRef.all_objects.filter(pk__in=self._touched, store=self.store).update(last_run_at=timezone.now())
            self._touched.clear()


# ── the update policy ───────────────────────────────────────────────────────────────────────

def _empty(value):
    return value is None or value == ''


def _same(old, new):
    if hasattr(old, 'pk') or hasattr(new, 'pk'):
        return getattr(old, 'pk', None) == getattr(new, 'pk', None)
    if isinstance(old, Decimal) or isinstance(new, Decimal):
        try:
            return Decimal(str(old if old is not None else 'NaN')) == Decimal(str(new if new is not None else 'NaN'))
        except InvalidOperation:
            return False
    if _empty(old) and _empty(new):
        return True
    return old == new


def apply_fields(obj, values, origin, always=()):
    """Set `values` on `obj` (not saved). An object the import CREATED follows the source on every
    field; one the shop made by hand (LINKED) keeps its own values, except the fields in `always`
    and fields it left empty. → [(field, old, new)] that changed."""
    changes = []
    for field, new in values.items():
        old = getattr(obj, field)
        if origin == ExternalRef.Origin.LINKED and field not in always and not _empty(old):
            continue
        if _same(old, new):
            continue
        setattr(obj, field, new)
        changes.append((field, old, new))
    return changes


def describe(changes):
    def show(v):
        return getattr(v, 'name', None) or ('—' if _empty(v) else str(v))
    return ', '.join(f'{f}: {show(o)} → {show(n)}' for f, o, n in changes)


# ── running ─────────────────────────────────────────────────────────────────────────────────

class Adapter:
    """Base for one source system. Subclasses set `system` and implement `load()` (read the files,
    count, fill report.blockers — no writes) and `run()` (the writes)."""
    system = ''

    def __init__(self, store, folder, options, report):
        self.store, self.folder, self.options, self.report = store, folder, options or {}, report
        self.commit = False
        self.refs = None

    @contextmanager
    def row(self, section, sample):
        """One row = one savepoint. In a dry run an unexpected error is reported as a skip so every
        problem shows at once; in a commit it stops the whole run (all or nothing)."""
        try:
            with transaction.atomic():
                yield
        except Exception as exc:   # noqa: BLE001 — reported, or re-raised on commit
            if self.commit:
                raise
            section.skip(f'unexpected error: {type(exc).__name__}: {str(exc)[:120]}', sample)

    def load(self):
        raise NotImplementedError

    def run(self):
        raise NotImplementedError


@contextmanager
def search_indexing_paused():
    """A product save pushes it into the search index AT ONCE (not on commit). Inside an import that
    would leak every row of a dry run (rolled back) into search. So the per-save push is paused for the
    run; the adapter hands the products it wrote to `sync_products_on_commit`, which only fires when
    the run really commits. The import runs in its own process (a management command), so pausing
    this process-wide signal touches nothing else."""
    from django.db.models.signals import post_save
    from inventory.models import Product
    from search import signals as search_signals
    receiver = search_signals._product_saved
    paused = post_save.disconnect(receiver, sender=Product)
    try:
        yield
    finally:
        if paused:
            post_save.connect(receiver, sender=Product)


def run_import(adapter_cls, store, folder, options=None, commit=False, user=None):
    """Dry run unless `commit`. Returns the Report. A commit with blockers saves nothing."""
    report = Report(adapter_cls.system, store, folder)
    adapter = adapter_cls(store, folder, options, report)
    adapter.load()
    if commit and report.blockers:
        return report
    adapter.commit = commit
    with search_indexing_paused(), transaction.atomic():
        adapter.refs = RefBook(store, adapter_cls.system)
        adapter.run()
        adapter.refs.flush()
        if commit:
            from core.models import ActivityLog
            ActivityLog.objects.create(
                store=store, user=user or store.owner,
                operation_type=ActivityLog.OperationType.OTHER,
                action=f'Import from {adapter_cls.system}: ' + ', '.join(
                    f'{k} +{c["add"]} ~{c["update"]} ={c["link"]}' for k, c in report.summary().items())[:255],
                details={'system': adapter_cls.system, 'counts': report.summary(),
                         'options': {k: v for k, v in (options or {}).items() if isinstance(v, (str, int, bool, list))}},
            )
            report.committed = True
        else:
            transaction.set_rollback(True)
    return report
