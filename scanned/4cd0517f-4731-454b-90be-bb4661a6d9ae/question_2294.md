# Q2294: ssh: A config value assembled into git -c that contains attacker-controlled key=value

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `ssh` in `internal/git/gitcmd/ssh.go` by supplying a config value assembled into git -c that contains attacker-controlled key=value, so that user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers is violated — specifically config assembly escapes/validates keys and values — leading to git argument/config injection or command execution?

## Target
- File/function: `internal/git/gitcmd/ssh.go` -> `ssh`
- Entrypoint: any RPC whose revision/ref/path/URL is forwarded to a spawned git process
- Attacker controls: revisions, ref names, path operands, and remote URLs passed to git
- Exploit idea: Supply a config value assembled into git -c that contains attacker-controlled key=value; if `ssh` uses it without enforcing that config assembly escapes/validates keys and values, the request escapes the intended boundary.
- Invariant to test: user-controlled revisions, refs, paths and URLs are passed as operands only, never interpretable as options, -c config, or transport helpers.
- Expected Immunefi impact: (GitLab HackerOne class) Git argument/config injection or command execution (leading '-', --upload-pack=, --output=, -c, ext::) letting Gitaly run attacker-chosen code or read attacker-chosen files.
- Fast validation: Test config injection into the git invocation.
