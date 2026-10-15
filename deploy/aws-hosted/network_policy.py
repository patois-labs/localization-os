"""Offline Cloudflare CIDR and inbound-rule validation for the hosted scripts."""

import ipaddress
import json
import sys


def cidrs(text):
    lines = text.splitlines()
    if not lines or any(not line or line != line.strip() for line in lines):
        raise ValueError("empty or malformed Cloudflare CIDR list")
    for line in lines:
        if "/" not in line:
            raise ValueError("Cloudflare entry must be a CIDR")
        ipaddress.ip_network(line, strict=True)
    return set(lines)


def check_rules(groups, allowed):
    errors = []
    for group in groups["SecurityGroups"]:
        for rule in group.get("IpPermissions", []):
            if (
                rule.get("IpProtocol") != "tcp"
                or rule.get("FromPort") not in (80, 443)
                or rule.get("ToPort") != rule.get("FromPort")
                or rule.get("UserIdGroupPairs")
                or rule.get("PrefixListIds")
            ):
                errors.append("inbound rules must be exact Cloudflare TCP 80/443 rules")
                continue
            sources = [r["CidrIp"] for r in rule.get("IpRanges", [])]
            sources += [r["CidrIpv6"] for r in rule.get("Ipv6Ranges", [])]
            if any(source not in allowed for source in sources):
                errors.append("inbound rule includes a non-Cloudflare source")
    return errors


def main():
    try:
        if sys.argv[1] == "cidrs":
            cidrs(sys.stdin.read())
            return 0
        allowed = cidrs(sys.argv[1])
        errors = check_rules(json.load(sys.stdin), allowed)
        for error in errors:
            print(error)
        return int(bool(errors))
    except (ValueError, KeyError, TypeError):
        print("Invalid network policy input; check failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
