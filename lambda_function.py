import json
import os
import time
import csv
import io
import pymysql
from google import genai 

# 1. Configurações
client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))
MODEL_ID = 'gemini-2.5-flash' 

DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER')
DB_PASS = os.environ.get('DB_PASS')
DB_NAME = 'db-zentrix'
SSL_CA = 'global-bundle.pem'

MINHAS_CATEGORIAS = [
    "Essencial", "Role e Lazer", "Rangos", "Transporte", 
    "Assinaturas", "Compras e Mimos", "A receber", "Outros"
]

def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        ssl={'ca': SSL_CA},
        cursorclass=pymysql.cursors.DictCursor
    )

def lambda_handler(event, context):
    metodo = event.get('httpMethod') or event.get('requestContext', {}).get('http', {}).get('method')
    
    try:
        if metodo == 'POST':
            return tratar_post(event)
        elif metodo == 'GET':
            return tratar_get(event)
        elif metodo == 'DELETE':
            return tratar_delete(event)
        else:
            return tratar_post(event)
            
    except Exception as e:
        print(f"ERRO CRÍTICO: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'erro': str(e)})
        }

def tratar_post(event):
    body = json.loads(event.get('body', '{}'))
    user_id = body.get('user_id', 'usuario_padrao')
    conn = get_db_connection()

    try:
        with conn.cursor() as cursor:
            # --- CASO 1: IMPORTAÇÃO DE CSV (C6 BANK) ---
            if 'csv_text' in body:
                f = io.StringIO(body['csv_text'].strip())
                reader = list(csv.DictReader(f, delimiter=';'))
                descricoes_brutas = list(set([row.get('Descrição', '') for row in reader if row.get('Descrição')]))
                
                prompt_lote = f"""
                Categorias: {MINHAS_CATEGORIAS}.
                Para cada item abaixo, retorne um JSON com a chave sendo o nome original:
                {{ "desc_limpa": "nome curto", "cat": "categoria" }}
                Itens: {descricoes_brutas}
                """
                
                response = client.models.generate_content(model=MODEL_ID, contents=prompt_lote)
                mapa_categorias = json.loads(response.text.replace('```json', '').replace('```', '').strip())
                
                contagem = 0
                # SQL atualizado com as novas colunas
                sql = """
                    INSERT INTO transacoes 
                    (user_id, description, amount, category, type, source, installments_total, installments_paid) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                for row in reader:
                    desc_orig = row.get('Descrição', '')
                    valor_bruto = row.get('Valor (em R$)', '0').replace('.', '').replace(',', '.')
                    valor = float(valor_bruto)
                    if valor <= 0: continue
                    
                    info_ia = mapa_categorias.get(desc_orig, {"desc_limpa": desc_orig, "cat": "Outros"})
                    parcela_raw = row.get('Parcela', 'Única')
                    
                    # Lógica de parcelas mantida
                    total_p = 1
                    paga_p = 1
                    if "/" in parcela_raw:
                        partes = parcela_raw.split("/")
                        paga_p = int(partes[0])
                        total_p = int(partes[1])
                    
                    tipo_movimentacao = "Crédito Parcelado" if "/" in parcela_raw else "Crédito à Vista"
                    if info_ia['cat'] == "A receber":
                        tipo_movimentacao = "Emprestado"

                    cursor.execute(sql, (user_id, info_ia['desc_limpa'], valor, info_ia['cat'], tipo_movimentacao, 'C6_BANK', total_p, paga_p))
                    contagem += 1
                
                conn.commit()
                return {'statusCode': 201, 'body': json.dumps(f'{contagem} itens processados!')}

            # --- CASO 2: FRASE POR VOZ ---
            elif 'frase' in body:
                # Prompt atualizado para capturar o devedor sem quebrar a lógica anterior
                prompt_ia = f"""
                Extraia os dados da frase: '{body['frase']}'
                Categorias: {MINHAS_CATEGORIAS}
                Tipos permitidos: [Débito, Crédito à Vista, Crédito Parcelado, Emprestado]
                
                Regras de Tipo:
                - Se eu disser 'no débito' ou 'no pix' -> Débito
                - Se houver parcelas -> Crédito Parcelado
                - Se for 'A receber' -> Emprestado
                - Padrão -> Crédito à Vista
                
                Retorne APENAS um JSON: {{"description": "nome", "amount": float, "category": "categoria", "type": "tipo", "installments": int, "debtor_name": "nome ou null"}}
                """
                response = client.models.generate_content(model=MODEL_ID, contents=prompt_ia)
                dados = json.loads(response.text.replace('```json', '').replace('```', '').strip())
                
                sql = """
                    INSERT INTO transacoes 
                    (user_id, description, amount, category, type, source, installments_total, debtor_name, raw_input_phrase) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(sql, (
                    user_id, 
                    dados['description'], 
                    dados['amount'], 
                    dados['category'], 
                    dados['type'], 
                    'IA_CHAT', 
                    dados.get('installments', 1),
                    dados.get('debtor_name'),
                    body['frase']
                ))
                
                conn.commit()
                return {'statusCode': 201, 'body': json.dumps({'message': 'Salvo!', 'dados': dados})}

    finally:
        conn.close()

def tratar_get(event):
    params = event.get('queryStringParameters', {})
    user_id = params.get('user_id')
    
    if not user_id:
        return {'statusCode': 400, 'body': json.dumps('user_id é obrigatório')}
        
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT * FROM transacoes WHERE user_id = %s ORDER BY created_at DESC"
            cursor.execute(sql, (user_id,))
            items = cursor.fetchall()
            
        return {
            'statusCode': 200, 
            'headers': {'Access-Control-Allow-Origin': '*'},
            'body': json.dumps(items, default=str)
        }
    finally:
        conn.close()

def tratar_delete(event):
    body = json.loads(event.get('body', '{}'))
    user_id = body.get('user_id')
    transacao_id = body.get('id') 
    
    if not user_id or not transacao_id:
        return {'statusCode': 400, 'body': json.dumps('Faltam chaves para deletar')}
        
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            sql = "DELETE FROM transacoes WHERE id = %s AND user_id = %s"
            cursor.execute(sql, (transacao_id, user_id))
            conn.commit()
            
        return {'statusCode': 200, 'body': json.dumps('Item removido.')}
    finally:
        conn.close()