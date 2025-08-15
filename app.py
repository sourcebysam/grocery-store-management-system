# grocery_pos_single.py
"""
Grocery Store Management Website — Single-file (extended)
Adds:
 - barcode search in POS
 - line discounts and order-level discount
 - CGST/SGST split display
 - thermal-style printable receipt (80mm)
Usage:
  pip install flask SQLAlchemy itsdangerous
  python grocery_pos_single.py
Default admin: admin / admin123
"""
from __future__ import annotations
from datetime import datetime, date, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import os, secrets, io, csv

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, abort, send_file
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey,
    CheckConstraint, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session
from werkzeug.security import generate_password_hash, check_password_hash
from jinja2 import DictLoader

# ---------------------------------------------------------------------
# App & DB
# ---------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET", "dev-secret-change-me")

engine = create_engine("sqlite:///grocery.db", echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()

TWOPLACES = Decimal("0.01")
def money(x) -> Decimal:
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    return x.quantize(TWOPLACES, rounding=ROUND_HALF_UP)

def today() -> date:
    return datetime.now().date()

# ---------------------------------------------------------------------
# Models (barcode + discount support)
# ---------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="admin")  # admin / staff
    created_at = Column(DateTime, default=datetime.utcnow)

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    sku = Column(String(60), unique=True, nullable=False)
    barcode = Column(String(120), unique=True, nullable=True)   # new: barcode/EAN/UPCA
    name = Column(String(200), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    category = relationship("Category")
    price = Column(Numeric(10,2), nullable=False)      # selling price
    cost_price = Column(Numeric(10,2), nullable=False) # purchase price
    gst_rate = Column(Numeric(5,2), nullable=False, default=Decimal("0.00"))  # %
    unit = Column(String(30), nullable=False, default="pcs")
    stock_qty = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        CheckConstraint("price >= 0"),
        CheckConstraint("cost_price >= 0"),
        CheckConstraint("gst_rate >= 0"),
        CheckConstraint("stock_qty >= 0"),
    )

class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    phone = Column(String(20), unique=True, nullable=False)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    customer = relationship("Customer")
    staff_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    staff = relationship("User")
    subtotal = Column(Numeric(12,2), nullable=False, default=Decimal("0.00"))   # before order discount
    order_discount = Column(Numeric(5,2), nullable=False, default=Decimal("0.00")) # % e.g. 5 for 5%
    tax_total = Column(Numeric(12,2), nullable=False, default=Decimal("0.00"))
    grand_total = Column(Numeric(12,2), nullable=False, default=Decimal("0.00"))
    profit_amount = Column(Numeric(12,2), nullable=False, default=Decimal("0.00"))
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    order = relationship("Order", back_populates="items")
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product = relationship("Product")
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(10,2), nullable=False)  # captured sale price
    gst_rate = Column(Numeric(5,2), nullable=False)
    discount_pct = Column(Numeric(5,2), nullable=False, default=Decimal("0.00"))  # line discount %
    __table_args__ = (CheckConstraint("quantity > 0"),)

    # computational helpers
    def line_subtotal(self):
        return money(Decimal(self.unit_price) * self.quantity)

    def line_discount_amount(self):
        return money(self.line_subtotal() * (Decimal(self.discount_pct)/Decimal("100")))

    def line_taxable(self):
        return money(self.line_subtotal() - self.line_discount_amount())

    def line_tax(self):
        rate = Decimal(self.gst_rate) / Decimal("100")
        return money(self.line_taxable() * rate)

    def line_total(self):
        return money(self.line_taxable() + self.line_tax())

    def line_profit(self):
        # profit reduces by discount amount
        cp = Decimal(self.product.cost_price)
        sp = Decimal(self.unit_price)
        base_profit = (sp - cp) * self.quantity
        profit_after_discount = Decimal(base_profit) - self.line_discount_amount()
        return money(profit_after_discount)

class InventoryLog(Base):
    __tablename__ = "inventory_logs"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    product = relationship("Product")
    change_qty = Column(Integer, nullable=False)
    reason = Column(String(30), nullable=False)
    staff_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    staff = relationship("User")
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

# ---------------------------------------------------------------------
# DB init & seed
# ---------------------------------------------------------------------
def db():
    return SessionLocal()

def init_db():
    Base.metadata.create_all(engine)
    s = db()
    if not s.query(User).filter_by(username="admin").first():
        u = User(username="admin", password_hash=generate_password_hash("admin123"), role="admin")
        s.add(u)
        # seed categories/products including barcode values
        c1 = Category(name="Food & Beverages")
        c2 = Category(name="Home Care")
        s.add_all([c1, c2]); s.flush()
        s.add_all([
            Product(sku="MILK500", barcode="8901000000010", name="Toned Milk 500ml", category_id=c1.id,
                    price=Decimal("28.00"), cost_price=Decimal("24.00"), gst_rate=Decimal("5.00"), unit="pack", stock_qty=50),
            Product(sku="RICE5", barcode="8901000000027", name="Rice 5kg", category_id=c1.id,
                    price=Decimal("350.00"), cost_price=Decimal("300.00"), gst_rate=Decimal("5.00"), unit="bag", stock_qty=20),
            Product(sku="DETER1", barcode="8901000000034", name="Detergent 1kg", category_id=c2.id,
                    price=Decimal("120.00"), cost_price=Decimal("90.00"), gst_rate=Decimal("18.00"), unit="pack", stock_qty=30),
        ])
        s.commit()
    s.close()

# ---------------------------------------------------------------------
# CSRF token (session)
# ---------------------------------------------------------------------
CSRF_SESSION_KEY = "_csrf_token"
def get_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_hex(16)
        session[CSRF_SESSION_KEY] = token
    return token

def require_csrf():
    form_token = request.form.get("csrf_token")
    if not form_token or form_token != session.get(CSRF_SESSION_KEY):
        abort(400, description="Invalid CSRF token")

@app.context_processor
def inject_csrf():
    return {"csrf_token": get_csrf_token}

# ---------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------
def is_logged_in() -> bool:
    return session.get("user_id") is not None

def current_role() -> Optional[str]:
    return session.get("role")

def require_login(role: Optional[str] = None):
    if not is_logged_in():
        flash("Please login.", "warning")
        return redirect(url_for("login"))
    if role and current_role() not in (role, "admin"):
        flash("Insufficient privileges.", "warning")
        return redirect(url_for("dashboard"))

# ---------------------------------------------------------------------
# Templates (DictLoader) - only changed POS & invoice/receipt templates
# ---------------------------------------------------------------------
TEMPLATES = {
"base.html": """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Grocery POS{% endblock %}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  <style>
    .container{max-width:1100px;margin:auto}
    .muted{color:#777;font-size:.9rem}
    @media print { nav,.no-print{display:none!important} body{background:#fff} }
    table td, table th { vertical-align: top; }
  </style>
</head>
<body>
  <nav class="container">
    <ul><li><strong>Grocery POS</strong></li></ul>
    <ul>
      {% if session.get('user_id') %}
        <li><a href="{{ url_for('dashboard') }}">Dashboard</a></li>
        <li><a href="{{ url_for('products') }}">Products</a></li>
        <li><a href="{{ url_for('pos') }}">POS</a></li>
        <li><a href="{{ url_for('orders') }}">Transactions</a></li>
        <li><a href="{{ url_for('reports_daily') }}">Reports</a></li>
        <li><a href="{{ url_for('logout') }}" class="contrast">Logout</a></li>
      {% else %}
        <li><a href="{{ url_for('login') }}">Login</a></li>
      {% endif %}
    </ul>
  </nav>
  <main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <div class="no-print">
          {% for cat,msg in messages %}
            <article class="{{ 'secondary' if cat=='info' else cat }}">{{ msg }}</article>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>
  <footer class="container muted no-print" style="margin-top:2rem">Single-file Flask • Barcode, discounts, CGST/SGST</footer>
</body>
</html>
""",
"login.html": """
{% extends 'base.html' %}
{% block title %}Login{% endblock %}
{% block content %}
<h2>Staff Login</h2>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Username <input name="username" required></label>
  <label>Password <input type="password" name="password" required></label>
  <button type="submit">Login</button>
</form>
<p class="muted">Default admin: admin / admin123</p>
{% endblock %}
""",
"dashboard.html": """
{% extends 'base.html' %}
{% block title %}Dashboard{% endblock %}
{% block content %}
<h2>Dashboard</h2>
<div class="grid">
  <article><header>Today's Sales</header><h3>₹ {{ today_sales }}</h3></article>
  <article><header>Today's Profit</header><h3>₹ {{ today_profit }}</h3></article>
  <article><header>This Month Sales</header><h3>₹ {{ month_sales }}</h3></article>
  <article><header>This Month Profit</header><h3>₹ {{ month_profit }}</h3></article>
</div>
{% endblock %}
""",
"products.html": """
{% extends 'base.html' %}
{% block title %}Products{% endblock %}
{% block content %}
<h2>Products</h2>
<details class="no-print" open>
  <summary>Add Product</summary>
  <form method="post" action="{{ url_for('add_product') }}">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="grid">
      <label>SKU <input name="sku" required></label>
      <label>Barcode <input name="barcode"></label>
      <label>Name <input name="name" required></label>
      <label>Category
        <select name="category_id" required>
          {% for c in categories %}<option value="{{c.id}}">{{c.name}}</option>{% endfor %}
        </select>
      </label>
      <label>Selling ₹ <input type="number" step="0.01" name="price" required></label>
      <label>Cost ₹ <input type="number" step="0.01" name="cost_price" required></label>
      <label>GST % <input type="number" step="0.01" name="gst_rate" required></label>
      <label>Unit <input name="unit" value="pcs" required></label>
      <label>Init Stock <input type="number" name="stock_qty" required></label>
    </div>
    <button>Add</button>
  </form>
</details>

<table>
  <thead><tr><th>SKU</th><th>Barcode</th><th>Name</th><th>Category</th><th>Price</th><th>GST%</th><th>Stock</th><th class="no-print"></th></tr></thead>
  <tbody>
    {% for p in products %}
    <tr>
      <td>{{p.sku}}</td>
      <td>{{p.barcode or '-'}}</td>
      <td>{{p.name}}</td>
      <td>{{p.category.name}}</td>
      <td>₹ {{ '%.2f'|format(p.price) }}</td>
      <td>{{ '%.2f'|format(p.gst_rate) }}</td>
      <td>{{p.stock_qty}} {{p.unit}}</td>
      <td class="no-print">
        <form method="post" action="{{ url_for('refill') }}" class="grid" style="grid-template-columns: 1fr auto; gap:.5rem; align-items:center">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <input type="hidden" name="product_id" value="{{p.id}}">
          <input type="number" name="qty" placeholder="Qty" required>
          <button class="secondary">Refill</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>

<p class="no-print">
  <a href="{{ url_for('products_export') }}" role="button">Export CSV</a>
  <details style="display:inline-block">
    <summary role="button">Import CSV</summary>
    <form method="post" action="{{ url_for('products_import') }}" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="file" name="file" accept=".csv" required>
      <button>Upload</button>
    </form>
  </details>
</p>
{% endblock %}
""",
"pos.html": """
{% extends 'base.html' %}
{% block title %}POS{% endblock %}
{% block content %}
<h2>Point of Sale</h2>
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <details class="no-print" open>
    <summary>Customer</summary>
    <div class="grid">
      <label>Phone <input name="phone" required></label>
      <label>Name (only if new) <input name="name" placeholder="New customer"></label>
    </div>
  </details>

  <details class="no-print" open>
    <summary>Add Item</summary>
    <div class="grid">
      <label>Barcode / SKU <input name="barcode_or_sku" placeholder="Scan or type barcode or SKU"></label>
      <label>OR choose product
        <select name="product_id">
          {% for p in products %}<option value="{{p.id}}">{{p.sku}} - {{p.name}} (Stock {{p.stock_qty}})</option>{% endfor %}
        </select>
      </label>
      <label>Qty <input type="number" name="qty" value="1" min="1"></label>
      <label>Line Discount % <input type="number" step="0.01" name="line_discount" value="0"></label>
    </div>
    <div class="grid">
      <button name="add_line" value="1" class="secondary">Add to cart</button>
    </div>
  </details>

  <details class="no-print">
    <summary>Order Options</summary>
    <div class="grid">
      <label>Order Discount % <input type="number" step="0.01" name="order_discount" value="{{ order_discount or 0 }}"></label>
    </div>
  </details>

  <p><button>Checkout</button></p>
</form>

{% if cart and cart|length>0 %}
  <h3>Cart</h3>
  <table>
    <thead><tr><th>Item</th><th>Qty</th><th>Unit ₹</th><th>Line Disc%</th><th>GST%</th><th>Line Total ₹</th></tr></thead>
    <tbody>
      {% for c in cart %}
        <tr>
          <td>{{ c.product.name }}</td>
          <td>{{ c.qty }}</td>
          <td>{{ '%.2f'|format(c.product.price) }}</td>
          <td>{{ '%.2f'|format(c.discount or 0) }}</td>
          <td>{{ '%.2f'|format(c.product.gst_rate) }}</td>
          <td>
            {% set ls = (c.product.price * c.qty) %}
            {% set ld = ls * ((c.discount or 0)/100) %}
            {% set ta = ls - ld %}
            {% set tax = ta * (c.product.gst_rate/100) %}
            ₹ {{ '%.2f'|format(ta + tax) }}
          </td>
        </tr>
      {% endfor %}
    </tbody>
  </table>

  <p class="muted">Apply order discount at checkout to reduce subtotal before taxes.</p>
{% endif %}
{% endblock %}
""",
"orders.html": """
{% extends 'base.html' %}
{% block title %}Transactions{% endblock %}
{% block content %}
<h2>Transactions</h2>
<table>
  <thead><tr><th>#</th><th>Date/Time</th><th>Customer</th><th>Staff</th><th>Subtotal</th><th>Order Disc%</th><th>Tax</th><th>Total</th><th>Profit</th><th class="no-print"></th></tr></thead>
  <tbody>
    {% for o in orders %}
    <tr>
      <td>{{o.id}}</td>
      <td>{{o.created_at.strftime('%Y-%m-%d %H:%M')}}</td>
      <td>{{ o.customer.phone if o.customer else '-' }}</td>
      <td>{{ o.staff.username }}</td>
      <td>₹ {{ '%.2f'|format(o.subtotal) }}</td>
      <td>{{ '%.2f'|format(o.order_discount) }}</td>
      <td>₹ {{ '%.2f'|format(o.tax_total) }}</td>
      <td>₹ {{ '%.2f'|format(o.grand_total) }}</td>
      <td>₹ {{ '%.2f'|format(o.profit_amount) }}</td>
      <td class="no-print"><a href="{{ url_for('invoice', order_id=o.id) }}">Invoice</a> | <a href="{{ url_for('receipt', order_id=o.id) }}" target="_blank">Receipt</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
""",
"invoice.html": """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Invoice #{{ o.id }}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  <style>@media print {.no-print{display:none}}</style>
</head>
<body class="container" style="padding:1.5rem">
  <p class="no-print"><button onclick="window.print()">Print</button></p>
  <h3>Grocery Store</h3>
  <p>Invoice #: <strong>{{ o.id }}</strong> &nbsp; | &nbsp; Date: {{ o.created_at.strftime('%Y-%m-%d %H:%M') }}<br>
     Customer: {{ o.customer.name if o.customer else '-' }} &nbsp; | &nbsp; Phone: {{ o.customer.phone if o.customer else '-' }}</p>
  <table>
    <thead><tr><th>Item</th><th>Qty</th><th>Unit ₹</th><th>Disc%</th><th>Taxable ₹</th><th>Tax ₹ (CGST/SGST)</th><th>Total ₹</th></tr></thead>
    <tbody>
      {% for it in o.items %}
      <tr>
        <td>{{ it.product.name }}</td>
        <td>{{ it.quantity }}</td>
        <td>{{ '%.2f'|format(it.unit_price) }}</td>
        <td>{{ '%.2f'|format(it.discount_pct) }}</td>
        <td>{{ '%.2f'|format(it.line_taxable()) }}</td>
        {% set tax = it.line_tax() %}
        <td>
          CGST ₹ {{ '%.2f'|format((tax/2)) }} / SGST ₹ {{ '%.2f'|format((tax/2)) }}
        </td>
        <td>{{ '%.2f'|format(it.line_total()) }}</td>
      </tr>
      {% endfor %}
    </tbody>
    <tfoot>
      <tr><th colspan="5" style="text-align:right">Subtotal</th><th colspan="2">₹ {{ '%.2f'|format(o.subtotal) }}</th></tr>
      <tr><th colspan="5" style="text-align:right">Order Discount (%)</th><th colspan="2">{{ '%.2f'|format(o.order_discount) }}</th></tr>
      <tr><th colspan="5" style="text-align:right">Tax</th><th colspan="2">₹ {{ '%.2f'|format(o.tax_total) }}</th></tr>
      <tr><th colspan="5" style="text-align:right">Grand Total</th><th colspan="2">₹ {{ '%.2f'|format(o.grand_total) }}</th></tr>
      <tr><th colspan="5" style="text-align:right">Profit</th><th colspan="2">₹ {{ '%.2f'|format(o.profit_amount) }}</th></tr>
    </tfoot>
  </table>
</body>
</html>
""",
"receipt.html": """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Receipt #{{ o.id }}</title>
  <style>
    /* Thermal receipt style ~80mm */
    @page { size: 80mm auto; margin: 0; }
    body { width:80mm; font-family: monospace; font-size:12px; margin:0; padding:6px; }
    h2 { text-align:center; font-size:14px; margin:6px 0; }
    .small { font-size:11px; }
    table { width:100%; border-collapse:collapse; margin-top:6px; }
    td, th { padding:2px 0; vertical-align:top; }
    .right { text-align:right; }
    .center { text-align:center; }
    .muted { color:#444; font-size:11px; }
    .ttl { font-weight:bold; }
    .no-print{display:none}
  </style>
</head>
<body>
  <h2>Grocery Store</h2>
  <div class="small center">Invoice #: {{ o.id }} | {{ o.created_at.strftime('%Y-%m-%d %H:%M') }}</div>
  <div class="small">Customer: {{ o.customer.name if o.customer else '-' }}</div>
  <table>
    <thead><tr><th>Item</th><th class="right">Qty</th><th class="right">Amt</th></tr></thead>
    <tbody>
      {% for it in o.items %}
      <tr>
        <td>{{ it.product.name|truncate(20) }}</td>
        <td class="right">{{ it.quantity }}</td>
        <td class="right">₹ {{ '%.2f'|format(it.line_total()) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <hr>
  <table>
    <tr><td>Subtotal</td><td class="right">₹ {{ '%.2f'|format(o.subtotal) }}</td></tr>
    <tr><td>Order Disc (%)</td><td class="right">{{ '%.2f'|format(o.order_discount) }}</td></tr>
    <tr><td>Tax</td><td class="right">₹ {{ '%.2f'|format(o.tax_total) }}</td></tr>
    <tr class="ttl"><td>Grand Total</td><td class="right">₹ {{ '%.2f'|format(o.grand_total) }}</td></tr>
  </table>
  <p class="center muted">Thank you! Visit again.</p>
</body>
</html>
""",
"reports.html": """
{% extends 'base.html' %}
{% block title %}Reports{% endblock %}
{% block content %}
<h2>Reports</h2>
<form class="grid no-print" method="get">
  <label>Date <input type="date" name="date" value="{{ req_date }}"></label>
  <button>Go</button>
</form>
<div class="grid">
  <article><header>Sales</header><h3>₹ {{ sales }}</h3></article>
  <article><header>Tax</header><h3>₹ {{ tax }}</h3></article>
  <article><header>Profit</header><h3>₹ {{ profit }}</h3></article>
</div>
<hr>
<h3>Monthly Summary</h3>
<form class="grid no-print" method="get" action="{{ url_for('reports_monthly') }}">
  <label>Month <input type="month" name="month" value="{{ month }}"></label>
  <button>Go</button>
</form>
{% endblock %}
""",
}
app.jinja_loader = DictLoader(TEMPLATES)

# ---------------------------------------------------------------------
# Routes: Auth / Home
# ---------------------------------------------------------------------
@app.route("/")
def root():
    if not is_logged_in():
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        require_csrf()
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        s = db()
        user = s.query(User).filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            session["role"] = user.role
            flash("Welcome!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials", "warning")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login"))

# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    r = require_login()
    if r: return r
    s = db()
    start = datetime.combine(today(), time.min)
    end = datetime.combine(today(), time.max)
    t_sales, t_profit = s.query(
        func.coalesce(func.sum(Order.grand_total), 0),
        func.coalesce(func.sum(Order.profit_amount), 0)
    ).filter(Order.created_at.between(start, end)).first()
    now = datetime.now()
    m_start = datetime(now.year, now.month, 1)
    m_end = datetime(now.year + (now.month==12), (now.month % 12) + 1, 1) - timedelta(seconds=1)
    m_sales, m_profit = s.query(
        func.coalesce(func.sum(Order.grand_total), 0),
        func.coalesce(func.sum(Order.profit_amount), 0)
    ).filter(Order.created_at.between(m_start, m_end)).first()
    return render_template("dashboard.html",
        today_sales=f"{(t_sales or 0):.2f}", today_profit=f"{(t_profit or 0):.2f}",
        month_sales=f"{(m_sales or 0):.2f}", month_profit=f"{(m_profit or 0):.2f}"
    )

# ---------------------------------------------------------------------
# Products & Inventory (with barcode import/export)
# ---------------------------------------------------------------------
@app.route("/products")
def products():
    r = require_login("staff")
    if r: return r
    s = db()
    products = s.query(Product).join(Category).order_by(Product.name).all()
    categories = s.query(Category).order_by(Category.name).all()
    if not categories:
        c = Category(name="General"); s.add(c); s.commit(); categories = [c]
    return render_template("products.html", products=products, categories=categories)

@app.route("/products/add", methods=["POST"])
def add_product():
    r = require_login("admin")
    if r: return r
    require_csrf()
    s = db()
    try:
        p = Product(
            sku=request.form.get("sku","").strip(),
            barcode=request.form.get("barcode","").strip() or None,
            name=request.form.get("name","").strip(),
            category_id=int(request.form.get("category_id")),
            price=money(request.form.get("price")),
            cost_price=money(request.form.get("cost_price")),
            gst_rate=money(request.form.get("gst_rate")),
            unit=request.form.get("unit","pcs").strip(),
            stock_qty=int(request.form.get("stock_qty",0))
        )
        s.add(p); s.commit()
        flash("Product added", "success")
    except Exception as e:
        s.rollback()
        flash(f"Could not add product: {e}", "warning")
    return redirect(url_for("products"))

@app.route("/inventory/refill", methods=["POST"])
def refill():
    r = require_login("staff")
    if r: return r
    require_csrf()
    s = db()
    try:
        pid = int(request.form.get("product_id"))
        qty = int(request.form.get("qty"))
        if qty <= 0:
            raise ValueError("Quantity must be positive")
        p = s.get(Product, pid)
        if not p: abort(404)
        p.stock_qty += qty
        s.add(InventoryLog(product=p, change_qty=qty, reason="refill", staff_id=session["user_id"], note="Refill"))
        s.commit()
        flash("Stock updated", "success")
    except Exception as e:
        s.rollback()
        flash(f"Refill failed: {e}", "warning")
    return redirect(url_for("products"))

@app.route("/products/export")
def products_export():
    r = require_login("staff")
    if r: return r
    s = db()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["sku","barcode","name","category","price","cost_price","gst_rate","unit","stock_qty"])
    for p in s.query(Product).join(Category).all():
        w.writerow([p.sku, p.barcode or "", p.name, p.category.name, f"{p.price:.2f}", f"{p.cost_price:.2f}", f"{p.gst_rate:.2f}", p.unit, p.stock_qty])
    buf.seek(0)
    return send_file(io.BytesIO(buf.read().encode("utf-8")), as_attachment=True, download_name="products.csv", mimetype="text/csv")

@app.route("/products/import", methods=["POST"])
def products_import():
    r = require_login("admin")
    if r: return r
    require_csrf()
    s = db()
    f = request.files.get("file")
    if not f:
        flash("No file uploaded", "warning"); return redirect(url_for("products"))
    created = 0
    reader = csv.DictReader(io.StringIO(f.read().decode("utf-8")))
    for row in reader:
        cname = (row.get("category") or "General").strip()
        cat = s.query(Category).filter_by(name=cname).first()
        if not cat:
            cat = Category(name=cname); s.add(cat); s.flush()
        sku = (row.get("sku") or "").strip()
        if not sku: continue
        existing = s.query(Product).filter_by(sku=sku).first()
        if existing:
            existing.barcode = (row.get("barcode") or existing.barcode)
            existing.name = row.get("name") or existing.name
            existing.category_id = cat.id
            existing.price = money(row.get("price") or existing.price)
            existing.cost_price = money(row.get("cost_price") or existing.cost_price)
            existing.gst_rate = money(row.get("gst_rate") or existing.gst_rate)
            existing.unit = row.get("unit") or existing.unit
            existing.stock_qty = int(row.get("stock_qty") or existing.stock_qty)
        else:
            s.add(Product(
                sku=sku,
                barcode=(row.get("barcode") or None),
                name=row.get("name") or sku,
                category_id=cat.id,
                price=money(row.get("price") or "0"),
                cost_price=money(row.get("cost_price") or "0"),
                gst_rate=money(row.get("gst_rate") or "0"),
                unit=row.get("unit") or "pcs",
                stock_qty=int(row.get("stock_qty") or 0)
            ))
            created += 1
    s.commit()
    flash(f"Imported/updated products (new: {created})", "success")
    return redirect(url_for("products"))

# ---------------------------------------------------------------------
# POS: barcode search, line discounts, order discount, checkout
# ---------------------------------------------------------------------
@app.route("/pos", methods=["GET","POST"])
def pos():
    r = require_login("staff")
    if r: return r
    s = db()
    products = s.query(Product).order_by(Product.name).all()
    cart = session.get("cart", [])
    order_discount = session.get("order_discount", 0)

    if request.method == "POST":
        require_csrf()
        # Add by barcode/SKU
        if "add_line" in request.form:
            barcode_or_sku = (request.form.get("barcode_or_sku") or "").strip()
            pid = None
            prod = None
            if barcode_or_sku:
                prod = s.query(Product).filter((Product.barcode==barcode_or_sku)|(Product.sku==barcode_or_sku)).first()
                if not prod:
                    flash("Product not found by barcode/SKU", "warning")
                    return redirect(url_for("pos"))
                pid = prod.id
            else:
                pid = int(request.form.get("product_id"))
                prod = s.get(Product, pid)
            qty = int(request.form.get("qty", 1))
            line_discount = Decimal(request.form.get("line_discount") or "0")
            if not prod:
                flash("Product not found", "warning"); return redirect(url_for("pos"))
            if qty <= 0:
                flash("Invalid quantity", "warning"); return redirect(url_for("pos"))
            if prod.stock_qty < qty:
                flash(f"Not enough stock for {prod.name}", "warning"); return redirect(url_for("pos"))
            cart.append({"product_id": pid, "qty": qty, "discount": float(line_discount)})
            session["cart"] = cart
            flash("Item added", "success")
            return redirect(url_for("pos"))

        # Checkout
        phone = (request.form.get("phone") or "").strip()
        name = (request.form.get("name") or "").strip() or "Customer"
        order_discount_val = Decimal(request.form.get("order_discount") or "0")
        if not cart:
            flash("Cart is empty", "warning"); return redirect(url_for("pos"))
        cust = s.query(Customer).filter_by(phone=phone).first()
        if not cust:
            cust = Customer(name=name, phone=phone); s.add(cust); s.flush()

        order = Order(customer=cust, staff_id=session["user_id"], order_discount=order_discount_val)
        s.add(order); s.flush()
        subtotal = Decimal("0.00")
        tax_total = Decimal("0.00")
        profit_total = Decimal("0.00")

        for line in cart:
            prod = s.get(Product, int(line["product_id"]))
            qty = int(line["qty"])
            if prod.stock_qty < qty:
                s.rollback(); flash(f"Stock changed for {prod.name}", "warning"); return redirect(url_for("pos"))
            item = OrderItem(order=order, product=prod, quantity=qty,
                             unit_price=prod.price, gst_rate=prod.gst_rate,
                             discount_pct=Decimal(str(line.get("discount", 0))))
            s.add(item)
            # update stock & log
            prod.stock_qty -= qty
            s.add(InventoryLog(product=prod, change_qty=-qty, reason="sale", staff_id=session["user_id"], note=f"Order #{order.id}"))
            # totals per line use new helpers
            subtotal += item.line_taxable()
            tax_total += item.line_tax()
            profit_total += item.line_profit()

        # apply order-level discount percentage on subtotal (reduces taxable base proportionally)
        order.subtotal = money(subtotal)
        if order.order_discount and order.order_discount > 0:
            od_pct = Decimal(order.order_discount) / Decimal("100")
            # discount amount on subtotal
            order_discount_amount = money(order.subtotal * od_pct)
            # reduce subtotal and proportionally reduce tax and profit
            order.subtotal = money(order.subtotal - order_discount_amount)
            # tax and profit are recomputed proportionally (simple approach)
            # compute proportion factor
            # if subtotal before was 0, skip
            # Apply proportional reduction:
            if subtotal > 0:
                factor = (subtotal - order_discount_amount) / subtotal
                tax_total = money(tax_total * factor)
                profit_total = money(profit_total * factor)
        order.tax_total = money(tax_total)
        order.grand_total = money(order.subtotal + order.tax_total)
        order.profit_amount = money(profit_total)
        s.commit()
        session["cart"] = []
        session.pop("order_discount", None)
        flash(f"Order #{order.id} created", "success")
        return redirect(url_for("invoice", order_id=order.id))

    # build cart view
    cart_view = []
    for line in cart:
        p = s.get(Product, int(line["product_id"]))
        if p:
            cart_view.append({"product": p, "qty": int(line["qty"]), "discount": Decimal(str(line.get("discount",0)))})
    return render_template("pos.html", products=products, cart=cart_view, order_discount=order_discount)

# ---------------------------------------------------------------------
# Orders, invoice, receipt, reports
# ---------------------------------------------------------------------
@app.route("/orders")
def orders():
    r = require_login("staff")
    if r: return r
    s = db()
    orders = s.query(Order).order_by(Order.created_at.desc()).limit(200).all()
    return render_template("orders.html", orders=orders)

@app.route("/invoice/<int:order_id>")
def invoice(order_id: int):
    r = require_login("staff")
    if r: return r
    s = db()
    o = s.get(Order, order_id)
    if not o: abort(404)
    # make item helper methods available in template via attribute access
    return render_template("invoice.html", o=o)

@app.route("/receipt/<int:order_id>")
def receipt(order_id: int):
    r = require_login("staff")
    if r: return r
    s = db()
    o = s.get(Order, order_id)
    if not o: abort(404)
    return render_template("receipt.html", o=o)

@app.route("/reports/daily")
def reports_daily():
    r = require_login("staff")
    if r: return r
    req = request.args.get("date")
    d = datetime.strptime(req,"%Y-%m-%d").date() if req else today()
    s = db()
    start = datetime.combine(d, time.min)
    end = datetime.combine(d, time.max)
    sales, tax, profit = s.query(
        func.coalesce(func.sum(Order.subtotal),0),
        func.coalesce(func.sum(Order.tax_total),0),
        func.coalesce(func.sum(Order.profit_amount),0)
    ).filter(Order.created_at.between(start,end)).first()
    month_val = f"{d.year}-{d.month:02d}"
    return render_template("reports.html", req_date=d.strftime("%Y-%m-%d"),
                           sales=f"{(sales or 0):.2f}", tax=f"{(tax or 0):.2f}",
                           profit=f"{(profit or 0):.2f}", month=month_val)

@app.route("/reports/monthly")
def reports_monthly():
    r = require_login("staff")
    if r: return r
    month = request.args.get("month")
    if month:
        y,m = map(int, month.split("-"))
    else:
        now = datetime.now(); y,m = now.year, now.month
    s = db()
    start = datetime(y,m,1)
    end = datetime(y + (m==12), (m%12)+1, 1) - timedelta(seconds=1)
    sub, tax, total, profit = s.query(
        func.coalesce(func.sum(Order.subtotal),0),
        func.coalesce(func.sum(Order.tax_total),0),
        func.coalesce(func.sum(Order.grand_total),0),
        func.coalesce(func.sum(Order.profit_amount),0)
    ).filter(Order.created_at.between(start,end)).first()
    html = TEMPLATES["base.html"].replace("{% block content %}{% endblock %}", f"""
    {{% block content %}}
    <h2>Monthly Report: {y}-{m:02d}</h2>
    <table>
      <tr><th>Subtotal</th><td>₹ { (sub or 0):.2f }</td></tr>
      <tr><th>Tax</th><td>₹ { (tax or 0):.2f }</td></tr>
      <tr><th>Grand Total</th><td>₹ { (total or 0):.2f }</td></tr>
      <tr><th>Profit</th><td>₹ { (profit or 0):.2f }</td></tr>
    </table>
    <p class="no-print"><a href="{{{{ url_for('reports_daily') }}}}">Back</a></p>
    {{% endblock %}}
    """)
    return html

# ---------------------------------------------------------------------
# Helpers and app start
# ---------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.jinja_env.globals["csrf_token"] = get_csrf_token
    app.run(debug=True)
