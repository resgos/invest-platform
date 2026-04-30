"""
Тесты safe_float — парсинг суммы из пользовательского ввода.
Поддерживает русский (12 345,50) и английский (12,345.50) форматы.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def safe_float(val, default=0):
    """Скопировано из app.py — тестируем чистую функцию без app context."""
    try:
        s = str(val).strip().replace(' ', '').replace('\xa0', '')
        if '.' in s:
            s = s.replace(',', '')
        elif s.count(',') == 1:
            s = s.replace(',', '.')
        else:
            s = s.replace(',', '')
        return float(s)
    except (ValueError, TypeError):
        return default


class SafeFloatTests(unittest.TestCase):

    def test_plain_int(self):
        self.assertEqual(safe_float('12345'), 12345.0)

    def test_plain_float(self):
        self.assertEqual(safe_float('12345.50'), 12345.5)

    def test_russian_decimal_comma(self):
        # Главная русская проблема: 12345,50 не должно превращаться в 1234550
        self.assertEqual(safe_float('12345,50'), 12345.5)

    def test_with_spaces(self):
        self.assertEqual(safe_float('100 000'), 100000.0)
        self.assertEqual(safe_float('1 000 000'), 1000000.0)

    def test_with_nbsp(self):
        # Неразрывный пробел \xa0 — типичный copy-paste из браузера
        self.assertEqual(safe_float('100\xa0000'), 100000.0)

    def test_russian_full(self):
        # 100 000,50 — сто тысяч рублей пятьдесят копеек
        self.assertEqual(safe_float('100 000,50'), 100000.5)
        self.assertEqual(safe_float('1 234 567,89'), 1234567.89)

    def test_english_with_thousands(self):
        # Англ. формат: 1,234.56 — запятые thousand-separator, точка десятичная
        self.assertEqual(safe_float('1,234.56'), 1234.56)
        self.assertEqual(safe_float('1,000,000.50'), 1000000.5)

    def test_invalid_returns_default(self):
        self.assertEqual(safe_float('abc'), 0)
        self.assertEqual(safe_float('abc', default=-1), -1)
        self.assertEqual(safe_float('', default=42), 42)
        self.assertEqual(safe_float(None, default=99), 99)

    def test_zero(self):
        self.assertEqual(safe_float('0'), 0.0)
        self.assertEqual(safe_float('0,00'), 0.0)

    def test_negative(self):
        self.assertEqual(safe_float('-100'), -100.0)
        self.assertEqual(safe_float('-100,50'), -100.5)

    def test_already_float(self):
        self.assertEqual(safe_float(12345.5), 12345.5)
        self.assertEqual(safe_float(0), 0.0)

    def test_leading_trailing_whitespace(self):
        self.assertEqual(safe_float('  100  '), 100.0)
        self.assertEqual(safe_float('\t12345,50\n'), 12345.5)


if __name__ == '__main__':
    unittest.main()
