#!/usr/bin/env bash
# Wires the bell's ear: creates/updates the "divebell" serverless service,
# deploys twilio/incoming.js as a protected function at /incoming, and points
# the toll-free number's incoming-message webhook at it. Idempotent — safe to
# re-run after editing incoming.js; each run deploys a fresh version.
# Needs: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM. Runs in CI.
set -euo pipefail

AUTH="$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN"
SLS="https://serverless.twilio.com/v1"

jqr() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }

echo "== service"
SVC=$(curl -sf -u "$AUTH" "$SLS/Services/divebell" 2>/dev/null | jqr "['sid']" || true)
if [ -z "$SVC" ]; then
  SVC=$(curl -sf -u "$AUTH" -X POST "$SLS/Services" \
    -d UniqueName=divebell -d FriendlyName="The Dive Bell" \
    -d IncludeCredentials=True | jqr "['sid']")
  echo "created $SVC"
else
  echo "exists $SVC"
fi

echo "== environment"
ENV=$(curl -sf -u "$AUTH" "$SLS/Services/$SVC/Environments" \
  | python3 -c "import json,sys; e=[x for x in json.load(sys.stdin)['environments'] if x['unique_name']=='prod']; print(e[0]['sid'] if e else '')")
if [ -z "$ENV" ]; then
  ENV=$(curl -sf -u "$AUTH" -X POST "$SLS/Services/$SVC/Environments" \
    -d UniqueName=prod -d DomainSuffix=prod | jqr "['sid']")
  echo "created $ENV"
else
  echo "exists $ENV"
fi

echo "== function"
FN=$(curl -sf -u "$AUTH" "$SLS/Services/$SVC/Functions" \
  | python3 -c "import json,sys; f=[x for x in json.load(sys.stdin)['functions'] if x['friendly_name']=='incoming']; print(f[0]['sid'] if f else '')")
if [ -z "$FN" ]; then
  FN=$(curl -sf -u "$AUTH" -X POST "$SLS/Services/$SVC/Functions" \
    -d FriendlyName=incoming | jqr "['sid']")
  echo "created $FN"
else
  echo "exists $FN"
fi

echo "== upload version"
VER=$(curl -sf -u "$AUTH" -X POST \
  "https://serverless-upload.twilio.com/v1/Services/$SVC/Functions/$FN/Versions" \
  -F "Path=/incoming" -F "Visibility=protected" \
  -F "Content=@twilio/incoming.js; type=application/javascript" | jqr "['sid']")
echo "version $VER"

echo "== build"
BUILD=$(curl -sf -u "$AUTH" -X POST "$SLS/Services/$SVC/Builds" \
  -d "FunctionVersions=$VER" -d "Runtime=node22" | jqr "['sid']")
for i in $(seq 1 30); do
  sleep 4
  ST=$(curl -sf -u "$AUTH" "$SLS/Services/$SVC/Builds/$BUILD/Status" | jqr "['status']")
  echo "build $ST"
  [ "$ST" = "completed" ] && break
  [ "$ST" = "failed" ] && { echo "BUILD FAILED"; exit 1; }
done
[ "$ST" = "completed" ] || { echo "BUILD TIMED OUT"; exit 1; }

echo "== deploy"
curl -sf -u "$AUTH" -X POST "$SLS/Services/$SVC/Environments/$ENV/Deployments" \
  -d "BuildSid=$BUILD" >/dev/null
DOMAIN=$(curl -sf -u "$AUTH" "$SLS/Services/$SVC/Environments/$ENV" | jqr "['domain_name']")
echo "live at https://$DOMAIN/incoming"

echo "== point the number"
PN=$(curl -sf -u "$AUTH" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$(python3 -c "import urllib.parse,os; print(urllib.parse.quote(os.environ['TWILIO_FROM']))")" \
  | jqr "['incoming_phone_numbers'][0]['sid']")
curl -sf -u "$AUTH" -X POST \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$PN.json" \
  -d "SmsUrl=https://$DOMAIN/incoming" -d "SmsMethod=POST" \
  | jqr "['sms_url']"
echo "THE EAR IS WIRED."
