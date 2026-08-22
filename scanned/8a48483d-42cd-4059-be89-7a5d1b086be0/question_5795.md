# Q5795: OptionArgs: A value injecting an extra '-c <config>' pair into the command

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `OptionArgs` in `internal/git/gitcmd/staticargs.go` (via the gRPC unary request) by supplying a value injecting an extra '-c <config>' pair into the command, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically user input cannot inject -c config keys — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/gitcmd/staticargs.go` -> `OptionArgs`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git (via the gRPC unary request)
- Exploit idea: Supply a value injecting an extra '-c <config>' pair into the command; if `OptionArgs` uses it without enforcing that user input cannot inject -c config keys, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test command_options rejecting embedded config flags.
