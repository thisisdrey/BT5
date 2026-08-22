# Q0609: GetFileAttributes: A raw-blame/tree path with encoded separators bypassing scope checks

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `GetFileAttributes` in `internal/gitaly/service/repository/file_attributes.go` by supplying a raw-blame/tree path with encoded separators bypassing scope checks, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically encoded paths do not bypass the tree scope — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/repository/file_attributes.go` -> `GetFileAttributes`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply a raw-blame/tree path with encoded separators bypassing scope checks; if `GetFileAttributes` uses it without enforcing that encoded paths do not bypass the tree scope, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Fuzz path operands for the read RPC.
