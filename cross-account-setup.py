#!/usr/bin/env python3
import argparse
import typing

import boto3
import botocore.exceptions

if typing.TYPE_CHECKING:
    import mypy_boto3_events

EVENT_BUS_NAME = 'marionette'


def setup_attacker_account(attacker_sess: boto3.Session):
    client = attacker_sess.client('events')
    try:
        client.create_event_bus(Name=EVENT_BUS_NAME)
        client.put_permission(
            EventBusName=EVENT_BUS_NAME,
            Action='events:PutEvents',
            Principal='*',
            StatementId='AllowAll',
        )
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'ResourceAlreadyExistsException':
            print(f"The event bus {EVENT_BUS_NAME} in the attacker account already exists")
        else:
            raise e

    try:
        client.put_rule(
            Name='ec2-on-stopped',
            EventPattern='''{
              "source": ["aws.ec2"],
              "detail-type": ["EC2 Instance State-change Notification"],
              "detail": {
                "state": ["stopped"]
              }
            }''',
            State='ENABLED',
            Description='Matches EC2 state changes to be forwarded to the marionette lambda.',
            EventBusName=EVENT_BUS_NAME,
        )
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'ResourceAlreadyExistsException':
            print(f"The event rule ec2_on_stopped in the attacker account already exists")
        else:
            raise e

    try:
        client.put_rule(
            Name='ec2-run-instances',
            EventPattern='''{
              "source": ["aws.ec2"],
              "detail": {
                "eventSource": ["ec2.amazonaws.com"],
                "eventName": ["RunInstances"]
              }
            }''',
            State='ENABLED',
            Description='Matches EC2 run instances to be forwarded to the marionette lambda.',
            EventBusName=EVENT_BUS_NAME,
        )
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'ResourceAlreadyExistsException':
            print(f"The event rule ec2_run_instances in the attacker account already exists")
        else:
            raise e


def setup_victim_account(victim_profile: boto3.Session, attacker_account_id: str, region: str):
    client = victim_profile.client('events')
    client.put_rule(
        Name='ec2-on-stopped',
        EventBusName='default',
        EventPattern='''{
          "source": ["aws.ec2"],
          "detail-type": ["EC2 Instance State-change Notification"],
          "detail": {
            "state": ["stopped"]
          }
        }''',
        State='ENABLED',
        Description='EC2 Maintenance',
    )

    client.put_targets(
        Rule='ec2-on-stopped',
        EventBusName='default',
        Targets=[
            {
                'Id': '1',
                'Arn': f'arn:aws:events:{region}:{attacker_account_id}:event-bus/{EVENT_BUS_NAME}',
            },
        ]
    )

    client.put_rule(
        Name='ec2-run-instances',
        EventBusName='default',
        EventPattern='''{
          "source": ["aws.ec2"],
          "detail": {      
            "eventSource": ["ec2.amazonaws.com"],
            "eventName": ["RunInstances"]
          }
        }''',
        State='ENABLED',
        Description='EC2 Maintenance',
    )

    client.put_targets(
        Rule='ec2-run-instances',
        EventBusName='default',
        Targets=[
            {
                'Id': '1',
                'Arn': f'arn:aws:events:{region}:{attacker_account_id}:event-bus/{EVENT_BUS_NAME}',
            },
        ]
    )


def main(attacker_profile: str, victim_profile: str, region: str):
    attacker_sess = boto3.Session(profile_name=attacker_profile, region_name=region)
    setup_attacker_account(attacker_sess)

    attacker_account_id = attacker_sess.client('sts').get_caller_identity()['Account']
    victim_profile = boto3.Session(profile_name=victim_profile, region_name=region)
    setup_victim_account(victim_profile, attacker_account_id, region)
    print(f"Accounts setup succesfully, run `AWS_DEFAULT_REGION={region} AWS_PROFILE={attacker_profile} make deploy/infra` to "
          "finish the lambda setup.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Deploy some stuff.')
    parser.add_argument('--attacker-profile', help='Profile of the attacker account.')
    parser.add_argument('--victim-profile', help='Profile of the victim account.')
    parser.add_argument('--region', help='Region to use')

    args = parser.parse_args()
    main(args.attacker_profile, args.victim_profile, args.region)
