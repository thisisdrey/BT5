### Title
Unchecked `CountDownLatch.await()` timeout result before unbounded `Future.get()` in shielded-transfer precompile allows CPU-budget bypass / node stall - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
`PrecompiledContracts.VerifyTransferProof.execute()` computes whether the concurrent zk-SNARK verification tasks finished within the CPU-time budget (`countDownLatch.await(...)`), but discards that boolean and then unconditionally calls the blocking, timeout-less `Future.get()` on every submitted task, exactly mirroring the CVE-2021-32845 pattern of "compute a status/return value, then use dependent state without checking it."

### Finding Description
In `VerifyTransferProof.execute()` the code submits per-spend and per-output zk-proof verification jobs (native `librustzcash` calls via JNI) to a small fixed thread pool and then does: [1](#0-0) 

`countDownLatch.await(getCPUTimeLeftInNanoSecond(), TimeUnit.NANOSECONDS)` returns `false` when the CPU-time budget has been exhausted before all submitted tasks finished, but the result is captured in `withNoTimeout` and never inspected or acted upon. Execution then falls straight into a loop calling `future.get()` (no timeout argument) on every submitted `Future`, so the calling thread — the thread executing the transaction inside the TVM/`Program.callToPrecompiledAddress` path — blocks until the native worker actually completes, irrespective of whatever CPU-time enforcement the rest of the VM relies on: [2](#0-1) 

The worker pool itself is a small, fixed-size, shared `ExecutorService` (`workersInConstantCall`/`workersInNonConstantCall`, size 5) used by every shielded-transfer precompile invocation on the node: [3](#0-2) 

Because the `withNoTimeout` value from the deadline check is never used to cancel the futures or abort, the intended per-call CPU-time guard is a no-op for this precompile: even after the budget nominally "expires," the calling thread keeps waiting on `future.get()`, and outstanding native cryptographic checks keep consuming a worker-pool slot. This is directly analogous to the reported bug class: an unchecked return value (`vq_getchain` result / here, `countDownLatch.await()` result) leads to continued use of state that was supposed to gate further action.

### Impact Explanation
Any unprivileged transaction broadcaster can invoke this precompile — it backs the shielded TRC-20 `transferProof` verification exposed through a normal TVM `CALL`/`STATICCALL` to the fixed precompile address, reachable by any signed transaction or constant call. Because the CPU-time check result is discarded, an attacker can:
- Submit crafted shielded-transfer input that is accepted by the size/format checks but drives the native Rust verification calls into long-running paths, or
- Flood the node with concurrent shielded-transfer invocations to saturate the fixed 5-thread pool shared by all callers,

causing the block/transaction-processing thread(s) in `Manager`'s block-application path to block on `future.get()` well past the CPU budget the rest of the VM otherwise enforces, stalling processing of that transaction/block. This falls into the accepted "node crash or halt" impact category, since the CPU-time-based execution guard — the mechanism meant to bound worst-case execution time for a single call — is bypassed for this precompile.

### Likelihood Explanation
The precompile is reachable directly from any account through a standard contract call into a fixed system-precompile address, so no privileged role is required; only crafted call-data structurally valid enough to pass the length/`spendCount`/`receiveCount` checks is needed to reach the vulnerable await/get sequence, making this High likelihood for triggering, though the degree of stall depends on how slow the native verification can be driven or how many concurrent calls flood the shared pool.

### Recommendation
Check the boolean returned by `countDownLatch.await(...)`; if `false` (timeout), cancel the outstanding `Future`s (`future.cancel(true)`), avoid calling blocking `future.get()` without a bound, and return the failure/energy-exhausted result immediately instead of waiting further. Apply the same fix to any other precompile using the identical await-then-get pattern (e.g. `VerifyMintProof`/`VerifyBurnProof`'s `insertLeaves` if it uses the same construct).

### Proof of Concept
1. Craft a valid-size `VerifyTransferProof` payload (`SIZE` in {2080,2368,2464,2752}) with `spendCount`/`receiveCount` in the allowed range but selected such that the underlying `librustzcashSaplingCheckSpendNew`/`CheckOutputNew` native calls take unusually long (or simply issue many concurrent shielded-transfer contract calls from different accounts/transactions).
2. Send this as a TVM `CALL` to the `VerifyTransferProof` precompile address from an ordinary account-owned smart contract, requiring only a normal signed transaction with sufficient fee limit.
3. Observe that `countDownLatch.await(getCPUTimeLeftInNanoSecond(), ...)` times out (`withNoTimeout == false`) but the code proceeds to call `future.get()` on all futures anyway, keeping the executing thread and a slot in the shared 5-thread pool occupied beyond the intended CPU-time budget, which can be repeated/parallelized to degrade or stall block/transaction processing on the node.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1449-1457)
```java
    private static final ExecutorService workersInConstantCall;
    private static final ExecutorService workersInNonConstantCall;
    private static final String constantCallName = "verify-transfer-constant-call";
    private static final String nonConstantCallName = "verify-transfer-non-constant-call";

    static {
      workersInConstantCall = ExecutorServiceManager.newFixedThreadPool(constantCallName, 5);
      workersInNonConstantCall = ExecutorServiceManager.newFixedThreadPool(nonConstantCallName, 5);
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L1589-1595)
```java
        boolean withNoTimeout = countDownLatch.await(getCPUTimeLeftInNanoSecond(),
            TimeUnit.NANOSECONDS);
        boolean checkResult = true;
        for (Future<Boolean> future : futures) {
          boolean eachTaskResult = future.get();
          checkResult = checkResult && eachTaskResult;
        }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1670-1774)
```java
  public void callToPrecompiledAddress(MessageCall msg,
      PrecompiledContracts.PrecompiledContract contract) {
    returnDataBuffer = null; // reset return buffer right before the call

    if (getCallDeep() == MAX_DEPTH) {
      stackPushZero();
      this.refundEnergy(msg.getEnergy().longValue(), " call deep limit reach");
      return;
    }

    Repository deposit = getContractState().newRepositoryChild();

    byte[] senderAddress = getContextAddress();
    byte[] contextAddress;
    if (msg.getOpCode() == Op.CALLCODE || msg.getOpCode() == Op.DELEGATECALL) {
      contextAddress = senderAddress;
    } else {
      contextAddress = msg.getCodeAddress().toTronAddress();
    }

    long endowment = msg.getEndowment().value().longValueExact();
    long senderBalance = 0;
    byte[] tokenId = null;

    checkTokenId(msg);
    boolean isTokenTransfer = isTokenTransfer(msg);
    // transfer TRX validation
    if (!isTokenTransfer) {
      senderBalance = deposit.getBalance(senderAddress);
    } else {
      // transfer trc10 token validation
      tokenId = String.valueOf(msg.getTokenId().longValue()).getBytes();
      senderBalance = deposit.getTokenBalance(senderAddress, tokenId);
    }
    if (senderBalance < endowment) {
      stackPushZero();
      refundEnergy(msg.getEnergy().longValue(), REFUND_ENERGY_FROM_MESSAGE_CALL);
      return;
    }
    byte[] data = this.memoryChunk(msg.getInDataOffs().intValue(),
        msg.getInDataSize().intValue());

    // Charge for endowment - is not reversible by rollback
    if (!ArrayUtils.isEmpty(senderAddress) && !ArrayUtils.isEmpty(contextAddress)
        && senderAddress != contextAddress && msg.getEndowment().value().longValueExact() > 0) {
      if (!isTokenTransfer) {
        try {
          MUtil.transfer(deposit, senderAddress, contextAddress,
              msg.getEndowment().value().longValueExact());
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException("transfer failure");
        }
      } else {
        try {
          VMUtils
              .validateForSmartContract(deposit, senderAddress, contextAddress, tokenId, endowment);
        } catch (ContractValidateException e) {
          throw new BytecodeExecutionException(VALIDATE_FOR_SMART_CONTRACT_FAILURE, e.getMessage());
        }
        deposit.addTokenBalance(senderAddress, tokenId, -endowment);
        deposit.addTokenBalance(contextAddress, tokenId, endowment);
      }
    }

    long requiredEnergy = contract.getEnergyForData(data);
    if (requiredEnergy > msg.getEnergy().longValue()) {
      // Not need to throw an exception, method caller needn't know that
      // regard as consumed the energy
      this.refundEnergy(0, CALL_PRE_COMPILED); //matches cpp logic
      this.stackPushZero();
    } else {
      // Delegate or not. if is delegated, we will use msg sender, otherwise use contract address
      if (msg.getOpCode() == Op.DELEGATECALL) {
        contract.setCallerAddress(getCallerAddress().toTronAddress());
      } else {
        contract.setCallerAddress(getContextAddress());
      }
      // this is the depositImpl, not contractState as above
      contract.setRepository(deposit);
      contract.setResult(this.result);
      contract.setConstantCall(isConstantCall());
      contract.setVmShouldEndInUs(getVmShouldEndInUs());
      Pair<Boolean, byte[]> out = contract.execute(data);

      if (out.getLeft()) { // success
        this.refundEnergy(msg.getEnergy().longValue() - requiredEnergy, CALL_PRE_COMPILED);
        this.stackPushOne();
        returnDataBuffer = out.getRight();
        deposit.commit();
      } else {
        // spend all energy on failure, push zero and revert state changes
        this.refundEnergy(0, CALL_PRE_COMPILED);
        this.stackPushZero();
        if (Objects.nonNull(this.result.getException())) {
          throw result.getException();
        }
      }

      if (VMConfig.allowTvmSelfdestructRestriction()) {
        this.memorySave(msg.getOutDataOffs().intValueSafe(), msg.getOutDataSize().intValueSafe(), out.getRight());
      } else {
        this.memorySave(msg.getOutDataOffs().intValue(), out.getRight());
      }
    }
  }
```
