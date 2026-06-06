from rest_framework import serializers
from .models import POSFavoriteItem


class POSFavoriteItemSerializer(serializers.ModelSerializer):
    product_name  = serializers.CharField(source='product.name', read_only=True)
    product_sku   = serializers.SerializerMethodField()
    product_price = serializers.SerializerMethodField()

    class Meta:
        model = POSFavoriteItem
        fields = ['id', 'product', 'product_name', 'product_sku', 'product_price', 'order']
        read_only_fields = ['id']

    def get_product_sku(self, obj):
        v = obj.product.variants.first()
        return v.sku if v else None

    def get_product_price(self, obj):
        v = obj.product.variants.first()
        return str(v.sell_price) if v else None

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
