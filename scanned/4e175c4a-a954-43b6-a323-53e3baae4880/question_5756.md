# Q5756: validateGetTreeEntriesRequest: A TreeEntry/GetBlob path resolving outside the revision's t

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateGetTreeEntriesRequest` in `internal/gitaly/service/commit/get_tree_entries.go` (via the gRPC unary request) by supplying a TreeEntry/GetBlob path resolving outside the revision's tree, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically path-scoped reads never touch the filesystem directly — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/commit/get_tree_entries.go` -> `validateGetTreeEntriesRequest`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors (via the gRPC unary request)
- Exploit idea: Supply a TreeEntry/GetBlob path resolving outside the revision's tree; if `validateGetTreeEntriesRequest` uses it without enforcing that path-scoped reads never touch the filesystem directly, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test tree_entry/get_blob path handling.
