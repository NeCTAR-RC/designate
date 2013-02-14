# Copyright 2012 Managed I.T.
#
# Author: Kiall Mac Innes <kiall@managedit.ie>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import re
from moniker.openstack.common import cfg
from moniker.openstack.common import log as logging
from moniker.openstack.common import rpc
from moniker.openstack.common.rpc import service as rpc_service
from stevedore.named import NamedExtensionManager
from moniker import exceptions
from moniker import policy
from moniker import storage
from moniker import utils
from moniker import backend

LOG = logging.getLogger(__name__)

HANDLER_NAMESPACE = 'moniker.notification.handler'


class Service(rpc_service.Service):
    def __init__(self, *args, **kwargs):

        backend_driver = cfg.CONF['service:central'].backend_driver
        self.backend = backend.get_backend(backend_driver,
                                           central_service=self)

        kwargs.update(
            host=cfg.CONF.host,
            topic=cfg.CONF.central_topic,
        )

        policy.init_policy()

        super(Service, self).__init__(*args, **kwargs)

        # Get a storage connection
        self.storage_conn = storage.get_connection()

        # Initialize extensions
        self.handlers = self._init_extensions()

        if self.handlers:
            # Get a rpc connection if needed
            self.rpc_conn = rpc.create_connection()

    def _init_extensions(self):
        """ Loads and prepares all enabled extensions """
        enabled_notification_handlers = \
            cfg.CONF['service:central'].enabled_notification_handlers

        self.extensions_manager = NamedExtensionManager(
            HANDLER_NAMESPACE, names=enabled_notification_handlers)

        def _load_extension(ext):
            handler_cls = ext.plugin
            return handler_cls(central_service=self)

        try:
            return self.extensions_manager.map(_load_extension)
        except RuntimeError:
            # No handlers enabled. No problem.
            return []

    def start(self):
        self.backend.start()
        super(Service, self).start()

        if self.handlers:
            # Setup notification subscriptions and start consuming
            self._setup_subscriptions()
            self.rpc_conn.consume_in_thread_group(self.tg)

    def stop(self):
        if self.handlers:
            # Try to shut the connection down, but if we get any sort of
            # errors, go ahead and ignore them.. as we're shutting down anyway
            try:
                self.rpc_conn.close()
            except Exception:
                pass

        super(Service, self).stop()
        self.backend.stop()

    def _setup_subscriptions(self):
        """
        Set's up subscriptions for the various exchange+topic combinations that
        we have a handler for.
        """
        for handler in self.handlers:
            exchange, topics = handler.get_exchange_topics()

            for topic in topics:
                queue_name = "moniker.notifications.%s.%s.%s" % (
                    handler.get_canonical_name(), exchange, topic)

                self.rpc_conn.declare_topic_consumer(
                    queue_name=queue_name,
                    topic=topic,
                    exchange_name=exchange,
                    callback=self._process_notification)

    def _get_handler_event_types(self):
        event_types = set()
        for handler in self.handlers:
            for et in handler.get_event_types():
                event_types.add(et)
        return event_types

    def _process_notification(self, notification):
        """
        Processes an incoming notification, offering each extension the
        opportunity to handle it.
        """
        event_type = notification.get('event_type')

        # NOTE(zykes): Only bother to actually do processing if there's any
        # matching events, skips logging of things like compute.exists etc.
        if event_type in self._get_handler_event_types():
            for handler in self.handlers:
                self._process_notification_for_handler(handler, notification)

    def _process_notification_for_handler(self, handler, notification):
        """
        Processes an incoming notification for a specific handler, checking
        to see if the handler is interested in the notification before
        handing it over.
        """
        event_type = notification['event_type']
        payload = notification['payload']

        if event_type in handler.get_event_types():
            LOG.debug('Found handler for: %s' % event_type)
            handler.process_notification(event_type, payload)

    def _is_blacklisted_domain_name(self, context, domain_name):
        """
        Ensures the provided domain_name is not blacklisted.
        """
        blacklists = cfg.CONF['service:central'].domain_name_blacklist

        for blacklist in blacklists:
            if bool(re.search(blacklist, domain_name)):
                return blacklist

        return False

    def _is_subdomain(self, context, domain_name):
        # Break the name up into it's component labels
        labels = domain_name.split(".")

        i = 1

        # Starting with label #2, search for matching domain's in the database
        while (i < len(labels)):
            name = '.'.join(labels[i:])

            try:
                domain = self.storage_conn.find_domain(context, {'name': name})
            except exceptions.DomainNotFound:
                i += 1
            else:
                return domain

        return False

    # Server Methods
    def create_server(self, context, values):
        policy.check('create_server', context)

        server = self.storage_conn.create_server(context, values)

        utils.notify(context, 'api', 'server.create', server)

        return server

    def get_servers(self, context, criterion=None):
        policy.check('get_servers', context)

        return self.storage_conn.get_servers(context, criterion)

    def get_server(self, context, server_id):
        policy.check('get_server', context, {'server_id': server_id})

        return self.storage_conn.get_server(context, server_id)

    def update_server(self, context, server_id, values):
        policy.check('update_server', context, {'server_id': server_id})

        server = self.storage_conn.update_server(context, server_id, values)

        utils.notify(context, 'api', 'server.update', server)

        return server

    def delete_server(self, context, server_id):
        policy.check('delete_server', context, {'server_id': server_id})

        server = self.storage_conn.get_server(context, server_id)

        utils.notify(context, 'api', 'server.delete', server)

        return self.storage_conn.delete_server(context, server_id)

    # TSIG Key Methods
    def create_tsigkey(self, context, values):
        policy.check('create_tsigkey', context)

        tsigkey = self.storage_conn.create_tsigkey(context, values)

        self.backend.create_tsigkey(context, tsigkey)
        utils.notify(context, 'api', 'tsigkey.create', tsigkey)

        return tsigkey

    def get_tsigkeys(self, context, criterion=None):
        policy.check('get_tsigkeys', context)

        return self.storage_conn.get_tsigkeys(context, criterion)

    def get_tsigkey(self, context, tsigkey_id):
        policy.check('get_tsigkey', context, {'tsigkey_id': tsigkey_id})

        return self.storage_conn.get_tsigkey(context, tsigkey_id)

    def update_tsigkey(self, context, tsigkey_id, values):
        policy.check('update_tsigkey', context, {'tsigkey_id': tsigkey_id})

        tsigkey = self.storage_conn.update_tsigkey(context, tsigkey_id, values)

        self.backend.update_tsigkey(context, tsigkey)
        utils.notify(context, 'api', 'tsigkey.update', tsigkey)

        return tsigkey

    def delete_tsigkey(self, context, tsigkey_id):
        policy.check('delete_tsigkey', context, {'tsigkey_id': tsigkey_id})

        tsigkey = self.storage_conn.get_tsigkey(context, tsigkey_id)

        self.backend.delete_tsigkey(context, tsigkey)
        utils.notify(context, 'api', 'tsigkey.delete', tsigkey)

        return self.storage_conn.delete_tsigkey(context, tsigkey_id)

    # Domain Methods
    def create_domain(self, context, values):
        values['tenant_id'] = context.tenant_id

        target = {
            'tenant_id': values['tenant_id'],
            'domain_name': values['name']
        }

        policy.check('create_domain', context, target)

        # Ensure the domain is not blacklisted
        if self._is_blacklisted_domain_name(context, values['name']):
            # Raises an exception if the policy check is denied
            policy.check('use_blacklisted_domain', context)

        # Handle sub-domains appropriately
        parent_domain = self._is_subdomain(context, values['name'])

        if parent_domain:
            if parent_domain['tenant_id'] == values['tenant_id']:
                # Record the Parent Domain ID
                values['parent_domain_id'] = parent_domain['id']
            else:
                raise exceptions.Forbidden('Unable to create subdomain in '
                                           'another tenants domain')

        # NOTE(kiall): Fetch the servers before creating the domain, this way
        #              we can prevent domain creation if no servers are
        #              configured.
        servers = self.storage_conn.get_servers(context)

        if len(servers) == 0:
            LOG.critical('No servers configured. Please create at least one '
                         'server')
            raise exceptions.NoServersConfigured()

        domain = self.storage_conn.create_domain(context, values)

        self.backend.create_domain(context, domain)
        utils.notify(context, 'api', 'domain.create', domain)

        return domain

    def get_domains(self, context, criterion=None):
        target = {'tenant_id': context.tenant_id}
        policy.check('get_domains', context, target)

        if criterion is None:
            criterion = {}

        if not context.is_admin:
            criterion['tenant_id'] = context.tenant_id

        return self.storage_conn.get_domains(context, criterion)

    def get_domain(self, context, domain_id):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'tenant_id': domain['tenant_id']
        }
        policy.check('get_domain', context, target)

        return domain

    def update_domain(self, context, domain_id, values):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('update_domain', context, target)

        if 'tenant_id' in values:
            # NOTE(kiall): Ensure the user is allowed to delete a domain from
            #              the original tenant.
            policy.check('delete_domain', context, target)

            # NOTE(kiall): Ensure the user is allowed to create a domain in
            #              the new tenant.
            target = {'domain_id': domain_id, 'tenant_id': values['tenant_id']}
            policy.check('create_domain', context, target)

        if 'name' in values and values['name'] != domain['name']:
            raise exceptions.BadRequest('Renaming a domain is not allowed')

        domain = self.storage_conn.update_domain(context, domain_id, values)

        self.backend.update_domain(context, domain)
        utils.notify(context, 'api', 'domain.update', domain)

        return domain

    def delete_domain(self, context, domain_id):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('delete_domain', context, target)

        self.backend.delete_domain(context, domain)
        utils.notify(context, 'api', 'domain.delete', domain)

        return self.storage_conn.delete_domain(context, domain_id)

    # Record Methods
    def create_record(self, context, domain_id, values):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'record_name': values['name'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('create_record', context, target)

        if not values['name'].endswith(domain['name']):
            raise exceptions.BadRequest('Records must be contained within '
                                        'their parent zone.')

        record = self.storage_conn.create_record(context, domain_id, values)

        self.backend.create_record(context, domain, record)
        utils.notify(context, 'api', 'record.create', record)

        return record

    def get_records(self, context, domain_id, criterion=None):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('get_records', context, target)

        return self.storage_conn.get_records(context, domain_id, criterion)

    def get_record(self, context, domain_id, record_id):
        domain = self.storage_conn.get_domain(context, domain_id)
        record = self.storage_conn.get_record(context, record_id)

        # Ensure the domain_id matches the record's domain_id
        if domain['id'] != record['domain_id']:
            raise exceptions.RecordNotFound()

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'record_id': record['id'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('get_record', context, target)

        return record

    def update_record(self, context, domain_id, record_id, values):
        domain = self.storage_conn.get_domain(context, domain_id)
        record = self.storage_conn.get_record(context, record_id)

        # Ensure the domain_id matches the record's domain_id
        if domain['id'] != record['domain_id']:
            raise exceptions.RecordNotFound()

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'record_id': record['id'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('update_record', context, target)

        if 'name' in values and not values['name'].endswith(domain['name']):
            raise exceptions.BadRequest('Records must be contained within '
                                        'their parent zone.')

        record = self.storage_conn.update_record(context, record_id, values)

        self.backend.update_record(context, domain, record)
        utils.notify(context, 'api', 'record.update', record)

        return record

    def delete_record(self, context, domain_id, record_id):
        domain = self.storage_conn.get_domain(context, domain_id)
        record = self.storage_conn.get_record(context, record_id)

        # Ensure the domain_id matches the record's domain_id
        if domain['id'] != record['domain_id']:
            raise exceptions.RecordNotFound()

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'record_id': record['id'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('delete_record', context, target)

        self.backend.delete_record(context, domain, record)
        utils.notify(context, 'api', 'record.delete', record)

        return self.storage_conn.delete_record(context, record_id)

    # Diagnostics Methods
    def sync_all(self, context):
        policy.check('diagnostics_sync_all', context)

        domains = self.storage_conn.get_domains(context)
        results = {}

        for domain in domains:
            servers = self.storage_conn.get_servers(context)
            records = self.storage_conn.get_records(context, domain['id'])

            results[domain['id']] = self.backend.sync_domain(context,
                                                             domain,
                                                             records,
                                                             servers)

        return results

    def sync_domain(self, context, domain_id):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'tenant_id': domain['tenant_id']
        }

        policy.check('diagnostics_sync_domain', context, target)

        records = self.storage_conn.get_records(context, domain_id)

        return self.backend.sync_domain(context, domain, records)

    def sync_record(self, context, domain_id, record_id):
        domain = self.storage_conn.get_domain(context, domain_id)

        target = {
            'domain_id': domain_id,
            'domain_name': domain['name'],
            'record_id': record_id,
            'tenant_id': domain['tenant_id']
        }

        policy.check('diagnostics_sync_record', context, target)

        record = self.storage_conn.get_record(context, record_id)

        return self.backend.sync_record(context, domain, record)

    def ping(self, context):
        policy.check('diagnostics_ping', context)

        try:
            backend_status = self.backend.ping(context)
        except Exception, e:
            backend_status = {'status': False, 'message': str(e)}

        try:
            storage_status = self.storage_conn.ping(context)
        except Exception, e:
            storage_status = {'status': False, 'message': str(e)}

        if backend_status and storage_status:
            status = True
        else:
            status = False

        return {
            'host': cfg.CONF.host,
            'status': status,
            'backend': backend_status,
            'storage': storage_status
        }
