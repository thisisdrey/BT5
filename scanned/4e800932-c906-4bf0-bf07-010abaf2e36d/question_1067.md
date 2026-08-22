# Q1067: validateRawBlameRequest: A search-files pattern causing traversal or reading arbitrary fil

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateRawBlameRequest` in `internal/gitaly/service/commit/raw_blame.go` by supplying a search-files pattern causing traversal or reading arbitrary files, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically search operates only within the repo working set — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/commit/raw_blame.go` -> `validateRawBlameRequest`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply a search-files pattern causing traversal or reading arbitrary files; if `validateRawBlameRequest` uses it without enforcing that search operates only within the repo working set, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test SearchFilesByContent/Name path scope.
