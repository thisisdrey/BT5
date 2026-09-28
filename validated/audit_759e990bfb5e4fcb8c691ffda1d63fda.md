### Title
Dust `DepositToken` transfers let an unprivileged attacker fill a victim's per-account token list and revert all of the victim's subsequent deposits/operations - (File: contracts/Pool.sol)

### Summary
Metronome tracks every distinct `DepositToken`/`DebtToken` an account interacts with in `Pool` using `MappedEnumerableSet.AddressSet` and enforces a `MAX_TOKENS_PER_USER` cap (present in `contracts/Pool.sol` and `contracts/storage/PoolStorage.sol`). The underlying `MappedEnumerableSet._add` pushes a new entry to the account's array unconditionally whenever the token is not already present (`contracts/lib/MappedEnumerableSet.sol:22-32`). Because `DepositToken.transfer`/`transferFrom` are public, permissionless ERC-20 paths, any EOA can send dust amounts of each pool deposit token to a victim address, registering up to `MAX_TOKENS_PER_USER` entries in the victim's set. Once the list is saturated, the cap check in `Pool` reverts, blocking the victim from depositing new collateral types — a crafted-input DoS analogous to CVE-2025-71011 (missing input validation leading to denial of service).

### Finding Description
- `MappedEnumerableSet._add` appends `value` to `set._ofAddress[key]._values` with no size bound; the bound lives only in the `Pool` cap check (`MAX_TOKENS_PER_USER`).
- `DepositToken` share transfers go through public `transfer`/`transferFrom`, which route through the pool's account-token bookkeeping (the same accounting used for `depositOf`/`debtPositionOf` enumeration). A transfer to a fresh recipient adds that deposit token to the recipient's set.
- Attack: attacker obtains (or deposits to mint) a dust amount of every deposit token registered in the pool, then `transfer(victim, 1 wei)` for each. Each transfer adds a new entry to `depositTokensOf[victim]`. After `MAX_TOKENS_PER_USER` distinct tokens are registered, any action that tries to register yet another token for the victim reverts.
- The victim cannot deposit any additional collateral type through `DepositToken.deposit`/`mint` or `Pool` flows (nor through `SmartFarmingManager.leverage`/`flashRepay`, which call `depositToken_`/`pool` internally) while the list is saturated. Depending on whether the pool removes entries when an account's balance returns to zero, the victim either must locate and zero out the dust entries (temporary freezing of the ability to add collateral) or is permanently blocked; if entries are not removable, an account holding an open debt position that needs new collateral to stay healthy can be pushed into liquidation.

### Impact Explanation
This is a user-level denial of service produced by crafted inputs (dust transfers), matching the bug class of the OneFlow advisory (insufficient input validation → DoS). Accepted impact category: temporary — and, if set entries cannot be removed while dust balances exist, permanent — freezing of the victim's ability to open new collateral positions. For an underwater-adjacent borrower, blocking the deposit of a new collateral type can force liquidation of the existing position (indirect loss of funds). It is not a gas/unbounded-loop DoS: the failure is a hard revert on the cap check.

### Likelihood Explanation
- Attacker requirements: an EOA holding at least 1 unit of each of the pool's deposit tokens. Cost is bounded by the dust value of the listed collateral tokens — low on chains with cheap collaterals.
- No privileged role, oracle manipulation, or governance action is needed; `DepositToken.transfer` is callable by anyone and the recipient does not need to opt in (standard ERC-20 has no receiver consent).
- Constraints: the attack is scoped per-pool (only tokens registered in that pool count toward the cap), and its effectiveness depends on whether `Pool` removes a token from the account set when the balance reaches zero — if removal exists, the victim can self-heal by transferring dust away, downgrading impact to temporary freezing. I was unable to read the exact cap-check and removal lines in `contracts/Pool.sol`/`DepositToken.sol` within this session, so the permanence and the precise revert site should be confirmed on-chain; the set-add primitive (`MappedEnumerableSet.sol:22-32`) and the existence of `MAX_TOKENS_PER_USER` in `Pool.sol`/`PoolStorage.sol` are confirmed in the repo and in the deployment ABIs.

### Recommendation
- Only register a token in an account's `MappedEnumerableSet` set via the protocol's own deposit/mint paths, or skip set insertion for transfers below a minimum meaningful amount.
- Ensure removal from the set on zero balance is always performed so a victim can clear forced entries by transferring dust out; alternatively drop the per-account cap and avoid iterating/`MAX_TOKENS_PER_USER`-bounded accounting entirely.
- Do not let a third-party transfer mutate another account's position-tracking state in a way that gates that account's future deposits.

### Proof of Concept
Reproducible as a Hardhat fork test against a live deployment (e.g., Optimism `Pool` at `deployments/optimism/Pool.json`):

```ts
// hardhat fork of optimism; alice = victim, attacker = fresh EOA
const pool = await ethers.getContractAt('Pool', POOL_ADDRESS);
const depositTokens: IDepositToken[] = await getAllDepositTokens(pool); // pool.getDepositTokens()

// 1) attacker mints/holds 1 wei of every deposit token
for (const dt of depositTokens) {
  await depositUnderlying(attacker, dt, 1);        // or deposit(1) via dt.deposit
}

// 2) attacker pushes dust into victim's account set
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(alice.address, 1);
}
expect(await pool.getDepositTokensOf(alice.address)).to.have.length(MAX_TOKENS_PER_USER);

// 3) victim cannot deposit a new collateral type
const newToken = depositTokens.find(dt => /* not yet in victim set */);
await expect(
  newToken.connect(alice).deposit(1)               // or Pool deposit flow
).revertedWithCustomError(pool, 'MaxTokensPerUserReached');
```

Steps 1-2 only use public `transfer`/`deposit` entry points with attacker-chosen dust amounts; step 3 demonstrates the DoS on the victim's deposit path. If `Pool` removes zero-balance entries, extend the PoC to show the victim can transfer dust out to recover (temporary freezing); otherwise the freeze is permanent for that account.