# Q5481: WithCompletionErrorLogFilter: A revision or ref of the form '--upload-pack=<cmd>' or '--ou

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `WithCompletionErrorLogFilter` in `internal/git/gitcmd/command_options.go` by supplying a revision or ref of the form '--upload-pack=<cmd>' or '--output=<file>', so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically dangerous long-options never reach the git argv — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/gitcmd/command_options.go` -> `WithCompletionErrorLogFilter`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a revision or ref of the form '--upload-pack=<cmd>' or '--output=<file>'; if `WithCompletionErrorLogFilter` uses it without enforcing that dangerous long-options never reach the git argv, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Unit test asserting the option is rejected or neutralized.
