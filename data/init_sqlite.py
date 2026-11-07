"""从data/business下的CSV/JSON初始化SQLite。仅需Python标准库。"""
from __future__ import annotations
import argparse, csv, json, sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from validate_data import check

ROOT=Path(__file__).resolve().parent
BUS=ROOT/'business'
DEFAULT_DB=BUS/'demo_store.sqlite3'

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE customers (
 user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1))
);
CREATE TABLE products (
 product_id TEXT PRIMARY KEY, category TEXT NOT NULL, brand TEXT NOT NULL,
 model TEXT NOT NULL, name TEXT NOT NULL, price_cny REAL NOT NULL CHECK(price_cny>=0),
 stock INTEGER NOT NULL CHECK(stock>=0), status TEXT NOT NULL,
 warranty_months INTEGER NOT NULL, features TEXT NOT NULL, scenario TEXT NOT NULL,
 description TEXT NOT NULL, specs_json TEXT NOT NULL
);
CREATE TABLE orders (
 order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES customers(user_id),
 status TEXT NOT NULL, placed_at TEXT NOT NULL, shipped_at TEXT, delivered_at TEXT,
 package_state TEXT NOT NULL, item_condition TEXT NOT NULL, issue_type TEXT NOT NULL,
 amount_cny REAL NOT NULL, data_mode TEXT NOT NULL
);
CREATE TABLE order_items (
 order_id TEXT NOT NULL REFERENCES orders(order_id), product_id TEXT NOT NULL REFERENCES products(product_id),
 quantity INTEGER NOT NULL CHECK(quantity>0), unit_price_snapshot_cny REAL NOT NULL,
 PRIMARY KEY(order_id,product_id)
);
CREATE TABLE shipments (
 shipment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
 carrier TEXT NOT NULL, tracking_no TEXT NOT NULL
);
CREATE TABLE shipment_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
 timestamp TEXT NOT NULL, status TEXT NOT NULL, description TEXT NOT NULL
);
CREATE INDEX idx_shipment_events_time ON shipment_events(shipment_id,timestamp);
CREATE INDEX idx_orders_user ON orders(user_id,status);
CREATE TABLE tickets (
 ticket_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 user_id TEXT NOT NULL REFERENCES customers(user_id),
 order_id TEXT NOT NULL REFERENCES orders(order_id), type TEXT NOT NULL, status TEXT NOT NULL,
 description TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_tickets_owner ON tickets(user_id,order_id);
"""

def load(name): return json.loads((BUS/name).read_text(encoding='utf-8'))

def seed(output:Path, force=False):
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() and not force:
        raise SystemExit(f'数据库已存在：{output}。如需重建请显式加 --force（覆盖现有业务数据）')
    temp=output.with_name(output.name+'.building')
    if temp.exists(): temp.unlink()
    try:
        with closing(sqlite3.connect(temp)) as con:
            con.executescript(SCHEMA)
            con.executemany('INSERT INTO customers VALUES (?,?,?)', [
                (u['user_id'],u['display_name'],int(u['verified'])) for u in load('customers.json')])
            with (BUS/'products.csv').open(encoding='utf-8-sig',newline='') as f:
                products=list(csv.DictReader(f))
            con.executemany('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', [
                (p['product_id'],p['category'],p['brand'],p['model'],p['name'],float(p['price_cny']),
                 int(p['stock']),p['status'],int(p['warranty_months']),p['features'],p['scenario'],
                 p['description'],p['specs_json']) for p in products])
            for o in load('orders.json'):
                con.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
                    o['order_id'],o['user_id'],o['status'],o['placed_at'],o['shipped_at'],o['delivered_at'],
                    o['package_state'],o['item_condition'],o['issue_type'],o['amount_cny'],o['data_mode']))
                for item in o['items']:
                    con.execute('INSERT INTO order_items VALUES (?,?,?,?)', (
                        o['order_id'],item['product_id'],item['quantity'],item['unit_price_snapshot_cny']))
            for s in load('shipments.json'):
                con.execute('INSERT INTO shipments VALUES (?,?,?,?)', (
                    s['shipment_id'],s['order_id'],s['carrier'],s['tracking_no']))
                for e in s['events']:
                    con.execute('INSERT INTO shipment_events(shipment_id,timestamp,status,description) VALUES (?,?,?,?)',
                        (s['shipment_id'],e['timestamp'],e['status'],e['description']))
            for t in load('tickets.json'):
                con.execute('INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)', (
                    t['ticket_id'],t['request_id'],t['user_id'],t['order_id'],t['type'],t['status'],t['description'],t['created_at']))
            assert not con.execute('PRAGMA foreign_key_check').fetchall(), '外键检查失败'
            con.commit()
        check(db_path=temp)
        temp.replace(output)
        with closing(sqlite3.connect(output)) as con:
            tables=('customers','products','orders','order_items','shipments','shipment_events','tickets')
            print('SQLite数据初始化成功:',output)
            print('表数量:',{t:con.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables})
            print('演示查询：300元以内ANC耳机:',con.execute("SELECT product_id,name,price_cny FROM products WHERE category='蓝牙耳机' AND price_cny<=300 AND stock>0 AND status='active' AND json_extract(specs_json, '$.anc') = 1 ORDER BY price_cny").fetchall())
    finally:
        if temp.exists(): temp.unlink()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=DEFAULT_DB)
    parser.add_argument('--force',action='store_true',help='覆盖已有数据库文件')
    args=parser.parse_args()
    seed(args.output,force=args.force)
