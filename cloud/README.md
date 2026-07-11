# i5-cloud — Worker + D1 for roast-color uploads

A Cloudflare Worker with a D1 database that receives readings from the
i5 GUI (`../gui/`), so any bench can push its roast color
profiles to one central server. Uploads are deduped server-side (unique on
device + label + mode + spectrum), so re-uploading a log is always safe.

Access is by **per-user API keys** ("license keys"): you verify a person, issue
them a key, and they can then read the full database and push readings. Every
reading is tagged with its uploader, keys can be read-only, carry a daily
upload quota, and can be revoked (and their rows purged) at any time. The
**admin key** is the `API_TOKEN` Worker secret — it manages user keys and is
also a normal working key for your own benches.

## Deploy (one time, ~5 minutes)

```bash
cd cloud
npm install wrangler --save-dev          # or use npx wrangler directly

npx wrangler d1 create i5-readings       # prints a database_id
#   → paste that id into wrangler.jsonc (database_id)

npx wrangler d1 execute i5-readings --remote --file schema.sql

npx wrangler secret put API_TOKEN        # invent a long random token; this is
                                         # what the GUI's "API token" field wants
npx wrangler deploy                      # prints https://i5-cloud.<acct>.workers.dev
```

The Worker refuses all requests until `API_TOKEN` is set (it never runs open).

## Use from the GUI

In the **Cloud sync** card: paste the `workers.dev` URL and a key (your admin
token or an issued user key), hit **Test connection**, then **Upload readings**.
The GUI proxies the upload through Flask (no CORS involved), sending the full
spectra.

## Issuing license keys (admin)

```bash
URL=https://i5-cloud.<acct>.workers.dev
ADMIN="Authorization: Bearer <your API_TOKEN>"

# verify a person, then mint their key (returned ONCE — only a hash is stored):
curl -s -X POST $URL/api/keys -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"owner":"acme-roastery","note":"verified 2026-07","daily_quota":5000}'
#   → {"ok":true,"key":"i5k_…","owner":"acme-roastery"}   ← send them this

# read-only key (can query, can't push):
curl -s -X POST $URL/api/keys -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"owner":"research-lab","can_write":false}'

curl -s $URL/api/keys -H "$ADMIN"                          # list keys + row counts
curl -s -X POST $URL/api/keys/revoke -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"owner":"acme-roastery"}'                           # kill a key
curl -s -X POST $URL/api/keys/purge -H "$ADMIN" -H 'Content-Type: application/json' \
  -d '{"owner":"acme-roastery"}'                           # delete their readings
```

Re-issuing a key for an existing owner rotates it (old key stops working,
their readings stay attributed to them).

## API (all routes need `Authorization: Bearer <key>`)

| Route | Method | Who | Purpose |
|-------|--------|-----|---------|
| `/api/health` | GET | any key | liveness + total row count |
| `/api/readings` | POST | write keys | `{device, readings:[…]}` → `{inserted, skipped, total}` (max 500/upload, daily quota) |
| `/api/readings?limit=&label=&device=&mode=&uploaded_by=` | GET | any key | query readings, spectra included |
| `/api/keys` · `/api/keys/revoke` · `/api/keys/purge` | POST/GET | admin only | key management (above) |

Example query:

```bash
curl -H "Authorization: Bearer $KEY" \
  "https://i5-cloud.<acct>.workers.dev/api/readings?label=roast-A&limit=10"
```

## Local smoke test (no Cloudflare account needed)

```bash
npx wrangler dev --local    # runs on http://localhost:8787 with a local D1
# set a dev token in .dev.vars:  echo 'API_TOKEN=devtoken' > .dev.vars
```

Then point the GUI's Cloud sync card at `http://localhost:8787` with token `devtoken`.
