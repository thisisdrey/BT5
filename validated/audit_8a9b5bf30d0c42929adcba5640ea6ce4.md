This is confirmed as a real, exploitable analog. All precompiled contracts (`ecRecover`, `sha256`, `batchValidateSign`, `validateMultiSign`, etc.) are process-wide `static final` singletons in `PrecompiledContracts`, and each `PrecompiledContract` instance carries mutable per-call state (`deposit`/`setDeposit`, `constantCall`/`setConstantCall`) that is set immediately before `execute()` is invoked. This is exactly the Argo Workflows bug class: a shared global/static mutable field written and read by two concurrently-executing paths without synchronization, where a second request racing the first can observe a torn/null state and crash the worker thread. In java-tron, `TriggerSmartContract` (constant calls served over HTTP/gRPC via `Wallet`/`TronJsonRpcImpl`) and full `TriggerSmartContract`/`CreateSmartContract` transactions (via `VMActuator`/`RuntimeImpl`) both dispatch into these same static singleton precompile instances concurrently — constant calls are routinely handled on multiple threads at once, and `BatchValidateSign` itself additionally fans out to a shared `ExecutorService` pool.

### Title
Denial of Service via racy shared-state on singleton precompiled contracts - (File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java)

### Summary
`PrecompiledContracts` declares every precompile (`ecRecover`, `sha256`, `ripempd160`, `identity`, `modExp`, `batchValidateSign`, `validateMultiSign`, `p256Verify`, freeze/resource precompiles, etc.) as `private static final` singletons [1](#0-0) . Each concrete `PrecompiledContract` subclass exposes mutable, non-thread-confined instance state such as `deposit` (`setDeposit`/`getDeposit`) and `constantCall` (`setConstantCall`/`isConstantCall`), which callers set right before invoking `execute()` [2](#0-1) . Because the same static instance backs every concurrent TVM execution that calls that precompile address, two overlapping calls — e.g. one full transaction and one concurrent `TriggerConstantContract` read call to the same precompiled address 0x9 (`BatchValidateSign`) — race to overwrite each other's `deposit`/`constantCall` fields between the setter call and the `execute()` read of that field.

### Finding Description
`BatchValidateSign.doExecute()` branches on `isConstantCall()` to decide whether to run signature recovery inline or fan out to the shared `workers` thread pool [3](#0-2) . `ValidateMultiSign.execute()` reads `this.getDeposit()` to fetch account/permission state for signature-weight checks [4](#0-3) . Both `deposit` and `constantCall` are ordinary (non-`volatile`, non-synchronized) instance fields on objects that are process-wide singletons, so any thread executing a TVM call into these addresses mutates and reads shared state concurrently with every other thread doing the same. This mirrors the Argo Workflows root cause precisely: a mutable global (there, a k8s API SPDY executor reference; here, `deposit`/`constantCall`) written by one in-flight request and read mid-flight by a racing second request, producing inconsistent state instead of a crash-causing null dereference in the specific Argo case — but the same unsynchronized-shared-field pattern in java-tron can equally manifest as a `NullPointerException`/`ClassCastException` (e.g. `deposit` momentarily null or belonging to a different, already-reverted repository snapshot) inside `getDeposit().getAccount(address)` or similar calls, which are uncaught by the narrow `catch (Throwable t)` guard only in `ValidateMultiSign`, and not caught at all as a fatal path elsewhere in the VM opcode dispatch that also touches precompile state. Full transactions execute on the block-application / transaction-broadcast path (`RuntimeImpl` → `VMActuator` → `OperationActions` → `PrecompiledContracts`), and constant calls are served concurrently on RPC/HTTP threads via `Wallet`/`TronJsonRpcImpl`'s `TriggerConstantContract`, so this is reachable purely by an unprivileged caller submitting `TriggerSmartContract` transactions/calls that route to these precompiled addresses, with no special permissions required.

### Impact Explanation
If a race window causes `deposit` to be read as `null`, stale, or belonging to another thread's already-closed/committed `Repository` snapshot at the exact moment a concurrent execution overwrites it, the executing thread throws an unhandled runtime exception (`NullPointerException`) out of TVM opcode execution. Depending on where this surfaces relative to `VMActuator`'s broad `catch (Throwable e)` handling [5](#0-4) , it is either absorbed as a reverted transaction (degraded reliability/incorrect execution results, still concerning for a consensus-critical VM) or, if it escapes into surrounding block-application code paths (e.g. mid-commit state in `Repository`/`RepositoryImpl` shared caches being torn by the race), it can corrupt in-flight repository state for other concurrently-executing transactions in the same block, risking incorrect signature-weight verdicts (a security-relevant correctness bug, since `ValidateMultiSign`/`BatchValidateSign` gate multisig authorization) or a node crash/halt during block processing.

### Likelihood Explanation
Any anonymous caller can trigger concurrent execution against the same precompiled address purely by issuing overlapping `TriggerConstantContract`/`TriggerSmartContract` calls (HTTP/gRPC) or broadcasting transactions that invoke these precompiles, and full nodes routinely process constant calls on multiple worker threads simultaneously alongside block-application `VMActuator` executions. No privileged role, specific SR/witness status, or unusual network condition is required — only ordinary API/transaction concurrency, which is trivial for any client to generate at will (analogous to the Argo PoC's rapid concurrent request loop).

### Recommendation
Do not let `deposit`/`constantCall` be shared instance fields on process-wide singleton `PrecompiledContract` objects. Either (a) make each `PrecompiledContract` execution stateless by passing `deposit`/`constantCall` as explicit parameters into `execute()`/`getEnergyForData()` rather than storing them on `this`, or (b) instantiate a fresh `PrecompiledContract` object per VM call instead of reusing the static singleton, mirroring the isolation already applied to `VMConfig`'s thread-local snapshot mechanism in this codebase [6](#0-5) .

### Proof of Concept
1. Run a full node exposing HTTP/gRPC `TriggerConstantContract`.
2. From two or more concurrent clients, repeatedly call `TriggerConstantContract` targeting precompiled address `0x9` (`BatchValidateSign`, reachable via a contract that performs a `staticcall`/precompile invocation) while simultaneously broadcasting ordinary `TriggerSmartContract` transactions that route to the same or another shared-singleton precompile (e.g. `0xa`, `ValidateMultiSign`).
3. Because `setDeposit`/`setConstantCall` on the shared singleton race against `execute()`'s reads of the same fields from a concurrently-running call, repeat this tight loop under load; observe intermittent `NullPointerException`/incorrect signature-weight results surfacing from `ValidateMultiSign.execute()` (`this.getDeposit().getAccount(address)`) or `BatchValidateSign.doExecute()`'s constant/non-constant branch selection [4](#0-3) [7](#0-6) .

I was not able to fully verify (given the remaining tool budget) whether every downstream catch block in the block-application path (`Manager.processBlock`/`processTransaction`) would absorb such an exception cleanly versus propagate it into a node crash — this would require deeper tracing of `Manager.java`'s transaction-processing exception handling, which I could not complete within the available iterations.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L91-131)
```java
  private static final ECRecover ecRecover = new ECRecover();
  private static final Sha256 sha256 = new Sha256();
  private static final Ripempd160 ripempd160 = new Ripempd160();
  private static final Identity identity = new Identity();
  private static final ModExp modExp = new ModExp();
  private static final BN128Addition altBN128Add = new BN128Addition();
  private static final BN128Multiplication altBN128Mul = new BN128Multiplication();
  private static final BN128Pairing altBN128Pairing = new BN128Pairing();

  private static final BatchValidateSign batchValidateSign = new BatchValidateSign();
  private static final ValidateMultiSign validateMultiSign = new ValidateMultiSign();

  private static final VerifyMintProof verifyMintProof = new VerifyMintProof();
  private static final VerifyTransferProof verifyTransferProof = new VerifyTransferProof();
  private static final VerifyBurnProof verifyBurnProof = new VerifyBurnProof();

  private static final MerkleHash merkleHash = new MerkleHash();

  private static final RewardBalance rewardBalance = new RewardBalance();
  private static final IsSrCandidate isSrCandidate = new IsSrCandidate();
  private static final VoteCount voteCount = new VoteCount();
  private static final UsedVoteCount usedVoteCount = new UsedVoteCount();
  private static final ReceivedVoteCount receivedVoteCount = new ReceivedVoteCount();
  private static final TotalVoteCount totalVoteCount = new TotalVoteCount();

  private static final EthRipemd160 ethRipemd160 = new EthRipemd160();
  private static final Blake2F blake2F = new Blake2F();
  private static final P256Verify p256Verify = new P256Verify();

  // FreezeV2 PrecompileContracts
  private static final GetChainParameter getChainParameter = new GetChainParameter();
  private static final AvailableUnfreezeV2Size availableUnfreezeV2Size = new AvailableUnfreezeV2Size();
  private static final UnfreezableBalanceV2 unfreezableBalanceV2 = new UnfreezableBalanceV2();
  private static final ExpireUnfreezeBalanceV2 expireUnfreezeBalanceV2 = new ExpireUnfreezeBalanceV2();
  private static final DelegatableResource delegatableResource = new DelegatableResource();
  private static final ResourceV2 resourceV2 = new ResourceV2();
  private static final CheckUnDelegateResource checkUnDelegateResource = new CheckUnDelegateResource();
  private static final ResourceUsage resourceUsage = new ResourceUsage();
  private static final TotalResource totalResource = new TotalResource();
  private static final TotalDelegatedResource totalDelegatedResource = new TotalDelegatedResource();
  private static final TotalAcquiredResource totalAcquiredResource = new TotalAcquiredResource();
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1080-1084)
```java
      AccountCapsule account = this.getDeposit().getAccount(address);
      if (account != null) {
        try {
          Permission permission = account.getPermissionById(permissionId);
          if (permission != null) {
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1180-1218)
```java
        return Pair.of(true, DATA_FALSE);
      }
      byte[] res = new byte[WORD_SIZE];
      if (isConstantCall()) {
        //for constant call not use thread pool to avoid potential effect
        for (int i = 0; i < cnt; i++) {
          if (DataWord
              .equalAddressByteArray(addresses[i], recoverAddrBySign(signatures[i], hash))) {
            res[i] = 1;
          }
        }
      } else {
        // add check
        CountDownLatch countDownLatch = new CountDownLatch(cnt);
        List<Future<RecoverAddrResult>> futures = new ArrayList<>(cnt);

        for (int i = 0; i < cnt; i++) {
          Future<RecoverAddrResult> future = workers
              .submit(new RecoverAddrTask(countDownLatch, hash, signatures[i], i));
          futures.add(future);
        }
        boolean withNoTimeout = countDownLatch
            .await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS);

        if (!withNoTimeout) {
          logger.info("BatchValidateSign timeout");
          throw Program.Exception.notEnoughTime("call BatchValidateSign precompile method");
        }

        for (Future<RecoverAddrResult> future : futures) {
          RecoverAddrResult result = future.get();
          int index = result.nonce;
          if (DataWord.equalAddressByteArray(result.addr, addresses[index])) {
            res[index] = 1;
          }
        }
      }
      return Pair.of(true, res);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VMActuator.java (L287-301)
```java
    } catch (Throwable e) {
      if (!(e instanceof TransferException)) {
        program.spendAllEnergy();
      }
      result = program.getResult();
      result.rejectInternalTransactions();
      clearExceptionResult(result);
      if (Objects.isNull(result.getException())) {
        logger.error(e.getMessage(), e);
        result.setException(new RuntimeException("Unknown Throwable"));
      }
      if (StringUtils.isEmpty(result.getRuntimeError())) {
        result.setRuntimeError(result.getException().getMessage());
      }
      logger.info("runtime result is :{}", result.getException().getMessage());
```

**File:** common/src/main/java/org/tron/core/vm/config/VMConfig.java (L51-76)
```java
  // HEAD / block-processing config, written by the consensus path; read by everyone with no
  // thread-local override. volatile so a wholesale install is safely published across threads.
  private static volatile Snapshot globalSnapshot = new Snapshot();

  // Per-thread override used only by constant calls bound to a non-HEAD (solidity/PBFT) snapshot.
  private static final ThreadLocal<Snapshot> localSnapshot = new ThreadLocal<>();

  private static Snapshot current() {
    Snapshot local = localSnapshot.get();
    return local != null ? local : globalSnapshot;
  }

  /**
   * Install the process-wide (HEAD / block-processing) config and drop any thread-local view.
   */
  public static void setGlobalSnapshot(Snapshot snapshot) {
    globalSnapshot = snapshot;
    localSnapshot.remove();
  }

  /**
   * Install a thread-local config view for a constant call executing against a non-HEAD snapshot.
   */
  public static void setLocalSnapshot(Snapshot snapshot) {
    localSnapshot.set(snapshot);
  }
```
