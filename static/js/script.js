
document.addEventListener('DOMContentLoaded', function() {
    // Calculator
    const calcInput = document.getElementById('calcAmount');
    if (calcInput) {
        const profitPct = parseFloat(calcInput.dataset.profit) || 0;
        const term = parseInt(calcInput.dataset.term) || 12;
        const investInput = document.getElementById('investAmount');

        function calculate() {
            const amount = parseFloat(calcInput.value) || 0;
            const totalProfit = amount * (profitPct / 100);
            const monthlyProfit = term > 0 ? totalProfit / term : 0;
            const totalReturn = amount + totalProfit;

            document.getElementById('calcProfit').textContent = formatCurrency(totalProfit) + ' руб.';
            document.getElementById('calcMonthly').textContent = formatCurrency(monthlyProfit) + ' руб.';
            document.getElementById('calcTotal').textContent = formatCurrency(totalReturn) + ' руб.';
            document.getElementById('calcROI').textContent = profitPct + '%';

            if (investInput) investInput.value = amount;
        }

        calcInput.addEventListener('input', calculate);
        calculate();
    }

    function formatCurrency(num) {
        return Math.round(num).toLocaleString('ru-RU');
    }

    // Auto-dismiss alerts
    document.querySelectorAll('.alert').forEach(function(alert) {
        setTimeout(function() {
            const bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
            bsAlert.close();
        }, 5000);
    });
});
