"""检查演示商品、订单、物流、政策、评测样本的一致性。仅需Python标准库。"""
from __future__ import annotations
import csv, json, re, sqlite3, hashlib
from datetime import datetime, date
from contextlib import closing
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parent
BUS = ROOT / 'business'

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))

def read_products():
    with (BUS/'products.csv').open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))

def require(ok, msg):
    if not ok: raise AssertionError(msg)

def check(db_path=None):
    products=read_products()
    customers=read_json(BUS/'customers.json')
    orders=read_json(BUS/'orders.json')
    shipments=read_json(BUS/'shipments.json')
    tickets=read_json(BUS/'tickets.json')
    rules=read_json(BUS/'policy_rules.json')
    evals=read_json(ROOT/'eval/eval_cases.json')
    manifest=read_json(ROOT/'knowledge/pdf_manifest.json')
    pmap={p['product_id']:p for p in products}
    umap={u['user_id']:u for u in customers}
    omap={o['order_id']:o for o in orders}
    require(len(pmap)==len(products)==40,'商品ID重复/缺失')
    require(len(umap)==len(customers)==6,'客户ID重复/缺失')
    require(len(omap)==len(orders)==30,'订单ID重复/缺失')
    require(len(set(s['shipment_id'] for s in shipments))==len(shipments),'物流ID重复')
    require(len(set(s['order_id'] for s in shipments))==len(shipments),'一个订单出现多条物流主记录')
    require(len(set(t['ticket_id'] for t in tickets))==len(tickets),'工单ID重复')
    require(len(set(t['request_id'] for t in tickets))==len(tickets),'工单request_id重复')
    require(len(set(e['case_id'] for e in evals))==len(evals)>=60,'评测ID重复/缺失或覆盖不足')
    require(rules['policy_version']=='STORE-2026-V1','政策版本不符')
    reference = date.fromisoformat(rules['evaluation_as_of_date'])
    for p in products:
        require(Decimal(p['price_cny'])>=0,'商品价格为负')
        require(int(p['stock'])>=0,'商品库存为负')
        require(p['status'] in {'active','inactive'},'商品状态未知')
        specs=json.loads(p['specs_json'])
        require(isinstance(specs,dict),'商品规格不是JSON对象')
    for o in orders:
        oid=o['order_id']
        require(o['user_id'] in umap,f'{oid}:客户不存在')
        require(o['status'] in {'pending_payment','paid','shipped','delivered','cancelled','refunded'},f'{oid}:状态无效')
        require(o['items'],f'{oid}:无商品')
        subtotal=Decimal('0')
        for item in o['items']:
            require(item['product_id'] in pmap,f'{oid}:商品不存在')
            require(isinstance(item['quantity'],int) and item['quantity']>0,f'{oid}:数量错误')
            subtotal += Decimal(str(item['unit_price_snapshot_cny'])) * item['quantity']
        shipping = Decimal('0') if subtotal>=Decimal('99') else Decimal('8')
        require(abs(Decimal(str(o['amount_cny']))-(subtotal+shipping))<Decimal('0.01'),f'{oid}:订单含运费总价有误')
        placed=datetime.fromisoformat(o['placed_at'])
        require(placed.date()<=reference,f'{oid}:下单日期超过评测基准日')
        shipped=datetime.fromisoformat(o['shipped_at']) if o['shipped_at'] else None
        delivered=datetime.fromisoformat(o['delivered_at']) if o['delivered_at'] else None
        if shipped: require(placed<=shipped and shipped.date()<=reference,f'{oid}:发货时间异常')
        if delivered: require(shipped is not None and shipped<=delivered and delivered.date()<=reference,f'{oid}:签收时间异常')
        if o['status'] in {'shipped','delivered','refunded'}: require(shipped is not None,f'{oid}:缺少发货时间')
        if o['status'] in {'delivered','refunded'}: require(delivered is not None,f'{oid}:缺少签收时间')
        if o['status'] in {'pending_payment','paid','cancelled'}: require(not shipped and not delivered,f'{oid}:状态与发货签收冲突')
        if o['status']=='shipped': require(not delivered and o['package_state']=='sealed',f'{oid}:未签收包裹状态异常')
    for s in shipments:
        order=omap.get(s['order_id'])
        require(order is not None,f"{s['shipment_id']}:订单不存在")
        require(order['status'] in {'shipped','delivered','refunded'},f"{s['shipment_id']}:状态不应有物流")
        times=[datetime.fromisoformat(e['timestamp']) for e in s['events']]
        require(times and times==sorted(times),f"{s['shipment_id']}:物流时间未排序")
        require(all(t.date()<=reference for t in times),f"{s['shipment_id']}:物流未来时间")
        require(times[0]>=datetime.fromisoformat(order['shipped_at']),f"{s['shipment_id']}:物流早于发货")
        if order['delivered_at']:
            require(s['events'][-1]['status']=='delivered' and times[-1]==datetime.fromisoformat(order['delivered_at']),f"{s['shipment_id']}:签收状态不一致")
        else:
            require(s['events'][-1]['status']!='delivered',f"{s['shipment_id']}:未签收但物流完成")
    for t in tickets:
        require(t['order_id'] in omap,f"{t['ticket_id']}:订单不存在")
        require(t['user_id']==omap[t['order_id']]['user_id'],f"{t['ticket_id']}:工单客户归属错误")
        require(t['status'] in rules['ticket']['allowed_statuses'],f"{t['ticket_id']}:状态不合法")
        require(t['type'] in rules['ticket']['allowed_types'],f"{t['ticket_id']}:工单类型不合法")
        require(datetime.fromisoformat(t['created_at']).date()<=reference,f"{t['ticket_id']}:工单未来时间")
    # PDF-only knowledge sources. All paths are relative to data/.
    docs=manifest['documents']
    require(manifest['version']==rules['policy_version'], 'PDF知识库与业务规则总版本不一致')
    require(len(docs)==7, 'PDF清单应包含7份文档（6公开+1内部SOP）')
    require(len({d['doc_id'] for d in docs})==len(docs), 'PDF文档ID重复')
    require(len({d['pdf_path'] for d in docs})==len(docs), 'PDF路径重复')
    require(sum(bool(d['ingest']) for d in docs)==6, '公开PDF数量与预期不一致')
    require(manifest['expected_public_pdf_count']==6, '公开知识库清单计数错误')
    rules_ids={ref for doc in docs for ref in doc['reference_policies']}
    require('RETURN-2026-V1' in rules_ids and 'CANCEL-2026-V1' in rules_ids,
            '关键售后政策未在PDF清单中标记')
    require(not (ROOT/'knowledge/manifest.json').exists(), '检测到旧Markdown清单，可能造成重复入库')
    require(not list((ROOT/'knowledge').rglob('*.md')), '知识源中仍有旧Markdown，请勿与PDF重复索引')

    total_pages=0
    pages_by_doc={}
    for d in docs:
        rel=Path(d['pdf_path'])
        require(not rel.is_absolute() and '..' not in rel.parts,
                f'PDF路径不安全：{rel}')
        path=ROOT/rel
        require(path.is_file() and path.suffix.lower()=='.pdf',
                f'PDF文件不存在：{rel}')
        require(path.read_bytes()[:5]==b'%PDF-',f'非有效PDF头：{rel}')
        require(hashlib.sha256(path.read_bytes()).hexdigest()==d['sha256'],
                f'PDF文件哈希与manifest不符：{rel}')
        require(d['policy_version']==rules['policy_version'],
                f'PDF版本与业务规则不符：{rel}')
        if d['visibility']=='internal':
            require(d['doc_id']=='KB-SOP-007' and not d['ingest'] and d['allowed_audiences']==['staff'],
                    '内部SOP不允许面向顾客入库')
        else:
            require(d['visibility']=='public' and d['ingest'] and 'customer' in d['allowed_audiences'],
                    f'公开PDF的入库权限设置不正确：{rel}')
        pages_by_doc[d['doc_id']]=d['pages']
        total_pages+=d['pages']
    require(total_pages==manifest['total_pdf_pages']==53,'PDF清单总页数不符')
    pdfs_on_disk={p.relative_to(ROOT).as_posix() for p in (ROOT/'knowledge/raw_pdfs').glob('*.pdf')}
    require(pdfs_on_disk=={d['pdf_path'] for d in docs}, 'PDF原件目录与入库清单不一致')

    # Optional deeper PDF check if PyMuPDF is available, while keeping the validation
    # executable with standard Python alone on fresh developer workstations.
    try:
        import fitz
    except ImportError:
        fitz=None
    if fitz:
        for d in docs:
            with fitz.open(ROOT/d['pdf_path']) as pdf:
                require(len(pdf)==d['pages'],f'PDF实际页数与清单不一致:{d["doc_id"]}')
                require(any(page.get_text().strip() for page in pdf),
                        f'PDF没有可提取文字:{d["doc_id"]}')
                require(any(page.get_images() for page in pdf),
                        f'PDF缺少图像嵌入:{d["doc_id"]}')

    refs=read_json(ROOT/'eval/faq_reference.json')
    require(len(refs)==55,'FAQ标准答案数不是55')
    require(len({r['faq_id'] for r in refs})==len(refs),'FAQ标准答案ID重复')
    require(all(r['expected_policy_id'] in rules_ids for r in refs),
            'FAQ参考了不存在的政策ID')
    qa=read_json(ROOT/'eval/pdf_qa_reference.json')['cases']
    require(len(qa)==26,'PDF问答样本不是26')
    require(len({q['id'] for q in qa})==len(qa),'PDF问答ID重复')
    for q in qa:
        did=q['expected_doc_id']
        require(did in pages_by_doc,f'PDF问答引用未知文档:{q["id"]}')
        require(q['candidate_pages'] and all(1<=int(page)<=pages_by_doc[did] for page in q['candidate_pages']),
                f'PDF问答引用无效页码:{q["id"]}')
        expected_audience='staff' if did=='KB-SOP-007' else 'customer'
        require(q['audience']==expected_audience, f'PDF问答受众权限不符:{q["id"]}')
    image_refs=read_json(ROOT/'eval/image_description_reference.json')['assets']
    require(len(image_refs)==15,'视觉参考图数量不是15')
    require(len({r['filename'] for r in image_refs})==15,'视觉参考图ID重复')
    require(sum(len(o['items'])>1 for o in orders)>=3,'多商品订单样本不足')
    for e in evals:
        require(e['category'] in {'rag','product','order','return'},f'{e["case_id"]}:未知评测类型')
        if 'expected_policy_id' in e:
            require(e['expected_policy_id'] in rules_ids,f'{e["case_id"]}:未知政策ID')
        if 'order_id' in e:
            require(e['order_id'] in omap,f'{e["case_id"]}:未知订单ID')
        if 'auth_user_id' in e:
            require(e['auth_user_id'] in umap,f'{e["case_id"]}:未知用户ID')
        for pid in e['expected_result'].get('expected_product_ids',[]):
            require(pid in pmap,f'{e["case_id"]}:未知商品ID')
    # Referential integrity of the shipped SQLite database.
    db=db_path or BUS/'demo_store.sqlite3'
    require(db.exists(),'预建SQLite数据库不存在')
    with closing(sqlite3.connect(db)) as con:
        require(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok',
                'SQLite完整性检测失败')
        require(not con.execute('PRAGMA foreign_key_check').fetchall(),
                'SQLite外键校验失败')
        for table,expected in [('products',40),('customers',6),('orders',30),('shipments',22),('tickets',8)]:
            require(con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]>=expected,
                    f'业务表行数少于初始数据：{table}')
    print(f'PASS: 商品{len(products)}；用户{len(customers)}；订单{len(orders)}；物流{len(shipments)}；工单{len(tickets)}；PDF{len(docs)}份/{total_pages}页（公开6/内部1）；FAQ{len(refs)}；PDF QA{len(qa)}；图片参考{len(image_refs)}；业务评测{len(evals)}。基础关系、文件哈希、权限、规则版本与引用检查通过。')
    return True

if __name__=='__main__': check()
