### Title
Cleartext Transmission of Sensitive Shielded Transaction Keys over Unencrypted HTTP API - (File: framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java)

### Summary
The FullNode HTTP API in java-tron exposes multiple shielded-transaction endpoints (`GetSpendingKeyServlet`, `GetExpandedSpendingKeyServlet`, `GetAkFromAskServlet`, `GetNkFromNskServlet`, `GetIncomingViewingKeyServlet`, `GetZenPaymentAddressServlet`, `CreateShieldedTransactionServlet`, etc.) that accept or return spending keys (`sk`), expanded spending keys (`ask`, `nsk`, `ovk`), incoming viewing keys (`ivk`), and related shielded secrets as plain HTTP request/response parameters, with no TLS/HTTPS termination configured in `FullNodeHttpApiService`. This mirrors the CWE-319 class in the reported advisory (secrets transmitted over an unencrypted channel), but here the sensitive material is the zk-SNARK shielded key material itself rather than build tzdata.

### Finding Description
`FullNodeHttpApiService` registers all wallet servlets on a plain Jetty HTTP connector, with no `SSLContext`/HTTPS setup anywhere in the `http` package [1](#0-0) . Servlets like `GetSpendingKeyServlet.doGet`/`doPost` return a raw spending key in the HTTP response body [2](#0-1) , `GetExpandedSpendingKeyServlet` accepts a spending key as a request parameter and returns `ask`/`nsk`/`ovk` [3](#0-2) , and `GetIncomingViewingKeyServlet` accepts `ak`/`nk` and returns the derived incoming viewing key, all as GET query parameters or JSON POST bodies over `http://` [4](#0-3) . The `createshieldedtransaction` endpoint likewise transmits `ask`, `nsk`, `ovk`, and note data (value, rcm, payment address) in a plaintext JSON POST body, as shown by the test harness building this exact request [5](#0-4) . The project's own `reference.conf` explicitly documents this exposure: "Some shielded transaction APIs require sending private keys as parameters. Calling these APIs on untrusted or remote nodes may leak your private keys" [6](#0-5) . The default FullNode HTTP port (8090) is enabled by default (`fullNodeEnable = true`) with no encryption [7](#0-6) .

### Impact Explanation
If a node operator exposes the HTTP API on a network path that is not fully trusted (LAN, cloud network, reverse proxy without TLS, etc.), an on-path attacker can passively capture spending keys, expanded spending keys (`ask`/`nsk`/`ovk`), and incoming viewing keys transmitted in cleartext. Possession of a spending key or its derived `ask`/`nsk` allows full control over shielded funds tied to that key (spend authority), i.e., outright theft of shielded balances — a concrete unauthorized account operation / theft-of-funds impact. This is consistent with the reported CWE-319 class (cleartext transmission of sensitive information enabling downstream compromise), but the impact here is materially worse than the original advisory's build-tooling risk, since it is direct key material for shielded transactions rather than tzdata.

### Likelihood Explanation
Likelihood is bounded by deployment configuration: this is only exploitable if `allowShieldedTransactionApi` is enabled and the HTTP endpoint is reachable over a network where traffic can be intercepted (the project's own documentation already warns operators to invoke these APIs "locally" only) [6](#0-5) . Because this is a known, documented, and consciously accepted risk (rather than a newly discovered unintentional flaw), and mitigation is advisory/configuration-based rather than a code defect, this is a design-level cleartext transmission weakness rather than a novel exploitable vulnerability.

### Recommendation
Given the existing documented warning in `reference.conf`, no additional finding is warranted beyond what the maintainers already flag; however, hardening options would include: enforcing TLS termination (e.g., requiring HTTPS/reverse-proxy-with-TLS) before allowing `allowShieldedTransactionApi=true`, binding these specific shielded-key servlets to loopback-only by default, or refusing to serve spend-key-bearing endpoints unless the client connects via `localhost`.

### Proof of Concept
Not applicable as a novel exploit — this is a documented, opt-in behavior (`node.allowShieldedTransactionApi`), and the project already warns against exposing it on untrusted/remote nodes [6](#0-5) . No further proof-of-concept is provided since this does not represent an unintended vulnerability distinct from the documented risk.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L20-30)
```java
@Component("fullNodeHttpApiService")
@Slf4j(topic = "API")
public class FullNodeHttpApiService extends HttpService {

  @Autowired
  private GetAccountServlet getAccountServlet;
  @Autowired
  private TransferServlet transferServlet;
  @Autowired
  private BroadcastServlet broadcastServlet;
  @Autowired
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

**File:** framework/src/test/java/org/tron/common/utils/client/utils/HttpMethed.java (L3326-3336)
```java
      if (shieldAddressInfo != null) {
        HttpResponse expandedSpendingKey =
            HttpMethed.getExpandedSpendingKey(
                httpNode, ByteArray.toHexString(shieldAddressInfo.getSk()));
        responseContent = HttpMethed.parseResponseContent(expandedSpendingKey);
        HttpMethed.printJsonContent(responseContent);
        String ovk = responseContent.getString("ovk");
        map.put("ask", responseContent.getString("ask"));
        map.put("nsk", responseContent.getString("nsk"));
        map.put("ovk", ovk);

```

**File:** common/src/main/resources/reference.conf (L210-216)
```text
  # WARNING: Some shielded transaction APIs require sending private keys as parameters.
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
