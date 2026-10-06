"""
Dashboard Executivo Jira - CCEE
Construído com Dash, Plotly, Dash Bootstrap Components e DuckDB.
Módulos:
- Aba 1: 📊 Visão Geral de Todas as Demandas
- Aba 2: 🔍 Consultas Técnicas (Lead Time, Aging, metas dinâmicas de SLA, conformidade e temas regulatórios)
- Aba 3: 📘 Cadernos e Versões (Acompanhamento das Versões 2026/2027, Escopo CLIQ e Cadernos de Regras)
- Aba 4: ✅ Demandas Finalizadas (Registros de Demanda vinculados ao épico REGRA-305)
"""

import sys
import threading
from pathlib import Path
import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, html, dcc, dash_table, Input, Output, State, ctx, no_update, ALL
import dash_bootstrap_components as dbc
from etl import run_etl


def localized_dropdown(*args, **kwargs):
    """Usa rótulos de busca em português em todos os dropdowns do painel."""
    labels = {"search": "Procurar"}
    labels.update(kwargs.pop("labels", {}) or {})
    return dcc.Dropdown(*args, labels=labels, **kwargs)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DUCKDB_PATH = str(APP_DIR / "jira.duckdb")
ASSETS_DIR = RESOURCE_DIR / "assets"
DEMANDAS_FINALIZADAS_PARENT_KEY = "REGRA-305"

SYNC_STATE_LOCK = threading.Lock()
SYNC_STATE = {
    "status": "idle",
    "percent": 0,
    "message": "",
    "result": None,
    "completed_at": None
}

# Inicializa aplicação Dash com tema corporativo Flatly
app = Dash(
    __name__,
    assets_folder=str(ASSETS_DIR),
    external_stylesheets=[
        dbc.themes.FLATLY,
        "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"
    ],
    title="GRPC | Dashboard das Demandas de Regras - Jira",
    suppress_callback_exceptions=True
)

server = app.server


def get_data():
    """Consulta dados analíticos da view de demandas do DuckDB."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    df = con.execute("SELECT * FROM v_issues_analytics").df()
    
    try:
        last_sync = con.execute("SELECT last_sync FROM meta_sync ORDER BY last_sync DESC LIMIT 1").fetchone()
        sync_str = last_sync[0].strftime("%d/%m/%Y %H:%M:%S") if last_sync else "Recém-carregado"
    except Exception:
        sync_str = "Hoje"
        
    con.close()
    return df, sync_str


def build_issue_search_options(df, key_column="chave", summary_column="resumo"):
    """Cria sugestões pesquisáveis com a chave Jira e o resumo do item."""
    if key_column not in df.columns or summary_column not in df.columns:
        return []
    options_df = df[[key_column, summary_column]].dropna(subset=[key_column]).drop_duplicates(key_column)
    options = []
    for row in options_df.to_dict("records"):
        key = str(row[key_column]).strip()
        summary_value = row.get(summary_column)
        summary = "" if pd.isna(summary_value) else str(summary_value).strip()
        options.append({"label": f"{key} — {summary}" if summary else key, "value": key})
    return options


def get_latest_jira_comment():
    """Retorna o comentário mais recente se os campos estiverem no feed do Jira."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        columns = {
            row[0].lower(): row[0]
            for row in con.execute("DESCRIBE issues").fetchall()
        }
        body_col = next(
            (columns[name] for name in ("comment", "comment_body", "comment_text") if name in columns),
            None
        )
        author_col = next(
            (columns[name] for name in ("comment_author", "comment_author_name") if name in columns),
            None
        )
        date_col = next(
            (columns[name] for name in ("comment_updated", "comment_created") if name in columns),
            None
        )
        if not body_col or not author_col:
            return None

        quote = lambda name: '"' + name.replace('"', '""') + '"'
        order_by = f"TRY_CAST({quote(date_col)} AS TIMESTAMP) DESC NULLS LAST" if date_col else "1"
        result = con.execute(
            f"""
            SELECT {quote(author_col)}, {quote(body_col)}
            FROM issues
            WHERE NULLIF(TRIM(CAST({quote(body_col)} AS VARCHAR)), '') IS NOT NULL
            ORDER BY {order_by}
            LIMIT 1
            """
        ).fetchone()
        if not result:
            return None
        return {"author": str(result[0] or "Autor não informado"), "body": str(result[1])}
    finally:
        con.close()


def build_latest_comment_card():
    comment = get_latest_jira_comment()
    if comment:
        content = [
            html.Div(comment["author"], className="fw-bold text-dark mb-1"),
            html.Div(comment["body"], style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"})
        ]
    else:
        content = html.Span(
            "Os campos de comentário ainda não estão disponíveis. Habilite Comment Author, Comment Body e Comment Created/Updated na seção Comments do conector Appfire.",
            className="text-muted"
        )
    return dbc.Card([
        dbc.CardHeader([
            html.I(className="fa-regular fa-comment-dots text-primary me-2"),
            html.Span("Último comentário do Jira", className="fw-bold")
        ], className="bg-transparent border-0 pb-0"),
        dbc.CardBody(content, className="pt-2")
    ], className="shadow-sm border-0 rounded-4 mb-4")


def get_cadernos_data():
    """Consulta dados analíticos agregados de cadernos e versões do DuckDB."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    df = con.execute("SELECT * FROM v_cadernos_analytics").df()
    con.close()
    return df


def get_demandas_finalizadas_data():
    """Consulta os Registros de Demanda filhos diretos do épico REGRA-305."""
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    df = con.execute(
        """
        SELECT
            a.*,
            COALESCE(s.total_subtarefas, 0) AS total_subtarefas,
            COALESCE(s.subtarefas_concluidas, 0) AS subtarefas_concluidas
        FROM v_issues_analytics a
        LEFT JOIN (
            SELECT
                parent_issue_key,
                COUNT(*) AS total_subtarefas,
                COUNT(*) FILTER (
                    WHERE resolucao IS NOT NULL AND resolucao != 'Unresolved'
                ) AS subtarefas_concluidas
            FROM issues
            WHERE tipo_de_item = 'Subtarefa'
            GROUP BY parent_issue_key
        ) s ON a.chave = s.parent_issue_key
        WHERE a.parent_key = ?
          AND a.tipo_de_item = 'Registro de Demanda'
        ORDER BY a.data_resolucao, a.chave
        """,
        [DEMANDAS_FINALIZADAS_PARENT_KEY]
    ).df()
    con.close()
    return df


def get_demanda_subtarefas(demanda_key):
    """Consulta as subtarefas diretamente vinculadas a um Registro de Demanda."""
    if not demanda_key:
        return pd.DataFrame()
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    df = con.execute(
        """
        SELECT *
        FROM v_issues_analytics
        WHERE parent_key = ?
          AND tipo_de_item = 'Subtarefa'
        ORDER BY data_criacao, chave
        """,
        [demanda_key]
    ).df()
    con.close()
    return df


def update_sync_state(**changes):
    """Atualiza de forma segura o estado compartilhado da sincronização."""
    with SYNC_STATE_LOCK:
        SYNC_STATE.update(changes)


def get_sync_state():
    """Retorna uma cópia consistente do estado da sincronização."""
    with SYNC_STATE_LOCK:
        return SYNC_STATE.copy()


def describe_sync_error(exc):
    """Converte a cadeia de exceções em uma mensagem segura e acionável."""
    error_types = set()
    seen_errors = set()
    current = exc
    while current is not None and id(current) not in seen_errors:
        seen_errors.add(id(current))
        error_types.add(type(current).__name__)
        current = current.__cause__ or current.__context__

    if "ProxyError" in error_types or "ConnectionError" in error_types:
        return (
            "Falha de conexão com o Jira ou com o proxy corporativo. "
            "Verifique a rede e tente novamente. O snapshot anterior foi preservado."
        )
    if "ReadTimeout" in error_types or "ConnectTimeout" in error_types or "Timeout" in error_types:
        return (
            "O Appfire excedeu o tempo de resposta mesmo após as novas tentativas. "
            "Tente novamente em alguns minutos; o snapshot anterior foi preservado."
        )
    if "HTTPError" in error_types:
        return (
            "O Jira/Appfire retornou um erro HTTP temporário após todas as tentativas. "
            "O snapshot anterior foi preservado."
        )
    if any(name in error_types for name in {"IOException", "TransactionException", "DatabaseError"}):
        return (
            "Não foi possível concluir a transação no DuckDB. "
            "O snapshot anterior foi preservado."
        )
    return (
        f"Falha técnica ({type(exc).__name__}) durante a atualização. "
        "O snapshot anterior foi preservado."
    )


def run_sync_worker():
    """Executa o ETL fora da thread de resposta do Dash."""
    try:
        result = run_etl(progress_callback=lambda event: update_sync_state(**event))
        update_sync_state(
            status="success",
            percent=100,
            message=f"Sincronização concluída: {result['total_issues']} itens carregados",
            result=result,
            completed_at=pd.Timestamp.now().isoformat()
        )
    except Exception as exc:
        error_message = describe_sync_error(exc)
        print(f"❌ Sincronização abortada ({type(exc).__name__})")
        update_sync_state(
            status="error",
            message=error_message,
            result=None,
            completed_at=pd.Timestamp.now().isoformat()
        )


def start_sync_worker():
    """Inicia uma única sincronização e rejeita cliques concorrentes."""
    with SYNC_STATE_LOCK:
        if SYNC_STATE["status"] == "running":
            return False
        SYNC_STATE.update({
            "status": "running",
            "percent": 1,
            "message": "Iniciando conexão com o Jira",
            "result": None,
            "completed_at": None
        })

    threading.Thread(target=run_sync_worker, daemon=True, name="jira-sync").start()
    return True


def build_sync_feedback(state):
    """Monta o feedback visual de progresso ou resultado do ETL."""
    if state["status"] == "running":
        percent = int(state.get("percent", 0))
        return dbc.Alert([
            html.Div([
                html.I(className="fa-solid fa-arrows-rotate fa-spin me-2"),
                html.Strong("Atualizando Jira: "),
                html.Span(state.get("message", "Processando..."))
            ], className="mb-2"),
            dbc.Progress(
                value=percent,
                label=f"{percent}%",
                striped=True,
                animated=True,
                color="primary",
                style={"height": "20px"}
            )
        ], color="info", className="shadow-sm mb-3")

    if state["status"] == "success":
        result = state.get("result") or {}
        entities = result.get("entities", {})
        details = " · ".join(
            f"{name}: {count}" for name, count in entities.items()
        )
        return dbc.Alert([
            html.Div([
                html.I(className="fa-solid fa-circle-check me-2"),
                html.Strong(state.get("message", "Jira atualizado com sucesso."))
            ]),
            html.Small(details, className="d-block mt-1") if details else None
        ], color="success", dismissable=True, duration=12000, className="shadow-sm mb-3")

    if state["status"] == "error":
        return dbc.Alert([
            html.I(className="fa-solid fa-triangle-exclamation me-2"),
            html.Strong("Atualização não concluída. "),
            html.Span(state.get("message", "O snapshot anterior foi preservado."))
        ], color="danger", dismissable=True, className="shadow-sm mb-3")

    return no_update


# Paleta corporativa CCEE
COLORS = {
    "primary": "#1E3A8A",      # Azul Marinho CCEE
    "secondary": "#2563EB",    # Azul Real
    "success": "#10B981",      # Verde Concluído
    "warning": "#F59E0B",      # Âmbar Aberto
    "danger": "#EF4444",       # Vermelho Crítico / Alerta
    "info": "#06B6D4",         # Ciano
    "purple": "#8B5CF6",       # Roxo Executivo
    "card_bg": "#FFFFFF",
    "bg": "#F8FAFC",
    "text": "#1E293B",
    "muted": "#64748B"
}


def build_kpi_card(title, value, subtitle, icon, color):
    """Gera um cartão de métrica moderno com ícone e borda temática."""
    return dbc.Card(
        dbc.CardBody([
            dbc.Row([
                dbc.Col([
                    html.H6(title, className="text-uppercase text-muted fw-bold mb-1", style={"fontSize": "0.72rem", "letterSpacing": "0.05em"}),
                    html.H3(value, className=f"fw-bold text-{color} mb-0", style={"fontSize": "1.75rem"}),
                    html.Small(subtitle, className="text-muted", style={"fontSize": "0.72rem"})
                ], width=9),
                dbc.Col([
                    html.Div(
                        html.I(className=f"{icon} fa-xl text-{color}"),
                        className=f"d-flex align-items-center justify-content-center bg-{color} bg-opacity-10 rounded-circle",
                        style={"width": "48px", "height": "48px"}
                    )
                ], width=3, className="d-flex justify-content-end align-items-center")
            ])
        ]),
        className="shadow-sm border-0 h-100 kpi-card",
        style={"borderRadius": "12px", "borderTop": f"4px solid var(--bs-{color})"}
    )


def build_demandas_finalizadas_layout():
    """Monta a aba dedicada aos Registros de Demanda do épico REGRA-305."""
    return html.Div([
        dbc.Alert([
            html.Div([
                html.I(className="fa-solid fa-box-archive fa-xl me-3 text-success"),
                html.Div([
                    html.Strong("Demandas finalizadas — Épico REGRA-305: ", className="d-block mb-1"),
                    html.Span(
                        "Acompanhamento dos Registros de Demanda agrupados em “Outras Demandas Finalizadas”, "
                        "incluindo itens Finalizados, Resolvidos, Resolvidos com ressalvas e Cancelados."
                    )
                ])
            ], className="d-flex align-items-center")
        ], color="light", className="border shadow-sm mb-4 rounded-4"),

        dbc.Card(
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label(
                            [html.I(className="fa-solid fa-circle-check me-1 text-success"), "Resolução:"],
                            className="fw-semibold text-muted small mb-1"
                        ),
                        localized_dropdown(
                            id="dem-filter-resolucao",
                            placeholder="Todas as resoluções",
                            multi=True,
                            className="shadow-none"
                        )
                    ], md=4, sm=12, className="mb-2 mb-md-0"),
                    dbc.Col([
                        html.Label(
                            [html.I(className="fa-solid fa-flag me-1 text-warning"), "Prioridade:"],
                            className="fw-semibold text-muted small mb-1"
                        ),
                        localized_dropdown(
                            id="dem-filter-prioridade",
                            placeholder="Todas as prioridades",
                            multi=True,
                            className="shadow-none"
                        )
                    ], md=4, sm=12, className="mb-2 mb-md-0"),
                    dbc.Col([
                        html.Label(
                            [html.I(className="fa-solid fa-user me-1 text-primary"), "Relator:"],
                            className="fw-semibold text-muted small mb-1"
                        ),
                        localized_dropdown(
                            id="dem-filter-relator",
                            placeholder="Todos os relatores",
                            multi=True,
                            className="shadow-none"
                        )
                    ], md=4, sm=12)
                ])
            ]),
            className="shadow-sm border-0 mb-4 rounded-4"
        ),

        dbc.Row(id="dem-kpis-row", className="mb-4 g-3"),

        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-chart-pie text-success me-2"),
                            html.Span("Distribuição por Resolução", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="dem-chart-resolucao", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=4, md=12, className="mb-4"),
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-stopwatch text-primary me-2"),
                            html.Span("Lead Time por Demanda", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="dem-chart-leadtime", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=8, md=12, className="mb-4")
        ]),

        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-calendar-check text-secondary me-2"),
                            html.Span("Conclusões por Mês", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="dem-chart-fluxo", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=6, md=12, className="mb-4"),
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-users text-info me-2"),
                            html.Span("Demandas por Relator", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="dem-chart-relatores", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=6, md=12, className="mb-4")
        ]),

        dbc.Card([
            dbc.CardHeader(
                dbc.Row([
                    dbc.Col(
                        html.Div([
                            html.I(className="fa-solid fa-table-list text-primary me-2"),
                            html.Span("Relação das Demandas do REGRA-305", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        width="auto"
                    ),
                    dbc.Col(
                        dbc.Button(
                            [html.I(className="fa-solid fa-download me-1"), "Exportar CSV Demandas"],
                            id="btn-dem-download-csv",
                            size="sm",
                            color="secondary",
                            outline=True,
                            className="rounded-pill"
                        ),
                        className="d-flex justify-content-end"
                    )
                ], align="center"),
                className="bg-transparent border-0 pt-3 px-3"
            ),
            dbc.CardBody([
                localized_dropdown(
                    id="dem-search-resumo",
                    options=[],
                    placeholder="Buscar demanda por chave ou resumo...",
                    clearable=True,
                    className="mb-3"
                ),
                dash_table.DataTable(
                    id="dem-tabela",
                    page_size=6,
                    sort_action="native",
                    filter_action="native",
                    style_as_list_view=True,
                    style_header={
                        "backgroundColor": "#F1F5F9", "fontWeight": "bold",
                        "color": COLORS["text"], "fontSize": "0.85rem",
                        "border": "none", "padding": "12px"
                    },
                    style_cell={
                        "fontSize": "0.85rem", "fontFamily": "Segoe UI, sans-serif",
                        "padding": "10px 12px", "textAlign": "left", "border": "none",
                        "whiteSpace": "normal", "height": "auto", "minWidth": "110px"
                    },
                    style_cell_conditional=[
                        {"if": {"column_id": "resumo"}, "minWidth": "320px", "width": "38%"}
                    ],
                    style_data_conditional=[
                        {"if": {"filter_query": '{resolucao} = "Cancelado"'}, "backgroundColor": "#FEF2F2", "color": "#991B1B"},
                        {"if": {"filter_query": '{resolucao} = "Resolvido com ressalvas"'}, "backgroundColor": "#FFFBEB", "color": "#92400E"},
                        {"if": {"filter_query": '{resolucao} = "Finalizado"'}, "backgroundColor": "#F0FDF4", "color": "#166534"}
                    ],
                    style_table={"overflowX": "auto"}
                ),
                dcc.Download(id="dem-download-dataframe-csv")
            ])
        ], className="shadow-sm border-0 rounded-4 mb-4"),

        dbc.Card([
            dbc.CardHeader(
                html.Div([
                    html.I(className="fa-solid fa-list-check text-primary me-2"),
                    html.Span("Detalhamento das Subtarefas por Demanda", className="fw-bold")
                ], className="d-flex align-items-center"),
                className="bg-transparent border-0 pt-3 px-3"
            ),
            dbc.CardBody([
                localized_dropdown(
                    id="dem-select-drilldown",
                    placeholder="Selecione uma demanda para visualizar suas subtarefas...",
                    className="shadow-none mb-3"
                ),
                dash_table.DataTable(
                    id="dem-drilldown-tabela",
                    page_size=6,
                    sort_action="native",
                    filter_action="native",
                    style_as_list_view=True,
                    style_header={
                        "backgroundColor": "#F8FAFC", "fontWeight": "bold",
                        "color": COLORS["text"], "fontSize": "0.82rem",
                        "border": "none", "padding": "10px"
                    },
                    style_cell={
                        "fontSize": "0.82rem", "fontFamily": "Segoe UI, sans-serif",
                        "padding": "8px 10px", "textAlign": "left", "border": "none",
                        "whiteSpace": "normal", "height": "auto"
                    },
                    style_data_conditional=[
                        {"if": {"filter_query": '{situacao} = "Concluído"'}, "color": "#166534"},
                        {"if": {"filter_query": '{situacao} = "Em Aberto"'}, "color": "#B45309", "fontWeight": "bold"}
                    ],
                    style_table={"overflowX": "auto"}
                )
            ])
        ], className="shadow-sm border-0 rounded-4 mb-5")
    ])


# Layout Principal com Abas
app.layout = html.Div(
    id="app-shell",
    className="theme-light",
    style={"height": "100vh", "minHeight": 0, "overflow": "hidden", "display": "flex", "flexDirection": "column"},
    children=[
        # Barra de Navegação Superior (Header)
        dbc.Navbar(
            dbc.Container([
                dbc.Row([
                    dbc.Col(
                        html.Div([
                            html.I(className="fa-solid fa-chart-line fa-xl text-primary me-2"),
                            html.Span("GRPC", className="fw-bold fs-4 text-primary me-2"),
                            html.Span("| Dashboard das Demandas de Regras", className="fs-5 text-dark fw-semibold")
                        ], className="d-flex align-items-center"),
                        width="auto"
                    ),
                ], align="center"),
                dbc.Row([
                    dbc.Col(
                        html.Div([
                            html.Span(id="sync-badge", className="badge bg-light text-dark border me-3 py-2 px-3", style={"fontSize": "0.85rem"}),
                            dbc.Button(
                                [html.I(className="fa-solid fa-arrows-rotate me-2"), "Atualizar"],
                                id="btn-sync",
                                color="primary",
                                size="sm",
                                className="fw-semibold px-3 py-2 shadow-sm rounded-pill"
                            )
                        ], className="d-flex align-items-center"),
                        width="auto"
                    )
                ], align="center")
            ], fluid=True, className="px-4"),
            color="white",
            className="shadow-sm py-3 border-bottom sticky-top flex-shrink-0"
        ),

        # Loading da Sincronização
        dcc.Loading(
            id="loading-sync",
            type="circle",
            color=COLORS["primary"],
            children=html.Div(id="sync-output", className="px-4")
        ),

        # Navegação lateral e área de conteúdo
        html.Div([
            html.Aside([
                dbc.Button(
                    html.I(className="fa-solid fa-angles-left"),
                    id="sidebar-toggle",
                    color="link",
                    className="sidebar-toggle",
                    title="Recolher navegação"
                ),
                dbc.Tabs([
                    dbc.Tab(label="Cadernos e Versões", tab_id="tab-cadernos"),
                    dbc.Tab(label="Demandas Finalizadas", tab_id="tab-demandas"),
                    dbc.Tab(label="Consultas Técnicas", tab_id="tab-consultas"),
                    dbc.Tab(label="Visão Geral", tab_id="tab-geral")
                ], id="main-tabs", active_tab="tab-cadernos", className="sidebar-main-tabs"),
                html.Div(className="sidebar-divider"),
                dbc.Tabs(id="section-tabs", className="sidebar-section-tabs"),
                html.Div(className="sidebar-spacer"),
                dbc.Button(
                    [html.I(className="fa-solid fa-moon sidebar-icon"), html.Span("Modo escuro", className="sidebar-label")],
                    id="dark-mode-toggle",
                    color="link",
                    className="sidebar-theme-toggle"
                )
            ], id="sidebar", className="sidebar sidebar-expanded"),
            html.Main(id="section-content", className="section-content")
        ], className="app-body"),

        # Armazenamento de disparo de carga
        dcc.Store(id="store-data-trigger"),
        dcc.Store(id="theme-store", storage_type="local", data="light"),
        dcc.Interval(id="sync-progress-interval", interval=1000, disabled=True, n_intervals=0)
    ]
)


# O tema escolhido fica salvo no navegador e é reaplicado ao abrir o painel.
@app.callback(
    Output("theme-store", "data"),
    Input("dark-mode-toggle", "n_clicks"),
    State("theme-store", "data"),
    prevent_initial_call=True
)
def toggle_theme(_, current_theme):
    return "dark" if current_theme != "dark" else "light"


@app.callback(
    [Output("app-shell", "className"), Output("dark-mode-toggle", "children")],
    Input("theme-store", "data")
)
def apply_theme(theme):
    if theme == "dark":
        return "theme-dark", [html.I(className="fa-solid fa-sun sidebar-icon"), html.Span("Modo claro", className="sidebar-label")]
    return "theme-light", [html.I(className="fa-solid fa-moon sidebar-icon"), html.Span("Modo escuro", className="sidebar-label")]


@app.callback(
    [Output("sidebar", "className"), Output("sidebar-toggle", "title")],
    Input("sidebar-toggle", "n_clicks"),
    State("sidebar", "className"),
    prevent_initial_call=True
)
def toggle_sidebar(_, current_class):
    collapsed = "sidebar-collapsed" in (current_class or "")
    if collapsed:
        return "sidebar sidebar-expanded", "Recolher navegação"
    return "sidebar sidebar-collapsed", "Expandir navegação"


# Callback para renderizar o layout da aba selecionada
SECTION_TITLES = {
    "tab-cadernos": ["Filtros e indicadores", "Avanço por caderno", "Escopo e status", "Cadernos", "Detalhamento"],
    "tab-demandas": ["Filtros e indicadores", "Status e resolução", "Evolução e relatores", "Demandas", "Detalhamento"],
    "tab-consultas": ["Filtros e indicadores", "SLA, temas e aging", "Lead time e fluxo", "Demandas"],
    "tab-geral": ["Filtros e indicadores", "Tipos e prioridades", "Resolução e evolução", "Demandas"]
}
SECTION_GROUPS = {
    "tab-cadernos": [[0, 1, 2], [3], [4], [5], [6]],
    "tab-demandas": [[0, 1, 2], [3], [4], [5], [6]],
    "tab-consultas": [[0, 1, 2], [3], [4], [5]],
    "tab-geral": [[0, 1], [2], [3], [4]]
}


def get_layout_sections(active_tab, layout):
    children = layout.children if isinstance(layout, html.Div) else [layout]
    groups = SECTION_GROUPS.get(active_tab)
    if not groups:
        groups = [[index] for index in range(len(children))]
    return [[children[index] for index in group if index < len(children)] for group in groups]


@app.callback(
    [
        Output("section-tabs", "children"),
        Output("section-tabs", "active_tab"),
        Output("section-content", "children")
    ],
    Input("main-tabs", "active_tab")
)
def update_section_navigation(active_tab):
    layout = _build_tab_layout(active_tab)
    sections = get_layout_sections(active_tab, layout)
    titles = SECTION_TITLES.get(active_tab, SECTION_TITLES["tab-geral"])
    tabs = [
        dbc.Tab(
            label=titles[index] if index < len(titles) else f"Seção {index + 1}",
            tab_id=f"section-{index}",
            label_class_name="fw-semibold"
        )
        for index in range(len(sections))
    ]
    pages = [
        html.Div(
            content,
            id={"type": "section-page", "index": index},
            className="section-page is-active" if index == 0 else "section-page is-preloaded"
        )
        for index, content in enumerate(sections)
    ]
    return tabs, "section-0", pages


@app.callback(
    Output({"type": "section-page", "index": ALL}, "className"),
    Input("section-tabs", "active_tab"),
    State({"type": "section-page", "index": ALL}, "id")
)
def render_tab_content(active_section, page_ids):
    page_count = len(page_ids or [])
    index = int(active_section.split("-")[-1]) if active_section and active_section.startswith("section-") else 0
    if index >= page_count:
        index = 0
    return [
        "section-page is-active" if page_index == index else "section-page is-preloaded"
        for page_index in range(page_count)
    ]


def _build_tab_layout(active_tab):
    if active_tab == "tab-cadernos":
        # Layout da Aba 3: Cadernos e Versões
        return html.Div([
            # Descrição Executiva do Módulo
            dbc.Alert([
                html.Div([
                    html.I(className="fa-solid fa-book-bookmark fa-xl me-3 text-primary"),
                    html.Div([
                        html.Strong("Acompanhamento de Cadernos de Regras e Versões Normativas (2026 / 2027): ", className="d-block mb-1"),
                        html.Span("Visão de portfólio executivo para monitoramento da entrega de cada Caderno Pai, avanço global dos ciclos regulatórios e ondas de escopo CLIQ 16 e CLIQ 17.")
                    ])
                ], className="d-flex align-items-center")
            ], color="light", className="border shadow-sm mb-4 rounded-4"),

            # Barra de Filtros de Cadernos
            dbc.Card(
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-code-branch me-1 text-primary"), "Versão Regulatória:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="cad-filter-versao",
                                options=[
                                    {"label": "Todas as Versões", "value": "Todos"},
                                    {"label": "Versão 2026", "value": "Versão 2026"},
                                    {"label": "Versão 2027", "value": "Versão 2027"},
                                    {"label": "Outros / Operacional", "value": "Outros / Operacional"}
                                ],
                                value="Todos",
                                clearable=False,
                                className="shadow-none"
                            )
                        ], md=4, sm=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-circle-check me-1 text-success"), "Status do Caderno:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="cad-filter-status",
                                placeholder="Todos os status",
                                multi=True,
                                className="shadow-none"
                            )
                        ], md=4, sm=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-layer-group me-1 text-info"), "Tipo de Item Pai:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="cad-filter-tipo",
                                placeholder="Todos os tipos",
                                multi=True,
                                className="shadow-none"
                            )
                        ], md=4, sm=12)
                    ])
                ]),
                className="shadow-sm border-0 mb-4 rounded-4"
            ),

            # Linha de KPIs de Cadernos
            dbc.Row(id="cad-kpis-row", className="mb-4 g-3 cad-kpis-row"),

            # Linha 1 de Gráficos (Termômetro por Caderno & Comparativo Versões)
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-bars-progress text-primary me-2"),
                                html.Span("Termômetro de Conclusão por Caderno (Top Volumes)", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="cad-chart-progresso", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=7, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-scale-balanced text-secondary me-2"),
                                html.Span("Avanço Estratégico: Versão 2026 vs. Versão 2027", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="cad-chart-versoes", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=5, md=12, className="mb-4")
            ]),

            # Linha 2 de Gráficos (Escopo CLIQ & Status dos Cadernos)
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-code-compare text-info me-2"),
                                html.Span("Matriz de Escopo CLIQ 16 (2026) vs. CLIQ 17 (2027)", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="cad-chart-cliq", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=6, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-chart-pie text-warning me-2"),
                                html.Span("Distribuição dos Cadernos por Status Operacional", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="cad-chart-status", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=6, md=12, className="mb-4")
            ]),

            # Tabela Executiva de Cadernos de Regras
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            dbc.Row([
                                dbc.Col(
                                    html.Div([
                                        html.I(className="fa-solid fa-table-list text-primary me-2"),
                                        html.Span("Relação Executiva de Cadernos e Entregas", className="fw-bold")
                                    ], className="d-flex align-items-center"),
                                    width="auto"
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        [html.I(className="fa-solid fa-download me-1"), "Exportar CSV Cadernos"],
                                        id="btn-cad-download-csv",
                                        size="sm",
                                        color="secondary",
                                        outline=True,
                                        className="rounded-pill"
                                    ),
                                    className="d-flex justify-content-end"
                                )
                            ], align="center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody([
                            localized_dropdown(
                                id="cad-search-caderno",
                                options=[],
                                placeholder="Buscar caderno por chave ou nome...",
                                clearable=True,
                                className="mb-3"
                            ),
                            dash_table.DataTable(
                                id="cad-tabela",
                                page_size=6,
                                sort_action="native",
                                filter_action="native",
                                style_as_list_view=True,
                                style_header={
                                    "backgroundColor": "#F1F5F9",
                                    "fontWeight": "bold",
                                    "color": COLORS["text"],
                                    "fontSize": "0.85rem",
                                    "border": "none",
                                    "padding": "12px"
                                },
                                style_cell={
                                    "fontSize": "0.85rem",
                                    "fontFamily": "Segoe UI, sans-serif",
                                    "padding": "10px 12px",
                                    "textAlign": "left",
                                    "border": "none",
                                    "whiteSpace": "normal",
                                    "height": "auto"
                                },
                                style_data_conditional=[
                                    {
                                        "if": {"filter_query": '{taxa_conclusao_pct} = 100'},
                                        "backgroundColor": "#F0FDF4",
                                        "color": "#166534",
                                        "fontWeight": "bold"
                                    },
                                    {
                                        "if": {"filter_query": '{taxa_conclusao_pct} < 30 && {total_demandas} >= 5'},
                                        "backgroundColor": "#FFFBEB",
                                        "color": "#92400E"
                                    }
                                ],
                                style_table={"overflowX": "auto"}
                            ),
                            dcc.Download(id="cad-download-dataframe-csv")
                        ])
                    ], className="shadow-sm border-0 rounded-4 mb-4")
                ], width=12)
            ]),

            # Seção de Drill-Down: Detalhamento de Demandas do Caderno
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-magnifying-glass-chart text-primary me-2"),
                                html.Span("Detalhamento Operacional: Selecione um Caderno para Inspecionar suas Demandas", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody([
                            dbc.Row([
                                dbc.Col([
                                    localized_dropdown(
                                        id="cad-select-drilldown",
                                        placeholder="Clique para selecionar ou buscar um Caderno / Épico...",
                                        className="shadow-none mb-3"
                                    )
                                ], width=12)
                            ]),
                            dash_table.DataTable(
                                id="cad-drilldown-tabela",
                                page_size=6,
                                sort_action="native",
                                filter_action="native",
                                style_as_list_view=True,
                                style_header={
                                    "backgroundColor": "#F8FAFC",
                                    "fontWeight": "bold",
                                    "color": COLORS["text"],
                                    "fontSize": "0.82rem",
                                    "border": "none",
                                    "padding": "10px"
                                },
                                style_cell={
                                    "fontSize": "0.82rem",
                                    "fontFamily": "Segoe UI, sans-serif",
                                    "padding": "8px 10px",
                                    "textAlign": "left",
                                    "border": "none",
                                    "whiteSpace": "normal"
                                },
                                style_data_conditional=[
                                    {
                                        "if": {"filter_query": '{situacao} = "Concluído"'},
                                        "color": "#166534"
                                    },
                                    {
                                        "if": {"filter_query": '{situacao} = "Em Aberto"'},
                                        "color": "#B45309",
                                        "fontWeight": "bold"
                                    }
                                ],
                                style_table={"overflowX": "auto"}
                            )
                        ])
                    ], className="shadow-sm border-0 rounded-4 mb-5")
                ], width=12)
            ])
        ])

    elif active_tab == "tab-demandas":
        return build_demandas_finalizadas_layout()

    elif active_tab == "tab-consultas":
        # Layout da Aba 2: Consultas Técnicas
        return html.Div([
            # Descrição do Módulo
            dbc.Alert([
                html.Div([
                    html.I(className="fa-solid fa-circle-info fa-lg me-2 text-primary"),
                    html.Strong("Módulo de Consultas Técnicas: "),
                    html.Span("Monitoramento especializado de tempo de ciclo (Lead Time), fila de espera (Aging), metas dinâmicas de SLA, conformidade e temas regulatórios mais frequentes.")
                ], className="d-flex align-items-center")
            ], color="light", className="border shadow-sm mb-4 rounded-4"),

            # Barra de Filtros da Consulta Técnica
            dbc.Card(
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-circle-check me-1 text-success"), "Situação:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-situacao",
                                options=[
                                    {"label": "Todas as Situações", "value": "Todos"},
                                    {"label": "Em Aberto (Pendentes)", "value": "Em Aberto"},
                                    {"label": "Concluídas (Finalizadas)", "value": "Concluído"}
                                ],
                                value="Todos",
                                clearable=False,
                                className="shadow-none"
                            )
                        ], md=4, sm=6, xs=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-hourglass-half me-1 text-warning"), "Faixa de Aging (Dias em Aberto):"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-aging",
                                options=[
                                    {"label": "Todas as Faixas", "value": "Todos"},
                                    {"label": "Até 15 dias (Recentes)", "value": "Até 15 dias"},
                                    {"label": "16 a 30 dias (Atenção)", "value": "16 a 30 dias"},
                                    {"label": "31 a 60 dias (Gargalo)", "value": "31 a 60 dias"},
                                    {"label": "Mais de 60 dias (Crítico)", "value": "Mais de 60 dias"}
                                ],
                                value="Todos",
                                clearable=False,
                                className="shadow-none"
                            )
                        ], md=4, sm=6, xs=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-user me-1 text-primary"), "Demandante / Relator:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-relator",
                                placeholder="Todos os solicitantes",
                                multi=True,
                                className="shadow-none"
                            )
                        ], md=4, sm=12, xs=12)
                    ]),
                    dbc.Row([
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-bullseye me-1 text-danger"), "Meta de SLA (Prazo em Dias):"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-meta-sla",
                                options=[
                                    {"label": "3 dias (Exigente)", "value": 3},
                                    {"label": "5 dias (Padrão Operacional)", "value": 5},
                                    {"label": "7 dias (1 semana)", "value": 7},
                                    {"label": "10 dias (Duas semanas)", "value": 10},
                                    {"label": "15 dias (Tolerante)", "value": 15},
                                    {"label": "30 dias (Mensal)", "value": 30}
                                ],
                                value=5,
                                clearable=False,
                                className="shadow-none"
                            )
                        ], md=4, sm=6, xs=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-traffic-light me-1 text-info"), "Conformidade de SLA:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-status-sla",
                                options=[
                                    {"label": "Todos os Status de SLA", "value": "Todos"},
                                    {"label": "✓ No Prazo (SLA OK)", "value": "No Prazo"},
                                    {"label": "⚠ Fora do Prazo (SLA Estourado)", "value": "Fora do Prazo"}
                                ],
                                value="Todos",
                                clearable=False,
                                className="shadow-none"
                            )
                        ], md=4, sm=6, xs=12, className="mb-2 mb-md-0"),
                        dbc.Col([
                            html.Label([html.I(className="fa-solid fa-tags me-1 text-secondary"), "Tema / Regra Regulatória:"], className="fw-semibold text-muted small mb-1"),
                            localized_dropdown(
                                id="ct-filter-tema",
                                placeholder="Todos os temas regulatórios",
                                clearable=True,
                                className="shadow-none"
                            )
                        ], md=4, sm=12, xs=12)
                    ], className="mt-3")
                ]),
                className="shadow-sm border-0 mb-4 rounded-4"
            ),

            # Linha de KPIs de Consulta Técnica
            dbc.Row(id="ct-kpis-row", className="mb-4 g-3"),

            # Linha 1 de Gráficos (SLA Gauge, Temas Regulatórios & Aging)
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-gauge-high text-success me-2"),
                                html.Span("Conformidade com a Meta de SLA", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-sla-gauge", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-tags text-primary me-2"),
                                html.Span("Temas Regulatórios Mais Consultados", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-temas", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-clock-rotate-left text-warning me-2"),
                                html.Span("Envelhecimento do Backlog (Aging)", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-aging", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4")
            ]),

            # Linha 2 de Gráficos (Top Demandantes, Lead Time vs SLA & Fluxo Mensal)
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-users text-primary me-2"),
                                html.Span("Top Demandantes de Consultas Técnicas", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-demandantes", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-bolt text-success me-2"),
                                html.Span("Tempo de Resolução vs. Meta SLA", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-leadtime", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4"),

                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            html.Div([
                                html.I(className="fa-solid fa-chart-column text-secondary me-2"),
                                html.Span("Fluxo Mensal: Entradas vs. Conclusões", className="fw-bold")
                            ], className="d-flex align-items-center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody(dcc.Graph(id="ct-chart-fluxo", config={"displayModeBar": False}))
                    ], className="shadow-sm border-0 rounded-4 h-100")
                ], lg=4, md=12, className="mb-4")
            ]),

            # Tabela Específica de Consultas Técnicas
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader(
                            dbc.Row([
                                dbc.Col(
                                    html.Div([
                                        html.I(className="fa-solid fa-table-list text-primary me-2"),
                                        html.Span("Relação Detalhada de Consultas Técnicas", className="fw-bold")
                                    ], className="d-flex align-items-center"),
                                    width="auto"
                                ),
                                dbc.Col(
                                    dbc.Button(
                                        [html.I(className="fa-solid fa-download me-1"), "Exportar CSV Consultas"],
                                        id="btn-ct-download-csv",
                                        size="sm",
                                        color="secondary",
                                        outline=True,
                                        className="rounded-pill"
                                    ),
                                    className="d-flex justify-content-end"
                                )
                            ], align="center"),
                            className="bg-transparent border-0 pt-3 px-3"
                        ),
                        dbc.CardBody([
                            localized_dropdown(
                                id="ct-search-consulta",
                                options=[],
                                placeholder="Buscar consulta por chave ou resumo...",
                                clearable=True,
                                className="mb-3"
                            ),
                            dash_table.DataTable(
                                id="ct-tabela-demandas",
                                page_size=6,
                                sort_action="native",
                                filter_action="native",
                                style_as_list_view=True,
                                style_header={
                                    "backgroundColor": "#F1F5F9",
                                    "fontWeight": "bold",
                                    "color": COLORS["text"],
                                    "fontSize": "0.85rem",
                                    "border": "none",
                                    "padding": "12px"
                                },
                                style_cell={
                                    "fontSize": "0.85rem",
                                    "fontFamily": "Segoe UI, sans-serif",
                                    "padding": "10px 12px",
                                    "textAlign": "left",
                                    "border": "none",
                                    "whiteSpace": "normal",
                                    "height": "auto"
                                },
                                style_data_conditional=[
                                    {
                                        "if": {"filter_query": '{status_sla_simples} = "No Prazo" && {situacao} = "Concluído"'},
                                        "backgroundColor": "#F0FDF4",
                                        "color": "#166534"
                                    },
                                    {
                                        "if": {"filter_query": '{status_sla_simples} = "Fora do Prazo" && {situacao} = "Concluído"'},
                                        "backgroundColor": "#FFFBEB",
                                        "color": "#92400E"
                                    },
                                    {
                                        "if": {"filter_query": '{status_sla_simples} = "Fora do Prazo" && {situacao} = "Em Aberto"'},
                                        "backgroundColor": "#FEF2F2",
                                        "color": "#991B1B",
                                        "fontWeight": "bold"
                                    },
                                    {
                                        "if": {"filter_query": '{status_sla_simples} = "No Prazo" && {situacao} = "Em Aberto"'},
                                        "backgroundColor": "#F0FDF4",
                                        "color": "#166534"
                                    },
                                    {
                                        "if": {
                                            "column_id": "status_sla",
                                            "filter_query": '{status_sla_simples} = "No Prazo"'
                                        },
                                        "fontWeight": "bold",
                                        "color": "#15803D"
                                    },
                                    {
                                        "if": {
                                            "column_id": "status_sla",
                                            "filter_query": '{status_sla_simples} = "Fora do Prazo"'
                                        },
                                        "fontWeight": "bold",
                                        "color": "#DC2626"
                                    }
                                ],
                                style_table={"overflowX": "auto"}
                            ),
                            dcc.Download(id="ct-download-dataframe-csv")
                        ])
                    ], className="shadow-sm border-0 rounded-4 mb-5")
                ], width=12)
            ])
        ])

    # Layout da Aba 1: Visão Geral de Todas as Demandas
    return html.Div([
        # Linha de Filtros
        dbc.Card(
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label([html.I(className="fa-solid fa-filter me-1 text-primary"), "Tipo de Item:"], className="fw-semibold text-muted small mb-1"),
                        localized_dropdown(
                            id="filter-tipo",
                            placeholder="Todos os tipos",
                            multi=True,
                            className="shadow-none"
                        )
                    ], md=4, sm=12, className="mb-2 mb-md-0"),
                    dbc.Col([
                        html.Label([html.I(className="fa-solid fa-flag me-1 text-warning"), "Prioridade:"], className="fw-semibold text-muted small mb-1"),
                        localized_dropdown(
                            id="filter-prioridade",
                            placeholder="Todas as prioridades",
                            multi=True,
                            className="shadow-none"
                        )
                    ], md=4, sm=12, className="mb-2 mb-md-0"),
                    dbc.Col([
                        html.Label([html.I(className="fa-solid fa-circle-check me-1 text-success"), "Situação:"], className="fw-semibold text-muted small mb-1"),
                        localized_dropdown(
                            id="filter-situacao",
                            options=[
                                {"label": "Todos", "value": "Todos"},
                                {"label": "Em Aberto", "value": "Em Aberto"},
                                {"label": "Concluído", "value": "Concluído"}
                            ],
                            value="Todos",
                            clearable=False,
                            className="shadow-none"
                        )
                    ], md=4, sm=12)
                ])
            ]),
            className="shadow-sm border-0 mb-4 rounded-4"
        ),

        # Linha de Cards de KPI
        dbc.Row(id="kpi-cards-row", className="mb-4 g-3"),

        # Linha de Gráficos Superiores
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-bars-staggered text-primary me-2"),
                            html.Span("Demandas por Tipo de Item", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="chart-tipos", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=5, md=12, className="mb-4"),

            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-chart-pie text-primary me-2"),
                            html.Span("Distribuição por Prioridade", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="chart-prioridade", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=3, md=6, className="mb-4"),

            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-list-check text-primary me-2"),
                            html.Span("Status de Resolução", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="chart-resolucao", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=4, md=6, className="mb-4"),
        ]),

        # Linha de Gráficos Inferiores
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-chart-line text-primary me-2"),
                            html.Span("Evolução Histórica de Criação de Demandas", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="chart-evolucao", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=7, md=12, className="mb-4"),

            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        html.Div([
                            html.I(className="fa-solid fa-stopwatch text-primary me-2"),
                            html.Span("Lead Time Médio por Tipo (Dias)", className="fw-bold")
                        ], className="d-flex align-items-center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody(dcc.Graph(id="chart-leadtime", config={"displayModeBar": False}))
                ], className="shadow-sm border-0 rounded-4 h-100")
            ], lg=5, md=12, className="mb-4"),
        ]),

        # Tabela de Dados Geral
        dbc.Row([
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(
                        dbc.Row([
                            dbc.Col(
                                html.Div([
                                    html.I(className="fa-solid fa-table text-primary me-2"),
                                    html.Span("Relação Detalhada de Demandas", className="fw-bold")
                                ], className="d-flex align-items-center"),
                                width="auto"
                            ),
                            dbc.Col(
                                dbc.Button(
                                    [html.I(className="fa-solid fa-download me-1"), "Exportar CSV"],
                                    id="btn-download-csv",
                                    size="sm",
                                    color="secondary",
                                    outline=True,
                                    className="rounded-pill"
                                ),
                                className="d-flex justify-content-end"
                            )
                        ], align="center"),
                        className="bg-transparent border-0 pt-3 px-3"
                    ),
                    dbc.CardBody([
                        localized_dropdown(
                            id="geral-search-resumo",
                            options=[],
                            placeholder="Buscar demanda por chave ou resumo...",
                            clearable=True,
                            className="mb-3"
                        ),
                        dash_table.DataTable(
                            id="tabela-demandas",
                            page_size=6,
                            sort_action="native",
                            filter_action="native",
                            style_as_list_view=True,
                            style_header={
                                "backgroundColor": "#F1F5F9",
                                "fontWeight": "bold",
                                "color": COLORS["text"],
                                "fontSize": "0.85rem",
                                "border": "none",
                                "padding": "12px"
                            },
                            style_cell={
                                "fontSize": "0.85rem",
                                "fontFamily": "Segoe UI, sans-serif",
                                "padding": "10px 12px",
                                "textAlign": "left",
                                "border": "none",
                                "whiteSpace": "normal",
                                "height": "auto"
                            },
                            style_data_conditional=[
                                {
                                    "if": {"filter_query": '{situacao} = "Concluído"'},
                                    "backgroundColor": "#F0FDF4",
                                    "color": "#166534"
                                },
                                {
                                    "if": {"filter_query": '{situacao} = "Em Aberto"'},
                                    "backgroundColor": "#FFFBEB",
                                    "color": "#92400E"
                                }
                            ],
                            style_table={"overflowX": "auto"}
                        ),
                        dcc.Download(id="download-dataframe-csv")
                    ])
                ], className="shadow-sm border-0 rounded-4 mb-5")
            ], width=12)
        ])
    ])


# Callback para opções de filtros gerais e badge
@app.callback(
    [
        Output("filter-tipo", "options"),
        Output("filter-prioridade", "options"),
        Output("sync-badge", "children"),
        Output("geral-search-resumo", "options")
    ],
    [Input("store-data-trigger", "data")],
    prevent_initial_call=False
)
def populate_general_dropdowns(_):
    df, sync_time = get_data()
    tipos = sorted([t for t in df["tipo_de_item"].dropna().unique()])
    prioridades = sorted([p for p in df["prioridade"].dropna().unique()])
    
    tipo_opts = [{"label": t, "value": t} for t in tipos]
    prioridade_opts = [{"label": p, "value": p} for p in prioridades]
    badge_text = f"Última Carga: {sync_time} ({len(df)} itens)"
    
    return tipo_opts, prioridade_opts, badge_text, build_issue_search_options(df)


# Callback para opções dos filtros de Consulta Técnica
@app.callback(
    [
        Output("ct-filter-relator", "options"),
        Output("ct-filter-tema", "options"),
        Output("ct-search-consulta", "options")
    ],
    [Input("main-tabs", "active_tab"), Input("store-data-trigger", "data")]
)
def populate_ct_dropdowns(active_tab, _):
    if active_tab != "tab-consultas":
        return [], [], []
    df, _ = get_data()
    df_ct = df[df["tipo_de_item"].str.contains("Consulta", case=False, na=False)]
    relatores = sorted([r for r in df_ct["relator"].dropna().unique() if r != "Não Informado"])

    temas_set = set()
    for cats in df_ct["categorias"].dropna():
        for c in cats.split(","):
            c_clean = c.strip()
            if c_clean and c_clean != "Geral":
                temas_set.add(c_clean)
    temas = sorted(list(temas_set))

    return (
        [{"label": r, "value": r} for r in relatores],
        [{"label": t, "value": t} for t in temas],
        build_issue_search_options(df_ct)
    )


# Callback para opções dos filtros de Demandas Finalizadas
@app.callback(
    [
        Output("dem-filter-resolucao", "options"),
        Output("dem-filter-prioridade", "options"),
        Output("dem-filter-relator", "options"),
        Output("dem-select-drilldown", "options"),
        Output("dem-search-resumo", "options")
    ],
    [Input("main-tabs", "active_tab"), Input("store-data-trigger", "data")]
)
def populate_demandas_dropdowns(active_tab, _):
    if active_tab != "tab-demandas":
        return [], [], [], [], []

    df = get_demandas_finalizadas_data()
    resolucoes = sorted(df["resolucao"].dropna().unique())
    prioridades = sorted(df["prioridade"].dropna().unique())
    relatores = sorted(df["relator"].dropna().unique())
    demandas = df.sort_values("chave")[["chave", "resumo"]].to_dict("records")

    return (
        [{"label": value, "value": value} for value in resolucoes],
        [{"label": value, "value": value} for value in prioridades],
        [{"label": value, "value": value} for value in relatores],
        [{"label": f"{item['chave']} — {item['resumo']}", "value": item["chave"]} for item in demandas],
        build_issue_search_options(df)
    )


# Callback para opções dos filtros de Cadernos (Aba 3)
@app.callback(
    [
        Output("cad-filter-status", "options"),
        Output("cad-filter-tipo", "options"),
        Output("cad-select-drilldown", "options"),
        Output("cad-search-caderno", "options")
    ],
    [Input("main-tabs", "active_tab"), Input("store-data-trigger", "data")]
)
def populate_cadernos_dropdowns(active_tab, _):
    if active_tab != "tab-cadernos":
        return [], [], [], []
    df_cad = get_cadernos_data()
    status_list = sorted([s for s in df_cad["caderno_status"].dropna().unique()])
    tipo_list = sorted([t for t in df_cad["caderno_tipo"].dropna().unique()])
    cadernos_list = sorted([c for c in df_cad["caderno_nome"].dropna().unique()])
    
    status_opts = [{"label": s, "value": s} for s in status_list]
    tipo_opts = [{"label": t, "value": t} for t in tipo_list]
    drilldown_opts = [{"label": c, "value": c} for c in cadernos_list]
    search_opts = build_issue_search_options(df_cad, "caderno_chave", "caderno_nome")
    
    return status_opts, tipo_opts, drilldown_opts, search_opts


# Callback para sincronização do Jira
@app.callback(
    [
        Output("btn-sync", "disabled"),
        Output("sync-progress-interval", "disabled"),
        Output("sync-output", "children"),
        Output("store-data-trigger", "data")
    ],
    [Input("btn-sync", "n_clicks"), Input("sync-progress-interval", "n_intervals")],
    prevent_initial_call=True
)
def sync_data(n_clicks, n_intervals):
    if ctx.triggered_id == "btn-sync":
        start_sync_worker()

    state = get_sync_state()
    feedback = build_sync_feedback(state)

    if state["status"] == "running":
        return True, False, feedback, no_update

    if state["status"] == "success":
        return False, True, feedback, {"timestamp": state["completed_at"]}

    if state["status"] == "error":
        return False, True, feedback, no_update

    return False, True, no_update, no_update


# Callback da Aba 3 (Cadernos e Versões)
@app.callback(
    [
        Output("cad-kpis-row", "children"),
        Output("cad-chart-progresso", "figure"),
        Output("cad-chart-versoes", "figure"),
        Output("cad-chart-cliq", "figure"),
        Output("cad-chart-status", "figure"),
        Output("cad-tabela", "data"),
        Output("cad-tabela", "columns")
    ],
    [
        Input("cad-filter-versao", "value"),
        Input("cad-filter-status", "value"),
        Input("cad-filter-tipo", "value"),
        Input("cad-search-caderno", "value"),
        Input("store-data-trigger", "data")
    ]
)
def update_cadernos(versao_sel, status_sel, tipo_sel, caderno_key, _):
    df_cad = get_cadernos_data()
    dff = df_cad.copy()

    if versao_sel and versao_sel != "Todos":
        dff = dff[dff["versao_regra"] == versao_sel]
    if status_sel:
        dff = dff[dff["caderno_status"].isin(status_sel)]
    if tipo_sel:
        dff = dff[dff["caderno_tipo"].isin(tipo_sel)]
    if caderno_key:
        dff = dff[dff["caderno_chave"].astype(str) == str(caderno_key)]

    total_cadernos = len(dff)
    total_demandas = dff["total_demandas"].sum()
    total_concluidas = dff["concluidas"].sum()
    taxa_global = (total_concluidas / total_demandas * 100) if total_demandas > 0 else 0
    cadernos_100 = len(dff[dff["taxa_conclusao_pct"] == 100.0])

    # Métricas globais das versões
    df_v26 = df_cad[df_cad["versao_regra"] == "Versão 2026"]
    tot_26 = df_v26["total_demandas"].sum()
    done_26 = df_v26["concluidas"].sum()
    pct_26 = (done_26 / tot_26 * 100) if tot_26 > 0 else 0

    df_v27 = df_cad[df_cad["versao_regra"] == "Versão 2027"]
    tot_27 = df_v27["total_demandas"].sum()
    done_27 = df_v27["concluidas"].sum()
    pct_27 = (done_27 / tot_27 * 100) if tot_27 > 0 else 0

    # 1. Cards de KPI Executivos
    kpi_cards = [
        dbc.Col(build_kpi_card("Cadernos & Épicos", str(total_cadernos), f"{cadernos_100} totalmente entregues", "fa-solid fa-book", "primary"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Versão 2026", f"{pct_26:.1f}%", f"{done_26} de {tot_26} demandas entregues", "fa-solid fa-flag-checkered", "success"), lg=3, sm=6, xs=12),
        dbc.Col(build_kpi_card("Versão 2027", f"{pct_27:.1f}%", f"{done_27} de {tot_27} demandas entregues", "fa-solid fa-hourglass-start", "warning"), lg=3, sm=6, xs=12),
        dbc.Col(build_kpi_card("100% Finalizados", str(cadernos_100), f"de {total_cadernos} cadernos no filtro", "fa-solid fa-circle-check", "info"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Demandas no Escopo", f"{total_demandas:,}".replace(",", "."), f"{total_concluidas} concluídas ({taxa_global:.1f}%)", "fa-solid fa-layer-group", "secondary"), lg=2, sm=6, xs=12),
    ]

    # 2. Gráfico 1: Termômetro de Conclusão por Caderno (Top 12 por volume)
    top_cad = dff.sort_values("total_demandas", ascending=True).tail(12)
    fig_progresso = go.Figure()
    fig_progresso.add_trace(go.Bar(
        y=top_cad["caderno_nome"],
        x=top_cad["concluidas"],
        name="Concluídas",
        orientation="h",
        marker_color=COLORS["success"],
        text=[f"{p:.0f}%" for p in top_cad["taxa_conclusao_pct"]],
        textposition="inside"
    ))
    fig_progresso.add_trace(go.Bar(
        y=top_cad["caderno_nome"],
        x=top_cad["em_aberto"],
        name="Em Aberto",
        orientation="h",
        marker_color=COLORS["warning"],
        text=top_cad["em_aberto"],
        textposition="inside"
    ))
    fig_progresso.update_layout(
        barmode="stack",
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=360,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    # 3. Gráfico 2: Comparativo Estratégico de Versões (2026 vs 2027)
    df_comp_v = df_cad[df_cad["versao_regra"].isin(["Versão 2026", "Versão 2027"])].groupby("versao_regra")[["concluidas", "em_aberto"]].sum().reset_index()
    fig_versoes = go.Figure()
    fig_versoes.add_trace(go.Bar(
        x=df_comp_v["versao_regra"],
        y=df_comp_v["concluidas"],
        name="Concluídas",
        marker_color=COLORS["success"],
        text=df_comp_v["concluidas"],
        textposition="auto"
    ))
    fig_versoes.add_trace(go.Bar(
        x=df_comp_v["versao_regra"],
        y=df_comp_v["em_aberto"],
        name="Em Aberto",
        marker_color=COLORS["warning"],
        text=df_comp_v["em_aberto"],
        textposition="auto"
    ))
    fig_versoes.update_layout(
        barmode="group",
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=360,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    # 4. Gráfico 3: Matriz de Escopo CLIQ 16 (2026) vs CLIQ 17 (2027)
    df_cliq = df_cad[df_cad["caderno_nome"].str.contains("CLIQ", case=False, na=False)].copy()
    fig_cliq = go.Figure()
    if not df_cliq.empty:
        fig_cliq.add_trace(go.Bar(
            x=df_cliq["caderno_nome"],
            y=df_cliq["concluidas"],
            name="Concluídas",
            marker_color=COLORS["success"],
            text=df_cliq["concluidas"],
            textposition="auto"
        ))
        fig_cliq.add_trace(go.Bar(
            x=df_cliq["caderno_nome"],
            y=df_cliq["em_aberto"],
            name="Em Aberto",
            marker_color=COLORS["danger"],
            text=df_cliq["em_aberto"],
            textposition="auto"
        ))
    fig_cliq.update_layout(
        barmode="group",
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=280,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    # 5. Gráfico 4: Donut de Status dos Cadernos
    df_status = dff.groupby("caderno_status").size().reset_index(name="qtd")
    fig_status = px.pie(
        df_status,
        names="caderno_status",
        values="qtd",
        hole=0.55,
        color="caderno_status",
        color_discrete_map={
            "Vigente": COLORS["success"],
            "Concluído": COLORS["primary"],
            "Em andamento": COLORS["warning"],
            "Em desenvolvimento": COLORS["info"],
            "Tarefas pendentes": COLORS["danger"]
        }
    )
    fig_status.update_layout(
        margin=dict(l=10, r=10, t=10, b=20),
        height=280,
        legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5),
        font=dict(family="Segoe UI, sans-serif")
    )

    # 6. Tabela Executiva
    tab_cols = [
        {"name": "Caderno / Épico Pai", "id": "caderno_nome"},
        {"name": "Chave Jira", "id": "caderno_chave"},
        {"name": "Versão", "id": "versao_regra"},
        {"name": "Tipo", "id": "caderno_tipo"},
        {"name": "Status", "id": "caderno_status"},
        {"name": "Total Demandas", "id": "total_demandas"},
        {"name": "Concluídas", "id": "concluidas"},
        {"name": "Em Aberto", "id": "em_aberto"},
        {"name": "% Conclusão", "id": "taxa_conclusao_pct"},
        {"name": "Lead Time Médio (dias)", "id": "avg_lead_time_dias"}
    ]
    tab_data = dff[[c["id"] for c in tab_cols]].sort_values("total_demandas", ascending=False).to_dict("records")

    return kpi_cards, fig_progresso, fig_versoes, fig_cliq, fig_status, tab_data, tab_cols


# Callback do Drill-Down de Subdemandas do Caderno Selecionado
@app.callback(
    [Output("cad-drilldown-tabela", "data"), Output("cad-drilldown-tabela", "columns")],
    [Input("cad-select-drilldown", "value"), Input("store-data-trigger", "data")]
)
def update_caderno_drilldown(caderno_sel, _):
    df, _ = get_data()
    cols = [
        {"name": "Chave", "id": "chave"},
        {"name": "Resumo da Demanda", "id": "resumo"},
        {"name": "Tipo", "id": "tipo_de_item"},
        {"name": "Situação", "id": "situacao"},
        {"name": "Relator", "id": "relator"},
        {"name": "Abertura", "id": "data_criacao_formatada"},
        {"name": "Fechamento", "id": "data_resolucao_formatada"},
        {"name": "Lead Time (dias)", "id": "lead_time_dias"},
        {"name": "Prioridade", "id": "prioridade"}
    ]
    if not caderno_sel:
        # Se nenhum caderno estiver selecionado, exibe as primeiras demandas vinculadas a cadernos
        dff = df[df["parent_summary"] != "Sem Item Pai"].head(10)
    else:
        dff = df[df["parent_summary"] == caderno_sel]

    tab_data = dff[[c["id"] for c in cols]].to_dict("records")
    return tab_data, cols


# Callback de download CSV da Aba 3 (Cadernos)
@app.callback(
    Output("cad-download-dataframe-csv", "data"),
    Input("btn-cad-download-csv", "n_clicks"),
    [
        State("cad-filter-versao", "value"),
        State("cad-filter-status", "value"),
        State("cad-filter-tipo", "value"),
        State("cad-search-caderno", "value")
    ],
    prevent_initial_call=True
)
def download_cadernos_csv(n_clicks, versao_sel, status_sel, tipo_sel, caderno_key):
    df_cad = get_cadernos_data()
    dff = df_cad.copy()
    if versao_sel and versao_sel != "Todos":
        dff = dff[dff["versao_regra"] == versao_sel]
    if status_sel:
        dff = dff[dff["caderno_status"].isin(status_sel)]
    if tipo_sel:
        dff = dff[dff["caderno_tipo"].isin(tipo_sel)]
    if caderno_key:
        dff = dff[dff["caderno_chave"].astype(str) == str(caderno_key)]
    return dcc.send_data_frame(dff.to_csv, "cadernos_e_versoes_ccee.csv", index=False)


def filter_demandas_finalizadas(df, resolucao_sel=None, prioridade_sel=None, relator_sel=None):
    """Aplica os filtros compartilhados pela aba e pela exportação."""
    dff = df.copy()
    if resolucao_sel:
        dff = dff[dff["resolucao"].isin(resolucao_sel)]
    if prioridade_sel:
        dff = dff[dff["prioridade"].isin(prioridade_sel)]
    if relator_sel:
        dff = dff[dff["relator"].isin(relator_sel)]
    return dff


# Callback da Aba 4 (Demandas Finalizadas)
@app.callback(
    [
        Output("dem-kpis-row", "children"),
        Output("dem-chart-resolucao", "figure"),
        Output("dem-chart-leadtime", "figure"),
        Output("dem-chart-fluxo", "figure"),
        Output("dem-chart-relatores", "figure"),
        Output("dem-tabela", "data"),
        Output("dem-tabela", "columns")
    ],
    [
        Input("dem-filter-resolucao", "value"),
        Input("dem-filter-prioridade", "value"),
        Input("dem-filter-relator", "value"),
        Input("dem-search-resumo", "value"),
        Input("store-data-trigger", "data")
    ]
)
def update_demandas_finalizadas(resolucao_sel, prioridade_sel, relator_sel, demanda_key, _):
    df = get_demandas_finalizadas_data()
    dff = filter_demandas_finalizadas(df, resolucao_sel, prioridade_sel, relator_sel)
    if demanda_key:
        dff = dff[dff["chave"].astype(str) == str(demanda_key)]

    total = len(dff)
    finalizadas = int((dff["resolucao"] == "Finalizado").sum())
    resolvidas = int(dff["resolucao"].fillna("").str.startswith("Resolvido").sum())
    canceladas = int((dff["resolucao"] == "Cancelado").sum())
    lead_time_medio = dff["lead_time_dias"].mean()
    lead_time_str = f"{lead_time_medio:.1f} dias" if pd.notna(lead_time_medio) else "N/A"
    total_subtarefas = int(dff["total_subtarefas"].sum()) if not dff.empty else 0
    subtarefas_concluidas = int(dff["subtarefas_concluidas"].sum()) if not dff.empty else 0

    kpis = [
        dbc.Col(build_kpi_card("Demandas no Escopo", str(total), "Filhas diretas do REGRA-305", "fa-solid fa-box-archive", "primary"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Finalizadas", str(finalizadas), "Resolução Finalizado", "fa-solid fa-circle-check", "success"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Resolvidas", str(resolvidas), "Com ou sem ressalvas", "fa-solid fa-check-double", "info"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Canceladas", str(canceladas), "Histórico preservado", "fa-solid fa-ban", "danger"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Lead Time Médio", lead_time_str, "Criação até resolução", "fa-solid fa-stopwatch", "secondary"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Subtarefas", str(total_subtarefas), f"{subtarefas_concluidas} concluídas", "fa-solid fa-list-check", "warning"), lg=2, sm=6, xs=12)
    ]

    color_map_resolucao = {
        "Finalizado": COLORS["success"],
        "Resolvido": COLORS["info"],
        "Resolvido com ressalvas": COLORS["warning"],
        "Cancelado": COLORS["danger"]
    }

    df_resolucao = dff.groupby("resolucao", dropna=False).size().reset_index(name="qtd")
    fig_resolucao = px.pie(
        df_resolucao,
        names="resolucao",
        values="qtd",
        hole=0.58,
        color="resolucao",
        color_discrete_map=color_map_resolucao
    )
    fig_resolucao.update_traces(textposition="inside", textinfo="percent+value")
    fig_resolucao.update_layout(
        margin=dict(l=10, r=10, t=10, b=20),
        height=340,
        legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5),
        font=dict(family="Segoe UI, sans-serif")
    )

    df_leadtime = dff.dropna(subset=["lead_time_dias"]).sort_values("lead_time_dias", ascending=True)
    fig_leadtime = px.bar(
        df_leadtime,
        x="lead_time_dias",
        y="chave",
        orientation="h",
        color="resolucao",
        color_discrete_map=color_map_resolucao,
        text="lead_time_dias",
        hover_data={"resumo": True, "relator": True, "lead_time_dias": ":.0f"},
        labels={"lead_time_dias": "Dias", "chave": "Ticket", "resolucao": "Resolução"}
    )
    fig_leadtime.update_traces(texttemplate="%{text:.0f}", textposition="outside")
    fig_leadtime.update_layout(
        margin=dict(l=20, r=30, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=340,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    df_fluxo = (
        dff.dropna(subset=["mes_resolucao"])
        .groupby(["mes_resolucao", "resolucao"])
        .size()
        .reset_index(name="qtd")
        .sort_values("mes_resolucao")
    )
    fig_fluxo = px.bar(
        df_fluxo,
        x="mes_resolucao",
        y="qtd",
        color="resolucao",
        color_discrete_map=color_map_resolucao,
        text="qtd",
        barmode="stack",
        labels={"mes_resolucao": "Mês da resolução", "qtd": "Demandas", "resolucao": "Resolução"}
    )
    fig_fluxo.update_layout(
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    df_relatores = dff.groupby("relator").size().reset_index(name="qtd").sort_values("qtd", ascending=True)
    fig_relatores = px.bar(
        df_relatores,
        x="qtd",
        y="relator",
        orientation="h",
        text="qtd",
        color_discrete_sequence=[COLORS["primary"]],
        labels={"qtd": "Demandas", "relator": "Relator"}
    )
    fig_relatores.update_traces(textposition="outside")
    fig_relatores.update_layout(
        margin=dict(l=20, r=30, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        yaxis=dict(title=""),
        font=dict(family="Segoe UI, sans-serif")
    )

    tab_cols = [
        {"name": "Ticket", "id": "chave"},
        {"name": "Resumo da Demanda", "id": "resumo"},
        {"name": "Resolução", "id": "resolucao"},
        {"name": "Prioridade", "id": "prioridade"},
        {"name": "Relator", "id": "relator"},
        {"name": "Criação", "id": "data_criacao_formatada"},
        {"name": "Resolução em", "id": "data_resolucao_formatada"},
        {"name": "Lead Time (dias)", "id": "lead_time_dias"},
        {"name": "Subtarefas", "id": "total_subtarefas"},
        {"name": "Subtarefas concluídas", "id": "subtarefas_concluidas"},
        {"name": "Temas / Regras", "id": "categorias"}
    ]
    tab_data = (
        dff.sort_values("data_resolucao", ascending=False)[[column["id"] for column in tab_cols]]
        .to_dict("records")
    )

    return kpis, fig_resolucao, fig_leadtime, fig_fluxo, fig_relatores, tab_data, tab_cols


@app.callback(
    [Output("dem-drilldown-tabela", "data"), Output("dem-drilldown-tabela", "columns")],
    [Input("dem-select-drilldown", "value"), Input("store-data-trigger", "data")]
)
def update_demanda_drilldown(demanda_key, _):
    cols = [
        {"name": "Ticket", "id": "chave"},
        {"name": "Resumo da Subtarefa", "id": "resumo"},
        {"name": "Situação", "id": "situacao"},
        {"name": "Resolução", "id": "resolucao"},
        {"name": "Relator", "id": "relator"},
        {"name": "Criação", "id": "data_criacao_formatada"},
        {"name": "Resolução em", "id": "data_resolucao_formatada"},
        {"name": "Lead Time (dias)", "id": "lead_time_dias"}
    ]
    if not demanda_key:
        return [], cols

    df = get_demanda_subtarefas(demanda_key)
    return df[[column["id"] for column in cols]].to_dict("records"), cols


@app.callback(
    Output("dem-download-dataframe-csv", "data"),
    Input("btn-dem-download-csv", "n_clicks"),
    [
        State("dem-filter-resolucao", "value"),
        State("dem-filter-prioridade", "value"),
        State("dem-filter-relator", "value"),
        State("dem-search-resumo", "value")
    ],
    prevent_initial_call=True
)
def download_demandas_finalizadas(n_clicks, resolucao_sel, prioridade_sel, relator_sel, demanda_key):
    df = get_demandas_finalizadas_data()
    dff = filter_demandas_finalizadas(df, resolucao_sel, prioridade_sel, relator_sel)
    if demanda_key:
        dff = dff[dff["chave"].astype(str) == str(demanda_key)]
    return dcc.send_data_frame(dff.to_csv, "demandas_finalizadas_regra_305.csv", index=False)


# Callback da Aba 2 (Consultas Técnicas)
@app.callback(
    [
        Output("ct-kpis-row", "children"),
        Output("ct-chart-sla-gauge", "figure"),
        Output("ct-chart-temas", "figure"),
        Output("ct-chart-aging", "figure"),
        Output("ct-chart-demandantes", "figure"),
        Output("ct-chart-leadtime", "figure"),
        Output("ct-chart-fluxo", "figure"),
        Output("ct-tabela-demandas", "data"),
        Output("ct-tabela-demandas", "columns")
    ],
    [
        Input("ct-filter-situacao", "value"),
        Input("ct-filter-aging", "value"),
        Input("ct-filter-relator", "value"),
        Input("ct-filter-meta-sla", "value"),
        Input("ct-filter-status-sla", "value"),
        Input("ct-filter-tema", "value"),
        Input("ct-search-consulta", "value"),
        Input("store-data-trigger", "data")
    ]
)
def update_consultas_tecnicas(situacao_sel, aging_sel, relator_sel, meta_sla_sel, status_sla_sel, tema_sel, consulta_key, _):
    df, _ = get_data()
    # Filtra estritamente por Consulta Técnica
    dff = df[df["tipo_de_item"].str.contains("Consulta", case=False, na=False)].copy()

    meta_sla = int(meta_sla_sel) if meta_sla_sel else 5

    # Classificação de SLA por ticket
    def classify_sla(row):
        if row["situacao"] == "Concluído":
            lt = row["lead_time_dias"]
            if pd.isna(lt):
                return "Indeterminado", "Sem Data"
            return ("No Prazo", "✓ Concluído no Prazo") if lt <= meta_sla else ("Fora do Prazo", "⚠ Concluído com Atraso")
        else: # Em Aberto
            ag = row["aging_dias"]
            if pd.isna(ag):
                return "Indeterminado", "Sem Data"
            return ("No Prazo", "✓ Em Aberto (No Prazo)") if ag <= meta_sla else ("Fora do Prazo", "⚠ Em Aberto (Atrasado)")

    sla_tuples = dff.apply(classify_sla, axis=1)
    dff["status_sla_simples"] = [t[0] for t in sla_tuples]
    dff["status_sla"] = [t[1] for t in sla_tuples]

    # Aplicação de filtros
    if situacao_sel and situacao_sel != "Todos":
        dff = dff[dff["situacao"] == situacao_sel]
    if aging_sel and aging_sel != "Todos":
        dff = dff[dff["faixa_aging"] == aging_sel]
    if relator_sel:
        dff = dff[dff["relator"].isin(relator_sel)]
    if status_sla_sel and status_sla_sel != "Todos":
        dff = dff[dff["status_sla_simples"] == status_sla_sel]
    if tema_sel:
        dff = dff[dff["categorias"].str.contains(tema_sel, case=False, na=False)]
    if consulta_key:
        dff = dff[dff["chave"].astype(str) == str(consulta_key)]

    total_ct = len(dff)
    df_done = dff[dff["situacao"] == "Concluído"]
    df_open = dff[dff["situacao"] == "Em Aberto"]

    concluidos_ct = len(df_done)
    abertos_ct = len(df_open)
    taxa_res = (concluidos_ct / total_ct * 100) if total_ct > 0 else 0

    # Métricas de SLA
    concluidos_no_prazo = len(df_done[df_done["status_sla_simples"] == "No Prazo"])
    taxa_sla_concluidas = (concluidos_no_prazo / concluidos_ct * 100) if concluidos_ct > 0 else 0

    abertos_no_prazo = len(df_open[df_open["status_sla_simples"] == "No Prazo"])
    abertos_atrasados = len(df_open[df_open["status_sla_simples"] == "Fora do Prazo"])

    # Lead time médio das concluídas
    lt_medio = df_done["lead_time_dias"].mean()
    lt_str = f"{lt_medio:.1f} dias" if pd.notna(lt_medio) else "N/A"

    # Aging médio das abertas
    aging_medio = df_open["aging_dias"].mean()
    aging_str = f"{aging_medio:.1f} dias" if pd.notna(aging_medio) else "0 dias"

    # Cor do card de SLA
    cor_sla = "success" if taxa_sla_concluidas >= 80 else ("warning" if taxa_sla_concluidas >= 60 else "danger")

    # 1. Cards de KPI especializados
    kpis = [
        dbc.Col(build_kpi_card("Total Consultas", str(total_ct), f"{concluidos_ct} concl. | {abertos_ct} abertas", "fa-solid fa-clipboard-question", "primary"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Em Aberto", str(abertos_ct), f"{abertos_atrasados} fora da meta", "fa-solid fa-clock-rotate-left", "warning"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Concluídas", str(concluidos_ct), f"{taxa_res:.1f}% resolvidas", "fa-solid fa-circle-check", "success"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Conformidade SLA", f"{taxa_sla_concluidas:.1f}%", f"Meta ≤ {meta_sla}d ({concluidos_no_prazo}/{concluidos_ct})", "fa-solid fa-bullseye", cor_sla), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Lead Time Médio", lt_str, "Tempo médio resposta", "fa-solid fa-bolt", "info"), lg=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Aging Médio Fila", aging_str, "Média em espera", "fa-solid fa-triangle-exclamation", "danger"), lg=2, sm=6, xs=12),
    ]

    # 2. Gráfico 1: Gauge de Conformidade de SLA
    fig_gauge = go.Figure(go.Indicator(
        mode="gauge+number",
        value=round(taxa_sla_concluidas, 1),
        domain={'x': [0, 1], 'y': [0, 1]},
        title={
            'text': f"<b>Conformidade de SLA Concluídas</b><br><span style='font-size:0.8em;color:#64748B'>Meta: ≤ {meta_sla} dias | {concluidos_no_prazo} de {concluidos_ct} no prazo</span>",
            'font': {'size': 13, 'family': 'Segoe UI, sans-serif'}
        },
        number={'suffix': "%", 'font': {'size': 26, 'family': 'Segoe UI, sans-serif', 'color': '#1E293B'}},
        gauge={
            'axis': {'range': [0, 100], 'tickwidth': 1, 'tickcolor': "#94A3B8"},
            'bar': {'color': "#10B981" if taxa_sla_concluidas >= 80 else ("#F59E0B" if taxa_sla_concluidas >= 60 else "#EF4444")},
            'bgcolor': "white",
            'borderwidth': 1,
            'bordercolor': "#E2E8F0",
            'steps': [
                {'range': [0, 60], 'color': '#FEE2E2'},
                {'range': [60, 85], 'color': '#FEF3C7'},
                {'range': [85, 100], 'color': '#DCFCE7'}
            ],
            'threshold': {
                'line': {'color': "#047857", 'width': 3},
                'thickness': 0.75,
                'value': 85
            }
        }
    ))
    fig_gauge.update_layout(
        margin=dict(l=25, r=25, t=45, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        height=300
    )

    # 3. Gráfico 2: Temas Regulatórios Mais Consultados
    all_categories = []
    for cats in dff["categorias"].dropna():
        for c in cats.split(","):
            c_clean = c.strip()
            if c_clean and c_clean != "Geral":
                all_categories.append(c_clean)

    if all_categories:
        df_temas = pd.Series(all_categories).value_counts().reset_index()
        df_temas.columns = ["tema", "qtd"]
        df_temas = df_temas.head(8)
    else:
        df_temas = pd.DataFrame([{"tema": "Geral / Sem Categoria", "qtd": len(dff)}])

    fig_temas = px.bar(
        df_temas,
        y="tema",
        x="qtd",
        orientation="h",
        color="qtd",
        color_continuous_scale="Blues",
        text="qtd",
        labels={"tema": "Tema / Regra", "qtd": "Consultas"}
    )
    fig_temas.update_layout(
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        coloraxis_showscale=False,
        yaxis=dict(title="", autorange="reversed"),
        font=dict(family="Segoe UI, sans-serif")
    )
    fig_temas.update_traces(textposition="outside")

    # 4. Gráfico 3: Aging do Backlog Aberto (Faixas de Tempo)
    aging_counts = df_open["faixa_aging"].value_counts().reset_index()
    aging_counts.columns = ["faixa", "qtd"]

    ordem_aging = ["Até 15 dias", "16 a 30 dias", "31 a 60 dias", "Mais de 60 dias"]
    color_map_aging = {
        "Até 15 dias": "#10B981",    # Verde
        "16 a 30 dias": "#F59E0B",   # Amarelo
        "31 a 60 dias": "#EA580C",   # Laranja
        "Mais de 60 dias": "#DC2626" # Vermelho Alerta
    }

    fig_aging = px.bar(
        aging_counts,
        x="faixa",
        y="qtd",
        color="faixa",
        color_discrete_map=color_map_aging,
        category_orders={"faixa": ordem_aging},
        text="qtd",
        labels={"faixa": "Tempo em Aberto", "qtd": "Consultas"}
    )
    fig_aging.update_layout(
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        showlegend=False,
        font=dict(family="Segoe UI, sans-serif")
    )
    fig_aging.update_traces(textposition="outside")

    # 5. Gráfico 4: Top Demandantes / Relatores
    top_relatores = dff.groupby("relator").size().reset_index(name="qtd").sort_values("qtd", ascending=True).tail(8)
    fig_demandantes = px.bar(
        top_relatores,
        y="relator",
        x="qtd",
        orientation="h",
        color_discrete_sequence=[COLORS["primary"]],
        text="qtd",
        labels={"relator": "Solicitante", "qtd": "Consultas"}
    )
    fig_demandantes.update_layout(
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        yaxis=dict(title=""),
        font=dict(family="Segoe UI, sans-serif")
    )
    fig_demandantes.update_traces(textposition="outside")

    # 6. Gráfico 5: Lead Time das Consultas Concluídas vs Meta SLA
    df_done_sorted = df_done.sort_values("lead_time_dias", ascending=False)
    fig_lt = px.bar(
        df_done_sorted,
        x="chave",
        y="lead_time_dias",
        color="lead_time_dias",
        color_continuous_scale="Viridis",
        labels={"chave": "Ticket", "lead_time_dias": "Dias para Concluir"},
        text="lead_time_dias"
    )
    fig_lt.update_layout(
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        coloraxis_showscale=False,
        xaxis=dict(tickangle=-45),
        font=dict(family="Segoe UI, sans-serif")
    )
    if not df_done_sorted.empty:
        fig_lt.add_hline(
            y=meta_sla,
            line_dash="dot",
            line_color="#DC2626",
            line_width=2,
            annotation_text=f"Meta SLA: {meta_sla}d",
            annotation_position="top left",
            annotation_font_color="#DC2626"
        )
        if pd.notna(lt_medio):
            fig_lt.add_hline(
                y=lt_medio,
                line_dash="dash",
                line_color="#2563EB",
                annotation_text=f"Média: {lt_medio:.1f}d",
                annotation_position="top right",
                annotation_font_color="#2563EB"
            )

    # 7. Gráfico 6: Fluxo Mensal (Abertas x Concluídas)
    m_criadas = dff.groupby("mes_criacao").size().reset_index(name="criadas")
    m_fechadas = dff[dff["mes_resolucao"].notna()].groupby("mes_resolucao").size().reset_index(name="fechadas")
    df_fluxo = pd.merge(m_criadas, m_fechadas, left_on="mes_criacao", right_on="mes_resolucao", how="outer")
    df_fluxo["mes"] = df_fluxo["mes_criacao"].combine_first(df_fluxo["mes_resolucao"])
    df_fluxo = df_fluxo.sort_values("mes").fillna(0)

    fig_fluxo = go.Figure()
    fig_fluxo.add_trace(go.Bar(x=df_fluxo["mes"], y=df_fluxo["criadas"], name="Abertas", marker_color=COLORS["warning"]))
    fig_fluxo.add_trace(go.Bar(x=df_fluxo["mes"], y=df_fluxo["fechadas"], name="Concluídas", marker_color=COLORS["success"]))
    fig_fluxo.update_layout(
        barmode="group",
        margin=dict(l=20, r=20, t=10, b=20),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=300,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        font=dict(family="Segoe UI, sans-serif")
    )

    # 8. Tabela de Consultas Técnicas
    tab_cols = [
        {"name": "Ticket", "id": "chave"},
        {"name": "Resumo da Consulta", "id": "resumo"},
        {"name": "Situação", "id": "situacao"},
        {"name": "Status SLA", "id": "status_sla"},
        {"name": "Solicitante", "id": "relator"},
        {"name": "Abertura", "id": "data_criacao_formatada"},
        {"name": "Fechamento", "id": "data_resolucao_formatada"},
        {"name": "Lead Time (dias)", "id": "lead_time_dias"},
        {"name": "Dias em Fila", "id": "aging_dias"},
        {"name": "Faixa Aging", "id": "faixa_aging"},
        {"name": "Temas / Regras", "id": "categorias"}
    ]
    cols_to_dict = [c["id"] for c in tab_cols] + ["status_sla_simples"]
    tab_data = dff[cols_to_dict].to_dict("records")

    return kpis, fig_gauge, fig_temas, fig_aging, fig_demandantes, fig_lt, fig_fluxo, tab_data, tab_cols


# Callback de download CSV da Aba 2 (Consultas)
@app.callback(
    Output("ct-download-dataframe-csv", "data"),
    Input("btn-ct-download-csv", "n_clicks"),
    [
        State("ct-filter-situacao", "value"),
        State("ct-filter-aging", "value"),
        State("ct-filter-relator", "value"),
        State("ct-filter-meta-sla", "value"),
        State("ct-filter-status-sla", "value"),
        State("ct-filter-tema", "value"),
        State("ct-search-consulta", "value")
    ],
    prevent_initial_call=True
)
def download_ct_csv(n_clicks, situacao_sel, aging_sel, relator_sel, meta_sla_sel, status_sla_sel, tema_sel, consulta_key):
    df, _ = get_data()
    dff = df[df["tipo_de_item"].str.contains("Consulta", case=False, na=False)].copy()
    meta_sla = int(meta_sla_sel) if meta_sla_sel else 5

    def classify_sla(row):
        if row["situacao"] == "Concluído":
            lt = row["lead_time_dias"]
            if pd.isna(lt):
                return "Indeterminado", "Sem Data"
            return ("No Prazo", "✓ Concluído no Prazo") if lt <= meta_sla else ("Fora do Prazo", "⚠ Concluído com Atraso")
        else:
            ag = row["aging_dias"]
            if pd.isna(ag):
                return "Indeterminado", "Sem Data"
            return ("No Prazo", "✓ Em Aberto (No Prazo)") if ag <= meta_sla else ("Fora do Prazo", "⚠ Em Aberto (Atrasado)")

    sla_tuples = dff.apply(classify_sla, axis=1)
    dff["status_sla_simples"] = [t[0] for t in sla_tuples]
    dff["status_sla"] = [t[1] for t in sla_tuples]
    dff["meta_sla_dias"] = meta_sla

    if situacao_sel and situacao_sel != "Todos":
        dff = dff[dff["situacao"] == situacao_sel]
    if aging_sel and aging_sel != "Todos":
        dff = dff[dff["faixa_aging"] == aging_sel]
    if relator_sel:
        dff = dff[dff["relator"].isin(relator_sel)]
    if status_sla_sel and status_sla_sel != "Todos":
        dff = dff[dff["status_sla_simples"] == status_sla_sel]
    if tema_sel:
        dff = dff[dff["categorias"].str.contains(tema_sel, case=False, na=False)]
    if consulta_key:
        dff = dff[dff["chave"].astype(str) == str(consulta_key)]

    cols_export = [
        "chave", "resumo", "situacao", "status_sla", "meta_sla_dias",
        "relator", "data_criacao_formatada", "data_resolucao_formatada",
        "lead_time_dias", "aging_dias", "faixa_aging", "categorias"
    ]
    cols_exist = [c for c in cols_export if c in dff.columns]
    return dcc.send_data_frame(dff[cols_exist].to_csv, "consultas_tecnicas_ccee.csv", index=False)


# Callback da Aba 1 (Visão Geral)
@app.callback(
    [
        Output("kpi-cards-row", "children"),
        Output("chart-tipos", "figure"),
        Output("chart-prioridade", "figure"),
        Output("chart-resolucao", "figure"),
        Output("chart-evolucao", "figure"),
        Output("chart-leadtime", "figure"),
        Output("tabela-demandas", "data"),
        Output("tabela-demandas", "columns")
    ],
    [
        Input("filter-tipo", "value"),
        Input("filter-prioridade", "value"),
        Input("filter-situacao", "value"),
        Input("geral-search-resumo", "value"),
        Input("store-data-trigger", "data")
    ]
)
def update_geral(tipo_sel, prioridade_sel, situacao_sel, demanda_key, _):
    df, _ = get_data()
    dff = df.copy()
    if tipo_sel:
        dff = dff[dff["tipo_de_item"].isin(tipo_sel)]
    if prioridade_sel:
        dff = dff[dff["prioridade"].isin(prioridade_sel)]
    if situacao_sel and situacao_sel != "Todos":
        dff = dff[dff["situacao"] == situacao_sel]
    if demanda_key:
        dff = dff[dff["chave"].astype(str) == str(demanda_key)]

    total = len(dff)
    abertos = len(dff[dff["situacao"] == "Em Aberto"])
    concluidos = len(dff[dff["situacao"] == "Concluído"])
    taxa_conclusao = (concluidos / total * 100) if total > 0 else 0
    lead_time_medio = dff["lead_time_dias"].mean()
    lead_time_str = f"{lead_time_medio:.1f} dias" if pd.notna(lead_time_medio) else "N/A"

    cards = [
        dbc.Col(build_kpi_card("Total Demandas", f"{total:,}".replace(",", "."), "Itens cadastrados", "fa-solid fa-list-check", "primary"), md=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Em Aberto", f"{abertos:,}".replace(",", "."), "Backlog em andamento", "fa-solid fa-clock", "warning"), md=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Concluídas", f"{concluidos:,}".replace(",", "."), "Itens entregues", "fa-solid fa-circle-check", "success"), md=3, sm=6, xs=12),
        dbc.Col(build_kpi_card("Taxa de Conclusão", f"{taxa_conclusao:.1f}%", f"{concluidos} de {total}", "fa-solid fa-chart-pie", "info"), md=2, sm=6, xs=12),
        dbc.Col(build_kpi_card("Lead Time Médio", lead_time_str, "Média das entregas", "fa-solid fa-bolt", "secondary"), md=3, sm=12, xs=12),
    ]

    df_tipos = dff.groupby(["tipo_de_item", "situacao"]).size().reset_index(name="qtd")
    fig_tipos = px.bar(
        df_tipos, y="tipo_de_item", x="qtd", color="situacao", orientation="h",
        color_discrete_map={"Concluído": COLORS["success"], "Em Aberto": COLORS["warning"]},
        labels={"tipo_de_item": "Tipo", "qtd": "Quantidade", "situacao": "Situação"}, barmode="stack"
    )
    fig_tipos.update_layout(margin=dict(l=20, r=20, t=10, b=20), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", height=320)
    fig_tipos.update_yaxes(categoryorder="total ascending", title="")

    df_prioridade = dff.groupby("prioridade").size().reset_index(name="qtd")
    fig_prioridade = px.pie(
        df_prioridade, names="prioridade", values="qtd", hole=0.55, color="prioridade",
        color_discrete_map={"Crítico": "#DC2626", "Alto": "#EA580C", "Médio": "#3B82F6", "Baixo": "#10B981"}
    )
    fig_prioridade.update_layout(margin=dict(l=10, r=10, t=10, b=20), height=320)

    df_res = dff["resolucao"].fillna("Não Resolvido (Em Aberto)").value_counts().reset_index()
    df_res.columns = ["resolucao", "qtd"]
    fig_res = px.bar(df_res, x="qtd", y="resolucao", orientation="h", color_discrete_sequence=[COLORS["primary"]], text="qtd")
    fig_res.update_layout(margin=dict(l=20, r=20, t=10, b=20), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", height=320, yaxis=dict(title=""))
    fig_res.update_traces(textposition="outside")

    df_evolucao = dff.groupby("mes_criacao").size().reset_index(name="qtd").sort_values("mes_criacao")
    fig_evolucao = px.area(df_evolucao, x="mes_criacao", y="qtd", markers=True, color_discrete_sequence=[COLORS["secondary"]])
    fig_evolucao.update_layout(margin=dict(l=20, r=20, t=10, b=20), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", height=280)

    df_lt = dff[dff["situacao"] == "Concluído"].groupby("tipo_de_item")["lead_time_dias"].mean().reset_index()
    df_lt["lead_time_dias"] = df_lt["lead_time_dias"].round(1)
    df_lt = df_lt.sort_values("lead_time_dias", ascending=False)
    fig_leadtime = px.bar(df_lt, x="lead_time_dias", y="tipo_de_item", orientation="h", color="lead_time_dias", color_continuous_scale="Teal", text="lead_time_dias")
    fig_leadtime.update_layout(margin=dict(l=20, r=20, t=10, b=20), plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)", height=280, coloraxis_showscale=False, yaxis=dict(title=""))
    fig_leadtime.update_traces(textposition="outside")

    tab_cols = [
        {"name": "Chave", "id": "chave"},
        {"name": "Resumo", "id": "resumo"},
        {"name": "Tipo", "id": "tipo_de_item"},
        {"name": "Prioridade", "id": "prioridade"},
        {"name": "Situação", "id": "situacao"},
        {"name": "Data Criação", "id": "data_criacao_formatada"},
        {"name": "Data Resolução", "id": "data_resolucao_formatada"},
        {"name": "Lead Time (dias)", "id": "lead_time_dias"},
        {"name": "Relator", "id": "relator"}
    ]
    tab_data = dff[[c["id"] for c in tab_cols]].to_dict("records")

    return cards, fig_tipos, fig_prioridade, fig_res, fig_evolucao, fig_leadtime, tab_data, tab_cols


# Callback de download CSV da Aba 1 (Geral)
@app.callback(
    Output("download-dataframe-csv", "data"),
    Input("btn-download-csv", "n_clicks"),
    [
        State("filter-tipo", "value"),
        State("filter-prioridade", "value"),
        State("filter-situacao", "value"),
        State("geral-search-resumo", "value")
    ],
    prevent_initial_call=True
)
def download_csv(n_clicks, tipo_sel, prioridade_sel, situacao_sel, demanda_key):
    df, _ = get_data()
    dff = df.copy()
    if tipo_sel:
        dff = dff[dff["tipo_de_item"].isin(tipo_sel)]
    if prioridade_sel:
        dff = dff[dff["prioridade"].isin(prioridade_sel)]
    if situacao_sel and situacao_sel != "Todos":
        dff = dff[dff["situacao"] == situacao_sel]
    if demanda_key:
        dff = dff[dff["chave"].astype(str) == str(demanda_key)]
    return dcc.send_data_frame(dff.to_csv, "demandas_jira_filtradas.csv", index=False)


if __name__ == "__main__":
    print("🚀 Servidor Dash iniciando em http://localhost:8050 ...")
    app.run(host="0.0.0.0", port=8050, debug=False)
