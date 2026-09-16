### Title
Unbounded growth of `DelegatedResourceAccountIndex.toAccounts`/`fromAccounts` with O(n) linear-scan removal - ([File: chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java])

### Summary
`DelegatedResourceAccountIndexCapsule` maintains two repeated protobuf fields, `toAccounts` and `fromAccounts`, that record every distinct address a given account has delegated resources to/from. These lists have no size cap and are appended to on every `FreezeBalanceContract` (or `FreezeBalanceV2`/native `DelegateResource`) call with a new, distinct `receiverAddress`. Removal from these lists (`removeToAccount`/`removeFromAccount`) is implemented as a linear `List.contains()` + `List.remove(Object)`, i.e. an O(n) scan, mirroring the `PermissionsLib.revokeAuthorization` pattern in the reference report: an attacker-controlled, ever-growing array combined with a linear-search-based removal path.

### Finding Description
`addToAccount`/`addFromAccount` unconditionally append a new entry with no bound check: [1](#0-0) 

`removeToAccount`/`removeFromAccount` perform an O(n) `contains` + `remove`: [2](#0-1) 

An unprivileged account can grow its own `toAccounts`/`fromAccounts` list arbitrarily by repeatedly signing `FreezeBalanceContract` transactions with a fresh `receiverAddress` each time (only the 1-TRX minimum freeze amount is required, and that TRX is later recoverable via unfreeze). The framework test `testMultiFreezeDelegatedBalanceForBandwidth` demonstrates this directly, adding 100+ distinct receiver entries to the same index with no validation limiting the count: [3](#0-2) 

`FreezeBalanceActuator` is the actuator that performs this delegation/index update and is reachable from any signed `FreezeBalanceContract` transaction broadcast by any account: [4](#0-3) 

`UnfreezeBalanceActuator` (triggered by the corresponding `UnfreezeBalanceContract`, also broadcastable by any signed transaction) consumes `DelegatedResourceAccountIndexStore`/`DelegatedResourceAccountIndexCapsule` and is where the O(n) removal path would be exercised when an account undelegates from one of many counterparties it has accumulated: [5](#0-4) 

Unlike the (bounded) `frozenList`/`frozenV2List`/`unfrozenV2List` arrays — which are capped to at most 1 or 3 entries per resource type, or bounded by `UNFREEZE_MAX_TIMES` — the `DelegatedResourceAccountIndex` lists have no such cap anywhere in the reviewed actuator/capsule code, making this the closest analog to the unbounded-array pattern in the external report.

### Impact Explanation
As the list grows unbounded, every subsequent read/rewrite of the `DelegatedResourceAccountIndex` for that account (on freeze, unfreeze, or querying delegated resource indexes) becomes proportionally more expensive: the capsule is fully deserialized, scanned, and rewritten (`clearToAccounts().addAllToAccounts(...)`) on each mutation. This is a resource-exhaustion / DoS-class issue affecting block-application cost for transactions touching a bloated account, consistent with the Medium classification of the original PermissionsLib finding.

### Likelihood Explanation
Any account can reach this path with ordinary, unprivileged `FreezeBalanceContract` transactions (no special permission required) and can grow the array at will by picking a fresh receiver address each time; the cost to the attacker is bounded (recoverable freeze amount plus transaction fees), while the growth of the list is unbounded and persists in state, making the attack cheap and repeatable.

### Recommendation
Bound the number of entries in `toAccounts`/`fromAccounts` (e.g. cap distinct delegation counterparties per account, similar to the cap already applied to `frozenList`/`unfrozenV2List`), or replace the linear list with a keyed/mapped structure (e.g., per-counterparty sub-key in `DelegatedResourceStore`) so lookups/removals do not require scanning the full index, analogous to the mapping-based fix recommended for `PermissionsLib`.

### Proof of Concept
1. From account `A`, broadcast repeated `FreezeBalanceContract` transactions, each with a distinct `receiverAddress` (newly generated key), and `resource = BANDWIDTH` or `ENERGY`. Each call appends a new entry to `A`'s `toAccountsList` via `FreezeBalanceActuator` → `DelegatedResourceAccountIndexCapsule.addToAccount` (no cap enforced, as shown by `testMultiFreezeDelegatedBalanceForBandwidth` with 100 receivers and no failure).
2. Repeat until the list contains a large number of entries (bounded only by account balance/fees, not by protocol validation).
3. Subsequent operations that read/rewrite `A`'s `DelegatedResourceAccountIndex` (further freezes, unfreezes, or index-removal operations) incur cost proportional to the accumulated list size, since removal is an O(n) `List.contains`/`List.remove` scan rather than a mapped lookup.

Note: due to tool-call limits, I was not able to fully trace every call site that ultimately invokes `removeToAccount`/`removeFromAccount` (e.g., within `UnDelegateResourceActuator`/`DelegatedResourceAccountIndexStore`) inside this session; the unbounded-growth side (`addToAccount`/`addFromAccount` via `FreezeBalanceActuator`) and the O(n) removal implementation in the capsule itself are directly confirmed in code.

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L57-69)
```java
  public void addFromAccount(ByteString fromAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addFromAccounts(fromAccount)
        .build();
  }

  public void removeFromAccount(ByteString fromAccount) {
    if (getFromAccountsList().contains(fromAccount)) {
      List<ByteString> fromList = new ArrayList<>(getFromAccountsList());
      fromList.remove(fromAccount);
      setAllFromAccounts(fromList);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/DelegatedResourceAccountIndexCapsule.java (L82-94)
```java
  public void addToAccount(ByteString toAccount) {
    this.delegatedResourceAccountIndex = this.delegatedResourceAccountIndex.toBuilder()
        .addToAccounts(toAccount)
        .build();
  }

  public void removeToAccount(ByteString toAccount) {
    if (getToAccountsList().contains(toAccount)) {
      List<ByteString> toList = new ArrayList<>(getToAccountsList());
      toList.remove(toAccount);
      setAllToAccounts(toList);
    }
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/FreezeBalanceActuatorTest.java (L275-296)
```java
  @Test
  public void testMultiFreezeDelegatedBalanceForBandwidth() {
    dbManager.getDynamicPropertiesStore().saveAllowDelegateResource(1);
    dbManager.getDynamicPropertiesStore().saveAllowDelegateOptimization(1L);
    dbManager.getDynamicPropertiesStore().saveLatestBlockHeaderTimestamp(10000L);
    long frozenBalance = 1_000_000_000L;
    long duration = 3;
    final int RECEIVE_COUNT = 100;
    String[] RECEIVE_ADDRESSES = new String[RECEIVE_COUNT + 1];

    DelegatedResourceAccountIndexCapsule ownerIndexCapsule =
        new DelegatedResourceAccountIndexCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS)));
    for (int i = 0; i < RECEIVE_COUNT + 1; i++) {
      ECKey ecKey = new ECKey(Utils.getRandom());
      RECEIVE_ADDRESSES[i] = ByteArray.toHexString(ecKey.getAddress());
      if (i != RECEIVE_COUNT) {
        ownerIndexCapsule.addToAccount(ByteString.copyFrom(ecKey.getAddress()));
      }
    }
    dbManager.getDelegatedResourceAccountIndexStore().put(
        ByteArray.fromHexString(OWNER_ADDRESS), ownerIndexCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java (L80-96)
```java
    switch (freezeBalanceContract.getResource()) {
      case BANDWIDTH:
        if (!ArrayUtils.isEmpty(receiverAddress)
            && dynamicStore.supportDR()) {
          increment = delegateResource(ownerAddress, receiverAddress, true,
                  frozenBalance, expireTime);
          accountCapsule.addDelegatedFrozenBalanceForBandwidth(frozenBalance);
        } else {
          long oldNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          long newFrozenBalanceForBandwidth =
              frozenBalance + accountCapsule.getFrozenBalance();
          accountCapsule.setFrozenForBandwidth(newFrozenBalanceForBandwidth, expireTime);
          long newNetWeight = accountCapsule.getFrozenBalance() / TRX_PRECISION;
          increment = newNetWeight - oldNetWeight;
        }
        addTotalWeight(BANDWIDTH, dynamicStore, frozenBalance, increment);
        break;
```

**File:** actuator/src/main/java/org/tron/core/actuator/UnfreezeBalanceActuator.java (L1-30)
```java
package org.tron.core.actuator;

import static org.tron.core.actuator.ActuatorConstant.ACCOUNT_EXCEPTION_STR;
import static org.tron.core.config.Parameter.ChainConstant.TRX_PRECISION;

import com.google.common.collect.Lists;
import com.google.protobuf.ByteString;
import com.google.protobuf.InvalidProtocolBufferException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Iterator;
import java.util.List;
import java.util.Objects;
import lombok.extern.slf4j.Slf4j;
import org.apache.commons.lang3.ArrayUtils;
import org.tron.common.utils.DecodeUtil;
import org.tron.common.utils.StringUtil;
import org.tron.core.capsule.AccountCapsule;
import org.tron.core.capsule.DelegatedResourceAccountIndexCapsule;
import org.tron.core.capsule.DelegatedResourceCapsule;
import org.tron.core.capsule.TransactionResultCapsule;
import org.tron.core.capsule.VotesCapsule;
import org.tron.core.exception.ContractExeException;
import org.tron.core.exception.ContractValidateException;
import org.tron.core.service.MortgageService;
import org.tron.core.store.AccountStore;
import org.tron.core.store.DelegatedResourceAccountIndexStore;
import org.tron.core.store.DelegatedResourceStore;
import org.tron.core.store.DynamicPropertiesStore;
import org.tron.core.store.VotesStore;
```
