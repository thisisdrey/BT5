# Q0290: Error: A remote/URL using the 'ext::' or 'fd::' transport helper

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Error` in `internal/git/object_id.go` by supplying a remote/URL using the 'ext::' or 'fd::' transport helper, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically external transport helpers are refused — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/object_id.go` -> `Error`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a remote/URL using the 'ext::' or 'fd::' transport helper; if `Error` uses it without enforcing that external transport helpers are refused, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test on URL handling rejecting ext:: schemes.
