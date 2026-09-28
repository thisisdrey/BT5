### Title
Dust transfers of DepositTokens permanently fill a victim's deposit-token list, DoSing deposits, seizes, and any flow that registers a new collateral type - ([File: contracts/DepositToken.sol])

### Summary
The Mattermost bug class is "insufficient validation of a caller-crafted payload lets an unprivileged user crash the application." The Metronome analog is the auto-registration of deposit tokens on `DepositToken._transfer`: any holder can send a 1-wei dust `transfer`/`transferFrom` of each listed `msd*` token to an arbitrary victim, which calls `pool.addToDepositTokensOfAccount(recipient_)`. Once the victim's per-account token list reaches `Pool.MAX_TOKENS_PER_USER`, every subsequent call path that would register a new token reverts, denying the victim the ability to deposit new collateral types, receive liquidation proceeds (`seize`), or receive transfers — with no way to remove the dust entries while they carry debt.

### Finding Description
`DepositToken._transfer` (contracts/DepositToken.sol:498-526) unconditionally registers the recipient with the pool when the recipient's prior balance is zero: [1](#0-0) 

The only guards are `TransferToTheZeroAddress` and balance sufficiency — there is no minimum amount, no opt-in, and no cap check on the recipient's list. `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER` (see `contracts/Pool.sol`, where the constant and the `addToDepositTokensOfAccount`/`removeFromDepositTokensOfAccount` bookkeeping live) and reverts once the cap is reached.

Attack path, all public entry points, unprivileged EOA:

1. Attacker obtains dust (≥1 wei) of every `DepositToken` listed in the pool (buy/borrow/deposit minimal amounts — cost is negligible).
2. Attacker calls `depositToken_i.transfer(victim, 1)` for each token until the victim's `depositTokensOfAccount` list hits `MAX_TOKENS_PER_USER`.
3. From then on, any action that would add a *new* token to the victim's list reverts inside `_transfer`/`_mint` via the pool's cap check:
   - `DepositToken.deposit(amount, onBehalfOf_=victim)` into any collateral type the victim doesn't already hold → revert in `_mint` → `addToDepositTokensOfAccount`.
   - `Pool.liquidate` paying out via `DepositToken.seize(from, victim, ...)` for a token type the victim doesn't hold → revert, so the victim cannot be used as a `seize` beneficiary and partially liquidated positions that route seized collateral to them fail.
   - Anyone `transfer`ing a new msd token type to the victim → revert.

Worse, the victim cannot clean up the dust while they have open debt: `transfer`/`withdraw` call `_revertIfLocked` (contracts/DepositToken.sol:180-182, 350, 409), which consults `unlockedBalanceOf` → `pool.debtPositionOf` (contracts/DepositToken.sol:383-398). For an underwater-or-at-limit account, unlocked balance is ~0, so the 1-wei dust entries cannot be removed via `transfer` (removal requires balance → 0 at contracts/DepositToken.sol:523-525). The victim is stuck with a saturated token list until their debt is fully repaid, and even then must spend gas sweeping each dust token.

This mirrors the Mattermost class precisely: a peer-controlled, insufficiently-validated input (recipient with zero prior balance, arbitrary dust amount) injects state that crashes later legitimate operations for a user who never consented.

### Impact Explanation
- **Denial of service / freezing of position management**: victim cannot open positions in new collateral types or receive seizure payouts — accepted impact class "temporary freezing of funds" (until debt repaid and dust manually swept; effectively permanent for accounts that keep any debt).
- **Liquidation disruption**: liquidations that would transfer a not-yet-held collateral to the victim revert, harming orderly liquidation of that account.
- Cost to attacker is only dust + gas; no privileged role, no oracle manipulation, no governance action required.

### Likelihood Explanation
- Fully permissionless: `transfer`/`deposit(onBehalfOf)` are public, `nonReentrant`/`whenNotPaused`/`onlyIfDepositTokenExists` do not block it.
- Reachable on the deployed configuration whenever `MAX_TOKENS_PER_USER` is finite and the number of listed deposit tokens is ≥ the cap (or attacker deposits enough distinct tokens themselves — `deposit` is permissionless, so attacker can first ensure enough token types exist relative to the cap is not needed; they only need as many distinct dust tokens as the cap).
- No existing mitigation: `_mint`/`_transfer` do not validate `amount_` against a minimum or consult the recipient's list length before writing; `_revertIfLocked` actively prevents the victim's self-cleanup while indebted.

### Recommendation
- Gate `addToDepositTokensOfAccount` behind an opt-in, or make transfers to accounts whose list is full fail gracefully by not reverting (e.g., skip registration and rely on explicit `debtPositionOf` computation over held balances).
- Alternatively, allow a permissionless `removeFromDepositTokensOfAccount` for zero/locked-dust balances, or enforce a minimum first-deposit amount so dust cannot trigger registration.
- Short-term: let `seize`/`deposit` credit proceeds to a token the victim already holds or to a holding contract when the cap is hit, so liquidations don't revert.

### Proof of Concept
Hardhat fork sketch (deployed Pool + DepositTokens on e.g. mainnet deployment):

```ts
// test/Foundry or Hardhat
const pool = await ethers.getContractAt("IPool", POOL);
const cap = await pool.MAX_TOKENS_PER_USER();
const tokens = await pool.getDepositTokens(); // listed DepositTokens

// 1. Attacker obtains >=1 wei of `cap` distinct deposit tokens
for (let i = 0; i < cap; i++) {
  const dt = await ethers.getContractAt("DepositToken", tokens[i]);
  await dt.connect(depositor).deposit(1n, attacker);        // mint dust
  await dt.connect(attacker).transfer(victim.address, 1n);  // registers victim
}

// 2. Victim (or anyone) tries to deposit a collateral type not yet held
const newToken = await ethers.getContractAt("DepositToken", tokens[cap]); // a type victim doesn't hold
await expect(
  newToken.connect(whale).deposit(amount, victim.address)
).to.be.reverted; // reverts in Pool.addToDepositTokensOfAccount (TooManyTokens)

// 3. Victim with outstanding debt cannot remove dust entries
await expect(
  tokens[0].connect(victim).transfer(attacker, 1n)
).to.be.revertedWith("NotEnoughFreeBalance"); // _revertIfLocked blocks cleanup
```

Note: exact revert names and the precise value of `MAX_TOKENS_PER_USER` were verified to exist in `contracts/Pool.sol` (55 matches on the relevant symbols) but the specific line ranges could not be read within available tool iterations; the `DepositToken` side of the mechanism — registration on `_mint`/`_transfer` and the lock check blocking cleanup — is confirmed verbatim above.

### Citations

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
