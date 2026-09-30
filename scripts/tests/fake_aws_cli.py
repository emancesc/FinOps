"""Finto eseguibile `aws` (via aws.cmd nel PATH) che risponde con fake_aws_data."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_aws_data import fake_aws  # noqa: E402

args = sys.argv[1:]
region = None
clean = []
skip = False
for i, a in enumerate(args):
    if skip:
        skip = False
        continue
    if a in ("--profile", "--region", "--output"):
        if a == "--region":
            region = args[i + 1]
        skip = True
        continue
    clean.append(a)

try:
    print(json.dumps(fake_aws(region, *clean)))
except RuntimeError as exc:
    print(f"An error occurred ({exc})", file=sys.stderr)
    sys.exit(254)
