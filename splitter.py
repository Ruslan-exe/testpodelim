"""
Расчёт долгов участников. Это самая критичная логика продукта —
именно здесь обеспечивается требование "общая сумма всегда должна сходиться".

Правила:
  1. Позиция, которую отметили конкретные люди, делится между ними
     (пропорционально весам; по умолчанию поровну).
  2. Позиция, которую НЕ отметил никто, делится поровну между ВСЕМИ
     участниками счёта — чтобы никто не мог "забыть" отметить дорогое
     блюдо и уйти от оплаты (антифрод-правило).
  3. Налог и сервисный сбор раскладываются пропорционально доле каждого.
  4. Метод наибольшего остатка (largest remainder) добивает копейки так,
     чтобы сумма долей ВСЕГДА равнялась итогу чека.

Метод: Decimal-арифметика — никаких float на последнем шаге.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def compute_split(
    bill,
    items_with_shares: list,
    tax: float,
    service_fee: float,
    total: float,
    participant_ids: list | None = None,
) -> dict:
    """
    items_with_shares: [(item_total_price, {user_id: weight, ...}), ...]
    participant_ids: все участники счёта — между ними делятся позиции,
        которые никто явно не отметил.

    Возвращает:
      per_user        — {user_id: Decimal} — сумма всех значений ТОЧНО равна total
      unclaimed_amount — сумма позиций, которые никто не отметил
                         (они уже поделены на всех и вошли в per_user)
      fully_claimed   — True, если каждую позицию кто-то отметил явно
    """
    participant_ids = list(participant_ids or [])
    raw_per_user = defaultdict(Decimal)  # доля без учёта налога/сервиса
    unclaimed_sum = Decimal("0")

    for item_total, shares in items_with_shares:
        item_total_d = _to_decimal(item_total)
        weight_sum = _to_decimal(sum(shares.values())) if shares else Decimal("0")

        if weight_sum > 0:
            # Позицию отметили конкретные люди — делим между ними по весам
            for user_id, weight in shares.items():
                raw_per_user[user_id] += item_total_d * _to_decimal(weight) / weight_sum
        elif participant_ids:
            # Никто не отметил — делим поровну на всех участников счёта
            unclaimed_sum += item_total_d
            n = _to_decimal(len(participant_ids))
            for user_id in participant_ids:
                raw_per_user[user_id] += item_total_d / n

    subtotal_assigned = sum(raw_per_user.values()) if raw_per_user else Decimal("0")

    extra_d = _to_decimal(tax or 0) + _to_decimal(service_fee or 0)
    total_d = _to_decimal(total)

    # Налог/сервис — пропорционально доле каждого
    final_per_user = {}
    if subtotal_assigned > 0:
        for user_id, amount in raw_per_user.items():
            final_per_user[user_id] = amount + (amount / subtotal_assigned) * extra_d

    rounded = {
        uid: amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for uid, amt in final_per_user.items()
    }

    # Метод наибольшего остатка: добиваем разницу в копейках, чтобы
    # сумма долей точно совпала с итогом чека.
    if rounded:
        distributed_sum = sum(rounded.values())
        diff = (total_d - distributed_sum).quantize(Decimal("0.01"))
        # Страховка от кривых данных OCR (total сильно меньше/больше суммы
        # позиций) — корректируем только "копеечные" расхождения.
        if diff != 0 and abs(diff) <= Decimal("1.00"):
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
        "unclaimed_amount": unclaimed_sum,
        "fully_claimed": unclaimed_sum == 0,
    }
