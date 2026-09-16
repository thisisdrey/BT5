Found it: `MUtil.transferAllToken` performs an unbounded loop over an account's entire TRC10 token map, triggered directly from a TVM opcode (`SELFDESTRUCT`/suicide transferring remaining tokens) reachable by any contract-calling transaction.

### Title
Unbounded iteration over `fromAccountCap.getAssetMapV2()` in `MUtil.transferAllToken` allows a contract-destruct transaction to be blocked by an attacker-inflated asset map - ([File: actuator/src/main/java/org/tron/core/vm/utils/MUtil.java])

### Summary
`MUtil.transferAllToken()` is invoked when a smart contract self-destructs, to sweep all of its TRC10 token balances to the inheritor address. It does so with `fromAccountCap.getAssetMapV2().forEach(...)`, an O(n) loop with no bound on `n`, where `n` is the number of distinct TRC10 tokens the contract account holds. Because any account (including a contract) can be made to hold an arbitrarily large number of distinct TRC10 token entries (there is no consensus-enforced limit on the number of different `assetV2` map keys per account, unlike frozen/vote/unfreeze lists which are explicitly capped via `MAX_VOTE_NUMBER`, `UNFREEZE_MAX_TIMES`, `getMaxFrozenSupplyNumber()`, etc.), an attacker can inflate a target contract's asset map before triggering its destruction, in the same fashion as the JOJO `Position.sol` report where an attacker inflates `openPositions[trader]` before triggering `_realizePnl()`.

### Finding Description
`transferAllToken` is called from `Program.suicide` handling paths for `SELFDESTRUCT`, i.e. anywhere a contract executes `SELFDESTRUCT`/self-destruct with an inheritor address: [1](#0-0) 

The loop body performs a `putAssetV2` write into both the `toBuilder` and `fromBuilder` protobuf builders for every distinct token id the source account holds — this is O(n) in CPU and also rebuilds increasingly large protobuf messages, and the resulting `AccountCapsule` for both accounts is persisted to the account store afterward.

Because TRC10 token ownership entries are added via ordinary `TransferAssetContract`/token transfer transactions (`TransferAssetActuator.execute` → `AccountCapsule.addAssetAmountV2` → `putAssetV2`), and there is no cap on how many distinct token ids an account (including a contract) can accumulate, an attacker can issue (or acquire) many distinct TRC10 tokens and transfer a small amount of each into the target contract address before that contract is caused to self-destruct. This directly mirrors the JOJO `Position.sol` pattern where `state.openPositions[trader]` is inflated before a size-dependent loop is triggered in `_realizePnl()`.

Other similar-looking loops in the codebase (frozen lists, unfreeze lists, vote lists, frozen-supply lists) are explicitly bounded by protocol constants (`MAX_VOTE_NUMBER = 30`, `UNFREEZE_MAX_TIMES = 32`, `dynamicStore.getMaxFrozenSupplyNumber()`), confirming that the java-tron team is otherwise careful about this bug class — `transferAllToken`'s `assetV2` map iteration appears to be the one path missing an equivalent bound. [2](#0-1) [3](#0-2) 

### Impact Explanation
A caller can grow the size of `assetV2Map` for any target account (including their own contract, or a victim contract they can induce to receive tokens) without bound. When that contract later self-destructs, the resulting energy/CPU cost of `transferAllToken` scales linearly with the number of distinct tokens, which can be pushed arbitrarily high, causing the transaction (and thus block-application logic in that transaction's execution) to run far longer than intended, or to run out of energy/consume excessive CPU relative to what fee/energy accounting assumes for a constant-cost opcode step. This is a resource-exhaustion / node-processing-delay issue triggerable by a single unprivileged party crafting transactions (issue small amounts of tokens they mint, transfer to the victim contract, trigger self-destruct), matching the Medium-severity DoS bug class described in the report.

### Likelihood Explanation
Reaching this code requires: (1) creating/controlling multiple distinct TRC10 token ids (asset issuance is cheap and unprivileged, subject only to `AssetIssueFee`), (2) transferring a tiny amount of each into the target contract address via ordinary `TransferAssetContract`, and (3) causing that contract to execute `SELFDESTRUCT` with an inheritor. All three steps are reachable purely through normal, unprivileged transaction submission and TVM contract calls, with no special permissions required, making likelihood moderate — the attacker needs a target contract that both accepts many TRC10 tokens and eventually self-destructs (e.g., a contract with an owner-only or attacker-controlled destruct path, or an attacker's own contract).

### Recommendation
Bound the number of distinct TRC10 token ids that can be tracked on a single account (mirroring the existing caps for frozen/vote/unfreeze lists), or change `transferAllToken` to defer/skip the sweep (or cap the number of tokens transferred per self-destruct) rather than iterating the full, unbounded `assetV2Map` inline in the state-transition path.

### Proof of Concept
1. Attacker issues N distinct TRC10 tokens (`AssetIssueContract`), N large (e.g. tens of thousands), paying only the per-issuance `AssetIssueFee`.
2. Attacker transfers 1 unit of each token into the address of a contract that the attacker can trigger to self-destruct with an inheritor (e.g., a contract the attacker deployed, or a target contract with a permissionless kill function).
3. Attacker calls the contract's self-destruct function; execution reaches `Program.suicide` → `MUtil.transferAllToken(deposit, contractAddr, inheritorAddr)`.
4. `fromAccountCap.getAssetMapV2().forEach(...)` at [4](#0-3) 
 iterates all N tokens, causing CPU/time cost proportional to N within a single transaction's execution.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L28-41)
```java
  public static void transferAllToken(Repository deposit, byte[] fromAddress, byte[] toAddress) {
    AccountCapsule fromAccountCap = deposit.getAccount(fromAddress);
    Protocol.Account.Builder fromBuilder = fromAccountCap.getInstance().toBuilder();
    AccountCapsule toAccountCap = deposit.getAccount(toAddress);
    toAccountCap.importAllAsset();
    Protocol.Account.Builder toBuilder = toAccountCap.getInstance().toBuilder();
    fromAccountCap.getAssetMapV2().forEach((tokenId, amount) -> {
      toBuilder.putAssetV2(tokenId, toBuilder.getAssetV2Map().getOrDefault(tokenId, 0L) + amount);
      fromBuilder.putAssetV2(tokenId, 0L);
    });

    deposit.putAccountValue(fromAddress, new AccountCapsule(fromBuilder.build()));
    deposit.putAccountValue(toAddress, new AccountCapsule(toBuilder.build()));
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/VoteWitnessActuator.java (L93-97)
```java
    int maxVoteNumber = MAX_VOTE_NUMBER;
    if (contract.getVotesCount() > maxVoteNumber) {
      throw new ContractValidateException(
          "VoteNumber more than maxVoteNumber " + maxVoteNumber);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceV2Actuator.java (L179-182)
```java
    int unfreezingCount = accountCapsule.getUnfreezingV2Count(now);
    if (UNFREEZE_MAX_TIMES <= unfreezingCount) {
      throw new ContractValidateException("Invalid unfreeze operation, unfreezing times is over limit");
    }
```
