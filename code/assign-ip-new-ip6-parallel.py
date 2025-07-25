#!/usr/bin/env python3
# -----------------------------------------------------------
#// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#// SPDX-License-Identifier: MIT-0
# This code demonstrates how to use a sidecar|initContainer to automate the IP allocation
# on the AWS EKS worker node ENIs for multus pods
# author: Raghvendra Singh
# -----------------------------------------------------------
import requests
import boto3, json
import sys, datetime
import netaddr
from netaddr import *
from requests.packages.urllib3 import Retry
import subprocess,copy,time
from collections import defaultdict
from multiprocessing import Process

## Logs are printed with timestamp as an output for kubectl logs of this container 
def tprint(var):
    print (datetime.datetime.now(),"-",var)
    
# This function, Finds the ENIs, for a list of given ipv6 secondary IPs 
# If an ENI is found, then the IPs are unassigned from that ENI 
def release_ipv6(ip6List,client):
    tprint("Going to release ip6List: " + str(ip6List))     
    
    response = client.describe_network_interfaces(
        Filters=[
            {
                'Name': 'ipv6-addresses.ipv6-address',
                'Values': ip6List
            },
        ],
    )
    if response['NetworkInterfaces'] == []:
        tprint("ENI of ipv6 not attached yet, no need to release")
    else:
        for j in response['NetworkInterfaces']:
            network_interface_id = j['NetworkInterfaceId']
            response = client.unassign_ipv6_addresses(
                Ipv6Addresses=ip6List,
                NetworkInterfaceId = network_interface_id
            )
    tprint("Finished releasing ip6List: " + str(ip6List))     

## This function, assigns/moves the list of secondary ipv4 addresses to the given ENI  
#  The AllowReassignment flag = True , enables the force move of secondary ip addresses f=if they are assigned to soem other ENI  
#  If there are any error, Exception is thrown which is handled in the main block.    
def assign_ip_to_nic(ipList,network_interface_id,client):  
    tprint("Going to reassign iplist: " + str(ipList) + " to ENI:" +network_interface_id )    

    response = client.assign_private_ip_addresses(
        AllowReassignment=True,
        NetworkInterfaceId=network_interface_id,
        PrivateIpAddresses = ipList    
        )

## This function, assigns the list of secondary ipv6 addresses to the given ENI  
#  If there are any error, Exception is thrown which is handled in the main block          
def assign_ip6_to_nic(ip6List,network_interface_id,client):  
    tprint("Going to assign ip6List: " + str(ip6List) + " to ENI:" +network_interface_id )     
    response = client.assign_ipv6_addresses(
        Ipv6Addresses=ip6List,
        NetworkInterfaceId=network_interface_id,
        )
## This function gets the metadata token
def get_metadata_token():
    token_url="http://169.254.169.254/latest/api/token"
    headers = {'X-aws-ec2-metadata-token-ttl-seconds': '21600'}
    r= requests.put(token_url,headers=headers,timeout=(2, 5))
    return r.text

def get_instance_id():
    instance_identity_url = "http://169.254.169.254/latest/dynamic/instance-identity/document"
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=0.3)
    metadata_adapter = requests.adapters.HTTPAdapter(max_retries=retries)
    session.mount("http://169.254.169.254/", metadata_adapter)
    try:
        r = requests.get(instance_identity_url, timeout=(2, 5))
        code=r.status_code
        if code == 401: ###This node has IMDSv2 enabled, hence unauthorzied, we need to get token first and use the token
            tprint("node has IMDSv2 enabled!! Fetching Token first")
            token=get_metadata_token()
            headers = {'X-aws-ec2-metadata-token': token}
            r = requests.get(instance_identity_url, headers=headers, timeout=(2, 5))
            code=r.status_code
        if code == 200:
            response_json = r.json()
            instanceid = response_json.get("instanceId")
            region = response_json.get("region")
            return(instanceid,region)
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as err:
        tprint("Exception: Connection to AWS EC2 Metadata timed out: " + str(err.__class__.__name__))
        tprint("Exception: Is this an EC2 instance? Is the AWS metadata endpoint blocked? (http://169.254.169.254/)")
        raise
    except Exception as e:
        tprint("Execption: caught exception " + str(e.__class__.__name__))
        raise             
          
## This function runs the shell command and returns the command output
def shell_run_cmd(cmd,retCode=0):
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,encoding="utf-8")
    stdout, stderr = p.communicate()
    retCode = p.returncode
    return stdout

## This function fetches the subnet CIDR for the given subnet
# All exceptions handled in the main function
def get_subnet_cidr(ec2_client,subnetId):
    CidrBlock, Ipv6CidrBlock = "", ""
    response = ec2_client.describe_subnets(
        SubnetIds=[
            subnetId,
        ],    
    )
    for i in response['Subnets']:
        if "Ipv6CidrBlockAssociationSet" in i:
            for ipv6_cidr_block in i['Ipv6CidrBlockAssociationSet']:
                Ipv6CidrBlock = ipv6_cidr_block['Ipv6CidrBlock']
        if "CidrBlock" in i and not any('CidrBlock' in j for j in i.get('Ipv6CidrBlockAssociationSet', [])):
            CidrBlock = i['CidrBlock']
    return CidrBlock, Ipv6CidrBlock


## This function collects the details of each ENI attached to the worker node and corrresponding subnet IDs
# later it fetches the subnetCIDR for the given subnet ID and stores them in a Dictionary where key is the CidrBlock and value is the ENI id
# All exceptions handled in the main function
def get_instanceDetails(ec2_client,instance_id,macENIMapping):
    ##Collect macAddresses from instance
    response = ec2_client.describe_instances(
        InstanceIds= [ instance_id ]
    )
    for r in response['Reservations']:
      for i in r['Instances']:
        for j in i["NetworkInterfaces"]:
            ##skip eth0 interface addition in the decideData collection
            if j['Attachment']['DeviceIndex'] !=0:
                mac=j['MacAddress']
                macENIMapping[mac] = {"NetworkInterfaceId": j["NetworkInterfaceId"]}
                cidrBlock, ipv6CidrBlock = get_subnet_cidr(ec2_client,j["SubnetId"])
                if len(cidrBlock) > 0:
                    macENIMapping[mac]["v4cidr"]= cidrBlock
                    tprint("Node ENIC: "+ j["NetworkInterfaceId"] + " Ipv4 cidr: " + cidrBlock  + " subnetID: " + j["SubnetId"] + ' mac: ' + mac) 
                if len(ipv6CidrBlock) > 0:
                    macENIMapping[mac]["v6cidr"]= ipv6CidrBlock
                    tprint("Node ENIC: "+ j["NetworkInterfaceId"] + " Ipv6 cidr: " + ipv6CidrBlock  + " subnetID: " + j["SubnetId"]+ ' mac: ' + mac) 
            else:
                tprint("skipping eth0 (device index 0 ) ENI: " + j["NetworkInterfaceId"] )

def perfromCidrMatch(input,target):
    match=""
    inputNetwork = IPNetwork(input)
    targetNetwork = IPNetwork(target)
    if inputNetwork == targetNetwork:
        match="EXACT"
    elif inputNetwork in targetNetwork:
        match="SUBSET"
    return match
## if provided cidr matches with subnet cidrs attached to worker node, then eniId is returned else return ""
def getInstanceENIIdFromCIDR(cidr,instanceNetworkingData):
    eniId=""
    subnet = IPNetwork(cidr)
    version = subnet.version
    for key in instanceNetworkingData:
        instanceSubnetcidr=""
        if version == 6:
            if "v6cidr" in instanceNetworkingData[key]:
                instanceSubnetcidr = instanceNetworkingData[key]["v6cidr"]
        else:
            if "v4cidr" in instanceNetworkingData[key]:
                instanceSubnetcidr = instanceNetworkingData[key]["v4cidr"]
        if instanceSubnetcidr:
            match=perfromCidrMatch(cidr,instanceSubnetcidr)
            if match:
                eniId = instanceNetworkingData[key]["NetworkInterfaceId"]
                tprint(match + " CIDR Match found!!!  cidr: " + cidr + " matches with attached worker node subnet cidrs. will be processed for secondary ip assignment on ENIID " + eniId)
            if eniId:
                break
    
    return eniId

def ipAddressAssignmentHandling(cmd, currIPList, instanceNetworkingData, ec2ClientArr):
    ipmap = defaultdict(list)
    ip6map = defaultdict(list)
    noChange=True
    retCode=0
    try:
        output = shell_run_cmd(cmd,retCode)
        if retCode == 0 :
            interfaceList = output.splitlines()
            newIPList = []
            for interface in interfaceList:
                if interface.strip(): # remove leading and trailing whitespace characters (spaces, tabs, newlines, etc.) from a string
                    ## Ex: aa:bb:cc:dd:ee:ff=192.168.192.17/30
                    data=interface.split('=')
                    mac=data[0]
                    if len(data) > 1:
                        ipList=[]
                        if data[1]: 
                            ipList=data[1].split(' ') 
                        if len(ipList) > 0 :
                            newIPList.extend(ipList)
                            #IPVLAN case when mac address matches with the instance mac-address
                            if mac in instanceNetworkingData:
                                v4Cidr=instanceNetworkingData[mac]['v4cidr'] if  'v4cidr' in instanceNetworkingData[mac] else ""
                                v6Cidr=instanceNetworkingData[mac]['v6cidr'] if  'v6cidr' in instanceNetworkingData[mac] else ""
                                eniId=instanceNetworkingData[mac]['NetworkInterfaceId']
                                for ipaddress in ipList:
                                    if ipaddress not in currIPList:
                                        ip = IPNetwork(ipaddress)
                                        cidr = str(ip.cidr) 
                                        version = ip.version
                                        match=""
                                        if version == 4:
                                            if v4Cidr:
                                                match=perfromCidrMatch(cidr,v4Cidr)
                                                if match : #if cidr on the pod matches or is subset of the subnet cidrs attached to worker node
                                                    ipmap[eniId].append(str(ip.ip))
                                                    tprint(match+" Match found!!! pod ip address: " + ipaddress + " with cidr: " + cidr + " matches with attached worker node subnet cidrs. will be processed for secondary ip assignment on ENIID " + eniId)
                                        else:
                                            if v6Cidr:
                                                match=perfromCidrMatch(cidr,v6Cidr)
                                                if match : #if cidr on the pod matches or is subset of the subnet cidrs attached to worker node                                            ip6map[eniId].append(str(ip.ip))
                                                    ip6map[eniId].append(str(ip.ip))
                                                    tprint(match+" Match found!!! pod ip address: " + ipaddress + " with cidr: " + cidr + " matches with attached worker node subnet cidrs. will be processed for secondary ip assignment on ENIID " + eniId)
                                        if not match:
                                                tprint("No Match found!!! pod ip address: " + ipaddress + " with cidr: " + cidr +  " doesnt match with attached subnet cidrs.Skipping secondary ip assignment!!!")
                                        noChange=False
                            else: #macvlan or some other corner/special case when POD mac doesnt match the instance mac. Check if subnet cidr of the applictaion/pod matches with instance subnet CIDR.
                                # such cases we  support single IP per cidr (cant have multiple ENI from same subnet) 
                                for ipaddress in ipList:
                                    if ipaddress not in currIPList:
                                        ip = IPNetwork(ipaddress)
                                        cidr = str(ip.cidr) 
                                        eniId=getInstanceENIIdFromCIDR(cidr,instanceNetworkingData) 
                                        if eniId: 
                                            if  netaddr.valid_ipv4(str(ip.ip)):
                                                ipmap[eniId].append(str(ip.ip))
                                            else :
                                                ip6map[eniId].append(str(ip.ip))
                                            tprint("Special case. Match found!!! MAC " + mac + " didnt match with worker mac addresses, but pod ip address: " + ipaddress + " with cidr: " + cidr + " matches with attached worker node subnet cidrs. will be processed for secondary ip assignment on ENIID " + eniId)
                                        else:
                                            tprint("No Match found!!! MAC " + mac + " didnt match with worker mac addresses, and pod ip address: " + ipaddress + " with cidr: " + cidr + " didnt match with any of the worker node subnet cidrs. Skipping secondary ip assignment!!!")
                                        noChange=False
            # if there are changes in the ips (new vs old) then reassign the ipv4 IP addresses asynchronously to save time  (parallel execution)  
            if noChange == False :   
                if len(ipmap) > 0:                        
                    procipv4 = []   
                    for eniId in ipmap:    
                        p = Process(target=assign_ip_to_nic, args=(ipmap[eniId], eniId, ec2ClientArr[eniId]))
                        p.start()
                        procipv4.append(p)                    
                    # wait for  the parallel requests to complete execution and return 
                    for p in procipv4:
                        p.join(2)    
                    tprint ("Finished all IPV4 Assignments: " + str(ipmap.values()))           
                    # if there are changes in the ips (new vs old) then release the ipv6 IP addresses from old ENIs asynchronously to save time  (parallel execution)  
                if len(ip6map) > 0:
                    procipv6 = []                   
                    for  eniId in ip6map:      
                        p = Process(target=release_ipv6, args=(ip6map[eniId],ec2ClientArr[eniId]))
                        p.start()
                        procipv6.append(p) 
                    for p in procipv6:
                        p.join(2)    
                    # if there are changes in the ips (new vs old) then relassignease the ipv6 IP addressess to the worker ENIs asynchronously to save time  (parallel execution)  
                    for  eniId in ip6map:      
                        p = Process(target=assign_ip6_to_nic, args=(ip6map[eniId],eniId,ec2ClientArr[eniId])) 
                        p.start()
                        procipv6.append(p) 
                    for p in procipv6:
                        p.join(2)                           
                    tprint ("Finished all IPv6: " + str(ip6map.values()))       
                # Once all the ipv4 and ipv6 assignments are completed, then copy the newIp list as current List     
                currIPList.clear()
                currIPList.extend(newIPList)    
                tprint("updated currIPList"+ str(currIPList))   

        else:
            tprint ("Error received: " + retCode + " for command: "+ cmd )
    except (Exception) as e:
        tprint ("Exception in function ipAddressAssignmentHandling:" + str(e))  
        raise       

def main():    
    instance_id = None
    currIPList = []
    #ipOnlyCmd = "ip a |grep -v eth0|grep 'scope global' |cut -d ' ' -f 6"
    ipWithMacCmd = "for x in `ls /sys/class/net/ | grep -vE 'eth0|lo'`; do y=`ip a show dev $x | grep -E 'link/ether'|cut -d ' ' -f 6`;z=`ip a show dev $x | grep -E 'scope global'|cut -d ' ' -f 6`; echo ${y}=${z}; done"
    region= None
    instanceNetworkingData = {} # A dictionary with key as MAC address and values are {NetworkInterfaceId, v4Cidr, v4Cidr}.Each EC2 instance will have unique Mac Address 
    initcontainer=False
    ec2ClientArr  =  {} 
    exceptionCtr=0 
    MAXEXCEPTION=60
    if len(sys.argv) > 0 :
        if sys.argv[1] == "initContainers":
            initcontainer=True
    if initcontainer == False:
        tprint("Running as Sidecar container")   

    while (1) :
        retCode=0
        try:
            # at the very first iteration, get the instance ID of the underlying worker & create a temp oto3 client to get instance data attached ENIs and corresponding subnet IP CIDRblocks 
            if not instance_id :
                data = get_instance_id()
                instance_id = data[0]
                region = data[1]
                tprint ("Got InstanceId: " + instance_id + " region: " + region)  
                ec2_client = boto3.client('ec2', region_name=region)
                get_instanceDetails(ec2_client,instance_id,instanceNetworkingData)
                tprint ("Got InstanceNetworkingData: " + str(instanceNetworkingData))
                # For parallel processing and same client cant be used in parallel for multiple API calls, so we are creating a Map/dictionary of ec2 clients for each ENI/MAC attached to the worker. These clients are stored as values against eniID as key
                for mac in instanceNetworkingData:
                    eniId = instanceNetworkingData[mac]["NetworkInterfaceId"]
                    ec2ClientArr[eniId] = boto3.client('ec2', region_name=region)
            #Check and Assigne IP addreses to respective ENIs 
            ipAddressAssignmentHandling(ipWithMacCmd,currIPList,instanceNetworkingData,ec2ClientArr)
            exceptionCtr = 0
            if initcontainer == True :
                tprint ("Started as initcontainer. Exiting after successful execution")  
                exit(0)      
            else:
                time.sleep(0.5)
        # If these are any exceptions in ip assignment to the NICs then catch it using catch all exception and keep trying & logging upto max counter untill the problem is resolved
        except (Exception) as e:
            tprint ("Exception :" + str(e))     
            exceptionCtr = exceptionCtr + 1
            if exceptionCtr > MAXEXCEPTION:
                tprint ("Max Exception count reached. Exiting")
                exit(1)
            tprint ("continuing the handling")

##Main Usage <scriptName> initContainers|sidecar

if __name__ == "__main__":
    main()
