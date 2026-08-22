# Q2236: recordServedBytes: An unbounded flood of refs/wants/haves in negotiation

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `recordServedBytes` in `internal/gitaly/service/smarthttp/upload_pack.go` by supplying an unbounded flood of refs/wants/haves in negotiation, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically negotiation input is size-limited — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/upload_pack.go` -> `recordServedBytes`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply an unbounded flood of refs/wants/haves in negotiation; if `recordServedBytes` uses it without enforcing that negotiation input is size-limited, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Load test asserting negotiation memory is bounded.
