// The bell's ear — deployed by .github/workflows/wire-sms.yml (run it after
// any edit here; it rebuilds the Twilio Function and re-points the number).
//
// Stateless on purpose: the message history IS the subscriber database
// (the repo's sms_subscribers() / sms_digest_optins() read it back). This
// only has to answer well. STOP is handled by Twilio before this runs.
//
// The whole vocabulary a diver needs is two words: their WATER to join,
// WEEK to ask. Joining includes the Wednesday reading — the ritual is the
// product — and the welcome IS this week's reading, so the bell proves
// itself in the first ten seconds. QUIET drops to rings only; WEEKLY brings
// the reading back. Verdicts (REEF / BUDDY / FINS) feed the calibration
// river. GSM-7 only in replies: one styled char silently halves a segment.

const BELLS = {
  LAGUNA: "Laguna Beach", DANA: "Dana Point", JOLLA: "La Jolla",
  MONTEREY: "Monterey", CATALINA: "Catalina", PALOS: "Palos Verdes",
  VERDES: "Palos Verdes", LOBOS: "Point Lobos", SANTA: "Santa Barbara",
  BARBARA: "Santa Barbara", OAHU: "Oahu North Shore", KONA: "Kona",
  BONAIRE: "Bonaire", MALIBU: "Malibu", VENTURA: "Ventura County",
  SYDNEY: "Sydney", MAUI: "Maui",
};
const VERDICTS = { REEF: "clear", BUDDY: "fair", FINS: "murk" };
const FB_TOPIC = "laguna-dive-86dd82e0-fb";   // the calibration river
const LEGAL = "Msg&data rates may apply. Reply HELP for help, STOP to end.";

// The week ahead for a bell, read live from the site's own board — the same
// truth the page shows. Returns {strip, read} or null when unreadable.
async function weekParts(name) {
  try {
    const r = await fetch("https://thedivebell.com/data/zones.json?t=" + Date.now());
    const d = await r.json();
    const z = Object.values(d.zones).find(
      (x) => x.name === name || (x.bell && x.bell.name === name)
    );
    if (!z || !z.windows || !z.windows.length) return null;
    const best = z.windows.reduce((a, b) => (b.score > a.score ? b : a));
    const ringing = z.windows.filter((w) => w.gate);
    const strip = z.windows
      .map((w) => {
        let s = w.label + " " + w.score.toFixed(1);
        if (w === best) s += "*";
        if (w.gate) s += " RINGING";
        return s;
      })
      .join(" / ");
    const entry = (best.entries && best.entries[0]) || "your cove";
    let read;
    if (ringing.length) {
      const r0 = ringing[0];
      read =
        "The gate stands open " + r0.label +
        " - every knowable thing aligned. " +
        ((r0.entries && r0.entries[0]) || entry) + " is the door.";
    } else if (best.score >= 7) {
      read =
        best.label + " is the one to watch - " + entry +
        (best.limit && best.limit !== "all clear"
          ? ". Held back only by " + best.limit + "."
          : ". Nothing in the way but the water's last word.");
    } else {
      read =
        "A quiet stretch - best is " + best.label + " at " +
        best.score.toFixed(1) +
        (best.limit && best.limit !== "all clear"
          ? ", held back by " + best.limit
          : "") +
        ". The bell keeps its silence for a reason.";
    }
    return { strip, read };
  } catch (e) {
    return null;
  }
}

async function weekMessage(name) {
  const p = await weekParts(name);
  if (!p) {
    return "The bell's board is briefly unreadable - try again in a minute, or see thedivebell.com";
  }
  return "THE DIVE BELL - " + name.toUpperCase() + "\n" + p.strip + "\n" +
    p.read + " thedivebell.com";
}

// The first thing a new subscriber ever sees is the water, not a manual.
async function welcome(name) {
  const p = await weekParts(name);
  const head = "THE DIVE BELL - " + name.toUpperCase() + ". You're on.";
  const tail =
    "The bell reads you the week each Wednesday and texts the morning it " +
    "rings. WEEK any time, QUIET for rings only. " + LEGAL;
  if (!p) return head + "\n" + tail;
  return head + "\n" + p.strip + "\n" + p.read + "\n" + tail;
}

async function senderBell(context, event) {
  // the sender's water, from the message history that is the subscriber db
  try {
    const client = context.getTwilioClient();
    const msgs = await client.messages.list({
      from: event.From, to: event.To, limit: 500,
    });
    for (const m of msgs) {
      const b = (m.body || "").toUpperCase();
      const hit = Object.keys(BELLS).find((k) => b.includes(k));
      if (hit) return BELLS[hit];
    }
  } catch (e) {}
  return null;
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").trim().toUpperCase();

  const verdict = Object.keys(VERDICTS).find((v) => body.includes(v));
  const bellWord = Object.keys(BELLS).find((b) => body.includes(b));

  // Order matters: QUIET before anything; WEEKLY before WEEK (a substring);
  // DIGEST OFF before DIGEST. A bell word alongside a mode word joins AND
  // sets the mode in one text.
  if (body === "HELP" || body === "INFO") {
    twiml.message(
      "The Dive Bell: dive conditions for the water you chose - the week " +
      "each Wednesday, plus a text the morning it rings. Text WEEK for the " +
      "week ahead, QUIET for rings only. Info: thedivebell.com. " + LEGAL
    );
  } else if (verdict) {
    // verdict|sms|<bell name> — the water travels with the verdict, so the
    // calibration river knows which coast is speaking
    const water = (bellWord ? BELLS[bellWord] : null) || (await senderBell(context, event)) || "";
    try {
      await fetch("https://ntfy.sh/" + FB_TOPIC, {
        method: "POST", body: VERDICTS[verdict] + "|sms|" + water,
      });
    } catch (e) {}
    twiml.message(
      "Noted - " + verdict.toLowerCase() + " it is. The bell learns from every dive."
    );
  } else if (body.includes("QUIET") || body.includes("DIGEST OFF") || body.includes("NODIGEST")) {
    twiml.message(
      "Rings only, then. The bell will speak when your water lines up, and " +
      "not before. Text WEEKLY to bring the Wednesday reading back."
    );
  } else if (body.includes("WEEKLY") || body.includes("DIGEST")) {
    twiml.message(
      "The Wednesday reading is yours again - your water's week ahead, every " +
      "week. Text QUIET for rings only. " + LEGAL
    );
  } else if (body.includes("WEEK")) {
    const name = bellWord ? BELLS[bellWord] : await senderBell(context, event);
    if (name) {
      twiml.message(await weekMessage(name));
    } else {
      twiml.message(
        "The bell doesn't know your water yet. Text its name first - LAGUNA, " +
        "MONTEREY, MAUI... - then WEEK any time. thedivebell.com"
      );
    }
  } else if (bellWord) {
    twiml.message(await welcome(BELLS[bellWord]));
  } else {
    twiml.message(
      "The Dive Bell: text a water to get on its bell - LAGUNA, MONTEREY, " +
      "CATALINA, MAUI... Full board: thedivebell.com " + LEGAL
    );
  }
  return callback(null, twiml);
};
