Confirmed: `FullNodeJsonRpcHttpService` extends `HttpService` (inherits the `ConnectionLimit` protection via `maxHttpConnectNumber`), while all gRPC entrypoints (`RpcApiService`, `RpcApiServiceOnSolidity`, `RpcApiServiceOnPBFT`) extend `RpcService`, which has no equivalent total-connection cap — only a per-connection idle/age timeout and per-connection concurrent-stream limit.

### Title
Unbounded gRPC connection acceptance combined with never-idle keep-alive traffic allows connection-slot exhaustion DoS - (File: framework/src/main/java/org/tron/common/application/RpcService.java)

### Summary
The rsync CVE-2026-70464 pattern is a daemon that accepts unlimited simultaneous connections and only tears them down via an I/O idle timeout, which an attacker defeats by trickling minimal traffic or stalling before the timeout starts. java-tron's gRPC API server (`node.rpc`, ports 50051/50061/50071) exhibits the same structural weakness: it enforces per-connection limits (`maxConcurrentCallsPerConnection`, `maxConnectionIdleInMillis`, `maxConnectionAgeInMillis`) but has **no cap on the total number of concurrent TCP connections** it will accept, unlike the sibling HTTP API server.

### Finding Description
`RpcService.initServerBuilder()` builds the gRPC `NettyServerBuilder` and configures only per-connection knobs: `maxConcurrentCallsPerConnection`, `flowControlWindow`, `maxConnectionIdle`, `maxConnectionAge`, `maxInboundMessageSize`, `maxHeaderListSize` [1](#0-0) . None of these bound the total number of simultaneous accepted connections.

By contrast, the sibling `HttpService` (used by the FullNode/Solidity/PBFT HTTP API and by `FullNodeJsonRpcHttpService`, the JSON-RPC endpoint) explicitly installs an `org.eclipse.jetty.server.ConnectionLimit` bean sized by `node.maxHttpConnectNumber` (default 50) precisely to stop this class of exhaustion [2](#0-1) . No analogous bean, `withOption(ChannelOption.SO_BACKLOG,...)` cap, or connection-count limiter exists in `RpcService`, so an unauthenticated remote client can open arbitrarily many TCP sockets to the gRPC listener.

Once connected, `maxConnectionIdleInMillis` only fires when a connection is observed as fully idle by gRPC-Netty's internal keepalive/idle machinery, which resets on any inbound traffic (including partial HTTP/2 frames, PING frames, or connection preface bytes sent slowly). An attacker can therefore keep a connection alive indefinitely by trickling minimal bytes just under the idle threshold — exactly the "trickle data at the minimum rate to avoid timeout" technique described in the CVE — while the default shipped `maxConnectionIdleInMillis = 60000` in `config.conf` [3](#0-2)  gives ample headroom, and operators who leave `reference.conf`'s own default of `maxConnectionIdleInMillis = 0` unset get *no* idle timeout at all (it is normalized to `Long.MAX_VALUE`) [4](#0-3) .

### Impact Explanation
Because there is no ceiling on total accepted gRPC connections, a single unauthenticated attacker can open enough sockets to exhaust the node's file descriptors and/or Netty worker event-loop capacity dedicated to the gRPC listener (port 50051 and the Solidity/PBFT variants), denying legitimate wallets, exchanges, and dApp backends the ability to broadcast transactions or query chain state through the gRPC API — "an API the node can no longer serve," which is one of the explicitly accepted impacts.

### Likelihood Explanation
No authentication, signature, or special privilege is required — any TCP client that can reach the configured gRPC port can attempt this. The per-connection idle timer only mitigates fully-silent connections; low-rate keep-alive traffic (or exploiting the negotiation window before the idle timer engages) evades it, mirroring the exact bypass technique in the source CVE. The main practical mitigation is that operators must expose the gRPC port to untrusted networks, which is common for public FullNode/SolidityNode deployments.

### Recommendation
Add a total concurrent-connection cap for the gRPC listener analogous to `HttpService`'s `ConnectionLimit`, e.g. a Netty `ChannelOption` connection counter or gRPC transport filter that rejects new connections once `node.rpc.maxConnections` (a new config, mirroring `maxHttpConnectNumber`) is reached, and consider tightening the idle-timeout semantics so a connection that has never completed HTTP/2 settings negotiation is force-closed after a short grace period regardless of trickled bytes.

### Proof of Concept
1. Configure a FullNode with default `config.conf` (`node.rpc.maxConnectionIdleInMillis = 60000`, no `maxHttpConnectNumber`-equivalent for gRPC).
2. From an attacker host, open N TCP connections to port 50051, complete only the TCP handshake and optionally the HTTP/2 client preface, and every ~50 seconds send a single HTTP/2 PING frame (or a byte of the preface) on each connection to keep it under the idle threshold — never opening any HTTP/2 stream (so `maxConcurrentCallsPerConnection`, which only bounds streams per connection, never triggers).
3. Repeat until the number of open sockets/file descriptors on the node approaches OS limits or the Netty boss/worker event loop backlog saturates.
4. Observe that legitimate `TransactionsBroadcast`/`GetAccount` gRPC calls from other clients start failing or timing out, confirming denial of service against the gRPC API while the block-production/sync path is unaffected.

### Citations

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

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L79-86)
```java
  protected void initServer() {
    this.apiServer = new Server(this.port);
    int maxHttpConnectNumber = Args.getInstance().getMaxHttpConnectNumber();
    if (maxHttpConnectNumber > 0) {
      this.apiServer.addBean(new ConnectionLimit(maxHttpConnectNumber, this.apiServer));
    }
    this.apiServer.setErrorHandler(new OversizedRequestErrorHandler());
  }
```

**File:** framework/src/main/resources/config.conf (L128-141)
```text
  rpc {
    enable = true
    port = 50051
    solidityEnable = true
    solidityPort = 50061
    PBFTEnable = true
    PBFTPort = 50071

    maxConnectionIdleInMillis = 60000
    minEffectiveConnection = 1

    # The switch of the reflection service for grpcurl tool. Default: false
    reflectionService = false
  }
```

**File:** common/src/main/java/org/tron/core/config/args/NodeConfig.java (L376-378)
```java
    if (rpc.maxConnectionIdleInMillis == 0) {
      rpc.maxConnectionIdleInMillis = Long.MAX_VALUE;
    }
```
