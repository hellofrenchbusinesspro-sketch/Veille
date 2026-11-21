"""Récupère l'actu de médias de référence, la résume avec Claude, écrit data/news.json."""
import datetime as dt, hashlib, json, os, re
import feedparser, trafilatura
from anthropic import Anthropic

MODEL = "claude-haiku-5-5"   # rapide et peu coûteux
PAR_FLUX = 4                 # articles max par flux et par jour
JOURS = 7                    # durée de conservation
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

client = Anthropic()  # lit la variable ANTHROPIC_API_KEY


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
    r = client.messages.create(model=MODEL, max_tokens=1000, system=CONSIGNE,
                               messages=[{"role": "user", "content": f"Titre : {titre}\n\n{texte[:8000]}"}])
    return json.loads(re.search(r"\{.*\}", r.content[0].text, re.S).group(0))


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
