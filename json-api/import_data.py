import os
import requests
import psycopg2
import time
import sys

# ============ COULEURS ANSI ============
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    ROUGE   = "\033[91m"
    VERT    = "\033[92m"
    JAUNE   = "\033[93m"
    BLEU    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    GRIS    = "\033[90m"


# ============ CONFIGURATION ============
API_URL = "http://localhost:8083"
DB_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "dbname": "postgres",
    "user": "postgres",
    "password": os.environ["PG_PASSWORD"]
}

# Chaque colonne a maintenant un TYPE (INTEGER ou TEXT)
COLLECTIONS = {
    "games": {
        "id": "INTEGER", "titre": "TEXT", "studio": "TEXT",
        "annee": "INTEGER", "genre": "TEXT", "note": "INTEGER", "plateforme": "TEXT"
    },
    "studios": {
        "id": "INTEGER", "nom": "TEXT", "pays": "TEXT",
        "fondation": "INTEGER", "jeux_cultes": "INTEGER"
    },
    "personnages": {
        "id": "INTEGER", "nom": "TEXT", "jeu": "TEXT", "role": "TEXT"
    },
    "plateformes": {
        "id": "INTEGER", "nom": "TEXT", "constructeur": "TEXT", "annee": "INTEGER"
    }
}


# ============ EFFETS VISUELS ============
def taper(texte, couleur=C.RESET, delai=0.01):
    for lettre in texte:
        sys.stdout.write(couleur + lettre + C.RESET)
        sys.stdout.flush()
        time.sleep(delai)
    print()


def spinner(message, duree=1.5):
    symboles = ["|", "/", "-", "\\"]
    fin = time.time() + duree
    i = 0
    while time.time() < fin:
        sys.stdout.write(f"\r{C.CYAN}{message} {symboles[i % 4]}{C.RESET}")
        sys.stdout.flush()
        time.sleep(0.1)
        i += 1
    sys.stdout.write(f"\r{C.VERT}{message} ✓{C.RESET}\n")


def barre_progression(total):
    def afficher(actuel):
        pourcent = int((actuel / total) * 100)
        rempli = int((actuel / total) * 30)
        barre = "█" * rempli + "░" * (30 - rempli)
        sys.stdout.write(f"\r  {C.JAUNE}[{barre}] {pourcent}%{C.RESET}")
        sys.stdout.flush()
        if actuel == total:
            print()
    return afficher


def banniere():
    print(C.MAGENTA + C.BOLD)
    print("╔══════════════════════════════════════════╗")
    print("║      IMPORTATEUR DE DONNÉES → POSTGRES   ║")
    print("╚══════════════════════════════════════════╝")
    print(C.RESET)


# ============ FONCTIONS MÉTIER ============
def recuperer_donnees(collection):
    reponse = requests.get(f"{API_URL}/{collection}")
    reponse.raise_for_status()
    return reponse.json()


def creer_table(cur, collection, colonnes):
    # id devient PRIMARY KEY, chaque colonne a son vrai type
    defs = []
    for col, typ in colonnes.items():
        if col == "id":
            defs.append(f"{col} {typ} PRIMARY KEY")
        else:
            defs.append(f"{col} {typ}")
    colonnes_sql = ", ".join(defs)
    cur.execute(f"CREATE TABLE IF NOT EXISTS {collection} ({colonnes_sql})")


def convertir(valeur, typ):
    """Convertit la valeur selon le type de colonne."""
    if valeur == "" or valeur is None:
        return None
    if typ == "INTEGER":
        try:
            return int(valeur)
        except (ValueError, TypeError):
            return None
    return str(valeur)


def inserer_donnees(cur, collection, colonnes, donnees):
    noms = list(colonnes.keys())
    placeholders = ", ".join(["%s"] * len(noms))
    noms_colonnes = ", ".join(noms)
    # ON CONFLICT : si l'id existe déjà, on met à jour au lieu de dupliquer
    maj = ", ".join(f"{col} = EXCLUDED.{col}" for col in noms if col != "id")
    requete = (
        f"INSERT INTO {collection} ({noms_colonnes}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {maj}"
    )
    avancer = barre_progression(len(donnees))
    for i, item in enumerate(donnees, start=1):
        valeurs = [convertir(item.get(col, ""), colonnes[col]) for col in noms]
        cur.execute(requete, valeurs)
        avancer(i)
        time.sleep(0.05)


def compter_lignes(cur, collection):
    cur.execute(f"SELECT COUNT(*) FROM {collection}")
    return cur.fetchone()[0]


def afficher_apercu(donnees, colonnes):
    deuxieme = list(colonnes.keys())[1]
    print(f"\n  {C.GRIS}Lignes disponibles :{C.RESET}")
    for item in donnees:
        repere = item.get(deuxieme, "")
        print(f"    {C.CYAN}id {item.get('id'):<3}{C.RESET} {repere}")


def traiter_collection(cur, collection, donnees):
    colonnes = COLLECTIONS[collection]
    spinner(f"Création de la table '{collection}'", 1)
    creer_table(cur, collection, colonnes)
    print(f"  {C.BLEU}Insertion en cours...{C.RESET}")
    inserer_donnees(cur, collection, colonnes, donnees)
    taper(f"  ✓ {len(donnees)} ligne(s) traitée(s) dans '{collection}'", C.VERT)
    total = compter_lignes(cur, collection)
    print(f"  {C.GRIS}Total en base : {total} ligne(s){C.RESET}")

# ============ PROGRAMME PRINCIPAL ============
def main():
    banniere()

    # Connexion UNE seule fois au démarrage
    try:
        spinner("Connexion à PostgreSQL", 1.5)
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
    except psycopg2.OperationalError as e:
        taper("Impossible de se connecter à PostgreSQL.", C.ROUGE)
        print(f"{C.GRIS}{e}{C.RESET}")
        return

    liste = list(COLLECTIONS.keys())

    # ===== BOUCLE PRINCIPALE : tourne jusqu'à Q =====
    while True:
        taper("\nCollections disponibles :", C.BOLD + C.JAUNE, 0.005)
        for i, nom in enumerate(liste, start=1):
            print(f"  {C.CYAN}{i}.{C.RESET} {nom}")
        print(f"  {C.CYAN}0.{C.RESET} {C.MAGENTA}TOUTES les collections{C.RESET}")
        print(f"  {C.CYAN}Q.{C.RESET} {C.GRIS}Quitter{C.RESET}")

        choix = input(f"\n{C.BOLD}Quelle collection ? (numéro ou Q) : {C.RESET}").strip()

        # --- Quitter le programme ---
        if choix.lower() == "q":
            taper("Fermeture. À bientôt.", C.GRIS)
            break

        try:
            if choix == "0":
                taper(">> Import complet de toutes les collections", C.MAGENTA)
                for collection in liste:
                    print(f"\n{C.BOLD}--- {collection} ---{C.RESET}")
                    spinner(f"Récupération de '{collection}' depuis l'API", 1)
                    donnees = recuperer_donnees(collection)
                    traiter_collection(cur, collection, donnees)
                conn.commit()
                taper("\n✦ Import terminé ✦", C.BOLD + C.VERT)

            else:
                try:
                    collection = liste[int(choix) - 1]
                except (ValueError, IndexError):
                    taper("Choix invalide.", C.ROUGE)
                    continue  # retour au menu

                colonnes = COLLECTIONS[collection]
                print(f"\n{C.BOLD}--- {collection} ---{C.RESET}")
                spinner("Récupération depuis l'API", 1)
                donnees = recuperer_donnees(collection)
                afficher_apercu(donnees, colonnes)

                mode = input(
                    f"\n  {C.BOLD}[T] tout  |  [id] une ligne  |  [R] retour  |  [Q] quitter : {C.RESET}"
                ).strip().lower()

                # --- Retour au menu principal ---
                if mode == "r":
                    taper("  Retour au menu.", C.GRIS)
                    continue

                # --- Quitter le programme ---
                if mode == "q":
                    taper("  Fermeture. À bientôt.", C.GRIS)
                    break

                if mode != "t":
                    donnees = [d for d in donnees if str(d.get("id")) == mode]
                    if not donnees:
                        taper(f"  Aucune ligne avec l'id '{mode}'.", C.ROUGE)
                        continue  # retour au menu

                traiter_collection(cur, collection, donnees)
                conn.commit()
                taper("\n✦ Import terminé ✦", C.BOLD + C.VERT)

        except requests.exceptions.RequestException as e:
            taper("Erreur lors de l'appel à l'API.", C.ROUGE)
            print(f"{C.GRIS}{e}{C.RESET}")
        except Exception as e:
            taper("Une erreur est survenue.", C.ROUGE)
            print(f"{C.GRIS}{e}{C.RESET}")

    # Fermeture propre à la sortie de la boucle
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
