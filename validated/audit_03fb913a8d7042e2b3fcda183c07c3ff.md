### Title
Authorization Bypass via Non-Canonical URL Path in HttpApiAccessFilter's disabledApi Enforcement - (File: framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java)

### Summary
`HttpApiAccessFilter.isDisabled()` derives the API name to check against the operator-configured `node.disabledApi` block-list by taking `endpoint.split("/")[2]` after a `URI.normalize()` call, instead of matching against the canonical last path segment that the Jetty servlet dispatcher actually uses to route the request to its handler servlet. This is structurally the same bug class as the `@fastify/static` advisory: an authorization decision (`allowedPath`/`disabledApi`) is made using a positional/segment-based parse of an attacker-controlled path that does not agree with how the underlying framework resolves that same path to a concrete resource/servlet, so a non-canonical path can slip past the check while still reaching the blocked handler.

### Finding Description
`isDisabled()` normalizes `.`/`..` segments via `URI.create(endpoint).normalize()` and then blindly indexes `split("/")[2]` to extract what it assumes is the API name: [1](#0-0) 

This fixed-index extraction is only correct when the normalized path has exactly the shape `/<module>/<api>`. The project's own unit test demonstrates the check silently returns `false` (i.e., "not disabled") for a request whose normalized path still contains extra intermediate segments: [2](#0-1) 

Specifically `/wallet/a/b/../getnowblock` normalizes to `/wallet/a/getnowblock`, so `split("/")[2]` yields `"a"` instead of `"getnowblock"`, and the filter lets the request through even though `getnowblock` is in `disabledApi`. Because each API endpoint (`GetNowBlockServlet`, `GetAccountServlet`, etc.) is registered as an individual Jetty `ServletHolder` in `FullNodeHttpApiService`, the question of whether such a crafted path still lands on the disabled servlet depends on Jetty's own path-mapping/normalization behavior for the *raw* request URI versus what `getServletPath()` returns to the filter — the filter's normalization and the container's routing normalization are two independent parsers acting on the same untrusted input, which is exactly the "non-canonical path disagreement" pattern in the advisory. `node.disabledApi` is a documented operator security control meant to hide sensitive endpoints (e.g. private-key or account-mutating APIs) from public exposure: [3](#0-2) [4](#0-3) 

The gRPC counterpart, `RpcApiAccessInterceptor`, uses the gRPC method descriptor's full name rather than a raw HTTP path, so it is not subject to the same class of ambiguity: [5](#0-4) 

### Impact Explanation
If an operator uses `node.disabledApi` to block sensitive HTTP endpoints (the documented, intended purpose of this feature — see `docs/configuration.md`), an unauthenticated remote client can potentially bypass that block via a crafted non-canonical path and still reach the disabled servlet, defeating the operator's security boundary. Depending on which API was intended to be disabled, this could re-expose account/wallet mutation endpoints, node-management endpoints, or other functionality the operator explicitly tried to hide from public access — a concrete access-control bypass on the query path into `Wallet`.

### Likelihood Explanation
Exploitability requires only an unauthenticated HTTP GET/POST request with a crafted path (e.g. `/wallet/a/b/../getnowblock`) — no credentials, no special network position. The bug is proven present by the project's own test (`testIsDisabled`), which explicitly asserts `false` for such a payload against a disabled API name. The main remaining uncertainty (not fully verifiable from the indexed source alone) is whether Jetty's servlet dispatch for this specific deployment collapses `..` segments identically to `URI.normalize()` before invoking the target servlet — if Jetty's routing resolves `/wallet/a/b/../getnowblock` to the same `GetNowBlockServlet` that handles `/wallet/getnowblock` (which is the common behavior for servlet containers that normalize dot-segments during URI parsing, independent of the servlet's registered exact path), the bypass is fully live end-to-end.

### Recommendation
Do not rely on positional array indexing of the (already-normalized) path for the security decision. Instead, derive the effective API identifier the same way the servlet dispatcher does — e.g., match against the last non-empty path segment after full normalization, reject requests whose normalized path segment count is unexpected, or better, tie the disabled check to the actual resolved `HttpServlet` class/instance (e.g., via `request.getServletPath()`'s mapped servlet name from `getServletContext().getServletRegistration(...)`) rather than string-splitting the URL. Add regression tests covering deeper/irregular path shapes (`/wallet/x/y/../../getnowblock`, encoded dot segments, duplicate slashes) to ensure the block-list decision and the actual servlet dispatch always agree.

### Proof of Concept
1. Operator configures `node.disabledApi = ["getnowblock"]` to hide `/wallet/getnowblock`.
2. Attacker sends `GET /wallet/a/b/../getnowblock` (or any path that normalizes such that the "api name" is no longer at `split("/")[2]`).
3. `isDisabled()` normalizes to `/wallet/a/getnowblock`, extracts `"a"` as the checked segment, finds it not in `disabledApiList`, and returns `false`.
4. `chain.doFilter()` proceeds; if Jetty's dispatcher independently normalizes the same `..` segment and routes to `GetNowBlockServlet`, the attacker receives the response from the endpoint the operator explicitly disabled — reproduced directly by `HttpApiAccessFilterTest.testIsDisabled` (lines 144–146) showing the check returns `false` for this crafted input while `getnowblock` remains in the block-list. [6](#0-5)

### Citations

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L60-74)
```java
  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      endpoint = URI.create(endpoint).normalize().toString();
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.warn("check isDisabled except, endpoint={}, {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L127-151)
```java
  @Test
  public void testIsDisabled() throws Exception {
    List<String> list = new ArrayList<>();
    list.add("getnowblock");
    CommonParameter.getInstance().setDisabledApiList(list);
    Method privateMethod = httpApiAccessFilter.getClass()
            .getDeclaredMethod("isDisabled", String.class);
    privateMethod.setAccessible(true);

    String url = "/wallet/getnowblock";
    boolean f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/../b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertTrue(f);

    url = "/wallet/a/b/../getnowblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);

    url = "/wallet/getblock";
    f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
    Assert.assertFalse(f);
  }
```

**File:** docs/configuration.md (L121-128)
```markdown
To disable an API endpoint that you do not want to expose publicly, set its `Enable` flag to `false` or add endpoints to `node.disabledApi`:

```hocon
node.disabledApi = [
  "getaccount",
  "getnowblock2"
]
```
```

**File:** common/src/main/resources/reference.conf (L445-449)
```text
  # Disabled API list (works for http, rpc and pbft, not jsonrpc). Case insensitive.
  disabledApi = [
    # "getaccount",
    # "getnowblock2"
  ]
```

**File:** framework/src/main/java/org/tron/core/services/ratelimiter/RpcApiAccessInterceptor.java (L41-54)
```java
  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[1].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.error("check isDisabled except, endpoint={}, error is {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```
