# Q3614: UploadPack: A pack with a decompression bomb or huge delta chain

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `UploadPack` in `internal/gitaly/service/smarthttp/upload_pack.go` by supplying a pack with a decompression bomb or huge delta chain, so that a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash is violated — specifically object inflation is bounded — leading to dos / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input?

## Target
- File/function: `internal/gitaly/service/smarthttp/upload_pack.go` -> `UploadPack`
- Entrypoint: smarthttp/ssh upload-pack, receive-pack, info-refs, and upload-archive
- Attacker controls: the pktline-framed request stream, negotiation refs/wants/haves, and pack contents
- Exploit idea: Supply a pack with a decompression bomb or huge delta chain; if `UploadPack` uses it without enforcing that object inflation is bounded, the request escapes the intended boundary.
- Invariant to test: a malformed or oversized stream is bounded and rejected without unbounded memory/CPU/disk or a process/partition crash.
- Expected Immunefi impact: (GitLab HackerOne class) DoS / resource exhaustion or handler crash on the default configuration via a crafted pack, pktline stream, or negotiation input.
- Fast validation: Test upload/receive-pack against a crafted pack asserting limits.
