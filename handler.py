#!/usr/bin/env python3
####################
# NOTE: This code is from
#    https://github.com/AndrewFarley/AWS-Automated-Daily-Instance-AMI-Snapshots
####################
import boto3
import os
import sys
import traceback
import datetime
import time

# List every region you'd like to scan.  We'll need to update this if AWS adds a region
aws_regions = ['us-east-1','us-east-2','us-west-1','us-west-2',
'ap-east-1','ap-northeast-1','ap-northeast-2','ap-northeast-3',
'ap-south-1','ap-southeast-1','ap-southeast-2','ca-central-1',
'eu-central-1','eu-west-1','eu-west-2','eu-west-3']

# If in serverless.yml we limited to a specific region(s)
if 'LIMIT_TO_REGIONS' in os.environ and len(os.getenv('LIMIT_TO_REGIONS')):
    aws_regions = os.getenv('LIMIT_TO_REGIONS').split(',')

# List of the tags on instances we want to look for to backup
tags_to_find = ['backup', 'Backup']

# Default Retention Time (in days)
default_retention_time = 7
if 'DEFAULT_RETENTION_TIME' in os.environ and len(os.getenv('DEFAULT_RETENTION_TIME')):
    default_retention_time = int(os.getenv('DEFAULT_RETENTION_TIME'))

# This is the key we'll set on all AMIs we create, to detect that we are managing them
global_key_to_tag_on = "AWSAutomatedDailySnapshots"
if 'KEY_TO_TAG_ON' in os.environ and len(os.getenv('KEY_TO_TAG_ON')):
    global_key_to_tag_on = str(os.getenv('KEY_TO_TAG_ON'))

dry_run = False
if 'DRY_RUN' in os.environ and (os.getenv('DRY_RUN') == 'true' or os.getenv('DRY_RUN') == 'True'):
    dry_run = True

#####################
# Helper function to backup tagged instances in a region
#####################
def backup_tagged_instances_in_region(ec2):

    print(f"Scanning for instances with tags ({','.join(tags_to_find)})")

    # Get our reservations
    try:
        reservations = ec2.describe_instances(Filters=[{'Name': 'tag-key', 'Values': tags_to_find}])['Reservations']
    except:
        # Don't fatal error on regions that we haven't activated/enabled
        if 'OptInRequired' in str(sys.exc_info()):
            print("Region not activated for this account, skipping...")
            return
        else:
            raise

    # Iterate through reservations and get instances
    instances = []
    for reservation in reservations:
        for instance in reservation['Instances']:
            if instance['State']['Name'] != 'terminated':
                instances.append(instance)

    # Get our instances and iterate through them...
    if len(instances) == 0:
        return
    print(f"Found {len(instances)} instances to backup...")
    for instance in instances:
        print("Instance: {instance['InstanceId']}")

        dict_tags = {}
        for tag in instance['Tags']:
            dict_tags[tag['Key']] = tag.get('Value')

        # Get the name of the instance, if set...
        instance_name = dict_tags.get('Name') if dict_tags.get('Name') else instance['InstanceId']
        print(f"Instance name: {instance_name}")

        # Get days to retain the backups from tags if set...
        retention_days = dict_tags.get('Retention') if dict_tags.get('Retention') else default_retention_time
        print(f'Retention period: {retention_days} days')

        # Catch if we were dry-running this
        if dry_run:
            print("DRY_RUN")
            print(f"InstanceId : {instance['InstanceId']}")
            print(f"Name       : {instance_name}-backup-{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S.%f')}")
        else:
            # Create our AMI
            try:
                image = ec2.create_image(
                    InstanceId=instance['InstanceId'],
                    Name=f"{instance_name}-backup-{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S.%f')}",
                    Description=f"Automatic Backup of {instance_name} from {instance['InstanceId']}",
                    NoReboot=True,
                    DryRun=False
                )
                print(f"AMI: {image['ImageId']}")

                # Tag our AMI appropriately
                delete_fmt = (datetime.date.today() + datetime.timedelta(days=retention_days)).strftime('%m-%d-%Y')
                instance['Tags'].append({'Key': 'DeleteAfter', 'Value': delete_fmt})
                instance['Tags'].append({'Key': 'OriginalInstanceID', 'Value': instance['InstanceId']})
                instance['Tags'].append({'Key': global_key_to_tag_on, 'Value': 'true'})
                # Remove any tags prefixed with aws: since they are internal and we aren't allowed to set.  These can come from CloudFormation, or from Autoscalers
                finaltags = []
                for index, item in enumerate(instance['Tags']):
                    if item['Key'].startswith('aws:'):
                        print(f"Modifying internal aws tag so it doesn't fail: {item['Key']}")
                        finaltags.append({'Key': f"internal-{item['Key']}", 'Value': item['Value']})
                    else:
                        finaltags.append(item)
                response = ec2.create_tags(
                    Resources=[image['ImageId']],
                    Tags=finaltags
                )
            except:
                print("Failure trying to create image or tag image.  See/report exception below")
                exc_info = sys.exc_info()
                traceback.print_exception(*exc_info)


#####################
# Helper function to delete expired AMIs
#####################
def delete_expired_amis(ec2):
    # Get our list of AMIs to consider deleting...
    try:
        print(f"Scanning for AMIs with tags ({global_key_to_tag_on})")
        amis_to_consider = response = ec2.describe_images(
            Filters=[{'Name': 'tag-key', 'Values': [global_key_to_tag_on]}],
            Owners=['self'],
        )['Images']
    except:
        # Don't fatal error on regions that we haven't activated/enabled
        if 'OptInRequired' in str(sys.exc_info()):
            print("  Region not activated for this account, skipping...")
            return
        else:
            raise

    today_date = time.strptime(datetime.datetime.now().strftime('%m-%d-%Y'), '%m-%d-%Y')

    # Iterate and decide...
    if len(amis_to_consider) == 0:
        return
    print(f"Found {len(amis_to_consider)} amis to consider...")
    for ami in amis_to_consider:
        print(f"  Found AMI to consider: {ami['ImageId']}")

        # Figure out when the DeleteAfter is set to
        try:
            delete_after = [t.get('Value') for t in ami['Tags']if t['Key'] == 'DeleteAfter'][0]
        except:
            print("Unable to find when to delete this image after, skipping...")
            continue
        print(f"Delete After: {delete_after}")

        # Figure out if we should delete this AMI
        delete_date = time.strptime(delete_after, "%m-%d-%Y")
        if today_date < delete_date:
            print("This item is too new, skipping...")
            continue

        # Catch if we were dry-running this
        if dry_run:
            print(f"DRY_RUN, would have deleted ami : {ami['ImageId']}")
            for snapshot in [i['Ebs']['SnapshotId'] for i in ami['BlockDeviceMappings'] if 'Ebs' in i]:
                print(f"DRY_RUN, would have deleted volume snapshot {snapshot}")
        else:
            # Delete this AMI...
            print(f"DELETING AMI : {ami['ImageId']}")
            try:
                amiResponse = ec2.deregister_image( ImageId=ami['ImageId'] )
            except Exception as e:
                print(f"Unable to delete AMI: {e}")

            # Delete all snapshots underneath that ami...
            for snapshot in [i['Ebs']['SnapshotId'] for i in ami['BlockDeviceMappings'] if 'Ebs' in i]:
                print(f"DELETING AMI {ami['ImageId']} SNAPSHOT : {snapshot}")
                try:
                    result = ec2.delete_snapshot(SnapshotId=snapshot)
                except Exception as e:
                    print(f"Unable to delete snapshot: {e}")


#####################
# Lambda/script entrypoint
#####################
def lambda_handler(event, context):

    # For each region we want to scan...
    for aws_region in aws_regions:
        ec2 = boto3.client('ec2', region_name=aws_region)
        print(f"Scanning region: {aws_region}")

        # AMIs...
        backup_tagged_instances_in_region(ec2) # First, backup tagged instances in that region
        delete_expired_amis(ec2)               # Then, go delete AMIs that have expired in that region


# If ran on the CLI, go ahead and run it
if __name__ == "__main__":
    lambda_handler({},{})
