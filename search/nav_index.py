"""Static nav + settings index for the global search bar.

Each entry: id, path, label (EN), label_ar (AR), optional parent/parent_ar,
group, kw (EN keywords), kw_ar (AR keywords).

No DB needed — this is the single source of truth.  Indexing into Typesense
gives typo-tolerance in both languages; client-side PAGES array gives instant
fallback. The two must stay in sync when new pages ship.
"""

NAV_ITEMS = [
    # ── General ───────────────────────────────────────────────────────────
    {'id': 'dashboard', 'path': '/dashboard',
     'label': 'Dashboard', 'label_ar': 'لوحة التحكم', 'group': 'general',
     'kw': 'home overview summary totals sales quick look',
     'kw_ar': 'الرئيسية نظرة عامة ملخص المبيعات'},

    {'id': 'pos', 'path': '/pos',
     'label': 'POS — Point of Sale', 'label_ar': 'نقطة البيع',
     'group': 'general',
     'kw': 'cashier sale checkout counter sell invoice receipt',
     'kw_ar': 'كاشير بيع فاتورة إيصال دفع عميل'},

    {'id': 'activity_log', 'path': '/activity-log',
     'label': 'Activity Log', 'label_ar': 'سجل النشاط', 'group': 'general',
     'kw': 'history audit log who changed actions',
     'kw_ar': 'سجل التاريخ تدقيق من غيّر الإجراءات'},

    {'id': 'inbox', 'path': '/inbox',
     'label': 'Notifications Inbox', 'label_ar': 'صندوق الإشعارات',
     'group': 'general',
     'kw': 'alerts messages inbox notifications bell',
     'kw_ar': 'تنبيهات رسائل إشعارات جرس صندوق'},

    # ── Inventory ─────────────────────────────────────────────────────────
    {'id': 'products', 'path': '/inventory/products',
     'label': 'Products', 'label_ar': 'المنتجات', 'group': 'inventory',
     'kw': 'items drugs medicines stock sku barcode search add edit product list',
     'kw_ar': 'منتجات أدوية مستلزمات مخزون باركود إضافة بحث'},

    {'id': 'purchases', 'path': '/inventory/purchases',
     'label': 'Purchases', 'label_ar': 'المشتريات', 'group': 'inventory',
     'kw': 'receive supplier invoice stock in buy purchase order receive',
     'kw_ar': 'مشتريات استلام مورد فاتورة شراء مخزون'},

    {'id': 'adjustments', 'path': '/inventory/adjustments',
     'label': 'Stock Adjustments', 'label_ar': 'تعديلات المخزون',
     'group': 'inventory',
     'kw': 'correct opening balance ledger movement adjust stock count',
     'kw_ar': 'تعديل مخزون جرد رصيد حركة'},

    {'id': 'memory_base', 'path': '/inventory/memory-base',
     'label': 'Memory Base', 'label_ar': 'قاعدة الذاكرة', 'group': 'inventory',
     'kw': 'drug catalog reference index names medicines database mb',
     'kw_ar': 'قاعدة ذاكرة كتالوج أدوية مرجع قاموس'},

    {'id': 'categories', 'path': '/inventory/categories',
     'label': 'Categories', 'label_ar': 'التصنيفات', 'group': 'inventory',
     'kw': 'groups types classify organize tree subcategory',
     'kw_ar': 'تصنيفات أقسام مجموعات ترتيب شجرة'},

    {'id': 'attributes', 'path': '/inventory/attributes',
     'label': 'Attributes', 'label_ar': 'السمات', 'group': 'inventory',
     'kw': 'fields manufacturer active ingredient custom meta color size',
     'kw_ar': 'سمات خصائص شركة مكون فعّال مخصص'},

    {'id': 'storage', 'path': '/inventory/storage',
     'label': 'Storage', 'label_ar': 'التخزين', 'group': 'inventory',
     'kw': 'warehouse shelf location bin rack storage locations',
     'kw_ar': 'تخزين مستودع رف موقع صندوق'},

    {'id': 'import_export', 'path': '/inventory/import-export',
     'label': 'Import / Export', 'label_ar': 'استيراد / تصدير',
     'group': 'inventory',
     'kw': 'csv bulk upload download data import export excel',
     'kw_ar': 'استيراد تصدير ملف بيانات تحميل رفع'},

    # ── Finance ───────────────────────────────────────────────────────────
    {'id': 'invoices', 'path': '/finance/invoices',
     'label': 'Sales Invoices', 'label_ar': 'فواتير المبيعات',
     'group': 'finance',
     'kw': 'invoice sale receipt order history customer list',
     'kw_ar': 'فواتير مبيعات إيصالات طلبات تاريخ'},

    {'id': 'returns', 'path': '/finance/returns',
     'label': 'Returns', 'label_ar': 'المرتجعات', 'group': 'finance',
     'kw': 'refund return exchange cancel reverse',
     'kw_ar': 'مرتجع إرجاع استرداد إلغاء'},

    {'id': 'expenses', 'path': '/finance/expenses',
     'label': 'Expenses', 'label_ar': 'المصروفات', 'group': 'finance',
     'kw': 'cost spending overhead bills expense',
     'kw_ar': 'مصروفات تكاليف نفقات فواتير'},

    {'id': 'shifts', 'path': '/finance/shifts',
     'label': 'Shifts', 'label_ar': 'الورديات', 'group': 'finance',
     'kw': 'open close shift daily session cash cashier',
     'kw_ar': 'وردية فتح غلق جلسة يومية كاشير'},

    {'id': 'cash_drawer', 'path': '/finance/cash-drawer',
     'label': 'Cash Drawer', 'label_ar': 'درج النقدية', 'group': 'finance',
     'kw': 'drawer float cash in out petty cash',
     'kw_ar': 'درج نقدية صندوق فلوس'},

    # ── People ────────────────────────────────────────────────────────────
    {'id': 'customers', 'path': '/people/customers',
     'label': 'Customers', 'label_ar': 'العملاء', 'group': 'people',
     'kw': 'clients patients buyer contact debt credit customer',
     'kw_ar': 'عملاء مرضى مشترين جهات اتصال دين'},

    {'id': 'suppliers', 'path': '/people/suppliers',
     'label': 'Suppliers', 'label_ar': 'الموردون', 'group': 'people',
     'kw': 'vendor company distributor source supplier',
     'kw_ar': 'موردون شركات موزعون مصادر'},

    {'id': 'staff', 'path': '/people/staff',
     'label': 'Staff', 'label_ar': 'الموظفون', 'group': 'people',
     'kw': 'employees cashier manager role user account staff team',
     'kw_ar': 'موظفون كاشير مدير دور مستخدم فريق'},

    # ── Reports ───────────────────────────────────────────────────────────
    {'id': 'rpt_sales', 'path': '/reports/sales',
     'label': 'Sales Report', 'label_ar': 'تقرير المبيعات', 'group': 'reports',
     'kw': 'revenue chart trend analytics sales report',
     'kw_ar': 'تقرير مبيعات إيرادات إحصاءات'},

    {'id': 'rpt_profit', 'path': '/reports/profit',
     'label': 'Profit & Margin', 'label_ar': 'الأرباح والهامش', 'group': 'reports',
     'kw': 'margin gross net cost profit',
     'kw_ar': 'أرباح هامش صافي تكلفة ربح'},

    {'id': 'rpt_pnl', 'path': '/reports/pnl',
     'label': 'P&L Statement', 'label_ar': 'بيان الأرباح والخسائر',
     'group': 'reports',
     'kw': 'profit loss income statement financial pnl',
     'kw_ar': 'أرباح خسائر دخل بيان مالي'},

    {'id': 'rpt_tax', 'path': '/reports/tax',
     'label': 'Tax Report', 'label_ar': 'تقرير الضريبة', 'group': 'reports',
     'kw': 'vat gst tax report',
     'kw_ar': 'ضريبة قيمة مضافة تقرير'},

    # ── Settings — parent pages ────────────────────────────────────────────
    {'id': 'set_store', 'path': '/settings',
     'label': 'Store Settings', 'label_ar': 'إعدادات المتجر', 'group': 'settings',
     'kw': 'store info general configuration setup name phone address currency timezone logo',
     'kw_ar': 'إعدادات المتجر معلومات اسم هاتف عنوان عملة منطقة زمنية شعار'},

    {'id': 'set_capabilities', 'path': '/settings/capabilities',
     'label': 'Capabilities', 'label_ar': 'الإمكانيات', 'group': 'settings',
     'kw': 'multi unit pharmacy fefo expiry weight selling mode features toggle enable disable',
     'kw_ar': 'إمكانيات وحدات متعددة صيدلية انتهاء صلاحية بيع بالوزن ميزات'},

    {'id': 'set_lockscreen', 'path': '/settings/lockscreen',
     'label': 'Lock Screen', 'label_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'lock screen auto lock idle timeout pin code password logo facts',
     'kw_ar': 'شاشة قفل قفل تلقائي خمول رمز سر كلمة مرور شعار معلومات'},

    {'id': 'set_pos', 'path': '/settings/pos',
     'label': 'POS Settings', 'label_ar': 'إعدادات نقطة البيع', 'group': 'settings',
     'kw': 'pos clock 12h 24h time format cart display favorites top selling ux keyboard',
     'kw_ar': 'إعدادات نقطة بيع ساعة تنسيق وقت عرض سلة المفضلة'},

    {'id': 'set_taxes', 'path': '/settings/taxes',
     'label': 'Taxes', 'label_ar': 'الضرائب', 'group': 'settings',
     'kw': 'vat rate tax rate percentage enable disable',
     'kw_ar': 'ضرائب قيمة مضافة نسبة مئوية'},

    {'id': 'set_profile', 'path': '/settings/profile',
     'label': 'My Profile', 'label_ar': 'ملفي الشخصي', 'group': 'settings',
     'kw': 'account profile name email avatar photo language',
     'kw_ar': 'ملف شخصي حساب اسم بريد إلكتروني صورة لغة'},

    {'id': 'set_security', 'path': '/settings/security',
     'label': 'Security', 'label_ar': 'الأمان', 'group': 'settings',
     'kw': 'password 2fa two factor auth login access change password security',
     'kw_ar': 'أمان كلمة مرور تحقق ثنائي تسجيل دخول وصول'},

    {'id': 'set_notifications', 'path': '/settings/notifications',
     'label': 'Notifications', 'label_ar': 'الإشعارات', 'group': 'settings',
     'kw': 'alerts push email sound notification preferences bell',
     'kw_ar': 'إشعارات تنبيهات صوت تفضيلات جرس'},

    {'id': 'set_billing', 'path': '/settings/billing',
     'label': 'Billing', 'label_ar': 'الفواتير', 'group': 'settings',
     'kw': 'subscription plan payment invoice billing upgrade',
     'kw_ar': 'فواتير اشتراك خطة دفع ترقية'},

    {'id': 'set_changelog', 'path': '/settings/changelog',
     'label': "What's New", 'label_ar': 'الجديد', 'group': 'settings',
     'kw': 'updates version release new features changelog whats new',
     'kw_ar': 'تحديثات إصدار جديد ميزات سجل تغييرات'},

    # ── Settings — child options (show as "Lock Screen > PIN Code") ─────────
    {'id': 'set_lock_timeout', 'path': '/settings/lockscreen',
     'label': 'Auto-Lock Timer', 'label_ar': 'مؤقت القفل التلقائي',
     'parent': 'Lock Screen', 'parent_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'auto lock timer idle timeout minutes disable 5 10 20 30',
     'kw_ar': 'قفل تلقائي مؤقت خمول دقائق تعطيل'},

    {'id': 'set_lock_pin', 'path': '/settings/lockscreen',
     'label': 'PIN Code', 'label_ar': 'رمز PIN',
     'parent': 'Lock Screen', 'parent_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'pin code lock password 4 6 digits set change remove unlock',
     'kw_ar': 'رمز سر قفل كلمة مرور أرقام تغيير إزالة'},

    {'id': 'set_lock_logo', 'path': '/settings/lockscreen',
     'label': 'Lock Screen Logo', 'label_ar': 'شعار شاشة القفل',
     'parent': 'Lock Screen', 'parent_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'lock logo image upload branding lock screen',
     'kw_ar': 'شعار شاشة قفل صورة رفع علامة تجارية'},

    {'id': 'set_lock_facts', 'path': '/settings/lockscreen',
     'label': 'Lock Screen Facts', 'label_ar': 'معلومات شاشة القفل',
     'parent': 'Lock Screen', 'parent_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'facts did you know fun trivia lock screen generate ai',
     'kw_ar': 'معلومات هل تعلم حقائق ممتعة شاشة قفل توليد'},

    {'id': 'set_store_type', 'path': '/settings/lockscreen',
     'label': 'Store Type', 'label_ar': 'نوع المتجر',
     'parent': 'Lock Screen', 'parent_ar': 'شاشة القفل', 'group': 'settings',
     'kw': 'store type pharmacy grocery electronics clothing general retail',
     'kw_ar': 'نوع متجر صيدلية بقالة إلكترونيات ملابس تجزئة عامة'},

    {'id': 'set_multi_unit', 'path': '/settings/capabilities',
     'label': 'Multi-Unit Selling', 'label_ar': 'البيع بوحدات متعددة',
     'parent': 'Capabilities', 'parent_ar': 'الإمكانيات', 'group': 'settings',
     'kw': 'multi unit strip pack tablet selling units enable disable',
     'kw_ar': 'وحدات متعددة شريط علبة حبة بيع تفعيل'},

    {'id': 'set_expiry', 'path': '/settings/capabilities',
     'label': 'Expiry & Batch Tracking', 'label_ar': 'تتبع الصلاحية والدفعات',
     'parent': 'Capabilities', 'parent_ar': 'الإمكانيات', 'group': 'settings',
     'kw': 'expiry batch fefo tracking enable pharmacy',
     'kw_ar': 'صلاحية دفعات تتبع انتهاء صيدلية'},

    {'id': 'set_weight', 'path': '/settings/capabilities',
     'label': 'Weight Selling', 'label_ar': 'البيع بالوزن',
     'parent': 'Capabilities', 'parent_ar': 'الإمكانيات', 'group': 'settings',
     'kw': 'weight kg sell by weight decimal grocery',
     'kw_ar': 'وزن كيلو بيع بالوزن كسور عشرية بقالة'},

    {'id': 'set_clock', 'path': '/settings/pos',
     'label': 'POS Clock Format', 'label_ar': 'تنسيق ساعة نقطة البيع',
     'parent': 'POS Settings', 'parent_ar': 'إعدادات نقطة البيع', 'group': 'settings',
     'kw': 'clock 12h 24h time format pos topbar am pm',
     'kw_ar': 'ساعة تنسيق وقت 12 24 نقطة بيع'},

    {'id': 'set_cart_display', 'path': '/settings/pos',
     'label': 'Cart Display Fields', 'label_ar': 'حقول عرض السلة',
     'parent': 'POS Settings', 'parent_ar': 'إعدادات نقطة البيع', 'group': 'settings',
     'kw': 'cart display fields sub-label category attribute pos',
     'kw_ar': 'حقول عرض سلة تسميات فئة سمة نقطة بيع'},

    {'id': 'set_2fa', 'path': '/settings/security',
     'label': 'Two-Factor Authentication', 'label_ar': 'المصادقة الثنائية',
     'parent': 'Security', 'parent_ar': 'الأمان', 'group': 'settings',
     'kw': '2fa two factor auth otp authenticator security',
     'kw_ar': 'مصادقة ثنائية تحقق أمان رمز'},

    {'id': 'set_password', 'path': '/settings/security',
     'label': 'Change Password', 'label_ar': 'تغيير كلمة المرور',
     'parent': 'Security', 'parent_ar': 'الأمان', 'group': 'settings',
     'kw': 'change password account security update',
     'kw_ar': 'تغيير كلمة مرور حساب أمان'},

    {'id': 'set_store_logo', 'path': '/settings',
     'label': 'Store Logo', 'label_ar': 'شعار المتجر',
     'parent': 'Store Settings', 'parent_ar': 'إعدادات المتجر', 'group': 'settings',
     'kw': 'logo light dark upload branding store image',
     'kw_ar': 'شعار متجر صورة رفع علامة تجارية فاتح داكن'},

    {'id': 'set_currency', 'path': '/settings',
     'label': 'Currency', 'label_ar': 'العملة',
     'parent': 'Store Settings', 'parent_ar': 'إعدادات المتجر', 'group': 'settings',
     'kw': 'currency egp usd eur symbol exchange rate',
     'kw_ar': 'عملة جنيه دولار يورو رمز'},

    {'id': 'set_sounds', 'path': '/settings/notifications',
     'label': 'Notification Sounds', 'label_ar': 'أصوات الإشعارات',
     'parent': 'Notifications', 'parent_ar': 'الإشعارات', 'group': 'settings',
     'kw': 'sounds notification alert info warning mute preview',
     'kw_ar': 'أصوات إشعارات تنبيه تحذير كتم معاينة'},
]
