"""
Расчёт долгов участников. Это самая критичная логика продукта —
именно здесь обеспечивается требование "общая сумма всегда должна сходиться".

Метод: Decimal-арифметика (никаких float на последнем шаге) + метод
наибольшего остатка (largest remainder method) для распределения копеек/тийинов,
которые неизбежно возникают при делении, — чтобы сумма долей участников
ВСЕГДА совпадала с total чека день в день, без "пропавших" 0.01.
"""
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def compute_split(bill, items_with_shares: list, tax: float, service_fee: float, total: float) -> dict:
    """
    items_with_shares: список [(item_total_price, {user_id: weight, ...}), ...]
        — для каждой позиции чека передаём её total_price и словарь
        "кто участвует в этой позиции и с каким весом" (по умолчанию вес 1 у всех,
        кто отметил себя на этой позиции — то есть блюдо на компанию делится поровну
        между отметившимися, а не автоматически на всех участников счёта).

    Возвращает {user_id: Decimal(сумма к оплате)}, где сумма всех значений
    ТОЧНО равна total (с точностью до минимальной денежной единицы).
    """
    raw_per_user = defaultdict(Decimal)  # доля без учёта налога/сервиса

    for item_total, shares in items_with_shares:
        if not shares:
            continue  # позицию ещё никто не забрал — не учитываем, пока не разберут
        item_total_d = _to_decimal(item_total)
        weight_sum = _to_decimal(sum(shares.values()))
        if weight_sum == 0:
            continue
        for user_id, weight in shares.items():
            raw_per_user[user_id] += item_total_d * _to_decimal(weight) / weight_sum

    subtotal_claimed = sum(raw_per_user.values()) if raw_per_user else Decimal("0")

    tax_d = _to_decimal(tax or 0)
    service_d = _to_decimal(service_fee or 0)
    extra_d = tax_d + service_d
    total_d = _to_decimal(total)

    final_per_user = {}
    if subtotal_claimed > 0:
        for user_id, amount in raw_per_user.items():
            ratio = amount / subtotal_claimed
            final_per_user[user_id] = amount + ratio * extra_d
    else:
        final_per_user = dict(raw_per_user)

    # Округление до 2 знаков (для UZS фактически центов не бывает, но оставляем
    # универсально — на фронте можно округлить до целого тийина/сума отдельно)
    rounded = {uid: amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for uid, amt in final_per_user.items()}

    # Метод наибольшего остатка: если позиции разобраны ПОЛНОСТЬЮ (сумма распределённого
    # покрывает total), досчитываем разницу в копейках до последней монеты.
    distributed_sum = sum(rounded.values())
    fully_claimed = subtotal_claimed >= (total_d - extra_d) - Decimal("0.01") if total_d else False

    if fully_claimed and rounded:
        diff = (total_d - distributed_sum).quantize(Decimal("0.01"))
        if diff != 0:
            # остатки на основе дробной части необработанной суммы — крупным долям достаётся приоритет
            remainders = sorted(
                final_per_user.items(),
                key=lambda kv: (kv[1] - kv[1].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
                reverse=(diff > 0),
            )
            step = Decimal("0.01") if diff > 0 else Decimal("-0.01")
            n_steps = int(abs(diff) / Decimal("0.01"))
            for i in range(n_steps):
                uid = remainders[i % len(remainders)][0]
                rounded[uid] += step

    return {
        "per_user": rounded,
        "unclaimed_amount": (total_d - sum(rounded.values())) if not fully_claimed else Decimal("0.00"),
        "fully_claimed": fully_claimed,
    }
