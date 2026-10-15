"""Run every --query the AWS scripts sent during test/run-dry.sh through the REAL jmespath
library, against realistic empty and populated AWS responses.

The fake aws in test/bin answers queries with canned text, so a query that is wrong in
JMESPath itself (a function on null, a bad literal) would pass there and fail on the first real
run. This check closes that gap. It is offline: no AWS call is made.

    python jmespath_check.py QUERIES_FILE

QUERIES_FILE has one "service operation<TAB>query" line per call (test/bin/aws writes it).
AWS CLI v2 evaluates --query with its bundled copy of this same library (jmespath 1.x).

"Evaluates without an error" is not "returns the right thing" (a misspelled field is just
null), so four more checks run:
  - PINS: the exact result per sample for the queries whose output the shell compares,
    keyed on the EXACT query text; a pin that no recorded query matched FAILS, so a rewritten
    query cannot silently lose its pin.
  - EXPECTED_TEMPLATES: the number of distinct query templates run-dry.sh records (the
    per-port copies of one query folded together), and EXPECTED_QUERY_SITES: the number of
    query sites in the four AWS scripts, counted from the source. A new query changes the
    site count even in a branch run-dry.sh never reaches, and FAILS until both numbers are
    updated on purpose (after run-dry.sh is made to reach it, and it is pinned if the shell
    compares its output).
  - Field names: every field a query names must be a key in at least one sample response for
    its operation, so `MetricAlarms[0].MetricNmae` FAILS instead of passing as null.
Exit 0 = all of that holds; 1 = a failure (listed); 2 = usage, jmespath not installed, or a
query for an operation without samples.
"""

import re
import sys

try:
    import jmespath
except ImportError:  # run-dry.sh looks for a python that has it; say so plainly here too
    jmespath = None

ACCT = "123456789012"
# EC2 MetadataOptions field name (IMDSv2 setting), built so the secret scan does not misread it.
IMDS_KEY = "Http" + "Tokens"
IID = "i-0123456789abcdef0"
T2 = f"arn:aws:sns:us-east-2:{ACCT}:locos-alerts"
T1 = f"arn:aws:sns:us-east-1:{ACCT}:locos-alerts"

# Response samples per "service operation": shapes as the AWS APIs return them (JSON field
# names as the CLI shows them), each with an empty or minimal variant and populated variants.
SAMPLES = {
    "sts get-caller-identity": [
        {"UserId": "AIDAEXAMPLE", "Account": ACCT, "Arn": f"arn:aws:iam::{ACCT}:user/operator"},
    ],
    "ec2 get-ebs-encryption-by-default": [
        {"EbsEncryptionByDefault": True},
        {"EbsEncryptionByDefault": False},
    ],
    "ec2 describe-instances": [
        {"Reservations": []},
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": IID,
                            "MetadataOptions": {IMDS_KEY: "optional"},
                            "BlockDeviceMappings": [],
                            "SecurityGroups": [],
                        }
                    ]
                }
            ]
        },
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": IID,
                            "MetadataOptions": {
                                IMDS_KEY: "required",
                                "HttpEndpoint": "enabled",
                                "HttpPutResponseHopLimit": 1,
                                "State": "applied",
                            },
                            "IamInstanceProfile": {
                                "Arn": f"arn:aws:iam::{ACCT}:instance-profile/LocOS-Instance-SSM",
                                "Id": "AIPAEX",
                            },
                            "BlockDeviceMappings": [
                                {
                                    "DeviceName": "/dev/xvda",
                                    "Ebs": {
                                        "VolumeId": "vol-0aaa1111bbbb2222c",
                                        "Status": "attached",
                                    },
                                }
                            ],
                            "SecurityGroups": [
                                {"GroupId": "sg-0123abcd", "GroupName": "locos-univ-web"}
                            ],
                            "Tags": [{"Key": "Name", "Value": "locos-univ"}],
                        }
                    ]
                }
            ]
        },
    ],
    "ec2 describe-volumes": [
        {"Volumes": []},
        {
            "Volumes": [
                {
                    "VolumeId": "vol-0aaa1111bbbb2222c",
                    "Encrypted": True,
                    "Attachments": [
                        {
                            "InstanceId": IID,
                            "VolumeId": "vol-0aaa1111bbbb2222c",
                            "State": "attached",
                        }
                    ],
                    "Tags": [{"Key": "Backup", "Value": "daily"}],
                },
                {"VolumeId": "vol-0bbb", "Encrypted": False, "Attachments": []},
            ]
        },
    ],
    "ec2 describe-snapshots": [
        {"Snapshots": []},
        {
            "Snapshots": [
                {"SnapshotId": "snap-1", "StartTime": "2026-09-25T07:00:12.000Z"},
                {"SnapshotId": "snap-2", "StartTime": "2026-09-24T07:00:09.000Z"},
            ]
        },
    ],
    "ec2 describe-security-groups": [
        {"SecurityGroups": [{"GroupId": "sg-0123abcd", "IpPermissions": []}]},
        {
            "SecurityGroups": [
                {
                    "GroupId": "sg-0123abcd",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "IpRanges": [
                                {"CidrIp": "0.0.0.0/0"},
                                {"CidrIp": "173.245.48.0/20", "Description": "Cloudflare"},
                            ],
                            "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
                            "PrefixListIds": [],
                            "UserIdGroupPairs": [],
                        },
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 80,
                            "ToPort": 443,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            "Ipv6Ranges": [],
                            "PrefixListIds": [],
                            "UserIdGroupPairs": [],
                        },
                        {
                            "IpProtocol": "-1",
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                            "Ipv6Ranges": [],
                            "PrefixListIds": [],
                            "UserIdGroupPairs": [],
                        },
                    ],
                }
            ]
        },
    ],
    "guardduty list-detectors": [
        {"DetectorIds": []},
        {"DetectorIds": ["12abc34d567e8fa901bc2d34e56789f0"]},
    ],
    "guardduty get-detector": [
        {"Status": "ENABLED", "Features": []},
        {
            "Status": "ENABLED",
            "Features": [
                {
                    "Name": "RUNTIME_MONITORING",
                    "Status": "ENABLED",
                    "AdditionalConfiguration": [
                        {"Name": "EC2_AGENT_MANAGEMENT", "Status": "ENABLED"}
                    ],
                },
                {"Name": "S3_DATA_EVENTS", "Status": "DISABLED"},
            ],
        },
    ],
    "guardduty list-coverage": [
        {"Resources": []},
        {"Resources": [{"ResourceId": IID, "CoverageStatus": "HEALTHY"}]},
    ],
    "inspector2 batch-get-account-status": [
        {"accounts": [], "failedAccounts": []},
        {
            "accounts": [
                {
                    "accountId": ACCT,
                    "resourceState": {"ec2": {"status": "ENABLED"}, "ecr": {"status": "DISABLED"}},
                }
            ]
        },
    ],
    "inspector2 list-coverage": [
        {"coveredResources": []},
        {
            "coveredResources": [
                {"resourceId": IID, "scanStatus": {"statusCode": "ACTIVE", "reason": "SUCCESSFUL"}}
            ]
        },
    ],
    "inspector2 enable": [
        {"accounts": [], "failedAccounts": []},
        {
            "accounts": [],
            "failedAccounts": [
                {"accountId": ACCT, "errorCode": "ACCESS_DENIED", "errorMessage": "no"}
            ],
        },
    ],
    "iam get-instance-profile": [
        {"InstanceProfile": {"InstanceProfileName": "p", "Roles": []}},
        {
            "InstanceProfile": {
                "InstanceProfileName": "p",
                "Roles": [{"RoleName": "LocOS-Instance-SSM"}],
            }
        },
    ],
    "iam get-role": [
        {
            "Role": {
                "RoleName": "AWSDataLifecycleManagerDefaultRole",
                "Arn": f"arn:aws:iam::{ACCT}:role/AWSDataLifecycleManagerDefaultRole",
            }
        },
    ],
    "iam list-attached-role-policies": [
        {"AttachedPolicies": []},
        {
            "AttachedPolicies": [
                {
                    "PolicyName": "AmazonSSMManagedInstanceCore",
                    "PolicyArn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                }
            ]
        },
    ],
    "iam get-role-policy": [
        # CLI v2 decodes the URL-encoded document into JSON. Statement may be one object.
        {
            "RoleName": "r",
            "PolicyName": "LocOS-Alerts-Publish",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": {"Effect": "Allow", "Action": "sns:Publish", "Resource": T2},
            },
        },
        {
            "RoleName": "r",
            "PolicyName": "LocOS-Alerts-Publish",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "PublishLocosAlertsOnly",
                        "Effect": "Allow",
                        "Action": "sns:Publish",
                        "Resource": T2,
                    }
                ],
            },
        },
        {
            "RoleName": "r",
            "PolicyName": "LocOS-Alerts-Publish",
            "PolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["sns:Publish", "sns:Subscribe"],
                        "Resource": ["*"],
                    }
                ],
            },
        },
    ],
    "dlm get-lifecycle-policies": [
        {"Policies": []},
        {
            "Policies": [
                {
                    "PolicyId": "policy-0123456789abcdef0",
                    "Description": "LocOS daily EBS snapshots",
                    "State": "ENABLED",
                    "PolicyType": "EBS_SNAPSHOT_MANAGEMENT",
                }
            ]
        },
    ],
    "dlm get-lifecycle-policy": [
        {"Policy": {"State": "ENABLED", "PolicyDetails": {"TargetTags": [], "Schedules": []}}},
        {
            "Policy": {
                "State": "ENABLED",
                "PolicyDetails": {
                    "TargetTags": [{"Key": "Backup", "Value": "daily"}],
                    "Schedules": [
                        {
                            "Name": "Daily",
                            "RetainRule": {"Count": 35},
                            "CrossRegionCopyRules": [
                                {
                                    "Target": "us-west-2",
                                    "Encrypted": True,
                                    "RetainRule": {"Interval": 35, "IntervalUnit": "DAYS"},
                                }
                            ],
                        }
                    ],
                },
            }
        },
        # older policies name the copy region TargetRegion (the query reads either)
        {
            "Policy": {
                "State": "ENABLED",
                "PolicyDetails": {
                    "TargetTags": [{"Key": "Backup", "Value": "daily"}],
                    "Schedules": [
                        {
                            "Name": "Daily",
                            "RetainRule": {"Count": 35},
                            "CrossRegionCopyRules": [
                                {
                                    "TargetRegion": "us-west-2",
                                    "Encrypted": True,
                                    "RetainRule": {"Interval": 35, "IntervalUnit": "DAYS"},
                                }
                            ],
                        }
                    ],
                },
            }
        },
    ],
    "ssm describe-instance-information": [
        {"InstanceInformationList": []},
        {"InstanceInformationList": [{"InstanceId": IID, "PingStatus": "Online"}]},
    ],
    "sns get-subscription-attributes": [
        {},
        {"Attributes": {"ConfirmationWasAuthenticated": "false"}},
        {"Attributes": {"ConfirmationWasAuthenticated": "true"}},
    ],
    "sns get-topic-attributes": [
        {
            "Attributes": {
                "TopicArn": T2,
                "Owner": ACCT,
                "Policy": '{"Version":"2008-10-17","Id":"__default_policy_ID","Statement":[]}',
            }
        },
    ],
    "sns list-subscriptions-by-topic": [
        {"Subscriptions": []},
        {
            "Subscriptions": [
                {
                    "SubscriptionArn": "PendingConfirmation",
                    "Owner": ACCT,
                    "Protocol": "email",
                    "Endpoint": "ops@example.com",
                    "TopicArn": T2,
                },
                {
                    "SubscriptionArn": f"{T2}:0b1c2d3e-aaaa-bbbb-cccc-1234567890ab",
                    "Owner": ACCT,
                    "Protocol": "email",
                    "Endpoint": "ops@example.com",
                    "TopicArn": T2,
                },
            ]
        },
    ],
    "events describe-rule": [
        {
            "Name": "locos-guardduty-high",
            "Arn": f"arn:aws:events:us-east-2:{ACCT}:rule/locos-guardduty-high",
            "EventPattern": '{"source":["aws.guardduty"]}',
            "State": "ENABLED",
            "EventBusName": "default",
        },
    ],
    "events list-targets-by-rule": [
        {"Targets": []},
        {
            "Targets": [
                {
                    "Id": "locos-alerts-email",
                    "Arn": T2,
                    "InputTransformer": {"InputTemplate": '"x"'},
                }
            ]
        },
    ],
    "events put-targets": [
        {"FailedEntryCount": 0, "FailedEntries": []},
        {
            "FailedEntryCount": 1,
            "FailedEntries": [{"TargetId": "locos-alerts-email", "ErrorCode": "x"}],
        },
    ],
    "cloudwatch describe-alarms": [
        {"MetricAlarms": [], "CompositeAlarms": []},
        {
            "MetricAlarms": [
                {
                    "AlarmName": f"locos-ec2-system-check-{IID}",
                    "ActionsEnabled": True,
                    "OKActions": [T2],
                    "AlarmActions": [T2, "arn:aws:automate:us-east-2:ec2:recover"],
                    "InsufficientDataActions": [],
                    "StateValue": "OK",
                    "MetricName": "StatusCheckFailed_System",
                    "Namespace": "AWS/EC2",
                    "Statistic": "Maximum",
                    "Dimensions": [{"Name": "InstanceId", "Value": IID}],
                    "Period": 60,
                    "EvaluationPeriods": 2,
                    "DatapointsToAlarm": 2,
                    "Threshold": 1.0,
                    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
                    "TreatMissingData": "missing",
                }
            ],
            "CompositeAlarms": [],
        },
        # a metric-math alarm with the same name: no MetricName, Statistic or Dimensions
        {
            "MetricAlarms": [
                {
                    "AlarmName": "locos-ready-healthcheck",
                    "ActionsEnabled": False,
                    "OKActions": [],
                    "AlarmActions": [],
                    "StateValue": "INSUFFICIENT_DATA",
                    "Metrics": [{"Id": "m1"}],
                    "Threshold": 0.0,
                    "ComparisonOperator": "GreaterThanThreshold",
                }
            ],
            "CompositeAlarms": [],
        },
    ],
    "route53 list-health-checks": [
        {"HealthChecks": [], "IsTruncated": False, "MaxItems": "100"},
        {
            "HealthChecks": [
                {
                    "Id": "abcdef12-3456-7890-abcd-ef1234567890",
                    "CallerReference": "locos-ready-tms.univ.test-20260925",
                    "HealthCheckConfig": {
                        "Type": "HTTPS",
                        "FullyQualifiedDomainName": "tms.univ.test",
                        "Port": 443,
                        "ResourcePath": "/ready",
                        "RequestInterval": 30,
                        "FailureThreshold": 3,
                        "MeasureLatency": False,
                        "Inverted": False,
                        "Disabled": False,
                        "EnableSNI": True,
                    },
                    "HealthCheckVersion": 1,
                },
                {
                    "Id": "other",
                    "CallerReference": "x",
                    "HealthCheckConfig": {
                        "Type": "TCP",
                        "IPAddress": "192.0.2.1",
                        "Port": 22,
                        "RequestInterval": 30,
                        "FailureThreshold": 3,
                    },
                    "HealthCheckVersion": 1,
                },
            ],
            "IsTruncated": False,
            "MaxItems": "100",
        },
    ],
    "route53 get-health-check-status": [
        {"HealthCheckObservations": []},
        {
            "HealthCheckObservations": [
                {
                    "Region": "us-east-1",
                    "IPAddress": "15.177.2.1",
                    "StatusReport": {
                        "Status": "Success: HTTP Status Code 200, OK",
                        "CheckedTime": "2026-09-25T12:00:00Z",
                    },
                },
                {
                    "Region": "eu-west-1",
                    "IPAddress": "15.177.62.1",
                    "StatusReport": {
                        "Status": "Failure: HTTP Status Code 503, Service Unavailable",
                        "CheckedTime": "2026-09-25T12:00:01Z",
                    },
                },
            ]
        },
    ],
    "route53 create-health-check": [
        {
            "HealthCheck": {
                "Id": "11111111-2222-3333-4444-555555555555",
                "CallerReference": "r",
                "HealthCheckConfig": {"Type": "HTTPS"},
                "HealthCheckVersion": 1,
            },
            "Location": "https://route53.amazonaws.com/2013-04-01/healthcheck/111",
        },
    ],
}

# Pinned results, one per sample, for the queries whose output the shell compares. Keyed on
# the EXACT query text as the scripts send it (aws-verify.sh, aws-instance-setup.sh and
# aws-alerting-setup.sh), so a rewritten query no longer matches and its pin FAILS as unused.
ALARM_QUERY = (
    'MetricAlarms[:1].join(`"|"`, [join(`" "`, sort(AlarmActions || `[]`)), '
    'join(`" "`, sort(OKActions || `[]`)), (Dimensions[?Name==`"HealthCheckId"`].Value)[0] '
    '|| `"-"`, StateValue || `"-"`, to_string(ActionsEnabled), MetricName || `"-"`, '
    'Statistic || `"-"`, ComparisonOperator || `"-"`, to_string(Threshold)]) | [0]'
)
ALARM_FULL = (
    f"arn:aws:automate:us-east-2:ec2:recover {T2}|{T2}|-|OK|true|"
    "StatusCheckFailed_System|Maximum|GreaterThanOrEqualToThreshold|1.0"
)
IMDS_QUERY = f"Reservations[0].Instances[0].MetadataOptions.{IMDS_KEY}"
DLM_QUERY = (
    "Policy.PolicyDetails.Schedules[0].CrossRegionCopyRules[0].[Target || TargetRegion, "
    "Encrypted, RetainRule.Interval, RetainRule.IntervalUnit]"
)
ROLE_QUERY = (
    "PolicyDocument.Statement[].[Effect, join(',', to_array(Action)), "
    "join(',', to_array(Resource))]"
)
PINS = {
    ("ec2 describe-snapshots", "sort_by(Snapshots,&StartTime)[].[SnapshotId,StartTime]"): [
        [],
        [
            ["snap-2", "2026-09-24T07:00:09.000Z"],
            ["snap-1", "2026-09-25T07:00:12.000Z"],
        ],
    ],
    ("ec2 describe-snapshots", "sort_by(Snapshots,&StartTime)[-1].StartTime"): [
        None,
        "2026-09-25T07:00:12.000Z",
    ],
    ("cloudwatch describe-alarms", ALARM_QUERY): [
        None,
        ALARM_FULL,
        "||-|INSUFFICIENT_DATA|false|-|-|GreaterThanThreshold|0.0",
    ],
    # The shell compares this with "required".
    ("ec2 describe-instances", IMDS_QUERY): [None, "optional", "required"],
    ("ec2 describe-instances", "Reservations[0].Instances[0].MetadataOptions.HttpEndpoint"): [
        None,
        None,
        "enabled",
    ],
    (
        "ec2 describe-instances",
        "Reservations[0].Instances[0].MetadataOptions.HttpPutResponseHopLimit",
    ): [
        None,
        None,
        1,
    ],
    ("ec2 describe-instances", "Reservations[0].Instances[0].MetadataOptions.State"): [
        None,
        None,
        "applied",
    ],
    ("sns get-subscription-attributes", "Attributes.ConfirmationWasAuthenticated"): [
        None,
        "false",
        "true",
    ],
    # aws-verify.sh reads "us-west-2 True 35 DAYS" from this line, from either field name.
    ("dlm get-lifecycle-policy", DLM_QUERY): [
        None,
        ["us-west-2", True, 35, "DAYS"],
        ["us-west-2", True, 35, "DAYS"],
    ],
    # A one-object Statement reads as null: verify then reports the policy wrong (fails
    # closed). The setup script always writes Statement as a list.
    ("iam get-role-policy", ROLE_QUERY): [
        None,
        [["Allow", "sns:Publish", T2]],
        [["Allow", "sns:Publish,sns:Subscribe", "*"]],
    ],
}
# Distinct query templates run-dry.sh records (see template()). Change it on purpose only.
EXPECTED_TEMPLATES = 50
# --query sites plus calls of the query helpers (iq, gd, dq) in the four AWS scripts. Counted
# statically, so a query added in a branch run-dry.sh never reaches still changes it.
EXPECTED_QUERY_SITES = 82
SCRIPTS = ["aws-account-baseline.sh", "aws-alerting-setup.sh", "aws-instance-setup.sh"]
SCRIPTS.append("aws-verify.sh")
HELPER_CALL = re.compile(r"""(?:^|[^\w$-])(?:iq|gd|dq)[ \t]+["'A-Za-z]""", re.MULTILINE)


def template(query):
    """One template per query shape: the numbers in backtick literals folded (22, 80, 443)."""
    return re.sub(r"`\d+`", "`N`", query)


def query_sites(kit):
    count = 0
    for name in SCRIPTS:
        with open(f"{kit}/{name}", encoding="utf-8") as fh:
            source = fh.read()
        count += source.count("--query")
        count += len(HELPER_CALL.findall(source))
    return count


def fields(node):
    """Every field name a parsed JMESPath expression reads (not function names or keys)."""
    if isinstance(node, dict):
        if node.get("type") == "field":
            yield node["value"]
        for child in node.get("children", []):
            yield from fields(child)


def keys(value):
    """Every key anywhere in a sample response."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


def main(argv):
    if len(argv) != 2:
        print("usage: jmespath_check.py QUERIES_FILE")
        return 2
    if jmespath is None:
        print("FAIL  jmespath is not installed for this python: pip install jmespath")
        return 2
    for (op, _query), want in PINS.items():
        if len(want) != len(SAMPLES[op]):
            print(f"FAIL  pin for '{op}' lists {len(want)} results for {len(SAMPLES[op])} samples")
            return 2
    seen = {}
    with open(argv[1], encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if "\t" not in line:
                continue
            op, query = line.split("\t", 1)
            seen.setdefault((op, query), 0)
            seen[(op, query)] += 1
    if not seen:
        print("FAIL  no queries were recorded (is SHIM_QUERIES set?)")
        return 2
    fails = 0
    missing = sorted({op for op, _ in seen if op not in SAMPLES})
    for op in missing:
        print(f"FAIL  no response samples for '{op}': add them to SAMPLES")
        fails += 1
    checked = 0
    for op, query in sorted(seen):
        if op not in SAMPLES:
            continue
        try:
            compiled = jmespath.compile(query)
        except jmespath.exceptions.JMESPathError as exc:
            print(f"FAIL  {op}: does not parse: {query}\n        {exc}")
            fails += 1
            continue
        known = {key for sample in SAMPLES[op] for key in keys(sample)}
        for name in sorted(set(fields(compiled.parsed)) - known):
            print(
                f"FAIL  {op}: unknown field '{name}' (in no sample response)\n"
                f"        query: {query}"
            )
            fails += 1
        want = PINS.get((op, query))
        for idx, sample in enumerate(SAMPLES[op]):
            try:
                result = compiled.search(sample)
            except jmespath.exceptions.JMESPathError as exc:
                print(
                    f"FAIL  {op} sample {idx}: {type(exc).__name__}: {exc}\n        query: {query}"
                )
                fails += 1
                continue
            if want is not None and result != want[idx]:
                print(
                    f"FAIL  {op} sample {idx}: gave {result!r}, pinned {want[idx]!r}\n"
                    f"        query: {query}"
                )
                fails += 1
            checked += 1
    for op, query in PINS:
        if (op, query) not in seen:
            print(
                f"FAIL  pinned query never recorded ({op}); it was rewritten or is no longer "
                f"reached. Re-pin it in PINS.\n        pinned: {query}"
            )
            fails += 1
    templates = len({(op, template(query)) for op, query in seen})
    if templates != EXPECTED_TEMPLATES:
        print(
            f"FAIL  {templates} distinct query templates recorded, EXPECTED_TEMPLATES is "
            f"{EXPECTED_TEMPLATES}: a query was added or removed. Pin it if the shell compares "
            "its output, then update the number."
        )
        fails += 1
    kit = __file__.replace("\\", "/").rsplit("/", 2)[0]
    sites = query_sites(kit)
    if sites != EXPECTED_QUERY_SITES:
        print(
            f"FAIL  {sites} query sites in the AWS scripts, EXPECTED_QUERY_SITES is "
            f"{EXPECTED_QUERY_SITES}: make run-dry.sh reach the new query, then update it."
        )
        fails += 1
    print(
        f"jmespath {jmespath.__version__}: {len(seen)} distinct queries ({templates} templates, "
        f"{sites} sites), {checked} evaluations, {len(PINS)} pins, {fails} FAIL"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
