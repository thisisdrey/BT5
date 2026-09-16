### Title
Unauthenticated `/wallet/getnodeinfo` HTTP/gRPC API discloses node version, network topology, and system information - (File: `framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java`)

### Summary
The FullNode/SolidityNode/PBFTNode HTTP API exposes a `getnodeinfo` endpoint that returns detailed software version, configuration, and machine information to any unauthenticated caller, mirroring the GeoWebCache home-page information-disclosure pattern (CVE-2024-38524, CWE-200).

### Finding Description
`GetNodeInfoServlet.doGet()` simply calls `NodeInfoService.getNodeInfo()` and writes the full result as JSON with no authentication, authorization, or field redaction beyond the generic rate limiter it extends (`RateLimiterServlet`). [1](#0-0) 

`NodeInfoService.getNodeInfo()` aggregates several categories of sensitive data:
- `setConfigNodeInfo` includes exact software version (`Version.getVersion()`, `Version.VERSION_CODE`), p2p protocol version, listen ports, discovery flag, backup listen port/priority, participation-rate and adaptive-energy configuration. [2](#0-1) 
- `setMachineInfo` includes JVM/OS version, CPU count, total/free memory, and (when present) deadlocked-thread stack traces. [3](#0-2) 
- `setConnectInfo` enumerates every connected peer with its host, port, node ID, sync/latency state. [4](#0-3) 

This endpoint is registered without any authentication requirement across FullNode, SolidityNode, and PBFT interfaces (`/wallet/getnodeinfo`, `/walletsolidity/getnodeinfo`), and is also exposed over gRPC via `RpcApiService.getNodeInfo`. [5](#0-4) [6](#0-5) 

The only gate in front of any wallet HTTP endpoint is `HttpApiAccessFilter`, which merely checks whether the API name is present in an operator-configured `disabledApiList` (empty/disabled by default) — it performs no authentication or credential check. [7](#0-6) 

### Impact Explanation
Any anonymous client that can reach a java-tron FullNode/SolidityNode's HTTP or gRPC port can fingerprint the exact software version and code revision, machine/OS/JVM details, backup and peer topology, and internal configuration parameters. This information materially assists an attacker in selecting version-specific exploits, identifying weakly configured or high-value backup/witness nodes, and mapping the network's peer topology for follow-on targeted attacks (e.g., against consensus or sync-critical peers) — the same class of risk as the referenced GeoWebCache advisory (CWE-200, precise version/config fingerprinting enabling further attacks).

### Likelihood Explanation
High likelihood: the endpoint requires no signed transaction, no credentials, and no special network position — a single anonymous HTTP GET/POST to `/wallet/getnodeinfo` (or gRPC `getNodeInfo`) is sufficient, and it is enabled by default (disabling it requires explicit operator opt-in via `disabledApiList`).

### Recommendation
Restrict `getnodeinfo` (and equivalent gRPC method) to trusted/internal callers by default (e.g., require it to be explicitly enabled, or gate it behind the same mechanism as other administrative APIs), and strip highly sensitive fields (JVM/OS details, deadlock stack traces, backup member counts/priority, and full peer host/port/node-ID lists) from the default unauthenticated response, following the same mitigation direction as GHSA-jm79-7xhw-6f6f (hide version/config/storage details from unauthenticated requests).

### Proof of Concept
```
curl http://<node-host>:8090/wallet/getnodeinfo
```
No API key, transaction signature, or session is required; the response contains `configNodeInfo.codeVersion`, `configNodeInfo.versionNum`, `machineInfo.osName`/`javaVersion`, and the full `peerList` as shown in `NodeInfoService.getNodeInfo()`.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L14-34)
```java
@Component
@Slf4j(topic = "API")
public class GetNodeInfoServlet extends RateLimiterServlet {

  @Autowired
  private NodeInfoService nodeInfoService;

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

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L71-124)
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
    //dead lock thread
    long[] deadlockedIds = threadMXBean.findDeadlockedThreads();
    if (ArrayUtils.isNotEmpty(deadlockedIds)) {
      machineInfo.setDeadLockThreadCount(deadlockedIds.length);
      ThreadInfo[] deadlockInfos = threadMXBean.getThreadInfo(deadlockedIds);
      List<DeadLockThreadInfo> deadLockThreadInfoList = new ArrayList<>();
      for (ThreadInfo deadlockInfo : deadlockInfos) {
        DeadLockThreadInfo deadLockThreadInfo = new DeadLockThreadInfo();
        deadLockThreadInfo.setName(deadlockInfo.getThreadName());
        deadLockThreadInfo.setLockName(deadlockInfo.getLockName());
        deadLockThreadInfo.setLockOwner(deadlockInfo.getLockOwnerName());
        deadLockThreadInfo.setBlockTime(deadlockInfo.getBlockedTime());
        deadLockThreadInfo.setWaitTime(deadlockInfo.getWaitedTime());
        deadLockThreadInfo.setState(deadlockInfo.getThreadState().name());
        deadLockThreadInfo.setStackTrace(Arrays.toString(deadlockInfo.getStackTrace()));
        deadLockThreadInfoList.add(deadLockThreadInfo);
      }
      machineInfo.setDeadLockThreadInfoList(deadLockThreadInfoList);
    }
    nodeInfo.setMachineInfo(machineInfo);
  }
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L126-170)
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
  }
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

**File:** framework/src/main/java/org/tron/core/services/http/solidity/SolidityNodeHttpApiService.java (L270-271)
```java
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/wallet/getnodeinfo");
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/walletsolidity/getnodeinfo");
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
