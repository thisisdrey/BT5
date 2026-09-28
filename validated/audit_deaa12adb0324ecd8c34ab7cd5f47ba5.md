### Title
Dust transfers of whitelisted DepositTokens fill a victim's `MAX_TOKENS_PER_USER` slot list, blocking collateral top-ups and liquidations - (File: contracts/DepositToken.sol)

### Summary
The bug class of the reference advisory is **improper input validation leading to attacker-triggered denial of service** (crafted inputs silently expand a resource — there, symbol-key conversion; here, a per-account token list). The Metronome analog: `DepositToken._transfer` unconditionally calls `Pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero, and `Pool` reverts with `UserReachedMaxTokens` once an account holds `MAX_TOKENS_PER_USER = 30` deposit+debt token entries. Any EOA can therefore dust-transfer wei-amounts of every whitelisted collateral token to a victim, permanently filling the victim's 30 slots so that *any* first-time deposit, transfer-in, liquidation seizure, or smart-farming mint to that account reverts.

### Finding Description
- `DepositToken._transfer` adds the token to the recipient's tracked set on first receipt: `if (_recipientBalanceBefore == 0 && amount_ > 0) { pool.addToDepositTokensOfAccount(recipient_); }` — `contracts/DepositToken.sol:518-520`. There is no minimum-amount check, so `amount_ = 1` wei suffices.
- The same hook fires on mints: `_mint` calls `pool.addToDepositTokensOfAccount(account_)` at `contracts/DepositToken.sol:486-488`, which is reached from the public `deposit(amount_, onBehalfOf_)` at `contracts/DepositToken.sol:211-237` and from `Pool.liquidate` via `seize` → `_transfer` (`contracts/DepositToken.sol:343-345`).
- `Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30) — `contracts/Pool.sol:143-148, 216-220`.
- Attack path (unprivileged EOA, no privileged roles):
  1. For each of N whitelisted `DepositToken`s in the victim's pool, the attacker calls `deposit(2, attacker)` to obtain dust shares (2 wei of underlying, or the smallest amount that mints ≥1 share), then `transfer(victim, 1)` — each transfer pushes that token into the victim's `depositTokensOfAccount` set.
  2. Once `depositTokensOfAccount[victim]` + `debtTokensOfAccount[victim]` reaches 30, every subsequent first-receipt reverts: `deposit(..., onBehalfOf_ = victim)` for a new collateral, `transfer(victim, ...)`, `seize` to victim, and `SmartFarmingManager` leverage mints all fail.
  3. Entries are only removed when the victim's balance hits zero (`_burn`/`_transfer` at `contracts/DepositToken.sol:460-462, 523-525`), so the victim can shed tokens by transferring the dust away — but the attacker can refill slots in the same or a subsequent transaction (front-running/back-running), making the DoS effectively indefinite for as long as the attacker wishes.

### Impact Explanation
- **Temporary freezing of funds / forced liquidation**: A borrower whose position approaches the liquidation threshold normally tops up collateral to restore health. With all 30 slots filled and the attacked collateral not already in the victim's set, every attempt to deposit a *new* collateral type reverts with `UserReachedMaxTokens`. The victim cannot improve `debtPositionOf` via fresh collateral and is pushed into liquidation, suffering the liquidation fee and collateral seizure — a concrete, attacker-caused loss triggered by unvalidated dust input, directly paralleling the reference DoS-by-crafted-input class.
- **Liveness for other users**: `seize(from_, to_, ...)` transfers to the liquidator's `to_` address; the attacker can likewise fill the slots of known liquidation bots/keepers' receiving addresses (EOAs), causing their liquidation transactions to revert unless they keep a sacrificial slot.
- The per-account list cap exists precisely to bound `debtOf`/`depositOf` loops (`contracts/Pool.sol:227-237, 274-288`); the flaw is that anyone — not the account owner — can consume those bounded slots with 1-wei transfers.

### Likelihood Explanation
- Requires only: the pool to list enough deposit tokens to cover `30 - len(victim's existing tokens)` (pools with many collaterals make this cheap), plus dust amounts of each underlying — capital cost is negligible.
- No privileged role, no oracle manipulation, no flash loan needed; `transfer` is a public ERC20 entry point with no amount minimum and no recipient opt-in.
- Mitigating factors: victims with debt can still `repay` via the DebtToken (repay burns debt tokens, which does not require adding a deposit token), and victims can self-clean slots by transferring dust out — but both can be countered (attacker refills after each cleanup; repay requires the victim to hold/buy the synthetic asset). The DoS reliably exists for at least one transaction window, which is all that is needed to block a rescue deposit and let a liquidation execute.

### Recommendation
- Do not add a token to `depositTokensOfAccount` on plain `transfer`/`seize` — track collateral contributions only on `deposit`/`_mint` (positions actually opened), or add the recipient entry inside `deposit` rather than inside the generic `_transfer` path.
- Alternatively, enforce a minimum share amount (e.g., a USD-denominated dust floor) before calling `addToDepositTokensOfAccount`, or let recipients remove unsolicited entries without zeroing via a `pool.removeFromDepositTokensOfAccount` escape callable by the account itself.
- If entries must be added on transfer, exempt `seize`/liquidation flows so liquidations cannot be DoS'd.

### Proof of Concept
```solidity
// Hardhat fork test outline (pool with >= N whitelisted DepositTokens)
// Assumes: victim already holds 2 deposit tokens; pool lists >= 29 deposit tokens.
const MAX = await pool.MAX_TOKENS_PER_USER(); // 30
const dtokens = await pool.getDepositTokens(); // whitelisted deposit tokens

// Attacker acquires dust shares of each collateral and gifts 1 wei to victim
for (let i = 0; i < MAX - 2; i++) {
  const dt = await ethers.getContractAt("DepositToken", dtokens[i]);
  const underlying = await ethers.getContractAt("IERC20", await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 2);
  await dt.connect(attacker).deposit(2, attacker.address);        // mint dust to attacker
  await dt.connect(attacker).transfer(victim.address, 1);         // occupies one victim slot
}

// victim's account list is now full
expect((await pool.getDepositTokensOfAccount(victim.address)).length).to.eq(MAX - victimDebts);

// Attacker-owned collateral type NOT yet in victim's set:
const newDt = await ethers.getContractAt("DepositToken", dtokens[MAX - 2]);
const newUnderlying = await ethers.getContractAt("IERC20", await newDt.underlying());
await newUnderlying.connect(victim).approve(newDt.address, amount);
await expect(newDt.connect(victim).deposit(amount, victim.address))
  .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");   // victim cannot top up collateral

// Victim transfers the dust out to free a slot; attacker re-gifts 1 wei next block -> DoS repeatable
```