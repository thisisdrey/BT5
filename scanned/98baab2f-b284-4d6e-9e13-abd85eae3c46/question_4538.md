# Q4538: WithConcurrencyLimiters: A sidechannel/backchannel negotiation abused to bypass auth scopi

## Question
Can an unprivileged GitLab user (no special role) who can push/fetch, fork or import a repository they own, and thereby drive Gitaly RPCs with attacker-chosen fields and repository content reach `WithConcurrencyLimiters` in `internal/grpc/middleware/limithandler/middleware.go` by supplying a sidechannel/backchannel negotiation abused to bypass auth scoping, so that a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler is violated — specifically side/backchannel connections inherit the auth scope — leading to transport-auth bypass?

## Target
- File/function: `internal/grpc/middleware/limithandler/middleware.go` -> `WithConcurrencyLimiters`
- Entrypoint: the gRPC auth interceptor, request-info/limit/panic middleware, and shared parsers on every RPC
- Attacker controls: the auth token header, request metadata, git-object/attributes/LFS-pointer bytes, and error-triggering fields
- Exploit idea: Supply a sidechannel/backchannel negotiation abused to bypass auth scoping; if `WithConcurrencyLimiters` uses it without enforcing that side/backchannel connections inherit the auth scope, the request escapes the intended boundary.
- Invariant to test: a request is authenticated only with a fresh valid token, secrets and absolute paths never survive into a readable error, and malformed parser input cannot crash or hang the handler.
- Expected Immunefi impact: (GitLab HackerOne class) Transport-auth bypass (HMAC replay/downgrade), secret/metadata leak through error surfaces, or DoS/panic of a request handler.
- Fast validation: Test sidechannel/backchannel auth propagation.
