"""Récupère l'actu de médias de référence, la résume avec GitHub Models, écrit data/news.json."""
import datetime as dt, hashlib, json, os, re, time
import urllib.error, urllib.parse, urllib.request
import feedparser, trafilatura

MODEL = "openai/gpt-4.1-mini"   # modèle léger, gratuit avec un compte GitHub
PAR_FLUX = 4                    # articles max par flux et par jour
JOURS = 7                       # durée de conservation
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

CLE = os.environ["GITHUB_TOKEN"]   # fourni automatiquement par GitHub Actions
URL_IA = "https://models.github.ai/inference/chat/completions"
ENTETES = {"Content-Type": "application/json", "Authorization": f"Bearer {CLE}",
           "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class PasDeRedirection(urllib.request.HTTPRedirectHandler):
    """Empêche Python de transformer silencieusement la demande en autre chose."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OUVREUR = urllib.request.build_opener(PasDeRedirection)


def appeler_ia(corps):
    """Envoie la demande ; suit à la main les redirections vers GitHub. Renvoie (statut, texte)."""
    url = URL_IA
    for _ in range(4):
        req = urllib.request.Request(url, corps, ENTETES)
        try:
            with OUVREUR.open(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            suite = urllib.parse.urljoin(url, err.headers.get("Location", ""))
            hote = urllib.parse.urlparse(suite).hostname or ""
            if err.code in (301, 302, 303, 307, 308) and hote.endswith(("github.ai", "github.com")):
                url = suite
                continue
            return err.code, err.read().decode("utf-8", "replace")
    return 0, "trop de redirections"


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
    corps = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": CONSIGNE},
            {"role": "user", "content": f"Titre : {titre}\n\n{texte[:8000]}"},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }).encode()
    for essai in range(3):
        statut, brut = appeler_ia(corps)
        if statut == 429 and essai < 2:   # limite atteinte : on attend et on réessaie
            time.sleep(40)
            continue
        break
    if statut != 200:
        raise RuntimeError(f"IA statut {statut} : {brut[:300]}".replace("\n", " "))
    try:
        contenu = json.loads(brut)["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"réponse illisible (statut {statut}) : {brut[:300]}".replace("\n", " "))
    time.sleep(5)   # espace les demandes pour rester dans la limite gratuite
    return json.loads(re.search(r"\{.*\}", contenu, re.S).group(0))


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
