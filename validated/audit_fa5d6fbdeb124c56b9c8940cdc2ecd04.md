Confirmed these are live registered endpoints in `FullNodeHttpApiService`, each supporting `doGet` where the sensitive value (`ask`, `nsk`, `ak`/`nk`, `ivk`/`d`) is read from `request.getParameter(...)`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

### Title
Shielded key material (`ask`/`nsk`/`ak`/`nk`/`ivk`) accepted via GET query parameters, causing disclosure through access logs and browser/proxy history - (File: `framework/src/main/java/org/tron/core/services/http/GetAkFromAskServlet.java`, `GetExpandedSpendingKeyServlet.java`, `GetNkFromNskServlet.java`, `GetIncomingViewingKeyServlet.java`, `GetZenPaymentAddressServlet.java`)

### Summary
Several shielded-transaction HTTP endpoints of the Wallet HTTP API accept secret key material as `doGet` query-string parameters, not just as POST body fields. Any client, proxy, CDN, or load balancer sitting in front of the FullNode HTTP API that logs request URLs (the CWE-598/CWE-532 pattern from the referenced advisory) will capture these secrets in plaintext.

### Finding Description
`GetAkFromAskServlet.doGet`, `GetExpandedSpendingKeyServlet.doGet`, `GetNkFromNskServlet.doGet`, `GetIncomingViewingKeyServlet.doGet`, and `GetZenPaymentAddressServlet.doGet` all read sensitive shielded key components — `ask` (spend authorizing key), `nsk` (nullifier deriving key), `ak`/`nk` (proof authorizing/nullifier keys), and `ivk` (incoming viewing key) — directly via `request.getParameter(...)` on a GET request. [6](#0-5) [7](#0-6) 

Because these are declared as `doGet` handlers on servlets registered under `/wallet/*` in `FullNodeHttpApiService`, any caller (or intervening infrastructure) can — and, per the test harness in `HttpMethed`, is documented to — submit these secrets as GET query parameters, exactly the CWE-598/CWE-532 exposure pattern described in the analog report: reverse-proxy `combined` access logs capture full request lines including the query string, browser/HTTP-client history retains the URL, and any downstream CDN or WAF logging pipeline persists it. Unlike a `POST`-body submission, a `GET` with parameters puts these shielded secrets on the same class of exposure surface as the redirected API-key token in the referenced advisory.

### Impact Explanation
`ask`/`nsk`/`ak`/`nk`/`ivk` are the private cryptographic components of a z-address (shielded account) in TRON's Sapling implementation. Disclosure of `ask`/`nsk` (via `expsk`) allows full spend authority reconstruction of a shielded key, and disclosure of `ivk` allows decrypting all incoming shielded transactions to that address. If any of these are submitted as GET parameters, they persist in plaintext in any access/proxy log, browser history, or intermediary cache along the API request path, resulting in permanent key disclosure with no way to revoke or rotate the leaked material (Sapling keys can't be rotated per-address).

### Likelihood Explanation
This requires no privilege beyond issuing a standard shielded-wallet HTTP API call and having any log-capable component (reverse proxy, load balancer, browser network history) sit on that request path — a very common production topology for any node operator that fronts the FullNode HTTP API. The test utility `HttpMethed` shows POST as the conventional pattern, but the presence and functioning of the parallel `doGet` handler taking the same secret parameter is what creates the exposure; no interaction beyond a routine wallet operation is needed.

### Recommendation
Remove the `doGet` variants (or reject them outright) for all shielded-key-derivation/view-key endpoints that take secret key material as input — `GetAkFromAskServlet`, `GetExpandedSpendingKeyServlet`, `GetNkFromNskServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, and any sibling servlet with the same shape — so that `ask`, `nsk`, `ak`, `nk`, `ivk`, and similar secret values can only be submitted via POST body. Document that operators must ensure these endpoints are never exposed through log-capturing intermediaries, and consider stripping query strings from access logs at the reverse-proxy layer as defense in depth.

### Proof of Concept
1. Stand up a FullNode with the default HTTP API enabled and place an nginx reverse proxy in front of it with the default `combined` access-log format (as in the reproducer of the referenced advisory).
2. Issue: `GET http://<node>/wallet/getexpandedspendingkey?value=<hex-encoded-spending-key>&visible=true`
3. `GetExpandedSpendingKeyServlet.doGet` reads `value` from `request.getParameter("value")` and returns the expanded spending key derivation. [2](#0-1) 
4. Inspect nginx's access log (or the FullNode HTTP client's browser/network history if fronted by a web UI) — the raw spending key seed appears in plaintext in the logged request line, exactly mirroring the raw-API-key-in-Referer/proxy-log exposure described in GHSA-9pg3-25fq-p6cc.

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

**File:** framework/src/main/java/org/tron/core/services/http/GetZenPaymentAddressServlet.java (L47-63)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String ivk = request.getParameter("ivk");
      String d = request.getParameter("d");

      GrpcAPI.PaymentAddressMessage s = wallet
          .getPaymentAddress(new IncomingViewingKey(ByteArray.fromHexString(ivk)),
              new DiversifierT(ByteArray.fromHexString(d)));

      response.getWriter()
          .println(JsonFormat.printToString(s, visible));

    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
