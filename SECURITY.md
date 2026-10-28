# Security

## Reporting a vulnerability

Please report a security problem privately, not in a public issue. This project lives on [GitLab](https://gitlab.com/sstangle73/telegram-mcp) and is mirrored to GitHub, and either route works:
- **[Report a vulnerability](https://github.com/sstangle73/telegram-mcp/security/advisories/new)** on the GitHub mirror's *Security* tab, or
- a [GitLab issue](https://gitlab.com/sstangle73/telegram-mcp/-/issues/new) marked **confidential**.

Only you and the maintainer see either one. Please include as much of this as you can:
- what's affected: the server, the Lambda handler, `login.py` or `deploy.py`
- how to reproduce it, or a proof of concept
- what someone could do with it

## What happens next

- You'll hear back within 7 days.
- Once it's confirmed, it gets fixed, and a GitHub security advisory is published that credits you, unless you'd rather not be named. A CVE is requested through GitHub when the problem warrants one.
- Please keep it private until a fix is out, or for 90 days after your report, whichever comes first.

This is a one-person project with no bug bounty.

## Supported versions

Only `main` gets fixes.

## What's in scope

These matter most:
- **Reaching the tools without the token**, or learning the token from anything the server sends or logs.
- **Getting past a guard:** reading, listing, searching or sending to Telegram's service chat (777000) or a `BLOCKED_CHATS` chat; sending other than through `send_message`, past the hourly limit, or without its Saved Messages copy; formatting or a link preview in a sent message.
- **Leaking the Telegram session, the API hash or the token** in logs, error messages, the Lambda zip or the AWS resources `deploy.py` makes (for example, permissions wider than the role and policy describe).
- **Chat content that changes what a tool does**: names, titles or message text that reach anything other than the tool's output.

Out of scope:
- what someone holding the connector URL, or the AWS account, can do: that is the account
- a send that the user approved in their client, however the model was talked into proposing it (the approval is the control, as the README says)
- denial of service by sheer volume
