from datetime import date

from app import planning
from app.cashflow_app import planned_mandatory_map, reported_mandatory_map


def test_payday_bill_moves_to_following_daily_budget(monkeypatch):
    advance_day = date(2026, 9, 22)
    daily_period_start = date(2026, 9, 23)

    def boundaries(_uid, start, end):
        return [
            {"date": day, "kind": "выплата"}
            for day in (date(2026, 9, 8), daily_period_start)
            if start <= day <= end
        ]

    def bills(_uid, start, end):
        rows = [
            {"due_date": advance_day.isoformat(), "amount": 700, "paid": False},
            {"due_date": date(2026, 9, 25).isoformat(), "amount": 300, "paid": False},
        ]
        return [
            row for row in rows
            if start <= date.fromisoformat(row["due_date"]) <= end
        ]

    monkeypatch.setattr(planning, "payday_boundaries", boundaries)
    monkeypatch.setattr(planning, "bill_events", bills)

    old_period = planning.budget_period_mandatory_map(
        1, date(2026, 9, 8), advance_day
    )
    new_period = planning.budget_period_mandatory_map(
        1, daily_period_start, date(2026, 10, 6)
    )
    closing_history = planned_mandatory_map(
        1, date(2026, 9, 8), advance_day
    )
    forecast = planned_mandatory_map(
        1, daily_period_start, date(2026, 10, 6)
    )

    assert advance_day not in old_period
    assert advance_day not in closing_history
    assert new_period[advance_day] == 700
    assert forecast[daily_period_start] == 700
    assert forecast[date(2026, 9, 25)] == 300


def test_paid_payday_bill_is_reported_but_not_charged_again(monkeypatch):
    payday = date(2026, 9, 22)
    period_start = payday.replace(day=23)

    monkeypatch.setattr(
        planning,
        "payday_boundaries",
        lambda _uid, start, end: (
            [{"date": period_start, "kind": "аванс"}]
            if start <= period_start <= end
            else []
        ),
    )
    monkeypatch.setattr(
        planning,
        "bill_events",
        lambda _uid, _start, _end: [
            {"due_date": payday.isoformat(), "amount": 25000, "paid": True}
        ],
    )

    assert planned_mandatory_map(1, period_start, date(2026, 10, 6)) == {}
    assert reported_mandatory_map(1, period_start, date(2026, 10, 6)) == {
        period_start: 25000
    }
