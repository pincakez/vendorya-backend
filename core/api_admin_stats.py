from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from users.permissions import IsSuperAdmin
from django.db.models import Count, Max
from django.utils import timezone


class AdminApiStatsView(APIView):
    """Sudo-only: Gemini API usage stats for the §DE drug enrichment."""
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        from inventory.models import Product, DrugProfile

        # DrugProfile enrichment counts
        total_profiles   = DrugProfile.objects.count()
        needs_review     = DrugProfile.objects.filter(needs_review=True).count()
        last_enriched    = DrugProfile.objects.aggregate(last=Max('enriched_at'))['last']

        # Breakdown by model
        by_model = list(
            DrugProfile.objects.values('model_used')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Unenriched MB products
        total_mb        = Product.objects.filter(source='MEMORY_BASE', is_deleted=False).count()
        enriched_linked = Product.objects.filter(
            source='MEMORY_BASE', is_deleted=False, drug_profile__isnull=False
        ).count()
        unenriched      = total_mb - enriched_linked

        # Rough solid-dose estimate (same filter used in the enrichment script)
        LIQUID_KEYWORDS = [
            'شراب', 'قطرة', 'قطر', 'حقن', 'حقنة', 'محلول', 'معلق', 'مرهم',
            'بخاخ', 'لبوس', 'تحاميل', 'جل', 'غسول', 'لصقة', 'كريم', 'غرغرة',
            'syrup', 'drops', 'drop', 'injection', 'solution', 'suspension',
            'ointment', 'spray', 'suppository', 'gel', 'lotion', 'patch',
            'infusion', 'vial', 'ampoule', 'amp', 'cream', 'emulsion',
            'enema', 'inhaler', 'inhalation', 'serum', 'tonic', 'wash',
        ]
        from django.db.models import Q
        liquid_q = Q()
        for kw in LIQUID_KEYWORDS:
            liquid_q |= Q(name__icontains=kw)
        solid_dose_total = Product.objects.filter(
            source='MEMORY_BASE', is_deleted=False
        ).exclude(liquid_q).count()

        solid_enriched = Product.objects.filter(
            source='MEMORY_BASE', is_deleted=False, drug_profile__isnull=False
        ).exclude(liquid_q).count()

        pct = round(solid_enriched / solid_dose_total * 100, 1) if solid_dose_total else 0

        return Response({
            'enrichment': {
                'total_profiles':   total_profiles,
                'needs_review':     needs_review,
                'last_enriched_at': last_enriched,
                'by_model':         by_model,
            },
            'catalog': {
                'total_mb':          total_mb,
                'solid_dose_total':  solid_dose_total,
                'solid_enriched':    solid_enriched,
                'unenriched':        unenriched,
                'pct_complete':      pct,
            },
            'as_of': timezone.now(),
        })
