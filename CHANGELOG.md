# Changelog

## 0.7.0 — unreleased candidate

- Add an optional personal-agent service with common peer discovery, scoped conversations and correlated replies. First native adapters: Grokbot routines and Dot MCP Events.
- Keep personal messages, OAuth grants and subscriptions in a separate private store; do not change local Codex/Claude session delivery or the private Drive adapter.
- Add peer-bound credentials, owner-approved connections, explicit consumption, atomic processing claims, bounded notifications and uncertainty-preserving recovery.
- Add local contract and loopback HTTP tests. Live personal-agent acceptance remains pending; Muse and Duet are not yet implemented.

## 0.6.1

- Add explicit desktop wake opt-in: open an unloaded Codex destination using its native app link, verify its receiver, then deliver peer tool output. The app may select that chat.
- Keep discovery read-only and preserve message provenance, exact destination IDs and no automatic replay after uncertainty. No new background service or terminal takeover.
- Allow unique unloaded names when desktop wake is enabled; refuse eligible duplicate names.
- Let normal desktop discovery finish before treating its missing-owner response as a timeout.

## 0.6.0 — first public release candidate

- Introduce agentpeers: local Codex terminal ↔ Claude Code terminal discovery, scoped messages, correlated replies and explicit read receipts.
- Connect Codex terminal or desktop sessions with Claude Code terminal sessions; document the tested versions and distinguish a running app from a compatible receiving chat.
- Support exact session names, deliberate named replacements, pinned session IDs, safe file/stdin input, response expectations and one blocking question from an explicitly assigned worker.
- Preserve uncertain delivery outcomes without automatic replay. Expose saved mailbox replies and sender return-route warnings.
- Package native plugins and guided setup for both clients with Python 3.10+ and no third-party Python runtime dependencies.
- Retain existing external message stores and environment settings when upgrading from Peerbridge.

Cross-machine transport and automatic wake-up of closed/unloaded chats are outside this release.
