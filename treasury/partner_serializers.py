"""Owner level API shapes — part B (TREASURY.md § 12.4): partners, settlements, business P&L. A CONTRACT the screens are
built against: never rename a field. Money goes out as strings with 2 decimals (`serializers.money`)."""
from decimal import Decimal

from rest_framework import serializers

from . import ledger
from .models import Partner
from .serializers import money, user_name


class PartnerSerializer(serializers.ModelSerializer):
    """Read shape + create / patch input. `part` is checked against the owner in the view (another owner's = 404) and
    is fixed once the partner exists; shares go through `ledger.check_shares` in the view."""
    part = serializers.UUIDField(source='part_id')
    part_name = serializers.CharField(source='part.name', read_only=True)
    part_kind = serializers.CharField(source='part.kind', read_only=True)
    share_pct = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0.01'),
                                         max_value=Decimal('99.99'))
    account = serializers.SerializerMethodField()

    class Meta:
        model = Partner
        fields = ['id', 'part', 'part_name', 'part_kind', 'name', 'share_pct', 'is_active', 'joined_on', 'account']
        read_only_fields = ['id']

    def get_account(self, obj):
        return money(ledger.partner_account(obj))

    def validate_name(self, v):
        v = v.strip()
        if not v:
            raise serializers.ValidationError('Write the partner\'s name.')
        return v


class PartnerMoneySerializer(serializers.Serializer):
    """POST partners/{id}/put-in/ and pay/ — the box is resolved against the owner in the view."""
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    box = serializers.UUIDField()
    note = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_amount(self, v):
        if v <= 0:
            raise serializers.ValidationError('The amount must be above zero.')
        return v


class SettlementCreateSerializer(serializers.Serializer):
    part = serializers.UUIDField()
    date_from = serializers.DateField()
    date_to = serializers.DateField()


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


def _line(partner, share_pct, amount):
    return {'partner': str(partner.id) if partner else None, 'partner_name': partner.name if partner else None,
            'share_pct': money(share_pct), 'amount': money(amount)}


def settlement_data(s):
    """A saved settlement. Lines: the partners by name, then the owner's own share (`partner` null) last."""
    rows = sorted(s.lines.all(), key=lambda l: (l.partner_id is None, l.partner.name if l.partner_id else '',
                                                str(l.partner_id or '')))
    return {
        'id': str(s.id), 'part': str(s.part_id), 'part_name': s.part.name,
        'date_from': s.date_from, 'date_to': s.date_to,
        'profit': money(s.profit), 'owner_only_expenses': money(s.owner_only_expenses),
        'lines': [_line(l.partner, l.share_pct, l.amount) for l in rows],
        'cancelled_at': s.cancelled_at, 'cancel_reason': s.cancel_reason,
        'created_at': s.created_at, 'created_by_name': user_name(s.created_by),
    }


def preview_data(part, date_from, date_to, fig):
    """`ledger.settlement_preview` in the saved shape (nothing saved: no id, never cancelled) + `base`."""
    return {
        'id': None, 'part': str(part.id), 'part_name': part.name, 'date_from': date_from, 'date_to': date_to,
        'profit': money(fig['profit']), 'owner_only_expenses': money(fig['owner_only_expenses']),
        'base': money(fig['base']),
        'lines': ([_line(l['partner'], l['share_pct'], l['amount']) for l in fig['lines']]
                  + [_line(None, fig['owner_share_pct'], fig['owner_amount'])]),
        'cancelled_at': None,
    }

