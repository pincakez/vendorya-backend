from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import Store
from .models import Subscription, SubscriptionPlan, BillingInvoice


@receiver(post_save, sender=Store)
def auto_create_subscription(sender, instance, created, **kwargs):
    """Every new tenant lands on the GO plan, ACTIVE, until sudo says otherwise."""
    if not created:
        return
    plan = SubscriptionPlan.objects.filter(name='GO').first() or \
           SubscriptionPlan.objects.filter(is_active=True).order_by('monthly_price', 'name').first()
    if plan is None:
        return  # no plans seeded yet — fresh DB during tests
    Subscription.objects.get_or_create(
        store=instance,
        defaults={'plan': plan, 'status': Subscription.Status.ACTIVE},
    )


@receiver(post_save, sender=BillingInvoice)
def notify_on_issue(sender, instance, created, **kwargs):
    """When an invoice flips DRAFT → ISSUED, drop an inbox notification for the store owner."""
    # Only fire when status is ISSUED and we haven't already notified.
    if instance.status != BillingInvoice.Status.ISSUED:
        return

    # Import here to avoid circular import at app-loading time.
    from notifications.models import Notification

    already = Notification.objects.filter(
        store=instance.store,
        type=Notification.Type.BILLING_INVOICE,
        payload__invoice_id=str(instance.id),
    ).exists()
    if already:
        return

    owner = instance.store.owner
    Notification.objects.create(
        store=instance.store,
        user=owner,
        type=Notification.Type.BILLING_INVOICE,
        title=f"New invoice: {instance.invoice_number}",
        body=(instance.line_description
              or f"Invoice for {instance.amount} {instance.currency}."),
        link=f"/settings/billing/invoices/{instance.id}",
        payload={
            'invoice_id': str(instance.id),
            'invoice_number': instance.invoice_number,
            'amount': str(instance.amount),
            'currency': instance.currency,
            'due_at': instance.due_at.isoformat() if instance.due_at else None,
        },
    )
