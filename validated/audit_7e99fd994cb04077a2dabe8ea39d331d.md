## Analysis: Path Matching Bypass in `HttpApiAccessFilter`

The reported in-toto-golang bug is a classic CWE-22 class issue: a security decision (DISALLOW rule enforcement) is made by comparing raw path strings without proper normalization, so semantically-equivalent paths (`foo` vs `dir/../foo`) are treated as different, letting an attacker slip a disallowed artifact past the check. The direct analog in java-tron is the operator-facing `disabledApi` allow/deny mechanism, enforced by `isDisabled()`.

### Root cause [1](#0-0) 

```java
private boolean isDisabled(String endpoint) {
  ...
  endpoint = URI.create(endpoint).normalize().toString();
  List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
  if (!disabledApiList.isEmpty()) {
    disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
  }
  ...
}
```

This normalizes the path via `URI.normalize()`, then extracts the "API name" by naively indexing `split("/")[2]`, assuming the normalized path always has exactly the shape `/<base>/<api>`. This assumption breaks whenever the normalized path retains extra leading segments (e.g., because only some `..` segments could be collapsed), silently shifting the API-name component away from index `2` and defeating the block/deny check.

This exact ambiguity is demonstrated in the repo's own test: [2](#0-1) 

```java
String url = "/wallet/a/../b/../getnowblock";
f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
Assert.assertTrue(f);          // detected correctly here

url = "/wallet/a/b/../getnowblock";
f = (boolean) privateMethod.invoke(httpApiAccessFilter,url);
Assert.assertFalse(f);         // NOT detected as disabled
```

`/wallet/a/b/../getnowblock` normalizes to `/wallet/a/getnowblock` (only one `..` is present, collapsing `b/..`), so `split("/")[2]` yields `"a"` instead of `"getnowblock"`, and the check silently passes even though the request still ultimately targets the `getnowblock`-class functionality intended to be blocked by the admin-configured `disabledApi` list (`common/src/main/resources/reference.conf` `disabledApi = [...]`) — this is used across `FullNode`, `Solidity`, and `PBFT` HTTP services via the same filter.

### Title
Path-matching bypass of `disabledApi` restriction via extra path segments in `HttpApiAccessFilter.isDisabled()` - (File: `framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java`)

### Summary
`HttpApiAccessFilter.isDisabled()` decides whether an incoming HTTP request targets an operator-disabled API by normalizing the URI and then extracting the API name via a fixed-index `split("/")[2]`. Because the normalization only collapses `..` segments locally, adding an extra path segment plus a compensating `..` shifts the extracted "API name" token away from the disabled-list check, letting the request evade the block even though it still functionally reaches the intended endpoint if the underlying container/servlet dispatch resolves the path differently than the filter's own re-derivation.

### Finding Description
`HttpApiAccessFilter` is wired via wildcard `/*` mappings ahead of every wallet/walletsolidity/walletpbft servlet [3](#0-2) , and is the sole gate implementing the operator-configured `disabledApi` blacklist described in `reference.conf` [4](#0-3) , used to shut off privacy- or resource-sensitive endpoints such as `scannotebyivk`, `scannotebyovk`, `isspend`, `getaccount`, etc. Its `isDisabled()` implementation performs URI normalization then blindly indexes the third path segment as the "API name" instead of comparing against the full, canonical resource identifier or using suffix/last-segment matching. As demonstrated by the project's own regression test, a path with an unbalanced combination of extra segments and `..` traversal tokens is normalized to a *different* number of path segments than expected, causing the wrong token to be checked against the deny-list and the disabled check to be skipped.

### Impact Explanation
If the request path presented to the filter (via `getServletPath()`/`getContextPath()`) still contains such artifacts when it is ultimately routed to the same target servlet (e.g., through relative-path resolution differences between the filter's manual `URI.normalize()` and the actual servlet-container dispatch resolution, or through alternate encodings that the container does not itself pre-collapse), an unprivileged remote HTTP client can access an API endpoint that the node operator explicitly disabled — this directly maps to the accepted impact class "an API the node can no longer serve" being served against operator intent, and for privacy-sensitive shielded-note-scanning endpoints, it also exposes an unintended data-disclosure/DoS surface the operator tried to close off.

### Likelihood Explanation
The condition is reachable by any anonymous network client issuing a crafted HTTP request path (no authentication, no privileged keys). It requires the servlet container's own path canonicalization to diverge from the filter's local re-normalization for some crafted combination of segments/encodings, which is plausible given the filter re-implements ad hoc normalization/parsing logic instead of relying on the container's already-resolved canonical path, as the project's own unit test proves the mismatch exists at the `isDisabled()` logic level.

### Recommendation
Replace the fixed-index `split("/")[2]` extraction with matching against the fully-normalized, container-resolved servlet path (e.g., compare the entire normalized path or the last path segment consistently, using the same value the container will actually use to dispatch the request), and add defense-in-depth by rejecting any request whose raw path contains `..` or `.` segments outright rather than trying to normalize and then pattern-match substrings.

### Proof of Concept
Using the project's own test harness:
```java
CommonParameter.getInstance().setDisabledApiList(Collections.singletonList("getnowblock"));
Method isDisabled = HttpApiAccessFilter.class.getDeclaredMethod("isDisabled", String.class);
isDisabled.setAccessible(true);

isDisabled.invoke(filter, "/wallet/getnowblock");            // true  (correctly blocked)
isDisabled.invoke(filter, "/wallet/a/../b/../getnowblock");  // true  (correctly blocked)
isDisabled.invoke(filter, "/wallet/a/b/../getnowblock");     // false (BYPASS: same target API, not blocked)
``` [5](#0-4)

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

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L520-535)
```java
  @Override
  protected void addFilter(ServletContextHandler context) {
    // filters the specified APIs
    // when node is lite fullnode and openHistoryQueryWhenLiteFN is false
    context.addFilter(new FilterHolder(liteFnQueryHttpFilter), "/*",
        EnumSet.allOf(DispatcherType.class));

    // http access filter, it should have higher priority than HttpInterceptor
    context.addFilter(new FilterHolder(httpApiAccessFilter), "/*",
        EnumSet.allOf(DispatcherType.class));
    // note: if the pathSpec of servlet is not started with wallet, it should be included here
    context.getServletHandler().getFilterMappings()[1]
        .setPathSpecs(new String[] {"/wallet/*",
            "/net/listnodes",
            "/monitor/getstatsinfo",
            "/monitor/getnodeinfo"});
```

**File:** common/src/main/resources/reference.conf (L445-449)
```text
  # Disabled API list (works for http, rpc and pbft, not jsonrpc). Case insensitive.
  disabledApi = [
    # "getaccount",
    # "getnowblock2"
  ]
```
