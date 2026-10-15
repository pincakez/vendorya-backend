"""s175 security review: the shop logo and the lock-screen logo take only real pictures (png/jpg/webp/gif) — /media/ is
served from the app's own address, so a web page or SVG saved as a "logo" would run as Vendorya."""
import io

from django.conf import settings as dj_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.models import Store, StoreSettings
from users.models import User


def _png():
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), 'orange').save(buf, format='PNG')
    return SimpleUploadedFile('logo.png', buf.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT='/tmp/vendorya-test-media-s175')
class LogoUploadTests(TestCase):
    def setUp(self):
        dj_settings.ALLOWED_HOSTS = ['*']
        self.owner = User.objects.create_user(username='lg_own', password='x', role='OWNER')
        self.store = Store.objects.create(name='L', store_code='701', owner=self.owner)
        self.owner.store = self.store
        self.owner.save(update_fields=['store'])
        StoreSettings.objects.get_or_create(store=self.store)
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION='Bearer ' + str(RefreshToken.for_user(self.owner).access_token))

    def test_a_web_page_is_refused_on_both_doors(self):
        page = b'<html><script>alert(1)</script></html>'
        r = self.c.patch('/api/core/store/logo/',
                         {'logo_light': SimpleUploadedFile('x.html', page, content_type='text/html')}, format='multipart')
        self.assertEqual(r.status_code, 400, r.content)
        r = self.c.patch('/api/core/lockscreen/logo/',
                         {'lock_logo': SimpleUploadedFile('x.svg', b'<svg onload="alert(1)"/>', content_type='image/svg+xml')},
                         format='multipart')
        self.assertEqual(r.status_code, 400, r.content)
        # a page renamed .png is still not a picture
        r = self.c.patch('/api/core/store/logo/',
                         {'logo_dark': SimpleUploadedFile('x.png', page, content_type='image/png')}, format='multipart')
        self.assertEqual(r.status_code, 400, r.content)
        self.store.refresh_from_db()
        self.assertFalse(self.store.logo_light or self.store.logo_dark)

    def test_a_real_picture_is_kept(self):
        r = self.c.patch('/api/core/store/logo/', {'logo_light': _png()}, format='multipart')
        self.assertEqual(r.status_code, 200, r.content)
        r = self.c.patch('/api/core/lockscreen/logo/', {'lock_logo': _png()}, format='multipart')
        self.assertEqual(r.status_code, 200, r.content)
