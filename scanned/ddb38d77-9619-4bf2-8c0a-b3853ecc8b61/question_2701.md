# Q2701: ValidateRevision: An object ID that is not a valid hash but still forwarded to git

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ValidateRevision` in `internal/git/revision.go` by supplying an object ID that is not a valid hash but still forwarded to git, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically object IDs are validated before use as operands — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/revision.go` -> `ValidateRevision`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply an object ID that is not a valid hash but still forwarded to git; if `ValidateRevision` uses it without enforcing that object IDs are validated before use as operands, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test object_id validation on malformed input.
