# Q2591: upload_command: Malformed pktline that triggers a panic in the parser

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `upload_command` in `internal/gitaly/service/ssh/upload_command.go` by supplying malformed pktline that triggers a panic in the parser, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically malformed frames return an error, never panic — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/ssh/upload_command.go` -> `upload_command`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply malformed pktline that triggers a panic in the parser; if `upload_command` uses it without enforcing that malformed frames return an error, never panic, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Fuzz pktline asserting no panic.
