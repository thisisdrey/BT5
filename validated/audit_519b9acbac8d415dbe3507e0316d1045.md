### Title
HTTP API Disabled-List Bypass via Duplicate Slashes in Endpoint Path - (File: `framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java`)

### Summary
`HttpApiAccessFilter.isDisabled()` determines whether an incoming HTTP request targets an API name that the node operator has placed on `disabledApiList` (`node.disabledApiList` config). It does so by taking `contextPath + servletPath`, calling `URI.create(endpoint).normalize()`, and then extracting the API name via `endpoint.split("/")[2]`. `URI.normalize()` only collapses `.`/`..` segments; it does **not** collapse repeated `/` characters, which mirrors the exact bug class in the referenced Jetty advisory (multiple `/` characters bypassing path-based protection checks).

### Finding Description
`isDisabled` is the sole gate deciding whether a wallet/HTTP query API that an operator has explicitly disabled (e.g. `getaccount`, `getnowblock`, or any sensitive read API) should be blocked and replaced with a 404 "API is unavailable due to config" response: [1](#0-0) 

The unit test confirms the developers were aware of and defended against `..` traversal segments (e.g. `/wallet/a/../b/../getnowblock` still resolves to `getnowblock`), but no equivalent test or handling exists for duplicate `/` characters: [2](#0-1) 

Because `endpoint.split("/")` naively splits on `/`, a request path containing a doubled slash such as `/wallet//getnowblock` produces the array `["", "wallet", "", "getnowblock"]`. Index `[2]` — the value compared against `disabledApiList` — becomes the empty string `""` instead of `getnowblock"`. Since `disabledApiList` will not contain the empty string, `isDisabled()` returns `false`, and the filter forwards the request to the underlying servlet via `chain.doFilter`, exactly reflecting the "multiple `/` characters bypass protection mechanism" bug class from the Jetty advisory.

This filter is the shared, first-line access-control gate wired into every HTTP surface of the node — full node, solidity node, and PBFT node — each mapped with a wildcard path spec (`/*` or `/wallet/*`, `/walletsolidity/*`) so it intercepts all wallet query endpoints before the target servlet executes: [3](#0-2) [4](#0-3) 

### Impact Explanation
`disabledApiList` is an operator-controlled hardening mechanism used to shut down specific wallet/query APIs (including sensitive ones such as `getaccount`, shielded-note scanning endpoints, or market/order data endpoints) at the node level, typically for privacy, DoS-mitigation, or compliance reasons. An anonymous client can bypass this control purely by inserting a duplicate slash into the requested path, causing the filter to misclassify the endpoint name and let the request through to the real servlet, defeating an explicit administrative security decision and disclosing data/functionality the operator intended to block. This matches the "Information Disclosure" classification of the referenced advisory, though impact here is capped by whatever data the specific disabled API exposes.

### Likelihood Explanation
Exploitability depends on whether the underlying Servlet container's path matching for the concrete servlet mapping (most wallet API paths are registered as exact `contextPath` + `servletPath` strings, e.g. `/wallet/getnowblock`) also accepts the doubled-slash variant of the URI as an equivalent match, so that the request is still routed to the intended servlet after bypassing the filter check. This depends on the specific embedded Jetty version's URI/path normalization behavior (whether `ServletHandler` collapses repeated slashes before exact-path servlet dispatch), which I could not conclusively confirm from the indexed files — the `framework/build.gradle` Jetty version reference was found but its content was not retrievable through the available tools.

### Recommendation
In `isDisabled()`, normalize the endpoint by additionally collapsing repeated `/` characters (e.g. `endpoint.replaceAll("/+", "/")`) before calling `URI.normalize()` and splitting, or match against `disabledApiList` using a case-insensitive comparison of the final decoded path segment obtained via a robust path-parsing utility rather than naive `split("/")[2]` indexing.

### Proof of Concept
1. Configure `node.disabledApiList=["getnowblock"]` on a full node.
2. Send `GET /wallet/getnowblock` — filter returns 404 "this API is unavailable due to config" (expected, confirmed by `HttpApiAccessFilterTest.testHttpFilter`).
3. Send `GET /wallet//getnowblock` (double slash). `isDisabled()` computes `endpoint.split("/")[2] == ""`, which is not present in `disabledApiList`, so `isDisabled()` returns `false` and the request is forwarded to `chain.doFilter`, potentially reaching `GetNowBlockServlet` if the container's servlet dispatch resolves the doubled-slash path to the same servlet mapping (unverified against the exact Jetty version in use). [1](#0-0)

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

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L128-151)
```java
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

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L520-536)
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

**File:** framework/src/main/java/org/tron/core/services/interfaceOnPBFT/http/PBFT/HttpApiOnPBFTService.java (L267-277)
```java
  @Override
  protected void addFilter(ServletContextHandler context) {
    // filters the specified APIs
    // when node is lite fullnode and openHistoryQueryWhenLiteFN is false
    context.addFilter(new FilterHolder(liteFnQueryHttpFilter), "/*",
        EnumSet.allOf(DispatcherType.class));

    // api access filter
    context.addFilter(new FilterHolder(httpApiAccessFilter), "/*",
        EnumSet.allOf(DispatcherType.class));
  }
```
