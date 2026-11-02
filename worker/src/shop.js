// Données et outils de la boutique. Tout chiffre renvoyé vient d'ici (règle 1).
// Stock, commandes : KV. Valeur "state" = produits + zones + stock courant.

export const SEED = {
  produits: [
    { ref: "P01", nom: "Pagne wax 6 yards", categorie: "tissu", prix: 12000, stock: 14 },
    { ref: "P02", nom: "Chemise bazin homme", categorie: "vetement", prix: 18500, stock: 3 },
    { ref: "P03", nom: "Sac en raphia", categorie: "accessoire", prix: 7500, stock: 0 },
    { ref: "P04", nom: "Sandales en cuir", categorie: "accessoire", prix: 9000, stock: 8 },
    { ref: "P05", nom: "Robe en wax", categorie: "vetement", prix: 22000, stock: 5 },
    { ref: "P06", nom: "Beurre de karité 500 g", categorie: "soin", prix: 3500, stock: 40 },
  ],
  zones: { Cotonou: 1000, Akpakpa: 1000, "Fidjrossè": 1500, Calavi: 2000, "Porto-Novo": 3000, Ouidah: 3500 },
  delai: "24 à 48 h",
};

const norm = (s) => String(s).toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").trim();
const stem = (w) => (w.length > 3 && w.endsWith("s") ? w.slice(0, -1) : w);

export async function loadState(env) {
  return (await env.KV.get("state", "json")) ?? structuredClone(SEED);
}
export const saveState = (env, s) => env.KV.put("state", JSON.stringify(s));

const trouverProduit = (s, ref) => s.produits.find((p) => p.ref === String(ref).trim().toUpperCase());

function trouverZone(s, zone) {
  const z = Object.keys(s.zones).find((k) => norm(k) === norm(zone));
  return z ? { z } : { err: { erreur: "zone non desservie", zones_desservies: Object.keys(s.zones) } };
}

function lignes(s, articles) {
  if (!Array.isArray(articles) || !articles.length)
    return { err: { erreur: "articles vide : donner une liste de {ref, quantite}" } };
  const ok = [], ko = [];
  for (const a of articles) {
    const p = trouverProduit(s, a?.ref ?? "");
    if (!p) return { err: { erreur: `référence inconnue : ${a?.ref}` } };
    if (!Number.isInteger(a.quantite) || a.quantite <= 0)
      return { err: { erreur: `quantité invalide pour ${p.ref} (entier > 0 attendu)` } };
    if (p.stock < a.quantite) ko.push({ ref: p.ref, nom: p.nom, demande: a.quantite, stock: p.stock });
    else ok.push({ ref: p.ref, nom: p.nom, quantite: a.quantite, prix_unitaire_fcfa: p.prix, sous_total_fcfa: p.prix * a.quantite });
  }
  return { ok, ko };
}

export const IRREVERSIBLES = new Set(["creer_commande"]);

export const FONCTIONS = {
  async chercher_produits(ctx, { mot_cle }) {
    const s = await loadState(ctx.env);
    const vides = ["de", "du", "en", "des", "le", "la", "les", "un", "une"];
    const mots = norm(mot_cle ?? "").split(/\s+/).filter((m) => m && !vides.includes(m)).map(stem);
    const trouves = !mots.length ? [] : s.produits.filter((p) => {
      const t = norm(p.nom + " " + p.categorie);
      return mots.some((m) => t.includes(m));
    }).map((p) => ({ ref: p.ref, nom: p.nom, prix_fcfa: p.prix }));
    return trouves.length ? { produits: trouves } : { erreur: `aucun produit pour ${mot_cle}` };
  },

  async verifier_stock(ctx, { ref }) {
    const s = await loadState(ctx.env);
    const p = trouverProduit(s, ref);
    if (!p) return { erreur: "référence inconnue" };
    const out = { ref: p.ref, nom: p.nom, prix_fcfa: p.prix, stock: p.stock };
    if (p.stock === 0) {
      const fiche = (a) => ({ ref: a.ref, nom: a.nom, prix_fcfa: a.prix, stock: a.stock });
      const dispo = s.produits.filter((a) => a.ref !== p.ref && a.stock > 0);
      const proches = dispo.filter((a) => a.categorie === p.categorie).map(fiche);
      out.rupture = true;
      out.alternatives_en_stock = proches.length ? proches : dispo.slice(0, 2).map(fiche);
    }
    return out;
  },

  async frais_livraison(ctx, { zone }) {
    const s = await loadState(ctx.env);
    const { z, err } = trouverZone(s, zone ?? "");
    return err ?? { zone: z, frais_fcfa: s.zones[z], delai: s.delai };
  },

  async convertir_devise(ctx, { montant_fcfa, devise }) {
    const montant = Number(montant_fcfa);
    if (!Number.isFinite(montant)) return { erreur: "montant invalide" };
    const code = String(devise ?? "").toUpperCase().trim();
    let data;
    try {
      const r = await fetch("https://open.er-api.com/v6/latest/XOF");
      data = await r.json();
    } catch (e) {
      return { erreur: "service de taux indisponible" };
    }
    const taux = data?.rates?.[code];
    if (taux == null) return { erreur: "devise non prise en charge" };
    return { montant_fcfa: montant, devise: code, taux: Number(taux.toFixed(6)),
             montant_converti: Number((montant * taux).toFixed(2)), taux_mis_a_jour: data.time_last_update_utc };
  },

  async calculer_total(ctx, { articles, zone }) {
    const s = await loadState(ctx.env);
    const { z, err: ez } = trouverZone(s, zone ?? "");
    if (ez) return ez;
    const { ok, ko, err } = lignes(s, articles);
    if (err) return err;
    const sous = ok.reduce((t, l) => t + l.sous_total_fcfa, 0);
    const out = { lignes: ok, sous_total_fcfa: sous, frais_livraison_fcfa: s.zones[z],
                  total_fcfa: ok.length ? sous + s.zones[z] : 0, zone: z, delai: s.delai };
    if (ko.length) out.indisponibles_exclus_du_total = ko;
    return out;
  },

  // État réel d'une commande du client (jamais celle d'un autre client).
  async statut_commande(ctx, { numero } = {}) {
    const mes = [];
    for (const k of (await ctx.env.KV.list({ prefix: "order:" })).keys) {
      const o = await ctx.env.KV.get(k.name, "json");
      if (o?.chatId === ctx.chatId) mes.push(o);
    }
    if (!mes.length) return { erreur: "aucune commande pour ce client" };
    const o = numero ? mes.find((m) => m.id === Number(numero)) : mes.sort((a, b) => b.id - a.id)[0];
    if (!o) return { erreur: `commande #${numero} introuvable pour ce client` };
    const libelle = { pending: "en attente de validation par Afi", validated: "confirmée par Afi et enregistrée", refused: "refusée par Afi, rien n'est enregistré" };
    return { commande: o.id, statut: libelle[o.status] ?? o.status, total_fcfa: o.total_fcfa, zone: o.zone,
             articles: o.lignes.map((l) => `${l.quantite} ${l.nom}`) };
  },

  // Palier 3 : la commande n'est PAS enregistrée ici. Elle est mise en attente, Afi valide par WhatsApp.
  async creer_commande(ctx, { client, articles, zone }) {
    const s = await loadState(ctx.env);
    const { z, err: ez } = trouverZone(s, zone ?? "");
    if (ez) return ez;
    const { ok, ko, err } = lignes(s, articles);
    if (err) return err;
    if (ko.length) return { erreur: "stock insuffisant pour " + ko.map((k) => k.ref).join(", ") };
    const total = ok.reduce((t, l) => t + l.sous_total_fcfa, 0) + s.zones[z];
    const id = (Number(await ctx.env.KV.get("order_seq")) || 0) + 1;
    await ctx.env.KV.put("order_seq", String(id));
    const cmd = { id, chatId: ctx.chatId, client: client || "client", lignes: ok, zone: z,
                  frais_livraison_fcfa: s.zones[z], total_fcfa: total, status: "pending", date: new Date().toISOString() };
    await ctx.env.KV.put(`order:${id}`, JSON.stringify(cmd));
    const resume = ok.map((l) => `${l.quantite} ${l.nom}`).join(", ");
    await ctx.notifyAfi(`Commande #${id} : ${resume}, ${z}, ${total} FCFA (client ${cmd.client}). Répondez OUI ${id} ou NON ${id}`);
    return { commande_en_attente_de_validation: id, total_fcfa: total, delai: s.delai,
             info: "Rien n'est encore enregistré : Afi doit valider. Le client sera prévenu du résultat par message. Dis-le au client." };
  },
};

// Validation par Afi : seul son numéro y arrive (contrôlé dans index.js).
export async function trancherCommande(env, id, accepter) {
  const raw = await env.KV.get(`order:${id}`, "json");
  if (!raw) return { texte: `Commande #${id} introuvable.` };
  if (raw.status !== "pending") return { texte: `Commande #${id} déjà traitée (${raw.status}).` };
  if (!accepter) {
    raw.status = "refused";
    await env.KV.put(`order:${id}`, JSON.stringify(raw));
    return { texte: `Commande #${id} refusée.`, client: raw.chatId,
             clientTexte: "Désolé, votre commande n'a pas pu être acceptée pour le moment. Rien n'a été enregistré. N'hésitez pas à nous réécrire 🙏" };
  }
  const s = await loadState(env);
  for (const l of raw.lignes) {
    const p = trouverProduit(s, l.ref);
    if (!p || p.stock < l.quantite)
      return { texte: `Commande #${id} impossible : stock insuffisant pour ${l.ref}.` };
  }
  for (const l of raw.lignes) trouverProduit(s, l.ref).stock -= l.quantite;
  await saveState(env, s);
  raw.status = "validated";
  await env.KV.put(`order:${id}`, JSON.stringify(raw));
  return { texte: `Commande #${id} enregistrée, stock mis à jour.`, client: raw.chatId,
           clientTexte: `Bonne nouvelle, votre commande #${id} est confirmée ! Total : ${raw.total_fcfa} FCFA, livraison à ${raw.zone} sous ${s.delai}. Merci 🎉` };
}

const ART = { type: "array", description: 'Liste d\'articles, ex. [{"ref":"P05","quantite":2}]',
  items: { type: "object", properties: { ref: { type: "string" }, quantite: { type: "integer" } }, required: ["ref", "quantite"] } };
const sch = (name, description, properties, required) =>
  ({ type: "function", function: { name, description, parameters: { type: "object", properties, required } } });

export const SCHEMAS = [
  sch("chercher_produits", "Trouver des produits du catalogue par mot-clé (ex. 'pagne', 'sac'). À utiliser dès que le client parle d'un produit ou demande un prix. Renvoie réf, nom, prix FCFA.", { mot_cle: { type: "string" } }, ["mot_cle"]),
  sch("verifier_stock", "Donner le stock d'une référence (ex. 'P03'). À utiliser AVANT de promettre un produit. Si rupture, renvoie des alternatives en stock.", { ref: { type: "string" } }, ["ref"]),
  sch("frais_livraison", "Donner les frais (FCFA) et le délai de livraison pour une zone (ex. 'Calavi'). Si zone non desservie, renvoie la liste des zones desservies.", { zone: { type: "string" } }, ["zone"]),
  sch("convertir_devise", "Convertir un montant FCFA vers une devise (EUR, USD, NGN...) au taux réel du moment. À utiliser dès que le client veut un prix dans sa devise.", { montant_fcfa: { type: "number" }, devise: { type: "string", description: "code ISO, ex. EUR" } }, ["montant_fcfa", "devise"]),
  sch("calculer_total", "Calculer le total exact d'un panier = articles + livraison. À utiliser pour tout total annoncé ; ne jamais calculer à la main. Les produits en rupture sont exclus et signalés.", { articles: ART, zone: { type: "string" } }, ["articles", "zone"]),
  sch("statut_commande", "Donner l'état réel d'une commande du client (en attente, confirmée ou refusée par Afi). À utiliser dès que le client demande où en est sa commande, ou avant d'affirmer qu'une commande est en attente, confirmée ou refusée. Sans numéro : la dernière commande du client.", { numero: { type: "integer", description: "numéro de commande, optionnel" } }, []),
  sch("creer_commande", "Soumettre une commande à Afi (IRRÉVERSIBLE une fois validée). Seulement après que le client a confirmé explicitement le récapitulatif ET le total. Afi valide ensuite par WhatsApp.", { client: { type: "string" }, articles: ART, zone: { type: "string" } }, ["client", "articles", "zone"]),
];
