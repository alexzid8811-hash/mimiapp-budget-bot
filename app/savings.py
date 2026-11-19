from .money import cents
from .vacation_funds import protected_cents


def validate_piggy_history(rows):
    """Validate every running balance in date/id order, not just the final sum."""
    balance = 0
    for row in sorted(rows, key=lambda r: (r['movement_date'], r['id'])):
        balance += cents(row['amount']) * (1 if row['direction'] == 'deposit' else -1)
        if balance < 0:
            raise ValueError('В копилке недостаточно денег на дату операции или последующего снятия')


def check_piggy_history(con, uid, protected=0):
    validate_piggy_history([dict(r) for r in con.execute(
        'SELECT id,direction,amount,movement_date FROM piggy_bank_movements WHERE user_id=?', (uid,))])
    if protected and protected_cents(con, uid) < protected:
        raise ValueError('Эти деньги в копилке зарезервированы на отпуск')
