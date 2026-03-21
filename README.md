# Self-hosted Git (Forgejo) with State-Level Geo-Blocking

A single-VPS Docker Compose stack providing:

- **Forgejo** — lightweight, Gitea-compatible Git hosting
- **nginx** — reverse proxy with TLS termination and GeoIP2 blocking
- **MaxMind GeoLite2** — IP → country + state/province database (auto-updated)
- **geoblock_watcher** — watches `geo_rules.yml` and hot-reloads nginx when rules change
- **Certbot** — automatic Let's Encrypt certificate renewal

---

## Directory Layout

```
.
├── docker-compose.yml
├── .env.example                 ← copy to .env and fill in
├── geo_rules.yml                ← ✏️  edit this to configure geo-blocking
├── bootstrap_certs.sh           ← run once before first `docker compose up`
├── nginx/
│   ├── Dockerfile               ← builds nginx + GeoIP2 dynamic module
│   ├── nginx.conf               ← main nginx config (loads GeoIP2 module)
│   ├── conf.d/
│   │   └── git.conf             ← virtual host (HTTP→HTTPS redirect + proxy)
│   └── geoblock/                ← rendered by geoblock_watcher at runtime
│       ├── repo_maps.conf
│       ├── repo_vars.conf
│       └── repo_locations.conf
└── geoblock_watcher/
    ├── Dockerfile
    └── watcher.py
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine ≥ 26 + Compose v2 | `docker compose version` |
| A public domain name | DNS A record → your VPS IP |
| Ports 80 and 443 open | Firewall / security group |
| MaxMind account | Free — [sign up here](https://www.maxmind.com/en/geolite2/signup) |
| `openssl` on the host | Used by `bootstrap_certs.sh` for the dummy cert |

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
$EDITOR .env          # fill in DOMAIN, MAXMIND_*, LETSENCRYPT_EMAIL
```

`.env` variables:

| Variable | Description |
|---|---|
| `DOMAIN` | Your public domain, e.g. `git.example.com` |
| `LETSENCRYPT_EMAIL` | Email for Let's Encrypt expiry notices |
| `MAXMIND_ACCOUNT_ID` | From your MaxMind account portal |
| `MAXMIND_LICENSE_KEY` | From your MaxMind account portal |
| `DISABLE_REGISTRATION` | Set `true` after creating your admin account |

### 2. Bootstrap TLS certificates (first run only)

```bash
chmod +x bootstrap_certs.sh
./bootstrap_certs.sh
```

This will:
1. Create a temporary self-signed cert so nginx can start
2. Bring up the stack
3. Obtain a real Let's Encrypt cert via the ACME webroot challenge
4. Reload nginx with the real cert
5. Print next steps

### 3. Complete Forgejo setup

Visit `https://your-domain/` and complete the web installer.  Create your
admin account.  Then set `DISABLE_REGISTRATION=true` in `.env` and run:

```bash
docker compose up -d forgejo
```

### 4. Configure geo-blocking

Edit `geo_rules.yml` — the watcher will detect the change within seconds and
hot-reload nginx automatically. No restart needed.

---

## Geo-Blocking Configuration

`geo_rules.yml` is the single source of truth. Example:

```yaml
repos:

  - path: /alice/secret-project
    rules:
      # Block California and Texas with HTTP 451
      - locales: ["US-CA", "US-TX"]
        status: 451
        body: "This repository is unavailable in your jurisdiction."

      # Block all of Germany and France with HTTP 403
      - locales: ["DE", "FR"]
        status: 403
        body: "Access to this repository is restricted in your country."

  - path: /alice/another-repo
    rules:
      - locales: ["CN", "RU"]
        status: 403
        body: "Access denied."
```

### Locale format

| Format | Example | Matches |
|---|---|---|
| Country (ISO 3166-1 α-2) | `"US"` | All IPs in the United States |
| Country + State (ISO 3166-2) | `"US-CA"` | IPs in California |

State-level rules take precedence over country-level rules for the same repo.

**Common US state codes:** `US-AL` `US-AK` `US-AZ` `US-AR` `US-CA` `US-CO`
`US-CT` `US-DE` `US-FL` `US-GA` `US-HI` `US-ID` `US-IL` `US-IN` `US-IA`
`US-KS` `US-KY` `US-LA` `US-ME` `US-MD` `US-MA` `US-MI` `US-MN` `US-MS`
`US-MO` `US-MT` `US-NE` `US-NV` `US-NH` `US-NJ` `US-NM` `US-NY` `US-NC`
`US-ND` `US-OH` `US-OK` `US-OR` `US-PA` `US-RI` `US-SC` `US-SD` `US-TN`
`US-TX` `US-UT` `US-VT` `US-VA` `US-WA` `US-WV` `US-WI` `US-WY`

For other countries, find subdivision codes at:
https://www.iso.org/obp/ui/#search (search for the country, then see "Subdivision")

### HTTP status codes

| Code | Meaning | When to use |
|---|---|---|
| `403` | Forbidden | General access restriction |
| `451` | Unavailable For Legal Reasons | Legal / jurisdictional block (RFC 7725) |

### Hot reload

The watcher polls every 60 seconds and also reacts to inotify events
immediately.  After saving `geo_rules.yml`, nginx will reload within seconds.
No traffic is dropped — nginx does a graceful configuration reload (SIGHUP).

---

## GeoIP Database Updates

The `geoipupdate` container fetches a fresh **GeoLite2-City** database every
72 hours (MaxMind publishes updates twice a week).  The database is stored in
the `geoip_db` Docker volume and mounted read-only into nginx.

nginx reads the database file at request time (not cached in memory), so a
fresh database takes effect for the next request after the file is replaced —
no nginx reload required.

---

## Certificate Renewal

The `certbot` container runs `certbot renew` every 12 hours.  When a
certificate is renewed, run:

```bash
docker compose exec nginx nginx -s reload
```

Or add this as a cron job on the host:

```cron
0 */12 * * * docker compose -f /path/to/docker-compose.yml exec nginx nginx -s reload
```

---

## Operations

### View logs

```bash
docker compose logs -f nginx          # access + error logs
docker compose logs -f geoblock_watcher
docker compose logs -f forgejo
docker compose logs -f geoipupdate
```

### Test geo-blocking (from a blocked region)

Use a proxy or VPN to simulate a request from a blocked locale, or test
directly with curl overriding your IP (only works if you control nginx):

```bash
# Verify nginx config is valid after a rules change
docker compose exec nginx nginx -t

# Force a manual nginx reload
docker compose exec nginx nginx -s reload
```

### Verify the GeoIP database is loaded

```bash
docker compose exec nginx nginx -T | grep geoip2
```

### Check which database version is in use

```bash
docker compose exec geoipupdate cat /usr/share/GeoIP/GeoLite2-City_*/COPYRIGHT_AND_LICENSE
```

---

## Security Notes

- **SSH is disabled** in Forgejo; all Git operations use HTTPS.
- **Registration is disabled** by default after initial setup — only the admin
  can create accounts.
- nginx **does not forward** `X-Forwarded-For` from downstream; it sets it
  from `$remote_addr` (the actual connected IP). This is intentional — we
  explicitly trust the direct connection IP as stated in the requirements.
- The `docker.sock` mount on `geoblock_watcher` is the minimum necessary
  to send SIGHUP to the nginx container.  If this is a concern, you can
  replace it with a small privileged sidecar that only accepts a reload signal.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| nginx won't start | `docker compose logs nginx` — likely a config syntax error |
| GeoIP variables always empty | Is the `geoip_db` volume populated? Check `docker compose logs geoipupdate` |
| Rules not applied | Check `docker compose logs geoblock_watcher` — look for YAML parse errors |
| Certificate errors | Ensure port 80 is open and DNS resolves before running `bootstrap_certs.sh` |
| 502 Bad Gateway | Forgejo not healthy yet — check `docker compose logs forgejo` |
