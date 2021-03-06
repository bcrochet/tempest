# Copyright 2012 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import atexit
import functools
import json
import os
import sys
import time
import urllib
import uuid

import fixtures
import nose.plugins.attrib
import testresources
import testtools

from tempest import clients
from tempest.common import generate_json
from tempest.common import isolated_creds
from tempest import config
from tempest import exceptions
from tempest.openstack.common import log as logging

LOG = logging.getLogger(__name__)

CONF = config.CONF

# All the successful HTTP status codes from RFC 2616
HTTP_SUCCESS = (200, 201, 202, 203, 204, 205, 206)


def attr(*args, **kwargs):
    """A decorator which applies the nose and testtools attr decorator

    This decorator applies the nose attr decorator as well as the
    the testtools.testcase.attr if it is in the list of attributes
    to testtools we want to apply.
    """

    def decorator(f):
        if 'type' in kwargs and isinstance(kwargs['type'], str):
            f = testtools.testcase.attr(kwargs['type'])(f)
            if kwargs['type'] == 'smoke':
                f = testtools.testcase.attr('gate')(f)
        elif 'type' in kwargs and isinstance(kwargs['type'], list):
            for attr in kwargs['type']:
                f = testtools.testcase.attr(attr)(f)
                if attr == 'smoke':
                    f = testtools.testcase.attr('gate')(f)
        return nose.plugins.attrib.attr(*args, **kwargs)(f)

    return decorator


def services(*args, **kwargs):
    """A decorator used to set an attr for each service used in a test case

    This decorator applies a testtools attr for each service that gets
    exercised by a test case.
    """
    service_list = {
        'compute': CONF.service_available.nova,
        'image': CONF.service_available.glance,
        'volume': CONF.service_available.cinder,
        'orchestration': CONF.service_available.heat,
        # NOTE(mtreinish) nova-network will provide networking functionality
        # if neutron isn't available, so always set to True.
        'network': True,
        'identity': True,
        'object_storage': CONF.service_available.swift,
        'dashboard': CONF.service_available.horizon,
    }

    def decorator(f):
        for service in args:
            if service not in service_list:
                raise exceptions.InvalidServiceTag('%s is not a valid service'
                                                   % service)
        attr(type=list(args))(f)

        @functools.wraps(f)
        def wrapper(self, *func_args, **func_kwargs):
            for service in args:
                if not service_list[service]:
                    msg = 'Skipped because the %s service is not available' % (
                        service)
                    raise testtools.TestCase.skipException(msg)
            return f(self, *func_args, **func_kwargs)
        return wrapper
    return decorator


def stresstest(*args, **kwargs):
    """Add stress test decorator

    For all functions with this decorator a attr stress will be
    set automatically.

    @param class_setup_per: allowed values are application, process, action
           ``application``: once in the stress job lifetime
           ``process``: once in the worker process lifetime
           ``action``: on each action
    @param allow_inheritance: allows inheritance of this attribute
    """
    def decorator(f):
        if 'class_setup_per' in kwargs:
            setattr(f, "st_class_setup_per", kwargs['class_setup_per'])
        else:
            setattr(f, "st_class_setup_per", 'process')
        if 'allow_inheritance' in kwargs:
            setattr(f, "st_allow_inheritance", kwargs['allow_inheritance'])
        else:
            setattr(f, "st_allow_inheritance", False)
        attr(type='stress')(f)
        return f
    return decorator


def skip_because(*args, **kwargs):
    """A decorator useful to skip tests hitting known bugs

    @param bug: bug number causing the test to skip
    @param condition: optional condition to be True for the skip to have place
    @param interface: skip the test if it is the same as self._interface
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(self, *func_args, **func_kwargs):
            skip = False
            if "condition" in kwargs:
                if kwargs["condition"] is True:
                    skip = True
            elif "interface" in kwargs:
                if kwargs["interface"] == self._interface:
                    skip = True
            else:
                skip = True
            if "bug" in kwargs and skip is True:
                msg = "Skipped until Bug: %s is resolved." % kwargs["bug"]
                raise testtools.TestCase.skipException(msg)
            return f(self, *func_args, **func_kwargs)
        return wrapper
    return decorator


def requires_ext(*args, **kwargs):
    """A decorator to skip tests if an extension is not enabled

    @param extension
    @param service
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*func_args, **func_kwargs):
            if not is_extension_enabled(kwargs['extension'],
                                        kwargs['service']):
                msg = "Skipped because %s extension: %s is not enabled" % (
                    kwargs['service'], kwargs['extension'])
                raise testtools.TestCase.skipException(msg)
            return func(*func_args, **func_kwargs)
        return wrapper
    return decorator


def is_extension_enabled(extension_name, service):
    """A function that will check the list of enabled extensions from config

    """
    config_dict = {
        'compute': CONF.compute_feature_enabled.api_extensions,
        'compute_v3': CONF.compute_feature_enabled.api_v3_extensions,
        'volume': CONF.volume_feature_enabled.api_extensions,
        'network': CONF.network_feature_enabled.api_extensions,
        'object': CONF.object_storage_feature_enabled.discoverable_apis,
    }
    if config_dict[service][0] == 'all':
        return True
    if extension_name in config_dict[service]:
        return True
    return False

# there is a mis-match between nose and testtools for older pythons.
# testtools will set skipException to be either
# unittest.case.SkipTest, unittest2.case.SkipTest or an internal skip
# exception, depending on what it can find. Python <2.7 doesn't have
# unittest.case.SkipTest; so if unittest2 is not installed it falls
# back to the internal class.
#
# The current nose skip plugin will decide to raise either
# unittest.case.SkipTest or its own internal exception; it does not
# look for unittest2 or the internal unittest exception.  Thus we must
# monkey-patch testtools.TestCase.skipException to be the exception
# the nose skip plugin expects.
#
# However, with the switch to testr nose may not be available, so we
# require you to opt-in to this fix with an environment variable.
#
# This is temporary until upstream nose starts looking for unittest2
# as testtools does; we can then remove this and ensure unittest2 is
# available for older pythons; then nose and testtools will agree
# unittest2.case.SkipTest is the one-true skip test exception.
#
#   https://review.openstack.org/#/c/33056
#   https://github.com/nose-devs/nose/pull/699
if 'TEMPEST_PY26_NOSE_COMPAT' in os.environ:
    try:
        import unittest.case.SkipTest
        # convince pep8 we're using the import...
        if unittest.case.SkipTest:
            pass
        raise RuntimeError("You have unittest.case.SkipTest; "
                           "no need to override")
    except ImportError:
        LOG.info("Overriding skipException to nose SkipTest")
        testtools.TestCase.skipException = nose.plugins.skip.SkipTest

at_exit_set = set()


def validate_tearDownClass():
    if at_exit_set:
        raise RuntimeError("tearDownClass does not call the super's "
                           "tearDownClass in these classes: "
                           + str(at_exit_set) + "\n"
                           "If you see the exception, with another "
                           "exception please do not report this one! "
                           "If you are changing tempest code, make sure you "
                           "are calling the super class's tearDownClass!")

atexit.register(validate_tearDownClass)

if sys.version_info >= (2, 7):
    class BaseDeps(testtools.TestCase,
                   testtools.testcase.WithAttributes,
                   testresources.ResourcedTestCase):
        pass
else:
    # Define asserts for py26
    import unittest2

    class BaseDeps(testtools.TestCase,
                   testtools.testcase.WithAttributes,
                   testresources.ResourcedTestCase,
                   unittest2.TestCase):
        pass


class BaseTestCase(BaseDeps):

    setUpClassCalled = False
    _service = None

    network_resources = {}

    @classmethod
    def setUpClass(cls):
        if hasattr(super(BaseTestCase, cls), 'setUpClass'):
            super(BaseTestCase, cls).setUpClass()
        cls.setUpClassCalled = True

    @classmethod
    def tearDownClass(cls):
        at_exit_set.discard(cls)
        if hasattr(super(BaseTestCase, cls), 'tearDownClass'):
            super(BaseTestCase, cls).tearDownClass()

    def setUp(self):
        super(BaseTestCase, self).setUp()
        if not self.setUpClassCalled:
            raise RuntimeError("setUpClass does not calls the super's"
                               "setUpClass in the "
                               + self.__class__.__name__)
        at_exit_set.add(self.__class__)
        test_timeout = os.environ.get('OS_TEST_TIMEOUT', 0)
        try:
            test_timeout = int(test_timeout)
        except ValueError:
            test_timeout = 0
        if test_timeout > 0:
            self.useFixture(fixtures.Timeout(test_timeout, gentle=True))

        if (os.environ.get('OS_STDOUT_CAPTURE') == 'True' or
                os.environ.get('OS_STDOUT_CAPTURE') == '1'):
            stdout = self.useFixture(fixtures.StringStream('stdout')).stream
            self.useFixture(fixtures.MonkeyPatch('sys.stdout', stdout))
        if (os.environ.get('OS_STDERR_CAPTURE') == 'True' or
                os.environ.get('OS_STDERR_CAPTURE') == '1'):
            stderr = self.useFixture(fixtures.StringStream('stderr')).stream
            self.useFixture(fixtures.MonkeyPatch('sys.stderr', stderr))
        if (os.environ.get('OS_LOG_CAPTURE') != 'False' and
            os.environ.get('OS_LOG_CAPTURE') != '0'):
            log_format = '%(asctime)-15s %(message)s'
            self.useFixture(fixtures.LoggerFixture(nuke_handlers=False,
                                                   format=log_format,
                                                   level=None))

    @classmethod
    def get_client_manager(cls, interface=None):
        """
        Returns an Openstack client manager
        """
        cls.isolated_creds = isolated_creds.IsolatedCreds(
            cls.__name__, network_resources=cls.network_resources)

        force_tenant_isolation = getattr(cls, 'force_tenant_isolation', None)
        if (CONF.compute.allow_tenant_isolation or
            force_tenant_isolation):
            creds = cls.isolated_creds.get_primary_creds()
            username, tenant_name, password = creds
            if getattr(cls, '_interface', None):
                os = clients.Manager(username=username,
                                     password=password,
                                     tenant_name=tenant_name,
                                     interface=cls._interface,
                                     service=cls._service)
            elif interface:
                os = clients.Manager(username=username,
                                     password=password,
                                     tenant_name=tenant_name,
                                     interface=interface,
                                     service=cls._service)
            else:
                os = clients.Manager(username=username,
                                     password=password,
                                     tenant_name=tenant_name,
                                     service=cls._service)
        else:
            if getattr(cls, '_interface', None):
                os = clients.Manager(interface=cls._interface,
                                     service=cls._service)
            elif interface:
                os = clients.Manager(interface=interface, service=cls._service)
            else:
                os = clients.Manager(service=cls._service)
        return os

    @classmethod
    def clear_isolated_creds(cls):
        """
        Clears isolated creds if set
        """
        if getattr(cls, 'isolated_creds'):
            cls.isolated_creds.clear_isolated_creds()

    @classmethod
    def _get_identity_admin_client(cls):
        """
        Returns an instance of the Identity Admin API client
        """
        os = clients.AdminManager(interface=cls._interface,
                                  service=cls._service)
        admin_client = os.identity_client
        return admin_client

    @classmethod
    def set_network_resources(self, network=False, router=False, subnet=False,
                              dhcp=False):
        """Specify which network resources should be created

        @param network
        @param router
        @param subnet
        @param dhcp
        """
        # network resources should be set only once from callers
        # in order to ensure that even if it's called multiple times in
        # a chain of overloaded methods, the attribute is set only
        # in the leaf class
        if not self.network_resources:
            self.network_resources = {
                'network': network,
                'router': router,
                'subnet': subnet,
                'dhcp': dhcp}


class NegativeAutoTest(BaseTestCase):

    _resources = {}

    @classmethod
    def setUpClass(cls):
        super(NegativeAutoTest, cls).setUpClass()
        os = cls.get_client_manager()
        cls.client = os.negative_client

    @staticmethod
    def load_schema(file):
        """
        Loads a schema from a file on a specified location.

        :param file: the file name
        """
        #NOTE(mkoderer): must be extended for xml support
        fn = os.path.join(
            os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
            "etc", "schemas", file)
        LOG.debug("Open schema file: %s" % (fn))
        return json.load(open(fn))

    @staticmethod
    def generate_scenario(description_file):
        """
        Generates the test scenario list for a given description.

        :param description: A dictionary with the following entries:
            name (required) name for the api
            http-method (required) one of HEAD,GET,PUT,POST,PATCH,DELETE
            url (required) the url to be appended to the catalog url with '%s'
                for each resource mentioned
            resources: (optional) A list of resource names such as "server",
                "flavor", etc. with an element for each '%s' in the url. This
                method will call self.get_resource for each element when
                constructing the positive test case template so negative
                subclasses are expected to return valid resource ids when
                appropriate.
            json-schema (optional) A valid json schema that will be used to
                create invalid data for the api calls. For "GET" and "HEAD",
                the data is used to generate query strings appended to the url,
                otherwise for the body of the http call.
        """
        description = NegativeAutoTest.load_schema(description_file)
        LOG.debug(description)
        generate_json.validate_negative_test_schema(description)
        schema = description.get("json-schema", None)
        resources = description.get("resources", [])
        scenario_list = []
        expected_result = None
        for resource in resources:
            if isinstance(resource, dict):
                expected_result = resource['expected_result']
                resource = resource['name']
            LOG.debug("Add resource to test %s" % resource)
            scn_name = "inv_res_%s" % (resource)
            scenario_list.append((scn_name, {"resource": (resource,
                                                          str(uuid.uuid4())),
                                             "expected_result": expected_result
                                             }))
        if schema is not None:
            for invalid in generate_json.generate_invalid(schema):
                scenario_list.append((invalid[0],
                                      {"schema": invalid[1],
                                       "expected_result": invalid[2]}))
        LOG.debug(scenario_list)
        return scenario_list

    def execute(self, description_file):
        """
        Execute a http call on an api that are expected to
        result in client errors. First it uses invalid resources that are part
        of the url, and then invalid data for queries and http request bodies.

        :param description: A dictionary with the following entries:
            name (required) name for the api
            http-method (required) one of HEAD,GET,PUT,POST,PATCH,DELETE
            url (required) the url to be appended to the catalog url with '%s'
                for each resource mentioned
            resources: (optional) A list of resource names such as "server",
                "flavor", etc. with an element for each '%s' in the url. This
                method will call self.get_resource for each element when
                constructing the positive test case template so negative
                subclasses are expected to return valid resource ids when
                appropriate.
            json-schema (optional) A valid json schema that will be used to
                create invalid data for the api calls. For "GET" and "HEAD",
                the data is used to generate query strings appended to the url,
                otherwise for the body of the http call.

        """
        description = NegativeAutoTest.load_schema(description_file)
        LOG.info("Executing %s" % description["name"])
        LOG.debug(description)
        method = description["http-method"]
        url = description["url"]

        resources = [self.get_resource(r) for
                     r in description.get("resources", [])]

        if hasattr(self, "resource"):
            # Note(mkoderer): The resources list already contains an invalid
            # entry (see get_resource).
            # We just send a valid json-schema with it
            valid = None
            schema = description.get("json-schema", None)
            if schema:
                valid = generate_json.generate_valid(schema)
            new_url, body = self._http_arguments(valid, url, method)
        elif hasattr(self, "schema"):
            new_url, body = self._http_arguments(self.schema, url, method)

        resp, resp_body = self.client.send_request(method, new_url,
                                                   resources, body=body)
        self._check_negative_response(resp.status, resp_body)

    def _http_arguments(self, json_dict, url, method):
        LOG.debug("dict: %s url: %s method: %s" % (json_dict, url, method))
        if not json_dict:
            return url, None
        elif method in ["GET", "HEAD", "PUT", "DELETE"]:
            return "%s?%s" % (url, urllib.urlencode(json_dict)), None
        else:
            return url, json.dumps(json_dict)

    def _check_negative_response(self, result, body):
        expected_result = getattr(self, "expected_result", None)
        self.assertTrue(result >= 400 and result < 500 and result != 413,
                        "Expected client error, got %s:%s" %
                        (result, body))
        self.assertTrue(expected_result is None or expected_result == result,
                        "Expected %s, got %s:%s" %
                        (expected_result, result, body))

    @classmethod
    def set_resource(cls, name, resource):
        """
        This function can be used in setUpClass context to register a resoruce
        for a test.

        :param name: The name of the kind of resource such as "flavor", "role",
            etc.
        :resource: The id of the resource
        """
        cls._resources[name] = resource

    def get_resource(self, name):
        """
        Return a valid uuid for a type of resource. If a real resource is
        needed as part of a url then this method should return one. Otherwise
        it can return None.

        :param name: The name of the kind of resource such as "flavor", "role",
            etc.
        """
        if isinstance(name, dict):
            name = name['name']
        if hasattr(self, "resource") and self.resource[0] == name:
            LOG.debug("Return invalid resource (%s) value: %s" %
                      (self.resource[0], self.resource[1]))
            return self.resource[1]
        if name in self._resources:
            return self._resources[name]
        return None


def call_until_true(func, duration, sleep_for):
    """
    Call the given function until it returns True (and return True) or
    until the specified duration (in seconds) elapses (and return
    False).

    :param func: A zero argument callable that returns True on success.
    :param duration: The number of seconds for which to attempt a
        successful call of the function.
    :param sleep_for: The number of seconds to sleep after an unsuccessful
                      invocation of the function.
    """
    now = time.time()
    timeout = now + duration
    while now < timeout:
        if func():
            return True
        LOG.debug("Sleeping for %d seconds", sleep_for)
        time.sleep(sleep_for)
        now = time.time()
    return False
