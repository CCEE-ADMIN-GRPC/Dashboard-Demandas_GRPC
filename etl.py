"""
ETL Pipeline: Jira OData Feed -> DuckDB
Extrai as coleções do conector Jira AIO e salva em banco local jira.duckdb.
"""

import sys
import time
import re
import urllib3
import requests
import pandas as pd
import duckdb

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Suprime avisos de SSL em redes corporativas com proxy/inspeção de pacote
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FEED_BASE_URL = "https://bi-reports.appfire.app/aio-app/rest/aio-cn/1.0/powerbi/export/NGI5NWQ0MDktOWVhMi00M2E5LWJiODctYWYxYjU5YWM5"
DUCKDB_PATH = "jira.duckdb"

ENTITIES = [
    "Issues",
    "Categorias",
    "Subtarefas",
    "Sprints"
]

REQUEST_TIMEOUT = (15, 180)
MAX_PAGE_ATTEMPTS = 4


def clean_colname(col: str) -> str:
    """Normaliza nomes de coluna para padrão SQL (snake_case limpo)."""
    c = col.strip()
    c = c.replace("*", "").replace(":", "").replace("/", "_").replace("-", "_")
    c = c.replace("(", "").replace(")", "").replace("'", "")
    c = re.sub(r"\s+", "_", c)
    c = c.replace("ç", "c").replace("Ç", "C")
    c = c.replace("ã", "a").replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    c = c.lower()
    c = re.sub(r"_+", "_", c).strip("_")
    return c


def fetch_odata_entity(
    entity_name: str,
    session: requests.Session,
    progress_callback=None
) -> pd.DataFrame:
    """Busca todos os registros de uma entidade OData lidando com paginação @odata.nextLink."""
    url = f"{FEED_BASE_URL}/{requests.utils.quote(entity_name)}"
    records = []
    page = 1
    
    print(f"📥 Buscando '{entity_name}'...", end="", flush=True)
    start_t = time.time()
    
    while url:
        data = None
        last_error = None

        for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
            try:
                resp = session.get(
                    url,
                    headers={"Accept": "application/json"},
                    verify=False,
                    timeout=REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < MAX_PAGE_ATTEMPTS:
                    wait_seconds = min(2 ** attempt, 15)
                    if progress_callback:
                        progress_callback({
                            "entity": entity_name,
                            "page": page,
                            "records": len(records),
                            "message": (
                                f"{entity_name}: falha transitória na página {page}; "
                                f"nova tentativa em {wait_seconds}s"
                            )
                        })
                    print(
                        f"\n↻ Tentativa {attempt}/{MAX_PAGE_ATTEMPTS} falhou na página {page} "
                        f"de '{entity_name}'. Nova tentativa em {wait_seconds}s...",
                        flush=True
                    )
                    time.sleep(wait_seconds)

        if data is None:
            raise RuntimeError(
                f"Falha ao extrair '{entity_name}' na página {page} após "
                f"{MAX_PAGE_ATTEMPTS} tentativas."
            ) from last_error

        items = data.get("value", [])
        records.extend(items)
        url = data.get("@odata.nextLink")
        if progress_callback:
            progress_callback({
                "entity": entity_name,
                "page": page,
                "records": len(records),
                "message": f"{entity_name}: página {page} processada ({len(records)} registros)"
            })
        page += 1

    elapsed = time.time() - start_t
    print(f" Concluído! {len(records)} registros em {elapsed:.2f}s")
    
    if not records:
        return pd.DataFrame()
        
    df = pd.DataFrame(records)
    df.columns = [clean_colname(c) for c in df.columns]
    return df


def create_views(con):
    """Cria ou atualiza a view analítica unificada v_issues_analytics."""
    print("🔄 Criando visão analítica unificada 'v_issues_analytics'...")
    con.execute("""
    CREATE OR REPLACE VIEW v_issues_analytics AS
    SELECT 
        i.chave,
        i.resumo,
        i.descricao,
        i.tipo_de_item,
        i.prioridade,
        i.resolucao,
        CASE 
            WHEN i.resolucao IS NOT NULL AND i.resolucao != 'Unresolved' THEN 'Concluído'
            ELSE 'Em Aberto'
        END AS situacao,
        TRY_CAST(i.criado AS TIMESTAMP) AS data_criacao,
        TRY_CAST(i.resolvido AS TIMESTAMP) AS data_resolucao,
        STRFTIME(TRY_CAST(i.criado AS DATE), '%d/%m/%Y') AS data_criacao_formatada,
        STRFTIME(TRY_CAST(i.resolvido AS DATE), '%d/%m/%Y') AS data_resolucao_formatada,
        STRFTIME(TRY_CAST(i.criado AS DATE), '%Y-%m') AS mes_criacao,
        STRFTIME(TRY_CAST(i.resolvido AS DATE), '%Y-%m') AS mes_resolucao,
        CASE 
            WHEN i.resolvido IS NOT NULL 
            THEN ROUND(DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), TRY_CAST(i.resolvido AS TIMESTAMP)), 1)
            ELSE NULL
        END AS lead_time_dias,
        CASE 
            WHEN i.resolucao IS NULL OR i.resolucao = 'Unresolved'
            THEN ROUND(DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), CURRENT_TIMESTAMP), 1)
            ELSE NULL
        END AS aging_dias,
        CASE 
            WHEN (i.resolucao IS NULL OR i.resolucao = 'Unresolved') AND DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), CURRENT_TIMESTAMP) <= 15 THEN 'Até 15 dias'
            WHEN (i.resolucao IS NULL OR i.resolucao = 'Unresolved') AND DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), CURRENT_TIMESTAMP) <= 30 THEN '16 a 30 dias'
            WHEN (i.resolucao IS NULL OR i.resolucao = 'Unresolved') AND DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), CURRENT_TIMESTAMP) <= 60 THEN '31 a 60 dias'
            WHEN (i.resolucao IS NULL OR i.resolucao = 'Unresolved') THEN 'Mais de 60 dias'
            ELSE 'Concluído'
        END AS faixa_aging,
        COALESCE(i.tempo_gasto, 0) / 3600.0 AS horas_gastas,
        COALESCE(i.estimativa_original, 0) / 3600.0 AS horas_estimadas,
        COALESCE(i.relator_name, 'Não Informado') AS relator,
        COALESCE(i.current_sprint_name, 'Sem Sprint') AS sprint,
        COALESCE(i.story_points, 0) AS story_points,
        COALESCE(c.lista_categorias, 'Geral') AS categorias,
        COALESCE(i.parent_issue_key, 'N/A') AS parent_key,
        COALESCE(i.parent_issue_summary, 'Sem Item Pai') AS parent_summary,
        COALESCE(i.parent_issue_type, 'Outros') AS parent_type,
        COALESCE(i.parent_issue_status, 'Não Informado') AS parent_status,
        CASE 
            WHEN i.parent_issue_summary LIKE '%2026%' OR i.parent_issue_summary LIKE '% 26%' OR i.parent_issue_summary LIKE '%CLIQ 16%' OR COALESCE(c.lista_categorias, '') LIKE '%2026%' THEN 'Versão 2026'
            WHEN i.parent_issue_summary LIKE '%2027%' OR i.parent_issue_summary LIKE '% 27%' OR i.parent_issue_summary LIKE '%CLIQ 17%' OR COALESCE(c.lista_categorias, '') LIKE '%2027%' THEN 'Versão 2027'
            ELSE 'Outros / Operacional'
        END AS versao_regra,
        CURRENT_TIMESTAMP AS data_atualizacao
    FROM issues i
    LEFT JOIN (
        SELECT chave, string_agg(categorias, ', ') as lista_categorias
        FROM categorias
        GROUP BY chave
    ) c ON i.chave = c.chave
    """)

    print("🔄 Criando visão analítica agregada 'v_cadernos_analytics'...")
    con.execute("""
    CREATE OR REPLACE VIEW v_cadernos_analytics AS
    SELECT 
        COALESCE(i.parent_issue_summary, 'Sem Item Pai') AS caderno_nome,
        COALESCE(i.parent_issue_key, 'N/A') AS caderno_chave,
        COALESCE(i.parent_issue_type, 'Outros') AS caderno_tipo,
        COALESCE(i.parent_issue_status, 'Não Informado') AS caderno_status,
        CASE 
            WHEN i.parent_issue_summary LIKE '%2026%' OR i.parent_issue_summary LIKE '% 26%' OR i.parent_issue_summary LIKE '%CLIQ 16%' OR STRING_AGG(COALESCE(cat.categorias, ''), ', ') LIKE '%2026%' THEN 'Versão 2026'
            WHEN i.parent_issue_summary LIKE '%2027%' OR i.parent_issue_summary LIKE '% 27%' OR i.parent_issue_summary LIKE '%CLIQ 17%' OR STRING_AGG(COALESCE(cat.categorias, ''), ', ') LIKE '%2027%' THEN 'Versão 2027'
            ELSE 'Outros / Operacional'
        END AS versao_regra,
        COUNT(DISTINCT i.chave) AS total_demandas,
        COUNT(DISTINCT CASE WHEN i.resolucao IS NOT NULL AND i.resolucao != 'Unresolved' THEN i.chave END) AS concluidas,
        COUNT(DISTINCT CASE WHEN i.resolucao IS NULL OR i.resolucao = 'Unresolved' THEN i.chave END) AS em_aberto,
        ROUND(COUNT(DISTINCT CASE WHEN i.resolucao IS NOT NULL AND i.resolucao != 'Unresolved' THEN i.chave END) * 100.0 / COUNT(DISTINCT i.chave), 1) AS taxa_conclusao_pct,
        ROUND(AVG(CASE WHEN i.resolvido IS NOT NULL THEN DATEDIFF('day', TRY_CAST(i.criado AS TIMESTAMP), TRY_CAST(i.resolvido AS TIMESTAMP)) END), 1) AS avg_lead_time_dias
    FROM issues i
    LEFT JOIN categorias cat ON i.chave = cat.chave
    WHERE i.parent_issue_summary IS NOT NULL
    GROUP BY i.parent_issue_summary, i.parent_issue_key, i.parent_issue_type, i.parent_issue_status;
    """)
    print("✅ Visões 'v_issues_analytics' e 'v_cadernos_analytics' criadas com sucesso!")


def run_etl(progress_callback=None):
    """Extrai todas as entidades e substitui o snapshot do DuckDB atomicamente."""
    print("=" * 60)
    print("INICIANDO SINCRONIZACAO JIRA -> DUCKDB")
    print("=" * 60)
    
    total_start = time.time()
    session = requests.Session()

    def report(percent, message, **details):
        if progress_callback:
            progress_callback({"percent": percent, "message": message, **details})

    report(1, "Iniciando conexão com o Jira")
    
    dfs = {}
    total_entities = len(ENTITIES)
    for entity_index, entity in enumerate(ENTITIES, start=1):
        table_name = clean_colname(entity)
        base_percent = int((entity_index - 1) / total_entities * 80)
        entity_limit = int(entity_index / total_entities * 80)
        report(base_percent, f"Preparando extração de {entity}", entity=entity, page=0, records=0)

        def page_progress(event, base=base_percent, limit=entity_limit):
            page_percent = min(base + max(event.get("page", 0), 1), limit - 1)
            report(page_percent, event["message"], **{
                key: value for key, value in event.items() if key != "message"
            })

        df = fetch_odata_entity(entity, session, page_progress)
        if df.empty:
            raise RuntimeError(
                f"A entidade obrigatória '{entity}' retornou zero registros. "
                "O snapshot anterior foi preservado."
            )
        dfs[table_name] = df
        report(
            entity_limit,
            f"{entity} concluída: {len(df)} registros",
            entity=entity,
            records=len(df)
        )

    report(85, "Gravando novo snapshot no DuckDB")
    con = duckdb.connect(DUCKDB_PATH)
    try:
        con.execute("BEGIN TRANSACTION")

        for table_name, df in dfs.items():
            con.register("temp_df", df)
            con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM temp_df")
            con.unregister("temp_df")

        report(92, "Atualizando views analíticas")
        create_views(con)

        # Metadados gravados somente após uma carga integral bem-sucedida.
        con.execute("""
        CREATE OR REPLACE TABLE meta_sync (
            last_sync TIMESTAMP,
            total_issues INTEGER
        )
        """)
        total_issues = len(dfs["issues"])
        con.execute("INSERT INTO meta_sync VALUES (CURRENT_TIMESTAMP, ?)", [total_issues])
        report(97, "Validando metadados e concluindo transação")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    
    total_time = time.time() - total_start
    print("=" * 60)
    print(f"✨ Sincronização concluída com sucesso em {total_time:.2f} segundos!")
    print(f"📊 Total de Chamados no banco: {total_issues}")
    print("=" * 60)
    report(100, f"Sincronização concluída: {total_issues} itens carregados")
    return {
        "total_issues": total_issues,
        "entities": {name: len(df) for name, df in dfs.items()},
        "elapsed_seconds": round(total_time, 2)
    }


if __name__ == "__main__":
    run_etl()
