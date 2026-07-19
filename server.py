"""
Apple Store 代充服务 - 全自动后端 v4
新增：多冲少冲检测 + 自动退款标记 + 客服通道
"""
from flask import Flask, send_from_directory, request, jsonify
from flask_cors import CORS
import requests, json, hashlib, base64, secrets, time, os, threading
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5, AES
from Crypto.Util.Padding import pad, unpad

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = secrets.token_hex(32)
CORS(app)

# ============ 配置 ============
ADMIN_EMAIL = "MG123456789mao@outlook.com"
ADMIN_PASSWORD = "MG123456789mao"
USDT_ADDRESS = "TBewuoJyvJiDzQYiZM7rYjcJu3Qq5nRFD7"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
UPSTREAM_API = "https://api.promoscard.org/api/v1"

COST_RATIO = 0.8385; MARKUP = 1.07; ADMIN_RMB_RATE = 5.65
ORDER_EXPIRE = 1800
ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'orders.json')
DENOMINATIONS = [25, 50, 100, 200, 300, 500, 1000]
CUSTOM_MIN, CUSTOM_MAX, CUSTOM_STEP = 10, 1200, 5
TOLERANCE = 0.02  # 2% 容差

# 客服联系方式
SUPPORT_TG = "@your_telegram"  # 改成您的
SUPPORT_EMAIL = "MG123456789mao@outlook.com"

# ============ 持久化订单 ============
pending_orders = {}
orders_lock = threading.Lock()

def save_orders():
    try:
        with open(ORDERS_FILE, 'w') as f:
            json.dump(pending_orders, f, ensure_ascii=False, indent=2)
    except: pass

def load_orders():
    global pending_orders
    try:
        if os.path.exists(ORDERS_FILE):
            with open(ORDERS_FILE, 'r') as f:
                pending_orders = json.load(f)
    except: pass

def cleanup_expired():
    now = time.time()
    with orders_lock:
        expired = [oid for oid, o in pending_orders.items()
                   if o.get('status') == 'pending' and now - o.get('created_at', 0) > ORDER_EXPIRE]
        for oid in expired:
            pending_orders[oid]['status'] = 'expired'
        if expired: save_orders()

# ============ PromosCard Admin API ============
class PromosCardAdmin:
    def __init__(self):
        self.session = requests.Session()
        self.aes_key_hex = self.aes_key = self.enc_token = self.auth_token = None
        self._lock = threading.Lock()

    def _init_encryption(self):
        r = self.session.get('https://api.promoscard.com/api/getkey', timeout=15)
        rsa_key = RSA.import_key(r.json()['publicKey'])
        self.aes_key_hex = secrets.token_hex(16)
        self.aes_key = hashlib.sha256(self.aes_key_hex.encode()).digest()
        enc = PKCS1_v1_5.new(rsa_key).encrypt(self.aes_key_hex.encode())
        r = self.session.post('https://api.promoscard.com/api/setkey',
            json={'sign': base64.b64encode(enc).decode()}, timeout=15)
        self.enc_token = r.json()['token']

    def _encrypt(self, d):
        js = json.dumps(d, separators=(',', ':'))
        iv = secrets.token_bytes(16)
        ct = AES.new(self.aes_key, AES.MODE_CBC, iv).encrypt(pad(js.encode(), AES.block_size))
        inner = {'ct': base64.b64encode(ct).decode(), 'iv': iv.hex()}
        return base64.b64encode(json.dumps(inner, separators=(',', ':')).encode()).decode()

    def _decrypt(self, sign):
        inner = json.loads(base64.b64decode(sign).decode())
        iv, ct = bytes.fromhex(inner['iv']), base64.b64decode(inner['ct'])
        return json.loads(unpad(AES.new(self.aes_key, AES.MODE_CBC, iv).decrypt(ct), AES.block_size).decode())

    def _call(self, method, endpoint, data=None):
        url = f'https://api.promoscard.com/api/{endpoint}'
        h = {'X-Encryption-Token': self.enc_token, 'Authorization': f'Bearer {self.auth_token}',
             'Content-Type': 'application/json', 'Accept': 'application/json'}
        if method == 'GET':
            sd = data if data else {'t': int(time.time() * 1000)}
            r = self.session.get(url, params={'sign': self._encrypt(sd)},
                headers={'X-Encryption-Token': self.enc_token, 'Authorization': f'Bearer {self.auth_token}'}, timeout=30)
        else:
            r = self.session.post(url, json={'sign': self._encrypt(data or {})}, headers=h, timeout=30)
        if r.status_code == 200:
            resp = r.json()
            return self._decrypt(resp['sign']) if resp.get('sign') else resp
        return None

    def login(self):
        with self._lock:
            self._init_encryption()
            r = self._call('POST', 'user/login', {'email': ADMIN_EMAIL, 'password': ADMIN_PASSWORD})
            if r and r.get('code') == 200:
                self.auth_token = r['data']['token']; return True
            return False

    def create_order(self, goods_id, amount):
        if not self.auth_token and not self.login(): return None
        pay_amount = round(amount * ADMIN_RMB_RATE, 2)
        return self._call('POST', 'order/create', {
            'goods_id': goods_id, 'recharge_amount': amount,
            'pay_amount': pay_amount, 'channel_level': 'level_2', 'num': 1
        })

admin_api = PromosCardAdmin()

# ============ 上游客户站 API ============
def upstream_post(endpoint, data):
    try:
        r = requests.post(f"{UPSTREAM_API}/{endpoint}", json=data, timeout=30)
        return r.json()
    except Exception as e:
        return {"code": 500, "msg": str(e)}

# ============ 状态轮询 ============
def poll_upstream_status():
    while True:
        try:
            to_poll = []
            with orders_lock:
                for oid, o in list(pending_orders.items()):
                    if o.get('phase') in ('charging', 'checking_2fa', 'submitted', 'need_credentials') and o.get('upstream_token'):
                        to_poll.append((oid, dict(o)))
            for oid, o in to_poll:
                try:
                    r = requests.get(f"{UPSTREAM_API}/order/status", params={'token': o['upstream_token']}, timeout=15)
                    if r.status_code == 200:
                        data = r.json().get('data', {})
                        status = data.get('status', -1)
                        remark = data.get('remark', '')
                        with orders_lock:
                            o2 = pending_orders.get(oid)
                            if not o2: continue
                            o2['recharge_status'] = status
                            o2['recharge_remark'] = remark
                            if status == 5:
                                o2['phase'] = 'done'; o2['status'] = 'completed'
                            elif status in (2,) or '验证' in remark or '2fa' in remark.lower():
                                o2['phase'] = 'need_2fa'; o2['twofa_required'] = True
                            elif status in (3, 4):
                                o2['phase'] = 'charging'
                        save_orders()
                except: pass
        except: pass
        time.sleep(15)

# ============ USDT 监控 ============
def calc_usdt(denomination):
    return round(denomination * COST_RATIO * MARKUP, 2)

def check_usdt():
    try:
        r = requests.get(f'https://api.trongrid.io/v1/accounts/{USDT_ADDRESS}/transactions/trc20', params={
            'contract_address': USDT_CONTRACT, 'limit': 20, 'only_confirmed': 'true', 'order_by': 'timestamp,desc'
        }, timeout=15)
        if r.status_code == 200:
            return [{'tx_id': tx['transaction_id'], 'from': tx.get('from',''), 'to': tx.get('to',''),
                     'amount': int(tx.get('value',0))/1e6, 'time': tx.get('block_timestamp',0)}
                    for tx in r.json().get('data', [])]
        return []
    except: return []

def monitor():
    seen = set()
    while True:
        try:
            cleanup_expired()
            txs = check_usdt()
            for tx in txs:
                tid = tx['tx_id']
                if tid in seen: continue
                seen.add(tid)
                amt = tx['amount']

                with orders_lock:
                    for oid, o in list(pending_orders.items()):
                        if o.get('status') != 'pending': continue
                        expected = o['usdt_amount']
                        diff_pct = abs(amt - expected) / expected
                        from_addr = tx['from']

                        if diff_pct < TOLERANCE:
                            # 精确匹配 → 正常下单
                            o['paid'] = True; o['tx_id'] = tid; o['status'] = 'processing'
                            o['matched_amount'] = amt; o['issue'] = 'ok'
                            break
                        elif amt < expected and amt >= expected * 0.5:
                            # 少付 → 标记 "少付"
                            o['paid'] = True; o['tx_id'] = tid; o['status'] = 'underpaid'
                            o['matched_amount'] = amt; o['issue'] = 'underpaid'
                            o['shortage'] = round(expected - amt, 2)
                            o['refund_address'] = from_addr
                            break
                        elif amt > expected and amt <= expected * 2:
                            # 多付 → 标记 "多付"
                            o['paid'] = True; o['tx_id'] = tid; o['status'] = 'overpaid'
                            o['matched_amount'] = amt; o['issue'] = 'overpaid'
                            o['overage'] = round(amt - expected, 2)
                            o['refund_address'] = from_addr
                            break

                # 如果有匹配的订单，正常处理
                with orders_lock:
                    o = pending_orders.get(list(pending_orders.keys())[0]) if pending_orders else None
                # Fix: process the matched order properly
                for oid, o in list(pending_orders.items()):
                    if o.get('status') == 'processing' and not o.get('order_created'):
                        o['order_created'] = True
                        result = admin_api.create_order(1, o['amount'])
                        with orders_lock:
                            o2 = pending_orders.get(oid)
                            if not o2: continue
                            if result and result.get('code') == 200:
                                o2['status'] = 'need_credentials'
                                o2['order_number'] = result.get('data',{}).get('order_number','')
                                o2['upstream_token'] = result.get('data',{}).get('token','') or result.get('data',{}).get('order_token','') or ''
                                if o2.get('apple_id') and o2.get('apple_password'):
                                    cr = upstream_post('order/submit', {
                                        'token': o2['upstream_token'],
                                        'account': o2['apple_id'],
                                        'password': o2['apple_password']
                                    })
                                    if cr.get('code') == 200:
                                        o2['status'] = 'submitted'; o2['phase'] = 'checking_2fa'
                            else:
                                o2['status'] = 'failed'
                                o2['error'] = result.get('msg','创建失败') if result else 'API错误'
                        save_orders()
        except Exception as e:
            print(f"Monitor: {e}")
        time.sleep(10)

def process_payment(oid, amt, tx_id, from_addr):
    """处理到账：检测多冲少冲"""
    o = pending_orders.get(oid)
    if not o: return
    expected = o['usdt_amount']
    diff = round(amt - expected, 2)

    with orders_lock:
        o['paid'] = True
        o['tx_id'] = tx_id
        o['matched_amount'] = amt
        o['refund_address'] = from_addr  # 退款的 TRC20 地址

        if abs(diff) / expected < TOLERANCE:
            # 正常支付
            o['status'] = 'processing'
            o['issue'] = 'ok'
        elif diff < 0:
            # 少付
            o['status'] = 'underpaid'
            o['issue'] = 'underpaid'
            o['shortage'] = abs(diff)
        else:
            # 多付
            o['status'] = 'overpaid'
            o['issue'] = 'overpaid'
            o['overage'] = diff
        save_orders()

load_orders()
threading.Thread(target=monitor, daemon=True).start()
threading.Thread(target=poll_upstream_status, daemon=True).start()

# ============ 路由 ============
@app.route("/")
def index():
    return app.send_static_file('index.html')

@app.route("/api/pricing")
def pricing():
    prices = [{"denomination": d, "sell_price": calc_usdt(d), "usdt_amount": calc_usdt(d), "symbol": "$"} for d in DENOMINATIONS]
    return jsonify({"code": 200, "data": {
        "denominations": prices, "usdt_address": USDT_ADDRESS,
        "custom": {"min": CUSTOM_MIN, "max": CUSTOM_MAX, "step": CUSTOM_STEP},
        "support": {"tg": SUPPORT_TG, "email": SUPPORT_EMAIL}
    }})

@app.route("/api/order/create", methods=["POST"])
def route_create_order():
    data = request.get_json() or {}
    amount = data.get("amount")
    apple_id = (data.get("apple_id") or "").strip()
    apple_pw = (data.get("apple_password") or "").strip()
    if not amount or amount < CUSTOM_MIN or amount > CUSTOM_MAX:
        return jsonify({"code": 400, "msg": f"金额范围: {CUSTOM_MIN}-{CUSTOM_MAX}"})
    if amount % CUSTOM_STEP != 0:
        return jsonify({"code": 400, "msg": f"必须是 {CUSTOM_STEP} 的倍数"})
    if not apple_id:
        return jsonify({"code": 400, "msg": "请输入 Apple ID"})
    oid = secrets.token_hex(8); usdt = calc_usdt(amount); now = time.time()
    with orders_lock:
        pending_orders[oid] = {
            'order_id': oid, 'amount': amount, 'usdt_amount': usdt,
            'apple_id': apple_id, 'apple_password': apple_pw,
            'created_at': now, 'expires_at': now + ORDER_EXPIRE,
            'paid': False, 'status': 'pending',
            'order_number': None, 'upstream_token': None,
            'phase': 'waiting_payment', 'twofa_required': False,
            'recharge_status': None, 'recharge_remark': '', 'error': None,
            'issue': None, 'matched_amount': 0, 'shortage': 0, 'overage': 0,
            'refund_address': None, 'refund_requested': False
        }
        save_orders()
    return jsonify({"code": 200, "data": {
        "order_id": oid, "amount": amount, "usdt_amount": usdt,
        "usdt_address": USDT_ADDRESS, "expires_in": ORDER_EXPIRE, "status": "pending",
        "warnings": [
            f"请精确转账 {usdt} USDT，使用 TRC20 网络",
            "多转或少转将无法自动下单，需联系客服处理",
            "如有疑问请联系 {support_tg}".format(support_tg=SUPPORT_TG)
        ]
    }})

@app.route("/api/order/status/<oid>")
def order_status(oid):
    with orders_lock:
        o = pending_orders.get(oid)
        if not o:
            return jsonify({"code": 404, "msg": "订单不存在或已过期"})
        return jsonify({"code": 200, "data": {
            "order_id": oid, "amount": o['amount'], "usdt_amount": o['usdt_amount'],
            "status": o['status'], "paid": o['paid'],
            "phase": o.get('phase', 'waiting_payment'),
            "twofa_required": o.get('twofa_required', False),
            "recharge_status": o.get('recharge_status'),
            "recharge_remark": o.get('recharge_remark', ''),
            "error": o.get('error'),
            "expires_at": o.get('expires_at', 0),
            "issue": o.get('issue'),
            "matched_amount": o.get('matched_amount', 0),
            "shortage": o.get('shortage', 0),
            "overage": o.get('overage', 0),
            "refund_requested": o.get('refund_requested', False),
            "order_number": o.get('order_number')
        }})

@app.route("/api/order/<oid>/request-refund", methods=["POST"])
def request_refund(oid):
    """客户申请退款"""
    data = request.get_json() or {}
    refund_addr = (data.get("refund_address") or "").strip()
    if not refund_addr:
        return jsonify({"code": 400, "msg": "请提供您的 TRC20 退款地址"})
    if not refund_addr.startswith("T") or len(refund_addr) != 34:
        return jsonify({"code": 400, "msg": "请输入有效的 TRC20 地址（以 T 开头，34 位）"})

    with orders_lock:
        o = pending_orders.get(oid)
        if not o:
            return jsonify({"code": 404, "msg": "订单不存在"})
        if o.get('refund_requested'):
            return jsonify({"code": 400, "msg": "退款申请已提交，请勿重复申请"})
        if o.get('status') not in ('underpaid', 'overpaid', 'failed', 'expired'):
            return jsonify({"code": 400, "msg": "当前订单状态不支持退款"})

        o['refund_requested'] = True
        o['refund_address'] = refund_addr
        o['refund_amount'] = o.get('matched_amount', 0)
        o['refund_time'] = time.time()
        save_orders()

    return jsonify({
        "code": 200,
        "msg": "退款申请已提交",
        "data": {
            "order_id": oid,
            "refund_amount": o['refund_amount'],
            "refund_address": refund_addr,
            "estimated_time": "24 小时内处理",
            "note": "客服将在 24 小时内处理您的退款，请留意 Telegram 或邮箱通知"
        }
    })

@app.route("/api/order/<oid>/submit-credentials", methods=["POST"])
def submit_credentials(oid):
    with orders_lock:
        o = pending_orders.get(oid)
        if not o: return jsonify({"code": 404, "msg": "订单不存在"})
        if not o.get('upstream_token'):
            return jsonify({"code": 400, "msg": "订单还未创建"})
    result = upstream_post('order/submit', {
        'token': o['upstream_token'],
        'account': o['apple_id'],
        'password': o['apple_password']
    })
    with orders_lock:
        if result.get('code') == 200:
            o['status'] = 'submitted'; o['phase'] = 'checking_2fa'
        else:
            o['cred_error'] = result.get('msg', '提交失败')
    save_orders()
    return jsonify(result)

@app.route("/api/order/<oid>/submit-2fa", methods=["POST"])
def submit_2fa(oid):
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    if len(code) < 6:
        return jsonify({"code": 400, "msg": "请输入6位验证码"})
    with orders_lock:
        o = pending_orders.get(oid)
        if not o: return jsonify({"code": 404, "msg": "订单不存在"})
    result = upstream_post('order/submit_2fa', {'token': o['upstream_token'], 'code': code})
    with orders_lock:
        if result.get('code') == 200:
            o['phase'] = 'charging'; o['twofa_required'] = False
    save_orders()
    return jsonify(result)

@app.route("/api/order/<oid>/resend-2fa", methods=["POST"])
def resend_2fa(oid):
    with orders_lock:
        o = pending_orders.get(oid)
        if not o: return jsonify({"code": 404, "msg": "订单不存在"})
    result = upstream_post('order/resend_2fa', {'token': o['upstream_token'], 'account': o['apple_id']})
    return jsonify(result)

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "usdt_address": USDT_ADDRESS, "pending_orders": len(pending_orders)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Apple 代充 v4 启动: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
