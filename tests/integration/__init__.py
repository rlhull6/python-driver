# Copyright DataStax, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from cassandra.cluster import Cluster

from tests import connection_class, EVENT_LOOP_MANAGER
Cluster.connection_class = connection_class

try:
    import unittest2 as unittest
except ImportError:
    import unittest  # noqa
from packaging.version import Version
import logging
import socket
import sys
import time
import traceback
import platform
from threading import Event
from subprocess import call
from itertools import groupby
import six

from cassandra import OperationTimedOut, ReadTimeout, ReadFailure, WriteTimeout, WriteFailure, AlreadyExists, \
    InvalidRequest
from cassandra.cluster import NoHostAvailable

from cassandra.protocol import ConfigurationException

try:
    from ccmlib.dse_cluster import DseCluster
    from ccmlib.cluster import Cluster as CCMCluster
    from ccmlib.cluster_factory import ClusterFactory as CCMClusterFactory
    from ccmlib import common
except ImportError as e:
    CCMClusterFactory = None

log = logging.getLogger(__name__)

CLUSTER_NAME = 'test_cluster'
SINGLE_NODE_CLUSTER_NAME = 'single_node'
MULTIDC_CLUSTER_NAME = 'multidc_test_cluster'

CCM_CLUSTER = None

path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'ccm')
if not os.path.exists(path):
    os.mkdir(path)

cass_version = None
cql_version = None


def get_server_versions():
    """
    Probe system.local table to determine Cassandra and CQL version.
    Returns a tuple of (cassandra_version, cql_version).
    """
    global cass_version, cql_version

    if cass_version is not None:
        return (cass_version, cql_version)

    c = Cluster()
    s = c.connect()
    row = s.execute('SELECT cql_version, release_version FROM system.local')[0]

    cass_version = _tuple_version(row.release_version)
    cql_version = _tuple_version(row.cql_version)

    c.shutdown()

    return (cass_version, cql_version)


def _tuple_version(version_string):
    if '-' in version_string:
        version_string = version_string[:version_string.index('-')]

    return tuple([int(p) for p in version_string.split('.')])


def cmd_line_args_to_dict(env_var):
    cmd_args_env = os.environ.get(env_var, None)
    args = {}
    if cmd_args_env:
        cmd_args = cmd_args_env.strip().split(' ')
        while cmd_args:
            cmd_arg = cmd_args.pop(0)
            cmd_arg_value = True if cmd_arg.startswith('--') else cmd_args.pop(0)
            args[cmd_arg.lstrip('-')] = cmd_arg_value
    return args


def _get_dse_version_from_cass(cass_version):
    if cass_version.startswith('2.1'):
        dse_ver = "4.8.15"
    elif cass_version.startswith('3.0'):
        dse_ver = "5.0.12"
    elif cass_version.startswith('3.10') or cass_version.startswith('3.11'):
        dse_ver = "5.1.7"
    elif cass_version.startswith('4.0'):
        dse_ver = "6.0"
    else:
        log.error("Unknown cassandra version found {0}, defaulting to 2.1".format(cass_version))
        dse_ver = "2.1"
    return dse_ver

USE_CASS_EXTERNAL = bool(os.getenv('USE_CASS_EXTERNAL', False))
KEEP_TEST_CLUSTER = bool(os.getenv('KEEP_TEST_CLUSTER', False))
SIMULACRON_JAR = os.getenv('SIMULACRON_JAR', None)

CASSANDRA_IP = os.getenv('CASSANDRA_IP', '127.0.0.1')
CASSANDRA_DIR = os.getenv('CASSANDRA_DIR', None)

default_cassandra_version = '3.11'
cv_string = os.getenv('CASSANDRA_VERSION', default_cassandra_version)
mcv_string = os.getenv('MAPPED_CASSANDRA_VERSION', None)
try:
    cassandra_version = Version(cv_string)  # env var is set to test-dse
except:
    # fallback to MAPPED_CASSANDRA_VERSION
    cassandra_version = Version(mcv_string)
CASSANDRA_VERSION = Version(mcv_string) if mcv_string else cassandra_version
CCM_VERSION = cassandra_version if mcv_string else CASSANDRA_VERSION

default_dse_version = _get_dse_version_from_cass(CASSANDRA_VERSION.base_version)

DSE_VERSION = Version(os.getenv('DSE_VERSION', default_dse_version))

CCM_KWARGS = {}
if CASSANDRA_DIR:
    log.info("Using Cassandra dir: %s", CASSANDRA_DIR)
    CCM_KWARGS['install_dir'] = CASSANDRA_DIR

else:
    log.info('Using Cassandra version: %s', CASSANDRA_VERSION)
    log.info('Using CCM version: %s', CCM_VERSION)
    CCM_KWARGS['version'] = CCM_VERSION

#This changes the default contact_point parameter in Cluster
def set_default_cass_ip():
    if CASSANDRA_IP.startswith("127.0.0."):
        return
    defaults = list(Cluster.__init__.__defaults__)
    defaults = [[CASSANDRA_IP]] + defaults[1:]
    try:
        Cluster.__init__.__defaults__ = tuple(defaults)
    except:
        Cluster.__init__.__func__.__defaults__ = tuple(defaults)


def set_default_beta_flag_true():
    defaults = list(Cluster.__init__.__defaults__)
    defaults = (defaults[:28] + [True] + defaults[29:])
    try:
        Cluster.__init__.__defaults__ = tuple(defaults)
    except:
        Cluster.__init__.__func__.__defaults__ = tuple(defaults)


def get_default_protocol():
    if CASSANDRA_VERSION >= Version('4.0'):
        set_default_beta_flag_true()
        return 5
    elif CASSANDRA_VERSION >= Version('2.2'):
        return 4
    elif CASSANDRA_VERSION >= Version('2.1'):
        return 3
    elif CASSANDRA_VERSION >= Version('2.0'):
        return 2
    else:
        return 1


def get_supported_protocol_versions():
    """
    1.2 -> 1
    2.0 -> 2, 1
    2.1 -> 3, 2, 1
    2.2 -> 4, 3, 2, 1
    3.X -> 4, 3
    3.10 -> 5(beta),4,3
`   """
    if CASSANDRA_VERSION >= Version('4.0'):
        return (3, 4, 5)
    elif CASSANDRA_VERSION >= Version('3.10'):
        return (3, 4)
    elif CASSANDRA_VERSION >= Version('3.0'):
        return (3, 4)
    elif CASSANDRA_VERSION >= Version('2.2'):
        return (1, 2, 3, 4)
    elif CASSANDRA_VERSION >= Version('2.1'):
        return (1, 2, 3)
    elif CASSANDRA_VERSION >= Version('2.0'):
        return (1, 2)
    else:
        return (1, )


def get_unsupported_lower_protocol():
    """
    This is used to determine the lowest protocol version that is NOT
    supported by the version of C* running
    """

    if CASSANDRA_VERSION >= Version('3.0'):
        return 2
    else:
        return None


def get_unsupported_upper_protocol():
    """
    This is used to determine the highest protocol version that is NOT
    supported by the version of C* running
    """

    if CASSANDRA_VERSION >= Version('2.2'):
        return None
    if CASSANDRA_VERSION >= Version('2.1'):
        return 4
    elif CASSANDRA_VERSION >= Version('2.0'):
        return 3
    else:
        return None


default_protocol_version = get_default_protocol()


PROTOCOL_VERSION = int(os.getenv('PROTOCOL_VERSION', default_protocol_version))


def local_decorator_creator():
    if not CASSANDRA_IP.startswith("127.0.0."):
        return unittest.skip('Tests only runs against local C*')

    def _id_and_mark(f):
        f.local = True
        return f

    return _id_and_mark

local = local_decorator_creator()
notprotocolv1 = unittest.skipUnless(PROTOCOL_VERSION > 1, 'Protocol v1 not supported')
lessthenprotocolv4 = unittest.skipUnless(PROTOCOL_VERSION < 4, 'Protocol versions 4 or greater not supported')
greaterthanprotocolv3 = unittest.skipUnless(PROTOCOL_VERSION >= 4, 'Protocol versions less than 4 are not supported')
protocolv5 = unittest.skipUnless(5 in get_supported_protocol_versions(), 'Protocol versions less than 5 are not supported')

greaterthancass20 = unittest.skipUnless(CASSANDRA_VERSION >= Version('2.1'), 'Cassandra version 2.1 or greater required')
greaterthancass21 = unittest.skipUnless(CASSANDRA_VERSION >= Version('2.2'), 'Cassandra version 2.2 or greater required')
greaterthanorequalcass30 = unittest.skipUnless(CASSANDRA_VERSION >= Version('3.0'), 'Cassandra version 3.0 or greater required')
greaterthanorequalcass36 = unittest.skipUnless(CASSANDRA_VERSION >= Version('3.6'), 'Cassandra version 3.6 or greater required')
greaterthanorequalcass3_10 = unittest.skipUnless(CASSANDRA_VERSION >= Version('3.10'), 'Cassandra version 3.10 or greater required')
greaterthanorequalcass3_11 = unittest.skipUnless(CASSANDRA_VERSION >= Version('3.11'), 'Cassandra version 3.10 or greater required')
greaterthanorequalcass40 = unittest.skipUnless(CASSANDRA_VERSION >= Version('4.0'), 'Cassandra version 4.0 or greater required')
lessthanorequalcass40 = unittest.skipIf(CASSANDRA_VERSION >= Version('4.0'), 'Cassandra version 4.0 or greater required')
lessthancass30 = unittest.skipUnless(CASSANDRA_VERSION < Version('3.0'), 'Cassandra version less then 3.0 required')
pypy = unittest.skipUnless(platform.python_implementation() == "PyPy", "Test is skipped unless it's on PyPy")
notpy3 = unittest.skipIf(sys.version_info >= (3, 0), "Test not applicable for Python 3.x runtime")
requiresmallclockgranularity = unittest.skipIf("Windows" in platform.system() or "asyncore" in EVENT_LOOP_MANAGER,
                                               "This test is not suitible for environments with large clock granularity")
requiressimulacron = unittest.skipIf(SIMULACRON_JAR is None or CASSANDRA_VERSION < Version("2.1"), "Simulacron jar hasn't been specified or C* version is 2.0")


def wait_for_node_socket(node, timeout):
    binary_itf = node.network_interfaces['binary']
    if not common.check_socket_listening(binary_itf, timeout=timeout):
        log.warning("Unable to connect to binary socket for node " + node.name)
    else:
        log.debug("Node %s is up and listening " % (node.name,))


def check_socket_listening(itf, timeout=60):
    end = time.time() + timeout
    while time.time() <= end:
        try:
            sock = socket.socket()
            sock.connect(itf)
            sock.close()
            return True
        except socket.error:
            # Try again in another 200ms
            time.sleep(.2)
            continue
    return False


def get_cluster():
    return CCM_CLUSTER


def get_node(node_id):
    return CCM_CLUSTER.nodes['node%s' % node_id]


def use_multidc(dc_list, workloads=[]):
    use_cluster(MULTIDC_CLUSTER_NAME, dc_list, start=True, workloads=workloads)


def use_singledc(start=True, workloads=[]):
    use_cluster(CLUSTER_NAME, [3], start=start, workloads=workloads)


def use_single_node(start=True, workloads=[]):
    use_cluster(SINGLE_NODE_CLUSTER_NAME, [1], start=start, workloads=workloads)


def remove_cluster():
    if USE_CASS_EXTERNAL or KEEP_TEST_CLUSTER:
        return

    global CCM_CLUSTER
    if CCM_CLUSTER:
        log.debug("Removing cluster {0}".format(CCM_CLUSTER.name))
        tries = 0
        while tries < 100:
            try:
                CCM_CLUSTER.remove()
                CCM_CLUSTER = None
                return
            except OSError:
                ex_type, ex, tb = sys.exc_info()
                log.warning("{0}: {1} Backtrace: {2}".format(ex_type.__name__, ex, traceback.extract_tb(tb)))
                del tb
                tries += 1
                time.sleep(1)

        raise RuntimeError("Failed to remove cluster after 100 attempts")


def is_current_cluster(cluster_name, node_counts):
    global CCM_CLUSTER
    if CCM_CLUSTER and CCM_CLUSTER.name == cluster_name:
        if [len(list(nodes)) for dc, nodes in
                groupby(CCM_CLUSTER.nodelist(), lambda n: n.data_center)] == node_counts:
            return True
    return False


def use_cluster(cluster_name, nodes, ipformat=None, start=True, workloads=[], set_keyspace=True, ccm_options=None,
                configuration_options={}, dse_cluster=False, dse_options={},
                dse_version=None):
    if (dse_version and not dse_cluster):
        raise ValueError('specified dse_version {} but not dse_cluster'.format(dse_version))
    set_default_cass_ip()

    if ccm_options is None and dse_cluster:
        ccm_options = {"version": dse_version or DSE_VERSION}
    elif ccm_options is None:
        ccm_options = CCM_KWARGS.copy()

    cassandra_version = ccm_options.get('version', CCM_VERSION)
    dse_version = ccm_options.get('version', DSE_VERSION)

    if 'version' in ccm_options:
        ccm_options['version'] = ccm_options['version'].base_version

    global CCM_CLUSTER
    if USE_CASS_EXTERNAL:
        if CCM_CLUSTER:
            log.debug("Using external CCM cluster {0}".format(CCM_CLUSTER.name))
        else:
            log.debug("Using unnamed external cluster")
        if set_keyspace and start:
            setup_keyspace(ipformat=ipformat, wait=False)
        return

    if is_current_cluster(cluster_name, nodes):
        log.debug("Using existing cluster, matching topology: {0}".format(cluster_name))
    else:
        if CCM_CLUSTER:
            log.debug("Stopping existing cluster, topology mismatch: {0}".format(CCM_CLUSTER.name))
            CCM_CLUSTER.stop()

        try:
            CCM_CLUSTER = CCMClusterFactory.load(path, cluster_name)
            log.debug("Found existing CCM cluster, {0}; clearing.".format(cluster_name))
            CCM_CLUSTER.clear()
            CCM_CLUSTER.set_install_dir(**ccm_options)
            CCM_CLUSTER.set_configuration_options(configuration_options)
        except Exception:
            ex_type, ex, tb = sys.exc_info()
            log.warning("{0}: {1} Backtrace: {2}".format(ex_type.__name__, ex, traceback.extract_tb(tb)))
            del tb

            ccm_options.update(cmd_line_args_to_dict('CCM_ARGS'))

            log.debug("Creating new CCM cluster, {0}, with args {1}".format(cluster_name, ccm_options))

            if dse_cluster:
                CCM_CLUSTER = DseCluster(path, cluster_name, **ccm_options)
                CCM_CLUSTER.set_configuration_options({'start_native_transport': True})
                CCM_CLUSTER.set_configuration_options({'batch_size_warn_threshold_in_kb': 5})
                if dse_version >= Version('5.0'):
                    CCM_CLUSTER.set_configuration_options({'enable_user_defined_functions': True})
                    CCM_CLUSTER.set_configuration_options({'enable_scripted_user_defined_functions': True})
                if 'spark' in workloads:
                    config_options = {"initial_spark_worker_resources": 0.1}
                    CCM_CLUSTER.set_dse_configuration_options(config_options)
                common.switch_cluster(path, cluster_name)
                CCM_CLUSTER.set_configuration_options(configuration_options)
                CCM_CLUSTER.populate(nodes, ipformat=ipformat)

                CCM_CLUSTER.set_dse_configuration_options(dse_options)
            else:
                CCM_CLUSTER = CCMCluster(path, cluster_name, **ccm_options)
                CCM_CLUSTER.set_configuration_options({'start_native_transport': True})
                if cassandra_version >= Version('2.2'):
                    CCM_CLUSTER.set_configuration_options({'enable_user_defined_functions': True})
                    if cassandra_version >= Version('3.0'):
                        CCM_CLUSTER.set_configuration_options({'enable_scripted_user_defined_functions': True})
                common.switch_cluster(path, cluster_name)
                CCM_CLUSTER.set_configuration_options(configuration_options)
                CCM_CLUSTER.populate(nodes, ipformat=ipformat)

    try:
        jvm_args = []
        # This will enable the Mirroring query handler which will echo our custom payload k,v pairs back

        if 'graph' not in workloads:
            if PROTOCOL_VERSION >= 4:
                jvm_args = [" -Dcassandra.custom_query_handler_class=org.apache.cassandra.cql3.CustomPayloadMirroringQueryHandler"]
        if(len(workloads) > 0):
            for node in CCM_CLUSTER.nodes.values():
                node.set_workloads(workloads)
        if start:
            log.debug("Starting CCM cluster: {0}".format(cluster_name))
            CCM_CLUSTER.start(wait_for_binary_proto=True, wait_other_notice=True, jvm_args=jvm_args)
            # Added to wait for slow nodes to start up
            for node in CCM_CLUSTER.nodes.values():
                wait_for_node_socket(node, 120)
            if set_keyspace:
                setup_keyspace(ipformat=ipformat)
    except Exception:
        log.exception("Failed to start CCM cluster; removing cluster.")

        if os.name == "nt":
            if CCM_CLUSTER:
                for node in six.itervalues(CCM_CLUSTER.nodes):
                    os.system("taskkill /F /PID " + str(node.pid))
        else:
            call(["pkill", "-9", "-f", ".ccm"])
        remove_cluster()
        raise
    return CCM_CLUSTER


def teardown_package():
    if USE_CASS_EXTERNAL or KEEP_TEST_CLUSTER:
        return
    # when multiple modules are run explicitly, this runs between them
    # need to make sure CCM_CLUSTER is properly cleared for that case
    remove_cluster()
    for cluster_name in [CLUSTER_NAME, MULTIDC_CLUSTER_NAME]:
        try:
            cluster = CCMClusterFactory.load(path, cluster_name)
            try:
                cluster.remove()
                log.info('Removed cluster: %s' % cluster_name)
            except Exception:
                log.exception('Failed to remove cluster: %s' % cluster_name)

        except Exception:
            log.warning('Did not find cluster: %s' % cluster_name)


def execute_until_pass(session, query):
    tries = 0
    while tries < 100:
        try:
            return session.execute(query)
        except (ConfigurationException, AlreadyExists, InvalidRequest):
            log.warning("Received already exists from query {0}   not exiting".format(query))
            # keyspace/table was already created/dropped
            return
        except (OperationTimedOut, ReadTimeout, ReadFailure, WriteTimeout, WriteFailure):
            ex_type, ex, tb = sys.exc_info()
            log.warning("{0}: {1} Backtrace: {2}".format(ex_type.__name__, ex, traceback.extract_tb(tb)))
            del tb
            tries += 1

    raise RuntimeError("Failed to execute query after 100 attempts: {0}".format(query))


def execute_with_long_wait_retry(session, query, timeout=30):
    tries = 0
    while tries < 10:
        try:
            return session.execute(query, timeout=timeout)
        except (ConfigurationException, AlreadyExists):
            log.warning("Received already exists from query {0}    not exiting".format(query))
            # keyspace/table was already created/dropped
            return
        except (OperationTimedOut, ReadTimeout, ReadFailure, WriteTimeout, WriteFailure):
            ex_type, ex, tb = sys.exc_info()
            log.warning("{0}: {1} Backtrace: {2}".format(ex_type.__name__, ex, traceback.extract_tb(tb)))
            del tb
            tries += 1

    raise RuntimeError("Failed to execute query after 100 attempts: {0}".format(query))


def execute_with_retry_tolerant(session, query, retry_exceptions, escape_exception):
    # TODO refactor above methods into this one for code reuse
    tries = 0
    while tries < 100:
        try:
            tries += 1
            rs = session.execute(query)
            return rs
        except escape_exception:
            return
        except retry_exceptions:
            time.sleep(.1)

    raise RuntimeError("Failed to execute query after 100 attempts: {0}".format(query))


def drop_keyspace_shutdown_cluster(keyspace_name, session, cluster):
    try:
        execute_with_long_wait_retry(session, "DROP KEYSPACE {0}".format(keyspace_name))
    except:
        log.warning("Error encountered when droping keyspace {0}".format(keyspace_name))
        ex_type, ex, tb = sys.exc_info()
        log.warning("{0}: {1} Backtrace: {2}".format(ex_type.__name__, ex, traceback.extract_tb(tb)))
        del tb
    finally:
        log.warning("Shutting down cluster")
        cluster.shutdown()


def setup_keyspace(ipformat=None, wait=True):
    # wait for nodes to startup
    if wait:
        time.sleep(10)

    if not ipformat:
        cluster = Cluster(protocol_version=PROTOCOL_VERSION)
    else:
        cluster = Cluster(contact_points=["::1"], protocol_version=PROTOCOL_VERSION)
    session = cluster.connect()

    try:
        for ksname in ('test1rf', 'test2rf', 'test3rf'):
            if ksname in cluster.metadata.keyspaces:
                execute_until_pass(session, "DROP KEYSPACE %s" % ksname)

        ddl = '''
            CREATE KEYSPACE test3rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '3'}'''
        execute_with_long_wait_retry(session, ddl)

        ddl = '''
            CREATE KEYSPACE test2rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '2'}'''
        execute_with_long_wait_retry(session, ddl)

        ddl = '''
            CREATE KEYSPACE test1rf
            WITH replication = {'class': 'SimpleStrategy', 'replication_factor': '1'}'''
        execute_with_long_wait_retry(session, ddl)

        ddl_3f = '''
            CREATE TABLE test3rf.test (
                k int PRIMARY KEY,
                v int )'''
        execute_with_long_wait_retry(session, ddl_3f)

        ddl_1f = '''
                    CREATE TABLE test1rf.test (
                        k int PRIMARY KEY,
                        v int )'''
        execute_with_long_wait_retry(session, ddl_1f)

    except Exception:
        traceback.print_exc()
        raise
    finally:
        cluster.shutdown()


class UpDownWaiter(object):

    def __init__(self, host):
        self.down_event = Event()
        self.up_event = Event()
        host.monitor.register(self)

    def on_up(self, host):
        self.up_event.set()

    def on_down(self, host):
        self.down_event.set()

    def wait_for_down(self):
        self.down_event.wait()

    def wait_for_up(self):
        self.up_event.wait()


class BasicKeyspaceUnitTestCase(unittest.TestCase):
    """
    This is basic unit test case that provides various utility methods that can be leveraged for testcase setup and tear
    down
    """
    @property
    def keyspace_name(self):
        return self.ks_name

    @property
    def class_table_name(self):
        return self.ks_name

    @property
    def function_table_name(self):
        return self._testMethodName.lower()

    @property
    def keyspace_table_name(self):
        return "{0}.{1}".format(self.keyspace_name, self._testMethodName.lower())

    @classmethod
    def drop_keyspace(cls):
        execute_with_long_wait_retry(cls.session, "DROP KEYSPACE {0}".format(cls.ks_name))

    @classmethod
    def create_keyspace(cls, rf):
        ddl = "CREATE KEYSPACE {0} WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': '{1}'}}".format(cls.ks_name, rf)
        execute_with_long_wait_retry(cls.session, ddl)

    @classmethod
    def common_setup(cls, rf, keyspace_creation=True, create_class_table=False, metrics=False):
        cls.cluster = Cluster(protocol_version=PROTOCOL_VERSION, metrics_enabled=metrics)
        cls.session = cls.cluster.connect(wait_for_all_pools=True)
        cls.ks_name = cls.__name__.lower()
        if keyspace_creation:
            cls.create_keyspace(rf)
        cls.cass_version, cls.cql_version = get_server_versions()

        if create_class_table:

            ddl = '''
                CREATE TABLE {0}.{1} (
                    k int PRIMARY KEY,
                    v int )'''.format(cls.ks_name, cls.ks_name)
            execute_until_pass(cls.session, ddl)

    def create_function_table(self):
            ddl = '''
                CREATE TABLE {0}.{1} (
                    k int PRIMARY KEY,
                    v int )'''.format(self.keyspace_name, self.function_table_name)
            execute_until_pass(self.session, ddl)

    def drop_function_table(self):
            ddl = "DROP TABLE {0}.{1} ".format(self.keyspace_name, self.function_table_name)
            execute_until_pass(self.session, ddl)


class MockLoggingHandler(logging.Handler):
    """Mock logging handler to check for expected logs."""

    def __init__(self, *args, **kwargs):
        self.reset()
        logging.Handler.__init__(self, *args, **kwargs)

    def emit(self, record):
        self.messages[record.levelname.lower()].append(record.getMessage())

    def reset(self):
        self.messages = {
            'debug': [],
            'info': [],
            'warning': [],
            'error': [],
            'critical': [],
        }

    def get_message_count(self, level, sub_string):
        count = 0
        for msg in self.messages.get(level):
            if sub_string in msg:
                count+=1
        return count

    def set_module_name(self, module_name):
        """
        This is intended to be used doing:
        with MockLoggingHandler().set_module_name(connection.__name__) as mock_handler:
        """
        self.module_name = module_name
        return self

    def __enter__(self):
        self.logger = logging.getLogger(self.module_name)
        self.logger.addHandler(self)
        return self

    def __exit__(self, *args):
        pass


class BasicExistingKeyspaceUnitTestCase(BasicKeyspaceUnitTestCase):
    """
    This is basic unit test defines class level teardown and setup methods. It assumes that keyspace is already defined, or created as part of the test.
    """
    @classmethod
    def setUpClass(cls):
        cls.common_setup(1, keyspace_creation=False)

    @classmethod
    def tearDownClass(cls):
        cls.cluster.shutdown()


class BasicSharedKeyspaceUnitTestCase(BasicKeyspaceUnitTestCase):
    """
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the testclass with a rf of 1.
    """
    @classmethod
    def setUpClass(cls):
        cls.common_setup(1)

    @classmethod
    def tearDownClass(cls):
        drop_keyspace_shutdown_cluster(cls.ks_name, cls.session, cls.cluster)


class BasicSharedKeyspaceUnitTestCaseRF1(BasicSharedKeyspaceUnitTestCase):
    """
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the testclass with a rf of 1
    """
    @classmethod
    def setUpClass(self):
        self.common_setup(1, True)


class BasicSharedKeyspaceUnitTestCaseRF2(BasicSharedKeyspaceUnitTestCase):
    """
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the test class with a rf of 2, and a table named after the class
    """
    @classmethod
    def setUpClass(self):
        self.common_setup(2)


class BasicSharedKeyspaceUnitTestCaseRF3(BasicSharedKeyspaceUnitTestCase):
    """
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the test class with a rf of 3
    """
    @classmethod
    def setUpClass(self):
        self.common_setup(3)


class BasicSharedKeyspaceUnitTestCaseRF3WM(BasicSharedKeyspaceUnitTestCase):
    """
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the test class with a rf of 3 with metrics enabled
    """
    @classmethod
    def setUpClass(self):
        self.common_setup(3, True, True, True)


class BasicSharedKeyspaceUnitTestCaseWFunctionTable(BasicSharedKeyspaceUnitTestCase):
    """"
    This is basic unit test case that can be leveraged to scope a keyspace to a specific test class.
    creates a keyspace named after the test class with a rf of 3 and a table named after the class
    the table is scoped to just the unit test and will be removed.

    """
    def setUp(self):
        self.create_function_table()

    def tearDown(self):
        self.drop_function_table()


class BasicSegregatedKeyspaceUnitTestCase(BasicKeyspaceUnitTestCase):
    """
    This unit test will create and teardown a keyspace for each individual unit tests.
    It has overhead and should only be used with complex unit test were sharing a keyspace will
    cause issues.
    """
    def setUp(self):
        self.common_setup(1)

    def tearDown(self):
        drop_keyspace_shutdown_cluster(self.ks_name, self.session, self.cluster)


class BasicExistingSegregatedKeyspaceUnitTestCase(BasicKeyspaceUnitTestCase):
    """
    This unit test will create and teardown or each individual unit tests. It assumes that keyspace is existing
    or created as part of a test.
    It has some overhead and should only be used when sharing cluster/session is not feasible.
    """
    def setUp(self):
        self.common_setup(1, keyspace_creation=False)

    def tearDown(self):
        self.cluster.shutdown()
