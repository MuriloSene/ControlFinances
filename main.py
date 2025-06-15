# uvicorn main:app --host 0.0.0.0 --port 3000

import logging
import requests
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy import (
    create_engine, Column, Integer, String, DECIMAL,
    ForeignKey, Text, DateTime, func, extract, text # Importar 'text' para executar SQL bruto
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from typing import Optional, List
from datetime import datetime, timedelta # Importar timedelta para cálculos de data
import json
import re
from decimal import Decimal # Importar Decimal para a verificação de tipo

# ─── Configuração de logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Configuração do Banco de Dados ─────────────────────────────────────────
DATABASE_URL = "mysql+pymysql://root:1809@localhost:3306/control_finances"
engine = create_engine(DATABASE_URL, echo=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ─── Configurações do Chat via HTTP (Ollama Docker) ──────────────────────────
CHAT_URL   = "http://localhost:11434/v1/chat/completions"
CHAT_MODEL = "gemma3"

# ─── Custom JSONResponse com charset UTF-8 ──────────────────────────────────
class CustomJSONResponse(JSONResponse):
    def __init__(
        self,
        content,
        status_code: int = 200,
        headers: Optional[dict] = None,
        media_type: Optional[str] = None,
        background: Optional[BackgroundTask] = None,
    ):
        super().__init__(
            content=content,
            status_code=status_code,
            headers=headers,
            media_type="application/json; charset=utf-8",
            background=background,
        )

app = FastAPI(default_response_class=CustomJSONResponse)

# ─── Middleware para logar requisições ───────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"{request.method} {request.url}")
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Erro interno no endpoint")
        raise
    return response

# ─── Tratamento de exceções HTTP ────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    logger.error(f"HTTPException {exc.status_code}: {exc.detail}")
    return CustomJSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# ─── MODELOS ORM ─────────────────────────────────────────────────────────────
class Usuario(Base):
    __tablename__ = "usuarios"
    id       = Column(Integer, primary_key=True, index=True)
    nome     = Column(String(100), nullable=False)
    email    = Column(String(100), unique=True, nullable=False)
    senha    = Column(String(255), nullable=False)
    entradas = relationship("Entrada", back_populates="usuario", cascade="all, delete")
    saidas   = relationship("Saida",   back_populates="usuario", cascade="all, delete")

class Entrada(Base):
    __tablename__ = "entradas"
    id           = Column(Integer, primary_key=True, index=True)
    usuario_id   = Column(Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    descricao    = Column(Text, nullable=True)
    data         = Column(DateTime, nullable=False)
    instituicao  = Column(String(100), nullable=False)
    valor        = Column(DECIMAL(10,2), nullable=False)
    usuario      = relationship("Usuario", back_populates="entradas")

class Saida(Base):
    __tablename__ = "saidas"
    id           = Column(Integer, primary_key=True, index=True)
    usuario_id   = Column(Integer, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    descricao    = Column(Text, nullable=True)
    data         = Column(DateTime, nullable=False)
    categoria    = Column(String(100), nullable=False)
    subcategoria = Column(String(100), nullable=True)
    instituicao  = Column(String(255), nullable=False)
    valor        = Column(DECIMAL(10,2), nullable=False)
    usuario      = relationship("Usuario", back_populates="saidas")

# Cria as tabelas (se não existirem ainda)
Base.metadata.create_all(bind=engine)

# ─── SCHEMAS Pydantic ────────────────────────────────────────────────────────
class UsuarioCreate(BaseModel):
    nome: str
    email: str
    senha: str

class UsuarioRead(BaseModel):
    id: int
    nome: str
    email: str
    model_config = ConfigDict(from_attributes=True)

class LoginSchema(BaseModel):
    email: str
    senha: str

class LoginResponse(BaseModel):
    message: str
    nome: str
    id: int

class EntradaCreateSchema(BaseModel):
    usuario_id: int
    descricao: Optional[str] = None
    data: datetime
    instituicao: str
    valor: float

class EntradaReadSchema(BaseModel):
    id: int
    usuario_id: int
    descricao: Optional[str]
    data: datetime
    instituicao: str
    valor: float
    model_config = ConfigDict(from_attributes=True)

class SaidaCreateSchema(BaseModel):
    usuario_id: int
    descricao: Optional[str] = None
    data: datetime
    categoria: str
    subcategoria: Optional[str] = None
    instituicao: str
    valor: float

class SaidaReadSchema(BaseModel):
    id: int
    usuario_id: int
    descricao: Optional[str]
    data: datetime
    categoria: str
    subcategoria: Optional[str] = None
    instituicao: str
    valor: float
    model_config = ConfigDict(from_attributes=True)

class DashboardResponse(BaseModel):
    saldo: float
    total_entradas: float
    total_saidas: float
    recent_entradas: List[dict]
    recent_saidas: List[dict]

class CategoriaTotal(BaseModel):
    categoria: str
    total: float

class InstTotal(BaseModel):
    instituicao: str
    total: float

class RelatorioResponse(BaseModel):
    por_categoria: List[CategoriaTotal]
    entrada_por_instituicao: List[InstTotal]
    saida_por_instituicao: List[InstTotal]

# ─── Esquema de Chat ─────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    usuario_id: int = Field(..., alias="usuario_id")
    pergunta:   str = Field(..., alias="pergunta")

    @model_validator(mode="before")
    def map_aliases(cls, data):
        # aceita { "question": "...", "userId": ... }
        if isinstance(data, dict):
            if "question" in data:
                data["pergunta"] = data.pop("question")
            if "userId" in data:
                data["usuario_id"] = data.pop("userId")
        return data

class ChatResponse(BaseModel):
    resposta: str

# ─── Dependência de sessão ───────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── ENDPOINTS EXISTENTES (cadastro, login, CRUD, dashboard, relatorio) ──────

@app.post("/api/cadastro", response_model=UsuarioRead)
def cadastrar_usuario(usuario: UsuarioCreate, db: Session = Depends(get_db)):
    if db.query(Usuario).filter(Usuario.email == usuario.email).first():
        raise HTTPException(400, "Email já cadastrado")
    novo = Usuario(**usuario.dict())
    db.add(novo); db.commit(); db.refresh(novo)
    return novo

@app.post("/api/login", response_model=LoginResponse)
def login(u: LoginSchema, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.email == u.email, Usuario.senha == u.senha).first()
    if not user:
        raise HTTPException(401, "Credenciais inválidas")
    return {"message":"Login autorizado","nome":user.nome,"id":user.id}

@app.post("/api/entrada", response_model=EntradaReadSchema)
def criar_entrada(e: EntradaCreateSchema, db: Session = Depends(get_db)):
    ent = Entrada(**e.dict()); db.add(ent); db.commit(); db.refresh(ent)
    return ent

@app.get("/api/entrada/{entrada_id}", response_model=EntradaReadSchema)
def get_entrada(entrada_id: int, db: Session = Depends(get_db)):
    ent = db.get(Entrada, entrada_id)
    if not ent:
        raise HTTPException(404, "Entrada não encontrada")
    return ent

@app.put("/api/entrada/{entrada_id}", response_model=EntradaReadSchema)
def atualizar_entrada(entrada_id: int, e: EntradaCreateSchema, db: Session = Depends(get_db)):
    ent = db.get(Entrada, entrada_id)
    if not ent:
        raise HTTPException(404, "Entrada não encontrada")
    for k, v in e.dict().items():
        setattr(ent, k, v)
    db.commit(); db.refresh(ent)
    return ent

@app.delete("/api/entrada/{entrada_id}")
def deletar_entrada(entrada_id: int, db: Session = Depends(get_db)):
    ent = db.get(Entrada, entrada_id)
    if not ent:
        raise HTTPException(404, "Entrada não encontrada")
    db.delete(ent); db.commit()
    return {"message": "Entrada excluída com sucesso"}

@app.get("/api/entradas/{usuario_id}", response_model=List[EntradaReadSchema])
def listar_entradas(usuario_id: int, db: Session = Depends(get_db)):
    return db.query(Entrada).filter(Entrada.usuario_id == usuario_id).order_by(Entrada.data.desc()).all()

@app.post("/api/saida", response_model=SaidaReadSchema)
def criar_saida(s: SaidaCreateSchema, db: Session = Depends(get_db)):
    sd = Saida(**s.dict()); db.add(sd); db.commit(); db.refresh(sd)
    return sd

@app.get("/api/saida/{saida_id}", response_model=SaidaReadSchema)
def get_saida(saida_id: int, db: Session = Depends(get_db)):
    sd = db.get(Saida, saida_id)
    if not sd:
        raise HTTPException(404, "Saída não encontrada")
    return sd

@app.put("/api/saida/{saida_id}", response_model=SaidaReadSchema)
def atualizar_saida(saida_id: int, s: SaidaCreateSchema, db: Session = Depends(get_db)):
    sd = db.get(Saida, saida_id)
    if not sd:
        raise HTTPException(404, "Saída não encontrada")
    for k, v in s.dict().items():
        setattr(sd, k, v)
    db.commit(); db.refresh(sd)
    return sd

@app.delete("/api/saida/{saida_id}")
def deletar_saida(saida_id: int, db: Session = Depends(get_db)):
    sd = db.get(Saida, saida_id)
    if not sd:
        raise HTTPException(404, "Saída não encontrada")
    db.delete(sd); db.commit()
    return {"message": "Saída excluída com sucesso"}

@app.get("/api/saidas/{usuario_id}", response_model=List[SaidaReadSchema])
def listar_saidas(usuario_id: int, db: Session = Depends(get_db)):
    return db.query(Saida).filter(Saida.usuario_id == usuario_id).order_by(Saida.data.desc()).all()

@app.get("/api/dashboard/{usuario_id}", response_model=DashboardResponse)
def dashboard(
    usuario_id: int,
    instituicao: Optional[str]        = Query(None, description="Filtrar por instituição"),
    categorias: Optional[List[str]]   = Query(None, description="Filtrar por categorias"),
    db: Session = Depends(get_db),
):
    # totais
    q_ent = db.query(func.coalesce(func.sum(Entrada.valor), 0)).filter(Entrada.usuario_id == usuario_id)
    q_sai = db.query(func.coalesce(func.sum(Saida.valor), 0)).filter(Saida.usuario_id   == usuario_id)
    if instituicao and instituicao.lower() != "todas":
        q_ent = q_ent.filter(Entrada.instituicao == instituicao)
        q_sai = q_sai.filter(Saida.instituicao   == instituicao)
    if categorias:
        q_sai = q_sai.filter(Saida.categoria.in_(categorias))
    total_ent = q_ent.scalar(); total_sai = q_sai.scalar()
    saldo = float(total_ent) - float(total_sai)

    # recentes
    ent_q = db.query(Entrada).filter(Entrada.usuario_id == usuario_id)
    sai_q = db.query(Saida)  .filter(Saida.usuario_id   == usuario_id)
    if instituicao and instituicao.lower() != "todas":
        ent_q = ent_q.filter(Entrada.instituicao == instituicao)
        sai_q = sai_q.filter(Saida.instituicao   == instituicao)
    if categorias:
        sai_q = sai_q.filter(Saida.categoria.in_(categorias))
    ents = ent_q.order_by(Entrada.data.desc()).limit(5).all()
    sais = sai_q.order_by(Saida.data.desc()).limit(5).all()

    return {
        "saldo": saldo,
        "total_entradas": float(total_ent),
        "total_saidas": float(total_sai),
        "recent_entradas": [
            {"id": e.id, "descricao": e.descricao, "valor": float(e.valor),
             "data": e.data.isoformat(), "instituicao": e.instituicao}
            for e in ents
        ],
        "recent_saidas": [
            {"id": s.id, "descricao": s.descricao, "categoria": s.categoria,
             "subcategoria": s.subcategoria, "instituicao": s.instituicao,
             "valor": float(s.valor), "data": s.data.isoformat()}
            for s in sais
        ]
    }

@app.get("/api/relatorio/{usuario_id}", response_model=RelatorioResponse)
def relatorio(
    usuario_id: int,
    instituicao: Optional[str]    = Query(None, description="Filtrar por instituição"),
    date_from:   Optional[datetime] = Query(None, description="Data inicial (ISO)"),
    date_to:     Optional[datetime] = Query(None, description="Data final (ISO)"),
    db: Session = Depends(get_db),
):
    sai_q = db.query(Saida).filter(Saida.usuario_id == usuario_id)
    ent_q = db.query(Entrada).filter(Entrada.usuario_id == usuario_id)
    if instituicao and instituicao.lower() != "todas":
        sai_q = sai_q.filter(Saida.instituicao == instituicao)
        ent_q = ent_q.filter(Entrada.instituicao == instituicao)
    if date_from:
        sai_q = sai_q.filter(Saida.data >= date_from)
        ent_q = ent_q.filter(Entrada.data >= date_from)
    if date_to:
        sai_q = sai_q.filter(Saida.data <= date_to)
        ent_q = ent_q.filter(Entrada.data <= date_to)

    cat_data = (
        sai_q.with_entities(Saida.categoria, func.sum(Saida.valor).label("total"))
             .group_by(Saida.categoria).all()
    )
    inst_ent = (
        ent_q.with_entities(Entrada.instituicao.label("inst"), func.sum(Entrada.valor).label("total"))
             .group_by("inst").all()
    )
    inst_sai = (
        sai_q.with_entities(Saida.instituicao.label("inst"), func.sum(Saida.valor).label("total"))
             .group_by("inst").all()
    )

    return {
        "por_categoria": [
            {"categoria": c, "total": float(v)} for c, v in cat_data
        ],
        "entrada_por_instituicao": [
            {"instituicao": inst, "total": float(t)} for inst, t in inst_ent
        ],
        "saida_por_instituicao": [
            {"instituicao": inst, "total": float(t)} for inst, t in inst_sai
        ]
    }

# ─── Definição do Esquema do Banco de Dados para o LLM ───────────────────────
DATABASE_SCHEMA = """
Tabela: usuarios
    - id (INTEGER, chave primária)
    - nome (STRING)
    - email (STRING, único)
    - senha (STRING)

Tabela: entradas
    - id (INTEGER, chave primária)
    - usuario_id (INTEGER, chave estrangeira para usuarios.id)
    - descricao (TEXT)
    - data (DATETIME)
    - instituicao (STRING)
    - valor (DECIMAL)

Tabela: saidas
    - id (INTEGER, chave primária)
    - usuario_id (INTEGER, chave estrangeira para usuarios.id)
    - descricao (TEXT)
    - data (DATETIME)
    - categoria (STRING)
    - subcategoria (STRING)
    - instituicao (STRING)
    - valor (DECIMAL)

Relações:
    - usuarios.id se relaciona com entradas.usuario_id (um usuário tem muitas entradas)
    - usuarios.id se relaciona com saidas.usuario_id (um usuário tem muitas saídas)

O campo 'data' é sempre armazenado no formato DATETIME.
Os campos 'valor' são sempre numéricos decimais.
"""

# ─── Endpoint de chat para geração e execução de SQL ─────────────────────────
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    user = db.get(Usuario, req.usuario_id)
    if not user:
        raise HTTPException(404, "Utilizador não encontrado")

    # Obtém a data e hora atual no fuso horário local
    now = datetime.now()
    # Define o início da semana (segunda-feira) e o fim da semana (domingo)
    start_of_week = now - timedelta(days=now.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    # Define o início do mês e o fim do mês
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Para o fim do mês, podemos ir para o primeiro dia do próximo mês e subtrair um dia
    if now.month == 12:
        end_of_month = now.replace(year=now.year + 1, month=1, day=1) - timedelta(microseconds=1)
    else:
        end_of_month = now.replace(month=now.month + 1, day=1) - timedelta(microseconds=1)
    # Define o início do ano e o fim do ano
    start_of_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_year = now.replace(month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)


    # Primeiro, instrua o modelo a gerar SQL
    sql_generation_prompt = f"""
    Você é um assistente financeiro que pode consultar um banco de dados SQL para responder a perguntas sobre finanças pessoais.
    Aqui está o esquema do banco de dados:
    {DATABASE_SCHEMA}

    Você DEVE gerar SOMENTE a consulta SQL que responde à pergunta do usuário.
    Não adicione explicações ou qualquer outro texto, APENAS o SQL.
    A consulta SQL deve sempre incluir `WHERE usuario_id = {req.usuario_id}` para filtrar os dados do usuário correto.
    Use `FROM saidas` para perguntas sobre gastos e `FROM entradas` para perguntas sobre receitas.
    Sempre arredonde os valores monetários para duas casas decimais no SELECT, usando `ROUND(campo, 2)`.
    Para calcular totais, use `SUM(valor)`. Para médias, use `AVG(valor)`. Para contagens, use `COUNT(*)`.
    Para filtros de data, utilize `data BETWEEN 'YYYY-MM-DD HH:MM:SS' AND 'YYYY-MM-DD HH:MM:SS'`.
    Para as datas atuais:
    - O início da semana é '{start_of_week.strftime('%Y-%m-%d %H:%M:%S')}'
    - O fim da semana é '{end_of_week.strftime('%Y-%m-%d %H:%M:%S')}'
    - O início do mês é '{start_of_month.strftime('%Y-%m-%d %H:%M:%S')}'
    - O fim do mês é '{end_of_month.strftime('%Y-%m-%d %H:%M:%S')}'
    - O início do ano é '{start_of_year.strftime('%Y-%m-%d %H:%M:%S')}'
    - O fim do ano é '{end_of_year.strftime('%Y-%m-%d %H:%M:%S')}'
    Use `ORDER BY data DESC` para listar os resultados mais recentes primeiro.
    Use `LIMIT X` para limitar o número de resultados.

    **Instruções Específicas:**
    - Para calcular o saldo (total de entradas menos total de saídas), você DEVE usar subconsultas.
    - A consulta para o saldo deve ter o seguinte formato:
      ```sql
      SELECT
          (SELECT ROUND(SUM(valor), 2) FROM entradas WHERE usuario_id = <usuario_id>) -
          (SELECT ROUND(SUM(valor), 2) FROM saidas WHERE usuario_id = <usuario_id>) AS saldo,
          (SELECT ROUND(SUM(valor), 2) FROM entradas WHERE usuario_id = <usuario_id>) AS total_entradas,
          (SELECT ROUND(SUM(valor), 2) FROM saidas WHERE usuario_id = <usuario_id>) AS total_saidas;
      ```
    - Retorne o saldo, o total de entradas e o total de saídas na mesma consulta.

    Exemplo de pergunta do usuário: 'qual é o meu saldo?'
    Exemplo de SQL esperada:
    SELECT
        (SELECT ROUND(SUM(valor), 2) FROM entradas WHERE usuario_id = {req.usuario_id}) -
        (SELECT ROUND(SUM(valor), 2) FROM saidas WHERE usuario_id = {req.usuario_id}) AS saldo,
        (SELECT ROUND(SUM(valor), 2) FROM entradas WHERE usuario_id = {req.usuario_id}) AS total_entradas,
        (SELECT ROUND(SUM(valor), 2) FROM saidas WHERE usuario_id = {req.usuario_id}) AS total_saidas;


    Exemplo de pergunta do usuário: 'quais foram os gastos totais em alimentação este mês?'
    Exemplo de SQL esperada:
    SELECT ROUND(SUM(valor), 2) FROM saidas WHERE usuario_id = {req.usuario_id} AND categoria = 'Alimentação' AND data BETWEEN '{start_of_month.strftime('%Y-%m-%d %H:%M:%S')}' AND '{end_of_month.strftime('%Y-%m-%d %H:%M:%S')}';

    Exemplo de pergunta do usuário: 'liste todas as minhas entradas de salário do ano passado'
    Exemplo de SQL esperada:
    SELECT descricao, instituicao, valor, data FROM entradas WHERE usuario_id = {req.usuario_id} AND descricao LIKE '%Salário%' AND YEAR(data) = YEAR(CURDATE()) - 1 ORDER BY data DESC;

    Exemplo de pergunta do usuário: 'quais as 5 saidas mais recentes?'
    Exemplo de SQL esperada:
    SELECT descricao, categoria, valor, data FROM saidas WHERE usuario_id = {req.usuario_id} ORDER BY data DESC LIMIT 5;

    Agora, gere a consulta SQL para a seguinte pergunta do usuário: '{req.pergunta}'
    """

    messages_step1 = [
        {"role": "system", "content": sql_generation_prompt},
        # A pergunta original já está incluída no prompt do sistema, então não a repetimos aqui como uma mensagem separada do usuário
    ]

    try:
        # Passo 1: Fazer o Gemma gerar o SQL
        payload_step1 = {
            "model": CHAT_MODEL,
            "messages": messages_step1,
            "temperature": 0.1, # Uma temperatura baixa incentiva respostas mais determinísticas (SQL)
            "max_output_tokens": 500 # Limite o tamanho para evitar SQL muito grande ou divagações
        }

        resp_step1 = requests.post(CHAT_URL, json=payload_step1, timeout=60)
        resp_step1.raise_for_status()
        data_step1 = resp_step1.json()

        generated_sql = ""
        if (
            "choices" in data_step1
            and len(data_step1["choices"]) > 0
            and "message" in data_step1["choices"][0]
            and "content" in data_step1["choices"][0]["message"]
        ):
            generated_sql = data_step1["choices"][0]["message"]["content"].strip()
            # Remove qualquer marcador de bloco de código (```sql) que o Gemma possa adicionar
            if generated_sql.startswith("```sql"):
                generated_sql = generated_sql[len("```sql"):].strip()
            if generated_sql.endswith("```"):
                generated_sql = generated_sql[:-len("```")].strip()
            logger.info(f"SQL Gerado pelo Gemma: {generated_sql}")
        else:
            logger.error(f"Gemma não gerou SQL válido: {data_step1}")
            return ChatResponse(resposta="Desculpe, não consegui entender sua solicitação para gerar uma consulta. Poderia reformular?")

        if not generated_sql:
            return ChatResponse(resposta="Não consegui gerar uma consulta SQL para sua pergunta. Por favor, tente ser mais específico.")

        # --- Validação básica de segurança: Garantir que é um SELECT e para o usuário correto ---
        if not generated_sql.lower().startswith("select"):
            logger.warning(f"Tentativa de executar SQL não-SELECT: {generated_sql}")
            return ChatResponse(resposta="Desculpe, só posso realizar consultas de leitura (SELECT). Não posso modificar ou deletar dados.")

        # Verifica se a cláusula WHERE para o usuario_id está presente para segurança
        # Isso é uma salvaguarda adicional caso o LLM "esqueça" da instrução do prompt
        # A validação pode ser mais robusta, por exemplo, verificando a sintaxe da cláusula.
        if f"usuario_id = {req.usuario_id}" not in generated_sql:
            logger.warning(f"SQL gerado não contém filtro de usuario_id: {generated_sql}")
            return ChatResponse(resposta="Por segurança, a consulta gerada não pôde ser executada. Por favor, reformule sua pergunta.")
        # --- Fim da Validação de Segurança ---

        # Passo 2: Executar o SQL gerado no banco de dados
        sql_results = []
        try:
            # CORREÇÃO AQUI: Use db.execute(text(generated_sql))
            result = db.execute(text(generated_sql))
            # Tentativa de obter nomes de colunas para melhor formatação
            column_names = result.keys() if hasattr(result, 'keys') else []
            for row in result:
                if column_names:
                    row_dict = {}
                    for i, col_name in enumerate(column_names):
                        value = row[i]
                        if isinstance(value, Decimal): # Usa Decimal importado
                            row_dict[col_name] = float(value) # Garante conversão para float
                        elif isinstance(value, datetime):
                            row_dict[col_name] = value.isoformat()
                        else:
                            row_dict[col_name] = value
                    sql_results.append(row_dict)
                else:
                    # Se for uma única coluna (ex: SUM), apenas adiciona o valor
                    value = row[0]
                    if isinstance(value, Decimal): # Usa Decimal importado
                        sql_results.append(float(value)) # Garante conversão para float
                    elif isinstance(value, datetime):
                        sql_results.append(value.isoformat())
                    else:
                        sql_results.append(value)
            logger.info(f"Resultados do SQL: {sql_results}")

        except Exception as e:
            logger.error(f"Erro ao executar SQL gerado: {e} | SQL: {generated_sql}")
            # Em caso de erro na execução do SQL, também devemos fazer rollback
            db.rollback() 
            raise HTTPException(500, f"Ocorreu um erro ao consultar os dados. Por favor, tente novamente ou reformule sua pergunta. (Detalhes técnicos: {e})")

        # Função auxiliar para garantir que nenhum Decimal persista para serialização JSON
        def convert_decimals_to_float_recursively(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, dict):
                return {k: convert_decimals_to_float_recursively(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [convert_decimals_to_float_recursively(elem) for elem in obj]
            return obj
        
        # Aplica a conversão recursiva antes de serializar para JSON
        final_sql_results_for_json = convert_decimals_to_float_recursively(sql_results)


        # Passo 3: Enviar os resultados da SQL de volta ao Gemma para formatação da resposta
        response_generation_prompt = f"""
        Você é um assistente financeiro. Aqui estão os resultados da consulta SQL que você solicitou:
        {json.dumps(final_sql_results_for_json, ensure_ascii=False, indent=2)}

        A pergunta original do usuário foi: '{req.pergunta}'

        Com base nesses resultados e na pergunta original, formule uma resposta amigável e clara para o usuário.
        Se os resultados estiverem vazios ou forem nulos, informe que não encontrou dados para a solicitação.
        Formate os valores monetários de forma adequada (ex: R$ 123,45).
        Se a pergunta pedia um total ou média, forneça-o de forma concisa. Se pedia uma lista, apresente os itens de forma legível.
        """

        messages_step2 = [
            {"role": "system", "content": response_generation_prompt},
            # Não é necessário repetir a pergunta do usuário aqui como uma mensagem separada, pois já está no prompt do sistema
        ]

        payload_step2 = {
            "model": CHAT_MODEL,
            "messages": messages_step2,
            "temperature": 0.5 # Uma temperatura um pouco mais alta para criatividade na resposta
        }

        resp_step2 = requests.post(CHAT_URL, json=payload_step2, timeout=60)
        resp_step2.raise_for_status()
        data_step2 = resp_step2.json()

        final_answer = ""
        if (
            "choices" in data_step2
            and len(data_step2["choices"]) > 0
            and "message" in data_step2["choices"][0]
            and "content" in data_step2["choices"][0]["message"]
        ):
            final_answer = data_step2["choices"][0]["message"]["content"].strip()
            final_answer = re.sub(r'\s+', ' ', final_answer).strip() # Limpeza de espaços
        else:
            logger.error(f"Gemma não gerou resposta final: {data_step2}")
            return ChatResponse(resposta="Desculpe, não consegui formular uma resposta clara com os dados obtidos.")

        return ChatResponse(resposta=final_answer)

    except requests.RequestException as e:
        logger.error(f"Erro ao chamar o serviço de chat: {e}")
        raise HTTPException(500, f"Erro no modelo de chat: {e}")
    except Exception:
        logger.exception("Erro inesperado no endpoint /api/chat")
        raise HTTPException(500, "Erro interno ao processar o chat")