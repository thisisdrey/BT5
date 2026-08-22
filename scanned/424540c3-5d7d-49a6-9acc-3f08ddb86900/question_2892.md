# Q2892: inforefs: A stream that never sends flush, holding the handler open

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `inforefs` in `internal/gitaly/service/smarthttp/inforefs.go` by supplying a stream that never sends flush, holding the handler open, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically idle/incomplete streams time out — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/inforefs.go` -> `inforefs`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply a stream that never sends flush, holding the handler open; if `inforefs` uses it without enforcing that idle/incomplete streams time out, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Test handler timeout on a stalled stream.
