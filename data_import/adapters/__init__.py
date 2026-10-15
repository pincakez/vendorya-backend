"""One adapter per source system. Add a system = add a module here + one line below."""
from importlib import import_module

ADAPTERS = {
    'proline': 'data_import.adapters.proline.ProlineAdapter',
}


def get_adapter(system):
    path = ADAPTERS[system]
    module, cls = path.rsplit('.', 1)
    return getattr(import_module(module), cls)
