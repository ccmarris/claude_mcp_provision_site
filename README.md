# provision_site.py

Tag-driven site provisioning for **Infoblox Universal DDI**.

Automates the full lifecycle of bringing up a new network site from
a single command, using metadata tags on address blocks to drive IP
allocation — no hardcoded CIDRs required.

Site definitions can be provided as a **YAML template** for full
customisation, or via CLI flags for quick one-liners.

---

## What it does

Given a site name, region, and environment, the script:

| Step | Action |
|------|--------|
| 1 | Resolves the configured IP space |
| 2 | **Discovers** an available address block matching `Region`, `Environment`, `Status=available` tags |
| 3 | Resolves the DNS view |
| 4 | **Carves** subnets per the plan (YAML template or built-in mgmt/lan/server default) |
| 5 | **Updates** the block — sets `Status=allocated`, `Site=<name>` |
| 6 | **Creates** a forward DNS zone: `site-<name>.<dns_parent>` |
| 7 | **Provisions** all hosts defined in the template (IPAM + DNS A/PTR) |

All destructive steps support `--dry-run`.

---

## Prerequisites

```
Python 3.8+
pip install requests pyyaml
```

---

## Configuration

Copy `provision_site.ini.example` to `provision_site.ini` and fill in:

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

> **Security:** `provision_site.ini` is listed in `.gitignore` and will
> never be committed. Keep your API key out of source control.

---

## Parameter precedence

Values are resolved in this order (highest wins):

```
CLI flags  >  YAML template  >  INI [DEFAULTS]  >  hardcoded fallbacks
```

This means you can:
- Use a template for the subnet/host plan and override the DNS view via CLI
- Use CLI-only flags with no template at all (backwards-compatible with v1.0.0)

---

## YAML template schema

```yaml
site:
  name:        london          # required (or via -s)
  region:      EMEA            # required (or via -r)
  environment: production      # required (or via -e)
  location:    "London, UK"    # optional

network:
  ip_space:    my-ip-space     # optional — overrides INI default
  subnet_size: 24              # default prefix length for subnets

  subnets:                     # optional — defaults to mgmt/lan/server /24
    - name:    london-mgmt
      purpose: mgmt
      dhcp:    false
      cidr:    24              # per-subnet override of subnet_size
    - name:    london-lan
      purpose: user-lan
      dhcp:    true
    - name:    london-server
      purpose: server
      dhcp:    false
    - name:    london-dmz
      purpose: dmz
      dhcp:    false

dns:
  parent: internal.example.com # optional — overrides INI default
  view:   default              # optional — overrides INI default

hosts:                         # optional — defaults to single gw01
  - hostname: gw01
    subnet:   london-mgmt      # must match a name in subnets above
    comment:  "Site gateway"
  - hostname: dns01
    subnet:   london-server
    comment:  "Site DNS server"

tags:                          # extra tags applied to block + all subnets
  Owner:      network-team
  CostCentre: CC-1234
```

---

## Tag schema

Address blocks must be tagged before the script can discover them.

| Tag | Required | Description | Example values |
|-----|----------|-------------|----------------|
| `Owner` | Yes | Responsible team or individual | `network-team` |
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

---

## Usage

```
provision_site.py [-h] [-t FILE]
                  [-s NAME] [-r REGION] [-e ENV] [-l LOCATION]
                  [--subnet-size N] [--dns-parent ZONE]
                  [--dns-view VIEW] [--ip-space SPACE]
                  [--dry-run] [-c FILE] [-d | -v] [-V]
```

| Argument | Description |
|----------|-------------|
| `-t`, `--template` | Path to YAML site definition template |
| `-s`, `--site` | Short site name (e.g. `london`) |
| `-r`, `--region` | Region tag to match on block (e.g. `EMEA`) |
| `-e`, `--environment` | Environment tag to match on block (e.g. `production`) |
| `-l`, `--location` | Human-readable location applied to the block |
| `--subnet-size` | Default subnet prefix length (overrides template/config) |
| `--dns-parent` | Parent DNS zone (overrides template/config) |
| `--dns-view` | DNS view name (overrides template/config) |
| `--ip-space` | IP space name (overrides template/config) |
| `--dry-run` | Preview all steps without making changes |
| `-c`, `--config` | INI config file path (default: `provision_site.ini`) |
| `-d`, `--debug` | Enable DEBUG logging (shows all API calls) |
| `-v`, `--verbose` | Enable INFO logging |
| `-V`, `--version` | Show version and exit |

---

## Examples

```bash
# Dry-run with full YAML template
python3 provision_site.py -t templates/site-london.yaml --dry-run -v

# Execute with YAML template
python3 provision_site.py -t templates/site-london.yaml -v

# Minimal template (site/region/environment only) — rest from INI
python3 provision_site.py -t templates/site-minimal.yaml -v

# Template + CLI override (CLI view wins over template)
python3 provision_site.py -t templates/site-london.yaml --dns-view internal

# CLI-only, no template (backwards compatible with v1.0.0)
python3 provision_site.py -s london -r EMEA -e production -l "London, UK" -v

# Custom subnet size
python3 provision_site.py -t templates/site-london.yaml --subnet-size 22

# Different config file
python3 provision_site.py -t templates/site-london.yaml -c /etc/infoblox/provision_site.ini
```

---

## Example output

```
Provisioning site: london
  Template: templates/site-london.yaml

============================================================
Site Provisioning Summary
============================================================
  Address block : 10.20.0.0/16

  Subnets:
    10.20.0.0/24         london-mgmt              id=ipam/subnet/...
    10.20.1.0/24         london-lan               id=ipam/subnet/...
    10.20.2.0/24         london-server            id=ipam/subnet/...
    10.20.3.0/24         london-dmz               id=ipam/subnet/...

  DNS zone      : site-london.marrison.internal  id=dns/auth_zone/...

  Hosts:
    gw01.site-london.marrison.internal            -> 10.20.0.1    id=ipam/host/...
    dns01.site-london.marrison.internal           -> 10.20.2.1    id=ipam/host/...
    mon01.site-london.marrison.internal           -> 10.20.0.2    id=ipam/host/...
============================================================
Provisioning complete.
```

---

## Extending the script

### Add more subnets

Add entries to the `network.subnets` list in your YAML template — no
code changes required.

### Add more hosts

Add entries to the `hosts` list in your YAML template. Multiple hosts
in the same subnet are assigned sequential IPs (`.1`, `.2`, ...).

### Add a reverse DNS zone

Override `SiteProvisioner.provision()` or add a `create_reverse_zone()`
method alongside `create_dns_zone()`.

### Pipeline / batch provisioning

Loop over a directory of YAML templates:

```bash
for tmpl in templates/sites/*.yaml; do
    python3 provision_site.py -t "$tmpl" -v
done
```

---

## File layout

```
claude_mcp_provision_site/
├── provision_site.py          # Main script
├── provision_site.ini.example # Configuration template
├── README.md                  # This file
└── templates/
    ├── site-london.yaml       # Full featured example
    └── site-minimal.yaml      # Minimal example (uses INI defaults)
```

---

## Changelog

| Version | Changes |
|---------|---------|
| 1.1.0 | YAML template support; multiple hosts; per-subnet CIDR; extra tags |
| 1.0.0 | Initial release — CLI-only, three-subnet plan, single gateway host |

---

## Author

Chris Marrison — chris@infoblox.com
