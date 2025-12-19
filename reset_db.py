import os
import django
from django.db import connection

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vendorya_project.settings')
django.setup()

with connection.cursor() as cursor:
    print("Dropping django_migrations table...")
    cursor.execute("DROP TABLE IF EXISTS django_migrations CASCADE;")
    print("Done.")
