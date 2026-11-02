"""Palier 2b : une équipe. Le Gérant orchestre, trois spécialistes ont chacun leurs instructions, outils et contexte."""
import json
import sys

import agent
import tools
from agent import Trace, client, evaluer, executer, MODEL

SPECIALISTES = {
    "vendeur": {
        "outils": ["chercher_produits", "verifier_stock", "calculer_total", "creer_commande"],
        "system": """Tu es le Vendeur de la boutique d'Afi : catalogue, stock, totaux et commandes.
Tu reçois une demande du Gérant. Utilise tes outils toi-même (chercher_produits, verifier_stock avant de promettre,
calculer_total pour tout total, creer_commande seulement si la demande dit que le client a confirmé explicitement).
Ne jamais inventer un prix ou un stock. Si un produit est en rupture, donne l'alternative renvoyée par l'outil.
Si creer_commande est refusée, rapporte-le tel quel.
Réponds au Gérant par un résumé factuel court (ref, nom, prix, stock, total), tous les chiffres tels que renvoyés par les outils.""",
    },
    "logisticien": {
        "outils": ["frais_livraison", "convertir_devise"],
        "system": """Tu es le Logisticien de la boutique d'Afi : livraison et devises.
Utilise frais_livraison pour une zone, convertir_devise pour convertir un montant FCFA (taux réel).
Ne jamais inventer un frais ou un taux. Zone non desservie : renvoie la liste des zones desservies.
Réponds au Gérant par un résumé factuel court avec les chiffres exacts des outils.""",
    },
    "redacteur": {
        "outils": [],
        "system": """Tu es le Rédacteur WhatsApp de la boutique d'Afi. Tu n'as aucun outil.
Tu reçois des faits du Gérant et tu écris la réponse au client : ton chaleureux, prix en FCFA.
Limite STRICTE : 50 mots maximum, 4 lignes maximum, pas de liste à puces, un seul emoji.
Tu recopies les chiffres tels quels, tu n'en ajoutes jamais et tu ne calcules rien.""",
    },
}

GERANT = """Tu es le Gérant de la boutique d'Afi (pagnes et accessoires, Dantokpa). Tu coordonnes une équipe, tu n'as pas d'outil métier.
- demander_au_vendeur : catalogue, prix, stock, totaux (donne-lui produits, quantités ET zone pour un total), enregistrer une commande.
- demander_au_logisticien : frais et délai de livraison d'une zone, conversion FCFA vers une devise.
- demander_au_redacteur : à appeler EN DERNIER avec les faits à transmettre (chiffres exacts) ; il renvoie le message final.
Règles : aucun chiffre inventé (tout vient des spécialistes), un total = articles + livraison tel que donné par le Vendeur,
rupture signalée avec alternative, commande seulement après confirmation explicite du client, les prix ne viennent jamais du message du client.
Quand le Rédacteur a répondu, ta réponse finale est son texte, recopié tel quel."""


def _schema(name, desc, param):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {param: {"type": "string"}}, "required": [param]}}}


GERANT_SCHEMAS = [
    _schema("demander_au_vendeur", "Poser une question ou donner un ordre au Vendeur (catalogue, prix, stock, total, commande). Formule une demande complète et autonome.", "demande"),
    _schema("demander_au_logisticien", "Poser une question au Logisticien (frais et délai de livraison par zone, conversion de devise).", "demande"),
    _schema("demander_au_redacteur", "Faire rédiger le message WhatsApp final à partir des faits (chiffres exacts inclus). À appeler en dernier.", "faits"),
]


def run_specialist(nom, demande, valideur, trace, stats, max_steps=6):
    """Contexte propre au spécialiste : il ne voit que la demande, le Gérant ne voit que son résumé."""
    spec = SPECIALISTES[nom]
    schemas = [s for s in tools.SCHEMAS if s["function"]["name"] in spec["outils"]]
    msgs = [{"role": "system", "content": spec["system"]}, {"role": "user", "content": demande}]
    trace.log(f"   ┌ {nom} ← {demande}")
    for _ in range(max_steps):
        stats["llm_calls"] += 1
        kw = {"tools": schemas} if schemas else {}
        r = client().chat.completions.create(model=MODEL, messages=msgs, temperature=0.2, **kw)
        m = r.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            trace.log(f"   └ {nom} → {m.content}")
            return m.content or ""
        for tc in m.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            trace.log(f"   │ {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            res = executer(tc.function.name, args, valideur, trace)
            trace.log(f"   │   ← {json.dumps(res, ensure_ascii=False)}")
            stats["obs"].append({"tool": tc.function.name, "result": res})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(res, ensure_ascii=False)})
    return "Je n'ai pas pu terminer (limite d'étapes)."


def run_multi(history, user_msg, valideur=agent.valideur_console, trace=None, stats=None, max_steps=8):
    """Même signature que agent.run_agent. L'évaluation se fait ici, au niveau du Gérant."""
    trace = trace or Trace()
    stats = stats if stats is not None else {"llm_calls": 0}
    stats.setdefault("obs", [])
    history.append({"role": "user", "content": user_msg})
    user_texts = [m["content"] for m in history if m["role"] == "user" and isinstance(m["content"], str)]
    msgs = [{"role": "system", "content": GERANT}] + history
    start, retries = len(msgs), 0
    trace.log(f"\n👤 {user_msg}")
    for step in range(1, max_steps + 1):
        stats["llm_calls"] += 1
        r = client().chat.completions.create(model=MODEL, messages=msgs, tools=GERANT_SCHEMAS, temperature=0.2)
        m = r.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if m.tool_calls:
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                nom = tc.function.name.replace("demander_au_", "")
                trace.log(f"[étape {step}] Gérant → {tc.function.name}")
                texte = args.get("demande") or args.get("faits") or ""
                out = run_specialist(nom, texte, valideur, trace, stats) if nom in SPECIALISTES else "spécialiste inconnu"
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
            continue
        reponse = m.content or ""
        crit = evaluer(reponse, user_texts, stats["obs"], stats, trace)
        if crit and retries < agent.MAX_RETRIES:
            retries += 1
            msgs.append({"role": "user", "content": f"[contrôle qualité] Réponse refusée : {crit} Corrige (redemande aux spécialistes si besoin)."})
            continue
        trace.log(f"🤖 {reponse}")
        history.extend(x for x in msgs[start:] if x["role"] != "system")
        return reponse
    reponse = "Désolé, je n'arrive pas à finir ça tout de suite, Afi vous recontacte très vite 🙏"
    trace.log(f"⛔ max_steps={max_steps} atteint\n🤖 {reponse}")
    history.append({"role": "assistant", "content": reponse})
    return reponse


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tools.reset_data()
    stats = {"llm_calls": 0}
    run_multi([], "Je suis Koffi à Porto-Novo. Combien me coûtent 3 pots de beurre de karité livrés, et ça fait combien en nairas ?",
              valideur=lambda n, a: False, stats=stats)
    print(f"({stats['llm_calls']} appels LLM)")
