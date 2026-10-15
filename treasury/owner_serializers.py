"""Owner level API shapes — part A (TREASURY.md § 12.3, a CONTRACT the screens are built against: never rename a field).

Money goes out as 2-decimal strings, like the Wave 1 shapes (`serializers.money`). Box / part / loan / amana ids in a
request body are plain UUIDs here; the views resolve them through `OwnerMixin` (another owner's row = 404).
Statement lines reuse the Wave 1 `move_line`; `owner_move_line` only swaps in the owner's own labels (the person may see
the other box, the counterparty, who the money is held for) — the shop's staff never get these.
"""
from rest_framework import serializers

from . import ledger
from .models import OwnerPart, MoneyMove, PartEntry, Loan, Amana
from .serializers import MoneyBoxSerializer, MoneyBoxCreateSerializer, money, move_line, move_ref

K = MoneyMove.Kind
# Two-line moves between boxes (Wave 1 transfers + the owner level's draws / put-ins): they share a transfer_id.
PAIR_KINDS = (K.TRANSFER_OUT, K.TRANSFER_IN, K.DRAW_OUT, K.DRAW_IN, K.PUT_OUT, K.PUT_IN)


# ── parts ─────────────────────────────────────────────────────────────────────────────────────────────────────
class OwnerPartSerializer(serializers.ModelSerializer):
    store_name = serializers.CharField(source='store.name', read_only=True, default=None)
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = OwnerPart
        fields = ['id', 'kind', 'name', 'store', 'store_name', 'is_active']
        read_only_fields = fields

    def get_is_active(self, obj):
        return not obj.is_deleted


class OwnerPartCreateSerializer(serializers.Serializer):
    """POST parts/ — a BUSINESS part only (PERSONAL is made by the switch, SHOP by the shop)."""
    name = serializers.CharField(max_length=150)
    kind = serializers.CharField(required=False, default=OwnerPart.Kind.BUSINESS)

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError('Give the business a name.')
        return v


class OwnerPartPatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)


# ── boxes ─────────────────────────────────────────────────────────────────────────────────────────────────────
class OwnerBoxSerializer(MoneyBoxSerializer):
    """The Wave 1 box shape + which part it sits in (and its shop, for a SHOP part)."""
    part_kind = serializers.CharField(source='part.kind', read_only=True)
    part_name = serializers.CharField(source='part.name', read_only=True)

    class Meta(MoneyBoxSerializer.Meta):
        fields = MoneyBoxSerializer.Meta.fields + ['part', 'part_kind', 'part_name', 'store']
        read_only_fields = MoneyBoxSerializer.Meta.read_only_fields + ['part', 'part_kind', 'part_name', 'store']


class OwnerBoxCreateSerializer(MoneyBoxCreateSerializer):
    part = serializers.UUIDField()


# ── money in / out bodies ─────────────────────────────────────────────────────────────────────────────────────
class _AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    box = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_amount(self, v):
        if v <= 0:
            raise serializers.ValidationError('The amount must be above zero.')
        return v


class MoneyLineInputSerializer(_AmountSerializer):
    """pay / more / return bodies: {amount, box, note?}."""


class LoanCreateSerializer(_AmountSerializer):
    part = serializers.UUIDField()
    direction = serializers.ChoiceField(choices=Loan.Direction.choices)
    counterparty = serializers.CharField(max_length=150)


class AmanaCreateSerializer(_AmountSerializer):
    part = serializers.UUIDField()
    held_for = serializers.CharField(max_length=150)


class PartEntryCreateSerializer(_AmountSerializer):
    part = serializers.UUIDField()
    direction = serializers.ChoiceField(choices=PartEntry.Direction.choices)
    category = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    date = serializers.DateField(required=False, allow_null=True, default=None)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    owner_only = serializers.BooleanField(required=False, default=False)


# ── read shapes ───────────────────────────────────────────────────────────────────────────────────────────────
class PartEntrySerializer(serializers.ModelSerializer):
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_kind = serializers.CharField(source='part.kind', read_only=True)
    box_name = serializers.CharField(source='box.name', read_only=True)

    class Meta:
        model = PartEntry
        fields = ['id', 'part', 'part_name', 'part_kind', 'direction', 'category', 'amount', 'date', 'box', 'box_name',
                  'description', 'owner_only', 'created_at']
        read_only_fields = fields


class LoanSerializer(serializers.ModelSerializer):
    part_name = serializers.CharField(source='part.name', read_only=True)
    owed = serializers.SerializerMethodField()

    class Meta:
        model = Loan
        fields = ['id', 'part', 'part_name', 'direction', 'counterparty', 'note', 'opened_on', 'is_closed', 'owed']
        read_only_fields = fields

    def get_owed(self, obj):
        s = getattr(obj, 'line_sum', None)
        if s is None:
            return money(ledger.loan_owed(obj))
        return money(s if obj.direction == Loan.Direction.BORROWED else -s)


class AmanaSerializer(serializers.ModelSerializer):
    part_name = serializers.CharField(source='part.name', read_only=True)
    held = serializers.SerializerMethodField()

    class Meta:
        model = Amana
        fields = ['id', 'part', 'part_name', 'held_for', 'note', 'opened_on', 'is_closed', 'held']
        read_only_fields = fields

    def get_held(self, obj):
        s = getattr(obj, 'line_sum', None)
        return money(s if s is not None else ledger.amana_held(obj))


# ── statement lines, as the OWNER sees them ───────────────────────────────────────────────────────────────────
def owner_counterparts(moves, own_box_id):
    """{transfer_id: the other box's name} for every two-line move in `moves` (transfers, draws, put-ins) — ONE query."""
    tids = {m.transfer_id for m in moves if m.transfer_id}
    if not tids:
        return {}
    rows = (MoneyMove.all_objects.filter(transfer_id__in=tids, kind__in=PAIR_KINDS)
            .exclude(box_id=own_box_id).values_list('transfer_id', 'box__name'))
    return dict(rows)


def owner_ref(m, counterpart_names=None):
    """{type, id, label} for the owner: a draw / put-in / transfer leg → the other box; a loan line → the counterparty;
    an amana line → who it is held for; an entry line → its category (else description); a partner line → the partner.
    A reversal carries its original's links, so it is labelled like the line it cancels. Anything else = Wave 1."""
    if m.loan_id:
        return {'type': 'loan', 'id': str(m.loan_id), 'label': m.loan.counterparty}
    if m.amana_id:
        return {'type': 'amana', 'id': str(m.amana_id), 'label': m.amana.held_for}
    if m.part_entry_id:
        e = m.part_entry
        return {'type': 'entry', 'id': str(e.id), 'label': e.category or (e.description or '')[:80]}
    if m.partner_id:
        return {'type': 'partner', 'id': str(m.partner_id), 'label': m.partner.name}
    if m.transfer_id:
        return {'type': 'transfer', 'id': str(m.transfer_id),
                'label': (counterpart_names or {}).get(m.transfer_id, '')}
    return move_ref(m, counterpart_names)


def owner_move_line(m, running_balance=None, counterpart_names=None):
    """A Wave 1 statement line with the owner's label + the box name (loan / amana detail lists span several boxes)."""
    line = move_line(m, running_balance, counterpart_names)
    line['ref'] = owner_ref(m, counterpart_names)
    line['box_name'] = m.box.name
    return line


OWNER_LINE_RELATED = ('box', 'loan', 'amana', 'part_entry', 'partner')


def lines_of(qs):
    """The lines of one loan / amana, oldest first, owner-labelled (no running balance — `owed` / `held` is the sum)."""
    from django.db.models import Exists, OuterRef
    from .views import STATEMENT_RELATED
    moves = list(qs.select_related(*STATEMENT_RELATED, *OWNER_LINE_RELATED)
                 .annotate(is_reversed=Exists(MoneyMove.all_objects.filter(reverses=OuterRef('pk'))))
                 .order_by('at', 'created_at', 'id'))
    return [owner_move_line(m) for m in moves]

