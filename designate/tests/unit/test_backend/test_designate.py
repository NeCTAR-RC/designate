# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Author: Endre Karlson <endre.karlson@hp.com>
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
import uuid


from designateclient import exceptions
from mock import patch
from mock import NonCallableMagicMock
from mock import Mock
from oslo_log import log as logging
import oslotest.base
import testtools

from designate import objects
from designate.backend import impl_designate

LOG = logging.getLogger(__name__)


def create_zone():
    id_ = str(uuid.uuid4())
    return objects.Domain(
        id=id_,
        name='%s-example.com.' % id_,
        email='root@example.com',
    )


class RoObject(dict):
    def __setitem__(self, *a):
        raise NotImplementedError

    def __setattr__(self, *a):
        raise NotImplementedError

    def __getattr__(self, k):
        return self[k]


class DesignateBackendTest(oslotest.base.BaseTestCase):
    def setUp(self):
        super(DesignateBackendTest, self).setUp()
        opts = RoObject(
            username='user',
            password='secret',
            project_name='project',
            project_domain_name='project_domain',
            user_domain_name='user_domain'
        )
        self.target = RoObject({
            'id': '4588652b-50e7-46b9-b688-a9bad40a873e',
            'type': 'dyndns',
            'masters': [RoObject({'host': '192.0.2.1', 'port': 53})],
            'options': opts
        })

        # Backends blow up when trying to self.admin_context = ... due to
        # policy not being initialized
        self.admin_context = Mock()
        get_context_patcher = patch(
            'designate.context.DesignateContext.get_admin_context')
        self.addCleanup(get_context_patcher.stop)
        get_context = get_context_patcher.start()
        get_context.return_value = self.admin_context

        self.backend = impl_designate.DesignateBackend(self.target)

        # Mock client
        self.client = NonCallableMagicMock()
        zones = NonCallableMagicMock(spec_set=[
            'create', 'delete'])
        self.client.configure_mock(zones=zones)

    def test_create_domain(self):
        zone = create_zone()
        masters = ["%(host)s:%(port)s" % self.target.masters[0]]
        with patch.object(
                self.backend, '_get_client', return_value=self.client):
            self.backend.create_domain(self.admin_context, zone)
        self.client.zones.create.assert_called_once_with(
            zone.name, 'SECONDARY', masters=masters)

    def test_delete_domain(self):
        zone = create_zone()
        with patch.object(
                self.backend, '_get_client', return_value=self.client):
            self.backend.delete_domain(self.admin_context, zone)
        self.client.zones.delete.assert_called_once_with(zone.name)

    def test_delete_domain_notfound(self):
        zone = create_zone()
        self.client.delete.side_effect = exceptions.NotFound
        with patch.object(
                self.backend, '_get_client', return_value=self.client):
            self.backend.delete_domain(self.admin_context, zone)
        self.client.zones.delete.assert_called_once_with(zone.name)

    def test_delete_domain_exc(self):
        class Exc(Exception):
            pass

        zone = create_zone()
        self.client.zones.delete.side_effect = Exc()
        with testtools.ExpectedException(Exc):
            with patch.object(
                    self.backend, '_get_client', return_value=self.client):
                self.backend.delete_domain(self.admin_context, zone)
        self.client.zones.delete.assert_called_once_with(zone.name)
