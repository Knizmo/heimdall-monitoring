"""
ingest.py — Découpe un .md par sections H1/H2/H3 et insère chunk + embedding
dans mon-postgres. Idempotent : supprime les anciens chunks du même fichier
avant d'insérer les nouveaux.

Usage : python3 ingest.py fichier.md [autre.md ...]
"""

import os
import sys
import re
import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_CFG = dict(
    host=os.getenv("PG_HOST"),
    port=int(os.getenv("PG_PORT")),
    dbname=os.getenv("PG_DB"),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
)
OLLAMA_URL = os.getenv("OLLAMA_URL")
EMBED_MODEL = os.getenv("EMBED_MODEL")


def embed(texte: str) -> list[float]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": texte},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def decouper_md(chemin: str) -> list[tuple[str, str]]:
    """Retourne [(titre, contenu), ...] par section H1/H2/H3."""
    with open(chemin, encoding="utf-8") as f:
        texte = f.read()

    # Pattern : lignes qui commencent par #, ## ou ###
    sections = re.split(r"^(#{1,3} .+)$", texte, flags=re.MULTILINE)
    # sections = [avant_1er_titre, titre1, contenu1, titre2, contenu2, ...]
    chunks = []
    for i in range(1, len(sections), 2):
        titre = sections[i].lstrip("# ").strip()
        contenu = sections[i + 1].strip() if i + 1 < len(sections) else ""
        if contenu:
            chunks.append((titre, f"{sections[i]}\n{contenu}"))
    return chunks


def ingerer(chemin: str) -> int:
    nom_fichier = os.path.basename(chemin)
    print(f"\n📄 Ingestion de : {nom_fichier}")

    chunks = decouper_md(chemin)
    print(f"   → {len(chunks)} sections détectées")

    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()

    # Idempotence : on efface les anciens chunks de CE fichier
    cur.execute("DELETE FROM documentation WHERE source LIKE %s",
                (f"{nom_fichier}%",))
    print(f"   → anciens chunks supprimés ({cur.rowcount})")

    for i, (titre, contenu) in enumerate(chunks, 1):
        source = f"{nom_fichier} § {titre}"
        vecteur = embed(contenu)
        vec_str = "[" + ",".join(f"{x:.6f}" for x in vecteur) + "]"
        cur.execute(
            "INSERT INTO documentation (contenu, source, embedding) "
            "VALUES (%s, %s, %s::vector)",
            (contenu, source, vec_str),
        )
        print(f"   [{i}/{len(chunks)}] {titre[:60]}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ {len(chunks)} chunks ingérés depuis {nom_fichier}")
    return len(chunks)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python3 ingest.py fichier.md [autre.md ...]")
        sys.exit(1)

    total = 0
    for chemin in sys.argv[1:]:
        if not os.path.exists(chemin):
            print(f"⚠️  Fichier introuvable : {chemin}")
            continue
        total += ingerer(chemin)

    print(f"\n🎯 Total ingéré : {total} chunks")
