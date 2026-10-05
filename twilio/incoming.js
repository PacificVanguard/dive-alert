// The bell's ear — deployed by .github/workflows/wire-sms.yml (run it after
// any edit here; it rebuilds the Twilio Function and re-points the number).
//
// Stateless on purpose: the message history IS the subscriber database
// (the repo's sms_subscribers() / sms_digest_optins() read it back). This
// only has to answer well. STOP is handled by Twilio before this runs.
//
// The whole vocabulary a diver needs is two words: their WATER to join,
// WEEK to ask. Joining includes the Wednesday reading — the ritual is the
// product — and the welcome IS this week's reading. QUIET drops to rings
// only; WEEKLY brings the reading back.
//
// Verdicts (REEF / BUDDY / FINS) feed the calibration river — and a verdict
// is worthless unless the bell knows WHICH DIVE it grades. A diver home from
// Friday night's lobster opener texts on Saturday morning; so the bell asks:
// "Was that Fri dusk at Laguna Beach?" — YES confirms, or they just say when
// (LAST NIGHT, THIS MORNING, SAT DAWN). GSM-7 only in replies: one styled
// char silently halves a segment.

const BELLS = {
  LAGUNA: "Laguna Beach", DANA: "Dana Point", JOLLA: "La Jolla",
  MONTEREY: "Monterey", CATALINA: "Catalina", PALOS: "Palos Verdes",
  VERDES: "Palos Verdes", LOBOS: "Point Lobos", SANTA: "Santa Barbara",
  BARBARA: "Santa Barbara", OAHU: "Oahu North Shore", KONA: "Kona",
  BONAIRE: "Bonaire", MALIBU: "Malibu", VENTURA: "Ventura County",
  SYDNEY: "Sydney", MAUI: "Maui",
};
// each bell keeps its own clock; anything unlisted is Pacific
const BELL_TZ = {
  "Oahu North Shore": "Pacific/Honolulu", "Kona": "Pacific/Honolulu",
  "Maui": "Pacific/Honolulu", "Bonaire": "America/Kralendijk",
  "Sydney": "Australia/Sydney",
};
const VERDICTS = { REEF: "clear", BUDDY: "fair", FINS: "murk" };
const FB_TOPIC = "laguna-dive-86dd82e0-fb";   // the calibration river
const LEGAL = "Msg&data rates may apply. Reply HELP for help, STOP to end.";

// ── when was the dive? ────────────────────────────────────────────────
const DAYS = {
  SUN: 0, SUNDAY: 0, MON: 1, MONDAY: 1, TUE: 2, TUES: 2, TUESDAY: 2,
  WED: 3, WEDS: 3, WEDNESDAY: 3, THU: 4, THUR: 4, THURS: 4, THURSDAY: 4,
  FRI: 5, FRIDAY: 5, SAT: 6, SATURDAY: 6,
};
const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const DAWN_WORDS = ["DAWN", "MORNING", "SUNRISE", "AM"];
const DUSK_WORDS = ["DUSK", "EVENING", "NIGHT", "SUNSET", "AFTERNOON", "PM", "TONIGHT"];

// The bell's local calendar day (as a UTC-midnight stand-in, so day math is
// plain arithmetic) and local hour, for any instant.
function localParts(date, tz) {
  const p = {};
  new Intl.DateTimeFormat("en-US", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", hourCycle: "h23",
  }).formatToParts(date).forEach((x) => { p[x.type] = x.value; });
  return {
    day: new Date(Date.UTC(+p.year, +p.month - 1, +p.day)),
    hour: +p.hour % 24,
  };
}
function addDays(d, n) { return new Date(d.getTime() + n * 86400000); }
function isoDay(d) { return d.toISOString().slice(0, 10); }
function describe(w) { return DAY_NAMES[w.date.getUTCDay()] + " " + w.kind; }

// The bell's guess from the clock alone. Before 10am you are telling it
// about last night; through the afternoon, this morning; after dark, tonight.
function guessWhen(now, tz) {
  const { day, hour } = localParts(now, tz);
  if (hour < 10) return { date: addDays(day, -1), kind: "dusk" };
  if (hour < 19) return { date: day, kind: "dawn" };
  return { date: day, kind: "dusk" };
}

// What the diver actually said, if anything: LAST NIGHT, YESTERDAY, THIS
// MORNING, TONIGHT, a day name, DAWN / DUSK. Words are matched whole, so
// MONTEREY is never Monday. Returns null when no time was named at all.
function parseWhen(text, now, tz) {
  const words = String(text || "").toUpperCase().split(/[^A-Z]+/).filter(Boolean);
  const has = (w) => words.includes(w);
  const { day, hour } = localParts(now, tz);
  let kind = DAWN_WORDS.some(has) ? "dawn" : DUSK_WORDS.some(has) ? "dusk" : null;
  let date = null, named = false;
  if (has("LAST") && has("NIGHT")) { date = addDays(day, -1); kind = "dusk"; }
  else if (has("YESTERDAY")) { date = addDays(day, -1); }
  else if (has("TONIGHT")) { date = day; kind = "dusk"; }
  else if (has("TODAY") || (has("THIS") && kind)) { date = day; }
  else {
    const dw = words.find((w) => Object.prototype.hasOwnProperty.call(DAYS, w));
    if (dw !== undefined) {
      date = addDays(day, -((day.getUTCDay() - DAYS[dw] + 7) % 7));
      named = true;
    }
  }
  if (!date && !kind) return null;
  if (!kind) kind = "dawn";                       // most dives are mornings
  if (!date) {                                    // a bare DAWN / DUSK
    date = (kind === "dusk" && hour < 17) ? addDays(day, -1) : day;
  } else if (named && isoDay(date) === isoDay(day)) {
    // "FRI DUSK" said on Friday morning means LAST Friday's — today's
    // hasn't happened yet
    if ((kind === "dusk" && hour < 17) || (kind === "dawn" && hour < 5)) {
      date = addDays(date, -7);
    }
  }
  return { date, kind };
}

// verdict|sms|<bell>|<YYYY-MM-DD>|<dawn|dusk>|<guess|confirmed>|<report id>
// The report id ties a later confirmation to the guess it answers (the
// engine keeps the newest line per id). It is the tail of Twilio's message
// SID — opaque, and not a phone number.
async function logVerdict(verdict, water, w, status, sid) {
  try {
    await fetch("https://ntfy.sh/" + FB_TOPIC, {
      method: "POST",
      body: [VERDICTS[verdict], "sms", water, isoDay(w.date), w.kind, status,
             String(sid || "").slice(-12)].join("|"),
    });
  } catch (e) {}
}

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
    "The bell reads you the week each Wednesday, texts when the week's best " +
    "morning firms up, and rings when it's perfect. WEEK any time, QUIET " +
    "for rings only. " + LEGAL;
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

// This sender's most recent verdict text — what a YES is answering.
async function lastVerdict(context, event) {
  try {
    const client = context.getTwilioClient();
    const msgs = await client.messages.list({
      from: event.From, to: event.To, limit: 60,
    });
    for (const m of msgs) {
      if (m.sid === event.MessageSid) continue;
      const b = (m.body || "").toUpperCase();
      const v = Object.keys(VERDICTS).find((k) => b.includes(k));
      if (v) {
        return { sid: m.sid, body: b, verdict: v, when: m.dateSent || m.dateCreated };
      }
    }
  } catch (e) {}
  return null;
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").trim().toUpperCase();
  const words = body.split(/[^A-Z]+/).filter(Boolean);

  const verdict = Object.keys(VERDICTS).find((v) => body.includes(v));
  const bellWord = Object.keys(BELLS).find((b) => body.includes(b));

  // An answer to "Was that Fri dusk?" — YES, NO, or simply the time. These
  // words mean something only while this sender has a recent verdict
  // waiting; otherwise the text falls through to the ordinary vocabulary.
  const isYes = words.length <= 2 &&
    ["YES", "Y", "YEP", "YEAH", "YUP", "CORRECT", "RIGHT"].includes(words[0]);
  const isNo = words.length <= 2 && ["NO", "N", "NOPE", "WRONG"].includes(words[0]);
  const modeWord = ["QUIET", "WEEK", "DIGEST", "HELP", "INFO"].some((w) => body.includes(w));
  if (!verdict && !modeWord &&
      (isYes || isNo || parseWhen(body, new Date(), "America/Los_Angeles"))) {
    const last = await lastVerdict(context, event);
    if (last && Date.now() - new Date(last.when).getTime() < 72 * 3600 * 1000) {
      const lb = Object.keys(BELLS).find((b) => last.body.includes(b));
      const water = (bellWord ? BELLS[bellWord] : null) || (lb ? BELLS[lb] : null) ||
        (await senderBell(context, event)) || "";
      const tz = BELL_TZ[water] || "America/Los_Angeles";
      const at = water ? " at " + water : "";
      if (isNo) {
        twiml.message(
          "No trouble. Tell me when the dive was - LAST NIGHT, THIS MORNING, " +
          "SAT DAWN, FRI DUSK."
        );
      } else {
        const asked = new Date(last.when);
        const w = isYes
          ? (parseWhen(last.body, asked, tz) || guessWhen(asked, tz))
          : parseWhen(body, new Date(), tz);
        await logVerdict(last.verdict, water, w, "confirmed", last.sid);
        twiml.message(
          "Logged - " + last.verdict.toLowerCase() + " for " + describe(w) + at +
          ". The bell learns from every dive."
        );
      }
      return callback(null, twiml);
    }
  }

  // Order matters: QUIET before anything; WEEKLY before WEEK (a substring);
  // DIGEST OFF before DIGEST. A bell word alongside a mode word joins AND
  // sets the mode in one text.
  if (body === "HELP" || body === "INFO") {
    twiml.message(
      "The Dive Bell: dive conditions for the water you chose - the week each " +
      "Wednesday, the week's best morning when it firms up, and a ring when " +
      "it's perfect. Text WEEK for the week ahead, QUIET for rings only. " +
      "Info: thedivebell.com. " + LEGAL
    );
  } else if (verdict) {
    // Which dive? If they said, take their word; if not, guess from the
    // clock, log the guess so nothing is lost, and ASK.
    const water = (bellWord ? BELLS[bellWord] : null) ||
      (await senderBell(context, event)) || "";
    const tz = BELL_TZ[water] || "America/Los_Angeles";
    const now = new Date();
    const said = parseWhen(body, now, tz);
    const w = said || guessWhen(now, tz);
    const at = water ? " at " + water : "";
    await logVerdict(verdict, water, w, said ? "confirmed" : "guess", event.MessageSid);
    if (said) {
      twiml.message(
        "Logged - " + verdict.toLowerCase() + " for " + describe(w) + at +
        ". The bell learns from every dive."
      );
    } else {
      twiml.message(
        "Noted - " + verdict.toLowerCase() + ". Was that " + describe(w) + at +
        "? Reply YES, or tell me when - LAST NIGHT, THIS MORNING, SAT DAWN."
      );
    }
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
