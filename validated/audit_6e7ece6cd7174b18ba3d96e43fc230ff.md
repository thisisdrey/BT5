### Title
Unmetered, computationally expensive `triggerconstantcontract` / `estimateenergy` requests allow cheap denial of service — ([File: framework/src/main/java/org/tron/core/Wallet.java])

### Summary
The Oracle-request bug class in the report describes users submitting arbitrarily many resource-consuming requests without paying any fee/penalty, only bounded by a small, easily bypassable rate limit. java-tron has a directly analogous unprivileged, unauthenticated surface: the HTTP `/wallet/triggerconstantcontract`, `/wallet/estimateenergy`, the equivalent gRPC methods, and JSON-RPC `eth_call`/`eth_estimateGas` all invoke full TVM execution via `Wallet.callConstantContract()`/`Wallet.estimateEnergy()` without ever broadcasting a transaction, without any signature check, and without deducting any energy/bandwidth fee from an account — yet they consume real CPU time and, in the `estimateEnergy` case, run the VM repeatedly (binary search) for a single client request.

### Finding Description
`Wallet.triggerConstantContract()` builds a `TransactionCapsule` and calls `callConstantContract()`, which runs `VMActuator.validate()`/`execute()` directly against `StoreFactory` state, with no requirement that the transaction be signed or broadcast: [1](#0-0) 

`Wallet.estimateEnergy()` performs a full binary search over the fee/energy space, invoking `cleanContextAndTriggerConstantContract()` — i.e. a fresh VM execution — up to `estimateEnergyMaxRetry` times per binary-search step, and the binary search itself runs `O(log(maxFeeLimit))` VM executions for a single incoming API call: [2](#0-1) 

Each of these constant calls is dispatched to `VMActuator`, which grants the call up to `maxEnergyLimit` energy and up to `constantCallTimeoutMs` of CPU time (configured independently of any economic cost), regardless of whether the caller ever pays anything: [3](#0-2) [4](#0-3) 

This is reachable via HTTP (`TriggerConstantContractServlet`, `EstimateEnergyServlet`), gRPC (`RpcApiService.triggerConstantContract`/`estimateEnergy`), and JSON-RPC (`TronJsonRpcImpl.estimateEnergy`): [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

The only protection is a generic, endpoint-agnostic rate limiter (`RateLimiterServlet`) that defaults to `qps=1000` per endpoint globally, plus a global per-IP QPS cap of 10000, when no per-servlet override is configured: [9](#0-8) [10](#0-9) 

This mirrors exactly the reported bug class: a query-type endpoint that performs real, costly work (Oracle job / TVM execution) is gated only by a coarse rate limit (fixed QPS / fixed 5s timeout) with **no financial cost to the caller**, and that rate limit is per-IP and therefore trivially bypassed by distributing requests across many source IPs — precisely the bypass called out in the original report ("this short rate limit can be easily bypassed by sending requests from several IPs simultaneously").

### Impact Explanation
An anonymous API client can send unauthenticated `triggerconstantcontract`/`estimateenergy` requests (no signature, no fee, no on-chain footprint) that each spend up to the constant-call CPU-time budget executing arbitrary deployed-contract bytecode with `maxEnergyLimit` energy, and for `estimateEnergy` this cost multiplies via the internal binary search and retry loop. By distributing requests across many source IPs (defeating the per-IP QPS limiter) an attacker can drive sustained high CPU/thread-pool consumption on a full node's RPC worker pool, degrading or denying service to legitimate constant-call/estimate/eth_call/eth_estimateGas users and potentially starving other RPC/HTTP handling on the same node. This is a resource-exhaustion / availability issue against a node-level API surface, not a fund-theft or consensus issue.

### Likelihood Explanation
Likelihood is high in the sense that no privilege, signature, or payment is required — any anonymous client can call these endpoints, and the only mitigation (QPS-based rate limiting) is explicitly documented as being per-IP/per-endpoint and configurable, with a fairly high default (1000 qps/endpoint) that does not account for the actual computational cost of the request (deploy-and-run arbitrary bytecode, or repeated binary-search VM runs for estimateEnergy). Exploitation only requires the ability to send many concurrent HTTP/gRPC/JSON-RPC requests from multiple IPs, which is inexpensive for an attacker with modest infrastructure.

### Recommendation
- Add cost-aware limiting for constant-call/estimateEnergy endpoints (e.g., limit maximum energy/CPU time budget for anonymous constant calls, or require a smaller default `maxEnergyLimit`/timeout for these specific unauthenticated code paths).
- Configure stricter default per-endpoint and per-IP rate limits specifically for `TriggerConstantContractServlet`, `EstimateEnergyServlet`, and the equivalent gRPC/JSON-RPC methods, rather than relying on the generic global default.
- Cap or make configurable the number of VM invocations per `estimateEnergy` call (binary-search depth × retry count) so a single request cannot trigger disproportionately many VM executions.
- Consider a global concurrency cap (e.g., `GlobalPreemptibleAdapter`) on constant-call/estimate endpoints to bound total concurrent VM executions regardless of per-IP distribution.

### Proof of Concept
1. Deploy (or reuse) a contract with an expensive constant/view function.
2. From many distinct source IPs, concurrently POST to `/wallet/triggerconstantcontract` or `/wallet/estimateenergy` (or the gRPC/JSON-RPC equivalents) targeting that contract's expensive function, staying under the default per-IP QPS (10000) and per-endpoint QPS (1000) limits on each individual IP.
3. Observe that the node's RPC/HTTP worker threads are consumed executing full TVM runs (with `estimateEnergy` multiplying cost via its binary search/retry loop) for zero cost to the attacker, degrading responsiveness for legitimate constant-call/query clients — analogous to the "excessive Oracle requests" DoS described in the source report.

### Citations

**File:** framework/src/main/java/org/tron/core/Wallet.java (L2986-3049)
```java
  public Transaction estimateEnergy(TriggerSmartContract triggerSmartContract,
      TransactionCapsule txCap, TransactionExtention.Builder txExtBuilder,
      Return.Builder txRetBuilder, GrpcAPI.EstimateEnergyMessage.Builder estimateBuilder)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    if (!Args.getInstance().estimateEnergy) {
      throw new ContractValidateException("this node does not support estimate energy");
    }

    if (!Args.getInstance().supportConstant) {
      throw new ContractValidateException("this node does not support constant, "
          + "so estimate energy cannot work");
    }
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
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3169)
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

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L359-422)
```java
    // insure the new contract address haven't exist
    if (rootRepository.getAccount(contractAddress) != null) {
      throw new ContractValidateException(
          "Trying to create a contract with existing contract address: " + StringUtil
              .encode58Check(contractAddress));
    }

    newSmartContract = newSmartContract.toBuilder()
        .setContractAddress(ByteString.copyFrom(contractAddress)).build();
    long callValue = newSmartContract.getCallValue();
    long tokenValue = 0;
    long tokenId = 0;
    if (VMConfig.allowTvmTransferTrc10()) {
      tokenValue = contract.getCallTokenValue();
      tokenId = contract.getTokenId();
    }
    byte[] callerAddress = contract.getOwnerAddress().toByteArray();
    // create vm to constructor smart contract
    try {
      long feeLimit = trx.getRawData().getFeeLimit();
      if (feeLimit < 0 || feeLimit > rootRepository.getDynamicPropertiesStore().getMaxFeeLimit()) {
        logger.info("invalid feeLimit {}", feeLimit);
        throw new ContractValidateException("feeLimit must be >= 0 and <= "
            + rootRepository.getDynamicPropertiesStore().getMaxFeeLimit());
      }
      AccountCapsule creator = rootRepository
          .getAccount(newSmartContract.getOriginAddress().toByteArray());

      long energyLimit;
      // according to version

      if (isConstantCall) {
        energyLimit = maxEnergyLimit;
      } else {
        if (StorageUtils.getEnergyLimitHardFork()) {
          if (callValue < 0) {
            throw new ContractValidateException("callValue must be >= 0");
          }
          if (tokenValue < 0) {
            throw new ContractValidateException("tokenValue must be >= 0");
          }
          if (newSmartContract.getOriginEnergyLimit() <= 0) {
            throw new ContractValidateException("The originEnergyLimit must be > 0");
          }
          energyLimit = getAccountEnergyLimitWithFixRatio(creator, feeLimit, callValue);
        } else {
          energyLimit = getAccountEnergyLimitWithFloatRatio(creator, feeLimit, callValue);
        }
      }

      checkTokenValueAndId(tokenValue, tokenId);

      byte[] ops = newSmartContract.getBytecode().toByteArray();
      rootInternalTx = new InternalTransaction(trx, trxType);

      long thisTxCPULimitInUs = calculateCpuLimitInUs(isConstantCall,
          rootRepository.getDynamicPropertiesStore().getMaxCpuTimeOfOneTx(),
          getCpuLimitInUsRatio(), CommonParameter.getInstance().getConstantCallTimeoutMs());
      long vmStartInUs = System.nanoTime() / VMConstant.ONE_THOUSAND;
      long vmShouldEndInUs = vmStartInUs + thisTxCPULimitInUs;
      ProgramInvoke programInvoke = ProgramInvokeFactory
          .createProgramInvoke(TrxType.TRX_CONTRACT_CREATION_TYPE, executorType, trx,
              tokenValue, tokenId, blockCap.getInstance(), rootRepository, vmStartInUs,
              vmShouldEndInUs, energyLimit);
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L514-552)
```java
    if (StorageUtils.getEnergyLimitHardFork()) {
      if (callValue < 0) {
        throw new ContractValidateException("callValue must be >= 0");
      }
      if (tokenValue < 0) {
        throw new ContractValidateException("tokenValue must be >= 0");
      }
    }

    byte[] callerAddress = contract.getOwnerAddress().toByteArray();
    checkTokenValueAndId(tokenValue, tokenId);

    byte[] code = rootRepository.getCode(contractAddress);
    if (isNotEmpty(code)) {
      long feeLimit = trx.getRawData().getFeeLimit();
      if (feeLimit < 0 || feeLimit > rootRepository.getDynamicPropertiesStore().getMaxFeeLimit()) {
        logger.info("invalid feeLimit {}", feeLimit);
        throw new ContractValidateException("feeLimit must be >= 0 and <= "
            + rootRepository.getDynamicPropertiesStore().getMaxFeeLimit());
      }
      AccountCapsule caller = rootRepository.getAccount(callerAddress);
      long energyLimit;
      if (isConstantCall) {
        energyLimit = maxEnergyLimit;
      } else {
        AccountCapsule creator = rootRepository
            .getAccount(deployedContract.getInstance().getOriginAddress().toByteArray());
        energyLimit = getTotalEnergyLimit(creator, caller, contract, feeLimit, callValue);
      }

      long thisTxCPULimitInUs = calculateCpuLimitInUs(isConstantCall,
          rootRepository.getDynamicPropertiesStore().getMaxCpuTimeOfOneTx(),
          getCpuLimitInUsRatio(), CommonParameter.getInstance().getConstantCallTimeoutMs());
      long vmStartInUs = System.nanoTime() / VMConstant.ONE_THOUSAND;
      long vmShouldEndInUs = vmStartInUs + thisTxCPULimitInUs;
      ProgramInvoke programInvoke = ProgramInvokeFactory
          .createProgramInvoke(TrxType.TRX_CONTRACT_CALL_TYPE, executorType, trx,
              tokenValue, tokenId, blockCap.getInstance(), rootRepository, vmStartInUs,
              vmShouldEndInUs, energyLimit);
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerConstantContractServlet.java (L35-66)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    TriggerSmartContract.Builder build = TriggerSmartContract.newBuilder();
    TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
    Return.Builder retBuilder = Return.newBuilder();
    boolean visible = false;
    try {
      String contract = request.getReader().lines()
          .collect(Collectors.joining(System.lineSeparator()));
      Util.checkBodySize(contract);
      visible = Util.getVisiblePost(contract);
      Util.validateParameter(contract);
      JsonFormat.merge(contract, build, visible);
      JSONObject jsonObject = JSONObject.parseObject(contract);

      boolean isFunctionSelectorSet =
          !StringUtil.isNullOrEmpty(jsonObject.getString(Util.FUNCTION_SELECTOR));
      if (isFunctionSelectorSet) {
        String selector = jsonObject.getString(Util.FUNCTION_SELECTOR);
        String parameter = jsonObject.getString(Util.FUNCTION_PARAMETER);
        String data = Util.parseMethod(selector, parameter);
        build.setData(ByteString.copyFrom(ByteArray.fromHexString(data)));
      }

      TransactionCapsule trxCap = wallet
          .createTransactionCapsule(build.build(), ContractType.TriggerSmartContract);

      Transaction trx = wallet
          .triggerConstantContract(build.build(),trxCap,
              trxExtBuilder,
              retBuilder);
      trx = Util.setTransactionPermissionId(jsonObject, trx);
```

**File:** framework/src/main/java/org/tron/core/services/http/EstimateEnergyServlet.java (L33-62)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response)
      throws IOException {
    TriggerSmartContract.Builder build = TriggerSmartContract.newBuilder();
    TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
    EstimateEnergyMessage.Builder estimateEnergyBuilder = EstimateEnergyMessage.newBuilder();
    Return.Builder retBuilder = Return.newBuilder();
    boolean visible = false;
    try {
      String contract = request.getReader().lines()
          .collect(Collectors.joining(System.lineSeparator()));
      Util.checkBodySize(contract);
      visible = Util.getVisiblePost(contract);
      Util.validateParameter(contract);
      JsonFormat.merge(contract, build, visible);
      JSONObject jsonObject = JSONObject.parseObject(contract);

      boolean isFunctionSelectorSet =
          !StringUtil.isNullOrEmpty(jsonObject.getString(Util.FUNCTION_SELECTOR));
      if (isFunctionSelectorSet) {
        String selector = jsonObject.getString(Util.FUNCTION_SELECTOR);
        String parameter = jsonObject.getString(Util.FUNCTION_PARAMETER);
        String data = Util.parseMethod(selector, parameter);
        build.setData(ByteString.copyFrom(ByteArray.fromHexString(data)));
      }

      TransactionCapsule trxCap = wallet.createTransactionCapsule(build.build(),
          Protocol.Transaction.Contract.ContractType.TriggerSmartContract);

      wallet.estimateEnergy(build.build(), trxCap,
          trxExtBuilder, retBuilder, estimateEnergyBuilder);
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L858-895)
```java
    @Override
    public void triggerConstantContract(TriggerSmartContract request,
        StreamObserver<TransactionExtention> responseObserver) {

      callContract(request, responseObserver, true);
    }

    @Override
    public void estimateEnergy(TriggerSmartContract request,
        StreamObserver<EstimateEnergyMessage> responseObserver) {
      TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
      Return.Builder retBuilder = Return.newBuilder();
      EstimateEnergyMessage.Builder estimateBuilder
          = EstimateEnergyMessage.newBuilder();

      try {
        TransactionCapsule trxCap = createTransactionCapsule(request,
            ContractType.TriggerSmartContract);
        wallet.estimateEnergy(request, trxCap, trxExtBuilder, retBuilder, estimateBuilder);
      } catch (ContractValidateException | VMIllegalException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_VALIDATE_ERROR)
            .setMessage(ByteString.copyFromUtf8(Wallet
                .CONTRACT_VALIDATE_ERROR + e.getMessage()));
        logger.warn(CONTRACT_VALIDATE_EXCEPTION, e.getMessage());
      } catch (RuntimeException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_EXE_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        logger.warn("When run estimate energy in VM, have Runtime Exception: " + e.getMessage());
      } catch (Exception e) {
        retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        logger.warn(UNKNOWN_EXCEPTION_CAUGHT + e.getMessage(), e);
      } finally {
        estimateBuilder.setResult(retBuilder);
        responseObserver.onNext(estimateBuilder.build());
        responseObserver.onCompleted();
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/TronJsonRpcImpl.java (L491-514)
```java
  private void estimateEnergy(byte[] ownerAddressByte, byte[] contractAddressByte,
      long value, byte[] data, TransactionExtention.Builder trxExtBuilder,
      Return.Builder retBuilder, EstimateEnergyMessage.Builder estimateBuilder)
      throws ContractValidateException, ContractExeException, HeaderNotFound, VMIllegalException {

    TriggerSmartContract triggerContract = triggerCallContract(
        ownerAddressByte,
        contractAddressByte,
        value,
        data,
        0,
        null
    );

    TransactionCapsule trxCap = wallet.createTransactionCapsule(triggerContract,
        ContractType.TriggerSmartContract);
    Transaction trx =
        wallet.estimateEnergy(triggerContract, trxCap, trxExtBuilder, retBuilder, estimateBuilder);
    trxExtBuilder.setTransaction(trx);
    trxExtBuilder.setTxid(trxCap.getTransactionId().getByteString());
    trxExtBuilder.setResult(retBuilder);
    retBuilder.setResult(true).setCode(response_code.SUCCESS);
    estimateBuilder.setResult(retBuilder);
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/RateLimiterServlet.java (L56-94)
```java
  @Autowired
  private RateLimiterContainer container;

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

  static IRateLimiter buildAdapter(String cName, String params, String name) {
    Class<? extends IRateLimiter> c = ALLOWED_ADAPTERS.get(cName);
    if (c == null) {
      throw rateLimiterInitError(cName, params, name,
          new IllegalArgumentException("unknown rate limiter adapter; allowed="
              + ALLOWED_ADAPTERS.keySet()));
    }
    try {
      return c.getConstructor(String.class).newInstance(params);
    } catch (Exception e) {
      throw rateLimiterInitError(cName, params, name, e);
    }
  }
```

**File:** common/src/main/resources/reference.conf (L452-519)
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

  # P2P message rate limits.
  p2p = {
    # QPS ceiling for individual P2P message types received from peers.
    # Values are doubles; fractional QPS is allowed (e.g. 0.5 = one per 2 s).
    syncBlockChain = 3.0  # SyncBlockChain handshake messages
    fetchInvData = 3.0    # FetchInvData (block/tx fetch) messages
    disconnect = 1.0      # Disconnect messages
  }

  # Node-wide QPS ceiling across all HTTP + gRPC requests combined.
  global.qps = 50000
  # Per-source-IP QPS ceiling across all HTTP + gRPC requests from that IP.
  global.ip.qps = 10000
  # Default per-endpoint QPS limit applied to any endpoint with no explicit strategy.
  global.api.qps = 1000
  # true = reject over-limit requests immediately; false = queue and block the caller.
  apiNonBlocking = false
```
