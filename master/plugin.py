"""plugin.py

Usage:
  plugin.py [options] endpoint
  plugin.py [options] network

Options:
    --log-dir=DIR      Log directory [default: /var/log/calico]

"""
import json
import logging
import logging.handlers
import sys
import time
import zmq
from docopt import docopt

import etcd
client = etcd.Client()

zmq_context = zmq.Context()
log = logging.getLogger(__name__)


def setup_logging(logfile):
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s %(lineno)d: %(message)s')
    handler = logging.StreamHandler(sys.stdout)
    # handler.setLevel(logging.ERROR)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    log.addHandler(handler)
    handler = logging.handlers.TimedRotatingFileHandler(logfile,
                                                        when='D',
                                                        backupCount=10)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    log.addHandler(handler)


# class Endpoint:
#     """
#     Endpoint as seen by the plugin. Enough to know what to put in an endpoint created message.
#     """
#
#     def __init__(self, id, mac, ip, group):
#         self.id = id
#         self.mac = mac
#         self.ip = ip
#         self.group = group


# Global variables for system state. These will be set up in load_data.
eps_by_host = {}
all_groups = {}
last_resync = {}


def strip(data):
    # Remove all from the first dot onwards
    index = data.find(".")
    if index > 0:
        data = data[0:index]
    return data




def load_data():
    """
    Load data from datastore - ccurently just etcd
    """
    # Clear all of the data structures
    log.info("Clearing data structures for full resync")
    eps_by_host.clear()
    all_groups.clear()

    result = client.read('/calico/host', recursive=True)

    # Iterate over all the leaves that we get back. For each leave we get the full path,
    # so we parse that to get the host and endpoint_ids
    # The goal of this iteration is to get the data into a simple Python data structure,
    # as opposed to the slightly complicated etcd datastructure.
    for res in result.leaves:
        log.debug("Processing key %s", res.key)
        keyparts = res.key.split("/")
        if len(keyparts) < 7:
            log.debug("Skipping non-endpoint related key")
            continue
        host = keyparts[3]
        endpoint_id = keyparts[7]
        key = keyparts[-1]

        # Container ID is currently unused.
        # container_id = keyparts[6]

        # Make sure the parent dicts are created since Python has no autovivification.
        if not host in eps_by_host:
            eps_by_host[host] = {}
        if not endpoint_id in eps_by_host[host]:
            eps_by_host[host][endpoint_id] = {}

        # Try to parse JSON out into a python datastructure, so that when we serialize it back for
        # zeroMQ we're not doing JSON in JSON.
        try:
            eps_by_host[host][endpoint_id][key] = json.loads(res.value)
            log.debug("Converted key: %s from JSON", key)
        except ValueError:
            eps_by_host[host][endpoint_id][key] = res.value

    # Build up the list of sections.
    # Endpoint. Note that we just fall over if there are missing lines.
    # group = items.get('group', 'default')

    # Put this endpoint in a group.
    # if group not in all_groups:
    #     all_groups[group] = dict()
    #
    # all_groups[group][id] = [ip]
    #
    # # Remove anything after the dot in host.
    # host = strip(items['host'])



    # log.debug("  Found configured endpoint %s (host=%s, mac=%s, ip=%s, group=%s)" %
    #           (id, host, mac, ip, group))
    # # elif section.lower().startswith("felix"):
    #     ip = items['ip']
    #     host = strip(items['host'])
    #     felix_ip[host] = ip
    #     log.debug("  Found configured Felix %s at %s" % (host, ip))


def do_ep_api():
    # Create the EP REP socket
    resync_socket = zmq_context.socket(zmq.REP)
    resync_socket.bind("tcp://*:9901")
    resync_socket.SNDTIMEO = 10000
    resync_socket.RCVTIMEO = 10000
    log.debug("Created EP socket for resync")

    # We create an EP REQ socket each time we get a connection from another
    # host.
    create_sockets = {}

    # Wait for a resync request, and send the response. Note that Felix is
    # expected to just send us a resync every now and then; it will do this 
    # because it keeps timing out our connections.                          
    while True:
        try:
            data = resync_socket.recv()
            fields = json.loads(data)
            log.debug("Got %s EP msg : %s" % (fields['type'], fields))
        except zmq.error.Again:
            # No data received after timeout.
            fields = {'type': ""}

        # Reload config files.
        load_data()

        if fields['type'] == "RESYNCSTATE":
            resync_id = fields['resync_id']
            host = strip(fields['hostname'])
            rsp = {"rc": "SUCCESS",
                   "message": "Hooray",
                   "type": fields['type'],
                   "endpoint_count": str(len(eps_by_host.get(host, set())))}
            resync_socket.send(json.dumps(rsp))
            log.debug("Sending %s EP msg : %s" % (fields['type'], rsp))
            last_resync[host] = int(time.time())

            send_all_eps(create_sockets, host, resync_id)

        elif fields['type'] == "HEARTBEAT":
            # Keepalive. We are still here.
            rsp = {"rc": "SUCCESS", "message": "Hooray", "type": fields['type']}
            resync_socket.send(json.dumps(rsp))


        # Send a keepalive on each EP REQ socket.
        for host in create_sockets.keys():
            last_time = last_resync.get(host, 0)
            log.debug("Last resync from %s was at %d", host, last_time)
            if time.time() - last_time > 15:
                log.error("Host %s has not sent a resync - "
                          "send lots of ENDPOINTCREATEDs to make sure", host)
                send_all_eps(create_sockets, host, None)
                last_resync[host] = int(time.time())
            else:
                create_socket = create_sockets[host]
                msg = {"type": "HEARTBEAT",
                       "issued": int(time.time() * 1000)}
                log.debug("Sending KEEPALIVE to %s : %s" % (host, msg))
                create_socket.send(json.dumps(msg))
                create_socket.recv()
                log.debug("Got response from host %s" % host)


def send_all_eps(create_sockets, host, resync_id):
    create_socket = create_sockets.get(host)

    if create_socket is None:
        create_socket = zmq_context.socket(zmq.REQ)
        create_socket.SNDTIMEO = 10000
        create_socket.RCVTIMEO = 10000
        create_socket.connect("tcp://%s:9902" % host)
        create_sockets[host] = create_socket

    # Send all of the ENDPOINTCREATED messages.
    for ep in eps_by_host.get(host, {}):
        msg = {"type": "ENDPOINTCREATED",
               "mac": eps_by_host[host][ep]["mac"],
               "endpoint_id": ep,
               "resync_id": resync_id,
               "issued": int(time.time() * 1000),
               "state": "enabled",
               "addrs": eps_by_host[host][ep]["addrs"]}
        log.debug("Sending ENDPOINTCREATED to %s : %s" % (host, msg))
        create_socket.send(json.dumps(msg))
        create_socket.recv()
        log.debug("Got endpoint created response")


def do_network_api():
    # Create the sockets
    rep_socket = zmq_context.socket(zmq.REP)
    rep_socket.bind("tcp://*:9903")
    rep_socket.RCVTIMEO = 15000

    pub_socket = zmq_context.socket(zmq.PUB)
    pub_socket.bind("tcp://*:9904")

    while True:
        # We just hang around waiting until we get a request for all
        # groups. If we do not get one within 15 seconds, we just send the  
        # data anyway. If we never receive anything (even a keepalive)      
        # we'll never send anything but that doesn't matter; if the ACL     
        # manager is there it will be sending either GETGROUPS or           
        # HEARTBEATs.                                                       
        try:
            data = rep_socket.recv()
            fields = json.loads(data)
            log.debug("Got %s network msg : %s" % (fields['type'], fields))
            if fields['type'] == "GETGROUPS":
                rsp = {"rc": "SUCCESS",
                       "message": "Hooray",
                       "type": fields['type']}
                rep_socket.send(json.dumps(rsp))
                got_groups = True
            else:
                # Heartbeat. Whatever.
                rsp = {"rc": "SUCCESS", "message": "Hooray", "type": fields['type']}
                rep_socket.send(json.dumps(rsp))

        except zmq.error.Again:
            # Timeout - press on.
            log.debug("No data received")

        # Reload config file just in case, before we send all the data.
        load_data()

        # Now send all the data we have on the PUB socket.
        log.debug("Build data to publish")

        if not all_groups:
            # No groups to send; send a keepalive instead so ACL Manager
            # doesn't think we have gone away.
            msg = {"type": "HEARTBEAT",
                   "issued": int(time.time() * 1000)}
            log.debug("Sending network heartbeat %s", msg)
            pub_socket.send_multipart(['networkheartbeat'.encode('utf-8'),
                                       json.dumps(msg).encode('utf-8')])

        for group in all_groups:
            members = all_groups[group]

            rules = dict()

            rule1 = {"group": group,
                     "cidr": None,
                     "protocol": None,
                     "port": None}

            rule2 = {"group": None,
                     "cidr": "0.0.0.0/0",
                     "protocol": None,
                     "port": None}

            rules["inbound"] = [rule1]
            rules["outbound"] = [rule1, rule2]
            rules["inbound_default"] = "deny"
            rules["outbound_default"] = "deny"

            data = {"type": "GROUPUPDATE",
                    "group": group,
                    "rules": rules,  # all outbound, inbound from group
                    "members": members,  # all endpoints
                    "issued": int(time.time() * 1000)}

            # Send the data to the ACL manager.
            log.debug("Sending data about group %s : %s" % (group, data))
            pub_socket.send_multipart(['groups'.encode('utf-8'),
                                       json.dumps(data).encode('utf-8')])

if __name__ == '__main__':
    arguments = docopt(__doc__)
    load_data()

    if arguments["endpoint"]:
        setup_logging("%s/plugin_ep.log" % arguments["--log-dir"])
        print eps_by_host
        do_ep_api()
    if arguments["network"]:
        setup_logging("%s/plugin_net.log" % arguments["--log-dir"])
        do_network_api()
