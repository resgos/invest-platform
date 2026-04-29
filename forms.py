from flask_wtf import FlaskForm
from wtforms import (StringField, PasswordField, TextAreaField, FloatField,
                     IntegerField, SelectField, SelectMultipleField, BooleanField,
                     MultipleFileField, HiddenField, DateField)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional, ValidationError
import re
from datetime import date


class LoginForm(FlaskForm):
    username = StringField('Логин', validators=[
        DataRequired(message='Введите логин'),
        Length(min=3, max=80, message='От 3 до 80 символов')
    ])
    password = PasswordField('Пароль', validators=[
        DataRequired(message='Введите пароль')
    ])


class CreateUserForm(FlaskForm):
    username = StringField('Логин', validators=[
        DataRequired(message='Введите логин'),
        Length(min=3, max=80, message='От 3 до 80 символов')
    ])
    email = StringField('Email', validators=[
        DataRequired(message='Введите email'),
        Email(message='Некорректный email')
    ])
    full_name = StringField('ФИО', validators=[
        DataRequired(message='Введите ФИО'),
        Length(max=150)
    ])
    phone = StringField('Телефон', validators=[Optional(), Length(max=30)])
    password = PasswordField('Пароль', validators=[
        DataRequired(message='Введите пароль'),
        Length(min=8, message='Минимум 8 символов')
    ])
    role = SelectField('Роль', choices=[
        ('investor', 'Инвестор'),
        ('admin', 'Администратор')
    ], validators=[DataRequired()])

    def validate_username(self, field):
        if not re.match(r'^[a-zA-Z0-9_]+$', field.data):
            raise ValidationError('Только латинские буквы, цифры и _')

    def validate_password(self, field):
        pw = field.data
        if not re.search(r'[A-Z]', pw):
            raise ValidationError('Нужна хотя бы одна заглавная буква')
        if not re.search(r'[a-z]', pw):
            raise ValidationError('Нужна хотя бы одна строчная буква')
        if not re.search(r'[0-9]', pw):
            raise ValidationError('Нужна хотя бы одна цифра')


class EditUserForm(FlaskForm):
    full_name = StringField('ФИО', validators=[
        DataRequired(message='Введите ФИО'),
        Length(max=150)
    ])
    email = StringField('Email', validators=[
        DataRequired(message='Введите email'),
        Email(message='Некорректный email')
    ])
    phone = StringField('Телефон', validators=[Optional(), Length(max=30)])
    is_active = BooleanField('Активен')
    new_password = PasswordField('Новый пароль (оставьте пустым для сохранения)', validators=[
        Optional(),
        Length(min=8, message='Минимум 8 символов')
    ])


class DealForm(FlaskForm):
    """
    Форма создания/редактирования предложения (Deal).

    Типы:
      - investment: обычная инвестиция с доходностью, сроком, пулом
      - urgent_sale: срочная продажа — только цена, описание, категория

    Срок задаётся двумя опциональными полями:
      - date_end: конкретная дата окончания приёма (все инвестиции до этой даты)
      - investment_term_months: срок в месяцах (каждая инвестиция живёт N мес.)
    Можно указать оба, одно или ни одного (бессрочно).
    """
    deal_type = SelectField('Тип сделки', choices=[
        ('investment', 'Инвестиция'),
        ('urgent_sale', 'Срочная продажа')
    ], default='investment', validators=[DataRequired()])

    title = StringField('Название', validators=[
        DataRequired(message='Введите название'),
        Length(max=200)
    ])
    description = TextAreaField('Описание', validators=[
        DataRequired(message='Введите описание')
    ])
    category = SelectField('Категория', choices=[
        ('realestate', 'Недвижимость'),
        ('auto', 'Автомобили'),
        ('business', 'Бизнес'),
        ('equipment', 'Оборудование'),
        ('other', 'Другое')
    ], validators=[DataRequired()])
    subcategory = StringField('Подкатегория', validators=[Optional(), Length(max=100)])

    price = FloatField('Стоимость актива (руб.)', validators=[
        DataRequired(message='Введите стоимость'),
        NumberRange(min=0, message='Должна быть >= 0')
    ])
    expected_profit_pct = FloatField('Ожидаемая доходность (%)', validators=[
        Optional(),
        NumberRange(min=0, max=1000)
    ])
    date_start = DateField('Дата старта сделки', format='%Y-%m-%d', validators=[Optional()])
    date_end = DateField('Дата окончания приёма', format='%Y-%m-%d', validators=[Optional()])
    investment_term_months = IntegerField('Срок инвестиции (мес.)', validators=[
        Optional(), NumberRange(min=1, max=600, message='От 1 до 600 месяцев')
    ])
    investment_term_days = IntegerField('Срок инвестиции (дн.)', validators=[
        Optional(), NumberRange(min=1, max=18000, message='От 1 до 18000 дней')
    ])

    def validate_date_end(self, field):
        if field.data and self.date_start.data and field.data < self.date_start.data:
            raise ValidationError('Дата окончания не может быть раньше даты старта')
    min_investment = FloatField('Мин. инвестиция (руб.)', validators=[
        Optional(),
        NumberRange(min=0)
    ])
    risk_level = SelectField('Уровень риска', choices=[
        ('low', 'Низкий'),
        ('medium', 'Средний'),
        ('high', 'Высокий')
    ])
    total_pool = FloatField('Общий пул (руб.)', validators=[
        Optional(),
        NumberRange(min=0)
    ])
    contact_info = TextAreaField('Контактная информация', validators=[Optional()])
    visibility = SelectField('Видимость', choices=[
        ('selected', 'Только выбранным'),
        ('all', 'Всем инвесторам')
    ])

    # Real estate
    property_type = StringField('Тип объекта', validators=[Optional()])
    area = StringField('Площадь', validators=[Optional()])
    rooms = StringField('Комнат', validators=[Optional()])
    location = StringField('Местоположение', validators=[Optional()])
    floor = StringField('Этаж', validators=[Optional()])
    total_floors = StringField('Всего этажей', validators=[Optional()])

    # Auto
    car_brand = StringField('Марка', validators=[Optional()])
    car_model = StringField('Модель', validators=[Optional()])
    car_year = StringField('Год', validators=[Optional()])
    car_power = StringField('Мощность (л.с.)', validators=[Optional()])
    car_mileage = StringField('Пробег (км)', validators=[Optional()])
    car_transmission = SelectField('КПП', choices=[
        ('', '—'), ('auto', 'Автомат'), ('manual', 'Механика')
    ], validators=[Optional()])
    car_fuel = SelectField('Топливо', choices=[
        ('', '—'), ('petrol', 'Бензин'), ('diesel', 'Дизель'),
        ('electric', 'Электро'), ('hybrid', 'Гибрид')
    ], validators=[Optional()])


class ExistingDealForm(FlaskForm):
    """Form for creating an already-existing investment (admin records past deal)."""
    deal_id = SelectField('Объект сделки', coerce=int, validators=[DataRequired()])
    user_id = SelectField('Инвестор', coerce=int, validators=[DataRequired()])
    amount = FloatField('Сумма инвестиции (руб.)', validators=[
        DataRequired(),
        NumberRange(min=0)
    ])
    expected_profit = FloatField('Ожидаемая прибыль (руб.)', validators=[
        DataRequired(),
        NumberRange(min=0)
    ])
    actual_profit = FloatField('Полученная прибыль (руб.)', validators=[
        Optional(),
        NumberRange(min=0)
    ])
    status = SelectField('Статус', choices=[
        ('active', 'Активная'),
        ('closed', 'Закрытая'),
        ('pending', 'Ожидание')
    ])
    # Admin can specify dates for past investments
    inv_date_start = DateField('Дата начала инвестиции', format='%Y-%m-%d', validators=[Optional()])
    inv_date_end = DateField('Дата окончания инвестиции', format='%Y-%m-%d', validators=[Optional()])
    notes = TextAreaField('Комментарий', validators=[Optional()])


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Текущий пароль', validators=[
        DataRequired(message='Введите текущий пароль')
    ])
    new_password = PasswordField('Новый пароль', validators=[
        DataRequired(message='Введите новый пароль'),
        Length(min=8, message='Минимум 8 символов')
    ])

    def validate_new_password(self, field):
        pw = field.data
        if not re.search(r'[A-Z]', pw):
            raise ValidationError('Нужна хотя бы одна заглавная буква')
        if not re.search(r'[a-z]', pw):
            raise ValidationError('Нужна хотя бы одна строчная буква')
        if not re.search(r'[0-9]', pw):
            raise ValidationError('Нужна хотя бы одна цифра')
