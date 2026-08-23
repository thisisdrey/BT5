### Title
FetchRemote RPC allows unprivileged callers to disable the fetch timeout entirely, enabling handler-resource DoS - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
The `FetchRemote` RPC only applies a context deadline to the fetch operation when the caller-supplied `req.GetTimeout()` field is a positive value. Because this field is entirely user-controlled and the RPC provides no server-side floor/ceiling or mandatory default, a caller can bypass the intended time-bound guard by omitting the field or sending `0`/a negative value, causing the fetch to run with no enforced deadline.

### Finding Description
`FetchRemote` conditionally wraps the context with a timeout: [1](#0-0) 

```go
func (s *server) FetchRemote(ctx context.Context, req *gitalypb.FetchRemoteRequest) (*gitalypb.FetchRemoteResponse, error) {
	if err := s.validateFetchRemoteRequest(ctx, req); err != nil {
		return nil, err
	}

	if req.GetTimeout() > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.GetTimeout())*time.Second)
		defer cancel()
	}
```

This is structurally the same bug class as the Sherlock finding: a guard meant to bound an operation's lifetime (`judgeExpired` in the DODO report) is gated on a value fully controlled by the caller, with the "safe"/expired branch reachable simply by choosing a value (`timeout <= 0`) that skips the check. Here, instead of a deadline being bypassed to allow a stale/expired action, the deadline itself is bypassed, allowing the underlying `git fetch` (invoked via `quarantineRepo.FetchRemote` on an attacker-supplied remote URL/SSH key) to run unbounded on the Gitaly node.

### Impact Explanation
Any caller of `FetchRemote` (an ordinary, non-privileged-in-Gitaly-terms gRPC client — this RPC does not require special server-side trust beyond normal auth) can set `timeout=0` and point `remote` at a slow, hanging, or adversarial Git server, causing the spawned `git fetch` subprocess and quarantine directory to persist indefinitely. This ties up the RPC handler, subprocess slots, and quarantine/temp storage, contributing to resource exhaustion / denial of service on the Gitaly node, since the only other bound is the client's own gRPC context (which the client also controls).

### Likelihood Explanation
Likelihood is high for triggering the bypass itself, since it requires nothing more than omitting or zeroing a single proto field on a request the caller already controls. Exploiting it into full node-level DoS additionally requires control of, or collusion with, a slow/malicious remote endpoint, which raises the bar somewhat but is a realistic scenario given `FetchRemote` explicitly supports fetching from arbitrary attacker-specified remotes.

### Recommendation
Enforce a mandatory, server-configured maximum timeout for `FetchRemote` regardless of the client-supplied value: always apply `context.WithTimeout` using `min(req.GetTimeout(), serverMaxFetchTimeout)` when the requested timeout is `<= 0` or exceeds an operator-defined ceiling, rather than skipping deadline enforcement altogether.

### Proof of Concept
1. Call `FetchRemote` with `Timeout: 0` (or unset) and `remote.Url` pointing to an endpoint that accepts the connection but never completes the Git protocol handshake/response.
2. Observe that no `context.WithTimeout` is applied (per [2](#0-1) ), so the underlying `git fetch` (via `quarantineRepo.FetchRemote`, [3](#0-2) ) blocks until the client's own gRPC deadline (if any) or the process is killed externally, holding the quarantine directory and subprocess resources open the entire time.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L30-39)
```go
func (s *server) FetchRemote(ctx context.Context, req *gitalypb.FetchRemoteRequest) (*gitalypb.FetchRemoteResponse, error) {
	if err := s.validateFetchRemoteRequest(ctx, req); err != nil {
		return nil, err
	}

	if req.GetTimeout() > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.GetTimeout())*time.Second)
		defer cancel()
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L103-104)
```go
	quarantineRepo := s.localRepoFactory.Build(quarantineDir.QuarantinedRepo())
	if err := quarantineRepo.FetchRemote(ctx, "inmemory", opts); err != nil {
```
