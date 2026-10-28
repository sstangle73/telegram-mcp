"""Deploy the Telegram MCP server to AWS Lambda behind a Function URL. A dry run unless --apply.

    python deploy.py                              what exists, and what --apply would change (builds the zip locally)
    python deploy.py --apply --session-file data/session.txt
                                                  the first deploy: store login.py's session, then create everything
    python deploy.py --apply                      after a code change: update what differs
    python deploy.py --apply --rotate-token       a new connector URL; the old one stops working
    python deploy.py --url                        put the connector URL on the clipboard (or in data/connector-url.txt)
    python deploy.py --apply --only-role          just the IAM role, for someone allowed to manage IAM (see below)

It uses boto3's usual credentials (AWS_PROFILE, AWS_ACCESS_KEY_ID and friends, SSO) or --profile, in --region (else
your AWS config's region, else us-east-1). Everything is named after --name (default telegram-mcp), so one account
can hold several copies, one per Telegram account.

What it manages:
  - SSM Parameter Store /<name>/: api-id (String), and api-hash, session and auth-token (SecureString). The session
    can do anything your Telegram account can. Nothing here prints it or the token.
  - IAM role <name>-lambda: its logs, and reading those four parameters. Deploying with credentials that can't manage
    IAM (aws/deploy-policy.json is such a policy)? Have someone who can run --only-role once.
  - Lambda function <name> (python3.14, arm64, 512 MB, 60 s), reserved concurrency 2, logs kept 30 days.
  - Its Function URL (auth NONE: the server checks the token itself) and the two public permissions a URL needs.
Whoever can replace the function's code can reach the session through it, so treat deploy credentials accordingly.
"""
import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent
RUNTIME, PYTHON, ARCH = "python3.14", "3.14", "arm64"
# Amazon Linux 2023 has glibc 2.34. pip doesn't widen a --platform to older manylinux tags, so each is listed.
PLATFORMS = ["manylinux_2_34_aarch64", "manylinux_2_28_aarch64", "manylinux_2_17_aarch64", "manylinux2014_aarch64"]
SDIST_ONLY = ["pyaes"]   # pure Python, published only as source: built here, the rest must be Linux wheels
FUNCTION = {"Runtime": RUNTIME, "Handler": "lambda_handler.handler", "MemorySize": 512, "Timeout": 60,
            "Description": "Telegram MCP server for Claude"}
TAGS = {"managed-by": "telegram-mcp deploy.py"}
TRUST = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def role_policy(account, region, name):
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "ItsLogs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{name}:*"},
        {"Sid": "ReadItsParameters", "Effect": "Allow", "Action": "ssm:GetParameters",
         "Resource": f"arn:aws:ssm:{region}:{account}:parameter/{name}/*"},
        {"Sid": "DecryptThemThroughSsm", "Effect": "Allow", "Action": "kms:Decrypt", "Resource": "*",
         "Condition": {"StringEquals": {"kms:ViaService": f"ssm.{region}.amazonaws.com"}}}]}


def code(e):
    return e.response["Error"]["Code"]


def dotenv():
    """API_ID and API_HASH from the environment, else from .env (KEY=value lines)."""
    vals = {k: os.environ[k] for k in ("API_ID", "API_HASH") if os.environ.get(k)}
    if (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#") and k in ("API_ID", "API_HASH") and k not in vals and v.strip():
                vals[k] = v.strip().strip("\"'")
    return vals


def build_zip():
    out = ROOT / "build" / "lambda"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    tags = [a for p in PLATFORMS for a in ("--platform", p)]
    tags += [a for abi in (f"cp{PYTHON.replace('.', '')}", "abi3", "none") for a in ("--abi", abi)]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps",
                    "--no-compile", "--only-binary=:all:", "--no-binary=" + ",".join(SDIST_ONLY), *tags,
                    "--implementation", "cp", "--python-version", PYTHON, "--target", str(out),
                    "-r", str(ROOT / "requirements.txt")], check=True)
    for f in ("server.py", "lambda_handler.py"):
        shutil.copy2(ROOT / f, out / f)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            rel = p.relative_to(out)
            # bin/ holds pip's console-script launchers (.exe files when built on Windows), and the RECORDs hash them
            # afresh each build; nothing reads either.
            if (p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] != "bin"
                    and not (rel.name == "RECORD" and rel.parent.name.endswith(".dist-info"))):
                info = zipfile.ZipInfo(rel.as_posix(), date_time=(2020, 1, 1, 0, 0, 0))   # same bytes, same hash
                info.external_attr, info.compress_type = 0o644 << 16, zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
    data = buf.getvalue()
    return data, base64.b64encode(hashlib.sha256(data).digest()).decode()


def to_clipboard(text):
    for cmd in (["clip"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode("ascii"), check=True)
            return True
    return False


def ensure_role(iam, role, want, apply):
    """The Lambda's role, created or brought in line. Returns False when the credentials may not manage IAM."""
    do = "" if apply else "would "
    try:
        iam.get_role(RoleName=role)
        try:
            doc = iam.get_role_policy(RoleName=role, PolicyName=role)["PolicyDocument"]
        except ClientError as e:
            if code(e) != "NoSuchEntity":
                raise
            doc = None
        state = "same" if doc == want else "policy differs"
    except ClientError as e:
        if code(e) != "NoSuchEntity":
            raise
        state = "missing"
    if state == "same":
        print(f"IAM role {role} in place")
        return True
    print(f"{do}{'create' if state == 'missing' else 'update'} IAM role {role}")
    if not apply:
        return True
    try:
        if state == "missing":
            iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(TRUST),
                            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
                            Description="Runs the telegram-mcp Lambda: its logs, and its own SSM parameters")
        iam.put_role_policy(RoleName=role, PolicyName=role, PolicyDocument=json.dumps(want))
    except ClientError as e:
        if code(e) not in ("AccessDenied", "AccessDeniedException"):
            raise
        return False
    return True


def wait(lam, name, waiter):
    lam.get_waiter(waiter).wait(FunctionName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 90})


def main():
    ap = argparse.ArgumentParser(description="Deploy the Telegram MCP server to AWS Lambda (dry run by default).")
    ap.add_argument("--apply", action="store_true", help="make the changes")
    ap.add_argument("--session-file", type=Path, help="store this Telegram session (from login.py)")
    ap.add_argument("--rotate-token", action="store_true", help="new auth token, so a new connector URL")
    ap.add_argument("--url", action="store_true", help="only put the connector URL on the clipboard")
    ap.add_argument("--only-role", action="store_true", help="only create or update the IAM role")
    ap.add_argument("--send-limit", type=int, help="SEND_LIMIT_PER_HOUR (default: keep, or 20)")
    ap.add_argument("--blocked-chats", help="BLOCKED_CHATS, comma-separated chat ids (default: keep)")
    ap.add_argument("--name", default="telegram-mcp", help="the function's name, and the stem of the others")
    ap.add_argument("--profile", help="an AWS profile (default: boto3's usual credentials)")
    ap.add_argument("--region", help="default: your AWS config's region, else us-east-1")
    args = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", args.name):
        sys.exit("--name: up to 50 letters, digits, - and _")
    apply, name = args.apply, args.name
    do = "" if apply else "would "
    prefix, role = f"/{name}", f"{name}-lambda"

    s = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = s.region_name or "us-east-1"
    s = boto3.Session(profile_name=args.profile, region_name=region)
    account = s.client("sts").get_caller_identity()["Account"]
    ssm, lam, iam, logs = (s.client(n) for n in ("ssm", "lambda", "iam", "logs"))
    print(f"account {account}, region {region}, name {name}")

    role_arn, want = f"arn:aws:iam::{account}:role/{role}", role_policy(account, region, name)
    denied = (f"these credentials may not manage IAM. Someone who may runs: python deploy.py --apply --only-role"
              f"{'' if name == 'telegram-mcp' else ' --name ' + name}")
    if args.only_role:
        if not ensure_role(iam, role, want, apply):
            sys.exit(denied)
        return

    function = None
    try:
        function = lam.get_function(FunctionName=name)["Configuration"]
    except ClientError as e:
        if code(e) != "ResourceNotFoundException":
            raise
    furl = None
    if function:
        try:
            furl = lam.get_function_url_config(FunctionName=name)["FunctionUrl"]
        except ClientError as e:
            if code(e) != "ResourceNotFoundException":
                raise

    if args.url:
        if not furl:
            sys.exit("no Function URL yet: run deploy.py --apply first")
        token = ssm.get_parameter(Name=f"{prefix}/auth-token", WithDecryption=True)["Parameter"]["Value"]
        url = f"{furl.rstrip('/')}/{token}/mcp"
        if to_clipboard(url):
            print(f"connector URL ({furl}<token>/mcp) is on the clipboard: paste it into Claude, then clear it")
        else:
            p = ROOT / "data" / "connector-url.txt"
            p.parent.mkdir(exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(url + "\n")
            print(f"connector URL ({furl}<token>/mcp) written to {p}")
        return

    # 1. The role, before any secret is written.
    if not ensure_role(iam, role, want, apply):
        sys.exit(denied)

    # 2. The parameters.
    have = {p["Name"].rsplit("/", 1)[1] for page in ssm.get_paginator("describe_parameters").paginate(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [prefix + "/"]}])
        for p in page["Parameters"]}
    changed_secrets = False
    puts = []
    env = dotenv()
    for key, kind, var in (("api-id", "String", "API_ID"), ("api-hash", "SecureString", "API_HASH")):
        if key not in have:
            value = env.get(var) or (apply and (input if kind == "String" else getpass.getpass)(f"{var}: ").strip())
            if apply and not value:
                sys.exit(f"{var} is needed (my.telegram.org)")
            puts.append((key, kind, value))
    if args.session_file:
        value = args.session_file.read_text(encoding="utf-8").strip()
        if len(value) < 100:
            sys.exit(f"{args.session_file} doesn't look like a Telegram session string")
        puts.append(("session", "SecureString", value))
    elif "session" not in have:
        sys.exit("no Telegram session in SSM yet: run login.py, then pass --session-file data/session.txt")
    if args.rotate_token or "auth-token" not in have:
        puts.append(("auth-token", "SecureString", secrets.token_urlsafe(32)))
    for key, kind, value in puts:
        print(f"{do}put SSM {prefix}/{key} ({kind}{', new' if key not in have else ', replacing'})")
        if apply:
            ssm.put_parameter(Name=f"{prefix}/{key}", Type=kind, Value=value, Overwrite=key in have, Tier="Standard")
        changed_secrets = True
    if not puts:
        print(f"SSM {prefix}/: api-id, api-hash, session and auth-token in place")
    elif args.session_file and apply:
        print(f"the session is in SSM now: delete {args.session_file} unless you also run the server locally")

    # 3. The code and the function.
    zip_bytes, sha = build_zip()
    print(f"built the zip: {len(zip_bytes) // 1024} KiB, sha256 {sha[:12]}...")
    current = (function or {}).get("Environment", {}).get("Variables", {})
    variables = {"SSM_PREFIX": prefix,
                 "ALLOWED_HOSTS": furl.split("/")[2] if furl else "",
                 "SEND_LIMIT_PER_HOUR": str(args.send_limit or current.get("SEND_LIMIT_PER_HOUR", "20")),
                 "BLOCKED_CHATS": args.blocked_chats if args.blocked_chats is not None else current.get(
                     "BLOCKED_CHATS", ""),
                 # A new value makes Lambda start fresh containers, which read the changed secrets.
                 "SECRETS_VERSION": str(int(current.get("SECRETS_VERSION", "0")) + changed_secrets)}
    if not function:
        print(f"{do}create Lambda function {name} ({RUNTIME}, {ARCH})")
        if apply:
            for attempt in range(15):   # a role just made takes a few seconds to be usable by Lambda
                try:
                    lam.create_function(FunctionName=name, Role=role_arn, Code={"ZipFile": zip_bytes},
                                        Architectures=[ARCH], Environment={"Variables": variables}, Tags=TAGS,
                                        **FUNCTION)
                    break
                except ClientError as e:
                    if code(e) != "InvalidParameterValueException" or attempt == 14:
                        raise
                    time.sleep(4)
            wait(lam, name, "function_active_v2")
    else:
        if function["CodeSha256"] == sha:
            print("function code unchanged")
        else:
            print(f"{do}update the function's code")
            if apply:
                lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
                wait(lam, name, "function_updated_v2")
        settings = {k: function.get(k) for k in FUNCTION}
        if settings != FUNCTION or current != variables or function["Role"] != role_arn:
            print(f"{do}update the function's settings")
            if apply:
                lam.update_function_configuration(FunctionName=name, Role=role_arn,
                                                  Environment={"Variables": variables}, **FUNCTION)
                wait(lam, name, "function_updated_v2")
        else:
            print("function settings unchanged")

    # 4. The URL and who may call it (everyone: the token is checked inside).
    if not furl:
        print(f"{do}create the Function URL (auth NONE)")
        if apply:
            furl = lam.create_function_url_config(FunctionName=name, AuthType="NONE", InvokeMode="BUFFERED")[
                "FunctionUrl"]
            variables["ALLOWED_HOSTS"] = furl.split("/")[2]
            lam.update_function_configuration(FunctionName=name, Environment={"Variables": variables})
            wait(lam, name, "function_updated_v2")
    try:
        sids = {st["Sid"] for st in json.loads(lam.get_policy(FunctionName=name)["Policy"])["Statement"]}
    except ClientError as e:
        if code(e) != "ResourceNotFoundException":
            raise
        sids = set()
    for sid, extra in (("public-function-url", {"Action": "lambda:InvokeFunctionUrl", "FunctionUrlAuthType": "NONE"}),
                       ("public-function-url-invoke", {"Action": "lambda:InvokeFunction",
                                                       "InvokedViaFunctionUrl": True})):
        if sid not in sids:
            print(f"{do}allow {extra['Action']} through the URL")
            if apply:
                lam.add_permission(FunctionName=name, StatementId=sid, Principal="*", **extra)

    # 5. A cap on concurrency (a leaked URL can't run up a bill) and log retention.
    try:
        reserved = lam.get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
    except ClientError as e:
        if code(e) != "ResourceNotFoundException":
            raise
        reserved = None
    if reserved != 2:
        print(f"{do}reserve concurrency 2")
        if apply:
            try:
                lam.put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=2)
            except ClientError as e:   # accounts with a low concurrency quota can't reserve any
                print(f"  could not reserve concurrency ({code(e)}): {e.response['Error']['Message']}")
    group = f"/aws/lambda/{name}"
    found = [g for g in logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"] if g["logGroupName"] == group]
    if not found or found[0].get("retentionInDays") != 30:
        print(f"{do}keep {group} for 30 days")
        if apply:
            if not found:
                logs.create_log_group(logGroupName=group)
            logs.put_retention_policy(logGroupName=group, retentionInDays=30)

    if not apply:
        print("dry run: nothing changed. Run again with --apply.")
    elif furl:
        print(f"done: {furl}<token>/mcp. python deploy.py --url puts the full connector URL on the clipboard.")


if __name__ == "__main__":
    main()
