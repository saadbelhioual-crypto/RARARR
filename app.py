import os
import sys
import json
import shutil
import subprocess
import threading
import time
import random
import string
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, 
    flash, jsonify, session, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, 
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# -------------------------------------------------------------------
# Dynamic Port & Host Configuration
# -------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'RAGNARHOST-ULTRA-SECURE-KEY-2024')
PORT = int(os.environ.get('SERVER_PORT', 5000))
HOST = '0.0.0.0'
EXTERNAL_HOST = os.environ.get('EXTERNAL_HOST', f'http://fi10.bot-hosting.net:{PORT}')

# Database
BASE_DIR = Path(__file__).resolve().parent
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{BASE_DIR}/ragnarhost.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Server storage root
SERVERS_ROOT = BASE_DIR / 'user_servers'
SERVERS_ROOT.mkdir(exist_ok=True)

# -------------------------------------------------------------------
# Database Models
# -------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    servers = db.relationship('Server', backref='owner', lazy=True)

class Server(db.Model):
    __tablename__ = 'servers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, default='')
    language = db.Column(db.String(50), default='Python')
    port = db.Column(db.Integer, unique=True, nullable=False)
    node = db.Column(db.String(100), default='fi10.bot-hosting.net')
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='stopped')  # stopped, running, suspended
    main_file = db.Column(db.String(200), default='app.py')
    cpu_usage = db.Column(db.Float, default=0.0)
    ram_usage = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    suspended = db.Column(db.Boolean, default=False)

class Subscription(db.Model):
    __tablename__ = 'subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    tier = db.Column(db.String(20), default='free')  # free, premium
    active = db.Column(db.Boolean, default=True)

# -------------------------------------------------------------------
# Admin Required Decorator
# -------------------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def allocate_port():
    """Allocate a unique port dynamically."""
    while True:
        port = random.randint(30000, 60000)
        if not Server.query.filter_by(port=port).first():
            return port

def get_server_address(server):
    """Return full external address for a server."""
    return f'http://{server.node}:{server.port}'

def get_server_dir(server_id):
    """Return server directory path and ensure it exists."""
    server_dir = SERVERS_ROOT / f'server_{server_id}'
    server_dir.mkdir(parents=True, exist_ok=True)
    return server_dir

def init_server_files(server_dir):
    """Create initial files for a new server."""
    main_py = server_dir / 'app.py'
    if not main_py.exists():
        with open(main_py, 'w') as f:
            f.write("""from flask import Flask
import os

app = Flask(__name__)
PORT = int(os.environ.get('PORT', 5000))

@app.route('/')
def home():
    return '<h1>Welcome to RAGNARHOST Server!</h1><p>Your Flask app is running.</p>'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
""")
    requirements = server_dir / 'requirements.txt'
    if not requirements.exists():
        with open(requirements, 'w') as f:
            f.write("flask\n")

def parse_requirements(server_dir):
    """Parse requirements.txt and return list of libraries."""
    req_file = server_dir / 'requirements.txt'
    if not req_file.exists():
        return []
    libs = []
    with open(req_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                libs.append(line.split('==')[0].split('>=')[0].strip())
    return libs

# -------------------------------------------------------------------
# Flask-Login user loader
# -------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# -------------------------------------------------------------------
# Routes - Landing
# -------------------------------------------------------------------
@app.route('/')
def index():
    total_servers = Server.query.count()
    active_users = User.query.count()
    node_status = 'Operational'  # Could be dynamic
    return render_template('index.html', 
                           total_servers=total_servers,
                           active_users=active_users,
                           node_status=node_status)

# -------------------------------------------------------------------
# Routes - Authentication
# -------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Login successful! Welcome back.', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not username or not password:
            flash('All fields are required.', 'danger')
        elif password != confirm:
            flash('Passwords do not match.', 'danger')
        elif User.query.filter_by(username=username).first():
            flash('Username already exists.', 'danger')
        else:
            user = User(
                username=username,
                password_hash=generate_password_hash(password),
                is_premium=False,
                is_admin=(username == 'RAGNARHOST')
            )
            db.session.add(user)
            db.session.commit()
            # Create subscription
            sub = Subscription(user_id=user.id, tier='free', active=True)
            db.session.add(sub)
            db.session.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# -------------------------------------------------------------------
# Routes - Dashboard
# -------------------------------------------------------------------
@app.route('/dashboard')
@login_required
def dashboard():
    servers = Server.query.filter_by(user_id=current_user.id).all()
    server_limit = 999 if current_user.is_premium else 1
    return render_template('dashboard.html', 
                           servers=servers, 
                           server_limit=server_limit,
                           user=current_user)

# -------------------------------------------------------------------
# Routes - Server Management
# -------------------------------------------------------------------
@app.route('/server/create', methods=['POST'])
@login_required
def create_server():
    # Check limits
    server_count = Server.query.filter_by(user_id=current_user.id).count()
    server_limit = 999 if current_user.is_premium else 1
    if server_count >= server_limit:
        flash('Server limit reached. Upgrade to Premium for unlimited servers.', 'warning')
        return redirect(url_for('dashboard'))
    
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    if not name:
        flash('Server name is required.', 'danger')
        return redirect(url_for('dashboard'))
    
    port = allocate_port()
    server = Server(
        name=name,
        description=description,
        language='Python',
        port=port,
        node='fi10.bot-hosting.net',
        user_id=current_user.id,
        status='stopped'
    )
    db.session.add(server)
    db.session.commit()
    
    # Initialize server files
    server_dir = get_server_dir(server.id)
    init_server_files(server_dir)
    
    flash(f'Server "{name}" created! Address: {get_server_address(server)}', 'success')
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/start', methods=['POST'])
@login_required
def start_server(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    server.status = 'running'
    server.cpu_usage = random.uniform(5, 45)
    server.ram_usage = random.uniform(30, 80)
    db.session.commit()
    flash(f'Server {server.name} started.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/stop', methods=['POST'])
@login_required
def stop_server(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    server.status = 'stopped'
    server.cpu_usage = 0.0
    server.ram_usage = 0.0
    db.session.commit()
    flash(f'Server {server.name} stopped.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/delete', methods=['POST'])
@login_required
def delete_server(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    # Remove files
    server_dir = get_server_dir(server.id)
    if server_dir.exists():
        shutil.rmtree(server_dir)
    db.session.delete(server)
    db.session.commit()
    flash('Server deleted permanently.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/set_main', methods=['POST'])
@login_required
def set_main_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    main_file = request.form.get('main_file', 'app.py').strip()
    server.main_file = main_file
    db.session.commit()
    flash(f'Main file set to {main_file}.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/server/<int:server_id>/console')
@login_required
def server_console(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    # Simulate console output
    lines = [
        f"[RAGNARHOST] Booting container for {server.name}...",
        f"[SYSTEM] Allocated port: {server.port}",
        f"[ENV] Python 3.11 detected",
        f"[ENV] Installing dependencies from requirements.txt...",
        f"[ENV] Dependencies installed successfully.",
        f"[APP] Starting {server.main_file}...",
        f"[APP] Server running at {get_server_address(server)}",
        f"[STATUS] CPU: {server.cpu_usage:.1f}% | RAM: {server.ram_usage:.1f} MB",
    ]
    return jsonify({'lines': lines, 'cpu': server.cpu_usage, 'ram': server.ram_usage})

# -------------------------------------------------------------------
# Routes - File Manager
# -------------------------------------------------------------------
@app.route('/server/<int:server_id>/files', methods=['GET'])
@login_required
def list_files(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    server_dir = get_server_dir(server.id)
    files = []
    for item in sorted(server_dir.iterdir()):
        files.append({
            'name': item.name,
            'type': 'directory' if item.is_dir() else 'file',
            'size': item.stat().st_size if item.is_file() else 0
        })
    return jsonify({'files': files})

@app.route('/server/<int:server_id>/file/read', methods=['GET'])
@login_required
def read_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    filename = request.args.get('filename', '')
    if not filename or '..' in filename or filename.startswith('/'):
        abort(400)
    server_dir = get_server_dir(server.id)
    filepath = server_dir / filename
    if not filepath.exists() or not filepath.is_file():
        abort(404)
    with open(filepath, 'r') as f:
        content = f.read()
    return jsonify({'content': content, 'filename': filename})

@app.route('/server/<int:server_id>/file/write', methods=['POST'])
@login_required
def write_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    data = request.get_json()
    filename = data.get('filename', '').strip()
    content = data.get('content', '')
    if not filename or '..' in filename or filename.startswith('/'):
        abort(400)
    server_dir = get_server_dir(server.id)
    filepath = server_dir / filename
    with open(filepath, 'w') as f:
        f.write(content)
    return jsonify({'status': 'saved'})

@app.route('/server/<int:server_id>/file/delete', methods=['POST'])
@login_required
def delete_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    data = request.get_json()
    filename = data.get('filename', '').strip()
    if not filename or '..' in filename or filename.startswith('/'):
        abort(400)
    server_dir = get_server_dir(server.id)
    filepath = server_dir / filename
    if filepath.exists():
        if filepath.is_dir():
            shutil.rmtree(filepath)
        else:
            filepath.unlink()
    return jsonify({'status': 'deleted'})

@app.route('/server/<int:server_id>/file/rename', methods=['POST'])
@login_required
def rename_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    data = request.get_json()
    old = data.get('old_name', '').strip()
    new = data.get('new_name', '').strip()
    if not old or not new or '..' in old or '..' in new:
        abort(400)
    server_dir = get_server_dir(server.id)
    old_path = server_dir / old
    new_path = server_dir / new
    if old_path.exists():
        old_path.rename(new_path)
    return jsonify({'status': 'renamed'})

@app.route('/server/<int:server_id>/file/upload', methods=['POST'])
@login_required
def upload_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    if 'file' not in request.files:
        abort(400)
    file = request.files['file']
    if file.filename == '':
        abort(400)
    filename = secure_filename(file.filename)
    server_dir = get_server_dir(server.id)
    file.save(server_dir / filename)
    return jsonify({'status': 'uploaded', 'filename': filename})

@app.route('/server/<int:server_id>/requirements', methods=['GET'])
@login_required
def get_requirements(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id:
        abort(403)
    server_dir = get_server_dir(server.id)
    libs = parse_requirements(server_dir)
    return jsonify({'libraries': libs})

# -------------------------------------------------------------------
# Routes - Admin
# -------------------------------------------------------------------
@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    users = User.query.all()
    servers = Server.query.all()
    return render_template('admin.html', users=users, servers=servers)

@app.route('/admin/toggle-premium/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def toggle_premium(user_id):
    user = User.query.get_or_404(user_id)
    user.is_premium = not user.is_premium
    sub = Subscription.query.filter_by(user_id=user.id).first()
    if sub:
        sub.tier = 'premium' if user.is_premium else 'free'
    db.session.commit()
    return jsonify({'status': 'ok', 'is_premium': user.is_premium})

@app.route('/admin/toggle-suspend/<int:server_id>', methods=['POST'])
@login_required
@admin_required
def toggle_suspend(server_id):
    server = Server.query.get_or_404(server_id)
    server.suspended = not server.suspended
    if server.suspended:
        server.status = 'stopped'
    db.session.commit()
    return jsonify({'status': 'ok', 'suspended': server.suspended})

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    # Delete all servers
    for server in user.servers:
        server_dir = get_server_dir(server.id)
        if server_dir.exists():
            shutil.rmtree(server_dir)
        db.session.delete(server)
    Subscription.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({'status': 'ok'})

# -------------------------------------------------------------------
# Error Handlers
# -------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    return render_template('login.html', error='Access denied.'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('index.html', error='Page not found.'), 404

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Ensure admin account exists
        admin_user = User.query.filter_by(username='RAGNARHOST').first()
        if not admin_user:
            admin_user = User(
                username='RAGNARHOST',
                password_hash=generate_password_hash('RAGNAR1234'),
                is_admin=True,
                is_premium=True
            )
            db.session.add(admin_user)
            db.session.commit()
            sub = Subscription(user_id=admin_user.id, tier='premium', active=True)
            db.session.add(sub)
            db.session.commit()
    print(f"🚀 RAGNARHOST running on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
