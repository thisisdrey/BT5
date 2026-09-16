### Title
Global Free-Bandwidth Pool (`PUBLIC_NET_LIMIT`) Can Be Exhausted by Any Unprivileged Sender, Denying Free Transactions to All Zero-Balance Accounts for a Day - (File: `chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java`)

### Summary
Like the `Teleportation` contract's `maxTransferAmountPerDay`, java-tron maintains a single, chain-wide, time-windowed quota — `PUBLIC_NET_LIMIT` — that is shared by *every* account on the network for free bandwidth. Any unprivileged transaction broadcaster can consume this shared pool at essentially zero cost (ordinary signed transactions), and once it is depleted, every other account that relies on the free-bandwidth path (typically brand-new or zero-TRX accounts) can no longer get their transactions processed for free until the next window resets — matching the report's "DoS attack can exhaust a shared daily allowance and prevent other users from performing the action" bug class.

### Finding Description
`BandwidthProcessor.consume()` resolves bandwidth cost for a transaction by trying, in order: account-owned net, asset-owner free net, then a global free path via `useFreeNet()`, and only then `useTransactionFee()` (burning TRX) [1](#0-0) .

`useFreeNet()` checks the account's own `freeNetLimit`, and then checks and increments a **single, global** counter, `publicNetUsage`, against `publicNetLimit`, which resets on a rolling window basis via `increase(...)`: [2](#0-1) 

This global `PUBLIC_NET_LIMIT`/`PUBLIC_NET_USAGE`/`PUBLIC_NET_TIME` state is stored and mutated directly in `DynamicPropertiesStore`, with no per-account cap on how much of the shared pool a given account can occupy beyond its own small `freeNetLimit` (default `5000`) per call, but there is nothing to prevent many independent accounts (which any actor can create/control) from each repeatedly consuming their own `freeNetLimit` share until the shared `publicNetLimit` (initialized to `14_400_000_000L`) is driven to zero within the window: [3](#0-2) 

The constants are defined in `DynamicResourceProperties`: [4](#0-3) 

Once `publicNetUsage` reaches `publicNetLimit`, `useFreeNet()` returns `false` for every account for the remainder of the window (`BandwidthProcessor.java:526-531`). Execution then falls through to `useTransactionFee()`, which requires the sender to burn TRX from their balance. Any account that has zero TRX balance (e.g., a freshly created account intended to rely solely on free bandwidth) is left unable to broadcast any transaction until the window resets, exactly mirroring Teleportation's "attacker exhausts the shared daily allowance, blocking everyone else" scenario, and this is reachable purely by an unprivileged transaction broadcaster issuing ordinary signed transactions — no special privilege, contract deployment, or witness/SR role is required.

### Impact Explanation
This denies use of the free-bandwidth path network-wide for all zero/low-balance accounts for the remainder of the reset window, which can be leveraged to censor specific classes of participants (e.g., new users, faucet-funded accounts, dApp onboarding flows) from transacting without paying TRX, a broad availability/DoS impact on an API/feature the node is expected to serve for all users.

### Likelihood Explanation
The attack requires only ordinary signed transactions from accounts the attacker controls; each transaction is cheap (or free, since it's drawn from the attacker's own `freeNetLimit`), and the aggregate `publicNetLimit` is a fixed, publicly known chain parameter, making the cost to exhaust it bounded and computable by any actor with access to enough accounts (which are free to create).

### Recommendation
Consider apportioning or rate-limiting the shared `PUBLIC_NET_LIMIT` per unique account/IP/time-window (rather than a single global bucket drained first-come-first-served), or removing the hard block-when-empty behavior in `useFreeNet()` in favor of a decaying/priced fallback so that exhausting the shared pool degrades service gracefully instead of fully blocking zero-balance accounts.

### Proof of Concept
1. Attacker controls (or cheaply creates) N accounts, each entitled to `freeNetLimit` (default `5000` bytes) of free bandwidth per day.
2. Attacker issues transactions from each account back-to-back; each call goes through `BandwidthProcessor.consume()` → `useFreeNet()`, incrementing the shared `publicNetUsage` counter via `chainBaseManager.getDynamicPropertiesStore().savePublicNetUsage(...)` [5](#0-4) .
3. Once cumulative usage across all N accounts reaches `publicNetLimit`, every other honest zero-balance account's call to `useFreeNet()` returns `false` at the check `bytes > (publicNetLimit - newPublicNetUsage)` [6](#0-5) , forcing fallback to `useTransactionFee()`, which fails for any account without sufficient TRX balance, per the final `AccountResourceInsufficientException` thrown in `consume()` [7](#0-6) .

### Citations

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L160-176)
```java
      if (useAccountNet(accountCapsule, bytesSize, now)) {
        continue;
      }

      if (useFreeNet(accountCapsule, bytesSize, now)) {
        continue;
      }

      if (useTransactionFee(accountCapsule, bytesSize, trace)) {
        continue;
      }

      long fee = chainBaseManager.getDynamicPropertiesStore().getTransactionFee() * bytesSize;
      throw new AccountResourceInsufficientException(
          String.format(
              "account [%s] has insufficient bandwidth[%d] and balance[%d] to create new account",
              StringUtil.encode58Check(address), bytesSize, fee));
```

**File:** chainbase/src/main/java/org/tron/core/db/BandwidthProcessor.java (L506-546)
```java
  private boolean useFreeNet(AccountCapsule accountCapsule, long bytes, long now) {

    long freeNetLimit = chainBaseManager.getDynamicPropertiesStore().getFreeNetLimit();
    long freeNetUsage = accountCapsule.getFreeNetUsage();
    long latestConsumeFreeTime = accountCapsule.getLatestConsumeFreeTime();
    long newFreeNetUsage = increase(freeNetUsage, 0, latestConsumeFreeTime, now);

    if (bytes > (freeNetLimit - newFreeNetUsage)) {
      logger.debug("Free net usage is running out."
              + " Bytes: {}, freeNetLimit: {}, newFreeNetUsage: {}.",
          bytes, freeNetLimit, newFreeNetUsage);
      return false;
    }

    long publicNetLimit = chainBaseManager.getDynamicPropertiesStore().getPublicNetLimit();
    long publicNetUsage = chainBaseManager.getDynamicPropertiesStore().getPublicNetUsage();
    long publicNetTime = chainBaseManager.getDynamicPropertiesStore().getPublicNetTime();

    long newPublicNetUsage = increase(publicNetUsage, 0, publicNetTime, now);

    if (bytes > (publicNetLimit - newPublicNetUsage)) {
      logger.debug("Free public net usage is running out."
              + " Bytes: {}, publicNetLimit: {}, newPublicNetUsage: {}.",
          bytes, publicNetLimit, newPublicNetUsage);
      return false;
    }

    latestConsumeFreeTime = now;
    long latestOperationTime = chainBaseManager.getHeadBlockTimeStamp();
    publicNetTime = now;
    newFreeNetUsage = increase(newFreeNetUsage, bytes, latestConsumeFreeTime, now);
    newPublicNetUsage = increase(newPublicNetUsage, bytes, publicNetTime, now);
    accountCapsule.setFreeNetUsage(newFreeNetUsage);
    accountCapsule.setLatestConsumeFreeTime(latestConsumeFreeTime);
    accountCapsule.setLatestOperationTime(latestOperationTime);

    chainBaseManager.getDynamicPropertiesStore().savePublicNetUsage(newPublicNetUsage);
    chainBaseManager.getDynamicPropertiesStore().savePublicNetTime(publicNetTime);
    chainBaseManager.getAccountStore().put(accountCapsule.createDbKey(), accountCapsule);
    return true;

```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L405-421)
```java
    try {
      this.getPublicNetUsage();
    } catch (IllegalArgumentException e) {
      this.savePublicNetUsage(0L);
    }

    try {
      this.getOneDayNetLimit();
    } catch (IllegalArgumentException e) {
      this.saveOneDayNetLimit(57_600_000_000L);
    }

    try {
      this.getPublicNetLimit();
    } catch (IllegalArgumentException e) {
      this.savePublicNetLimit(14_400_000_000L);
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/DynamicPropertiesStore.java (L3088-3097)
```java
    private static final byte[] ONE_DAY_NET_LIMIT = "ONE_DAY_NET_LIMIT".getBytes();
    //public free bandwidth
    private static final byte[] PUBLIC_NET_USAGE = "PUBLIC_NET_USAGE".getBytes();
    //fixed
    private static final byte[] PUBLIC_NET_LIMIT = "PUBLIC_NET_LIMIT".getBytes();
    private static final byte[] PUBLIC_NET_TIME = "PUBLIC_NET_TIME".getBytes();
    private static final byte[] FREE_NET_LIMIT = "FREE_NET_LIMIT".getBytes();
    private static final byte[] TOTAL_NET_WEIGHT = "TOTAL_NET_WEIGHT".getBytes();
    //ONE_DAY_NET_LIMIT - PUBLIC_NET_LIMIT，current TOTAL_NET_LIMIT
    private static final byte[] TOTAL_NET_LIMIT = "TOTAL_NET_LIMIT".getBytes();
```
