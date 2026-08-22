# Q5218: WithGlobalOption: A path-scoped revision (HEAD:path) whose path portion carries option-lik

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `WithGlobalOption` in `internal/git/gitcmd/command_options.go` by supplying a path-scoped revision (HEAD:path) whose path portion carries option-like bytes, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically path-scoped operands cannot smuggle options — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/gitcmd/command_options.go` -> `WithGlobalOption`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a path-scoped revision (HEAD:path) whose path portion carries option-like bytes; if `WithGlobalOption` uses it without enforcing that path-scoped operands cannot smuggle options, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test AllowPathScopedRevision boundaries.
