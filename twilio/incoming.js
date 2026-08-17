// The bell's ear — paste into a Twilio Function (Functions & Assets → 
// Services → create "divebell" → add function /incoming → paste → deploy),
// then set the toll-free number's Messaging webhook to this Function.
//
// Stateless on purpose: the message history IS the subscriber database
// (the repo's sms_subscribers() reads it back). This only has to answer
// well. STOP/HELP are handled by Twilio Advanced Opt-Out before this runs.
//
// Verdicts (REEF / BUDDY / FINS) are forwarded into the same ntfy feedback
// pipeline the app buttons use — one calibration river, two mouths.

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

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").trim().toUpperCase();

  const verdict = Object.keys(VERDICTS).find((v) => body.includes(v));
  const bellWord = Object.keys(BELLS).find((b) => body.includes(b));

  if (verdict) {
    try {
      await fetch("https://ntfy.sh/" + FB_TOPIC, {
        method: "POST", body: VERDICTS[verdict] + "|sms",
      });
    } catch (e) {}
    twiml.message(
      "Noted - " + verdict.toLowerCase() + " it is. The bell learns from every dive."
    );
  } else if (bellWord) {
    twiml.message(
      "You're on the " + BELLS[bellWord] + " bell. It stays quiet on purpose - " +
      "most mornings the ocean says no. When everything lines up, it rings. " +
      "After a dive, text REEF (saw it all), BUDDY, or FINS. " +
      "thedivebell.com  Reply STOP to end."
    );
  } else {
    twiml.message(
      "The Dive Bell: text a water to get on its bell - LAGUNA, MONTEREY, " +
      "CATALINA, MAUI, BONAIRE, SYDNEY... The full board: thedivebell.com " +
      "Reply STOP to end."
    );
  }
  return callback(null, twiml);
};
