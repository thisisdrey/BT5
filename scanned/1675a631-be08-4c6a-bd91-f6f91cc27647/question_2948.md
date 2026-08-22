# Q2948: validateUploadPackRequest: An upload-archive request with a pathologically deep/large tree

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateUploadPackRequest` in `internal/gitaly/service/smarthttp/upload_pack.go` by supplying an upload-archive request with a pathologically deep/large tree request, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically archive work is bounded per request — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/upload_pack.go` -> `validateUploadPackRequest`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply an upload-archive request with a pathologically deep/large tree request; if `validateUploadPackRequest` uses it without enforcing that archive work is bounded per request, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Test upload-archive resource bounds.
