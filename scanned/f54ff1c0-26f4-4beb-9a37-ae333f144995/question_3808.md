# Q3808: Query answered for arbitrary target in dumpCommands

## Question
Can an unauthenticated client use an unauthenticated command line to make `dumpCommands` (sshd/command.go) resolve or act on a target outside the node's own scope?

## Target
- File/function: `sshd/command.go` -> `dumpCommands` (declared at sshd/command.go:57)
- Entrypoint: Unauthenticated TCP/UDP connection to a locally reachable Nebula listener (SSH admin or DNS)
- Attacker controls: an unauthenticated command line; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Issue the request naming an arbitrary overlay address and inspect the result.
- Invariant to test: Requests are answered only for targets the node is authoritative for and the client is entitled to.
- Expected Immunefi impact: Overlay reconnaissance or unauthorized action against a chosen host.
- Fast validation: Table-driven test over out-of-scope targets asserting `dumpCommands` refuses each.
