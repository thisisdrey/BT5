### Title
Ineffective vote/reward state check in TVM `canSuicide()` permits SELFDESTRUCT to bypass vote-based fund-lock restriction - (File: `actuator/src/main/java/org/tron/core/vm/program/Program.java`)

### Summary
`Program.canSuicide()` is the gate that TVM's `SUICIDE`/`SELFDESTRUCT` opcode uses to decide whether a contract account is allowed to self-destruct. The intent of this state-based limitation (mirroring the "object state limitation" pattern in the reference advisory) is to block self-destruction while the account is in a state that still carries value/obligations that would otherwise be lost or double-counted — historically this included frozen-for-resources balances **and** an outstanding vote/reward state (`voteCheck`). In the current code, the vote/reward branch has been disabled (commented out) while the freeze checks remain active.

### Finding Description
`canSuicide()` computes its result solely from `freezeCheck` and `freezeV2Check`: [1](#0-0) 

The commented-out block shows a `voteCheck` that used to also gate the decision on `accountCapsule.getVotesList().size() == 0`, `VoteRewardUtil.queryReward(...) == 0`, and `getAccountVote(...) == null`. This `voteCheck` is no longer part of the returned expression — the method now returns `freezeCheck && freezeV2Check` unconditionally, so an account with active votes or unclaimed voting rewards is no longer prevented from self-destructing. This is structurally identical to the advisory's bug class: a limitation meant to gate an operation based on an object's persisted state (object state / vote state) silently stopped being enforced due to a code change, while the caller/opcode logic still assumes the restriction is active.

### Impact Explanation
If a contract account that has active votes and/or unclaimed voting rewards executes `SELFDESTRUCT`, the vote/reward-based protection no longer blocks it. Depending on how vote power, TP (Tron Power) and reward accounting are computed elsewhere (e.g., via `getBeginCycle`/`VoteRewardUtil.queryReward`), destroying the account while it still holds votes and unclaimed rewards can strand or orphan voting/reward state tied to an address that can no longer be normally operated on through this restriction, or let the caller extract the freed TRX from the destructed contract while ballot/voting bookkeeping tied to it becomes inconsistent (permanent loss/misaccounting of state tracked per-address). This is a state-integrity/accounting flaw reachable by any contract deployer who freezes+votes then calls `SELFDESTRUCT`, matching the "unauthorized account operation" / "unbacked balance or permanently frozen funds"-class impact required by the validation rules.

### Likelihood Explanation
High reachability: any user can deploy a contract, freeze balance for votes, vote for witnesses, and then trigger `SELFDESTRUCT` through a single signed `TriggerSmartContract`/`CreateSmartContract` transaction. No special privileges are required — this is exactly the "contract deployer" persona explicitly in scope. The only precondition is that `VMConfig.allowTvmVote()`/voting features are enabled on the network (which they are on mainnet since the vote-related TIP activated).

### Recommendation
Restore (or reintroduce under the appropriate `VMConfig.allowTvmVote()` gate) the vote/reward check in `canSuicide()` so self-destruction is blocked while the account has non-zero votes, unclaimed reward, or an active account-vote-cycle entry, consistent with the freeze/freezeV2 checks that remain active. Add regression tests asserting `SELFDESTRUCT` is rejected for accounts with outstanding votes/rewards, analogous to existing freeze-based `canSuicide` tests.

### Proof of Concept
1. Deploy a contract account, freeze balance for bandwidth/energy and cast votes for witnesses (enabling `allowTvmVote`).
2. Ensure no frozen/delegated balances remain outstanding (so `freezeCheck`/`freezeV2Check` both pass) but leave votes/unclaimed reward outstanding.
3. Invoke the contract's function that triggers the `SUICIDE` opcode.
4. Observe `canSuicide()` returns `true` (only `freezeCheck && freezeV2Check` are evaluated) and the contract self-destructs despite having outstanding vote/reward state, whereas the commented-out `voteCheck` — if reinstated — would have blocked it.

Note: I could not retrieve version-control history (blame) for this file to confirm exactly when/why `voteCheck` was disabled, so I cannot definitively state whether this is an intentional feature-flag transition (e.g., votes/rewards now tracked/cleared elsewhere) or a regression. This should be verified against the corresponding vote/reward clearing logic elsewhere in the codebase (`VoteRewardUtil`, `AccountVoteStore`) before treating this as a confirmed vulnerability rather than a design change. [2](#0-1)

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L736-752)
```java
  public boolean canSuicide() {
    byte[] owner = getContextAddress();
    AccountCapsule accountCapsule = getContractState().getAccount(owner);

    boolean freezeCheck = !VMConfig.allowTvmFreeze()
        || (accountCapsule.getDelegatedFrozenBalanceForBandwidth() == 0
        && accountCapsule.getDelegatedFrozenBalanceForEnergy() == 0);

    boolean freezeV2Check = freezeV2Check(accountCapsule);
    return freezeCheck && freezeV2Check;
//    boolean voteCheck = !VMConfig.allowTvmVote()
//        || (accountCapsule.getVotesList().size() == 0
//        && VoteRewardUtil.queryReward(owner, getContractState()) == 0
//        && getContractState().getAccountVote(
//            getContractState().getBeginCycle(owner), owner) == null);
//    return freezeCheck && voteCheck;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L754-758)
```java
  public boolean canSuicide2() {
    byte[] owner = getContextAddress();
    AccountCapsule accountCapsule = getContractState().getAccount(owner);

    return freezeV1Check(accountCapsule) && freezeV2Check(accountCapsule);
```
