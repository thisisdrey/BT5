### Title
Unbounded concurrent TVM constant-call/estimateEnergy execution allows memory and CPU resource-exhaustion DoS - (File: framework/src/main/java/org/tron/core/Wallet.java)

### Summary
The Mattermost analog describes unauthenticated resource exhaustion caused by the server failing to bound in-memory resource consumption for concurrently processed, attacker-triggerable operations. The structural analog in java-tron is the `/wallet/triggerconstantcontract` and `/wallet/estimateenergy` HTTP/gRPC endpoints: each request spins up a full TVM execution that can allocate up to the per-call memory ceiling and consume up to the per-call CPU time ceiling, with **no default concurrency cap** on these endpoints and no global memory/CPU accounting across concurrent calls.

### Finding Description
`triggerConstantContract`/`estimateEnergy` are reachable by any anonymous API client (no signature or fee required, since constant calls bypass real fee charging) via `TriggerConstantContractServlet`, `EstimateEnergyServlet`, and the gRPC `Wallet`/`WalletSolidity` services: [1](#0-0) [2](#0-1) 

Each call goes through `VMActuator` with `isConstantCall=true`, allowing energy up to `maxEnergyLimitForConstant` (default 100,000,000, clamped to a minimum of 3,000,000) and TVM memory up to a fixed 3 MB ceiling per program: [3](#0-2) [4](#0-3) 

`estimateEnergy` amplifies this further: it performs a binary search that re-executes `triggerConstantContract` (i.e., a full VM run, each up to the same CPU/memory ceiling) up to `estimateEnergyMaxRetry` retries plus `O(log(maxFeeLimit))` iterations per call: [5](#0-4) 

The only throttling in front of these endpoints is the generic HTTP/gRPC rate limiter, which by default applies a plain QPS limiter (`qps=1000` global API-wide, `qps=10000` per-IP) — not a concurrency (in-flight request) cap — unless an operator explicitly configures a `GlobalPreemptibleAdapter` for these specific servlets/methods, which is commented out by default: [6](#0-5) [7](#0-6) 

A QPS limiter bounds the *rate of admission* but not the number of requests executing *simultaneously*; long-running, memory-heavy constant calls admitted within the QPS budget can still stack up concurrently on the Jetty/gRPC worker thread pool, each independently allocating its own TVM `Memory` (up to 3 MB) and running for up to the configured constant-call CPU deadline. This mirrors the Mattermost pattern of "insufficiently limit the in-memory sizes of concurrently processed [attacker-supplied] payloads."

### Impact Explanation
An attacker who submits many concurrent `triggerconstantcontract`/`estimateenergy` requests (each invoking a heavy, memory/CPU-maximizing contract, e.g. one that fills TVM memory near the 3 MB cap and loops to the CPU-time limit) can drive up aggregate heap and CPU usage on the node's API-serving JVM, since there is no default global concurrency limit tied to actual resource cost — only a flat QPS admission counter. This can degrade or crash the node's API service (and potentially the whole process under heap pressure), denying service to legitimate wallets/dApps that depend on it. This does not affect consensus, funds, or keys directly, so it is scoped to node/API availability — consistent with the "Medium" severity of the underlying report.

### Likelihood Explanation
Both `triggerconstantcontract` and `estimateenergy` are explicitly public-facing, unauthenticated, feeless read-only endpoints intended for wallets/dApps, so they are trivially reachable by any client with network access to a full node's HTTP/gRPC API (assuming the node has `vm.supportConstant`/`vm.estimateEnergy` enabled, which is common for public API nodes). No special privilege or malicious server role is required. The main mitigating factor is that many production deployments front these APIs with a reverse proxy or explicit `rate.limiter.http`/`rate.limiter.rpc` concurrency configuration, and per-call CPU/memory is capped, so a single request is not by itself catastrophic — the risk materializes only under concurrent abuse, which the default configuration does not prevent.

### Recommendation
Add a default `GlobalPreemptibleAdapter` (concurrency permit) rate limit for `TriggerConstantContractServlet`, `EstimateEnergyServlet`, and their gRPC counterparts, sized to the node's available memory/CPU budget (e.g., a bounded pool of concurrent constant-call executions), rather than relying solely on QPS-style admission control. Additionally, consider tracking aggregate in-flight TVM memory/CPU budget across all concurrent constant calls and rejecting new calls once a global ceiling is reached, similar to how `maxEnergyLimitForConstant` bounds a single call.

### Proof of Concept
1. Enable `vm.supportConstant = true` and `vm.estimateEnergy = true` on a full node exposing the default HTTP API, with no custom `rate.limiter.http` entries for `TriggerConstantContractServlet`/`EstimateEnergyServlet` (default reference.conf).
2. Deploy (or reuse an existing) contract whose view/pure function loops and expands memory close to the per-call cap (e.g., repeated `MSTORE` into large offsets, exercised as a "view" function so it routes through `callConstantContract`).
3. Concurrently fire hundreds of parallel POST requests to `/wallet/triggerconstantcontract` (or `/wallet/estimateenergy`, which multiplies the work via its binary-search re-execution loop) invoking that function, staying under the default 1000 QPS admission threshold but exceeding the server's true concurrent-execution capacity.
4. Observe JVM heap and CPU usage on the node climb from the many concurrently-executing TVM instances, each holding up to ~3 MB of `Memory` and consuming CPU up to the constant-call deadline, degrading or crashing the node's API-serving process while block production/consensus paths may also be starved of resources on the same host.

Note: I could not directly execute or benchmark this PoC (no runtime access), so the actual point at which resource exhaustion becomes impactful (thread pool size, JVM heap size, `constantCallTimeoutMs`/`maxCpuTimeOfOneTx` defaults) would need to be empirically validated in a running deployment to confirm real-world severity.

### Citations

**File:** framework/src/main/java/org/tron/core/services/http/TriggerConstantContractServlet.java (L59-66)
```java
      TransactionCapsule trxCap = wallet
          .createTransactionCapsule(build.build(), ContractType.TriggerSmartContract);

      Transaction trx = wallet
          .triggerConstantContract(build.build(),trxCap,
              trxExtBuilder,
              retBuilder);
      trx = Util.setTransactionPermissionId(jsonObject, trx);
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2999-3072)
```java
    int retry = Args.getInstance().estimateEnergyMaxRetry;

    DynamicPropertiesStore dps = chainBaseManager.getDynamicPropertiesStore();
    long high = dps.getMaxFeeLimit();

    Transaction transaction;

    while (true) {
      try {
        transaction = cleanContextAndTriggerConstantContract(
            triggerSmartContract, txCap, txExtBuilder, txRetBuilder, high);
        break;
      } catch (Program.OutOfTimeException e) {
        retry--;
        if (retry < 0) {
          throw e;
        }
      }
    }

    // If failed, return directly.
    if (transaction.getRet(0).getRet().equals(code.FAILED)) {
      txRetBuilder.setCode(response_code.CONTRACT_EXE_ERROR);
      estimateBuilder.setResult(txRetBuilder);
      return transaction;
    }

    long low = dps.getEnergyFee() * txExtBuilder.getEnergyUsed();

    long twoTimes = low * 2;
    if (twoTimes < high) {
      while (true) {
        try {
          transaction = cleanContextAndTriggerConstantContract(
              triggerSmartContract, txCap, txExtBuilder, txRetBuilder, twoTimes);

          if (transaction.getRet(0).getRet().equals(code.FAILED)) {
            low = twoTimes;
          } else {
            high = twoTimes;
          }

          break;
        } catch (Program.OutOfTimeException e) {
          retry--;
          if (retry < 0) {
            throw e;
          }
        }
      }
    }

    while (low + TRX_PRECISION < high) {
      long mid = (low + high) / 2;

      while (true) {
        try {
          transaction = cleanContextAndTriggerConstantContract(
              triggerSmartContract, txCap, txExtBuilder, txRetBuilder, mid);
          break;
        } catch (Program.OutOfTimeException e) {
          retry--;
          if (retry < 0) {
            throw e;
          }
        }
      }

      if (transaction.getRet(0).getRet().equals(code.FAILED)) {
        low = mid;
      } else {
        high = mid;
      }
    }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3168)
```java
  public Transaction callConstantContract(TransactionCapsule trxCap,
      Builder builder, Return.Builder retBuilder, boolean isEstimating)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    if (!Args.getInstance().isSupportConstant()) {
      throw new ContractValidateException("this node does not support constant");
    }

    Block headBlock;
    List<BlockCapsule> blockCapsuleList = chainBaseManager.getBlockStore()
        .getBlockByLatestNum(1);
    if (CollectionUtils.isEmpty(blockCapsuleList)) {
      throw new HeaderNotFound("latest block not found");
    } else {
      headBlock = blockCapsuleList.get(0).getInstance();
    }

    BlockCapsule headBlockCapsule = new BlockCapsule(headBlock);
    TransactionContext context = new TransactionContext(headBlockCapsule, trxCap,
        StoreFactory.getInstance(), true, false);
    VMActuator vmActuator = new VMActuator(true);

    try {
      vmActuator.validate(context);
      vmActuator.execute(context);
    } finally {
      // constant call runs on a pooled RPC worker; drop its thread-local VM config view so it
      // can never leak into a later (block/broadcast) execution on the same thread.
      VMConfig.clearLocalSnapshot();
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L102-134)
```java
  public VMActuator(boolean isConstantCall) {
    this.isConstantCall = isConstantCall;
    this.maxEnergyLimit = CommonParameter.getInstance().maxEnergyLimitForConstant;
  }

  private static long getEnergyFee(long callerEnergyUsage, long callerEnergyFrozen,
      long callerEnergyTotal) {
    if (callerEnergyTotal <= 0) {
      return 0;
    }
    return BigInteger.valueOf(callerEnergyFrozen).multiply(BigInteger.valueOf(callerEnergyUsage))
        .divide(BigInteger.valueOf(callerEnergyTotal)).longValueExact();
  }

  @Override
  public void validate(Object object) throws ContractValidateException {

    TransactionContext context = (TransactionContext) object;
    if (Objects.isNull(context)) {
      throw new RuntimeException("TransactionContext is null");
    }

    // Load Config
    ConfigLoader.load(context.getStoreFactory(), isConstantCall);
    // Warm up registry class
    OperationRegistry.init();
    trx = context.getTrxCap().getInstance();
    // If tx`s fee limit is set, use it to calc max energy limit for constant call
    if (isConstantCall && trx.getRawData().getFeeLimit() > 0) {
      maxEnergyLimit = min(maxEnergyLimit, trx.getRawData().getFeeLimit()
          / context.getStoreFactory().getChainBaseManager()
          .getDynamicPropertiesStore().getEnergyFee(), VMConfig.disableJavaLangMath());
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/EnergyCost.java (L25-27)
```java
  // 3MB
  private static final BigInteger MEM_LIMIT = BigInteger.valueOf(3L * 1024 * 1024);
  private static final long MEMORY = 3;
```

**File:** common/src/main/resources/reference.conf (L452-501)
```text
## Rate limiter config
rate.limiter = {
  # Each HTTP servlet and gRPC method can have its own rate-limit strategy.
  # Three API rate-limit strategies are available:
  #   GlobalPreemptibleAdapter – limits maximum concurrent requests globally.
  #                              paramString = "permit=N" (N = max concurrent calls)
  #   QpsRateLimiterAdapter    – limits average QPS across all callers.
  #                              paramString = "qps=N" (N may be a decimal)
  #   IPQPSRateLimiterAdapter  – limits average QPS per source IP.
  #                              paramString = "qps=N" (N may be a decimal)
  # If no strategy is configured for an endpoint, QpsRateLimiterAdapter with
  # qps=1000 is applied automatically.

  # Per-servlet HTTP rate limits. component is the servlet class simple name.
  http = [
    # {
    #   component = "GetNowBlockServlet",
    #   strategy = "GlobalPreemptibleAdapter",
    #   paramString = "permit=1"
    # },
    # {
    #   component = "GetAccountServlet",
    #   strategy = "IPQPSRateLimiterAdapter",
    #   paramString = "qps=1"
    # },
    # {
    #   component = "ListWitnessesServlet",
    #   strategy = "QpsRateLimiterAdapter",
    #   paramString = "qps=1"
    # }
  ]

  # Per-method gRPC rate limits. component is "package.ServiceName/MethodName".
  rpc = [
    # {
    #   component = "protocol.Wallet/GetBlockByLatestNum2",
    #   strategy = "GlobalPreemptibleAdapter",
    #   paramString = "permit=1"
    # },
    # {
    #   component = "protocol.Wallet/GetAccount",
    #   strategy = "IPQPSRateLimiterAdapter",
    #   paramString = "qps=1"
    # },
    # {
    #   component = "protocol.Wallet/ListWitnesses",
    #   strategy = "QpsRateLimiterAdapter",
    #   paramString = "qps=1"
    # }
  ]
```

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L59-80)
```java
  @PostConstruct
  private void addRateContainer() {
    final String name = getClass().getSimpleName();
    RateLimiterInitialization.HttpRateLimiterItem item = Args.getInstance()
        .getRateLimiterInitialization().getHttpMap().get(name);

    String cName;
    String params;
    if (item == null) {
      cName = DEFAULT_ADAPTER_NAME;
      params = QpsStrategy.DEFAULT_QPS_PARAM;
    } else {
      cName = item.getStrategy();
      params = item.getParams();
    }

    try {
      container.add(KEY_PREFIX_HTTP, name, buildAdapter(cName, params, name));
    } catch (Exception e) {
      throw rateLimiterInitError(cName, params, name, e);
    }
  }
```
