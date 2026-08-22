Confirmed — no `SetDeadline`/`Timeout` calls exist anywhere in `sshd/server.go`, so nothing bounds the time `ssh.NewServerConn` can block on a stalled peer.

### Title
Unbounded per-connection goroutine/FD growth from stalled SSH handshakes enables DoS of sshd control-plane listener - (File: sshd/server.go)

### Summary
`SSHServer.run` accepts every TCP connection and spawns a goroutine that calls `ssh.NewServerConn(c, s.config)` with no per-connection handshake timeout and no bound on concurrent unauthenticated connections. An attacker can open many TCP connections to the configured `sshd.listen` address and never send valid SSH handshake bytes, causing each goroutine (and its underlying socket) to remain open indefinitely, exhausting file descriptors and goroutines on the host.

### Finding Description
In `sshd/server.go`, `run` accepts connections in a loop and, for each one, launches a goroutine that only unblocks in three ways: successful handshake, handshake error from the peer, or the parent context being cancelled: [1](#0-0) 

The `sessionContext` used to force-close the socket is only cancelled when the *parent* `ctx` (tied to `Control.Stop()` or a config reload disabling sshd) is done — there is no per-connection deadline set via `c.SetDeadline`/`SetReadDeadline`, and `ssh.ServerConfig` has no `MaxAuthTries`/timeout configured either: [2](#0-1) 

Because `ssh.NewServerConn` blocks reading from the raw connection during key exchange, a client that connects and simply never sends (or sends partial garbage that never completes a valid packet) keeps that goroutine and its file descriptor alive for as long as the server itself runs. There is no limit on the number of concurrent in-flight (unauthenticated) connections, so an attacker can repeat this to grow goroutine/FD counts without bound, since `listener.Accept()` in the loop keeps spawning a new goroutine per new TCP connection regardless of how many prior connections are still stalled: [3](#0-2) 

This violates the intended invariant "no packet/connection is trusted before authentication ... accept loop must bound resources per unauthenticated peer" — the accept loop places no bound on resource consumption by unauthenticated peers.

### Impact Explanation
This is a real, reachable resource-exhaustion path against the sshd control-plane listener when `sshd.enabled=true`. Each stalled connection permanently consumes one goroutine, one file descriptor, and the associated per-connection contexts/closures until process restart or `Control.Stop()`. At scale this can exhaust the process's file-descriptor limit or induce goroutine-scheduling pressure, which can degrade or starve other goroutines in the same process (including the UDP tunnel packet-processing goroutines), since they share the same process-wide FD table and Go scheduler. This matches a remote crash/wedge / DoS impact against the control-plane path, scoped to hosts that have opted into `sshd.enabled=true` with a reachable listener.

### Likelihood Explanation
Preconditions are exactly as stated in the question: `sshd.enabled=true` and a reachable `sshd.listen` address — no host key, CA cert, or authorized key is needed by the attacker, since the vulnerable code path (`listener.Accept` → goroutine → `ssh.NewServerConn`) runs before any authentication occurs. The attack is trivially repeatable: opening raw TCP sockets and withholding/garbage-sending data requires no special privileges or timing precision, and can be automated to open many connections in parallel, so exploitation is straightforward and fully within reach of an unprivileged network attacker who can reach the listener.

### Recommendation
Bound per-connection resource usage in `SSHServer.run`:
- Set an explicit handshake deadline via `c.SetDeadline(time.Now().Add(handshakeTimeout))` before calling `ssh.NewServerConn`, clearing/refreshing it once authenticated.
- Add a semaphore/counter capping the number of concurrent in-flight (pre-auth) connections, rejecting/closing new connections beyond the cap immediately.
- Consider adding `MaxAuthTries` and reducing accepted idle time to fail fast on non-conforming clients.

### Proof of Concept
Integration test (extending `e2e/sshd_test.go` patterns):
1. Start a control with `sshd.enabled=true` and a valid host key, as in `TestSSHDLifecycle`.
2. Record baseline `runtime.NumGoroutine()`.
3. Open N (e.g. 200) raw `net.Dial("tcp", sshdAddr)` connections and never write any bytes to them (or write a few garbage bytes only, never completing a valid SSH handshake).
4. Assert that `runtime.NumGoroutine()` grows by ~N and remains elevated after a timeout window (e.g. 30s) that would be enough for a legitimate handshake to fail, showing the goroutines/FDs are never reclaimed absent an explicit timeout.
5. Optionally assert that a legitimate SSH client (`sshExecReload`-style) experiences degraded `Accept` responsiveness or failure once FD limits are approached, confirming the DoS impact on other peers using the same listener.

### Citations

**File:** sshd/server.go (L88-91)
```go
	s.config = &ssh.ServerConfig{
		PublicKeyCallback: cc.Authenticate,
		ServerVersion:     fmt.Sprintf("SSH-2.0-Nebula???"),
	}
```

**File:** sshd/server.go (L210-228)
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
```
