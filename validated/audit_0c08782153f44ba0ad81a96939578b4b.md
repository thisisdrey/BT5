### Title
Unauthenticated Slowloris/connection-exhaustion DoS against the FullNode/Solidity/PBFT HTTP API — the default Jetty `serve()` path lacks a mandatory connection cap and a minimum-data-rate guard — ([File: framework/src/main/java/org/tron/common/application/HttpService.java])

### Summary
`HttpService.initServer()` builds the Jetty `Server` for every HTTP API (FullNode `8090`, Solidity `8091`, PBFT `8092`, JSON-RPC) with `new Server(this.port)` and only attaches a `ConnectionLimit` bean if the operator explicitly configures `node.http... maxHttpConnectNumber > 0`. No `HttpConfiguration.setMinRequestDataRate()` (or equivalent low-and-slow guard) is ever configured on the connector, and no explicit idle-timeout/header-read-timeout override is set beyond Jetty's connector defaults. [1](#0-0) 

### Finding Description
This mirrors the reported bug class exactly: "allocation of resources without limits in the default serve() path" via missing connection caps and read timeouts, enabling a Slowloris-style attack. In java-tron's implementation:

- The connection cap (`ConnectionLimit`) is opt-in, not a secure-by-default control — it is only installed when `maxHttpConnectNumber > 0`; a node run with default/unset config exposes an HTTP listener with an unbounded number of concurrent sockets. [2](#0-1) 
- `SizeLimitHandler` only limits the total *bytes* of a request body once dispatched; it does nothing to bound how slowly those bytes may arrive, so a client can dribble a request (headers or chunked body) one byte at a time indefinitely while remaining under the size cap. [3](#0-2) 
- There is no explicit `HttpConfiguration`/`ServerConnector` construction where a `minRequestDataRate`, blocking-timeout, or header-read timeout is set — Jetty's connector is created through the bare `new Server(port)` convenience constructor, which relies on Jetty's out-of-the-box idle timeout only. An idle timeout resets on any byte received, so a classic Slowloris drip (send 1 byte just before the idle timeout expires) evades it entirely, exactly the flaw in the smithy-rs advisory.

By contrast, the gRPC path (`RpcService`) enforces secure, non-zero-by-default protections for `maxConnectionIdleInMillis`, `maxConcurrentCallsPerConnection` (0 is coerced to a secure default of 100), `maxHeaderListSize`, and an RST_STREAM flood limiter — showing the project explicitly hardened one transport (gRPC) against this bug class but left the HTTP servlet transport's connection cap opt-in and without any minimum-data-rate protection. [4](#0-3) [5](#0-4) 

### Impact Explanation
Any unauthenticated remote client reachable to the FullNode/Solidity/PBFT HTTP ports (`8090`/`8091`/`8092`) or JSON-RPC port can open many TCP connections and send partial/slow HTTP requests that never complete. On a default configuration (no `maxHttpConnectNumber` set), the Jetty server accepts an unbounded number of such connections, exhausting server sockets/file descriptors and Jetty's request-handling thread pool. This denies the HTTP API to legitimate transaction broadcasters and dApp/wallet clients — "an API the node can no longer serve" — without needing any signed transaction, credential, or prior interaction.

### Likelihood Explanation
High likelihood for any node operator who has not manually set `maxHttpConnectNumber` (it's off by default), since the attack requires only standard TCP sockets and slow writes — no cryptographic material, no privileged account, and no chain state is needed. It is a purely network-facing, always-reachable surface for any full node/solidity node exposing the HTTP API (the common configuration for RPC/dApp gateways).

### Recommendation
- Make the HTTP connection cap secure-by-default (mirroring the gRPC `maxConcurrentCallsPerConnection` fix): install `ConnectionLimit` with a sane non-zero default even when `maxHttpConnectNumber` is unset/zero, instead of treating `0` as "no limit."
- Configure Jetty's `HttpConfiguration` with `setMinRequestDataRate(...)` (or use `AbstractConnector#setIdleTimeout` together with a low-and-slow guard) on the connector built in `HttpService.initServer()` so drip-fed headers/bodies are terminated.
- Add an explicit, bounded header-read/request-line timeout for all HTTP listeners (FullNode/Solidity/PBFT/JSON-RPC), analogous to the timeouts already enforced on the gRPC path.

### Proof of Concept
1. Start a FullNode with default config (`maxHttpConnectNumber` unset, i.e., 0/disabled).
2. From an attacking host, open N TCP connections to port `8090` (or `8091`/`8092`) and, on each, send `POST /wallet/broadcasttransaction HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\n` followed by a single byte every ~20 seconds (below Jetty's idle timeout, never completing the body).
3. Repeat until server sockets/threads are exhausted; legitimate clients attempting to hit the same HTTP API port receive connection refusals or timeouts, confirming denial of service — reproducing the same "allocation of resources without limits" condition described in GHSA-jvxp-qmx7-gjpx, but against java-tron's own default HTTP `serve()` path in `HttpService.initServer()`. [6](#0-5)

### Citations

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L79-95)
```java
  protected void initServer() {
    this.apiServer = new Server(this.port);
    int maxHttpConnectNumber = Args.getInstance().getMaxHttpConnectNumber();
    if (maxHttpConnectNumber > 0) {
      this.apiServer.addBean(new ConnectionLimit(maxHttpConnectNumber, this.apiServer));
    }
    this.apiServer.setErrorHandler(new OversizedRequestErrorHandler());
  }

  protected ServletContextHandler initContextHandler() {
    ServletContextHandler context = new ServletContextHandler(ServletContextHandler.SESSIONS);
    context.setContextPath(this.contextPath);
    SizeLimitHandler sizeLimitHandler = new SizeLimitHandler(this.maxRequestSize, -1);
    sizeLimitHandler.setHandler(context);
    this.apiServer.setHandler(sizeLimitHandler);
    return context;
  }
```

**File:** framework/src/main/java/org/tron/common/application/RpcService.java (L94-114)
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
```

**File:** common/src/main/java/org/tron/core/config/args/NodeConfig.java (L360-378)
```java
    if (rpc.thread == 0) {
      rpc.thread = (Runtime.getRuntime().availableProcessors() + 1) / 2;
    }

    if (rpc.maxConcurrentCallsPerConnection < 0) {
      throw new TronError("node.rpc.maxConcurrentCallsPerConnection must be non-negative, got: "
          + rpc.maxConcurrentCallsPerConnection, PARAMETER_INIT);
    }
    if (rpc.maxConcurrentCallsPerConnection == 0) {
      logger.warn("Configuring [node.rpc.maxConcurrentCallsPerConnection] as 0 no longer "
          + "disables the limit; using the secure default of {}. Configure an explicit positive "
          + "value if more concurrency is required.",
          RpcConfig.DEFAULT_MAX_CONCURRENT_CALLS_PER_CONNECTION);
      rpc.maxConcurrentCallsPerConnection =
          RpcConfig.DEFAULT_MAX_CONCURRENT_CALLS_PER_CONNECTION;
    }
    if (rpc.maxConnectionIdleInMillis == 0) {
      rpc.maxConnectionIdleInMillis = Long.MAX_VALUE;
    }
```
