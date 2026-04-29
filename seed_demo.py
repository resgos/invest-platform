"""Заливает демо-данные для просмотра дизайна (горящие сделки, обычные, бессрочные)."""
import sys, os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app
from models import db, User, Deal


DEMO = [
    dict(
        title='Квартира 2-к в ЖК «Ривер-Парк», Москва',
        description='Залоговый объект: 78 м² на 14/25 этаже, ремонт от застройщика, развитый район. Документы проверены, обременений нет.',
        category='realestate', subcategory='Двухкомнатная',
        price=18_500_000, expected_profit_pct=18,
        date_start=date.today() + timedelta(days=2),  # 🔥 ГОРЯЩАЯ
        date_end=date.today() + timedelta(days=180),
        investment_term_months=6,
        min_investment=500_000, total_pool=15_000_000,
        risk_level='low', visibility='all',
        property_type='Квартира', area=78, rooms=2, location='Москва, ЦАО', floor=14, total_floors=25,
    ),
    dict(
        title='BMW X5 xDrive40i 2024, премиум-пакет',
        description='Залог по обеспечению кредита. Один владелец, обслуживание у официала, без ДТП. Полный пакет документов.',
        category='auto',
        price=8_900_000, expected_profit_pct=22,
        date_start=date.today() + timedelta(days=5),  # 🔥 ГОРЯЩАЯ
        investment_term_months=4,
        min_investment=200_000, total_pool=7_500_000,
        risk_level='medium', visibility='all',
        car_brand='BMW', car_model='X5 xDrive40i', car_year=2024, car_power=340, car_mileage=12000,
        car_transmission='auto', car_fuel='petrol',
    ),
    dict(
        title='Складской комплекс в Подольске, 1200 м²',
        description='Действующий бизнес-актив с арендатором (логистическая компания). Высокая ликвидность, стабильный денежный поток.',
        category='business',
        price=45_000_000, expected_profit_pct=15,
        date_start=date.today() + timedelta(days=14),  # обычная — НЕ горящая (>7 дней)
        date_end=date.today() + timedelta(days=400),
        investment_term_months=12,
        min_investment=1_000_000, total_pool=40_000_000,
        risk_level='medium', visibility='all',
    ),
    dict(
        title='Mercedes-Benz S-Class W223 — срочная продажа',
        description='Срочная реализация залогового авто. Состояние идеальное, 2023 год.',
        category='auto', deal_type='urgent_sale',
        price=11_500_000, expected_profit_pct=0,
        date_end=date.today() + timedelta(days=14),
        min_investment=11_500_000, total_pool=11_500_000,
        risk_level='low', visibility='all',
        car_brand='Mercedes-Benz', car_model='S-Class W223', car_year=2023,
    ),
    dict(
        title='Производственное оборудование (ЧПУ-станки)',
        description='Партия из 3 станков с ЧПУ DMG MORI. Бессрочное предложение — условия и срок согласовываются индивидуально.',
        category='equipment',
        price=22_000_000, expected_profit_pct=20,
        # бессрочная: ни старта, ни окончания, ни срока
        min_investment=500_000, total_pool=20_000_000,
        risk_level='high', visibility='all',
    ),
    dict(
        title='Земельный участок ИЖС, Новая Рига',
        description='Уже стартовавшая сделка, идёт сбор средств. 25 соток, ровный, коммуникации по границе.',
        category='realestate',
        price=14_000_000, expected_profit_pct=14,
        date_start=date.today() - timedelta(days=10),
        date_end=date.today() + timedelta(days=300),
        investment_term_months=10,
        min_investment=300_000, total_pool=12_000_000,
        risk_level='low', visibility='all',
        property_type='Земельный участок', area=2500, location='МО, Новая Рига',
    ),
]


def seed():
    with app.app_context():
        db.create_all()
        admin = User.query.filter_by(role='admin').first()
        if not admin:
            print('Админ не найден, запустите app.py хотя бы один раз')
            return
        if Deal.query.count() >= 5:
            print(f'В БД уже {Deal.query.count()} сделок. Пропускаем seed.')
            return

        for d in DEMO:
            deal = Deal(created_by=admin.id, status='active',
                        contact_info='Алексей Титов · +7 (495) 555-77-99 · alexey@gruppa-titan.ru',
                        **d)
            db.session.add(deal)
        db.session.commit()
        print(f'Добавлено сделок: {Deal.query.count()}')


if __name__ == '__main__':
    seed()
