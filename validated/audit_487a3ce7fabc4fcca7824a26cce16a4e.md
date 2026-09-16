Confirmed finding: `FullNodeJsonRpcHttpService` (JSON-RPC endpoint at `/jsonrpc`) registers only `HttpInterceptor` as a filter and does **not** register `HttpApiAccessFilter`, unlike `FullNodeHttpApiService`, which explicitly wires `httpApiAccessFilter` against `/wallet/*`, `/net/listnodes`, `/monitor/getstatsinfo`, `/monitor/getnodeinfo`. [1](#0-0) [2](#0-1) 

### Title
`disabledApiList` Improper Access Control / Security-Feature Bypass via JSON-RPC endpoint - (File: `framework/src/main/java/org/tron/core/services/jsonrpc/FullNodeJsonRpcHttpService.java`)

### Summary
The `disabledApiList` node configuration is a documented security control that lets an operator disable specific wallet APIs (e.g. `getaccount`, sensitive query/transaction endpoints) to reduce their node's attack surface or enforce operational policy. This control is enforced by `HttpApiAccessFilter` for the HTTP `/wallet/*` interface and by `RpcApiAccessInterceptor` for gRPC. The JSON-RPC HTTP interface (`FullNodeJsonRpcHttpService`, mapped to `/jsonrpc`), however, only registers `HttpInterceptor` and never registers `HttpApiAccessFilter`, so any API "disabled" by the operator remains fully reachable through equivalent JSON-RPC methods implemented in `TronJsonRpcImpl`, which internally calls the same `Wallet` methods.

### Finding Description
`HttpApiAccessFilter.doFilter` checks the request path against `CommonParameter.getInstance().getDisabledApiList()` and returns HTTP 404 if the endpoint is disabled. [3](#0-2) 
This filter is only attached to the `/wallet/*`, `/net/listnodes`, `/monitor/getstatsinfo`, `/monitor/getnodeinfo` paths of `FullNodeHttpApiService`. [2](#0-1) 
`RpcApiAccessInterceptor` provides the equivalent enforcement for gRPC. [4](#0-3) 
`FullNodeJsonRpcHttpService`, which exposes the `/jsonrpc` endpoint backed by `TronJsonRpcImpl`, registers no such interceptor — only `HttpInterceptor` (a metrics/logging filter, not an access-control filter). [1](#0-0) 
`TronJsonRpcImpl` delegates the underlying data/functionality to the same `Wallet` instance used by the HTTP/gRPC servlets (e.g. `wallet.getAccount`, `wallet.getBlockById`, etc.), meaning an operator who disables `getaccount` via `disabledApiList` still exposes the same account data through `eth_getBalance`/`getTrxBalance` and other JSON-RPC methods. [5](#0-4) 

### Impact Explanation
An operator relying on `disabledApiList` to block a specific API (for privacy, DoS mitigation, or compliance reasons) does not actually block equivalent functionality if the JSON-RPC service is enabled, since the same underlying `Wallet` calls remain reachable. This is a security-feature bypass matching the CVE class (improper access control leading to bypass of an intended restriction), but the concrete impact is limited to unauthorized *read* access to data the operator intended to restrict (account balances, blocks, etc.) via the JSON-RPC path — there is no way to reach a state-changing/broadcast method through this specific gap because JSON-RPC's transaction-sending methods still require normal signature/permission verification in the actuator layer, and `disabledApiList` is a config feature (not itself protecting funds). This keeps the impact at "an API the node operator intended to disable remains reachable," which is a Medium-severity access-control bypass, not a funds-theft or crash primitive.

### Likelihood Explanation
The bypass requires no privileges: any client that can reach the node's JSON-RPC port (enabled via `jsonRpcHttpFullNodeEnable`) can invoke the equivalent method without any special setup, as soon as the operator has (mistakenly, but per documented capability) relied on `disabledApiList` for restricting HTTP/gRPC-only.

### Recommendation
Register an equivalent `HttpApiAccessFilter`-style check (aligned to JSON-RPC method names) in `FullNodeJsonRpcHttpService.addFilter`, or centralize the `disabledApiList` enforcement inside `JsonRpcServlet`/`TronJsonRpcImpl` so JSON-RPC method dispatch consults the same disabled-list before invoking `Wallet`.

### Proof of Concept
1. Configure `node.jsonrpc.httpFullNodePort` enabled and add `getaccount` to `node.disabledApiList`.
2. Confirm `curl http://<node>:<httpPort>/wallet/getaccount` now returns `{"Error":"this API is unavailable due to config"}` (per `HttpApiAccessFilterTest`). [6](#0-5) 
3. Issue the equivalent JSON-RPC request: `curl -X POST http://<node>:<jsonRpcPort>/jsonrpc -d '{"jsonrpc":"2.0","method":"eth_getBalance","params":["<address>","latest"],"id":1}'` and observe the balance is returned successfully, bypassing the operator's disabled-API restriction, since `getTrxBalance`/`eth_getBalance` calls `wallet.getAccount` directly with no `disabledApiList` check in the JSON-RPC path. [5](#0-4) 

**Caveat**: I was unable to fully verify (index limitations / no further tool calls available) whether `JsonRpcServlet` itself performs any independent disabled-API check before dispatching to `TronJsonRpcImpl`, nor whether other node types (Solidity/PBFT JSON-RPC services) have the same gap — a full audit of `JsonRpcServlet.java`, `JsonRpcOnSolidityServlet.java`, and `JsonRpcOnPBFTServlet.java` in a Devin session would be needed to close out this analysis with certainty.

### Citations

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/FullNodeJsonRpcHttpService.java (L35-43)
```java
  @Override
  protected void addFilter(ServletContextHandler context) {
    // filter
    ServletHandler handler = new ServletHandler();
    FilterHolder fh = handler
        .addFilterWithMapping(HttpInterceptor.class, "/*",
            EnumSet.of(DispatcherType.REQUEST));
    context.addFilter(fh, "/*", EnumSet.of(DispatcherType.REQUEST));
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L520-535)
```java
  @Override
  protected void addFilter(ServletContextHandler context) {
    // filters the specified APIs
    // when node is lite fullnode and openHistoryQueryWhenLiteFN is false
    context.addFilter(new FilterHolder(liteFnQueryHttpFilter), "/*",
        EnumSet.allOf(DispatcherType.class));

    // http access filter, it should have higher priority than HttpInterceptor
    context.addFilter(new FilterHolder(httpApiAccessFilter), "/*",
        EnumSet.allOf(DispatcherType.class));
    // note: if the pathSpec of servlet is not started with wallet, it should be included here
    context.getServletHandler().getFilterMappings()[1]
        .setPathSpecs(new String[] {"/wallet/*",
            "/net/listnodes",
            "/monitor/getstatsinfo",
            "/monitor/getnodeinfo"});
```

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L26-53)
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
```

**File:** framework/src/main/java/org/tron/core/services/ratelimiter/RpcApiAccessInterceptor.java (L17-39)
```java
public class RpcApiAccessInterceptor implements ServerInterceptor {

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
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L449-464)
```java
  @Override
  public String getTrxBalance(String address, String blockNumOrTag)
      throws JsonRpcInvalidParamsException {
    requireLatestBlockTag(blockNumOrTag);

    byte[] addressData = addressCompatibleToByteArray(address);

    Account account = Account.newBuilder().setAddress(ByteString.copyFrom(addressData)).build();
    Account reply = wallet.getAccount(account);
    long balance = 0;

    if (reply != null) {
      balance = reply.getBalance();
    }
    return ByteArray.toJsonHex(balance);
  }
```

**File:** framework/src/test/java/org/tron/core/services/filter/HttpApiAccessFilterTest.java (L51-88)
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
```
