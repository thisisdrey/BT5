### Title
Shielded-wallet key material (`sk`/`ask`/`nk`/`ivk`) transmitted and generated over plaintext, unauthenticated HTTP endpoints - (File: framework/src/main/java/org/tron/core/services/http/GetSpendingKeyServlet.java)

### Summary
The FullNode HTTP API is served by a plain Jetty `Server` with no TLS/`SslContextFactory` configured anywhere in `HttpService`/`FullNodeHttpApiService`, meaning `node.jsonrpc`/`node.http` traffic is unencrypted by default unless a reverse proxy adds TLS. Several shielded-wallet servlets exchange raw spend/viewing key material over this unauthenticated, unencrypted channel, analogous to the yarn CVE-2019-5448 pattern where sensitive authentication data traveled in cleartext over plain HTTP.

### Finding Description
`FullNodeHttpApiService` registers dozens of `/wallet/...` servlets on a bare Jetty `Server` created in `HttpService.initServer()`, which never attaches an `SslContextFactory` or enforces HTTPS: [1](#0-0) 

Endpoints such as `GetSpendingKeyServlet`, `GetExpandedSpendingKeyServlet`, `GetNkFromNskServlet`, `GetIncomingViewingKeyServlet`, and `GetAkFromAskServlet` accept or return raw shielded spending/viewing key bytes (`sk`, `ask`, `nsk`, `ak`, `nk`, `ivk`) directly as GET query parameters or JSON POST bodies and echo them back in the plaintext HTTP response: [2](#0-1) [3](#0-2) [4](#0-3) 

These servlets are registered without any additional authentication/authorization filter distinguishing them from public read-only endpoints — they are reached the same way as any other `/wallet/*` call: [5](#0-4) 

Because `GetSpendingKeyServlet.doGet` returns `wallet.getSpendingKey()` in cleartext to whoever issues the GET request, and because the server transport itself has no default encryption, any network position between the client and the node (proxy, load balancer, shared LAN, misconfigured deployment served over plain HTTP as documented in `docs/configuration.md`) can capture the full shielded spending key — the equivalent of stealing signing credentials, mirroring the yarn advisory's root cause (secrets sent unencrypted over HTTP).

### Impact Explanation
A shielded spending key (`sk`) is the sole secret required to spend all funds held at the corresponding shielded (Sapling) address. If the plaintext HTTP transport is intercepted (MITM, misconfigured reverse proxy, shared infrastructure, or a public RPC endpoint reachable over `http://` rather than `https://` as shown in the node's own JSON-RPC/HTTP config examples), an attacker gains full unauthorized spend authority over the victim's shielded balance — concrete theft of funds, matching the "unauthorized account operation / theft of funds" bar.

### Likelihood Explanation
Exploitability depends entirely on deployment topology: many operators run FullNode's `/wallet` HTTP API directly over plain HTTP (the default, since the codebase supplies no TLS wiring), often behind load balancers or on shared networks, and the shielded-key endpoints (`getspendingkey`, `getexpandedspendingkey`, `getincomingviewingkey`, etc.) exist specifically so wallet clients can request key material remotely. Any node operator or wallet integrator who does not manually front the API with TLS is exposed to passive network interception, which is a realistic and common misconfiguration for blockchain full-node RPC/HTTP deployments.

### Recommendation
- Do not implement key generation/derivation server-side over the network API; shielded key material (`sk`, `ask`, `nsk`, `ivk`, etc.) should be generated and held client-side only, never round-tripped through the FullNode HTTP/gRPC interface.
- If these endpoints must remain for backward compatibility, bind them to loopback-only interfaces by default, require explicit opt-in plus authentication (e.g., mutual TLS or API keys) before enabling them externally, and document in `docs/configuration.md` that operators must terminate TLS in front of `node.http`/`node.jsonrpc` before exposing them.
- Add these endpoints to a "sensitive" category that is disabled by default via `node.disabledApi` and warn loudly if enabled without HTTPS termination.

### Proof of Concept
1. Deploy a FullNode with the default configuration (`node.http` enabled, no TLS termination in front of it, as is the out-of-the-box behavior since `HttpService.initServer()` never configures SSL).
2. From a network position that can observe traffic to the node's HTTP port (e.g., shared cloud network, transparent proxy, or ARP-spoofed LAN), have a legitimate user call:
   `GET http://<node-ip>:8090/wallet/getspendingkey`
   or a client submits an existing key via
   `GET http://<node-ip>:8090/wallet/getexpandedspendingkey?value=<sk_hex>`
3. Capture the plaintext HTTP response/request containing the `sk`/`ask`/`nsk` fields returned by `GetSpendingKeyServlet`/`GetExpandedSpendingKeyServlet`.
4. Use the captured spending key to construct and broadcast a shielded transfer draining the victim's shielded balance via `wallet/createshieldedtransaction`.

### Citations

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L79-86)
```java
  protected void initServer() {
    this.apiServer = new Server(this.port);
    int maxHttpConnectNumber = Args.getInstance().getMaxHttpConnectNumber();
    if (maxHttpConnectNumber > 0) {
      this.apiServer.addBean(new ConnectionLimit(maxHttpConnectNumber, this.apiServer));
    }
    this.apiServer.setErrorHandler(new OversizedRequestErrorHandler());
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

**File:** framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java (L22-30)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String sk = request.getParameter("value");
      fillResponse(visible, ByteString.copyFrom(ByteArray.fromHexString(sk)), response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetIncomingViewingKeyServlet.java (L34-44)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String ak = request.getParameter("ak");
      String nk = request.getParameter("nk");

      fillResponse(visible, ak, nk, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L172-201)
```java
  @Autowired
  private GetAccountByIdServlet getAccountByIdServlet;
  @Autowired
  private GetExpandedSpendingKeyServlet getExpandedSpendingKeyServlet;
  @Autowired
  private GetAkFromAskServlet getAkFromAskServlet;
  @Autowired
  private GetNkFromNskServlet getNkFromNskServlet;
  @Autowired
  private GetSpendingKeyServlet getSpendingKeyServlet;
  @Autowired
  private GetNewShieldedAddressServlet getNewShieldedAddressServlet;
  @Autowired
  private GetDiversifierServlet getDiversifierServlet;
  @Autowired
  private GetIncomingViewingKeyServlet getIncomingViewingKeyServlet;
  @Autowired
  private GetZenPaymentAddressServlet getZenPaymentAddressServlet;
  @Autowired
  private CreateShieldedTransactionServlet createShieldedTransactionServlet;
  @Autowired
  private ScanNoteByIvkServlet scanNoteByIvkServlet;
  @Autowired
  private ScanAndMarkNoteByIvkServlet scanAndMarkNoteByIvkServlet;
  @Autowired
  private ScanNoteByOvkServlet scanNoteByOvkServlet;
  @Autowired
  private GetRcmServlet getRcmServlet;
  @Autowired
  private CreateSpendAuthSigServlet createSpendAuthSigServlet;
```
