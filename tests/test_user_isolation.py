import io
import json

from openpyxl import load_workbook

from app.backup import export_user_data, restore_user_data
from app.db import connect, ensure_user, init_db
from app.excel_export import build_workbook

SECRET_A = "СЕКРЕТ-ПОЛЬЗОВАТЕЛЯ-А"
SECRET_B = "СЕКРЕТ-ПОЛЬЗОВАТЕЛЯ-Б"


def _seed(user_id: int, secret: str) -> None:
    ensure_user(user_id, f"user{user_id}")
    with connect() as con:
        category_id = con.execute(
            "INSERT INTO categories(user_id,title,emoji) VALUES(?,?,'🧰')", (user_id, f"Кат {secret}")
        ).lastrowid
        con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month,category_id) VALUES(?,?,500,10,?)",
            (user_id, f"Платёж {secret}", category_id),
        )
        con.execute(
            "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind) VALUES(?,?,900,5,'other')",
            (user_id, f"Доход {secret}"),
        )
        con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note) "
            "VALUES(?,'expense',100,'2026-01-05',?,?)",
            (user_id, category_id, f"Заметка {secret}"),
        )
        con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) "
            "VALUES(?,'deposit',50,'2026-01-06',?)",
            (user_id, f"Копилка {secret}"),
        )
        con.execute(
            "INSERT INTO vacations(user_id,start_date,end_date,amount,payment_date,note) "
            "VALUES(?,'2026-02-01','2026-02-10',1000,'2026-01-28',?)",
            (user_id, f"Отпуск {secret}"),
        )


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    init_db()
    _seed(1, SECRET_A)
    _seed(2, SECRET_B)


def test_backup_contains_only_own_data(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    dump_a = json.dumps(export_user_data(1), ensure_ascii=False)
    dump_b = json.dumps(export_user_data(2), ensure_ascii=False)
    assert SECRET_A in dump_a and SECRET_B not in dump_a
    assert SECRET_B in dump_b and SECRET_A not in dump_b


def test_excel_contains_only_own_data(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    for user_id, own, foreign in ((1, SECRET_A, SECRET_B), (2, SECRET_B, SECRET_A)):
        wb = load_workbook(io.BytesIO(build_workbook(user_id)))
        text = " ".join(
            str(cell.value) for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value
        )
        assert own in text
        assert foreign not in text


def test_restore_does_not_touch_other_users(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    before_b = json.dumps(export_user_data(2)["data"], ensure_ascii=False, sort_keys=True)
    # Restoring user 1's backup (even into user 1) must leave user 2 untouched,
    # and a backup made by user 2 restored by user 1 must not alter user 2.
    restore_user_data(1, export_user_data(1))
    restore_user_data(1, export_user_data(2))
    after_b = json.dumps(export_user_data(2)["data"], ensure_ascii=False, sort_keys=True)
    assert before_b == after_b
