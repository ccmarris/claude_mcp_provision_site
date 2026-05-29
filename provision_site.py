#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Tag-driven site provisioning script for Infoblox Universal DDI.

    Automates the full lifecycle of bringing up a new network site:

      1. Discover an available address block using metadata tags
         (Region, Environment, Status=available)
      2. Carve subnets from the block (standard 3-subnet plan or fully
         customised via a YAML template)
      3. Apply per-subnet tags (Site, Purpose, DHCP, etc.)
      4. Mark the parent block as allocated and update its Site tag
      5. Create a forward DNS authoritative zone for the site
      6. Provision one or more host records (IPAM + DNS A/PTR)

    All destructive steps support --dry-run so you can preview the
    full plan before committing any changes.

    Site definitions can be supplied three ways (highest to lowest
    precedence):

      1. CLI flags  (-s, -r, -e, --subnet-size, ...)
      2. YAML template  (--template site.yaml)
      3. INI configuration defaults  (provision_site.ini)

 Usage:
    provision_site.py [-h] [-t TEMPLATE]
                      [-s SITE] [-r REGION] [-e ENVIRONMENT]
                      [-l LOCATION] [--subnet-size SUBNET_SIZE]
                      [--dns-parent DNS_PARENT] [--dns-view DNS_VIEW]
                      [--ip-space IP_SPACE] [--dry-run]
                      [-c CONFIG] [-d] [-v]

 Examples:
    # Dry-run using a YAML template
    provision_site.py -t templates/site-london.yaml --dry-run -v

    # Execute using a YAML template
    provision_site.py -t templates/site-london.yaml -v

    # CLI-only (no template) with verbose output
    provision_site.py -s london -r EMEA -e production -l "London, UK" -v

    # Template + CLI override (CLI wins)
    provision_site.py -t templates/site-london.yaml --dns-view internal

 Requirements:
    Python 3.8+ with requests and PyYAML modules

    pip install requests pyyaml

 Configuration:
    Create an INI file (default: provision_site.ini):

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default
      owner       = network-team
      subnet_size = 24

    YAML template schema (all keys optional — missing keys fall back
    to INI defaults or CLI flags):

      site:
        name:        london
        region:      EMEA
        environment: production
        location:    "London, UK"

      network:
        ip_space:    my-ip-space     # overrides INI default
        subnet_size: 24              # default size for subnets

        subnets:
          - name:    london-mgmt
            purpose: mgmt
            dhcp:    false
            cidr:    24              # per-subnet override of subnet_size
          - name:    london-lan
            purpose: user-lan
            dhcp:    true

      dns:
        parent:  internal.example.com
        view:    default

      hosts:
        - hostname: gw01
          subnet:   london-mgmt      # name from subnets list above
          comment:  "Site gateway"
        - hostname: dns01
          subnet:   london-server
          comment:  "Site DNS server"

      tags:
        Owner:      network-team
        CostCentre: CC-1234

 Author: Chris Marrison

 Date Last Updated: 20260529

 Copyright (c) 2026 Chris Marrison / Infoblox

 Redistribution and use in source and binary forms,
 with or without modification, are permitted provided
 that the following conditions are met:

 1. Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

 2. Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.

 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
 FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
 INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.

------------------------------------------------------------------------
'''
__version__ = '1.1.0'
__author__ = 'Chris Marrison'
__author_email__ = 'chris@infoblox.com'

import argparse
import configparser
import datetime
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

import requests
import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SubnetDef:
    '''
    Definition of a single subnet to be carved from the address block.

    Attributes:
        name:    Resource name applied in IPAM (e.g. london-mgmt)
        purpose: Tag value for the Purpose key (e.g. mgmt, user-lan)
        dhcp:    Whether DHCP is enabled on this subnet ('true'/'false')
        cidr:    Prefix length override; falls back to SiteConfig.subnet_size
    '''
    name: str
    purpose: str
    dhcp: str = 'false'
    cidr: Optional[int] = None


@dataclass
class HostDef:
    '''
    Definition of a host to be provisioned in IPAM with DNS records.

    Attributes:
        hostname: Short hostname (FQDN will be hostname.dns_zone)
        subnet:   Name of the subnet (must match a SubnetDef.name) in
                  which to allocate the first available IP
        comment:  Free-text description stored on the IPAM host object
    '''
    hostname: str
    subnet: str
    comment: str = ''


@dataclass
class SiteConfig:
    '''
    Holds all parameters needed to provision a single site.

    The subnet_plan and hosts lists drive what gets created.  When no
    YAML template is supplied the built-in three-subnet / one-gateway
    defaults are used, preserving backwards compatibility with v1.0.0.
    '''
    site: str
    region: str
    environment: str
    location: str
    ip_space: str
    dns_parent: str
    dns_view: str
    owner: str
    subnet_size: int
    dry_run: bool
    extra_tags: dict = field(default_factory=dict)
    _subnet_plan: list[SubnetDef] = field(default_factory=list)
    _hosts: list[HostDef] = field(default_factory=list)
    date: str = field(default_factory=lambda: datetime.date.today().isoformat())

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'

    @property
    def subnet_plan(self) -> list[SubnetDef]:
        '''
        Custom subnet plan from YAML template, or the built-in default
        (mgmt / user-lan / server) when no template was supplied.
        '''
        if self._subnet_plan:
            return self._subnet_plan
        return [
            SubnetDef(name=f'{self.site}-mgmt',   purpose='mgmt',     dhcp='false'),
            SubnetDef(name=f'{self.site}-lan',    purpose='user-lan', dhcp='true'),
            SubnetDef(name=f'{self.site}-server', purpose='server',   dhcp='false'),
        ]

    @property
    def hosts(self) -> list[HostDef]:
        '''
        Custom host list from YAML template, or a single gateway host
        (gw01 in the first subnet) when no template was supplied.
        '''
        if self._hosts:
            return self._hosts
        first_subnet = self.subnet_plan[0].name
        return [
            HostDef(
                hostname=f'gw01',
                subnet=first_subnet,
                comment=f'{self.site.capitalize()} site gateway',
            )
        ]


@dataclass
class ProvisionResult:
    '''
    Accumulates resource IDs created during provisioning so callers
    can inspect, log, or roll back.
    '''
    block_id: str = ''
    block_address: str = ''
    subnets: list[dict] = field(default_factory=list)
    dns_zone_id: str = ''
    dns_zone_fqdn: str = ''
    hosts: list[dict] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# YAML template loader
# ---------------------------------------------------------------------------

def load_yaml_template(path: str) -> dict:
    '''
    Load and parse a YAML site template file.

    Args:
        path: Filesystem path to the YAML template

    Returns:
        Parsed template as a plain dict

    Raises:
        SystemExit if the file cannot be opened or is not valid YAML
    '''
    logger.info('Loading YAML template: %s', path)
    try:
        with open(path, 'r') as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.error('YAML template not found: %s', path)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error('Invalid YAML in %s: %s', path, exc)
        sys.exit(1)

    if not isinstance(data, dict):
        logger.error('YAML template must be a mapping (dict) at the top level: %s', path)
        sys.exit(1)

    logger.debug('Template loaded: %s', data)
    return data


def template_to_site_config(
    template: dict,
    ini_defaults: dict,
    cli_args: argparse.Namespace,
) -> SiteConfig:
    '''
    Merge a YAML template, INI defaults, and CLI arguments into a
    SiteConfig.

    Precedence (highest → lowest):
        CLI flags  >  YAML template  >  INI defaults  >  hardcoded fallbacks

    Args:
        template:     Parsed YAML template dict (may be empty)
        ini_defaults: Dict of key/value pairs from [DEFAULTS] in the
                      INI configuration file
        cli_args:     Parsed argparse Namespace from parseargs()

    Returns:
        Fully populated SiteConfig instance

    Raises:
        SystemExit if mandatory values (site, region, environment,
        ip_space, dns_parent) cannot be resolved from any source
    '''
    site_sec  = template.get('site', {}) or {}
    net_sec   = template.get('network', {}) or {}
    dns_sec   = template.get('dns', {}) or {}
    tags_sec  = template.get('tags', {}) or {}
    hosts_sec = template.get('hosts', []) or []
    subnets_sec = net_sec.get('subnets', []) or []

    def resolve(cli_val, yaml_val, ini_key, fallback=''):
        '''Return the first non-None/non-empty value in priority order.'''
        if cli_val is not None and cli_val != '':
            return cli_val
        if yaml_val is not None and yaml_val != '':
            return yaml_val
        return ini_defaults.get(ini_key, fallback)

    # --- Mandatory fields ---
    site = resolve(
        getattr(cli_args, 'site', None),
        site_sec.get('name'),
        'site',
    )
    region = resolve(
        getattr(cli_args, 'region', None),
        site_sec.get('region'),
        'region',
    )
    environment = resolve(
        getattr(cli_args, 'environment', None),
        site_sec.get('environment'),
        'environment',
    )
    ip_space = resolve(
        getattr(cli_args, 'ip_space', None),
        net_sec.get('ip_space'),
        'ip_space',
    )
    dns_parent = resolve(
        getattr(cli_args, 'dns_parent', None),
        dns_sec.get('parent'),
        'dns_parent',
    )

    errors = []
    for label, value in [
        ('site / --site', site),
        ('region / site.region', region),
        ('environment / site.environment', environment),
        ('ip_space / network.ip_space', ip_space),
        ('dns_parent / dns.parent', dns_parent),
    ]:
        if not value:
            errors.append(label)
    if errors:
        logger.error(
            'Required values missing (supply via CLI, YAML template, or INI): %s',
            ', '.join(errors),
        )
        sys.exit(1)

    # --- Optional fields ---
    location = resolve(
        getattr(cli_args, 'location', None),
        site_sec.get('location'),
        'location',
        fallback=site.capitalize(),
    )
    dns_view = resolve(
        getattr(cli_args, 'dns_view', None),
        dns_sec.get('view'),
        'dns_view',
        fallback='default',
    )
    owner = resolve(
        None,
        tags_sec.get('Owner') or site_sec.get('owner'),
        'owner',
        fallback='network-team',
    )
    subnet_size_raw = resolve(
        getattr(cli_args, 'subnet_size', None),
        net_sec.get('subnet_size'),
        'subnet_size',
        fallback=24,
    )
    subnet_size = int(subnet_size_raw)

    # --- Subnet plan from YAML ---
    subnet_plan: list[SubnetDef] = []
    for s in subnets_sec:
        subnet_plan.append(SubnetDef(
            name=s.get('name', f'{site}-{s.get("purpose", "net")}'),
            purpose=s.get('purpose', 'general'),
            dhcp=str(s.get('dhcp', False)).lower(),
            cidr=s.get('cidr'),          # None → use subnet_size default
        ))

    # --- Host list from YAML ---
    host_list: list[HostDef] = []
    for h in hosts_sec:
        if 'hostname' not in h:
            logger.warning('Skipping host entry with no hostname: %s', h)
            continue
        # Default subnet is the first in the plan (mgmt)
        default_subnet = subnet_plan[0].name if subnet_plan else f'{site}-mgmt'
        host_list.append(HostDef(
            hostname=h['hostname'],
            subnet=h.get('subnet', default_subnet),
            comment=h.get('comment', ''),
        ))

    # Extra tags from the YAML [tags] section (Owner already extracted above)
    extra_tags = {k: str(v) for k, v in tags_sec.items()}

    return SiteConfig(
        site=site.lower(),
        region=region,
        environment=environment,
        location=location,
        ip_space=ip_space,
        dns_parent=dns_parent,
        dns_view=dns_view,
        owner=owner,
        subnet_size=subnet_size,
        dry_run=getattr(cli_args, 'dry_run', False),
        extra_tags=extra_tags,
        _subnet_plan=subnet_plan,
        _hosts=host_list,
    )


# ---------------------------------------------------------------------------
# Infoblox Universal DDI API client
# ---------------------------------------------------------------------------

class UDDIClient:
    '''
    Thin wrapper around the Infoblox Universal DDI REST API.

    Handles authentication, base URL construction, and common
    error handling so provisioning logic stays clean.
    '''

    BASE_PATH = '/api/ddi/v1'

    def __init__(self, url: str, api_key: str) -> None:
        '''
        Initialise the client.

        Args:
            url:     Base CSP URL, e.g. https://csp.infoblox.com
            api_key: BloxOne / Universal DDI API key
        '''
        self.base_url = url.rstrip('/') + self.BASE_PATH
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {api_key}',
            'Content-Type': 'application/json',
        })

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        '''
        HTTP GET with error handling.

        Args:
            path:   API path relative to BASE_PATH (e.g. '/ipam/ip_space')
            params: Optional query parameters

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('GET %s  params=%s', url, params)
        response = self.session.get(url, params=params)
        self._check(response)
        return response.json()

    def post(self, path: str, body: dict) -> dict:
        '''
        HTTP POST with error handling.

        Args:
            path: API path relative to BASE_PATH
            body: Request body as a dict (will be JSON-encoded)

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('POST %s  body=%s', url, json.dumps(body))
        response = self.session.post(url, json=body)
        self._check(response)
        return response.json()

    def patch(self, path: str, body: dict) -> dict:
        '''
        HTTP PATCH with error handling.

        Args:
            path: API path relative to BASE_PATH (must include resource ID)
            body: Fields to update

        Returns:
            Parsed JSON response body

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('PATCH %s  body=%s', url, json.dumps(body))
        response = self.session.patch(url, json=body)
        self._check(response)
        return response.json()

    def _check(self, response: requests.Response) -> None:
        '''
        Raise a clear error on non-2xx responses.

        Args:
            response: requests.Response to inspect

        Raises:
            SystemExit with status code and body on error
        '''
        if not response.ok:
            logger.error(
                'API error %s %s: %s',
                response.request.method,
                response.url,
                response.text,
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# Site provisioner
# ---------------------------------------------------------------------------

class SiteProvisioner:
    '''
    Orchestrates the full site provisioning sequence against the
    Infoblox Universal DDI API.

    Steps
    -----
    1. resolve_ip_space()     - look up IP space ID from name
    2. find_available_block() - tag-based discovery of address block
    3. resolve_dns_view()     - look up DNS view ID from name
    4. create_subnets()       - carve subnets per plan (standard or YAML)
    5. update_block_status()  - mark block as allocated
    6. create_dns_zone()      - create forward authoritative zone
    7. provision_hosts()      - IPAM host + DNS A/PTR for each host in plan
    '''

    def __init__(self, client: UDDIClient, cfg: SiteConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        self._view_id: str = ''
        self._zone_id: str = ''

    # ------------------------------------------------------------------
    # Step 1: Resolve IP space
    # ------------------------------------------------------------------

    def resolve_ip_space(self) -> str:
        '''
        Resolve IP space name to its API resource ID.

        Returns:
            IP space resource ID string

        Raises:
            SystemExit if the space is not found
        '''
        logger.info('Resolving IP space: %s', self.cfg.ip_space)
        data = self.client.get(
            '/ipam/ip_space',
            params={'_filter': f'name=="{self.cfg.ip_space}"'},
        )
        results = data.get('results', [])
        if not results:
            logger.error('IP space not found: %s', self.cfg.ip_space)
            sys.exit(1)
        space_id = results[0]['id']
        logger.debug('IP space ID: %s', space_id)
        self._space_id = space_id
        return space_id

    # ------------------------------------------------------------------
    # Step 2: Find available address block by tags
    # ------------------------------------------------------------------

    def find_available_block(self) -> dict:
        '''
        Search address blocks in the configured IP space for one whose
        tags match:
            Region      == cfg.region
            Environment == cfg.environment
            Status      == "available"

        Returns:
            Address block resource dict (id, address, cidr, tags, ...)

        Raises:
            SystemExit if no matching block is found
        '''
        logger.info(
            'Searching for available block: Region=%s Environment=%s Status=available',
            self.cfg.region, self.cfg.environment,
        )
        filter_expr = (
            f'space=="{self._space_id}" and '
            f'tags.Region=="{self.cfg.region}" and '
            f'tags.Environment=="{self.cfg.environment}" and '
            f'tags.Status=="available"'
        )
        data = self.client.get(
            '/ipam/address_block',
            params={'_filter': filter_expr},
        )
        results = data.get('results', [])
        if not results:
            logger.error(
                'No available address block found for Region=%s Environment=%s',
                self.cfg.region, self.cfg.environment,
            )
            sys.exit(1)

        block = results[0]
        logger.info(
            'Found block: %s/%s  id=%s',
            block['address'], block['cidr'], block['id'],
        )
        return block

    # ------------------------------------------------------------------
    # Step 3: Resolve DNS view
    # ------------------------------------------------------------------

    def resolve_dns_view(self) -> str:
        '''
        Resolve DNS view name to its API resource ID.

        Returns:
            DNS view resource ID string

        Raises:
            SystemExit if the view is not found
        '''
        logger.info('Resolving DNS view: %s', self.cfg.dns_view)
        data = self.client.get(
            '/dns/view',
            params={'_filter': f'name=="{self.cfg.dns_view}"'},
        )
        results = data.get('results', [])
        if not results:
            logger.error('DNS view not found: %s', self.cfg.dns_view)
            sys.exit(1)
        view_id = results[0]['id']
        logger.debug('DNS view ID: %s', view_id)
        self._view_id = view_id
        return view_id

    # ------------------------------------------------------------------
    # Step 4: Carve subnets
    # ------------------------------------------------------------------

    def create_subnets(self, block: dict) -> dict[str, dict]:
        '''
        Carve one subnet per entry in cfg.subnet_plan from the given
        address block, assigning addresses sequentially from the start
        of the block.

        Each subnet uses its own cidr if specified in the plan, otherwise
        falls back to cfg.subnet_size.

        Each subnet receives tags:
            Site, Region, Environment, Owner, Purpose, DHCP
            plus any extra_tags defined in the YAML template.

        Args:
            block: Address block resource dict from find_available_block()

        Returns:
            Dict mapping subnet name → subnet resource dict (or dry-run
            plan dict), allowing hosts to look up subnets by name.
        '''
        block_addr = block['address']  # e.g. '10.20.0.0'
        base_octets = block_addr.split('.')
        created: dict[str, dict] = {}

        for idx, sdef in enumerate(self.cfg.subnet_plan):
            cidr = sdef.cidr if sdef.cidr is not None else self.cfg.subnet_size
            # Assign sequentially from third octet within the block
            subnet_addr = '.'.join(base_octets[:2] + [str(idx)] + ['0'])
            tags = {
                'Site':        self.cfg.site,
                'Region':      self.cfg.region,
                'Environment': self.cfg.environment,
                'Owner':       self.cfg.owner,
                'Purpose':     sdef.purpose,
                'DHCP':        sdef.dhcp,
                **self.cfg.extra_tags,
            }
            logger.info(
                '%sCreating subnet %s/%s  name=%s  purpose=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                subnet_addr, cidr, sdef.name, sdef.purpose,
            )
            if self.cfg.dry_run:
                created[sdef.name] = {
                    'dry_run': True,
                    'address': subnet_addr,
                    'cidr': cidr,
                    'name': sdef.name,
                    'tags': tags,
                }
                continue

            body = {
                'address': subnet_addr,
                'cidr':    cidr,
                'name':    sdef.name,
                'space':   self._space_id,
                'comment': f'{self.cfg.site.capitalize()} site - {sdef.purpose} network',
                'tags':    tags,
            }
            result = self.client.post('/ipam/subnet', body)
            subnet = result.get('result', {})
            logger.info('  Created subnet id=%s', subnet.get('id'))
            created[sdef.name] = subnet

        return created

    # ------------------------------------------------------------------
    # Step 5: Update block status
    # ------------------------------------------------------------------

    def update_block_status(self, block: dict) -> dict:
        '''
        Update the address block tags to mark it as allocated and
        record the site name and provision date.

        Args:
            block: Original address block resource dict

        Returns:
            Updated address block resource dict (or dry-run plan dict)
        '''
        existing_tags = block.get('tags', {})
        updated_tags = {
            **existing_tags,
            **self.cfg.extra_tags,
            'Status':    'allocated',
            'Site':       self.cfg.site,
            'Location':   self.cfg.location,
            'Provisioned': self.cfg.date,
        }
        logger.info(
            '%sUpdating block %s/%s: Status=allocated, Site=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            block['address'], block['cidr'], self.cfg.site,
        )
        if self.cfg.dry_run:
            return {'dry_run': True, 'tags': updated_tags}

        result = self.client.patch(
            f'/{block["id"]}',
            body={'tags': updated_tags},
        )
        return result.get('result', {})

    # ------------------------------------------------------------------
    # Step 6: Create DNS zone
    # ------------------------------------------------------------------

    def create_dns_zone(self) -> dict:
        '''
        Ensure the forward authoritative DNS zone exists for the site.

        Checks whether the zone already exists in the configured view
        before attempting to create it, so the script is safe to re-run
        and will not fail on duplicate-zone errors.

        Zone name: site-<site>.<dns_parent>

        Returns:
            DNS zone resource dict (existing or newly created), or a
            dry-run plan dict.  Always stores the zone ID in
            self._zone_id for use by provision_hosts().

        Raises:
            SystemExit if zone creation fails for any reason other than
            the zone already existing.
        '''
        fqdn = self.cfg.dns_zone
        logger.info(
            '%sEnsuring DNS zone exists: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, self.cfg.dns_view,
        )

        if self.cfg.dry_run:
            return {'dry_run': True, 'fqdn': fqdn, 'view': self.cfg.dns_view}

        # Check whether the zone already exists in this view
        existing = self.client.get(
            '/dns/auth_zone',
            params={
                '_filter': (
                    f'fqdn=="{fqdn}." and '
                    f'view=="{self._view_id}"'
                ),
            },
        )
        results = existing.get('results', [])
        if results:
            zone = results[0]
            logger.info(
                '  Zone already exists: %s  id=%s — skipping creation',
                fqdn, zone.get('id'),
            )
            self._zone_id = zone['id']
            return zone

        # Zone does not exist — create it
        body = {
            'fqdn':         fqdn,
            'view':         self._view_id,
            'primary_type': 'cloud',
        }
        result = self.client.post('/dns/auth_zone', body)
        zone = result.get('result', {})
        self._zone_id = zone['id']
        logger.info('  Created zone id=%s', self._zone_id)
        return zone

    # ------------------------------------------------------------------
    # Step 7: Provision hosts
    # ------------------------------------------------------------------

    def provision_hosts(self, subnets: dict[str, dict]) -> list[dict]:
        '''
        Provision each host defined in cfg.hosts, allocating the first
        available IP in the named subnet and creating DNS A/PTR records.

        The host IP is derived as subnet_base + 1.  For multiple hosts
        in the same subnet the offset increments automatically.

        Args:
            subnets: Dict of subnet_name → subnet resource dict returned
                     by create_subnets()

        Returns:
            List of created IPAM host resource dicts (or dry-run plan
            dicts), one per host definition.
        '''
        # Track per-subnet offset so multiple hosts in the same subnet
        # get sequential IPs (.1, .2, ...)
        subnet_offsets: dict[str, int] = {}
        results = []

        for hdef in self.cfg.hosts:
            subnet = subnets.get(hdef.subnet)
            if subnet is None:
                logger.warning(
                    'Host %s references unknown subnet "%s" — skipping',
                    hdef.hostname, hdef.subnet,
                )
                continue

            if self.cfg.dry_run:
                base_addr = subnet.get('address', '<subnet-base>')
            else:
                base_addr = subnet.get('address', '')

            offset = subnet_offsets.get(hdef.subnet, 1)
            subnet_offsets[hdef.subnet] = offset + 1

            octets = base_addr.split('.')
            octets[-1] = str(int(octets[-1]) + offset)
            host_ip = '.'.join(octets)

            fqdn = f'{hdef.hostname}.{self.cfg.dns_zone}'
            logger.info(
                '%sProvisioning host: %s -> %s  (subnet=%s)',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                fqdn, host_ip, hdef.subnet,
            )

            if self.cfg.dry_run:
                results.append({
                    'dry_run':  True,
                    'fqdn':     fqdn,
                    'ip':       host_ip,
                    'subnet':   hdef.subnet,
                    'hostname': hdef.hostname,
                })
                continue

            body = {
                'name':    fqdn,
                'comment': hdef.comment or f'{self.cfg.site.capitalize()} - {hdef.hostname}',
                'addresses': [{
                    'address':     host_ip,
                    'space':       self._space_id,
                    'enable_dhcp': False,
                }],
                'auto_generate_records': True,
                'dns_zone': self._zone_id,   # zone ID, not view ID
            }
            result = self.client.post('/ipam/host', body)
            host = result.get('result', {})
            host['fqdn'] = fqdn
            host['ip'] = host_ip
            host['hostname'] = hdef.hostname
            logger.info('  Created host id=%s', host.get('id'))
            results.append(host)

        return results

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def provision(self) -> ProvisionResult:
        '''
        Run the full site provisioning sequence and return a result
        object containing all created resource IDs.

        Returns:
            ProvisionResult with details of everything created
        '''
        result = ProvisionResult(dry_run=self.cfg.dry_run)

        # Step 1: Resolve IP space
        self.resolve_ip_space()

        # Step 2: Find available block by tags
        block = self.find_available_block()
        result.block_id = block.get('id', '')
        result.block_address = f'{block["address"]}/{block["cidr"]}'

        # Step 3: Resolve DNS view
        self.resolve_dns_view()

        # Step 4: Carve subnets (returns dict of name -> resource)
        subnets = self.create_subnets(block)
        result.subnets = [
            {
                'address': f'{s.get("address")}/{s.get("cidr")}',
                'name':    s.get('name', ''),
                'id':      s.get('id', '(dry-run)'),
            }
            for s in subnets.values()
        ]

        # Step 5: Update block status
        self.update_block_status(block)

        # Step 6: Create DNS zone
        zone = self.create_dns_zone()
        result.dns_zone_id = zone.get('id', '(dry-run)')
        result.dns_zone_fqdn = zone.get('fqdn', self.cfg.dns_zone)

        # Step 7: Provision hosts
        hosts = self.provision_hosts(subnets)
        result.hosts = [
            {
                'fqdn':     h.get('fqdn', ''),
                'ip':       h.get('ip', ''),
                'hostname': h.get('hostname', ''),
                'id':       h.get('id', '(dry-run)'),
            }
            for h in hosts
        ]

        return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: ProvisionResult) -> None:
    '''
    Print a human-readable provisioning summary to stdout.

    Args:
        result: ProvisionResult from SiteProvisioner.provision()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Site Provisioning Summary')
    print('=' * 60)
    print(f'  Address block : {result.block_address}')
    print()
    print('  Subnets:')
    for s in result.subnets:
        print(f'    {s["address"]:<22}  {s["name"]:<28}  id={s["id"]}')
    print()
    print(f'  DNS zone      : {result.dns_zone_fqdn}  id={result.dns_zone_id}')
    print()
    print('  Hosts:')
    for h in result.hosts:
        print(f'    {h["fqdn"]:<45}  -> {h["ip"]:<16}  id={h["id"]}')
    print('=' * 60)
    if result.dry_run:
        print('DRY-RUN complete. Rerun without --dry-run to execute.')
    else:
        print('Provisioning complete.')
    print()


# ---------------------------------------------------------------------------
# Configuration and CLI
# ---------------------------------------------------------------------------

def read_config(config_file: str) -> configparser.ConfigParser:
    '''
    Read INI configuration file.

    Expected sections:

        [UDDI]
        api_key = <key>
        url     = https://csp.infoblox.com

        [DEFAULTS]
        ip_space    = my-ip-space
        dns_parent  = internal.example.com
        dns_view    = default
        owner       = network-team
        subnet_size = 24

    Args:
        config_file: Path to the INI configuration file

    Returns:
        Populated ConfigParser instance

    Raises:
        SystemExit if the file cannot be read or required keys are missing
    '''
    cfg = configparser.ConfigParser()
    if not cfg.read(config_file):
        logger.error('Configuration file not found: %s', config_file)
        sys.exit(1)

    required = [('UDDI', 'api_key'), ('UDDI', 'url')]
    for section, key in required:
        if not cfg.has_option(section, key):
            logger.error(
                'Missing required config [%s] %s in %s',
                section, key, config_file,
            )
            sys.exit(1)

    return cfg


def setup_logging(debug: bool = False, verbose: bool = False) -> None:
    '''
    Configure root logger and this module's logger.

    Args:
        debug:   Enable DEBUG level (overrides verbose)
        verbose: Enable INFO level (default is WARNING)
    '''
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


def parseargs() -> argparse.Namespace:
    '''
    Parse command-line arguments.

    Returns:
        Parsed argparse Namespace
    '''
    parser = argparse.ArgumentParser(
        description='Tag-driven site provisioning for Infoblox Universal DDI',
        epilog=(
            'Site parameters are resolved from (highest to lowest priority): '
            'CLI flags > YAML template (--template) > INI file defaults.'
        ),
    )

    # Version
    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    # YAML template
    parser.add_argument(
        '-t', '--template',
        default=None,
        metavar='FILE',
        help='Path to a YAML site definition template',
    )

    # Site parameters (all optional when a template is provided)
    site_grp = parser.add_argument_group(
        'site parameters',
        'Override or supplement values from the YAML template',
    )
    site_grp.add_argument(
        '-s', '--site',
        default=None,
        metavar='NAME',
        help='Short site name used in subnet names and DNS zone (e.g. london)',
    )
    site_grp.add_argument(
        '-r', '--region',
        default=None,
        metavar='REGION',
        help='Geographic region tag to match on address block (e.g. EMEA)',
    )
    site_grp.add_argument(
        '-e', '--environment',
        default=None,
        metavar='ENV',
        help='Environment tag to match on address block (e.g. production)',
    )
    site_grp.add_argument(
        '-l', '--location',
        default=None,
        metavar='LOCATION',
        help='Human-readable location applied to the block (e.g. "London, UK")',
    )

    # Optional overrides
    opt_grp = parser.add_argument_group('optional overrides')
    opt_grp.add_argument(
        '--subnet-size',
        type=int,
        default=None,
        metavar='CIDR',
        help='Default subnet prefix length to carve (overrides template/config)',
    )
    opt_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone (overrides template/config)',
    )
    opt_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (overrides template/config)',
    )
    opt_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (overrides template/config)',
    )

    # Execution control
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Preview all steps without making any changes',
    )
    parser.add_argument(
        '-c', '--config',
        default='provision_site.ini',
        metavar='FILE',
        help='Path to INI configuration file (default: provision_site.ini)',
    )

    # Logging
    log_grp = parser.add_mutually_exclusive_group()
    log_grp.add_argument(
        '-d', '--debug',
        action='store_true',
        default=False,
        help='Enable DEBUG logging (shows all API calls)',
    )
    log_grp.add_argument(
        '-v', '--verbose',
        action='store_true',
        default=False,
        help='Enable INFO logging',
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    '''
    Main entry point.

    Reads configuration and optional YAML template, builds SiteConfig,
    runs the provisioner, and prints a summary.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    # Load INI config
    cfg_file = read_config(args.config)
    ini_defaults = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    # Load YAML template (empty dict if none supplied)
    template: dict = {}
    if args.template:
        template = load_yaml_template(args.template)

    # Merge template + INI defaults + CLI args into SiteConfig
    site_cfg = template_to_site_config(template, ini_defaults, args)

    logger.info('Site config: %s', site_cfg)

    mode_label = '[DRY-RUN] ' if site_cfg.dry_run else ''
    print(f'\n{mode_label}Provisioning site: {site_cfg.site}')
    if args.template:
        print(f'  Template: {args.template}')

    # Initialise API client
    client = UDDIClient(
        url=cfg_file['UDDI']['url'],
        api_key=cfg_file['UDDI']['api_key'],
    )

    # Run provisioner
    provisioner = SiteProvisioner(client, site_cfg)
    result = provisioner.provision()

    # Print summary
    print_result(result)


if __name__ == '__main__':
    main()
