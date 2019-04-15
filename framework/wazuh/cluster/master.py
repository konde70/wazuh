# Copyright (C) 2015-2019, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is a free software; you can redistribute it and/or modify it under the terms of GPLv2
import asyncio
import json
import random
import re
import shutil
from calendar import timegm
from datetime import datetime
import functools
import operator
import os
from typing import Tuple, Dict, Callable
import fcntl
from wazuh.agent import Agent
from wazuh.cluster import server, cluster, common as c_common
from wazuh import cluster as metadata
from wazuh import common, utils, exception
from wazuh.cluster.dapi import dapi


class ReceiveIntegrityTask(c_common.ReceiveFileTask):
    """
    Defines the process and variables necessary to receive and process integrity information from the master.

    This task is created by the master when the worker starts sending its integrity file checksums and it's destroyed
    by the master once the necessary files to update have been sent.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger_tag = "Integrity"

    def set_up_coro(self) -> Callable:
        return self.wazuh_common.sync_integrity

    def done_callback(self, future=None):
        super().done_callback(future)
        self.wazuh_common.sync_integrity_free = True


class ReceiveAgentInfoTask(c_common.ReceiveFileTask):
    """
    Defines the process and variables necessary to receive and process agent info files.

    This task is created by the master when the worker starts sending its agent-info files and its destroyed once the
    master has updated its agent-info files.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger_tag = "Agent info"

    def set_up_coro(self) -> Callable:
        return self.wazuh_common.sync_agent_info

    def done_callback(self, future=None):
        super().done_callback(future)
        self.wazuh_common.sync_agent_info_free = True


class ReceiveExtraValidTask(c_common.ReceiveFileTask):
    """
    Defines the process and variables necessary to receive and process extra valid files from the worker.

    This task is created when the worker starts sending extra valid files and its destroyed once the master has updated
    all the required information
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger_tag = "Extra valid"

    def set_up_coro(self) -> Callable:
        return self.wazuh_common.sync_extra_valid

    def done_callback(self, future=None):
        super().done_callback(future)
        self.wazuh_common.sync_extra_valid_free = True


class MasterHandler(server.AbstractServerHandler, c_common.WazuhCommon):

    def __init__(self, **kwargs):
        super().__init__(**kwargs, tag="Worker")
        # sync status variables. Used to prevent sync process from overlapping.
        self.sync_integrity_free = True  # the worker isn't currently synchronizing integrity
        self.sync_extra_valid_free = True
        self.sync_agent_info_free = True
        # sync status variables. Used in cluster_control -i and GET/cluster/healthcheck
        self.sync_integrity_status = {'date_start_master': "n/a", 'date_end_master': "n/a",
                                      'total_files': {'missing': 0, 'shared': 0, 'extra': 0, 'extra_valid': 0}}
        self.sync_agent_info_status = {'date_start_master': "n/a", 'date_end_master': "n/a",
                                       'total_agentinfo': 0}
        self.sync_extra_valid_status = {'date_start_master': "n/a", 'date_end_master': "n/a",
                                        'total_agentgroups': 0}
        # variables which will be filled when the worker sends the hello request
        self.version = ""
        self.cluster_name = ""
        self.node_type = ""
        # dictionary to save loggers for each sync task
        self.task_loggers = {}

    def to_dict(self):
        return {'info': {'name': self.name, 'type': self.node_type, 'version': self.version, 'ip': self.ip},
                'status': {'sync_integrity_free': self.sync_integrity_free, 'last_sync_integrity': self.sync_integrity_status,
                           'sync_agentinfo_free': self.sync_agent_info_free, 'last_sync_agentinfo': self.sync_agent_info_status,
                           'sync_extravalid_free': self.sync_extra_valid_free, 'last_sync_agentgroups': self.sync_extra_valid_status,
                           'last_keep_alive': self.last_keepalive}}

    def process_request(self, command: bytes, data: bytes) -> Tuple[bytes, bytes]:
        """
        Defines available commands to receive in master nodes

        :param command: command to receive
        :param data: payload
        :return: response command and payload
        """
        self.logger.debug("Command received: {}".format(command))
        if command == b'sync_i_w_m_p' or command == b'sync_e_w_m_p' or command == b'sync_a_w_m_p':
            return self.get_permission(command)
        elif command == b'sync_i_w_m' or command == b'sync_e_w_m' or command == b'sync_a_w_m':
            return self.setup_sync_integrity(command)
        elif command == b'sync_i_w_m_e' or command == b'sync_e_w_m_e' or command == b'sync_a_w_m_e':
            return self.end_receiving_integrity_checksums(data.decode())
        elif command == b'sync_i_w_m_r' or command == b'sync_e_w_m_r' or command == b'sync_a_w_m_r':
            return self.process_sync_error_from_worker(command, data)
        elif command == b'dapi':
            self.server.dapi.add_request(self.name.encode() + b'*' + data)
            return b'ok', b'Added request to API requests queue'
        elif command == b'dapi_res':
            return self.process_dapi_res(data)
        elif command == b'dapi_err':
            dapi_client, error_msg = data.split(b' ', 1)
            asyncio.create_task(self.server.local_server.clients[dapi_client.decode()].send_request(command, error_msg))
            return b'ok', b'DAPI error forwarded to worker'
        elif command == b'get_nodes':
            cmd, res = self.get_nodes(json.loads(data))
            return cmd, json.dumps(res).encode()
        elif command == b'get_health':
            cmd, res = self.get_health(json.loads(data))
            return cmd, json.dumps(res).encode()
        else:
            return super().process_request(command, data)

    async def execute(self, command: bytes, data: bytes, wait_for_complete: bool) -> Dict:
        """
        Sends a distributed API request and wait for a response in command dapi_res

        :param command: Command to execute
        :param data: Data to send
        :param wait_for_complete: Raise a timeout exception or not
        :return: A dictionary with the API response
        """
        request_id = str(random.randint(0, 2**10 - 1))
        self.server.pending_api_requests[request_id] = {'Event': asyncio.Event(), 'Response': ''}
        if command == b'dapi_forward':
            client, request = data.split(b' ', 1)
            client = client.decode()
            if client == 'fw_all_nodes':
                for worker in self.server.clients.values():
                    result = (await worker.send_request(b'dapi', request_id.encode() + b' ' + request)).decode()
            elif client in self.server.clients:
                result = (await self.server.clients[client].send_request(b'dapi', request_id.encode() + b' ' + request)).decode()
            else:
                raise exception.WazuhException(3022, client)
        else:
            result = (await self.send_request(b'dapi', request_id.encode() + b' ' + data)).decode()

        if command == b'dapi' or command == b'dapi_forward':
            try:
                timeout = None if wait_for_complete \
                               else self.cluster_items['intervals']['communication']['timeout_api_request']
                await asyncio.wait_for(self.server.pending_api_requests[request_id]['Event'].wait(), timeout=timeout)
                request_result = json.loads(self.server.pending_api_requests[request_id]['Response'])
            except asyncio.TimeoutError:
                raise exception.WazuhClusterError(3021)
        else:
            request_result = json.loads(result, cls=c_common.as_wazuh_object)
        return request_result

    def hello(self, data: bytes) -> Tuple[bytes, bytes]:
        """
        Processes "hello" command sent by a worker right after it connects to the server. It also initializes
        the task loggers.

        :param data: Node name, cluster name, node type and wazuh version all separated by spaces.
        :return: response command and payload.
        """
        name, cluster_name, node_type, version = data.split(b' ')
        cmd, payload = super().hello(name)

        self.task_loggers = {'Integrity': self.setup_task_logger('Integrity'),
                             'Extra valid': self.setup_task_logger('Extra valid'),
                             'Agent info': self.setup_task_logger('Agent info')}

        self.version, self.cluster_name, self.node_type = version.decode(), cluster_name.decode(), node_type.decode()

        if self.cluster_name != self.server.configuration['name']:
            raise exception.WazuhClusterError(3030)
        elif self.version != metadata.__version__:
            raise exception.WazuhClusterError(3031)

        worker_dir = '{}/queue/cluster/{}'.format(common.ossec_path, self.name)
        if cmd == b'ok' and not os.path.exists(worker_dir):
            utils.mkdir_with_mode(worker_dir)
        return cmd, payload

    def get_manager(self) -> server.AbstractServer:
        """
        Returns the master object
        """
        return self.server

    def process_dapi_res(self, data: bytes) -> Tuple[bytes, bytes]:
        """
        Processes a DAPI response coming from a worker node.
        :param data: Response JSON data
        :return: confirmation message
        """
        req_id, string_id = data.split(b' ', 1)
        req_id = req_id.decode()
        if req_id in self.server.pending_api_requests:
            self.server.pending_api_requests[req_id]['Response'] = self.in_str[string_id].payload.decode()
            self.server.pending_api_requests[req_id]['Event'].set()
            return b'ok', b'Forwarded response'
        elif req_id in self.server.local_server.clients:
            asyncio.create_task(self.forward_dapi_response(data))
            return b'ok', b'Response forwarded to worker'
        else:
            raise exception.WazuhClusterError(3032, req_id)

    def get_nodes(self, arguments: Dict) -> Tuple[bytes, Dict]:
        """
        Processes get_nodes request.
        :param arguments: Arguments to use in get_connected_nodes function (filter, sort, etc)
        :return: a JSON object containing all nodes information
        """
        return b'ok', self.server.get_connected_nodes(**arguments)

    def get_health(self, filter_nodes: Dict) -> Tuple[bytes, Dict]:
        """
        Processes get_health request.
        :param filter_nodes: Whether to filter by a node or return all health information
        :return: Health information in a JSON object
        """
        return b'ok', self.server.get_health(filter_nodes)

    def get_permission(self, sync_type: bytes) -> Tuple[bytes, bytes]:
        """
        Gets whether a sync process is in progress or not
        :param sync_type: sync process to check
        :return: True if it's free and False if it's in progress
        """
        if sync_type == b'sync_i_w_m_p':
            permission = self.sync_integrity_free
        elif sync_type == b'sync_e_w_m_p':
            permission = self.sync_extra_valid_free
        elif sync_type == b'sync_a_w_m_p':
            permission = self.sync_agent_info_free
        else:
            permission = False

        return b'ok', str(permission).encode()

    def setup_sync_integrity(self, sync_type: bytes) -> Tuple[bytes, bytes]:
        """
        Starts synchronization process.
        :param sync_type: Sync process to start
        :return: confirmation message
        """
        if sync_type == b'sync_i_w_m':
            self.sync_integrity_free, sync_function = False, ReceiveIntegrityTask
        elif sync_type == b'sync_e_w_m':
            self.sync_extra_valid_free, sync_function = False, ReceiveExtraValidTask
        elif sync_type == b'sync_a_w_m':
            self.sync_agent_info_free, sync_function = False, ReceiveAgentInfoTask
        else:
            sync_function = None

        return super().setup_receive_file(sync_function)

    def process_sync_error_from_worker(self, command: bytes, error_msg: bytes) -> Tuple[bytes, bytes]:
        """
        The worker reports an error during the synchronization process
        :param command: Specifies synchronization process where the error happened
        :param error_msg: JSON error information
        :return: Confirmation message
        """
        if command == b'sync_i_w_m_r':
            sync_type, self.sync_integrity_free = "Integrity", True
        elif command == b'sync_e_w_m_r':
            sync_type, self.sync_extra_valid_free = "Extra valid", True
        else:  # command == b'sync_a_w_m_r'
            sync_type, self.sync_agent_info_free = "Agent status", True

        return super().error_receiving_file(error_msg.decode())

    def end_receiving_integrity_checksums(self, task_and_file_names: str) -> Tuple[bytes, bytes]:
        """
        Finishes receiving a file and starts the function to process it
        :param task_and_file_names: Task ID awaiting the file and the filename separated by a space
        :return: confirmation message
        """
        return super().end_receiving_file(task_and_file_names)

    async def sync_worker_files(self, task_name: str, received_file: asyncio.Event, logger):
        """
        Waits until the master sends the files to update and then updates the necessary ones
        :param task_name: Task holding a lock while the files are not received
        :param received_file: Filename of the received file
        :param logger: logger to use (can't use self since we'll use one of the task loggers)
        :return: None
        """
        logger.info("Waiting to receive zip file from worker")
        await asyncio.wait_for(received_file.wait(),
                               timeout=self.cluster_items['intervals']['communication']['timeout_receiving_file'])

        received_filename = self.sync_tasks[task_name].filename
        if isinstance(received_filename, Exception):
            raise received_filename

        logger.debug("Received file from worker: '{}'".format(received_filename))

        files_checksums, decompressed_files_path = cluster.decompress_files(received_filename)
        logger.info("Analyzing worker files: Received {} files to check.".format(len(files_checksums)))
        self.process_files_from_worker(files_checksums, decompressed_files_path, logger)

    async def sync_extra_valid(self, task_name: str, received_file: asyncio.Event):
        """
        Function called to do the extra valid sync process.
        It sets up necessary parameters for sync_worker_files function.
        :param task_name: Task name in charge of doing the sync process
        :param received_file: Received filename containing information to sync
        :return: None
        """
        extra_valid_logger = self.task_loggers['Extra valid']
        self.sync_extra_valid_status['date_start_master'] = str(datetime.now())
        await self.sync_worker_files(task_name, received_file, extra_valid_logger)
        self.sync_extra_valid_free = True
        self.sync_extra_valid_status['date_end_master'] = str(datetime.now())

    async def sync_agent_info(self, task_name: str, received_file: asyncio.Event):
        """
        Function called to do the agent info sync process.
        It sets up necessary parameters for sync_worker_files function.
        :param task_name: Task name in charge of doing the sync process
        :param received_file: Received filename containing information to sync
        :return: None
        """
        agent_info_logger = self.task_loggers['Agent info']
        self.sync_agent_info_status['date_start_master'] = str(datetime.now())
        await self.sync_worker_files(task_name, received_file, agent_info_logger)
        self.sync_agent_info_free = True
        self.sync_agent_info_status['date_end_master'] = str(datetime.now())

    async def sync_integrity(self, task_name: str, received_file: asyncio.Event):
        """
        Function called to do the integrity sync process.
        It waits until the worker sends its integrity checksums and then it sends the necessary files to update.
        :param task_name: Task name in charge of doing the sync process
        :param received_file: Received filename containing information to sync
        :return: None
        """
        logger = self.task_loggers['Integrity']

        self.sync_integrity_status['date_start_master'] = str(datetime.now())

        logger.info("Waiting to receive zip file from worker")
        await asyncio.wait_for(received_file.wait(),
                               timeout=self.cluster_items['intervals']['communication']['timeout_receiving_file'])

        received_filename = self.sync_tasks[task_name].filename
        if isinstance(received_filename, Exception):
            raise received_filename

        logger.debug("Received file from worker: '{}'".format(received_filename))

        files_checksums, decompressed_files_path = cluster.decompress_files(received_filename)
        logger.info("Analyzing worker integrity: Received {} files to check.".format(len(files_checksums)))

        # classify files in shared, missing, extra and extra valid.
        worker_files_ko, counts = cluster.compare_files(self.server.integrity_control, files_checksums, self.name)

        # health check
        self.sync_integrity_status['total_files'] = counts
        shutil.rmtree(decompressed_files_path)

        if not functools.reduce(operator.add, map(len, worker_files_ko.values())):
            logger.info("Analyzing worker integrity: Files checked. There are no KO files.")
            result = await self.send_request(command=b'sync_m_c_ok', data=b'')
        else:
            logger.info("Analyzing worker integrity: Files checked. There are KO files.")

            # Compress data: master files (only KO shared and missing)
            logger.debug("Analyzing worker integrity: Files checked. Compressing KO files.")
            master_files_paths = worker_files_ko['shared'].keys() | worker_files_ko['missing'].keys()
            compressed_data = cluster.compress_files(self.name, master_files_paths, worker_files_ko)

            logger.info("Analyzing worker integrity: Files checked. KO files compressed.")
            try:
                task_name = await self.send_request(command=b'sync_m_c', data=b'')

                result = await self.send_file(compressed_data)
                result = await self.send_request(command=b'sync_m_c_e',
                                                 data=task_name + b' ' + os.path.relpath(
                                                     compressed_data, common.ossec_path).encode())
            except exception.WazuhException as e:
                self.logger.error(f"Error sending files information: {e}")
                result = await self.send_request(command=b'sync_m_c_r', data=task_name + b' ' +
                                                 json.dumps(e, cls=c_common.WazuhJSONEncoder).encode())
            except Exception as e:
                self.logger.error(f"Error sending files information: {e}")
                exc_info = json.dumps(exception.WazuhClusterError(code=1000, extra_message=str(e)),
                                      cls=c_common.WazuhJSONEncoder).encode()
                result = await self.send_request(command=b'sync_m_c_r', data=task_name + b' ' + exc_info)
            finally:
                os.unlink(compressed_data)

        self.sync_integrity_status['date_end_master'] = str(datetime.now())
        self.sync_integrity_free = True
        logger.info("Finished integrity synchronization.")
        return result

    def process_files_from_worker(self, files_checksums: Dict, decompressed_files_path: str, logger):
        """
        Updates files sent from the worker.
        :param files_checksums: Dictionary containing all files' metadata information
        :param decompressed_files_path: Directory where files have been decompressed
        :param logger: task logger
        :return: None
        """
        def update_file(name: str, data: Dict):
            """
            Updates a file from the worker. It checks the modification date to decide whether to update it or not.
            If it's a merged file, it unmerges it.
            :param name: Filename to update
            :param data: File metadata
            :return: None
            """
            # Full path
            full_path, error_updating_file, n_merged_files = common.ossec_path + name, False, 0

            # Cluster items information: write mode and permissions
            lock_full_path = "{}/queue/cluster/lockdir/{}.lock".format(common.ossec_path, os.path.basename(full_path))
            lock_file = open(lock_full_path, 'a+')
            try:
                fcntl.lockf(lock_file, fcntl.LOCK_EX)
                if os.path.basename(name) == 'client.keys':
                    self.logger.warning("Client.keys received in a master node")
                    raise exception.WazuhClusterError(3007)
                if data['merged']:
                    is_agent_info = data['merge_type'] == 'agent-info'
                    if is_agent_info:
                        self.sync_agent_info_status['total_agent_info'] = len(agent_ids)
                    else:
                        self.sync_extra_valid_status['total_extra_valid'] = len(agent_ids)
                    for file_path, file_data, file_time in cluster.unmerge_agent_info(data['merge_type'],
                                                                                      decompressed_files_path,
                                                                                      data['merge_name']):
                        try:
                            full_unmerged_name = common.ossec_path + file_path
                            tmp_unmerged_path = full_unmerged_name + '.tmp'
                            if is_agent_info:
                                agent_name_re = re.match(r'(^.+)-(.+)$', os.path.basename(file_path))
                                agent_name = agent_name_re.group(1) if agent_name_re else os.path.basename(file_path)
                                if agent_name not in agent_names:
                                    n_errors['warnings'][data['cluster_item_key']] = 1 \
                                        if n_errors['warnings'].get(data['cluster_item_key']) is None \
                                        else n_errors['warnings'][data['cluster_item_key']] + 1

                                    self.logger.debug2("Received status of an non-existent agent '{}'".format(agent_name))
                                    continue
                            else:
                                agent_id = os.path.basename(file_path)
                                if agent_id not in agent_ids:
                                    n_errors['warnings'][data['cluster_item_key']] = 1 \
                                        if n_errors['warnings'].get(data['cluster_item_key']) is None \
                                        else n_errors['warnings'][data['cluster_item_key']] + 1

                                    self.logger.debug2("Received group of an non-existent agent '{}'".format(agent_id))
                                    continue

                            try:
                                mtime = datetime.strptime(file_time, '%Y-%m-%d %H:%M:%S.%f')
                            except ValueError:
                                mtime = datetime.strptime(file_time, '%Y-%m-%d %H:%M:%S')

                            if os.path.isfile(full_unmerged_name):

                                local_mtime = datetime.utcfromtimestamp(int(os.stat(full_unmerged_name).st_mtime))
                                # check if the date is older than the manager's date
                                if local_mtime > mtime:
                                    logger.debug2("Receiving an old file ({})".format(file_path))
                                    return

                            with open(tmp_unmerged_path, 'wb') as f:
                                f.write(file_data)

                            mtime_epoch = timegm(mtime.timetuple())
                            os.utime(tmp_unmerged_path, (mtime_epoch, mtime_epoch))  # (atime, mtime)
                            os.chown(tmp_unmerged_path, common.ossec_uid, common.ossec_gid)
                            os.chmod(tmp_unmerged_path, self.cluster_items['files'][data['cluster_item_key']]['permissions'])
                            os.rename(tmp_unmerged_path, full_unmerged_name)
                        except Exception as e:
                            self.logger.debug2("Error updating agent group/status: {}".format(e))
                            if is_agent_info:
                                self.sync_agent_info_status['total_agent_info'] -= 1
                            else:
                                self.sync_extra_valid_status['total_extra_valid'] -= 1

                            n_errors['errors'][data['cluster_item_key']] = 1 \
                                if n_errors['errors'].get(data['cluster_item_key']) is None \
                                else n_errors['errors'][data['cluster_item_key']] + 1

                else:
                    zip_path = "{}{}".format(decompressed_files_path, name)
                    os.chown(zip_path, common.ossec_uid, common.ossec_gid)
                    os.chmod(zip_path, self.cluster_items['files'][data['cluster_item_key']]['permissions'])
                    os.rename(zip_path, full_path)

            except exception.WazuhException as e:
                logger.debug2("Warning updating file '{}': {}".format(name, e))
                error_tag = 'warnings'
                error_updating_file = True
            except Exception as e:
                logger.debug2("Error updating file '{}': {}".format(name, e))
                error_tag = 'errors'
                error_updating_file = True

            if error_updating_file:
                n_errors[error_tag][data['cluster_item_key']] = 1 if not n_errors[error_tag].get(
                    data['cluster_item_key']) \
                    else n_errors[error_tag][data['cluster_item_key']] + 1

            fcntl.lockf(lock_file, fcntl.LOCK_UN)
            lock_file.close()

        # tmp path
        tmp_path = "/queue/cluster/{}/tmp_files".format(self.name)
        n_merged_files = 0
        n_errors = {'errors': {}, 'warnings': {}}

        # create temporary directory for lock files
        lock_directory = "{}/queue/cluster/lockdir".format(common.ossec_path)
        if not os.path.exists(lock_directory):
            utils.mkdir_with_mode(lock_directory)

        try:
            agents = Agent.get_agents_overview(select=['name'], limit=None)['items']
            agent_names = set(map(operator.itemgetter('name'), agents))
            agent_ids = set(map(operator.itemgetter('id'), agents))
        except Exception as e:
            logger.debug2("Error getting agent ids and names: {}".format(e))
            agent_names, agent_ids = {}, {}

        try:
            for filename, data in files_checksums.items():
                update_file(data=data, name=filename)

            shutil.rmtree(decompressed_files_path)

        except Exception as e:
            self.logger.error("Error updating worker files: '{}'.".format(e))
            raise e

        if sum(n_errors['errors'].values()) > 0:
            logger.error("Errors updating worker files: {}".format(' | '.join(
                ['{}: {}'.format(key, value) for key, value
                 in n_errors['errors'].items()])
            ))
        if sum(n_errors['warnings'].values()) > 0:
            for key, value in n_errors['warnings'].items():
                if key == '/queue/agent-info/':
                    logger.debug2("Received {} agent statuses for non-existent agents. Skipping.".format(value))
                elif key == '/queue/agent-groups/':
                    logger.debug2("Received {} group assignments for non-existent agents. Skipping.".format(value))

    def get_logger(self, logger_tag: str = ''):
        """
        Returns a logger
        :param logger_tag: logger task to return. If empty, it will return main class logger.
        :return: A logger
        """
        if logger_tag == '' or logger_tag not in self.task_loggers:
            return self.logger
        else:
            return self.task_loggers[logger_tag]

    def connection_lost(self, exc: Exception):
        """
        Connection with the worker node has been lost
        :param exc: In case the connection was lost due to an exception, it will be available on this parameter
        :return: None
        """
        super().connection_lost(exc)
        # cancel all pending tasks
        self.logger.info("Cancelling pending tasks.")
        for pending_task in self.sync_tasks.values():
            pending_task.task.cancel()


class Master(server.AbstractServer):

    def __init__(self, **kwargs):
        super().__init__(**kwargs, tag="Master")
        self.integrity_control = {}
        self.tasks.append(self.file_status_update)
        self.handler_class = MasterHandler
        self.dapi = dapi.APIRequestQueue(server=self)
        self.tasks.append(self.dapi.run)
        # pending API requests waiting for a response
        self.pending_api_requests = {}

    def to_dict(self):
        return {'info': {'name': self.configuration['node_name'], 'type': self.configuration['node_type'],
                'version': metadata.__version__, 'ip': self.configuration['nodes'][0]}}

    async def file_status_update(self):
        file_integrity_logger = self.setup_task_logger("File integrity")
        while True:
            file_integrity_logger.debug("Calculating")
            try:
                self.integrity_control = cluster.get_files_status('master', self.configuration['node_name'])
            except Exception as e:
                file_integrity_logger.error("Error calculating file integrity: {}".format(e))
            file_integrity_logger.debug("Calculated.")

            await asyncio.sleep(self.cluster_items['intervals']['master']['recalculate_integrity'])

    def get_health(self, filter_node) -> Dict:
        """
        Return healthcheck data

        :param filter_node: Node to filter by
        :return: Dictionary
        """
        workers_info = {key: val.to_dict() for key, val in self.clients.items()
                        if filter_node is None or filter_node == {} or key in filter_node}
        n_connected_nodes = len(workers_info)
        if filter_node is None or self.configuration['node_name'] in filter_node:
            workers_info.update({self.configuration['node_name']: self.to_dict()})

        # Get active agents by node and format last keep alive date format
        for node_name in workers_info.keys():
            workers_info[node_name]["info"]["n_active_agents"] = Agent.get_agents_overview(filters={'status': 'Active', 'node_name': node_name})['totalItems']
            if workers_info[node_name]['info']['type'] != 'master':
                workers_info[node_name]['status']['last_keep_alive'] = str(
                    datetime.fromtimestamp(workers_info[node_name]['status']['last_keep_alive']))

        return {"n_connected_nodes": n_connected_nodes, "nodes": workers_info}

    def get_node(self) -> Dict:
        return {'type': self.configuration['node_type'], 'cluster': self.configuration['name'],
                'node': self.configuration['node_name']}
