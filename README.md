# provision_site.py

Tag-driven site provisioning for **Infoblox Universal DDI**.

Automates the full lifecycle of bringing up a new network site from
a single command, using metadata tags on address blocks to drive
IP allocation — no hardcoded CIDRs required.

---

## What it does

Given a site name, region, and environment, the script:

| Step | Action |
|------|--------|
| 1 | Resolves the configured IP space |
| 2 | **Discovers** an available address block matching `Region`, `Environment`, `Status=available` tags |
| 3 | Resolves the DNS view |
| 4 | **Carves** three `/24` subnets (mgmt, user-lan, server) and tags each one |
| 5 | **Updates** the block — sets `Status=allocated`, `Site=<name>` |
| 6 | **Creates** a forward DNS zone: `site-<name>.<dns_parent>` |
| 7 | **Provisions** a gateway host: `gw01.site-<name>.<dns_parent>` with IPAM + DNS A/PTR |

All destructive steps support `--dry-run` so you can preview the full
plan before committing any changes.

---

## Prerequisites

```
Python 3.8+
pip install requests
```

---

## Configuration

Copy `provision_site.ini` and populate your values:

```ini
[UDDI]
api_key = <your-api-key>
url     = https://csp.infoblox.com

[DEFAULTS]
ip_space    = my-ip-space
dns_parent  = internal.example.com
dns_view    = default
owner       = network-team
subnet_size = 24
```

Keep the INI file secure — it contains your API key.

---

## Tag schema

Address blocks must be tagged before the script can discover them.
Recommended schema:

| Tag | Required | Description | Example |
|-----|----------|-------------|---------|
| `Owner` | Yes | Team or individual responsible | `network-team` |
| `Environment` | Yes | Deployment tier | `production`, `lab` |
| `Region` | Yes | Geographic region | `AMER`, `EMEA`, `APAC` |
| `Site` | Yes | Short site name | `london`, `unassigned` |
| `Status` | Yes | Lifecycle state | `available`, `allocated` |
| `Location` | No | Human-readable location | `London, UK` |
| `Provisioned` | No | ISO date of allocation | `2026-05-29` |
| `BlockSize` | No | CIDR size of block | `/16` |

Status lifecycle:

```
available  →  allocated  →  decommissioned
```

The script finds blocks where `Status == "available"` and sets
`Status = "allocated"` upon completion.

---

## Usage

```
provision_site.py [-h] -s SITE -r REGION -e ENVIRONMENT
                  [-l LOCATION] [--subnet-size N]
                  [--dns-parent ZONE] [--dns-view VIEW]
                  [--ip-space SPACE] [--dry-run]
                  [-c CONFIG] [-d] [-v] [-V]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `-s`, `--site` | Short site name (e.g. `london`) |
| `-r`, `--region` | Region tag to match on block (e.g. `EMEA`) |
| `-e`, `--environment` | Environment tag to match on block (e.g. `production`) |
| `-l`, `--location` | Human-readable location applied to the block |
| `--subnet-size` | Subnet prefix length to carve (default: `24`) |
| `--dns-parent` | Parent DNS zone (overrides config) |
| `--dns-view` | DNS view name (overrides config) |
| `--ip-space` | IP space name (overrides config) |
| `--dry-run` | Preview all steps without making changes |
| `-c`, `--config` | Path to INI config file (default: `provision_site.ini`) |
| `-d`, `--debug` | Enable DEBUG logging (shows all API calls) |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

---

## Examples

```bash
# Preview provisioning a London EMEA production site
python3 provision_site.py \
    -s london -r EMEA -e production -l "London, UK" \
    --dry-run -v

# Execute
python3 provision_site.py \
    -s london -r EMEA -e production -l "London, UK" \
    -v

# Provision a Sydney APAC site with a /22 subnet size
python3 provision_site.py \
    -s sydney -r APAC -e production -l "Sydney, AU" \
    --subnet-size 22 -v

# Use a custom config file
python3 provision_site.py \
    -s berlin -r EMEA -e staging \
    -c /etc/infoblox/provision_site.ini
```

---

## Example output

```
Provisioning site: london

============================================================
Site Provisioning Summary
============================================================
  Address block : 10.20.0.0/16

  Subnets:
    10.20.0.0/24         london-mgmt               id=ipam/subnet/...
    10.20.1.0/24         london-lan                id=ipam/subnet/...
    10.20.2.0/24         london-server             id=ipam/subnet/...

  DNS zone      : site-london.marrison.internal  id=dns/auth_zone/...
  Gateway host  : gw01.site-london.marrison.internal -> 10.20.0.1
============================================================
Provisioning complete.
```

---

## Extending the script

### Add more subnets

Edit `SiteConfig.subnet_plan` to include additional roles:

```python
@property
def subnet_plan(self) -> list[dict]:
    return [
        {'name': f'{self.site}-mgmt',   'purpose': 'mgmt',     'dhcp': 'false'},
        {'name': f'{self.site}-lan',    'purpose': 'user-lan', 'dhcp': 'true'},
        {'name': f'{self.site}-server', 'purpose': 'server',   'dhcp': 'false'},
        {'name': f'{self.site}-dmz',    'purpose': 'dmz',      'dhcp': 'false'},  # new
        {'name': f'{self.site}-iot',    'purpose': 'iot',      'dhcp': 'true'},   # new
    ]
```

### Add a reverse DNS zone

After `create_dns_zone()` in `SiteProvisioner.provision()`:

```python
reverse_zone = self.create_reverse_zone(block)
```

### Add more gateway hosts

Call `provision_gateway()` multiple times with different hostnames
and subnets (e.g. provision `dns01` in the server subnet).

---

## Author

Chris Marrison — chris@infoblox.com
