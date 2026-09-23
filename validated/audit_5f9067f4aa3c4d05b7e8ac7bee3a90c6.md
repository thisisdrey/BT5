Based on my research, I found a legitimate, currently-unfixed analog to the Tomcat AJP "Content-Length: 0 hang" bug (CVE-2014-0095) inside Go's `net/http` server. It is explicitly acknowledged in the test suite as an open, unfixed issue.

### Title
Denial of service via goroutine/connection hang on unconsumed `Expect: 100-continue` request body - (File: `src/net/http/server.go`)

### Summary
When an HTTP/1.1 handler does not read a request body that was sent with `Expect: 100-continue` and a small `Content-Length`, Go's server tries to silently discard the unread body before deciding whether to reuse the connection. This discard path reads directly from the raw request body (`w.reqBody`) instead of through the `expectContinueReader` wrapper that would send the `100 Continue` status line, so the goroutine blocks forever waiting for bytes the client will never send (because the client is correctly waiting for the `100 Continue` signal before sending). This is directly analogous to the Tomcat AJP bug: an unauthenticated "Content-Length" request triggers a hang in request processing that consumes a server thread/goroutine indefinitely.

### Finding Description
Entry point: `(*conn).serve` in [1](#0-0)  wraps `req.Body` in an `expectContinueReader` only when the request declares `Expect: 100-continue`; the `100 Continue` status line is only written the first time that wrapper's `Read` is invoked. `w.reqBody`, however, still refers to the original unwrapped `*body`, as set in `(*conn).readRequest`: [2](#0-1) .

If the handler never reads `req.Body` (e.g., it just calls `WriteHeader`/returns), `finishRequest`'s connection-reuse logic decides to discard the unconsumed body directly via `w.reqBody`, bypassing the `expectContinueReader`: [3](#0-2) . The actual discard is performed by `(*body).Close`, which calls `io.CopyN(io.Discard, bodyLocked{b}, maxPostHandlerReadBytes+1)` when `doEarlyClose` is set (true for server request bodies): [4](#0-3) . That `Read` blocks on the raw connection, waiting for the body bytes — but since the `100 Continue` was never sent (the wrapper that would send it was never invoked in this discard path), the client is correctly waiting to receive `100 Continue` before transmitting the body. Both sides block: the server goroutine hangs in the discard `Read`, and the client waits for `100 Continue`. Unless a `Server.ReadTimeout` is explicitly configured, this hang is unbounded.

This exact scenario is captured (and marked skipped, pending a fix) in the standard library's own test suite: [5](#0-4) .

### Impact Explanation
An unauthenticated remote client can send a minimal HTTP/1.1 request (`Expect: 100-continue`, small `Content-Length`, no body bytes) to any `net/http` server whose handler doesn't proactively drain `req.Body`, causing the serving goroutine (and the underlying TCP connection) to hang indefinitely when `Server.ReadTimeout`/`ReadHeaderTimeout` aren't set. Repeating this with many connections ties up goroutines/file descriptors — a thread/goroutine-consumption denial of service, matching the Tomcat AJP CWE-20/DoS class. This is a production code path in `net/http`, not a test/mock/vendored path, and is unfixed at the time the test was written (tracked as https://go.dev/issue/75933), so it should be routed via Go's standard PUBLIC vulnerability-report track once confirmed against the currently released version.

### Likelihood Explanation
Any Go program exposing `net/http.Server` to untrusted clients without setting `ReadTimeout`, and whose handler for the affected route doesn't explicitly read the full request body, is reachable. This is common: many handlers ignore bodies on GET-like semantics, redirect responses, or simple status-only endpoints for POST. No authentication, TLS trust, or special privilege is required — the attacker only needs to open a plain HTTP connection and send a crafted request.

### Recommendation
In the discard path of `finishRequest` (`src/net/http/server.go`, around the `if discard { w.reqBody.Close() ... }` block), route the discard read through `w.ecReader` (the `expectContinueReader`) when it is non-nil, so that `100 Continue` is sent before attempting to read/discard the body — or, alternatively, skip attempting to discard/reuse the connection whenever `Expect: 100-continue` was received but never satisfied (treat it like the `closeAfterReply` case already handled a few lines above for `ecReader != nil && bodyRemains()`).

### Proof of Concept
```go
package http

import (
	"net"
	"testing"
	"time"
)

// Demonstrates that a handler which never reads req.Body on an
// Expect:100-continue request with a small Content-Length causes
// the server to hang indefinitely discarding the body, because the
// discard path never sends "100 Continue" to unblock the client.
func TestExpect100ContinueUnreadSmallBodyHangs(t *testing.T) {
	ts := newClientServerTest(t, http1Mode, HandlerFunc(func(w ResponseWriter, r *Request) {
		// Handler intentionally does not read r.Body.
		w.WriteHeader(StatusOK)
	})).ts
	defer ts.Close()

	conn, err := net.Dial("tcp", ts.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	_, err = conn.Write([]byte("POST / HTTP/1.1\r\n" +
		"Host: example.com\r\n" +
		"Expect: 100-continue\r\n" +
		"Content-Length: 1\r\n\r\n"))
	// Body byte is never sent, matching real Expect:100-continue semantics:
	// client is waiting for "100 Continue" before sending it.
	if err != nil {
		t.Fatal(err)
	}

	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	buf := make([]byte, 512)
	n, err := conn.Read(buf)
	if err == nil {
		t.Fatalf("expected read timeout (server hang); got response: %q", buf[:n])
	}
	// Expected today: read times out (server hung discarding body,
	// having never sent "100 Continue"). Once fixed, either a
	// "100 Continue" or the final "200 OK" should arrive promptly.
}
```
Expected today: the test observes a client-side read timeout because the server goroutine is blocked in the body-discard `Read`, never having sent `100 Continue`.

### Citations

**File:** src/net/http/server.go (L1179-1187)
```go
	var reqBody *body
	switch b := req.Body.(type) {
	case noBody:
	case *body:
		reqBody = b
		reqBody.doEarlyClose = true
	default:
		panic(fmt.Errorf("http: unexpected request body type %T", req.Body))
	}
```

**File:** src/net/http/server.go (L1535-1556)
```go
	if w.req.ContentLength != 0 && w.reqBody != nil && !w.closeAfterReply && !w.fullDuplex {
		var discard, tooBig bool
		w.reqBody.mu.Lock()
		switch {
		case w.reqBody.closed:
			if !w.reqBody.sawEOF {
				// Body was closed in handler with non-EOF error.
				w.closeAfterReply = true
			}
		case w.reqBody.unreadDataSizeLocked() >= maxPostHandlerReadBytes:
			tooBig = true
		default:
			discard = true
		}
		w.reqBody.mu.Unlock()

		if discard {
			w.reqBody.Close()
			if !w.reqBody.consumedEntireBody() {
				w.closeAfterReply = true
			}
		}
```

**File:** src/net/http/server.go (L2189-2201)
```go
		// Expect 100 Continue support
		req := w.req
		if req.expectsContinue() {
			if req.ProtoAtLeast(1, 1) && req.ContentLength != 0 {
				// Wrap the Body reader with one that replies on the connection
				w.ecReader = &expectContinueReader{readCloser: req.Body, resp: w}
				w.canWriteContinue.Store(true)
				req.Body = w.ecReader
			}
		} else if req.Header.get("Expect") != "" {
			w.sendExpectationFailed()
			return
		}
```

**File:** src/net/http/transfer.go (L1054-1095)
```go
func (b *body) Close() error {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return nil
	}
	var err error
	switch {
	case b.sawEOF:
		// Already saw EOF, so no need going to look for it.
	case b.hdr == nil && b.closing:
		// no trailer and closing the connection next.
		// no point in reading to EOF.
	case b.doEarlyClose:
		// Read up to maxPostHandlerReadBytes bytes of the body, looking
		// for EOF (and trailers), so we can re-use this connection.
		if lr, ok := b.src.(*io.LimitedReader); ok && lr.N > maxPostHandlerReadBytes {
			// There was a declared Content-Length, and we have more bytes remaining
			// than our maxPostHandlerReadBytes tolerance. So, give up.
		} else {
			var n int64
			// Consume the body, or, which will also lead to us reading
			// the trailer headers after the body, if present.
			n, err = io.CopyN(io.Discard, bodyLocked{b}, maxPostHandlerReadBytes+1)
			if err == io.EOF && n <= maxPostHandlerReadBytes {
				b.sawEOF = true
				// Reaching the end of the body is the expected
				// outcome here, not an error to report to the caller.
				err = nil
			}
		}
	default:
		// Fully consume the body, which will also lead to us reading
		// the trailer headers after the body, if present.
		_, err = io.Copy(io.Discard, bodyLocked{b})
	}
	b.closed = true
	return err
}
```

**File:** src/net/http/serve_test.go (L8058-8079)
```go
	}, {
		// Send a request with a 1-byte body, which the server handler never reads.
		// We should either send a 100-Continue and read the body
		// or we should close the connection.
		//
		// Right now, the server hangs trying to read the request body
		// the client isn't sending.
		skip: "https://go.dev/issue/75933",

		name: "100-continue unconsumed small body",
		message: []string{
			"POST / HTTP/1.1",
			"Host: example.tld",
			"Expect: 100-continue",
			"Content-Length: 1",
			"",
			// body is never sent
		},
		want100Continue: false,
		wantResponse:    200,
		wantReused:      true,
	}, {
```
