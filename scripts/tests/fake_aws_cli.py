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

# FAKE_AWS_LOG: file dove registrare le chiamate; FAKE_AWS_FAIL: "servizio operazione" da far fallire
if os.environ.get("FAKE_AWS_LOG"):
    with open(os.environ["FAKE_AWS_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps([region, *clean]) + "\n")
if os.environ.get("FAKE_AWS_FAIL") == " ".join(clean[:2]):
    print("An error occurred (Throttling)", file=sys.stderr)
    sys.exit(254)

try:
    print(json.dumps(fake_aws(region, *clean)))
except RuntimeError as exc:
    print(f"An error occurred ({exc})", file=sys.stderr)
    sys.exit(254)
