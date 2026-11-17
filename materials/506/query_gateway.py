import sqlite3
from pathlib import Path
def open_seed():
    db=sqlite3.connect(':memory:');db.executescript(Path(__file__).with_name('seed.sql').read_text());return db
def fixed_rows(db):return db.execute('select id,dept,salary from employees order by id').fetchall()
