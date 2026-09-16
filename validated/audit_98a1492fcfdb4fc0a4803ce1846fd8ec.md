I have sufficient evidence to confirm this analog: java-tron's shielded-transaction HTTP servlets accept sensitive spending/viewing key material via `GET` query parameters, mirroring the QloApps CVE-2025-26058 pattern of sensitive secrets exposed via URL.

### Title
Shielded transaction secret keys (spending key, expanded spending key, ask/nsk) accepted via GET URL query parameters, causing disclosure through logs/proxies/history - ([File: framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java])

### Summary
Several `wallet` HTTP API servlets that operate on shielded-pool secret key material accept the secret value as a `GET` query string parameter (`?value=<secret>`), in addition to supporting it over `POST`. This is the same bug class as CVE-2025-26058: secrets that should never leave the client except over a body are instead placed on the URL, where they are captured by web/proxy access logs, browser/CLI history, and `Referer` headers.

### Finding Description
`GetExpandedSpendingKeyServlet.doGet` reads the shielded pool expanded spending key directly from `request.getParameter("value")`: [1](#0-0) 

The same GET-parameter pattern is used to submit other shielded secret/authority material:
- `GetAkFromAskServlet.doGet` takes the spend authorizing key `ask` as `value` on GET. [2](#0-1) 
- `GetNkFromNskServlet.doGet` takes the nullifier deriving seed `nsk` as `value` on GET. [3](#0-2) 

These endpoints are all registered on the full node's public HTTP API surface (default enabled, reachable by any client that can send a request to the node's HTTP port): [4](#0-3) 

`RateLimiterServlet.service`, the shared dispatcher these servlets extend, logs the request URL (`req.getServletPath()`/contextPath) via `logger.error` on unexpected exceptions and computes metrics keyed on `url`, but critically the underlying request handling does not strip or avoid capturing the query string anywhere in the stack — the node relies on the embedded Jetty `Server`/`ServletContextHandler` configured in `HttpService.initServer`/`initContextHandler`, which uses Jetty defaults: [5](#0-4) 

Any deployment that enables standard Jetty NCSA-style access logging, or that places the node behind a reverse proxy/load balancer/CDN (all common production topologies), will capture the full request URI **including the query string** for every `GET` request. Because `value` here is the raw hex/base58 secret key material for a shielded address, this means the spend authority for a shielded balance is written verbatim into any access log, proxy log, or intermediate network device log that records request lines — exactly the "sensitive token exposed via URL" class described in CVE-2025-26058.

### Impact Explanation
An attacker with access to any HTTP intermediary or log store between client and node (reverse proxy, CDN edge log, corporate web gateway, shared hosting access log, or even browser history if a user manually queries the API) can recover the raw expanded spending key / `ask` / `nsk` for a shielded TRON address. Possession of the spending key is sufficient to derive spend authority and construct valid spend proofs/authorization signatures for the corresponding shielded notes, i.e., **theft of shielded funds** — this is a concrete "unauthorized account operation / theft of funds" outcome, not merely an information leak with no impact.

### Likelihood Explanation
Exploitation requires no special privilege beyond the ability to observe logs or network traffic between the caller and the node (e.g., operating a proxy in front of the node, or having log access on infrastructure the node runs behind), which is common in production deployments (reverse proxies, load balancers, hosting providers). The vulnerable pattern is reachable by any client since it is a documented public `wallet` API and the GET path requires no authentication. However, actual capture of the secret depends on the deployment logging or proxying GET query strings, so real-world exploitability is deployment-dependent, which is reflected in the CVE's own Medium/4.2 rating and `PR:H` requirement (attacker needs some position enabling log/traffic observation).

### Recommendation
Remove the `GET` handlers (or the `value` query-parameter path) for all shielded secret-key servlets — `GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetNkFromNskServlet`, `GetSpendingKeyServlet`'s consumers, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet` — and require `POST` with body-only transmission, consistent with how credentials/secrets should never be placed in a URL. If GET must be retained for backward compatibility, document it as deprecated/insecure and disable it by default, and ensure any front-end proxy/log configuration redacts the `value` parameter.

### Proof of Concept
1. Deploy a `java-tron` full node with the HTTP API enabled and fronted by a reverse proxy that logs request lines (default Nginx/Apache access log behavior).
2. Send `GET http://<node>:8090/wallet/getexpandedspendingkey?value=<hex-spending-key>&visible=true`.
3. Observe the proxy/access log entry containing the full URL, including `value=<hex-spending-key>` in plaintext.
4. Use the disclosed key to derive spend authority over the corresponding shielded address, or replay/decrypt shielded notes belonging to the victim.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java (L22-26)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String sk = request.getParameter("value");
      fillResponse(visible, ByteString.copyFrom(ByteArray.fromHexString(sk)), response);
```

**File:** framework/src/main/java/org/tron/core/services/http/GetAkFromAskServlet.java (L20-26)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String input = request.getParameter("value");
      BytesMessage reply = wallet
          .getAkFromAsk(ByteString.copyFrom(ByteArray.fromHexString(input)));
      response.getWriter().println(JsonFormat.printToString(reply, visible));
```

**File:** framework/src/main/java/org/tron/core/services/http/GetNkFromNskServlet.java (L20-26)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String input = request.getParameter("value");
      BytesMessage reply = wallet
          .getNkFromNsk(ByteString.copyFrom(ByteArray.fromHexString(input)));
      response.getWriter().println(JsonFormat.printToString(reply, visible));
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L174-181)
```java
  @Autowired
  private GetExpandedSpendingKeyServlet getExpandedSpendingKeyServlet;
  @Autowired
  private GetAkFromAskServlet getAkFromAskServlet;
  @Autowired
  private GetNkFromNskServlet getNkFromNskServlet;
  @Autowired
  private GetSpendingKeyServlet getSpendingKeyServlet;
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
