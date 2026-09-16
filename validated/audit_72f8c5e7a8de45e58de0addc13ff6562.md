### Title
`HttpApiAccessFilter.isDisabled()` can be bypassed with a double-slash path segment, letting a registered/anonymous API client reach APIs an operator explicitly disabled - (File: framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java)

### Summary
`HttpApiAccessFilter` is the HTTP filter that enforces the node operator's `disabledApiList` configuration for the `/wallet`, `/walletsolidity`, and `/walletpbft` HTTP endpoints. It derives the API name to check by normalizing the request URI and splitting on `/`, then testing `endpoint.split("/")[2]` against the configured disabled-API set. A request path containing a doubled slash (e.g. `/wallet//getnowblock`) shifts the array indices so that `split("/")[2]` becomes an empty string instead of the actual API name, causing `isDisabled()` to return `false` even when that API is in `disabledApiList`. This lets any HTTP client bypass an operator-configured access restriction and reach an API that was intentionally disabled.

### Finding Description
The filter builds `endpoint` from `contextPath + getServletPath()` and only guards against `.`/`..` traversal via `URI.normalize()`: [1](#0-0) 

The actual authorization check is: [2](#0-1) 

`URI.create(endpoint).normalize()` only resolves `.` and `..` segments; it does not collapse repeated `/` characters. For a legitimate disabled endpoint `/wallet/getnowblock`, `endpoint.split("/")` yields `["", "wallet", "getnowblock"]`, so index `2` correctly equals `"getnowblock"`. If the same logical request is sent as `/wallet//getnowblock` (an extra slash, which most servlet containers pass through in `getServletPath()` without collapsing), `endpoint.split("/")` yields `["", "wallet", "", "getnowblock"]`, so index `2` is `""` — which will never match any entry in `disabledApiList`. The `isDisabled()` check therefore returns `false`, the `SC_NOT_FOUND`/"unavailable due to config" short-circuit is skipped, and `chain.doFilter` forwards the request to the underlying `Wallet` HTTP servlet, which resolves the same target API (servlets typically tolerate the extra slash when routing to the handler), letting the caller reach an API the operator explicitly disabled via `disabledApiList`.

This is a direct analog of the CVE-2025-32373 bug class: a crafted request path lets a caller reach a resource/endpoint they are not supposed to have access to, because the access-control check parses the path differently than the downstream resource resolver does.

The existing regression test only validates `.`/`..` traversal handling and confirms the code has no protection against duplicate-slash manipulation: [3](#0-2) 

### Impact Explanation
`disabledApiList` is the operator's primary mechanism (documented via `Args`/`CommonParameter`) to turn off sensitive HTTP APIs on a node (e.g. `getaccount`, `getnowblock`, or any other wallet API an operator wants to restrict for a public-facing full/solidity/PBFT node) [4](#0-3) . Because the mismatch is in the string-splitting logic rather than in the servlet's own routing, an anonymous or registered API client can bypass this restriction entirely by inserting an extra `/` in the path, defeating the operator's intended access control and reaching data/functionality that was deliberately disabled. This matches the "Medium" severity class of the reference CVE (unauthorized enumeration/access to restricted resources over the network, no direct fund theft or RCE).

### Likelihood Explanation
The bypass requires only a single crafted HTTP GET/POST to a public `/wallet`, `/walletsolidity`, or `/walletpbft` port — no authentication, no special privileges, and no chain state manipulation. Any anonymous API client reachable over the node's HTTP interface can attempt this immediately, making likelihood high wherever the operator relies on `disabledApiList` for access restriction.

### Recommendation
Normalize the path before splitting: collapse consecutive slashes (e.g. `endpoint.replaceAll("/+", "/")`) in addition to the existing `URI.normalize()` call in `isDisabled()`, or switch to parsing path segments via `HttpServletRequest.getPathInfo()`/a proper URI path-segment API rather than naive `split("/")` indexing, so the access-control parsing matches how the underlying servlet dispatch resolves the same path.

### Proof of Concept
1. Configure the node with `vm.disabledApiList` (or equivalent) containing `getnowblock`.
2. Confirm `GET /wallet/getnowblock` returns `{"Error":"this API is unavailable due to config"}` (matches existing test behavior) [5](#0-4) .
3. Send `GET /wallet//getnowblock` (double slash) to the same node.
4. `isDisabled()` computes `endpoint.split("/")[2]` = `""`, which is not in `disabledApiList`, so the filter forwards the request; if the underlying servlet mapping tolerates the extra slash and still routes to the `getnowblock` handler, the response returns normal block data instead of the "unavailable due to config" error, demonstrating the bypass of the operator-configured restriction.

### Citations

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L26-41)
```java
  @Override
  public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain) {
    try {
      if (request instanceof HttpServletRequest) {
        String contextPath = ((HttpServletRequest) request).getContextPath();
        String endpoint = contextPath + ((HttpServletRequest) request).getServletPath();
        HttpServletResponse resp = (HttpServletResponse) response;

        if (isDisabled(endpoint)) {
          resp.setStatus(HttpServletResponse.SC_NOT_FOUND);
          resp.setContentType("application/json; charset=utf-8");
          JSONObject jsonObject = new JSONObject();
          jsonObject.put("Error", "this API is unavailable due to config");
          resp.getWriter().println(jsonObject.toJSONString());
          return;
        }
```

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

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L51-90)
```java
  @Test
  public void testHttpFilter() {
    appT.startup();
    List<String> disabledApiList = new ArrayList<>();
    disabledApiList.add("getaccount");
    disabledApiList.add("getnowblock");

    List<String> emptyList = Collections.emptyList();

    List<String> patterns = new ArrayList<>();
    patterns.add("/walletsolidity/");
    patterns.add("/walletpbft/");
    patterns.add("/wallet/");

    int httpPort;
    String ip = "127.0.0.1";
    for (String api : disabledApiList) {
      for (String pattern : patterns) {
        String urlPath = pattern + api;
        if (urlPath.contains("/walletsolidity")) {
          httpPort = Args.getInstance().getSolidityHttpPort();
        } else if (urlPath.contains("/walletpbft")) {
          httpPort = Args.getInstance().getPBFTHttpPort();
        } else {
          httpPort = Args.getInstance().getFullNodeHttpPort();
        }

        String url = String.format("http://%s:%d%s", ip, httpPort, urlPath);

        Args.getInstance().setDisabledApiList(disabledApiList);
        String response = sendGetRequest(url);
        Assert.assertEquals("{\"Error\":\"this API is unavailable due to config\"}",
            response);

        Args.getInstance().setDisabledApiList(emptyList);
        int statusCode = getRequestCode(url);
        Assert.assertEquals(HttpStatus.SC_OK, statusCode);
      }
    }
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

**File:** common/src/main/java/org/tron/common/parameter/CommonParameter.java (L1-1)
```java
package org.tron.common.parameter;
```
