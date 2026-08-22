# Q3019: sortTrees: A match_walker pattern following symlinks out of the archive root

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `sortTrees` in `internal/gitaly/service/commit/get_tree_entries.go` by supplying a match_walker pattern following symlinks out of the archive root, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically archive walk does not follow escaping symlinks — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/commit/get_tree_entries.go` -> `sortTrees`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply a match_walker pattern following symlinks out of the archive root; if `sortTrees` uses it without enforcing that archive walk does not follow escaping symlinks, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test match_walker symlink handling.
