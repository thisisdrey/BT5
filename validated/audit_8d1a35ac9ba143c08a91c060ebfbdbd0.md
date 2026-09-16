### Title
Shielded Zcash-derived private key material (spending key, ak/nk, ivk/ovk) accepted as plaintext GET query parameters, causing information disclosure via URLs, logs and browser history - (File: framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java)

### Summary
The CVE-2020-27612 bug class is "sensitive per-user identifiers placed in URLs, leaking to logs/browsers/outsiders." In java-tron's HTTP Wallet API, several endpoints that operate on shielded-transaction secret key material accept those secrets as `GET` query-string parameters instead of requiring `POST` with a body, so the secrets end up embedded in the request URL.

### Finding Description
Multiple full-node HTTP servlets implement `doGet` handlers that read highly sensitive shielded (Zcash Sapling-style) key material directly from `request.getParameter(...)`:

- `GetExpandedSpendingKeyServlet.doGet` reads the raw spending key `sk` from the `value` query parameter and returns the expanded spending key: [1](#0-0) 
- `GetIncomingViewingKeyServlet.doGet` reads `ak`/`nk` (authorizing/nullifier keys) from query parameters to derive an incoming viewing key: [2](#0-1) 
- `ScanNoteByIvkServlet.doGet` and `ScanNoteByOvkServlet.doGet` read the raw incoming/outgoing viewing key (`ivk`/`ovk`) as a query parameter and use it to scan/decrypt shielded transaction notes: [3](#0-2) [4](#0-3) 
- `ScanAndMarkNoteByIvkServlet.doGet` and the TRC20 variants `ScanShieldedTRC20NotesByIvkServlet.doGet` / `ScanShieldedTRC20NotesByOvkServlet.doGet` follow the identical GET-with-secret-in-URL pattern: [5](#0-4) [6](#0-5) 

Placing these secrets in the URL query string (rather than only in a POST body, as `GetSpendingKeyServlet`/`CreateSpendAuthSigServlet` correctly restrict some other key-material operations to POST, e.g. [7](#0-6) ) means the full spending/viewing key is:
- written into web-server / reverse-proxy / load-balancer access logs,
- cached in browser history if the endpoint is ever hit via a browser or embedded link,
- forwarded via the `Referer` header to any subsequent third-party resource if a user navigates from a page containing that URL,
- exposed to any monitoring/APM tooling that indexes request URLs.

This is the same information-disclosure pattern as CVE-2020-27612 (sensitive identifier placed in a URL, unintentionally leaked to logs/other parties), except in java-tron the leaked secret is not a username but shielded transaction key material: an `ivk`/`ak`/`nk` allows decrypting/enumerating all of a shielded account's private transaction notes, and a spending key (`sk`) allows spending shielded funds outright.

There is no request-scrubbing, log-redaction, or protocol-level rejection of these parameters on GET; the only generic mitigation in the codebase is `DesensitizedConverter`, which only redacts IPv4 addresses from log lines, not key material: [8](#0-7) 

### Impact Explanation
If an operator's HTTP access logs, reverse-proxy logs, browser history, or any URL-logging middleware capture these GET requests, an attacker with access to those logs gains the plaintext spending key or viewing keys of a shielded TRC20/TRC10 account. A leaked spending key permits direct theft of shielded funds; a leaked `ivk`/`ovk`/`ak`+`nk` permits full disclosure of a user's private shielded transaction history (violating the confidentiality guarantee that is the entire purpose of the shielded pool). This maps to the "key disclosure" and "theft of funds" acceptance criteria.

### Likelihood Explanation
Exploitation requires a mediating log/history mechanism (proxy, CDN, browser, monitoring tool) to have captured the URL — this is not a direct remote exploit against the node itself, but such logging is extremely common in production HTTP deployments (nginx/Apache/ELB access logs, APM tools) sitting in front of a full node's `/wallet/*` HTTP API. Any wallet/UI or user that calls these endpoints via GET (which the servlet explicitly supports and documents as an alternate mode) is at risk. This is a realistic, medium-likelihood exposure for any operator who doesn't specifically disable GET on these shielded endpoints.

### Recommendation
- Remove or hard-disable the `doGet` handlers for all endpoints that accept spending keys, `ak`, `nk`, `ivk`, or `ovk` (`GetExpandedSpendingKeyServlet`, `GetIncomingViewingKeyServlet`, `ScanNoteByIvkServlet`, `ScanNoteByOvkServlet`, `ScanAndMarkNoteByIvkServlet`, `ScanShieldedTRC20NotesByIvkServlet`, `ScanShieldedTRC20NotesByOvkServlet`), forcing callers to use POST with a JSON body, consistent with `GetSpendingKeyServlet`'s pattern of not requiring the secret on GET.
- Document that these endpoints must never be exposed through logging proxies/CDNs, and add explicit filtering in `HttpInterceptor`/`LiteFnQueryHttpFilter` to block or mask query-string logging for these paths.
- Extend `DesensitizedConverter` (or add a new log filter) to redact known sensitive parameter names (`value`, `ivk`, `ovk`, `ak`, `nk`) from any logged URLs.

### Proof of Concept
```
GET /wallet/getexpandedspendingkey?value=<spending_key_hex> HTTP/1.1
Host: fullnode:8090
```
This request, made by any unprivileged client able to reach the HTTP Wallet API, places the raw spending key directly in the URL. Any intermediary access log (e.g. `nginx` access log, load balancer log, browser history if triggered via a link) permanently records the secret in plaintext, exactly mirroring the "username in room URL" information-leak pattern from CVE-2020-27612, but with shielded-fund-compromising key material instead of a username.

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

**File:** framework/src/main/java/org/tron/core/services/http/ScanNoteByIvkServlet.java (L54-63)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      long startNum = Long.parseLong(request.getParameter("start_block_index"));
      long endNum = Long.parseLong(request.getParameter("end_block_index"));
      String ivk = request.getParameter("ivk");
      boolean visible = Util.getVisible(request);

      GrpcAPI.DecryptNotes notes = wallet
          .scanNoteByIvk(startNum, endNum, ByteArray.fromHexString(ivk));
      response.getWriter().println(convertOutput(notes, visible));
```

**File:** framework/src/main/java/org/tron/core/services/http/ScanNoteByOvkServlet.java (L38-49)
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
```

**File:** framework/src/main/java/org/tron/core/services/http/ScanAndMarkNoteByIvkServlet.java (L62-79)
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

      response.getWriter().println(convertOutput(notes, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/ScanShieldedTRC20NotesByIvkServlet.java (L63-93)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      Util.rejectIfEventsPresent(request.getParameterValues("events"));
    } catch (IllegalArgumentException e) {
      response.setStatus(HttpServletResponse.SC_BAD_REQUEST);
      Util.processError(e, response);
      return;
    }
    try {
      boolean visible = Util.getVisible(request);
      long startNum = Long.parseLong(request.getParameter("start_block_index"));
      long endNum = Long.parseLong(request.getParameter("end_block_index"));
      String ivk = request.getParameter("ivk");

      String contractAddress = request.getParameter("shielded_TRC20_contract_address");
      if (visible) {
        contractAddress = Util.getHexAddress(contractAddress);
      }

      String ak = request.getParameter("ak");
      String nk = request.getParameter("nk");

      GrpcAPI.DecryptNotesTRC20 notes = wallet
          .scanShieldedTRC20NotesByIvk(startNum, endNum,
              ByteArray.fromHexString(contractAddress), ByteArray.fromHexString(ivk),
              ByteArray.fromHexString(ak), ByteArray.fromHexString(nk));
      response.getWriter().println(convertOutput(notes, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateSpendAuthSigServlet.java (L20-22)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {

  }
```

**File:** common/src/main/java/org/tron/common/log/layout/DesensitizedConverter.java (L17-19)
```java
  private static final Pattern pattern = Pattern.compile(
      "(((25[0-5]|2[0-4]\\d|((1\\d{2})|([1-9]?\\d)))\\.){3}(25[0-5]|2[0-4]\\d|((1\\d{2})|"
          + "([1-9]?\\d))))");
```
