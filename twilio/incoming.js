// The bell's ear — deployed by .github/workflows/wire-sms.yml (run it after
// any edit here; it rebuilds the Twilio Function and re-points the number).
//
// WHAT THE BELL IS (first principles, 2026-10-05):
//   1. The ring   — a text when the water is perfect. Rare. The staple.
//   2. The forecast — the week ahead, every Wednesday. Comes with the bell;
//                     BELL ONLY switches it off, FORECAST brings it back.
//   3. Your word back — FINS / REEF / BUDDY after a dive; the bell answers
//                     with the dive it logged.
// That is the whole product. Nothing here should grow a fourth thing.
//
// Stateless on purpose: the message history IS the subscriber database (the
// repo's sms_subscribers() / sms_digest_optins() read it back). STOP is
// handled by Twilio before this runs. GSM-7 only in replies: one styled
// character silently halves every segment.
//
// Old words still work, silently: WEEK/WEEKLY/DIGEST = FORECAST,
// QUIET/NO FORECAST = BELL ONLY.

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
const OFF_WORDS = ["BELL ONLY", "NO FORECAST", "QUIET", "DIGEST OFF", "NODIGEST"];
const ON_WORDS = ["FORECAST", "WEEK", "DIGEST"];          // WEEKLY contains WEEK
const QUERY_WORDS = ["FORECAST", "WEEK", "REEF", "BUDDY", "FINS"];

const bellIn = (b) => Object.keys(BELLS).find((k) => b.includes(k));
const isQuery = (b) => QUERY_WORDS.some((q) => b.includes(q));

// ── when was the dive? ────────────────────────────────────────────────
const DAYS = {
  SUN: 0, SUNDAY: 0, MON: 1, MONDAY: 1, TUE: 2, TUES: 2, TUESDAY: 2,
  WED: 3, WEDS: 3, WEDNESDAY: 3, THU: 4, THUR: 4, THURS: 4, THURSDAY: 4,
  FRI: 5, FRIDAY: 5, SAT: 6, SATURDAY: 6,
};
const DAY_FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                  "Friday", "Saturday"];
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
// A diver names their dive the human way: "Friday evening", not "Fri dusk".
function describe(w) {
  return DAY_FULL[w.date.getUTCDay()] + (w.kind === "dawn" ? " morning" : " evening");
}

// The bell's guess from the clock alone, plus the likeliest correction to
// offer. Before 10am you are telling it about last night; through the
// afternoon, this morning; after dark, tonight.
function guessWhen(now, tz) {
  const { day, hour } = localParts(now, tz);
  if (hour < 10) return { date: addDays(day, -1), kind: "dusk", alt: "THIS MORNING" };
  if (hour < 19) return { date: day, kind: "dawn", alt: "LAST NIGHT" };
  return { date: day, kind: "dusk", alt: "THIS MORNING" };
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

// "December through January, mostly" -> "December through January";
// "March, August, October" -> "March, August and October".
function seasonPhrase(s) {
  if (!s) return "";
  let t = String(s).replace(/,?\s*mostly\s*$/i, "").trim();
  if (!/through/i.test(t) && t.includes(",")) {
    const parts = t.split(",").map((x) => x.trim()).filter(Boolean);
    t = parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1];
  }
  return t;
}

// verdict|sms|<bell>|<YYYY-MM-DD>|<dawn|dusk>|<guess|confirmed>|<report id>
// The report id lets a correction supersede the guess it corrects (the
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

// The bell's own board — the same file the site reads.
async function zoneFor(name) {
  try {
    const r = await fetch("https://thedivebell.com/data/zones.json?t=" + Date.now());
    const d = await r.json();
    return Object.values(d.zones).find(
      (x) => x.name === name || (x.bell && x.bell.name === name)
    ) || null;
  } catch (e) {
    return null;
  }
}

// The forecast is written once, by the engine, and published per bell as
// forecast_sms — the Wednesday text and this on-demand reply are the same
// words, so they cannot drift apart. The fallback only covers a board
// published before that field existed.
async function forecastMessage(name) {
  const z = await zoneFor(name);
  if (z && z.forecast_sms) return z.forecast_sms;
  if (z && z.windows && z.windows.length) {
    const best = z.windows.reduce((a, b) => (b.score > a.score ? b : a));
    return "THE DIVE BELL - " + name.toUpperCase() + ". Best: " + best.label + ", " +
      best.score.toFixed(1) + " of 10. thedivebell.com";
  }
  return "The bell's board is briefly unreadable - try again in a minute, or see thedivebell.com";
}

// The first text says what the bell is, when THIS bell tends to ring, and
// the one switch a new subscriber might want.
function welcomeText(name, rawSeason) {
  const season = seasonPhrase(rawSeason);
  return "THE DIVE BELL - " + name.toUpperCase() + ". You're on. The bell rings " +
    "only when the water is perfect - rarely" +
    (season ? ", and at " + name + " mostly " + season : "") + ". " +
    "Each Wednesday you also get the week's forecast; text BELL ONLY for the " +
    "ring alone. " + LEGAL;
}
async function welcome(name) {
  const z = await zoneFor(name);
  return welcomeText(name, z && z.casting && z.casting.season);
}

// Everything this sender said before now, newest first.
async function priorTexts(context, event) {
  try {
    const client = context.getTwilioClient();
    const msgs = await client.messages.list({
      from: event.From, to: event.To, limit: 200,
    });
    return msgs
      .filter((m) => m.sid !== event.MessageSid)
      .map((m) => ({
        sid: m.sid, body: (m.body || "").toUpperCase(),
        when: m.dateSent || m.dateCreated,
      }));
  } catch (e) {
    return [];
  }
}

// The sender's home water. Asking about another water ("MAUI FORECAST") or
// reporting a dive there ("FINS FRI NIGHT DANA") is a question, not a move:
// a plain join outranks any query. Mirrors sms_subscribers() in the engine.
function homeBell(prior) {
  const joined = prior.find((m) => bellIn(m.body) && !isQuery(m.body));
  const asked = prior.find((m) => bellIn(m.body));
  const hit = joined || asked;
  return hit ? BELLS[bellIn(hit.body)] : null;
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").trim().toUpperCase();
  const reply = (t) => { twiml.message(t); return callback(null, twiml); };

  if (body === "HELP" || body === "INFO") {
    return reply(
      "The Dive Bell: a text when your water is perfect, plus the week's " +
      "forecast each Wednesday. Text BELL ONLY for the ring alone, FORECAST " +
      "to see the week now. Info: thedivebell.com. " + LEGAL
    );
  }

  const verdict = Object.keys(VERDICTS).find((v) => body.includes(v));
  const bellWord = bellIn(body);
  const prior = await priorTexts(context, event);
  const home = homeBell(prior);
  const hourAgo = Date.now() - 3600 * 1000;
  const recent = prior.find((m) => Object.keys(VERDICTS).some((v) => m.body.includes(v)) &&
    new Date(m.when).getTime() > hourAgo);

  // 3. YOUR WORD BACK. One text in, one text back, naming the dive.
  if (verdict) {
    const water = (bellWord ? BELLS[bellWord] : null) || home || "";
    const tz = BELL_TZ[water] || "America/Los_Angeles";
    const now = new Date();
    const said = parseWhen(body, now, tz);
    const w = said || guessWhen(now, tz);
    const at = water ? " at " + water : "";
    // a verdict re-sent with the right time, minutes after a wrong guess,
    // corrects that report rather than adding a second one
    const rid = said && recent ? recent.sid : event.MessageSid;
    await logVerdict(verdict, water, w, said ? "confirmed" : "guess", rid);
    return reply(
      "Logged: " + verdict.toLowerCase() + " for " + describe(w) + at +
      ". The bell learns from every dive." +
      (said ? "" : " Wrong dive? Text " + verdict + " " + w.alt + ".")
    );
  }
  // ...and a bare "THIS MORNING" right after a verdict corrects it too.
  if (recent && !bellWord && !OFF_WORDS.concat(ON_WORDS).some((k) => body.includes(k))) {
    const rv = Object.keys(VERDICTS).find((v) => recent.body.includes(v));
    const rb = bellIn(recent.body);
    const water = (rb ? BELLS[rb] : null) || home || "";
    const said = parseWhen(body, new Date(), BELL_TZ[water] || "America/Los_Angeles");
    if (said) {
      await logVerdict(rv, water, said, "confirmed", recent.sid);
      return reply(
        "Logged: " + rv.toLowerCase() + " for " + describe(said) +
        (water ? " at " + water : "") + ". The bell learns from every dive."
      );
    }
  }

  // 2. THE FORECAST. Off-words first: "NO FORECAST" contains "FORECAST".
  if (OFF_WORDS.some((k) => body.includes(k))) {
    return reply(
      "Bell only, then. You'll hear from it when the water is perfect, and " +
      "not before. Text FORECAST for Wednesdays again."
    );
  }
  if (ON_WORDS.some((k) => body.includes(k))) {
    const name = bellWord ? BELLS[bellWord] : home;
    if (!name) {
      return reply(
        "The bell doesn't know your water yet. Text its name - LAGUNA, " +
        "MONTEREY, MAUI... - and you're on. thedivebell.com"
      );
    }
    // someone whose very first text is "FORECAST LAGUNA" has just joined:
    // the first thing they are owed is the welcome, with its terms
    if (bellWord && !prior.some((m) => bellIn(m.body))) {
      return reply(await welcome(name));
    }
    return reply(await forecastMessage(name));
  }

  // 1. THE BELL. Text a water; you're on.
  if (bellWord) return reply(await welcome(BELLS[bellWord]));

  return reply(
    "The Dive Bell: text the water you dive to get on its bell - LAGUNA, " +
    "MONTEREY, CATALINA, MAUI... Full board: thedivebell.com " + LEGAL
  );
};
