import io
from datetime import date

from openpyxl import load_workbook

from app import clock
from app.db import connect, ensure_user, init_db
from app.excel_export import build_workbook


def test_excel_export_contains_operations_pivot_and_piggy(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 15))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1, "Alex")
    with connect() as con:
        category_id = con.execute(
            "INSERT INTO categories(user_id,title,emoji) VALUES(1,'Тестовая кофейня','☕')"
        ).lastrowid
        con.executemany(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note) VALUES(1,?,?,?,?,?)",
            [
                ("expense", 300, "2026-08-10", category_id, "кофе"),
                ("expense", 700, "2026-09-02", category_id, ""),
                ("income", 5000, "2026-09-03", None, "подарок"),
            ],
        )
        con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) VALUES(1,'deposit',1000,'2026-09-01','')"
        )
        con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) VALUES(1,'withdraw',400,'2026-09-05','')"
        )

    wb = load_workbook(io.BytesIO(build_workbook(1)))
    assert {"Операции", "Расходы по месяцам", "Копилка", "Обязательные платежи", "Доходы"} <= set(wb.sheetnames)

    ops = list(wb["Операции"].iter_rows(min_row=2, values_only=True))
    assert [(r[1], r[2], r[3]) for r in ops] == [
        ("Расход", "Тестовая кофейня", -300), ("Расход", "Тестовая кофейня", -700), ("Доход", "Дневной бюджет", 5000),
    ]
    assert ops[0][0].isoformat()[:10] == "2026-08-10"

    pivot = list(wb["Расходы по месяцам"].iter_rows(values_only=True))
    assert pivot[0] == ("Категория", "08.2026", "09.2026", "Всего")
    assert pivot[1] == ("Тестовая кофейня", 300, 700, 1000)
    assert pivot[-1] == ("Итого", 300, 700, 1000)

    piggy = list(wb["Копилка"].iter_rows(min_row=2, values_only=True))
    assert [(r[1], r[2], r[3]) for r in piggy] == [("Пополнение", 1000, 1000), ("Снятие", -400, 600)]

    assert wb.sheetnames[1] == "Графики"
    charts = wb["Графики"]
    assert charts["A1"].value == "Аналитика расходов · Сентябрь 2026"
    values = [row for row in charts.iter_rows(values_only=True) if any(v is not None for v in row)]
    kinds = {r[0]: r[1] for r in values}
    assert (kinds["Повседневные траты"], kinds["Обязательные платежи"], kinds["Отложено в копилку"]) == (700, 0, 1000)
    assert any(r[0] == "☕ Тестовая кофейня" and r[1] == 700 and r[3] == 300 for r in values)
    assert any(r[0] == "сен 26" and r[1] == 700 and r[3] == 1000 for r in values)


def test_excel_charts_are_embedded(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 15))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1, "Alex")
    with connect() as con:
        con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note) "
            "SELECT 1,'expense',500,'2026-09-10',id,'' FROM categories WHERE user_id=1 LIMIT 1"
        )
    import zipfile

    content = build_workbook(1)
    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    # Pie, categories, days and months on "Графики" plus the pivot chart.
    assert len([n for n in names if n.startswith("xl/charts/chart")]) == 5
    # Titles and legends stay outside the plot area and the pie shows only
    # percentages: Excel draws every omitted flag, which made labels overlap.
    archive = zipfile.ZipFile(io.BytesIO(content))
    for name in [n for n in names if n.startswith("xl/charts/chart")]:
        xml = archive.read(name).decode()
        assert xml.count('<overlay val="1"') == 0, name
        if "<title>" in xml:
            assert '<overlay val="0"' in xml, name
    pie = next(archive.read(n).decode() for n in names if n.startswith("xl/charts/") and b"pieChart" in archive.read(n))
    for flag in ("showVal", "showCatName", "showSerName", "showLegendKey"):
        assert f'<{flag} val="0"' in pie, flag
