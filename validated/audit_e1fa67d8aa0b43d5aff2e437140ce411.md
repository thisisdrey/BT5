### Title
Shielded Transaction Key Material (Spending Key / Ask / Ak / Nk) Accepted via GET URL Query Parameters, Exposing Sensitive Key Data to Server Logs and Intermediaries - ([File: framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java])

### Summary
Several FullNode HTTP API servlets that operate on shielded-transaction cryptographic key material accept that key material via `doGet` using `HttpServletRequest.getParameter(...)`, i.e., as URL query-string parameters, in addition to supporting the same values via POST body. This mirrors the PinchTab bug class (CWE-598): sensitive secrets transported in the URL query string are liable to be captured by reverse proxies, load-balancer/web-server access logs, browser history, shell/`curl` history, and request-tracing systems — none of which are aware that the "value" being logged is a private spending-key component.

### Finding Description
`GetExpandedSpendingKeyServlet.doGet` reads a raw spending key from the `value` query parameter and passes it to `wallet.getExpandedSpendingKey`: [1](#0-0) 

`GetAkFromAskServlet.doGet` reads the `ask` component (via the `value` parameter) from the query string and derives the `ak`: [2](#0-1) 

`GetIncomingViewingKeyServlet.doGet` reads both `ak` and `nk` viewing-key components directly from query parameters: [3](#0-2) 

All three servlets are registered on the FullNode HTTP API and are reachable by any anonymous HTTP client that can reach the node's HTTP port (default `8090`, `node.http.fullNodeEnable = true`): [4](#0-3) 

The project's own configuration documentation already acknowledges that shielded-transaction-related APIs can leak private keys and recommends invoking them only locally: [5](#0-4) 

However, that guidance addresses *who* should call these APIs, not *how* the secret is transported. Because these `doGet` handlers accept the exact same sensitive values (`ask`, `ak`, `nk`, expanded spending key seed) via the query string as they do via POST body, any client using `curl "http://node:8090/wallet/getexpandedspendingkey?value=<ask>"` (or any GUI/browser-based tool that assembles such a GET URL) causes the raw key material to appear in the full request URI. Unlike POST bodies, GET query strings are the canonical thing that reverse proxies (e.g., nginx/HAProxy access logs), APM/tracing systems, browser history, and shell history capture and retain — exactly the exposure vector described in the PinchTab advisory (CWE-598).

### Impact Explanation
If an operator (or a monitoring/tracing/proxy layer sitting in front of the FullNode HTTP API) captures full request URIs — a very common default for reverse proxies and APM tools — the `ask`/expanded spending key/`ak`/`nk` values used to derive shielded addresses and viewing/spending authority are persisted in plaintext logs. Recovery of a spending key or its expansion (`ask`, `nsk`, `ovk`) enables an attacker to spend shielded funds tied to that key, i.e., direct key disclosure leading to unauthorized shielded-fund control. This satisfies the "key disclosure" / "unauthorized account operation" acceptance criteria.

### Likelihood Explanation
Exploitation requires (a) a deployment with the shielded transaction HTTP API reachable (guarded by `node.allowShieldedTransactionApi`, off by default) and (b) a client or intermediary that constructs/logs the GET form of these endpoints instead of POST. This is analogous to the original PinchTab finding: it is not a direct bypass, but any first-party tooling, documentation example, or wrapper script that calls these endpoints with `curl -G` or browser-based GET requests creates realistic, low-friction exposure through standard logging/proxy infrastructure. Given the endpoints exist specifically to accept these parameters via GET as a first-class, documented interface (not merely an oversight limited to internal testing), the likelihood is non-trivial for any environment where the shielded API is enabled and network-accessible.

### Recommendation
1. Remove `doGet` support (or reject GET requests) for all shielded-key-related servlets (`GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetIncomingViewingKeyServlet`, and any sibling servlets handling `ask`/`ak`/`nk`/spending-key values), requiring POST-body submission only.
2. Ensure the HTTP access logger and any downstream reverse-proxy guidance explicitly warn against logging full query strings for the shielded API path set.
3. Add server-side validation that rejects sensitive key parameters supplied via query string even if accidentally sent, returning an explicit error directing callers to use POST.

### Proof of Concept
1. Enable the shielded transaction API (`node.allowShieldedTransactionApi = true`) and start a FullNode with `node.http.fullNodeEnable = true` on port `8090`.
2. Issue: `curl -i "http://<node-ip>:8090/wallet/getexpandedspendingkey?value=<hex-encoded-spending-key>"` — the servlet's `doGet` accepts and processes the key material from the URL query string, per `GetExpandedSpendingKeyServlet.java:22-26`.
3. Any reverse proxy, load balancer, or tracing system placed in front of the node that logs full request URIs (a common default) will now contain the plaintext spending-key value in its logs, enabling later key disclosure and theft of shielded funds by anyone with access to those logs.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/http/GetAkFromAskServlet.java (L20-30)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String input = request.getParameter("value");
      BytesMessage reply = wallet
          .getAkFromAsk(ByteString.copyFrom(ByteArray.fromHexString(input)));
      response.getWriter().println(JsonFormat.printToString(reply, visible));
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

**File:** common/src/main/resources/reference.conf (L211-216)
```text
  # Calling these APIs on untrusted or remote nodes may leak your private keys.
  # It is recommended to invoke them locally for development and testing.
  # To opt in, set: allowShieldedTransactionApi = true
  # Migration: the legacy key node.fullNodeAllowShieldedTransaction is still supported
  # but deprecated; please migrate to node.allowShieldedTransactionApi.
  # allowShieldedTransactionApi = false
```

**File:** common/src/main/resources/reference.conf (L266-270)
```text
  # HTTP API settings.
  http {
    fullNodeEnable = true # Whether to enable FullNode HTTP API.
    fullNodePort = 8090  # FullNode HTTP API port.
    solidityEnable = true # Whether to enable Solidity HTTP API.
```
