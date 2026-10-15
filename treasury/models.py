"""Treasury Ledger (TREASURY.md, Wave 1) — where the money physically is.

Owner (the person) → OwnerPart (a shop / a light business / personal money) → MoneyBox (a drawer, a safe, a bank account,
an e-wallet…) → MoneyMove (one immutable line: + in / − out). A box's balance is ALWAYS the sum of its lines — there is
no balance column to type into. A line is never edited or deleted (the model refuses, and so does the database — see the
trigger in migration 0002); a mistake gets a REVERSAL line. All writes go through `treasury.ledger`.

Iron rules: nothing named after a shop, person, bank or wallet — every box / part / income category is named by the owner on
screen; the whole thing is OFF per shop until `StoreSettings.treasury_enabled` is turned on.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import TimestampedModel, SoftDeleteModel, Store, Branch
from core.tenancy import TenantScopedManager, TenantScopedQuerySet, TenantSoftDeleteManager


class Owner(TimestampedModel):
    """The person who owns money. Today: found through a shop's OWNER login (`Store.owner`); one person with two shops =
    ONE Owner. `gid_subject` is the GATES ID link (GATES-ID.md "subject") — empty until GATES ID sign-in exists."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=150, blank=True, default='')
    gid_subject = models.UUIDField(null=True, blank=True, unique=True)
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='treasury_owners', blank=True)

    def __str__(self):
        return self.display_name or str(self.id)


class OwnerPart(TimestampedModel, SoftDeleteModel):
    """One pocket of an owner's money. SHOP = a Vendorya shop (Wave 1). BUSINESS / PERSONAL = Wave 2 (the owner-level switch)."""
    class Kind(models.TextChoices):
        SHOP     = 'SHOP',     _('Shop')
        BUSINESS = 'BUSINESS', _('Business')
        PERSONAL = 'PERSONAL', _('Personal')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='parts')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SHOP)
    name = models.CharField(max_length=150, blank=True, default='')
    # Only for kind=SHOP. The tenant key for scoping (`objects` = the current shop's parts).
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='treasury_parts')

    objects = TenantSoftDeleteManager()

    class Meta(TimestampedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=['store'], condition=models.Q(store__isnull=False, is_deleted=False),
                                    name='one_live_part_per_shop'),
            # Wave 2: an owner has ONE personal pocket (made by the owner-level switch).
            models.UniqueConstraint(fields=['owner'], condition=models.Q(kind='PERSONAL', is_deleted=False),
                                    name='one_personal_part_per_owner'),
        ]

    def __str__(self):
        return self.name or self.kind


class MoneyBox(TimestampedModel):
    """A place money sits, inside ONE part. Never deleted: closing = `is_active=False` (only at balance 0)."""
    class Kind(models.TextChoices):
        DRAWER   = 'DRAWER',   _('Till drawer')
        CASH     = 'CASH',     _('Cash (safe / petty cash)')
        BANK     = 'BANK',     _('Bank account')
        WALLET   = 'WALLET',   _('E-wallet')
        RESERVED = 'RESERVED', _('Money set aside')
        SAVINGS  = 'SAVINGS',  _('Savings')

    class Role(models.TextChoices):
        NONE     = '',         _('Normal box')
        # The ONE per-shop box non-cash money lands in while its payment method has no "lands in" box yet (honest, never
        # silent: the Treasury page asks the owner to choose a box for that method).
        UNSORTED = 'UNSORTED', _('Not sorted yet')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='boxes')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='boxes')
    # Copied from the part (SHOP parts only) — the tenant key, so `objects` is the current shop's boxes.
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='money_boxes')
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    system_role = models.CharField(max_length=10, choices=Role.choices, default=Role.NONE, blank=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True, related_name='money_boxes')
    # The branch's default drawer (made when the shop turns the Treasury on). Cash with no shift lands here.
    is_default_drawer = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # Cash can't be below zero (a missing line); a bank account may run an overdraft. Set from the kind on create.
    allow_negative = models.BooleanField(default=False)
    note = models.TextField(blank=True, default='')   # bank name / account no. — NEVER a password

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta(TimestampedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=['branch'], condition=models.Q(is_default_drawer=True),
                                    name='one_default_drawer_per_branch'),
            models.UniqueConstraint(fields=['store'], condition=models.Q(system_role='UNSORTED'),
                                    name='one_unsorted_box_per_shop'),
        ]

    def __str__(self):
        return self.name


class IncomeCategory(TimestampedModel, SoftDeleteModel):
    """Categories for money that isn't a sale (named by the shop, like expense categories)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='income_categories')
    name = models.CharField(max_length=100)

    objects = TenantSoftDeleteManager()

    def __str__(self):
        return self.name


class OtherIncome(TimestampedModel, SoftDeleteModel):
    """Money that came in and is NOT a sale (PROLINE-PARITY New 3). Counts as "Other income" in the P&L. Its box line is
    written by `treasury.ledger`; deleting it (soft) writes the reversal."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='other_incomes')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, null=True, blank=True)
    category = models.ForeignKey(IncomeCategory, on_delete=models.PROTECT, null=True, blank=True)
    box = models.ForeignKey(MoneyBox, on_delete=models.PROTECT, related_name='other_incomes')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField(default=timezone.localdate)
    description = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='+')

    objects = TenantSoftDeleteManager()

    def __str__(self):
        return f'{self.amount} ({self.date})'


# ── Wave 2: the owner level (TREASURY.md § 12) ─────────────────────────────────────────────────────────────────────
# Everything below belongs to an OWNER (the person), not to a shop: no `store` column, never in any shop query. The money
# lines live in MoneyMove (written only by `treasury.ledger`); these rows only name what the lines are about.

class PartEntry(TimestampedModel, SoftDeleteModel):
    """Income or spending of a light BUSINESS part or of PERSONAL money (a shop uses its own expenses / other income).
    Its box line is written by `treasury.ledger`; deleting it (soft) writes the reversal."""
    class Direction(models.TextChoices):
        IN  = 'IN',  _('Income')
        OUT = 'OUT', _('Spending')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='part_entries')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='entries')
    direction = models.CharField(max_length=3, choices=Direction.choices)
    category = models.CharField(max_length=100, blank=True, default='')   # typed by the owner
    amount = models.DecimalField(max_digits=14, decimal_places=2)          # always > 0; direction gives the sign
    date = models.DateField(default=timezone.localdate)
    box = models.ForeignKey(MoneyBox, on_delete=models.PROTECT, related_name='part_entries')
    description = models.TextField(blank=True, default='')
    # A business with partners: spending only the owner carries (partners' profit share ignores it).
    owner_only = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='+')

    class Meta(TimestampedModel.Meta):
        ordering = ['-date', '-created_at']


class Loan(TimestampedModel, SoftDeleteModel):
    """Money borrowed from someone (BORROWED) or lent to someone (LENT). No principal column — the lines ARE the history:
    owed = the signed sum of the loan's lines (see `ledger.loan_owed`). Never income, never expense."""
    class Direction(models.TextChoices):
        BORROWED = 'BORROWED', _('Borrowed')
        LENT     = 'LENT',     _('Lent')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='loans')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='loans')
    direction = models.CharField(max_length=8, choices=Direction.choices)
    counterparty = models.CharField(max_length=150)   # typed by the owner
    note = models.TextField(blank=True, default='')
    opened_on = models.DateField(default=timezone.localdate)
    is_closed = models.BooleanField(default=False)

    class Meta(TimestampedModel.Meta):
        ordering = ['is_closed', '-opened_on', '-created_at']


class Amana(TimestampedModel, SoftDeleteModel):
    """Money held for someone else (أمانة): it sits in our box but is not ours. Held = the signed sum of its lines."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='amanat')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='amanat')
    held_for = models.CharField(max_length=150)   # typed by the owner
    note = models.TextField(blank=True, default='')
    opened_on = models.DateField(default=timezone.localdate)
    is_closed = models.BooleanField(default=False)

    class Meta(TimestampedModel.Meta):
        ordering = ['is_closed', '-opened_on', '-created_at']


class Partner(TimestampedModel):
    """A partner in a SHOP or BUSINESS part, with a profit share. The owner keeps 100 − Σ active shares. A partner is a
    typed name today; `gid_subject` is the GATES ID slot (empty until GATES ID sign-in exists)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='partners')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='partners')
    name = models.CharField(max_length=150)
    share_pct = models.DecimalField(max_digits=5, decimal_places=2)
    gid_subject = models.UUIDField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    joined_on = models.DateField(default=timezone.localdate)

    class Meta(TimestampedModel.Meta):
        ordering = ['-is_active', 'name']

    def __str__(self):
        return self.name


class PartnerSettlement(TimestampedModel):
    """One period's profit split between the partners of a part. Figures are frozen when saved; a mistake = cancel
    (never delete) and settle again."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(Owner, on_delete=models.PROTECT, related_name='settlements')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='settlements')
    date_from = models.DateField()
    date_to = models.DateField()
    profit = models.DecimalField(max_digits=14, decimal_places=2)
    owner_only_expenses = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='+')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default='')

    class Meta(TimestampedModel.Meta):
        ordering = ['-date_to', '-created_at']


class PartnerSettlementLine(models.Model):
    """One share of a settlement. `partner` empty = the owner's own share (the rest after the partners)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    settlement = models.ForeignKey(PartnerSettlement, on_delete=models.CASCADE, related_name='lines')
    partner = models.ForeignKey(Partner, on_delete=models.PROTECT, null=True, blank=True, related_name='settlement_lines')
    share_pct = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)   # signed: a loss is negative


class MoneyMoveQuerySet(TenantScopedQuerySet):
    def update(self, **kwargs):
        raise TypeError('Money movements are immutable — write a reversal (treasury.ledger.reverse).')

    def delete(self):
        raise TypeError('Money movements are immutable — write a reversal (treasury.ledger.reverse).')


class MoneyMoveManager(TenantScopedManager):
    """The current shop's lines, through the immutable queryset (no .update() / .delete())."""
    def get_queryset(self):
        qs = MoneyMoveQuerySet(self.model, using=self._db)
        qs.tenant_lookup = self.tenant_lookup
        return qs.current_tenant()


class MoneyMove(models.Model):
    """ONE line in ONE box. Signed amount: + money in, − money out. Immutable (model + DB trigger)."""
    class Kind(models.TextChoices):
        SALE_IN         = 'SALE_IN',         _('Sale payment')
        RECEIPT_IN      = 'RECEIPT_IN',      _('Customer paid a debt')
        REFUND_OUT      = 'REFUND_OUT',      _('Refund paid back')
        SUPPLIER_OUT    = 'SUPPLIER_OUT',    _('Supplier payment')
        EXPENSE_OUT     = 'EXPENSE_OUT',     _('Expense')
        OTHER_INCOME_IN = 'OTHER_INCOME_IN', _('Other income')
        TRANSFER_IN     = 'TRANSFER_IN',     _('Transfer in')
        TRANSFER_OUT    = 'TRANSFER_OUT',    _('Transfer out')
        OPENING         = 'OPENING',         _('Opening balance')
        COUNT_ADJUST    = 'COUNT_ADJUST',    _('Count difference')
        REVERSAL        = 'REVERSAL',        _('Reversal')
        # Wave 2 — the owner level (TREASURY.md § 12). None of these is income or expense.
        DRAW_OUT        = 'DRAW_OUT',        _('Owner draw')           # left a business part …
        DRAW_IN         = 'DRAW_IN',         _('Owner draw received')  # … arrived in personal money
        PUT_OUT         = 'PUT_OUT',         _('Put into a business')  # left personal money (or another business) …
        PUT_IN          = 'PUT_IN',          _('Owner put in')         # … arrived in a business part
        LOAN_IN         = 'LOAN_IN',         _('Loan received')
        LOAN_REPAY      = 'LOAN_REPAY',      _('Loan paid back')
        LOAN_OUT        = 'LOAN_OUT',        _('Loan given')
        LOAN_BACK       = 'LOAN_BACK',       _('Loan repaid to us')
        AMANA_IN        = 'AMANA_IN',        _('Money held for someone')
        AMANA_OUT       = 'AMANA_OUT',       _('Held money returned')
        PARTNER_IN      = 'PARTNER_IN',      _('Partner put in')
        PARTNER_OUT     = 'PARTNER_OUT',     _('Paid to a partner')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    box = models.ForeignKey(MoneyBox, on_delete=models.PROTECT, related_name='moves')
    part = models.ForeignKey(OwnerPart, on_delete=models.PROTECT, related_name='+')
    store = models.ForeignKey(Store, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    at = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name='+')
    note = models.TextField(blank=True, default='')
    reason = models.TextField(blank=True, default='')   # required for COUNT_ADJUST (≠ 0) and REVERSAL

    # What caused it — at most one of these is set (a transfer / opening / count has none, a transfer has transfer_id).
    payment = models.ForeignKey('finance.Payment', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    customer_receipt = models.ForeignKey('finance.CustomerReceipt', on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='+')
    refund = models.ForeignKey('finance.RefundInvoice', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    supplier_payment = models.ForeignKey('finance.SupplierPayment', on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='+')
    expense = models.ForeignKey('finance.Expense', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    other_income = models.ForeignKey(OtherIncome, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # Wave 2 sources (owner level)
    part_entry = models.ForeignKey(PartEntry, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, null=True, blank=True, related_name='moves')
    amana = models.ForeignKey(Amana, on_delete=models.PROTECT, null=True, blank=True, related_name='moves')
    partner = models.ForeignKey(Partner, on_delete=models.PROTECT, null=True, blank=True, related_name='moves')
    transfer_id = models.UUIDField(null=True, blank=True, db_index=True)
    shift = models.ForeignKey('finance.WorkShift', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    reverses = models.OneToOneField('self', on_delete=models.PROTECT, null=True, blank=True, related_name='reversed_by')

    # Hidden "imported from <system>" + its key — makes a money-book import re-runnable (never doubles).
    source_tag = models.CharField(max_length=120, blank=True, default='', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = MoneyMoveManager()
    all_objects = models.Manager.from_queryset(MoneyMoveQuerySet)()

    class Meta:
        ordering = ['-at', '-created_at']
        indexes = [models.Index(fields=['box', 'at'])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError('Money movements are immutable — write a reversal (treasury.ledger.reverse).')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('Money movements are immutable — write a reversal (treasury.ledger.reverse).')

    def __str__(self):
        return f'{self.kind} {self.amount:+} → {self.box_id}'
