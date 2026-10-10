def rechercher_documentation(vecteur, k: int = 5) -> list[dict]:
    """
    Recherche par similarité vectorielle dans la documentation du projet.

    Principe (RAG - Retrieval-Augmented Generation) :
    Au lieu de laisser le LLM répondre uniquement avec ses connaissances
    générales, on lui fournit d'abord des extraits de LA documentation
    réelle du projet, les plus proches sémantiquement de la question posée.

    Paramètres :
        vecteur : embedding de la question (liste de 1024 flottants,
                  généré en amont par le modèle bge-m3)
        k       : nombre de passages à retourner (5 par défaut = bon
                  compromis entre contexte suffisant et prompt pas trop long)

    Retour :
        Liste de dictionnaires {source, contenu, distance}, triés du
        passage le plus pertinent au moins pertinent.
    """
    # pgvector attend le vecteur sous forme de chaîne "[0.123,0.456,...]"
    # et non comme une liste Python — d'où cette conversion manuelle.
    vec_str = "[" + ",".join(f"{x:.6f}" for x in vecteur) + "]"

    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()

    # L'opérateur "<=>" est fourni par l'extension pgvector : il calcule
    # la distance cosinus entre deux vecteurs (plus la valeur est petite,
    # plus les deux textes sont sémantiquement proches).
    # On l'utilise deux fois : une fois pour calculer la distance affichée,
    # une fois dans le ORDER BY pour trier — c'est la syntaxe SQL standard
    # pour ce type de requête.
    cur.execute(
        """
        SELECT source, contenu, embedding <=> %s::vector AS distance
        FROM documentation
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (vec_str, vec_str, k),
    )

    # On arrondit la distance à 4 décimales : suffisant pour comparer
    # la pertinence des résultats, inutile d'avoir plus de précision.
    res = [{"source": s, "contenu": c, "distance": round(d, 4)}
           for (s, c, d) in cur.fetchall()]

    cur.close()
    conn.close()
    return res
