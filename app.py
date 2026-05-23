import os
import sys
import json
import time
import signal
import shutil
import psutil
import threading
import subprocess
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for, 
    flash, jsonify, session, send_file, Response
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, 
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ==================== DYNAMIC ENVIRONMENT DETECTION ====================
HOST = '0.0.0.0'
PORT = int(os.environ.get('SERVER_PORT', 5000))
EXTERNAL_HOST = os.environ.get('SERVER_HOST', 'localhost')
EXTERNAL_DOMAIN = os.environ.get('SERVER_DOMAIN', f'{EXTERNAL_HOST}:{PORT}')

if os.environ.get('PTERODACTYL_SERVER_ID'):
    SERVER_ALLOCATION = os.environ.get('SERVER_ALLOCATION', '')
    if SERVER_ALLOCATION:
        try:
            allocation = json.loads(SERVER_ALLOCATION)
            PORT = int(allocation.get('port', PORT))
            EXTERNAL_DOMAIN = f"{allocation.get('ip', EXTERNAL_HOST)}:{PORT}"
        except:
            pass

FULL_ADDRESS = f"http://{EXTERNAL_DOMAIN}"

# ==================== FLASK APP INITIALIZATION ====================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'ragnarhost-secret-key-jagwar-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///ragnarhost.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'servers')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

# Ensure servers directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ==================== DATABASE SETUP ====================
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ==================== DATABASE MODELS ====================
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    servers = db.relationship('Server', backref='owner', lazy=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Server(db.Model):
    __tablename__ = 'servers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, default='')
    language = db.Column(db.String(20), default='python')
    server_port = db.Column(db.Integer, unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    is_suspended = db.Column(db.Boolean, default=False)
    process_pid = db.Column(db.Integer, default=None)
    main_file = db.Column(db.String(100), default='main.py')
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_started = db.Column(db.DateTime, default=None)
    
    @property
    def server_path(self):
        return os.path.join(app.config['UPLOAD_FOLDER'], f'server_{self.id}')
    
    @property
    def is_running(self):
        if self.process_pid:
            try:
                process = psutil.Process(self.process_pid)
                return process.is_running()
            except:
                return False
        return False

# ==================== LOGIN MANAGER ====================
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==================== PROCESS MANAGER ====================
class ProcessManager:
    """Manages subprocesses for user servers"""
    running_processes = {}
    process_outputs = {}
    process_lock = threading.Lock()
    
    @classmethod
    def start_server(cls, server):
        """Start a server subprocess"""
        with cls.process_lock:
            if server.id in cls.running_processes:
                cls.stop_server(server)
            
            server_path = server.server_path
            main_file = os.path.join(server_path, server.main_file)
            
            if not os.path.exists(main_file):
                # Create default main.py if it doesn't exist
                os.makedirs(server_path, exist_ok=True)
                default_code = f"""from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os

PORT = {server.server_port}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {{"status": "running", "server": "{server.name}", "message": "RAGNARHOST Server Active"}}
            self.wfile.write(json.dumps(response).encode())
        elif self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {{"health": "ok", "timestamp": str(__import__('datetime').datetime.now())}}
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        response = {{"received": post_data.decode(), "status": "processed"}}
        self.wfile.write(json.dumps(response).encode())

print(f"Server starting on port {{PORT}}")
server = HTTPServer(('0.0.0.0', PORT), Handler)
print(f"Server running on port {{PORT}}")
server.serve_forever()
"""
                with open(main_file, 'w') as f:
                    f.write(default_code)
            
            # Install requirements if exists
            req_file = os.path.join(server_path, 'requirements.txt')
            if os.path.exists(req_file):
                try:
                    subprocess.run(
                        [sys.executable, '-m', 'pip', 'install', '-r', req_file, '--quiet'],
                        cwd=server_path,
                        timeout=60
                    )
                except Exception as e:
                    print(f"Error installing requirements: {e}")
            
            # Start the process
            try:
                process = subprocess.Popen(
                    [sys.executable, main_file],
                    cwd=server_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env={**os.environ, 'PORT': str(server.server_port), 'SERVER_PORT': str(server.server_port)}
                )
                
                cls.running_processes[server.id] = process
                cls.process_outputs[server.id] = []
                server.process_pid = process.pid
                server.last_started = datetime.utcnow()
                db.session.commit()
                
                # Start output reader thread
                reader_thread = threading.Thread(
                    target=cls._read_output,
                    args=(server.id, process),
                    daemon=True
                )
                reader_thread.start()
                
                return True
            except Exception as e:
                print(f"Error starting server: {e}")
                return False
    
    @classmethod
    def stop_server(cls, server):
        """Stop a server subprocess"""
        with cls.process_lock:
            if server.id in cls.running_processes:
                process = cls.running_processes[server.id]
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except:
                    try:
                        process.kill()
                    except:
                        pass
                del cls.running_processes[server.id]
                if server.id in cls.process_outputs:
                    del cls.process_outputs[server.id]
                server.process_pid = None
                db.session.commit()
                return True
        return False
    
    @classmethod
    def _read_output(cls, server_id, process):
        """Read process output in real-time"""
        try:
            for line in iter(process.stdout.readline, ''):
                if line:
                    cls.process_outputs[server_id].append(line.strip())
                    # Keep only last 1000 lines
                    if len(cls.process_outputs[server_id]) > 1000:
                        cls.process_outputs[server_id] = cls.process_outputs[server_id][-500:]
        except:
            pass
    
    @classmethod
    def get_output(cls, server_id, lines=100):
        """Get recent process output"""
        if server_id in cls.process_outputs:
            return cls.process_outputs[server_id][-lines:]
        return []

# ==================== SYSTEM METRICS ====================
def get_system_metrics():
    """Get real-time system metrics"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        return {
            'cpu': cpu_percent,
            'memory_total': memory.total,
            'memory_used': memory.used,
            'memory_percent': memory.percent,
            'disk_total': disk.total,
            'disk_used': disk.used,
            'disk_percent': disk.percent,
            'active_connections': len(ProcessManager.running_processes)
        }
    except:
        return {
            'cpu': 0, 'memory_percent': 0, 'disk_percent': 0,
            'memory_total': 0, 'memory_used': 0, 'disk_total': 0, 'disk_used': 0,
            'active_connections': 0
        }

# ==================== DECORATORS ====================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Access denied. Admin privileges required.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ==================== ROUTES ====================

@app.route('/')
def index():
    stats = {
        'total_servers': Server.query.count(),
        'active_users': User.query.filter_by(is_banned=False).count(),
        'running_servers': len(ProcessManager.running_processes),
        'total_users': User.query.count()
    }
    return render_template('index.html', stats=stats, full_address=FULL_ADDRESS)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not username or not email or not password:
            flash('All fields are required.', 'error')
            return redirect(url_for('register'))
        
        if len(username) < 3 or len(username) > 30:
            flash('Username must be between 3 and 30 characters.', 'error')
            return redirect(url_for('register'))
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('register'))
        
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('register'))
        
        if User.query.filter_by(username=username).first():
            flash('Username already exists.', 'error')
            return redirect(url_for('register'))
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Username and password are required.', 'error')
            return redirect(url_for('login'))
        
        user = User.query.filter_by(username=username).first()
        
        if not user or not user.check_password(password):
            flash('Invalid username or password.', 'error')
            return redirect(url_for('login'))
        
        if user.is_banned:
            flash('Your account has been suspended. Contact admin.', 'error')
            return redirect(url_for('login'))
        
        login_user(user)
        flash('Login successful!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.is_banned:
        flash('Your account has been suspended.', 'error')
        return redirect(url_for('logout'))
    
    servers = Server.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', 
                         servers=servers, 
                         full_address=FULL_ADDRESS,
                         external_host=EXTERNAL_HOST,
                         external_domain=EXTERNAL_DOMAIN)

@app.route('/admin')
@admin_required
def admin_panel():
    users = User.query.all()
    servers = Server.query.all()
    metrics = get_system_metrics()
    return render_template('admin.html', 
                         users=users, 
                         servers=servers, 
                         metrics=metrics,
                         full_address=FULL_ADDRESS)

@app.route('/api/metrics')
def api_metrics():
    return jsonify(get_system_metrics())

@app.route('/api/server/<int:server_id>/output')
@login_required
def api_server_output(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    lines = request.args.get('lines', 100, type=int)
    output = ProcessManager.get_output(server_id, lines)
    return jsonify({
        'output': output,
        'is_running': server.is_running,
        'pid': server.process_pid
    })

@app.route('/api/server/<int:server_id>/start', methods=['POST'])
@login_required
def api_server_start(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    if server.is_suspended:
        return jsonify({'error': 'Server is suspended'}), 400
    
    success = ProcessManager.start_server(server)
    if success:
        return jsonify({
            'success': True,
            'message': 'Server started successfully',
            'port': server.server_port,
            'address': f'http://{EXTERNAL_DOMAIN}'
        })
    return jsonify({'error': 'Failed to start server'}), 500

@app.route('/api/server/<int:server_id>/stop', methods=['POST'])
@login_required
def api_server_stop(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    success = ProcessManager.stop_server(server)
    return jsonify({'success': success})

@app.route('/api/server/create', methods=['POST'])
@login_required
def api_server_create():
    if current_user.is_banned:
        return jsonify({'error': 'Account suspended'}), 403
    
    # Check server limit for non-premium users
    if not current_user.is_premium:
        server_count = Server.query.filter_by(user_id=current_user.id).count()
        if server_count >= 1:
            return jsonify({'error': 'Free users can only create 1 server. Upgrade to premium for unlimited servers.'}), 403
    
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    language = request.form.get('language', 'python')
    
    if not name:
        return jsonify({'error': 'Server name is required'}), 400
    
    # Find available port
    existing_ports = [s.server_port for s in Server.query.all()]
    available_port = PORT + 1
    while available_port in existing_ports or available_port == PORT:
        available_port += 1
    
    server = Server(
        name=name,
        description=description,
        language=language,
        server_port=available_port,
        user_id=current_user.id
    )
    db.session.add(server)
    db.session.commit()
    
    # Create server directory
    os.makedirs(server.server_path, exist_ok=True)
    
    return jsonify({
        'success': True,
        'server_id': server.id,
        'port': available_port,
        'address': f'http://{EXTERNAL_DOMAIN}'
    })

@app.route('/api/server/<int:server_id>/delete', methods=['POST'])
@login_required
def api_server_delete(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Stop server if running
    ProcessManager.stop_server(server)
    
    # Delete server files
    try:
        if os.path.exists(server.server_path):
            shutil.rmtree(server.server_path)
    except:
        pass
    
    db.session.delete(server)
    db.session.commit()
    
    return jsonify({'success': True})

@app.route('/api/server/<int:server_id>/files', methods=['GET'])
@login_required
def api_server_files_list(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    path = request.args.get('path', '')
    full_path = os.path.join(server.server_path, path)
    
    # Security: prevent directory traversal
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    files = []
    try:
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            rel_path = os.path.join(path, item) if path else item
            files.append({
                'name': item,
                'path': rel_path,
                'is_dir': os.path.isdir(item_path),
                'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0,
                'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).isoformat()
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
    return jsonify({'files': files, 'current_path': path})

@app.route('/api/server/<int:server_id>/file/read', methods=['GET'])
@login_required
def api_server_file_read(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    file_path = request.args.get('path', '')
    full_path = os.path.join(server.server_path, file_path)
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    if not os.path.exists(full_path) or os.path.isdir(full_path):
        return jsonify({'error': 'File not found'}), 404
    
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return jsonify({'content': content, 'path': file_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/file/write', methods=['POST'])
@login_required
def api_server_file_write(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    file_path = request.json.get('path', '')
    content = request.json.get('content', '')
    
    full_path = os.path.join(server.server_path, file_path)
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/file/delete', methods=['POST'])
@login_required
def api_server_file_delete(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    file_path = request.json.get('path', '')
    full_path = os.path.join(server.server_path, file_path)
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    try:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/file/rename', methods=['POST'])
@login_required
def api_server_file_rename(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    old_path = request.json.get('old_path', '')
    new_path = request.json.get('new_path', '')
    
    full_old_path = os.path.join(server.server_path, old_path)
    full_new_path = os.path.join(server.server_path, new_path)
    
    if not os.path.realpath(full_old_path).startswith(os.path.realpath(server.server_path)) or \
       not os.path.realpath(full_new_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    try:
        os.makedirs(os.path.dirname(full_new_path), exist_ok=True)
        os.rename(full_old_path, full_new_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/file/upload', methods=['POST'])
@login_required
def api_server_file_upload(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    upload_path = request.form.get('path', '')
    filename = secure_filename(file.filename)
    full_path = os.path.join(server.server_path, upload_path, filename)
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file.save(full_path)
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/directory/create', methods=['POST'])
@login_required
def api_server_directory_create(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    dir_path = request.json.get('path', '')
    full_path = os.path.join(server.server_path, dir_path)
    
    if not os.path.realpath(full_path).startswith(os.path.realpath(server.server_path)):
        return jsonify({'error': 'Invalid path'}), 403
    
    try:
        os.makedirs(full_path, exist_ok=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/server/<int:server_id>/requirements', methods=['GET'])
@login_required
def api_server_requirements(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    req_file = os.path.join(server.server_path, 'requirements.txt')
    requirements = []
    
    if os.path.exists(req_file):
        with open(req_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Parse package name and version
                    parts = line.split('==')
                    if len(parts) == 2:
                        requirements.append({
                            'name': parts[0].strip(),
                            'version': parts[1].strip()
                        })
                    else:
                        requirements.append({
                            'name': line.strip(),
                            'version': 'latest'
                        })
    
    return jsonify({'requirements': requirements})

@app.route('/api/server/<int:server_id>/main-file', methods=['POST'])
@login_required
def api_server_set_main_file(server_id):
    server = Server.query.get_or_404(server_id)
    if server.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'error': 'Unauthorized'}), 403
    
    main_file = request.json.get('main_file', 'main.py')
    
    # Validate file exists
    full_path = os.path.join(server.server_path, main_file)
    if not os.path.exists(full_path):
        return jsonify({'error': 'File does not exist'}), 400
    
    server.main_file = main_file
    db.session.commit()
    
    return jsonify({'success': True, 'main_file': main_file})

@app.route('/api/admin/user/<int:user_id>/toggle-premium', methods=['POST'])
@admin_required
def api_admin_toggle_premium(user_id):
    user = User.query.get_or_404(user_id)
    user.is_premium = not user.is_premium
    db.session.commit()
    return jsonify({'success': True, 'is_premium': user.is_premium})

@app.route('/api/admin/user/<int:user_id>/toggle-ban', methods=['POST'])
@admin_required
def api_admin_toggle_ban(user_id):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        return jsonify({'error': 'Cannot ban admin users'}), 400
    user.is_banned = not user.is_banned
    db.session.commit()
    return jsonify({'success': True, 'is_banned': user.is_banned})

@app.route('/api/admin/server/<int:server_id>/toggle-suspend', methods=['POST'])
@admin_required
def api_admin_toggle_suspend(server_id):
    server = Server.query.get_or_404(server_id)
    server.is_suspended = not server.is_suspended
    if server.is_suspended:
        ProcessManager.stop_server(server)
    db.session.commit()
    return jsonify({'success': True, 'is_suspended': server.is_suspended})

# ==================== APPLICATION STARTUP ====================
def init_db():
    """Initialize database and create admin user if not exists"""
    with app.app_context():
        db.create_all()
        
        # Create admin user if not exists
        admin = User.query.filter_by(username='RAGNARHOST').first()
        if not admin:
            admin = User(
                username='RAGNARHOST',
                email='admin@ragnarhost.com',
                is_admin=True,
                is_premium=True
            )
            admin.set_password('RAGNAR1234')
            db.session.add(admin)
            db.session.commit()
            print("Admin user created: RAGNARHOST / RAGNAR1234")

if __name__ == '__main__':
    init_db()
    print(f"\n{'='*60}")
    print(f"  RAGNARHOST - Web Hosting Platform")
    print(f"  Developed entirely by JAGWAR. All rights reserved.")
    print(f"  Running on: {FULL_ADDRESS}")
    print(f"  Environment Port: {PORT}")
    print(f"{'='*60}\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
