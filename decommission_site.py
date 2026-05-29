#!/usr/local/bin/python3
# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
'''
------------------------------------------------------------------------

 Description:

    Tag-driven site decommissioning script for Infoblox Universal DDI.

    Automates the full teardown of a provisioned site, using the Site
    and Status tags on address blocks to drive discovery — the exact
    reverse of provision_site.py.

    Decommission sequence:

      1. Discover the allocated address block for the site (tags:
         Site=<name>, Status=allocated)
      2. Enumerate all IPAM hosts allocated within the site subnets
         and delete them (removes IPAM record + DNS A/PTR)
      3. Delete the forward DNS authoritative zone for the site
         (site-<name>.<dns_parent>)
      4. Delete all subnets carved from the block
      5. Reset the address block tags — Status → <final_status>,
         Site → unassigned, clears Provisioned/Location

    All destructive steps support --dry-run so you can preview the
    full plan before committing any changes.

    --force skips the interactive confirmation prompt.

 Usage:
    decommission_site.py [-h] -s SITE
                         [--final-status {available,decommissioned}]
                         [--keep-zone] [--dns-parent ZONE]
                         [--dns-view VIEW] [--ip-space SPACE]
                         [--dry-run] [--force]
                         [-c CONFIG] [-d | -v] [-V]

 Examples:
    # Dry-run — preview everything that would be removed
    decommission_site.py -s london --dry-run -v

    # Full decommission with confirmation prompt
    decommission_site.py -s london -v

    # Skip confirmation (for pipelines / batch runs)
    decommission_site.py -s london --force -v

    # Reset block to available instead of decommissioned
    decommission_site.py -s london --final-status available -v

    # Keep the DNS zone (hosts only removed from IPAM, not DNS)
    decommission_site.py -s london --keep-zone -v

 Configuration:
    Shares the same INI file as provision_site.py (default:
    provision_site.ini):

      [UDDI]
      api_key  = <your BloxOne/Universal DDI API key>
      url      = https://csp.infoblox.com

      [DEFAULTS]
      ip_space    = my-ip-space
      dns_parent  = internal.example.com
      dns_view    = default

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
class DecommissionConfig:
    '''
    Holds all parameters needed to decommission a single site.

    Attributes:
        site:         Short site name (matches Site tag on address block)
        ip_space:     IP space to search for the allocated block
        dns_parent:   Parent DNS zone (used to derive site zone FQDN)
        dns_view:     DNS view containing the site zone
        final_status: Tag value to apply to the block after teardown
        keep_zone:    When True, leave the DNS zone intact
        dry_run:      When True, print plan but make no API changes
        force:        When True, skip interactive confirmation prompt
    '''
    site: str
    ip_space: str
    dns_parent: str
    dns_view: str
    final_status: str = 'decommissioned'
    keep_zone: bool = False
    dry_run: bool = False
    force: bool = False
    date: str = field(default_factory=lambda: datetime.date.today().isoformat())

    @property
    def dns_zone(self) -> str:
        '''Fully-qualified DNS zone name for the site.'''
        return f'site-{self.site}.{self.dns_parent}'


@dataclass
class DecommissionResult:
    '''
    Accumulates counts and IDs of resources removed during teardown.
    '''
    block_id: str = ''
    block_address: str = ''
    hosts_deleted: list[dict] = field(default_factory=list)
    dns_zone_deleted: bool = False
    dns_zone_fqdn: str = ''
    subnets_deleted: list[dict] = field(default_factory=list)
    block_updated: bool = False
    final_status: str = ''
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Infoblox Universal DDI API client
# ---------------------------------------------------------------------------

class UDDIClient:
    '''
    Thin wrapper around the Infoblox Universal DDI REST API.

    Handles authentication, base URL construction, and common error
    handling so decommission logic stays clean.
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
            path:   API path relative to BASE_PATH
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

    def delete(self, path: str) -> None:
        '''
        HTTP DELETE with error handling.

        Args:
            path: API path relative to BASE_PATH (must include resource ID)

        Raises:
            SystemExit on HTTP error
        '''
        url = self.base_url + path
        logger.debug('DELETE %s', url)
        response = self.session.delete(url)
        self._check(response)

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
# Site decommissioner
# ---------------------------------------------------------------------------

class SiteDecommissioner:
    '''
    Orchestrates the full site teardown sequence against the
    Infoblox Universal DDI API.

    Steps
    -----
    1. resolve_ip_space()     - look up IP space ID from name
    2. find_allocated_block() - tag-based discovery of the site's block
    3. resolve_dns_view()     - look up DNS view ID from name
    4. find_subnets()         - list all subnets inside the block
    5. delete_hosts()         - remove all IPAM hosts in site subnets
    6. delete_dns_zone()      - delete forward authoritative zone
    7. delete_subnets()       - remove all carved subnets
    8. reset_block_status()   - set block Status=<final_status>, Site=unassigned
    '''

    def __init__(self, client: UDDIClient, cfg: DecommissionConfig) -> None:
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
        self._space_id = results[0]['id']
        logger.debug('IP space ID: %s', self._space_id)
        return self._space_id

    # ------------------------------------------------------------------
    # Step 2: Find the allocated block for this site
    # ------------------------------------------------------------------

    def find_allocated_block(self) -> dict:
        '''
        Search address blocks in the configured IP space for one whose
        tags match:
            Site   == cfg.site
            Status == "allocated"

        Returns:
            Address block resource dict (id, address, cidr, tags, ...)

        Raises:
            SystemExit if no matching block is found
        '''
        logger.info(
            'Searching for allocated block: Site=%s Status=allocated',
            self.cfg.site,
        )
        filter_expr = (
            f'space=="{self._space_id}" and '
            f'tags.Site=="{self.cfg.site}" and '
            f'tags.Status=="allocated"'
        )
        data = self.client.get(
            '/ipam/address_block',
            params={'_filter': filter_expr},
        )
        results = data.get('results', [])
        if not results:
            logger.error(
                'No allocated address block found for site "%s".  '
                'Verify the Site and Status tags on the block.',
                self.cfg.site,
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
        self._view_id = results[0]['id']
        logger.debug('DNS view ID: %s', self._view_id)
        return self._view_id

    # ------------------------------------------------------------------
    # Step 4: Find subnets inside the block
    # ------------------------------------------------------------------

    def find_subnets(self, block: dict) -> list[dict]:
        '''
        List all subnets that belong to the given address block.

        Uses the parent address_block_id field to filter precisely,
        so only subnets directly inside this block are returned.

        Args:
            block: Address block resource dict from find_allocated_block()

        Returns:
            List of subnet resource dicts (may be empty)
        '''
        block_id = block['id']
        logger.info('Listing subnets in block %s/%s', block['address'], block['cidr'])
        data = self.client.get(
            '/ipam/subnet',
            params={'_filter': f'parent=="{block_id}"'},
        )
        subnets = data.get('results', [])
        logger.info('  Found %d subnet(s)', len(subnets))
        for s in subnets:
            logger.debug('  Subnet: %s/%s  name=%s  id=%s',
                         s['address'], s['cidr'], s.get('name', ''), s['id'])
        return subnets

    # ------------------------------------------------------------------
    # Step 5: Delete IPAM hosts in site subnets
    # ------------------------------------------------------------------

    def delete_hosts(self, subnets: list[dict]) -> list[dict]:
        '''
        Find and delete all IPAM host records whose address falls within
        any of the given subnets.

        Hosts are looked up by their address assignments using the
        subnet's resource ID, then deleted individually.  Deleting an
        IPAM host automatically removes its associated DNS A and PTR
        records when auto_generate_records was enabled.

        Args:
            subnets: List of subnet resource dicts from find_subnets()

        Returns:
            List of dicts describing each deleted (or dry-run) host
        '''
        removed: list[dict] = []

        for subnet in subnets:
            subnet_id = subnet['id']
            subnet_cidr = f'{subnet["address"]}/{subnet["cidr"]}'
            logger.info('  Searching for hosts in subnet %s', subnet_cidr)

            data = self.client.get(
                '/ipam/host',
                params={'_filter': f'addresses.address>="{subnet["address"]}"'},
            )
            all_hosts = data.get('results', [])

            # Filter to hosts that have at least one address in this subnet
            site_hosts = [
                h for h in all_hosts
                if any(
                    addr.get('space') == self._space_id and
                    _in_subnet(addr.get('address', ''), subnet)
                    for addr in h.get('addresses', [])
                )
            ]

            for host in site_hosts:
                fqdn = host.get('name', host.get('id', 'unknown'))
                host_id = host['id']
                logger.info(
                    '%sDeleting host: %s  id=%s',
                    '[DRY-RUN] ' if self.cfg.dry_run else '',
                    fqdn, host_id,
                )
                if not self.cfg.dry_run:
                    self.client.delete(f'/{host_id}')
                removed.append({
                    'fqdn':   fqdn,
                    'id':     host_id,
                    'subnet': subnet_cidr,
                })

        if not removed:
            logger.info('  No IPAM hosts found in site subnets')

        return removed

    # ------------------------------------------------------------------
    # Step 6: Delete DNS zone
    # ------------------------------------------------------------------

    def delete_dns_zone(self) -> bool:
        '''
        Delete the forward authoritative DNS zone for the site, if it
        exists.  All records inside the zone are removed with it.

        When cfg.keep_zone is True this step is skipped and False is
        returned so the caller can reflect that in the summary.

        Returns:
            True if the zone was deleted (or would be in dry-run),
            False if it was not found or keep_zone is set.
        '''
        fqdn = self.cfg.dns_zone

        if self.cfg.keep_zone:
            logger.info('--keep-zone set — skipping deletion of zone: %s', fqdn)
            return False

        logger.info(
            '%sLooking up DNS zone: %s  view=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, self.cfg.dns_view,
        )

        data = self.client.get(
            '/dns/auth_zone',
            params={
                '_filter': (
                    f'fqdn=="{fqdn}." and '
                    f'view=="{self._view_id}"'
                ),
            },
        )
        results = data.get('results', [])
        if not results:
            logger.info('  DNS zone not found — nothing to delete: %s', fqdn)
            return False

        zone = results[0]
        zone_id = zone['id']
        logger.info(
            '%sDeleting DNS zone: %s  id=%s',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            fqdn, zone_id,
        )
        if not self.cfg.dry_run:
            self.client.delete(f'/{zone_id}')
        return True

    # ------------------------------------------------------------------
    # Step 7: Delete subnets
    # ------------------------------------------------------------------

    def delete_subnets(self, subnets: list[dict]) -> list[dict]:
        '''
        Delete all subnets in the provided list.

        Args:
            subnets: List of subnet resource dicts from find_subnets()

        Returns:
            List of dicts describing each deleted (or dry-run) subnet
        '''
        removed: list[dict] = []
        for subnet in subnets:
            subnet_cidr = f'{subnet["address"]}/{subnet["cidr"]}'
            subnet_id = subnet['id']
            logger.info(
                '%sDeleting subnet: %s  name=%s  id=%s',
                '[DRY-RUN] ' if self.cfg.dry_run else '',
                subnet_cidr, subnet.get('name', ''), subnet_id,
            )
            if not self.cfg.dry_run:
                self.client.delete(f'/{subnet_id}')
            removed.append({
                'address': subnet_cidr,
                'name':    subnet.get('name', ''),
                'id':      subnet_id,
            })
        return removed

    # ------------------------------------------------------------------
    # Step 8: Reset address block
    # ------------------------------------------------------------------

    def reset_block_status(self, block: dict) -> dict:
        '''
        Reset the address block tags to reflect that it is no longer
        in use:
            Status      → cfg.final_status  ('decommissioned' or 'available')
            Site        → 'unassigned'
            Location    → cleared
            Provisioned → cleared
            Decommissioned → today's ISO date

        Args:
            block: Original address block resource dict

        Returns:
            Updated address block resource dict (or dry-run plan dict)
        '''
        existing_tags = dict(block.get('tags', {}))

        # Remove site-specific tags; set lifecycle state
        updated_tags = {
            **existing_tags,
            'Status':         self.cfg.final_status,
            'Site':           'unassigned',
            'Location':       '',
            'Provisioned':    '',
            'Decommissioned': self.cfg.date,
        }

        logger.info(
            '%sResetting block %s/%s: Status=%s, Site=unassigned',
            '[DRY-RUN] ' if self.cfg.dry_run else '',
            block['address'], block['cidr'], self.cfg.final_status,
        )
        if self.cfg.dry_run:
            return {'dry_run': True, 'tags': updated_tags}

        result = self.client.patch(
            f'/{block["id"]}',
            body={'tags': updated_tags},
        )
        return result.get('result', {})

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def decommission(self) -> DecommissionResult:
        '''
        Run the full site decommission sequence and return a result
        object summarising everything that was removed.

        Returns:
            DecommissionResult with details of all removed resources
        '''
        result = DecommissionResult(
            dry_run=self.cfg.dry_run,
            final_status=self.cfg.final_status,
        )

        # Step 1: Resolve IP space
        self.resolve_ip_space()

        # Step 2: Find allocated block for this site
        block = self.find_allocated_block()
        result.block_id = block.get('id', '')
        result.block_address = f'{block["address"]}/{block["cidr"]}'

        # Step 3: Resolve DNS view
        self.resolve_dns_view()

        # Step 4: Enumerate subnets
        subnets = self.find_subnets(block)

        # Step 5: Delete IPAM hosts
        result.hosts_deleted = self.delete_hosts(subnets)

        # Step 6: Delete DNS zone
        result.dns_zone_fqdn = self.cfg.dns_zone
        result.dns_zone_deleted = self.delete_dns_zone()

        # Step 7: Delete subnets
        result.subnets_deleted = self.delete_subnets(subnets)

        # Step 8: Reset block status
        self.reset_block_status(block)
        result.block_updated = True

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _in_subnet(address: str, subnet: dict) -> bool:
    '''
    Return True if address falls within the subnet's IP range.

    Uses a simple integer comparison on the 32-bit IPv4 representation
    so no external libraries are required.

    Args:
        address: Dotted-quad IPv4 address string
        subnet:  Subnet resource dict with 'address' and 'cidr' keys

    Returns:
        True if the address is within the subnet, False otherwise
    '''
    try:
        def to_int(ip: str) -> int:
            parts = ip.split('.')
            result = 0
            for part in parts:
                result = (result << 8) | int(part)
            return result

        cidr = int(subnet['cidr'])
        mask = (0xFFFFFFFF << (32 - cidr)) & 0xFFFFFFFF
        net_int  = to_int(subnet['address']) & mask
        addr_int = to_int(address) & mask
        return addr_int == net_int
    except (ValueError, KeyError):
        return False


def confirm_decommission(cfg: DecommissionConfig) -> bool:
    '''
    Print a prominent warning and prompt the operator for explicit
    confirmation before any destructive changes are made.

    Args:
        cfg: DecommissionConfig describing what will be removed

    Returns:
        True if the operator confirmed, False if they declined
    '''
    print()
    print('!' * 60)
    print('  WARNING — Destructive operation')
    print('!' * 60)
    print(f'  Site      : {cfg.site}')
    print(f'  IP space  : {cfg.ip_space}')
    print(f'  DNS zone  : {cfg.dns_zone}')
    print(f'  DNS view  : {cfg.dns_view}')
    print(f'  Final status: {cfg.final_status}')
    if cfg.keep_zone:
        print('  DNS zone  : WILL BE KEPT (--keep-zone)')
    else:
        print('  DNS zone  : WILL BE DELETED')
    print()
    print('  The following will be permanently removed:')
    print('    • All IPAM host records in site subnets')
    if not cfg.keep_zone:
        print('    • The site DNS authoritative zone and all its records')
    print('    • All site subnets')
    print('    • Block tags will be reset (Status, Site, Location, Provisioned)')
    print()

    answer = input('  Type the site name to confirm, or press Enter to abort: ').strip()
    return answer == cfg.site


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_result(result: DecommissionResult) -> None:
    '''
    Print a human-readable decommission summary to stdout.

    Args:
        result: DecommissionResult from SiteDecommissioner.decommission()
    '''
    mode = '[DRY-RUN] ' if result.dry_run else ''
    print()
    print('=' * 60)
    print(f'{mode}Site Decommission Summary')
    print('=' * 60)
    print(f'  Address block : {result.block_address}  (id={result.block_id})')
    print()

    if result.hosts_deleted:
        print(f'  Hosts removed ({len(result.hosts_deleted)}):')
        for h in result.hosts_deleted:
            print(f'    {h["fqdn"]:<45}  id={h["id"]}')
    else:
        print('  Hosts removed : none found')
    print()

    if result.dns_zone_deleted:
        print(f'  DNS zone deleted : {result.dns_zone_fqdn}')
    else:
        print(f'  DNS zone         : kept / not found ({result.dns_zone_fqdn})')
    print()

    if result.subnets_deleted:
        print(f'  Subnets removed ({len(result.subnets_deleted)}):')
        for s in result.subnets_deleted:
            print(f'    {s["address"]:<22}  {s["name"]:<28}  id={s["id"]}')
    else:
        print('  Subnets removed : none found')
    print()

    if result.block_updated:
        print(f'  Block reset to   : Status={result.final_status}, Site=unassigned')
    print('=' * 60)

    if result.dry_run:
        print('DRY-RUN complete. Rerun without --dry-run to execute.')
    else:
        print('Decommission complete.')
    print()


# ---------------------------------------------------------------------------
# Configuration and CLI
# ---------------------------------------------------------------------------

def read_config(config_file: str) -> configparser.ConfigParser:
    '''
    Read INI configuration file.

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
        description='Tag-driven site decommissioning for Infoblox Universal DDI',
        epilog=(
            'Discovers the site block via Site and Status tags, then '
            'removes hosts, DNS zone, and subnets before resetting the block.'
        ),
    )

    # Version
    parser.add_argument(
        '-V', '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    # Site name — required
    parser.add_argument(
        '-s', '--site',
        required=True,
        metavar='NAME',
        help='Short site name to decommission (must match the Site tag on the block)',
    )

    # Optional overrides
    opt_grp = parser.add_argument_group('optional overrides')
    opt_grp.add_argument(
        '--ip-space',
        default=None,
        metavar='SPACE',
        help='IP space name (overrides INI default)',
    )
    opt_grp.add_argument(
        '--dns-parent',
        default=None,
        metavar='ZONE',
        help='Parent DNS zone used to derive the site zone FQDN (overrides INI default)',
    )
    opt_grp.add_argument(
        '--dns-view',
        default=None,
        metavar='VIEW',
        help='DNS view name (overrides INI default)',
    )
    opt_grp.add_argument(
        '--final-status',
        default='decommissioned',
        choices=['decommissioned', 'available'],
        metavar='{decommissioned,available}',
        help=(
            'Tag value to set on the block after teardown '
            '(default: decommissioned)'
        ),
    )
    opt_grp.add_argument(
        '--keep-zone',
        action='store_true',
        default=False,
        help='Leave the site DNS zone intact (skip zone deletion step)',
    )

    # Execution control
    parser.add_argument(
        '--dry-run',
        action='store_true',
        default=False,
        help='Preview all steps without making any changes',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        default=False,
        help='Skip the interactive confirmation prompt',
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

    Reads INI configuration, builds DecommissionConfig, optionally
    prompts for confirmation, runs the decommissioner, and prints a
    summary.
    '''
    args = parseargs()
    setup_logging(debug=args.debug, verbose=args.verbose)

    logger.debug('Arguments: %s', args)

    # Load INI config
    cfg_file = read_config(args.config)
    ini = dict(cfg_file['DEFAULTS']) if cfg_file.has_section('DEFAULTS') else {}

    # Resolve each value: CLI > INI > error
    def resolve(cli_val: Optional[str], ini_key: str, label: str) -> str:
        if cli_val:
            return cli_val
        v = ini.get(ini_key, '')
        if not v:
            logger.error(
                'Required value "%s" not supplied via CLI or INI [DEFAULTS].%s',
                label, ini_key,
            )
            sys.exit(1)
        return v

    ip_space   = resolve(args.ip_space,   'ip_space',   '--ip-space')
    dns_parent = resolve(args.dns_parent, 'dns_parent', '--dns-parent')
    dns_view   = resolve(args.dns_view,   'dns_view',   '--dns-view')

    cfg = DecommissionConfig(
        site=args.site.lower(),
        ip_space=ip_space,
        dns_parent=dns_parent,
        dns_view=dns_view,
        final_status=args.final_status,
        keep_zone=args.keep_zone,
        dry_run=args.dry_run,
        force=args.force,
    )

    mode_label = '[DRY-RUN] ' if cfg.dry_run else ''
    print(f'\n{mode_label}Decommissioning site: {cfg.site}')

    # Confirmation gate — skipped in dry-run mode and when --force is set
    if not cfg.dry_run and not cfg.force:
        if not confirm_decommission(cfg):
            print('Aborted.')
            sys.exit(0)

    # Initialise API client
    client = UDDIClient(
        url=cfg_file['UDDI']['url'],
        api_key=cfg_file['UDDI']['api_key'],
    )

    # Run decommissioner
    decommissioner = SiteDecommissioner(client, cfg)
    result = decommissioner.decommission()

    # Print summary
    print_result(result)


if __name__ == '__main__':
    main()
