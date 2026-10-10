"""
app.py — Assistant IA supervision Zabbix + Docker.

Architecture :
  - Génération  : Groq (llama-3.3-70b-versatile) via API
  - Embeddings  : bge-m3 local via Ollama (RAG, dim 1024)
  - Vecteurs    : mon-postgres + pgvector (table documentation)
  - Données live: API Zabbix (hôtes, problèmes, dernières valeurs d'items)
"""

import json
import os
import time
import requests
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# =========================================================================
# Config
# =========================================================================
DB_CFG = dict(
    host=os.getenv("PG_HOST"),
    port=int(os.getenv("PG_PORT")),
    dbname=os.getenv("PG_DB"),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
)
OLLAMA_URL   = os.getenv("OLLAMA_URL")
EMBED_MODEL  = os.getenv("EMBED_MODEL")
GROQ_URL     = os.getenv("GROQ_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHAT_MODEL   = os.getenv("CHAT_MODEL")
ZABBIX_URL   = os.getenv("ZABBIX_URL")
ZABBIX_TOKEN = os.getenv("ZABBIX_TOKEN")

# --- Base "jeux" (base PostgreSQL nommée "postgres") + API json-server ---
# Réutilise les identifiants de DB_CFG mais pointe une autre base.
JSON_API_URL = os.getenv("JSON_API_URL", "http://localhost:8083")
GAMES_DB_CFG = {**DB_CFG, "dbname": os.getenv("PG_GAMES_DB", "postgres")}

# Schéma des collections — repris de ~/json-api/import_data.py
COLLECTIONS = {
    "games": {
        "id": "INTEGER", "titre": "TEXT", "studio": "TEXT",
        "annee": "INTEGER", "genre": "TEXT", "note": "INTEGER", "plateforme": "TEXT",
    },
    "studios": {
        "id": "INTEGER", "nom": "TEXT", "pays": "TEXT",
        "fondation": "INTEGER", "jeux_cultes": "INTEGER",
    },
    "personnages": {
        "id": "INTEGER", "nom": "TEXT", "jeu": "TEXT", "role": "TEXT",
    },
    "plateformes": {
        "id": "INTEGER", "nom": "TEXT", "constructeur": "TEXT", "annee": "INTEGER",
    },
}


# =========================================================================
# Embedding (bge-m3 local via Ollama — utilisé pour le RAG)
# =========================================================================
def _appel_embedding(texte: str) -> list[float]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": texte},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


# =========================================================================
# Routage — toutes les questions passent, le prompt gère les hors-sujet
# =========================================================================
def router_question(question: str) -> dict:
    """Toutes les questions passent — le prompt système gère les hors-sujet."""
    return {"decision": "traiter", "voie": "direct"}


# =========================================================================
# Recherche RAG (pgvector)
# =========================================================================
def rechercher_documentation(vecteur, k: int = 5) -> list[dict]:
    vec_str = "[" + ",".join(f"{x:.6f}" for x in vecteur) + "]"
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT source, contenu, embedding <=> %s::vector AS distance
        FROM documentation
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (vec_str, vec_str, k),
    )
    res = [{"source": s, "contenu": c, "distance": round(d, 4)}
           for (s, c, d) in cur.fetchall()]
    cur.close(); conn.close()
    return res


# =========================================================================
# Couche Zabbix
# =========================================================================
def _zabbix_rpc(method: str, params: dict):
    r = requests.post(
        ZABBIX_URL,
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {ZABBIX_TOKEN}",
        },
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Zabbix {method} : {data['error']}")
    return data["result"]


def recuperer_hotes() -> list[dict]:
    statut = {"0": "activé", "1": "désactivé"}
    hotes = _zabbix_rpc("host.get", {"output": ["host", "status"]})
    return [{"host": h["host"], "supervision": statut.get(h.get("status"), "?")}
            for h in hotes]


def recuperer_problemes() -> list[dict]:
    sev = {"0": "non classé", "1": "info", "2": "warning",
           "3": "average", "4": "high", "5": "disaster"}
    probs = _zabbix_rpc("problem.get", {
        "output": ["name", "severity", "clock"],
        "recent": False,
        "sortfield": ["eventid"],
        "sortorder": "DESC",
    })
    return [{"probleme": p["name"], "severite": sev.get(p["severity"], "?")}
            for p in probs]


def _humaniser_octets(v) -> str:
    try:
        o = float(v)
    except (TypeError, ValueError):
        return str(v)
    for u in ("o", "Ko", "Mo", "Go"):
        if o < 1024:
            return f"{o:.1f} {u}"
        o /= 1024
    return f"{o:.1f} To"


def recuperer_metriques() -> list[dict]:
    items = _zabbix_rpc("item.get", {
        "output": ["name", "key_", "lastvalue", "units"],
        "search": {"key_": [
            "docker.container_stats.memory.usage_total",
            "docker.container_info.state.running",
            "docker.container_stats.cpu_pct_usage",
            "docker.networks.rx_bytes",
            "docker.networks.tx_bytes",
            "pgsql.custom.",
        ]},
        "searchByAny": True,
        "monitored": True,
    })
    out = []
    for it in items:
        val = it.get("lastvalue")
        if val in (None, ""):
            continue
        key = it.get("key_", "")
        if "memory.usage_total" in key:
            val = _humaniser_octets(val)
        elif "state.running" in key:
            val = "en marche" if str(val) == "1" else "arrêté"
        elif "cpu_pct_usage" in key:
            try:
                val = f"{float(val):.2f} %"
            except (TypeError, ValueError):
                val = f"{val} %"
        elif "rx_bytes" in key or "tx_bytes" in key:
            val = _humaniser_octets(val) + "/s"
        else:
            unite = it.get("units", "")
            val = f"{val} {unite}".strip()
        out.append({"metrique": it["name"], "valeur": val})
    return out


def recuperer_contexte_zabbix() -> dict:
    ctx = {"hotes": [], "problemes": [], "metriques": []}
    for cle, fn in (("hotes", recuperer_hotes),
                    ("problemes", recuperer_problemes),
                    ("metriques", recuperer_metriques)):
        try:
            ctx[cle] = fn()
        except Exception as e:
            print(f"⚠️  Zabbix {cle} indisponible : {e}")


         # États des services
    try:
        ctx["health"] = {
            "database": health_db().get_json(),
            "ollama": health_ollama().get_json(),
            "groq": health_groq().get_json(),
            "zabbix": health_zabbix().get_json()
        }
    except Exception as e:
        print(f"Health indisponible : {e}")



    return ctx


# =========================================================================
# Génération via Groq
# =========================================================================
PROMPT_SYSTEME = """Tu es Heimdall, un assistant technique expert en supervision d'infrastructure, DevOps et ingénierie système.

Tu reçois trois sources LIVE (vérité sur l'état actuel de l'infrastructure) et une source documentaire :
- <hotes>         : liste EXACTE des hôtes supervisés. Aucun autre hôte n'existe.
- <problemes>     : triggers actuellement en PROBLEM. Si nb="0", rien n'est en panne.
- <metriques>     : dernière valeur mesurée de chaque item (mémoire, CPU, réseau, compteurs).
- <documentation> : extraits de la documentation du projet.

TON PÉRIMÈTRE (réponds librement à tout ce qui touche à) :
- Supervision et monitoring (Zabbix, alertes, triggers, LLD, templates)
- Infrastructure Docker et conteneurs
- Bases de données (PostgreSQL, pgvector)
- Développement backend (Python, Flask, API REST)
- DevOps, scripting bash, Linux, WSL
- Networking, Nginx, reverse proxy
- Intelligence artificielle, RAG, LLM, embeddings
- Ingénierie et informatique en général

HORS PÉRIMÈTRE : tout ce qui n'a aucun rapport avec la technique et l'ingénierie
(cuisine, sport, politique, divertissement...). Dans ce cas, décline poliment en une phrase.

RÈGLES SUR LES DONNÉES LIVE (non négociables) :

1. Pour l'ÉTAT ACTUEL (valeurs, pannes, disponibilité), appuie-toi UNIQUEMENT sur
   <hotes>, <problemes> et <metriques>. Ne cite jamais une valeur absente de ces balises.

2. N'invente JAMAIS de nom d'hôte. La liste réelle est EXACTEMENT celle de <hotes>.

3. Si un hôte précis cité est absent de <hotes>, dis-le et liste les hôtes présents.

4. Si <problemes nb="0">, la réponse à "y a-t-il des problèmes ?" est :
   "Aucun problème actif actuellement."

5. Un trigger décrit dans <documentation> est une DÉFINITION, pas un événement actif.
   Un trigger n'est actif QUE s'il apparaît dans <problemes>.

6. Ne recopie JAMAIS les balises XML dans ta réponse.

LIBERTÉ D'EXPRESSION :
- Analyse, interprète, commente les données — si une métrique semble anormale, dis-le.
- Propose des pistes de diagnostic et des recommandations.
- Explique des concepts techniques librement.
- Adapte la longueur à la question : une phrase pour une question simple, plusieurs paragraphes pour une analyse.
- Tu t'appelles Heimdall, le gardien de l'infrastructure — tu peux avoir de la personnalité.

Réponds toujours en français, de façon claire et technique.
Pour les rapports, analyses ou réponses multi-points, utilise du markdown :
titres (##), listes (-), **gras** pour les valeurs clés. Pour les questions
simples, réponds en prose sans formatage superflu.
"""


def _bloc_hotes(hotes):
    if not hotes:
        return '<hotes nb="0"><!-- Zabbix injoignable --></hotes>'
    lignes = "\n".join(
        f'  <hote nom="{h["host"]}" supervision="{h["supervision"]}" />'
        for h in hotes
    )
    return f'<hotes nb="{len(hotes)}">\n{lignes}\n</hotes>'


def _bloc_problemes(problemes):
    if not problemes:
        return '<problemes nb="0"><!-- aucun problème actif --></problemes>'
    lignes = "\n".join(
        f'  <probleme severite="{p["severite"]}">{p["probleme"]}</probleme>'
        for p in problemes
    )
    return f'<problemes nb="{len(problemes)}">\n{lignes}\n</problemes>'


def _bloc_metriques(metriques):
    if not metriques:
        return '<metriques nb="0"><!-- aucune métrique --></metriques>'
    lignes = "\n".join(
        f'  <metrique nom="{m["metrique"]}">{m["valeur"]}</metrique>'
        for m in metriques
    )
    return f'<metriques nb="{len(metriques)}">\n{lignes}\n</metriques>'

def _bloc_health(health: dict) -> str:
    return f"""
<health>
PostgreSQL : {health.get("database", {})}
Ollama : {health.get("ollama", {})}
Groq : {health.get("groq", {})}
Zabbix : {health.get("zabbix", {})}
</health>
"""



def construire_prompt(question: str, passages: list[dict], ctx: dict) -> str:
    passages_xml = "\n".join(
        f'<passage id="{i+1}" source="{p["source"]}">\n{p["contenu"]}\n</passage>'
        for i, p in enumerate(passages)
    )
    return f"""{_bloc_hotes(ctx.get("hotes", []))}

{_bloc_problemes(ctx.get("problemes", []))}

{_bloc_metriques(ctx.get("metriques", []))}

{_bloc_health(ctx.get("health", {}))}

<documentation>
{passages_xml}
</documentation>

<question>{question}</question>

Réponds en français. Sois précis sur les données live, libre sur l'analyse et les explications."""


def generer_reponse(question: str, passages: list[dict], ctx: dict) -> str:
    prompt_user = construire_prompt(question, passages, ctx)
    r = requests.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": PROMPT_SYSTEME},
                {"role": "user", "content": prompt_user},
            ],
            "temperature": 0.0,
            "max_tokens": 2048,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def sauver_historique(question: str, reponse: str) -> None:
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO historique_chat (question, reponse) VALUES (%s, %s)",
        (question, reponse),
    )
    conn.commit()
    cur.close(); conn.close()


# =========================================================================
# Bases de données — CRUD assistant_ai + gestion des jeux (base "postgres")
# =========================================================================
AIDB_TABLES = ("documentation", "historique_chat")


def _aidb_columns(cur, table):
    cur.execute(
        """SELECT column_name, data_type, udt_name
           FROM information_schema.columns
           WHERE table_schema='public' AND table_name=%s
           ORDER BY ordinal_position""",
        (table,),
    )
    return cur.fetchall()


def _aidb_pk(cur, table):
    cur.execute(
        """SELECT a.attname AS pk
           FROM pg_index i
           JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
           WHERE i.indrelid=%s::regclass AND i.indisprimary LIMIT 1""",
        (table,),
    )
    r = cur.fetchone()
    return r["pk"] if r else "id"


def _is_embedding(col):
    return col["column_name"] == "embedding" or col["udt_name"] == "vector"


def _vec_literal(texte):
    v = _appel_embedding(texte)
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


@app.route("/api/aidb/tables")
def aidb_tables():
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        out = []
        for t in AIDB_TABLES:
            cols = _aidb_columns(cur, t)
            if not cols:
                continue
            pk = _aidb_pk(cur, t)  # exécute sa propre requête → avant le count
            cur.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(t)))
            n = cur.fetchone()["n"]
            out.append({
                "table": t,
                "colonnes": [c["column_name"] for c in cols if not _is_embedding(c)],
                "pk": pk,
                "lignes": n,
                "a_embedding": any(_is_embedding(c) for c in cols),
            })
        cur.close(); conn.close()
        return jsonify({"tables": out})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/aidb/<table>", methods=["GET"])
def aidb_list(table):
    if table not in AIDB_TABLES:
        return jsonify({"erreur": "table inconnue"}), 404
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cols = _aidb_columns(cur, table)
        pk = _aidb_pk(cur, table)
        visibles = [c["column_name"] for c in cols if not _is_embedding(c)]
        q = sql.SQL("SELECT {fields} FROM {tbl} ORDER BY {pk} DESC LIMIT 1000").format(
            fields=sql.SQL(", ").join(map(sql.Identifier, visibles)),
            tbl=sql.Identifier(table),
            pk=sql.Identifier(pk),
        )
        cur.execute(q)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({
            "table": table, "pk": pk, "colonnes": visibles,
            "a_embedding": any(_is_embedding(c) for c in cols), "lignes": rows,
        })
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/aidb/<table>", methods=["POST"])
def aidb_create(table):
    if table not in AIDB_TABLES:
        return jsonify({"erreur": "table inconnue"}), 404
    payload = request.get_json(silent=True) or {}
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cols_meta = _aidb_columns(cur, table)
        pk = _aidb_pk(cur, table)
        has_embed = any(_is_embedding(c) for c in cols_meta)
        editable = [c["column_name"] for c in cols_meta
                    if not _is_embedding(c) and c["column_name"] != pk]
        cols = [c for c in editable if c in payload]
        values = [payload[c] for c in cols]
        placeholders = [sql.SQL("%s")] * len(cols)
        if has_embed and payload.get("contenu"):
            cols.append("embedding")
            placeholders.append(sql.SQL("%s::vector"))
            values.append(_vec_literal(payload["contenu"]))
        if not cols:
            return jsonify({"erreur": "aucun champ fourni"}), 400
        q = sql.SQL("INSERT INTO {tbl} ({cols}) VALUES ({ph}) RETURNING {pk}").format(
            tbl=sql.Identifier(table),
            cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
            ph=sql.SQL(", ").join(placeholders),
            pk=sql.Identifier(pk),
        )
        cur.execute(q, values)
        new_id = cur.fetchone()[pk]
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True, "id": new_id}), 201
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/aidb/<table>/<pk_val>", methods=["PUT"])
def aidb_update(table, pk_val):
    if table not in AIDB_TABLES:
        return jsonify({"erreur": "table inconnue"}), 404
    payload = request.get_json(silent=True) or {}
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cols_meta = _aidb_columns(cur, table)
        pk = _aidb_pk(cur, table)
        has_embed = any(_is_embedding(c) for c in cols_meta)
        editable = [c["column_name"] for c in cols_meta
                    if not _is_embedding(c) and c["column_name"] != pk]
        sets, values = [], []
        for c in editable:
            if c in payload:
                sets.append(sql.SQL("{} = %s").format(sql.Identifier(c)))
                values.append(payload[c])
        if has_embed and payload.get("contenu"):
            sets.append(sql.SQL("embedding = %s::vector"))
            values.append(_vec_literal(payload["contenu"]))
        if not sets:
            return jsonify({"erreur": "aucun champ à modifier"}), 400
        values.append(pk_val)
        q = sql.SQL("UPDATE {tbl} SET {sets} WHERE {pk} = %s").format(
            tbl=sql.Identifier(table),
            sets=sql.SQL(", ").join(sets),
            pk=sql.Identifier(pk),
        )
        cur.execute(q, values)
        conn.commit()
        n = cur.rowcount
        cur.close(); conn.close()
        return jsonify({"ok": True, "modifiees": n})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/aidb/<table>/<pk_val>", methods=["DELETE"])
def aidb_delete(table, pk_val):
    if table not in AIDB_TABLES:
        return jsonify({"erreur": "table inconnue"}), 404
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        pk = _aidb_pk(cur, table)
        q = sql.SQL("DELETE FROM {tbl} WHERE {pk} = %s").format(
            tbl=sql.Identifier(table), pk=sql.Identifier(pk))
        cur.execute(q, (pk_val,))
        conn.commit()
        n = cur.rowcount
        cur.close(); conn.close()
        return jsonify({"ok": True, "supprimees": n})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


# --------- Jeux : proxy vers l'API json-server + import vers "postgres" -------
def _games_convert(v, typ):
    if v == "" or v is None:
        return None
    if typ == "INTEGER":
        try:
            return int(v)
        except (ValueError, TypeError):
            return None
    return str(v)


def _games_import(collections):
    conn = psycopg2.connect(**GAMES_DB_CFG)
    cur = conn.cursor()
    resultat = {}
    for coll in collections:
        colonnes = COLLECTIONS[coll]
        data = requests.get(f"{JSON_API_URL}/{coll}", timeout=15).json()
        defs = [f"{c} {t} PRIMARY KEY" if c == "id" else f"{c} {t}"
                for c, t in colonnes.items()]
        cur.execute(f"CREATE TABLE IF NOT EXISTS {coll} ({', '.join(defs)})")
        noms = list(colonnes.keys())
        ph = ", ".join(["%s"] * len(noms))
        maj = ", ".join(f"{c} = EXCLUDED.{c}" for c in noms if c != "id")
        req = (f"INSERT INTO {coll} ({', '.join(noms)}) VALUES ({ph}) "
               f"ON CONFLICT (id) DO UPDATE SET {maj}")
        for item in data:
            cur.execute(req, [_games_convert(item.get(c, ""), colonnes[c]) for c in noms])
        cur.execute(f"SELECT count(*) FROM {coll}")
        resultat[coll] = {"importees": len(data), "total_en_base": cur.fetchone()[0]}
    conn.commit(); cur.close(); conn.close()
    return resultat


@app.route("/api/games/collections")
def games_collections():
    return jsonify({"collections": {k: list(v.keys()) for k, v in COLLECTIONS.items()}})


@app.route("/api/games/<coll>", methods=["GET"])
def games_list(coll):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        r = requests.get(f"{JSON_API_URL}/{coll}", timeout=10)
        r.raise_for_status()
        return jsonify({"collection": coll, "colonnes": list(COLLECTIONS[coll].keys()),
                        "lignes": r.json()})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/games/<coll>", methods=["POST"])
def games_create(coll):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        r = requests.post(f"{JSON_API_URL}/{coll}",
                          json=request.get_json(silent=True) or {}, timeout=10)
        r.raise_for_status()
        return jsonify(r.json()), 201
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/games/<coll>/<item_id>", methods=["PUT"])
def games_update(coll, item_id):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        r = requests.put(f"{JSON_API_URL}/{coll}/{item_id}",
                         json=request.get_json(silent=True) or {}, timeout=10)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/games/<coll>/<item_id>", methods=["DELETE"])
def games_delete(coll, item_id):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        r = requests.delete(f"{JSON_API_URL}/{coll}/{item_id}", timeout=10)
        r.raise_for_status()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/games/import", methods=["POST"])
@app.route("/api/games/import/<coll>", methods=["POST"])
def games_import(coll=None):
    colls = [coll] if coll else list(COLLECTIONS.keys())
    for c in colls:
        if c not in COLLECTIONS:
            return jsonify({"erreur": f"collection inconnue : {c}"}), 404
    try:
        return jsonify({"ok": True, "resultat": _games_import(colls)})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


def _games_ensure_table(cur, coll):
    colonnes = COLLECTIONS[coll]
    defs = [f"{c} {t} PRIMARY KEY" if c == "id" else f"{c} {t}"
            for c, t in colonnes.items()]
    cur.execute(f"CREATE TABLE IF NOT EXISTS {coll} ({', '.join(defs)})")


@app.route("/api/gamesdb/<coll>", methods=["GET"])
def gamesdb_list(coll):
    """Contenu réellement présent dans la base 'postgres' (peut être vide)."""
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        conn = psycopg2.connect(**GAMES_DB_CFG)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(coll)))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"collection": coll, "lignes": rows})
    except Exception as e:
        # table pas encore créée / import jamais lancé
        return jsonify({"collection": coll, "lignes": [], "note": str(e)})


@app.route("/api/gamesdb/<coll>", methods=["POST"])
def gamesdb_create(coll):
    """Ajoute une ligne précise directement dans la base postgres (upsert)."""
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    payload = request.get_json(silent=True) or {}
    colonnes = COLLECTIONS[coll]
    try:
        conn = psycopg2.connect(**GAMES_DB_CFG)
        cur = conn.cursor()
        _games_ensure_table(cur, coll)
        noms = list(colonnes.keys())
        vals = [_games_convert(payload.get(c, ""), colonnes[c]) for c in noms]
        if vals[noms.index("id")] is None:
            return jsonify({"erreur": "id (entier) requis"}), 400
        ph = ", ".join(["%s"] * len(noms))
        maj = ", ".join(f"{c} = EXCLUDED.{c}" for c in noms if c != "id")
        cur.execute(
            f"INSERT INTO {coll} ({', '.join(noms)}) VALUES ({ph}) "
            f"ON CONFLICT (id) DO UPDATE SET {maj}", vals)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"ok": True}), 201
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/gamesdb/<coll>/<item_id>", methods=["PUT"])
def gamesdb_update(coll, item_id):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    payload = request.get_json(silent=True) or {}
    colonnes = COLLECTIONS[coll]
    try:
        conn = psycopg2.connect(**GAMES_DB_CFG)
        cur = conn.cursor()
        _games_ensure_table(cur, coll)
        sets, vals = [], []
        for c, t in colonnes.items():
            if c == "id" or c not in payload:
                continue
            sets.append(f"{c} = %s")
            vals.append(_games_convert(payload.get(c, ""), t))
        if not sets:
            return jsonify({"erreur": "aucun champ à modifier"}), 400
        vals.append(_games_convert(item_id, "INTEGER"))
        cur.execute(f"UPDATE {coll} SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit(); n = cur.rowcount; cur.close(); conn.close()
        return jsonify({"ok": True, "modifiees": n})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


@app.route("/api/gamesdb/<coll>/<item_id>", methods=["DELETE"])
def gamesdb_delete(coll, item_id):
    if coll not in COLLECTIONS:
        return jsonify({"erreur": "collection inconnue"}), 404
    try:
        conn = psycopg2.connect(**GAMES_DB_CFG)
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {coll} WHERE id = %s",
                    (_games_convert(item_id, "INTEGER"),))
        conn.commit(); n = cur.rowcount; cur.close(); conn.close()
        return jsonify({"ok": True, "supprimees": n})
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


# =========================================================================
# Routes
# =========================================================================
@app.route("/")
def index():
    """Sert l'interface web (SPA) depuis static/index.html."""
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/info")
def api_info():
    return jsonify({
        "service": "assistant-ia",
        "statut": "vivant 🟢",
        "generation": f"Groq / {CHAT_MODEL}",
        "embeddings": f"Ollama local / {EMBED_MODEL}",
        "endpoints": ["/health/db", "/health/ollama", "/health/groq",
                      "/health/zabbix", "/chat", "/debug/metriques",
                      "/api/aidb/<table>", "/api/games/<coll>", "/api/games/import"],
    })


@app.route("/health/db")
def health_db():
    try:
        conn = psycopg2.connect(**DB_CFG)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM documentation")
        n = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
        pgvec = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({
            "statut": "ok ✅",
            "base": DB_CFG["dbname"],
            "chunks_documentation": n,
            "pgvector_version": pgvec[0] if pgvec else "absent",
        })
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur": str(e)}), 500


@app.route("/health/ollama")
def health_ollama():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        r.raise_for_status()
        modeles = [m["name"] for m in r.json().get("models", [])]
        embed_ok = any(EMBED_MODEL in m for m in modeles)
        return jsonify({
            "statut": "ok ✅" if embed_ok else "modèle manquant ⚠️",
            "url": OLLAMA_URL,
            "modeles": modeles,
            "embed_model_present": embed_ok,
        })
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur": str(e)}), 500


@app.route("/health/groq")
def health_groq():
    """Santé de l'API Groq — SANS consommer un seul token.

    GET /models vérifie la joignabilité, la validité de la clé et la présence du
    modèle : hors quota de complétions (aucun en-tête x-ratelimit renvoyé), 0 token.

    L'ancienne version envoyait une vraie complétion "ping" : 36 tokens + 1 requête
    de quota par appel. Intenable pour un check périodique — le scénario web Zabbix
    "Flask Heimdall health" (delay=1m) faisait à lui seul 1440 requêtes/jour pour un
    quota de 1000/jour, épuisant le quota partagé avec /chat.

    Contrepartie assumée : on ne prouve plus que la génération fonctionne. Elle l'est
    de toute façon à chaque question réelle passée par /chat.
    """
    try:
        base = GROQ_URL.rsplit("/chat/completions", 1)[0]
        r = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=10,
        )
        r.raise_for_status()
        modeles = [m["id"] for m in r.json().get("data", [])]
        present = CHAT_MODEL in modeles
        return jsonify({
            "statut": "ok ✅" if present else "modèle manquant ⚠️",
            "modele": CHAT_MODEL,
            "modele_present": present,
            "modeles_dispo": len(modeles),
        })
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur": str(e)}), 500


@app.route("/health/zabbix")
def health_zabbix():
    try:
        hotes = _zabbix_rpc("host.get", {"output": ["host", "status"]})
        return jsonify({
            "statut": "ok ✅",
            "url": ZABBIX_URL,
            "nb_hotes": len(hotes),
            "hotes": [h["host"] for h in hotes],
        })
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur": str(e)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"erreur": "champ 'question' manquant ou vide"}), 400

    t0 = time.perf_counter()
    try:
        route = router_question(question)
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur_routage": str(e)}), 500
    t1 = time.perf_counter()

    if route["decision"] == "refus_hors_sujet":
        print(f"⏱  routage={t1-t0:.2f}s  (refus hors-sujet)")
        return jsonify({
            "decision": "refus_hors_sujet",
            "question": question,
            "reponse": (
                "Désolé, je suis un assistant dédié à la supervision Zabbix et "
                "à l'infrastructure Docker de ce projet. Je ne peux répondre "
                "qu'à des questions sur ce périmètre."
            ),
        })

    try:
        vec_q = _appel_embedding(question)
        passages = rechercher_documentation(vec_q, k=5)
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur_rag": str(e)}), 500
    t2 = time.perf_counter()

    ctx = recuperer_contexte_zabbix()
    t3 = time.perf_counter()

    try:
        reponse = generer_reponse(question, passages, ctx)
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur_groq": str(e)}), 500
    t4 = time.perf_counter()

    try:
        sauver_historique(question, reponse)
    except Exception as e:
        print(f"⚠️  Sauvegarde historique ratée : {e}")

    print(f"⏱  routage={t1-t0:.2f}s  rag={t2-t1:.2f}s  "
          f"zabbix={t3-t2:.2f}s  groq={t4-t3:.2f}s  "
          f"TOTAL={t4-t0:.2f}s")

    return jsonify({
        "decision": "traiter",
        "voie": route["voie"],
        "question": question,
        "reponse": reponse,
        "meta": {
            "passages_utilises": len(passages),
            "sources": [p["source"] for p in passages],
            "hotes_vus": len(ctx["hotes"]),
            "problemes_actifs": len(ctx["problemes"]),
            "metriques_vues": len(ctx["metriques"]),
            "chrono_s": {
                "routage": round(t1 - t0, 2),
                "rag": round(t2 - t1, 2),
                "zabbix": round(t3 - t2, 2),
                "groq": round(t4 - t3, 2),
                "total": round(t4 - t0, 2),
            },
        },
    })


@app.route("/debug/metriques", methods=["GET"])
def debug_metriques():
    """Affiche les métriques brutes remontées par Zabbix — utile pour débugger."""
    try:
        ctx = recuperer_contexte_zabbix()
        return jsonify(ctx)
    except Exception as e:
        return jsonify({"statut": "ko ❌", "erreur": str(e)}), 500


if __name__ == "__main__":
    # Préchauffe bge-m3 au démarrage pour éviter le cold start à la 1ère question
    print("→ Préchauffage de bge-m3...")
    try:
        _appel_embedding("préchauffage")
        print("✅ bge-m3 prêt.")
    except Exception as e:
        print(f"⚠️  Préchauffage bge-m3 raté : {e}")
    app.run(host="0.0.0.0", port=5000, debug=True)
