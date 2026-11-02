// Webhook Green API -> agent -> réponse WhatsApp. Validation des commandes par Afi uniquement.
import { runAgent } from "./agent.js";
import { trancherCommande } from "./shop.js";

const ok = (body = "ok") => new Response(body, { status: 200 });

const log = (o) => console.log(JSON.stringify({ t: new Date().toISOString(), ...o }));

export async function envoyer(env, chatId, message) {
  if (env.DRY_RUN === "1") {                       // tests locaux : pas d'appel Green API, on garde une boîte d'envoi
    const box = (await env.KV.get("outbox", "json")) ?? [];
    box.push({ chatId, message });
    await env.KV.put("outbox", JSON.stringify(box.slice(-50)));
    log({ evt: "envoi_simule", chatId, message });
    return;
  }
  const url = `${env.GREEN_API_URL}/waInstance${env.GREEN_ID_INSTANCE}/sendMessage/${env.GREEN_API_TOKEN}`;
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify({ chatId, message }) });
  log({ evt: "envoi", chatId, status: r.status });
}

// Green API envoie le jeton tel que configuré dans l'en-tête Authorization (avec ou sans « Bearer »).
function autorise(req, env) {
  const h = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  return !!env.WEBHOOK_TOKEN && h === env.WEBHOOK_TOKEN;
}

function extraireTexte(body) {
  const d = body.messageData;
  if (d?.typeMessage === "textMessage") return d.textMessageData?.textMessage;
  if (d?.typeMessage === "extendedTextMessage") return d.extendedTextMessageData?.text;
  return null;
}

async function limiteAtteinte(env, chatId) {
  const max = Number(env.MAX_MSG_PER_HOUR) || 20;
  const cle = `rl:${chatId}:${Math.floor(Date.now() / 3600000)}`;
  const n = (Number(await env.KV.get(cle)) || 0) + 1;
  await env.KV.put(cle, String(n), { expirationTtl: 7200 });
  return n > max;
}

async function traiter(env, chatId, texte, idMessage) {
  const afi = chatId === env.AFI_CHAT_ID;
  const valid = texte.trim().match(/^(oui|non)\s+#?(\d+)\.?$/i);

  if (valid) {
    if (!afi) {                                     // « OUI 12 » écrit par un client : refusé
      log({ evt: "validation_refusee", chatId, texte });
      return envoyer(env, chatId, "Seule Afi peut valider une commande. Vous serez prévenu(e) par message dès qu'elle aura répondu.");
    }
    const res = await trancherCommande(env, valid[2], valid[1].toLowerCase() === "oui");
    log({ evt: "validation_afi", texte, resultat: res.texte });
    await envoyer(env, chatId, res.texte);
    if (res.client) {
      await envoyer(env, res.client, res.clientTexte);
      // l'agent doit savoir ce qui a été dit au client : on l'ajoute à son historique
      const cle = `hist:${res.client}`;
      const exchanges = (await env.KV.get(cle, "json")) ?? [];
      exchanges.push({ msgs: [{ role: "assistant", content: res.clientTexte }] });
      await env.KV.put(cle, JSON.stringify(exchanges.slice(-(Number(env.HISTORY_EXCHANGES) || 4))), { expirationTtl: 7 * 86400 });
    }
    return;
  }

  if (await limiteAtteinte(env, chatId)) {
    log({ evt: "limite_messages", chatId });
    return envoyer(env, chatId, "Vous m'écrivez beaucoup 🙂 Patientez un peu, Afi vous répond très vite.");
  }

  const cle = `hist:${chatId}`;
  const exchanges = (await env.KV.get(cle, "json")) ?? [];
  const ctx = { env, chatId, notifyAfi: (m) => envoyer(env, env.AFI_CHAT_ID, m) };
  const t0 = Date.now();
  const { reponse, exchange } = await runAgent(env, ctx, exchanges, texte, (o) => log({ chatId, idMessage, ...o }));
  const garde = Number(env.HISTORY_EXCHANGES) || 4;                // historique borné
  await env.KV.put(cle, JSON.stringify([...exchanges, exchange].slice(-garde)), { expirationTtl: 7 * 86400 });
  await envoyer(env, chatId, reponse);
  log({ evt: "reponse", chatId, ms: Date.now() - t0 });
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (env.DRY_RUN === "1" && url.pathname.startsWith("/debug/")) {      // outils de test local uniquement
      const json = (o) => new Response(JSON.stringify(o), { headers: { "Content-Type": "application/json" } });
      if (url.pathname === "/debug/outbox") return json((await env.KV.get("outbox", "json")) ?? []);
      if (url.pathname === "/debug/state") {
        const orders = [];
        for (const k of (await env.KV.list({ prefix: "order:" })).keys) orders.push(await env.KV.get(k.name, "json"));
        const s = await env.KV.get("state", "json");
        return json({ stock: s && Object.fromEntries(s.produits.map((p) => [p.ref, p.stock])), orders });
      }
      if (url.pathname === "/debug/reset") {
        for (const k of (await env.KV.list()).keys) await env.KV.delete(k.name);
        return json({ reset: true });
      }
    }
    if (req.method !== "POST") return ok("Boutique intelligente : ok");
    if (!autorise(req, env)) return new Response("unauthorized", { status: 401 });

    let body;
    try { body = await req.json(); } catch { return ok(); }
    if (body.typeWebhook !== "incomingMessageReceived") return ok();       // statuts, sortants, etc.
    const chatId = body.senderData?.chatId;
    const texte = extraireTexte(body);
    if (!chatId || chatId.endsWith("@g.us") || !texte) return ok();       // groupes, médias

    const id = body.idMessage;                                             // dédoublonnage
    if (id) {
      if (await env.KV.get(`seen:${id}`)) { log({ evt: "doublon", id }); return ok(); }
      await env.KV.put(`seen:${id}`, "1", { expirationTtl: 86400 });
    }
    // 200 tout de suite, l'agent tourne en arrière-plan
    ctx.waitUntil(traiter(env, chatId, texte, id).catch((e) => log({ evt: "erreur", chatId, id, message: e.message })));
    return ok();
  },
};
