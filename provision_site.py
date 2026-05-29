#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Tag-driven site provisioning script for Infoblox Universal DDI.

    Automates the full lifecycle of bringing up a new network site:

      1. Discover an available address block using metadata tags
         (Region, Environment, Status=available)
      2. Carve management, user-LAN and server subnets from the block
      3. Apply per-subnet tags (Site, Purpose, DHCP, etc.)
      4. Mark the parent block as allocated and update its Site tag
      5. Create a forward DNS authoritative zone for the site
      6. Provision a gateway host record (IPAM + DNS A/PTR)

    All destructive steps support --dry-run so you can preview the
    full plan before committing any changes.

 Usage:
    provision_site.py [-h] -s SITE -r REGION -e ENVIRONMENT
                      [-l LOCATION] [--subnet-size SUBNET_SIZE]
                      [--dns-parent DNS_PARENT] [--dns-view DNS_VIEW]
                      [--ip-space IP_SPACE] [--dry-run]
                      [-c CONFIG] [-d] [-v]

 Examples:
    # Dry-run: preview provisioning a London EMEA production site
    provision_site.py -s london -r EMEA -e production -l "London, UK" --dry-run

    # Execute: provision the site for real
    provision_site.py -s london -r EMEA -e production -l "London, UK"

    # Override defaults
    provision_site.py -s sydney -r APAC -e production \\
        --dns-parent corp.example.com --dns-view internal \\
        --subnet-size 24

 Requirements:
    Python 3.8+ with requests module

    pip install requests

 Configuration:
    Create an INI file (default: provision_site.ini) with the
    following structure:

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default
      owner       = network-team
      subnet_size = 24

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
__version__ = '1.0.0'
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SiteConfig:
    '''
    Holds all parameters needed to provision a single site.
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
    date: str = field(default_factory=lambda: datetime.date.today().isoformat())

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'

    @property
    def subnet_plan(self) -> list[dict]:
        '''Standard three-subnet plan carved from the address block.'''
        return [
            {'name': f'{self.site}-mgmt',   'purpose': 'mgmt',     'dhcp': 'false'},
            {'name': f'{self.site}-lan',    'purpose': 'user-lan', 'dhcp': 'true'},
            {'name': f'{self.site}-server', 'purpose': 'server',   'dhcp': 'false'},
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
    gateway_host_id: str = ''
    gateway_fqdn: str = ''
    gateway_ip: str = ''
    dry_run: bool = False


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
    1. resolve_ip_space()        - look up IP space ID from name
    2. find_available_block()    - tag-based discovery of address block
    3. resolve_dns_view()        - look up DNS view ID from name
    4. create_subnets()          - carve /N subnets from the block
    5. update_block_status()     - mark block as allocated
    6. create_dns_zone()         - create forward authoritative zone
    7. provision_gateway()       - IPAM host + DNS A/PTR for gw01
    '''

    def __init__(self, client: UDDIClient, cfg: SiteConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._space_id: str = ''
        self._view_id: str = ''

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

    def create_subnets(self, block: dict) -> list[dict]:
        '''
        Carve one /subnet_size subnet per role (mgmt, lan, server)
        from the given address block, assigning addresses sequentially
        from the start of the block.

        Each subnet receives tags:
            Site, Region, Environment, Owner, Purpose, DHCP

        Args:
            block: Address block resource dict from find_available_block()

        Returns:
            List of created subnet resource dicts (or dry-run plan dicts)
        '''
        block_addr = block['address']  # e.g. '10.20.0.0'
        base_octets = block_addr.split('.')
        created = []

        for idx, role in enumerate(self.cfg.subnet_plan):
            # Increment third octet for each subnet in the /16 block
            subnet_addr = '.'.join(base_octets[:2] + [str(idx)] + ['0'])
            cidr = f'{subnet_addr}/{self.cfg.subnet_size}'
            tags = {
                'Site':        self.cfg.site,
                'Region':      self.cfg.region,
                'Environment': self.cfg.environment,
                'Owner':       self.cfg.owner,
                'Purpose':     role['purpose'],
                'DHCP':        role['dhcp'],
            }
            logger.info(
                '%sCreating subnet %s  name=%s  purpose=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                cidr, role['name'], role['purpose'],
            )
            if self.cfg.dry_run:
                created.append({
                    'dry_run': True,
                    'address': subnet_addr,
                    'cidr': self.cfg.subnet_size,
                    'name': role['name'],
                    'tags': tags,
                })
                continue

            body = {
                'address':  subnet_addr,
                'cidr':     self.cfg.subnet_size,
                'name':     role['name'],
                'space':    self._space_id,
                'comment':  f'{self.cfg.site.capitalize()} site - {role["purpose"]} network',
                'tags':     tags,
            }
            result = self.client.post('/ipam/subnet', body)
            subnet = result.get('result', {})
            logger.info('  Created subnet id=%s', subnet.get('id'))
            created.append(subnet)

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
        Create a forward authoritative DNS zone for the site under
        the configured parent zone and DNS view.

        Zone name: site-<site>.<dns_parent>

        Returns:
            Created DNS zone resource dict (or dry-run plan dict)
        '''
        fqdn = self.cfg.dns_zone
        logger.info(
            '%sCreating DNS zone: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, self.cfg.dns_view,
        )
        if self.cfg.dry_run:
            return {'dry_run': True, 'fqdn': fqdn, 'view': self.cfg.dns_view}

        body = {
            'fqdn':         fqdn,
            'view':         self._view_id,
            'primary_type': 'cloud',
        }
        result = self.client.post('/dns/auth_zone', body)
        zone = result.get('result', {})
        logger.info('  Created zone id=%s', zone.get('id'))
        return zone

    # ------------------------------------------------------------------
    # Step 7: Provision gateway host
    # ------------------------------------------------------------------

    def provision_gateway(self, subnets: list[dict]) -> dict:
        '''
        Create an IPAM host for the site gateway (gw01) in the
        management subnet, with auto-generated DNS A and PTR records.

        The gateway is assigned the first usable address in the
        management subnet (base_address + 1).

        Args:
            subnets: List of subnet resource dicts from create_subnets()

        Returns:
            Created IPAM host resource dict (or dry-run plan dict)
        '''
        # Management subnet is always the first in the plan
        mgmt_subnet = subnets[0]
        if self.cfg.dry_run:
            mgmt_addr = mgmt_subnet.get('address', '<mgmt-subnet-base>')
        else:
            mgmt_addr = mgmt_subnet.get('address', '')

        # First usable host = base + 1 (e.g. 10.20.0.0 -> 10.20.0.1)
        octets = mgmt_addr.split('.')
        octets[-1] = str(int(octets[-1]) + 1)
        gw_ip = '.'.join(octets)

        hostname = f'gw01.{self.cfg.dns_zone}'
        logger.info(
            '%sProvisioning gateway host: %s -> %s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            hostname, gw_ip,
        )
        if self.cfg.dry_run:
            return {
                'dry_run': True,
                'fqdn': hostname,
                'ip':   gw_ip,
            }

        body = {
            'name':    hostname,
            'comment': f'{self.cfg.site.capitalize()} site gateway',
            'addresses': [{
                'address':     gw_ip,
                'space':       self._space_id,
                'enable_dhcp': False,
            }],
            'auto_generate_records': True,
            'dns_zone': self._view_id,
        }
        result = self.client.post('/ipam/host', body)
        host = result.get('result', {})
        logger.info('  Created host id=%s', host.get('id'))
        return host

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

        # Step 4: Carve subnets
        subnets = self.create_subnets(block)
        result.subnets = [
            {
                'address': f'{s.get("address")}/{s.get("cidr")}',
                'name':    s.get('name', ''),
                'id':      s.get('id', '(dry-run)'),
            }
            for s in subnets
        ]

        # Step 5: Update block status
        self.update_block_status(block)

        # Step 6: Create DNS zone
        zone = self.create_dns_zone()
        result.dns_zone_id = zone.get('id', '(dry-run)')
        result.dns_zone_fqdn = zone.get('fqdn', self.cfg.dns_zone)

        # Step 7: Provision gateway host
        gw = self.provision_gateway(subnets)
        result.gateway_host_id = gw.get('id', '(dry-run)')
        result.gateway_fqdn = gw.get('fqdn', f'gw01.{self.cfg.dns_zone}')
        result.gateway_ip = gw.get('ip', '')

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
        print(f'    {s["address"]:<20}  {s["name"]:<25}  id={s["id"]}')
    print()
    print(f'  DNS zone      : {result.dns_zone_fqdn}  id={result.dns_zone_id}')
    print(f'  Gateway host  : {result.gateway_fqdn} -> {result.gateway_ip}')
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
            logger.error('Missing required config [%s] %s in %s', section, key, config_file)
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
            'The script discovers an available address block using Region, '
            'Environment, and Status tags, then carves subnets, creates a '
            'DNS zone, and provisions a gateway host — all in one step.'
        ),
    )

    # Version
    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    # Required site parameters
    site_grp = parser.add_argument_group('site parameters')
    site_grp.add_argument(
        '-s', '--site',
        required=True,
        metavar='NAME',
        help='Short site name used in subnet names and DNS zone (e.g. london)',
    )
    site_grp.add_argument(
        '-r', '--region',
        required=True,
        metavar='REGION',
        help='Geographic region tag to match on address block (e.g. EMEA)',
    )
    site_grp.add_argument(
        '-e', '--environment',
        required=True,
        metavar='ENV',
        help='Environment tag to match on address block (e.g. production)',
    )
    site_grp.add_argument(
        '-l', '--location',
        default='',
        metavar='LOCATION',
        help='Human-readable location applied to the block after allocation (e.g. "London, UK")',
    )

    # Optional overrides
    opt_grp = parser.add_argument_group('optional overrides')
    opt_grp.add_argument(
        '--subnet-size',
        type=int,
        default=None,
        metavar='CIDR',
        help='Subnet prefix length to carve (default from config, typically 24)',
    )
    opt_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone (default from config, e.g. internal.example.com)',
    )
    opt_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (default from config)',
    )
    opt_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (default from config)',
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

    Reads configuration, builds SiteConfig, runs the provisioner,
    and prints a summary.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    # Load config file
    cfg_file = read_config(args.config)

    # Build SiteConfig — CLI args override config file defaults
    defaults = cfg_file['DEFAULTS'] if cfg_file.has_section('DEFAULTS') else {}

    site_cfg = SiteConfig(
        site=args.site.lower(),
        region=args.region,
        environment=args.environment,
        location=args.location or f'{args.site.capitalize()}',
        ip_space=args.ip_space or defaults.get('ip_space', ''),
        dns_parent=args.dns_parent or defaults.get('dns_parent', ''),
        dns_view=args.dns_view or defaults.get('dns_view', 'default'),
        owner=defaults.get('owner', 'network-team'),
        subnet_size=args.subnet_size or int(defaults.get('subnet_size', 24)),
        dry_run=args.dry_run,
    )

    if not site_cfg.ip_space:
        logger.error('ip_space must be set via --ip-space or [DEFAULTS] ip_space in config')
        sys.exit(1)
    if not site_cfg.dns_parent:
        logger.error('dns_parent must be set via --dns-parent or [DEFAULTS] dns_parent in config')
        sys.exit(1)

    logger.info('Site config: %s', site_cfg)

    if site_cfg.dry_run:
        print(f'\n[DRY-RUN] Previewing site provisioning for: {site_cfg.site}')
    else:
        print(f'\nProvisioning site: {site_cfg.site}')

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
