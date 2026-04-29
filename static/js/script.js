/**
 * Группа Титан — клиентский скрипт.
 * Все обработчики делегированы (CSP-friendly), inline onclick не используется.
 */
document.addEventListener('DOMContentLoaded', function () {

    // ── Галерея в детали сделки: клик по миниатюре меняет основное фото ──
    const mainImg = document.getElementById('mainImg');
    if (mainImg) {
        document.querySelectorAll('.detail-thumb').forEach(function (thumb) {
            thumb.addEventListener('click', function () {
                mainImg.src = this.src;
                document.querySelectorAll('.detail-thumb').forEach(function (t) {
                    t.classList.remove('active');
                });
                this.classList.add('active');
            });
            // Доступность с клавиатуры
            thumb.setAttribute('tabindex', '0');
            thumb.setAttribute('role', 'button');
            thumb.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.click();
                }
            });
        });
    }

    // ── Кнопки и формы с подтверждением (data-confirm="...") ──
    document.querySelectorAll('button[data-confirm], a[data-confirm]').forEach(function (el) {
        el.addEventListener('click', function (e) {
            const msg = this.getAttribute('data-confirm');
            if (msg && !window.confirm(msg)) {
                e.preventDefault();
                e.stopPropagation();
            }
        });
    });
    document.querySelectorAll('form[data-confirm]').forEach(function (form) {
        form.addEventListener('submit', function (e) {
            const msg = this.getAttribute('data-confirm');
            if (msg && !window.confirm(msg)) {
                e.preventDefault();
                e.stopPropagation();
            }
        });
    });

    // ── Кнопка инвестиции / покупки — стандартное подтверждение ──
    document.querySelectorAll('.btn-invest').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
            const msg = btn.classList.contains('btn-buy')
                ? 'Подтверждаете покупку?'
                : 'Подтверждаете инвестицию?';
            if (!window.confirm(msg)) {
                e.preventDefault();
                e.stopPropagation();
            }
        });
    });

    // ── Кликабельная карточка-объявление целиком ──
    function navigateCardClick(e) {
        // Игнорируем клики по интерактивным элементам (ссылки, кнопки, формы)
        if (e.target.closest('a, button, input, label, select, textarea')) return;
        const href = this.getAttribute('data-card-href');
        if (!href) return;
        // Ctrl/Cmd-клик и средняя кнопка — открыть в новой вкладке
        if (e.ctrlKey || e.metaKey || e.button === 1) {
            window.open(href, '_blank', 'noopener');
        } else {
            window.location.href = href;
        }
    }
    document.querySelectorAll('.card-clickable[data-card-href]').forEach(function (card) {
        card.addEventListener('click', navigateCardClick);
        card.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                window.location.href = this.getAttribute('data-card-href');
            }
        });
    });

    // ── Авто-скрытие flash-плашек через 5 секунд (только те, что с кнопкой закрытия) ──
    document.querySelectorAll('.alert.alert-dismissible').forEach(function (alert) {
        setTimeout(function () {
            if (window.bootstrap && bootstrap.Alert) {
                bootstrap.Alert.getOrCreateInstance(alert).close();
            }
        }, 5000);
    });

    // ── Калькулятор инвестиции (ad_detail.html) ──
    // Поддерживает редактируемый «свой» срок (#calcCustomDays) с приоритетом
    // над сроком сделки. Если он пуст — берётся срок из data-атрибутов.
    const calcAmount = document.getElementById('calcAmount');
    if (calcAmount) {
        const cfg = calcAmount.dataset;
        const pct = parseFloat(cfg.profitPct) || 0;
        const dealDateEnd = cfg.dateEnd || null;
        const dealTermMonths = cfg.termMonths ? parseInt(cfg.termMonths, 10) : null;
        const dealTermDays = cfg.termDays ? parseInt(cfg.termDays, 10) : null;
        const dealStart = cfg.dateStart || null;
        const customDaysEl = document.getElementById('calcCustomDays');

        const fmtRub = (n) => n.toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' ₽';

        // Человекочитаемый срок: дни / месяцы с округлением до 0.5
        function humanTerm(days) {
            if (days < 30) {
                return days + ' ' + pluralRu(days, ['день', 'дня', 'дней']);
            }
            const months = days / 30;
            // Округляем до 0.5
            const rounded = Math.round(months * 2) / 2;
            const monthsStr = (rounded % 1 === 0) ? rounded.toFixed(0) : rounded.toFixed(1).replace('.', ',');
            return '~' + monthsStr + ' ' + pluralRu(Math.round(rounded), ['месяц', 'месяца', 'месяцев']);
        }
        function pluralRu(n, forms) {
            const abs = Math.abs(n) % 100;
            const n1 = abs % 10;
            if (abs > 10 && abs < 20) return forms[2];
            if (n1 > 1 && n1 < 5) return forms[1];
            if (n1 === 1) return forms[0];
            return forms[2];
        }

        function calcTerm() {
            const today = new Date(); today.setHours(0, 0, 0, 0);
            const baseDate = (dealStart && new Date(dealStart + 'T00:00:00') > today)
                ? new Date(dealStart + 'T00:00:00')
                : today;

            // Свой срок имеет приоритет
            const customDays = customDaysEl ? parseInt(customDaysEl.value, 10) : NaN;
            if (customDays && customDays > 0) {
                const endDate = new Date(baseDate); endDate.setDate(endDate.getDate() + customDays);
                const months = Math.max(customDays / 30, 1 / 30);
                return {
                    months, days: customDays, openEnded: false, custom: true,
                    termText: baseDate.toLocaleDateString('ru-RU') + ' + ' + customDays + ' дн. (' + humanTerm(customDays) + ')'
                };
            }

            const candidates = [];
            if (dealDateEnd) candidates.push(new Date(dealDateEnd + 'T00:00:00'));
            if (dealTermMonths) {
                const d = new Date(baseDate); d.setMonth(d.getMonth() + dealTermMonths);
                candidates.push(d);
            }
            if (dealTermDays) {
                const d = new Date(baseDate); d.setDate(d.getDate() + dealTermDays);
                candidates.push(d);
            }
            if (candidates.length === 0) {
                return { months: 0, days: 0, termText: 'Бессрочно — задайте срок справа', openEnded: true };
            }
            const endDate = candidates.reduce((a, b) => a < b ? a : b);
            const diffDays = Math.max(Math.round((endDate - baseDate) / 86400000), 1);
            const months = Math.max(diffDays / 30, 1 / 30);
            const startStr = baseDate.toLocaleDateString('ru-RU');
            const endStr = endDate.toLocaleDateString('ru-RU');
            return {
                months, days: diffDays, openEnded: false, custom: false,
                termText: startStr + ' — ' + endStr + ' (' + diffDays + ' дн. / ' + humanTerm(diffDays) + ')'
            };
        }

        function calc() {
            const a = parseFloat(calcAmount.value) || 0;
            const t = calcTerm();
            const setText = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };

            if (t.openEnded) {
                setText('calcTerm', t.termText);
                setText('calcProfit', '—');
                setText('calcMonthly', '—');
                setText('calcDaily', '—');
                setText('calcTotal', fmtRub(a));
                return;
            }
            const profit = a * pct / 100 * (t.days / 365);
            setText('calcTerm', t.termText);
            setText('calcProfit', fmtRub(profit));
            // Если срок меньше месяца — не показываем месячную экстраполяцию
            if (t.days < 30) {
                setText('calcMonthly', '—');
            } else {
                setText('calcMonthly', fmtRub(t.months > 0 ? profit / t.months : 0));
            }
            setText('calcDaily', fmtRub(t.days > 0 ? profit / t.days : 0));
            setText('calcTotal', fmtRub(a + profit));
        }
        calcAmount.addEventListener('input', calc);
        if (customDaysEl) customDaysEl.addEventListener('input', calc);
        calc();
    }

    // ── Условные блоки формы сделки (create_ad/edit_ad/edit_visibility) ──
    function bindToggle(controlId, fn) {
        const el = document.getElementById(controlId);
        if (!el) return;
        el.addEventListener('change', fn);
        fn();
    }
    bindToggle('categorySelect', function () {
        const v = document.getElementById('categorySelect').value;
        const re = document.getElementById('realestate-fields');
        const auto = document.getElementById('auto-fields');
        if (re) re.style.display = v === 'realestate' ? '' : 'none';
        if (auto) auto.style.display = v === 'auto' ? '' : 'none';
    });
    bindToggle('visibilitySelect', function () {
        const v = document.getElementById('visibilitySelect').value;
        const sel = document.getElementById('investorSelector');
        if (sel) sel.style.display = v === 'selected' ? '' : 'none';
    });
    bindToggle('dealTypeSelect', function () {
        const isUrgent = document.getElementById('dealTypeSelect').value === 'urgent_sale';
        document.querySelectorAll('.investment-only-field').forEach(function (el) {
            el.style.display = isUrgent ? 'none' : '';
        });
        const term = document.getElementById('investment-term-section');
        if (term) term.style.display = isUrgent ? 'none' : '';
    });

    // ── Reject-модалка в админке (admin_pending.html) ──
    const rejectForm = document.getElementById('rejectForm');
    const rejectInfo = document.getElementById('rejectInfo');
    const rejectReason = document.getElementById('rejectReason');
    const rejectModalEl = document.getElementById('rejectModal');
    document.querySelectorAll('[data-reject-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            const action = this.getAttribute('data-reject-action');
            const investor = this.getAttribute('data-investor') || '';
            const deal = this.getAttribute('data-deal') || '';
            const amount = parseFloat(this.getAttribute('data-amount') || '0').toLocaleString('ru-RU');
            if (rejectForm && action) rejectForm.action = action;
            if (rejectInfo) rejectInfo.textContent =
                'Отклонить заявку от ' + investor + ' на ' + amount + ' ₽ в сделку «' + deal + '»?';
            if (rejectReason) rejectReason.value = '';
            if (rejectModalEl && window.bootstrap && bootstrap.Modal) {
                bootstrap.Modal.getOrCreateInstance(rejectModalEl).show();
            }
        });
    });
});
