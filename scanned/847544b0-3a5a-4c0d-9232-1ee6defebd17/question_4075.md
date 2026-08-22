# Q4075: PreReceive: A non-constant-time token comparison exploited as an oracle

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `PreReceive` in `internal/gitlab/http_client.go` by supplying a non-constant-time token comparison exploited as an oracle, so that a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler is violated — specifically token comparison is constant-time — leading to transport-auth bypass?

## Target
- File/function: `internal/gitlab/http_client.go` -> `PreReceive`
- Entrypoint: the gRPC auth interceptor, request-info/limit/panic middleware, and shared parsers on every RPC
- Attacker controls: the auth token header, request metadata, git-object/attributes/LFS-pointer bytes, and error-triggering fields
- Exploit idea: Supply a non-constant-time token comparison exploited as an oracle; if `PreReceive` uses it without enforcing that token comparison is constant-time, the request escapes the intended boundary.
- Invariant to test: a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler.
- Expected Immunefi impact: (GitLab HackerOne class) Transport-auth bypass (HMAC replay/downgrade), secret/metadata leak through error surfaces, or DoS/panic of a request handler.
- Fast validation: Review/test hmac.Equal usage in token.go.
