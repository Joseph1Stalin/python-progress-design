"""
Self-study Room Reservation System (Course Project - Single-file Flask app)

How to run:
1. Create a Python virtual environment and install requirements:
     python -m venv venv
   source venv/bin/activate   # on Windows: venv\Scripts\activate
   pip install -r requirements.txt

2. Run the app:
   python self_study_reservation_app.py

3. Open http://127.0.0.1:5000 in a browser.

Notes:
- This is a single-file demo suitable for course practice and local demonstration.
- It uses SQLite (file: data.db) created automatically on first run.
- QR codes are generated as inline PNG images (requires `qrcode` and `pillow`).
- Security: passwords are stored in plain text for simplicity. Do NOT use this in production.

Requirements (requirements.txt):
flask
qrcode
pillow

"""

from flask import Flask, g, render_template_string, request, redirect, url_for, session, jsonify, abort
import sqlite3
import os
from datetime import datetime, timedelta, time as dtime
import io
import base64
import qrcode
import functools
import uuid

app = Flask(__name__)
app.secret_key = 'change-this-to-a-random-secret-for-prod'
DB_PATH = 'data.db'

# ---------------------------- Database helpers ----------------------------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        need_init = not os.path.exists(DB_PATH)
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        if need_init:
            init_db(db)
    return db


def close_db(e=None):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

app.teardown_appcontext(close_db)


def init_db(db):
    cur = db.cursor()
    # Users
    cur.executescript('''
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0
    );

    CREATE TABLE rooms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        desc TEXT
    );

    CREATE TABLE seats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        label TEXT,
        x INTEGER NOT NULL,
        y INTEGER NOT NULL,
        is_open INTEGER DEFAULT 1,
        note TEXT,
        FOREIGN KEY(room_id) REFERENCES rooms(id)
    );

    CREATE TABLE bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        seat_id INTEGER NOT NULL,
        start_dt TEXT NOT NULL,
        end_dt TEXT NOT NULL,
        token TEXT UNIQUE,
        status TEXT DEFAULT 'booked', -- 'booked', 'using', 'cancelled'
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(seat_id) REFERENCES seats(id)
    );
    ''')

    # add sample users
    cur.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)", ('student1', 'pwd1', 0))
    cur.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)", ('student2', 'pwd2', 0))
    cur.execute("INSERT INTO users (username, password, is_admin) VALUES (?, ?, ?)", ('admin', 'admin', 1))

    # sample rooms and seats
    cur.execute("INSERT INTO rooms (name, desc) VALUES (?,?)", ('Room A', 'First floor, quiet'))
    cur.execute("INSERT INTO rooms (name, desc) VALUES (?,?)", ('Room B', 'Second floor, group tables'))
    cur.execute("INSERT INTO rooms (name, desc) VALUES (?,?)", ('Room C', 'West wing, individual desks'))

    # Room A: 4x3 grid
    room_a = cur.lastrowid - 2
    for r in range(3):
        for c in range(4):
            label = f'A-{r*4 + c + 1}'
            cur.execute("INSERT INTO seats (room_id,label,x,y) VALUES (?,?,?,?,)".replace('?,?,?,?,','?,?,?,?'), (room_a, label, 100 + c*60, 60 + r*60))

    # Room B: 3x3 grid
    room_b = room_a + 1
    id_base = 0
    for r in range(3):
        for c in range(3):
            label = f'B-{r*3 + c + 1}'
            cur.execute("INSERT INTO seats (room_id,label,x,y) VALUES (?,?,?,?,)".replace('?,?,?,?,','?,?,?,?'), (room_b, label, 100 + c*80, 60 + r*80))

    # Room C: scattered seats
    room_c = room_b + 1
    coords = [(50,50),(150,40),(250,60),(60,160),(160,160),(260,160),(100,260),(200,260)]
    for i, (xx,yy) in enumerate(coords):
        cur.execute("INSERT INTO seats (room_id,label,x,y) VALUES (?,?,?,?,)".replace('?,?,?,?,','?,?,?,?'), (room_c, f'C-{i+1}', xx, yy))

    db.commit()

# ---------------------------- Auth decorators ----------------------------

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if not session.get('is_admin'):
            abort(403)
        return view(*args, **kwargs)
    return wrapped

# ---------------------------- Utilities ----------------------------

def parse_dt(date_str, time_str):
    # date_str: YYYY-MM-DD, time_str: HH:MM
    return datetime.strptime(date_str + ' ' + time_str, '%Y-%m-%d %H:%M')


def round_to_half_hour(dt):
    # ensure minutes are 00 or 30
    m = dt.minute
    if m == 0 or m == 30:
        return dt.replace(second=0, microsecond=0)
    if m < 30:
        return dt.replace(minute=30, second=0, microsecond=0)
    else:
        dt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return dt


def overlaps(start1, end1, start2, end2):
    # return True if [start1, end1) overlaps [start2, end2)
    return not (end1 <= start2 or end2 <= start1)

# ---------------------------- Routes: auth ----------------------------

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()
        db = get_db()
        try:
            db.execute('INSERT INTO users (username,password) VALUES (?,?)', (username,password))
            db.commit()
        except sqlite3.IntegrityError:
            return '用户名已存在', 400
        return redirect(url_for('login'))
    return render_template_string(REG_TEMPLATE)


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        cur = db.execute('SELECT id, password, is_admin FROM users WHERE username=?', (username,))
        row = cur.fetchone()
        if not row or row['password'] != password:
            return '用户名或密码错误', 400
        session['user_id'] = row['id']
        session['username'] = username
        session['is_admin'] = bool(row['is_admin'])
        return redirect(url_for('index'))
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ---------------------------- Routes: main ----------------------------

@app.route('/')
@login_required
def index():
    db = get_db()
    rooms = db.execute('SELECT * FROM rooms').fetchall()
    return render_template_string(INDEX_TEMPLATE, rooms=rooms, username=session.get('username'))


@app.route('/room/<int:room_id>')
@login_required
def room_view(room_id):
    db = get_db()
    room = db.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
    if not room:
        abort(404)
    seats = db.execute('SELECT * FROM seats WHERE room_id=?', (room_id,)).fetchall()
    today = datetime.now().date().isoformat()
    return render_template_string(ROOM_TEMPLATE, room=room, seats=seats, today=today)

# API: seat status for given date/time range
@app.route('/api/seat_status', methods=['POST'])
@login_required
def seat_status():
    data = request.get_json()
    room_id = int(data['room_id'])
    date = data['date']
    start = data['start']
    end = data['end']
    try:
        start_dt = parse_dt(date, start)
        end_dt = parse_dt(date, end)
    except Exception:
        return jsonify({'error':'invalid date/time'}), 400
    if start_dt >= end_dt:
        return jsonify({'error':'start must be before end'}), 400
    db = get_db()
    seats = db.execute('SELECT * FROM seats WHERE room_id=?', (room_id,)).fetchall()
    out = []
    for s in seats:
        seat = dict(s)
        seat_id = s['id']
        if not s['is_open']:
            seat['status'] = 'closed'
            out.append(seat)
            continue
        # find bookings that overlap
        q = db.execute('SELECT * FROM bookings WHERE seat_id=? AND status != "cancelled"', (seat_id,)).fetchall()
        state = 'available'
        occupying = None
        for b in q:
            bstart = datetime.fromisoformat(b['start_dt'])
            bend = datetime.fromisoformat(b['end_dt'])
            if overlaps(bstart, bend, start_dt, end_dt):
                # if booking covers current selection window
                if b['status'] == 'using' or (bstart <= datetime.now() < bend):
                    state = 'using'
                    occupying = b
                    break
                else:
                    state = 'booked'
                    occupying = b
                    break
        seat['status'] = state
        if occupying:
            seat['booking'] = {'start': occupying['start_dt'], 'end': occupying['end_dt'], 'user_id': occupying['user_id']}
        out.append(seat)
    return jsonify(out)

# API: make booking
@app.route('/api/book', methods=['POST'])
@login_required
def api_book():
    # 管理员禁止预约
    if session.get('is_admin'):
        return jsonify({'error': '管理员不能预约座位'}), 403

    data = request.get_json()
    seat_id = int(data['seat_id'])
    date = data['date']
    start = data['start']
    end = data['end']
    try:
        start_dt = parse_dt(date, start)
        end_dt = parse_dt(date, end)
    except Exception:
        return jsonify({'error':'invalid date/time'}), 400

    # enforce half-hour granularity
    if start_dt.minute % 30 != 0 or end_dt.minute % 30 != 0:
        return jsonify({'error':'times must be on 30-minute boundaries'}), 400
    if start_dt >= end_dt:
        return jsonify({'error':'start must be before end'}), 400

    db = get_db()
    now = datetime.now()

    # 学生只能有一个未完成预约
    existing = db.execute(
        '''
        SELECT * FROM bookings
        WHERE user_id = ?
          AND status != 'cancelled'
          AND end_dt > ?
        ''',
        (session['user_id'], now.isoformat())
    ).fetchone()

    if existing:
        return jsonify({'error': '你已有未完成的预约，请先取消或等待结束'}), 409

    # check seat open
    seat = db.execute('SELECT * FROM seats WHERE id=?', (seat_id,)).fetchone()
    if not seat or not seat['is_open']:
        return jsonify({'error':'seat not available'}), 400

    # conflict check for the seat
    q = db.execute('SELECT * FROM bookings WHERE seat_id=? AND status != "cancelled"', (seat_id,)).fetchall()
    for b in q:
        bstart = datetime.fromisoformat(b['start_dt'])
        bend = datetime.fromisoformat(b['end_dt'])
        if overlaps(bstart, bend, start_dt, end_dt):
            return jsonify({'error':'该位置在所选时间段已被预约或占用'}), 409

    token = str(uuid.uuid4())
    db.execute(
        'INSERT INTO bookings (user_id,seat_id,start_dt,end_dt,token) VALUES (?,?,?,?,?)',
        (session['user_id'], seat_id, start_dt.isoformat(), end_dt.isoformat(), token)
    )
    db.commit()
    booking_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    return jsonify({'ok':True, 'booking_id': booking_id, 'token': token})

# My bookings
@app.route('/mybookings')
@login_required
def mybookings():
    db = get_db()
    rows = db.execute('SELECT b.*, s.label, r.name as room_name FROM bookings b JOIN seats s ON b.seat_id=s.id JOIN rooms r ON s.room_id=r.id WHERE b.user_id=? ORDER BY b.start_dt DESC', (session['user_id'],)).fetchall()
    return render_template_string(MYBOOKINGS_TEMPLATE, bookings=rows)

@app.route('/cancel/<int:bid>')
@login_required
def cancel(bid):
    db = get_db()
    b = db.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b or b['user_id'] != session['user_id']:
        abort(403)
    db.execute('UPDATE bookings SET status="cancelled" WHERE id=?', (bid,))
    db.commit()
    return redirect(url_for('mybookings'))

# QR code for booking
@app.route('/qr/<int:bid>')
@login_required
def qr(bid):
    db = get_db()
    b = db.execute('SELECT b.*, s.label, r.name as room_name FROM bookings b JOIN seats s ON b.seat_id=s.id JOIN rooms r ON s.room_id=r.id WHERE b.id=?', (bid,)).fetchone()
    if not b or b['user_id'] != session['user_id']:
        abort(403)
    # token already exists
    token = b['token']
    # encode token as simple QR
    img = qrcode.make(token)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    data = base64.b64encode(buf.getvalue()).decode('ascii')
    return render_template_string(QR_TEMPLATE, booking=b, qrcode_data=data)

# Validate QR (simulate scanner/administrator scanning)
@app.route('/scan', methods=['GET','POST'])
def scan():
    if request.method == 'POST':
        token = request.form['token'].strip()
        db = get_db()
        b = db.execute('SELECT * FROM bookings WHERE token=?', (token,)).fetchone()
        if not b:
            return '无效二维码', 400
        # check time window
        start_dt = datetime.fromisoformat(b['start_dt'])
        end_dt = datetime.fromisoformat(b['end_dt'])
        now = datetime.now()
        if not (start_dt <= now <= end_dt):
            return '当前不在预约时间内，无法入场', 400
        if b['status'] == 'using':
            return '该预约已签到并正在使用', 400
        db.execute('UPDATE bookings SET status="using" WHERE id=?', (b['id'],))
        db.commit()
        return f'签到成功，座位ID: {b["seat_id"]}'
    return render_template_string(SCAN_TEMPLATE)

# Admin: manage seats open/close
@app.route('/admin/rooms')
@admin_required
def admin_rooms():
    db = get_db()
    rooms = db.execute('SELECT * FROM rooms').fetchall()
    return render_template_string(ADMIN_ROOMS_TEMPLATE, rooms=rooms)

@app.route('/admin/room/<int:room_id>', methods=['GET','POST'])
@admin_required
def admin_room(room_id):
    db = get_db()
    if request.method == 'POST':
        # toggle seat open/close or update note
        seat_id = int(request.form['seat_id'])
        is_open = 1 if request.form.get('is_open')=='1' else 0
        note = request.form.get('note','')
        db.execute('UPDATE seats SET is_open=?, note=? WHERE id=?', (is_open, note, seat_id))
        db.commit()
        return redirect(url_for('admin_room', room_id=room_id))
    room = db.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
    seats = db.execute('SELECT * FROM seats WHERE room_id=?', (room_id,)).fetchall()
    return render_template_string(ADMIN_ROOM_TEMPLATE, room=room, seats=seats)

# ---------------------------- Templates ----------------------------

REG_TEMPLATE = '''
<!doctype html>
<title>Register</title>
<h2>Register</h2>
<form method=post>
  <input name=username placeholder="username">
  <input name=password placeholder="password">
  <button type=submit>Register</button>
</form>
<a href="/login">Login</a>
'''

LOGIN_TEMPLATE = '''
<!doctype html>
<title>Login</title>
<h2>Login</h2>
<form method=post>
  <input name=username placeholder="username">
  <input name=password placeholder="password">
  <button type=submit>Login</button>
</form>
<p>Sample accounts: student1/pwd1, student2/pwd2, admin/admin</p>
'''

INDEX_TEMPLATE = '''
<!doctype html>
<title>Home</title>
<h2>Welcome {{username}}</h2>
<a href="/logout">Logout</a> | <a href="/mybookings">My Bookings</a>
{% if session.is_admin %} | <a href="/admin/rooms">Admin</a>{% endif %}
<hr>
<h3>Available Rooms</h3>
<ul>
{% for r in rooms %}
  <li><a href="/room/{{r.id}}">{{r.name}}</a> - {{r.desc}}</li>
{% endfor %}
</ul>
'''

ROOM_TEMPLATE = '''
<!doctype html>
<title>{{room.name}}</title>
<style>
svg { border:1px solid #ccc; }
.circle { cursor:pointer; }
.legend { margin-top:10px }
.legend span { display:inline-block; width:120px }
</style>
<h2>{{room.name}}</h2>
<a href="/">Home</a> | <a href="/mybookings">My Bookings</a>
<hr>
<label>Date: <input id=date type=date value="{{today}}"></label>
<label>Start: <input id=start type=time value="08:00"></label>
<label>End: <input id=end type=time value="09:00"></label>
<button id=check>Check</button>
<button id=refresh>Refresh</button>
<div>
<svg id=layout width=600 height=400>
{% for s in seats %}
  <circle class="seat circle" data-seatid="{{s.id}}" cx="{{s.x}}" cy="{{s.y}}" r="18" fill="#ddd" stroke="#333"></circle>
  <text x="{{s.x-12}}" y="{{s.y+4}}" font-size="10">{{s.label}}</text>
{% endfor %}
</svg>
</div>
<div class=legend>
  <span><strong>颜色说明：</strong></span>
  <span style="background:#b3e6b3">可预约</span>
  <span style="background:#f5b7b1">已被预约</span>
  <span style="background:#f7dc6f">正在使用</span>
  <span style="background:#d6dbdf">未开放</span>
</div>
<div id=info></div>
<script>
const roomId = {{room.id}};
async function refresh(){
  const date = document.getElementById('date').value;
  const start = document.getElementById('start').value;
  const end = document.getElementById('end').value;
  const resp = await fetch('/api/seat_status', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({room_id: roomId, date, start, end})});
  const data = await resp.json();
  if(!Array.isArray(data)){ document.getElementById('info').innerText = JSON.stringify(data); return; }
  for(const s of data){
    const circle = document.querySelector('.seat[data-seatid="'+s.id+'"]');
    if(!circle) continue;
    let color = '#ddd';
    if(s.status=='available') color = '#b3e6b3';
    if(s.status=='booked') color = '#f5b7b1';
    if(s.status=='using') color = '#f7dc6f';
    if(s.status=='closed') color = '#d6dbdf';
    circle.setAttribute('fill', color);
    circle.dataset.status = s.status;
    circle.dataset.info = s.booking ? JSON.stringify(s.booking) : '';
  }
}

document.getElementById('check').onclick = refresh;
document.getElementById('refresh').onclick = refresh;

// click seat
document.getElementById('layout').addEventListener('click', async (e)=>{
  if(e.target.classList.contains('seat')){
    const seatId = e.target.dataset.seatid;
    const status = e.target.dataset.status || 'unknown';
    const info = e.target.dataset.info || '';
    let html = '<p>座位: '+seatId+'</p><p>状态: '+status+'</p>';
    if(info) html += '<p>占用信息: '+info+'</p>';
    if(status=='available'){
      html += '<button id="bookBtn">预约此座位</button>';
    }
    document.getElementById('info').innerHTML = html;
    if(status=='available'){
      document.getElementById('bookBtn').onclick = async ()=>{
        const date = document.getElementById('date').value;
        const start = document.getElementById('start').value;
        const end = document.getElementById('end').value;
        const resp = await fetch('/api/book', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({seat_id:seatId, date, start, end})});
        const j = await resp.json();
        if(resp.status===200){
          alert('预约成功');
          refresh();
          window.location.href = '/mybookings';
        } else {
          alert('预约失败：'+JSON.stringify(j));
          refresh();
        }
      }
    }
  }
});

// initial
refresh();
</script>
'''

MYBOOKINGS_TEMPLATE = '''
<!doctype html>
<title>My Bookings</title>
<h2>My Bookings</h2>
<a href="/">Home</a>
<table border=1 cellpadding=6>
<tr><th>ID</th><th>Room</th><th>Seat</th><th>Start</th><th>End</th><th>Status</th><th>Actions</th></tr>
{% for b in bookings %}
  <tr>
    <td>{{b.id}}</td>
    <td>{{b.room_name}}</td>
    <td>{{b.label}}</td>
    <td>{{b.start_dt}}</td>
    <td>{{b.end_dt}}</td>
    <td>{{b.status}}</td>
    <td>
      {% if b.status != 'cancelled' %}
        <a href="/cancel/{{b.id}}">Cancel</a>
        | <a href="/qr/{{b.id}}">Show QR</a>
      {% endif %}
    </td>
  </tr>
{% endfor %}
</table>
'''

QR_TEMPLATE = '''
<!doctype html>
<title>QR</title>
<h2>Booking QR</h2>
<p>Room: {{booking.room_name}} | Seat: {{booking.label}}</p>
<img src="data:image/png;base64,{{qrcode_data}}" alt="qr">
<p>Token: {{booking.token}}</p>
<p><a href="/mybookings">Back</a></p>
'''

SCAN_TEMPLATE = '''
<!doctype html>
<title>Scan (simulate)</title>
<h2>Scan QR (simulate)</h2>
<form method=post>
  <input name=token placeholder="paste token here">
  <button type=submit>Validate</button>
</form>
'''

ADMIN_ROOMS_TEMPLATE = '''
<!doctype html>
<title>Admin - Rooms</title>
<h2>Admin - Rooms</h2>
<a href="/">Home</a>
<ul>
{% for r in rooms %}
  <li><a href="/admin/room/{{r.id}}">{{r.name}}</a></li>
{% endfor %}
</ul>
'''

ADMIN_ROOM_TEMPLATE = '''
<!doctype html>
<title>Admin - Room</title>
<h2>Admin - {{room.name}}</h2>
<a href="/admin/rooms">Back</a>
<table border=1 cellpadding=6>
<tr><th>ID</th><th>Label</th><th>Open</th><th>Note</th><th>Action</th></tr>
{% for s in seats %}
<tr>
  <form method=post>
  <td>{{s.id}}</td>
  <td>{{s.label}}</td>
  <td><select name=is_open><option value=1 {% if s.is_open %}selected{% endif %}>Open</option><option value=0 {% if not s.is_open %}selected{% endif %}>Closed</option></select></td>
  <td><input name=note value="{{s.note or ''}}"></td>
  <td><input type=hidden name=seat_id value="{{s.id}}"><button type=submit>Save</button></td>
  </form>
</tr>
{% endfor %}
</table>
'''

# ---------------------------- Run ----------------------------

if __name__ == '__main__':
    # Listen on all network interfaces so other machines can access this server
    # One machine runs the server, other machines use browser to access via IP:port
    app.run(host='0.0.0.0', port=5000, debug=True)
