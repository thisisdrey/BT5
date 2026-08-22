# Q0701: sendTreeEntry: An archive prefix that collides with an absolute path

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `sendTreeEntry` in `internal/gitaly/service/commit/tree_entry.go` by supplying an archive prefix that collides with an absolute path, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically archive prefixes are treated as in-tree only — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/commit/tree_entry.go` -> `sendTreeEntry`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply an archive prefix that collides with an absolute path; if `sendTreeEntry` uses it without enforcing that archive prefixes are treated as in-tree only, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test tar_builder prefix handling.
