"""Starter SPEC SETS a shop can apply to a category in one click (PROLINE-PARITY Upgrade 9).

These are DATA, not product fields: applying one creates the shop's own AttributeDefinitions (if a
definition with the same `key` is not there yet) and attaches them to the chosen category. Nothing
here is wired to one shop or one trade — a shop picks a preset, then adds / removes / reorders
freely. Reusing an existing key never changes that definition (the shop's own options win).

Shape of one field:
    key         slug, the attribute's code name (also the ?key=value filter on the products list)
    name        {'en', 'ar'} — the definition is created in the shop's default language
    input_type  TEXT | NUMBER | SELECT (AttributeDefinition.InputType)
    unit        optional, printed after a NUMBER/SELECT value on labels ("16GB", '14"')
    options     SELECT choices
    required    the product form asks for it before saving (this category only)
"""

SPEC_PRESETS = {
    'laptop': {
        'name': {'en': 'Laptop', 'ar': 'لابتوب'},
        'fields': [
            {'key': 'brand', 'name': {'en': 'Brand', 'ar': 'الماركة'}, 'input_type': 'TEXT'},
            {'key': 'cpu', 'name': {'en': 'Processor (CPU)', 'ar': 'المعالج'}, 'input_type': 'TEXT',
             'required': True},
            {'key': 'ram', 'name': {'en': 'RAM', 'ar': 'الرامات'}, 'input_type': 'SELECT',
             'options': ['4GB', '8GB', '16GB', '32GB', '64GB'], 'required': True},
            {'key': 'storage', 'name': {'en': 'Storage', 'ar': 'التخزين'}, 'input_type': 'SELECT',
             'options': ['128GB SSD', '256GB SSD', '512GB SSD', '1TB SSD', '2TB SSD', '1TB HDD'],
             'required': True},
            {'key': 'display', 'name': {'en': 'Screen size', 'ar': 'حجم الشاشة'}, 'input_type': 'NUMBER',
             'unit': '"'},
            {'key': 'gpu', 'name': {'en': 'Graphics (GPU)', 'ar': 'كارت الشاشة'}, 'input_type': 'TEXT'},
            {'key': 'os', 'name': {'en': 'Operating system', 'ar': 'نظام التشغيل'}, 'input_type': 'SELECT',
             'options': ['Windows 11', 'Windows 10', 'macOS', 'Linux', 'No OS']},
            {'key': 'keyboard', 'name': {'en': 'Keyboard', 'ar': 'الكيبورد'}, 'input_type': 'SELECT',
             'options': ['English', 'Arabic / English']},
            {'key': 'battery', 'name': {'en': 'Battery', 'ar': 'البطارية'}, 'input_type': 'TEXT'},
        ],
    },
    'phone': {
        'name': {'en': 'Phone', 'ar': 'موبايل'},
        'fields': [
            {'key': 'brand', 'name': {'en': 'Brand', 'ar': 'الماركة'}, 'input_type': 'TEXT'},
            {'key': 'storage', 'name': {'en': 'Storage', 'ar': 'التخزين'}, 'input_type': 'SELECT',
             'options': ['64GB', '128GB', '256GB', '512GB', '1TB'], 'required': True},
            {'key': 'ram', 'name': {'en': 'RAM', 'ar': 'الرامات'}, 'input_type': 'SELECT',
             'options': ['4GB', '6GB', '8GB', '12GB', '16GB']},
            {'key': 'color', 'name': {'en': 'Color', 'ar': 'اللون'}, 'input_type': 'TEXT'},
            {'key': 'display', 'name': {'en': 'Screen size', 'ar': 'حجم الشاشة'}, 'input_type': 'NUMBER',
             'unit': '"'},
            {'key': 'sim', 'name': {'en': 'SIM', 'ar': 'الشريحة'}, 'input_type': 'SELECT',
             'options': ['Single SIM', 'Dual SIM', 'eSIM']},
            {'key': 'network', 'name': {'en': 'Network', 'ar': 'الشبكة'}, 'input_type': 'SELECT',
             'options': ['4G', '5G']},
        ],
    },
    'monitor': {
        'name': {'en': 'Monitor', 'ar': 'شاشة'},
        'fields': [
            {'key': 'brand', 'name': {'en': 'Brand', 'ar': 'الماركة'}, 'input_type': 'TEXT'},
            {'key': 'display', 'name': {'en': 'Screen size', 'ar': 'حجم الشاشة'}, 'input_type': 'NUMBER',
             'unit': '"', 'required': True},
            {'key': 'resolution', 'name': {'en': 'Resolution', 'ar': 'الدقة'}, 'input_type': 'SELECT',
             'options': ['1366×768 (HD)', '1920×1080 (FHD)', '2560×1440 (QHD)', '3840×2160 (4K)']},
            {'key': 'refresh_rate', 'name': {'en': 'Refresh rate', 'ar': 'معدل التحديث'}, 'input_type': 'NUMBER',
             'unit': 'Hz'},
            {'key': 'panel', 'name': {'en': 'Panel type', 'ar': 'نوع اللوحة'}, 'input_type': 'SELECT',
             'options': ['IPS', 'VA', 'TN', 'OLED']},
            {'key': 'ports', 'name': {'en': 'Ports', 'ar': 'المنافذ'}, 'input_type': 'TEXT'},
        ],
    },
    'clothing': {
        'name': {'en': 'Clothing', 'ar': 'ملابس'},
        'fields': [
            {'key': 'size', 'name': {'en': 'Size', 'ar': 'المقاس'}, 'input_type': 'SELECT',
             'options': ['XS', 'S', 'M', 'L', 'XL', 'XXL'], 'required': True},
            {'key': 'color', 'name': {'en': 'Color', 'ar': 'اللون'}, 'input_type': 'TEXT'},
            {'key': 'material', 'name': {'en': 'Material', 'ar': 'الخامة'}, 'input_type': 'TEXT'},
            {'key': 'gender', 'name': {'en': 'Gender', 'ar': 'النوع'}, 'input_type': 'SELECT',
             'options': ['Men', 'Women', 'Kids', 'Unisex']},
        ],
    },
    'pharmacy_item': {
        'name': {'en': 'Pharmacy item', 'ar': 'صنف صيدلية'},
        'fields': [
            {'key': 'active_ing', 'name': {'en': 'Active ingredient', 'ar': 'المادة الفعالة'},
             'input_type': 'TEXT', 'required': True},
            {'key': 'strength', 'name': {'en': 'Strength', 'ar': 'التركيز'}, 'input_type': 'TEXT'},
            {'key': 'dosage_form', 'name': {'en': 'Dosage form', 'ar': 'الشكل الدوائي'}, 'input_type': 'SELECT',
             'options': ['Tablet', 'Capsule', 'Syrup', 'Injection', 'Cream', 'Drops']},
            {'key': 'manufacturer', 'name': {'en': 'Manufacturer', 'ar': 'الشركة المصنعة'}, 'input_type': 'TEXT'},
        ],
    },
}


def preset_catalog():
    """The presets as the category screen lists them (both languages; the screen picks one)."""
    return [
        {
            'key': key,
            'name': p['name'],
            'fields': [{'key': f['key'], 'name': f['name'], 'input_type': f['input_type'],
                        'unit': f.get('unit', ''), 'required': bool(f.get('required'))}
                       for f in p['fields']],
        }
        for key, p in SPEC_PRESETS.items()
    ]
