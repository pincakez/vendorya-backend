from decimal import Decimal
import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from core.models import Store, Branch
from inventory.models import ProductVariant, StockLevel
from finance.models import SalesInvoice, SalesInvoiceItem, Payment, PaymentMethod, WorkShift
from users.models import Customer

@login_required
def pos_view(request, store_id):
    """Renders the Mini-POS Interface."""
    store = get_object_or_404(Store, id=store_id)
    # Get the first branch for this store (Simulated for PoC)
    branch = Branch.objects.filter(store=store).first()
    
    context = {
        'store': store,
        'branch': branch,
    }
    return render(request, 'admin/finance/pos.html', context)

@login_required
def pos_search_api(request, store_id):
    """Live Search for Products (Variants)."""
    query = request.GET.get('q', '').strip()
    if len(query) < 3:
        return JsonResponse({'results': []})

    store = get_object_or_404(Store, id=store_id)
    
    # Search Variants by Name, SKU, or Barcode
    variants = ProductVariant.objects.filter(product__store=store).filter(
        Q(product__name__icontains=query) | 
        Q(sku__icontains=query) | 
        Q(barcode__icontains=query)
    )[:10] # Limit to 10 results

    results = []
    for v in variants:
        # Get stock for the first branch (PoC)
        branch = Branch.objects.filter(store=store).first()
        stock = StockLevel.objects.filter(variant=v, branch=branch).first()
        qty = stock.quantity if stock else 0

        results.append({
            'id': str(v.id),
            'name': f"{v.product.name} ({v.sku})",
            'price': float(v.sell_price),
            'stock': float(qty)
        })

    return JsonResponse({'results': results})

@login_required
@transaction.atomic
def pos_checkout_api(request, store_id):
    """Handles the transaction (Invoice + Stock + Payment)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid method'}, status=405)

    data = json.loads(request.body)
    items = data.get('items', [])
    
    if not items:
        return JsonResponse({'error': 'Cart is empty'}, status=400)

    store = get_object_or_404(Store, id=store_id)
    branch = Branch.objects.filter(store=store).first()
    user = request.user

    # 1. Check for Open Shift
    shift = WorkShift.objects.filter(user=user, store=store, status=WorkShift.Status.OPEN).first()
    if not shift:
        return JsonResponse({'error': 'No Open Shift found! Please open a shift in Finance first.'}, status=403)

    # 2. Get Default Customer (Walk-in)
    # For PoC, we grab the first customer or create a dummy one
    customer = Customer.objects.filter(store=store).first()
    if not customer:
        return JsonResponse({'error': 'No customers found in store. Create one first.'}, status=400)

    # 3. Create Invoice
    invoice = SalesInvoice.objects.create(
        store=store,
        branch=branch,
        customer=customer,
        status=SalesInvoice.Status.POSTED, # Finalized immediately
        date=timezone.now()
    )

    # 4. Add Items & Deduct Stock
    for item in items:
        variant = ProductVariant.objects.get(id=item['id'])
        qty = float(item['qty'])
        
        # Create Line Item
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            variant=variant,
            quantity=qty,
            unit_price=variant.sell_price
        )

        # Deduct Stock
        stock_level, created = StockLevel.objects.get_or_create(variant=variant, branch=branch)
        stock_level.quantity -= Decimal(qty)
        stock_level.save()

    # 5. Create Payment (Cash)
    # Find a cash method
    cash_method = PaymentMethod.objects.filter(store=store, is_cash=True).first()
    if not cash_method:
        # Fallback if no method exists
        cash_method = PaymentMethod.objects.create(store=store, name="Cash", is_cash=True)

    Payment.objects.create(
        invoice=invoice,
        method=cash_method,
        amount=invoice.grand_total, # Full payment
        created_by=user
    )

    return JsonResponse({
        'success': True, 
        'invoice_number': invoice.invoice_number,
        'total': float(invoice.grand_total)
    })