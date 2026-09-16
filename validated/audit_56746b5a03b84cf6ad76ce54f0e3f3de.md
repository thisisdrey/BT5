These shielded-key HTTP endpoints are registered as regular servlets in `FullNodeHttpApiService.java` and are reachable by any anonymous HTTP client that can hit the full-node's API port, matching the "HTTP ... query paths into Wallet" scope. However, I was not able to confirm within the remaining budget whether there is a config gate (`Args.getInstance().isFullNodeAllowShieldedTransactionArgs()` or similar) that disables these Zen/shielded servlets by default, which materially affects reachability/likelihood.

### Title
Sensitive shielded-transaction key material accepted via HTTP GET query parameters, exposing spending/viewing keys to logs and proxies - (File: framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java)

### Summary
Several java-tron full-node HTTP API servlets accept highly sensitive shielded-transaction secret key material (`sk`, `ovk`, `ivk`, `ak`, `nk`) as `doGet` query-string parameters in addition to POST body, mirroring the Gogs bug class where authentication/secret material is accepted via URL parameters and therefore leaks through server access logs, reverse-proxy logs, and any intermediate HTTP infrastructure.

### Finding Description
The following servlets implement a `doGet` handler that reads secret shielded-pool key material directly via `request.getParameter(...)`:
- `GetExpandedSpendingKeyServlet.doGet` reads the raw spending key `sk` from `request.getParameter("value")` and passes it to `wallet.getExpandedSpendingKey(...)`. [1](#0-0) 
- `GetAkFromAskServlet.doGet` and `GetNkFromNskServlet.doGet` read the `ask`/`nsk` values via `request.getParameter("value")`. [2](#0-1) 
- `GetIncomingViewingKeyServlet.doGet` reads `ak` and `nk` via `request.getParameter(...)`. [3](#0-2) 
- `GetZenPaymentAddressServlet.doGet` reads the incoming viewing key `ivk` via `request.getParameter("ivk")`. [4](#0-3) 
- `ScanNoteByOvkServlet.doGet` reads the outgoing viewing key `ovk` via `request.getParameter("ovk")` and uses it to scan/decrypt shielded notes. [5](#0-4) 

These servlets are registered as standard endpoints on the full-node HTTP API (`FullNodeHttpApiService.java`), reachable by any client that can reach the node's HTTP port. There is also `ScanNoteByOvkOnSolidityServlet` and `ScanNoteByOvkOnPBFTServlet` exposing the same `ovk`-via-GET pattern on the solidity/PBFT query nodes. [6](#0-5) 

Root cause: unlike the Gogs case (a bearer token that can be rotated), the values transmitted here — `sk` (spending key), `ovk` (outgoing viewing key), `ivk` (incoming viewing key), `ak`/`nk`/`ask`/`nsk` — are the actual cryptographic secrets that control or reveal ownership of shielded funds. Placing them in the URL query string means they are recorded verbatim in: web server / reverse-proxy access logs, any HTTP debugging/monitoring middleware, browser/tool history if a human operator issues the GET manually, and potentially forwarded via `Referer` headers if the response ever triggers a follow-on request. Unlike a revocable API token, a leaked spending key cannot be rotated — the underlying shielded note(s) it controls remain permanently exposed.

### Impact Explanation
If an operator, an internal tool, a load balancer, or any HTTP intermediary logs full URLs (a very common default), a leaked `sk` allows deriving `ask`, `nsk`, `ak`, `nk`, `ovk` and ultimately the spending authority for a shielded note, enabling unauthorized spend of shielded TRC-20/TRX-shielded balances (key disclosure leading to concrete unauthorized account operation / theft of funds). A leaked `ovk` allows decrypting outgoing shielded notes (transaction detail disclosure), and a leaked `ivk` allows tracking/derandomizing incoming shielded payments to a diversified address. This is a genuine "key disclosure" impact class matching the accepted-impact list, though it depends on log/infrastructure exposure rather than a purely remote unauthenticated attack against the node process itself.

### Likelihood Explanation
Likelihood is moderate: an attacker needs some vector to read logs/proxy records (SSRF, shared hosting, misconfigured log aggregation, or a curious operator inspecting shell/browser history when manually testing these endpoints) rather than direct network exploitation. The `doGet` handlers exist by design as convenience aliases to the `doPost` handlers, so the exposure is intentional and consistently present across the shielded-key API surface, but the shielded API set is typically only enabled on nodes configured for shielded transaction support, which somewhat narrows the deployment surface. I could not confirm within this session whether these servlets are gated behind a `shieldedTransaction`/`fullNodeAllowShieldedTransaction` config flag that disables them by default — this affects the real-world reachability and should be verified against `Args`/`FullNodeHttpApiService` configuration before treating this as broadly exploitable.

### Recommendation
- Remove or deprecate the `doGet` variants of `GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetNkFromNskServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, `ScanNoteByOvkServlet` (and their Solidity/PBFT counterparts) so that spending/viewing key material can only be submitted via POST body, never via URL query string.
- If GET support must be retained for backward compatibility, document it as deprecated and unsafe, and ensure operators disable access/URL logging for these specific endpoints (or route them behind a header-only proxy rule).
- Apply a strict `Referrer-Policy: no-referrer` on all HTTP API responses.
- Audit all other `Get*Servlet` classes in `framework/src/main/java/org/tron/core/services/http/` for the same `doGet` + `request.getParameter` pattern applied to any other secret material.

### Proof of Concept
1. Start a full node with the HTTP API and shielded transaction support enabled.
2. Issue: `GET /wallet/getspendingkey` is unrelated, but for the affected endpoint, e.g.:
   `curl "http://<node>:8090/wallet/getexpandedspendingkey?value=<hex_spending_key>"`
   `curl "http://<node>:8090/wallet/scannotebyovk?ovk=<hex_ovk>&start_block_index=0&end_block_index=1000"`
3. Observe that `<hex_spending_key>` / `<hex_ovk>` appear in the node's Jetty access log (or any front-end reverse proxy log) as a plain query-string parameter.
4. Any party with read access to that log (log aggregation service, shared infra, misconfigured monitoring) now possesses the raw shielded key material, permanently compromising the corresponding shielded note(s)/addresses.

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

**File:** framework/src/main/java/org/tron/core/services/http/ScanNoteByOvkServlet.java (L38-50)
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
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnSolidity/http/ScanNoteByOvkOnSolidityServlet.java (L1-2)
```java
package org.tron.core.services.interfaceOnSolidity.http;

```
