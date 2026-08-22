# Q2162: ReceivePack: A pktline frame declaring a length far larger than its payload

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ReceivePack` in `internal/gitaly/service/smarthttp/receive_pack.go` by supplying a pktline frame declaring a length far larger than its payload, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically frame length is bounded and validated against payload — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/receive_pack.go` -> `ReceivePack`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply a pktline frame declaring a length far larger than its payload; if `ReceivePack` uses it without enforcing that frame length is bounded and validated against payload, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Fuzz pktline parser asserting bounded allocation.
