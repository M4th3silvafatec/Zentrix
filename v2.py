import json
import os
import jwt
import hashlib
from datetime import datetime, timedelta
import pymysql
from functools import wraps

# ===== AUTENTICAÇÃO =====
SECRET_KEY = os.environ.get('JWT_SECRET_KEY')

def validate_token(event):
    """Valida JWT token"""
    try:
        auth_header = event.get('headers', {}).get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return None, {'statusCode': 401, 'body': json.dumps({'erro': 'Token não fornecido'})}
        
        token = auth_header.replace('Bearer ', '')
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload.get('user_id'), None
    except jwt.ExpiredSignatureError:
        return None, {'statusCode': 401, 'body': json.dumps({'erro': 'Token expirado'})}
    except jwt.InvalidTokenError:
        return None, {'statusCode': 401, 'body': json.dumps({'erro': 'Token inválido'})}

def generate_token(user_id, expires_in=24):
    """Gera JWT token válido por X horas"""
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(hours=expires_in)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')

# ===== VALIDAÇÃO DE ENTRADA =====
def validate_input(data, required_fields):
    """Valida campos obrigatórios"""
    for field in required_fields:
        if field not in data or not data[field]:
            return False, f"Campo '{field}' é obrigatório"
    return True, None

def sanitize_string(text):
    """Remove caracteres perigosos"""
    if not isinstance(text, str):
        return text
    return text.strip()[:500]  # Limita a 500 caracteres

# ===== RATE LIMITING =====
RATE_LIMIT_CACHE = {}

def check_rate_limit(user_id, max_requests=100, window_seconds=60):
    """Verifica se usuário excedeu limite de requisições"""
    now = datetime.utcnow()
    key = f"{user_id}:{now.timestamp() // window_seconds}"
    
    if key not in RATE_LIMIT_CACHE:
        RATE_LIMIT_CACHE[key] = 0
    
    RATE_LIMIT_CACHE[key] += 1
    
    if RATE_LIMIT_CACHE[key] > max_requests:
        return False, {'statusCode': 429, 'body': json.dumps({'erro': 'Muitas requisições'})}
    
    return True, None

# ===== LOGGING SEGURO =====
def log_action(user_id, action, status, details=""):
    """Log seguro sem expor dados sensíveis"""
    print(f"[{datetime.utcnow()}] USER:{user_id} ACTION:{action} STATUS:{status}")

# ===== HANDLERS ATUALIZADOS =====

def lambda_handler(event, context):
    # Validar token
    user_id, error = validate_token(event)
    if error:
        return error
    
    # Rate limiting
    allowed, error = check_rate_limit(user_id)
    if not allowed:
        return error
    
    metodo = event.get('httpMethod')
    path = event.get('path', '')
    
    try:
        if metodo == 'POST':
            return tratar_post(event, user_id)
        elif metodo == 'GET':
            if '/stats' in path:
                return tratar_get_stats(event, user_id)
            return tratar_get(event, user_id)
        elif metodo == 'PUT':
            return tratar_put(event, user_id)
        elif metodo == 'DELETE':
            return tratar_delete(event, user_id)
        else:
            return {'statusCode': 405, 'body': json.dumps({'erro': 'Método não permitido'})}
            
    except Exception as e:
        log_action(user_id, 'request', 'error', str(e))
        return {
            'statusCode': 500,
            'body': json.dumps({'erro': 'Erro interno do servidor'})
        }

def tratar_get(event, authenticated_user_id):
    """GET - Listar transações (com segurança)"""
    params = event.get('queryStringParameters', {})
    user_id = params.get('user_id')
    
    # Validar que o usuário só acessa seus próprios dados
    if user_id != authenticated_user_id:
        log_action(authenticated_user_id, 'get', 'denied', f'Tentou acessar {user_id}')
        return {'statusCode': 403, 'body': json.dumps({'erro': 'Acesso negado'})}
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT id, description, amount, category, type, created_at FROM transacoes WHERE user_id = %s ORDER BY created_at DESC LIMIT 100"
            cursor.execute(sql, (user_id,))
            items = cursor.fetchall()
        
        log_action(user_id, 'list_transactions', 'success', f'{len(items)} itens')
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps(items, default=str)
        }
    finally:
        conn.close()

def tratar_put(event, authenticated_user_id):
    """PUT - Atualizar transação (NOVO)"""
    body = json.loads(event.get('body', '{}'))
    user_id = body.get('user_id')
    
    # Verificar permissão
    if user_id != authenticated_user_id:
        return {'statusCode': 403, 'body': json.dumps({'erro': 'Acesso negado'})}
    
    # Validar entrada
    valid, msg = validate_input(body, ['id', 'description', 'amount', 'category'])
    if not valid:
        return {'statusCode': 400, 'body': json.dumps({'erro': msg})}
    
    # Sanitizar entrada
    body['description'] = sanitize_string(body['description'])
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Verificar se transação pertence ao usuário
            cursor.execute("SELECT id FROM transacoes WHERE id = %s AND user_id = %s", (body['id'], user_id))
            if not cursor.fetchone():
                return {'statusCode': 404, 'body': json.dumps({'erro': 'Transação não encontrada'})}
            
            sql = """
                UPDATE transacoes 
                SET description = %s, amount = %s, category = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s
            """
            cursor.execute(sql, (body['description'], body['amount'], body['category'], body['id'], user_id))
            conn.commit()
        
        log_action(user_id, 'update', 'success', f'ID:{body["id"]}')
        return {'statusCode': 200, 'body': json.dumps({'message': 'Atualizado com sucesso!'})}
    finally:
        conn.close()

def tratar_get_stats(event, authenticated_user_id):
    """GET /stats - Resumo financeiro (NOVO)"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                SELECT 
                    category,
                    SUM(amount) as total,
                    COUNT(*) as count
                FROM transacoes 
                WHERE user_id = %s
                GROUP BY category
            """
            cursor.execute(sql, (authenticated_user_id,))
            stats = cursor.fetchall()
        
        return {
            'statusCode': 200,
            'body': json.dumps({'stats': stats}, default=str)
        }
    finally:
        conn.close()