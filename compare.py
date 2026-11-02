"""Compare agent seul / multi-agents sur 3 mêmes demandes. Usage : python compare.py"""
import io
import json
import re
import sys
import time
from contextlib import redirect_stdout

import agent
import multi_agent
import tools

sys.stdout.reconfigure(encoding="utf-8")

PRIX_ENTREE, PRIX_SORTIE = 75.7, 378.1      # RODI / 1M tokens (anthropic/claude-haiku-5-5, catalogue /v1/models)

DEMANDES = [
    ("Je suis Koffi à Porto-Novo. Combien me coûtent 3 pots de beurre de karité livrés, et ça fait combien en nairas ?",
     lambda r: "13500" in compact(r) and ("ngn" in r.lower() or "naira" in r.lower())),
    ("Je m'appelle Afi. Je veux 2 robes en wax et 1 sac en raphia, livrés à Calavi.",
     lambda r: "46000" in compact(r) and ("rupture" in r.lower() or "indisponible" in r.lower())),
    ("Combien pour 1 pagne wax livré à Ouidah, en euros ?",
     lambda r: "15500" in compact(r) and ("€" in r or "eur" in r.lower())),
]

tokens = {"in": 0, "out": 0}


def compact(t):
    return re.sub(r"[   ]", "", t)


def compter_tokens():
    """Enveloppe l'appel LLM pour cumuler les tokens consommés."""
    c = agent.client()
    original = c.chat.completions.create

    def create(*a, **kw):
        r = original(*a, **kw)
        if r.usage:
            tokens["in"] += r.usage.prompt_tokens
            tokens["out"] += r.usage.completion_tokens
        return r
    c.chat.completions.create = create


def mesurer(fn):
    res = []
    for demande, verif in DEMANDES:
        tools.reset_data()
        tokens.update({"in": 0, "out": 0})
        stats, buf = {"llm_calls": 0}, io.StringIO()
        t0 = time.time()
        with redirect_stdout(buf):
            reponse = fn([], demande, valideur=lambda n, a: False, stats=stats)
        res.append({"ok": bool(verif(reponse)), "appels": stats["llm_calls"], "temps": time.time() - t0,
                    "rodi": (tokens["in"] * PRIX_ENTREE + tokens["out"] * PRIX_SORTIE) / 1e6,
                    "tokens": dict(tokens), "reponse": reponse})
    return res


def moy(r, k):
    return sum(x[k] for x in r) / len(r)


if __name__ == "__main__":
    compter_tokens()
    seul = mesurer(agent.run_agent)
    multi = mesurer(multi_agent.run_multi)
    for nom, r in (("Agent seul", seul), ("Multi-agents", multi)):
        print(f"\n## {nom}")
        for (d, _), x in zip(DEMANDES, r):
            print(f"- {'OK ' if x['ok'] else 'KO '} {x['appels']} appels, {x['temps']:.1f} s, {x['rodi']:.2f} RODI | {d[:50]}...")
    tab = (
        "| | Agent seul | Multi-agents |\n|---|---|---|\n"
        f"| Réponses correctes (/3) | {sum(x['ok'] for x in seul)} | {sum(x['ok'] for x in multi)} |\n"
        f"| Nombre d'appels LLM (moyenne) | {moy(seul, 'appels'):.1f} | {moy(multi, 'appels'):.1f} |\n"
        f"| Temps de réponse moyen | {moy(seul, 'temps'):.1f} s | {moy(multi, 'temps'):.1f} s |\n"
        f"| Coût estimé moyen (RODI) | {moy(seul, 'rodi'):.2f} | {moy(multi, 'rodi'):.2f} |\n")
    print("\n" + tab)
    open("comparaison_resultats.md", "w", encoding="utf-8").write(tab)
    json.dump({"seul": seul, "multi": multi}, open("comparaison_resultats.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
