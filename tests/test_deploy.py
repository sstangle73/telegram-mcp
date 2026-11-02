"""deploy.py against a fake AWS, so nothing here needs credentials: python -m unittest discover -s tests"""
import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import boto3  # noqa: F401
except ImportError:   # it's only for deploy.py, so requirements.txt leaves it out
    class ClientError(Exception):
        def __init__(self, response, operation):
            super().__init__(operation)
            self.response = response
    sys.modules.update({"boto3": types.ModuleType("boto3"), "botocore": types.ModuleType("botocore"),
                        "botocore.exceptions": types.SimpleNamespace(ClientError=ClientError)})
    sys.modules["boto3"].Session = None

import deploy  # noqa: E402

ACCOUNT, REGION = "123456789012", "us-east-1"
FURL = "https://abc123.lambda-url.us-east-1.on.aws/"
SESSION = "1SESSION" + "s" * 345
HASH, TOTP, KEY = "scrypt:32768:8:1:c2FsdA:aGFzaA", "JBSWY3DPEHPK3PXP", "k" * 43


def error(code):
    return deploy.ClientError({"Error": {"Code": code, "Message": code}}, "operation")


class Service:
    """One AWS client: every call is noted, and answered by Aws.<service>_<operation> if there is one."""

    def __init__(self, aws, name):
        self.aws, self.name = aws, name

    def __getattr__(self, op):
        def call(**kw):
            self.aws.calls.append((self.name, op, kw))
            answer = getattr(self.aws, f"{self.name}_{op}", None)
            return answer(**kw) if answer else {}
        return call

    def get_paginator(self, op):
        return types.SimpleNamespace(paginate=lambda **kw: [getattr(self, op)(**kw)])

    def get_waiter(self, waiter):
        return types.SimpleNamespace(wait=lambda **kw: self.aws.calls.append((self.name, "wait", {"waiter": waiter})))


class Aws:
    """Just enough of one account, stateful so that a second run sees what the first made."""
    region_name = REGION

    def __init__(self):
        self.calls, self.params, self.kinds = [], {}, {}
        self.role_doc, self.function, self.url, self.sids, self.reserved, self.log_days = None, None, None, set(), None, None
        self.deny = set()   # operations that get AccessDenied
        self.function_failures = []

    def client(self, name):
        return Service(self, name)

    def writes(self):
        return [c for c in self.calls if c[1] != "wait" and not c[1].startswith(("get_", "describe_", "list_"))]

    def ops(self):
        return [op for _, op, _ in self.writes()]

    def sts_get_caller_identity(self):
        return {"Account": ACCOUNT}

    def iam_get_role(self, RoleName):
        if self.role_doc is None:
            raise error("NoSuchEntity")
        return {}

    def iam_get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": self.role_doc}

    def iam_create_role(self, **kw):
        self._deny("create_role")

    def iam_put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self._deny("put_role_policy")
        self.role_doc = json.loads(PolicyDocument)

    def _deny(self, op):
        if op in self.deny:
            raise error("AccessDenied")

    def ssm_describe_parameters(self, ParameterFilters):
        return {"Parameters": [{"Name": n} for n in self.params]}

    def ssm_put_parameter(self, Name, Type, Value, Overwrite, Tier):
        self.params[Name], self.kinds[Name] = Value, Type

    def ssm_delete_parameter(self, Name):
        del self.params[Name]

    def ssm_get_parameter(self, Name, WithDecryption):
        return {"Parameter": {"Value": self.params[Name]}}

    def lambda_get_function(self, FunctionName):
        if not self.function:
            raise error("ResourceNotFoundException")
        return {"Configuration": self.function}

    def lambda_get_function_url_config(self, FunctionName):
        if not self.url:
            raise error("ResourceNotFoundException")
        return {"FunctionUrl": self.url}

    def lambda_create_function(self, Role, Code, Environment, **kw):
        if self.function_failures:
            raise error(self.function_failures.pop())
        self.function = {k: kw.get(k) for k in deploy.FUNCTION}
        self.function.update(Role=Role, Environment=Environment, CodeSha256="sha-1")

    def lambda_update_function_code(self, **kw):
        self.function["CodeSha256"] = "sha-1"

    def lambda_update_function_configuration(self, Environment, Role=None, **kw):
        self.function["Environment"] = Environment

    def lambda_create_function_url_config(self, **kw):
        self.url = FURL
        return {"FunctionUrl": FURL}

    def lambda_get_policy(self, FunctionName):
        if not self.sids:
            raise error("ResourceNotFoundException")
        return {"Policy": json.dumps({"Statement": [{"Sid": s} for s in self.sids]})}

    def lambda_add_permission(self, StatementId, **kw):
        self.sids.add(StatementId)

    def lambda_get_function_concurrency(self, FunctionName):
        return {"ReservedConcurrentExecutions": self.reserved} if self.reserved else {}

    def lambda_put_function_concurrency(self, ReservedConcurrentExecutions, **kw):
        self._deny("put_function_concurrency")
        self.reserved = ReservedConcurrentExecutions

    def logs_describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [] if self.log_days is None else
                [{"logGroupName": logGroupNamePrefix, "retentionInDays": self.log_days}]}

    def logs_put_retention_policy(self, retentionInDays, **kw):
        self.log_days = retentionInDays

    @property
    def env(self):
        return self.function["Environment"]["Variables"]


class Deploy(unittest.TestCase):
    def setUp(self):
        self.aws, self.dir = Aws(), Path(tempfile.mkdtemp())
        self.session_file = self.dir / "session.txt"
        self.session_file.write_text(SESSION)
        self.signin_file = self.dir / "signin.env"
        self.signin_file.write_text(f"# from setup_signin.py\nLOGIN_PASSWORD_HASH={HASH}\nTOTP_SECRET={TOTP}\n"
                                    f"SIGNING_KEY={KEY}\n")
        self.clipboard = []

    def run_it(self, *argv):
        """deploy.py with these arguments; what it printed. SystemExit comes through."""
        out = io.StringIO()
        session = mock.Mock(return_value=self.aws)
        with mock.patch.object(deploy.boto3, "Session", session), mock.patch.object(deploy, "ROOT", self.dir), \
                mock.patch.object(deploy, "build_zip", lambda: (b"zip", "sha-1")), \
                mock.patch.object(deploy, "to_clipboard", lambda t: self.clipboard.append(t) or True), \
                mock.patch.object(sys, "argv", ["deploy.py", *argv]), mock.patch.object(deploy.time, "sleep"), \
                mock.patch.dict(os.environ, {"API_ID": "12345", "API_HASH": "0123456789abcdef"}), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")), \
                mock.patch.object(deploy.getpass, "getpass", side_effect=AssertionError("asked")), \
                contextlib.redirect_stdout(out):
            deploy.main()
        return out.getvalue()

    def first_deploy(self, *more):
        return self.run_it("--apply", "--session-file", str(self.session_file), *more)

    def with_signin(self):
        return self.first_deploy("--signin-file", str(self.signin_file))

    def param(self, key):
        return self.aws.params.get(f"/telegram-mcp/{key}")

    def test_a_dry_run_changes_nothing(self):
        for argv in ([], ["--session-file", str(self.session_file)], ["--signin-file", str(self.signin_file)]):
            with self.subTest(argv=argv):
                self.aws = Aws()
                out = self.run_it("--session-file", str(self.session_file), *argv)
                self.assertEqual(self.aws.writes(), [])
                self.assertIn("dry run: nothing changed", out)

    def test_a_dry_run_on_a_deployment_changes_nothing_either(self):
        self.with_signin()
        self.aws.calls.clear()
        self.run_it("--rotate-signing-key", "--rotate-token", "--send-limit", "5")
        self.assertEqual(self.aws.writes(), [])
        self.run_it("--no-token")
        self.assertEqual(self.aws.writes(), [])

    def test_a_first_deploy_with_sign_in_makes_no_token(self):
        self.with_signin()
        self.assertEqual(set(self.aws.params), {f"/telegram-mcp/{k}" for k in (
            "api-id", "api-hash", "session", "login-password-hash", "totp-secret", "signing-key")})
        self.assertEqual((self.param("session"), self.param("totp-secret")), (SESSION, TOTP))

    def test_only_the_api_id_is_stored_in_the_clear(self):
        self.first_deploy()
        self.assertEqual({k for k, v in self.aws.kinds.items() if v != "SecureString"}, {"/telegram-mcp/api-id"})

    def test_a_first_deploy_without_sign_in_makes_a_token(self):
        self.first_deploy()
        self.assertGreaterEqual(len(self.param("auth-token")), 32)

    def test_the_role_comes_before_any_secret_and_secrets_before_the_function(self):
        self.with_signin()
        ops = self.aws.ops()
        self.assertLess(ops.index("put_role_policy"), ops.index("put_parameter"))
        last_secret = len(ops) - 1 - ops[::-1].index("put_parameter")
        self.assertLess(last_secret, ops.index("create_function"))

    def test_a_first_deploy_ends_with_the_url_the_server_expects(self):
        out = self.with_signin()
        self.assertEqual(self.aws.env["PUBLIC_URL"], FURL.rstrip("/"))
        self.assertEqual(self.aws.env["ALLOWED_HOSTS"], "abc123.lambda-url.us-east-1.on.aws")
        self.assertEqual(self.aws.env["SSM_PREFIX"], "/telegram-mcp")
        self.assertEqual(self.aws.env["SEND_LIMIT_PER_HOUR"], "20")
        self.assertEqual(self.aws.sids, {"public-function-url", "public-function-url-invoke"})
        self.assertEqual((self.aws.reserved, self.aws.log_days), (2, 30))
        self.assertIn(f"the connector URL is {FURL.rstrip('/')}/mcp", out)

    def test_a_second_run_changes_nothing(self):
        self.with_signin()
        self.aws.calls.clear()
        out = self.run_it("--apply")
        self.assertEqual(self.aws.writes(), [])
        self.assertIn("function code unchanged", out)
        self.assertIn("function settings unchanged", out)

    def test_new_code_is_uploaded_and_only_new_code(self):
        self.with_signin()
        self.aws.function["CodeSha256"] = "sha-0"
        self.aws.calls.clear()
        self.run_it("--apply")
        self.assertEqual(self.aws.ops(), ["update_function_code"])

    def test_no_secret_is_ever_printed(self):
        printed = self.with_signin()
        token = "t" * 43
        printed += self.run_it("--apply", "--rotate-token", "--rotate-signing-key")
        self.aws.params["/telegram-mcp/auth-token"] = token
        printed += self.run_it("--url", "token")
        for secret in (SESSION, HASH, TOTP, KEY, token, self.param("signing-key"), self.param("api-hash")):
            self.assertNotIn(secret, printed)

    def test_a_secret_change_starts_fresh_containers(self):
        self.with_signin()
        self.assertEqual(self.aws.env["SECRETS_VERSION"], "1")
        self.run_it("--apply")
        self.assertEqual(self.aws.env["SECRETS_VERSION"], "1")
        self.run_it("--apply", "--rotate-signing-key")
        self.assertEqual(self.aws.env["SECRETS_VERSION"], "2")

    def test_rotation_replaces_the_value(self):
        self.first_deploy()
        old = self.param("auth-token")
        self.run_it("--apply", "--rotate-token")
        self.assertNotEqual(self.param("auth-token"), old)
        self.assertIs([c[2]["Overwrite"] for c in self.aws.calls if c[1] == "put_parameter"][-1], True)

    def test_the_way_in_is_never_turned_off(self):
        self.first_deploy()
        self.aws.calls.clear()
        with self.assertRaises(SystemExit):
            self.run_it("--apply", "--no-token")
        self.assertNotIn("delete_parameter", self.aws.ops())
        self.assertIsNotNone(self.param("auth-token"))

    def test_no_token_with_sign_in_deletes_it(self):
        self.with_signin()
        self.run_it("--apply", "--rotate-token")
        self.run_it("--apply", "--no-token")
        self.assertIsNone(self.param("auth-token"))
        self.assertIsNotNone(self.param("signing-key"))

    def test_refused_flags_write_no_secret(self):
        self.with_signin()
        short = self.dir / "short.txt"
        short.write_text("not a session")
        for argv in (["--no-token", "--rotate-token"], ["--session-file", str(short)]):
            with self.subTest(argv=argv):
                self.aws.calls.clear()
                with self.assertRaises(SystemExit):
                    self.run_it("--apply", *argv)
                self.assertEqual(self.aws.ops(), [])

    def test_a_first_deploy_needs_a_session(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_it("--apply")
        self.assertIn("no Telegram session", str(cm.exception))
        self.assertNotIn("put_parameter", self.aws.ops())
        self.assertIsNone(self.aws.function)

    def test_rotating_the_signing_key_needs_sign_in(self):
        self.first_deploy()
        with self.assertRaises(SystemExit):
            self.run_it("--apply", "--rotate-signing-key")

    def test_a_half_written_signin_file_is_refused(self):
        for text in (f"LOGIN_PASSWORD_HASH={HASH}\nTOTP_SECRET={TOTP}\n",            # one missing
                     f"LOGIN_PASSWORD_HASH=plain\nTOTP_SECRET={TOTP}\nSIGNING_KEY={KEY}\n",
                     f"LOGIN_PASSWORD_HASH={HASH}\nTOTP_SECRET={TOTP}\nSIGNING_KEY=short\n"):
            with self.subTest(text=text):
                self.signin_file.write_text(text)
                with self.assertRaises(SystemExit):
                    self.with_signin()

    def test_a_token_url_goes_to_the_clipboard_and_never_the_screen(self):
        self.first_deploy()
        out = self.run_it("--url")
        token = self.param("auth-token")
        self.assertEqual(self.clipboard, [f"{FURL.rstrip('/')}/{token}/mcp"])
        self.assertNotIn(token, out)

    def test_without_a_clipboard_it_goes_to_a_file(self):
        self.first_deploy()
        with mock.patch.object(deploy.shutil, "which", lambda _: None):
            self.assertFalse(deploy.to_clipboard("x"))
        token = self.param("auth-token")
        out = io.StringIO()
        with mock.patch.object(deploy.boto3, "Session", lambda **kw: self.aws), \
                mock.patch.object(deploy, "ROOT", self.dir), mock.patch.object(deploy, "to_clipboard", lambda t: False), \
                mock.patch.object(sys, "argv", ["deploy.py", "--url"]), contextlib.redirect_stdout(out):
            deploy.main()
        self.assertEqual((self.dir / "data" / "connector-url.txt").read_text(), f"{FURL.rstrip('/')}/{token}/mcp\n")
        self.assertNotIn(token, out.getvalue())

    def test_a_sign_in_url_holds_no_secret(self):
        self.with_signin()
        out = self.run_it("--url")
        self.assertIn(f"{FURL.rstrip('/')}/mcp", out)
        self.assertEqual(self.clipboard, [f"{FURL.rstrip('/')}/mcp"])

    def test_url_without_what_it_needs_says_so(self):
        with self.assertRaises(SystemExit):
            self.run_it("--url")
        self.with_signin()
        with self.assertRaises(SystemExit):
            self.run_it("--url", "token")
        self.aws = Aws()
        self.first_deploy()
        with self.assertRaises(SystemExit):
            self.run_it("--url", "signin")

    def test_send_limit_and_blocked_chats_are_kept_until_changed(self):
        self.first_deploy("--send-limit", "5", "--blocked-chats", "-1001")
        self.run_it("--apply")
        self.assertEqual((self.aws.env["SEND_LIMIT_PER_HOUR"], self.aws.env["BLOCKED_CHATS"]), ("5", "-1001"))
        self.run_it("--apply", "--blocked-chats", "")
        self.assertEqual(self.aws.env["BLOCKED_CHATS"], "")

    # deploy.py:328 tests args.send_limit for truth, so 0, the way to stop all sends, keeps the old limit and says
    # nothing. BLOCKED_CHATS, the line below it, tests `is not None`.
    @unittest.expectedFailure
    def test_a_send_limit_of_zero_is_taken(self):
        self.first_deploy("--send-limit", "5")
        self.run_it("--apply", "--send-limit", "0")
        self.assertEqual(self.aws.env["SEND_LIMIT_PER_HOUR"], "0")

    def test_credentials_that_may_not_manage_iam_stop_before_any_secret(self):
        self.aws.deny = {"create_role"}
        with self.assertRaises(SystemExit) as cm:
            self.first_deploy()
        self.assertIn("--only-role", str(cm.exception))
        self.assertEqual(self.aws.params, {})

    def test_only_role_touches_only_the_role(self):
        self.run_it("--apply", "--only-role")
        self.assertEqual({s for s, _, _ in self.aws.writes()}, {"iam"})
        self.assertEqual(self.aws.role_doc, deploy.role_policy(ACCOUNT, REGION, "telegram-mcp"))

    def test_only_role_says_when_it_may_not(self):
        self.aws.deny = {"create_role"}
        with self.assertRaises(SystemExit):
            self.run_it("--apply", "--only-role")

    def test_a_drifted_role_policy_is_put_back(self):
        self.first_deploy()
        self.aws.role_doc = {"Statement": "*"}
        self.run_it("--apply")
        self.assertEqual(self.aws.role_doc, deploy.role_policy(ACCOUNT, REGION, "telegram-mcp"))

    def test_a_role_that_cant_be_read_is_not_taken_as_missing(self):
        def denied(**kw):
            raise error("AccessDenied")
        self.aws.iam_get_role = denied
        with self.assertRaises(deploy.ClientError):
            self.first_deploy()
        self.assertEqual(self.aws.params, {})

    def test_it_waits_for_a_role_lambda_cant_use_yet(self):
        self.aws.function_failures = ["InvalidParameterValueException"] * 3
        self.first_deploy()
        self.assertIsNotNone(self.aws.function)

    def test_other_create_errors_stop_it(self):
        self.aws.function_failures = ["ResourceConflictException"]
        with self.assertRaises(deploy.ClientError):
            self.first_deploy()

    def test_a_low_concurrency_quota_isnt_fatal(self):
        self.aws.deny = {"put_function_concurrency"}
        out = self.first_deploy()
        self.assertIn("could not reserve concurrency", out)
        self.assertIn("done:", out)

    def test_names_are_kept_to_what_aws_allows(self):
        for name in ("a/b", "x" * 51, "", "a b"):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                self.run_it("--name", name)
        self.aws = Aws()
        self.run_it("--apply", "--session-file", str(self.session_file), "--name", "second-copy")
        self.assertEqual(self.aws.env["SSM_PREFIX"], "/second-copy")
        self.assertTrue(all(k.startswith("/second-copy/") for k in self.aws.params))
        self.assertIn("/second-copy/", self.aws.role_doc["Statement"][1]["Resource"])


class Policies(unittest.TestCase):
    def test_the_role_reads_only_its_own_parameters_and_logs(self):
        statements = {s["Sid"]: s for s in deploy.role_policy(ACCOUNT, REGION, "copy")["Statement"]}
        self.assertEqual(statements["ReadItsParameters"]["Resource"], f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/copy/*")
        self.assertEqual(statements["ItsLogs"]["Resource"], f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/copy:*")
        self.assertEqual(statements["DecryptThemThroughSsm"]["Condition"],
                         {"StringEquals": {"kms:ViaService": f"ssm.{REGION}.amazonaws.com"}})
        self.assertNotIn("ssm:*", json.dumps(statements))

    def test_the_deploy_policy_allows_every_call_a_deploy_makes(self):
        """aws/deploy-policy.json is for credentials that can't manage IAM, so what the role needs is left out."""
        run = Deploy("test_a_second_run_changes_nothing")
        run.setUp()
        run.aws.role_doc = deploy.role_policy(ACCOUNT, REGION, "telegram-mcp")   # someone else made the role
        run.with_signin()
        run.run_it("--apply", "--rotate-token")
        run.run_it("--apply", "--no-token")
        run.run_it("--url")
        run.aws.function["CodeSha256"] = "sha-0"
        run.aws.function["Timeout"] = 1
        run.run_it("--apply")
        policy = json.loads((deploy.ROOT / "aws" / "deploy-policy.json").read_text(encoding="utf-8"))
        allowed = {a for s in policy["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
        prefix = {"lambda": "lambda", "ssm": "ssm", "iam": "iam", "logs": "logs"}
        needed = {f"{prefix[s]}:{''.join(w.title() for w in op.split('_'))}" for s, op, _ in run.aws.calls
                  if s in prefix and op != "wait"}
        self.assertEqual(needed - allowed, set())
        self.assertIn("iam:PassRole", allowed)   # create_function passes the role


class BuildZip(unittest.TestCase):
    def build(self):
        """build_zip with pip replaced by a fake site-packages, from a checkout holding things that must not ship."""
        def pip(cmd, check):
            self.assertTrue(check)
            self.assertIn("--only-binary=:all:", cmd)
            self.assertIn("--no-deps", cmd)
            target = Path(cmd[cmd.index("--target") + 1])
            for name in ("telethon/__init__.py", "bin/uvicorn.exe", "pkg-1.0.dist-info/RECORD",
                         "pkg-1.0.dist-info/METADATA", "x/__pycache__/x.pyc"):
                (target / name).parent.mkdir(parents=True, exist_ok=True)
                (target / name).write_bytes(os.urandom(8) if name.endswith((".exe", "RECORD")) else b"same")
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy.subprocess, "run", pip):
            return deploy.build_zip()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for name in ("server.py", "oauth.py", "lambda_handler.py", "login.py", "deploy.py", ".env", "requirements.txt"):
            (self.root / name).write_text("# " + name)
        (self.root / "data").mkdir()
        (self.root / "data" / "session.txt").write_text(SESSION)

    def test_only_the_server_goes_in(self):
        data, _ = self.build()
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertEqual(names, {"server.py", "oauth.py", "lambda_handler.py", "telethon/__init__.py",
                                 "pkg-1.0.dist-info/METADATA"})

    def test_the_same_code_makes_the_same_hash(self):
        first, second = self.build(), self.build()
        self.assertEqual(first, second)   # so a redeploy of unchanged code is "function code unchanged"
        self.assertEqual(first[1], base64.b64encode(hashlib.sha256(first[0]).digest()).decode())   # as Lambda gives it

    def test_a_changed_file_changes_the_hash(self):
        first = self.build()
        (self.root / "server.py").write_text("# changed")
        self.assertNotEqual(first[1], self.build()[1])


class Dotenv(unittest.TestCase):
    def test_the_environment_wins_and_only_the_two_keys_are_read(self):
        root = Path(tempfile.mkdtemp())
        (root / ".env").write_text("# API_ID=1\nAPI_ID='777'\nAPI_HASH=\"abc\"\nAUTH_TOKEN=secret\n")
        with mock.patch.object(deploy, "ROOT", root), mock.patch.dict(os.environ):
            os.environ.pop("API_ID", None)
            os.environ.pop("API_HASH", None)
            self.assertEqual(deploy.dotenv(), {"API_ID": "777", "API_HASH": "abc"})
            os.environ["API_ID"] = "9"
            self.assertEqual(deploy.dotenv()["API_ID"], "9")


if __name__ == "__main__":
    unittest.main()
