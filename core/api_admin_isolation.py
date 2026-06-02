"""Sudo-only endpoint behind the admin "Isolation Check" button.

POST /api/admin/isolation-check/        → self-contained full-coverage audit
POST /api/admin/isolation-check/?mode=live → read-only audit of real stores

The heavy lifting lives in core.isolation_audit so the exact same engine also
backs a pre-ship test. This view just runs it and records that it was run.
"""
from rest_framework.views import APIView
from rest_framework.response import Response

from users.permissions import IsSuperAdmin
from .activity import log_activity
from .models import ActivityLog
from .isolation_audit import run_self_contained_audit, run_isolation_audit


class AdminIsolationAuditView(APIView):
    """Run the tenant-isolation audit and return the full report card."""
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        mode = request.query_params.get('mode', 'self_contained')
        report = run_isolation_audit() if mode == 'live' else run_self_contained_audit()

        log_activity(
            request=request,
            action=f"Ran tenant isolation audit ({report.get('status')})",
            op_type=ActivityLog.OperationType.OTHER,
            details={
                'mode': report.get('mode'),
                'status': report.get('status'),
                'leaks': report.get('leaks'),
                'endpoints_checked': report.get('endpoints_checked'),
            },
        )
        return Response(report)

    # convenience: allow a plain GET to run it too
    def get(self, request):
        return self.post(request)
