## Finding [1](#0-0) 

`Program` maintains a single **static**, JVM-wide cache — `programPrecompileLRUMap`, an Apache Commons `LRUMap<Key, ProgramPrecompile>` — shared by every TVM execution in the node [1](#0-0) . It is read/written from `getProgramPrecompile()` on every JUMP/JUMPI, without any external synchronization: `programPrecompileLRUMap.containsKey(key)` / `.get(key)` / `.put(key, programPrecompile)` [2](#0-1) .

`LRUMap` from Apache Commons Collections is explicitly documented as not thread-safe — its internal doubly-linked ordering structure (used to track/evict the least-recently-used entry) is mutated on every `get`/`put`. Concurrent, unsynchronized mutation of that structure from multiple threads can corrupt the linked list, which can manifest as an infinite loop during eviction, a `NullPointerException`/`ConcurrentModificationException`, or entries silently disappearing/being duplicated.

### Reachability
Every TVM execution constructs a fresh `Program` but shares this static map. Contract execution is reachable in parallel from unauthenticated callers via:
- HTTP `POST /wallet/triggerconstantcontract` → `TriggerConstantContractServlet.doPost` → `Wallet.triggerConstantContract` → `callConstantContract` → `VMActuator.execute` [3](#0-2) [4](#0-3) 
- gRPC `triggerConstantContract` / `triggerContract` / `estimateEnergy` in `RpcApiService` [5](#0-4) 
- The PBFT/Solidity read-only servlet variants which explicitly dispatch onto a shared worker pool (`walletOnPBFT.futureGet(...)`) [6](#0-5) 

Each HTTP/gRPC request is served on its own worker thread (Jetty/gRPC thread pool), so an anonymous client can trivially trigger many concurrent constant-call (or even normal broadcast) contract executions that all hit `getProgramPrecompile()` at the same time, hammering the same unsynchronized static `LRUMap`.

This is directly analogous to the Spring Cloud Function CVE-2022-22979 report: an unprivileged caller drives a shared, framework-managed lookup/cache path (there: the function catalog; here: the JUMPDEST/precompile analysis LRU cache) into an unsafe concurrent-access state that produces a denial of service.

### Impact
Corruption of the shared `LRUMap`'s internal linked list can cause the eviction path (`removeLRU`) to loop indefinitely or throw unexpected runtime exceptions on the affected worker thread. Because the map is process-wide and used on the hot JUMP/JUMPI path of every contract execution (both constant-call API requests and consensus-critical block application via `VMActuator`), sustained corruption can degrade or hang TVM execution service-wide, not just for the calling thread — a genuine denial-of-service condition reachable by any anonymous API client or contract caller, with no special privileges required.

### Recommendation
Replace the shared `LRUMap` with a thread-safe bounded cache (e.g., Guava `CacheBuilder`/`TronCache`, which the codebase already uses elsewhere, such as `common/src/main/java/org/tron/common/cache/TronCache.java`) or wrap access with proper synchronization, so concurrent constant-call/API traffic cannot corrupt the shared jump-destination analysis cache. [7](#0-6)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L117-119)
```java
  private static final int lruCacheSize = CommonParameter.getInstance().getSafeLruCacheSize();
  private static final LRUMap<Key, ProgramPrecompile> programPrecompileLRUMap
      = new LRUMap<>(lruCacheSize);
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L208-225)
```java
  public ProgramPrecompile getProgramPrecompile() {
    if (isConstantCall()) {
      if (programPrecompile == null) {
        programPrecompile = ProgramPrecompile.compile(ops);
      }
      return programPrecompile;
    }
    if (programPrecompile == null) {
      Key key = getJumpDestAnalysisCacheKey();
      if (programPrecompileLRUMap.containsKey(key)) {
        programPrecompile = programPrecompileLRUMap.get(key);
      } else {
        programPrecompile = ProgramPrecompile.compile(ops);
        programPrecompileLRUMap.put(key, programPrecompile);
      }
    }
    return programPrecompile;
  }
```

**File:** framework/src/main/java/org/tron/core/services/http/TriggerConstantContractServlet.java (L59-65)
```java
      TransactionCapsule trxCap = wallet
          .createTransactionCapsule(build.build(), ContractType.TriggerSmartContract);

      Transaction trx = wallet
          .triggerConstantContract(build.build(),trxCap,
              trxExtBuilder,
              retBuilder);
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L3139-3163)
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
```

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L1802-1846)
```java
    @Override
    public void triggerConstantContract(TriggerSmartContract request,
        StreamObserver<TransactionExtention> responseObserver) {

      callContract(request, responseObserver, true);
    }

    private void callContract(TriggerSmartContract request,
        StreamObserver<TransactionExtention> responseObserver, boolean isConstant) {
      TransactionExtention.Builder trxExtBuilder = TransactionExtention.newBuilder();
      Return.Builder retBuilder = Return.newBuilder();
      try {
        TransactionCapsule trxCap = createTransactionCapsule(request,
            ContractType.TriggerSmartContract);
        Transaction trx;
        if (isConstant) {
          trx = wallet.triggerConstantContract(request, trxCap, trxExtBuilder, retBuilder);
        } else {
          trx = wallet.triggerContract(request, trxCap, trxExtBuilder, retBuilder);
        }
        trxExtBuilder.setTransaction(trx);
        trxExtBuilder.setTxid(trxCap.getTransactionId().getByteString());
        retBuilder.setResult(true).setCode(response_code.SUCCESS);
        trxExtBuilder.setResult(retBuilder);
      } catch (ContractValidateException | VMIllegalException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_VALIDATE_ERROR)
            .setMessage(ByteString.copyFromUtf8(Wallet
                .CONTRACT_VALIDATE_ERROR + e.getMessage()));
        trxExtBuilder.setResult(retBuilder);
        logger.warn(CONTRACT_VALIDATE_EXCEPTION, e.getMessage());
      } catch (RuntimeException e) {
        retBuilder.setResult(false).setCode(response_code.CONTRACT_EXE_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        trxExtBuilder.setResult(retBuilder);
        logger.warn("When run constant call in VM, have Runtime Exception: " + e.getMessage());
      } catch (Exception e) {
        retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
            .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
        trxExtBuilder.setResult(retBuilder);
        logger.warn(UNKNOWN_EXCEPTION_CAUGHT + e.getMessage(), e);
      } finally {
        responseObserver.onNext(trxExtBuilder.build());
        responseObserver.onCompleted();
      }
    }
```

**File:** framework/src/main/java/org/tron/core/services/interfaceOnPBFT/http/TriggerConstantContractOnPBFTServlet.java (L24-32)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    walletOnPBFT.futureGet(() -> {
      try {
        super.doPost(request, response);
      } catch (IOException e) {
        logger.error("TriggerConstantContractOnPBFTServlet Exception", e);
      }
    });
  }
```

**File:** common/src/main/java/org/tron/common/cache/TronCache.java (L12-26)
```java
public class TronCache<K, V> {

  @Getter
  private final CacheType name;
  private final Cache<K, V> cache;

  TronCache(CacheType name, String strategy) {
    this.name = name;
    this.cache = CacheBuilder.from(strategy).build();
  }

  TronCache(CacheType name, String strategy, CacheLoader<K, V> loader) {
    this.name = name;
    this.cache = CacheBuilder.from(strategy).build(loader);
  }
```
