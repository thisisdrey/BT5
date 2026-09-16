### Title
Shielded transaction key material (`ask`/`nsk`/`ovk`/spending keys) sent as request parameters over unencrypted HTTP/gRPC, enabling MITM key disclosure - (File: framework/src/main/java/org/tron/core/services/http/CreateSpendAuthSigServlet.java)

### Summary
Analogous to CVE-2025-64648 (IBM Concert transmitting sensitive data in clear text, allowing MITM disclosure), java-tron's FullNode HTTP API and gRPC `WalletApi` accept and echo highly sensitive Sapling shielded-transaction key material — `ask` (spend authorizing key), `nsk`, `ovk`, `ivk`, `ak`, and full spending keys — as plain JSON/protobuf parameters over the network, with no TLS/SSL support anywhere in the server stack.

### Finding Description
Several servlets take raw private key material as request parameters and process/return it without any transport encryption:
- `CreateSpendAuthSigServlet.doPost` merges `SpendAuthSigParameters` (containing `ask`, the spend authorizing key) directly from the client-supplied POST body and calls `wallet.createSpendAuthSig(...)` [1](#0-0) .
- `GetExpandedSpendingKeyServlet` and `GetSpendingKeyServlet` accept/return `ask`, `nsk`, `ovk` spending-key material via GET/POST parameters [2](#0-1) [3](#0-2) .
- `GetIncomingViewingKeyServlet` accepts `ak`/`nk` viewing key components as plain GET/POST parameters [4](#0-3) .
- The `PrivateParameters`/`PrivateParametersWithoutAsk`/`SpendAuthSigParameters` protobuf messages used across HTTP and gRPC (`WalletGrpc`) carry `ask`, `nsk`, `ovk` fields in cleartext protocol messages [5](#0-4) .

Nowhere in the server bootstrap is TLS/SSL configured: the gRPC server is built exclusively with `configurePlaintext` in `GrpcNettyMaxConcurrentStreamsLimiter` and `RpcService.initServerBuilder`, with no `useTransportSecurity`/`SslContext` option [6](#0-5) [7](#0-6) . The `FullNodeHttpApiService` is a plain Jetty servlet context with no HTTPS connector configuration [8](#0-7) . The repository's own configuration documentation explicitly acknowledges the risk: `node.allowShieldedTransactionApi` docs warn "Calling these APIs on untrusted or remote nodes may leak your private keys" — confirming the developers are aware these parameters are sent unencrypted [9](#0-8) . Test/client utilities in the repo also construct requests using plain `http://` URLs when exercising these key-bearing endpoints (`getspendingkey`, `getexpandedspendingkey`, `getincomingviewingkey`, `createspendauthsig`) and gRPC channels built with `.usePlaintext()` [10](#0-9) [11](#0-10) .

### Impact Explanation
Any network-positioned attacker (MITM) between an unprivileged API client and a FullNode/SolidityNode serving these HTTP/gRPC endpoints can passively capture `ask`, `nsk`, `ovk`, `ivk`, or full spending keys transmitted as request parameters or responses. Possession of `ask`/spending key material for a Sapling shielded address allows an attacker to forge spend authorization signatures and steal shielded funds outright — a concrete unauthorized-account-operation / theft-of-funds impact, matching the "High" bar in the validation rules (key disclosure leading to fund theft).

### Likelihood Explanation
Reachability requires only an anonymous API client sending a normal HTTP POST/GET or gRPC call to a node's exposed `fullNodePort`/`rpc.port` while an attacker can observe the network path (classic MITM, consistent with the CVSS AC:H/PR:N/UI:N vector of the source CVE). The project's own configuration comments already flag this exact risk for shielded-transaction APIs, indicating the code path and its plaintext-transport exposure are intentional/known behavior rather than a hypothetical.

### Recommendation
Provide first-class TLS support for both the Jetty HTTP servlet listeners (`FullNodeHttpApiService`/`SolidityNodeHttpApiService`) and the gRPC `NettyServerBuilder` in `RpcService`/`GrpcNettyMaxConcurrentStreamsLimiter` (e.g., `sslContext`/`useTransportSecurity`), and default `allowShieldedTransactionApi`-gated servlets (`CreateSpendAuthSigServlet`, `GetSpendingKeyServlet`, `GetExpandedSpendingKeyServlet`, `GetIncomingViewingKeyServlet`, etc.) to bind to loopback only or require TLS, refusing to serve key-bearing parameters over plaintext transport.

### Proof of Concept
1. Start a FullNode with `allowShieldedTransactionApi = true` and default plaintext HTTP (`fullNodePort = 8090`).
2. From a machine that can observe network traffic between client and node (e.g., ARP-spoofing MITM), have a legitimate client call `POST http://<node>:8090/wallet/createspendauthsig` with `{"ask": "<hex>", "alpha": "<hex>", "tx_hash": "<hex>"}` as shown in test helper `HttpMethed.getExpandedSpendingKey`/`CreateSpendAuthSigServlet` usage [12](#0-11) .
3. The attacker sniffs the unencrypted TCP stream and extracts the `ask` value directly from the JSON body, since the connection is plain HTTP (or plaintext gRPC for the equivalent `WalletGrpc` call).
4. With `ask` recovered, the attacker can independently compute spend authorization signatures for the victim's shielded notes and construct fraudulent shielded transfers, resulting in theft of shielded funds.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/CreateSpendAuthSigServlet.java (L24-34)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      SpendAuthSigParameters.Builder build = SpendAuthSigParameters.newBuilder();
      JsonFormat.merge(params.getParams(), build);
      BytesMessage result = wallet.createSpendAuthSig(build.build());
      response.getWriter().println(JsonFormat.printToString(result, params.isVisible()));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetExpandedSpendingKeyServlet.java (L22-41)
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

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      BytesMessage.Builder build = BytesMessage.newBuilder();
      JsonFormat.merge(params.getParams(), build);
      fillResponse(params.isVisible(), build.getValue(), response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetSpendingKeyServlet.java (L19-41)
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

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      BytesMessage reply = wallet.getSpendingKey();
      if (reply != null) {
        response.getWriter().println(JsonFormat.printToString(reply, params.isVisible()));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetIncomingViewingKeyServlet.java (L21-44)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      JSONObject jsonObject = JSONObject.parseObject(params.getParams());
      String ak = jsonObject.getString("ak");
      String nk = jsonObject.getString("nk");

      fillResponse(params.isVisible(), ak, nk, response);
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }

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

**File:** protocol/src/main/protos/api/api.proto (L961-991)
```text
message PrivateParameters {
  bytes transparent_from_address = 1;
  bytes ask = 2;
  bytes nsk = 3;
  bytes ovk = 4;
  int64 from_amount = 5;
  repeated SpendNote shielded_spends = 6;
  repeated ReceiveNote shielded_receives = 7;
  bytes transparent_to_address = 8;
  int64 to_amount = 9;
  int64 timeout = 10; // timeout in seconds, it works only when it bigger than 0
}

message PrivateParametersWithoutAsk {
  bytes transparent_from_address = 1;
  bytes ak = 2;
  bytes nsk = 3;
  bytes ovk = 4;
  int64 from_amount = 5;
  repeated SpendNote shielded_spends = 6;
  repeated ReceiveNote shielded_receives = 7;
  bytes transparent_to_address = 8;
  int64 to_amount = 9;
  int64 timeout = 10; // timeout in seconds, it works only when it bigger than 0
}

message SpendAuthSigParameters {
  bytes ask = 1;
  bytes tx_hash = 2;
  bytes alpha = 3;
}
```

**File:** framework/src/main/java/org/tron/common/application/GrpcNettyMaxConcurrentStreamsLimiter.java (L34-41)
```java
  static NettyServerBuilder configurePlaintext(
      NettyServerBuilder builder, int maxConcurrentStreams) {
    checkNotNull(builder, "builder");
    checkArgument(maxConcurrentStreams > 0, "maxConcurrentStreams must be positive");
    builder.maxConcurrentCallsPerConnection(maxConcurrentStreams);
    // TODO: Remove this shim after https://github.com/grpc/grpc-java/issues/12930 is fixed.
    return builder.protocolNegotiator(newPlaintextNegotiator(maxConcurrentStreams));
  }
```

**File:** framework/src/main/java/org/tron/common/application/RpcService.java (L94-120)
```java
  protected NettyServerBuilder initServerBuilder() {
    NettyServerBuilder serverBuilder = NettyServerBuilder.forPort(this.port);
    CommonParameter parameter = Args.getInstance();
    if (parameter.getRpcThreadNum() > 0) {
      this.executorService = ExecutorServiceManager.newFixedThreadPool(
          this.executorName, parameter.getRpcThreadNum());
      serverBuilder = serverBuilder.executor(this.executorService);
    }
    // Set configs from config.conf or default value
    serverBuilder = GrpcNettyMaxConcurrentStreamsLimiter.configurePlaintext(
        serverBuilder, parameter.getMaxConcurrentCallsPerConnection());
    serverBuilder
        .flowControlWindow(parameter.getFlowControlWindow())
        .maxConnectionIdle(parameter.getMaxConnectionIdleInMillis(), TimeUnit.MILLISECONDS)
        .maxConnectionAge(parameter.getMaxConnectionAgeInMillis(), TimeUnit.MILLISECONDS)
        .maxInboundMessageSize(parameter.getMaxMessageSize())
        .maxHeaderListSize(parameter.getMaxHeaderListSize());
    if (parameter.getRpcMaxRstStream() > 0 && parameter.getRpcSecondsPerWindow() > 0) {
      serverBuilder.maxRstFramesPerWindow(
          parameter.getRpcMaxRstStream(), parameter.getRpcSecondsPerWindow());
    }

    if (parameter.isRpcReflectionServiceEnable()) {
      serverBuilder.addService(ProtoReflectionService.newInstance());
    }
    return serverBuilder;
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L20-34)
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
  private UpdateAccountServlet updateAccountServlet;
  @Autowired
  private VoteWitnessAccountServlet voteWitnessAccountServlet;
  @Autowired
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

**File:** framework/src/test/java/org/tron/common/utils/client/utils/HttpMethed.java (L3120-3220)
```java


  /** constructor. */
  public static HttpResponse getSpendingKey(String httpNode) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getspendingkey";
      response = createConnect(requestUrl);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

  /** constructor. */
  public static HttpResponse getDiversifier(String httpNode) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getdiversifier";
      response = createConnect(requestUrl);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

  /** constructor. */
  public static HttpResponse getRcm(String httpNode) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getrcm";
      response = createConnect(requestUrl);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

  /** constructor. */
  public static HttpResponse getExpandedSpendingKey(String httpNode, String spendingKey) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getexpandedspendingkey";
      JsonObject userBaseObj2 = new JsonObject();
      userBaseObj2.addProperty("value", spendingKey);
      response = createConnect(requestUrl, userBaseObj2);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

  /** constructor. */
  public static HttpResponse getAkFromAsk(String httpNode, String ask) {
    try {
      String requestUrl = "http://" + httpNode + "/wallet/getakfromask";
      JsonObject userBaseObj2 = new JsonObject();
      userBaseObj2.addProperty("value", ask);
      response = createConnect(requestUrl, userBaseObj2);
    } catch (Exception e) {
      e.printStackTrace();
      httppost.releaseConnection();
      return null;
    }
    return response;
  }

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

**File:** framework/src/test/java/org/tron/common/utils/client/utils/HttpMethed.java (L4187-4196)
```java
        String spendauthsigUrl = "http://" + httpNode + "/wallet/createspendauthsig";
        JSONObjectWarp spendauthsigJson =
            new JSONObjectWarp()
                .put("ask", ask)
                .put("alpha", ByteArray.toHexString(alpha))
                .put("tx_hash", hash);
        String spendauthsigStr = spendauthsigJson.toJSONString();
        JsonObject spendauthsigObj = new JsonParser().parse(spendauthsigStr).getAsJsonObject();
        logger.info("spendauthsigObj:" + spendauthsigObj.toString());
        response = createConnect(spendauthsigUrl, spendauthsigObj);
```

**File:** framework/src/test/java/org/tron/common/utils/client/GrpcClient.java (L58-72)
```java
  public GrpcClient(String fullnode, String soliditynode) {
    if (!(fullnode.isEmpty())) {
      channelFull = ManagedChannelBuilder.forTarget(fullnode)
          .usePlaintext()
          .build();
      blockingStubFull = WalletGrpc.newBlockingStub(channelFull);
    }
    if (!(soliditynode.isEmpty())) {
      channelSolidity = ManagedChannelBuilder.forTarget(soliditynode)
          .usePlaintext()
          .build();
      blockingStubSolidity = WalletSolidityGrpc.newBlockingStub(channelSolidity);
      blockingStubExtension = WalletExtensionGrpc.newBlockingStub(channelSolidity);
    }
  }
```
