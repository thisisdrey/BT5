### Title
Unbounded per-connection goroutines/file descriptors due to missing handshake timeout in SSH accept loop, causing accept-loop failure and admin listener DoS - (File: sshd/server.go)

### Summary
`SSHServer.run` (sshd/server.go:210-264) spawns one goroutine per accepted TCP connection and calls the blocking `ssh.NewServerConn` with no per-connection deadline or global concurrency limit. An attacker who can reach the configured `sshd.listen` address can open many TCP connections and never send SSH handshake bytes, each of which permanently consumes a goroutine and a file descriptor until the whole server context is cancelled (i.e. until `SSHServer.Stop` is called via config reload/shutdown). Once file descriptors are exhausted, `listener.Accept()` in the same loop returns a non-`net.ErrClosed` error, causing `run` to `return` and the admin SSH listener to stop accepting connections entirely, denying legitimate admin/debug access until the process is restarted or reloaded.

### Finding Description
`s.run` (sshd/server.go:210-264) is the accept loop backing every `Run`/`Stop` cycle of `SSHServer`: [1](#0-0) 

For each accepted `net.Conn`, a goroutine is started that only closes the socket if the *server-wide* context (`ctx`, derived from `Control.Stop`/reload) is cancelled — there is no per-connection idle/handshake timeout via `SetDeadline`/`SetReadDeadline`, and no bound on the number of concurrent in-flight connections/goroutines: [2](#0-1) 

Because `ssh.NewServerConn` blocks waiting for the client's handshake, a client that opens the TCP connection and sends nothing (a half-open connection) keeps its goroutine parked and its socket open indefinitely — the only cleanup path is server shutdown/reload, not per-connection expiry. This violates the stated invariant that "idle and half-open connections are timed out and total concurrency is bounded," which is not enforced anywhere in `sshd/server.go`.

The accept loop itself (`listener.Accept()`) keeps running and spawning goroutines as fast as connections arrive, so it is not directly "stuck," but the process's file-descriptor budget is shared across the whole node. Once descriptors are exhausted, `Accept()` returns an error that is not `net.ErrClosed`, and `run` unconditionally executes `return`, terminating the entire accept loop and shutting the SSH admin listener down until an operator issues `reload`/restarts the process: [3](#0-2) 

`SSHServer.Stop` itself is only reachable by the local operator/reload path (sshd.enabled toggling, config reload) and does not introduce the exhaustion — the exhaustion is caused by the missing per-connection timeout/limit in `run`, and `Stop` is simply the mechanism that would otherwise clean up the run loop on intentional shutdown: [4](#0-3) 

The `sshd` listener is opt-in and configured via `sshd.enabled`/`sshd.listen`; it is only reachable if an operator has explicitly enabled and bound it (see `configSSH` in ssh.go), but per the question's threat model the listener is assumed to be locally reachable: [5](#0-4) 

### Impact Explanation
This is a Denial of Service against Nebula's local control/admin surface (the SSH debug/admin server): an attacker who can reach the configured `sshd.listen` port can exhaust the process's file descriptors and goroutines using cheap half-open TCP connections, eventually causing the accept loop to exit and the SSH admin interface to stop working for legitimate operators until it is manually restarted/reloaded. This matches the "Denial of service against the node's control surface" impact category referenced in the question.

### Likelihood Explanation
Exploitation only requires unauthenticated TCP connect-and-hold behavior against a reachable `sshd.listen` port — no certificate, key, or CA control is needed. It is fully repeatable: an attacker can open connections at whatever rate the OS/network allows, and each connection permanently consumes a goroutine/fd until the operator restarts the server. Feasibility is bounded only by the target's file-descriptor limits, which are typically in the low thousands by default, making this readily achievable from a single unprivileged client.

### Recommendation
- Set a read/handshake deadline on each accepted connection before calling `ssh.NewServerConn` (e.g., `c.SetDeadline(time.Now().Add(handshakeTimeout))`, cleared after successful handshake) in `sshd/server.go`'s `run` function.
- Bound the number of concurrent in-flight (pre-authentication) connections with a semaphore/worker pool, rejecting or immediately closing new connections beyond the limit.
- Treat `Accept()` errors from resource exhaustion (e.g., `EMFILE`) as transient/retryable with backoff rather than unconditionally returning and shutting down the whole listener.

### Proof of Concept
Integration test plan for `sshd/server_test.go` (or a new test file):
1. Start an `SSHServer` via `Run` on a test TCP port with a valid host key/CA configured.
2. From N goroutines (e.g., N = current process fd soft limit or a large number like 2000), `net.Dial("tcp", addr)` and hold the connection open without sending any bytes.
3. Assert that after the flood, `runtime.NumGoroutine()` grows roughly linearly with N and remains elevated (no timeout reclaims them).
4. Attempt a legitimate SSH client connection (with valid cert/key) against the same listener and assert it either fails or hangs beyond a reasonable timeout, or that after enough stalled connections `Accept()` starts failing and the server's log emits "Error in listener, shutting down" / the listener closes, demonstrating the loop terminates and the admin interface becomes unavailable.
5. Compare against expected behavior post-fix: legitimate connections should still succeed and stalled connections should be closed by a handshake timeout well before FD exhaustion.

### Citations

**File:** sshd/server.go (L210-250)
```go
func (s *SSHServer) run(ctx context.Context, listener net.Listener) {
	for {
		c, err := listener.Accept()
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				s.l.Warn("Error in listener, shutting down", "error", err)
			}
			return
		}
		go func(c net.Conn) {
			// NewServerConn may block while waiting for the client to complete the handshake.
			// Ensure that a bad client doesn't hurt us by checking for the parent context
			// cancellation before calling NewServerConn, and forcing the socket to close when
			// the context is cancelled.
			sessionContext, sessionCancel := context.WithCancel(ctx)
			go func() {
				<-sessionContext.Done()
				c.Close()
			}()
			conn, chans, reqs, err := ssh.NewServerConn(c, s.config)
			fp := ""
			if conn != nil {
				fp = conn.Permissions.Extensions["fp"]
			}

			if err != nil {
				l := s.l.With(
					"error", err,
					"remoteAddress", c.RemoteAddr(),
				)
				if conn != nil {
					l = l.With("sshUser", conn.User())
					conn.Close()
				}
				if fp != "" {
					l = l.With("sshFingerprint", fp)
				}
				l.Warn("failed to handshake")
				sessionCancel()
				return
			}
```

**File:** sshd/server.go (L266-272)
```go
func (s *SSHServer) Stop() {
	if s.listener != nil {
		if err := s.listener.Close(); err != nil {
			s.l.Warn("Failed to close the sshd listener", "error", err)
		}
	}
}
```

**File:** ssh.go (L81-93)
```go
func configSSH(l *slog.Logger, ssh *sshd.SSHServer, c *config.C) (func(), error) {
	listen := c.GetString("sshd.listen", "")
	if listen == "" {
		return nil, fmt.Errorf("sshd.listen must be provided")
	}

	_, port, err := net.SplitHostPort(listen)
	if err != nil {
		return nil, fmt.Errorf("invalid sshd.listen address: %s", err)
	}
	if port == "22" {
		return nil, fmt.Errorf("sshd.listen can not use port 22")
	}
```
