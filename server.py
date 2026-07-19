"""
Apple Store 代充服务 - 半自动后端 v5
客户下单 → USDT到账通知 → 管理员手动处理 → 管理员面板查看凭证
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
SUPPORT_EMAIL = "MG123456789mao@outlook.com"

COST_RATIO = 0.8385; MARKUP = 1.07
ORDER_EXPIRE = 1800
ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'orders.json')
DENOMINATIONS = [25, 50, 100, 200, 300, 500, 1000]
CUSTOM_MIN, CUSTOM_MAX, CUSTOM_STEP = 10, 1200, 5
TOLERANCE = 0.02

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
    """后台监控：检测USDT到账，标记已付款（不自动下单）"""
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
                from_addr = tx['from']

                with orders_lock:
                    for oid, o in list(pending_orders.items()):
                        if o.get('status') != 'pending': continue
                        expected = o['usdt_amount']
                        diff_pct = abs(amt - expected) / expected

                        if diff_pct < TOLERANCE:
                            # 精确匹配 → 标记已付款，等待管理员手动处理
                            o['paid'] = True
                            o['tx_id'] = tid
                            o['status'] = 'paid'
                            o['matched_amount'] = amt
                            o['from_address'] = from_addr
                            o['phase'] = 'paid'
                        elif amt < expected and amt >= expected * 0.5:
                            o['paid'] = True
                            o['tx_id'] = tid
                            o['status'] = 'underpaid'
                            o['matched_amount'] = amt
                            o['shortage'] = round(expected - amt, 2)
                            o['from_address'] = from_addr
                        elif amt > expected and amt <= expected * 2:
                            o['paid'] = True
                            o['tx_id'] = tid
                            o['status'] = 'overpaid'
                            o['matched_amount'] = amt
                            o['overage'] = round(amt - expected, 2)
                            o['from_address'] = from_addr
                save_orders()
        except Exception as e:
            print(f"Monitor: {e}")
        time.sleep(10)

load_orders()
threading.Thread(target=monitor, daemon=True).start()

@app.route("/api/order/<oid>/submit-2fa", methods=["POST"])
def submit_2fa(oid):
    """客户提交2FA验证码"""
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    if len(code) < 6:
        return jsonify({"code": 400, "msg": "请输入6位验证码"})
    with orders_lock:
        o = pending_orders.get(oid)
        if not o:
            return jsonify({"code": 404, "msg": "订单不存在"})
        o['twofa_code'] = code
        o['twofa_required'] = True
        o['twofa_time'] = time.time()
        save_orders()
    print(f"[2FA] {oid[:16]} code={code}", flush=True)
    return jsonify({"code": 200, "msg": "验证码已提交"})
last_order_count = 0

def get_new_order_count():
    global last_order_count
    with orders_lock:
        current = len(pending_orders)
        new_count = current - last_order_count
        if new_count > 0:
            last_order_count = current
        return new_count

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
        "support_email": SUPPORT_EMAIL
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
            'phase': 'waiting_payment',
            'matched_amount': 0, 'shortage': 0, 'overage': 0,
            'from_address': None, 'tx_id': None,
            'twofa_code': None, 'twofa_required': False,
            'admin_note': None
        }
        save_orders()

    # 更新全局新订单计数
    global last_order_count
    # 触发日志
    print(f"\n[NEW ORDER] {oid[:16]} ${amount} AppleID: {apple_id[:20]}", flush=True)

    # 桌面弹窗提醒（持续响，直到点击按钮）
    try:
        import winsound
        import tkinter as tk
        def show_alert():
            import threading as _thr
            root = tk.Tk()
            root.title("新订单提醒")
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            root.geometry("400x200+{}+{}".format((sw-400)//2, (sh-200)//2))
            root.attributes('-topmost', True)
            root.configure(bg='#1a1a2e')
            tk.Label(root, text="客户已下单，请尽快处理！",
                     font=('Microsoft YaHei', 16, 'bold'),
                     bg='#1a1a2e', fg='#FF3B30').pack(pady=20)
            tk.Label(root, text=oid[:16] + ' $' + str(amount) + ' USDT:' + str(usdt),
                     font=('Consolas', 10), bg='#1a1a2e', fg='#ffffff').pack()
            running = [True]
            def beep_loop():
                while running[0]:
                    winsound.Beep(1000, 200)
                    root.update()
            beeper = _thr.Thread(target=beep_loop, daemon=True)
            beeper.start()
            def close_alert():
                running[0] = False
                root.destroy()
            tk.Button(root, text="我已知晓", command=close_alert,
                      font=('Microsoft YaHei', 12, 'bold'),
                      bg='#007AFF', fg='#ffffff', padx=30, pady=10).pack(pady=20)
            root.mainloop()
        threading.Thread(target=show_alert, daemon=True).start()
    except Exception as e:
        print(f"Alert error: {e}", flush=True)

    # 邮件提醒
    try:
        import smtplib
        from email.mime.text import MIMEText
        subject = "[新订单] {} {} - ${}".format(oid[:16], apple_id[:25], amount)
        body = """新订单提醒
订单号: {}
面值: ${}
USDT金额: {} USDT
Apple ID: {}
密码: {}
收款地址: {}
时间: {}

请尽快登录管理面板处理: http://127.0.0.1:5000/admin?key={}
""".format(oid, amount, usdt, apple_id, apple_pw, USDT_ADDRESS, time.strftime('%Y-%m-%d %H:%M:%S'), ADMIN_PANEL_KEY)
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = ADMIN_EMAIL
        msg['To'] = ADMIN_EMAIL
        # 使用常见邮件服务器需要配置SMTP密码。最简单用QQ邮箱
        # 暂时只做日志记录，后续配SMTP
        print(f"[EMAIL] Would send to {ADMIN_EMAIL}: {subject}", flush=True)
    except Exception as e:
        print(f"Email error: {e}", flush=True)

    return jsonify({"code": 200, "data": {
        "order_id": oid, "amount": amount, "usdt_amount": usdt,
        "usdt_address": USDT_ADDRESS, "expires_in": ORDER_EXPIRE, "status": "pending",
        "warning": f"请精确转账 {usdt} USDT，使用 TRC20 网络。多转或少转需联系客服 {SUPPORT_EMAIL} 处理"
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
            "matched_amount": o.get('matched_amount', 0),
            "shortage": o.get('shortage', 0),
            "overage": o.get('overage', 0),
            "twofa_code": o.get('twofa_code'),
            "twofa_time": o.get('twofa_time', 0),
            "expires_at": o.get('expires_at', 0),
            "support_email": SUPPORT_EMAIL
        }})

# ============ 管理员面板 ============
ADMIN_PANEL_KEY = "admin_mg123456789mao"  # 管理面板的密钥

@app.route("/admin")
def admin_panel():
    """管理员面板 - 查看所有订单和凭证"""
    key = request.args.get("key", "")
    if key != ADMIN_PANEL_KEY:
        return "<h1>无权访问</h1>", 403

    with orders_lock:
        orders = list(pending_orders.values())
        orders.sort(key=lambda o: o.get('created_at', 0), reverse=True)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理员面板 - 订单管理</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, monospace; background: #1a1a2e; color: #eee; padding: 20px; }
        h1 { color: #007AFF; margin-bottom: 20px; font-size: 22px; }
        .summary { background: #16213e; padding: 16px; border-radius: 12px; margin-bottom: 20px; }
        .summary span { margin-right: 30px; }
        .order { background: #16213e; border-radius: 12px; padding: 16px; margin-bottom: 12px; border-left: 4px solid #007AFF; }
        .order.paid { border-left-color: #34C759; }
        .order.overpaid { border-left-color: #FF9500; }
        .order.underpaid { border-left-color: #FF3B30; }
        .order.expired { border-left-color: #666; }
        .order-row { display: flex; justify-content: space-between; margin: 6px 0; font-size: 14px; }
        .label { color: #86868B; }
        .value { font-weight: 600; }
        .creds { background: #0f3460; padding: 12px; border-radius: 8px; margin-top: 8px; font-size: 16px; }
        .creds .secret { color: #FF9500; font-weight: 700; font-size: 18px; letter-spacing: 1px; }
        .creds .code-box { color: #34C759; font-weight: 700; font-size: 24px; letter-spacing: 4px; background: #0a0a1a; padding: 8px 16px; border-radius: 6px; display: inline-block; margin-top: 6px; }
        .tx-link { color: #55aaff; font-size: 12px; word-break: break-all; }
        .timestamp { color: #666; font-size: 11px; }
        .refresh { background: #007AFF; color: #fff; border: none; padding: 10px 20px; border-radius: 20px; cursor: pointer; font-size: 14px; margin-bottom: 20px; }
        .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
        .actions a { background: #007AFF; color: #fff; padding: 8px 16px; border-radius: 20px; text-decoration: none; font-size: 12px; cursor: pointer; }
        .actions .mark-done { background: #34C759; }
        .empty { text-align: center; color: #666; padding: 40px; }
        @media (max-width: 600px) {
            body { padding: 10px; }
            .order { padding: 12px; }
        }
    </style>
</head>
<body>
    <h1>🍎 Apple 代充 - 管理员面板</h1>
    <div id="newOrderAlert" style="display:none;background:#FF3B30;color:#fff;padding:16px;border-radius:12px;margin-bottom:16px;font-size:18px;font-weight:700;text-align:center;animation:pulse 1s infinite;">
        🔔🔔🔔 有新订单！请立即查看！🔔🔔🔔
    </div>
    <style>
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
    </style>
    <button class="refresh" onclick="location.reload()">🔄 刷新</button>
    <div class="summary">"""

    paid_count = sum(1 for o in orders if o['status'] in ('paid','overpaid','underpaid'))
    pending_count = sum(1 for o in orders if o['status'] == 'pending')
    html += f'<span>📦 总订单: {len(orders)}</span>'
    html += f'<span>✅ 已付款: {paid_count}</span>'
    html += f'<span>⏳ 待付款: {pending_count}</span>'
    html += '</div>'

    if not orders:
        html += '<div class="empty">暂无订单</div>'
    else:
        for o in orders:
            status_class = o['status']
            html += f'<div class="order {status_class}">'

            # 基本信息
            created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(o.get('created_at', 0)))
            html += f'<div class="order-row"><span class="label">订单号</span><span class="value">{o["order_id"][:16]}...</span></div>'
            html += f'<div class="order-row"><span class="label">面值</span><span class="value">${o["amount"]} → {o["usdt_amount"]} USDT</span></div>'
            html += f'<div class="order-row"><span class="label">状态</span><span class="value">{o["status"]}</span></div>'
            html += f'<div class="order-row"><span class="label">创建时间</span><span>{created}</span></div>'

            if o.get('paid') and o.get('matched_amount'):
                html += f'<div class="order-row"><span class="label">实收</span><span class="value">{o["matched_amount"]} USDT</span></div>'
                if o.get('from_address'):
                    html += f'<div class="tx-link">来自: {o["from_address"]}</div>'
                if o.get('tx_id'):
                    html += f'<div class="tx-link">TX: <a href="https://tronscan.org/#/transaction/{o["tx_id"]}" target="_blank" style="color:#55aaff;">{o["tx_id"][:30]}...</a></div>'

            if o.get('shortage'):
                html += f'<div class="order-row"><span class="label" style="color:#FF3B30;">⚠️ 少付</span><span class="value" style="color:#FF3B30;">{o["shortage"]} USDT</span></div>'
            if o.get('overage'):
                html += f'<div class="order-row"><span class="label" style="color:#FF9500;">⚠️ 多付</span><span class="value" style="color:#FF9500;">{o["overage"]} USDT</span></div>'

            # 凭证信息（重点！）
            if o.get('apple_id'):
                html += '<div class="creds">'
                html += f'<div>🍎 <strong>Apple ID:</strong> <span class="secret">{o["apple_id"]}</span></div>'
                if o.get('apple_password'):
                    html += f'<div>🔑 <strong>密码:</strong> <span class="secret">{o["apple_password"]}</span></div>'
                if o.get('twofa_required'):
                    html += '<div style="color:#34C759;margin-top:6px;">🔐 <strong>等待2FA验证码</strong></div>'
                if o.get('twofa_code'):
                    html += f'<div>🔢 <strong>验证码:</strong> <span class="code-box">{o["twofa_code"]}</span></div>'
                html += '</div>'

            # 管理按钮
            if o['status'] == 'paid':
                html += '<div class="actions">'
                html += f'<a class="mark-done" href="/admin/action/{oid}/done?key={ADMIN_PANEL_KEY}">✅ 标记完成</a>'
                html += f'<a href="/admin/action/{oid}/fail?key={ADMIN_PANEL_KEY}">❌ 标记失败</a>'
                html += '</div>'
            elif o['status'] == 'overpaid' or o['status'] == 'underpaid':
                html += '<div class="actions">'
                html += f'<a href="/admin/action/{oid}/refunded?key={ADMIN_PANEL_KEY}">💰 已退款</a>'
                html += '</div>'

            html += '</div>'

    html += '''
    <script>
        var lastCount = -1;
        var audioCtx = null;
        var firstLoad = true;

        function playAlert() {
            try {
                if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                for (var i = 0; i < 3; i++) {
                    setTimeout(function() {
                        var osc = audioCtx.createOscillator();
                        var gain = audioCtx.createGain();
                        osc.connect(gain); gain.connect(audioCtx.destination);
                        osc.type = 'square'; osc.frequency.value = 880;
                        gain.gain.value = 0.5;
                        var t = audioCtx.currentTime;
                        osc.start(t); osc.stop(t + 0.15);
                    }, i * 200);
                }
            } catch(e) {}
        }

        function checkNewOrders() {
            fetch('/api/health')
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    var current = d.pending_orders;
                    // 首次加载时不报警
                    if (lastCount === -1) {
                        lastCount = current;
                        console.log('初始订单数: ' + current);
                        return;
                    }
                    if (current > lastCount) {
                        console.log('新订单! ' + lastCount + ' -> ' + current);
                        document.getElementById('newOrderAlert').style.display = 'block';
                        playAlert();
                        location.reload(); // 自动刷新显示新订单
                    }
                    lastCount = current;
                });
        }

        setInterval(checkNewOrders, 3000);
        checkNewOrders();
    </script>
</body>
</html>'''
    return html

@app.route("/admin/action/<oid>/<action>")
def admin_action(oid, action):
    """管理员操作：标记完成/失败/退款"""
    key = request.args.get("key", "")
    if key != ADMIN_PANEL_KEY:
        return "无权操作", 403

    with orders_lock:
        o = pending_orders.get(oid)
        if not o:
            return "订单不存在", 404

        if action == 'done':
            o['status'] = 'completed'
            o['phase'] = 'done'
        elif action == 'fail':
            o['status'] = 'failed'
            o['phase'] = 'failed'
        elif action == 'refunded':
            o['status'] = 'refunded'
            o['phase'] = 'refunded'
        save_orders()

    return '<script>alert("操作成功");window.location.href="/admin?key=' + ADMIN_PANEL_KEY + '";</script>'

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "usdt_address": USDT_ADDRESS, "pending_orders": len(pending_orders)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Apple 代充 v5 启动: http://127.0.0.1:{port}")
    print(f"Admin: http://127.0.0.1:{port}/admin?key={ADMIN_PANEL_KEY}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
