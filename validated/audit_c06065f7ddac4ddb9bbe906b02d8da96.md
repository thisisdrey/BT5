No vulnerability found for this question.

**Rationale:** CVE-2020-13543 describes a use-after-free memory-safety bug in WebKitGTK's C++ WebSocket implementation — a rendering-engine-specific bug class rooted in manual memory management, triggered by a malicious webpage. The `go--008` repository is the Go standard library/toolchain source; it does not implement a WebKit-style browser engine or its own WebSocket protocol implementation. The only "websocket" references found are:

- `httputil.ReverseProxy`'s protocol-upgrade proxying, which forwards `Connection: upgrade`/`Upgrade: websocket` headers and hijacked connections without parsing WebSocket frame contents [1](#0-0) .
- `Request.requiresHTTP1`, which only checks header tokens to decide HTTP/1 vs HTTP/2 routing [2](#0-1) .
- HTTP/2's extended-CONNECT guard against WebSockets-over-HTTP/2, a config flag, not a parser [3](#0-2) .

None of these parse or hold WebSocket frame payloads with manual object lifetime management akin to WebKit's C++ object model, so the use-after-free primitive has no reachable analog here. Go's garbage-collected memory model also structurally precludes this class of bug outside of `unsafe`/`cgo` code, and the only UAF-adjacent code found is an intentional test fixture for ASan (`src/cmd/cgo/internal/testsanitizers/testdata/asan1_fail.go`), which is test/demonstration code, not a production entry point reachable by untrusted network input. Stretching this CVE to any of these paths would be an unsupported analogy, so no finding is reported.

### Citations

**File:** src/net/http/httputil/reverseproxy_test.go (L1371-1391)
```go
func TestReverseProxyWebSocket(t *testing.T) {
	backendServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if upgradeType(r.Header) != "websocket" {
			t.Error("unexpected backend request")
			http.Error(w, "unexpected request", 400)
			return
		}
		c, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		defer c.Close()
		io.WriteString(c, "HTTP/1.1 101 Switching Protocols\r\nConnection: upgrade\r\nUpgrade: WebSocket\r\n\r\n")
		bs := bufio.NewScanner(c)
		if !bs.Scan() {
			t.Errorf("backend failed to read line from client: %v", bs.Err())
			return
		}
		fmt.Fprintf(c, "backend got %q\n", bs.Text())
	}))
```

**File:** src/net/http/request.go (L1600-1607)
```go
// requiresHTTP1 reports whether this request requires being sent on
// an HTTP/1 connection.
func (r *Request) requiresHTTP1() bool {
	return hasToken(r.Header.Get("Connection"), "upgrade") &&
		ascii.EqualFold(r.Header.Get("Upgrade"), "websocket")
}


```

**File:** src/net/http/internal/http2/http2.go (L30-43)
```go
var (
	VerboseLogs    bool
	logFrameWrites bool
	logFrameReads  bool

	// Enabling extended CONNECT by causes browsers to attempt to use
	// WebSockets-over-HTTP/2. This results in problems when the server's websocket
	// package doesn't support extended CONNECT.
	//
	// Disable extended CONNECT by default for now.
	//
	// Issue #71128.
	disableExtendedConnectProtocol = true

```
