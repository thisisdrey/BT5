# Q5084: WithConfigEnv: A ref name containing whitespace/newlines that splits into extra argv entri

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `WithConfigEnv` in `internal/git/gitcmd/command_options.go` by supplying a ref name containing whitespace/newlines that splits into extra argv entries, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically one field maps to exactly one argv element — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/gitcmd/command_options.go` -> `WithConfigEnv`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a ref name containing whitespace/newlines that splits into extra argv entries; if `WithConfigEnv` uses it without enforcing that one field maps to exactly one argv element, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Fuzz ref names asserting argv arity is stable.
