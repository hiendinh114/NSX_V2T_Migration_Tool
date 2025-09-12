#!/usr/bin/env python3
# ******************************************************
# Copyright © 2020-2021 VMware, Inc. All rights reserved.
# ******************************************************

"""
Description: Module which performs the VMware Cloud Director NSX-V to NSX-T Migration Operations
"""

import operator
import ipaddress
import logging
import json
import re
import time
import os
import copy
import sys
import prettytable
import requests
import threading
import traceback
from collections import defaultdict
from itertools import zip_longest
from functools import reduce
import src.core.vcd.vcdConstants as vcdConstants
from src.commonUtils.utils import listify, urn_id
from src.core.vcd.vcdValidations import (
    isSessionExpired, description, remediate, remediate_threaded, getSession)
from src.core.vcd.vcdConfigureEdgeGatewayServices import ConfigureEdgeGatewayServices
from pkg_resources._vendor.packaging import version
logger = logging.getLogger('mainLogger')
endStateLogger = logging.getLogger("endstateLogger")


class VCloudDirectorOperations(ConfigureEdgeGatewayServices):
    """
    Description: Class that performs the VMware Cloud Director NSX-V to NSX-T Migration Operations
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.targetStorageProfileMap = dict()
        vcdConstants.VCD_API_HEADER = vcdConstants.VCD_API_HEADER.format(self.version)
        vcdConstants.GENERAL_JSON_ACCEPT_HEADER = vcdConstants.GENERAL_JSON_ACCEPT_HEADER.format(self.version)
        vcdConstants.OPEN_API_CONTENT_TYPE = vcdConstants.OPEN_API_CONTENT_TYPE.format(self.version)

    def _getEdgeGatewaySubnets(self):
        # getting details of ip ranges used in source edge gateways
        # Schema of return value edgeGatewaySubnetDict:
        # edgeGatewaySubnetDict = {
        #     target_ext_net_name : {
        #         network_address: {
        #             list(ip_ranges_of_edge_gateways)
        #         }
        #     }
        # }
        edgeGatewaySubnetDict = {}
        for edgeGateway in copy.deepcopy(self.rollback.apiData['sourceEdgeGateway']):
            extNet = self.orgVdcInput['EdgeGateways'][edgeGateway['name']]['Tier0Gateways']
            edgeGatewaySubnetDict.setdefault(extNet, defaultdict(list))
            for edgeGatewayUplink in edgeGateway['edgeGatewayUplinks']:
                for subnet in edgeGatewayUplink['subnets']['values']:
                    networkAddress = ipaddress.ip_network(
                        '{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                        strict=False)
                    if networkAddress in [ipaddress.ip_network('{}/{}'.format(subnetData[0], subnetData[1]), strict=False)
                                          for subnetData in self.rollback.apiData['isT0Connected'].get(edgeGateway['name'], {}).get(extNet, [])]:
                        edgeGatewaySubnetDict[extNet][networkAddress].extend(subnet['ipRanges']['values'])

                    # TODO pranshu: multiple T0 - this can be removed.
                    #  Check self.rollback.apiData['sourceEdgeGateway'] in older versions
                    # # adding primary ip to sub allocated ip pool
                    # primaryIp = subnet.get('primaryIp')
                    # if primaryIp and ipaddress.ip_address(primaryIp) in networkAddress:
                    #     edgeGatewaySubnetDict[extNet][networkAddress].extend([{
                    #         'startAddress': primaryIp, 'endAddress': primaryIp}])

        return edgeGatewaySubnetDict

    def _updateTargetExternalNetworkPool(self):
        # Acquiring lock as only one operation can be performed on an external network at a time
        self.lock.acquire(blocking=True)
        logger.debug("Updating Target External networks with sub allocated ip pools")

        edgeGatewaySubnetDict = self._getEdgeGatewaySubnets()

        for targetExtNetName, sourceEgwSubnets in edgeGatewaySubnetDict.items():
            logger.debug("Updating Target External network {} with sub allocated ip pools".format(targetExtNetName))
            targetExtNetData = self.getExternalNetworkByName(targetExtNetName)
            if targetExtNetData.get("usingIpSpace"):
                ipSpaces = self.getProviderGatewayIpSpaces(targetExtNetData)
                for edgeGatewaySubnet, edgeGatewayIpRangesList in sourceEgwSubnets.items():
                    for ipSpace in ipSpaces:
                        if [internalScope for internalScope in ipSpace["ipSpaceInternalScope"]
                            if type(edgeGatewaySubnet) == type(
                                ipaddress.ip_network('{}'.format(internalScope), strict=False)) and
                               self.subnetOf(edgeGatewaySubnet,
                                             ipaddress.ip_network('{}'.format(internalScope), strict=False))]:
                            # Adding IPs used by edge gateway from this subnet to IP Space ranges
                            self._prepareIpSpaceRanges(ipSpace, edgeGatewayIpRangesList)
                for ipSpace in ipSpaces:
                    url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.UPDATE_IP_SPACES.format(ipSpace["id"]))
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    response = self.restClientObj.put(url, self.headers, data=json.dumps(ipSpace))
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug("Provider Gateway IP Space uplink - '{}' updated successfully with sub allocated ip pools.".format(
                            ipSpace['name']))
                    else:
                        errorResponse = response.json()
                        raise Exception("Provider Gateway IP Space uplink - '{}' with sub allocated ip pools - {}".format(
                            ipSpace['name'], errorResponse['message']))
            else:
                for targetExtNetSubnet in targetExtNetData['subnets']['values']:
                    targetExtNetSubnetAddress = ipaddress.ip_network(
                        '{}/{}'.format(targetExtNetSubnet['gateway'], targetExtNetSubnet['prefixLength']), strict=False)
                    targetExtNetSubnet['ipRanges']['values'].extend(sourceEgwSubnets.get(targetExtNetSubnetAddress, []))

                url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.ALL_EXTERNAL_NETWORKS, targetExtNetData['id'])
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                response = self.restClientObj.put(url, self.headers, data=json.dumps(targetExtNetData))
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Target External network {} updated successfully with sub allocated ip pools.'.format(
                        targetExtNetData['name']))
                else:
                    errorResponse = response.json()
                    raise Exception('Failed to update External network {} with sub allocated ip pools - {}'.format(
                        targetExtNetData['name'], errorResponse['message']))

        # Releasing lock
        self.lock.release()

    def _prepareIpSpaceRanges(self, ipSpace, edgeGatewayIpRangesList, rollback=False):

        def _createIpList(start, end):
            '''Return IPs in range, inclusive.'''
            start_int = int(ipaddress.ip_address(start).packed.hex(), 16)
            end_int = int(ipaddress.ip_address(end).packed.hex(), 16)
            return [ipaddress.ip_address(ip) for ip in range(start_int, end_int + 1)]

        def _addIpsToIpSpaceRanges(ipList):
            for ip in ipList:
                for ipSpaceRange in ipSpace["ipSpaceRanges"]["ipRanges"]:
                    if ipaddress.ip_address(ipSpaceRange["startIpAddress"]) <= ip <= ipaddress.ip_address(
                            ipSpaceRange["endIpAddress"]):
                        break
                else:
                    ipSpace["ipSpaceRanges"]["ipRanges"].append({
                        "id": None,
                        "startIpAddress": ip.exploded,
                        "endIpAddress": ip.exploded
                    })

        def _removeIpsFromIpSpaceRanges(ipList):
            for ip in ipList:
                ipSpace["ipSpaceRanges"]["ipRanges"] = [ipSpaceRange for ipSpaceRange in ipSpace["ipSpaceRanges"]["ipRanges"]
                                                        if not(ipaddress.ip_address(ipSpaceRange["startIpAddress"]) <= ip <= ipaddress.ip_address(
                                                        ipSpaceRange["endIpAddress"]) and ipSpaceRange["totalIpCount"] == "1")]

        ipList = list()
        for edgeGatewayIpRange in edgeGatewayIpRangesList:
            ipList.extend(_createIpList(edgeGatewayIpRange["startAddress"], edgeGatewayIpRange["endAddress"]))
        if not rollback:
            if not ipSpace["ipSpaceRanges"]:
                ipSpace["ipSpaceRanges"] = {}
                ipSpace["ipSpaceRanges"]["ipRanges"] = []
            _addIpsToIpSpaceRanges(ipList)
        else:
            _removeIpsFromIpSpaceRanges(ipList)
            if not ipSpace["ipSpaceRanges"]["ipRanges"]:
                ipSpace["ipSpaceRanges"] = None

    def _createEdgeGateway(self, nsxObj):
        data = self.rollback.apiData
        # Getting the edge gateway details of the target org vdc.
        # In case of remediation these gateway creation will not be attempted.
        targetEdgeGatewayNames = [
            edgeGateway['name']
            for edgeGateway in self.getOrgVDCEdgeGateway(data['targetOrgVDC']['@id'])
        ]

        for sourceEdgeGatewayDict in copy.deepcopy(data['sourceEdgeGateway']):
            if sourceEdgeGatewayDict['name'] in targetEdgeGatewayNames:
                continue

            sourceEdgeGatewayId = sourceEdgeGatewayDict['id'].split(':')[-1]

            # Prepare payload for edgeGatewayUplinks->dedicated
            bgpConfigDict = self.getEdgegatewayBGPconfig(sourceEdgeGatewayId, validation=False)
            # Use dedicated external network if BGP is configured
            # or AdvertiseRoutedNetworks parameter is set to True


            if (isinstance(bgpConfigDict, dict) and bgpConfigDict['enabled'] == "true"
                    or self.orgVdcInput['EdgeGateways'][sourceEdgeGatewayDict['name']]['AdvertiseRoutedNetworks']):
                dedicated = True
            else:
                dedicated = False

            t0Gateway = self.orgVdcInput['EdgeGateways'][sourceEdgeGatewayDict['name']]['Tier0Gateways']

            # Prepare payload for edgeClusterConfig->primaryEdgeCluster->backingId
            # Checking if edge cluster is specified in user input yaml
            externalDict = self.getExternalNetworkByName(t0Gateway)

            if self.orgVdcInput.get('EdgeGatewayDeploymentEdgeCluster'):
                # Fetch edge cluster id
                edgeClusterId = nsxObj.fetchEdgeClusterDetails(self.orgVdcInput["EdgeGatewayDeploymentEdgeCluster"]).get('id')
            else:
                edgeClusterId = nsxObj.fetchEdgeClusterIdForTier0Gateway(
                    externalDict['networkBackings']['values'][0]['backingId'])

            # Prepare payload for edgeGatewayUplinks->subnets->values
            subnetData = []
            if not externalDict.get('usingIpSpace'):
                if sourceEdgeGatewayDict['name'] in data['isT0Connected']:
                    # Adding only those subnets to T0 subnet data that are going to be connected to external network via T0
                    gatewayList = [subnetData[0] for subnetData in data['isT0Connected'][sourceEdgeGatewayDict['name']][t0Gateway]]
                    for uplink in sourceEdgeGatewayDict['edgeGatewayUplinks']:
                        if uplink['subnets']['values'][0]['gateway'] in gatewayList:
                            subnetData += uplink['subnets']['values']
                else:
                    # In case target edge gateway is not going to be connected to T0, a dummy T0/VRF is necessary
                    # Adding first subnet from dummy T0 because payload demands atleast one subnet
                    subnetData = [subnet for subnet in externalDict['subnets']['values'] if
                                  subnet['totalIpCount'] != subnet['usedIpCount']]
                    subnetData = [subnetData[0]]
                    subnetData[0]['ipRanges'] = {'values': []}
                    subnetData[0]['primaryIp'] = None

            payloadData = {
                'name': sourceEdgeGatewayDict['name'],
                'description': sourceEdgeGatewayDict.get('description') or '',
                'edgeGatewayUplinks': [
                    {
                        'uplinkId': externalDict['id'],
                        'uplinkName': externalDict['name'],
                        'connected': False,
                        'dedicated': False if externalDict.get('usingIpSpace') else dedicated,
                        'subnets': {
                            'values': subnetData
                        } if subnetData else None
                    }
                ],
                'distributedRoutingEnabled': False,
                'serviceNetworkDefinition': self.orgVdcInput['EdgeGateways'][sourceEdgeGatewayDict['name']]['serviceNetworkDefinition'],
                'orgVdc': {
                    'name': data['targetOrgVDC']['@name'],
                    'id': data['targetOrgVDC']['@id'],
                },
                'ownerRef': {
                    "name": data['targetOrgVDC']['@name'],
                    "id": data['targetOrgVDC']['@id'],
                },
                'orgRef': {
                    'name': data['Organization']['@name'],
                    'id': data['Organization']['@id'],
                },
                "edgeClusterConfig": {
                    "primaryEdgeCluster": {
                        "backingId": edgeClusterId
                    }
                },
            }

            # Checking if target edge gateway is going to be connected to segment backed network directly
            if sourceEdgeGatewayDict['name'] in data.get('isT1Connected', []):
                for uplink in sourceEdgeGatewayDict['edgeGatewayUplinks']:
                    if uplink['uplinkName'] in data['isT1Connected'][sourceEdgeGatewayDict['name']]:
                        # Adding respective uplinks to target edge gateway payload
                        uplinkDict = {
                            'uplinkId': data['segmentToIdMapping'][uplink['uplinkName'] + '-v2t'],
                            'uplinkName': uplink['uplinkName'] + '-v2t',
                            'subnets': {
                                'values': uplink['subnets']['values']
                            }
                        }
                        payloadData['edgeGatewayUplinks'].append(uplinkDict)

            # edge gateway create URL
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS)
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            response = self.restClientObj.post(url, self.headers, data=json.dumps(payloadData))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of creating target edge gateway task
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug(f"Target Edge Gateway ({sourceEdgeGatewayDict['name']}) created successfully.")
            else:
                errorResponse = response.json()
                raise Exception(
                    'Failed to create target Org VDC Edge Gateway - {}'.format(errorResponse['message']))

    @description("creation of target Org VDC Edge Gateway")
    @remediate
    def createEdgeGateway(self, nsxObj):
        """
        Description :   Creates an Edge Gateway in the specified Organization VDC
        """
        try:
            if not self.rollback.apiData['sourceEdgeGateway']:
                logger.debug('Skipping Target Edge Gateway creation as no source Edge Gateway exist')
                # If source Edge Gateway are not present, target Edge Gateway will also be empty
                self.rollback.apiData['targetEdgeGateway'] = list()
                return

            logger.info('Creating target Org VDC Edge Gateway')
            self._updateTargetExternalNetworkPool()
            self._createEdgeGateway(nsxObj)
            self.rollback.apiData['targetEdgeGateway'] = self.getOrgVDCEdgeGateway(
                self.rollback.apiData['targetOrgVDC']['@id'])

        except Exception:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @description("Disconnection of external network directly connected to T1")
    @remediate
    def disconnectSegmentBackedNetwork(self):
        """
        Description : Disconnection of NSX-T segment backed external network from target edge gateways
        """
        if not self.rollback.apiData.get('isT1Connected'):
            return
        logger.debug('Disconnecting segment backed external network directly connected to target edge gateways')
        data = self.rollback.apiData
        for targetEdgeGateway in data.get('targetEdgeGateway', []):
            if targetEdgeGateway['name'] not in data['isT1Connected']:
                continue
            payloadDict = copy.deepcopy(targetEdgeGateway)
            del payloadDict['status']
            t0Gateway = self.orgVdcInput['EdgeGateways'][targetEdgeGateway['name']]['Tier0Gateways']
            targetUplinks = [uplink for uplink in targetEdgeGateway['edgeGatewayUplinks'] if uplink['uplinkName'] == t0Gateway]
            payloadDict['edgeGatewayUplinks'] = targetUplinks

            # edge gateway update URL
            url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                   targetEdgeGateway['id'])
            # creating the payload data
            payloadData = json.dumps(payloadDict)
            self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
            # put api to reconnect the target edge gateway
            response = self.restClientObj.put(url, self.headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of the reconnecting target edge gateway task
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug(
                    'Target Org VDC Edge Gateway {} disconnected from segment backed external networks successfully.'.format(targetEdgeGateway['name']))
            else:
                raise Exception(
                    'Failed to disconnect target Org VDC Edge Gateway from external networks {}'.format(targetEdgeGateway['name'],
                                                                                   response.json()['message']))
        logger.debug('Successfully disconnect target Edge gateway from external network directly connected to T1.')

    @description("Allowing of non distributed routing for edge gateway.")
    @remediate
    def allowNonDistributedRoutingOnEdgeGW(self, implicitGateways):
        """
        Description : Allow Non-Distributed routing on edge gateway of Organization VDC
        """
        logger.debug('Allow Non-Distributed Routing is getting configured')
        # get the target edge gateway data and enable non distributed routing.
        for index, edgeGateway in enumerate(self.rollback.apiData['targetEdgeGateway']):
            if not (self.orgVdcInput['EdgeGateways'][edgeGateway['name']]['NonDistributedNetworks']
                    or edgeGateway['name'] in implicitGateways):
                continue

            gatewayId = edgeGateway['id']
            gatewayName = edgeGateway['name']
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.UPDATE_EDGE_GATEWAYS_BY_ID.format(gatewayId))
            response = self.restClientObj.get(url, self.headers)

            # Fetching JSON response from API call
            responseDict = response.json()
            if response.status_code == requests.codes.ok:
                logger.debug(
                    'Fetched source orgvdc edge gateway "{}"  successfully.'.format(gatewayName))
            else:
                raise Exception(
                    'Failed to fetch target orgvdc edge gateway "{}" due to error- "{}"'.format(
                        gatewayName, responseDict['message']))

            # create paylaod for the allowing non distributed routing.
            payLoadDict = responseDict

            # set a flag for allow non distributed routing
            if payLoadDict['nonDistributedRoutingEnabled']:
                logger.debug('Allow non distributed routing is already enabled for edge gateway {}.'
                             .format(gatewayName))
                return

            payLoadDict['nonDistributedRoutingEnabled'] = True
            payLoadData = json.dumps(payLoadDict)

            # put api call to configure allow non distributed routing on target edge gateway.
            response = self.restClientObj.put(url, self.headers, data=payLoadData)
            if response.status_code == requests.codes.accepted:
                # successful configuration of non distributed routing on target edgeGateway
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                self.rollback.apiData['targetEdgeGateway'][index]['nonDistributedRoutingEnabled'] = True
                logger.debug('Allow non distributed routing configuration updated successfully for edge gateway {}.'
                             .format(gatewayName))
            else:
                # failure in configuring allow non distributed routing on target edge gateway
                response = response.json()
                raise Exception('Failed to configure non distributed routing in Target Edge Gateway {} - {}'
                                .format(gatewayName, response['message']))

    def getEdgeGatewayDnsrelayConfig(self, edgeGatewayId):
        """
            Description :  Get DNS relay config on edge gateway
        """
        logger.debug('Getting edge gateway DNS relay configuration.')
        # get DNS relay configuration of target edge gateway.
        url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                              vcdConstants.ALL_EDGE_GATEWAYS,
                              vcdConstants.DNS_CONFIG.format(edgeGatewayId))
        self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
        # get api call to get dns listener ip
        response = self.restClientObj.get(url, headers=self.headers)
        responseDict = response.json()
        if response.status_code != requests.codes.ok:
            raise Exception("Failed to get edgeGateway DNS relay configuration : ", responseDict['message'])

        return responseDict

    @description("creation of target Org VDC Networks")
    @remediate
    def createOrgVDCNetwork(self, sourceOrgVDCNetworks, inputDict, nsxObj, implicitNetworks):
        """
        Description : Create Org VDC Networks in the specified Organization VDC
        """
        try:
            if not isinstance(self.rollback.metadata.get("prepareTargetVDC", {}).get("createOrgVDCNetwork"), bool):
                self.rollback.executionResult.setdefault('prepareTargetVDC', {})
                self.rollback.executionResult["prepareTargetVDC"]["createOrgVDCNetwork"] = False
                self.saveMetadataInOrgVdc()

            segmetList = list()

            # Check if overlay id's are to be cloned or not
            cloneOverlayIds = inputDict['VCloudDirector'].get('CloneOverlayIds')

            logger.info('Creating target Org VDC Networks')
            data = self.rollback.apiData
            targetOrgVDC = data['targetOrgVDC']
            targetEdgeGateway = data['targetEdgeGateway']

            # create org vdc network URL
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_ORG_VDC_NETWORKS)
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')

            # getting target org vdc network name list
            targetOrgVDCNetworksList = [network['name'] for network in self.getOrgVDCNetworks(targetOrgVDC['@id'], 'targetOrgVDCNetworks', saveResponse=False)]

            for sourceOrgVDCNetwork in sourceOrgVDCNetworks:
                overlayId = None
                # Fetching overlay id of the org vdc network, if CloneOverlayIds parameter is set to true
                if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_1) and cloneOverlayIds:
                    # URL to fetch overlay id of source org vdc networks
                    overlayIdUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                 vcdConstants.ORG_VDC_NETWORK_ADDITIONAL_PROPERTIES.format(
                                                     sourceOrgVDCNetwork['id']
                                                 ))
                    # Getting response from API call
                    response = self.restClientObj.get(overlayIdUrl, self.headers)
                    # Fetching JSON response from API call
                    responseDict = response.json()
                    if response.status_code == requests.codes.ok:
                        logger.debug(
                            'Fetched source org vdc network "{}" overlay id successfully.'.format(
                                sourceOrgVDCNetwork['name']))
                        overlayId = responseDict.get('overlayId')
                    else:
                        raise Exception(
                            'Failed to fetch source org vdc network "{}" overlay id  due to error- "{}"'.format(
                                sourceOrgVDCNetwork['name'], responseDict['message']))

                # Handled remediation in case of network creation failure
                if sourceOrgVDCNetwork['name'] + '-v2t' in targetOrgVDCNetworksList:
                    continue
                if sourceOrgVDCNetwork['networkType'] == "DIRECT":
                    segmentid, payloadData = self.createDirectNetworkPayload(inputDict, nsxObj,
                                                                             orgvdcNetwork=sourceOrgVDCNetwork,
                                                                             parentNetworkId=sourceOrgVDCNetwork[
                                                                                 'parentNetworkId'])
                    if segmentid:
                        segmetList.append(segmentid)
                else:
                    # creating payload dictionary
                    payloadDict = {'orgVDCNetworkName': sourceOrgVDCNetwork['name'] + '-v2t',
                                   'orgVDCNetworkDescription': sourceOrgVDCNetwork[
                                       'description'] if sourceOrgVDCNetwork.get('description') else '',
                                   'orgVDCNetworkGateway': sourceOrgVDCNetwork['subnets']['values'][0]['gateway'],
                                   'orgVDCNetworkPrefixLength': sourceOrgVDCNetwork['subnets']['values'][0]['prefixLength'],
                                   'orgVDCNetworkDNSSuffix': sourceOrgVDCNetwork['subnets']['values'][0]['dnsSuffix'],
                                   'orgVDCNetworkDNSServer1': sourceOrgVDCNetwork['subnets']['values'][0]['dnsServer1'],
                                   'orgVDCNetworkDNSServer2': sourceOrgVDCNetwork['subnets']['values'][0]['dnsServer2'],

                                   'orgVDCNetworkType': sourceOrgVDCNetwork['networkType'],
                                   'orgVDCName': targetOrgVDC['@name'],
                                   'orgVDCId': targetOrgVDC['@id']}

                if sourceOrgVDCNetwork['networkType'] == "ISOLATED":
                    payloadDict.update({'edgeGatewayName': "", 'edgeGatewayId': "", 'edgeGatewayConnectionType': ""})
                elif sourceOrgVDCNetwork['networkType'] != "DIRECT":
                    edgeGatewayName = sourceOrgVDCNetwork['connection']['routerRef']['name']
                    edgeGatewayId = \
                    list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == edgeGatewayName, targetEdgeGateway))[
                        0]['id']
                    payloadDict.update({'edgeGatewayName': edgeGatewayName,
                                        'edgeGatewayId': edgeGatewayId})
                if sourceOrgVDCNetwork['networkType'] != "DIRECT":
                    # creating payload data
                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.CREATE_ORG_VDC_NETWORK_TEMPLATE, apiVersion=self.version)

                # Loading JSON payload data to python Dict Structure
                payloadData = json.loads(payloadData)

                if float(self.version) < float(vcdConstants.API_VERSION_ZEUS):
                    payloadData['orgVdc'] = {
                        "name": targetOrgVDC['@name'],
                        "id": targetOrgVDC['@id']
                    }
                else:
                    payloadData['ownerRef'] = {
                        "name": targetOrgVDC['@name'],
                        "id": targetOrgVDC['@id']
                    }
                if sourceOrgVDCNetwork['networkType'] == "ISOLATED":
                    payloadData['connection'] = {}
                if not sourceOrgVDCNetwork['subnets']['values'][0]['ipRanges']['values']:
                    payloadData['subnets']['values'][0]['ipRanges']['values'] = None
                elif sourceOrgVDCNetwork['networkType'] != "DIRECT":
                    ipRangeList = []
                    for ipRange in sourceOrgVDCNetwork['subnets']['values'][0]['ipRanges']['values']:
                        ipPoolDict = {}
                        ipPoolDict['startAddress'] = ipRange['startAddress']
                        ipPoolDict['endAddress'] = ipRange['endAddress']
                        ipRangeList.append(ipPoolDict)
                    payloadData['subnets']['values'][0]['ipRanges']['values'] = ipRangeList

                # Handling code for dual stack networks
                if sourceOrgVDCNetwork.get('enableDualSubnetNetwork', None):
                    payloadData['subnets'] = sourceOrgVDCNetwork['subnets']
                    payloadData['enableDualSubnetNetwork'] = True

                # Adding overlay id in payload if cloneOverlayIds parameter is set to True and
                # if overlay id exists for corresponding org vdc network
                if cloneOverlayIds and overlayId:
                    payloadData.update({'overlayId': overlayId})

                # Enable guest vlan
                if sourceOrgVDCNetwork.get('guestVlanTaggingAllowed'):
                    payloadData['guestVlanTaggingAllowed'] = sourceOrgVDCNetwork['guestVlanTaggingAllowed']

                if (payloadData['networkType'] == 'NAT_ROUTED'
                        and sourceOrgVDCNetwork['connection']['connectionType'] == "INTERNAL"):
                    # Create the non distributed routed network.
                    edgeGatewayName = sourceOrgVDCNetwork['connection']['routerRef']['name']
                    if (self.orgVdcInput['EdgeGateways'][edgeGatewayName]['NonDistributedNetworks']
                            or sourceOrgVDCNetwork['id'] in implicitNetworks):
                        payloadData['connection']['connectionType'] = None
                        payloadData['connection']['connectionTypeValue'] = "NON_DISTRIBUTED"

                # Setting headers for the OPENAPI requests
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE

                payloadData = json.dumps(payloadData)
                # post api to create org vdc network

                response = self.restClientObj.post(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of the creating org vdc network task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Target Org VDC Network {} created successfully.'.format(sourceOrgVDCNetwork['name']))
                else:
                    errorResponse = response.json()
                    raise Exception(
                        'Failed to create target Org VDC Network {} - {}'.format(sourceOrgVDCNetwork['name'],
                                                                                 errorResponse['message']))
            if segmetList:
                self.rollback.apiData['LogicalSegments'] = segmetList
            # saving the org vdc network details to apiOutput.json
            self.getOrgVDCNetworks(targetOrgVDC['@id'], 'targetOrgVDCNetworks', saveResponse=True)
            logger.info('Successfully created target Org VDC Networks.')
            conflictNetwork = self.rollback.apiData.get('ConflictNetworks')
            if conflictNetwork:
                networkList = list()
                targetnetworks = self.retrieveNetworkListFromMetadata(targetOrgVDC['@id'], dfwStatus=False, orgVDCType='target')
                for targetnetwork in targetnetworks:
                    for network in conflictNetwork:
                        if network['name'] + '-v2t' == targetnetwork['name']:
                            # networkIds = list(filter(lambda network: network['name']+'-v2t' == targetnetwork['name'], conflictNetwork))[0]
                            networkList.append({'name': network['name'], 'id': targetnetwork['id'], 'shared': network['shared']})
                self.rollback.apiData['ConflictNetworks'] = networkList
        except:
            raise

    @description("creation of private IP Spaces")
    @remediate
    def createPrivateIpSpacesForNetworks(self, sourceOrgVDCNetworks):
        """
        Description : Creates Private IP Space for source org vdc network
        """
        try:
            data = self.rollback.apiData
            # Creating a list of edges mapped to IP Space enabled provider gateways
            ipSpaceEnabledEdges = [edge["id"] for edge in data['sourceEdgeGateway']
                                                if self.orgVdcInput['EdgeGateways'][edge["name"]]['Tier0Gateways']
                                                in data['ipSpaceProviderGateways']]
            # If VCD version is less than 10.4.2 and no such edges exists return
            if not (float(self.version) >= float(vcdConstants.API_10_4_2_BUILD) and ipSpaceEnabledEdges):
                return

            logger.info("Creating Private IP Spaces for Target Org VDC Networks")
            privateIpSpaces = data.get("privateIpSpaces", {})
            for sourceOrgVDCNetwork in sourceOrgVDCNetworks:
                # Private IP Spaces are not created for direct Networks
                if sourceOrgVDCNetwork['networkType'] == "DIRECT":
                    continue
                # Private IP Spaces are not create for Routed Network connected to Non IP Space Enabled Edges
                if sourceOrgVDCNetwork['networkType'] == "NAT_ROUTED" and sourceOrgVDCNetwork['connection']['routerRef']['id'] not in ipSpaceEnabledEdges:
                    continue
                gateway = sourceOrgVDCNetwork['subnets']['values'][0]['gateway']
                prefixLength = sourceOrgVDCNetwork['subnets']['values'][0]['prefixLength']
                subnet = "{}/{}".format(gateway, prefixLength)
                network = ipaddress.ip_network(subnet, strict=False)
                # Checking if the Org VDC network subnet exists in metadata list that contains prefix to be added to public IP Space uplinks
                # Private IP Space is not created for network if it exists else this network subnet is added to public IP Space uplink as IP Prefix
                for ipSpaceId, ipBlockToBeAddedList in data.get("ipBlockToBeAddedToIpSpaceUplinks", {}).items():
                    ipBlockNetworks = [ipaddress.ip_network(ipBlock, strict=False) for ipBlock in ipBlockToBeAddedList]
                    if network in ipBlockNetworks:
                        if subnet not in data.get("prefixAddedToIpSpaces", []):
                            self.addPrefixToIpSpace(ipSpaceId, subnet)
                        break
                else:
                    # Private IP Space being created for Org VDC networks should have route advertisement enabled is VDC networks needs to be Advertised...
                    # since Org VDC network is going to use this private IP Space

                    # Org VDC networks is route advertised if AdvertisedRoutedNetworks flag is True OR BGP is advertising all subnets(
                    # can be checked by edge gateway id in "advertiseEdgeNetworks" in metadata which contains list of edge Ids advertising all its routed networks) OR
                    # there exists an ip prefix in route redistribution section of Edge gateway which is equal to this network subnet
                    routeAdvertisement = sourceOrgVDCNetwork['networkType'] == "NAT_ROUTED" and (
                        self.orgVdcInput['EdgeGateways'][sourceOrgVDCNetwork['connection']['routerRef']['name']]['AdvertiseRoutedNetworks'] or \
                        sourceOrgVDCNetwork['connection']['routerRef']['id'] in data.get("advertiseEdgeNetworks", []) or \
                        network in [ipaddress.ip_network(net, strict=False) for net in data.get("prefixToBeAdvertised",
                                    {}).get(sourceOrgVDCNetwork['connection']['routerRef']['name'], [])])
                    ipPrefixList = [(gateway, prefixLength)]
                    # Checking whether the private IP Space is already created, if not creating it
                    if subnet not in privateIpSpaces:
                        ipSpaceId = self.createPrivateIpSpace(subnet, ipPrefixList=ipPrefixList, routeAdvertisement=routeAdvertisement, returnOutput=True)
                    else:
                        ipSpaceId = data.get("privateIpSpaces", {}).get(subnet)
                    # If route advertisement is enabled for private IP Space which means it will eventually be connected as an uplink to private provider gateway
                    if routeAdvertisement and not any([uplink for uplink in data.get("manuallyAddedUplinks", []) if ipSpaceId in uplink]):
                        self.connectIpSpaceUplinkToProviderGateway(sourceOrgVDCNetwork['connection']['routerRef']['name'], subnet, ipSpaceId)
            networkList = [ipaddress.ip_network(ipspace, strict=False) for ipspace in data.get("privateIpSpaces", {})]
            for edgeGatewayName, prefixToBeAdvertisedList in data.get("prefixToBeAdvertised", {}).items():
                    for prefixToBeAdvertised in prefixToBeAdvertisedList:
                        if ipaddress.ip_network(prefixToBeAdvertised, strict=False) not in networkList:
                            ipSpaceId = self.createPrivateIpSpace(prefixToBeAdvertised, ipPrefixList=[(prefixToBeAdvertised.split("/")[0], prefixToBeAdvertised.split("/")[-1])],
                                                                routeAdvertisement=True, returnOutput=True)
                        else:
                            for privIpSpace in data.get("privateIpSpaces", {}):
                                if ipaddress.ip_network(prefixToBeAdvertised, strict=False) == ipaddress.ip_network(privIpSpace, strict=False):
                                    ipSpaceId = data["privateIpSpaces"][privIpSpace]
                                    break
                        if not any([uplink for uplink in data.get("manuallyAddedUplinks", []) if ipSpaceId in uplink]):
                            self.connectIpSpaceUplinkToProviderGateway(edgeGatewayName, prefixToBeAdvertised, ipSpaceId)
            prefixList = [ipaddress.ip_network(prefix, strict=False) for prefix in data.get("prefixAddedToIpSpaces", [])]
            for ipSpaceId, ipBlockToBeAddedList in data.get("ipBlockToBeAddedToIpSpaceUplinks", {}).items():
                for ipBlock in ipBlockToBeAddedList:
                    if ipaddress.ip_network(ipBlock, strict=False) in prefixList:
                        continue
                    else:
                        self.addPrefixToIpSpace(ipSpaceId, ipBlock)

        except Exception:
            # Saving metadata in org VDC
            self.saveMetadataInOrgVdc()
            raise

    @description("creation of target DNAT for non distributed OrgVDC Networks")
    @remediate
    def configureTargetDnatForDns(self):
        """
            Description :  Configure DNAT rules on edge gateway for non distributed networks
        """
        # Added a interop handler to work this feature only with vcd version 10.3.2 and later.
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA_10_3_2):
            return

        if not self.rollback.apiData['targetEdgeGateway']:
            logger.info('Skipping target DNAT configuration for DNS as edge gateway does '
                        'not exists')
            return

        targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
        logger.debug('DNAT rules are getting configured for Non-Distributed networks.')

        # application port profile list
        applicationPortProfileList = self.getApplicationPortProfiles()
        applicationPortProfileDict = self.filterApplicationPortProfiles(applicationPortProfileList)
        tcpPortName, tcpPortId = self._searchApplicationPortProfile(applicationPortProfileDict, 'tcp', '53')
        udpPortName, udpPortId = self._searchApplicationPortProfile(applicationPortProfileDict, 'udp', '53')

        # get target OrgVDC Network details.
        # We are not creating DNAT rules for shared network for non-DR, bcz of the VCD issue on lower versions.
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA_10_3_3):
            orgvdcNetworks = self.getOrgVDCNetworks(targetOrgVDCId, 'targetOrgVDCNetworks', saveResponse=False)
        else:
            orgvdcNetworks = self.getOrgVDCNetworks(targetOrgVDCId, 'targetOrgVDCNetworks', sharedNetwork=True,
                                                    saveResponse=False)
        sourceEdgeGateway = copy.deepcopy(self.rollback.apiData['sourceEdgeGateway'])

        # iterate over the OrgVDC networks and configure DNAT rule. Each non-distributed routed networks will
        # have two DNAT rules are needed - one for TCP and another for UDP DNS traffic.
        for network in orgvdcNetworks:
            logger.debug(f"Checking {network['name']}")
            # If network is not routed or distributed then do not configure DNAT rules.
            if not (network['networkType'] == 'NAT_ROUTED'
                    and network['connection']['connectionTypeValue'] == 'NON_DISTRIBUTED'):
                logger.debug(f"{network['name']} is not distributed")
                continue


            # add DNAT rules for non distributed routed networks.
            edgeGatewayId = network['connection']['routerRef']['id']
            edgeGatewayName = network['connection']['routerRef']['name']

            if not any([edgeGatewayId == edgeGateway["id"] for edgeGateway in self.rollback.apiData["targetEdgeGateway"]]):
                continue

            # Parse Source edge gateway id
            sourceEdgeGatewayId = list(
                filter(lambda edgeGatewayData: edgeGatewayData['name'] == edgeGatewayName,
                       sourceEdgeGateway))[0]['id']
            # Get DNS configuration of source edge gateway
            dnsRelayConfig = self.getEdgeGatewayDnsConfig(sourceEdgeGatewayId.split(':')[-1], False)
            orgvdcNetworkGatewayIp = network['subnets']['values'][0]['gateway']
            orgvdcNetworkDns = network['subnets']['values'][0]['dnsServer1']
            if not(orgvdcNetworkGatewayIp == orgvdcNetworkDns and dnsRelayConfig):
                logger.debug(f"{network['name']}: DNS relay not enabled or DNS IP is not same as gateway IP")
                continue

            # get DNS relay configuration of target edge gateway.
            dnsRelayConfig = self.getEdgeGatewayDnsrelayConfig(edgeGatewayId)
            if dnsRelayConfig.get('enabled'):
                listnerIp = dnsRelayConfig['listenerIp']
            else:
                # NSX-T backed Org VDC Edge Gateway has DNS forwarding service running on the SR
                # component of Tier-1 GW on a loopback interface with an arbitrary non-overlapping IP which
                # by default uses 192.168.255.228 IP address
                listnerIp = "192.168.255.228"

            # API to configure NAT rules on edge gateway
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                  vcdConstants.T1_ROUTER_NAT_CONFIG.format(edgeGatewayId))

            # Create a payload for DNAT rules for TCP on port 53 for DNS forwarding
            if network['name'].endswith('-v2t'):
                networkName = network['name'][:-(len('-v2t'))]
            else:
                networkName = network['name']
            tcpDNATPayload = {"name": "DNS Forwarding DNAT for network {} TCP".format(networkName),
                              "description": "Created during NSX-V to T migration",
                              "enabled": True, "type": "DNAT", "externalAddresses": orgvdcNetworkGatewayIp,
                              "internalAddresses": listnerIp, "appliedTo": {"id": network['id']},
                              "applicationPortProfile": {"id": tcpPortId, "name": tcpPortName},
                              "firewallMatch": "MATCH_EXTERNAL_ADDRESS"}
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            tcpDNATPayloadData = json.dumps(tcpDNATPayload)

            # post api call to configure DNAT rule in target edge gateway.
            response = self.restClientObj.post(url, headers=self.headers, data=tcpDNATPayloadData)
            if response.status_code == requests.codes.accepted:
                # successful configuration of ip prefix list
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Successfully created DNAT rules for TCP on target edge gateway: {}".format(
                    edgeGatewayName))
            else:
                errorResponse = response.json()
                raise Exception("Failed create DNAT rules for TCP on target edge gateway {}:{} ".format(
                    edgeGatewayName, errorResponse['message']))

            # Create a payload for DNAT rules for UDP on port 53 for DNS forwarding
            udpDNATPayload = {"name": "DNS Forwarding DNAT for network {} UDP".format(networkName),
                              "description": "Created during NSX-V to T migration",
                              "enabled": True, "type": "DNAT", "externalAddresses": orgvdcNetworkGatewayIp,
                              "internalAddresses": listnerIp, "appliedTo": {"id": network['id']},
                              "applicationPortProfile": {"id": udpPortId, "name": udpPortName},
                              "firewallMatch": "MATCH_EXTERNAL_ADDRESS"}
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            udpDNATPayloadData = json.dumps(udpDNATPayload)

            # post api call to configure DNAT rule in target edge gateway.
            response = self.restClientObj.post(url, headers=self.headers, data=udpDNATPayloadData)
            if response.status_code == requests.codes.accepted:
                # successful configuration of ip prefix list
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Successfully created DNAT rules for UDP on target edge gateway: {}".format(
                    edgeGatewayName))
            else:
                errorResponse = response.json()
                raise Exception("Failed create DNAT rules for UDP on target edge gateway {}:{} ".format(
                    edgeGatewayName, errorResponse['message']))

    @isSessionExpired
    def getEdgeGatewayRateLimit(self, edgeGatewayId):
        """
            Description :   Validate Edge Gateway uplinks
            Parameters  :   edgeGatewayId   -   Id of the Edge Gateway  (STRING)
        """
        url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                            vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId))
        acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
        headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
        # retrieving the details of the edge gateway
        response = self.restClientObj.get(url, headers)
        if response.status_code == requests.codes.ok:
            responseDict = response.json()
            ifaceRateLimitInfo = {}
            gatewayInterfaces = responseDict['configuration']['gatewayInterfaces']['gatewayInterface']

            # checking whether source edge gateway has rate limit configured
            rateLimitEnabledIfaces = [interface for interface in gatewayInterfaces if
                                          interface.get('applyRateLimit')]
            # get rate limits of all interfaces and find lowest amongst all for both In and Out rate limits.
            inRateLimit = [int(iface['inRateLimit']) for iface in rateLimitEnabledIfaces if iface['inRateLimit']]
            outRateLimit = [int(iface['outRateLimit']) for iface in rateLimitEnabledIfaces if iface['outRateLimit']]
            if inRateLimit:
                ifaceRateLimitInfo['inRateLimit'] = min(inRateLimit)
            if outRateLimit:
                ifaceRateLimitInfo['outRateLimit'] = min(outRateLimit)
            return ifaceRateLimitInfo
        else:
            raise Exception("Failed to get edgeGateway Rate Limit.")

    @isSessionExpired
    def getNsxtManagerQos(self, nsxtManagerId, qosName):
        """
        Description :   Get QOS profiles from NSXT-Manager
        Parameters  :   nsxtManagerId -  NSXT-Manager ID
                        qosName - Name of the QOS profile
        """
        # Get the all QOS profile details
        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                            vcdConstants.NSX_T_QOS_PROFILE.format(nsxtManagerId))
        response = self.restClientObj.get(url, self.headers)
        responseDict = response.json()
        if not response.status_code == requests.codes.ok:
            raise Exception("Failed to get NSXT-Manager QOS profiles.")

        # find the QOS profile from the available QOS, if absent then create new QOS.
        for qosProfile in responseDict["values"]:
            if qosName == qosProfile["displayName"]:
                qosProfileDetails = {"name": qosProfile["displayName"], "id": qosProfile["id"]}
                return qosProfileDetails
        else:
            logger.debug("QOS profile {} is not present on NSXT-Manager.".format(qosName))
            return None

    @description("Configure Edge gateway rate limits.")
    @remediate
    def configureEdgeGWRateLimit(self, nsxObj):
        """
        Description :   Configure Edge gateway rate limits.
        Parameters  :   OrgVDCId  -   Id of the Organization VDC that is to be deleted (STRING)
        """
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA_10_3_2):
            return

        logger.debug('Edge GateWay Rate limiting (QOS) is getting configured')
        targetEdgeGateway = copy.deepcopy(self.rollback.apiData['targetEdgeGateway'])

        # fetching NSX-T manager id
        tpvdcName = self.rollback.apiData['targetProviderVDC']['@name']
        nsxtManagerId = self.getNsxtManagerId(tpvdcName)
        for sourceEdgeGateway in self.rollback.apiData['sourceEdgeGateway']:
            logger.debug("Rate Limiting (QoS) configuration for EdgeGateway - {}".format(sourceEdgeGateway['name']))
            sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
            targetEdgeGatewayId = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'], targetEdgeGateway))[0]['id']
            targetEdgeGatewayName = list(
                filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                       targetEdgeGateway))[0]['name']
            interfaceRateLimitInfo = self.getEdgeGatewayRateLimit(sourceEdgeGatewayId)
            if not interfaceRateLimitInfo:
                logger.debug("Rate Limiting (QoS) configuration not present on EdgeGateway : {}".format(sourceEdgeGateway['name']))
                continue
            inRateLimit, outRateLimit = interfaceRateLimitInfo.get('inRateLimit'), interfaceRateLimitInfo.get('outRateLimit')
            # get the QOS profiles from NSX-T for rate limits and create Payload.
            payloadDict = {}
            if inRateLimit:
                inQOSName = "{} Mbps".format(inRateLimit)
                inQOSProfileDetails = self.getNsxtManagerQos(nsxtManagerId, inQOSName)
                if not inQOSProfileDetails:
                    nsxObj.createNsxtManagerQos(inQOSName.split()[0])
                    inQOSProfileDetails = self.getNsxtManagerQos(nsxtManagerId, inQOSName)
                payloadDict["egressProfile"] = inQOSProfileDetails
            if outRateLimit:
                outQOSName = "{} Mbps".format(outRateLimit)
                outQOSProfileDetails = self.getNsxtManagerQos(nsxtManagerId, outQOSName)
                if not outQOSProfileDetails:
                    nsxObj.createNsxtManagerQos(outQOSName.split()[0])
                    outQOSProfileDetails = self.getNsxtManagerQos(nsxtManagerId, outQOSName)
                payloadDict["ingressProfile"] = outQOSProfileDetails

            # Configure rate limit on target edge gateway
            qosProfileUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.QOS_PROFILE.format(targetEdgeGatewayId))
            payloadData = json.dumps(payloadDict)
            apiResponse = self.restClientObj.put(qosProfileUrl, self.headers, data=payloadData)
            if apiResponse.status_code == requests.codes.accepted:
                taskUrl = apiResponse.headers['Location']
                # checking the status of the Rate Limiting (QoS) configuration for EdgeGateway
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.info("Updated Rate Limiting (QoS) configuration for EdgeGateway {}."
                            .format(targetEdgeGatewayName))
            else:
                raise Exception("Failed to update Rate Limiting (QoS) configuration for EdgeGateway : ",
                                apiResponse.json())

    @isSessionExpired
    def deleteOrgVDC(self, orgVDCId, rollback=False):
        """
        Description :   Deletes the specified Organization VDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC that is to be deleted (STRING)
        """
        try:
            if rollback and not self.rollback.metadata.get(
                    "prepareTargetVDC", {}).get("createOrgVDC"):
                return

            if rollback:
                logger.info("RollBack: Deleting Target Org-Vdc")
            # splitting the org vdc id as per the requirement of the xml api
            orgVDCId = orgVDCId.split(':')[-1]
            # url to delete the org vdc
            url = "{}{}?force=true&recursive=true".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                                          vcdConstants.ORG_VDC_BY_ID.format(orgVDCId))
            # delete api to delete the org vdc
            response = self.restClientObj.delete(url, self.headers)
            responseDict = self.vcdUtils.parseXml(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of deleting org vdc task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Organization VDC deleted successfully.')
                    return
            else:
                raise Exception('Failed to delete target Org VDC {}'.format(responseDict['Error']['@message']))
        except Exception:
            raise

    @isSessionExpired
    def removeDHCPBinding(self, networkId):
        """
        Description :   Deletes the DHCP binding on OrgVDC network if present
        Parameters  :   networkId  -  Id of the Org VDC network. (STRING)
        """
        logger.debug("checking DHCP binding status")
        # Enables the DHCP bindings on OrgVDC network.
        DHCPBindingUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.DHCP_BINDINGS.format(networkId))
        # call to get api to get dhcp binding config details of specified networkId
        response = self.restClientObj.get(DHCPBindingUrl, self.headers)
        if response.status_code == requests.codes.ok:
            responsedict = response.json()
            # checking DHCP bindings configuration, if present then deleting the DHCP Binding config.
            for bindings in responsedict['values']:
                deleteDHCPBindingURL = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.DHCP_BINDINGS.format(networkId), bindings['id'])
                response = self.restClientObj.delete(deleteDHCPBindingURL, self.headers)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Organization VDC Network DHCP Bindings deleted successfully.')
                else:
                    logger.debug(
                        'Failed to delete Organization VDC Network DHCP bindings {}.{}'.format(networkId, response.json()['message']))

    @isSessionExpired
    def deleteEmptyvApp(self,orgVDCId):
        """
        Description : Delete empty vApp from specified OrgVDC
        Parameters :  orgVDCId  -   Id of the Organization VDC
        """
        sourceVappsList = self.getOrgVDCvAppsList(orgVDCId)
        for vApp in sourceVappsList:
            vAppResponse = self.restClientObj.get(vApp['@href'], self.headers)
            responseDict = self.vcdUtils.parseXml(vAppResponse.content)
            if vAppResponse.status_code == requests.codes.ok:
                # checking if the vapp has vms present in it.
                if 'VApp' in responseDict.keys():
                    if not responseDict['VApp'].get('Children'):
                        vAppID = responseDict['VApp']["@href"].split('/')[-1]
                        if responseDict['VApp']["@status"] == vcdConstants.VAPP_STATUS['POWERED_ON']:
                            payloadDict = dict()
                            payloadData = self.vcdUtils.createPayload(
                                filePath=os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml'),
                                payloadDict=payloadDict,
                                fileType='yaml',
                                componentName=vcdConstants.COMPONENT_NAME,
                                templateName=vcdConstants.UNDEPLOY_VAPP_TEMPLATE)
                            payloadData = json.loads(payloadData)
                            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                                vcdConstants.UNDEPLOY_VAPP_API.format(vAppID))
                            self.headers['Content-Type'] = vcdConstants.GENERAL_XML_CONTENT_TYPE
                            # post api call to undeploy vapp
                            response = self.restClientObj.post(url, self.headers, data=payloadData)
                            if response.status_code == requests.codes.accepted:
                                task_url = response.headers['Location']
                                self._checkTaskStatus(taskUrl=task_url)
                            else:
                                errorResponse = response.json()
                                raise Exception('Failed to power off vApp  - {}'.format(errorResponse['message']))

                        url = "{}vApp/{}".format(vcdConstants.XML_API_URL.format(self.ipAddress), vAppID)
                        # delete api call to delete empty vapp
                        response = self.restClientObj.delete(url, self.headers)
                        if response.status_code == requests.codes.accepted:
                            task_url = response.headers['Location']
                            self._checkTaskStatus(taskUrl=task_url)
                        else:
                            errorResponse = response.json()
                            raise Exception('Failed to delete empty vApp  - {}'.format(errorResponse['message']))
                else:
                    raise Exception(f"Failed to get vApp {vApp['@name']} details.")
            else:
                raise Exception(f"Failed to get vApp {vApp['@name']} details: {responseDict['Error']['@message']}")

    @isSessionExpired
    def deleteOrgVDCNetworks(self, orgVDCId, rollback=False):
        """
        Description :   Deletes all Organization VDC Networks from the specified OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
                        source    -   Defaults to True meaning delete the NSX-V backed Org VDC Networks (BOOL)
                                      If set to False meaning delete the NSX-t backed Org VDC Networks (BOOL)
        """
        try:
            # Check if org vdc networks were created or not
            if not isinstance(self.rollback.metadata.get("prepareTargetVDC", {}).get("createOrgVDCNetwork"), bool):
                return

            if rollback:
                logger.info("RollBack: Deleting Target Org VDC Networks")
            orgVDCNetworksErrorList = []

            dfwStatus = False
            if rollback:
                dfwStatus = True if self.rollback.apiData.get('OrgVDCGroupID') else False

            orgVDCNetworksList = self.getOrgVDCNetworks(orgVDCId, None, dfwStatus=dfwStatus, saveResponse=False)
            # iterating over the org vdc network list
            for orgVDCNetwork in orgVDCNetworksList:
                # Check if DHCP Binding enabled on Network, if enabled then delete binding first.
                # Binding should already be removed during rollback in disconnectTargetOrgVDCNetwork, just checking here if present then remove
                if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_1):
                    self.removeDHCPBinding(orgVDCNetwork['id'])
                # url to delete the org vdc network
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.DELETE_ORG_VDC_NETWORK_BY_ID.format(orgVDCNetwork['id']))
                response = self.restClientObj.delete(url, self.headers)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Organization VDC Network deleted successfully.')
                else:
                    logger.debug('Failed to delete Organization VDC Network {}.{}'.format(orgVDCNetwork['name'],
                                                                                          response.json()['message']))
                    orgVDCNetworksErrorList.append(orgVDCNetwork['name'])
            if orgVDCNetworksErrorList:
                raise Exception(
                    'Failed to delete Org VDC networks {} - as it is in use'.format(orgVDCNetworksErrorList))
        except Exception:
            raise

    @isSessionExpired
    def deletePrivateIpSpaces(self):
        """
        Description :   Deletes all the private IP Spaces creates by the tool
        """
        data = self.rollback.apiData
        ipSpaceEnabledEdges = [edge["id"] for edge in data['sourceEdgeGateway']
                               if self.orgVdcInput['EdgeGateways'][edge["name"]]['Tier0Gateways']
                               in data['ipSpaceProviderGateways']]
        if not (float(self.version) >= float(vcdConstants.API_10_4_2_BUILD) and ipSpaceEnabledEdges):
            return
        logger.info("Removing Private IP Spaces used by target Org VDC Networks")
        privateIpSpacesIdList = [ipSpace["id"] for ipSpace in self.fetchAllIpSpaces() if ipSpace["type"] == "PRIVATE"]
        for ipSpaceName, ipSpaceId in self.rollback.apiData.get("privateIpSpaces", {}).items():
            if ipSpaceId not in privateIpSpacesIdList:
                continue
            floatingIpUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                             vcdConstants.UPDATE_IP_SPACES.format(ipSpaceId),
                                             vcdConstants.IP_SPACE_ALLOCATIONS)
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
            floatingIpList = self.getPaginatedResults("Floating IPs", floatingIpUrl, headers,
                                                      urlFilter="filter=type==FLOATING_IP")
            if floatingIpList:
                logger.warning("Skipping deleting IP Space - '{}' since it has allocated IPs".format(ipSpaceName))
                continue
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.UPDATE_IP_SPACES.format(ipSpaceId))
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE,
                       'Content-Type': vcdConstants.OPEN_API_CONTENT_TYPE,
                       'X-VMWARE-VCLOUD-TENANT-CONTEXT': self.rollback.apiData.get('Organization', {}).get('@id').split(":")[-1]}
            response = self.restClientObj.delete(url, headers=headers)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('Private IP Space - {} deleted successsfully'.format(ipSpaceName))
            else:
                logger.debug('Failed to delete Private IP Space - {}.{}'.format(ipSpaceName,
                                                                                      response.json()['message']))

    @isSessionExpired
    def releaseFloatingIps(self):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-V Backed OrgVDC
        """
        data = self.rollback.apiData
        for ipSpace in data.get("floatingIps", {}):
            floatingIpUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                   vcdConstants.UPDATE_IP_SPACES.format(ipSpace), vcdConstants.IP_SPACE_ALLOCATIONS)
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
            floatingIpList = self.getPaginatedResults("Floating IPs", floatingIpUrl, headers, urlFilter="filter=type==FLOATING_IP")
            for ip in data["floatingIps"][ipSpace]:
                for floatingIp in floatingIpList:
                    if ip == floatingIp["value"] and floatingIp["usageState"] == "UNUSED":
                        deleteUrl = "{}/{}".format(floatingIpUrl, floatingIp["id"])
                        response = self.restClientObj.delete(deleteUrl, headers)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug("Floating IP {} released from IP Space {}".format(ip, ipSpace))
                            break
                        else:
                            logger.debug('Failed to release floating IP {} from IP Space {}.{}'.format(ip, ipSpace,
                                                                                                  response.json()['message']))

    @isSessionExpired
    def releaseIpPrefixes(self):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-V Backed OrgVDC
        """
        data = self.rollback.apiData
        if not data.get("ipBlockToBeAddedToIpSpaceUplinks", {}):
            return
        for ipSpaceId, ipPrefixList in data.get("ipBlockToBeAddedToIpSpaceUplinks", {}).items():
            prefixUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                             vcdConstants.UPDATE_IP_SPACES.format(ipSpaceId),
                                             vcdConstants.IP_SPACE_ALLOCATIONS)
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
            allocatedPrefixList = self.getPaginatedResults("Floating IPs", prefixUrl, headers,
                                                      urlFilter="filter=type==IP_PREFIX")
            for ipPrefix in ipPrefixList:
                for ipSpacePrefix in allocatedPrefixList:
                    if ipaddress.ip_network(ipPrefix, strict=False) == ipaddress.ip_network(ipSpacePrefix["value"], strict=False)\
                            and ipSpacePrefix["usageState"] == "UNUSED":
                        deleteUrl = "{}/{}".format(prefixUrl, ipSpacePrefix["id"])
                        response = self.restClientObj.delete(deleteUrl, headers)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug("IP Prefix - '{}' released from IP Space - '{}' successfully".format(ipPrefix, ipSpaceId))
                            break
                        else:
                            logger.debug("Failed to release IP Prefix - '{}' released from IP Space - '{}' with error - '{}'".format(ipPrefix, ipSpaceId,
                                                                                                       response.json()[
                                                                                                           'message']))
        logger.debug("All ip prefixes added to public IP Spaces released successfully")

    @isSessionExpired
    def deleteIpPrefixAddedToIpSpaceUplinks(self):
        """
        Description :   Removes IP Prefixes Added to IP Space Uplinks
        """
        if not self.rollback.apiData.get("ipBlockToBeAddedToIpSpaceUplinks", {}):
            return
        # Acquiring lock as only one ipspace can be updated at a time
        self.lock.acquire(blocking=True)
        for ipSpaceId, ipPrefixList in self.rollback.apiData.get("ipBlockToBeAddedToIpSpaceUplinks", {}).items():
            ipSpaceDict = self.fetchIpSpace(ipSpaceId)
            preFixNetworkList = [ipaddress.ip_network(prefix, strict=False) for prefix in ipPrefixList]
            targetPrefixes = []
            for ipPrefix in ipSpaceDict.get("ipSpacePrefixes", []):
                targetPrefixSequence = [prefixSequence for prefixSequence in ipPrefix["ipPrefixSequence"]
                                        if ipaddress.ip_network("{}/{}".format(prefixSequence["startingPrefixIpAddress"],
                                                                               prefixSequence["prefixLength"]), strict=False) not in preFixNetworkList]
                if targetPrefixSequence:
                    targetPrefixes.append({
                        "ipPrefixSequence": targetPrefixSequence,
                        "defaultQuotaForPrefixLength": ipPrefix["defaultQuotaForPrefixLength"]
                    })
            ipSpaceDict["ipSpacePrefixes"] = targetPrefixes if targetPrefixes else None
            ipSpaceUrl = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.UPDATE_IP_SPACES.format(ipSpaceId))
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
            payloadData = json.dumps(ipSpaceDict)
            response = self.restClientObj.put(ipSpaceUrl, headers=headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of the creating org vdc network task
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Prefixes - '{}' removed from IP Space Uplink - '{}' successfully.".format(ipPrefixList, ipSpaceId))
            else:
                errorResponse = response.json()
                raise Exception(
                    "Failed to remove Prefixes - '{}' from IP Space Uplink - '{}' with error - {}".format(prefixList, ipSpaceId,
                                                                                                   errorResponse['message']))
        logger.debug("Removed IP Prefixes from respective IP Space Uplinks successfully")
        # Releasing lock
        self.lock.release()

    @isSessionExpired
    def removeManuallyAddedUplinks(self):
        """
        Description :   Removes manually added uplinks during migration to Provider Gateways
        """
        if not self.rollback.apiData.get("manuallyAddedUplinks", []):
            return
        # Acquiring lock as only one uplink can be added to provider gateway at a time
        self.lock.acquire(blocking=True)
        for ipSpaceId, ipSpaceUplinkId in self.rollback.apiData.get("manuallyAddedUplinks", []):
            url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.IP_SPACE_UPLINKS, ipSpaceUplinkId)
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
            delResponse = self.restClientObj.delete(url, headers=headers)
            if delResponse.status_code == requests.codes.accepted:
                taskUrl = delResponse.headers['Location']
                # checking the status of deleting nsx-v backed edge gateway task
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('IP Space Uplink removed successfully')
            else:
                delResponseDict = delResponse.content.json()
                raise Exception("Failed to remove IP Space uplink - '{}' due to error ".format(ipSpaceUplinkId, delResponseDict["message"]))
        logger.debug("Removed manually added Uplinks to IP Space Provider Gateways during migration successfully")
        # Releasing lock
        self.lock.release()

    @isSessionExpired
    def deleteNsxVBackedOrgVDCEdgeGateways(self, orgVDCId):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-V Backed OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
        """
        try:
            # retrieving the details of the org vdc edge gateway
            responseDict = self.getOrgVDCEdgeGateway(orgVDCId)
            if responseDict:
                for orgVDCEdgeGateway in responseDict:
                    orgVDCEdgeGatewayId = orgVDCEdgeGateway['id'].split(':')[-1]
                    # url to fetch edge gateway details
                    getUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                           vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                    getResponse = self.restClientObj.get(getUrl, headers=self.headers)
                    if getResponse.status_code == requests.codes.ok:
                        responseDict = self.vcdUtils.parseXml(getResponse.content)
                        edgeGatewayDict = responseDict['EdgeGateway']
                        # checking if distributed routing is enabled on edge gateway, if so disabling it
                        if edgeGatewayDict['Configuration']['DistributedRoutingEnabled'] == 'true':
                            self.disableDistributedRoutingOnOrgVdcEdgeGateway(orgVDCEdgeGateway['id'])
                    # url to delete the edge gateway
                    deleteUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                              vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                    # delete api to delete edge gateway
                    delResponse = self.restClientObj.delete(deleteUrl, self.headers)
                    if delResponse.status_code == requests.codes.accepted:
                        taskUrl = delResponse.headers['Location']
                        # checking the status of deleting nsx-v backed edge gateway task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Source Org VDC Edge Gateway deleted successfully.')
                    else:
                        delResponseDict = self.vcdUtils.parseXml(delResponse.content)
                        raise Exception('Failed to delete Edge gateway {}:{}'.format(orgVDCEdgeGateway['name'],
                                                                                     delResponseDict['Error'][
                                                                                         '@message']))
            else:
                logger.warning("Target Edge Gateway doesn't exist")
        except Exception:
            raise

    @isSessionExpired
    def deleteNsxTBackedOrgVDCEdgeGateways(self, orgVDCId):
        """
        Description :   Deletes all the Edge Gateways in the specified NSX-t Backed OrgVDC
        Parameters  :   orgVDCId  -   Id of the Organization VDC (STRING)
        """
        try:
            # Locking thread. When Edge gateways from multiple org VDC having IPSEC enabled are rolled back at the same
            # time, target edge gateway deletion fails.
            self.lock.acquire(blocking=True)

            # Check if org vdc edge gateways were created or not
            if not self.rollback.metadata.get("prepareTargetVDC", {}).get("createEdgeGateway"):
                return

            logger.info("RollBack: Deleting Target Edge Gateway")
            # retrieving the details of the org vdc edge gateway
            responseDict = self.getOrgVDCEdgeGateway(orgVDCId)
            if responseDict:
                for orgVDCEdgeGateway in responseDict:
                    # url to fetch edge gateway details
                    url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                        vcdConstants.UPDATE_EDGE_GATEWAYS_BY_ID.format(orgVDCEdgeGateway['id']))
                    # delete api to delete the nsx-t backed edge gateway
                    response = self.restClientObj.delete(url, self.headers)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of deleting the nsx-t backed edge gateway
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Target Org VDC Edge Gateway deleted successfully.')
                    else:
                        raise Exception('Failed to delete Edge gateway {}:{}'.format(orgVDCEdgeGateway['name'],
                                                                                     response.json()['message']))
            else:
                logger.warning('Target Edge Gateway do not exist')
        except Exception:
            raise
        finally:
            # Releasing thread lock
            try:
                self.lock.release()
            except RuntimeError:
                pass

    @description("disconnection of source routed Org VDC Networks from source Edge gateway")
    @remediate
    def disconnectSourceOrgVDCNetwork(self, orgVDCNetworkList, sourceEdgeGatewayId, rollback=False):
        """
        Description : Disconnect source Org VDC network from edge gateway
        Parameters  : orgVdcNetworkList - Org VDC's network list for a specific Org VDC (LIST)
                      rollback - key that decides whether to perform rollback or not (BOOLEAN)
        """
        # list of networks disconnected successfully
        networkDisconnectedList = []
        orgVDCNetworksErrorList = []

        try:
            # Check if source org vdc network disconnection was performed
            if rollback and (self.rollback.metadata.get("configureTargetVDC") == None and self.rollback.executionResult.get("configureTargetVDC") == None):
                return

            if not sourceEdgeGatewayId:
                logger.debug('Skipping disconnecting/reconnecting soruce org VDC '
                             'networks as edge gateway does not exists')
                return

            if not rollback:
                logger.info('Disconnecting source routed Org VDC Networks from source Edge gateway.')
            else:
                logger.info('Rollback: Reconnecting Source Org VDC Network to Edge Gateway')
            # iterating over the org vdc network list
            for orgVdcNetwork in orgVDCNetworkList:
                # checking only for nat routed Org VDC Network
                if orgVdcNetwork['networkType'] == "NAT_ROUTED":
                    # url to disconnect org vdc networks
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS, orgVdcNetwork['id'])
                    response = self.restClientObj.get(url, self.headers)
                    responseDict = response.json()
                    # creating payload data as per the requirements
                    responseDict['connection'] = None
                    responseDict['networkType'] = 'ISOLATED'
                    del responseDict['status']
                    del responseDict['lastTaskFailureMessage']
                    del responseDict['retainNicResources']
                    del responseDict['crossVdcNetworkId']
                    del responseDict['crossVdcNetworkLocationId']
                    del responseDict['totalIpCount']
                    del responseDict['usedIpCount']
                    if rollback:
                        responseDict['connection'] = orgVdcNetwork['connection']
                        responseDict['networkType'] = 'NAT_ROUTED'
                    payloadDict = json.dumps(responseDict)
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api to disconnect the org vdc networks
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadDict)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the disconnecting org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        if not rollback:
                            logger.debug(
                                'Source Org VDC Network {} disconnected successfully.'.format(orgVdcNetwork['name']))
                            # saving network on successful disconnection to list
                            networkDisconnectedList.append(orgVdcNetwork)
                        else:
                            logger.debug(
                                'Source Org VDC Network {} reconnected successfully.'.format(orgVdcNetwork['name']))
                    else:
                        if rollback:
                            logger.debug('Rollback: Failed to reconnect Source Org VDC Network {}.'.format(
                                orgVdcNetwork['name']))
                        else:
                            logger.debug(
                                'Failed to disconnect Source Org VDC Network {} due to error.'.format(orgVdcNetwork['name'], ))
                        orgVDCNetworksErrorList.append(orgVdcNetwork['name'])
                if orgVDCNetworksErrorList:
                    raise Exception('Failed to disconnect Org VDC Networks {}'.format(orgVDCNetworksErrorList))
        except Exception as exception:
            # reconnecting the networks in case of disconnection failure
            if networkDisconnectedList:
                self.disconnectSourceOrgVDCNetwork(networkDisconnectedList, sourceEdgeGatewayId, rollback=True)
                self.dhcpRollBack(networkDisconnectedList)
            raise exception

    @description("Setting source edge gateway static route interfaces to None")
    @remediate
    def setStaticRoutesInterfaces(self, rollback=False):
        """
        Description : Set the interfaces of source edge gateway static routes to None before disconnecting any org vdc network
        """
        if float(self.version) < float(vcdConstants.API_VERSION_BETELGEUSE_10_4):
            return
        filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
        staticRoutes = self.rollback.apiData.get('sourceStaticRoutes', {})
        for sourceEdgeGateway in self.rollback.apiData.get('sourceEdgeGateway', []):
            internalStaticRoutes = staticRoutes.get(sourceEdgeGateway['name'], [])
            if not internalStaticRoutes:
                continue
            sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
            staticRouteConfig = self.getStaticRoutesDetails(sourceEdgeGatewayId, Migration=True)
            edgeGatewayStaticRouteDict = staticRouteConfig["staticRoutes"].get("staticRoutes", [])
            # url to retrieve the routing config info
            url = "{}{}/{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                     vcdConstants.NETWORK_EDGES, sourceEdgeGatewayId, vcdConstants.VNIC)
            # get api call to retrieve the edge gateway config info
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = self.vcdUtils.parseXml(response.content)
                vNicsDetails = responseDict['vnics']['vnic']
            else:
                raise Exception("Failed to get edge gateway {} vnic details".format(sourceEdgeGatewayId))
            for internalStaticRoute in internalStaticRoutes:
                for edgeGatewayStaticRoute in edgeGatewayStaticRouteDict:
                    if internalStaticRoute.get('network') == edgeGatewayStaticRoute.get('network') and \
                            internalStaticRoute.get('nextHop') == edgeGatewayStaticRoute.get('nextHop'):
                        if not rollback:
                            edgeGatewayStaticRoute.pop('vnic', None)
                        else:
                            for vnicData in vNicsDetails:
                                if "portgroupName" in vnicData.keys() and vnicData.get('portgroupName') == internalStaticRoute.get('interface'):
                                    edgeGatewayStaticRoute['vnic'] = vnicData["index"]
            url = '{}{}?async=true'.format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                vcdConstants.STATIC_ROUTING_CONFIG.format(sourceEdgeGatewayId))
            payloadData = self.vcdUtils.createPayload(
                                filePath,
                                payloadDict={'staticRouteConfig': staticRouteConfig},
                                fileType='yaml',
                                componentName=vcdConstants.COMPONENT_NAME,
                                templateName=vcdConstants.STATIC_ROUTE_INTERFACE_TEMPLATE
                        )
            headers = {'Authorization': self.headers['Authorization'],
                       'Content-Type': vcdConstants.GENERAL_XML_CONTENT_TYPE}
            response = self.restClientObj.put(url, headers, data=json.loads(payloadData))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkJobStatus(taskUrl=taskUrl)
                if not rollback:
                    logger.debug(f'Successfully removed static routes interface of {sourceEdgeGateway["name"]}')
                else:
                    logger.debug(f'Successfully restored static routes interface of {sourceEdgeGateway["name"]}')
            else:
                raise Exception("Failed to set interface of static route to None")

    @description("disconnection of source Edge gateway from external network")
    @remediate
    def reconnectOrDisconnectSourceEdgeGateway(self, sourceEdgeGatewayIdList, connect=True):
        """
        Description :  Disconnect source Edge Gateways from the specified OrgVDC
        Parameters  :   sourceEdgeGatewayId -   Id of the Organization VDC Edge gateway (STRING)
                        connect             -   Defaults to True meaning reconnects the source edge gateway (BOOL)
                                            -   if set False meaning disconnects the source edge gateway (BOOL)
        """
        try:
            # Check if services configuration or network switchover was performed or not
            if connect and not self.rollback.metadata.get("configureTargetVDC", {}).get("reconnectOrDisconnectSourceEdgeGateway"):
                return

            if not sourceEdgeGatewayIdList:
                logger.debug('Skipping disconnecting/reconnecting source Edge '
                             'gateway from external network as it does not exists')
                return

            if not connect:
                logger.info('Disconnecting source Edge gateway from external network.')
            else:
                logger.info('Rollback: Reconnecting source Edge gateway to external network.')

            for sourceEdgeGatewayId in sourceEdgeGatewayIdList:
                # Fetching edge gateway details from metadata corresponding to edge gateway id
                edgeGatewaydata = \
                    list(filter(lambda edgeGatewayData: edgeGatewayData['id'] == sourceEdgeGatewayId,
                                copy.deepcopy(self.rollback.apiData['sourceEdgeGateway'])))[0]
                orgVDCEdgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                # url to disconnect/reconnect the source edge gateway
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                responseDict = response.json()
                if response.status_code == requests.codes.ok:
                    if not responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][0][
                        'connected'] and not connect:
                        logger.warning(
                            'Source Edge Gateway external network uplink - {} is already in disconnected state.'.format(
                                responseDict['name']))
                        continue
                    # establishing/disconnecting the edge gateway as per the connect flag
                    if not connect:
                        for i in range(len(responseDict['configuration']['gatewayInterfaces']['gatewayInterface'])):
                            if responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i]['interfaceType'] == 'uplink' and \
                                    responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i]['name'] != self.rollback.apiData['dummyExternalNetwork']['name']:
                                responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i]['connected'] = False
                    elif any([data['connected'] for data in edgeGatewaydata['edgeGatewayUplinks']]):
                        for i in range(len(responseDict['configuration']['gatewayInterfaces']['gatewayInterface'])):
                            if responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i][
                                'interfaceType'] == 'uplink' and responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i]['name'] != \
                                    self.rollback.apiData['dummyExternalNetwork']['name']:
                                responseDict['configuration']['gatewayInterfaces']['gatewayInterface'][i]['connected'] = True

                        for index, uplink in enumerate(responseDict['configuration']['gatewayInterfaces']['gatewayInterface']):
                            if uplink['interfaceType'] == 'internal':
                                responseDict['configuration']['gatewayInterfaces']['gatewayInterface'].pop(index)
                                #responseDict['configuration']['gatewayInterfaces']['gatewayInterface'].pop()
                    else:
                        continue
                    payloadData = json.dumps(responseDict)
                    acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
                    self.headers["Content-Type"] = vcdConstants.XML_UPDATE_EDGE_GATEWAY
                    headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                               'Content-Type': vcdConstants.JSON_UPDATE_EDGE_GATEWAY}
                    # updating the details of the edge gateway
                    response = self.restClientObj.put(url + '/action/updateProperties', headers, data=payloadData)
                    responseData = response.json()
                    if response.status_code == requests.codes.accepted:
                        taskUrl = responseData["href"]
                        if taskUrl:
                            # checking the status of connecting/disconnecting the edge gateway
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug('Source Edge Gateway updated successfully.')
                            continue
                    else:
                        raise Exception('Failed to update source Edge Gateway {}'.format(responseData['message']))
                else:
                    raise Exception("Failed to get edge gateway '{}' details due to error - {}".format(
                        edgeGatewaydata['name'], responseDict['message']))
        except:
            raise

    @description("Reconnection of target Edge gateway to T0 router")
    @remediate
    def reconnectTargetEdgeGateway(self, reconnect=True):
        """
        Description : Reconnect Target Edge Gateway to T0 router
        """
        try:
            if not self.rollback.apiData.get('targetEdgeGateway'):
                logger.debug('Skipping reconnecting target Edge gateway to T0 router'
                             ' as it does not exists')
                return

            if reconnect:
                logger.info('Reconnecting target Edge gateway to T0 router.')
            else:
                logger.info('Disconnecting target Edge gateway from T0 router.')
            data = self.rollback.apiData
            for targetEdgeGateway in data['targetEdgeGateway']:
                payloadDict = targetEdgeGateway
                if reconnect:
                    del payloadDict['status']
                    if self.rollback.apiData.get('OrgVDCGroupID', {}).get(targetEdgeGateway['id']):
                        ownerRef = self.rollback.apiData['OrgVDCGroupID'].get(targetEdgeGateway['id'])
                        payloadDict['ownerRef'] = {'id': ownerRef}
                    if targetEdgeGateway['name'] in data['isT0Connected']:
                        payloadDict['edgeGatewayUplinks'][0]['connected'] = reconnect
                else:
                    payloadDict['edgeGatewayUplinks'] = [payloadDict['edgeGatewayUplinks'][0]]
                    payloadDict['edgeGatewayUplinks'][0]['connected'] = reconnect

                # edge gateway update URL
                url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EDGE_GATEWAYS,
                                       targetEdgeGateway['id'])
                # creating the payload data
                payloadData = json.dumps(payloadDict)
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                # put api to reconnect the target edge gateway
                response = self.restClientObj.put(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of the reconnecting target edge gateway task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug(
                        'Target Org VDC Edge Gateway {} reconnected/disconnected successfully.'.format(targetEdgeGateway['name']))
                    continue
                else:
                    raise Exception(
                        'Failed to reconnect target Org VDC Edge Gateway {} {}'.format(targetEdgeGateway['name'],
                                                                                       response.json()['message']))
            if reconnect:
                logger.info('Successfully reconnected target Edge gateway to T0 router.')
            else:
                logger.info('Successfully disconnected target Edge gateway to T0 router.')
        except:
            raise

    @description("getting the portgroup of source org vdc networks")
    @remediate
    def getPortgroupInfo(self, orgVdcNetworkList):
        """
        Description : Get Portgroup Info
        Parameters  : orgVdcNetworkList - List of source org vdc networks (LIST)
                      vcenterObj - Object of vcenterApis module (Object)
        """
        try:
            logger.info('Getting the portgroup of source org vdc networks.')
            data = self.rollback.apiData

            # making a list of non-direct networks
            NonDirectNetworks = [network for network in orgVdcNetworkList if network["networkType"] != "DIRECT"]
            # Fetching name and ids of all the org vdc networks
            networkIdMapping, networkNameList = dict(), set()
            for orgVdcNetwork in NonDirectNetworks:
                networkIdMapping[orgVdcNetwork['id'].split(":")[-1]] = orgVdcNetwork
                networkNameList.add(orgVdcNetwork['name'])

            allPortGroups = self.fetchAllPortGroups()
            portGroupDict = defaultdict(list)
            # Iterating over all the port groups to find the portgroups linked to org vdc network
            for portGroup in allPortGroups:
                if portGroup['networkName'] != '--' and \
                        portGroup['scopeType'] not in ['-1', '1'] and \
                        portGroup['networkName'] in networkNameList and \
                        portGroup['network'].split('/')[-1] in networkIdMapping.keys():
                    portGroupDict[portGroup['networkName']].append({"moref": portGroup["moref"],
                                                                   "networkName": portGroup["networkName"]})

            # Saving portgroups data to metadata data structure
            data['portGroupList'] = list(portGroupDict.values())
            logger.info('Retrieved the portgroup of source org vdc networks.')
            return
        except:
            raise

    @isSessionExpired
    def createMoveVappVmPayload(self, vApp, targetOrgVDCId, rollback=False):
        """
        Description : Create vApp vm payload for move vApp api
        Parameters : vApp - dict containing source vApp details
                     targetOrgVDCId - target Org VDC Id (STRING)
                     rollback - whether to rollback vapp from T2V (BOOLEAN)
        """
        try:
            xmlPayloadData = ''
            data = self.rollback.apiData
            if rollback:
                targetStorageProfileList = [
                    data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                    data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                data["sourceOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']
            else:
                targetStorageProfileList = [
                    data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                    data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                data["targetOrgVDC"]['VdcStorageProfiles']['VdcStorageProfile']
            vmInVappList = []
            # get api call to retrieve the info of source vapp
            response = self.restClientObj.get(vApp['@href'], self.headers)
            responseDict = self.vcdUtils.parseXml(response.content)
            if not responseDict['VApp'].get('Children'):
                return
            targetSizingPolicyOrgVDCUrn = 'urn:vcloud:vdc:{}'.format(targetOrgVDCId)
            vmList = listify(responseDict['VApp']['Children']['Vm'])
            networkTypes = {
                vAppNetwork['@networkName']: vAppNetwork['Configuration']['FenceMode']
                for vAppNetwork in listify(responseDict['VApp']['NetworkConfigSection'].get('NetworkConfig', []))
            }
            # iterating over the vms in vapp
            for vm in vmList:
                # retrieving the compute policy of vm
                computePolicyName = vm['ComputePolicy']['VmPlacementPolicy']['@name'] if vm['ComputePolicy'].get(
                    'VmPlacementPolicy') else None
                # retrieving the compute policy id of vm
                computePolicyId = vm['ComputePolicy']['VmPlacementPolicy']['@id'] if vm['ComputePolicy'].get(
                    'VmPlacementPolicy') else None

                # Retrieving the Disk storage policy details
                diskSection = []
                for diskSetting in listify(vm['VmSpecSection']['DiskSection']['DiskSettings']):
                    if diskSetting['overrideVmDefault'] == 'true':
                        diskSection = listify(vm['VmSpecSection']['DiskSection']['DiskSettings'])
                        break

                # Retrieving hardware version
                if diskSection:
                    hardwareVersion = vm['VmSpecSection']['HardwareVersion']
                else:
                    hardwareVersion = None

                # retrieving the sizing policy of vm
                if vm['ComputePolicy'].get('VmSizingPolicy'):
                    if vm['ComputePolicy']['VmSizingPolicy']['@name'] != 'System Default':
                        sizingPolicyHref = vm['ComputePolicy']['VmSizingPolicy']['@href']
                    else:
                        # get the target System Default policy id
                        defaultSizingPolicy = self.getVmSizingPoliciesOfOrgVDC(targetSizingPolicyOrgVDCUrn,
                                                                               isTarget=True)
                        if defaultSizingPolicy:
                            defaultSizingPolicyId = defaultSizingPolicy[0]['id']
                            sizingPolicyHref = "{}{}/{}".format(
                                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.VDC_COMPUTE_POLICIES, defaultSizingPolicyId)
                        else:
                            sizingPolicyHref = None
                else:
                    sizingPolicyHref = None
                storageProfileList = [storageProfile for storageProfile in targetStorageProfileList if
                                      storageProfile['@name'] == vm['StorageProfile']['@name']]
                if storageProfileList:
                    storageProfileHref = storageProfileList[0]['@href']
                else:
                    storageProfileHref = ''

                # gathering the vm's data required to create payload data and appending the dict to the 'vmInVappList'.
                # update primaryNetworkConnectionIndex value for No NIC present at VM level set default value None
                vmInVappList.append(
                    {'name': vm['@name'], 'description': vm['Description'] if vm.get('Description') else '',
                    'href': vm['@href'], 'networkConnectionSection': vm['NetworkConnectionSection'],
                    'storageProfileHref': storageProfileHref, 'state': responseDict['VApp']['@status'],
                    'computePolicyName': computePolicyName, 'computePolicyId': computePolicyId,
                    'sizingPolicyHref': sizingPolicyHref,
                    'primaryNetworkConnectionIndex': vm['NetworkConnectionSection'].get('PrimaryNetworkConnectionIndex'),
                    'diskSection': diskSection if diskSection else None,
                    'hardwareVersion': hardwareVersion})
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # iterating over the above saved vms list of source vapp
            for vm in vmInVappList:
                logger.debug('Getting VM - {} details'.format(vm['name']))
                # check whether the vapp state is powered on i.e 4 then poweron else poweroff
                if vm['state'] != "4":
                    state = "false"
                else:
                    state = "true"
                networkConnectionList = listify(vm['networkConnectionSection'].get('NetworkConnection', []))
                networkConnectionPayloadData = ''
                # creating payload for mutiple/single network connections in a vm
                for networkConnection in networkConnectionList:
                    if networkConnection['@network'] == 'none':
                        networkName = 'none'
                    elif networkTypes.get(networkConnection['@network']) in ('natRouted', 'isolated'):
                        networkName = networkConnection['@network']
                    else:
                        if rollback:
                            # remove the appended -v2t from network name
                            networkName = networkConnection['@network'].replace('-v2t', '')
                        else:
                            networkName = networkConnection['@network'] + '-v2t'

                    # checking for the 'IpAddress' attribute if present
                    if networkConnection.get('IpAddress'):
                        ipAddress = networkConnection['IpAddress']
                    else:
                        ipAddress = ""

                    # Check ip allocation mode for vm's
                    if networkConnection['IpAddressAllocationMode'] == 'POOL' and \
                            float(self.version) <= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_1):
                        networkConnection['IpAddressAllocationMode'] = 'MANUAL'
                    payloadDict = {
                        'networkName': networkName,
                        'needsCustomization': networkConnection.get('@needsCustomization', 'false'),
                        'ipAddress': ipAddress,
                        'IpType': networkConnection.get('IpType', ''),
                        'ExternalIpAddress': networkConnection.get('ExternalIpAddress', ''),
                        'connected': networkConnection['IsConnected'],
                        'macAddress': networkConnection['MACAddress'],
                        'allocationModel': networkConnection['IpAddressAllocationMode'],
                        'SecondaryIpAddressAllocationMode': networkConnection.get('SecondaryIpAddressAllocationMode', 'NONE'),
                        'adapterType': networkConnection['NetworkAdapterType'],
                        'networkConnectionIndex': networkConnection['NetworkConnectionIndex']
                        }
                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.VAPP_VM_NETWORK_CONNECTION_SECTION_TEMPLATE)
                    networkConnectionPayloadData += payloadData.strip("\"")
                # getting diskSection data
                vAppVMDiskStorageProfileData = ''
                if vm['diskSection']:
                    payloadDict = {}
                    diskSection = []
                    for diskSetting in vm['diskSection']:
                        diskSettingDict = {
                            "DiskId": diskSetting['DiskId'],
                            "SizeMb": diskSetting['SizeMb'],
                            "UnitNumber": diskSetting['UnitNumber'],
                            "BusNumber": diskSetting['BusNumber'],
                            "AdapterType": diskSetting['AdapterType'],
                            "ThinProvisioned": diskSetting['ThinProvisioned'],
                            "Disk": diskSetting.get('Disk', {}).get('@href'),    # present in named disk
                            "overrideVmDefault":diskSetting['overrideVmDefault'],
                            "VirtualQuantityUnit": diskSetting['VirtualQuantityUnit'],
                            "resizable": diskSetting['resizable'],
                            "encrypted": diskSetting['encrypted'],
                            "shareable": diskSetting['shareable'],
                            "sharingType": diskSetting['sharingType'],
                        }
                        if float(self.version) < float(vcdConstants.API_VERSION_BETELGEUSE_10_4):
                            diskSettingDict["iops"] = diskSetting['iops']
                        else:
                            diskSettingDict["IopsAllocation"] = diskSetting['IopsAllocation']

                        for storagePolicy in targetStorageProfileList:
                            if storagePolicy['@name'] == diskSetting['StorageProfile']['@name']:
                                diskSettingDict["StorageProfile"] = {"href": storagePolicy['@href'],
                                                                     "id": storagePolicy['@id'],
                                                                     "type": storagePolicy['@type'],
                                                                     "name": storagePolicy['@name']}
                                break
                        else:
                            raise Exception("Could not find disk storage policy {} in target Org VDC.".
                                            format(storagePolicy['@name']))
                        diskSection.append(diskSettingDict)

                    hardwareVersionDict = {"href": vm['hardwareVersion']['@href'],
                                           "type": vm['hardwareVersion']['@type'],
                                           "text": vm['hardwareVersion']['#text']}
                    payloadDict["modifyVmSpecSection"] = "true"
                    payloadDict["hardwareVersion"] = hardwareVersionDict
                    payloadDict['DiskSection'] = diskSection

                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.VAPP_VM_DISK_STORAGE_POLICY_TEMPLATE)
                    vAppVMDiskStorageProfileData = payloadData.strip("\"")

                else:
                    vAppVMDiskStorageProfileData = None

                # handling the case:- if both compute policy & sizing policy are absent
                # update primaryNetworkConnectionIndex value for No NIC present at VM level set default value None
                if not vm["computePolicyName"] and not vm['sizingPolicyHref']:
                    payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                   'storageProfileHref': vm['storageProfileHref'],
                                   'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                   'vAppVMDiskStorageProfileDetails': vAppVMDiskStorageProfileData,
                                   'primaryNetworkConnectionIndex': vm['networkConnectionSection'].get('PrimaryNetworkConnectionIndex')
                                   }
                    payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.MOVE_VAPP_VM_TEMPLATE)
                # handling the case:- if either policy is present
                else:
                    # handling the case:- if compute policy is present and sizing policy is absent
                    if vm["computePolicyName"] and not vm['sizingPolicyHref']:
                        # retrieving the org vdc compute policy
                        allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
                        # getting the list instance of compute policies of org vdc
                        orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(
                            allOrgVDCComputePolicesList, dict) else allOrgVDCComputePolicesList
                        if rollback:
                            targetProviderVDCid = data['sourceProviderVDC']['@id']
                        else:
                            targetProviderVDCid = data['targetProviderVDC']['@id']
                        # iterating over the org vdc compute policies
                        for eachComputPolicy in orgVDCComputePolicesList:
                            # checking if the org vdc compute policy name is same as the source vm's applied compute policy & org vdc compute policy id is same as that of target provider vdc's id
                            if eachComputPolicy["name"] == vm["computePolicyName"] and not eachComputPolicy["isSizingOnly"]:
                                if not eachComputPolicy["pvdcId"]:
                                    if vm['computePolicyId'] == eachComputPolicy['id']:
                                        # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                        href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                vcdConstants.VDC_COMPUTE_POLICIES,
                                                                eachComputPolicy["id"])
                                        break
                                elif eachComputPolicy["pvdcId"] == targetProviderVDCid:
                                    # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                    href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.VDC_COMPUTE_POLICIES,
                                                            eachComputPolicy["id"])
                        # if vm's compute policy does not match with org vdc compute policy or org vdc compute policy's id does not match with target provider vdc's id then href will be set none
                        # resulting into raising the exception that source vm's applied placement policy is absent in target org vdc
                        if not href:
                            raise Exception(
                                'Could not find placement policy {} in target Org VDC.'.format(vm["computePolicyName"]))
                        # creating the payload dictionary
                        # update primaryNetworkConnectionIndex value for No NIC present at VM level set default value None
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'vmPlacementPolicyHref': href,
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'vAppVMDiskStorageProfileDetails': vAppVMDiskStorageProfileData,
                                       'primaryNetworkConnectionIndex': vm['networkConnectionSection'].get('PrimaryNetworkConnectionIndex')
                                       }
                        # creating the payload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_PLACEMENT_POLICY_TEMPLATE)
                    # handling the case:- if sizing policy is present and compute policy is absent
                    elif vm['sizingPolicyHref'] and not vm["computePolicyName"]:
                        # creating the payload dictionary
                        # update primaryNetworkConnectionIndex value for No NIC present at VM level set default value None
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'sizingPolicyHref': vm['sizingPolicyHref'],
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'vAppVMDiskStorageProfileDetails': vAppVMDiskStorageProfileData,
                                       'primaryNetworkConnectionIndex': vm['networkConnectionSection'].get('PrimaryNetworkConnectionIndex')
                                       }
                        # creating the payload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_SIZING_POLICY_TEMPLATE)
                    # handling the case:- if both policies are present
                    elif vm['sizingPolicyHref'] and vm["computePolicyName"]:
                        # retrieving the org vdc compute policy
                        allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
                        # getting the list instance of compute policies of org vdc
                        orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(
                            allOrgVDCComputePolicesList, dict) else allOrgVDCComputePolicesList
                        if rollback:
                            targetProviderVDCid = data['sourceProviderVDC']['@id']
                        else:
                            targetProviderVDCid = data['targetProviderVDC']['@id']
                        # iterating over the org vdc compute policies
                        for eachComputPolicy in orgVDCComputePolicesList:
                            # checking if the org vdc compute policy name is same as the source vm's applied compute policy & org vdc compute policy id is same as that of target provider vdc's id
                            if eachComputPolicy["name"] == vm["computePolicyName"] and not eachComputPolicy["isSizingOnly"]:
                                if not eachComputPolicy["pvdcId"]:
                                    if vm['computePolicyId'] == eachComputPolicy['id']:
                                        # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                        href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                vcdConstants.VDC_COMPUTE_POLICIES,
                                                                eachComputPolicy["id"])
                                        break
                                elif eachComputPolicy["pvdcId"] == targetProviderVDCid:
                                    # creating the href of compute policy that should be passed in the payload data for recomposing the vapp
                                    href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.VDC_COMPUTE_POLICIES,
                                                            eachComputPolicy["id"])
                        # if vm's compute policy does not match with org vdc compute policy or org vdc compute policy's id does not match with target provider vdc's id then href will be set none
                        # resulting into raising the exception that source vm's applied placement policy is absent in target org vdc
                        if not href:
                            raise Exception(
                                'Could not find placement policy {} in target Org VDC.'.format(vm["computePolicyName"]))
                        # creating the payload dictionary
                        # update primaryNetworkConnectionIndex value for No NIC present at VM level set default value None
                        payloadDict = {'vmHref': vm['href'], 'vmDescription': vm['description'], 'state': state,
                                       'storageProfileHref': vm['storageProfileHref'],
                                       'vmPlacementPolicyHref': href, 'sizingPolicyHref': vm['sizingPolicyHref'],
                                       'vmNetworkConnectionDetails': networkConnectionPayloadData,
                                       'vAppVMDiskStorageProfileDetails': vAppVMDiskStorageProfileData,
                                       'primaryNetworkConnectionIndex': vm['networkConnectionSection'].get('PrimaryNetworkConnectionIndex')
                                       }
                        # creating the pauload data
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.MOVE_VAPP_VM_COMPUTE_POLICY_TEMPLATE)
                xmlPayloadData += payloadData.strip("\"")

            return xmlPayloadData
        except Exception:
            raise

    @isSessionExpired
    def getOrgVDCStorageProfileDetails(self, orgVDCStorageProfileId):
        """
        Description :   Gets the details of the specified Org VDC Storage Profile ID
        Parameters  :   orgVDCStorageProfileId -   ID of the Org VDC Storage Profile (STRING)
        Returns     :   Details of the Org VDC Storage Profile (DICTIONARY)
        """
        try:
            logger.debug("Getting Org VDC Storage Profile details of {}".format(orgVDCStorageProfileId))
            # splitting the orgVDCStorageProfileId as per the requirement of the xml api call
            orgVDCStorageProfileId = orgVDCStorageProfileId.split(':')[-1]
            # url to get the vdc storage profile of specified id
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.VCD_STORAGE_PROFILE_BY_ID.format(orgVDCStorageProfileId))
            response = self.restClientObj.get(url, self.headers)
            responseDict = self.vcdUtils.parseXml(response.content)
            return responseDict
        except Exception:
            raise

    @description("Checking ACL on target Org vdc")
    @remediate
    def createACL(self):
        """
        Description : Create ACL on Org VDC
        """
        try:
            logger.info('Checking ACL on target Org vdc')

            data = self.rollback.apiData
            # retrieving the source org vdc id & target org vdc is
            sourceOrgVDCId = data["sourceOrgVDC"]['@id'].split(':')[-1]
            targetOrgVDCId = data["targetOrgVDC"]['@id'].split(':')[-1]
            # url to get the access control in org vdc
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.GET_ACCESS_CONTROL_IN_ORG_VDC.format(sourceOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # get api call to retrieve the access control details in source org vdc
            response = self.restClientObj.get(url, headers)
            data = json.loads(response.content)
            if not data['accessSettings']:
                logger.debug('ACL doesnot exist on source Org VDC')
                return
            # url to create access control in target org vdc
            url = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                vcdConstants.CREATE_ACCESS_CONTROL_IN_ORG_VDC.format(targetOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'Content-Type': vcdConstants.CONTROL_ACCESS_CONTENT_TYPE}
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
            # creating the payload dictionary
            payloadDict = {'isShared': data['isSharedToEveryone'],
                           'everyoneAccess': data['everyoneAccessLevel'] if data['everyoneAccessLevel'] else "Null"}
            # creating the payload data
            payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_ORG_VDC_ACCESS_CONTROL_TEMPLATE)
            accessSettingsList = []
            # iterating over the access settings of source org vdc
            for subjectData in data['accessSettings']['accessSetting']:
                userData = {"subject": {"href": subjectData['subject']['href']},
                            "accessLevel": subjectData['accessLevel']}
                accessSettingsList.append(userData)
            jsonData = json.loads(payloadData)
            # attaching the access settings to the payload data
            jsonData['accessSettings'] = {'accessSetting': accessSettingsList}
            payloadData = json.dumps(jsonData)
            # put api to create access control in target org vdc
            response = self.restClientObj.put(url, headers, data=payloadData)
            if response.status_code != requests.codes.ok:
                responseDict = self.vcdUtils.parseXml(response.content)
                raise Exception(
                    'Failed to create target ACL on target Org VDC {}'.format(responseDict['Error']['@message']))
            logger.info('Successfully created ACL on target Org vdc')
        except Exception:
            raise

    @description("application of vm placement policy on target Org vdc")
    @remediate
    def applyVDCPlacementPolicy(self):
        """
        Description : Applying VM placement policy on vdc
        """
        try:
            data = self.rollback.apiData
            computePolicyHrefList = []
            # retrieving the target org vdc id, target provider vdc id & compute policy list of source from apiOutput.json
            targetOrgVDCId = data['targetOrgVDC']['@id'].split(':')[-1]
            targetProviderVDCId = data['targetProviderVDC']['@id']
            if not data.get('sourceOrgVDCComputePolicyList'):
                logger.debug('No source Org VDC compute Policy exist')
                return
            logger.info('Applying vm placement policy on target Org vdc')
            sourcePolicyList = data['sourceOrgVDCComputePolicyList']
            # getting list instance of sourcePolicyList
            sourceComputePolicyList = [sourcePolicyList] if isinstance(sourcePolicyList, dict) else sourcePolicyList
            allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
            # getting list instance of org vdc compute policies
            orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(allOrgVDCComputePolicesList,
                                                                                   dict) else allOrgVDCComputePolicesList
            # iterating over the org vdc compute policies
            for eachComputePolicy in orgVDCComputePolicesList:
                if (eachComputePolicy["pvdcId"] == targetProviderVDCId or not eachComputePolicy["pvdcId"]) and \
                        not eachComputePolicy["isSizingOnly"]:
                    # if compute policy's id is same as target provider vdc id and compute policy is not the system default
                    if eachComputePolicy["name"] != 'System Default':
                        # iterating over the source compute policies
                        for computePolicy in sourceComputePolicyList:
                            if computePolicy['@name'] == eachComputePolicy['name'] and eachComputePolicy['id'] != \
                                    data['sourceOrgVDC']['DefaultComputePolicy']['@id']:
                                # get api call to retrieve compute policy details
                                response = self.restClientObj.get(computePolicy['@href'], self.headers)
                                if response.status_code == requests.codes.ok:
                                    responseDict = response.json()
                                else:
                                    raise Exception("Failed to retrieve ComputePolicy with error {}".format(responseDict["message"]))
                                if responseDict["pvdcComputePolicy"] == eachComputePolicy["pvdcComputePolicy"]:
                                    # creating the href of the org vdc compute policy
                                    href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.VDC_COMPUTE_POLICIES,
                                                            eachComputePolicy["id"])
                                    computePolicyHrefList.append({'href': href})
            # url to get the compute policy details of target org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_COMPUTE_POLICY.format(targetOrgVDCId))
            acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # get api call to retrieve the target org vdc compute policy details
            response = self.restClientObj.get(url, headers)
            data = json.loads(response.content)
            alreadyPresentComputePoliciesList = []
            payloadDict = {}
            for computePolicy in data['vdcComputePolicyReference']:
                if computePolicy['href'] not in computePolicyHrefList:
                    # getting the list of compute policies which are already
                    alreadyPresentComputePoliciesList.append(
                        {'href': computePolicy['href'], 'id': computePolicy['id'], 'name': computePolicy['name']})
            payloadDict['vdcComputePolicyReference'] = alreadyPresentComputePoliciesList + computePolicyHrefList
            acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                       'Content-Type': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            # creating the payload data
            payloadData = json.dumps(payloadDict)
            response = self.restClientObj.put(url, headers, data=payloadData)
            if response.status_code == requests.codes.ok:
                # there exists atleast single placement policy in source org vdc, so checking the computPolicyHrefList
                if computePolicyHrefList:
                    logger.debug('Successfully applied vm placement policy on target VDC')
            else:
                raise Exception(
                    'Failed to apply vm placement policy on target VDC {}'.format(response.json()['message']))
        except Exception:
            # setting the delete target org vdc flag
            self.DELETE_TARGET_ORG_VDC = True
            raise

    @description("Enabling Affinity Rules in Target VDC")
    @remediate
    def enableTargetAffinityRules(self, rollback=False):
        """
        Description :   Enable Affinity Rules in Target VDC
        """
        try:
            threading.current_thread().name = self.vdcName
            # Check if migrate vApp was performed as a part of migration
            if rollback and not self.rollback.metadata.get("enableTargetAffinityRules"):
                return

            data = self.rollback.apiData
            # reading the data from the apiOutput.json
            targetOrgVdcId = data['targetOrgVDC']['@id']
            targetvdcid = targetOrgVdcId.split(':')[-1]
            # checking if affinity rules present in source
            if data.get('sourceVMAffinityRules'):
                logger.info('Configuring target Org VDC affinity rules')
                sourceAffinityRules = data['sourceVMAffinityRules'] if isinstance(data['sourceVMAffinityRules'],
                                                                                  list) else [
                    data['sourceVMAffinityRules']]
                # iterating over the affinity rules
                for sourceAffinityRule in sourceAffinityRules:
                    affinityID = sourceAffinityRule['@id']
                    # url to enable/disable the affinity rules
                    # url = vcdConstants.ENABLE_DISABLE_AFFINITY_RULES.format(self.ipAddress, affinityID)
                    url = "{}{}".format(vcdConstants.AFFINITY_URL.format(self.ipAddress, targetvdcid), affinityID)
                    filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
                    vmReferencesPayloadData = ''
                    for eachVmReference in sourceAffinityRule['VmReferences']['VmReference']:
                        payloadDict = {'vmHref': eachVmReference['@href'],
                                       'vmId': eachVmReference['@id'],
                                       'vmName': eachVmReference['@name'],
                                       'vmType': eachVmReference['@type']}
                        payloadData = self.vcdUtils.createPayload(filePath,
                                                                  payloadDict,
                                                                  fileType='yaml',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.VM_REFERENCES_TEMPLATE_NAME)
                        vmReferencesPayloadData += payloadData.strip("\"")
                    if rollback:
                        isEnabled = "false"
                    else:
                        isEnabled = "true" if sourceAffinityRule['IsEnabled'] == "true" else "false"
                    payloadDict = {'affinityRuleName': sourceAffinityRule['Name'],
                                   'isEnabled': isEnabled,
                                   'isMandatory': "true" if sourceAffinityRule['IsMandatory'] == "true" else "false",
                                   'polarity': sourceAffinityRule['Polarity'],
                                   'vmReferences': vmReferencesPayloadData}
                    payloadData = self.vcdUtils.createPayload(filePath,
                                                              payloadDict,
                                                              fileType='yaml',
                                                              componentName=vcdConstants.COMPONENT_NAME,
                                                              templateName=vcdConstants.ENABLE_DISABLE_AFFINITY_RULES_TEMPLATE_NAME)
                    payloadData = json.loads(payloadData)
                    self.headers['Content-Type'] = vcdConstants.GENERAL_XML_CONTENT_TYPE
                    # put api call to enable / disable affinity rules
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    responseDict = self.vcdUtils.parseXml(response.content)
                    if response.status_code == requests.codes.accepted:
                        task_url = response.headers['Location']
                        # checking the status of the enabling/disabling affinity rules task
                        self._checkTaskStatus(taskUrl=task_url)
                        logger.debug('Affinity Rules got updated successfully in Target')
                    else:
                        raise Exception(
                            'Failed to update Affinity Rules in Target {}'.format(responseDict['Error']['@message']))
                logger.info('Successfully configured target Org VDC affinity rules')
        except Exception:
            logger.error(traceback.format_exc())
            raise

    @isSessionExpired
    def renameOrgVDC(self, sourceOrgVDCName, targetVDCId):
        """
        Description :   Renames the target Org VDC
        Parameters  :   sourceOrgVDCName    - name of the source org vdc (STRING)
                        targetVDCId         - id of the target org vdc (STRING)
        """
        try:
            # splitting the target org vdc id as per the requirement of xml api
            targetVDCId = targetVDCId.split(':')[-1]
            acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
            headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
            # url to get the target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetVDCId))
            # get api call to retrieve the target org vdc details
            response = self.restClientObj.get(url, headers=headers)
            responseDict = response.json()
            # creating the payload data by just changing the name of org vdc same as source org vdc
            responseDict['name'] = sourceOrgVDCName
            payloadData = json.dumps(responseDict)
            headers['Content-Type'] = vcdConstants.VDC_RENAME_CONTENT_TYPE
            # put api call to update the target org vdc name
            response = self.restClientObj.put(url, headers=headers, data=payloadData)
            responseData = response.json()
            if response.status_code == requests.codes.accepted:
                taskUrl = responseData["href"]
                if taskUrl:
                    # checking the status of renaming target org vdc task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Renamed Org VDC to {} successfully'.format(responseDict['name']))
                return response
            raise Exception("Failed to rename the Org VDC {}".format(responseData['message']))
        except Exception:
            raise

    @isSessionExpired
    def getVmSizingPoliciesOfOrgVDC(self, orgVdcId, isTarget=False):
        """
        Description :   Fetches the list of vm sizing policies assigned to the specified Org VDC
        Parameters  :   orgVdcId    -   ID of the org VDC (STRING)
                        isTarget - True if its target Org VDC else False
        """
        try:
            logger.debug("Getting the VM Sizing Policy of Org VDC {}".format(orgVdcId))
            # url to retrieve the vm sizing policy details of the vm
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_VM_SIZING_POLICY.format(orgVdcId))
            # get api call to retrieve the vm sizing policy of the vm
            response = self.restClientObj.get(url, headers=self.headers)
            responseDict = response.json()
            if response.status_code == requests.codes.ok:
                logger.debug("Retrieved the VM Sizing Policy of Org VDC {} successfully".format(orgVdcId))
                if not isTarget:
                    # getting the source vm sizing policy excluding the policy named 'System Default'
                    sourceOrgVDCSizingPolicyList = [response for response in responseDict['values'] if
                                                    response['name'] != 'System Default']
                else:
                    # getting the source vm sizing policy for the policy named 'System Default'
                    sourceOrgVDCSizingPolicyList = [response for response in responseDict['values'] if
                                                    response['name'] == 'System Default']
                return sourceOrgVDCSizingPolicyList
            raise Exception("Failed to retrieve VM Sizing Policies of Organization VDC {} {}".format(orgVdcId,
                                                                                                     responseDict[
                                                                                                         'message']))
        except Exception:
            raise

    @description("application of vm sizing policy on target Org vdc")
    @remediate
    def applyVDCSizingPolicy(self):
        """
        Description :   Assigns the VM Sizing Policy to the specified OrgVDC
        """
        try:
            logger.info('Applying vm sizing policy on target Org vdc')

            data = self.rollback.apiData
            # retrieving the target org vdc name & id
            targetOrgVdcName = data['targetOrgVDC']['@name']
            targetOrgVdcId = data['targetOrgVDC']['@id']
            # retrieving the source org vdc id
            sourceOrgVdcId = data['sourceOrgVDC']['@id']
            # retrieving the source org vdc vm sizing policy
            sourceSizingPoliciesList = self.getVmSizingPoliciesOfOrgVDC(sourceOrgVdcId)
            if isinstance(sourceSizingPoliciesList, dict):
                sourceSizingPoliciesList = [sourceSizingPoliciesList]
            # iterating over the source org vdc vm sizing policies
            for eachPolicy in sourceSizingPoliciesList:
                # url to assign sizing policies to the target org vdc
                url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.ASSIGN_COMPUTE_POLICY_TO_VDC.format(eachPolicy['id']))
                payloadDict = [{"name": targetOrgVdcName,
                                "id": targetOrgVdcId}]
                # creating the payload data
                payloadData = json.dumps(payloadDict)
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                # post api call to assign the sizing policies to the target org vdc
                response = self.restClientObj.post(url, headers=self.headers, data=payloadData)
                if response.status_code == requests.codes.ok:
                    logger.debug("VM Sizing Policy {} assigned to Org VDC {} successfully".format(eachPolicy['name'],
                                                                                                  targetOrgVdcName))
                else:
                    raise Exception("Failed to assign VM Sizing Policy {} to Org VDC {} {}".format(eachPolicy['name'],
                                                                                                   targetOrgVdcName,
                                                                                                   response.json()[
                                                                                                       'message']))
        except Exception:
            self.DELETE_TARGET_ORG_VDC = True
            raise

    @description("disconnection of target Org VDC Networks")
    @remediate
    def disconnectTargetOrgVDCNetwork(self, rollback=False):
        """
        Description : Disconnect target Org VDC networks
        """
        try:
            if not self.rollback.apiData['sourceEdgeGateway'] or not self.rollback.apiData.get('targetOrgVDC'):
                logger.debug('Skipping Target Org VDC Network disconnection as edge '
                             'gateway does not exist.')
                return

            logger.info('Disconnecting target Org VDC Networks.')
            targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
            targetOrgVDCNetworkList = self.getOrgVDCNetworks(targetOrgVDCId, 'targetOrgVDCNetworks', saveResponse=False)
            # retrieving the target org vdc network list
            for vdcNetwork in targetOrgVDCNetworkList:
                # handling only the routed networks
                if vdcNetwork['networkType'] == "NAT_ROUTED":
                    vdcNetworkID = vdcNetwork['id']
                    # removing security groups first if present
                    if vdcNetwork.get('securityGroups'):
                        vdcNetwork['securityGroups'] = None
                        url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                               vcdConstants.ALL_ORG_VDC_NETWORKS,
                                               vdcNetworkID)
                        payload = json.dumps(vdcNetwork)
                        self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                        # put api call to remove the security group from target org vdc network
                        response = self.restClientObj.put(url, self.headers, data=payload)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            # checking the status of removing the security group from target org vdc network
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug('Removed security groups from target Org VDC network - {} successfully.'.format(vdcNetwork['name']))
                    if rollback:
                        # removing dhcp if present
                        urlDHCP = "{}{}/{}/dhcp".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                        vcdConstants.ALL_ORG_VDC_NETWORKS,
                                                        vdcNetworkID)
                        responseDHCP = self.restClientObj.get(urlDHCP, self.headers)
                        if responseDHCP.status_code == requests.codes.ok:
                            responseDict = responseDHCP.json()
                            if responseDict["enabled"]:
                                # Check if DHCP Binding enabled on Network, if enabled then delete binding first then dhcp
                                if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_1):
                                    self.removeDHCPBinding(vdcNetworkID)

                                responseDel = self.restClientObj.delete(urlDHCP, self.headers)
                                if responseDel.status_code == requests.codes.accepted:
                                    if responseDel.headers.get("Location"):
                                        # checking the status of deleting dhcp task
                                        self._checkTaskStatus(taskUrl=responseDel.headers.get("Location"))
                                        logger.debug(
                                            "DHCP Deleted for network id {} before disconnecting network".format(
                                                vdcNetworkID))
                                else:
                                    logger.debug(
                                        "Failed to delete DHCP from target org vdc network {} - {}".format(vdcNetworkID,
                                                                                                           responseDel.message))
                        else:
                            logger.debug(
                                "Failed to retrieve DHCP state from Target org vdc network {}".format(vdcNetworkID))

                    # url to disconnect the target org vdc network
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS,
                                           vdcNetworkID)

                    # creating the payload data
                    vdcNetwork['connection'] = None
                    vdcNetwork['networkType'] = 'ISOLATED'

                    payloadData = json.dumps(vdcNetwork)
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to disconnect the target org vdc network
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of disconnecting the target org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            'Disconnected target Org VDC network - {} successfully.'.format(vdcNetwork['name']))
                    else:
                        response = response.json()
                        raise Exception('Failed to disconnect target Org VDC network {} - {}'.format(vdcNetwork['name'],
                                                                                                     response[
                                                                                    'message']))
            logger.info('Successfully disconnected target Org VDC Networks.')
        except Exception:
            raise

    @description("Reconnection of target Org VDC Networks")
    @remediate
    def reconnectOrgVDCNetworks(self, sourceOrgVDCId, targetOrgVDCId, source=True):
        """
        Description :   Reconnects the Org VDC networks of source/ target Org VDC
        Parameters  :   source  -   Defaults to True meaning reconnect the Source Org VDC Networks (BOOL)
                                -   if False meaning reconnect the Target Org VDC Networks (BOOL)
        """
        def _isSourceNetworkDistributed(network):
            for sourceOrgVDCNetwork in sourceOrgVDCNetworks:
                if sourceOrgVDCNetwork['name'] + '-v2t' == network['name']:
                    return sourceOrgVDCNetwork['connection']['connectionTypeValue'] == 'DISTRIBUTED'

        try:
            if not self.rollback.apiData['targetEdgeGateway']:
                logger.debug('Reconnecting target Org VDC Networks as edge gateway '
                             'does not exists')
                return

            logger.info('Reconnecting target Org VDC Networks.')
            # get the listener ip configured on all target edge gateways
            listenerIp = self.rollback.apiData.get('listenerIp', {})

            # checking whether to reconnect the org vdc  networks of source or target, and
            # getting the org vdc networks info from metadata
            OrgVDCNetworkList = self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target')
            sourceOrgVDCNetworks = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, orgVDCType='source')
            # iterating over the org vdc networks
            for vdcNetwork in OrgVDCNetworkList:
                # handling only routed networks
                if vdcNetwork['networkType'] == "NAT_ROUTED":
                    # check added for the reconnection of the network which is of type non distributed routed network.
                    # if the network is non distributed then check respective networks connectionType on source side.
                    # and configure the dns IP according to connection type of OrgVDC network.
                    GatewayID = vdcNetwork['connection']['routerRef']['id']
                    if listenerIp.get(GatewayID) and vdcNetwork.get('connection'):
                        if _isSourceNetworkDistributed(vdcNetwork):
                            # When source network is distributed
                            if vdcNetwork['subnets']['values'][0]['dnsServer1'] == vcdConstants.DLR_DNR_IFACE:
                                vdcNetwork['subnets']['values'][0]['dnsServer1'] = listenerIp[GatewayID]
                        else:
                            # When source network is internal routed
                            # and target network is not NON_DISTRIBUTED (applicable from VCD 10.3.2)
                            if (vdcNetwork['subnets']['values'][0]['dnsServer1'] == vdcNetwork['subnets']['values'][0]['gateway']
                                    and vdcNetwork['connection']['connectionTypeValue'] != 'NON_DISTRIBUTED'):
                                vdcNetwork['subnets']['values'][0]['dnsServer1'] = listenerIp[GatewayID]

                    # url to reconnect the org vdc network
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_ORG_VDC_NETWORKS,
                                           vdcNetwork['id'])
                    # creating payload using data from apiOutput.json
                    payloadData = json.dumps(vdcNetwork)
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to reconnect the org vdc
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    srcTgt = "source" if source else "target"
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of recoonecting the specified org vdc
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Reconnected {} Org VDC network - {} successfully.'.format(srcTgt, vdcNetwork['name']))
                    else:
                        response = response.json()
                        raise Exception('Failed to reconnect {} Org VDC network {} - {}'.format(srcTgt, vdcNetwork['name'],
                                                                                                response['message']))
        except Exception:
            raise

    @description("Setting target edge gateway static routes scopes")
    @remediate
    def setStaticRoutesScope(self, rollback=False):
        """
        Description :   Sets target edge gateway static routes scopes
        """
        if float(self.version) < float(vcdConstants.API_VERSION_BETELGEUSE_10_4):
            return
        # getting the org vdc networks info from metadata
        OrgVDCNetworkList = self.rollback.apiData.get('targetOrgVDCNetworks')
        sourceStaticRoutes = copy.deepcopy(self.rollback.apiData.get('sourceStaticRoutes', {}))
        for targetEdgeGateway in self.rollback.apiData.get('targetEdgeGateway', []):
            edgeGatewayID = targetEdgeGateway['id']
            edgeGatewayName = targetEdgeGateway['name']
            targetStaticRoutes = self.getTargetStaticRouteDetails(edgeGatewayID, edgeGatewayName)
            if rollback and targetStaticRoutes:
                sourceStaticRoutes[targetEdgeGateway["name"]] += [{"network": targetStaticRoute["networkCidr"],
                                                                   "nextHop": targetStaticRoute["nextHops"][0]["ipAddress"],
                                                                   "interface": targetStaticRoute["nextHops"][0]["scope"]["name"]}
                                                                  for targetStaticRoute in targetStaticRoutes
                                                                  if targetStaticRoute["networkCidr"] in ["0.0.0.0/1", "128.0.0.0/1"]]
            for targetStaticRoute in targetStaticRoutes:
                for sourceStaticRoute in sourceStaticRoutes.get(edgeGatewayName):
                    if targetStaticRoute["networkCidr"] == sourceStaticRoute["network"]:
                        if sourceStaticRoute.get('interface'):
                            targetStaticRouteID = targetStaticRoute["id"]
                            payloadData = {
                                "name": targetStaticRoute["name"],
                                "description": targetStaticRoute["description"],
                                "networkCidr": targetStaticRoute["networkCidr"],
                                "nextHops": [
                                    {   "ipAddress": targetStaticRoute["nextHops"][0]["ipAddress"],
                                        "adminDistance": targetStaticRoute["nextHops"][0]["adminDistance"],
                                        "scope": {
                                                    "name": sourceStaticRoute['interface'] + '-v2t',
                                                    "id": OrgVDCNetworkList[sourceStaticRoute['interface'] + '-v2t']['id'] if
                                                            sourceStaticRoute['interface'] + '-v2t' in OrgVDCNetworkList else
                                                        self.rollback.apiData['segmentToIdMapping'][sourceStaticRoute['interface'] + '-v2t'],
                                                    "scopeType": "NETWORK"
                                        } if not rollback else None
                                    }
                                ]
                            }
                            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                  vcdConstants.ALL_EDGE_GATEWAYS,
                                                  vcdConstants.TARGET_STATIC_ROUTE_BY_ID.format(edgeGatewayID, targetStaticRouteID))
                            headers = {'Authorization': self.headers['Authorization'],
                                       'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
                            payloadDict = json.dumps(payloadData)
                            response = self.restClientObj.put(url, headers, data=payloadDict)
                            if response.status_code == requests.codes.accepted:
                                taskUrl = response.headers['Location']
                                self._checkTaskStatus(taskUrl=taskUrl)
                                logger.debug("Scope set for static route {} on target edge gateway {}".format(targetStaticRouteID, targetEdgeGateway['name']))
                            else:
                                raise Exception('Failed to set scope of static route')
                            break

    @description("Updating target Edge Gateway NAT rules")
    @remediate
    def updateNATRules(self, rollback=False):
        """
        Description :   Updates the NAT rules created on internal interfaces of source edge gateway
        """
        if float(self.version) < float(vcdConstants.API_VERSION_BETELGEUSE_10_4):
            return
        data = self.rollback.apiData
        targetEdgeGateway = copy.deepcopy(data['targetEdgeGateway'])
        for sourceEdgeGateway in data['sourceEdgeGateway']:
            sourceEdgeGatewayId = sourceEdgeGateway['id'].split(':')[-1]
            if data.get("natInterfaces", {}).get(sourceEdgeGatewayId):
                t1gatewayId = list(filter(lambda edgeGatewayData: edgeGatewayData['name'] == sourceEdgeGateway['name'],
                                          targetEdgeGateway))[0]['id']
                url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                      vcdConstants.ALL_EDGE_GATEWAYS,
                                      vcdConstants.T1_ROUTER_NAT_CONFIG.format(t1gatewayId))
                # rest api call to retrive target edge nat config
                response = self.restClientObj.get(url, headers=self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    natRuleList = responseDict["values"]
                else:
                    raise Exception("Failed to fetch target edge gateway {} nat info".format(sourceEdgeGateway["name"]))

                for natRule in natRuleList:
                    if natRule["name"] not in data["natInterfaces"][sourceEdgeGatewayId]:
                        continue

                    putUrl = "{}{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                vcdConstants.ALL_EDGE_GATEWAYS,
                                                vcdConstants.T1_ROUTER_NAT_CONFIG.format(t1gatewayId),
                                                natRule["id"])
                    payLoad = {
                        "name": natRule.get("name"),
                        "description": natRule.get("description"),
                        "enabled": natRule.get("enabled"),
                        "type": natRule.get("type"),
                        "externalAddresses": natRule.get("externalAddresses"),
                        "internalAddresses": natRule.get("internalAddresses"),
                        "snatDestinationAddresses": natRule.get("snatDestinationAddresses"),
                        "logging": natRule.get("logging"),
                        "priority": natRule.get("priority"),
                        "firewallMatch": natRule.get("firewallMatch"),
                        "applicationPortProfile": natRule.get("applicationPortProfile"),
                        "dnatExternalPort": natRule.get("dnatExternalPort"),
                        "id": natRule.get("id")
                    }
                    if not rollback:
                        if data["natInterfaces"][sourceEdgeGatewayId][natRule["name"]] in data["sourceOrgVDCNetworks"]:
                            payLoad["appliedTo"] = {"id": data["targetOrgVDCNetworks"][
                                data["natInterfaces"][sourceEdgeGatewayId][natRule["name"]] + '-v2t']["id"]}
                        elif data["natInterfaces"][sourceEdgeGatewayId][natRule["name"]] in data.get("isT1Connected", {}).get(sourceEdgeGateway["name"], {}):
                            payLoad["appliedTo"] = {"id": data["segmentToIdMapping"][
                                data["natInterfaces"][sourceEdgeGatewayId][natRule["name"]] + '-v2t']}
                    else:
                        if data["natInterfaces"][sourceEdgeGatewayId][natRule["name"]] + '-v2t' not in data['segmentToIdMapping']:
                            continue
                        payLoad["appliedTo"] = None

                    headers = {'Authorization': self.headers['Authorization'],
                                'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
                    payloadDict = json.dumps(payLoad)
                    response = self.restClientObj.put(putUrl, headers, data=payloadDict)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            "Target NAT rule '{}' on target edge gateway '{}' updated successfully".format(
                                natRule["name"], t1gatewayId))
                    else:
                        raise Exception(
                            'Failed to update NAT rule {} on target edge gateway {}'.format(natRule["name"],
                                                                                            t1gatewayId))

    @isSessionExpired
    def disableDistributedRoutingOnOrgVdcEdgeGateway(self, orgVDCEdgeGatewayId):
        """
        Description :   Disables the Distributed Routing on the specified edge gateway
        Parameters  :   orgVDCEdgeGatewayId -   ID of the edge gateway (STRING)
        """
        try:
            # splitting the edge gateway id as per the requuirements of xml api
            edgeGatewayId = orgVDCEdgeGatewayId.split(':')[-1]
            # url to disable distributed routing on specified edge gateway
            url = "{}{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                  vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(edgeGatewayId),
                                  vcdConstants.DISABLE_EDGE_GATEWAY_DISTRIBUTED_ROUTING)
            # post api call to disable distributed routing on the specified edge gateway
            response = self.restClientObj.post(url, self.headers)
            responseDict = self.vcdUtils.parseXml(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of disabling the edge gateway
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Disabled Distributed Routing on source edge gateway successfully")
            else:
                raise Exception("Failed to disable Distributed Routing on source edge gateway {}".format(responseDict['Error']['@message']))
        except Exception:
            raise

    @description("Update DHCP on Target Org VDC Networks")
    def _updateDhcpInOrgVdcNetworks(self, url, payload):
        """
            Description : Put API request to configure DHCP
            Parameters  : url - URL path (STRING)
                          payload - source dhcp configuration to be updated (DICT)
        """
        try:
            logger.debug('Updating DHCP configuration in OrgVDC network')
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            response = self.restClientObj.put(url, self.headers, data=json.dumps(payload))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                # checking the status of configuring the dhcp on target org vdc networks task
                self._checkTaskStatus(taskUrl=taskUrl)
                # setting the configStatus flag meaning the particular DHCP rule is configured successfully in order to skip its reconfiguration
                logger.debug('DHCP pool created successfully.')
            else:
                errorResponse = response.json()
                raise Exception('Failed to create DHCP  - {}'.format(errorResponse['message']))
        except Exception:
            raise

    @isSessionExpired
    def getEdgeClusterData(self, edgeClusterName, nsxtObj):
        """
                    Description : Get the edge clusters data from edge cluster name
                    Parameters  : edgeClusterName - Name of the edge cluster
                                  nsxtObj - nsxt Object.
        """
        try:
            edgeClusterInfoDict = {}
            # Get Backing ID of edge cluster
            edgeClusterData = nsxtObj.fetchEdgeClusterDetails(edgeClusterName)
            edgeClusterInfoDict['backingId'] = edgeClusterData['id']

            # Get name and ID of edge cluster(STRING format)
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.EDGE_CLUSTER_DATA)
            response = self.restClientObj.get(url, self.headers)

            if response.status_code == requests.codes.ok:
                responseDict = response.json()
                resultTotal = responseDict['resultTotal']
                pageNo = 1
                pageSizeCount = 0
                resultList = []
            else:
                errorDict = response.json()
                raise Exception("Failed to get edge cluster '{}' data, error '{}' ".format(edgeClusterName, errorDict['message']))

            logger.debug('Getting edge cluster details')
            while resultTotal > 0 and pageSizeCount < resultTotal:
                url = "{}{}?page={}&pageSize={}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                                       vcdConstants.EDGE_CLUSTER_DATA, pageNo, 25)
                getSession(self)
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    resultList.extend(responseDict['values'])
                    pageSizeCount += len(responseDict['values'])
                    logger.debug('edge cluster details result pageSize = {}'.format(pageSizeCount))
                    pageNo += 1
                    resultTotal = responseDict['resultTotal']
                else:
                    errorDict = response.json()
                    raise Exception("Failed to get edge cluster '{}' data.".format(errorDict['message']))

            for edgeData in resultList:
                if edgeClusterName == edgeData['name']:
                    edgeClusterInfoDict['name'] = edgeData['name']
                    edgeClusterInfoDict['id'] = edgeData['id']
                    break
            else:
                raise Exception("Edge Gateway Cluster {} data not found in VCD.".format(edgeClusterName))
            return edgeClusterInfoDict
        except:
            raise

    @description("Configure network profile on OrgVDC")
    @remediate
    def updateNetworkProfileOnTarget(self, sourceOrgVDCId, targetOrgVDCID, edgeGatewayDeploymentEdgeCluster, nsxtObj):
        """
            Description : Configure network profile on OrgVDC if dhcp is enabled on isolated vApp network.
            Parameters  : sourceOrgVdcID,   -   Id of the source organization VDC in URN format (STRING)
                          targetOrgVDCId    -   Id of the target organization VDC in URN format (STRING)
                          nsxtObj           -   NSX-T Object
                          edgeGatewayDeploymentEdgeCluster - edge gateway deployment edge cluster.
        """
        logger.debug(
            'Configuring network profile if isolated vApp networks with DHCP or routed vApp networks are present')
        vAppList = self.getOrgVDCvAppsList(sourceOrgVDCId.split(":")[-1])
        if not vAppList:
            return

        for vApp in vAppList:
            response = self.restClientObj.get(vApp['@href'], self.headers)
            responseDict = self.vcdUtils.parseXml(response.content)
            if not response.status_code == requests.codes.ok:
                raise Exception('Error occurred while retrieving vapp details while configuring network profile '
                                'for {} due to {}'.format(vApp['@name'], responseDict['Error']['@message']))

            vAppData = responseDict['VApp']
            if not vAppData['NetworkConfigSection'].get('NetworkConfig'):
                continue

            for vAppNetwork in listify(vAppData['NetworkConfigSection']['NetworkConfig']):
                if (vAppNetwork['Configuration']['FenceMode'] == "natRouted"
                        or vAppNetwork['Configuration'].get('Features', {}).get('DhcpService', {}).get(
                            'IsEnabled') == 'true'):
                    self.configureNetworkProfile(targetOrgVDCID, edgeGatewayDeploymentEdgeCluster, nsxtObj)
                    return

    @isSessionExpired
    def configureNetworkProfile(self, targetOrgVDCId, edgeGatewayDeploymentEdgeCluster=None, nsxtObj=None):
        """
            Description : Configure network profile on target OrgVDC
            Parameters  : targetOrgVDCId    -   Id of the target organization VDC in URN format (STRING)
                          nsxtObj           -   NSX-T Object
                          edgeGatewayDeploymentEdgeCluster - edge gateway deployment edge cluster.
        """
        try:
            logger.debug('Configuring network profile on target orgVDC')
            data = self.rollback.apiData
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.NETWORK_PROFILE.format(targetOrgVDCId))
            if edgeGatewayDeploymentEdgeCluster is not None and len(data['targetEdgeGateway']) == 0:
                edgeClusterData = self.getEdgeClusterData(edgeGatewayDeploymentEdgeCluster, nsxtObj)
                # payload to configure edge cluster details from target edge gateway
                payload = {
                    "servicesEdgeCluster": {
                        "edgeClusterRef": {
                            "name": edgeClusterData['name'],
                            "id": edgeClusterData['id']
                        },
                        "backingId": edgeClusterData['backingId']
                    }
                }
            elif len(data['targetEdgeGateway']) > 0:
                # payload to configure edge cluster details from target edge gateway
                payload = {
                    "servicesEdgeCluster": {
                        "edgeClusterRef": {
                            "name": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['edgeClusterRef']['name'],
                            "id": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['edgeClusterRef']['id']
                        },
                        "backingId": data['targetEdgeGateway'][0]['edgeClusterConfig']['primaryEdgeCluster']['backingId']
                    }
                }
            else:
                raise Exception("Failed to configure network profile on target OrgVDC, As there is no Target EdgeGateway"
                                " and edgeGateway DeploymentEdgeCluster.")
            self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
            response = self.restClientObj.put(url, self.headers, data=json.dumps(payload))
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug('Network profile on target OrgVDC is configured')
            else:
                errorResponce = response.json()
                raise Exception('Failed to configure network profile on target OrgVDC: {}'.format(errorResponce['message']))
        except Exception:
            raise

    def getPools(self, start, end, ipToBeRemove):
        """
            Description : Get the Splitted DHCP pool.
        """
        ipRangeAddresses = [str(ipaddress.IPv4Address(ip)) for ip in
                            range(int(ipaddress.IPv4Address(start)),
                                  int(ipaddress.IPv4Address(end) + 1))]
        splittedDhcpPool = [{'startAddress': '', 'endAddress': ''}]

        if ipToBeRemove not in ipRangeAddresses:
            return None

        if start == ipToBeRemove:
            del ipRangeAddresses[0]
            if ipRangeAddresses:
                splittedDhcpPool[0]['startAddress'] = ipRangeAddresses[0]
        elif end == ipToBeRemove:
            del ipRangeAddresses[-1]
            if ipRangeAddresses:
                splittedDhcpPool[0]['endAddress'] = ipRangeAddresses[-1]
        else:
            ipIndex = ipRangeAddresses.index(ipToBeRemove)
            splittedDhcpPool[0]['startAddress'] = ipRangeAddresses[0]
            splittedDhcpPool[0]['endAddress'] = ipRangeAddresses[ipIndex - 1]
            del ipRangeAddresses[ipIndex]
            remainingIpPool = ipRangeAddresses[ipIndex:]
            if len(remainingIpPool) > 0:
                splittedDhcpPool.extend([{'startAddress': remainingIpPool[0], 'endAddress': remainingIpPool[-1]}])
        return splittedDhcpPool

    def getNewDHCPPool(self, start, end, staticBinding):
        """
            Description : Get the New dhcp pool by splitting existing DHCP pool by binding IP.
                            if the DHCP binding IP belongs to DHCP pool.
        """
        ipRangeAddresses = [str(ipaddress.IPv4Address(ip)) for ip in
                            range(int(ipaddress.IPv4Address(start)),
                                  int(ipaddress.IPv4Address(end) + 1))]
        dhcpPoolData = list()
        # sort the binding Ips in ascending order.
        staticBindingIps = sorted(staticBinding, key=lambda x: [int(m) for m in re.findall("\d+", x)])
        if len(ipRangeAddresses) == 1:
            return None
        else:
            dhcpPools = self.getPools(start, end, staticBindingIps[0])
            if dhcpPools:
                dhcpPoolData.append(dhcpPools[0])
            if len(staticBindingIps) == 1 and len(dhcpPools) > 1:
                dhcpPoolData.append(dhcpPools[1])
                return dhcpPoolData
            start = dhcpPools[1]['startAddress']
            end = dhcpPools[1]['endAddress']
            del staticBindingIps[0]
            # create the new dhcp pool by splitting original dhcp pool by removing binding ip if present.
            for staticBinding in staticBindingIps:
                # get the new pool by splitting existing dhcp pool by dhcp binsing ip.
                dhcpPools = self.getPools(start, end, staticBinding)
                if dhcpPools:
                    dhcpPoolData.append(dhcpPools[0])
                    if len(dhcpPools) > 1:
                        start = dhcpPools[1]['startAddress']
                        end = dhcpPools[1]['endAddress']
            else:
                if len(dhcpPools) > 1:
                    dhcpPoolData.append(dhcpPools[1])
            return dhcpPoolData

    @description("Configuration of DHCP on Target Org VDC Networks")
    @remediate
    def configureDHCP(self, targetOrgVDCId, edgeGatewayDeploymentEdgeCluster=None, nsxtObj=None):
        """
        Description : Configure DHCP on Target Org VDC networks
        Parameters  : targetOrgVDCId    -   Id of the target organization VDC (STRING)
        """
        try:
            logger.debug("Configuring DHCP on Target Org VDC Networks")
            data = self.rollback.apiData
            sourceStaticBindingInfo = dict()
            targetOrgVdcNetworks = self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target')
            for sourceEdgeGatewayDHCP in data['sourceEdgeGatewayDHCP'].values():
                # checking if dhcp is enabled on source edge gateway
                if not sourceEdgeGatewayDHCP['enabled']:
                    logger.debug('DHCP service is not enabled or configured in Source Edge Gateway')
                else:
                    # retrieving the dhcp rules of the source edge gateway
                    sourceDhcpPools = listify(sourceEdgeGatewayDHCP['ipPools'].get('ipPools'))
                    # Retrieving the source DHCP static binding if present
                    if sourceEdgeGatewayDHCP.get('staticBindings'):
                        sourceStaticBindings = listify(sourceEdgeGatewayDHCP['staticBindings']['staticBindings'])
                        for staticBinding in sourceStaticBindings:
                            if staticBinding.get('defaultGateway'):
                                sourceStaticBindingInfo.setdefault(staticBinding['defaultGateway'], []).append(staticBinding['ipAddress'])
                    # iterating over the source edge gateway dhcp rules
                    for iprange in sourceDhcpPools:
                        # if configStatus flag is already set means that the dhcp rule is already configured,
                        # if so then skipping the configuring of same rule and moving to the next dhcp rule
                        if iprange.get('configStatus'):
                            continue

                        start = iprange['ipRange'].split('-')[0]
                        end = iprange['ipRange'].split('-')[-1]
                        ipRangeAddresses = [str(ipaddress.IPv4Address(ip)) for ip in
                                            range(int(ipaddress.IPv4Address(start)),
                                                  int(ipaddress.IPv4Address(end) + 1))]

                        # get the list of Ips which are used for DHCP binding belongs to DHCP pool.
                        staticBindingBelongsToPool = [staticBindingIp for staticBindingIp in sourceStaticBindingInfo.get(iprange['defaultGateway'], []) if staticBindingIp in ipRangeAddresses]
                        # iterating over the target org vdc networks
                        for vdcNetwork in targetOrgVdcNetworks:
                            # handling only the routed networks
                            if not vdcNetwork['networkType'] == "NAT_ROUTED":
                                continue

                            for vdcNet in vdcNetwork['subnets']['values']:
                                networkSubnet = "{}/{}".format(vdcNet['gateway'], vdcNet['prefixLength'])
                                if ipaddress.ip_address(start) not in ipaddress.ip_network(networkSubnet, strict=False):
                                    continue

                                # url to configure dhcp on target org vdc networks
                                url = "{}{}/{}".format(
                                    vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                    vcdConstants.ALL_ORG_VDC_NETWORKS,
                                    vcdConstants.DHCP_ENABLED_FOR_ORG_VDC_NETWORK_BY_ID.format(vdcNetwork['id']))
                                response = self.restClientObj.get(url, self.headers)
                                responseDict = response.json()
                                if response.status_code != requests.codes.ok:
                                    raise Exception(
                                        'Failed to fetch DHCP service - {}'.format(responseDict['message']))

                                dhcpMode, dhcpIpAddress, dhcpPoolEndAddress = None, None, None
                                if (float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_2)
                                        and vdcNetwork['connection']['connectionTypeValue'] == 'NON_DISTRIBUTED'
                                        and responseDict['mode'] != "NETWORK"):
                                    self.configureNetworkProfile(targetOrgVDCId, edgeGatewayDeploymentEdgeCluster)
                                    dhcpMode = "NETWORK"
                                    dhcpIpAddress = end
                                    dhcpPoolEndAddress = str(ipaddress.ip_address(end) - 1)

                                # If DHCP static binding IPs are from DHCP pool then split the existing pool by
                                # removing static binding IPs from the pool.
                                if staticBindingBelongsToPool:
                                    newDhcpPools = []
                                    newDhcpPoolsData = self.getNewDHCPPool(start, dhcpPoolEndAddress or end, staticBindingBelongsToPool)
                                    if newDhcpPoolsData:
                                        for dhcpPoolInfo in newDhcpPoolsData:
                                            newDhcpPools.append({
                                            "enabled": "true" if sourceEdgeGatewayDHCP['enabled'] else "false",
                                            "ipRange": {
                                                "startAddress": dhcpPoolInfo['startAddress'],
                                                "endAddress": dhcpPoolInfo['endAddress']
                                            },
                                            "defaultLeaseTime": 0})
                                else:
                                    # CREATE NEW POOL
                                    newDhcpPools = [{
                                        "enabled": "true" if sourceEdgeGatewayDHCP['enabled'] else "false",
                                        "ipRange": {
                                            "startAddress": start,
                                             "endAddress": dhcpPoolEndAddress or end
                                        },
                                        "defaultLeaseTime": 0
                                    }]
                                newLeaseTime = 4294967295 if iprange['leaseTime'] == "infinite" else iprange['leaseTime']

                                payload = {
                                    'enabled': "true" if sourceEdgeGatewayDHCP['enabled'] else "false",
                                    'dhcpPools':
                                        responseDict['dhcpPools'] + newDhcpPools
                                        if responseDict['dhcpPools']
                                        else newDhcpPools,
                                    'leaseTime':
                                        newLeaseTime
                                        if not responseDict['dhcpPools']
                                        else min(int(responseDict['leaseTime']), int(newLeaseTime)),
                                    'mode': dhcpMode or responseDict['mode'],
                                    'ipAddress': dhcpIpAddress or responseDict['ipAddress'],
                                }

                                # put api call to configure dhcp on target org vdc networks
                                self._updateDhcpInOrgVdcNetworks(url, payload)
                                # setting the configStatus,flag meaning the particular DHCP rule is
                                # configured successfully in order to skip its reconfiguration
                                iprange['configStatus'] = True
                                break

                            # Break from loop and skip next networks when iprange is successfully configured to one
                            # network as one iprange(source dhcp pool) can be configured on only one network
                            if iprange.get('configStatus'):
                                break

            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS) and data.get('OrgVDCIsolatedNetworkDHCP', []) != []:
                data = self.rollback.apiData
                targetOrgVDCNetworksList = data['targetOrgVDCNetworks'].keys()
                self.configureNetworkProfile(targetOrgVDCId, edgeGatewayDeploymentEdgeCluster, nsxtObj)
                self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                for eachDHCPConfig in data['OrgVDCIsolatedNetworkDHCP']:
                    payload = dict()
                    orgVDCNetworkName, OrgVDCIsolatedNetworkDHCPDetails = list(eachDHCPConfig.items())[0]
                    payload["enabled"] = OrgVDCIsolatedNetworkDHCPDetails['enabled']
                    payload["leaseTime"] = OrgVDCIsolatedNetworkDHCPDetails['leaseTime']
                    payload["dhcpPools"] = list()
                    firstPoolIndex = 0
                    maxLeaseTimeDhcp = []
                    if OrgVDCIsolatedNetworkDHCPDetails.get("dhcpPools"):
                        for eachDhcpPool in OrgVDCIsolatedNetworkDHCPDetails["dhcpPools"]:
                            currentPoolDict = dict()
                            currentPoolDict["enabled"] = eachDhcpPool['enabled']
                            if firstPoolIndex == 0:
                                ipToBeRemoved = OrgVDCIsolatedNetworkDHCPDetails["dhcpPools"][0]['ipRange']['startAddress']
                                newStartIpAddress = ipToBeRemoved.split('.')
                                newStartIpAddress[-1] = str(int(newStartIpAddress[-1]) + 1)
                                currentPoolDict["ipRange"] = {"startAddress": '.'.join(newStartIpAddress),
                                                              "endAddress": eachDhcpPool['ipRange']['endAddress']}
                                payload['ipAddress'] = ipToBeRemoved
                                firstPoolIndex += 1
                            else:
                                currentPoolDict["ipRange"] = {"startAddress": eachDhcpPool['ipRange']['startAddress'],
                                                              "endAddress": eachDhcpPool['ipRange']['endAddress']}
                            currentPoolDict["maxLeaseTime"] = eachDhcpPool['maxLeaseTime']
                            currentPoolDict["defaultLeaseTime"] = eachDhcpPool['defaultLeaseTime']
                            maxLeaseTimeDhcp.append(eachDhcpPool['maxLeaseTime'])
                            payload["dhcpPools"].append(currentPoolDict)
                        payload['mode'] = "NETWORK"
                        payload['leaseTime'] = min(maxLeaseTimeDhcp)
                    else:
                        logger.debug('DHCP pools not present in OrgVDC Network: {}'.format(orgVDCNetworkName))
                        continue
                    if orgVDCNetworkName + '-v2t' in targetOrgVDCNetworksList:
                        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                            vcdConstants.ORG_VDC_NETWORK_DHCP.format(
                                                data['targetOrgVDCNetworks'][orgVDCNetworkName + '-v2t']['id']))
                        self._updateDhcpInOrgVdcNetworks(url, payload)
            else:
                logger.debug('Isolated OrgVDC networks not present on source OrgVDC')
        except:
            raise

    @description("Cleanup of IP/s from external network used by direct network")
    @remediate
    def directNetworkIpCleanup(self, source=False, key="directNetworkIP"):
        """
        Description: Remove IP's from used by shared direct networks from external networks
        Parameters: source - Remove the IP's from source external network (BOOL)
        """
        try:
            # Return if there are no ip's to migrate
            if not self.rollback.apiData.get(key):
                return
            # Locking thread as external network can be common
            self.lock.acquire(blocking=True)

            if not source:
                logger.debug("Rollback: Clearing IP's from NSX-T segment backed external network")
            # Iterating over all the networks to migrate the ip's
            for extNetName, ipData in self.rollback.apiData[key].items():
                extNetName = extNetName + '-v2t' if not source else extNetName

                # Fetching source external network
                externalNetworkurl = "{}{}?{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.ALL_EXTERNAL_NETWORKS,
                                                            vcdConstants.EXTERNAL_NETWORK_FILTER.format(
                                                                extNetName))
                # GET call to fetch the External Network details using its name
                response = self.restClientObj.get(externalNetworkurl, headers=self.headers)

                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    extNetData = responseDict.get("values")[0]
                else:
                    raise Exception(f"External Network {extNetName} is not present in vCD")
                if ipData:
                    for ip in set(ipData):
                        # Iterating over subnets in the external network
                        for subnet in extNetData['subnets']['values']:
                            if subnet.get('totalIpCount'):
                                del subnet['totalIpCount']
                            if subnet.get('usedIpCount'):
                                del subnet['usedIpCount']
                            networkAddress = ipaddress.ip_network('{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                                                                  strict=False)
                            # If IP belongs to the network add to ipRange value
                            if ipaddress.ip_address(ip) in networkAddress:
                                ipList = list()
                                for ipRange in subnet['ipRanges']['values']:
                                    ipList.extend(self.createIpRange('{}/{}'.format(subnet['gateway'],
                                                                                    subnet['prefixLength']),
                                                                     ipRange['startAddress'],
                                                                     ipRange['endAddress']))
                                # Removing the IP from the IP list if present
                                if ip in ipList:
                                    ipList.remove(ip)
                                ipRangePayload = self.createExternalNetworkSubPoolRangePayload(ipList)
                                subnet['ipRanges']['values'] = ipRangePayload
                    # url to update external network properties
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_EXTERNAL_NETWORKS, extNetData['id'])
                    # put api call to update external network
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    payloadData = json.dumps(extNetData)

                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        # checking the status of the updating external network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug(
                            'External network {} updated successfully with sub allocated ip pools.'.format(
                                extNetData['name']))
                    else:
                        errorResponse = response.json()
                        msg = "Failed to update External network {} with sub allocated ip pools - {}".format(
                                extNetData['name'], errorResponse['message'])
                        if "provided list 'ipRanges.values' should have at least one" in errorResponse.get("message", ""):
                            msg += " Add one extra IP address to static pool of external network - {}".format(extNetData['name'])
                        raise Exception(msg)
        except:
            logger.error(traceback.format_exc())
            raise
        finally:
            # Releasing thread lock
            try:
                self.lock.release()
            except RuntimeError:
                pass

    @description("Migration of IP/s to segment backed external network")
    @remediate
    def copyIPToSegmentBackedExtNet(self, rollback=False, orgVDCIDList=None, edgeGatewayIpMigration=False, key='directNetworkIP'):
        """
        Description: Migrate the IP assigned to vm connected to shared direct network to segment backed external network
        """
        try:
            # Acquire thread lock
            self.lock.acquire(blocking=True)

            if edgeGatewayIpMigration and self.rollback.apiData.get('isT1Connected', {}):
                self.getIpUsedByEdgeGateway()

            if not rollback and not edgeGatewayIpMigration:
                #Fetching the IP's to be migrated to segment backed external network
                # getting the source org vdc urn
                sourceOrgVDCId = self.rollback.apiData.get('sourceOrgVDC', {}).get('@id', str())
                # getting source network list from metadata
                orgVDCNetworkList = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, orgVDCType='source')
                # Iterating over source org vdc networks to find IP's used by VM's connected to direct shared network
                for sourceOrgVDCNetwork in orgVDCNetworkList:
                    if sourceOrgVDCNetwork['networkType'] == "DIRECT":
                        # url to retrieve the networks with external network id
                        url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.ALL_ORG_VDC_NETWORKS,
                                              vcdConstants.QUERY_EXTERNAL_NETWORK.format(
                                                  sourceOrgVDCNetwork['parentNetworkId']['id']))
                        # get api call to retrieve the networks with external network id
                        response = self.restClientObj.get(url, self.headers)
                        responseDict = response.json()
                        if response.status_code == requests.codes.ok:
                            # Checking the external network backing
                            extNetUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                         vcdConstants.ALL_EXTERNAL_NETWORKS,
                                                         sourceOrgVDCNetwork['parentNetworkId']['id'])
                            extNetResponse = self.restClientObj.get(extNetUrl, self.headers)
                            extNetResponseDict = extNetResponse.json()
                            if extNetResponse.status_code != requests.codes.ok:
                                raise Exception('Failed to get external network {} details with error - {}'.format(
                                    sourceOrgVDCNetwork['parentNetworkId']['name'], extNetResponseDict["message"]))
                            if (int(responseDict['resultTotal']) > 1 and not self.orgVdcInput.get('LegacyDirectNetwork', False)) or \
                                extNetResponseDict['networkBackings']['values'][0]["name"][:7] == "vxw-dvs":
                                sourceOrgVDCNetworkSubnetList = [ipaddress.ip_network('{}/{}'.format(subnet['gateway'], subnet['prefixLength']), strict=False)
                                                                        for subnet in sourceOrgVDCNetwork['subnets']['values']]
                                directNetworkId = sourceOrgVDCNetwork['id'].split(':')[-1]
                                # Fetch the ips used by the VM's linked to this external network for IP migration
                                self.getIPAssociatedUsedByVM(sourceOrgVDCNetwork['name'], directNetworkId,
                                                             sourceOrgVDCNetwork['parentNetworkId']['name'],
                                                             sourceOrgVDCNetworkSubnetList, orgVDCIDList)
                        else:
                            raise Exception('Failed to get direct networks connected to external network {}, '
                                            'due to error -{}'.format(sourceOrgVDCNetwork['parentNetworkId']['name'],
                                                                      responseDict['message']))

            # Return if there are no ip's to migrate
            if not self.rollback.apiData.get(key):
                return

            if rollback:
                logger.debug("Rollback: Copying IP's from NSX-T segment backed external network to source external network")
            else:
                logger.info("Copying IP's to NSX-T segment backed external network")
            # Iterating over all the networks to migrate the ip's
            for extNetName, ipData in self.rollback.apiData[key].items():

                # if not rollback ip's will be added to target nsxt segment backed external network
                if not rollback:
                    extNetName += '-v2t'

                externalNetworkurl = "{}{}?{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                            vcdConstants.ALL_EXTERNAL_NETWORKS,
                                                            vcdConstants.EXTERNAL_NETWORK_FILTER.format(extNetName))
                # GET call to fetch the External Network details using its name
                response = self.restClientObj.get(externalNetworkurl, headers=self.headers)

                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    segmentBackedExtNetData = responseDict.get("values")[0]
                else:
                    raise Exception(f"External Network {extNetName} is not present in vCD")

                for ip in set(ipData):
                    # Iterating over subnets in the external network
                    for subnet in segmentBackedExtNetData['subnets']['values']:
                        networkAddress = ipaddress.ip_network('{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                                                              strict=False)
                        # If IP belongs to the network add to ipRange value
                        if ipaddress.ip_address(ip) in networkAddress:
                            if subnet['ipRanges']['values']:
                                subnet['ipRanges']['values'].extend(self.createExternalNetworkSubPoolRangePayload([ip]))
                                break
                            else:
                                subnet['ipRanges']['values'] = []
                                subnet['ipRanges']['values'].extend(self.createExternalNetworkSubPoolRangePayload([ip]))
                                break

                # url to update external network properties
                url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.ALL_EXTERNAL_NETWORKS, segmentBackedExtNetData['id'])
                # put api call to update external network
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                payloadData = json.dumps(segmentBackedExtNetData)

                response = self.restClientObj.put(url, self.headers, data=payloadData)
                if response.status_code == requests.codes.accepted:
                    taskUrl = response.headers['Location']
                    # checking the status of the updating external network task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug(
                        'Target External network {} updated successfully with sub allocated ip pools.'.format(
                            segmentBackedExtNetData['name']))
                else:
                    errorResponse = response.json()
                    raise Exception(
                        'Failed to update External network {} with sub allocated ip pools - {}'.format(
                            segmentBackedExtNetData['name'], errorResponse['message']))
            if rollback:
                logger.debug("Successfully migrated IP's to source external network from NSX-T segment backed external network")
            else:
                logger.debug("Successfully migrated IP's to NSX-T segment backed external network")
        except:
            logger.error(traceback.format_exc())
            raise
        finally:
            # Releasing thread lock
            try:
                self.lock.release()
            except RuntimeError:
                pass

    def migrateEdgeGatewayIps(self):
        """
        Description: Migrate the IP assigned to edges from source external network to segment backed external network
        """
        self.copyIPToSegmentBackedExtNet(edgeGatewayIpMigration=True, key='segmentBackedNetworkIP')

    def prepareTargetVDC(self, vcdObjList, sourceOrgVDCId, inputDict, nsxObj, sourceOrgVDCName, vcenterObj, configureBridging=False, configureServices=False):
        """
        Description :   Preparing Target VDC
        Parameters  :   vcdObjList       -   List of vcd operations class objects (LIST)
                        sourceOrgVDCId   -   ID of source Org VDC (STRING)
                        orgVDCIDList     -   List of all the org vdc's undergoing parallel migration (LIST)
                        inputDict        -   Dictionary containing data from input yaml file (DICT)
                        nsxObj           -   NSXTOperations class object (OBJECT)
                        sourceOrgVDCName -   Name of source org vdc (STRING)
                        orgVDCIDList     -   List of source org vdc's ID's (LIST)
                        vcenterObj - Object of vcenterApis module (Object)
                        configureBridging-   Flag that decides bridging is to be configured further or not (BOOLEAN)
                        configureServices-   Flag that decides services are to be configured further or not (BOOLEAN)
        """
        try:
            # Replacing thread name with org vdc name
            threading.current_thread().name = self.vdcName

            # Fetching org vdc network list
            orgVdcNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)

            # creating target Org VDC
            self.createOrgVDC()

            # applying the vm placement policy on target org vdc
            self.applyVDCPlacementPolicy()

            # applying the vm sizing policy on target org vdc
            self.applyVDCSizingPolicy()

            # checking the acl on target org vdc
            self.createACL()

            # migrating edge gateway ips to segment backed network
            self.migrateEdgeGatewayIps()

            # creating target Org VDC Edge Gateway
            self.createEdgeGateway(nsxObj)

            # disconnecting external network directly connected to T1 via CSP port
            self.disconnectSegmentBackedNetwork()

            implicitGateways, implicitNetworks = set(), set()
            if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_2):
                # Configure Edge gateway RateLimits
                self.configureEdgeGWRateLimit(nsxObj)

                # Allow Non distributed routing for edge gateways
                implicitGateways, implicitNetworks = self._checkNonDistributedImplicitCondition(orgVdcNetworkList, natConfig=True)
                self.allowNonDistributedRoutingOnEdgeGW(implicitGateways)

            # only if source org vdc networks exist
            if orgVdcNetworkList:
                # creating private IP Spaces for Org VDC Networks
                self.createPrivateIpSpacesForNetworks(orgVdcNetworkList)
                # creating target Org VDC networks
                self.createOrgVDCNetwork(orgVdcNetworkList, inputDict, nsxObj, implicitNetworks)
                # disconnecting target Org VDC networks
                self.disconnectTargetOrgVDCNetwork()
            else:
                # If not source Org VDC networks are not present target Org VDC networks will also be empty
                logger.debug('Skipping Target Org VDC Network creation as no source Org VDC network exist.')
                self.rollback.apiData['targetOrgVDCNetworks'] = {}

            # Check if services are to be configured and API version is compatible or not
            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):

                # Variable to set that the thread has reached here
                self.__done__ = True
                # Wait while all threads have reached this stage
                while not all([True if hasattr(obj, '__done__') else False for obj in vcdObjList]):
                    # Exit if any thread encountered any error
                    if [obj for obj in vcdObjList if hasattr(obj, '__exception__')]:
                        return
                    continue
                # Sleep time for all threads to reach this point
                time.sleep(5)
                delattr(self, '__done__')

                if configureServices:
                    # creating orgVdcGroups
                    self.createOrgvDCGroup(sourceOrgVDCName, vcdObjList)

                # Variable to set that the thread has reached here
                self.__done__ = True
                # Wait while all threads have reached this stage
                while not all([True if hasattr(obj, '__done__') else False for obj in vcdObjList]):
                    # Exit if any thread encountered any error
                    if [obj for obj in vcdObjList if hasattr(obj, '__exception__')]:
                        return
                    continue

            # Creating dc group for direct networks
            self.createOrgvDCGroupForImportedNetworks(sourceOrgVDCName, vcdObjList)

            # Check if bridging is to be performed
            if configureBridging:
                # writing the promiscuous mode and forged mode details to apiData dict
                self.getPromiscModeForgedTransmit(sourceOrgVDCId)

                # enable the promiscous mode and forged transmit of source org vdc networks
                self.enablePromiscModeForgedTransmit(orgVdcNetworkList)

                # get the portgroup of source org vdc networks
                self.getPortgroupInfo(orgVdcNetworkList)

            # Migrating metadata from source org vdc to target org vdc
            self.migrateMetadata()

        except:
            self.__exception__ = True
            logger.error(traceback.format_exc())
            raise
        finally:
            # Delete attribute once not required
            if hasattr(self, '__done__'):
                delattr(self, '__done__')

    def configureTargetVDC(self, vcdObjList, edgeGatewayDeploymentEdgeCluster=None, nsxtObj=None):
        """
        Description :   Configuring Target VDC
        Parameters  :   vcdObjList - List of objects of vcd operations class (LIST)
        """
        try:
            #Changing thread name to org vdc name
            threading.currentThread().name = self.vdcName

            # Fetching data from metadata
            data = self.rollback.apiData
            sourceEdgeGatewayIdList = data['sourceEdgeGatewayId']
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
            orgVdcNetworkList = self.retrieveNetworkListFromMetadata(sourceOrgVDCId, orgVDCType='source')
            targetOrgVDCNetworkList = self.retrieveNetworkListFromMetadata(targetOrgVDCId, orgVDCType='target')
            # Creating a list of edges mapped to IP Space enabled provider gateways
            ipSpaceEnabledEdges = [edge["id"] for edge in data['sourceEdgeGateway']
                                   if self.orgVdcInput['EdgeGateways'][edge["name"]]['Tier0Gateways']
                                   in data['ipSpaceProviderGateways']]

            # edgeGatewayId = copy.deepcopy(data['targetEdgeGateway']['id'])
            if orgVdcNetworkList:
                # Setting static route interfaces to None
                self.setStaticRoutesInterfaces()

                # disconnecting source org vdc networks from edge gateway
                self.disconnectSourceOrgVDCNetwork(orgVdcNetworkList, sourceEdgeGatewayIdList)

            # connecting dummy uplink to edge gateway
            self.connectUplinkSourceEdgeGateway(sourceEdgeGatewayIdList)

            # disconnecting source org vdc edge gateway from external
            self.reconnectOrDisconnectSourceEdgeGateway(sourceEdgeGatewayIdList, connect=False)

            if targetOrgVDCNetworkList:
                # reconnecting target Org VDC networks
                self.reconnectOrgVDCNetworks(sourceOrgVDCId, targetOrgVDCId, source=False)

                if ipSpaceEnabledEdges:
                    self.configureRouteAdvertisement(ipSpace=True)

            # configuring firewall security groups
            self.configureFirewall(networktype=True)

            # configuring dhcp service target Org VDC networks
            self.configureDHCP(targetOrgVDCId, edgeGatewayDeploymentEdgeCluster, nsxtObj)

            # Configure DHCP relay and Binding on target edge gateway.
            if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_1):
                self.configureDHCPRelayService()
                self.configureDHCPBindingService()

            # reconnecting target org vdc edge gateway from T0
            self.reconnectTargetEdgeGateway()

            # set static route scopes
            self.setStaticRoutesScope()

            # setting static routes for NSX-T segment directly connected to target edge gateways
            self.setEdgeGatewayStaticRoutes()

            # update NAT rules in internal interfaces
            self.updateNATRules()

            # update firewall rules
            self.updateFirewallRules()

            # updating route redistribution rules on tier-0 routers for rules
            if float(self.version) < float(vcdConstants.API_VERSION_BETELGEUSE_10_4):
                self.updateRouteRedistributionRules(nsxtObj)

            # Configure DNAT rules for non-distributed network if implicit condition is met
            if float(self.version) >= float(vcdConstants.API_VERSION_ANDROMEDA_10_3_2):
                self.configureTargetDnatForDns()

            if float(self.version) >= float(vcdConstants.API_VERSION_ZEUS):
                # increase in scope of Target edgegateways
                self.increaseScopeOfEdgegateways()
                # # increase in scope of Target ORG VDC networks
                self.increaseScopeforNetworks()
                # Enable DFW in the orgVDC groups
                self.enableDFWinOrgvdcGroup()

                # Variable to set that the thread has reached here
                self.__done__ = True
                # Wait while all threads have reached this stage
                while not all([True if hasattr(obj, '__done__') else False for obj in vcdObjList]):
                    # Exit if any thread encountered any error
                    if [obj for obj in vcdObjList if hasattr(obj, '__exception__')]:
                        return
                    continue

                # Configure DFW in org VDC groups
                self.configureSecurityTags()
                self.configureDFW(vcdObjList, sourceOrgVDCId=sourceOrgVDCId)

                # Variable to set that the thread has reached here
                self._dfw_configured = True
                # Wait while all threads have reached this stage
                while not all([True if hasattr(obj, '_dfw_configured') else False for obj in vcdObjList]):
                    # Exit if any thread encountered any error
                    if [obj for obj in vcdObjList if hasattr(obj, '__exception__')]:
                        return
                    continue
            logger.debug("Configured target vdc successfully")
        except:
            logger.error(traceback.format_exc())
            self.__exception__ = True
            raise
        finally:
            # Delete attribute once not required
            if hasattr(self, '__done__'):
                delattr(self, '__done__')

    def updateRouteRedistributionRules(self, nsxtObj):
        """
        Description : update route redistribution rules - services like NAT, LB VIP, IPSEC are set for route
        redistribution
        """
        if not self.rollback.apiData['targetEdgeGateway']:
            logger.debug("Skipping updating route redistribution rules as target edge gateway does not exists")
            return

        logger.info('Updating Route Redistribution Rules')
        T0GatewayList = self.rollback.apiData['targetExternalNetwork']
        for edge in self.rollback.apiData['sourceEdgeGateway']:
            edgeGatewayId = edge["id"].split(":")[-1]
            t0Gateway = self.orgVdcInput['EdgeGateways'][edge['name']]['Tier0Gateways']
            vrfBackingId = T0GatewayList[t0Gateway]["networkBackings"]["values"][0]["backingId"]
            vrfData = nsxtObj.getVRFdetails(vrfBackingId)
            bgpConfigDict = self.getEdgegatewayBGPconfig(edgeGatewayId, validation=False)
            routeRedistributionRules = vrfData["results"][0].get("route_redistribution_config", {}).get("redistribution_rules", [])
            advertisedSubnets = vcdConstants.ADVERTISED_SUBNET_LIST
            if (self.getStaticRoutesDetails(edgeGatewayId)
                    or isinstance(bgpConfigDict, dict) and bgpConfigDict['enabled'] == 'true'
                    or self.orgVdcInput['EdgeGateways'][edge['name']]['AdvertiseRoutedNetworks']):
                advertisedSubnets.append("TIER1_CONNECTED")
            for rule in routeRedistributionRules:
                advertisedSubnets = list(set(advertisedSubnets) - set(rule["route_redistribution_types"]))

            sytemVcdEdgeServicesRedistributionDict = {
                "name": "SYSTEM-VCD-EDGE-SERVICES-REDISTRIBUTION",
                "route_redistribution_types": advertisedSubnets
            }
            for rule in routeRedistributionRules:
                if rule.get("name") == "SYSTEM-VCD-EDGE-SERVICES-REDISTRIBUTION":
                    rule["route_redistribution_types"] = rule["route_redistribution_types"] + advertisedSubnets
                    break
            else:
                routeRedistributionRules.append(sytemVcdEdgeServicesRedistributionDict)
            if advertisedSubnets:
                nsxtObj.createRouteRedistributionRule(vrfData, t0Gateway, routeRedistributionRules)

    def updateCatalogVappVmPolicy(self, vappVmList, templateName):
        """
        Description :   Update the Catalog VApp template VM Default Storage Policies and return the response
        Parameters: vappVmList -  list of all VM present under VApp templates (LIST)
        """
        logger.debug("Updating Default VM Template Storage Policy for VMs - {} using vApp template - '{}'".format(
            [vm['@name'] for vm in vappVmList], templateName))
        filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
        # iterate through Catalog VApp template Vm list
        for vappVm in vappVmList:
            payloadDict = {
                'catlogvappvmTempUrl': vappVm['@href'],
                'vmCatalogName': vappVm['@name']
            }
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      payloadDict,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CATALOG_VAPP_VM_TEMP_STORAGE_POLICY)
            payloadData = json.loads(payloadData)
            self.headers["Content-Type"] = vcdConstants.GENERAL_XML_CONTENT_TYPE
            # Api Call to update VM Default Storage Policy
            response = self.restClientObj.put(vappVm['@href'], self.headers, data=payloadData)
            if response.status_code == requests.codes.accepted:
                taskUrl = response.headers['Location']
                self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Default VM Template Storage Policy of VM - '{}' for Catalog Vapp template - '{}' updated successfully with Default storage policy of target Org VDC".format(
                    vappVm['@name'], templateName))
            else:
                raise Exception("Failed to update Default VM Template Storage Policy of VM - '{}' for Catalog Vapp template - '{}' with Default storage policy of target Org VDC".format(
                    vappVm['@name'], templateName))

    def checkVappCatalogVmDefaultStoragePolicy(self, catalogItemResponseDict, targetOrgVDCStoragePolicyName, headers):
        """
        Description :   Check Default Storage Policy applied to the VAPp Template
        Parameters  :   catalogItemResponseDict - Catalog Items
                        targetOrgVDCStoragePolicyName - Name of Target Org VDC Storage Polices (LIST)
        """
        logger.debug("Checking Default VM Template Storage Policy for VApp template - {}.".format(
            catalogItemResponseDict['@name']))
        # API call to get Catlog VApp template response
        catalogVappTempItemResponse = self.restClientObj.get(catalogItemResponseDict['@href'],
                                                             headers=headers)
        catalogVappTempItemResponseDict = self.vcdUtils.parseXml(catalogVappTempItemResponse.content)
        # List all chidrens of Vapp template and convert return values to list if not list
        vappChildrenList = catalogVappTempItemResponseDict['VAppTemplate']['Children'][
            'Vm'] if isinstance(catalogVappTempItemResponseDict['VAppTemplate']['Children']['Vm'], list) else [
            catalogVappTempItemResponseDict['VAppTemplate']['Children']['Vm']]
        # iterate through all VApp template chidrens and filter records with different storage policy than target Org VDC
        vappVmList = [vm for vm in vappChildrenList if vm.get('DefaultStorageProfile') and vm.get(
            'DefaultStorageProfile') not in targetOrgVDCStoragePolicyName]
        if vappVmList:
            self.updateCatalogVappVmPolicy(vappVmList, catalogItemResponseDict['@name'])

    def migrateCatalogItems(self, sourceOrgVDCId, targetOrgVDCId, orgName, timeout):
        """
        Description : Migrating Catalog Items - vApp Templates and Media & deleting catalog thereafter
        Parameters  :   sourceOrgVDCId  - source Org VDC id (STRING)
                        targetOrgVDCId  - target Org VDC id (STRING)
                        orgUrl          - Organization url (STRING)
        """
        try:
            orgId, sourceOrgVDCResponseDict, orgCatalogs, sourceOrgVDCCatalogDetails = self.getOrgVDCPublishedCatalogs(sourceOrgVDCId, orgName, Migration=True)

            if not orgCatalogs:
                logger.debug("No Catalogs exist in Organization")
                return

            # getting the target storage profile details
            targetOrgVDCId = targetOrgVDCId.split(':')[-1]
            # url to get target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetOrgVDCId))

            # get api call to retrieve the target org vdc details
            targetOrgVDCResponse = self.restClientObj.get(url, self.headers)
            targetOrgVDCResponseDict = self.vcdUtils.parseXml(targetOrgVDCResponse.content)

            # targetStorageProfileIDsList holds list the IDs of the target org vdc storage profiles
            targetStorageProfileIDsList = []
            # targetStorageProfilesList holds the list of dictionaries of details of each target org vdc storage profile
            targetStorageProfilesList = []
            # retrieving target org vdc storage profiles list
            targetOrgVDCStorageList = targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles'][
                'VdcStorageProfile'] if isinstance(
                targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile'], list) else [
                targetOrgVDCResponseDict['AdminVdc']['VdcStorageProfiles']['VdcStorageProfile']]
            for storageProfile in targetOrgVDCStorageList:
                targetStorageProfilesList.append(storageProfile)
                targetStorageProfileIDsList.append(storageProfile['@id'])

            # targetOrgVDCCatalogDetails will hold list of only catalogs present in the target org vdc
            targetOrgVDCCatalogDetails = []
            # targetOrgVDCCatalogNameList will hold name of target org vdc catalogs
            targetOrgVDCCatalogNameList = []
            # iterating over all the organization catalogs
            for catalog in orgCatalogs:
                # get api call to retrieve the catalog details
                catalogResponse = self.restClientObj.get(catalog['@href'], headers=self.headers)
                catalogResponseDict = self.vcdUtils.parseXml(catalogResponse.content)
                if catalogResponseDict['AdminCatalog'].get('CatalogStorageProfiles'):
                    # checking if catalogs storage profile is same from target org vdc storage profile by matching the ID of storage profile
                    if catalogResponseDict['AdminCatalog']['CatalogStorageProfiles']['VdcStorageProfile'][
                            '@id'] in targetStorageProfileIDsList:
                        # creating the list of catalogs from source org vdc
                        targetOrgVDCCatalogDetails.append(catalogResponseDict['AdminCatalog'])
                        targetOrgVDCCatalogNameList.append(catalogResponseDict['AdminCatalog']["@name"])
            # List of all target OrgVDC storage policies name
            targetOrgVDCStoragePolicyName = [storagePolicy['@name'] for storagePolicy in targetOrgVDCStorageList]
            # iterating over the source org vdc catalogs to migrate them to target org vdc
            for srcCatalog in sourceOrgVDCCatalogDetails:
                logger.debug("Migrating source Org VDC specific Catalogs")
                storageProfileHref = ''
                for storageProfile in targetOrgVDCStorageList:
                    srcOrgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(
                        srcCatalog['CatalogStorageProfiles']['VdcStorageProfile']['@id'])
                    # checking for the same name of target org vdc profile name matching with source catalog's storage profile
                    if srcOrgVDCStorageProfileDetails['AdminVdcStorageProfile']['@name'] == storageProfile['@name']:
                        storageProfileHref = storageProfile['@href']
                        break

                # creating target catalogs for migration
                payloadDict = {'catalogName': srcCatalog['@name'] + '-v2t',
                               'storageProfileHref': storageProfileHref,
                               'catalogDescription': srcCatalog['Description'] if srcCatalog.get('Description') else ''}
                if payloadDict['catalogName'] not in targetOrgVDCCatalogNameList:
                    # owner is a dictionary and it contains href, type and name of owner of the catalog
                    owner = srcCatalog.get('Owner', {}).get('User')
                    # Source Catalog ID
                    srcCatalogId = srcCatalog.get('@id').split(':')[-1]
                    # Parameter to check is the catalogs Read-Only Access is shared to all ORGs
                    readAccessToAllOrg = srcCatalog.get('IsPublished')
                    # Function call to create a new Target side catalog
                    catalogId = self.createCatalog(payloadDict, orgId, owner, srcCatalogId, readAccessToAllOrg)
                else:
                    catalogId = list(filter(lambda catalog: catalog["@name"] == payloadDict['catalogName'],
                    targetOrgVDCCatalogDetails))[0]["@id"].split(':')[-1]
                if catalogId:
                    # empty catalogs
                    if not srcCatalog.get('CatalogItems'):
                        logger.debug("Migrating empty catalog '{}'".format(srcCatalog['@name']))
                        # deleting the source org vdc catalog
                        self.deleteSourceCatalog(srcCatalog['@href'], srcCatalog)
                        # renaming the target org vdc catalog
                        self.renameTargetCatalog(catalogId, srcCatalog)
                        continue

                    # non-empty catalogs
                    logger.debug("Migrating non-empty catalog '{}'".format(srcCatalog['@name']))
                    # retrieving the catalog items of the catalog
                    catalogItemList = srcCatalog['CatalogItems']['CatalogItem'] if isinstance(srcCatalog['CatalogItems']['CatalogItem'], list) else [srcCatalog['CatalogItems']['CatalogItem']]

                    vAppTemplateCatalogItemList = []
                    mediaCatalogItemList = []
                    # creating seperate lists for catalog items - 1. One for media catalog items 2. One for vApp template catalog items
                    for catalogItem in catalogItemList:
                        catalogItemResponse = self.restClientObj.get(catalogItem['@href'], headers=self.headers)
                        catalogItemResponseDict = self.vcdUtils.parseXml(catalogItemResponse.content)
                        if catalogItemResponseDict['CatalogItem']['Entity']['@type'] == vcdConstants.TYPE_VAPP_TEMPLATE:
                            self.checkVappCatalogVmDefaultStoragePolicy(catalogItemResponseDict['CatalogItem']['Entity'],
                                                                        targetOrgVDCStoragePolicyName, self.headers)
                            vAppTemplateCatalogItemList.append(catalogItem)
                        elif catalogItemResponseDict['CatalogItem']['Entity']['@type'] == vcdConstants.TYPE_VAPP_MEDIA:
                            mediaCatalogItemList.append(catalogItem)
                        else:
                            raise Exception("Catalog Item '{}' of type '{}' is not supported".format(catalogItem['@name'], catalogItemResponseDict['CatalogItem']['Entity']['@type']))

                    logger.debug('Starting to move source org VDC catalog items: ')
                    # Note: First migrating the media then migrating the vapp templates to target catalog(because if migrating of media fails(it fails if the same media is used by other org vdc as well) then no need of remigrating back the vapp templates to source catalogs)
                    # moving each catalog item from the 'mediaCatalogItemList' to target catalog created above
                    for catalogItem in mediaCatalogItemList:
                        logger.debug("Migrating Media catalog item: '{}'".format(catalogItem['@name']))
                        # creating payload data to move media
                        payloadDict = {'catalogItemName': catalogItem['@name'],
                                       'catalogItemHref': catalogItem['@href']}
                        self.moveCatalogItem(payloadDict, catalogId, timeout)

                    # moving each catalog item from the 'vAppTemplateCatalogItemList' to target catalog created above
                    for catalogItem in vAppTemplateCatalogItemList:
                        logger.debug("Migrating vApp Template catalog item: '{}'".format(catalogItem['@name']))
                        # creating payload data to move vapp template
                        payloadDict = {'catalogItemName': catalogItem['@name'],
                                       'catalogItemHref': catalogItem['@href']}
                        self.moveCatalogItem(payloadDict, catalogId, timeout)

                    # deleting the source org vdc catalog
                    self.deleteSourceCatalog(srcCatalog['@href'], srcCatalog)
                    # renaming the target org vdc catalog
                    self.renameTargetCatalog(catalogId, srcCatalog)

                    # deleting the temporary lists
                    del vAppTemplateCatalogItemList
                    del mediaCatalogItemList
            else:
                # migrating non-specific org vdc  catalogs
                # in this case catalog uses any storage available in the organization; but while creating media or vapp template it uses our source org vdc's storage profile by default
                logger.debug("Migrating Non-specific Org VDC Catalogs")

                # case where no catalog items found in source org vdc to migrate non-specific org vdc catalog
                if sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities'] is None:
                    # no catalog items found in the source org vdc
                    logger.debug("No catalogs items found in the source org vdc")
                    return

                # resourceEntitiesList holds the resource entities of source org vdc
                resourceEntitiesList = listify(sourceOrgVDCResponseDict['AdminVdc']['ResourceEntities']['ResourceEntity'])

                # sourceCatalogItemsList holds the list of resource entities of type media or vapp template found in source org vdc
                sourceCatalogItemsList = [resourceEntity for resourceEntity in resourceEntitiesList if
                                          resourceEntity['@type'] == vcdConstants.TYPE_VAPP_MEDIA or resourceEntity[
                                              '@type'] == vcdConstants.TYPE_VAPP_TEMPLATE]

                organizationCatalogItemList = []
                # organizationCatalogItemList holds the resource entities of type vapp template from whole organization
                organizationCatalogItemList = self.getvAppTemplates(orgId)
                # now organizationCatalogItemList will also hold resource entities of type media from whole organization
                organizationCatalogItemList.extend(self.getCatalogMedia(orgId))
                # commonCatalogItemsDetailsList holds the details of catalog common from source org vdc and organization
                commonCatalogItemsDetailsList = [orgResource for orgResource in organizationCatalogItemList for
                                                 srcResource in sourceCatalogItemsList if
                                                 srcResource['@href'] == orgResource['href']]
                # Validate if any stale vapp template/media files found
                catalogItemsWithNoCatalog = self.validateVappMediasNotStale(commonCatalogItemsDetailsList)
                if catalogItemsWithNoCatalog:
                    logger.warning(
                        "Media Items - {} with no catalog linked to them exists. Migration of catalog might fail. Please "
                        "remove stale items manually.".format(','.join(catalogItemsWithNoCatalog)))

                # getting the default storage profile of the target org vdc
                defaultTargetStorageProfileHref = None
                # iterating over the target org vdc storage profiles
                for eachStorageProfile in targetOrgVDCStorageList:
                    # fetching the details of the storage profile
                    orgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(eachStorageProfile['@id'])
                    # checking if the storage profile is the default one
                    if orgVDCStorageProfileDetails['AdminVdcStorageProfile']['Default'] == "true":
                        defaultTargetStorageProfileHref = eachStorageProfile['@href']
                        break

                # catalogItemDetailsList is a list of dictionaries; each dictionary holds the details of each catalog item found in source org vdc
                # each dictionary finally holds keys {'@href', '@id', '@name', '@type', 'catalogName', 'catalogHref', 'catalogItemHref', 'catalogDescription'}
                catalogItemDetailsList = []
                # catalogNameList is a temporary list used to get the single occurence of catalog in catalogDetailsList list
                catalogNameList = []
                # catalogDetailsList is a list of dictionaries; each dictionary holds the details of each catalog
                # each dictionary finally holds keys {'catalogName', 'catalogHref', 'catalogDescription'}
                catalogDetailsList = []
                # iterating over the source catalog items
                for eachResource in sourceCatalogItemsList:
                    # iterating over the catalogs items found in both source org vdc and organization
                    for resource in commonCatalogItemsDetailsList:
                        if eachResource['@href'] == resource['href']:
                            # catalogItem is a dict to hold the catalog item details
                            catalogItem = eachResource
                            catalogItem['catalogName'] = resource['catalogName']

                            for orgCatalog in orgCatalogs:
                                if orgCatalog['@name'] == resource['catalogName']:
                                    catalogItem['catalogHref'] = orgCatalog['@href']
                                    catalogResponseDict = self.getCatalogDetails(orgCatalog['@href'])
                                    if catalogResponseDict.get('catalogItems'):
                                        catalogItemsList = catalogResponseDict['catalogItems']['catalogItem'] if isinstance(catalogResponseDict['catalogItems']['catalogItem'], list) else [catalogResponseDict['catalogItems']['catalogItem']]
                                        for item in catalogItemsList:
                                            if item['name'] == eachResource['@name']:
                                                catalogItem['catalogItemHref'] = item['href']
                                                break

                            catalogResponseDict = self.getCatalogDetails(catalogItem['catalogHref'])
                            catalogItem['catalogDescription'] = catalogResponseDict['description'] if catalogResponseDict.get('description') else ''
                            catalogItemDetailsList.append(catalogItem)
                            # URL for catalog owner
                            catalogOwnerUrl = "{}/{}".format(str(catalogItem['catalogHref']), "owner")
                            # Getting Catalog Owner details
                            catalogOwnerDict = self.getCatalogOwner(catalogOwnerUrl)
                            if resource['catalogName'] not in catalogNameList:
                                catalogNameList.append(resource['catalogName'])
                                catalog = {'catalogName': resource['catalogName'],
                                           'catalogHref': catalogItem['catalogHref'],
                                           'catalogDescription': catalogResponseDict['description'] if catalogResponseDict.get('description') else '',
                                           'catalogOwner': catalogOwnerDict.get('user'),
                                           'readAccessToAllOrg': catalogResponseDict.get('isPublished')}
                                catalogDetailsList.append(catalog)
                # deleting the temporary list since no more needed
                del catalogNameList
                # iterating over catalogs in catalogDetailsList
                for catalog in catalogDetailsList:
                    # creating the payload dict to create a place holder target catalog
                    payloadDict = {'catalogName': catalog['catalogName'] + '-v2t',
                                   'storageProfileHref': defaultTargetStorageProfileHref,
                                   'catalogDescription': catalog['catalogDescription']}
                    if payloadDict['catalogName'] not in targetOrgVDCCatalogNameList:
                        # owner is a dictionary and it contains href, type, name, otherAttributes and id of owner of the catalog(also adding @ prefix in keys)
                        if catalog.get('catalogOwner'):
                            owner = {'@' + str(key): value for key, value in catalog['catalogOwner'].items()}
                        else:
                            owner = None
                        # Source Catalog ID
                        srcCatalogId = catalog.get('catalogHref').split('/')[-1]
                        # Parameter to check if the catalogs Read-Only Access is shared to all ORGs
                        readAccessToAllOrg = catalog.get('readAccessToAllOrg')
                        # Function call to create a new Target side catalog
                        catalogId = self.createCatalog(payloadDict, orgId, owner, srcCatalogId, readAccessToAllOrg)
                    else:
                        catalogId = list(filter(lambda catalog: catalog["@name"] == payloadDict['catalogName'],
                                                targetOrgVDCCatalogDetails))[0]["@id"].split(':')[-1]
                    if catalogId:
                        vAppTemplateCatalogItemList = []
                        mediaCatalogItemList = []
                        # creating seperate lists for catalog items - 1. One for media catalog items 2. One for vApp template catalog items
                        for catalogItem in catalogItemDetailsList:
                            if catalogItem['@type'] == vcdConstants.TYPE_VAPP_TEMPLATE:
                                if catalogItem['catalogName'] == catalog['catalogName']:
                                    self.checkVappCatalogVmDefaultStoragePolicy(catalogItem,
                                                                                targetOrgVDCStoragePolicyName, self.headers)
                                    vAppTemplateCatalogItemList.append(catalogItem)
                            elif catalogItem['@type'] == vcdConstants.TYPE_VAPP_MEDIA:
                                mediaCatalogItemList.append(catalogItem)
                            else:
                                raise Exception("Catalog Item '{}' of type '{}' is not supported".format(catalogItem['@name'], catalogItem['@type']))

                        logger.debug('Starting to move non-specific org VDC catalog items: ')
                        # iterating over the catalog items in mediaCatalogItemList
                        for catalogItem in mediaCatalogItemList:
                            # checking if the catalogItem belongs to the above created catalog; if so migrating that catalogItem to the newly created target catalog
                            if catalogItem['catalogName'] == catalog['catalogName']:
                                logger.debug("Migrating Media catalog item: '{}'".format(catalogItem['@name']))
                                # migrating this catalog item
                                payloadDict = {'catalogItemName': catalogItem['@name'],
                                               'catalogItemHref': catalogItem['catalogItemHref']}
                                # move api call to migrate the catalog item
                                self.moveCatalogItem(payloadDict, catalogId, timeout)

                        # iterating over the catalog items in mediaCatalogItemList
                        for catalogItem in vAppTemplateCatalogItemList:
                            # checking if the catalogItem belongs to the above created catalog; if so migrating that catalogItem to the newly created target catalog
                            if catalogItem['catalogName'] == catalog['catalogName']:
                                logger.debug("Migrating vApp Template catalog item: '{}'".format(catalogItem['@name']))
                                # migrating this catalog item
                                payloadDict = {'catalogItemName': catalogItem['@name'],
                                               'catalogItemHref': catalogItem['catalogItemHref']}
                                # move api call to migrate the catalog item
                                self.moveCatalogItem(payloadDict, catalogId, timeout)

                        catalogData = {'@name': catalog['catalogName'],
                                       '@href': catalog['catalogHref'],
                                       'Description': catalog['catalogDescription']}

                        # deleting the source org vdc catalog
                        self.deleteSourceCatalog(catalogData['@href'], catalogData)
                        # renaming the target org vdc catalog
                        self.renameTargetCatalog(catalogId, catalogData)

                        # deleting the temporary lists
                        del vAppTemplateCatalogItemList
                        del mediaCatalogItemList

        except Exception:
            raise

    @isSessionExpired
    def getSourceEdgeGatewayMacAddress(self, interfacesList):
        """
        Description :   Get source edge gateway mac address for source org vdc network portgroups
        Parameters  :   portGroupList   -   source org vdc networks corresponding portgroup details (LIST)
                        interfacesList  -   Interfaces details of source edge gateway (LIST)
        Returns     :   macAddressList  -   list of mac addresses (LIST)
        """
        try:
            data = self.rollback.apiData
            portGroupList = data.get('portGroupList')
            logger.debug("Getting Source Edge Gateway Mac Address")
            macAddressList = []
            for networkPortGroups in portGroupList:
                for portGroup in networkPortGroups:
                    for nicDetail in interfacesList:
                        # comparing source org vdc network portgroup moref and edge gateway interface details
                        if portGroup['moref'] == nicDetail['value']['backing']['network']:
                            macAddressList.append(nicDetail['value']['mac_address'])
            return macAddressList
        except Exception:
            raise

    def checkIfSourceVappsExist(self, orgVDCId, vAppListFlag=False):
        """
        Description :   Checks if there exist atleast a single vapp in source org vdc
        Returns     :   True    -   if found atleast single vapp (BOOL)
                        False   -   if not a single vapp found in source org vdc (BOOL)
                        vAppList   -    if getvAppList is set.(List)
        """
        try:
            vAppList = []
            orgvdcId = orgVDCId.split(':')[-1]
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(orgvdcId))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code == requests.codes.ok:
                responseDict = self.vcdUtils.parseXml(response.content)
            else:
                raise Exception('Error occurred while retrieving Org VDC - {} details'.format(orgVDCId))
            if not responseDict['AdminVdc'].get('ResourceEntities'):
                logger.debug('No resource entities found in source Org VDC')
                if vAppListFlag:
                    return vAppList
                return False
            # getting list instance of resources in the source org vdc
            sourceOrgVDCEntityList = responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'] if isinstance(
                responseDict['AdminVdc']['ResourceEntities']['ResourceEntity'], list) else [
                responseDict['AdminVdc']['ResourceEntities']['ResourceEntity']]
            vAppList = [vAppEntity for vAppEntity in sourceOrgVDCEntityList if vAppEntity['@type'] == vcdConstants.TYPE_VAPP]
            if vAppListFlag:
                return vAppList
            if len(vAppList) >= 1:
                return True
            return False
        except Exception:
            raise

    @description("Saving vApp count to metadata")
    @remediate
    def savevAppNoToMetadata(self):
        """
        It saves No of vApp of Sourve OrgVdc to metadata.
        """
        try:
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            vAppList = self.checkIfSourceVappsExist(sourceOrgVDCId, True)
            # save No of vApp in Source OrgVdc to metadata.
            self.rollback.apiData['sourceOrgVDC']['NoOfvApp'] = len(vAppList)
        except:
            raise

    def copyMetadatatToTargetVDC(self):
        """
        It copies source org vdc metadata to target org vdc metadata
        """
        try:
            # fetching source and target org vdc id from metadata
            sourceOrgVDCId = self.rollback.apiData.get('sourceOrgVDC', {}).get('@id')
            targetOrgVDCId = self.rollback.apiData.get('targetOrgVDC', {}).get('@id')

            # copying general metadata to target vdc
            metadata = self.getOrgVDCMetadata(sourceOrgVDCId, domain='general')
            self.createMetaDataInOrgVDC(targetOrgVDCId, metadataDict=metadata, domain='general')

            # copying system metadata to target vdc
            metadata = self.getOrgVDCMetadata(sourceOrgVDCId, domain='system')
            self.createMetaDataInOrgVDC(targetOrgVDCId, metadataDict=metadata, domain='system')
        except Exception as e:
            logger.error(f'Exception occurred while copying metadata to target vdc: {e}')
            raise

    def dumpEndStateLog(self):
        """
                Description :   It dumps the Migration State Log at the end of file.
                                It creates two table which shows source and target details.
        """
        try:
            # Get metadata and target vApplist.
            targetOrgVdcId = self.rollback.apiData['targetOrgVDC']['@id']
            targetvAppList = self.checkIfSourceVappsExist(targetOrgVdcId, True)
            sourcevAppNo = self.rollback.apiData['sourceOrgVDC'].get('NoOfvApp', 'NA')
            metadata = dict(self.rollback.apiData)

            # Add logger for state log.
            endStateTableObj = prettytable.PrettyTable()
            endStateTableObj.field_names = ['Entity Names', 'Source Org VDC Details', 'Target Org VDC Details']
            endStateTableObj.align['Entity Names'] = 'l'
            endStateTableObj.align['Source Org VDC Details'] = 'l'
            endStateTableObj.align['Target Org VDC Details'] = 'l'
            StateLog = {}

            # Get organization details
            organization = metadata[vcdConstants.ORG]
            organization_name = organization['@name']
            StateLog[vcdConstants.ORG] = {'Name': organization_name}

            # Get Source OrgVDC details.
            sourceOrgVdcData = metadata[vcdConstants.SOURCE_ORG_VDC]
            sourceOrgVdc_name = sourceOrgVdcData['@name']
            StateLog[vcdConstants.SOURCE_ORG_VDC] = {'Name': sourceOrgVdc_name}

            # Get sourceOrgVDCNetwork details.
            sourceOrgVDCNWdata = metadata[vcdConstants.SOURCE_ORG_VDC_NW]
            StateLog[vcdConstants.SOURCE_ORG_VDC_NW] = {'routed': [], 'isolated': [], 'direct': []}
            for key in sourceOrgVDCNWdata.keys():
                nw_type = sourceOrgVDCNWdata[key]['networkType']
                if nw_type == 'NAT_ROUTED':
                    StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['routed'].append(key)
                elif nw_type == 'ISOLATED':
                    StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['isolated'].append(key)
                elif nw_type == 'DIRECT':
                    StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['direct'].append(key)

            # Get SourceEdgeGateway details.
            sourceEdgeGWList = metadata[vcdConstants.SOURCE_EDGE_GW]
            sourceEdgeGwNo = len(sourceEdgeGWList)
            StateLog[vcdConstants.SOURCE_EDGE_GW] = {'sourceEdgeGwNo': sourceEdgeGwNo, 'sourceEdgeGwData': []}
            for items in sourceEdgeGWList:
                name = items['name']
                StateLog[vcdConstants.SOURCE_EDGE_GW]['sourceEdgeGwData'].append(name)

            # Get TargetOrgVDC details.
            targetOrgVDC = metadata['targetOrgVDC']
            targetOrgVDCName = targetOrgVDC['@name']
            StateLog['targetOrgVDC'] = {'Name': targetOrgVDCName}

            # Get Target OrgVDC network data.
            targetOrgVDCNWdata = metadata[vcdConstants.TARGET_ORG_VDC_NW]
            StateLog[vcdConstants.TARGET_ORG_VDC_NW] = {'routed': [], 'isolated': [], 'direct': [], 'imported': []}
            for key in targetOrgVDCNWdata.keys():
                nw_type = targetOrgVDCNWdata[key]['networkType']
                if nw_type == 'NAT_ROUTED':
                    StateLog[vcdConstants.TARGET_ORG_VDC_NW]['routed'].append(key)
                elif nw_type == 'ISOLATED':
                    StateLog[vcdConstants.TARGET_ORG_VDC_NW]['isolated'].append(key)
                elif nw_type == 'DIRECT':
                    StateLog[vcdConstants.TARGET_ORG_VDC_NW]['direct'].append(key)
                elif nw_type == 'OPAQUE':
                    StateLog[vcdConstants.TARGET_ORG_VDC_NW]['imported'].append(key)

            # Get TargetEdgeGateway details.
            targetEdgeGWList = metadata[vcdConstants.TARGET_EDGE_GW]
            targetEdgeGwNo = len(targetEdgeGWList)
            StateLog[vcdConstants.TARGET_EDGE_GW] = {'targetEdgeGwNo': targetEdgeGwNo, 'targetEdgeGwData': []}
            for items in targetEdgeGWList:
                name = items['name']
                StateLog[vcdConstants.TARGET_EDGE_GW]['targetEdgeGwData'].append(name)

            # Get TargetvAppList details.
            targetvAppNo = len(targetvAppList)
            StateLog[vcdConstants.TARGET_VAPPS] = {'TargetvAppNo': targetvAppNo, 'TargetvAppData': []}
            for item in targetvAppList:
                data = dict(item)
                name = data['@name']
                StateLog[vcdConstants.TARGET_VAPPS]['TargetvAppData'].append(name)

            # Dump StateLog in Table.
            # Dump OrgVdc Name
            sourceOrgvdcName = StateLog[vcdConstants.SOURCE_ORG_VDC]['Name'] + '\n'
            targetOrgvdcName = StateLog[vcdConstants.TARGET_ORG_VDC]['Name'] + '\n'
            endStateTableObj.add_row(['Org VDC Name', sourceOrgvdcName, targetOrgvdcName])

            # Get source and target edge gateway.
            edgeGWList = StateLog[vcdConstants.SOURCE_EDGE_GW]['sourceEdgeGwData']
            sourceEdgeGwData = str(len(edgeGWList)) + " Edges - " + ", ".join(edgeGWList) + '\n'
            edgeGWList = StateLog[vcdConstants.TARGET_EDGE_GW]['targetEdgeGwData']
            targetEdgeGwData = str(len(edgeGWList)) + " Edges - " + ", ".join(edgeGWList) + '\n'
            endStateTableObj.add_row(['Edge Gateway details', sourceEdgeGwData, targetEdgeGwData])

            # Get source orgvdc network details.
            sourceNWData = ''
            for item in StateLog[vcdConstants.SOURCE_ORG_VDC_NW].keys():
                if item == 'routed':
                    nwList = StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['routed']
                    sourceNWData += str(len(nwList)) + ' Routed - ' + ", ".join(nwList) + '\n'
                elif item == 'isolated':
                    nwList = StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['isolated']
                    sourceNWData += str(len(nwList)) + ' Isolated - ' + ", ".join(nwList) + '\n'
                elif item == 'direct':
                    nwList = StateLog[vcdConstants.SOURCE_ORG_VDC_NW]['direct']
                    sourceNWData += str(len(nwList)) + ' Direct - ' + ", ".join(nwList) + '\n'

            # Get Target OrgVdc details.
            targetNWData = ''
            for item in StateLog[vcdConstants.TARGET_ORG_VDC_NW].keys():
                if item == 'routed':
                    nwList = StateLog[vcdConstants.TARGET_ORG_VDC_NW]['routed']
                    targetNWData += str(len(nwList)) + ' Routed - ' + ", ".join(nwList) + '\n'
                elif item == 'isolated':
                    nwList = StateLog[vcdConstants.TARGET_ORG_VDC_NW]['isolated']
                    targetNWData += str(len(nwList)) + ' Isolated - ' + ", ".join(nwList) + '\n'
                elif item == 'direct':
                    nwList = StateLog[vcdConstants.TARGET_ORG_VDC_NW]['direct']
                    targetNWData += str(len(nwList)) + ' Direct - ' + ", ".join(nwList) + '\n'
                elif item == 'imported':
                    nwList = StateLog[vcdConstants.TARGET_ORG_VDC_NW]['imported']
                    targetNWData += str(len(nwList)) + ' Imported - ' + ", ".join(nwList) + '\n'
            endStateTableObj.add_row(['Org VDC Networks', sourceNWData, targetNWData])

            # Get source and target vApp data.
            vAppList = StateLog[vcdConstants.TARGET_VAPPS]['TargetvAppData']
            # endStateTableObj.add_row(['vApp Details', sourcevAppData, targetvAppData])
            endStateTableObj.add_row(["No of vApps (Including Standalone VMs)", sourcevAppNo, len(targetvAppList)])

            threading.currentThread().name = "MainThread"

            # End state table details
            endStateTable = endStateTableObj.get_string()
            endStateLogger.info('\nOrganization Name : {}\nOrgVdc Details\n{}'.format(
                StateLog[vcdConstants.ORG]['Name'], endStateTable))
        except Exception:
            logger.debug(traceback.format_exc())
            raise Exception('Failed to create migration end state log table.')
        finally:
            threading.currentThread().name = "MainThread"

    def fetchTargetStorageProfiles(self, targetVdc):
        """
        Description :   Collects target storage profiles and saves name to href map.
        Parameters  :   targetVdc - target Org VDC details (DICT)
        """
        targetStorageProfileList = (
            targetVdc['VdcStorageProfiles']['VdcStorageProfile']
            if isinstance(targetVdc['VdcStorageProfiles']['VdcStorageProfile'], list)
            else [targetVdc['VdcStorageProfiles']['VdcStorageProfile']])

        self.targetStorageProfileMap = {
            storageProfile['@name']: storageProfile['@href']
            for storageProfile in targetStorageProfileList
        }

    @isSessionExpired
    def moveDisk(self, disk, target_vdc_href, timeout=None):
        """
        Description : Move disk from its current VDC to target VDC
        Parameters  : disk -  Disk details fetched using get disk api (DICT)
                      target_vdc_href  -  HREF/URL for Org VDC to which disk is to be
                       migrated (STRING)
                      timeout  -  Timeout to be used for disk move process(INT)
        """
        logger.info(f'Moving disk {disk["name"]}')
        url = f'{disk["href"]}/{vcdConstants.DISK_MOVE}'
        payload = json.dumps({
            'vdc': {'href': target_vdc_href},
            'storagePolicy': {'href': self.targetStorageProfileMap.get(disk['storageProfileName'])},
            'iops': disk['iops'],
        })
        headers = {
            'Authorization': self.headers['Authorization'],
            'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER.format(self.version),
            'Content-Type': vcdConstants.GENERAL_JSON_CONTENT_TYPE_HEADER,
            'X-VMWARE-VCLOUD-TENANT-CONTEXT': self.rollback.apiData['Organization']['@id'],
        }

        response = self.restClientObj.post(url, headers, data=payload)
        same_vdc_error = 'The destination VDC must be different from the VDC the disk is already in.'
        if response.status_code == requests.codes.accepted:
            for link in response.json()['link']:
                if link['type'] == vcdConstants.JSON_TASK_TYPE:
                    self._checkTaskStatus(link['href'], timeoutForTask=timeout)
            logger.info(f'Successfully moved disk {disk["name"]}')

        elif response.status_code == requests.codes.bad_request and same_vdc_error in response.json()['message']:
            logger.debug(f'Disk {disk["name"]} is already present in VDC')

        else:
            raise Exception(f'Move disk {disk["name"]} failed with error: {response.json()["message"]}')

    def _moveNamedDisks(self, vcdObjList, sourceVdcForDisk, targetVdcForDisk, timeout=None):
        """
        Description :   Move all not attached named disks.
        Parameters  :   vcdObjList - List of objects of vcd operations class (LIST)
                        sourceVdcForDisk - Source (w.r.t. disk movement) Org VDC details (STR)
                        targetVdcForDisk - Target (w.r.t. disk movement) Org VDC details (STR)
                        timeout - timeout for disk operation (INT)
        """
        if float(self.version) < float(vcdConstants.API_VERSION_ANDROMEDA):
            return

        try:
            # As all attached disks are moved with moveVapp call, we should only get non-attached disks
            disks = [
                (vcdObj, disk)
                for vcdObj in vcdObjList
                for disk in vcdObj.getNamedDiskInOrgVDC(vcdObj.rollback.apiData[sourceVdcForDisk]['@id'])
                if not disk['isAttached']
            ]
            if not disks:
                logger.debug('No non-attached independent disks present')
                return

            threading.current_thread().name = "MainThread"
            logger.info("Moving non-attached independent disks")

            for vcdObj in vcdObjList:
                vcdObj.fetchTargetStorageProfiles(vcdObj.rollback.apiData[targetVdcForDisk])

            # Start disk movement
            for vcdObj, disk in disks:
                self.thread.spawnThread(
                    vcdObj.moveDisk, disk, vcdObj.rollback.apiData[targetVdcForDisk]['@href'], timeout)

            # Blocking the main thread until all the threads complete execution
            self.thread.joinThreads()
            if self.thread.stop():
                raise Exception('Failed to move non-attached independent disks')

            threading.current_thread().name = "MainThread"
            logger.info('Successfully moved non-attached independent disks')

        except Exception as e:
            logger.error(f'Exception occurred while moving disk: {e}')
            raise

    @description("Moving non-attached independent disks")
    @remediate_threaded
    def moveNamedDisks(self, vcdObjList, timeout=None, threadCount=75):
        """
        Description :   Move all named disks which are not attached to any VM.
        Parameters  :   vcdObjList  - List of objects of vcd operations class (LIST)
                        timeout     - timeout for disk operation (INT)
                        threadCount - Thread count for simultaneous disk operation
                                      (used in remediate_threaded decorator)(INT)
        """
        self._moveNamedDisks(
            vcdObjList, sourceVdcForDisk='sourceOrgVDC', targetVdcForDisk='targetOrgVDC', timeout=timeout)

    @remediate_threaded
    def moveNamedDisksRollback(self, vcdObjList, timeout=None, threadCount=75):
        """
        Description :   Move all named disks which are not attached to any VM (Rollback)
        Parameters  :   vcdObjList  - List of objects of vcd operations class (LIST)
                        timeout     - timeout for disk operation (INT)
                        threadCount - Thread count for simultaneous disk operation
                                      (used in remediate_threaded decorator)(INT)
        """
        # Check if moveNamedDisks was performed or not
        if not isinstance(self.rollback.metadata.get('moveNamedDisks'), bool):
            return

        self._moveNamedDisks(
            vcdObjList, sourceVdcForDisk='targetOrgVDC', targetVdcForDisk='sourceOrgVDC', timeout=timeout)

        if isinstance(self.rollback.metadata.get('moveNamedDisks'), bool):
            # If NamedDisks rollback is successful, remove the moveNamedDisks key from metadata
            self.deleteMetadataApiCall(
                key='moveNamedDisks-system-v2t', orgVDCId=self.rollback.apiData.get('sourceOrgVDC', {}).get('@id'))

    def migrateVapps(self, vcdObjList, inputDict, timeout=None, threadCount=75):
        """
        Description : Migrating vApps i.e composing target placeholder vapps and recomposing target vapps
        Parameters  : vcdObjList - List of objects of vcd operations class (LIST)
                      inputDict  - input file data in form of dictionary (DICT)
                      timeout    - timeout for vApp migration (INT)
                      threadCount- Thread count for vApp migration (INT)
        """
        # Saving current number of threads
        currentThreadCount = self.thread.numOfThread
        try:
            # Setting new thread count
            self.thread.numOfThread = threadCount
            # Saving status of moveVapp function
            self.rollback.executionResult['moveVapp'] = False
            # Iterating over vcd operations objects to fetch the corresponding details
            sourceOrgVDCNameList, sourceOrgVDCIdList, targetOrgVDCIdList, orgVDCNetworkList = list(), list(), list(), list()
            for vcdObj, orgVdcDict in zip(vcdObjList, inputDict["VCloudDirector"]["SourceOrgVDC"]):
                sourceOrgVDCNameList.append(orgVdcDict["OrgVDCName"])
                sourceOrgVDCIdList.append(vcdObj.rollback.apiData['sourceOrgVDC']['@id'])
                targetOrgVDCIdList.append(vcdObj.rollback.apiData['targetOrgVDC']['@id'])
                dfwStatus = True if vcdObj.rollback.apiData.get('OrgVDCGroupID') else False
                orgVDCNetworkList.append(vcdObj.getOrgVDCNetworks(vcdObj.rollback.apiData['targetOrgVDC']['@id'],
                                                             'targetOrgVDCNetworks', dfwStatus=dfwStatus,
                                                             saveResponse=False, sharedNetwork=True))

            threading.current_thread().name = "MainThread"
            # handling the case if there exist no vapps in source org vdc
            # if no source vapps are present then skipping all the below steps as those are not required
            if not any([self.checkIfSourceVappsExist(sourceOrgVDCId) for sourceOrgVDCId in sourceOrgVDCIdList]):
                logger.debug("No Vapps in Source Org VDC, hence skipping migrateVapps task.")
                self.rollback.executionResult['moveVapp'] = True
            else:
                # Logging continuation message
                if self.rollback.metadata and not hasattr(self.rollback, 'retry'):
                    logger.info(
                        'Continuing migration of NSX-V backed Org VDC to NSX-T backed from {}.'.format(
                            "Migration of vApps"))
                    for vcdObj in vcdObjList:
                        vcdObj.rollback.retry = True

                if not self.rollback.metadata.get('moveVapp'):
                    # recompose target vApp by adding source vm
                    logger.info('Migrating source vApps.')
                    self.moveVapp(sourceOrgVDCIdList, targetOrgVDCIdList, orgVDCNetworkList, timeout, vcdObjList, sourceOrgVDCNameList)
                    logger.info('Successfully migrated source vApps.')
                    self.rollback.executionResult['moveVapp'] = True
        except Exception:
            raise
        finally:
            # Restoring thread count
            self.thread.numOfThread = currentThreadCount
            # Saving metadata
            self.saveMetadataInOrgVdc()
            threading.current_thread().name = "MainThread"

    def vappRollback(self, vcdObjList, inputDict, timeout, threadCount=75):
        """
        Description: Rollback of vapps from target to source org vdc
        Parameters : vcdObjList - List of objects of vcd operations class (LIST)
                     inputDict  - input file data in form of dictionary (DICT)
                     timeout    - timeout for vApp migration (INT)
                     threadCount- Thread count for vApp migration (INT)
        """
        # Saving current number of threads
        currentThreadCount = self.thread.numOfThread
        try:
            # Check if vApp migration was performed or not
            if not isinstance(self.rollback.metadata.get('moveVapp'), bool):
                return
            # Setting new thread count
            self.thread.numOfThread = threadCount

            # Iterating over vcd operations objects to fetch the corresponding details
            sourceOrgVDCNameList, sourceOrgVDCIdList, targetOrgVDCIdList, orgVDCNetworkList, = list(), list(), list(), list()
            for vcdObj, orgVdcDict in zip(vcdObjList, inputDict["VCloudDirector"]["SourceOrgVDC"]):

                try:
                    sourceOrgVDCNameList.append(orgVdcDict["OrgVDCName"])
                    sourceOrgVDCIdList.append(vcdObj.rollback.apiData['sourceOrgVDC']['@id'])
                    targetOrgVDCIdList.append(vcdObj.rollback.apiData['targetOrgVDC']['@id'])
                    dfwStatus = True if vcdObj.rollback.apiData.get('OrgVDCGroupID') else False
                except:
                    # If rollback of one of the org vdc is complete then return
                    return

                # get source org vdc networks
                orgVDCNetworkList.append(vcdObj.getOrgVDCNetworks(vcdObj.rollback.apiData['sourceOrgVDC']['@id'],
                                                                  'sourceOrgVDCNetworks', dfwStatus=dfwStatus,
                                                                  saveResponse=False, sharedNetwork=True))

                # Rolling back affinity rules
                vcdObj.enableTargetAffinityRules(rollback=True)

            self.rollback.executionResult['moveVapp'] = False
            self.saveMetadataInOrgVdc()

            # move vapp from target to source org vdc
            self.moveVapp(targetOrgVDCIdList, sourceOrgVDCIdList, orgVDCNetworkList, timeout, vcdObjList, sourceOrgVDCNameList, rollback=True)
        except Exception:
            raise
        else:
            if isinstance(self.rollback.metadata.get('moveVapp'), bool):
                # If moveVapp rollback is successful, remove the moveVapp key from metadata
                self.deleteMetadataApiCall(key='moveVapp-system-v2t',
                                                    orgVDCId=self.rollback.apiData.get('sourceOrgVDC', {}).get(
                                                        '@id'))
        finally:
            # Restoring thread count
            self.thread.numOfThread = currentThreadCount
            # Restoring thread name
            threading.current_thread().name = "MainThread"

    @isSessionExpired
    def getEdgeVmId(self):
        """
        Description : Method to get edge VM ID
        Parameters : edgeGatewayId - Edge gateway ID (STRING)
        Returns : edgeVmId - Edge Gateway VM ID (STRING)
        """
        try:
            logger.debug("Getting Edge VM ID")
            edgeVmIdMapping = dict()
            edgeGatewayIdList = self.rollback.apiData['sourceEdgeGatewayId']
            for edgeGatewayId in edgeGatewayIdList:
                orgVDCEdgeGatewayId = edgeGatewayId.split(':')[-1]
                # url to retrieve the firewall config details of edge gateway
                url = "{}{}{}".format(vcdConstants.XML_VCD_NSX_API.format(self.ipAddress),
                                      vcdConstants.NETWORK_EDGES,
                                      vcdConstants.EDGE_GATEWAY_STATUS.format(orgVDCEdgeGatewayId))
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    # Convert XML data to dictionary
                    edgeNetworkDict = self.vcdUtils.parseXml(response.content)
                    # Get the edge gateway VM ID
                    # if edge ha is configured, then the response is list
                    if isinstance(edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][
                                      vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY],
                                  list):
                        edgeVmId = [edgeNetworkData for edgeNetworkData in
                                    edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][
                                        vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][
                                        vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY] if
                                    edgeNetworkData['haState'] == 'active']
                        if edgeVmId:
                            edgeVmId = edgeVmId[0]["id"]
                        else:
                            raise Exception(
                                'Could not find the edge vm id for source edge gateway {}'.format(edgeGatewayId))
                    else:
                        edgeVmId = \
                        edgeNetworkDict[vcdConstants.EDGE_GATEWAY_STATUS_KEY][vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY][
                            vcdConstants.EDGE_GATEWAY_VM_STATUS_KEY]["id"]
                    edgeVmIdMapping[orgVDCEdgeGatewayId] = edgeVmId
                else:
                    errorDict = self.vcdUtils.parseXml(response.content)
                    raise Exception(
                        "Failed to get edge gateway status. Error - {}".format(errorDict['error']['details']))
            return edgeVmIdMapping
        except Exception:
            raise

    @description("connection of dummy uplink to source Edge gateway")
    @remediate
    def connectUplinkSourceEdgeGateway(self, sourceEdgeGatewayIdList, rollback=False):
        """
        Description :  Connect another uplink to source Edge Gateways from the specified OrgVDC
        Parameters  :   sourceEdgeGatewayId -   Id of the Organization VDC Edge gateway (STRING)
                        rollback - key that decides whether to perform rollback or not (BOOLEAN)
        """
        try:
            # Check if services configuration or network switchover was performed or not
            if rollback and not isinstance(self.rollback.metadata.get("configureTargetVDC", {}).get("connectUplinkSourceEdgeGateway"), bool):
                return

            if not sourceEdgeGatewayIdList:
                logger.debug('Skipping connecting/disconnecting dummy uplink as edge'
                             ' gateway does not exists')
                return

            if rollback:
                logger.info('Rollback: Disconnecting dummy-uplink from source Edge Gateway')
            else:
                logger.info('Connecting dummy uplink to source Edge gateway.')
            logger.debug("Connecting another uplink to source Edge Gateway")

            data = self.rollback.apiData
            dummyExternalNetwork = self.getDummyExternalNetwork(data['dummyExternalNetwork']['name'])
            if not rollback:
                # Validating if sufficient free IP's are present in dummy external network
                freeIpCount = dummyExternalNetwork['totalIpCount'] - dummyExternalNetwork['usedIpCount']
                if freeIpCount < len(sourceEdgeGatewayIdList):
                    raise Exception(
                        f"{len(sourceEdgeGatewayIdList)} free IP's are required in dummy external network "
                        f"but only {freeIpCount} free IP's are present.")

            for sourceEdgeGatewayId in sourceEdgeGatewayIdList:
                orgVDCEdgeGatewayId = sourceEdgeGatewayId.split(':')[-1]
                # url to connect uplink the source edge gateway
                url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                    vcdConstants.UPDATE_EDGE_GATEWAY_BY_ID.format(orgVDCEdgeGatewayId))
                acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
                headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
                # retrieving the details of the edge gateway
                response = self.restClientObj.get(url, headers)
                responseDict = response.json()
                if response.status_code == requests.codes.ok:
                    gatewayInterfaces = responseDict['configuration']['gatewayInterfaces']['gatewayInterface']
                    if not rollback:
                        if len(gatewayInterfaces) >= 9:
                            raise Exception(
                                f'No more uplinks present on source Edge Gateway ({sourceEdgeGatewayId}) to connect '
                                f'dummy External Uplink.')

                        dummyUplinkAlreadyConnected = True if [interface for interface in gatewayInterfaces
                                                               if interface['name'] == dummyExternalNetwork['name']] \
                                                                else False
                        if dummyUplinkAlreadyConnected:
                            logger.debug("Dummy Uplink is already connected to edge gateway - {}".format(responseDict['name']))
                            continue
                        filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.json')
                        # creating the dummy external network link
                        networkId = dummyExternalNetwork['id'].split(':')[-1]
                        networkHref = "{}network/{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                                            networkId)
                        # creating the payload data for adding dummy external network
                        payloadDict = {'edgeGatewayUplinkName': dummyExternalNetwork['name'],
                                       'networkHref': networkHref,
                                       'uplinkGateway': dummyExternalNetwork['subnets']['values'][0]['gateway'],
                                       'prefixLength': dummyExternalNetwork['subnets']['values'][0]['prefixLength'],
                                       'uplinkIpAddress': ""}
                        payloadData = self.vcdUtils.createPayload(filePath, payloadDict, fileType='json',
                                                                  componentName=vcdConstants.COMPONENT_NAME,
                                                                  templateName=vcdConstants.CONNECT_ADDITIONAL_UPLINK_EDGE_GATEWAY_TEMPLATE)
                        payloadData = json.loads(payloadData)
                        gatewayInterfaces.append(payloadData)
                    else:

                        # Computation to remove dummy external network key from API payload
                        extNameList = [externalNetwork['name'] for externalNetwork in data['sourceExternalNetwork']]
                        extRemoveList = list()
                        for index, value in enumerate(gatewayInterfaces):
                            if value['name'] not in extNameList:
                                extRemoveList.append(value)
                        for value in extRemoveList:
                            gatewayInterfaces.remove(value)
                            # if value['name'] == dummyExternalNetwork['name']:
                            #     gatewayInterfaces.pop(index)
                    responseDict['configuration']['gatewayInterfaces']['gatewayInterface'] = gatewayInterfaces
                    responseDict['edgeGatewayServiceConfiguration'] = None
                    del responseDict['tasks']
                    payloadData = json.dumps(responseDict)
                    acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER
                    self.headers["Content-Type"] = vcdConstants.XML_UPDATE_EDGE_GATEWAY
                    headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader,
                               'Content-Type': vcdConstants.JSON_UPDATE_EDGE_GATEWAY}
                    # updating the details of the edge gateway
                    response = self.restClientObj.put(url + '/action/updateProperties', headers, data=payloadData)
                    responseData = response.json()
                    if response.status_code == requests.codes.accepted:
                        taskUrl = responseData["href"]
                        if taskUrl:
                            # checking the status of renaming target org vdc task
                            self._checkTaskStatus(taskUrl=taskUrl)
                            if rollback:
                                logger.debug(
                                    'Disconnected dummy uplink from source Edge gateway {} successfully'.format(
                                        responseDict['name']))
                            else:
                                logger.debug('Connected dummy uplink to source Edge gateway {} successfully'.format(
                                    responseDict['name']))

                                # Saving rollback key after successful dummy uplink connection to one edge gateway
                                self.rollback.executionResult["configureTargetVDC"]["connectUplinkSourceEdgeGateway"] = False
                            continue
                    else:
                        if rollback:
                            raise Exception(
                                "Failed to disconnect dummy uplink from source Edge gateway {} with error {}".format(
                                    responseDict['name'], responseData['message']))
                        else:
                            raise Exception(
                                "Failed to connect dummy uplink to source Edge gateway {} with error {}".format(
                                    responseDict['name'], responseData['message']))
                else:
                    raise Exception("Failed to get edge gateway '{}' details due to error - {}".format(
                        sourceEdgeGatewayId, responseDict['message']))
            if not rollback:
                logger.info('Successfully connected dummy uplink to source Edge gateway.')
        except Exception:
            self.saveMetadataInOrgVdc()
            raise

    @isSessionExpired
    def updateSourceExternalNetwork(self, networkData, edgeGatewaySubnetDict, targetOrgVDCId):
        """
        Description : Update Source External Network sub allocated ip pools
        Parameters : networkName: source external network name (STRING)
                     edgeGatewaySubnetDict: source edge gateway sub allocated ip pools (DICT)
        """
        if not networkData:
            return
        try:
            # Acquire lock as source external network in different org vdc's
            self.lock.acquire(blocking=True)
            networkMetadata = copy.deepcopy(networkData)
            for network in networkData:
                networkName = network['name']
                response = self.getExternalNetworkByName(networkName)
                # getting the external network sub allocated pools
                for index, subnet in enumerate(response['subnets']['values']):
                    externalRanges = subnet['ipRanges']['values']
                    externalRangeList = []
                    externalNetworkSubnet = ipaddress.ip_network(
                        '{}/{}'.format(subnet['gateway'], subnet['prefixLength']),
                        strict=False)
                    # creating range of source external network pool range
                    for externalRange in externalRanges:
                        externalRangeList.extend(
                            self.createIpRange(externalNetworkSubnet, externalRange['startAddress'], externalRange['endAddress']))
                    subIpPools = edgeGatewaySubnetDict.get(externalNetworkSubnet)
                    # If no ipPools are used from corresponding network then skip the iteration
                    if not subIpPools:
                        continue
                    # Raise exception if target ext network subnet has only one IP and EmptyPoolOverride flag is False
                    if subnet["totalIpCount"] == len(set(tuple(d.items()) for d in edgeGatewaySubnetDict[externalNetworkSubnet])):
                        if self.orgVdcInput.get("EmptyIPPoolOverride", False):
                            logger.warning("Skipping removing '{}' IP from source external network - '{}'".format(externalRanges[0]["startAddress"], networkName))
                            continue
                        else:
                            raise Exception("External Network subnet should have atleast one free IP address which cannot be removed."
                                            " EmptyPoolOverride flag must be set to true to perform successfull rollback/cleanup")

                    # creating range of source edge gateway sub allocated pool range
                    subIpRangeList = []
                    for ipRange in subIpPools:
                        subIpRangeList.extend(
                            self.createIpRange(externalNetworkSubnet, ipRange['startAddress'], ipRange['endAddress']))
                    # removing the sub allocated ip pools of source edge gateway from source external network
                    for ip in subIpRangeList:
                        if ip in externalRangeList:
                            if len(externalRangeList) == 1 and self.orgVdcInput.get("EmptyIPPoolOverride", False):
                                logger.warning("Skipping removing the sub allocated '{}' IP of source edge gateway from source external network - '{}'".format(
                                    externalRangeList[0], networkName))
                                break
                            else:
                                externalRangeList.remove(ip)
                    # getting the source edge gateway sub allocated ip pool after removing used ips i.e source edge gateway
                    result = self.createExternalNetworkSubPoolRangePayload(externalRangeList)
                    response['subnets']['values'][index]['ipRanges']['values'] = result

                # API call to update external network details
                payloadData = json.dumps(response)
                # TODO pranshu: multiple T0 - cleanup - update source network pool
                url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                       vcdConstants.ALL_EXTERNAL_NETWORKS, response['id'])
                # put api call to update the external networks ip allocation
                self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                if apiResponse.status_code == requests.codes.accepted:
                    taskUrl = apiResponse.headers['Location']
                    # checking the status of the creating org vdc network task
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug('Updating external network sub allocated ip pool {}'.format(networkName))
                    # save source extenal network metadata in target org vdc
                    networkMetadata.remove(network)
                    self.createMetaDataInOrgVDC(targetOrgVDCId, metadataDict={"sourceExternalNetwork": networkMetadata})
                else:
                    errorDict = apiResponse.json()
                    raise Exception("Failed to update source external network '{}': {}".format(
                            networkName, errorDict['message']))
        except Exception:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def syncOrgVDCGroup(self, OrgVDCGroupID):
        """
        Description : Sync DC groups created during migration
        Parameters :  OrgVDCGroupID - DC Groups IDs (DICT)
        """
        for ID in OrgVDCGroupID.values():
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                  vcdConstants.GET_VDC_GROUP_BY_ID.format(ID), vcdConstants.VDC_GROUP_SYNC)
            response = self.restClientObj.post(url, self.headers)
            if response.status_code == requests.codes.accepted:
                try:
                    taskUrl = response.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                except Exception as e:
                    logger.warning("Failed to sync DC Groups created with exception - {}".format(e))
            else:
                logger.warning("Failed to sync DC Groups created with error code - {}".format(response.status_code))

    @staticmethod
    def createExternalNetworkSubPoolRangePayload(externalNetworkPoolRangeList):
        """
        Description : Create external network sub ip pool range payload
        Parameters : externalNetworkPoolRangeList - external network pool range (LIST)
        """
        resultData = []
        for ipAddress in externalNetworkPoolRangeList:
            resultData.append({'startAddress': ipAddress, 'endAddress': ipAddress})
        return resultData

    @isSessionExpired
    def deleteSourceCatalog(self, catalogUrl, srcCatalog):
        """
        Description :   Deletes the source org vdc catalog of the specified catalog url
        Parameters  :   catalogUrl  -   url of the source catalog (STRING)
                        srcCatalog  -   Details of the source catalog (DICT)
        """
        try:
            # deleting catalog
            logger.debug("Deleting catalog '{}'".format(srcCatalog['@name']))
            # url to delete the catalog
            deleteCatalogUrl = '{}?recursive=true&force=true'.format(catalogUrl)
            # delete api call to delete the catalog
            deleteCatalogResponse = self.restClientObj.delete(deleteCatalogUrl, self.headers)
            deleteCatalogResponseDict = self.vcdUtils.parseXml(deleteCatalogResponse.content)
            if deleteCatalogResponse.status_code == requests.codes.accepted:
                task = deleteCatalogResponseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of deleting the catalog task
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.debug("Catalog '{}' deleted successfully".format(srcCatalog['@name']))
            else:
                raise Exception("Failed to delete catalog '{}' - {}".format(srcCatalog['@name'],
                                                                            deleteCatalogResponseDict['Error'][
                                                                                '@message']))

        except Exception:
            raise

    @isSessionExpired
    def renameTargetCatalog(self, catalogId, srcCatalog):
        """
        Description :   Renames the target org vdc catalog of the specified catalog url
        Parameters  :   catalogId   -   ID of the source catalog (STRING)
                        srcCatalog  -   Details of the source catalog (DICT)
        """
        try:
            # renaming catalog
            logger.debug("Renaming the catalog '{}' to '{}'".format(srcCatalog['@name'] + '-v2t',
                                                                    srcCatalog['@name']))
            # url to rename the catalog
            renameCatalogUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                             vcdConstants.RENAME_CATALOG.format(catalogId))
            # creating the payload
            payloadDict = {'catalogName': srcCatalog['@name'],
                           'catalogDescription': srcCatalog['Description'] if srcCatalog.get('Description') else ''}

            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      payloadDict,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.RENAME_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # setting the content-type to rename the catalog
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.VCD_API_HEADER,
                       'Content-Type': vcdConstants.RENAME_CATALOG_CONTENT_TYPE}
            # put api call to rename the catalog back to its original name
            renameCatalogResponse = self.restClientObj.put(renameCatalogUrl, headers, data=payloadData)
            if renameCatalogResponse.status_code == requests.codes.ok:
                logger.debug("Catalog '{}' renamed to '{}' successfully".format(srcCatalog['@name'] + '-v2t',
                                                                                srcCatalog['@name']))
            else:
                raise Exception("Failed to rename catalog '{}' to '{}'".format(srcCatalog['@name'] + '-v2t',
                                                                               srcCatalog['@name']))
        except Exception:
            raise

    def updateRoutedOrgVdcNetworkStaticIpPool(self, vAppData):
        """
            Description : Update the static IP pool of OrgVDC network. Called during routed vapp migration
        """
        for vAppNetwork in listify(vAppData['NetworkConfigSection'].get('NetworkConfig', [])):
            if vAppNetwork['Configuration']['FenceMode'] != 'natRouted':
                continue

            natService = vAppNetwork['Configuration'].get('Features', {}).get('NatService')
            if not(natService and natService['NatType'] == 'ipTranslation' and natService.get('NatRule')):
                continue

            networkName = vAppNetwork['Configuration']['ParentNetwork']['@name']
            networkId = vAppNetwork['Configuration']['ParentNetwork']['@id']

            logger.warning("Get Static IP pool of OrgVDC network {}.".format(networkName))
            url = "{}{}".format(
                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(urn_id(networkId, 'network')))
            response = self.restClientObj.get(url, self.headers)
            if response.status_code != requests.codes.ok:
                raise Exception("Failed to get OrgVDC details.")

            networkData = response.json()
            # TODO pranshu: handle case where static pool is empty
            staticIpPools = networkData['subnets']['values'][0]['ipRanges'].get('values')
            ipRangeAddresses = set(
                str(ipaddress.IPv4Address(ip))
                for ipPool in staticIpPools
                for ip in range(
                    int(ipaddress.IPv4Address(ipPool['startAddress'])),
                    int(ipaddress.IPv4Address(ipPool['endAddress']) + 1))
            )

            ipToBeUpdated = [
                natRule['OneToOneVmRule']['ExternalIpAddress']
                for natRule in listify(natService['NatRule'])
                if natRule['OneToOneVmRule'].get('ExternalIpAddress')
                if natRule['OneToOneVmRule']['ExternalIpAddress'] not in ipRangeAddresses
            ]

            if not ipToBeUpdated:
                continue

            logger.warning("Update Static IP pool of OrgVDC network {}".format(networkName))
            url = "{}{}".format(
                vcdConstants.OPEN_API_URL.format(self.ipAddress),
                vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(networkData['id']))

            ipRanges = [
                {'startAddress': ip, 'endAddress': ip}
                for ip in ipToBeUpdated
            ]
            if staticIpPools:
                staticIpPools.extend(ipRanges)
            else:
                staticIpPools = ipRanges

            networkData['subnets']['values'][0]['ipRanges']['values'] = staticIpPools

            apiResponse = self.restClientObj.put(url, self.headers, data=json.dumps(networkData))
            if apiResponse.status_code != requests.codes.accepted:
                raise Exception("Failed to update OrgVDC static pool details : ", apiResponse.json()['message'])
            task_url = apiResponse.headers['Location']
            self._checkTaskStatus(taskUrl=task_url)
            logger.warning("Successfully updated static pool of OrgVDC network {}.".format(networkName))

    @isSessionExpired
    def createMoveVappNetworkPayload(self, vAppData, targetOrgVDCNetworkList, filePath, rollback=False):
        """
            Description :   Prepares the network config payload for moving the vApp
            Parameters  :   vAppData  -   Information related to a specific vApp (DICT)
                            targetOrgVDCNetworkList - All the target org vdc networks (LIST)
                            filePath - file path of template.yml which holds all the templates (STRING)
                            rollback - whether to rollback from T2V (BOOLEAN)
        """
        def getName(vAppNetwork):
            """Get name of target vapp network"""
            if (vAppNetwork['@networkName'] == 'none'
                    or vAppNetwork['Configuration']['FenceMode'] in ('natRouted', 'isolated')):
                return vAppNetwork['@networkName']

            if rollback:
                return vAppNetwork['@networkName'].replace('-v2t', '')

            return vAppNetwork['@networkName'] + '-v2t'

        def prepareIpScopesConfig(vAppNetwork):
            """Prepare target network ipscopes config"""
            if vAppNetwork['Configuration'].get('IpScopes'):
                return [
                    {
                        'isInherited': ipScope['IsInherited'],
                        'gateway': ipScope['Gateway'],
                        'netmask': ipScope.get('Netmask'),
                        'subnet': ipScope.get('SubnetPrefixLength', 1),
                        'IsEnabled': ipScope.get('IsEnabled'),
                        'dns1': ipScope.get('Dns1'),
                        'dns2': ipScope.get('Dns2'),
                        'dnsSuffix': ipScope.get('DnsSuffix'),
                        'ipRanges': listify(ipScope.get('IpRanges', {}).get('IpRange')),
                    }
                    for ipScope in listify(vAppNetwork['Configuration']['IpScopes']['IpScope'])
                    if ipScope['IsInherited'] == 'false' or float(self.version) >= float(vcdConstants.API_10_4_2_BUILD)
                ]

        def getParentNetwork(vAppNetwork):
            """Get target network's parent network"""
            if vAppNetwork['Configuration'].get('ParentNetwork'):
                networkName = (
                    vAppNetwork['Configuration']['ParentNetwork']['@name'].replace('-v2t', '')
                    if rollback
                    else vAppNetwork['Configuration']['ParentNetwork']['@name'] + '-v2t')
                return "{}network/{}".format(
                    vcdConstants.XML_API_URL.format(self.ipAddress),
                    targetOrgVDCNetworks.get(networkName).split(':')[-1])

        def prepareFeaturesConfig(vAppNetwork):
            """Prepare target network features config"""
            if not vAppNetwork['Configuration'].get('Features'):
                return

            featuresConfig = {}

            # DHCP service config
            if vAppNetwork['Configuration']['Features'].get('DhcpService'):
                sourceDhcpConfig = vAppNetwork['Configuration']['Features']['DhcpService']
                if sourceDhcpConfig.get('IsEnabled') == 'true':
                    featuresConfig['dhcpConfig'] = sourceDhcpConfig

            # Firewall service config
            if vAppNetwork['Configuration']['Features'].get('FirewallService'):
                firewallConfig = vAppNetwork['Configuration']['Features']['FirewallService']
                firewallConfig['FirewallRule'] = listify(firewallConfig.get('FirewallRule'))
                featuresConfig['FirewallService'] = firewallConfig

            # NAT service config
            if vAppNetwork['Configuration']['Features'].get('NatService'):
                natConfig = vAppNetwork['Configuration']['Features']['NatService']
                natConfig['NatRule'] = listify(natConfig.get('NatRule'))
                featuresConfig['NatService'] = natConfig

            # Static Routing service config
            if vAppNetwork['Configuration']['Features'].get('StaticRoutingService'):
                staticRoutingConfig = vAppNetwork['Configuration']['Features']['StaticRoutingService']
                staticRoutingConfig['StaticRoute'] = listify(staticRoutingConfig.get('StaticRoute'))
                featuresConfig['StaticRoutingService'] = staticRoutingConfig

            return featuresConfig

        targetOrgVDCNetworks = {network['name']: network['id'] for network in targetOrgVDCNetworkList}

        logger.debug(f"Preparing network payload for moveVapp of {vAppData['@name']}")
        if vAppData['NetworkConfigSection'].get('NetworkConfig'):
            networkConfig = [
                {
                    'name': getName(vAppNetwork),
                    'description': vAppNetwork.get('Description') or '',
                    'ipScopes': prepareIpScopesConfig(vAppNetwork),
                    'parentNetwork': getParentNetwork(vAppNetwork),
                    'fenceMode': vAppNetwork['Configuration']['FenceMode'],
                    'RetainNetInfoAcrossDeployments':
                        vAppNetwork['Configuration'].get('RetainNetInfoAcrossDeployments', 'false'),
                    'features': prepareFeaturesConfig(vAppNetwork),
                    'routerExternalIp': vAppNetwork['Configuration'].get('RouterInfo', {}).get('ExternalIp'),
                    'GuestVlanAllowed': vAppNetwork['Configuration'].get('GuestVlanAllowed', 'false'),
                    'DualStackNetwork': vAppNetwork['Configuration'].get('DualStackNetwork', 'false'),
                    'isDeployed': vAppNetwork['IsDeployed'],
                }
                for vAppNetwork in listify(vAppData['NetworkConfigSection']['NetworkConfig'])
            ]
        else:
            # TODO pranshu: Need to test this section
            networkConfig = []

        return self.vcdUtils.createPayload(
            filePath,
            payloadDict={'networkConfig': networkConfig},
            fileType='yaml',
            componentName=vcdConstants.COMPONENT_NAME,
            templateName=vcdConstants.MOVE_VAPP_NETWORK_CONFIG_TEMPLATE
        ).strip("\"")

    @isSessionExpired
    def moveVappApiCall(self, vApp, targetOrgVDCNetworkList, targetOrgVDCId, filePath, timeout, sourceOrgVDCName=None, rollback=False):
        """
            Description :   Prepares the payload for moving the vApp and sends post api call for it
            Parameters  :   vApp  -   Information related to a specific vApp (DICT)
                            targetOrgVDCNetworkList - All the target org vdc networks (LIST)
                            targetOrgVDCId - ID of target org vdc (STRING)
                            filePath - file path of template.yml which holds all the templates (STRING)
                            timeout  -  timeout to be used for vapp migration task (INT)
                            rollback - whether to rollback from T2V (BOOLEAN)
        """
        # Saving thread name as per vdc name
        threading.currentThread().name = sourceOrgVDCName

        if rollback:
            logger.info('Moving vApp - {} to source Org VDC - {}'.format(vApp['@name'], sourceOrgVDCName))
        else:
            logger.info('Moving vApp - {} to target Org VDC - {}'.format(vApp['@name'], sourceOrgVDCName + '-v2t'))

        response = self.restClientObj.get(vApp['@href'], self.headers)
        sourceVapp = response.content
        endStateLogger.debug(f"[vApp][{vApp['@name']}] Source vapp xml: {sourceVapp}")
        responseDict = self.vcdUtils.parseXml(sourceVapp)
        if response.status_code != requests.codes.ok:
            raise Exception(f"Failed to get vApp details: {responseDict['Error']['@message']}")
        if not responseDict['VApp'].get('Children'):
            logger.info('vApp {} is not moved as vApp does not have any VMs'.format(vApp['@name']))
            return
            # skip moving vApp in case of unsupported states
        if responseDict['VApp']["@status"] in [
            code for state, code in vcdConstants.VAPP_STATUS.items()
            if state in ['FAILED_CREATION', 'UNRESOLVED', 'UNRECOGNIZED', 'INCONSISTENT_STATE']
        ]:
            logger.warning('vApp {} is not moved as vApp is in FAILED_CREATION/UNRESOLVED/UNRECOGNIZED/INCONSISTENT_STATE state'.format(vApp['@name']))
            return
        vAppData = responseDict['VApp']
        # self.updateRoutedOrgVdcNetworkStaticIpPool(vAppData)
        payloadDict = {
            'vAppHref': vApp['@href'],
            'networkConfig': self.createMoveVappNetworkPayload(vAppData, targetOrgVDCNetworkList, filePath, rollback),
            'vmDetails': self.createMoveVappVmPayload(vApp, targetOrgVDCId, rollback=rollback),
        }
        payloadData = self.vcdUtils.createPayload(
            filePath, payloadDict, fileType='yaml', componentName=vcdConstants.COMPONENT_NAME,
            templateName=vcdConstants.MOVE_VAPP_TEMPLATE)

        url = "{}{}".format(
            vcdConstants.XML_API_URL.format(self.ipAddress),
            vcdConstants.MOVE_VAPP_IN_ORG_VDC.format(targetOrgVDCId))
        self.headers["Content-Type"] = vcdConstants.XML_MOVE_VAPP
        endStateLogger.debug(f"[vApp][{vApp['@name']}] Payload for moveVapp API: {payloadData}")

        response = self.restClientObj.post(url, self.headers, data=json.loads(payloadData))
        if response.status_code == requests.codes.accepted:
            responseDict = self.vcdUtils.parseXml(response.content)
            taskUrl = responseDict["Task"]["@href"]
            if taskUrl:
                # checking for the status of the composing vapp task
                self._checkTaskStatus(taskUrl, timeoutForTask=timeout, entityName=vApp['@name'])
        else:
            responseDict = self.vcdUtils.parseXml(response.content)
            raise Exception(
                'Failed to move vApp - {} with errors {}'.format(vApp['@name'], responseDict['Error']['@message']))

        if rollback:
            logger.info(
                'Moved vApp - {} successfully to source Org VDC - {}'.format(vApp['@name'], sourceOrgVDCName))
        else:
            logger.info(
                'Moved vApp - {} successfully to target Org VDC - {}'.format(vApp['@name'], sourceOrgVDCName + '-v2t'))

    @isSessionExpired
    def moveVapp(self, sourceOrgVDCIdList, targetOrgVDCIdList, targetOrgVDCNetworkList, timeout, vcdObjList, sourceOrgVDCNameList=None, rollback=False):
        """
        Description : Move vApp from source Org VDC to Target Org vdc
        Parameters  : sourceOrgVDCId    -   Id of the source organization VDC (STRING)
                      targetOrgVDCId    -   Id of the target organization VDC (STRING)
                      targetOrgVDCNetworkList - List of target Org VDC networks (LIST)
                      timeout  -  timeout to be used for vapp migration task (INT)
                      rollback - whether to rollback from T2V (BOOLEAN)
        """
        try:
            vAppData = list()
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # Fetching vApps from org vdc
            for sourceOrgVDCId, targetOrgVDCId, targetOrgVDCNetworks, sourceOrgVDCName in zip_longest(sourceOrgVDCIdList,
                                                                                                     targetOrgVDCIdList,
                                                                                                     targetOrgVDCNetworkList,
                                                                                                     sourceOrgVDCNameList):
                sourceOrgVDCId = sourceOrgVDCId.split(':')[-1]
                vAppData.append(self.getOrgVDCvAppsList(sourceOrgVDCId))

            threading.current_thread().name = "MainThread"
            if rollback and reduce(lambda x, y: x+y, vAppData):
                logger.info("RollBack: Migrating Target vApps")
            elif rollback and not reduce(lambda x, y: x+y, vAppData):
                return

            for vcdObj, sourceOrgVDCId, targetOrgVDCId, targetOrgVDCNetworks, sourceOrgVDCName, vAppList in zip_longest(
                    vcdObjList,
                    sourceOrgVDCIdList,
                    targetOrgVDCIdList,
                    targetOrgVDCNetworkList,
                    sourceOrgVDCNameList,
                    vAppData):
                # retrieving target org vdc id
                targetOrgVDCId = targetOrgVDCId.split(':')[-1]

                # iterating over the source vapps
                for vApp in vAppList:
                    # Spawning threads for move vApp call
                    self.thread.spawnThread(vcdObj.moveVappApiCall, vApp, targetOrgVDCNetworks, targetOrgVDCId, filePath,
                                            timeout, sourceOrgVDCName=sourceOrgVDCName, rollback=rollback, block=True)
            # Blocking the main thread until all the threads complete execution
            self.thread.joinThreads()

            # Checking if any thread's execution failed
            if self.thread.stop():
                raise Exception('Failed to move vApp/s')
        except Exception:
            raise
        else:
            self.rollback.executionResult['moveVapp'] = True

    @isSessionExpired
    def renameTargetNetworks(self, targetVDCId):
        """
        Description :   Renames all the target org vdc networks in the specified target Org VDC as those in source Org VDC
        Parameters  :   targetVDCId -   id of the target org vdc (STRING)
        """
        try:
            # splitting thr target org vdc id as per the xml api requirements
            targetVDCId = targetVDCId.split(':')[-1]
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            # url to get the target org vdc details
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.ORG_VDC_BY_ID.format(targetVDCId))
            # get api call to retrieve the target org vdc details
            response = self.restClientObj.get(url, headers=headers)
            getResponseDict = response.json()

            # Case 1: Handling the case of renaming target org vdc networks
            # getting the list instance of all the target org vdc networks
            targetOrgVDCNetworks = getResponseDict['availableNetworks']['network'] if isinstance(getResponseDict['availableNetworks']['network'], list) else [getResponseDict['availableNetworks']['network']]
            # iterating over the target org vdc networks
            for network in targetOrgVDCNetworks:
                self.renameTargetOrgVDCNetworks(network)

            # Case 2: Handling the case renaming target vapp isolated networks
            # to get the target vapp networks, getting the target vapps
            if getResponseDict.get('resourceEntities'):
                targetOrgVDCEntityList = getResponseDict['resourceEntities']['resourceEntity'] if isinstance(getResponseDict['resourceEntities']['resourceEntity'], list) else [getResponseDict['resourceEntities']['resourceEntity']]
                vAppList = [vAppEntity for vAppEntity in targetOrgVDCEntityList if vAppEntity['type'] == vcdConstants.TYPE_VAPP]
                if vAppList:
                    self.renameTargetVappIsolatedNetworks(vAppList)
        except Exception:
            raise

    @description("Fetching Promiscous Mode and Forged transmit information")
    @remediate
    def getPromiscModeForgedTransmit(self, sourceOrgVDCId):
        """
        Description : Get the Promiscous Mode and Forged transmit information of source org vdc network
        """
        try:
            logger.info("Fetching Promiscous Mode and Forged transmit information of source org vdc network")
            orgVDCNetworkList = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            data = self.rollback.apiData
            # list of the org vdc networks with its promiscuous mode and forged transmit details
            promiscForgedList = []
            # iterating over the org vdc network list
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS, orgVdcNetwork['id'], vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    # creating the dictionary of details of the promiscuous mode and forge transmit details
                    detailsDict = {}
                    detailsDict["id"] = orgVdcNetwork['id']
                    detailsDict["name"] = orgVdcNetwork['name']
                    detailsDict["promiscForge"] = responseDict
                    # appending the dictionary to the above list
                    promiscForgedList.append(detailsDict)
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(orgVdcNetwork['name']))
            # writing promiscForgedList to the apiOutput.json for further use(for disabling the promiscuous mode and forged transmit in case of rollback)
            data["orgVDCNetworkPromiscModeList"] = promiscForgedList
        except Exception:
            raise

    @isSessionExpired
    def resetTargetExternalNetwork(self):
        """
        Description :   Resets the target external network(i.e updating the target external network to its initial
        state)
        """
        try:
            # Check if org vdc edge gateways were created or not
            if not self.rollback.metadata.get("prepareTargetVDC", {}).get("createEdgeGateway"):
                return

            # Locking as this operation can only be performed by one thread at a time
            self.lock.acquire(blocking=True)
            logger.debug("Lock acquired by thread - '{}'".format(threading.currentThread().getName()))

            logger.info('Rollback: Reset the target external network')

            edgeGatewaySubnetDict = self._getEdgeGatewaySubnets()
            for targetExtNetName, sourceEgwSubnets in edgeGatewaySubnetDict.items():
                logger.debug("Updating Target External network {} with sub allocated ip pools".format(targetExtNetName))
                targetExtNetData = self.getExternalNetworkByName(targetExtNetName)
                if targetExtNetData.get("usingIpSpace"):
                    ipSpaces = self.getProviderGatewayIpSpaces(targetExtNetData)
                    for edgeGatewaySubnet, edgeGatewayIpRangesList in sourceEgwSubnets.items():
                        for ipSpace in ipSpaces:
                            if [internalScope for internalScope in ipSpace["ipSpaceInternalScope"]
                                if type(edgeGatewaySubnet) == type(
                                    ipaddress.ip_network('{}'.format(internalScope), strict=False)) and
                                    self.subnetOf(edgeGatewaySubnet,
                                                  ipaddress.ip_network('{}'.format(internalScope), strict=False))]:
                                self._prepareIpSpaceRanges(ipSpace, edgeGatewayIpRangesList, rollback=True)
                    for ipSpace in ipSpaces:
                        url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                            vcdConstants.UPDATE_IP_SPACES.format(ipSpace["id"]))
                        self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                        response = self.restClientObj.put(url, self.headers, data=json.dumps(ipSpace))
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                            logger.debug(
                                "Provider Gateway IP Space uplink - '{}' updated successfully with sub allocated ip pools.".format(
                                    ipSpace['name']))
                else:
                    for targetExtNetSubnet in targetExtNetData['subnets']['values']:
                        targetExtNetSubnetAddress = ipaddress.ip_network(
                            '{}/{}'.format(targetExtNetSubnet['gateway'], targetExtNetSubnet['prefixLength']), strict=False)

                        #  Continue if source edge gateway has no IP from the specific subnet
                        if not sourceEgwSubnets.get(targetExtNetSubnetAddress):
                            continue

                        # Raise exception if target ext network subnet has not any free IP and EmptyPoolOverride flag is False

                        if targetExtNetSubnet["totalIpCount"] == len(sourceEgwSubnets.get(targetExtNetSubnetAddress)):
                            if self.orgVdcInput.get("EmptyIPPoolOverride", False):
                                continue
                            else:
                                raise Exception("External Network subnet should have atleast one free IP address which cannot be removed."
                                                " EmptyPoolOverride flag must be set to true to perform successfull rollback/cleanup")

                        # creating range of target external network pool range
                        targetExtNetIpRange = set()
                        for externalRange in targetExtNetSubnet['ipRanges']['values']:
                            targetExtNetIpRange.update(self.createIpRange(
                                '{}/{}'.format(targetExtNetSubnet['gateway'], targetExtNetSubnet['prefixLength']),
                                externalRange['startAddress'], externalRange['endAddress']
                            ))
                        targetExtNetIpRange = set(targetExtNetIpRange)

                        # creating range of source edge gateway ip range
                        sourceEdgeGatewaySubIpRange = set()
                        for ipRange in sourceEgwSubnets.get(targetExtNetSubnetAddress, []):
                            sourceEdgeGatewaySubIpRange.update(self.createIpRange(
                                '{}/{}'.format(targetExtNetSubnet['gateway'], targetExtNetSubnet['prefixLength']),
                                ipRange['startAddress'], ipRange['endAddress']
                            ))

                        # removing the source edge gateway's static ips from target external ip list
                        targetExtNetIpRange = targetExtNetIpRange.difference(sourceEdgeGatewaySubIpRange)

                        # creating the range of each single ip in target external network's ips
                        targetExtNetSubnet['ipRanges']['values'] = self.createExternalNetworkSubPoolRangePayload(
                            targetExtNetIpRange)

                    payloadData = json.dumps(targetExtNetData)

                    # url to update the target external networks
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_EXTERNAL_NETWORKS,
                                           targetExtNetData['id'])

                    # setting the content type to json
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE

                    # put api call to update the target external networks
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug("Successfully reset the target external network '{}' to its initial state".format(
                            targetExtNetData['name']))
                    else:
                        errorDict = apiResponse.json()
                        msg = "Failed to update External network {} with sub allocated ip pools - {}".format(
                            targetExtNetData['name'], errorDict['message'])
                        if "provided list 'ipRanges.values' should have at least one" in errorDict.get("message", ""):
                            msg += " Add one extra IP address to static pool of external network - {}".format(
                                targetExtNetData['name'])
                        raise Exception(msg)

        except Exception:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def getCatalogDetails(self, catalogHref):
        """
        Description :   Returns the details of the catalog
        Parameters: catalogHref - href of catalog for which details required (STRING)
        """
        try:
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            catalogResponse = self.restClientObj.get(catalogHref, headers)
            if catalogResponse.status_code == requests.codes.ok:
                catalogResponseDict = catalogResponse.json()
                return catalogResponseDict
            else:
                errorDict = catalogResponse.json()
                raise Exception("Failed to retrieve the catalog details: {}".format(errorDict['message']))
        except Exception:
            raise

    @isSessionExpired
    def getCatalogOwner(self, catalogOwnerHref):
        """
        Description :   Returns the details of the catalog owner
        Parameters: catalogHref - href of catalog owner for which details required (STRING)
        """
        try:
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            catalogOwnerResponse = self.restClientObj.get(catalogOwnerHref, headers)
            if catalogOwnerResponse.status_code == requests.codes.ok:
                catalogOwnerResponseDict = catalogOwnerResponse.json()
                return catalogOwnerResponseDict
            else:
                errorDict = catalogOwnerResponse.json()
                raise Exception("Failed to retrieve the catalog owner details: {}".format(errorDict['message']))
        except Exception:
            raise

    @isSessionExpired
    def updateCatalogOwner(self, owner, catalogResponseDict):
        """
                Description :   Updates catalog owner to whomever created the catalog in source side
                Parameters: owner - dict containing owner details like href, type, name
                            catalogResponseDict - catalog dict which is POST API response content for creating catalog
        """
        try:
            # create PUT API catalog url
            putUrl = "{}/{}".format(str(catalogResponseDict['AdminCatalog']['@href']), "owner")
            # creating the payload
            payloadData = {
                "user": {
                    "href": owner['@href'],
                    "type": owner['@type'],
                    "name": owner['@name']}
                }
            payloadData = json.dumps(payloadData)
            # setting headers
            headers = {'Authorization': self.headers['Authorization'],
                         'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER,
                         'Content-Type': vcdConstants.GENERAL_JSON_CONTENT_TYPE_HEADER}
            # PUT API for updating catalog owner
            updateCatalogOwner = self.restClientObj.put(putUrl, headers, data=payloadData)

            if updateCatalogOwner.status_code == (requests.codes.no_content or requests.codes.ok):
                logger.debug("Catalog '{}' owner updated successfully".format(catalogResponseDict['AdminCatalog']['@name']))
            else:
                errorDict = self.vcdUtils.parseXml(updateCatalogOwner.content)
                raise Exception("Failed to update Catalog '{}' owner : {}".format(catalogResponseDict['AdminCatalog']['@name'],
                                                                            errorDict['Error']['@message']))

        except Exception:
            raise

    @isSessionExpired
    def createCatalog(self, catalog, orgId, owner, srcCatalogId, readAccessToAllOrg):
        """
        Description :   Creates an empty placeholder catalog
        Parameters: catalog - payload dict for creating catalog (DICT)
                    orgId - Organization Id where catalog is to be created (STRING)
                    owner - owner dict containing details of Owner
                    srcCatalogId - Source Catalog ID
                    readAccessToAllOrg - Is the Read-Only access of catalog given to all ORGs (Boolean)
        """
        try:
            # create catalog url
            catalogUrl = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                       vcdConstants.CREATE_CATALOG.format(orgId))
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # creating the payload data
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      catalog,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.CREATE_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # setting the content-type to create a catalog
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.VCD_API_HEADER,
                       'Content-Type': vcdConstants.XML_CREATE_CATALOG}
            # post api call to create target catalogs
            createCatalogResponse = self.restClientObj.post(catalogUrl, headers, data=payloadData)
            if createCatalogResponse.status_code == requests.codes.created:
                logger.debug("Catalog '{}' created successfully".format(catalog['catalogName']))
                createCatalogResponseDict = self.vcdUtils.parseXml(createCatalogResponse.content)
                # getting the newly created target catalog id
                catalogId = createCatalogResponseDict["AdminCatalog"]["@id"].split(':')[-1]

                # Checking if catalog owner is system or not
                # if not system than updating the owner in above created Target Catalog
                if owner and owner.get('@name') != vcdConstants.ADMIN_USER:
                    # calling function to update owner of catalog
                    self.updateCatalogOwner(owner, createCatalogResponseDict)
                # Source catalog name
                srcCatalogName = str(catalog.get('catalogName')).replace("-v2t", "")
                # Getting the Share Permissions to set in the catalog
                sharePermissions = self.getSharePermissions(srcCatalogId, srcCatalogName)
                # Setting the Share Permissions in the catalog
                self.setSharePermissions(sharePermissions, catalogId, catalog.get('catalogName'))
                # Check if catalog has shared READ-ONLY access with all orgs
                if readAccessToAllOrg == "true" or readAccessToAllOrg is True:
                    readAccessUrl = "{}/{}".format(createCatalogResponseDict["AdminCatalog"]["@href"],
                                                vcdConstants.PUBLISH_CATALOG_READ_ACCESS_TO_ALL_ORG)
                    self.readAccessToAllOrgs(readAccessUrl, catalog.get('catalogName'))
                return catalogId
            else:
                errorDict = self.vcdUtils.parseXml(createCatalogResponse.content)
                raise Exception("Failed to create Catalog '{}' : {}".format(catalog['catalogName'],
                                                                            errorDict['Error']['@message']))

        except Exception:
            raise

    @isSessionExpired
    def readAccessToAllOrgs(self, readAccessUrl, migratedCatalogName):
        """
                Description : Set READ_ONLY Access of a catalog to all Orgs
                Parameters: readAccessUrl - URL to Share READ-ONLY Access of catalog to all Orgs
                            migratedCatalogName - Target Catalog Name
        """
        try:
            # setting headers
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.VCD_API_HEADER.format(self.version),
                       'Content-Type': vcdConstants.GENERAL_XML_CONTENT_TYPE}
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # Payload for Read-Only Access of Catalog to all ORGs
            publishPayload = dict()
            publishPayloadData = self.vcdUtils.createPayload(filePath,
                                                             publishPayload,
                                                             fileType='yaml',
                                                             componentName=vcdConstants.COMPONENT_NAME,
                                                             templateName=vcdConstants.READ_ACCESS_CATALOG_TEMPLATE)
            publishPayloadData = json.loads(publishPayloadData)
            # POST API for sharing Read-Only access of catalog to all Orgs
            response = self.restClientObj.post(readAccessUrl, headers, data=publishPayloadData)

            if response.status_code == (requests.codes.no_content or requests.codes.ok):
                logger.debug(
                    "Catalog '{}'s READ-ONLY Access shared to all ORGs Successfully".format(migratedCatalogName))
                return
            else:
                raise Exception(
                    "Failed to Share READ-ONLY Access of Catalog '{}' to all ORGs : {}".format(migratedCatalogName,
                                                                                                    response))
        except Exception:
            raise

    @isSessionExpired
    def setSharePermissions(self, sharePermissions, catalogId, migratedCatalogName):
        """
                Description :  Gets the Share Permissions of a catalog
                Parameters: sharePermissions - Share Permission Data
                            catalogId - Target Catalog ID
                            migratedCatalogName - Target Catalog Name
        """
        try:
            # POST API URL for share permission
            postUrl = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                    vcdConstants.SET_CATALOG_SHARE_PERMISSIONS.format(catalogId))
            # setting headers
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER.format(self.version),
                       'Content-Type': vcdConstants.GENERAL_JSON_CONTENT_TYPE_HEADER}
            # Payload of Share Permissions
            payloadDict = json.loads(sharePermissions.content)
            # POST API for setting Share Permission for catalog
            response = self.restClientObj.post(postUrl, headers, data=json.dumps(payloadDict))

            if response.status_code == requests.codes.ok:
                logger.debug(
                    "Catalog '{}' Share Permissions attached successfully".format(migratedCatalogName))
                return
            else:
                raise Exception(
                    "Failed to set Share Permissions for Catalog '{}' : {}".format(migratedCatalogName,
                                                                                   response))
        except Exception:
            raise

    @isSessionExpired
    def getSharePermissions(self, srcCatalogId, srcCatalogName):
        """
                Description :  Gets the Share Permissions of a catalog
                Parameters: srcCatalogId - ID of the source catalog
                            srcCatalogName - Name of the source catalog
        """
        try:
            # GET API URL for share permission
            getUrl = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                   vcdConstants.GET_CATALOG_SHARE_PERMISSIONS.format(srcCatalogId))
            # setting headers
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER.format(self.version)}
            # GET API for fetching Share Permission for catalog
            sharePermissionsResponse = self.restClientObj.get(getUrl, headers)

            if sharePermissionsResponse.status_code == requests.codes.ok:
                logger.debug(
                    "Catalog '{}' Share Permissions fetched successfully".format(srcCatalogName))
                return sharePermissionsResponse
            else:
                raise Exception(
                    "Failed to fetch Share Permissions for Catalog '{}' : {}".format(srcCatalogName,
                                                                                     sharePermissionsResponse))
        except Exception:
            raise

    @isSessionExpired
    def moveCatalogItem(self, catalogItem, catalogId, timeout):
        """
        Description :   Moves the catalog Item
        Parameters : catalogItem - catalog item payload (DICT)
                     catalogId - catalog Id where this catalogitem to be moved (STRING)
        """
        try:
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            # move catalog item url
            moveCatalogItemUrl = "{}{}".format(vcdConstants.XML_API_URL.format(self.ipAddress),
                                               vcdConstants.MOVE_CATALOG.format(catalogId))
            # creating the payload data to move the catalog item
            payloadData = self.vcdUtils.createPayload(filePath,
                                                      catalogItem,
                                                      fileType='yaml',
                                                      componentName=vcdConstants.COMPONENT_NAME,
                                                      templateName=vcdConstants.MOVE_CATALOG_TEMPLATE)
            payloadData = json.loads(payloadData)
            # post api call to move catalog items
            response = self.restClientObj.post(moveCatalogItemUrl, self.headers, data=payloadData)
            responseDict = self.vcdUtils.parseXml(response.content)
            if response.status_code == requests.codes.accepted:
                task = responseDict["Task"]
                taskUrl = task["@href"]
                if taskUrl:
                    # checking the status of moving catalog item task
                    self._checkTaskStatus(taskUrl=taskUrl, timeoutForTask=timeout)
                logger.debug("Catalog Item '{}' moved successfully".format(catalogItem['catalogItemName']))
            else:
                raise Exception('Failed to move catalog item - {}'.format(responseDict['Error']['@message']))

        except Exception:
            raise

    @description("creation of target Org VDC")
    @remediate
    def createOrgVDC(self):
        """
        Description :   Creates an Organization VDC
        """
        try:
            logger.info('Preparing Target VDC.')
            logger.info('Creating target Org VDC')
            filePath = os.path.join(vcdConstants.VCD_ROOT_DIRECTORY, 'template.yml')
            data = self.rollback.apiData
            targetOrgVDCId = ''
            # organization id
            orgCompleteId = data['Organization']['@id']
            orgId = orgCompleteId.split(':')[-1]
            # retrieving organization url
            orgUrl = data['Organization']['@href']
            # retrieving source org vdc and target provider vdc data
            sourceOrgVDCPayloadDict = data["sourceOrgVDC"]
            targetPVDCPayloadDict = data['targetProviderVDC']
            targetPVDCPayloadList = [
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile']] if isinstance(
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile'], dict) else \
                targetPVDCPayloadDict['StorageProfiles']['ProviderVdcStorageProfile']
            sourceOrgVDCPayloadList = [
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile']] if isinstance(
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile'], dict) else \
                sourceOrgVDCPayloadDict['VdcStorageProfiles']['VdcStorageProfile']

            vdcStorageProfilePayloadData = ''
            # iterating over the source org vdc storage profiles
            for eachStorageProfile in sourceOrgVDCPayloadList:
                orgVDCStorageProfileDetails = self.getOrgVDCStorageProfileDetails(eachStorageProfile['@id'])
                vdcStorageProfileDict = {'vspEnabled': "true" if orgVDCStorageProfileDetails['AdminVdcStorageProfile'][
                                                                     'Enabled'] == "true" else "false",
                                         'vspUnits': 'MB',
                                         'vspLimit': str(
                                             orgVDCStorageProfileDetails['AdminVdcStorageProfile']['Limit']),
                                         'vspDefault': "true" if orgVDCStorageProfileDetails['AdminVdcStorageProfile'][
                                                                     'Default'] == "true" else "false"}
                for eachSP in targetPVDCPayloadList:
                    if eachStorageProfile['@name'] == eachSP['@name']:
                        vdcStorageProfileDict['vspHref'] = eachSP['@href']
                        vdcStorageProfileDict['vspName'] = eachSP['@name']
                        break
                eachStorageProfilePayloadData = self.vcdUtils.createPayload(filePath,
                                                                            vdcStorageProfileDict,
                                                                            fileType='yaml',
                                                                            componentName=vcdConstants.COMPONENT_NAME,
                                                                            templateName=vcdConstants.STORAGE_PROFILE_TEMPLATE_NAME)
                vdcStorageProfilePayloadData += eachStorageProfilePayloadData.strip("\"")

            # Shared network and DFW need target org VDC to be part of DC group. If org VDC is created without network
            # pool, it cannot be part of DC group. Hence if shared network or DFW is present, assign default or user
            # provided network pool of target PVDC to target Org VDC.
            if (data['sourceOrgVDC'].get('NetworkPoolReference')
                    or self.isSharedNetworkPresent()
                    or self.getDistributedFirewallConfig()):
                networkPoolReferences = targetPVDCPayloadDict['NetworkPoolReferences']

                # if multiple network pools exist, take the network pool references passed in user spec
                if isinstance(networkPoolReferences['NetworkPoolReference'], list):
                    tpvdcNetworkPool = [
                        pool
                        for pool in networkPoolReferences['NetworkPoolReference']
                        if pool['@name'] == self.orgVdcInput.get('NSXTNetworkPoolName')
                    ]
                    if tpvdcNetworkPool:
                        networkPoolHref = tpvdcNetworkPool[0]['@href']
                        networkPoolId = tpvdcNetworkPool[0]['@id']
                        networkPoolName = tpvdcNetworkPool[0]['@name']
                        networkPoolType = tpvdcNetworkPool[0]['@type']
                    else:
                        raise Exception(
                            f"Network Pool {self.orgVdcInput.get('NSXTNetworkPoolName')} doesn't exist in Target PVDC")

                # if PVDC has a single network pool, take it
                else:
                    networkPoolHref = targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@href']
                    networkPoolId = targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@id']
                    networkPoolName = targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@name']
                    networkPoolType = targetPVDCPayloadDict['NetworkPoolReferences']['NetworkPoolReference']['@type']

            else:
                logger.debug(
                    'Network pool not present and Org VDC is not using shared network or distributed firewall')
                networkPoolHref = None
                networkPoolId = None
                networkPoolName = None
                networkPoolType = None

            # creating the payload dict
            orgVdcPayloadDict = {'orgVDCName': data["sourceOrgVDC"]["@name"] + '-v2t',
                                 'vdcDescription': data['sourceOrgVDC']['Description'] if data['sourceOrgVDC'].get(
                                     'Description') else '',
                                 'allocationModel': data['sourceOrgVDC']['AllocationModel'],
                                 'cpuUnits': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Units'],
                                 'cpuAllocated': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Allocated'],
                                 'cpuLimit': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Limit'],
                                 'cpuReserved': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Reserved'],
                                 'cpuUsed': data['sourceOrgVDC']['ComputeCapacity']['Cpu']['Used'],
                                 'memoryUnits': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Units'],
                                 'memoryAllocated': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Allocated'],
                                 'memoryLimit': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Limit'],
                                 'memoryReserved': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Reserved'],
                                 'memoryUsed': data['sourceOrgVDC']['ComputeCapacity']['Memory']['Used'],
                                 'nicQuota': data['sourceOrgVDC']['NicQuota'],
                                 'networkQuota': data['sourceOrgVDC']['NetworkQuota'],
                                 'vmQuota': data['sourceOrgVDC']['VmQuota'],
                                 'isEnabled': "true",
                                 'vdcStorageProfile': vdcStorageProfilePayloadData,
                                 'resourceGuaranteedMemory': data['sourceOrgVDC']['ResourceGuaranteedMemory'],
                                 'resourceGuaranteedCpu': data['sourceOrgVDC']['ResourceGuaranteedCpu'],
                                 'vCpuInMhz': data['sourceOrgVDC']['VCpuInMhz'],
                                 'isThinProvision': data['sourceOrgVDC']['IsThinProvision'],
                                 'networkPoolHref': networkPoolHref,
                                 'networkPoolId': networkPoolId,
                                 'networkPoolName': networkPoolName,
                                 'networkPoolType': networkPoolType,
                                 'providerVdcHref': targetPVDCPayloadDict['@href'],
                                 'providerVdcId': targetPVDCPayloadDict['@id'],
                                 'providerVdcName': targetPVDCPayloadDict['@name'],
                                 'providerVdcType': targetPVDCPayloadDict['@type'],
                                 'usesFastProvisioning': data['sourceOrgVDC']['UsesFastProvisioning'],
                                 'defaultComputePolicy': '',
                                 'isElastic': data['sourceOrgVDC']['IsElastic'],
                                 'includeMemoryOverhead': data['sourceOrgVDC']['IncludeMemoryOverhead']}

            # retrieving org vdc compute policies
            allOrgVDCComputePolicesList = self.getOrgVDCComputePolicies()
            isSizingPolicy = False
            # getting the vm sizing policy of source org vdc
            sourceSizingPoliciesList = self.getVmSizingPoliciesOfOrgVDC(data['sourceOrgVDC']['@id'])
            if isinstance(sourceSizingPoliciesList, dict):
                sourceSizingPoliciesList = [sourceSizingPoliciesList]
            # iterating over the source org vdc vm sizing policies and check the default compute policy is sizing policy
            for eachPolicy in sourceSizingPoliciesList:
                if eachPolicy['id'] == data['sourceOrgVDC']['DefaultComputePolicy']['@id'] and eachPolicy[
                    'name'] != 'System Default':
                    # set sizing policy to true if default compute policy is sizing
                    isSizingPolicy = True
            if data['sourceOrgVDC']['DefaultComputePolicy']['@name'] != 'System Default' and not isSizingPolicy:
                # Getting the href of the compute policy if not 'System Default' as default compute policy
                orgVDCComputePolicesList = [allOrgVDCComputePolicesList] if isinstance(allOrgVDCComputePolicesList,
                                                                                       dict) else allOrgVDCComputePolicesList
                # iterating over the org vdc compute policies
                for eachComputPolicy in orgVDCComputePolicesList:
                    if eachComputPolicy["name"] == data['sourceOrgVDC']['DefaultComputePolicy']['@name'] and \
                            (eachComputPolicy["pvdcId"] == data['targetProviderVDC']['@id'] or not eachComputPolicy["pvdcId"]):
                        if not eachComputPolicy["pvdcId"] and not eachComputPolicy['id'] == data['sourceOrgVDC']['DefaultComputePolicy']['@id']:
                            continue
                        href = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                vcdConstants.VDC_COMPUTE_POLICIES,
                                                eachComputPolicy["id"])
                        computePolicyDict = {'defaultComputePolicyHref': href,
                                             'defaultComputePolicyId': eachComputPolicy["id"],
                                             'defaultComputePolicyName': data['sourceOrgVDC']['DefaultComputePolicy'][
                                                 '@name']}
                        computePolicyPayloadData = self.vcdUtils.createPayload(filePath,
                                                                               computePolicyDict,
                                                                               fileType='yaml',
                                                                               componentName=vcdConstants.COMPONENT_NAME,
                                                                               templateName=vcdConstants.COMPUTE_POLICY_TEMPLATE_NAME)
                        orgVdcPayloadDict['defaultComputePolicy'] = computePolicyPayloadData.strip("\"")
                        break
                else:  # for else (loop else)
                    raise Exception(
                        "No Target Compute Policy found with same name as Source Org VDC default Compute Policy and belonging to the target Provider VDC.")
            # if sizing policy is set, default compute policy is vm sizing polciy
            if isSizingPolicy:
                computePolicyDict = {'defaultComputePolicyHref': data['sourceOrgVDC']['DefaultComputePolicy']['@href'],
                                     'defaultComputePolicyId': data['sourceOrgVDC']['DefaultComputePolicy']['@id'],
                                     'defaultComputePolicyName': data['sourceOrgVDC']['DefaultComputePolicy']['@name']}
                computePolicyPayloadData = self.vcdUtils.createPayload(filePath,
                                                                       computePolicyDict,
                                                                       fileType='yaml',
                                                                       componentName=vcdConstants.COMPONENT_NAME,
                                                                       templateName=vcdConstants.COMPUTE_POLICY_TEMPLATE_NAME)
                orgVdcPayloadDict['defaultComputePolicy'] = computePolicyPayloadData.strip("\"")
            orgVdcPayloadData = self.vcdUtils.createPayload(filePath,
                                                            orgVdcPayloadDict,
                                                            fileType='yaml',
                                                            componentName=vcdConstants.COMPONENT_NAME,
                                                            templateName=vcdConstants.CREATE_ORG_VDC_TEMPLATE_NAME)

            payloadData = json.loads(orgVdcPayloadData)

            # url to create org vdc
            url = "{}{}".format(vcdConstants.XML_ADMIN_API_URL.format(self.ipAddress),
                                vcdConstants.CREATE_ORG_VDC.format(orgId))
            self.headers["Content-Type"] = vcdConstants.XML_CREATE_VDC_CONTENT_TYPE
            # post api to create org vdc

            response = self.restClientObj.post(url, self.headers, data=payloadData)
            responseDict = self.vcdUtils.parseXml(response.content)
            if response.status_code == requests.codes.created:
                taskId = responseDict["AdminVdc"]["Tasks"]["Task"]
                if isinstance(taskId, dict):
                    taskId = [taskId]
                for task in taskId:
                    if task["@operationName"] == vcdConstants.CREATE_VDC_TASK_NAME:
                        taskUrl = task["@href"]
                        # Fetching target org vdc id for deleting target vdc in case of failure
                        targetOrgVDCId = re.search(r'\((.*)\)', task['@operation']).group(1)
                if taskUrl:
                    # checking the status of the task of creating the org vdc
                    self._checkTaskStatus(taskUrl=taskUrl)
                logger.info('Target Org VDC {} created successfully'.format(data["sourceOrgVDC"]["@name"] + '-v2t'))
                # returning the id of the created org vdc
                return self.getOrgVDCDetails(orgUrl, responseDict['AdminVdc']['@name'], 'targetOrgVDC')
            raise Exception('Failed to create target Org VDC. Errors {}.'.format(responseDict['Error']['@message']))
        except Exception as exception:
            logger.debug(traceback.format_exc())
            if targetOrgVDCId:
                logger.debug("Creation of target vdc failed, so removing that entity from vCD")
                try:
                    self.deleteOrgVDC(targetOrgVDCId)
                except Exception as e:
                    errorMessage = f'No access to entity "com.vmware.vcloud.entity.vdc:{targetOrgVDCId}'
                    if errorMessage in str(e):
                        pass
                    else:
                        raise Exception('Failed to delete target org vdc during rollback')
            raise exception

    @description(desc="enabling the promiscuous mode and forged transmit on source Org VDC networks")
    @remediate
    def enablePromiscModeForgedTransmit(self, orgVDCNetworkList):
        """
        Description : Enabling Promiscuous Mode and Forged transmit of source org vdc network
        Parameters  : orgVDCNetworkList - List containing source org vdc networks (LIST)
        """
        try:
            logger.info('Enabling the promiscuous mode and forged transmit on source Org VDC networks.')
            # if call to disable to promiscuous mode then orgVDCNetworkList will be retrieved from apiOutput.json
            # iterating over the orgVDCNetworkList
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS,
                                          orgVdcNetwork['id'],
                                          vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()
                    # if enable call then setting the mode True
                    for portGroupData in responseDict['dvpgProperties']:
                        portGroupData['promiscuousMode'] = True
                        portGroupData['forgedTransmit'] = True

                    payloadData = json.dumps(responseDict)
                    # updating the org vdc network dvportgroup properties
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to update the promiscuous mode and forged mode
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the updating dvpgportgroup properties of org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Successfully enabled source Org VDC Network {} dvportgroup properties.'.format(
                            orgVdcNetwork['name']))
                    else:
                        errorResponse = apiResponse.json()
                        raise Exception(
                            'Failed to enable dvportgroup properties of source Org VDC network {} - {}'.format(
                                orgVdcNetwork['name'], errorResponse['message']))
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(
                        orgVdcNetwork['name']))
        except Exception:
            raise

    @isSessionExpired
    def disablePromiscModeForgedTransmit(self):
        """
        Description : Disabling Promiscuous Mode and Forged transmit of source org vdc network
        """
        try:
            if not self.rollback.metadata.get("prepareTargetVDC", {}).get("enablePromiscModeForgedTransmit"):
                return
            logger.info("RollBack: Restoring the Promiscuous Mode and Forged Mode")
            data = self.rollback.apiData
            orgVDCNetworkList = data["orgVDCNetworkPromiscModeList"]
            # iterating over the orgVDCNetworkList
            for orgVdcNetwork in orgVDCNetworkList:
                # url to get the dvportgroup details of org vdc network
                url = "{}{}/{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                          vcdConstants.ALL_ORG_VDC_NETWORKS,
                                          orgVdcNetwork['id'],
                                          vcdConstants.ORG_VDC_NETWORK_PORTGROUP_PROPERTIES_URI)
                # get api call to retrieve the dvportgroup details of org vdc network
                response = self.restClientObj.get(url, self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = response.json()

                    # Iterating over all the portgroups to reset the promiscous and forged-transmit value
                    for index, portGroupData in enumerate(orgVdcNetwork['promiscForge']['dvpgProperties']):
                        # disable call then setting the mode to its initial state by retrieving from metadata
                        responseDict['dvpgProperties'][index]['promiscuousMode'] = portGroupData['promiscuousMode']
                        responseDict['dvpgProperties'][index]['forgedTransmit'] = portGroupData['forgedTransmit']

                    payloadData = json.dumps(responseDict)

                    # updating the org vdc network dvportgroup properties
                    self.headers["Content-Type"] = vcdConstants.OPEN_API_CONTENT_TYPE
                    # put api call to update the promiscuous mode and forged mode
                    apiResponse = self.restClientObj.put(url, self.headers, data=payloadData)
                    if apiResponse.status_code == requests.codes.accepted:
                        taskUrl = apiResponse.headers['Location']
                        # checking the status of the updating dvpgportgroup properties of org vdc network task
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug('Successfully disabled source Org VDC Network {} dvportgroup properties.'.format(
                            orgVdcNetwork['name']))
                    else:
                        errorResponse = apiResponse.json()
                        raise Exception(
                            'Failed to disabled dvportgroup properties of source Org VDC network {} - {}'.format(
                                orgVdcNetwork['name'], errorResponse['message']))
                else:
                    raise Exception('Failed to get dvportgroup properties of source Org VDC network {}'.format(
                        orgVdcNetwork['name']))
        except Exception:
            raise

    @isSessionExpired
    def renameVappNetworks(self, vAppNetworkHref):
        """
        Description :   Renames the vApp isolated network back to its original name
                        (i.e removes the trailing -v2t string of the vapp network name)
        Parameters  :   vAppNetworkHref -   href of the vapp network (STRING)
        """
        try:
            # setting the headers required for the api
            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            # get api call to retrieve the vapp isolated networks' details
            vAppNetworkResponse = self.restClientObj.get(vAppNetworkHref, headers)
            vAppNetworkResponseDict = vAppNetworkResponse.json()
            # changing the name of the vapp isolated network
            vAppNetworkResponseDict['name'] = vAppNetworkResponseDict['name'][
                                              0: len(vAppNetworkResponseDict['name']) - 4]
            # creating the payload data
            payloadData = json.dumps(vAppNetworkResponseDict)
            # setting the content-type required for the api
            headers['Content-Type'] = vcdConstants.VAPP_NETWORK_CONTENT_TYPE
            # put api call to update rename the target vapp isolated network
            putResponse = self.restClientObj.put(vAppNetworkHref, headers=headers, data=payloadData)
            if putResponse.status_code == requests.codes.ok:
                logger.debug(
                    "Target vApp Isolated Network successfully renamed to '{}'".format(vAppNetworkResponseDict['name']))
            else:
                putResponseDict = putResponse.json()
                raise Exception("Failed to rename the target vApp Isolated Network '{}' : {}".format(
                    vAppNetworkResponseDict['name'] + '-v2t',
                    putResponseDict['message']))
            # sleep for 5 seconds before deleting next network
            time.sleep(5)
        except Exception:
            raise

    @isSessionExpired
    def renameTargetVappIsolatedNetworks(self, vAppList):
        """
        Description :   Renames all the vApp isolated networks for each vApp in the specified vApps list
        Parameters  :   vAppList    -   list of details of target vApps (LIST)
        """
        try:
            # iterating over the target vapps
            for vApp in vAppList:
                # get api call to retrieve the details of target vapp
                vAppResponse = self.restClientObj.get(vApp['href'], self.headers)
                vAppResponseDict = self.vcdUtils.parseXml(vAppResponse.content)
                vAppData = vAppResponseDict['VApp']
                # checking for the networks in the vapp
                if vAppData['NetworkConfigSection'].get('NetworkConfig'):
                    vAppNetworkList = vAppData['NetworkConfigSection']['NetworkConfig'] if isinstance(vAppData['NetworkConfigSection']['NetworkConfig'], list) else [vAppData['NetworkConfigSection']['NetworkConfig']]
                    if vAppNetworkList:
                        # iterating over the networks in vapp
                        for vAppNetwork in vAppNetworkList:
                            # handling only vapp isolated networks whose name ends with -v2t
                            if vAppNetwork['@networkName'].endswith('-v2t'):
                                vAppLinksList = vAppData['Link'] if isinstance(vAppData['Link'], list) else [vAppData['Link']]
                                # iterating over the vAppLinksList to get the vapp isolated networks' href
                                for link in vAppLinksList:
                                    if link.get('@name'):
                                        if link['@name'] == vAppNetwork['@networkName'] and 'admin' not in link['@href']:
                                            vAppNetworkHref = link['@href']
                                            break
                                else:
                                    logger.debug("Failed to rename the target isolated network '{}', since failed to get href".format(vAppNetwork['@networkName']))
                                    continue
                                self.renameVappNetworks(vAppNetworkHref)
        except Exception:
            raise

    @isSessionExpired
    def renameTargetOrgVDCNetworks(self, network):
        """
        Description :   Renames the target org VDC networks back to its original name
                        (i.e removes the trailing -v2t from the target org VDC network name)
        Parameters  :   network -   details of the network that is to be renamed (DICT)
        """
        try:
            if not network["name"].endswith('-v2t'):
                return

            headers = {'Authorization': self.headers['Authorization'],
                       'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            # open api get url to retrieve the details of target org vdc network
            url = "{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                vcdConstants.GET_ORG_VDC_NETWORK_BY_ID.format(network['id']))
            # get api call to retrieve the details of target org vdc network
            networkResponse = self.restClientObj.get(url, headers=self.headers)
            networkResponseDict = networkResponse.json()

            # checking if the target org vdc network name endwith '-v2t', if so removing the '-v2t' from the name
            if networkResponseDict['name'].endswith('-v2t'):
                # getting the original name of the
                networkResponseDict['name'] = networkResponseDict['name'][0: len(networkResponseDict['name']) - 4]
                # creating the payload data of the retrieved details of the org vdc network
                payloadData = json.dumps(networkResponseDict)
                # setting the content-type as per the api requirement
                headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                # put api call to rename the target org vdc network
                putResponse = self.restClientObj.put(url, headers=headers, data=payloadData)
                if putResponse.status_code == requests.codes.accepted:
                    taskUrl = putResponse.headers['Location']
                    self._checkTaskStatus(taskUrl=taskUrl)
                    logger.debug("Target Org VDC Network '{}' renamed successfully".format(networkResponseDict['name']))
                else:
                    errorDict = putResponse.json()
                    raise Exception("Failed to rename the target org VDC to '{}' : {}".format(networkResponseDict['name'],
                                                                                              errorDict['message']))

        except Exception:
            raise

    @isSessionExpired
    def createDCgroup(self, dcGroupName, sharedGroup=False, orgVdcIdList=None):
        """
        Description: Create datacenter group
        Parameter: dcGroupName - Name of datacenter group to be created (STRING)
                   sharedGroup - Flag that decides to share org vdc group with multiple org vdc (BOOLEAN)
                   orgVDCIDList-   List of all the org vdc's undergoing parallel migration (LIST)
        """
        # open api to create Org vDC group
        url = '{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS)
        targetOrgVDCId = self.rollback.apiData['targetOrgVDC']['@id']
        organizationId = self.rollback.apiData['Organization']['@id']

        if sharedGroup:
            payloadDict = {'orgId': organizationId,
                           'name': dcGroupName,
                           'participatingOrgVdcs': [{
                               'vdcRef': {'id': orgVDCId}, 'orgRef': {'id': organizationId},
                           } for orgVDCId in orgVdcIdList],
                           'type': 'LOCAL',
                           'networkProviderType': 'NSX_T'
                           }
        else:
            payloadDict = {'orgId': organizationId,
                           'name': dcGroupName,
                           'participatingOrgVdcs': [{
                               'vdcRef': {'id': targetOrgVDCId}, 'orgRef': {'id': organizationId}}],
                           'type': 'LOCAL',
                           'networkProviderType': 'NSX_T'
                           }
        payloadData = json.dumps(payloadDict)
        # setting the content-type as per the api requirement
        self.headers['Content-Type'] = 'application/json'
        response = self.restClientObj.post(url, self.headers, data=payloadData)
        if response.status_code == requests.codes.accepted:
            taskUrl = response.headers['Location']
            header = {'Authorization': self.headers['Authorization'],
                      'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
            taskResponse = self.restClientObj.get(url=taskUrl, headers=header)
            responseDict = taskResponse.json()
            self._checkTaskStatus(taskUrl=taskUrl)
            logger.debug(
                "Target Org VDC Group '{}' created successfully".format(dcGroupName))
            return responseDict['owner']['id']
        else:
            errorDict = response.json()
            raise Exception("Failed to create target org VDC Group '{}' ".format(errorDict['message']))

    @description('Creating Org vDC groups for Imported Networks in Target Org VDC')
    @remediate
    def createOrgvDCGroupForImportedNetworks(self, sourceOrgVDCName, vcdObjList):
        """
        Description: Creating Shared Org vDC group with multiple Org vDC for imported networks
        Parameter:   sourceOrgVDCName -  Name of the source orgVDC (STRING)
                     vcdObjList       -   List of vcd operations class objects (LIST)
        """
        try:
            logger.debug("Org VDC group is getting created for direct/imported networks")
            # Taking lock as one org vdc will be creating groups first
            self.lock.acquire(blocking=True)
            # Source org vdc id list
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            # Fetching target org vdc id list
            orgVDCIDList = [vcdObj.rollback.apiData['targetOrgVDC']['@id'] for vcdObj in vcdObjList]
            # Fetch data center group id from metadata
            ownerIds = self.rollback.apiData.get('OrgVDCGroupID', {})
            orgVdcNetworks = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            # Fetch target org vdc id list
            targetOrgVDCNetworks = self.retrieveNetworkListFromMetadata(self.rollback.apiData['targetOrgVDC']['@id'],
                                                                        dfwStatus=False, orgVDCType='target')
            # Fetching all target org vdc networks from all the org vdc's
            allTargetOrgVDCNetworks = list()
            for vcdObj in vcdObjList:
                allTargetOrgVDCNetworks += vcdObj.retrieveNetworkListFromMetadata(
                    self.rollback.apiData['targetOrgVDC']['@id'], dfwStatus=False, orgVDCType='target')

            orgId = self.rollback.apiData['Organization']['@id']
            targetOrgVDCNameList = [vcdObj.vdcName + "-v2t" for vcdObj in vcdObjList]

            for targetNetwork in targetOrgVDCNetworks:
                # Finding all vdc groups linked to the org vdc's to be parallely migrated
                vdcGroups = [dcGroup for dcGroup in self.getOrgVDCGroup() if
                             dcGroup['orgId'] == orgId and [vdc for vdc in dcGroup['participatingOrgVdcs'] if
                                                            vdc['vdcRef']['name'] in targetOrgVDCNameList]]


                # Finding shared dc group
                sharedDCGroup = [dcGroup for dcGroup in vdcGroups if
                                 len(dcGroup['participatingOrgVdcs']) == len(vcdObjList)]

                # Finding Non-shared dc group
                nonSharedDCGroup = [dcGroup for dcGroup in vdcGroups if
                                 len(dcGroup['participatingOrgVdcs']) == 1]

                for network in orgVdcNetworks:
                    if targetNetwork['name'] == network['name'] + '-v2t':
                        # Handle datacenter group scenario for imported Shared/non-Shared network use case.
                        if network["networkType"] == "DIRECT" and \
                                targetNetwork["networkType"] == "OPAQUE" and \
                                network[
                                    "backingNetworkType"] == vcdConstants.DIRECT_NETWORK_CONNECTED_TO_PG_BACKED_EXT_NET and \
                                targetNetwork['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}):

                            # Searching for dc group having no conflicts with the imported network
                            edgeGatewayNetworkMapping = dict()
                            isolatedNetworksList = []
                            for ntw in allTargetOrgVDCNetworks:
                                # Check in case of shared/non-shared imported networks.
                                dcGroupData = sharedDCGroup if network["shared"] else nonSharedDCGroup
                                if ntw["networkType"] == "NAT_ROUTED":
                                    if ntw["connection"]["routerRef"]["id"] in self.rollback.apiData.get(
                                            'OrgVDCGroupID', {}) and self.rollback.apiData['OrgVDCGroupID'][
                                        ntw["connection"]["routerRef"]["id"]] in [group['id'] for group in
                                                                                  dcGroupData]:
                                        if ntw["connection"]["routerRef"]["id"] not in edgeGatewayNetworkMapping:
                                            edgeGatewayNetworkMapping[ntw["connection"]["routerRef"]["id"]] = [
                                                ntw]
                                        else:
                                            edgeGatewayNetworkMapping[ntw["connection"]["routerRef"]["id"]].append(
                                                ntw)

                                # Check in case of shared/non-shared isolated networks.
                                if ntw["networkType"] == "ISOLATED":
                                    if ntw["id"] in self.rollback.apiData.get(
                                            'OrgVDCGroupID', {}) and self.rollback.apiData['OrgVDCGroupID'][
                                       ntw["id"]] in [group['id'] for group in dcGroupData]:
                                        isolatedNetworksList.append(ntw)

                            dcGroupName = sourceOrgVDCName + '-Group-' + network['name']

                            dcGroupId = None
                            # Finding if the routed networks conflict with the imported network
                            for gatewayId, networkList in edgeGatewayNetworkMapping.items():
                                for ntw in networkList:
                                    for subnet in ntw['subnets']['values']:
                                        networkAddress = ipaddress.ip_network(f"{subnet['gateway']}/"
                                                                              f"{subnet['prefixLength']}",
                                                                              strict=False)
                                        networkToCheckAddress = ipaddress.ip_network(
                                            f"{targetNetwork['subnets']['values'][0]['gateway']}/"
                                            f"{targetNetwork['subnets']['values'][0]['prefixLength']}",
                                            strict=False)
                                        if networkAddress.overlaps(networkToCheckAddress):
                                            break
                                    else:
                                        continue
                                    break
                                else:
                                    dcGroupId = self.rollback.apiData['OrgVDCGroupID'][gatewayId]
                                    break

                            # Finding if isolated shared networks conflicts with the imported network
                            if not dcGroupId:
                                for ntw in isolatedNetworksList:
                                    for subnet in ntw['subnets']['values']:
                                        networkAddress = ipaddress.ip_network(f"{subnet['gateway']}/"
                                                                              f"{subnet['prefixLength']}",
                                                                              strict=False)
                                        networkToCheckAddress = ipaddress.ip_network(
                                            f"{targetNetwork['subnets']['values'][0]['gateway']}/"
                                            f"{targetNetwork['subnets']['values'][0]['prefixLength']}",
                                            strict=False)
                                        if networkAddress.overlaps(networkToCheckAddress):
                                            break
                                    else:
                                        dcGroupId = self.rollback.apiData['OrgVDCGroupID'][ntw['id']]

                            # If sdc group id without any conflicts is present use that
                            # Else create a new shared/non-shared dc group for this network
                            if not dcGroupId:
                                if network["shared"]:
                                    dcGroupId = self.createDCgroup(dcGroupName, sharedGroup=True,
                                                                   orgVdcIdList=orgVDCIDList)
                                else:
                                    dcGroupId = self.createDCgroup(dcGroupName)
                            ownerIds.update({targetNetwork['id']: dcGroupId})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                        break
        except:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @description('Creating Org vDC groups in Target Org VDC')
    @remediate
    def createOrgvDCGroup(self, sourceOrgVDCName, vcdObjList):
        """
        Description: Creating Org vDC group with single Org vDC
        Parameter:   sourceOrgVDCName -  Name of the source orgVDC (STRING)
                     vcdObjList       -   List of vcd operations class objects (LIST)
        """
        try:
            # Taking lock as one org vdc will be creating groups first
            self.lock.acquire(blocking=True)
            # Fetching target org vdc id list
            orgVDCIDList = [vcdObj.rollback.apiData['targetOrgVDC']['@id'] for vcdObj in vcdObjList]
            # Source org vdc id list
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            # Fetch all DFW rules from source org vdc id
            allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
            # Name of conflicting isolated networks
            conflictingNetworksName = list()

            targetEdgegateways = self.rollback.apiData['targetEdgeGateway']
            conflictNetworks = self.rollback.apiData.get('ConflictNetworks')
            orgVdcNetworks = self.getOrgVDCNetworks(sourceOrgVDCId, 'sourceOrgVDCNetworks', saveResponse=False)
            # Fetch target org vdc id list
            targetOrgVDCNetworks = self.retrieveNetworkListFromMetadata(self.rollback.apiData['targetOrgVDC']['@id'],
                                                                        dfwStatus=False, orgVDCType='target')
            if not conflictNetworks:
                conflictNetworks = []

            # Fetch data center group id from metadata
            ownerIds = self.rollback.apiData.get('OrgVDCGroupID', {})
            # Check if DFW is configured on source org vdc id
            if allLayer3Rules:
                logger.info('Org VDC group is getting created')
                # Iterate over target edge gateways
                for targetEdgegateway in targetEdgegateways:
                    # Check if dc group for this edge gateway is already created or not
                    if targetEdgegateway['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}):
                        dcGroupName = sourceOrgVDCName + '-Group-' + targetEdgegateway['name']
                        # Finding list of target networks connected to this edge gateway
                        targetNetworkConnectedToEdge = list(filter(
                            lambda network: network["networkType"] == "NAT_ROUTED" and
                                            network['connection']['routerRef']['id'] == targetEdgegateway['id'],
                            targetOrgVDCNetworks))

                        # Finding list of shared networks from source org vdc linked to these target networks
                        if ([targetNetwork
                             for targetNetwork in targetNetworkConnectedToEdge
                             for sourceNetwork in orgVdcNetworks
                             if sourceNetwork['name'] + '-v2t' == targetNetwork['name'] and sourceNetwork['shared']]):
                            # Creating a shared dc groups
                            dcGroupId = self.createDCgroup(dcGroupName, sharedGroup=True,
                                                           orgVdcIdList=orgVDCIDList)
                            ownerIds.update({targetEdgegateway['id']: dcGroupId})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                            # As this dc group is shared adding this to all object id's
                            for vcdObj in vcdObjList:
                                dcGroupMapping = vcdObj.rollback.apiData.get('OrgVDCGroupID', {})
                                dcGroupMapping.update({targetEdgegateway['id']: dcGroupId})
                                vcdObj.rollback.apiData['OrgVDCGroupID'] = dcGroupMapping
                        # If no shared network is connected to this edge gateway, create a normal dc group
                        else:
                            dcGroupId = self.createDCgroup(dcGroupName)
                            ownerIds.update({targetEdgegateway['id']: dcGroupId})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                # Creating dc group for all the conflicting isolated networks
                for network in conflictNetworks:
                    if network['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}):
                        dcGroupName = sourceOrgVDCName + '-Group-' + network['name']
                        # If network is shared, create a shared dc group
                        if network['shared']:
                            dcGroupId = self.createDCgroup(dcGroupName, sharedGroup=True,
                                                           orgVdcIdList=orgVDCIDList)
                            ownerIds.update({network['id']: dcGroupId})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                            # As this dc group is shared adding this to all object id's
                            for vcdObj in vcdObjList:
                                dcGroupMapping = vcdObj.rollback.apiData.get('OrgVDCGroupID', {})
                                dcGroupMapping.update({network['id']: dcGroupId})
                                vcdObj.rollback.apiData['OrgVDCGroupID'] = dcGroupMapping
                        else:
                            dcGroupId = self.createDCgroup(dcGroupName)
                            ownerIds.update({network['id']: dcGroupId})
                            self.rollback.apiData['OrgVDCGroupID'] = ownerIds

                # Creating/Checking dc group for non-conflicting non-shared isolated networks
                for targetNetwork in targetOrgVDCNetworks:
                    for network in orgVdcNetworks:
                        if targetNetwork['name'] == network['name'] + '-v2t':
                            if network["networkType"] == "ISOLATED" and \
                                    not network['shared'] and \
                                    targetNetwork['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}):
                                orgId = self.rollback.apiData['Organization']['@id']
                                # Finding non-shared dc groups for non-shared non-conflicting isolated networks
                                vdcGroups = [dcGroup for dcGroup in self.getOrgVDCGroup() if
                                             dcGroup['orgId'] == orgId and len(dcGroup['participatingOrgVdcs']) == 1 and
                                             dcGroup['participatingOrgVdcs'][0][
                                                 'vdcRef']['name'] == sourceOrgVDCName + '-v2t']
                                # Removing dc groups created for isolated conflicting networks
                                filteredVDCGroups = list(filter(lambda group: not any([
                                    True if networkName in group['name'] else False for networkName in
                                    [ntw['name'] for ntw in conflictNetworks]]),
                                                                vdcGroups))
                                # If non-shared dc-group is present use that else create a new dc group
                                if filteredVDCGroups:
                                    dcGroupId = filteredVDCGroups[0]['id']
                                else:
                                    dcGroupName = sourceOrgVDCName + '-Group-' + network['name']
                                    dcGroupId = self.createDCgroup(dcGroupName)
                                ownerIds.update({targetNetwork['id']: dcGroupId})
                                self.rollback.apiData['OrgVDCGroupID'] = ownerIds

            # Create datacenter groups if DFW is not configured but shared nws are present
            elif [network for network in orgVdcNetworks if network['shared']]:
                logger.info('Org VDC group is getting created for shared networks')

                # Fetching name of all the conflicting networks
                if conflictNetworks:
                    conflictingNetworksName = [network['name'] for network in conflictNetworks]

                # Creating DC Group for routed shared networks
                for targetNetwork in targetOrgVDCNetworks:
                    for network in orgVdcNetworks:
                        if targetNetwork['name'] == network['name'] + '-v2t':
                            if network["networkType"] == "NAT_ROUTED" and \
                                    network['shared'] and \
                                    targetNetwork['connection']['routerRef']['id'] not in \
                                    self.rollback.apiData.get('OrgVDCGroupID', {}):
                                dcGroupName = sourceOrgVDCName + '-Group-' + network['connection']['routerRef']['name']
                                dcGroupId = self.createDCgroup(dcGroupName,
                                                               sharedGroup=True,
                                                               orgVdcIdList=orgVDCIDList)
                                ownerIds.update({
                                    targetNetwork['connection']['routerRef']['id']: dcGroupId
                                })
                                self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                                # As this dc group is shared adding this to all object id's
                                for vcdObj in vcdObjList:
                                    dcGroupMapping = vcdObj.rollback.apiData.get('OrgVDCGroupID', {})
                                    dcGroupMapping.update({targetNetwork['connection']['routerRef']['id']: dcGroupId})
                                    vcdObj.rollback.apiData['OrgVDCGroupID'] = dcGroupMapping
                            break

                # Creating dc group for isolated shared conflicting networks
                if conflictNetworks:
                    for targetNetwork in targetOrgVDCNetworks:
                        for network in conflictNetworks:
                            if targetNetwork['name'] == network['name'] + '-v2t':
                                if targetNetwork['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}) and \
                                        network['shared']:
                                    dcGroupName = sourceOrgVDCName + '-Group-' + network['name']
                                    dcGroupId = self.createDCgroup(dcGroupName, sharedGroup=True,
                                                                   orgVdcIdList=orgVDCIDList)
                                    ownerIds.update({targetNetwork['id']: dcGroupId})
                                    self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                                    # As this dc group is shared adding this to all object id's
                                    for vcdObj in vcdObjList:
                                        dcGroupMapping = vcdObj.rollback.apiData.get('OrgVDCGroupID', {})
                                        dcGroupMapping.update(
                                            {targetNetwork['id']: self.rollback.apiData['OrgVDCGroupID'][
                                                targetNetwork['id']]})
                                        vcdObj.rollback.apiData['OrgVDCGroupID'] = dcGroupMapping
                                break

            # Handling corner case for shared isolated networks with no conflicts
            if [network for network in orgVdcNetworks if network['shared']]:
                orgId = self.rollback.apiData['Organization']['@id']
                targetOrgVDCNameList = [vcdObj.vdcName + "-v2t" for vcdObj in vcdObjList]

                for targetNetwork in targetOrgVDCNetworks:
                    # Finding all vdc groups linked to the org vdc's to be parallely migrated
                    vdcGroups = [dcGroup for dcGroup in self.getOrgVDCGroup() if
                                 dcGroup['orgId'] == orgId and [vdc for vdc in dcGroup['participatingOrgVdcs'] if
                                                                vdc['vdcRef']['name'] in targetOrgVDCNameList]]

                    # Removing dc groups created for isolated networks
                    filteredVDCGroups = list(filter(lambda group: not any([
                        True if networkName in group['name'] else False for networkName in conflictingNetworksName]),
                                                    vdcGroups))

                    # Finding filtered shared dc groups
                    filteredSharedVDCGroups = [dcGroup for dcGroup in filteredVDCGroups if
                                               len(dcGroup['participatingOrgVdcs']) == len(vcdObjList)]

                    for network in orgVdcNetworks:
                        if targetNetwork['name'] == network['name'] + '-v2t':
                            if network["networkType"] == "ISOLATED" and network["shared"] and \
                                    targetNetwork['id'] not in self.rollback.apiData.get('OrgVDCGroupID', {}):
                                dcGroupName = sourceOrgVDCName + '-Group-' + network['name']
                                # If shared dc group id is present use that
                                if filteredSharedVDCGroups:
                                    dcGroupId = filteredSharedVDCGroups[0]['id']
                                # Else create a new shared dc group for this network
                                else:
                                    dcGroupId = self.createDCgroup(dcGroupName, sharedGroup=True,
                                                                   orgVdcIdList=orgVDCIDList)
                                ownerIds.update({targetNetwork['id']: dcGroupId})
                                self.rollback.apiData['OrgVDCGroupID'] = ownerIds
                                # As this dc group is shared adding this to all object id's
                                for vcdObj in vcdObjList:
                                    dcGroupMapping = vcdObj.rollback.apiData.get('OrgVDCGroupID', {})
                                    dcGroupMapping.update(
                                        {targetNetwork['id']: self.rollback.apiData['OrgVDCGroupID'][
                                            targetNetwork['id']]})
                                    vcdObj.rollback.apiData['OrgVDCGroupID'] = dcGroupMapping
                            break
        except Exception:
            raise
        finally:
            try:
                # Saving metadata for all org vdc's
                for vcdObj in vcdObjList:
                    # Check for current class object
                    if self is not vcdObj:
                        vcdObj.saveMetadataInOrgVdc()
            finally:
                try:
                    # Releasing the lock
                    self.lock.release()
                    logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
                except RuntimeError:
                    pass

    @description('Enable DFW in Orgvdc group')
    @remediate
    def enableDFWinOrgvdcGroup(self, rollback=False):
        """
        Description :   Enable DFW in Orgvdc group
        Parameters  :   rollback- True to disable DFW in ORG VDC group
        """
        try:
            # Acquire lock as dc groups can be common in different org vdc's
            self.lock.acquire(blocking=True)

            # Check if services configuration or network switchover was performed or not
            if rollback and not self.rollback.metadata.get("configureTargetVDC", {}).get("enableDFWinOrgvdcGroup"):
                return
            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            # Fetch DFW rules from source org vdc
            allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
            orgvDCgroupIds = self.rollback.apiData['OrgVDCGroupID'].values() if self.rollback.apiData.get('OrgVDCGroupID') else []
            # Enable DFW only if DFW was enabled and configured on source org vdc
            if allLayer3Rules:
                for orgvDCgroupId in orgvDCgroupIds:
                    if rollback:
                        url = '{}{}{}/default'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.GET_VDC_GROUP_BY_ID.format(orgvDCgroupId),
                                              vcdConstants.ENABLE_DFW_POLICY)
                        logger.debug('DFW is getting disabled in Org VDC group id: {}'.format(orgvDCgroupId))
                        payloadDict = {"id": "default", "name": "Default", "enabled": False}
                    else:
                        url = '{}{}{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                              vcdConstants.GET_VDC_GROUP_BY_ID.format(orgvDCgroupId),
                                              vcdConstants.ENABLE_DFW_POLICY)
                        logger.debug('DFW is getting enabled in Org VDC group id: {}'.format(orgvDCgroupId))
                        payloadDict = {"enabled": True, "defaultPolicy": {"name": "defaultPolicy Allow", "enabled": True}}
                    payloadData = json.dumps(payloadDict)
                    # setting the content-type as per the api requirement
                    self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                    response = self.restClientObj.put(url, self.headers, data=payloadData)
                    if response.status_code == requests.codes.accepted:
                        taskUrl = response.headers['Location']
                        header = {'Authorization': self.headers['Authorization'],
                                  'Accept': vcdConstants.GENERAL_JSON_ACCEPT_HEADER}
                        taskResponse = self.restClientObj.get(url=taskUrl, headers=header)
                        responseDict = taskResponse.json()
                        self._checkTaskStatus(taskUrl=taskUrl)
                        logger.debug("DFW is enabled successfully on VDC group id: {}".format(orgvDCgroupId))
                    else:
                        errorDict = response.json()
                        raise Exception("Failed to enable DFW '{}' ".format(errorDict['message']))
                if not rollback:
                    self.deleteDfwRulesAllDcGroups()
                    self.configureDfwDefaultRule(sourceOrgVDCId)

        except Exception:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @description('Increasing/Decreasing the scope of Edge gateways')
    @remediate
    def increaseScopeOfEdgegateways(self, rollback=False):
        """
        Description: Increasing the scope of Edge gateways to VDC group
        parameter: rollback- True to decrease the scope of edgegateway from NSX-T ORG VDC
        """
        try:
            # Check if scope of edge gateways was changed or not
            if rollback and not self.rollback.metadata.get("configureTargetVDC", {}).get("increaseScopeOfEdgegateways"):
                return

            edgeGatewayList = self.rollback.apiData['targetEdgeGateway']
            if not edgeGatewayList:
                return

            sourceOrgVDCId = self.rollback.apiData['sourceOrgVDC']['@id']
            allLayer3Rules = self.getDistributedFirewallConfig(sourceOrgVDCId)
            if allLayer3Rules or [network for network in self.retrieveNetworkListFromMetadata(
                    sourceOrgVDCId, orgVDCType='source') if network['shared']]:
                if rollback:
                    logger.info("Rollback: Decreasing scope of edge gateways")
                else:
                    logger.info('Increasing scope of edge gateways')
                ownerRefIDs = self.rollback.apiData.get('OrgVDCGroupID', {})
                targetOrgVdcId = self.rollback.apiData['targetOrgVDC']['@id']
                for edgeGateway in edgeGatewayList:
                    if rollback:
                        logger.debug('Decreasing the scope of Edge gateway - {}'.format(edgeGateway['name']))
                    else:
                        logger.debug('Increasing the scope of Edge gateway - {}'.format(edgeGateway['name']))
                    # url to update external network properties
                    url = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                           vcdConstants.ALL_EDGE_GATEWAYS, edgeGateway['id'])
                    header = {'Authorization': self.headers['Authorization'],
                              'Accept': vcdConstants.OPEN_API_CONTENT_TYPE+';version='+self.version}
                    response = self.restClientObj.get(url, header)
                    if response.status_code == requests.codes.ok:
                        responseDict = response.json()
                        if rollback:
                            # changing the owner reference from org VDC to org VDC group
                            responseDict['ownerRef'] = {'id': targetOrgVdcId}
                        else:
                            ownerRefID = ownerRefIDs[edgeGateway['id']] if ownerRefIDs.get(edgeGateway['id']) else targetOrgVdcId
                            # changing the owner reference from org VDC to org VDC group
                            responseDict['ownerRef'] = {'id': ownerRefID}
                        payloadData = json.dumps(responseDict)
                        self.headers['Content-Type'] = vcdConstants.OPEN_API_CONTENT_TYPE
                        # put call to increase the scope of the edgegateway
                        response = self.restClientObj.put(url, self.headers, data=payloadData)
                        if response.status_code == requests.codes.accepted:
                            # successful creation of firewall group
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl, returnOutput=False)
                            logger.debug('Successfully changed the scope of Edge gateway - {}'.format(edgeGateway['name']))
                        else:
                            errorResponse = response.json()
                            # failure in increase scope of the network
                            raise Exception('Failed to increase scope of the EdgeGateway {} - {}'.format(responseDict['name'], errorResponse['message']))
                    else:
                        responseDict = response.json()
                        raise Exception('Failed to retrieve Edgewateway- {}'.format(responseDict['message']))

        except Exception:
            raise

    @isSessionExpired
    def deleteOrgVDCGroup(self):
        """
        Description: Deleting the ORG VDC group as part of rollback
        """
        try:
            # Taking thread lock as one org vdc will delete groups first
            self.lock.acquire(blocking=True)
            # Check if org vdc groups were created or not
            if not self.rollback.metadata.get("prepareTargetVDC", {}).get("createOrgvDCGroup"):
                return

            ownerRefIDs = self.rollback.apiData.get('OrgVDCGroupID')
            if ownerRefIDs:
                logger.info("Rollback: Deleting Data Center Groups")
                vdcGroupsIds = [group['id'] for group in self.getOrgVDCGroup()]

                for ownerRefID in set(ownerRefIDs.values()):
                    if ownerRefID in vdcGroupsIds:
                        # open api to create Org vDC group
                        url = '{}{}/{}'.format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.VDC_GROUPS, ownerRefID)
                        response = self.restClientObj.delete(url, self.headers)
                        if response.status_code == requests.codes.accepted:
                            taskUrl = response.headers['Location']
                            self._checkTaskStatus(taskUrl=taskUrl)
                        else:
                            response = response.json()
                            raise Exception("Failed to delete ORG VDC group from target - {}".format(response['message']))
        except Exception:
            raise
        finally:
            try:
                # Releasing the lock
                self.lock.release()
                logger.debug("Lock released by thread - '{}'".format(threading.currentThread().getName()))
            except RuntimeError:
                pass

    @isSessionExpired
    def getIPAssociatedUsedByVM(self, networkName, directNetworkId, externalNetworkName, externalNetworkSubnets, vdcIDList):
        """
        Description: Method to find all the IPS to be migrated used by vm connected to shared direct networks and save that to metadata
        Parameters:  networkName - Name of shared service direct network
                     vdcIDList - list of id of source org vdc (LIST)
        """
        try:
            ipList = list()

            vAppList = list()
            # Fetching vapps from all the org vdc's partaking in the migration
            for vdcId in vdcIDList:
                vAppList += self.getOrgVDCvAppsList(orgVDCId=vdcId)
            for vApp in vAppList:
                # Check vCD session
                getSession(self)
                # get api call to retrieve the vapp details
                response = self.restClientObj.get(vApp['@href'], self.headers)
                if response.status_code == requests.codes.ok:
                    responseDict = self.vcdUtils.parseXml(response.content, process_namespaces=False, attr_prefix='')
                    vAppData = responseDict.get('VApp', {})
                    # checking if the vapp has vms
                    vappRoutedListConnectedToDirectNet = list()
                    if vAppData and vAppData.get('Children'):
                        networkConfig = listify(vAppData.get('NetworkConfigSection', {}).get('NetworkConfig', []))
                        for network in networkConfig:
                            if network.get('Configuration', {}).get('ParentNetwork', {}).get('id') == directNetworkId:
                                if network['Configuration'].get('RouterInfo', {}).get('ExternalIp'):
                                    ipList.append(network['Configuration']['RouterInfo']['ExternalIp'])
                                    vappRoutedListConnectedToDirectNet.append(network['networkName'])
                        vmList = vAppData['Children']['Vm'] if isinstance(
                            vAppData['Children']['Vm'],
                            list) else [
                            vAppData['Children']['Vm']]
                        # iterating over vms in the vapp
                        for vm in vmList:
                            if vm.get('NetworkConnectionSection') and \
                                    vm['NetworkConnectionSection'].get('NetworkConnection'):
                                vmNetworkSpec = vm['NetworkConnectionSection']['NetworkConnection'] \
                                    if isinstance(vm['NetworkConnectionSection']['NetworkConnection'], list) \
                                    else [vm['NetworkConnectionSection']['NetworkConnection']]
                                for network in vmNetworkSpec:
                                    if network['network'] == networkName and network['IpAddressAllocationMode'] == 'POOL':
                                        ipList.append(network['IpAddress'])
                                    elif network['network'] in vappRoutedListConnectedToDirectNet:
                                        if network.get('ExternalIpAddress') and any([ipaddress.ip_address(network['ExternalIpAddress']) in subnet for subnet in externalNetworkSubnets]):
                                            ipList.append(network['ExternalIpAddress'])
                else:
                    raise Exception("Failed to fetch vApp details")
                # Saving these IP's in metadata
                directNetworkIPS = self.rollback.apiData.get("directNetworkIP", {})
                if externalNetworkName in directNetworkIPS:
                    directNetworkIPS[externalNetworkName] = list(set(directNetworkIPS[externalNetworkName] + ipList))
                else:
                    directNetworkIPS[externalNetworkName] = list(set(ipList))
                self.rollback.apiData["directNetworkIP"] = directNetworkIPS
            return ipList
        except:
            raise
        finally:
            self.saveMetadataInOrgVdc()

    @isSessionExpired
    def getIpUsedByEdgeGateway(self):
        """
        Description: Method to find all the IPS to be migrated used by edge gateways and save that to metadata
        """
        try:
            data = self.rollback.apiData
            directNetworkIPS = data.get("segmentBackedNetworkIP", {})
            for edgeGateway in data.get('sourceEdgeGateway', []):
                ipAddressList = list()
                for externalNetworkName in data['isT1Connected'].get(edgeGateway['name'], {}):
                    externalNetworkId = [uplink['uplinkId'] for uplink in edgeGateway['edgeGatewayUplinks'] if externalNetworkName == uplink['uplinkName']][0]
                    url = "{}{}/{}/usedIpAddresses".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EXTERNAL_NETWORKS, externalNetworkId)
                    headers = {'Authorization': self.headers['Authorization'],
                               'Accept': vcdConstants.OPEN_API_CONTENT_TYPE}
                    edgeGatewayConnectedToExtNetList = self.getPaginatedResults('External Network IP usage', url, headers)
                    ipAddressList = ipAddressList + [edge['ipAddress'] for edge in edgeGatewayConnectedToExtNetList if edge['entityId'] == edgeGateway['id']]
                    if externalNetworkName in directNetworkIPS:
                        directNetworkIPS[externalNetworkName] = list(set(directNetworkIPS[externalNetworkName] + ipAddressList))
                    else:
                        directNetworkIPS[externalNetworkName] = list(set(ipAddressList))

            data["segmentBackedNetworkIP"] = directNetworkIPS
        except:
            raise
        finally:
            self.saveMetadataInOrgVdc()

    @isSessionExpired
    def importedNetworkPayload(self, parentNetworkId, orgvdcNetwork, inputDict, nsxObj):
        """
        Description: THis method is used to create payload for dedicated direct network(shared/non-shared)
        return: payload data - payload data for creating a dedicated direct network(shared/non-shared)
        """
        # Getting source external network details
        extNetUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                     vcdConstants.ALL_EXTERNAL_NETWORKS,
                                     parentNetworkId['id'])
        extNetResponse = self.restClientObj.get(extNetUrl, self.headers)
        if extNetResponse.status_code == requests.codes.ok:
            extNetResponseDict = extNetResponse.json()
        else:
            raise Exception('Failed to get external network {} details with error - {}'.format(
                parentNetworkId['name'], extNetResponse["message"]))

        externalList = extNetResponseDict['networkBackings']

        backingid = [values['backingId'] for values in externalList['values']]
        url = '{}{}'.format(vcdConstants.XML_API_URL.format(self.ipAddress),
                            vcdConstants.GET_PORTGROUP_VLAN_ID.format(backingid[0]))
        acceptHeader = vcdConstants.GENERAL_JSON_ACCEPT_HEADER.format(self.version)
        headers = {'Authorization': self.headers['Authorization'], 'Accept': acceptHeader}
        # get api call to retrieve the networks with external network id
        response = self.restClientObj.get(url, headers)
        responseDict = response.json()
        if response.status_code == requests.codes.ok:
            if responseDict['record']:
                for record in responseDict['record']:
                    vlanId = record['vlanId']
                segmetId, segmentName = nsxObj.createLogicalSegments(orgvdcNetwork, inputDict["VCloudDirector"][
                    "ImportedNetworkTransportZone"], vlanId)
            ipRanges = [
                {
                    'startAddress': ipRange['startAddress'],
                    'endAddress': ipRange['endAddress'],
                }
                for ipRange in orgvdcNetwork['subnets']['values'][0]['ipRanges']['values']
            ]
            payload = {
                'name': orgvdcNetwork['name'] + '-v2t',
                'description': orgvdcNetwork['description'] if orgvdcNetwork.get('description') else '',
                'networkType': 'OPAQUE',
                "subnets": {
                    "values": [{
                        "gateway": orgvdcNetwork['subnets']['values'][0]['gateway'],
                        "prefixLength": orgvdcNetwork['subnets']['values'][0]['prefixLength'],
                        "dnsSuffix": orgvdcNetwork['subnets']['values'][0]['dnsSuffix'],
                        "dnsServer1": orgvdcNetwork['subnets']['values'][0]['dnsServer1'],
                        "dnsServer2": orgvdcNetwork['subnets']['values'][0]['dnsServer2'],
                        "ipRanges": {
                            "values": ipRanges
                        },
                    }]
                },
                'backingNetworkId': segmetId
            }
        else:
            raise Exception('Failed to get external network {} vlan ID'.format(
                parentNetworkId['name'], responseDict['message']))
        return segmentName, payload

    @isSessionExpired
    def extendedParentNetworkPayload(self, orgvdcNetwork, Shared):
        """
        Description: THis method is used to create payload for service direct network in legacy mode
        return: payload data - payload data for creating a service direct network in legacy mode
        """
        payLoad = {
                                'name': orgvdcNetwork['name'] + '-v2t',
                                'description': orgvdcNetwork['description'] if orgvdcNetwork.get('description') else '',
                                'networkType': orgvdcNetwork['networkType'],
                                'parentNetworkId': orgvdcNetwork['parentNetworkId'],
                                'shared': Shared
                            }
        return payLoad

    @isSessionExpired
    def v2tBackedNetworkPayload(self, parentNetworkId, orgvdcNetwork, Shared):
        """
        Description: THis method is used to create payload for service direct network(shared/non-shared) in non-legacy mode
        return: payload data - payload data for creating a service direct network
        """

        # Payload for shared direct network / service network use case
        targetExternalNetworkurl = "{}{}?{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress),
                                                    vcdConstants.ALL_EXTERNAL_NETWORKS,
                                                    vcdConstants.EXTERNAL_NETWORK_FILTER.format(
                                                        parentNetworkId['name'] + '-v2t'))
        # GET call to fetch the External Network details using its name
        response = self.restClientObj.get(targetExternalNetworkurl, headers=self.headers)

        if response.status_code == requests.codes.ok:
            responseDict = response.json()
            extNet = responseDict.get("values")[0]
            # Finding segment backed ext net for shared direct network
            if [backing for backing in extNet['networkBackings']['values'] if
                 backing['backingTypeValue'] == 'IMPORTED_T_LOGICAL_SWITCH']:
                payload = {
                    'name': orgvdcNetwork['name'] + '-v2t',
                    'description': orgvdcNetwork['description'] if orgvdcNetwork.get(
                        'description') else '',
                    'networkType': orgvdcNetwork['networkType'],
                    'parentNetworkId': {'name': extNet['name'],
                                        'id': extNet['id']},
                    'shared': Shared
                }
        else:
            raise (
                f"NSXT segment backed external network {parentNetworkId['name'] + '-v2t'} is not present, and it is "
                f"required for this direct shared network - {orgvdcNetwork['name']}")
        return payload

    @isSessionExpired
    def createDirectNetworkPayload(self, inputDict, nsxObj, orgvdcNetwork, parentNetworkId):
        """
        Description: THis method is used to create payload for direct network and imported network
        return: payload data - payload data for creating a network
        """
        try:
            segmentName = None
            payloadDict = dict()
            # url to retrieve the networks with external network id
            url = "{}{}{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_ORG_VDC_NETWORKS,
                                  vcdConstants.QUERY_EXTERNAL_NETWORK.format(parentNetworkId['id']))
            # get api call to retrieve the networks with external network id
            response = self.restClientObj.get(url, self.headers)
            responseDict = response.json()
            if response.status_code == requests.codes.ok:
                # Implementation for Direct Network connected to VXLAN backed External Network irrespective of the dedicated/non-dedicated or shared/non-shared status. 
                extNetUrl = "{}{}/{}".format(vcdConstants.OPEN_API_URL.format(self.ipAddress), vcdConstants.ALL_EXTERNAL_NETWORKS,
                                  parentNetworkId['id'])
                extNetResponse = self.restClientObj.get(extNetUrl, self.headers)
                extNetResponseDict =extNetResponse.json()
                if extNetResponse.status_code == requests.codes.ok:
                    if extNetResponseDict['networkBackings']['values'][0]["name"][:7] == "vxw-dvs":
                        payloadDict = self.v2tBackedNetworkPayload(parentNetworkId, orgvdcNetwork, Shared=orgvdcNetwork['shared'])
                        payloadData = json.dumps(payloadDict)
                        return segmentName, payloadData
                else:
                    raise Exception('Failed to get external network {} details with error - {}'.format(
                            parentNetworkId['name'], extNetResponseDict["message"]))
                if int(responseDict['resultTotal']) > 1:
                    if self.orgVdcInput.get('LegacyDirectNetwork', False):
                        # Service direct network legacy implementation
                        payloadDict = self.extendedParentNetworkPayload(orgvdcNetwork, Shared=orgvdcNetwork['shared'])
                    else:
                        # Service direct network default implementation
                        payloadDict = self.v2tBackedNetworkPayload(parentNetworkId, orgvdcNetwork, Shared=orgvdcNetwork['shared'])
                else:
                    # Dedicated direct network implementation
                    segmentName, payloadDict = self.importedNetworkPayload(parentNetworkId, orgvdcNetwork, inputDict, nsxObj)
            else:
                raise Exception('Failed to get external network {}: {}'.format(
                    parentNetworkId['name'], responseDict['message']))
            payloadData = json.dumps(payloadDict)
            return segmentName, payloadData
        except Exception:
            raise

