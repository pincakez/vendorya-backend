from rest_framework import serializers
from .models import POSFavoriteItem


class POSFavoriteItemSerializer(serializers.ModelSerializer):
    product_name          = serializers.CharField(source='product.name', read_only=True)
    product_sku           = serializers.SerializerMethodField()
    product_price         = serializers.SerializerMethodField()
    # The actual sellable variant — the cart needs this, not the product id.
    default_variant_id    = serializers.SerializerMethodField()
    default_variant_price = serializers.SerializerMethodField()
    default_variant_stock = serializers.SerializerMethodField()

    class Meta:
        model = POSFavoriteItem
        fields = ['id', 'product', 'product_name', 'product_sku', 'product_price',
                  'default_variant_id', 'default_variant_price', 'default_variant_stock', 'order']
        read_only_fields = ['id']

    def _variant(self, obj):
        return obj.product.variants.first()

    def get_product_sku(self, obj):
        v = self._variant(obj)
        return v.sku if v else None

    def get_product_price(self, obj):
        v = self._variant(obj)
        return str(v.sell_price) if v else None

    def get_default_variant_id(self, obj):
        v = self._variant(obj)
        return str(v.id) if v else None

    def get_default_variant_price(self, obj):
        v = self._variant(obj)
        return str(v.sell_price) if v else None

    def get_default_variant_stock(self, obj):
        v = self._variant(obj)
        if not v:
            return 0
        return sum(s.quantity for s in v.stock_levels.all())

    def validate(self, data):
        store = self.context['request'].user.store
        if store is None:
            raise serializers.ValidationError("No store associated with this user.")
        instance = self.instance
        qs = POSFavoriteItem.objects.filter(store=store)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.count() >= 10:
            raise serializers.ValidationError("A store can have at most 10 favorite items.")
        return data
