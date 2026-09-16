### Title
`node.disabledApi` access-control list is not enforced on the JSON-RPC interface, allowing disabled/restricted wallet endpoints to still be queried by any anonymous client - (File: `common/src/main/resources/reference.conf`, `framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcServlet.java`)

### Summary
The XWiki advisory describes a REST endpoint that ignored the wiki's configured view-permission restriction ("Prevent unregistered user to view pages") and still returned protected data to unauthenticated users. java-tron has an analogous, self-documented gap: the node operator's `node.disabledApi` restriction — the mechanism intended to block specific wallet API methods from being served to remote clients — is enforced only on the HTTP, gRPC, and PBFT interfaces, but is explicitly **not** enforced on the JSON-RPC interface, per the comment in the config file itself.

### Finding Description
`node.disabledApi` is the operator-facing control for turning off specific API methods (e.g. `getaccount`, `getnowblock2`) that should not be publicly reachable. [1](#0-0) 

Enforcement is implemented per-protocol:
- HTTP: `HttpApiAccessFilter.isDisabled()` checks the endpoint's path segment against `disabledApiList` and rejects it with `"this API is unavailable due to config"`. [2](#0-1) 
- gRPC: `RpcApiAccessInterceptor.isDisabled()` performs the equivalent check against the full gRPC method name. [3](#0-2) 

These filters are wired into `FullNodeHttpApiService`, `SolidityNodeHttpApiService`, and `HttpApiOnPBFTService`, but there is no equivalent filter/interceptor registered for `JsonRpcServlet`, which dispatches all `eth_*`/Tron JSON-RPC calls through `TronJsonRpcImpl` via a `JsonRpcServer` with no `disabledApi`/access-control check anywhere in its request path. [4](#0-3) 

This is explicitly acknowledged in the configuration itself: *"Disabled API list (works for http, rpc and pbft, not jsonrpc)."* [5](#0-4) 

Many JSON-RPC methods on `TronJsonRpc`/`TronJsonRpcImpl` expose the exact same underlying `Wallet` data that the HTTP `/wallet/getaccount`, `/wallet/getnowblock`, etc. servlets expose (account balances, transaction/receipt lookups, block data). An operator who adds `"getaccount"` or `"gettransactionbyid"` (etc.) to `node.disabledApi` — intending to fully block that data from being queried remotely, exactly as documented in `docs/configuration.md` — will succeed for `/wallet/*`, gRPC, and `/walletpbft/*` paths, but the equivalent JSON-RPC method remains fully reachable by any anonymous client on the `node.jsonrpc.httpFullNodePort`. [6](#0-5) 

### Impact Explanation
This is a confirmed access-control-bypass in the reachable query surface: an operator's explicit intent to disable an API is silently ignored for one of three parallel query protocols exposed by the node, letting any anonymous API client retrieve data through the JSON-RPC channel that the operator deliberately restricted. Test coverage for `HttpApiAccessFilterTest` and `RpcApiAccessInterceptorTest` demonstrates the intended behavior only for HTTP/gRPC/PBFT; no analogous JSON-RPC test exists, confirming the gap is untested/unenforced. [7](#0-6) 

Since the disabled data (accounts, blocks, transactions) is inherently public ledger data rather than access-controlled private information, the confidentiality impact is limited — this differs from XWiki's true page-permission bypass, where "Prevent unregistered user to view" is a genuine privacy boundary. In java-tron there is no genuine privacy boundary being crossed; `disabledApi` is primarily an operational/DoS-mitigation and API-surface-reduction control, not a confidentiality control. The impact is therefore best characterized as "an API the node operator intended to no longer serve remains servable" — closer to a configuration-enforcement gap than an unauthorized-account-operation, fund-theft, or node-crash class of bug required by the validation rules.

### Likelihood Explanation
Trivial to trigger: any client that can reach the node's configured `node.jsonrpc.httpFullNodePort` can call the disabled method via a standard JSON-RPC POST request with no authentication, bypassing the entire `disabledApi` enforcement path.

### Recommendation
Not applicable in ask-only mode — this response is informational.

### Proof of Concept
1. Configure `node.disabledApi = ["getaccount"]` and enable JSON-RPC (`node.jsonrpc.httpFullNodeEnable = true`).
2. Confirm `/wallet/getaccount` on the HTTP port now returns `{"Error":"this API is unavailable due to config"}` (per `HttpApiAccessFilterTest.testHttpFilter`). [8](#0-7) 
3. Send an equivalent account-balance query (e.g. `eth_getBalance`) to the JSON-RPC port (`node.jsonrpc.httpFullNodePort`, default 8545) for the same address.
4. Observe the JSON-RPC call succeeds and returns the account data, because `JsonRpcServlet` never consults `CommonParameter.getInstance().getDisabledApiList()`. [4](#0-3) 

**Note on confidence:** Given the strict impact bar in the validation rules (unauthorized account operation, fund theft/freezing, unbacked balance, node crash/halt, chain split, key disclosure, RCE, or an API the node can no longer serve), and given that java-tron's "disabled" data is public ledger data rather than genuinely private/permissioned data, this analog is a real, verifiable code/config gap but its severity mapping to the XWiki High-severity confidentiality bypass is weak. I am flagging this uncertainty explicitly rather than overstating the impact.

### Citations

**File:** common/src/main/resources/reference.conf (L445-449)
```text
  # Disabled API list (works for http, rpc and pbft, not jsonrpc). Case insensitive.
  disabledApi = [
    # "getaccount",
    # "getnowblock2"
  ]
```

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L26-74)
```java
  @Override
  public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain) {
    try {
      if (request instanceof HttpServletRequest) {
        String contextPath = ((HttpServletRequest) request).getContextPath();
        String endpoint = contextPath + ((HttpServletRequest) request).getServletPath();
        HttpServletResponse resp = (HttpServletResponse) response;

        if (isDisabled(endpoint)) {
          resp.setStatus(HttpServletResponse.SC_NOT_FOUND);
          resp.setContentType("application/json; charset=utf-8");
          JSONObject jsonObject = new JSONObject();
          jsonObject.put("Error", "this API is unavailable due to config");
          resp.getWriter().println(jsonObject.toJSONString());
          return;
        }

        CharResponseWrapper responseWrapper = new CharResponseWrapper(resp);
        chain.doFilter(request, responseWrapper);

      } else {
        chain.doFilter(request, response);
      }

    } catch (Exception e) {
      logger.error("http api access filter exception: {}", e.getMessage());
    }
  }

  @Override
  public void destroy() {

  }

  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      endpoint = URI.create(endpoint).normalize().toString();
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[2].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.warn("check isDisabled except, endpoint={}, {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```

**File:** framework/src/main/java/org/tron/core/services/ratelimiter/RpcApiAccessInterceptor.java (L19-54)
```java
  @Override
  public <ReqT, RespT> Listener<ReqT> interceptCall(ServerCall<ReqT, RespT> call,
      Metadata headers,
      ServerCallHandler<ReqT, RespT> next) {

    String endpoint = call.getMethodDescriptor().getFullMethodName();

    try {
      if (isDisabled(endpoint)) {
        call.close(Status.UNAVAILABLE
            .withDescription("this API is unavailable due to config"), headers);
        return new ServerCall.Listener<ReqT>() {};

      } else {
        return next.startCall(call, headers);
      }
    } catch (Exception e) {
      logger.error("check rpc api access Error: {}", e.getMessage());
      return next.startCall(call, headers);
    }
  }

  private boolean isDisabled(String endpoint) {
    boolean disabled = false;

    try {
      List<String> disabledApiList = CommonParameter.getInstance().getDisabledApiList();
      if (!disabledApiList.isEmpty()) {
        disabled = disabledApiList.contains(endpoint.split("/")[1].toLowerCase(Locale.ROOT));
      }
    } catch (Exception e) {
      logger.error("check isDisabled except, endpoint={}, error is {}", endpoint, e.getMessage());
    }

    return disabled;
  }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcServlet.java (L104-149)
```java
  @Override
  protected void doPost(HttpServletRequest req, HttpServletResponse resp) throws IOException {
    CommonParameter parameter = CommonParameter.getInstance();

    // Transport IOException from readBody propagates as HTTP 500 (genuine IO failure).
    byte[] body = readBody(req.getInputStream());
    JsonNode rootNode;
    try {
      rootNode = MAPPER.readTree(body);
      if (rootNode == null || rootNode.isMissingNode()) {
        writeJsonRpcError(resp, JsonRpcError.PARSE_ERROR, "JSON parse error", null, false);
        return;
      }
    } catch (JsonProcessingException e) {
      if (e instanceof StreamConstraintsException) {
        writeJsonRpcError(resp, JsonRpcError.PARSE_ERROR, e.getMessage(), null, false);
      } else {
        writeJsonRpcError(resp, JsonRpcError.PARSE_ERROR, "JSON parse error", null, false);
      }
      return;
    }

    if (!rootNode.isObject() && !rootNode.isArray()) {
      writeJsonRpcError(resp, JsonRpcError.INVALID_REQUEST, "Invalid Request", null, false);
      return;
    }

    boolean isBatch = rootNode.isArray();
    if (isBatch && rootNode.isEmpty()) {
      writeJsonRpcError(resp, JsonRpcError.INVALID_REQUEST, "Invalid Request", null, false);
      return;
    }
    int batchSize = parameter.getJsonRpcMaxBatchSize();
    if (isBatch && batchSize > 0 && rootNode.size() > batchSize) {
      writeJsonRpcError(resp, JsonRpcError.EXCEED_LIMIT,
          "Batch size " + rootNode.size() + " exceeds the limit of " + batchSize, null, true);
      return;
    }

    int maxResponseSize = parameter.getJsonRpcMaxResponseSize();
    if (isBatch) {
      handleBatch(resp, rootNode, maxResponseSize);
    } else {
      handleSingle(req, resp, rootNode, body, maxResponseSize);
    }
  }
```

**File:** docs/configuration.md (L121-128)
```markdown
To disable an API endpoint that you do not want to expose publicly, set its `Enable` flag to `false` or add endpoints to `node.disabledApi`:

```hocon
node.disabledApi = [
  "getaccount",
  "getnowblock2"
]
```
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L51-90)
```java
  @Test
  public void testHttpFilter() {
    appT.startup();
    List<String> disabledApiList = new ArrayList<>();
    disabledApiList.add("getaccount");
    disabledApiList.add("getnowblock");

    List<String> emptyList = Collections.emptyList();

    List<String> patterns = new ArrayList<>();
    patterns.add("/walletsolidity/");
    patterns.add("/walletpbft/");
    patterns.add("/wallet/");

    int httpPort;
    String ip = "127.0.0.1";
    for (String api : disabledApiList) {
      for (String pattern : patterns) {
        String urlPath = pattern + api;
        if (urlPath.contains("/walletsolidity")) {
          httpPort = Args.getInstance().getSolidityHttpPort();
        } else if (urlPath.contains("/walletpbft")) {
          httpPort = Args.getInstance().getPBFTHttpPort();
        } else {
          httpPort = Args.getInstance().getFullNodeHttpPort();
        }

        String url = String.format("http://%s:%d%s", ip, httpPort, urlPath);

        Args.getInstance().setDisabledApiList(disabledApiList);
        String response = sendGetRequest(url);
        Assert.assertEquals("{\"Error\":\"this API is unavailable due to config\"}",
            response);

        Args.getInstance().setDisabledApiList(emptyList);
        int statusCode = getRequestCode(url);
        Assert.assertEquals(HttpStatus.SC_OK, statusCode);
      }
    }
  }
```
