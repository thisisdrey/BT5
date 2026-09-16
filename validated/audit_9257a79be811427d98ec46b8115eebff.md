The `/wallet/getnodeinfo` endpoint is worth flagging as an information exposure analog, since it is reachable by any anonymous HTTP client without authentication and returns detailed internal node/network state.

### Title
Unauthenticated `/wallet/getnodeinfo` API discloses internal peer, network, and machine information - ([File: framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java])

### Summary
`GetNodeInfoServlet.doGet` calls `NodeInfoService.getNodeInfo()` and dumps the full `NodeInfo` object as JSON to any caller with no authentication or permission check, similar in class to the GitLab CVE-2019-9223 information-exposure issue where an endpoint returned internal data to unauthorized callers.

### Finding Description
`GetNodeInfoServlet` is registered on `/wallet/getnodeinfo`, `/walletsolidity/getnodeinfo`, and `/monitor/getnodeinfo` in `FullNodeHttpApiService`/`SolidityNodeHttpApiService` with no per-caller authorization beyond the generic rate limiter. [1](#0-0) 
The servlet simply serializes whatever `NodeInfoService.getNodeInfo()` returns: [2](#0-1) 
That object aggregates connected peer IPs/ports, node IDs, disconnect reasons, sync state, and full JVM/machine info (thread counts, memory pools, deadlocked-thread stack traces) for every connected peer: [3](#0-2) [4](#0-3) 
This includes `deadLockThreadInfo.setStackTrace(...)` for any deadlocked JVM thread, i.e. raw stack traces of the node process, and full config (listen ports, backup members, backup priority) via `setConfigNodeInfo`: [5](#0-4) 
The route is registered without a scoped authorization filter (only the generic `httpApiAccessFilter`, which is a whitelist/blacklist IP filter, not per-account auth): [6](#0-5) 

### Impact Explanation
An anonymous client can enumerate all connected peer IP addresses/ports, node IDs, sync/backup topology, and JVM internals (including thread stack traces when deadlocked) of any java-tron full node, solidity node, or PBFT node exposing the HTTP API. This is reconnaissance-grade information disclosure that assists targeted network attacks (e.g., identifying SR/backup nodes, peer topology mapping, or picking DoS targets) but does not by itself grant fund theft, RCE, or consensus manipulation.

### Likelihood Explanation
High likelihood of reachability: the endpoint requires no signed transaction, no special permission, and is reachable by any HTTP client that is not blocked by the IP-based `httpApiAccessFilter` (which many public nodes leave open for query endpoints). No parameters or private information are needed from the caller.

### Recommendation
Restrict `/wallet/getnodeinfo`, `/walletsolidity/getnodeinfo`, and `/monitor/getnodeinfo` to trusted/internal callers by default (e.g., localhost-only or explicit allow-list), or strip peer IP/node-ID/stack-trace fields from the public response and only expose non-sensitive fields (version, block height) to unauthenticated callers.

### Proof of Concept
1. Start a java-tron full node with the HTTP API enabled and default `httpApiAccessFilter` configuration.
2. From any remote host, issue `curl http://<node-ip>:8090/wallet/getnodeinfo`.
3. Observe the JSON response contains `peerList` with peer IPs/ports/nodeId, `configNodeInfo` (backup members, ports), and `machineInfo` (thread counts, memory, and any deadlocked-thread stack traces) — all without any authentication. [7](#0-6) [8](#0-7)

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/GetNodeInfoServlet.java (L1-41)
```java
package org.tron.core.services.http;

import java.io.IOException;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.tron.common.entity.NodeInfo;
import org.tron.core.services.NodeInfoService;
import org.tron.json.JSON;


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

  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    doGet(request, response);
  }
}


```

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L1-69)
```java
package org.tron.core.services;

import com.sun.management.OperatingSystemMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.RuntimeMXBean;
import java.lang.management.ThreadInfo;
import java.lang.management.ThreadMXBean;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map.Entry;

import org.apache.commons.codec.binary.Hex;
import org.apache.commons.collections4.CollectionUtils;
import org.apache.commons.lang3.ArrayUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.tron.common.entity.NodeInfo;
import org.tron.common.entity.NodeInfo.ConfigNodeInfo;
import org.tron.common.entity.NodeInfo.MachineInfo;
import org.tron.common.entity.NodeInfo.MachineInfo.DeadLockThreadInfo;
import org.tron.common.entity.NodeInfo.MachineInfo.MemoryDescInfo;
import org.tron.common.entity.PeerInfo;
import org.tron.common.parameter.CommonParameter;
import org.tron.common.prometheus.MetricTime;
import org.tron.core.ChainBaseManager;
import org.tron.core.db.Manager;
import org.tron.core.net.TronNetService;
import org.tron.core.net.peer.PeerConnection;
import org.tron.core.net.peer.PeerManager;
import org.tron.core.net.service.statistics.NodeStatistics;
import org.tron.core.services.WitnessProductBlockService.CheatWitnessInfo;
import org.tron.p2p.P2pConfig;
import org.tron.p2p.P2pService;
import org.tron.p2p.connection.Channel;
import org.tron.program.Version;
import org.tron.protos.Protocol.ReasonCode;

@Component
public class NodeInfoService {

  private MemoryMXBean memoryMXBean = ManagementFactory.getMemoryMXBean();
  private RuntimeMXBean runtimeMXBean = ManagementFactory.getRuntimeMXBean();
  private ThreadMXBean threadMXBean = ManagementFactory.getThreadMXBean();
  private OperatingSystemMXBean operatingSystemMXBean = (OperatingSystemMXBean) ManagementFactory
      .getOperatingSystemMXBean();
  private CommonParameter parameter = CommonParameter.getInstance();

  @Autowired
  private Manager dbManager;

  @Autowired
  private ChainBaseManager chainBaseManager;

  @Autowired
  private WitnessProductBlockService witnessProductBlockService;

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

**File:** framework/src/main/java/org/tron/core/services/NodeInfoService.java (L105-122)
```java
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
