[1](#0-0) No response headers controlling caching are ever set in the HTTP servlet base class, and no `Cache-Control`/`Pragma`/`Expires` header is set anywhere in the codebase's HTTP API layer.

### Title
Missing Cache-Control Headers on FullNode HTTP API Allows CDN Caching and Cross-User Disclosure of Freshly Generated Shielded Keys - (File: framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java)

### Summary
Every FullNode HTTP servlet is dispatched through `RateLimiterServlet.service()`, which only sets `Content-Type: application/json; charset=utf-8` on the response and never emits `Cache-Control`, `Pragma`, or `Expires` headers. [2](#0-1)  Several GET-serviceable endpoints generate fresh, secret key material on every invocation with no request-specific parameters in the URL, e.g. `GetSpendingKeyServlet.doGet` calling `wallet.getSpendingKey()` [3](#0-2)  and `GetNewShieldedAddressServlet.doGet` calling `wallet.getNewShieldedAddress()`. [4](#0-3)  Both are registered as plain GET routes on the FullNode HTTP API (`/wallet/getspendingkey`, `/wallet/getnewshieldedaddress`). [5](#0-4) 

### Finding Description
Because these endpoints are identical GET URLs with no query parameters distinguishing one caller's request from another's, any CDN, reverse proxy, or intermediary cache placed in front of a public FullNode HTTP API (a very common production deployment pattern for TRON node operators) will, absent an explicit `Cache-Control: no-store` directive, treat the response as cacheable by default for that URL. Since `RateLimiterServlet.service()` never sets any caching-related header, the JSON response containing a freshly generated shielded spending key or shielded payment address can be cached on the first request and replayed verbatim to every subsequent client that requests the same path — exactly the CWE-525 "Use of Web Browser Cache Containing Sensitive Information" class described in the referenced advisory, but here the sensitive artifact is not a session cookie but raw private key material returned in the JSON body itself.

### Impact Explanation
An unprivileged anonymous HTTP client interacting with a cached CDN/proxy layer would receive another user's previously cached shielded spending key or shielded address instead of a freshly generated one. Because the shielded spending key directly controls shielded TRC-20/TRX shielded balances, disclosure of a stale cached key to a different requester constitutes key disclosure and can lead directly to theft of shielded funds by whichever party first requests (and thus "owns" the cache entry for) that URL, or by an attacker who deliberately warms the cache and waits for a victim's client/wallet integration to fetch the same cached endpoint.

### Likelihood Explanation
Any operator fronting the public wallet HTTP API with a CDN or shared caching reverse proxy (a standard scaling practice for public blockchain RPC endpoints) is exposed by default, since java-tron does not instruct downstream caches not to store these responses. No authentication or special privilege is required to trigger the request; it only requires a plain `GET /wallet/getspendingkey` or `GET /wallet/getnewshieldedaddress` call.

### Recommendation
Explicitly set `Cache-Control: no-store, no-cache` (and `Pragma: no-cache`) on all HTTP API responses in `RateLimiterServlet.service()`, or at minimum on servlets that return secret/key material (`GetSpendingKeyServlet`, `GetNewShieldedAddressServlet`, `GetDiversifierServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, `GetRcmServlet`), so that intermediary caches never store these responses.

### Proof of Concept
1. Deploy a FullNode with the HTTP API exposed behind a caching CDN/reverse proxy configured with default caching rules for GET requests (a common deployment topology).
2. Client A issues `GET /wallet/getspendingkey`; the CDN caches the JSON response since no `Cache-Control` header forbids it.
3. Client B issues the identical `GET /wallet/getspendingkey` request; the CDN serves the cached response instead of forwarding to the node, returning Client A's previously generated spending key to Client B.
4. Client B now controls the same shielded spending key material as Client A and can spend/track Client A's shielded funds derived from that key.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L103-136)
```java
  @Override
  protected void service(HttpServletRequest req, HttpServletResponse resp)
      throws ServletException, IOException {

    RuntimeData runtimeData = new RuntimeData(req);
    IRateLimiter rateLimiter = container.get(KEY_PREFIX_HTTP, getClass().getSimpleName());

    // Check per-endpoint first to avoid consuming global IP/QPS quota for requests
    // that would be rejected by the per-endpoint limiter anyway. acquirePermit()
    // chooses blocking or non-blocking semantics based on rate.limiter.apiNonBlocking.
    boolean perEndpointAcquired = rateLimiter == null || rateLimiter.acquirePermit(runtimeData);
    boolean acquireResource = perEndpointAcquired && GlobalRateLimiter.acquirePermit(runtimeData);

    String contextPath = req.getContextPath();
    String url = Strings.isNullOrEmpty(req.getServletPath())
        ? MetricLabels.UNDEFINED : contextPath + req.getServletPath();
    // int64_as_string is honored only on GET requests (URL query). POST is intentionally
    // unsupported because reading the body here would consume request.getReader() and
    // break downstream servlets that read it themselves.
    if ("GET".equalsIgnoreCase(req.getMethod())) {
      JsonFormat.setInt64AsString(Util.getInt64AsString(req));
    }
    try {
      resp.setContentType("application/json; charset=utf-8");

      if (acquireResource) {
        Histogram.Timer requestTimer = Metrics.histogramStartTimer(
            MetricKeys.Histogram.HTTP_SERVICE_LATENCY, url);
        super.service(req, resp);
        Metrics.histogramObserve(requestTimer);
      } else {
        resp.getWriter()
            .println(Util.printErrorMsg(new IllegalAccessException("lack of computing resources")));
      }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetSpendingKeyServlet.java (L19-27)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      BytesMessage reply = wallet.getSpendingKey();
      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetNewShieldedAddressServlet.java (L18-27)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      ShieldedAddressInfo reply = wallet.getNewShieldedAddress();

      response.getWriter().println(JsonFormat.printToString(reply, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L421-425)
```java
    context.addServlet(new ServletHolder(getNkFromNskServlet), "/wallet/getnkfromnsk");
    context.addServlet(new ServletHolder(getSpendingKeyServlet), "/wallet/getspendingkey");
    context
        .addServlet(new ServletHolder(getNewShieldedAddressServlet),
            "/wallet/getnewshieldedaddress");
```
