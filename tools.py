"""Outils de la Boutique intelligente. Tout chiffre renvoyé vient d'ici (règle 1)."""
import json
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).parent / "data"
SEED = DATA / "catalogue_seed.json"
STOCK = DATA / "stock.json"          # mémoire persistante : produits + stock courant
COMMANDES = DATA / "commandes.json"


def _norm(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip()


def _stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def reset_data():
    """Remet le stock au catalogue d'origine et vide les commandes."""
    STOCK.write_text(SEED.read_text(encoding="utf-8"), encoding="utf-8")
    COMMANDES.write_text("[]", encoding="utf-8")


def _load():
    if not STOCK.exists():
        reset_data()
    return json.loads(STOCK.read_text(encoding="utf-8"))


def _save(d):
    STOCK.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _orders():
    return json.loads(COMMANDES.read_text(encoding="utf-8")) if COMMANDES.exists() else []


def _produit(d, ref):
    ref = str(ref).strip().upper()
    return next((p for p in d["produits"] if p["ref"] == ref), None)


def _zone(d, zone):
    z = next((k for k in d["zones"] if _norm(k) == _norm(zone)), None)
    if z is None:
        return None, {"erreur": "zone non desservie",
                      "zones_desservies": list(d["zones"])}
    return z, None


# ---------- outils ----------

def chercher_produits(mot_cle):
    d = _load()
    vides = ("de", "du", "en", "des", "le", "la", "les", "un", "une")
    mots = [_stem(m) for m in _norm(mot_cle).split() if m not in vides]
    if not mots:
        return {"erreur": f"aucun produit pour {mot_cle}"}
    trouves = []
    for p in d["produits"]:
        texte = _norm(p["nom"] + " " + p["categorie"])
        if any(m in texte for m in mots):
            trouves.append({"ref": p["ref"], "nom": p["nom"], "prix_fcfa": p["prix"]})
    if not trouves:
        return {"erreur": f"aucun produit pour {mot_cle}"}
    return {"produits": trouves}


def verifier_stock(ref):
    d = _load()
    p = _produit(d, ref)
    if not p:
        return {"erreur": "référence inconnue"}
    out = {"ref": p["ref"], "nom": p["nom"], "prix_fcfa": p["prix"], "stock": p["stock"]}
    if p["stock"] == 0:
        def fiche(a):
            return {"ref": a["ref"], "nom": a["nom"], "prix_fcfa": a["prix"], "stock": a["stock"]}
        dispo = [a for a in d["produits"] if a["ref"] != p["ref"] and a["stock"] > 0]
        proches = [fiche(a) for a in dispo if a["categorie"] == p["categorie"]]
        out["rupture"] = True
        out["alternatives_en_stock"] = proches or [fiche(a) for a in dispo][:2]
    return out


def frais_livraison(zone):
    d = _load()
    z, err = _zone(d, zone)
    if err:
        return err
    return {"zone": z, "frais_fcfa": d["zones"][z], "delai": d["delai"]}


def convertir_devise(montant_fcfa, devise):
    devise = str(devise).upper().strip()
    try:
        montant = float(montant_fcfa)
    except (TypeError, ValueError):
        return {"erreur": "montant invalide"}
    try:
        req = urllib.request.Request("https://open.er-api.com/v6/latest/XOF",
                                     headers={"User-Agent": "boutique-intelligente"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception as e:
        return {"erreur": f"service de taux indisponible ({type(e).__name__})"}
    taux = data.get("rates", {}).get(devise)
    if taux is None:
        return {"erreur": "devise non prise en charge"}
    return {"montant_fcfa": montant, "devise": devise, "taux": round(taux, 6),
            "montant_converti": round(montant * taux, 2),
            "taux_mis_a_jour": data.get("time_last_update_utc")}


def _lignes(d, articles):
    """Valide les articles. Renvoie (lignes_ok, indisponibles, erreur)."""
    if not isinstance(articles, list) or not articles:
        return None, None, {"erreur": "articles vide : donner une liste de {ref, quantite}"}
    ok, ko = [], []
    for a in articles:
        p = _produit(d, a.get("ref", ""))
        if not p:
            return None, None, {"erreur": f"référence inconnue : {a.get('ref')}"}
        q = a.get("quantite")
        if not isinstance(q, int) or q <= 0:
            return None, None, {"erreur": f"quantité invalide pour {p['ref']} (entier > 0 attendu)"}
        if p["stock"] < q:
            ko.append({"ref": p["ref"], "nom": p["nom"], "demande": q, "stock": p["stock"]})
        else:
            ok.append({"ref": p["ref"], "nom": p["nom"], "quantite": q, "prix_unitaire_fcfa": p["prix"],
                       "sous_total_fcfa": p["prix"] * q})
    return ok, ko, None


def calculer_total(articles, zone):
    d = _load()
    z, err = _zone(d, zone)
    if err:
        return err
    ok, ko, err = _lignes(d, articles)
    if err:
        return err
    sous_total = sum(l["sous_total_fcfa"] for l in ok)
    frais = d["zones"][z]
    out = {"lignes": ok, "sous_total_fcfa": sous_total, "frais_livraison_fcfa": frais,
           "total_fcfa": sous_total + frais if ok else 0, "zone": z, "delai": d["delai"]}
    if ko:
        out["indisponibles_exclus_du_total"] = ko
    return out


def creer_commande(client, articles, zone):
    """Irréversible. La validation humaine est faite par le harness avant l'appel."""
    d = _load()
    z, err = _zone(d, zone)
    if err:
        return err
    ok, ko, err = _lignes(d, articles)
    if err:
        return err
    if ko:
        return {"erreur": "stock insuffisant pour " + ", ".join(k["ref"] for k in ko)}
    for l in ok:
        _produit(d, l["ref"])["stock"] -= l["quantite"]
    _save(d)
    orders = _orders()
    total = sum(l["sous_total_fcfa"] for l in ok) + d["zones"][z]
    cmd = {"id": len(orders) + 1, "client": client, "lignes": ok, "zone": z,
           "frais_livraison_fcfa": d["zones"][z], "total_fcfa": total,
           "date": datetime.now().isoformat(timespec="seconds")}
    orders.append(cmd)
    COMMANDES.write_text(json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"commande_enregistree": cmd["id"], "total_fcfa": total, "delai": d["delai"]}


FONCTIONS = {f.__name__: f for f in (chercher_produits, verifier_stock, frais_livraison,
                                     convertir_devise, calculer_total, creer_commande)}
IRREVERSIBLES = {"creer_commande"}

_ART = {"type": "array", "description": "Liste d'articles, ex. [{\"ref\":\"P05\",\"quantite\":2}]",
        "items": {"type": "object", "properties": {"ref": {"type": "string"}, "quantite": {"type": "integer"}},
                  "required": ["ref", "quantite"]}}


def _s(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


SCHEMAS = [
    _s("chercher_produits", "Trouver des produits du catalogue par mot-clé (ex. 'pagne', 'sac'). À utiliser dès que le client parle d'un produit ou demande un prix. Renvoie réf, nom, prix FCFA.",
       {"mot_cle": {"type": "string"}}, ["mot_cle"]),
    _s("verifier_stock", "Donner le stock d'une référence (ex. 'P03'). À utiliser AVANT de promettre un produit. Si rupture, renvoie des alternatives en stock.",
       {"ref": {"type": "string"}}, ["ref"]),
    _s("frais_livraison", "Donner les frais (FCFA) et le délai de livraison pour une zone (ex. 'Calavi'). Si zone non desservie, renvoie la liste des zones desservies.",
       {"zone": {"type": "string"}}, ["zone"]),
    _s("convertir_devise", "Convertir un montant FCFA vers une devise (EUR, USD, NGN...) au taux réel du moment. À utiliser dès que le client veut un prix dans sa devise.",
       {"montant_fcfa": {"type": "number"}, "devise": {"type": "string", "description": "code ISO, ex. EUR"}}, ["montant_fcfa", "devise"]),
    _s("calculer_total", "Calculer le total exact d'un panier = articles + livraison. À utiliser pour tout total annoncé ; ne jamais calculer à la main. Les produits en rupture sont exclus et signalés.",
       {"articles": _ART, "zone": {"type": "string"}}, ["articles", "zone"]),
    _s("creer_commande", "Enregistrer une commande (IRRÉVERSIBLE, décrémente le stock). Seulement après que le client a confirmé explicitement le récapitulatif ET le total. Afi doit encore valider.",
       {"client": {"type": "string"}, "articles": _ART, "zone": {"type": "string"}}, ["client", "articles", "zone"]),
]
