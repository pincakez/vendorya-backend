from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.db.models import Q
from .models import Store
from inventory.models import Product, Supplier, Category
from users.models import Customer

@login_required
def store_global_search_view(request, store_id):
    """Renders the search page."""
    store = get_object_or_404(Store, id=store_id)
    return render(request, 'admin/core/store_search.html', {'store': store})

@login_required
def store_global_search_api(request, store_id):
    """Returns JSON results for the live search."""
    query = request.GET.get('q', '').strip()
    if len(query) < 3:
        return JsonResponse({'results': {}})

    store = get_object_or_404(Store, id=store_id)
    results = {
        'Products': [],
        'Suppliers': [],
        'Categories': [],
        'Customers': []
    }

    # 1. Search Products
    products = Product.objects.filter(store=store).filter(
        Q(name__icontains=query) | Q(product_code__icontains=query)
    )[:5]
    for p in products:
        url = reverse('admin:inventory_product_change', args=[p.id])
        results['Products'].append({'name': f"{p.name} ({p.product_code})", 'url': url})

    # 2. Search Suppliers
    suppliers = Supplier.objects.filter(store=store, name__icontains=query)[:5]
    for s in suppliers:
        url = reverse('admin:inventory_supplier_change', args=[s.id])
        results['Suppliers'].append({'name': s.name, 'url': url})

    # 3. Search Categories
    categories = Category.objects.filter(store=store, name__icontains=query)[:5]
    for c in categories:
        url = reverse('admin:inventory_category_change', args=[c.id])
        results['Categories'].append({'name': c.name, 'url': url})

    # 4. Search Customers
    customers = Customer.objects.filter(store=store).filter(
        Q(name__icontains=query) | Q(phone_number__icontains=query)
    )[:5]
    for c in customers:
        url = reverse('admin:users_customer_change', args=[c.id])
        results['Customers'].append({'name': f"{c.name} ({c.phone_number})", 'url': url})

    return JsonResponse({'results': results})