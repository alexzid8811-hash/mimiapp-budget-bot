from .db import connect
from .money import cents, amount


def validate_piggy_history(rows):
    """Validate every running balance in date/id order, not just the final sum."""
    balance = 0
    for row in sorted(rows, key=lambda r: (r['movement_date'], r['id'])):
        balance += cents(row['amount']) * (1 if row['direction'] == 'deposit' else -1)
        if balance < 0:
            raise ValueError('В копилке недостаточно денег на дату операции или последующего снятия')


def check_piggy_history(con, uid):
    validate_piggy_history([dict(r) for r in con.execute(
        'SELECT id,direction,amount,movement_date FROM piggy_bank_movements WHERE user_id=?', (uid,))])
