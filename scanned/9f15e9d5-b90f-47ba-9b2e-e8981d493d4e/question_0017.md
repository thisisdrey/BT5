# Q0017: Error: A revision beginning with '-' so git parses it as a flag

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `Error` in `internal/git/object_id.go` by supplying a revision beginning with '-' so git parses it as a flag, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically the revision is passed after '--' or rejected as an option — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/object_id.go` -> `Error`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a revision beginning with '-' so git parses it as a flag; if `Error` uses it without enforcing that the revision is passed after '--' or rejected as an option, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test ValidateRevision / command assembly with a leading-dash revision.
