# Q1525: archive: An archive path selector with '../' escaping the requested tree

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `archive` in `internal/archive/archive.go` by supplying an archive path selector with '../' escaping the requested tree, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically archive contents come only from the requested tree — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/archive/archive.go` -> `archive`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply an archive path selector with '../' escaping the requested tree; if `archive` uses it without enforcing that archive contents come only from the requested tree, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test GetArchive with a traversing path selector.
