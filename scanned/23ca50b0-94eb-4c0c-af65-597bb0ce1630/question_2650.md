# Q2650: ReceivePack: A negotiation causing catastrophic CPU in ref matching

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ReceivePack` in `internal/gitaly/service/smarthttp/receive_pack.go` by supplying a negotiation causing catastrophic CPU in ref matching, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically ref matching is linear/bounded in input size — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/receive_pack.go` -> `ReceivePack`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply a negotiation causing catastrophic CPU in ref matching; if `ReceivePack` uses it without enforcing that ref matching is linear/bounded in input size, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Microbenchmark/fuzz on negotiation ref handling.
