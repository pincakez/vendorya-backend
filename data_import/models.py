"""The hidden "came from another system" tag.

Every row an importer creates (or links to) gets ONE ExternalRef: (shop, source system, kind, the
source system's own key) → the Vendorya object. A re-run of the same import finds the tag and
UPDATES that object — it never makes a second one. The tag lives in its own table so no business
model needs extra columns, and one source key can point at two objects of different kinds (e.g. a
party that is both a customer and a supplier in the old system).
"""
import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from core.models import Store, TimestampedModel
from core.tenancy import TenantScopedManager


class ExternalRef(TimestampedModel):
    class Origin(models.TextChoices):
        CREATED = 'CREATED', 'Created by the import'
        LINKED = 'LINKED', 'Linked to a row the shop made by hand'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='external_refs')
    system = models.CharField(max_length=30)          # the source system, e.g. "proline"
    kind = models.CharField(max_length=30)            # category / product / customer / supplier …
    external_key = models.CharField(max_length=255)   # the source system's own key for the row
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.UUIDField()
    target = GenericForeignKey('content_type', 'object_id')
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.CREATED)
    last_run_at = models.DateTimeField(null=True, blank=True)

    objects = TenantScopedManager()
    all_objects = models.Manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['store', 'system', 'kind', 'external_key'],
                                    name='uniq_external_ref_per_store'),
        ]
        indexes = [models.Index(fields=['content_type', 'object_id'], name='external_ref_target_idx')]

    def __str__(self):
        return f'{self.system}:{self.kind}:{self.external_key}'
