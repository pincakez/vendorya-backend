"""Treasury API shapes (TREASURY.md § 11 part A — a CONTRACT the screens are built against: never rename a field).

Money goes out as strings with 2 decimals (like every other Vendorya money field). A box's `balance` is the sum of its
lines — annotated by the viewset (`balance_sum`, one query for the whole list), else read from the ledger.
"""
from decimal import Decimal

from rest_framework import serializers

from . import ledger
from .models import MoneyBox, MoneyMove, IncomeCategory, OtherIncome

CENT = Decimal('0.01')


def money(v):
    return str(Decimal(v or 0).quantize(CENT))


def user_name(user):
    if user is None:
        return None
    return user.get_full_name() or user.username


class MoneyBoxSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    balance = serializers.SerializerMethodField()

    class Meta:
        model = MoneyBox
        fields = ['id', 'name', 'kind', 'system_role', 'branch', 'branch_name', 'is_default_drawer', 'is_active',
                  'allow_negative', 'note', 'balance']
        # PATCH may change only name / note / is_active (closing is checked in the view).
        read_only_fields = ['id', 'kind', 'system_role', 'branch', 'branch_name', 'is_default_drawer',
                            'allow_negative', 'balance']

    def get_balance(self, obj):
        bal = getattr(obj, 'balance_sum', None)
        return money(bal if bal is not None else ledger.balance(obj))


class MoneyBoxCreateSerializer(serializers.Serializer):
    """POST boxes/ — `branch` is checked against the caller's shop in the view (another shop's branch = 404)."""
    name = serializers.CharField(max_length=150)
    kind = serializers.ChoiceField(choices=MoneyBox.Kind.choices)
    branch = serializers.UUIDField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, default='')
    opening = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, default=Decimal('0'))
    allow_negative = serializers.BooleanField(required=False, allow_null=True, default=None)

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError('Give the box a name.')
        return v


class CountSerializer(serializers.Serializer):
    counted = serializers.DecimalField(max_digits=14, decimal_places=2)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class TransferSerializer(serializers.Serializer):
    from_box = serializers.UUIDField()
    to_box = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    note = serializers.CharField(required=False, allow_blank=True, default='')


class ReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class IncomeCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = IncomeCategory
        fields = ['id', 'name']
        read_only_fields = ['id']

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError('Give the category a name.')
        return v


class OtherIncomeSerializer(serializers.ModelSerializer):
    """Read shape + create input. `box` / `category` / `branch` are resolved against the caller's shop in the view."""
    box = serializers.UUIDField(source='box_id')
    category = serializers.UUIDField(source='category_id', required=False, allow_null=True)
    branch = serializers.UUIDField(source='branch_id', required=False, allow_null=True)
    box_name = serializers.CharField(source='box.name', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = OtherIncome
        fields = ['id', 'box', 'amount', 'date', 'category', 'description', 'branch',
                  'box_name', 'category_name', 'branch_name', 'created_by_name', 'created_at']
        read_only_fields = ['id', 'created_at']

    def get_created_by_name(self, obj):
        return user_name(obj.created_by)

    def validate_amount(self, v):
        if v <= 0:
            raise serializers.ValidationError('The amount must be above zero.')
        return v


# ── statement lines ─────────────────────────────────────────────────────────────────────────────────────────────
# Owner-level lines (TREASURY.md § 12.4) as shop staff see them: a generic type only — never the personal box, the
# counterparty, the person money is held for or the partner's name. The kind tells draw vs put-in on screen.
_K = MoneyMove.Kind
OWNER_LEVEL_REF = {
    _K.DRAW_OUT: 'owner', _K.DRAW_IN: 'owner', _K.PUT_OUT: 'owner', _K.PUT_IN: 'owner',
    _K.LOAN_IN: 'loan', _K.LOAN_REPAY: 'loan', _K.LOAN_OUT: 'loan', _K.LOAN_BACK: 'loan',
    _K.AMANA_IN: 'amana', _K.AMANA_OUT: 'amana',
    _K.PARTNER_IN: 'partner', _K.PARTNER_OUT: 'partner',
}


def owner_level_type(m):
    """'owner' / 'loan' / 'amana' / 'partner' for an owner-level line (or the reversal of one), else None."""
    if m.kind in OWNER_LEVEL_REF:
        return OWNER_LEVEL_REF[m.kind]
    if m.kind == _K.REVERSAL:
        if m.loan_id:
            return 'loan'
        if m.amana_id:
            return 'amana'
        if m.partner_id:
            return 'partner'
        if m.transfer_id and m.reverses_id:
            return OWNER_LEVEL_REF.get(m.reverses.kind)
    return None


def move_ref(m, counterpart_names=None):
    """What caused a line: {type, id, label}. type = sale · receipt · refund · supplier_payment · expense · other_income
    · transfer · shift · opening · count (or None for a line with nothing to point at). A reversal carries its
    original's links, so it points at the same source. For a sale payment `id` is the SALE (what the screen opens),
    the label its invoice number. A count at a shift's open/close points at the shift. Owner-level lines: generic
    (`owner_level_type`, no id, no label)."""
    olt = owner_level_type(m)
    if olt:
        return {'type': olt, 'id': None, 'label': ''}
    if m.payment_id:
        inv = m.payment.invoice
        return {'type': 'sale', 'id': str(inv.id),
                'label': str(inv.invoice_number) if inv.invoice_number is not None else ''}
    if m.customer_receipt_id:
        r = m.customer_receipt
        return {'type': 'receipt', 'id': str(r.id), 'label': r.customer.name if r.customer_id else ''}
    if m.refund_id:
        r = m.refund
        return {'type': 'refund', 'id': str(r.id),
                'label': str(r.refund_number) if r.refund_number is not None else ''}
    if m.supplier_payment_id:
        sp = m.supplier_payment
        return {'type': 'supplier_payment', 'id': str(sp.id), 'label': sp.supplier.name}
    if m.purchase_return_id:
        pr = m.purchase_return
        return {'type': 'purchase_return', 'id': str(pr.id),
                'label': str(pr.return_number) if pr.return_number is not None else ''}
    if m.expense_id:
        e = m.expense
        return {'type': 'expense', 'id': str(e.id), 'label': e.category.name if e.category_id else ''}
    if m.other_income_id:
        oi = m.other_income
        return {'type': 'other_income', 'id': str(oi.id),
                'label': oi.category.name if oi.category_id else (oi.description or '')[:80]}
    if m.reservation_deposit_id:
        r = m.reservation_deposit.reservation
        return {'type': 'reservation', 'id': str(r.id),
                'label': f'{r.number} · {r.customer.name}' if r.customer_id else str(r.number)}
    if m.transfer_id:
        # The label is the OTHER box of the transfer ("→ Safe" / "← Drawer 1" — the screen adds the arrow by kind).
        return {'type': 'transfer', 'id': str(m.transfer_id),
                'label': (counterpart_names or {}).get(m.transfer_id, '')}
    if m.shift_id:
        s = m.shift
        return {'type': 'shift', 'id': str(s.id), 'label': user_name(s.user) or ''}
    # No business source: an opening or a count (a reversal of one is named after the line it cancels).
    src = m.reverses if (m.kind == MoneyMove.Kind.REVERSAL and m.reverses_id) else m
    if src.kind == MoneyMove.Kind.OPENING:
        return {'type': 'opening', 'id': str(src.id), 'label': ''}
    if src.kind == MoneyMove.Kind.COUNT_ADJUST:
        return {'type': 'count', 'id': str(src.id), 'label': ''}
    return None


def move_line(m, running_balance=None, counterpart_names=None):
    """One statement line (also the `move` / `out` / `in` answers of count, transfer and reverse)."""
    line = {
        'id': str(m.id),
        'box': str(m.box_id),
        'at': m.at.isoformat(),
        'kind': m.kind,
        'amount': money(m.amount),
        'running_balance': money(running_balance) if running_balance is not None else None,
        'note': m.note,
        'reason': m.reason,
        'created_by_name': user_name(m.created_by),
        'ref': move_ref(m, counterpart_names),
        'reversed': bool(getattr(m, 'is_reversed', False)),
        'reverses': str(m.reverses_id) if m.reverses_id else None,
    }
    return line


def transfer_counterparts(moves, own_box_id):
    """{transfer_id: the other box's name} for every transfer line in `moves` — ONE query."""
    tids = {m.transfer_id for m in moves if m.transfer_id}
    if not tids:
        return {}
    rows = (MoneyMove.all_objects
            .filter(transfer_id__in=tids, kind__in=[MoneyMove.Kind.TRANSFER_IN, MoneyMove.Kind.TRANSFER_OUT])
            .exclude(box_id=own_box_id)
            .values_list('transfer_id', 'box__name'))
    return dict(rows)
