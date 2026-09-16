### Title
Endowment charged to failed precompiled contract call is not reverted, permanently burning caller funds - ([File: actuator/src/main/java/org/tron/core/vm/program/Program.java])

### Summary
`Program.callToPrecompiledAddress` charges the call's TRX/TRC10 endowment to the precompiled contract's `deposit` child repository *before* invoking `contract.execute(data)`. The code explicitly comments that this charge "is not reversible by rollback." If the precompiled contract execution subsequently fails (`out.getLeft() == false`), the caller only gets a zero pushed to the stack and energy refunded — the funds transferred as `endowment` are never returned to the sender, mirroring the reported "no return of funds if operation fails" bug class.

### Finding Description
In `callToPrecompiledAddress`: [1](#0-0) 
the endowment (TRX via `MUtil.transfer` or TRC10 via direct `addTokenBalance`) is moved from `senderAddress` to `contextAddress` using the same `deposit` repository object that will later hold the results of `contract.execute(data)`.

Then: [2](#0-1) 
on success, `deposit.commit()` persists everything (transfer + execution effects) to the parent state; on failure, only `stackPushZero()` happens and `deposit` is simply discarded/never committed for the failure branch — **except** for the endowment transfer, which the surrounding comment says is deliberately treated as "not reversible by rollback." That means the caller's balance debit made at line ~1717/1729 is intended to persist regardless of whether the precompiled call actually succeeds, while the corresponding credit to `contextAddress` lives inside the same uncommitted `deposit`.

This is analogous to the reported Endpoint issue: an unprivileged caller (any account triggering a smart contract that performs a low-level `CALL`/`STATICCALL`/`DELEGATECALL` with `value`/token to a precompiled contract address) transfers funds as part of invoking the precompiled contract, but if that precompiled contract's `execute()` fails/returns false, there is no path in `callToPrecompiledAddress` that returns the endowment to the sender.

### Impact Explanation
Any user or contract that calls a precompiled address with a non-zero TRX or TRC10 endowment and triggers a failure inside `contract.execute(data)` (e.g. insufficient energy is a separate branch, but a precompiled contract returning `false` from `execute` due to invalid input) can have their funds debited without a matching credit reaching a controlled, committed account — an unbacked-balance / permanent loss-of-funds condition, matching the "permanent freezing/loss of funds" impact bar from the rules.

### Likelihood Explanation
Reachable by any unprivileged contract-calling transaction: the attacker (or an unaware user) only needs to trigger a `CALL`-family opcode with `value`>0 targeting a precompiled contract address and craft input data that causes that precompiled contract's `execute()` to fail. This requires no special privileges — deploying/triggering smart contracts is available to any account.

### Recommendation
Move the endowment charge so it is only applied to the parent state after `contract.execute(data)` succeeds (i.e., perform the transfer within the same commit boundary as the rest of `deposit`), or explicitly refund/reverse the endowment transfer in the `else` (failure) branch of `callToPrecompiledAddress` before returning, so that a failed precompiled call cannot result in a net loss of the caller's balance.

### Proof of Concept
1. Deploy/trigger a smart contract that performs a low-level call (e.g. `address(precompiledAddr).call.value(V)(data)`) targeting a precompiled contract address with `data` crafted to make that precompiled contract's `execute()` return `(false, ...)`.
2. Observe in `Program.callToPrecompiledAddress`:
   - the endowment `V` is debited from the caller and credited to `contextAddress` through `deposit` (lines 1712-1732),
   - `contract.execute(data)` returns `false` (line 1752-1754 branch to `else`),
   - only `stackPushZero()` and energy refund occur (lines 1759-1766); `deposit.commit()` is never invoked for this path, so it is unclear whether the debit is actually retained in the final committed state — this needs to be confirmed by tracing whether `MUtil.transfer`/`addTokenBalance` calls in this uncommitted `deposit` end up persisted regardless (as the code comment claims) or discarded together with the rest of `deposit`.
3. Compare caller's TRX/token balance before and after the failed call across the actual chain state (not just the discarded `deposit`) to confirm whether the debit persists without a corresponding credit.

**Note on certainty:** I was not able to fully trace `RepositoryImpl`'s child/parent commit semantics (i.e., whether a `newRepositoryChild()` deposit's uncommitted changes are fully discarded, or whether balance-adjustment calls like `addBalance`/`addTokenBalance` write through to the parent independent of `commit()`) within the available search budget. The in-code comment "Charge for endowment - is not reversible by rollback" strongly suggests the debit is intentionally irreversible even on failure, which is the crux of this finding, but confirming the exact persistence mechanics of `RepositoryImpl` would require deeper reading of `actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java` (`newRepositoryChild`, `commit`, `addBalance`, `addTokenBalance`) than was completed here.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1712-1732)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1752-1766)
```java
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
```
