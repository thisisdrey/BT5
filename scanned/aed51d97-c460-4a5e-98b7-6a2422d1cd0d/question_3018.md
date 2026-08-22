# Q3018: validateSearchFilesRequest: A match_walker pattern following symlinks out of the archive r

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `validateSearchFilesRequest` in `internal/gitaly/service/repository/search_files.go` by supplying a match_walker pattern following symlinks out of the archive root, so that a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository is violated — specifically archive walk does not follow escaping symlinks — leading to unintended file disclosure via path traversal in a path-scoped read/archive rpc, reading files outside the requested tree or repository?

## Target
- File/function: `internal/gitaly/service/repository/search_files.go` -> `validateSearchFilesRequest`
- Entrypoint: GetArchive, GetSnapshot, SearchFilesByContent, TreeEntry, GetBlob and related read RPCs
- Attacker controls: path/prefix operands, revision, tree paths, and archive path selectors
- Exploit idea: Supply a match_walker pattern following symlinks out of the archive root; if `validateSearchFilesRequest` uses it without enforcing that archive walk does not follow escaping symlinks, the request escapes the intended boundary.
- Invariant to test: a path-scoped read serves only blobs reachable under the requested revision/prefix and never escapes to the filesystem or another repository.
- Expected Immunefi impact: (GitLab HackerOne class) Unintended file disclosure via path traversal in a path-scoped read/archive RPC, reading files outside the requested tree or repository.
- Fast validation: Test match_walker symlink handling.
