"""Supplier payments use the shop's OWN payment methods (PROLINE-PARITY Upgrade 6).

Before this change a SupplierPayment recorded HOW the shop paid with a fixed code
(CASH / BANK / CARD / OTHER — the `method` column, kept for history). Now it points at one
of the shop's PaymentMethod rows (`payment_method`), the same list the till uses — minus
Ajel (credit is not money paid) and the system store-credit method (a customer's wallet,
never the shop's money).

`pick_method_for_legacy()` maps an old code to one of the shop's methods. It is used by
BOTH the data migration (finance 0018) that fills `payment_method` on existing rows AND the
API fallback for an old browser tab that still sends `method`. It takes plain objects (it
works on the migration's historical models too), so keep it free of model imports.
"""

# Old code → words that mark a matching method name (lower-case, English + Arabic).
LEGACY_NAME_WORDS = {
    'BANK': ('bank', 'بنك'),
    'CARD': ('card', 'كارت', 'بطاقة'),
}


def usable_for_supplier(method):
    """Can this method pay a supplier? Not deleted, not Ajel, not the store-credit wallet."""
    return not (getattr(method, 'is_deleted', False) or method.is_agel
                or getattr(method, 'is_store_credit', False))


def pick_method_for_legacy(methods, code):
    """The shop's method that best matches an old CASH / BANK / CARD / OTHER code, or None.

    `methods` = the shop's PaymentMethod rows, oldest first (any iterable). CASH → the first cash
    method; BANK / CARD → the first method whose name contains "bank" / "card" (any case, or the
    Arabic word); OTHER or no match → None (the caller keeps the old text)."""
    usable = [m for m in methods if usable_for_supplier(m)]
    code = (code or '').upper()
    if code == 'CASH':
        return next((m for m in usable if m.is_cash), None)
    words = LEGACY_NAME_WORDS.get(code)
    if not words:
        return None
    return next((m for m in usable if any(w in (m.name or '').lower() for w in words)), None)


def legacy_code_for(method):
    """The old `method` code to store next to a chosen PaymentMethod, so anything still reading the
    old column sees something true: a cash method → CASH, a "bank" / "card" name → BANK / CARD,
    anything else → OTHER. (The reverse of pick_method_for_legacy.)"""
    if method.is_cash:
        return 'CASH'
    name = (method.name or '').lower()
    for code, words in LEGACY_NAME_WORDS.items():
        if any(w in name for w in words):
            return code
    return 'OTHER'
