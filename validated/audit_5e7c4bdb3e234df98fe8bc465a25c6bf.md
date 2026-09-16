### Title
Unbounded `toAccounts`/`fromAccounts` list load in `DelegatedResourceAccountIndexCapsule` allows resource-delegation DoS - ([File: actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java])

### Summary
`FreezeBalanceActuator.delegateResource()` maintains a legacy `DelegatedResourceAccountIndexCapsule` per owner/receiver account that stores the full list of delegated counterparties as a single protobuf-backed list (`toAccounts`/`fromAccounts`). On every `FreezeBalanceContract` transaction that delegates bandwidth/energy to a new receiver (when `supportAllowDelegateOptimization` is not enabled), the actuator loads the *entire* existing index list into memory via `getToAccountsList()`/`getFromAccountsList()`, performs an `O(n)` `List.contains()` scan, and then rewrites the whole record with `addToAccount`/`addFromAccount`, persisting the full growing list back to storage.

### Finding Description
In `actuator/src/main/java/org/tron/core/actuator/FreezeBalanceActuator.java` (`delegateResource`, lines ~319-345), the code does:
```java
List<ByteString> toAccountsList = ownerIndexCapsule.getToAccountsList();
if (!toAccountsList.contains(ByteString.copyFrom(receiverAddress))) {
    ownerIndexCapsule.addToAccount(ByteString.copyFrom(receiverAddress));
}
```
and symmetrically for `fromAccountsList`/`addFromAccount`. This mirrors the reported bug class exactly: instead of checking/updating storage incrementally or bounding the number of distinct counterparties, the full list is copied out of the capsule (`getToAccountsList()` returns the underlying protobuf repeated field, effectively the entire array) into memory and linearly scanned on every transaction. Nothing in `validate()` bounds how many distinct receiver addresses a single owner account may delegate to over time, so this list can grow without limit as an attacker repeatedly issues `FreezeBalanceContract` (or `DelegateResource`-adjacent legacy) transactions to new, unique receiver addresses from the same owner account.

Each subsequent delegation transaction from that owner (or to that owner as a receiver) must deserialize, copy, and linearly scan the entire accumulated list (`getWithPrefix`/`getIndex` in `DelegatedResourceAccountIndexStore` similarly rebuilds and sorts the full list from a prefix-scan), meaning per-transaction cost grows linearly with the attacker's own prior transaction count. This is also exposed read-side via the `getDelegatedResourceAccountIndex`/`getDelegatedResourceAccountIndexV2` Wallet API/HTTP/gRPC endpoints (`GetDelegatedResourceAccountIndexServlet`, `RpcApiService`), which call `DelegatedResourceAccountIndexStore.getIndex()`/`getV2Index()` and fully materialize and sort the prefix-scanned list on every query — an unauthenticated query can be aimed at an address the attacker previously bloated, causing repeated large in-memory copy/sort work on the node servicing the API.

### Impact Explanation
Unlike EVM contract execution, these legacy resource-delegation actuators are not metered by energy — their cost is charged only via flat bandwidth/fee, independent of the size of state they touch. An attacker who cheaply and repeatedly delegates small resource amounts to many unique receiver addresses can inflate the `toAccounts`/`fromAccounts` list for their own account indefinitely. Later transactions/queries touching that account incur growing CPU/memory cost during block application (in `Manager`), potentially slowing down or stalling transaction processing/block validation for other transactions in the same block, and burdening full nodes servicing the corresponding read-only API. This matches the report's "DOS" impact class: cheap, attacker-triggerable growth of storage causes disproportionate resource consumption for both actuator execution and query paths.

### Likelihood Explanation
Reaching this path requires only ordinary, unprivileged `FreezeBalanceContract` transactions (broadcastable by any account) targeting many distinct new receiver addresses, or exploiting the legacy `getIndex()`/`getV2Index()` HTTP/gRPC endpoints. However, the affected legacy branch is gated behind `!dynamicPropertiesStore.supportAllowDelegateOptimization()`, a chain parameter that mainnet networks may already have permanently enabled, in which case the newer V2 path (`delegatedResourceAccountIndexStore.delegate/convert`, prefix-keyed per-pair records) is used instead and this specific list-growth vector would not apply on such networks. Confirmation of the current on-chain value of `supportAllowDelegateOptimization` (and whether any network still runs with it disabled) could not be verified from the indexed code alone.

### Recommendation
Avoid representing an account's delegation counterparties as a single unbounded list attached to one storage record. Store each owner→receiver (and receiver→owner) delegation relationship as its own keyed record (as already done for the V2 `delegate()`/`unDelegateV2()` paths), and cap or paginate any legacy full-list read/scan operations (`getWithPrefix`, `getIndex`) so a single account cannot force unbounded in-memory materialization on write or on read.

### Proof of Concept
1. Ensure `supportAllowDelegateOptimization` is not enabled on the target network (legacy path active).
2. From a funded account `A`, repeatedly submit `FreezeBalanceContract` transactions with unique `receiverAddress` values (`R1, R2, ..., Rn`), each delegating a minimal frozen balance for bandwidth/energy.
3. Each transaction triggers `FreezeBalanceActuator.delegateResource()`, which loads and grows `A`'s `DelegatedResourceAccountIndexCapsule.toAccountsList` (and each `Ri`'s `fromAccountsList`) by one entry, with a full list copy + `contains()` scan each time.
4. As `n` grows large, subsequent delegate/undelegate transactions from `A` (or to any `Ri`) become progressively more expensive to execute, and querying `wallet.getDelegatedResourceAccountIndex(A)` via HTTP/gRPC forces the node to prefix-scan, copy, and sort the entire accumulated list on every call, degrading node responsiveness for that query path.