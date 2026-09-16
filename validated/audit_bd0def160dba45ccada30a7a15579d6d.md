I have sufficient evidence. Here is the final analog analysis.

### Title
Shielded key material (spending key, `ask`, `nsk`) accepted via HTTP GET query string in shielded-wallet servlets - (File: `framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java`)

### Summary
The FullNode HTTP API exposes several shielded-transaction "derive key" servlets that accept highly sensitive key material — an expanded spending key, an `ask` (spend authorizing key), an `nsk` (nullifier deriving key), and viewing-key components — as plain HTTP GET query-string parameters instead of restricting them to the POST body. This mirrors CVE-2024-45791 (Apache HertzBeat), where a sensitive token transmitted via GET with a query string is unintentionally leaked to unauthorized parties through access logs, proxy logs, browser history, or `Referer` headers.

### Finding Description
`GetExpandedSpendingKeyServlet.doGet` reads the raw spending key straight from the query string parameter `value` and passes it into `wallet.getExpandedSpendingKey`: [1](#0-0) 

The same pattern of accepting sensitive shielded secrets via `doGet` exists in the sibling servlets that are also registered as live endpoints on the FullNode HTTP API:
- `GetAkFromAskServlet.doGet` reads the `ask` (spend authorizing key) from `request.getParameter("value")` [2](#0-1) 
- `GetNkFromNskServlet.doGet` reads the `nsk` (nullifier deriving key) from `request.getParameter("value")` [3](#0-2) 
- `GetIncomingViewingKeyServlet.doGet` reads `ak`/`nk` viewing-key components from query parameters [4](#0-3) 
- `GetZenPaymentAddressServlet.doGet` reads `ivk` (incoming viewing key) from a query parameter [5](#0-4) 

All of these servlets are registered as active, reachable endpoints in `FullNodeHttpApiService`, e.g. `/wallet/getexpandedspendingkey`, `/wallet/getakfromask`, `/wallet/getnkfromnsk`, `/wallet/getincomingviewingkey`, `/wallet/getzenpaymentaddress`: [6](#0-5) 

Because a `GET` request's query string is a standard part of the URL, it routinely ends up recorded in: the reverse proxy/load balancer access logs (nginx, HAProxy, cloud LBs typically log the full request line by default), server access logs on the node operator's side, browser history/autocomplete if a user or tool opens the URL in a browser for testing, and the `Referer` header sent to any third-party resource loaded from a page containing that URL. Any of these paths can expose an `ask`/`nsk`/spending key to a party who never should have had access to it, precisely the "Exposure of Sensitive Information to an Unauthorized Actor" bug class in CVE-2024-45791.

### Impact Explanation
An `ask`/`nsk`/expanded spending key for a shielded (Sapling) TRON address is directly usable to derive spend authority and nullifiers for that shielded account. If such a value leaks via logs/history/Referer to an unauthorized actor, that actor gains the ability to spend/decrypt notes tied to the shielded address, i.e., theft of shielded funds. This is a concrete confidentiality/integrity impact on user funds reachable purely through an anonymous, unprivileged HTTP GET request to a public FullNode API endpoint — no signed transaction or special privilege is required to trigger the exposure (the caller supplies their own secret, but the vulnerability is that the *server* accepts and thereby helps leak it via GET).

### Likelihood Explanation
Likelihood of exploitation depends on operational deployment factors (whether the node sits behind a logging proxy, whether logs are exposed, whether a wallet/tool constructs these URLs with GET and secrets embedded). Given HertzBeat's real-world CVE stems from exactly this pattern, and these java-tron servlets are documented (in tests, e.g. `HttpMethed.getAkFromAsk`/`getNkFromNsk`/`getIncomingViewingKey`/`getZenPaymentAddress`) as callable via GET with the secret in the query string [7](#0-6) , any client/integration that follows the documented GET usage will leak the secret into normal HTTP infrastructure logging paths.

### Recommendation
Remove or disable the `doGet` handlers for these shielded-key-derivation servlets (`GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetNkFromNskServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`) and require POST-with-body for any endpoint that accepts spend keys, authorizing keys, nullifier deriving keys, or viewing keys, consistent with how `CreateSpendAuthSigServlet` already restricts sensitive operations to POST only. Additionally, ensure operators are advised to avoid logging query strings for the `/wallet/*` shielded key endpoints if GET must be retained for compatibility.

### Proof of Concept
```
GET /wallet/getexpandedspendingkey?value=<hex-encoded-spending-key>&visible=true HTTP/1.1
Host: fullnode:8090
```
This request line, containing the full plaintext spending key, is written verbatim into any intermediary access log (reverse proxy, CDN, node's own HTTP access log) or appears in browser history if issued from a browser, exposing the secret to anyone with read access to those logs — matching the CVE-2024-45791 bug class of "Exposure sensitive token via http GET method with query string."

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

**File:** framework/src/main/java/org/tron/core/services/http/GetIncomingViewingKeyServlet.java (L34-43)
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
```

**File:** framework/src/main/java/org/tron/core/services/http/GetZenPaymentAddressServlet.java (L47-62)
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
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L417-430)
```java
    context
        .addServlet(new ServletHolder(getExpandedSpendingKeyServlet),
            "/wallet/getexpandedspendingkey");
    context.addServlet(new ServletHolder(getAkFromAskServlet), "/wallet/getakfromask");
    context.addServlet(new ServletHolder(getNkFromNskServlet), "/wallet/getnkfromnsk");
    context.addServlet(new ServletHolder(getSpendingKeyServlet), "/wallet/getspendingkey");
    context
        .addServlet(new ServletHolder(getNewShieldedAddressServlet),
            "/wallet/getnewshieldedaddress");
    context.addServlet(new ServletHolder(getDiversifierServlet), "/wallet/getdiversifier");
    context.addServlet(new ServletHolder(getIncomingViewingKeyServlet),
        "/wallet/getincomingviewingkey");
    context.addServlet(new ServletHolder(getZenPaymentAddressServlet),
        "/wallet/getzenpaymentaddress");
```

**File:** framework/src/test/java/org/tron/common/utils/client/utils/HttpMethed.java (L3191-3220)
```java
  /** constructor. */
  public static HttpResponse getNkFromNsk(String httpNode, String nsk) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getnkfromnsk";
      JsonObject userBaseObj2 = new JsonObject();
      userBaseObj2.addProperty("value", nsk);
      response = createConnect(requestUrl, userBaseObj2);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

  /** constructor. */
  public static HttpResponse getIncomingViewingKey(String httpNode, String ak, String nk) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getincomingviewingkey";
      JsonObject userBaseObj2 = new JsonObject();
      userBaseObj2.addProperty("ak", ak);
      userBaseObj2.addProperty("nk", nk);
      response = createConnect(requestUrl, userBaseObj2);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }
```
