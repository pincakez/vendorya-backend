"""Admin → SYSTEM → SKU Management (s159, Yakot 2026-10-07) — SUDO ONLY.

  GET   /api/admin/stores/<id>/sku/                 the shop's SKU setup + SKU2 settings + a live preview
  PATCH /api/admin/stores/<id>/sku/                 change them (rules below)
  POST  /api/admin/stores/<id>/sku2/disable/        SKU2 off FOR GOOD: {password, delete_history}

Rules (TODO §SKU-SETUP + §SKU2):
- The SKU setup is chosen with the shop owner before the first product. Once the shop HAS SKUs, a
  change needs sudo's password again — and only NEW SKUs follow it (old ones and their stickers never change).
- SKU2 goes OFF → ON once. There is no way back to OFF: the only exit is "disable for good" (password +
  the page's last warning), which can keep the existing SKU2s as history or wipe them. DISABLED is final.
"""
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.activity import log_activity
from core.models import ActivityLog, Store, StoreSettings
from users.permissions import IsSuperAdmin

SETUP_FIELDS = ('sku_product_digits', 'sku_supplier_digits', 'sku_shop_code', 'sku_dashes', 'product_numbering_mode')
SKU2_FIELDS = ('sku2_state', 'sku2_digits', 'sku2_mode', 'sku2_auto_new',
               'sku2_hide_tables', 'sku2_hide_search', 'sku2_print_label')


class SkuSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoreSettings
        fields = SETUP_FIELDS + SKU2_FIELDS

    def validate_sku2_state(self, value):
        current = self.instance.sku2_state
        if value == current:
            return value
        if current == StoreSettings.Sku2State.OFF and value == StoreSettings.Sku2State.ON:
            return value
        if current == StoreSettings.Sku2State.DISABLED:
            raise serializers.ValidationError('SKU2 was disabled for good — it can never be switched on again.')
        raise serializers.ValidationError('SKU2 can only be turned off with "Disable for good".')


def _store(store_id):
    return Store.all_objects.filter(pk=store_id, is_deleted=False).first()


def _payload(store):
    from inventory.models import ProductVariant
    from inventory.sku import assemble, shop_code, supplier_code_width, next_free_supplier_code
    st = store.settings
    variants = ProductVariant.all_objects.filter(store=store)
    width = supplier_code_width(store)
    sample_supplier = next_free_supplier_code(store) or ('1' + '0' * (width - 1))
    return {
        **SkuSettingsSerializer(st).data,
        'store_id': str(store.id), 'store_name': store.name, 'store_code': store.store_code or '',
        'shop_code_in_use': shop_code(store),
        'skus_count': variants.filter(sku__isnull=False).count(),
        'sku2_count': variants.filter(sku2__isnull=False).count(),
        'supplier_code_width': width,
        # What the NEXT SKU of a new supplier would look like with these settings.
        'preview': assemble(1, st.sku_product_digits, sample_supplier, shop_code(store) or '00', st.sku_dashes),
        'setup_locked': variants.filter(sku__isnull=False).exists(),
    }


class AdminStoreSkuView(APIView):
    permission_classes = [IsSuperAdmin]

    def get(self, request, store_id):
        store = _store(store_id)
        if store is None:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_payload(store))

    def patch(self, request, store_id):
        store = _store(store_id)
        if store is None:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)
        st = store.settings
        data = {k: v for k, v in request.data.items() if k in SETUP_FIELDS + SKU2_FIELDS}
        ser = SkuSettingsSerializer(st, data=data, partial=True)
        ser.is_valid(raise_exception=True)
        changed = {k: v for k, v in ser.validated_data.items() if getattr(st, k) != v}
        setup_changed = [k for k in changed if k in SETUP_FIELDS and k != 'product_numbering_mode']
        if setup_changed and _payload(store)['setup_locked']:
            if not request.user.check_password(request.data.get('password') or ''):
                return Response({'detail': 'This shop already has SKUs. Type your password to change the setup.',
                                 'code': 'password_required'}, status=status.HTTP_403_FORBIDDEN)
        if not changed:
            return Response(_payload(store))
        ser.save()
        log_activity(request=request, store=store, op_type=ActivityLog.OperationType.OTHER,
                     action='SKU settings changed (sudo)',
                     details={k: str(v) for k, v in changed.items()})
        return Response(_payload(store))


class AdminStoreSku2DisableView(APIView):
    """SKU2 off FOR GOOD. `delete_history` false = products keep their SKU2 as history (shown, never
    used); true = every SKU2 in the shop is wiped. Either way it can never be switched on again."""
    permission_classes = [IsSuperAdmin]

    def post(self, request, store_id):
        from inventory.models import ProductVariant
        store = _store(store_id)
        if store is None:
            return Response({'detail': 'Store not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not request.user.check_password(request.data.get('password') or ''):
            return Response({'detail': 'Wrong password.', 'code': 'password_required'},
                            status=status.HTTP_403_FORBIDDEN)
        st = store.settings
        if st.sku2_state == StoreSettings.Sku2State.DISABLED:
            return Response({'detail': 'SKU2 is already disabled for good.'}, status=status.HTTP_400_BAD_REQUEST)
        wipe = bool(request.data.get('delete_history'))
        wiped = 0
        if wipe:
            wiped = ProductVariant.all_objects.filter(store=store, sku2__isnull=False).update(sku2=None)
        st.sku2_state = StoreSettings.Sku2State.DISABLED
        st.save(update_fields=['sku2_state', 'updated_at'])
        log_activity(request=request, store=store, op_type=ActivityLog.OperationType.OTHER,
                     action='SKU2 disabled for good (sudo)',
                     details={'history_deleted': wipe, 'sku2_wiped': wiped})
        return Response(_payload(store))
