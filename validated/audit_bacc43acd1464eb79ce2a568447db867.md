### Title
Unauthenticated `/wallet/getnodeinfo` (and gRPC `GetNodeInfo`) API leaks internal node information without authentication - (File: `framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java`)

### Summary
Analogous to CVE-2021-26923 (Argo CD's unauthenticated `/api/version` leaking internal system info), java-tron exposes a `getnodeinfo` endpoint on every node interface (`/wallet/getnodeinfo`, `/wallet/getnodeinfo` on solidity/PBFT nodes, `/monitor/getnodeinfo`, and the gRPC `Wallet.GetNodeInfo` RPC) that returns detailed internal machine, network, and configuration data to any anonymous caller with no authentication or authorization check.

### Finding Description
`GetNodeInfoServlet.doGet()` calls `nodeInfoService.getNodeInfo()` and serializes the result directly to the HTTP response with no caller identity check: [1](#0-0) 

`NodeInfoService.getNodeInfo()` aggregates highly sensitive internal state:
- Machine internals: JVM heap/thread stats, `java.version`, OS name/version, CPU load, and full stack traces of deadlocked threads: [2](#0-1) 
- P2P peer topology: every connected peer's raw IP/host, port, node ID, latency, disconnect reasons, sync state: [3](#0-2) 
- Node/network configuration: code version, P2P version, listen port, discovery enablement, active/passive/backup node counts, backup listen port, DB version, energy/participation parameters: [4](#0-3) 

This servlet is registered on the main full-node HTTP context at `/wallet/getnodeinfo` and `/monitor/getnodeinfo`, on the solidity node at `/wallet/getnodeinfo` / `/walletsolidity/getnodeinfo`, and on the PBFT node at `/getnodeinfo`: [5](#0-4) [6](#0-5) 

The only filter applied to these paths is `HttpApiAccessFilter`, which exclusively checks a configurable "disabled API list" — it performs no authentication, no IP allow-listing, and no rate limiting decision beyond the generic `RateLimiterServlet` base class: [7](#0-6) 

The same data is reachable unauthenticated via gRPC as well: [8](#0-7) 

### Impact Explanation
Any anonymous network client — with zero credentials, zero on-chain stake, and no signed transaction — can enumerate the full topology of the node's P2P mesh (raw IP addresses, ports, and node IDs of every connected peer), the node's exact software/JVM/OS version, and internal configuration (listen ports, backup member counts, discovery settings). This is precisely the "leaks internal information … not protected with authentication" bug class from CVE-2021-26923. In java-tron this information materially aids network-level reconnaissance against specific super-representative/witness or full nodes (e.g., identifying and directly targeting peer IPs/ports for connection exhaustion or eclipse-style attacks, or fingerprinting exact software versions to select known exploits), which is significantly more sensitive than Argo CD's original leaked build/version string.

### Likelihood Explanation
Trivially exploitable: a single unauthenticated `GET /wallet/getnodeinfo` (or `GET /monitor/getnodeinfo`, or gRPC `GetNodeInfo`) HTTP request against any full/solidity/PBFT node returns the full information dump. No special network position, credentials, or on-chain assets are required — likelihood is high given the endpoint is enabled by default on public-facing nodes and not present in `disabledApiList` by default.

### Recommendation
Require authentication/authorization (e.g., local-only binding, API key, or IP allow-list) for `getnodeinfo` across `FullNodeHttpApiService`, `SolidityNodeHttpApiService`, `HttpApiOnPBFTService`, and the gRPC `GetNodeInfo` RPC, or strip peer IP/port/node-ID and machine/OS/JVM details from the response for unauthenticated callers, exposing only non-sensitive chain-sync status fields.

### Proof of Concept
```
curl http://<any-tron-node>:8090/wallet/getnodeinfo
```
No API key, JWT, or account signature is required; the response JSON includes `peerList[].host`, `peerList[].port`, `peerList[].nodeId`, `machineInfo.osName`, `machineInfo.javaVersion`, and `configNodeInfo.listenPort`/`backupListenPort`/`backupMemberSize`, confirming the unauthenticated internal-information leak.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L21-34)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      NodeInfo nodeInfo = nodeInfoService.getNodeInfo();
      response.getWriter().println(JSON.toJSONString(nodeInfo));

    } catch (Exception e) {
      logger.error("", e);
      try {
        response.getWriter().println(Util.printErrorMsg(e));
      } catch (IOException ioe) {
        logger.debug("IOException: {}", ioe.getMessage());
      }
    }
  }
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L71-84)
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
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L126-169)
```java
  private void setConnectInfo(NodeInfo nodeInfo) {
    int activeCnt = PeerManager.getActivePeersCount().get();
    int passiveCnt = PeerManager.getPassivePeersCount().get();
    nodeInfo.setCurrentConnectCount(activeCnt + passiveCnt);
    nodeInfo.setActiveConnectCount(activeCnt);
    nodeInfo.setPassiveConnectCount(passiveCnt);
    List<PeerInfo> peerInfoList = new ArrayList<>();
    for (PeerConnection peerConnection : PeerManager.getPeers()) {
      Channel channel = peerConnection.getChannel();
      NodeStatistics nodeStatistics = peerConnection.getNodeStatistics();
      P2pService P2pService = TronNetService.getP2pService();
      P2pConfig p2pConfig = TronNetService.getP2pConfig();
      PeerInfo peerInfo = new PeerInfo();
      peerInfo.setHeadBlockWeBothHave(peerConnection.getBlockBothHave().getString());
      peerInfo.setActive(peerConnection.getChannel().isActive());
      peerInfo.setAvgLatency(peerConnection.getChannel().getAvgLatency());
      peerInfo.setBlockInPorcSize(peerConnection.getSyncBlockInProcess().size());
      peerInfo.setConnectTime(channel.getStartTime());
      peerInfo.setDisconnectTimes(nodeStatistics.getDisconnectTimes());
      //peerInfo.setHeadBlockTimeWeBothHave(peerConnection.getHeadBlockTimeWeBothHave());
      peerInfo.setHost(channel.getInetAddress().toString());
      peerInfo.setLastBlockUpdateTime(peerConnection.getBlockBothHaveUpdateTime());
      peerInfo.setLastSyncBlock(peerConnection.getLastSyncBlockId() == null ? ""
          : peerConnection.getLastSyncBlockId().getString());
      ReasonCode reasonCode = nodeStatistics.getLocalDisconnectReason();
      peerInfo.setLocalDisconnectReason(reasonCode == null ? "" : reasonCode.toString());
      reasonCode = nodeStatistics.getRemoteDisconnectReason();
      peerInfo.setRemoteDisconnectReason(reasonCode == null ? "" : reasonCode.toString());
      peerInfo.setNeedSyncFromPeer(peerConnection.isNeedSyncFromPeer());
      peerInfo.setNeedSyncFromUs(peerConnection.isNeedSyncFromUs());
      int tableNodesSize = P2pService.getTableNodes().size();
      peerInfo.setNodeCount(tableNodesSize);
      peerInfo.setNodeId(Hex.encodeHexString(p2pConfig.getNodeID()));
      peerInfo.setPort(p2pConfig.getPort());
      peerInfo.setRemainNum(peerConnection.getRemainNum());
      peerInfo.setSyncBlockRequestedSize(peerConnection.getSyncBlockRequested().size());
      peerInfo.setSyncFlag(peerConnection.isDisconnect());
      peerInfo.setSyncToFetchSize(peerConnection.getSyncBlockToFetch().size());
      peerInfo.setSyncToFetchSizePeekNum(peerConnection.getSyncBlockToFetch().size() > 0
          ? peerConnection.getSyncBlockToFetch().peek().getNum() : -1);
      peerInfo.setUnFetchSynNum(peerConnection.getRemainNum());
      peerInfoList.add(peerInfo);
    }
    nodeInfo.setPeerList(peerInfoList);
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

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L396-396)
```java
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/wallet/getnodeinfo");
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L474-475)
```java
    context.addServlet(new ServletHolder(metricsServlet), "/monitor/getstatsinfo");
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/monitor/getnodeinfo");
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
