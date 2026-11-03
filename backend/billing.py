"""Transactional, per-batch budget reservations. Amounts use integer micro-units."""
from contextvars import ContextVar
from decimal import Decimal, ROUND_CEILING
from .core import Waiting, now, uid

inference_budget = ContextVar('inference_budget', default=None)

def micros(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError('费用必须为非负有限数值')
    return int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING))

class BudgetRequired(Waiting): pass

class Ledger:
    def __init__(self, db):
        self.db = db
        with db.conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS production_budgets(owner TEXT,provider TEXT,unit TEXT,limit_micro INTEGER,
                PRIMARY KEY(owner,provider));
            CREATE TABLE IF NOT EXISTS production_charges(id TEXT PRIMARY KEY,owner TEXT,provider TEXT,unit TEXT,
                reserved INTEGER,actual INTEGER,state TEXT,reference TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS model_usage(id TEXT PRIMARY KEY,owner TEXT,provider TEXT,model TEXT,
                input_tokens INTEGER,output_tokens INTEGER,seconds REAL,created TEXT);
            ''')

    def configure(self, owner, provider, unit, limit):
        value = micros(limit)
        with self.db.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT unit FROM production_budgets WHERE owner=? AND provider=?',(owner,provider)).fetchone()
            charges = c.execute('SELECT COUNT(*) FROM production_charges WHERE owner=? AND provider=?',(owner,provider)).fetchone()[0]
            if old and old['unit'] != unit and charges: raise ValueError('已有费用记录，不能更改计费单位')
            c.execute('INSERT OR REPLACE INTO production_budgets VALUES(?,?,?,?)',(owner,provider,unit,value))

    def reserve(self, owner, provider, unit, amount, reference=''):
        value = micros(amount)
        with self.db.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            cfg = c.execute('SELECT * FROM production_budgets WHERE owner=? AND provider=?',(owner,provider)).fetchone()
            if not cfg or cfg['limit_micro'] <= 0: raise BudgetRequired('请填写 '+provider+' 本批预算')
            if cfg['unit'] != unit: raise BudgetRequired(provider+' 计费单位与本批预算不同')
            used = c.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual WHEN state='released' THEN 0 ELSE reserved END),0) FROM production_charges WHERE owner=? AND provider=?",(owner,provider)).fetchone()[0]
            if used + value > cfg['limit_micro']: raise BudgetRequired(provider+' 本批剩余额度不足')
            identifier = uid()
            c.execute('INSERT INTO production_charges VALUES(?,?,?,?,?,?,?,?,?)',(identifier,owner,provider,unit,value,None,'reserved',reference,now()))
            return identifier

    def settle(self, identifier, amount=None, state='settled'):
        if state not in {'settled','released','uncertain'}: raise ValueError('未知费用状态')
        self.db.execute('UPDATE production_charges SET actual=?,state=? WHERE id=?',
                        (micros(amount) if amount is not None else None,state,identifier))

    def summary(self, owner):
        rows = self.db.query('SELECT * FROM production_budgets WHERE owner=?',(owner,))
        for row in rows:
            charges = self.db.query('SELECT * FROM production_charges WHERE owner=? AND provider=?',(owner,row['provider']))
            row['spent'] = sum(r['actual'] or 0 for r in charges if r['state']=='settled')/1000000
            row['held'] = sum(r['reserved'] for r in charges if r['state'] in {'reserved','uncertain'})/1000000
            row['limit'] = row.pop('limit_micro')/1000000
        return rows
