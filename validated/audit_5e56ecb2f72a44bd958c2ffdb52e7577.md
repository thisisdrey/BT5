### Title
Cache-Control leaks into `412 Precondition Failed` responses from `net/http`'s `checkPreconditions`/`ServeContent` — ([File: src/net/http/fs.go])

### Summary
`net/http.ServeContent`/`ServeFile` allow callers to set caching headers (e.g. `Cache-Control: public, max-age=..., immutable`) on the `ResponseWriter` before calling into the file-serving helpers, exactly as demonstrated in the standard library's own tests. When a client sends a mismatched or malformed `If-Match` header, `checkPreconditions` fails the precondition and writes a `412` status directly, without stripping the caller-set `Cache-Control` (or `Etag`/`Last-Modified`) headers — unlike the sibling error path `serveError`, which explicitly strips these headers for exactly this reason. This is the same root cause as the reported Astro bug class: an error/negative response inherits long-lived cache directives meant only for the success response, enabling caching intermediaries to store and replay the error indefinitely.

### Finding Description
Attacker input: an HTTP request to any handler that serves static content via `http.ServeContent`/`http.ServeFile` and has previously set long-lived cache headers on the response (a common, documented pattern — see `w.Header().Set("Cache-Control", ...)` usage alongside `ServeFile`/`ServeContent` in `src/net/http/fs_test.go:624-627`), carrying an `If-Match` header that does not match the current ETag.

Entry point: `serveContent` in `src/net/http/fs.go:276-281` calls `setLastModified(w, modtime)` and then `checkPreconditions(w, r, modtime)` [1](#0-0)  before any of `serveContent`'s own header manipulation happens.

Failed check: `checkPreconditions` evaluates `checkIfMatch(w, r)`; when the ETag doesn't match, `condFalse` is returned and the function writes the failure status **directly**, bypassing the header-stripping logic used elsewhere: [2](#0-1) 

Compare this to the dedicated `serveError` helper, added specifically to prevent caller-configured `Cache-Control`/`Etag`/`Last-Modified` from leaking into non-2xx responses: [3](#0-2) 

`serveError` is called for range-parsing errors, seek errors, etc., but the `412` path in `checkPreconditions` (both the `If-Match`/`If-Unmodified-Since` branch and the `If-None-Match` non-GET/HEAD branch) never calls it, so `Cache-Control` set by the calling handler survives unstripped into the `412` response.

Sink: the `ResponseWriter`'s header map is flushed with `w.WriteHeader(StatusPreconditionFailed)` while still carrying a `public, max-age=31536000, immutable` (or any application-chosen) `Cache-Control` directive — a response an RFC 9111–compliant cache is permitted to store because of the explicit `public` directive, regardless of the `412` status.

### Impact Explanation
A caching proxy/CDN sitting in front of a Go server using `ServeContent`/`ServeFile` with pre-set `Cache-Control` headers (a pattern the standard library itself documents and tests) can be tricked into caching a `412 Precondition Failed` response for a URL, because the response still advertises `public, max-age=31536000, immutable`. Any subsequent client requesting that same URL through the same cache — even without any `If-Match` header — would be served the cached `412` error instead of the real content, i.e. availability/integrity impact via cache poisoning, matching the CVSS `A:L` impact of the source advisory. This does not grant code execution or data disclosure, so it would be a low/medium-severity availability issue if reported to the Go security team (would not qualify for URGENT track; PUBLIC-track hardening at most).

### Likelihood Explanation
Requires only an unauthenticated HTTP request with a crafted `If-Match` header — no privileges, no victim interaction. It is reachable in any deployment that (a) uses `net/http`'s `ServeContent`/`ServeFile` for static assets and (b) sets long-lived `Cache-Control` on the `ResponseWriter` before calling them (a pattern explicitly exercised in `src/net/http/fs_test.go`'s `testServeFileNotModified`), and (c) sits behind a cache/CDN that treats explicit `public`/`max-age` as cacheable independent of status code. This narrows real-world exposure compared to the Astro case (where Cache-Control is set unconditionally for hashed asset paths), so likelihood is moderate rather than universal.

### Recommendation
Route the `StatusPreconditionFailed` (and the analogous `condFalse` branch under `checkIfNoneMatch` for non-GET/HEAD methods) through the same header-stripping logic as `serveError`, e.g. call a shared helper that deletes `Cache-Control`, `Content-Encoding`, `Etag`, and `Last-Modified` before calling `w.WriteHeader(StatusPreconditionFailed)` in `checkPreconditions` (`src/net/http/fs.go:653-665`), consistent with the intent already documented for `serveError`.

### Proof of Concept
```go
package http_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Demonstrates that a caller-set Cache-Control header survives into a
// 412 Precondition Failed response produced by ServeContent when the
// If-Match header does not match, mirroring the Astro cache-poisoning bug.
func TestServeContentLeaksCacheControlOn412(t *testing.T) {
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Pattern documented/tested in fs_test.go's testServeFileNotModified:
		// callers commonly set long-lived cache headers before calling ServeContent.
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		w.Header().Set("Etag", `"abc"`)
		http.ServeContent(w, r, "asset.js", timeZero, strings.NewReader("console.log(1)"))
	})

	ts := httptest.NewServer(handler)
	defer ts.Close()

	req, _ := http.NewRequest("GET", ts.URL, nil)
	req.Header.Set("If-Match", `"does-not-match"`)

	resp, err := ts.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusPreconditionFailed {
		t.Fatalf("got status %d, want 412", resp.StatusCode)
	}

	// BUG: Cache-Control from the success path should NOT be present on
	// a 412 error, otherwise CDNs/caches will store and replay the error.
	if cc := resp.Header.Get("Cache-Control"); cc != "" {
		t.Errorf("Cache-Control leaked into 412 response: %q (expected empty)", cc)
	}
}

var timeZero = struct{}{} // placeholder; use time.Time{} in real test file
```
Expected (buggy) result: the test fails because `Cache-Control: public, max-age=31536000, immutable` is present on the `412` response, confirming the response is cacheable by a conforming HTTP cache despite representing a transient client error.

### Citations

**File:** src/net/http/fs.go (L186-213)
```go
// serveError serves an error from ServeFile, ServeFileFS, and ServeContent.
// Because those can all be configured by the caller by setting headers like
// Etag, Last-Modified, and Cache-Control to send on a successful response,
// the error path needs to clear them, since they may not be meant for errors.
func serveError(w ResponseWriter, text string, code int) {
	h := w.Header()

	nonDefault := false
	for _, k := range []string{
		"Cache-Control",
		"Content-Encoding",
		"Etag",
		"Last-Modified",
	} {
		if !h.has(k) {
			continue
		}
		if httpservecontentkeepheaders.Value() == "1" {
			nonDefault = true
		} else {
			h.Del(k)
		}
	}
	if nonDefault {
		httpservecontentkeepheaders.IncNonDefault()
	}

	Error(w, text, code)
```

**File:** src/net/http/fs.go (L276-281)
```go
func serveContent(w ResponseWriter, r *Request, name string, modtime time.Time, sizeFunc func() (int64, error), content io.ReadSeeker) {
	setLastModified(w, modtime)
	done, rangeReq := checkPreconditions(w, r, modtime)
	if done {
		return
	}
```

**File:** src/net/http/fs.go (L647-656)
```go
func checkPreconditions(w ResponseWriter, r *Request, modtime time.Time) (done bool, rangeHeader string) {
	// This function carefully follows RFC 7232 section 6.
	ch := checkIfMatch(w, r)
	if ch == condNone {
		ch = checkIfUnmodifiedSince(r, modtime)
	}
	if ch == condFalse {
		w.WriteHeader(StatusPreconditionFailed)
		return true, ""
	}
```
