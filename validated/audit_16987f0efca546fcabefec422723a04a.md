### Title
HTTP API-disablement/authorization bypass via naive path parsing in `HttpApiAccessFilter.isDisabled()` - (File: `framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java`)

### Summary
`HttpApiAccessFilter` is the node operator's mechanism to block access to specific API endpoints (`node.disabledApi`) across the FullNode/Solidity/PBFT HTTP interfaces [1](#0-0) . Just like the Next.js middleware vulnerability where authorization decisions made on a raw `pathname` string could be bypassed by URLs that Next.js's router still resolves to the protected route but that the middleware's string match fails to recognize, `isDisabled()` derives its decision from a hand-rolled `URI.create(endpoint).normalize()` + `split("/")[2]` parse of the servlet path rather than relying on the actual resolved servlet/route identity [2](#0-1) .

### Finding Description
`isDisabled(endpoint)` takes `contextPath + getServletPath()`, calls `URI.create(endpoint).normalize()`, and then indexes `split("/")[2]` to extract the presumed API name for comparison against the configured `disabledApiList` [3](#0-2) . `URI.normalize()` only collapses `.`/`..` dot-segments; it does not collapse redundant empty segments (e.g. `//`) nor decode/re-normalize percent-encoded slashes. A path such as `/wallet//getaccount` normalizes to itself, and `split("/")` on it yields `["", "wallet", "", "getaccount"]`, so `split("/")[2]` is the empty string `""` instead of `"getaccount"` — the comparison against `disabledApiList` silently fails even though the request still targets the same servlet if Jetty's servlet path-mapping tolerates the doubled slash for an exact-path mapping. The existing regression test only validates dot-segment collapsing (`/wallet/a/../b/../getnowblock`), confirming that this class of "path string used for an authorization decision differs from the actual routed resource" bug was already on the team's radar for one canonicalization case but not for others such as duplicate slashes or encoded separators [4](#0-3) . The filter itself is wired as `/*` (or `/wallet/*` etc.) ahead of the actual servlets in `FullNodeHttpApiService`, `SolidityNodeHttpApiService`, `HttpApiOnSolidityService`, and `HttpApiOnPBFTService`, so it is the sole gate for the disabled-API policy on every deployment topology [5](#0-4) .

The gRPC analog, `RpcApiAccessInterceptor.isDisabled()`, uses `call.getMethodDescriptor().getFullMethodName()` which is a fixed, protocol-derived identifier rather than an attacker-influenced string, so it is not vulnerable to the same class of bypass [6](#0-5) .

### Impact Explanation
An operator who disables a sensitive HTTP endpoint via `node.disabledApi` (e.g. to hide account/asset/market data or restrict administrative queries) relies on `HttpApiAccessFilter` as the enforcement point. If the underlying servlet mapping still routes a malformed variant of the path (duplicate slash, encoded slash, extra empty segment) to the intended servlet while `isDisabled()`'s naive parsing fails to match it, the operator's access-control decision is silently bypassed, exposing an endpoint that was explicitly configured to be unreachable — the exact bug class described in the Next.js advisory (path-based authorization decision diverges from the actual resolved route).

### Likelihood Explanation
Exploitability is contingent on whether Jetty's servlet container still dispatches the exact-path-mapped servlet for a request URI containing doubled slashes or encoded path separators — this depends on Jetty's URI compliance/ambiguous-path handling for the deployed version, which was not confirmed to allow such dispatch in this codebase. No `UriCompliance`/ambiguous-path hardening configuration was found in `HttpService.java`'s server setup [7](#0-6) , leaving the actual bypass reachability dependent on the Jetty version's default behavior rather than proven end-to-end in this review.

### Recommendation
Base the disabled-API decision on the actually-resolved servlet/handler identity (e.g. compare against `HttpServletRequest.getServletPath()` after Jetty's own canonicalization, or match by registered servlet class/name) rather than re-deriving it via ad-hoc `URI.normalize()` + `split("/")`. Additionally, harden the Jetty server configuration to reject ambiguous URIs (duplicate slashes, encoded separators) at the connector level so no downstream component sees divergent path representations of the same route.

### Proof of Concept
1. Configure `node.disabledApi = ["getaccount"]`.
2. Confirm `GET /wallet/getaccount` is blocked with `{"Error":"this API is unavailable due to config"}` as covered by the existing test [8](#0-7) .
3. Send `GET /wallet//getaccount` (or another path variant containing an empty/duplicate segment). Trace `isDisabled()`: `endpoint = "/wallet//getaccount"`, `URI.normalize()` leaves it unchanged, `split("/")` produces `["", "wallet", "", "getaccount"]`, so `split("/")[2]` is `""`, which does not match `"getaccount"` in `disabledApiList`, so `isDisabled()` returns `false` and the request is forwarded to the chain [2](#0-1) .
4. If the Jetty servlet mapping for `/wallet/getaccount` still resolves this malformed path to `GetAccountServlet`, the disabled endpoint is served despite the operator's configuration, confirming the bypass.

### Citations

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L26-53)
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

        CharResponseWrapper responseWrapper = new CharResponseWrapper(resp);
        chain.doFilter(request, responseWrapper);

      } else {
        chain.doFilter(request, response);
      }

    } catch (Exception e) {
      logger.error("http api access filter exception: {}", e.getMessage());
    }
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

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L79-95)
```java
  protected void initServer() {
    this.apiServer = new Server(this.port);
    int maxHttpConnectNumber = Args.getInstance().getMaxHttpConnectNumber();
    if (maxHttpConnectNumber > 0) {
      this.apiServer.addBean(new ConnectionLimit(maxHttpConnectNumber, this.apiServer));
    }
    this.apiServer.setErrorHandler(new OversizedRequestErrorHandler());
  }

  protected ServletContextHandler initContextHandler() {
    ServletContextHandler context = new ServletContextHandler(ServletContextHandler.SESSIONS);
    context.setContextPath(this.contextPath);
    SizeLimitHandler sizeLimitHandler = new SizeLimitHandler(this.maxRequestSize, -1);
    sizeLimitHandler.setHandler(context);
    this.apiServer.setHandler(sizeLimitHandler);
    return context;
  }
```
