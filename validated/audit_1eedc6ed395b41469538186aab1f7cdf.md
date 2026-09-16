### Title
Unauthenticated `/monitor/getnodeinfo` and gRPC `getNodeInfo` disclose internal node configuration and runtime data to any API client - (File: framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java)

### Summary
The HTTP endpoint `/monitor/getnodeinfo` and the equivalent gRPC `Wallet.getNodeInfo` RPC return a full `NodeInfo` object containing peer/network topology, node configuration, and JVM/OS runtime internals to *any* caller who can reach the FullNode HTTP/gRPC interface, with no authentication, authorization, or IP-restriction check. This is the java-tron analog of CVE-2017-2600 (Jenkins SECURITY-343), where node monitor data was exposed to low-privilege users via the remote API.

### Finding Description
`GetNodeInfoServlet.doGet()` calls `NodeInfoService.getNodeInfo()` and serializes the result directly to the HTTP response with no permission check of any kind: [1](#0-0) 

The servlet only extends `RateLimiterServlet`, which performs QPS/rate limiting but not authentication: [2](#0-1) 

The only other gatekeeper, `HttpApiAccessFilter`, merely checks whether the endpoint is in the operator-configured `disabledApiList`; by default `/monitor/getnodeinfo` (and `/wallet/listnodes`) are not disabled, so the check is a no-op for default configurations: [3](#0-2) 

`FullNodeHttpApiService` wires the servlet up at `/monitor/getnodeinfo` alongside other unauthenticated wallet endpoints: [4](#0-3) 

`NodeInfoService.getNodeInfo()` aggregates block sync state, connection counts, peer list, node configuration and JVM/OS machine info into the `NodeInfo` object: [5](#0-4) 

The `ConfigNodeInfo` portion exposes internal node configuration such as listen port, backup member configuration/priority, discovery settings, DB version, participation-rate and time-ratio thresholds, and contract-creation/adaptive-energy flags: [6](#0-5) 

The `MachineInfo` portion exposes JVM/OS internals: thread counts, deadlock thread stack traces, CPU core count, total/free memory, Java version, OS name, and per-memory-pool usage — directly analogous to Jenkins' SECURITY-343 "system configuration and runtime information" disclosure: [7](#0-6) 

The identical data is also reachable unauthenticated over gRPC via `Wallet.getNodeInfo`: [8](#0-7) 

The peer list (`peerInfoList`) additionally leaks connected peers' IPs, ports, node IDs, sync state, scores, and disconnect reasons — network topology/reconnaissance data useful for planning targeted network attacks (e.g., partition or eclipse attempts against specific peers): [9](#0-8) 

### Impact Explanation
Any anonymous client that can reach the FullNode HTTP (`:8090`) or gRPC (`:50051`) interface can enumerate: exact software/DB version (useful to target known vulnerabilities for that version), backup/HA member counts and priorities, discovery/connection limits, JVM heap/GC state and deadlock thread dumps (aiding DoS timing attacks), and the live peer topology (IP:port, node ID, sync lag, disconnect history) of the node's connections. This is purely an information-disclosure vulnerability — it does not by itself allow theft of funds or unauthorized transactions — but it materially aids reconnaissance for further attacks (network-level DoS/eclipse targeting, version-specific exploit targeting, resource-exhaustion timing) against the queried node and its peers.

### Likelihood Explanation
High: the endpoint requires no credentials, is enabled by default (not in `disabledApiList`), and both HTTP and gRPC surfaces are exposed to the internet on any public FullNode by default configuration.

### Recommendation
Restrict `/monitor/getnodeinfo`, `/wallet/listnodes`, `/monitor/getstatsinfo`, and the gRPC `getNodeInfo`/`listNodes` RPCs to trusted operators (e.g., require them to be explicitly whitelisted/disabled by default, bind to a localhost-only management port, or gate them behind an API-key/IP-allowlist mechanism similar to what `disabledApiList` provides but opt-in for sensitive introspection endpoints rather than opt-out).

### Proof of Concept
```
curl http://<full-node-ip>:8090/monitor/getnodeinfo
```
returns the full JSON `NodeInfo` payload (peer list, config, machine info) with no authentication, as served by [1](#0-0) . The equivalent unauthenticated gRPC call is `WalletGrpc.newBlockingStub(channel).getNodeInfo(EmptyMessage.getDefaultInstance())`.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L21-27)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      NodeInfo nodeInfo = nodeInfoService.getNodeInfo();
      response.getWriter().println(JSON.toJSONString(nodeInfo));

    } catch (Exception e) {
      logger.error("", e);
```

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L103-131)
```java
  @Override
  protected void service(HttpServletRequest req, HttpServletResponse resp)
      throws ServletException, IOException {

    RuntimeData runtimeData = new RuntimeData(req);
    IRateLimiter rateLimiter = container.get(KEY_PREFIX_HTTP, getClass().getSimpleName());

    // Check per-endpoint first to avoid consuming global IP/QPS quota for requests
    // that would be rejected by the per-endpoint limiter anyway. acquirePermit()
    // chooses blocking or non-blocking semantics based on rate.limiter.apiNonBlocking.
    boolean perEndpointAcquired = rateLimiter == null || rateLimiter.acquirePermit(runtimeData);
    boolean acquireResource = perEndpointAcquired && GlobalRateLimiter.acquirePermit(runtimeData);

    String contextPath = req.getContextPath();
    String url = Strings.isNullOrEmpty(req.getServletPath())
        ? MetricLabels.UNDEFINED : contextPath + req.getServletPath();
    // int64_as_string is honored only on GET requests (URL query). POST is intentionally
    // unsupported because reading the body here would consume request.getReader() and
    // break downstream servlets that read it themselves.
    if ("GET".equalsIgnoreCase(req.getMethod())) {
      JsonFormat.setInt64AsString(Util.getInt64AsString(req));
    }
    try {
      resp.setContentType("application/json; charset=utf-8");

      if (acquireResource) {
        Histogram.Timer requestTimer = Metrics.histogramStartTimer(
            MetricKeys.Histogram.HTTP_SERVICE_LATENCY, url);
        super.service(req, resp);
```

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L60-74)
```java
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

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L472-477)
```java
    context.addServlet(new ServletHolder(listNodesServlet), "/net/listnodes");

    context.addServlet(new ServletHolder(metricsServlet), "/monitor/getstatsinfo");
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/monitor/getnodeinfo");
    context.addServlet(new ServletHolder(marketSellAssetServlet), "/wallet/marketsellasset");
    context.addServlet(new ServletHolder(marketCancelOrderServlet), "/wallet/marketcancelorder");
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L60-69)
```java
  @MetricTime
  public NodeInfo getNodeInfo() {
    NodeInfo nodeInfo = new NodeInfo();
    setConnectInfo(nodeInfo);
    setMachineInfo(nodeInfo);
    setConfigNodeInfo(nodeInfo);
    setBlockInfo(nodeInfo);
    setCheatWitnessInfo(nodeInfo);
    return nodeInfo;
  }
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L71-103)
```java
  private void setMachineInfo(NodeInfo nodeInfo) {
    MachineInfo machineInfo = new MachineInfo();
    machineInfo.setThreadCount(threadMXBean.getThreadCount());
    machineInfo.setCpuCount(Runtime.getRuntime().availableProcessors());
    machineInfo.setTotalMemory(operatingSystemMXBean.getTotalPhysicalMemorySize());
    machineInfo.setFreeMemory(operatingSystemMXBean.getFreePhysicalMemorySize());
    machineInfo.setCpuRate(operatingSystemMXBean.getSystemCpuLoad());
    machineInfo.setJavaVersion(runtimeMXBean.getSystemProperties().get("java.version"));
    machineInfo
        .setOsName(operatingSystemMXBean.getName() + " " + operatingSystemMXBean.getVersion());
    machineInfo.setJvmTotalMemory(memoryMXBean.getHeapMemoryUsage().getMax());
    machineInfo.setJvmFreeMemory(
        memoryMXBean.getHeapMemoryUsage().getMax() - memoryMXBean.getHeapMemoryUsage().getUsed());
    machineInfo.setProcessCpuRate(operatingSystemMXBean.getProcessCpuLoad());
    List<MemoryDescInfo> memoryDescInfoList = new ArrayList<>();
    List<MemoryPoolMXBean> pools = ManagementFactory.getMemoryPoolMXBeans();
    if (CollectionUtils.isNotEmpty(pools)) {
      for (MemoryPoolMXBean pool : pools) {
        MemoryDescInfo memoryDescInfo = new MemoryDescInfo();
        memoryDescInfo.setName(pool.getName());
        memoryDescInfo.setInitSize(pool.getUsage().getInit());
        memoryDescInfo.setUseSize(pool.getUsage().getUsed());
        memoryDescInfo.setMaxSize(pool.getUsage().getMax());
        if (pool.getUsage().getMax() > 0) {
          memoryDescInfo.setUseRate((double) pool.getUsage().getUsed() / pool.getUsage().getMax());
        } else {
          memoryDescInfo
              .setUseRate((double) pool.getUsage().getUsed() / pool.getUsage().getCommitted());
        }
        memoryDescInfoList.add(memoryDescInfo);
      }
    }
    machineInfo.setMemoryDescInfoList(memoryDescInfoList);
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L172-195)
```java
  private void setConfigNodeInfo(NodeInfo nodeInfo) {
    ConfigNodeInfo configNodeInfo = new ConfigNodeInfo();
    configNodeInfo.setCodeVersion(Version.getVersion());
    configNodeInfo.setVersionNum(Version.VERSION_CODE);
    configNodeInfo.setP2pVersion(String.valueOf(parameter.getNodeP2pVersion()));
    configNodeInfo.setListenPort(parameter.getNodeListenPort());
    configNodeInfo.setDiscoverEnable(parameter.isNodeDiscoveryEnable());
    configNodeInfo.setActiveNodeSize(parameter.getActiveNodes().size());
    configNodeInfo.setPassiveNodeSize(parameter.getPassiveNodes().size());
    configNodeInfo.setSendNodeSize(parameter.getSeedNode().getAddressList().size());
    configNodeInfo.setMaxConnectCount(parameter.getMaxConnections());
    configNodeInfo.setSameIpMaxConnectCount(parameter.getMaxConnectionsWithSameIp());
    configNodeInfo.setBackupListenPort(parameter.getBackupPort());
    configNodeInfo.setBackupMemberSize(parameter.getBackupMembers().size());
    configNodeInfo.setBackupPriority(parameter.getBackupPriority());
    configNodeInfo.setDbVersion(2);
    configNodeInfo.setMinParticipationRate(parameter.getMinParticipationRate());
    configNodeInfo.setSupportConstant(parameter.isSupportConstant());
    configNodeInfo.setMinTimeRatio(parameter.getMinTimeRatio());
    configNodeInfo.setMaxTimeRatio(parameter.getMaxTimeRatio());
    configNodeInfo.setAllowCreationOfContracts(parameter.getAllowCreationOfContracts());
    configNodeInfo.setAllowAdaptiveEnergy(parameter.getAllowAdaptiveEnergy());
    nodeInfo.setConfigNodeInfo(configNodeInfo);
  }
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L2060-2068)
```java
    @Override
    public void getNodeInfo(EmptyMessage request, StreamObserver<NodeInfo> responseObserver) {
      try {
        responseObserver.onNext(nodeInfoService.getNodeInfo().transferToProtoEntity());
      } catch (Exception e) {
        responseObserver.onError(getRunTimeException(e));
      }
      responseObserver.onCompleted();
    }
```

**File:** protocol/src/main/protos/core/Tron.proto (L673-699)
```text
  message PeerInfo {
    string lastSyncBlock = 1;
    int64 remainNum = 2;
    int64 lastBlockUpdateTime = 3;
    bool syncFlag = 4;
    int64 headBlockTimeWeBothHave = 5;
    bool needSyncFromPeer = 6;
    bool needSyncFromUs = 7;
    string host = 8;
    int32 port = 9;
    string nodeId = 10;
    int64 connectTime = 11;
    double avgLatency = 12;
    int32 syncToFetchSize = 13;
    int64 syncToFetchSizePeekNum = 14;
    int32 syncBlockRequestedSize = 15;
    int64 unFetchSynNum = 16;
    int32 blockInPorcSize = 17;
    string headBlockWeBothHave = 18;
    bool isActive = 19;
    int32 score = 20;
    int32 nodeCount = 21;
    int64 inFlow = 22;
    int32 disconnectTimes = 23;
    string localDisconnectReason = 24;
    string remoteDisconnectReason = 25;
  }
```
