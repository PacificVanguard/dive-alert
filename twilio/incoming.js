// The bell's ear — deployed by .github/workflows/wire-sms.yml (run it after
// any edit here; it rebuilds the Twilio Function and re-points the number).
//
// Stateless on purpose: the message history IS the subscriber database
// (the repo's sms_subscribers() reads it back). This only has to answer
// well. STOP/HELP are handled by Twilio Advanced Opt-Out before this runs.
//
// Verdicts (REEF / BUDDY / FINS) are forwarded into the same ntfy feedback
// pipeline the app buttons use — one calibration river, two mouths.
//
// WEEK: the bell answers with the week ahead, read live from the site's own
// data/zones.json — the same truth the page shows. "LAGUNA WEEK" names the
// water directly; bare "WEEK" finds your bell in the message history.
// GSM-7 only in replies (no unicode) — one styled char silently halves
// every segment.

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

async function weekMessage(name) {
  try {
    const r = await fetch("https://thedivebell.com/data/zones.json?t=" + Date.now());
    const d = await r.json();
    const z = Object.values(d.zones).find(
      (x) => x.name === name || (x.bell && x.bell.name === name)
    );
    if (!z || !z.windows || !z.windows.length) throw new Error("no board");
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
    return (
      "THE DIVE BELL - " + name.toUpperCase() + "\n" + strip + "\n" +
      read + " thedivebell.com"
    );
  } catch (e) {
    return "The bell's board is briefly unreadable - try again in a minute, or see thedivebell.com";
  }
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").trim().toUpperCase();

  const verdict = Object.keys(VERDICTS).find((v) => body.includes(v));
  const bellWord = Object.keys(BELLS).find((b) => body.includes(b));

  // HELP is normally intercepted by Twilio Advanced Opt-Out before this runs;
  // handled here too so the reply is identical either way (and so the text
  // matches the toll-free verification submission exactly).
  if (body === "HELP" || body === "INFO") {
    twiml.message(
      "The Dive Bell: dive conditions alerts for the water you chose. " +
      "About 15 msgs/yr. Text WEEK for the week ahead. Info: thedivebell.com. " +
      "Msg&data rates may apply. Reply STOP to end."
    );
  } else if (verdict) {
    try {
      await fetch("https://ntfy.sh/" + FB_TOPIC, {
        method: "POST", body: VERDICTS[verdict] + "|sms",
      });
    } catch (e) {}
    twiml.message(
      "Noted - " + verdict.toLowerCase() + " it is. The bell learns from every dive."
    );
  } else if (body.includes("WEEK")) {
    let name = bellWord ? BELLS[bellWord] : null;
    if (!name) {
      // whose bell? the message history remembers — newest word wins
      try {
        const client = context.getTwilioClient();
        const msgs = await client.messages.list({
          from: event.From, to: event.To, limit: 500,
        });
        for (const m of msgs) {
          const b = (m.body || "").toUpperCase();
          const hit = Object.keys(BELLS).find((k) => b.includes(k));
          if (hit) { name = BELLS[hit]; break; }
        }
      } catch (e) {}
    }
    if (name) {
      twiml.message(await weekMessage(name));
    } else {
      twiml.message(
        "The bell doesn't know your water yet. Text its name first - LAGUNA, " +
        "MONTEREY, MAUI... - then WEEK any time. thedivebell.com"
      );
    }
  } else if (bellWord) {
    twiml.message(
      "The Dive Bell: you're on the " + BELLS[bellWord] + " bell. Most mornings " +
      "the ocean says no; when it says yes, it rings - about 15 msgs/yr. " +
      "Text WEEK any time for the week ahead. After a dive text REEF, BUDDY " +
      "or FINS. thedivebell.com " +
      "Msg&data rates may apply. Reply HELP for help, STOP to end."
    );
  } else {
    twiml.message(
      "The Dive Bell: text a water to get on its bell - LAGUNA, MONTEREY, " +
      "CATALINA, MAUI... - or WEEK for the week ahead. Full board: " +
      "thedivebell.com Msg&data rates may apply. Reply HELP for help, STOP to end."
    );
  }
  return callback(null, twiml);
};
