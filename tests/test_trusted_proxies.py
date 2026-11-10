"""
TRUSTED_PROXIES: за nginx клиентский IP и схема берутся из X-Forwarded-*,
без доверия (0) — заголовки игнорируются (иначе клиент подделывает IP
и обходит лимитер).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request

from config import apply_trusted_proxies


def make_app(trusted):
    app = Flask(__name__)
    app.config['TRUSTED_PROXIES'] = trusted
    apply_trusted_proxies(app)

    @app.route('/who')
    def who():
        return f'{request.remote_addr}|{request.scheme}'

    return app


NGINX = {'REMOTE_ADDR': '127.0.0.1'}


class TrustedProxiesTest(unittest.TestCase):
    def test_behind_nginx_real_client_ip_and_https(self):
        c = make_app(1).test_client()
        r = c.get('/who', environ_base=NGINX, headers={
            'X-Forwarded-For': '203.0.113.7',
            'X-Forwarded-Proto': 'https',
        })
        self.assertEqual(r.get_data(as_text=True), '203.0.113.7|https')

    def test_spoofed_left_values_ignored(self):
        # Клиент прислал свой X-Forwarded-For, nginx дописал реальный IP справа
        c = make_app(1).test_client()
        r = c.get('/who', environ_base=NGINX, headers={
            'X-Forwarded-For': '1.2.3.4, 203.0.113.7',
        })
        self.assertEqual(r.get_data(as_text=True), '203.0.113.7|http')

    def test_disabled_by_default_headers_not_trusted(self):
        c = make_app(0).test_client()
        r = c.get('/who', environ_base=NGINX, headers={
            'X-Forwarded-For': '203.0.113.7',
            'X-Forwarded-Proto': 'https',
        })
        self.assertEqual(r.get_data(as_text=True), '127.0.0.1|http')


if __name__ == '__main__':
    unittest.main()
