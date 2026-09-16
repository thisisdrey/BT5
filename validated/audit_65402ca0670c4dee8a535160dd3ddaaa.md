Based on the investigation, I have enough to produce the final analog report.

### Title
Unauthenticated `/wallet/getnodeinfo` HTTP API Leaks Internal System, Network Topology and Peer Data - (File: framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java)

### Summary
The ZimaOS report describes unauthenticated HTTP endpoints leaking sensitive installed-application and system configuration data to any anonymous requester. java-tron exposes an analogous unauthenticated HTTP endpoint, `/wallet/getnodeinfo` (and its `/monitor/getnodeinfo`, `/walletsolidity/getnodeinfo`, PBFT equivalents), which returns detailed internal machine, configuration and P2P peer information to any anonymous API client with no authentication or authorization check whatsoever.

### Finding Description
`GetNodeInfoServlet` is registered directly on the HTTP context with no signature/API-key/authentication requirement: `doGet` simply calls `nodeInfoService.getNodeInfo()` and writes the result as JSON to any caller. [1](#0-0) 

The only filter placed in front of this servlet, `HttpApiAccessFilter`, is not an authentication/authorization mechanism — it merely checks the operator-configured `disabledApiList` blocklist (`node.disabledApi`, empty by default) and otherwise passes every request through unchanged. [2](#0-1) 

`NodeInfoService.getNodeInfo()` aggregates and returns highly sensitive internal data: JVM/OS details (Java version, OS name, CPU/memory usage), full node configuration (P2P version, listen port, backup port/priority, DB version, max connections, adaptive-energy/contract-creation flags), and — critically — a full peer list containing each connected peer's raw IP address (`channel.getInetAddress()`), listening port, node ID, sync/disconnect state and disconnect reasons. [3](#0-2) [4](#0-3) [5](#0-4) 

This endpoint is registered by default on the FullNode HTTP API, the SolidityNode HTTP API, and the PBFT HTTP API servlet mappings, so it is reachable on any standard node deployment. [6](#0-5) [7](#0-6) [8](#0-7) 

The gRPC equivalent (`GetNodeInfo`) has the identical lack of authentication and returns the same data via `Wallet`/`RpcApiService`. [9](#0-8) 

### Impact Explanation
An anonymous, unauthenticated remote attacker can enumerate a node's real IP addresses of all its connected peers (including super representative/witness nodes if they are directly connected), their P2P node IDs, listening ports, and connection/sync health, as well as the target node's own P2P listen port, backup configuration and software/JVM version. This is reconnaissance data that materially assists targeted network-level denial-of-service or eclipse attacks against specific witness/SR nodes, and version fingerprinting to target known vulnerabilities. Because the leaked data is network/topology and system metadata (not funds directly), the impact class matches the reported CVE (sensitive information disclosure) rather than direct fund theft.

### Likelihood Explanation
Likelihood is high: no authentication, no special conditions, and no rate/permission gate beyond an operator-configurable blocklist that is empty by default. The endpoint is enabled out-of-the-box on FullNode, SolidityNode, and PBFT HTTP services and is walkable with a single unauthenticated GET/POST request.

### Recommendation
Treat `/wallet/getnodeinfo` (and `/monitor/getnodeinfo`, PBFT/Solidity variants) as an operationally sensitive endpoint: require it to be disabled by default or gated behind an authenticated/local-only access control (e.g., bind to localhost, require an API key, or add to `disabledApi` by default), and strip peer IP/nodeId fields from the public-facing JSON response, only exposing safe aggregate counters.

### Proof of Concept
```
curl http://<node-ip>:8090/wallet/getnodeinfo
```
This returns a JSON payload (via `GetNodeInfoServlet.doGet` → `NodeInfoService.getNodeInfo()`) that includes `peerList[].host`, `peerList[].nodeId`, `configNodeInfo.listenPort`, `configNodeInfo.backupListenPort`, `machineInfo.osName`, `machineInfo.javaVersion`, etc., with no credentials required.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L21-24)
```java
  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    try {
      NodeInfo nodeInfo = nodeInfoService.getNodeInfo();
      response.getWriter().println(JSON.toJSONString(nodeInfo));
```

**File:** framework/src/main/java/org/tron/core/services/filter/HttpApiAccessFilter.java (L27-53)
```java
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

**File:** framework/src/main/java/org/tron/core/services/interfaceOnSolidity/http/solidity/HttpApiOnSolidityService.java (L276-278)
```java
    context.addServlet(new ServletHolder(getNodeInfoOnSolidityServlet), "/wallet/getnodeinfo");
    context.addServlet(new ServletHolder(getNodeInfoOnSolidityServlet),
        "/walletsolidity/getnodeinfo");
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnPBFT/http/PBFT/HttpApiOnPBFTService.java (L225-225)
```java
    context.addServlet(new ServletHolder(getNodeInfoOnPBFTServlet), "/getnodeinfo");
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
