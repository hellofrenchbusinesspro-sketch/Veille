"""Récupère l'actu de médias de référence, la résume avec une IA gratuite, écrit data/news.json."""
import datetime as dt, hashlib, json, os, re, time
import urllib.error, urllib.request
import feedparser, trafilatura

PAR_FLUX = 3      # articles max par flux et par jour
JOURS = 7         # durée de conservation
PAUSE = 20        # secondes entre deux demandes (respecte les limites gratuites)
SORTIE = "data/news.json"

# Médias reconnus uniquement : ajoute ou retire des lignes (source, domaine, flux RSS).
# Un flux qui ne répond plus est simplement ignoré.
FLUX = [
    ("Le Monde", "Actualité", "https://www.lemonde.fr/rss/une.xml"),
    ("Le Monde", "Politique", "https://www.lemonde.fr/politique/rss_full.xml"),
    ("Le Monde", "Économie", "https://www.lemonde.fr/economie/rss_full.xml"),
    ("Le Monde", "International", "https://www.lemonde.fr/international/rss_full.xml"),
    ("Le Monde", "Sciences", "https://www.lemonde.fr/sciences/rss_full.xml"),
    ("Le Monde", "Tech", "https://www.lemonde.fr/pixels/rss_full.xml"),
    ("franceinfo", "Actualité", "https://www.francetvinfo.fr/titres.rss"),
    ("franceinfo", "Économie", "https://www.francetvinfo.fr/economie.rss"),
    ("Le Figaro", "Économie", "https://www.lefigaro.fr/rss/figaro_economie.xml"),
    ("RFI", "International", "https://www.rfi.fr/fr/rss"),
    ("Numerama", "Tech", "https://www.numerama.com/feed/"),
    ("Automobile Propre", "Automobile", "https://www.automobile-propre.com/feed/"),
]

CONSIGNE = """Tu es un éditeur strictement factuel. À partir UNIQUEMENT du texte fourni, sans rien ajouter de ta propre connaissance, réponds par un objet JSON et rien d'autre :
{"recap": "récapitulatif complet de A à Z en 6 à 10 phrases : contexte, faits, acteurs, chiffres, enjeux, suite attendue",
 "points_cles": ["3 à 5 points essentiels, une phrase courte chacun"],
 "mots_cles": ["4 à 6 mots-clés"],
 "nature": "fait" | "analyse" | "opinion" | "incertain"}
Utilise "incertain" si le texte relève de la rumeur, d'une affirmation non sourcée ou d'un contenu trop vague.
Si une information manque dans le texte, ne l'invente pas. Écris en français."""

# Fournisseurs gratuits : le premier dont la clé existe dans les secrets est utilisé.
# (nom du secret, adresse de l'API, modèles à essayer dans l'ordre)
FOURNISSEURS = [
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions",
     ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile"]),
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     ["gemini-flash-latest", "gemini-flash-lite-latest"]),
    ("MISTRAL_API_KEY", "https://api.mistral.ai/v1/chat/completions",
     ["mistral-small-latest"]),
]

for nom, adresse, modeles in FOURNISSEURS:
    if os.environ.get(nom):
        CLE, URL_IA, MODELES = os.environ[nom], adresse, list(modeles)
        print("Fournisseur utilisé :", nom)
        break
else:
    raise SystemExit("Aucune clé trouvée : ajoute GROQ_API_KEY dans les secrets du dépôt.")


def appeler(modele, messages):
    donnees = {"model": modele, "messages": messages, "temperature": 0.2, "max_tokens": 2500}
    if modele.startswith("openai/gpt-oss"):
        donnees["reasoning_effort"] = "low"
    req = urllib.request.Request(URL_IA, json.dumps(donnees).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {CLE}",
        "User-Agent": "veille-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def texte_article(url, repli):
    """Texte complet si accessible, sinon le résumé du flux (cas des articles payants)."""
    try:
        texte = trafilatura.extract(trafilatura.fetch_url(url)) or ""
    except Exception:
        texte = ""
    if len(texte) > 600:
        return texte, False
    return re.sub(r"<[^>]+>", "", repli), True


def resumer(titre, texte):
    messages = [{"role": "system", "content": CONSIGNE},
                {"role": "user", "content": f"Titre : {titre}\n\n{texte[:6000]}"}]
    erreur = ""
    for modele in list(MODELES):
        for essai in range(3):
            statut, brut = appeler(modele, messages)
            if statut == 429 and essai < 2:   # limite atteinte : on attend une minute
                time.sleep(60)
                continue
            break
        if statut == 401:
            raise SystemExit("Clé refusée (401) : vérifie le secret dans les paramètres du dépôt.")
        if statut == 200:
            try:
                contenu = json.loads(brut)["choices"][0]["message"]["content"]
                resultat = json.loads(re.search(r"\{.*\}", contenu, re.S).group(0))
            except Exception:
                erreur = f"{modele} réponse illisible : {brut[:200]}"
                continue
            MODELES.remove(modele)
            MODELES.insert(0, modele)   # on garde en premier le modèle qui marche
            time.sleep(PAUSE)
            return resultat
        erreur = f"{modele} statut {statut} : {brut[:200]}"
    raise RuntimeError(erreur.replace("\n", " "))


def main():
    anciens = []
    if os.path.exists(SORTIE):
        with open(SORTIE, encoding="utf-8") as f:
            anciens = json.load(f).get("articles", [])
    limite = (dt.datetime.now() - dt.timedelta(days=JOURS)).isoformat()
    anciens = [a for a in anciens if a["date"] >= limite and a.get("source") != "Démo"]
    vus = {a["id"] for a in anciens}
    nouveaux = []

    for source, domaine, url in FLUX:
        try:
            entrees = feedparser.parse(url).entries[:PAR_FLUX]
        except Exception as err:
            print("flux ignoré", url, err)
            continue
        for e in entrees:
            ident = hashlib.md5(e.link.encode()).hexdigest()
            if ident in vus:
                continue
            texte, partiel = texte_article(e.link, e.get("summary", ""))
            if len(texte) < 80:
                continue
            try:
                r = resumer(e.title, texte)
            except Exception as err:
                print("article ignoré", e.link, err)
                continue
            if r.get("nature") == "incertain":
                continue
            vus.add(ident)
            nouveaux.append({"id": ident, "titre": e.title, "lien": e.link, "source": source,
                             "domaine": domaine, "partiel": partiel,
                             "date": dt.datetime.now().isoformat(timespec="minutes"), **r})

    os.makedirs("data", exist_ok=True)
    articles = sorted(nouveaux + anciens, key=lambda a: a["date"], reverse=True)
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump({"maj": dt.datetime.now().isoformat(timespec="minutes"), "articles": articles},
                  f, ensure_ascii=False, indent=1)
    print(len(nouveaux), "nouveaux articles")


if __name__ == "__main__":
    main()
