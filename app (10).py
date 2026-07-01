"""
FripSourcing Pro
=================
Outil d'aide à la décision pour le sourcing en friperie.
Scrape les ventes/annonces eBay France (et, en best-effort, Vinted) pour
un mot-clé donné, nettoie les données, élimine les outliers, et calcule
une marge nette estimée par rapport à un prix d'achat en friperie.

⚠️ AVERTISSEMENTS IMPORTANTS (à lire avant usage) :
1. Le scraping d'eBay et Vinted n'est PAS couvert par une API officielle
   gratuite équivalente à ce que fait ce script. Il repose sur le parsing
   du HTML/JSON public des pages. Ces structures changent fréquemment :
   attends-toi à devoir ajuster les sélecteurs CSS de temps en temps.
2. Ceci est un usage personnel/léger. Ne fais pas tourner ce script en
   boucle ou en masse : respecte des délais entre requêtes (déjà inclus),
   et consulte les Conditions Générales d'Utilisation des plateformes.
3. Vinted n'expose PAS de "ventes conclues" publiques (contrairement à
   eBay). Les prix Vinted récupérés ici sont donc des PRIX D'ANNONCES
   ACTIVES, pas des ventes confirmées. L'app le précise explicitement
   dans l'interface pour ne pas fausser ton estimation de marge.
4. Pour un usage professionnel/production plus robuste, privilégie à
   terme une API officielle (ex. eBay Browse API avec clé développeur
   gratuite) plutôt que du scraping HTML.
"""

import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------

# Plusieurs User-Agents pour limiter le risque de blocage trivial.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

REQUEST_TIMEOUT = 10  # secondes
MIN_DELAY_BETWEEN_REQUESTS = 1.0  # secondes, politesse envers le serveur

# Frais estimés par plateforme (à ajuster selon ta situation réelle :
# statut pro/particulier, boost publicitaire, etc.)
PLATFORM_FEES = {
    "eBay": {
        "commission_pct": 0.129,   # ~12.9% commission vente eBay (particulier, ordre de grandeur)
        "frais_fixes": 0.30,       # frais fixes par transaction (ordre de grandeur)
        "note": "Frais à la charge du VENDEUR sur eBay.",
    },
    "Vinted": {
        "commission_pct": 0.0,     # Vinted ne prélève pas de commission au vendeur (hors Vinted Pro)
        "frais_fixes": 0.0,
        "note": (
            "Vinted facture des 'frais de protection acheteur' payés par "
            "l'ACHETEUR, pas par le vendeur. Impact indirect possible sur "
            "le prix accepté par l'acheteur, mais pas de frais direct pour toi."
        ),
    },
}

MONTHS_FR = {
    "janv": 1, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12,
}


def get_random_headers() -> dict:
    """Retourne un header HTTP avec un User-Agent aléatoire, pour limiter
    la détection triviale de bot."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


@dataclass
class Listing:
    titre: str
    prix: float
    date_vente: Optional[datetime]
    lien: str = ""


# ----------------------------------------------------------------------------
# MODULE DE SCRAPING — eBay
# ----------------------------------------------------------------------------

def parse_price(text: str) -> Optional[float]:
    """Extrait un prix sous forme de float depuis un texte type '25,50 €'
    ou '25.50 EUR' ou 'De 10,00 € à 20,00 €' (on prend alors le 1er prix)."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace(" ", "")
    match = re.search(r"(\d+[.,]\d{1,2}|\d+)", cleaned)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_ebay_sold_date(text: str) -> Optional[datetime]:
    """Parse une date du type 'Vendu le 15 juin 2024' (format eBay FR)."""
    if not text:
        return None
    match = re.search(r"(\d{1,2})\s+([a-zéû]+)\.?\s+(\d{4})", text.lower())
    if not match:
        return None
    day, month_raw, year = match.groups()
    month_key = month_raw[:4] if month_raw[:4] in MONTHS_FR else month_raw[:5]
    month = None
    for key, val in MONTHS_FR.items():
        if month_raw.startswith(key):
            month = val
            break
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day))
    except ValueError:
        return None


def fetch_ebay_sold(keyword: str, max_results: int = 30) -> list[Listing]:
    """
    Scrape les résultats "Ventes réussies" (LH_Sold=1&LH_Complete=1) sur
    eBay France pour un mot-clé donné.

    NOTE : les noms de classes CSS ci-dessous ('s-item__title',
    's-item__price', ...) correspondent à la structure d'eBay au moment
    de l'écriture de ce script. Si eBay change son design, ces sélecteurs
    devront être mis à jour (inspecte le HTML via les outils dev du
    navigateur en cas de résultats vides).
    """
    query = requests.utils.quote(keyword)
    url = (
        f"https://www.ebay.fr/sch/i.html?_nkw={query}"
        f"&LH_Sold=1&LH_Complete=1&_sop=13&_ipg=60"
    )

    try:
        response = requests.get(url, headers=get_random_headers(), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        st.error(f"Erreur réseau lors de la requête eBay : {exc}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("li.s-item, div.s-item__info")

    results: list[Listing] = []
    for item in items:
        title_elem = item.select_one(".s-item__title")
        price_elem = item.select_one(".s-item__price")
        date_elem = item.select_one(".s-item__title--tag, .POSITIVE, .s-item__ended-date")
        link_elem = item.select_one("a.s-item__link")

        if not title_elem or not price_elem:
            continue

        title = title_elem.get_text(strip=True)
        if not title or "Shop on eBay" in title or "Nouvelle annonce" == title.strip():
            continue

        price = parse_price(price_elem.get_text(strip=True))
        if price is None:
            continue

        date_sold = parse_ebay_sold_date(date_elem.get_text(strip=True)) if date_elem else None
        link = link_elem["href"] if link_elem and link_elem.has_attr("href") else ""

        results.append(Listing(titre=title, prix=price, date_vente=date_sold, lien=link))

        if len(results) >= max_results:
            break

    return results


# ----------------------------------------------------------------------------
# MODULE DE SCRAPING — Vinted (best-effort, non garanti)
# ----------------------------------------------------------------------------

def fetch_vinted_active(keyword: str, max_results: int = 30) -> list[Listing]:
    """
    Tente de récupérer des annonces ACTIVES (pas des ventes confirmées)
    via l'endpoint JSON interne utilisé par le site Vinted.

    ⚠️ Cet endpoint n'est pas une API publique officielle : il est protégé
    par des mécanismes anti-bot (Datadome) qui peuvent bloquer les requêtes
    sans session/cookies valides. Ce code peut donc renvoyer une liste vide
    même quand des annonces existent — c'est une limite connue, pas un bug
    à corriger indéfiniment.
    """
    url = "https://www.vinted.fr/api/v2/catalog/items"
    params = {
        "search_text": keyword,
        "per_page": max_results,
        "order": "newest_first",
    }
    try:
        response = requests.get(
            url, headers=get_random_headers(), params=params, timeout=REQUEST_TIMEOUT
        )
        if response.status_code != 200:
            return []
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []

    results: list[Listing] = []
    for entry in payload.get("items", [])[:max_results]:
        title = entry.get("title", "").strip()
        price_info = entry.get("price", {})
        price = None
        if isinstance(price_info, dict):
            price = parse_price(str(price_info.get("amount", "")))
        elif isinstance(price_info, (int, float, str)):
            price = parse_price(str(price_info))
        if not title or price is None:
            continue
        results.append(Listing(titre=title, prix=price, date_vente=None, lien=entry.get("url", "")))

    return results


# ----------------------------------------------------------------------------
# TRAITEMENT DES DONNÉES
# ----------------------------------------------------------------------------

def listings_to_dataframe(listings: list[Listing]) -> pd.DataFrame:
    if not listings:
        return pd.DataFrame(columns=["Titre", "Prix (€)", "Date de vente", "Lien"])
    df = pd.DataFrame(
        [
            {
                "Titre": l.titre,
                "Prix (€)": l.prix,
                "Date de vente": l.date_vente.strftime("%d/%m/%Y") if l.date_vente else "N/A",
                "Lien": l.lien,
            }
            for l in listings
        ]
    )
    return df


def remove_outliers_iqr(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Élimine les valeurs aberrantes via la méthode de l'écart interquartile
    (IQR). Utile pour retirer les prix "lots de X pièces" ou erreurs de saisie
    qui faussent la moyenne du marché."""
    if df.empty or len(df) < 4:
        return df
    q1 = df[column].quantile(0.25)
    q3 = df[column].quantile(0.75)
    iqr = q3 - q1
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr
    return df[(df[column] >= lower_bound) & (df[column] <= upper_bound)]


def compute_liquidity_score(listings: list[Listing]) -> tuple[int, str]:
    """
    Calcule un score de liquidité de 0 à 100 basé sur la récurrence des
    ventes dans le temps : plus il y a de ventes rapprochées, plus l'article
    se revend vite (= stock qui tourne = bon pour le sourcing).

    Méthode : nombre de ventes datées / étendue en jours entre la plus
    ancienne et la plus récente, normalisé sur une échelle indicative.
    """
    dated = [l for l in listings if l.date_vente is not None]
    if len(dated) < 2:
        return 0, "Données insuffisantes"

    dates = sorted(d.date_vente for d in dated)
    span_days = max((dates[-1] - dates[0]).days, 1)
    ventes_par_jour = len(dated) / span_days

    # Normalisation empirique : 1 vente/jour ou plus => score max
    score = min(int(ventes_par_jour * 100), 100)

    if score >= 60:
        label = "🟢 Forte liquidité (rotation rapide)"
    elif score >= 25:
        label = "🟡 Liquidité moyenne"
    else:
        label = "🔴 Faible liquidité (article qui dort)"

    return score, label


def compute_margin(prix_moyen_marche: float, prix_achat: float, platform: str) -> dict:
    """Calcule les frais de plateforme et la marge nette, avec une formule
    explicite et traçable."""
    fees = PLATFORM_FEES[platform]
    frais_plateforme = round(
        prix_moyen_marche * fees["commission_pct"] + fees["frais_fixes"], 2
    )
    marge_nette = round(prix_moyen_marche - prix_achat - frais_plateforme, 2)
    marge_pct = round((marge_nette / prix_achat) * 100, 1) if prix_achat > 0 else None

    return {
        "prix_moyen_marche": round(prix_moyen_marche, 2),
        "frais_plateforme": frais_plateforme,
        "marge_nette": marge_nette,
        "marge_pct": marge_pct,
        "note_frais": fees["note"],
    }


# ----------------------------------------------------------------------------
# INTERFACE STREAMLIT
# ----------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="FripSourcing Pro", page_icon="👕", layout="centered")
    st.title("👕 FripSourcing Pro — Calculateur de marge friperie")
    st.caption(
        "Analyse le marché de seconde main pour estimer si un article vaut "
        "le coup à l'achat en friperie."
    )

    with st.form("recherche_form"):
        col_a, col_b = st.columns([2, 1])
        with col_a:
            keyword = st.text_input(
                "Marque + modèle", placeholder="Ex: Carhartt Detroit Jacket"
            )
        with col_b:
            platform = st.selectbox("Plateforme", ["eBay", "Vinted"])

        prix_achat = st.number_input(
            "Prix d'achat en friperie (€)", min_value=0.0, value=5.0, step=0.5
        )
        submitted = st.form_submit_button("🔍 Analyser le marché")

    if not submitted or not keyword.strip():
        st.info("Renseigne un mot-clé puis lance l'analyse.")
        return

    if platform == "Vinted":
        st.warning(
            "⚠️ Vinted n'expose pas de ventes confirmées publiques. Les prix "
            "affichés ci-dessous sont des **annonces actives**, pas des "
            "ventes réelles — traite-les comme une fourchette indicative, "
            "pas comme un prix de vente garanti."
        )

    with st.spinner(f"Analyse du marché {platform} en cours..."):
        time.sleep(MIN_DELAY_BETWEEN_REQUESTS)  # politesse envers le serveur cible
        if platform == "eBay":
            listings = fetch_ebay_sold(keyword, max_results=40)
        else:
            listings = fetch_vinted_active(keyword, max_results=40)

    if not listings:
        st.error(
            "Aucun résultat exploitable. Causes possibles : mot-clé trop "
            "spécifique, blocage temporaire de la plateforme, ou changement "
            "de structure HTML côté source (voir les commentaires du code)."
        )
        return

    df_raw = listings_to_dataframe(listings)
    df_clean = remove_outliers_iqr(df_raw, "Prix (€)")

    n_outliers = len(df_raw) - len(df_clean)
    prix_moyen_marche = df_clean["Prix (€)"].mean()

    metrics = compute_margin(prix_moyen_marche, prix_achat, platform)
    score, score_label = compute_liquidity_score(listings)

    # ---- Dashboard financier ----
    st.subheader("📊 Analyse financière")
    if n_outliers:
        st.caption(f"{n_outliers} valeur(s) aberrante(s) exclue(s) du calcul de moyenne.")

    col1, col2, col3 = st.columns(3)
    col1.metric("Prix moyen marché", f"{metrics['prix_moyen_marche']} €")
    col2.metric("Frais plateforme (est.)", f"{metrics['frais_plateforme']} €")

    marge = metrics["marge_nette"]
    if marge > 15:
        delta_label, delta_color = "🔥 Très rentable", "normal"
    elif marge > 5:
        delta_label, delta_color = "👍 Rentable", "normal"
    else:
        delta_label, delta_color = "❌ À éviter", "inverse"
    col3.metric("Marge nette estimée", f"{marge} €", delta=delta_label, delta_color=delta_color)

    if metrics["marge_pct"] is not None:
        st.caption(
            f"Soit une marge de **{metrics['marge_pct']}%** par rapport au prix d'achat. "
            f"({metrics['note_frais']})"
        )

    st.markdown(
        "**Formule appliquée :** `Marge nette = Prix moyen marché − Prix d'achat − Frais plateforme`"
    )

    # ---- Score de liquidité ----
    st.subheader("💧 Score de liquidité")
    st.progress(score / 100)
    st.write(f"**{score}/100** — {score_label}")
    if platform == "Vinted":
        st.caption(
            "Score non calculable de façon fiable sur Vinted (pas de date de "
            "vente confirmée disponible publiquement)."
        )

    # ---- Tableau des dernières ventes ----
    st.subheader("📋 Dernières ventes / annonces constatées")
    st.dataframe(
        df_raw[["Titre", "Prix (€)", "Date de vente"]].head(10),
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
