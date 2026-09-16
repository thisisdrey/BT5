### Title
Disabling `ALLOW_SHIELDED_TRC20_TRANSACTION` after mints permanently locks TRC20 tokens deposited into shielded pool - ([File: actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java])

### Summary
This is the same bug class as the reported Omni bridge issue: an unprivileged deposit-style operation (locking transparent TRC20 tokens into a shielded pool via `mint`) is followed, in a separate transaction, by a privileged "pause" (committee disabling shielded TRC20 support). Because the withdrawal path (`transfer`/`burn`) is gated by the exact same on/off switch as the deposit path, and there is no queued/retry mechanism, tokens already locked in the shielded contract become permanently unredeemable if the switch is turned off after deposits have been made.

### Finding Description
The Shielded TRC20 feature lets a user "mint" transparent TRC20 balance into a shielded pool (analogous to `bridge()`/deposit) and later "transfer"/"burn" shielded notes back to a transparent TRC20 balance (analogous to `withdraw()`). All three operations (`mint`, `transfer`, `burn`) rely on TVM precompiled contracts (`verifyMintProof`, `verifyTransferProof`, `verifyBurnProof`, `merkleHash`) that are only reachable when `VMConfig.allowShieldedTRC20Transaction()` is true: [1](#0-0) 

This flag is controlled by the `ALLOW_SHIELDED_TRC20_TRANSACTION` chain parameter, which the `ProposalUtil` validator explicitly allows to be toggled to either `0` or `1` at any time, with no check on whether shielded pool funds currently exist: [2](#0-1) 

When a committee proposal for this parameter is approved, `ProposalService.process` simply overwrites the dynamic property with no validation of outstanding shielded balances: [3](#0-2) 

`DynamicPropertiesStore.supportShieldedTRC20Transaction()` is the single boolean gate read by `VMConfig`/`PrecompiledContracts` for mint, transfer, and burn alike: [4](#0-3) 

Because a single flag gates both the "deposit" direction (mint) and the "withdraw" direction (transfer/burn) of the shielded pool, once any unprivileged user has minted TRC20 tokens into the shielded contract (locking their transparent balance and receiving a shielded note), a subsequent governance vote turning the switch off removes the *only* mechanism (`verifyTransferProof`/`verifyBurnProof` precompiles) by which those users can ever redeem their shielded notes back to a transparent, spendable TRC20 balance — the `PrecompiledContracts.getContractForAddress` call returns `null` for those addresses once the flag is off, so any TVM call to the shielded contract's transfer/burn entry points that reach those fixed precompile addresses will fail. There is no per-user "owed"/claimable ledger recorded on mint that would let the tokens be recovered through an alternate path if the switch is re-enabled improperly, or if it is never re-enabled.

This mirrors the reported analog exactly: an unprivileged "deposit" (mint) commits assets into a pool, and a later privileged "pause" of the counterpart action (transfer/burn) — done without regard to outstanding balances or without a queued retry mechanism — makes those specific users' funds permanently unreachable through the intended user flow.

### Impact Explanation
Any TRC20 tokens that unprivileged users have minted into the shielded pool become permanently locked in the shielded contract with no path to transparent withdrawal if `ALLOW_SHIELDED_TRC20_TRANSACTION` is set to `0` while notes are outstanding. This is a permanent freezing-of-funds condition affecting ordinary users' assets, not just node metadata — qualifying as at least Medium/High impact under the frozen-funds category.

### Likelihood Explanation
The trigger requires a committee-approved proposal (governance action), which is a normal, expected chain operation rather than an attack by a single unprivileged actor; however, the vulnerable condition (users minting funds) is fully reachable and routine for any unprivileged TRC20 holder, and the validator code contains no safeguard preventing the toggle while pool balances are non-zero. The likelihood of this occurring accidentally (e.g., a well-intentioned governance decision to disable an underused/risky feature) is non-trivial given there's no guard rail in `ProposalUtil` or `ProposalService` against it.

### Recommendation
- Track an on-chain "shielded pool outstanding balance" (or per-note claimable ledger) and prevent `ALLOW_SHIELDED_TRC20_TRANSACTION` from being set to `0` while any shielded value remains locked, similar to how other toggles check preconditions in `ProposalUtil.validator`.
- Alternatively, split the flag into independent mint-enable and transfer/burn-enable switches, ensuring the withdrawal path (`verifyTransferProof`/`verifyBurnProof`/`merkleHash`) can never be disabled while `verifyMintProof` was ever enabled and used, or ensure withdrawal precompiles remain callable independent of the flag once minted value exists.
- Add an explicit recovery/retry mechanism (analogous to `OmniGasPump.owed`) so that if the withdrawal precompiles are ever disabled, an emergency path still allows redemption of already-minted balances.

### Proof of Concept
1. Committee approves `ALLOW_SHIELDED_TRC20_TRANSACTION = 1` (as done today for TRC20 shielded pools).
2. An unprivileged user calls the shielded TRC20 contract's `mint`, which triggers the `verifyMintProof` precompile (`PrecompiledContracts.getContractForAddress`) — succeeds because `VMConfig.allowShieldedTRC20Transaction()` is true — locking the user's transparent TRC20 balance in the shielded contract and issuing them a shielded note.
3. Committee later approves a proposal setting `ALLOW_SHIELDED_TRC20_TRANSACTION = 0` — allowed unconditionally by `ProposalUtil.validator` (`case ALLOW_SHIELDED_TRC20_TRANSACTION`) and applied unconditionally by `ProposalService.process`.
4. The user attempts to redeem their shielded note via `transfer`/`burn`; the shielded contract calls `verifyTransferProof`/`verifyBurnProof` at their fixed precompile addresses, but `PrecompiledContracts.getContractForAddress` now returns `null` for those addresses (guarded by `VMConfig.allowShieldedTRC20Transaction()` which is now false), so the call cannot succeed.
5. The user's TRC20 tokens remain locked in the shielded contract indefinitely, with no way to complete the transfer/burn to reclaim a transparent balance.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/PrecompiledContracts.java (L260-271)
```java
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyMintProofAddr)) {
      return verifyMintProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyTransferProofAddr)) {
      return verifyTransferProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(verifyBurnProofAddr)) {
      return verifyBurnProof;
    }
    if (VMConfig.allowShieldedTRC20Transaction() && address.equals(merkleHashAddr)) {
      return merkleHash;
    }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L347-357)
```java
      case ALLOW_SHIELDED_TRC20_TRANSACTION: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_0_1)) {
          throw new ContractValidateException(
              "Bad chain parameter id [ALLOW_SHIELDED_TRC20_TRANSACTION]");
        }
        if (value != 1 && value != 0) {
          throw new ContractValidateException(
              "This value[ALLOW_SHIELDED_TRC20_TRANSACTION] is only allowed to be 1 or 0");
        }
        break;
      }
```

**File:** framework/src/main/java/org/tron/core/consensus/ProposalService.java (L216-219)
```java
        case ALLOW_SHIELDED_TRC20_TRANSACTION: {
          manager.getDynamicPropertiesStore().saveAllowShieldedTRC20Transaction(entry.getValue());
          break;
        }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L2049-2082)
```java
  public void saveAllowShieldedTRC20Transaction(long allowShieldedTRC20Transaction) {
    this.put(DynamicPropertiesStore.ALLOW_SHIELDED_TRC20_TRANSACTION,
        new BytesCapsule(ByteArray.fromLong(allowShieldedTRC20Transaction)));
  }

  public long getAllowShieldedTRC20Transaction() {
    String msg = "not found ALLOW_SHIELDED_TRC20_TRANSACTION";
    return Optional.ofNullable(getUnchecked(ALLOW_SHIELDED_TRC20_TRANSACTION))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElseThrow(
            () -> new IllegalArgumentException(msg));
  }

  public void saveAllowTvmIstanbul(long allowTVMIstanbul) {
    this.put(DynamicPropertiesStore.ALLOW_TVM_ISTANBUL,
        new BytesCapsule(ByteArray.fromLong(allowTVMIstanbul)));
  }

  public long getAllowTvmIstanbul() {
    String msg = "not found ALLOW_TVM_ISTANBUL";
    return Optional.ofNullable(getUnchecked(ALLOW_TVM_ISTANBUL))
        .map(BytesCapsule::getData)
        .map(ByteArray::toLong)
        .orElseThrow(
            () -> new IllegalArgumentException(msg));
  }

  public boolean supportShieldedTransaction() {
    return getAllowShieldedTransaction() == 1L;
  }

  public boolean supportShieldedTRC20Transaction() {
    return getAllowShieldedTRC20Transaction() == 1L;
```
