# Infra: Actual on EC2 over Nebula

Actual runs on one EC2 host. It is reachable only over a self-hosted Nebula mesh, at **https://actual.h.boyer.consulting**.

## Topology

```
phone / laptop / Windows ──Nebula UDP 4242──► s1.h.boyer.consulting (EC2, lighthouse 10.42.0.1)
                                                  │
                         https://actual.h.boyer.consulting → 10.42.0.1:443 (Caddy, Let's Encrypt)
                                                  │
                                            127.0.0.1:5006 (actual-server)
```

| Host | Nebula IP | Group | Cert/key location |
|---|---|---|---|
| EC2 lighthouse | 10.42.0.1 | `lighthouse` | `/etc/nebula/` on the instance; the key never left it |
| Laptop (WSL, arm64) | 10.42.0.2 | `admin` | `~/.config/nebula/laptop.*` → `/etc/nebula/` |
| Casey's phone | 10.42.0.3 | `admin` | Mobile Nebula (key on the phone) |
| Amanda's phone | 10.42.0.4 | `admin` | Mobile Nebula (key on the phone) |
| Windows host (arm64) | 10.42.0.5 | `admin` | `C:\nebula\` |

- **CA:** "boyer-mesh", v2 certs, expires 2031-11. `ca.key` exists only in `~/.config/nebula/` and the backup.
- **Lighthouse firewall:** inbound ICMP from any host; TCP 443 from `admin` only. Clients accept ICMP only.

## AWS resources

| Account | Resource | Purpose |
|---|---|---|
| ch-dev (us-east-2) | EC2 `i-04b5b1d767b6a9bd5` (`dev-casey`, t4g.small, AL2023) | Nebula, Docker, Caddy, Actual |
| ch-dev | Elastic IP `3.17.107.203` | Stable lighthouse address |
| ch-dev | Security group: UDP 4242 open | Nebula only; nothing else is exposed |
| ch-dev | Role `dev-casey`, inline policy `assume-acme-dns` | Lets Caddy assume the kc-man DNS role |
| kc-man | Route 53 zone `boyer.consulting` (`Z06264492NMTRZQZZLN7V`) | `s1.h` A → EIP; `actual.h` A → 10.42.0.1 |
| kc-man | Role `acme-dns-h-boyer` ([deploy/iam/](../deploy/iam/)) | TXT `_acme-challenge.h.boyer.consulting` only |

Admin access to the instance is SSM only (no SSH): `aws ssm start-session --profile ch-dev --region us-east-2 --target i-04b5b1d767b6a9bd5`.

## On the instance

| Path | Source in repo |
|---|---|
| `/etc/nebula/config.yml` | [deploy/nebula/lighthouse.yml](../deploy/nebula/lighthouse.yml) |
| `/etc/systemd/system/nebula.service` | [deploy/nebula/nebula.service](../deploy/nebula/nebula.service) |
| `/opt/actual/docker-compose.yml` | [deploy/actual-server.compose.yml](../deploy/actual-server.compose.yml) |
| `/opt/actual/caddy/*` | [deploy/caddy/](../deploy/caddy/) |
| `/opt/actual/data` | Actual's data (back it up with `budget export`, below) |
| `/opt/actual/ntfy` | ntfy message cache (72h; not backed up) |
| `/etc/sysctl.d/90-nebula-bind.conf` | `net.ipv4.ip_nonlocal_bind=1` |

- **Deploy config changes:** `uv run python scripts/deploy.py`. It writes all the files above over SSM, restarts Nebula, rebuilds the Caddy image and runs `docker compose up -d`.
- **Docker on AL2023 has no buildx.** Caddy is built with `docker build` and tagged `local/caddy-route53:2.11.7`; compose only references the tag.
- **The certificate** is a wildcard `*.h.boyer.consulting` obtained via DNS-01. Caddy renews it on its own. The Route 53 sync wait and the 30 s propagation delay in the Caddyfile are required; without them, Let's Encrypt queries before the TXT record exists.
- **Upgrading Actual:** bump the image tag in the compose file and bump `actualpy` in `pyproject.toml` to a version that supports it, then deploy.

## Apps

| URL | Upstream | Notes |
|---|---|---|
| https://actual.h.boyer.consulting | `127.0.0.1:5006` | Actual Budget |
| https://ntfy.h.boyer.consulting | `127.0.0.1:2586` | ntfy; no auth (mesh-only). Topic `budget` gets `make nightly` results |

To add one: a service in the compose file bound to `127.0.0.1`, a `@host` block in the Caddyfile, and a kc-man Route 53 A record `<name>.h` → `10.42.0.1`. Then deploy.

**Nightly sync:** Windows Task Scheduler runs `make nightly` in WSL. It appends to `.data/sync.log` and posts the result to ntfy (high priority on failure).

## Adding a device

1. On the device, generate a key pair. In Mobile Nebula, use the site's certificate screen. On a desktop, run `nebula-cert keygen`. Send only the `.pub` file.
2. Sign it on the laptop, using the next free IP:
   ```
   nebula-cert sign -version 2 -ca-crt ~/.config/nebula/ca.crt -ca-key ~/.config/nebula/ca.key \
     -name <name> -networks 10.42.0.N/24 -groups admin -in-pub <name>.pub -out-crt ~/.config/nebula/<name>.crt
   nebula-cert print -path ~/.config/nebula/<name>.crt -out-qr ~/.config/nebula/<name>-qr.png
   ```
3. Configure the device:
   - **Cert:** the `.crt` file, or scan its QR.
   - **CA:** `ca-qr.png`.
   - **Lighthouse host:** `10.42.0.1` at `s1.h.boyer.consulting:4242`.
   - Desktops: use [laptop.yml](../deploy/nebula/laptop.yml) or [windows.yml](../deploy/nebula/windows.yml) as the template.
4. Mobile Nebula: **turn the site off and on** after importing; it doesn't reload a cert while it's running.
5. Add the device to the table above.

## Backups

Run `scripts/backup.sh [out-dir]`. It asks for the Actual password (unless `ACTUAL_PASSWORD` is set) and then for a passphrase. It writes `budget-backup-YYYYMMDD.zip.gpg` (AES-256) to `out-dir`, or to `~` if none is given. Upload that file to Google Drive.

| In the backup | Why |
|---|---|
| `actual-export.zip` | The budget, from `budget export`; restore it with Actual → Import → Actual |
| `nebula/` (includes `ca.key`) | Without the CA key, every device must be re-enrolled under a new CA |
| `repo/.env`, `repo/.data/simplefin.url` | Secrets; the SimpleFIN access URL can't be re-fetched without a new setup token |
| `repo/statements/`, `repo/checks/`, `repo/Debt view.xlsx` | Source documents, which are gitignored |

Not backed up, because they can be rebuilt: Caddy certs (re-issued), the lighthouse key (generate a new one and re-sign), and `reports/` (`budget report`).

To restore, decrypt with `gpg -d budget-backup-YYYYMMDD.zip.gpg > b.zip && unzip b.zip`, then copy the files back to the paths above.

**Store the passphrase somewhere other than Google Drive**, for example a password manager.

## Recovery

| Lost | Do |
|---|---|
| EC2 instance | Launch a new arm64 AL2023 instance with the `dev-casey` role and SSM. Re-associate the EIP. Open UDP 4242. Install Docker, compose and Nebula 1.12.0. Generate a lighthouse key on the instance and sign its `.pub` as 10.42.0.1/`lighthouse`. Run `scripts/deploy.py`. Import the Actual export in the UI. |
| Laptop | Restore `~/.config/nebula` and the repo secrets from the backup. |
| Phone | Generate a new key and re-sign it at the same IP. |
| CA key | Make a new CA, re-sign every host, and replace `ca.crt` everywhere. |
