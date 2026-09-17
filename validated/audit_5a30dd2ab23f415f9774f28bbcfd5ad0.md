### Title
Shielded transaction key material (spending key, ak/nk, ivk, ovk) accepted via HTTP GET query parameters, exposing secrets in browser history, Referer headers, and reverse-proxy/access logs - ([File: framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java])

### Summary
Multiple `GET`-method HTTP API servlets in java-tron's node HTTP endpoint accept private, secret shielded-pool key material as URL query parameters instead of requiring the request body. This mirrors the Tugtainer bug class (CVE-2026-23846): secrets transmitted in the URL query string end up in access logs, browser history, and `Referer` headers of any downstream request.

### Finding Description
`GetExpandedSpendingKeyServlet.doGet` reads the raw spend-authorizing key straight from the query string: [1](#0-0) 

The same GET-based pattern is used by several other zen/shielded servlets that accept sensitive viewing/spending key components as `?ak=`, `?nk=`, `?ivk=`, `?ovk=`, or `?value=` query parameters:
- `GetAkFromAskServlet.doGet` reads the ask value from `request.getParameter("value")` [2](#0-1) 
- `GetIncomingViewingKeyServlet.doGet` reads `ak`/`nk` from query parameters [3](#0-2) 
- `GetZenPaymentAddressServlet.doGet` reads the incoming viewing key `ivk` from a query parameter [4](#0-3) 
- `ScanAndMarkNoteByIvkServlet.doGet` and `ScanNoteByOvkServlet.doGet` read the `ivk`/`ak`/`nk` and `ovk` respectively from query parameters, which are then used to decrypt an account's shielded note history [5](#0-4) [6](#0-5) 

These servlets extend `RateLimiterServlet`, which delegates to Jetty via `HttpService`/`FullNodeHttpApiService` and `SolidityNodeHttpApiService`, both of which register these servlets on the node's public-facing HTTP API port [7](#0-6) . Unlike Tugtainer's original login endpoint, java-tron itself does not maintain an internal access log/`RequestLog`; no `NCSARequestLog`/`RequestLog` component exists in the codebase. However, that does not eliminate the exposure vector: any reverse proxy (nginx/Apache/CDN) commonly placed in front of these nodes logs full request URLs including the query string by default, and any browser-based tool, script, or shell history that constructs these GET requests will retain the secret key material in cleartext outside of TLS-terminated transport, in violation of standard "secrets belong in body, not URL" practice.

### Impact Explanation
The exposed values are not ordinary API arguments — they are the cryptographic secrets that gate the confidentiality of the shielded (zk-SNARK) transaction pool: the spend-authorizing key input to `getExpandedSpendingKey`, `ak`/`nk` used to derive the incoming viewing key, and the `ivk`/`ovk` used to decrypt an account's entire shielded note history via `wallet.scanAndMarkNoteByIvk`/`wallet.scanNoteByOvk`. Leakage of `ivk`/`ovk`/spending key material through logs, browser history, or a caching/logging proxy is a direct key-disclosure vulnerability: whoever obtains the logged value can decrypt a victim's shielded transaction history (loss of the shielded pool's confidentiality guarantee) or, for spend-key-derived material, potentially derive spending authority over shielded funds.

### Likelihood Explanation
Likelihood is high for anyone using or operating a client that issues GET requests to these endpoints (e.g., wallet integrations, scripts, or any deployment behind a standard reverse proxy/load balancer/CDN that logs URLs) since this requires no special privilege — it is simply how these public documented HTTP API endpoints are designed to be called.

### Recommendation
Remove or deprecate the `doGet` handlers on all shielded-key-accepting servlets (`GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, `ScanAndMarkNoteByIvkServlet`, `ScanNoteByOvkServlet`, and any sibling shielded-TRC20 scan servlets that take `ivk`/`ovk`/`ak`/`nk`/spending-key values as GET parameters), forcing callers to use the existing `doPost` path (`PostParams.getPostParams`) which already supports the same operations via the request body.

### Proof of Concept
```
GET /wallet/getexpandedspendingkey?value=<ASK_HEX_SECRET>&visible=true HTTP/1.1
Host: node-ip:8090
```
or, for the shielded scan API:
```
GET /wallet/scannotebyovk?start_block_index=0&end_block_index=1000000&ovk=<OVK_HEX_SECRET>
```
Both requests leak the secret value in the URL, which will be recorded verbatim by any intermediary (reverse proxy, CDN, browser history, shell history) that logs request URLs, as demonstrated conceptually in [8](#0-7)  and [6](#0-5) .

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

**File:** framework/src/main/java/org/tron/core/services/http/GetZenPaymentAddressServlet.java (L47-55)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      String ivk = request.getParameter("ivk");
      String d = request.getParameter("d");

      GrpcAPI.PaymentAddressMessage s = wallet
          .getPaymentAddress(new IncomingViewingKey(ByteArray.fromHexString(ivk)),
              new DiversifierT(ByteArray.fromHexString(d)));
```

**File:** framework/src/main/java/org/tron/core/services/http/ScanAndMarkNoteByIvkServlet.java (L62-73)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      long startNum = Long.parseLong(request.getParameter("start_block_index"));
      long endNum = Long.parseLong(request.getParameter("end_block_index"));
      String ivk = request.getParameter("ivk");
      String ak = request.getParameter("ak");
      String nk = request.getParameter("nk");
      boolean visible = Util.getVisible(request);

      GrpcAPI.DecryptNotesMarked notes = wallet
          .scanAndMarkNoteByIvk(startNum, endNum, ByteArray.fromHexString(ivk),
              ByteArray.fromHexString(ak), ByteArray.fromHexString(nk));
```

**File:** framework/src/main/java/org/tron/core/services/http/ScanNoteByOvkServlet.java (L38-46)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      boolean visible = Util.getVisible(request);
      long startBlockIndex = Long.parseLong(request.getParameter("start_block_index"));
      long endBlockIndex = Long.parseLong(request.getParameter("end_block_index"));
      String ovk = request.getParameter("ovk");
      GrpcAPI.DecryptNotes notes = wallet
          .scanNoteByOvk(startBlockIndex, endBlockIndex, ByteArray.fromHexString(ovk));
      response.getWriter().println(ScanNoteByIvkServlet.convertOutput(notes, visible));
```

**File:** framework/src/main/java/org/tron/core/services/http/solidity/SolidityNodeHttpApiService.java (L55-59)
```java
import org.tron.core.services.http.ScanAndMarkNoteByIvkServlet;
import org.tron.core.services.http.ScanNoteByIvkServlet;
import org.tron.core.services.http.ScanNoteByOvkServlet;
import org.tron.core.services.http.ScanShieldedTRC20NotesByIvkServlet;
import org.tron.core.services.http.ScanShieldedTRC20NotesByOvkServlet;
```
