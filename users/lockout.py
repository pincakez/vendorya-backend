from django.http import JsonResponse


def lockout_response(request, credentials=None, *args, **kwargs):
    """Returned by django-axes when a user/IP is locked out.

    JSON 429 so the SPA can show a clear message instead of Django's HTML page.
    """
    return JsonResponse(
        {
            'detail': 'Account locked due to too many failed login attempts. '
                      'Try again later or contact your administrator.',
            'code': 'account_locked',
        },
        status=429,
    )
