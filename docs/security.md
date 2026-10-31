# Security

Hangar gives whoever signs in a shell on your machine, with Claude Code and your Claude login. Treat the password like an SSH key.

- There is one owner account. Its password is stored as a salted scrypt hash and must be at least 10 characters.
- A fresh install can only be set up with the one-time code from Hangar's log, so someone else on the network can't claim it first.
- SSO uses the authorization code flow with PKCE. The `state` is single-use, expires after 10 minutes, and must come back to the browser that started it; the ID token's signature (from the provider's published keys), issuer, audience, expiry, and nonce are all checked, and unsigned tokens are refused. Only the linked identity (issuer + subject) can sign in.
- Sign-in and setup attempts are rate-limited (10 per 5 minutes per address) and the cookie is signed. Changing the password, or "Sign out other devices" in Settings, signs every other device out.
- Don't forward the port to the internet. Use your LAN, Tailscale, or another VPN.
- Every API request needs the login cookie, and requests that change anything also need a custom header, which blocks cross-site requests. Terminal WebSockets check the page origin.
- New sessions, uploads, and downloads are limited to `HANGAR_WORKSPACES`. Downloaded files are served with a sandbox policy, so an uploaded HTML file can't run scripts as Hangar.
- A terminal can still `cd` anywhere the container can see, so only mount what you need.
- In Docker, the container user has passwordless sudo inside the container (unless `ALLOW_SUDO=false`). That gives root in the container, not on the host, but root in the container can still change everything that's mounted.
- Linking GitHub stores a GitHub token in `gh`'s config folder (`~/.config/gh`), readable by anyone signed in to Hangar, the same as the Claude login. Clones go only into `HANGAR_WORKSPACES`, and repo names are checked before git runs.
- Hangar reads Claude Code's OAuth token to check usage. It never refreshes or changes it. It reads Codex's `auth.json` to show which account is linked and to check ChatGPT usage the way Codex does; it never refreshes or changes the token.
- Codex is downloaded from a pinned GitHub release and installed only if its SHA-256 matches the one built into Hangar.
- The Codex browser fallback only accepts `http://localhost:1455/auth/callback` addresses, so it can't be used to make Hangar request anything else.

[Back to the README](../README.md)
