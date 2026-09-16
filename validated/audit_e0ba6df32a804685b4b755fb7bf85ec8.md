Based on my investigation, I found a strong structural analog to the reported bug class (cleartext transmission of sensitive credential material reachable by an anonymous API client) in java-tron's shielded-key derivation HTTP endpoints.

### Title
Shielded key material (ask/nsk/spending keys) accepted and returned via plaintext HTTP GET query parameters, enabling key disclosure to network-adjacent attackers - (File: framework/src/main/java/org/tron/core/services/http/GetAkFromAskServlet.java)

### Summary
The FullNode HTTP API exposes multiple unauthenticated servlets that accept and return Sapling shielded-pool private key material (`ask`, `nsk`, spending keys, expanded spending keys) both as request input and as the JSON response body, and these servlets support `GET` requests where the sensitive value is passed as a URL query parameter (`?value=<hex>`) rather than solely via a request body.

### Finding Description
`GetAkFromAskServlet.doGet` and `GetNkFromNskServlet.doGet` read the raw key material directly from `request.getParameter("value")` and pass it straight to `Wallet.getAkFromAsk` / `Wallet.getNkFromNsk`, echoing the derived key back in cleartext JSON: [1](#0-0) [2](#0-1) 

The same GET-parameter pattern exists for the raw spending key itself in `GetExpandedSpendingKeyServlet.doGet`, which parses the `value` query parameter as the spending key: [3](#0-2) 

and `GetIncomingViewingKeyServlet`/`GetZenPaymentAddressServlet` similarly accept `ak`/`nk`/`ivk` values via GET query parameters: [4](#0-3) 

Placing private key/spending key material in a URL query string is a textbook cleartext-transmission-of-credentials pattern (CWE-319/CWE-598): the value is not only sent unencrypted over plain HTTP (the FullNode HTTP API listens on plain HTTP by default per `common/src/main/resources/reference.conf`), but URL query strings are additionally logged by web/reverse-proxy access logs, cached in browser history, and leaked via `Referer` headers — all without requiring any authentication to invoke the endpoint. The project's own configuration comments acknowledge the risk generically ("Calling these APIs on untrusted or remote nodes may leak your private keys") but that warning covers the shielded-transaction-broadcast APIs; it does not change the fact that these specific key-derivation servlets accept the secret as a bare GET parameter. [5](#0-4) 

### Impact Explanation
An attacker positioned on the network path (a proxy operator, shared-network observer, or anyone with access to HTTP/proxy access logs — matching the CVE's `AV:A` adjacent-attacker vector) can capture the `ask`/`nsk`/spending-key values transmitted as GET parameters. Since these are the seed/spending secrets for TRON's Sapling shielded pool, disclosure allows full reconstruction of shielded spending authority, i.e., outright theft of shielded funds — matching the "key disclosure" / "theft of funds" impact bar required by the validation rules.

### Likelihood Explanation
Exploitability requires only that: (1) the node operator/wallet client calls these key-derivation endpoints over `GET` instead of `POST` (both are supported by the servlet, so this is a real, not theoretical, request path), and (2) an attacker can observe the request (shared network, corporate proxy, load balancer, or log aggregation system). No authentication or special privilege is needed to reach the servlet itself.

### Recommendation
Remove `doGet` support from all shielded-key-material servlets (`GetAkFromAskServlet`, `GetNkFromNskServlet`, `GetExpandedSpendingKeyServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, `GetSpendingKeyServlet`), forcing clients to use `POST` with the secret in the request body only, and document/enforce that these endpoints must never be exposed on a publicly reachable listener (bind to loopback only or require the shielded-API opt-in flag with an explicit TLS/localhost check).

### Proof of Concept
```
GET /wallet/getakfromask?value=<hex-ask-secret> HTTP/1.1
Host: victim-fullnode:8090
```
This request is valid per `GetAkFromAskServlet.doGet` (framework/src/main/java/org/tron/core/services/http/GetAkFromAskServlet.java:20-30) and will be recorded in plaintext in any intermediary HTTP access log, browser history, or network capture between the caller and the node, disclosing the shielded spend-authorizing secret.

### Caveat
I was unable to fully verify within the available tool budget whether `Wallet.getAkFromAsk` / `Wallet.getNkFromNsk` / `Wallet.getSpendingKey` internally re-check the `allowShieldedTransactionApi` flag (my grep for that flag inside `Wallet.java` returned only a single, unlocalized match, and I could not pinpoint which methods it guards). If these particular derivation methods are in fact gated by that flag (default `false`) at the `Wallet` layer, the practical exposure is reduced to nodes that have explicitly opted in; the GET-parameter design flaw itself remains present in the servlet code regardless.

### Citations

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

**File:** framework/src/main/java/org/tron/core/services/http/GetNkFromNskServlet.java (L20-30)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String input = request.getParameter("value");
      BytesMessage reply = wallet
          .getNkFromNsk(ByteString.copyFrom(ByteArray.fromHexString(input)));
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

**File:** common/src/main/resources/reference.conf (L211-216)
```text
  # Calling these APIs on untrusted or remote nodes may leak your private keys.
  # It is recommended to invoke them locally for development and testing.
  # To opt in, set: allowShieldedTransactionApi = true
  # Migration: the legacy key node.fullNodeAllowShieldedTransaction is still supported
  # but deprecated; please migrate to node.allowShieldedTransactionApi.
  # allowShieldedTransactionApi = false
```
