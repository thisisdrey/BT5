### Title
`/wallet/getnodeinfo` HTTP and gRPC `GetNodeInfo` API exposes sensitive internal node, peer, and machine information to any anonymous caller - (File: `framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java`)

### Summary
The `GetNodeInfoServlet` (mapped to `/wallet/getnodeinfo`, `/walletsolidity/getnodeinfo`, `/walletpbft/getnodeinfo`) and the equivalent gRPC `getNodeInfo` RPC in `RpcApiService` return a detailed `NodeInfo` object to any unauthenticated caller who can reach the node's HTTP/gRPC port. This mirrors the referenced Mattermost issue (CWE-200, GHSA-q3g9-hgrx-hwhx), where an API exposed sensitive infrastructure information (team URLs) without any access control. Here, the exposed information is broader: full peer list with IPs/ports/node IDs, disconnect reasons, machine resource details (CPU/memory/OS/Java version), and node configuration, none of which requires authentication or authorization by default.

### Finding Description
`GetNodeInfoServlet.doGet` simply calls `nodeInfoService.getNodeInfo()` and serializes the result to JSON with no authentication, authorization, or IP allow-listing beyond a generic rate limiter: [1](#0-0) 

`NodeInfoService.getNodeInfo()` aggregates connection info, machine info, config info, block info, and cheat-witness info: [2](#0-1) 

The `MachineInfo` includes thread count, CPU count/rate, total/free physical memory, JVM heap usage, Java version, and OS name — internal host details that should not be exposed to arbitrary remote clients: [3](#0-2) 

The `PeerInfo` list within `NodeInfo.transferToProtoEntity()` discloses each connected peer's host, port, node ID, sync status, disconnect reasons, and score — effectively a map of the node's P2P topology and the identities/addresses of its peers: [4](#0-3) 

The proto schema confirms these fields (`host`, `port`, `nodeId`, `localDisconnectReason`, `remoteDisconnectReason`, etc.) are all serialized in the response: [5](#0-4) 

The same unauthenticated data is also reachable via the gRPC `getNodeInfo` RPC in `RpcApiService`, and via the Solidity/PBFT HTTP variants (`GetNodeInfoOnSolidityServlet`, `GetNodeInfoOnPBFTServlet`), all of which simply forward to the same `NodeInfoService`: [6](#0-5) [7](#0-6) 

These endpoints are wired up by default when the FullNode/Solidity HTTP services are enabled, which is the out-of-the-box configuration (`fullNodeEnable = true`, `solidityEnable = true`): [8](#0-7) [9](#0-8) 

There is no per-API authentication mechanism built in; the only opt-in mitigation is the operator manually adding `getnodeinfo` to `node.disabledApi`, which is not the default: [10](#0-9) 

### Impact Explanation
An anonymous remote client can enumerate a node's full peer graph (IP addresses, ports, node IDs, sync/connection health, disconnect history) and internal host resource characteristics (memory pressure, CPU load, OS, Java version) with a single unauthenticated HTTP GET or gRPC call. This is reconnaissance-grade information disclosure (CWE-200) that materially aids targeted network attacks (e.g., identifying weak/overloaded peers or specific machine fingerprints for exploit targeting), analogous to how the Mattermost advisory's team-URL leak aided unauthorized access/reconnaissance against the affected server.

### Likelihood Explanation
Likelihood is high: the endpoint requires no credentials, no special network position, and no on-chain transaction — a single anonymous HTTP GET to `/wallet/getnodeinfo` (or the gRPC equivalent) on any publicly reachable FullNode/SolidityNode is sufficient, and this is the default enabled configuration.

### Recommendation
Restrict `getnodeinfo` (and equivalent gRPC/PBFT/Solidity variants) to trusted/internal callers by default — e.g., disable it by default for public-facing nodes, gate it behind `walletExtensionApi`/an explicit opt-in flag, or strip peer IP/nodeId/machine details from the public response while keeping only chain-sync-relevant fields (block, solidityBlock) for general clients.

### Proof of Concept
```
curl -s http://<public-fullnode-host>:8090/wallet/getnodeinfo
```
Response JSON includes `peerList[].host`, `peerList[].port`, `peerList[].nodeId`, `peerList[].localDisconnectReason`/`remoteDisconnectReason`, and `machineInfo` (memory, CPU, OS, Java version), all without any authentication, as produced by `GetNodeInfoServlet.doGet` → `NodeInfoService.getNodeInfo()` → `NodeInfo.transferToProtoEntity()`.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L14-24)
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
```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L61-69)
```java
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

**File:** common/src/main/java/org/tron/common/entity/NodeInfo.java (L142-169)
```java
    for (PeerInfo peerInfo : getPeerList()) {
      Protocol.NodeInfo.PeerInfo.Builder peerInfoBuilder = Protocol.NodeInfo.PeerInfo.newBuilder();
      peerInfoBuilder.setLastSyncBlock(peerInfo.getLastSyncBlock());
      peerInfoBuilder.setRemainNum(peerInfo.getRemainNum());
      peerInfoBuilder.setLastBlockUpdateTime(peerInfo.getLastBlockUpdateTime());
      peerInfoBuilder.setSyncFlag(peerInfo.isSyncFlag());
      peerInfoBuilder.setHeadBlockTimeWeBothHave(peerInfo.getHeadBlockTimeWeBothHave());
      peerInfoBuilder.setNeedSyncFromPeer(peerInfo.isNeedSyncFromPeer());
      peerInfoBuilder.setNeedSyncFromUs(peerInfo.isNeedSyncFromUs());
      peerInfoBuilder.setHost(peerInfo.getHost());
      peerInfoBuilder.setPort(peerInfo.getPort());
      peerInfoBuilder.setNodeId(peerInfo.getNodeId());
      peerInfoBuilder.setConnectTime(peerInfo.getConnectTime());
      peerInfoBuilder.setAvgLatency(peerInfo.getAvgLatency());
      peerInfoBuilder.setSyncToFetchSize(peerInfo.getSyncToFetchSize());
      peerInfoBuilder.setSyncToFetchSizePeekNum(peerInfo.getSyncToFetchSizePeekNum());
      peerInfoBuilder.setSyncBlockRequestedSize(peerInfo.getSyncBlockRequestedSize());
      peerInfoBuilder.setUnFetchSynNum(peerInfo.getUnFetchSynNum());
      peerInfoBuilder.setBlockInPorcSize(peerInfo.getBlockInPorcSize());
      peerInfoBuilder.setHeadBlockWeBothHave(peerInfo.getHeadBlockWeBothHave());
      peerInfoBuilder.setIsActive(peerInfo.isActive());
      peerInfoBuilder.setScore(peerInfo.getScore());
      peerInfoBuilder.setNodeCount(peerInfo.getNodeCount());
      peerInfoBuilder.setInFlow(peerInfo.getInFlow());
      peerInfoBuilder.setDisconnectTimes(peerInfo.getDisconnectTimes());
      peerInfoBuilder.setLocalDisconnectReason(peerInfo.getLocalDisconnectReason());
      peerInfoBuilder.setRemoteDisconnectReason(peerInfo.getRemoteDisconnectReason());
      builder.addPeerInfoList(peerInfoBuilder.build());
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

**File:** framework/src/main/java/org/tron/core/services/interfaceOnSolidity/http/GetNodeInfoOnSolidityServlet.java (L14-26)
```java
public class GetNodeInfoOnSolidityServlet extends GetNodeInfoServlet {

  @Autowired
  private WalletOnSolidity walletOnSolidity;

  protected void doGet(HttpServletRequest request, HttpServletResponse response) {
    walletOnSolidity.futureGet(() -> super.doGet(request, response));
  }

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    walletOnSolidity.futureGet(() -> super.doPost(request, response));
  }
}
```

**File:** framework/src/main/java/org/tron/core/services/http/FullNodeHttpApiService.java (L396-396)
```java
    context.addServlet(new ServletHolder(getNodeInfoServlet), "/wallet/getnodeinfo");
```

**File:** common/src/main/resources/reference.conf (L266-270)
```text
  # HTTP API settings.
  http {
    fullNodeEnable = true # Whether to enable FullNode HTTP API.
    fullNodePort = 8090  # FullNode HTTP API port.
    solidityEnable = true # Whether to enable Solidity HTTP API.
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
