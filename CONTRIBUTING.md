# Contributing to Agentpeers

Agentpeers connects existing Codex terminal or desktop sessions with Claude Code terminal sessions on one Mac. Keep changes focused on discovery, message delivery, receipts and understandable setup. Discuss new runtimes, remote transport or orchestration features in an issue before adding them.

## Development

Clone the repository and use Python 3.10+:

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

No third-party runtime packages are needed. Tests use temporary stores and fixture sockets; they must not contact live agents or depend on your private client configuration. Some sandboxes prohibit Unix socket binding: run through normal host-access approval rather than weakening protections. GitHub CI covers macOS with Python 3.10 and 3.13. The runtime adapter is currently macOS-only.

For plugin changes, test a relocated copy with spaces in its path, MCP initialization and tools/list, missing Python, and version consistency across both plugin manifests, the server and pyproject.toml. Run `python3 scripts/package.py` after committing; it writes a ZIP and SHA-256 sidecar under ignored `dist/`, using only tracked files from that commit.

## Live verification

Fixture tests do not prove session delivery. After a transport change, obtain human authorization for the exact live peers, then verify a unique request, explicit receiver read, correlated reply, and explicit originating-session read. Also exercise busy/idle and unavailable behavior. Report unsupported paths honestly. Keep local proof under ignored `proof/`; publish only redacted summaries.

Preserve the boundary between peer content and human authority. Replies retain scope; messages cannot authorize new assignments, new destinations or permission approvals. Preserve unrelated work and configuration. Do not read or modify agents' private conversation databases, automatically replay uncertain sends, interrupt turns, take over chats, or fall back to typing peer content as human input.

## Pull requests

Explain the user-visible problem, resulting behavior, relevant client versions and validation. Add meaningful regression coverage where behavior changes. Keep generated packages, databases, credentials, transcripts and personal paths out of Git. Contributions use this repository's MIT license. Contributing does not require assigning copyright to Agenteer LLC.

## Releases

Maintainers review compatibility, live receipts, packaging and the repository's disclosure boundary before a release. CI is regression evidence, not a live-delivery certificate. There is no automated publication workflow; changing repository visibility or publishing a package requires an explicit maintainer decision. Release reviews are tracked in GitHub issues and pull requests.

Before the first public release:

1. Review the exact candidate commit and align the default branch with it, so the documented Git installation gets the reviewed version.
2. Run regression tests and the tracked-source packager. Test installation, update and removal with separate temporary client configuration directories; preserve real client settings and message stores.
3. Record a fresh live terminal exchange on the client versions advertised for release, with explicit reads in both directions. Verify busy, idle and unavailable behavior without interrupting existing work. State desktop receiving requirements and separate earlier acceptance evidence from current checks.
4. Review all reachable Git history and the GitHub material that will become visible: issues/comments, pull requests, Actions logs/artifacts and releases. Preserve private trial evidence outside the public export. Do not upload an entire working directory or discard history without a concrete disclosure reason.
5. Present the release scope, exact commit, verification results and remaining limits for the maintainer's final publication decision. Do not treat a scheduled review date as approval.
