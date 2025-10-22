from flask import Flask, render_template, request, jsonify, session
from flask_cors import CORS
import time
import threading
from datetime import datetime
import os

from trading_engine import TradingEngine
from market_data import MarketDataFetcher
from ai_trader import AITrader
from database import Database
from services.risk_manager import RiskManager
from services.backtester import Backtester
from services.performance_analyzer import PerformanceAnalyzer
from utils.auth import hash_password, verify_password, login_required, get_current_user_id, set_current_user, clear_current_user
from utils.timezone import get_current_utc_time_str, get_current_beijing_time_str, utc_to_beijing
import config

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', '8XRxYeeymuCa2URjWcg6AIKPo')
CORS(app, supports_credentials=True)

db = Database(config.DATABASE_PATH)
market_fetcher = MarketDataFetcher()
risk_manager = RiskManager(db)
performance_analyzer = PerformanceAnalyzer(db)
backtester = None  # 延迟初始化
trading_engines = {}
auto_trading = config.AUTO_TRADING

# ============ Helper Functions ============

def _create_trading_engine(model_id: int) -> TradingEngine:
    """
    创建TradingEngine实例（DRY原则：消除重复代码）

    Args:
        model_id: 模型ID

    Returns:
        TradingEngine实例

    Raises:
        Exception: 模型不存在或创建失败
    """
    model = db.get_model(model_id)
    if not model:
        raise Exception(f"Model {model_id} not found")

    return TradingEngine(
        model_id=model_id,
        db=db,
        market_fetcher=market_fetcher,
        ai_trader=AITrader(
            api_key=model['api_key'],
            api_url=model['api_url'],
            model_name=model['model_name'],
            system_prompt=model.get('system_prompt')  # 传递自定义prompt
        )
    )

def _get_current_market_prices():
    """
    获取当前市场价格（DRY原则：消除重复代码）

    Returns:
        dict: {coin: price} 或空字典（如果所有API都失败且无缓存）
    """
    try:
        prices_data = market_fetcher.get_current_prices(config.SUPPORTED_COINS)
        if not prices_data:
            # 所有API都失败且无缓存，返回空字典
            print(f'[ERROR] No market data available - all APIs failed and no cache')
            return {}
        return {coin: prices_data[coin]['price'] for coin in prices_data if coin in prices_data}
    except Exception as e:
        print(f'[ERROR] Failed to get market prices: {e}')
        import traceback
        traceback.print_exc()
        return {}

def _check_model_ownership(model_id: int, user_id: int) -> bool:
    """
    检查模型是否属于当前用户

    Args:
        model_id: 模型ID
        user_id: 用户ID

    Returns:
        是否拥有该模型
    """
    model = db.get_model(model_id)
    if not model:
        return False
    return model.get('user_id') == user_id

@app.route('/image/<path:filename>')
def serve_image(filename):
    """提供image目录下的静态文件"""
    from flask import send_from_directory
    return send_from_directory('image', filename)

@app.route('/')
def index():
    """主页（公开）"""
    return render_template('home.html')

@app.route('/login')
def login_page():
    """登录页面"""
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    """仪表板（需要登录）"""
    return render_template('dashboard.html')

# ============ Authentication APIs ============

@app.route('/api/auth/register', methods=['POST'])
def register():
    """用户注册"""
    data = request.json
    username = data.get('username')
    password = data.get('password')
    email = data.get('email')

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    # 检查用户名是否已存在
    existing_user = db.get_user_by_username(username)
    if existing_user:
        return jsonify({'error': '用户名已存在'}), 400

    # 创建用户
    password_hash = hash_password(password)
    user_id = db.create_user(username, password_hash, email)

    # 自动登录
    set_current_user(user_id, username)

    return jsonify({
        'message': '注册成功',
        'user': {
            'id': user_id,
            'username': username,
            'email': email
        }
    })

@app.route('/api/auth/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.json
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    # 验证用户
    user = db.get_user_by_username(username)
    if not user or not verify_password(user['password_hash'], password):
        return jsonify({'error': '用户名或密码错误'}), 401

    # 设置Session
    set_current_user(user['id'], user['username'])

    return jsonify({
        'message': '登录成功',
        'user': {
            'id': user['id'],
            'username': user['username'],
            'email': user.get('email')
        }
    })

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """用户登出"""
    clear_current_user()
    return jsonify({'message': '登出成功'})

@app.route('/api/auth/linuxdo', methods=['GET'])
def linuxdo_oauth():
    """Linux DO OAuth 授权跳转"""
    import urllib.parse

    # 构建授权URL
    params = {
        'client_id': config.LINUXDO_CLIENT_ID,
        'redirect_uri': config.LINUXDO_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'user'  # 修正：使用正确的scope
    }

    auth_url = f"{config.LINUXDO_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # 重定向到Linux DO授权页面
    from flask import redirect
    return redirect(auth_url)

@app.route('/api/auth/callback', methods=['GET'])
def linuxdo_callback():
    """Linux DO OAuth 回调处理"""
    import requests

    # 获取授权码
    code = request.args.get('code')
    if not code:
        return jsonify({'error': '授权失败，未获取到授权码'}), 400

    try:
        # 1. 用授权码换取access_token
        token_data = {
            'client_id': config.LINUXDO_CLIENT_ID,
            'client_secret': config.LINUXDO_CLIENT_SECRET,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': config.LINUXDO_REDIRECT_URI
        }

        token_response = requests.post(config.LINUXDO_TOKEN_URL, data=token_data, timeout=10)
        token_response.raise_for_status()
        token_json = token_response.json()

        access_token = token_json.get('access_token')
        if not access_token:
            return jsonify({'error': 'OAuth授权失败，未获取到access_token'}), 400

        # 2. 用access_token获取用户信息
        headers = {'Authorization': f'Bearer {access_token}'}
        userinfo_response = requests.get(config.LINUXDO_USERINFO_URL, headers=headers, timeout=10)
        userinfo_response.raise_for_status()
        userinfo = userinfo_response.json()

        # 3. 验证trust_level
        trust_level = userinfo.get('trust_level', 0)
        if trust_level < config.LINUXDO_MIN_TRUST_LEVEL:
            return jsonify({
                'error': f'您的信任等级为{trust_level}，需要达到{config.LINUXDO_MIN_TRUST_LEVEL}级才能登录'
            }), 403

        # 4. 获取用户信息
        linuxdo_id = userinfo.get('id')
        username = userinfo.get('username')
        email = userinfo.get('email', '')

        if not linuxdo_id or not username:
            return jsonify({'error': 'OAuth授权失败，未获取到用户信息'}), 400

        # 5. 查找或创建用户
        # 使用linuxdo_id作为唯一标识
        linuxdo_username = f'linuxdo_{linuxdo_id}'
        user = db.get_user_by_username(linuxdo_username)

        if not user:
            # 创建新用户（Linux DO用户不需要密码）
            password_hash = hash_password(f'linuxdo_oauth_{linuxdo_id}')  # 随机密码
            user_id = db.create_user(linuxdo_username, password_hash, email)
        else:
            user_id = user['id']

        # 6. 设置Session
        set_current_user(user_id, linuxdo_username)

        # 7. 重定向到首页或控制台
        from flask import redirect
        return redirect('/dashboard')

    except requests.RequestException as e:
        print(f'[ERROR] Linux DO OAuth failed: {e}')
        return jsonify({'error': f'OAuth授权失败: {str(e)}'}), 500
    except Exception as e:
        print(f'[ERROR] Linux DO OAuth callback failed: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'登录失败: {str(e)}'}), 500

@app.route('/api/auth/me', methods=['GET'])
@app.route('/api/user/info', methods=['GET'])
def get_current_user():
    """获取当前登录用户信息"""
    user_id = get_current_user_id()
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401

    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # 转换created_at为东八区时间
    created_at = user.get('created_at')
    if created_at:
        created_at = utc_to_beijing(created_at)

    return jsonify({
        'id': user['id'],
        'username': user['username'],
        'email': user.get('email'),
        'created_at': created_at
    })

# ============ Model APIs ============

@app.route('/api/models', methods=['GET'])
@login_required
def get_models():
    """获取当前用户的模型列表"""
    user_id = get_current_user_id()
    models = db.get_all_models(user_id=user_id)
    return jsonify(models)

@app.route('/api/models/<int:model_id>', methods=['GET'])
@login_required
def get_model(model_id):
    """获取单个模型详情"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    model = db.get_model(model_id)
    if not model:
        return jsonify({'error': '模型不存在'}), 404

    return jsonify(model)

@app.route('/api/models', methods=['POST'])
@login_required
def add_model():
    """创建新模型（需要登录）"""
    user_id = get_current_user_id()
    data = request.json
    model_id = db.add_model(
        user_id=user_id,
        name=data['name'],
        api_key=data['api_key'],
        api_url=data['api_url'],
        model_name=data['model_name'],
        initial_capital=float(data.get('initial_capital', 10000)),
        system_prompt=data.get('system_prompt')  # 可选的自定义prompt
    )

    try:
        trading_engines[model_id] = _create_trading_engine(model_id)
        print(f"[INFO] Model {model_id} ({data['name']}) initialized")
    except Exception as e:
        print(f"[ERROR] Model {model_id} initialization failed: {e}")

    return jsonify({'id': model_id, 'message': 'Model added successfully'})

@app.route('/api/models/<int:model_id>', methods=['PUT'])
@login_required
def update_model(model_id):
    """更新模型（只允许更新system_prompt）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权操作此模型'}), 403

    try:
        data = request.get_json()

        # 只允许更新system_prompt
        if 'system_prompt' not in data:
            return jsonify({'error': '缺少system_prompt参数'}), 400

        # 检查是否有其他字段（防止F12修改参数漏洞）
        allowed_fields = {'system_prompt'}
        if not set(data.keys()).issubset(allowed_fields):
            return jsonify({'error': '只允许修改交易策略'}), 400

        system_prompt = data['system_prompt']

        # 更新数据库
        db.update_model_prompt(model_id, system_prompt)

        # 重新创建trading engine（使用新的prompt）
        if model_id in trading_engines:
            del trading_engines[model_id]
        trading_engines[model_id] = _create_trading_engine(model_id)

        print(f"[INFO] Model {model_id} system_prompt updated")
        return jsonify({'message': 'Model updated successfully'})
    except Exception as e:
        print(f"[ERROR] Update model {model_id} failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>', methods=['DELETE'])
@login_required
def delete_model(model_id):
    """删除模型（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权操作此模型'}), 403

    try:
        model = db.get_model(model_id)
        model_name = model['name'] if model else f"ID-{model_id}"

        db.delete_model(model_id)
        if model_id in trading_engines:
            del trading_engines[model_id]

        print(f"[INFO] Model {model_id} ({model_name}) deleted")
        return jsonify({'message': 'Model deleted successfully'})
    except Exception as e:
        print(f"[ERROR] Delete model {model_id} failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/portfolio', methods=['GET'])
@login_required
def get_portfolio(model_id):
    """获取投资组合（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    current_prices = _get_current_market_prices()

    portfolio = db.get_portfolio(model_id, current_prices)
    account_value = db.get_account_value_history(model_id, limit=100)

    return jsonify({
        'portfolio': portfolio,
        'account_value_history': account_value
    })

@app.route('/api/models/<int:model_id>/trades', methods=['GET'])
@login_required
def get_trades(model_id):
    """获取交易记录（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    limit = request.args.get('limit', 50, type=int)
    trades = db.get_trades(model_id, limit=limit)

    # 转换时间为东八区
    for trade in trades:
        if 'timestamp' in trade:
            trade['timestamp'] = utc_to_beijing(trade['timestamp'])

    return jsonify(trades)

@app.route('/api/models/<int:model_id>/conversations', methods=['GET'])
@login_required
def get_conversations(model_id):
    """获取AI对话记录（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    limit = request.args.get('limit', 20, type=int)
    conversations = db.get_conversations(model_id, limit=limit)

    # 过滤掉空响应（AI调用失败的记录）
    valid_conversations = []
    for conv in conversations:
        # 转换时间为东八区
        if 'timestamp' in conv:
            conv['timestamp'] = utc_to_beijing(conv['timestamp'])

        # 过滤掉空响应
        ai_response = conv.get('ai_response', '')
        if ai_response and ai_response.strip() not in ['{}', '']:
            valid_conversations.append(conv)

    return jsonify(valid_conversations)

@app.route('/api/models/<int:model_id>/risk', methods=['GET'])
@login_required
def get_risk_metrics(model_id):
    """获取风险指标（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    current_prices = _get_current_market_prices()
    portfolio = db.get_portfolio(model_id, current_prices)
    risk_metrics = risk_manager.get_risk_metrics(model_id, portfolio)
    return jsonify(risk_metrics)

@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    """运行回测"""
    global backtester

    data = request.json
    model_config = {
        'api_key': data.get('api_key'),
        'api_url': data.get('api_url'),
        'model_name': data.get('model_name')
    }
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    initial_capital = data.get('initial_capital', 10000)

    # 延迟初始化backtester
    if backtester is None:
        ai_trader = AITrader(
            api_key=model_config['api_key'],
            api_url=model_config['api_url'],
            model_name=model_config['model_name']
        )
        backtester = Backtester(db, market_fetcher, ai_trader)

    try:
        result = backtester.run_backtest(
            model_config, start_date, end_date, initial_capital
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/performance', methods=['GET'])
@login_required
def get_performance(model_id):
    """获取绩效分析（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权访问此模型'}), 403

    try:
        performance = performance_analyzer.analyze_performance(model_id)
        return jsonify(performance)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/analytics', methods=['GET'])
@login_required
def get_user_analytics():
    """获取当前用户所有模型的详细分析数据（Dashboard绩效分析页面）"""
    user_id = get_current_user_id()

    try:
        models = db.get_all_models(user_id=user_id)

        overall_stats = []
        advanced_analytics = []

        for model in models:
            model_id = model['id']

            # 获取最新账户价值
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT total_value FROM account_values
                WHERE model_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
            ''', (model_id,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                continue

            total_value = row['total_value']
            initial_capital = model['initial_capital']
            total_pnl = total_value - initial_capital
            return_pct = (total_pnl / initial_capital) * 100

            # 获取交易统计
            cursor.execute('''
                SELECT
                    COUNT(*) as trade_count,
                    SUM(CASE WHEN signal = 'buy_to_enter' OR signal = 'sell_to_enter' THEN price * quantity * 0.001 ELSE 0 END) as total_fees,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count,
                    MAX(pnl) as biggest_win,
                    MIN(pnl) as biggest_loss
                FROM trades
                WHERE model_id = ?
            ''', (model_id,))
            trade_stats = cursor.fetchone()
            conn.close()

            trade_count = trade_stats['trade_count'] or 0
            win_count = trade_stats['win_count'] or 0
            win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0
            total_fees = trade_stats['total_fees'] or 0
            biggest_win = trade_stats['biggest_win'] or 0
            biggest_loss = trade_stats['biggest_loss'] or 0

            # 获取详细绩效分析
            try:
                performance = performance_analyzer.analyze_performance(model_id)
                risk_metrics = performance.get('risk_metrics', {})
                trading_stats = performance.get('trading_stats', {})

                sharpe_ratio = risk_metrics.get('sharpe_ratio', 0)
                sortino_ratio = risk_metrics.get('sortino_ratio', 0)
                calmar_ratio = risk_metrics.get('calmar_ratio', 0)
                max_drawdown = risk_metrics.get('max_drawdown', 0)
                volatility = risk_metrics.get('volatility', 0)
                avg_win = trading_stats.get('avg_win', 0)
                avg_loss = trading_stats.get('avg_loss', 0)
                profit_factor = trading_stats.get('profit_factor', 0)
            except:
                sharpe_ratio = 0
                sortino_ratio = 0
                calmar_ratio = 0
                max_drawdown = 0
                volatility = 0
                avg_win = 0
                avg_loss = 0
                profit_factor = 0

            # Overall Stats数据
            overall_stats.append({
                'model_id': model_id,
                'model_name': model['name'],
                'return_pct': return_pct,
                'total_value': total_value,
                'total_pnl': total_pnl,
                'fees': total_fees,
                'win_rate': win_rate,
                'biggest_win': biggest_win,
                'biggest_loss': biggest_loss,
                'sharpe': sharpe_ratio,
                'trades': trade_count
            })

            # Advanced Analytics数据
            advanced_analytics.append({
                'model_id': model_id,
                'model_name': model['name'],
                'sharpe': sharpe_ratio,
                'sortino': sortino_ratio,
                'calmar': calmar_ratio,
                'max_drawdown': max_drawdown,
                'volatility': volatility,
                'avg_win': avg_win,
                'avg_loss': avg_loss,
                'profit_factor': profit_factor,
                'trades': trade_count
            })

        # 按收益率排序
        overall_stats.sort(key=lambda x: x['return_pct'], reverse=True)
        advanced_analytics.sort(key=lambda x: x['sharpe'], reverse=True)

        return jsonify({
            'overall_stats': overall_stats,
            'advanced_analytics': advanced_analytics
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/top-coins', methods=['GET'])
def get_top_coins():
    """获取顶部币种价格栏数据（公开API）"""
    try:
        prices = _get_current_market_prices()

        # 如果没有价格数据，返回错误
        if not prices:
            return jsonify({'error': 'Market data unavailable - all APIs failed and no cache'}), 503

        result = []
        for coin in config.SUPPORTED_COINS:
            price = prices.get(coin, 0)
            if price == 0:
                # 跳过没有价格的币种
                continue
            # 计算24小时涨跌幅（这里简化处理，实际应该从历史数据计算）
            change_24h = (hash(coin) % 20 - 10) / 100  # 模拟数据，实际应该从API获取
            result.append({
                'symbol': coin,
                'price': price,
                'change_24h': change_24h
            })

        if not result:
            return jsonify({'error': 'No market data available'}), 503

        return jsonify(result)
    except Exception as e:
        print(f'[ERROR] Failed to get top coins: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/total-stats', methods=['GET'])
def get_total_stats():
    """获取全平台总统计数据（公开API）"""
    try:
        conn = db.get_connection()
        cursor = conn.cursor()

        # 获取所有模型的最新总价值
        cursor.execute('''
            SELECT m.id, m.initial_capital, av.total_value
            FROM models m
            LEFT JOIN (
                SELECT model_id, total_value
                FROM account_values
                WHERE (model_id, timestamp) IN (
                    SELECT model_id, MAX(timestamp)
                    FROM account_values
                    GROUP BY model_id
                )
            ) av ON m.id = av.model_id
        ''')

        models = cursor.fetchall()
        conn.close()

        total_value = 0
        total_pnl = 0

        for model in models:
            if model['total_value']:
                total_value += model['total_value']
                total_pnl += (model['total_value'] - model['initial_capital'])

        return jsonify({
            'total_value': total_value,
            'total_pnl': total_pnl
        })
    except Exception as e:
        print(f'[ERROR] Failed to get total stats: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/detailed-leaderboard', methods=['GET'])
def get_detailed_leaderboard():
    """获取详细排行榜数据（公开API）"""
    try:
        conn = db.get_connection()
        cursor = conn.cursor()

        # 获取所有模型及其统计数据
        cursor.execute('''
            SELECT
                m.id,
                m.name,
                m.initial_capital,
                av.total_value,
                COUNT(DISTINCT t.id) as trade_count,
                SUM(CASE WHEN t.signal = 'buy_to_enter' OR t.signal = 'sell_to_enter' THEN t.price * t.quantity * 0.001 ELSE 0 END) as total_fees,
                SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END) as win_count,
                MAX(t.pnl) as biggest_win,
                MIN(t.pnl) as biggest_loss
            FROM models m
            LEFT JOIN (
                SELECT model_id, total_value
                FROM account_values
                WHERE (model_id, timestamp) IN (
                    SELECT model_id, MAX(timestamp)
                    FROM account_values
                    GROUP BY model_id
                )
            ) av ON m.id = av.model_id
            LEFT JOIN trades t ON m.id = t.model_id
            GROUP BY m.id
        ''')

        models = cursor.fetchall()
        conn.close()

        leaderboard = []
        for model in models:
            if not model['total_value']:
                continue

            total_value = model['total_value']
            initial_capital = model['initial_capital']
            total_pnl = total_value - initial_capital
            return_pct = (total_pnl / initial_capital) * 100

            trade_count = model['trade_count'] or 0
            win_count = model['win_count'] or 0
            win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0

            leaderboard.append({
                'id': model['id'],
                'name': model['name'],
                'total_value': total_value,
                'return_pct': return_pct,
                'total_pnl': total_pnl,
                'fees': model['total_fees'] or 0,
                'win_rate': win_rate,
                'biggest_win': model['biggest_win'] or 0,
                'biggest_loss': model['biggest_loss'] or 0,
                'sharpe': 0,  # 简化处理，实际需要计算
                'trades': trade_count
            })

        # 按收益率排序（从高到低）
        leaderboard.sort(key=lambda x: x['return_pct'], reverse=True)

        # 只返回前100名
        leaderboard = leaderboard[:100]

        return jsonify(leaderboard)
    except Exception as e:
        print(f'[ERROR] Failed to get detailed leaderboard: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/advanced-analytics', methods=['GET'])
def get_advanced_analytics():
    """获取高级分析数据（公开API）- 包含Sharpe、Sortino、Calmar等指标"""
    try:
        models = db.get_all_models()
        analytics = []

        for model in models:
            model_id = model['id']

            # 获取最新账户价值
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT total_value FROM account_values
                WHERE model_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
            ''', (model_id,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                continue

            total_value = row['total_value']

            # 获取交易统计
            cursor.execute('''
                SELECT COUNT(*) as trade_count
                FROM trades
                WHERE model_id = ?
            ''', (model_id,))
            trade_stats = cursor.fetchone()
            conn.close()

            trade_count = trade_stats['trade_count'] or 0

            # 获取详细绩效分析
            try:
                performance = performance_analyzer.analyze_performance(model_id)
                risk_metrics = performance.get('risk_metrics', {})
                trading_stats = performance.get('trading_stats', {})

                sharpe_ratio = risk_metrics.get('sharpe_ratio', 0)
                sortino_ratio = risk_metrics.get('sortino_ratio', 0)
                calmar_ratio = risk_metrics.get('calmar_ratio', 0)
                max_drawdown = risk_metrics.get('max_drawdown', 0)
                volatility = risk_metrics.get('volatility', 0)
                avg_win = trading_stats.get('avg_win', 0)
                avg_loss = trading_stats.get('avg_loss', 0)
                profit_factor = trading_stats.get('profit_factor', 0)
            except:
                sharpe_ratio = 0
                sortino_ratio = 0
                calmar_ratio = 0
                max_drawdown = 0
                volatility = 0
                avg_win = 0
                avg_loss = 0
                profit_factor = 0

            analytics.append({
                'id': model_id,
                'name': model['name'],
                'sharpe': sharpe_ratio,
                'sortino': sortino_ratio,
                'calmar': calmar_ratio,
                'max_drawdown': max_drawdown,
                'volatility': volatility,
                'avg_win': avg_win,
                'avg_loss': avg_loss,
                'profit_factor': profit_factor,
                'trades': trade_count
            })

        # 按夏普比率排序（从高到低）
        analytics.sort(key=lambda x: x['sharpe'], reverse=True)

        # 只返回前100名
        analytics = analytics[:100]

        return jsonify(analytics)
    except Exception as e:
        print(f'[ERROR] Failed to get advanced analytics: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/performance-chart', methods=['GET'])
def get_performance_chart():
    """获取收益曲线图数据（公开API）- 前6名模型 + BTC基准"""
    try:
        # 获取排行榜前6名
        models = db.get_all_models()
        leaderboard = []

        for model in models:
            # 从account_values表获取最新的total_value
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT total_value FROM account_values
                WHERE model_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
            ''', (model['id'],))
            row = cursor.fetchone()
            conn.close()

            if not row:
                continue

            total_value = row['total_value']
            total_return = ((total_value - model['initial_capital']) / model['initial_capital']) * 100

            leaderboard.append({
                'model_id': model['id'],
                'model_name': model['name'],
                'total_value': total_value,
                'total_return': total_return
            })

        # 按收益率排序，取前6名
        leaderboard.sort(key=lambda x: x['total_return'], reverse=True)
        top_models = leaderboard[:6]

        # 获取每个模型的历史账户价值数据
        result = []
        conn = db.get_connection()

        # 系统实际运行时间：2025-10-21 13:00:00（东八区）
        # 转换为UTC时间用于数据库查询
        from datetime import datetime, timedelta
        system_start_beijing = datetime(2025, 10, 21, 13, 0, 0)
        system_start_utc = system_start_beijing - timedelta(hours=8)
        system_start_utc_str = system_start_utc.strftime('%Y-%m-%d %H:%M:%S')

        for model in top_models:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp, total_value
                FROM account_values
                WHERE model_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            ''', (model['model_id'], system_start_utc_str))

            history = cursor.fetchall()
            print(f'[DEBUG] Model {model["model_name"]} (ID:{model["model_id"]}): {len(history)} data points after filtering (>= {system_start_utc_str})')

            data_points = []
            for row in history:
                # 将UTC时间转换为东八区时间
                beijing_time = utc_to_beijing(row['timestamp'])
                data_points.append({
                    'time': beijing_time,
                    'value': row['total_value']
                })

            result.append({
                'model_id': model['model_id'],
                'model_name': model['model_name'],
                'data': data_points
            })

        # 添加BTC基准线（从2025-10-21开始，假设初始10000美元买入BTC并持有）
        # 获取当前东八区时间用于显示
        current_time = get_current_beijing_time_str()

        # 获取BTC当前价格（只使用真实数据）
        try:
            btc_prices = market_fetcher.get_current_prices(['BTC'])
            if not btc_prices or 'BTC' not in btc_prices:
                print('[ERROR] Cannot get BTC price for baseline - skipping BTC baseline')
                btc_current_price = None
            else:
                btc_current_price = btc_prices.get('BTC', {}).get('price')
        except Exception as e:
            print(f'[ERROR] Failed to get BTC price: {e}')
            btc_current_price = None

        # 获取BTC历史价格（从base_time开始，只使用真实数据）
        btc_historical_data = []
        if btc_current_price:
            try:
                btc_historical = market_fetcher.get_historical_prices('BTC', days=30)
                if btc_historical and len(btc_historical) > 0:
                    btc_historical_data = btc_historical
                else:
                    print('[WARN] No BTC historical data available - cannot calculate baseline')
            except Exception as e:
                print(f'[ERROR] Failed to get BTC historical data: {e}')

        # 只有在有真实BTC数据时才添加BTC基准线
        if btc_current_price and btc_historical_data:
            # 过滤BTC历史数据，只保留系统运行时间之后的数据
            filtered_btc_data = []
            for hist_point in btc_historical_data:
                # timestamp可能是整数（Unix时间戳毫秒）或字符串
                timestamp = hist_point['timestamp']
                if isinstance(timestamp, int):
                    # Unix时间戳（毫秒）转换为datetime
                    hist_time_utc = datetime.fromtimestamp(timestamp / 1000)
                else:
                    # 字符串格式
                    hist_time_utc = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')

                if hist_time_utc >= system_start_utc:
                    filtered_btc_data.append(hist_point)

            if not filtered_btc_data:
                print('[WARN] No BTC data after system start time')
            else:
                # 使用过滤后的最早价格作为初始价格
                btc_initial_price = filtered_btc_data[0]['price']

                # 计算BTC持有收益
                # 假设初始10000美元全部买入BTC
                btc_quantity = 10000 / btc_initial_price

                # 构建BTC基准线数据点（只包含系统运行时间之后的数据）
                btc_baseline_data = []
                for hist_point in filtered_btc_data:
                    btc_value = btc_quantity * hist_point['price']
                    # 转换时间为东八区
                    timestamp = hist_point['timestamp']
                    if isinstance(timestamp, int):
                        # Unix时间戳（毫秒）转换为UTC字符串
                        hist_time_utc_str = datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
                        hist_time_beijing = utc_to_beijing(hist_time_utc_str)
                    else:
                        hist_time_beijing = utc_to_beijing(timestamp)

                    btc_baseline_data.append({
                        'time': hist_time_beijing,
                        'value': btc_value
                    })

                # 添加当前时间点
                btc_current_value = btc_quantity * btc_current_price
                btc_baseline_data.append({
                    'time': current_time,
                    'value': btc_current_value
                })

                result.append({
                    'model_id': 'BTC_BASELINE',
                    'model_name': 'BTC基准',
                    'data': btc_baseline_data
                })
        else:
            print('[WARN] Skipping BTC baseline - no real market data available')

        conn.close()

        return jsonify(result)
    except Exception as e:
        print(f'[ERROR] Failed to get performance chart: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/dashboard/recent-trades', methods=['GET'])
def get_recent_trades():
    """获取最近交易动态（公开API）"""
    try:
        limit = int(request.args.get('limit', 100))

        conn = db.get_connection()
        cursor = conn.cursor()

        # 获取最近的交易记录，包含模型名称
        cursor.execute('''
            SELECT
                t.id,
                t.model_id,
                m.name as model_name,
                t.coin,
                t.signal,
                t.quantity,
                t.price,
                t.leverage,
                t.pnl,
                t.timestamp
            FROM trades t
            JOIN models m ON t.model_id = m.id
            ORDER BY t.timestamp DESC
            LIMIT ?
        ''', (limit,))

        trades = []
        for row in cursor.fetchall():
            # 将UTC时间转换为东八区时间
            beijing_time = utc_to_beijing(row['timestamp'])
            trades.append({
                'id': row['id'],
                'model_id': row['model_id'],
                'model_name': row['model_name'],
                'coin': row['coin'],
                'action': row['signal'],  # 映射signal到action
                'quantity': row['quantity'],
                'price': row['price'],
                'leverage': row['leverage'],
                'pnl': row['pnl'],
                'created_at': beijing_time  # 映射timestamp到created_at，并转换为东八区
            })

        conn.close()
        return jsonify(trades)
    except Exception as e:
        print(f'[ERROR] Failed to get recent trades: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    """获取排行榜（公开API，不需要登录）- 增强版，支持多维度排序"""
    sort_by = request.args.get('sort_by', 'returns')

    # 获取所有模型
    all_models = db.get_all_models()
    current_prices = _get_current_market_prices()

    leaderboard = []
    for model in all_models:
        portfolio = db.get_portfolio(model['id'], current_prices)

        # 获取用户名
        user = db.get_user_by_id(model.get('user_id'))
        username = user['username'] if user else 'Unknown'

        # 计算收益率
        account_value = portfolio.get('total_value', model['initial_capital'])
        returns = ((account_value - model['initial_capital']) / model['initial_capital']) * 100

        # 计算胜率
        trades = db.get_trades(model['id'], limit=100)
        winning_trades = [t for t in trades if t['pnl'] > 0]
        win_rate = (len(winning_trades) / len(trades)) if trades else 0

        # 计算夏普比率（简化版）
        if len(trades) > 1:
            returns_list = [t['pnl'] for t in trades]
            avg_return = sum(returns_list) / len(returns_list)
            std_return = (sum((r - avg_return) ** 2 for r in returns_list) / len(returns_list)) ** 0.5
            sharpe_ratio = (avg_return / std_return) if std_return > 0 else 0
        else:
            sharpe_ratio = 0

        # 计算最大回撤
        max_drawdown = risk_manager._calculate_max_drawdown(model['id'])

        leaderboard.append({
            'model_id': model['id'],
            'model_name': model['name'],
            'username': username,
            'total_value': account_value,
            'total_return': returns,
            'sharpe_ratio': sharpe_ratio,
            'win_rate': win_rate,
            'max_drawdown': max_drawdown,
            'total_trades': len(trades)
        })

    # 排序
    if sort_by == 'returns':
        leaderboard.sort(key=lambda x: x['total_return'], reverse=True)
    elif sort_by == 'sharpe':
        leaderboard.sort(key=lambda x: x['sharpe_ratio'], reverse=True)
    elif sort_by == 'win_rate':
        leaderboard.sort(key=lambda x: x['win_rate'], reverse=True)
    elif sort_by == 'drawdown':
        leaderboard.sort(key=lambda x: x['max_drawdown'])

    return jsonify(leaderboard)

@app.route('/api/market/prices', methods=['GET'])
def get_market_prices():
    """获取市场价格（公开API）"""
    prices = market_fetcher.get_current_prices(config.SUPPORTED_COINS)
    return jsonify(prices)

@app.route('/api/market/historical/<coin>', methods=['GET'])
def get_historical_prices(coin):
    """获取历史价格数据"""
    days = request.args.get('days', 30, type=int)
    try:
        historical = market_fetcher.get_historical_prices(coin, days=days)
        return jsonify(historical)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/execute', methods=['POST'])
@login_required
def execute_trading(model_id):
    """执行交易（需要登录且拥有该模型）"""
    user_id = get_current_user_id()

    # 检查权限
    if not _check_model_ownership(model_id, user_id):
        return jsonify({'error': '无权操作此模型'}), 403

    if model_id not in trading_engines:
        try:
            trading_engines[model_id] = _create_trading_engine(model_id)
        except Exception as e:
            return jsonify({'error': str(e)}), 404

    try:
        result = trading_engines[model_id].execute_trading_cycle()
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def trading_loop():
    print("[INFO] Trading loop started")
    
    while auto_trading:
        try:
            if not trading_engines:
                time.sleep(30)
                continue
            
            print(f"\n{'='*60}")
            print(f"[CYCLE] {get_current_beijing_time_str()}")  # 日志显示东八区时间
            print(f"[INFO] Active models: {len(trading_engines)}")
            print(f"{'='*60}")
            
            for model_id, engine in list(trading_engines.items()):
                try:
                    print(f"\n[EXEC] Model {model_id}")
                    result = engine.execute_trading_cycle()
                    
                    if result.get('success'):
                        print(f"[OK] Model {model_id} completed")
                        if result.get('executions'):
                            for exec_result in result['executions']:
                                signal = exec_result.get('signal', 'unknown')
                                coin = exec_result.get('coin', 'unknown')
                                msg = exec_result.get('message', '')
                                if signal != 'hold':
                                    print(f"  [TRADE] {coin}: {msg}")
                    else:
                        error = result.get('error', 'Unknown error')
                        print(f"[WARN] Model {model_id} failed: {error}")
                        
                except Exception as e:
                    print(f"[ERROR] Model {model_id} exception: {e}")
                    import traceback
                    print(traceback.format_exc())
                    continue
            
            print(f"\n{'='*60}")
            print(f"[SLEEP] Waiting 3 minutes for next cycle")
            print(f"{'='*60}\n")
            
            time.sleep(180)
            
        except Exception as e:
            print(f"\n[CRITICAL] Trading loop error: {e}")
            import traceback
            print(traceback.format_exc())
            print("[RETRY] Retrying in 60 seconds\n")
            time.sleep(60)
    
    print("[INFO] Trading loop stopped")



def init_trading_engines():
    try:
        models = db.get_all_models()

        if not models:
            print("[WARN] No trading models found")
            return

        print(f"\n[INIT] Initializing trading engines...")
        for model in models:
            model_id = model['id']
            model_name = model['name']

            try:
                trading_engines[model_id] = _create_trading_engine(model_id)
                print(f"  [OK] Model {model_id} ({model_name})")
            except Exception as e:
                print(f"  [ERROR] Model {model_id} ({model_name}): {e}")
                continue

        print(f"[INFO] Initialized {len(trading_engines)} engine(s)\n")

    except Exception as e:
        print(f"[ERROR] Init engines failed: {e}\n")

if __name__ == '__main__':
    db.init_db()
    
    print("\n" + "=" * 60)
    print("AI Trading Platform")
    print("=" * 60)
    
    init_trading_engines()
    
    if auto_trading:
        trading_thread = threading.Thread(target=trading_loop, daemon=True)
        trading_thread.start()
        print("[INFO] Auto-trading enabled")
    
    print("\n" + "=" * 60)
    print(f"Server: http://localhost:{config.PORT}")
    print("Press Ctrl+C to stop")
    print("=" * 60 + "\n")

    app.run(debug=config.DEBUG, host=config.HOST, port=config.PORT, use_reloader=False)
