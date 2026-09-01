import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = 'near/intents'
# todo: the name of the repository
REPO_NAME = 'intents'

run_number = os.environ.get('GITHUB_RUN_NUMBER', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    # =================================================================================
    # LENS: FROM A MESSAGE AN ORDINARY USER SIGNS OFF-CHAIN TO SOMEBODY ELSE'S TOKENS
    # LEAVING `intents.near`.
    # NEAR Intents is a custodial multi-token ledger ("the Verifier"): users deposit
    # NEP-141 / NEP-171 / NEP-245 assets, and every later move of those assets is
    # authorised by an off-chain signature over a `DefusePayload`. Untrusted bytes enter
    # through doors any unprivileged party fully controls: a `MultiPayload` handed to
    # `execute_intents` / `simulate_intents` by ANY caller (the signature is the only
    # authority - the predecessor is irrelevant), a `ft_on_transfer` / `nft_on_transfer` /
    # `mt_on_transfer` deposit `msg`, a direct `ft_withdraw`-family call from an account
    # that enabled `auth_by_predecessor_id`, and the return value of a receiver contract
    # the attacker deploys and names in `msg` / `AuthCall::contract_id` /
    # `NotifyOnTransfer` - which comes back into `*_resolve_*` callbacks.
    # Those bytes end in one place: the `token_balances` of accounts inside the Verifier,
    # and the real assets those balances are a claim on. A file belongs here only if an
    # authorisation, conservation, replay or settlement invariant must hold across it.
    # =================================================================================

    # -- The engine: the only thing between a signed blob and someone else's balance ------
    # `execute_signed_intent` verifies, binds a signer, commits a nonce and runs intents;
    # `Deltas`/`TransferMatcher` must net every `TokenDiff` back to zero in `finalize`.
    "contracts/defuse/core/src/engine/mod.rs",
    "contracts/defuse/core/src/engine/state/mod.rs",
    "contracts/defuse/core/src/engine/state/deltas.rs",
    "contracts/defuse/core/src/engine/state/cached.rs",
    "contracts/defuse/core/src/engine/inspector.rs",
    "contracts/defuse/core/src/intents/mod.rs",
    "contracts/defuse/core/src/intents/token_diff.rs",
    "contracts/defuse/core/src/intents/tokens.rs",
    "contracts/defuse/core/src/intents/account.rs",
    "contracts/defuse/core/src/intents/auth.rs",
    "contracts/defuse/core/src/intents/imt.rs",
    "contracts/defuse/core/src/accounts.rs",
    "contracts/defuse/core/src/amounts.rs",
    "contracts/defuse/core/src/fees.rs",
    "contracts/defuse/core/src/lock.rs",
    "contracts/defuse/core/src/tokens.rs",
    "contracts/defuse/core/src/error.rs",
    "contracts/defuse/core/src/events/mod.rs",
    "contracts/defuse/core/src/lib.rs",

    # -- Who signed it: the identity binding every intent rests on ------------------------
    # Seven external signing standards collapse into one `PublicKey`, and a missing
    # account falls back to `to_implicit_account_id()`.
    "contracts/defuse/core/src/payload/mod.rs",
    "contracts/defuse/core/src/payload/multi.rs",
    "contracts/defuse/core/src/payload/nep413.rs",
    "contracts/defuse/core/src/payload/erc191.rs",
    "contracts/defuse/core/src/payload/tip191.rs",
    "contracts/defuse/core/src/payload/sep53.rs",
    "contracts/defuse/core/src/payload/raw.rs",
    "contracts/defuse/core/src/payload/ton_connect.rs",
    "contracts/defuse/core/src/payload/webauthn.rs",
    "contracts/defuse/core/src/public_key.rs",
    "contracts/defuse/core/src/signature.rs",
    "crates/signatures/nep413/src/lib.rs",
    "crates/signatures/nep461/src/lib.rs",
    "crates/signatures/erc191/src/lib.rs",
    "crates/signatures/tip191/src/lib.rs",
    "crates/signatures/sep53/src/lib.rs",
    "crates/signatures/ton-connect/src/lib.rs",
    "crates/signatures/ton-connect/src/cell.rs",
    "crates/signatures/webauthn/src/lib.rs",
    "crates/signatures/webauthn/src/ed25519.rs",
    "crates/signatures/webauthn/src/p256.rs",
    "crates/crypto/src/curve.rs",
    "crates/crypto/src/ed25519.rs",
    "crates/crypto/src/secp256k1.rs",
    "crates/crypto/src/p256.rs",
    "crates/crypto/src/signer.rs",
    "crates/crypto/src/fmt.rs",
    "crates/crypto/src/lib.rs",
    "crates/digest/src/lib.rs",
    "crates/digest/src/sha2/mod.rs",
    "crates/digest/src/sha2/near.rs",
    "crates/digest/src/sha3/mod.rs",
    "crates/digest/src/sha3/near.rs",
    "crates/digest/src/ripemd/mod.rs",
    "crates/digest/src/ripemd/near.rs",
    "crates/digest/src/utils.rs",

    # -- Replay: one signature must move funds exactly once --------------------------------
    "contracts/defuse/core/src/nonce/mod.rs",
    "contracts/defuse/core/src/nonce/versioned.rs",
    "contracts/defuse/core/src/nonce/salted.rs",
    "contracts/defuse/core/src/nonce/expirable.rs",
    "contracts/defuse/src/contract/accounts/account/nonces.rs",
    "contracts/defuse/src/contract/state/salt_registry.rs",
    "contracts/defuse/src/contract/salts.rs",
    "contracts/defuse/src/contract/garbage_collector.rs",
    "contracts/defuse/src/garbage_collector.rs",
    "contracts/defuse/src/salts.rs",
    "crates/bitmap/src/lib.rs",
    "crates/bitmap/src/b256.rs",
    "crates/primitives/time/src/lib.rs",
    "crates/primitives/time/src/borsh.rs",
    "crates/primitives/time/src/serde.rs",
    "crates/primitives/time/src/error.rs",

    # -- The Verifier contract: entry points, accounts and persisted balances --------------
    "contracts/defuse/src/contract/mod.rs",
    "contracts/defuse/src/contract/intents/mod.rs",
    "contracts/defuse/src/contract/intents/state.rs",
    "contracts/defuse/src/contract/intents/execute.rs",
    "contracts/defuse/src/contract/intents/simulate.rs",
    "contracts/defuse/src/contract/intents/auth_call.rs",
    "contracts/defuse/src/contract/intents/relayer.rs",
    "contracts/defuse/src/contract/accounts/mod.rs",
    "contracts/defuse/src/contract/accounts/state.rs",
    "contracts/defuse/src/contract/accounts/force.rs",
    "contracts/defuse/src/contract/accounts/account/mod.rs",
    "contracts/defuse/src/contract/accounts/account/entry/mod.rs",
    "contracts/defuse/src/contract/accounts/account/entry/v0.rs",
    "contracts/defuse/src/contract/accounts/account/entry/v1.rs",
    "contracts/defuse/src/contract/state/mod.rs",
    "contracts/defuse/src/contract/state/v0.rs",
    "contracts/defuse/src/contract/versioned/mod.rs",
    "contracts/defuse/src/contract/versioned/v0.rs",
    "contracts/defuse/src/contract/config.rs",
    "contracts/defuse/src/contract/fees.rs",
    "contracts/defuse/src/contract/admin.rs",
    "contracts/defuse/src/contract/upgrade.rs",
    "contracts/defuse/src/contract/events.rs",
    "contracts/defuse/src/contract/prefix.rs",
    "contracts/defuse/src/accounts.rs",
    "contracts/defuse/src/intents.rs",
    "contracts/defuse/src/fees.rs",
    "contracts/defuse/src/far.rs",
    "contracts/defuse/src/simulation_output.rs",
    "contracts/defuse/src/lib.rs",

    # -- Settlement: assets crossing the contract boundary, and the callbacks that undo it --
    # Balances are debited before the Promise resolves; every `*_resolve_*` re-credits.
    "contracts/defuse/src/contract/tokens/mod.rs",
    "contracts/defuse/src/contract/tokens/imt.rs",
    "contracts/defuse/src/contract/tokens/nep141/mod.rs",
    "contracts/defuse/src/contract/tokens/nep141/deposit.rs",
    "contracts/defuse/src/contract/tokens/nep141/withdraw.rs",
    "contracts/defuse/src/contract/tokens/nep141/native.rs",
    "contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs",
    "contracts/defuse/src/contract/tokens/nep171/mod.rs",
    "contracts/defuse/src/contract/tokens/nep171/deposit.rs",
    "contracts/defuse/src/contract/tokens/nep171/withdraw.rs",
    "contracts/defuse/src/contract/tokens/nep245/mod.rs",
    "contracts/defuse/src/contract/tokens/nep245/core.rs",
    "contracts/defuse/src/contract/tokens/nep245/deposit.rs",
    "contracts/defuse/src/contract/tokens/nep245/withdraw.rs",
    "contracts/defuse/src/contract/tokens/nep245/resolver.rs",
    "contracts/defuse/src/contract/tokens/nep245/enumeration.rs",
    "contracts/defuse/src/contract/tokens/nep245/force.rs",
    "contracts/defuse/src/tokens/mod.rs",
    "contracts/defuse/src/tokens/imt.rs",
    "contracts/defuse/src/tokens/nep141.rs",
    "contracts/defuse/src/tokens/nep171.rs",
    "contracts/defuse/src/tokens/nep245.rs",
    "crates/near/nep245/src/core.rs",
    "crates/near/nep245/src/checked.rs",
    "crates/near/nep245/src/resolver.rs",
    "crates/near/nep245/src/receiver.rs",
    "crates/near/nep245/src/enumeration.rs",
    "crates/near/nep245/src/events.rs",
    "crates/near/nep245/src/token.rs",
    "crates/near/nep245/src/errors.rs",
    "crates/near/nep245/src/lib.rs",
    "crates/near/wnear/src/lib.rs",
    "crates/near/auth-call/src/lib.rs",
    "crates/near/promise/src/lib.rs",
    "crates/near/promise/src/actions/mod.rs",
    "crates/near/promise/src/actions/function_call.rs",
    "crates/near/promise/src/actions/state_init.rs",
    "crates/near/promise/src/actions/transfer.rs",
    "crates/near/utils/src/lib.rs",
    "crates/near/utils/src/promise.rs",
    "crates/near/utils/src/event.rs",
    "crates/near/utils/src/panic_on_clone.rs",
    "crates/near/sender/src/lib.rs",
    "crates/near/controller/src/lib.rs",
    "crates/near/admin-utils/src/lib.rs",
    "crates/near/admin-utils/src/full_access_keys.rs",

    # -- Token identity and arithmetic: what a balance is a claim on, and how much ---------
    "crates/primitives/token-id/src/lib.rs",
    "crates/primitives/token-id/src/nep141.rs",
    "crates/primitives/token-id/src/nep171.rs",
    "crates/primitives/token-id/src/nep245.rs",
    "crates/primitives/token-id/src/imt.rs",
    "crates/primitives/token-id/src/error.rs",
    "crates/primitives/fees/src/lib.rs",
    "crates/primitives/decimal/src/lib.rs",
    "crates/primitives/decimal/src/ops.rs",
    "crates/primitives/decimal/src/str.rs",
    "crates/num-utils/src/lib.rs",
    "crates/num-utils/src/add_sub.rs",
    "crates/num-utils/src/mul.rs",
    "crates/num-utils/src/div.rs",
    "crates/num-utils/src/mul_div.rs",
    "crates/map-utils/src/lib.rs",
    "crates/map-utils/src/cleanup.rs",
    "crates/map-utils/src/btree_map.rs",
    "crates/map-utils/src/hash_map.rs",
    "crates/map-utils/src/near.rs",
    "crates/map-utils/src/iter.rs",
    "crates/serde-utils/src/lib.rs",
    "crates/serde-utils/src/base64.rs",
    "crates/serde-utils/src/hex.rs",
    "crates/serde-utils/src/seq.rs",
    "crates/serde-utils/src/cow.rs",
    "crates/serde-utils/src/tlb.rs",
    "crates/borsh-utils/src/lib.rs",
    "crates/borsh-utils/src/duration.rs",
    "crates/borsh-utils/src/schema.rs",
    "crates/io-utils/src/lib.rs",

    # -- Wallet contracts: NEP-641 authorisation for accounts that hold Verifier balances ---
    "contracts/wallet/src/contract.rs",
    "contracts/wallet/src/message.rs",
    "contracts/wallet/src/nonces.rs",
    "contracts/wallet/src/request/mod.rs",
    "contracts/wallet/src/request/ops.rs",
    "contracts/wallet/src/state.rs",
    "contracts/wallet/src/schema.rs",
    "contracts/wallet/src/events.rs",
    "contracts/wallet/src/error.rs",
    "contracts/wallet/src/lib.rs",
    "contracts/wallet/signatures/ed25519/src/contract.rs",
    "contracts/wallet/signatures/ed25519/src/signer.rs",
    "contracts/wallet/signatures/ed25519/src/lib.rs",
    "contracts/wallet/signatures/no-sign/src/contract.rs",
    "contracts/wallet/signatures/no-sign/src/lib.rs",
    "contracts/wallet/signatures/webauthn/src/lib.rs",
    "contracts/wallet/signatures/webauthn/src/ed25519.rs",
    "contracts/wallet/signatures/webauthn/src/p256.rs",
    "contracts/wallet/signatures/webauthn/ed25519/src/lib.rs",
    "contracts/wallet/signatures/webauthn/p256/src/lib.rs",
    "crates/signatures/nep641/src/lib.rs",
    "crates/signatures/nep641/src/message.rs",
    "crates/signatures/nep641/src/access_keys.rs",
    "crates/signatures/nep641/src/client.rs",
    "crates/signatures/nep641/src/resolver/mod.rs",
    "crates/signatures/nep641/src/resolver/contract.rs",
    "crates/signatures/nep641/src/resolver/access_keys.rs",
    "crates/signatures/nep641/src/resolver/error.rs",
    "crates/mpc/signer/src/contract.rs",
    "crates/mpc/signer/src/secp256k1.rs",
    "crates/mpc/signer/src/ed25519.rs",
    "crates/mpc/signer/src/convert.rs",
    "crates/mpc/signer/src/lib.rs",
    "crates/mpc/kdf/src/lib.rs",
    "crates/mpc/kdf/src/ckd.rs",
    "crates/mpc/kdf/src/tweak/mod.rs",
    "crates/mpc/kdf/src/tweak/secp256k1.rs",
    "crates/mpc/kdf/src/tweak/ed25519.rs",
    "crates/mpc/ckd/src/lib.rs",
    "crates/mpc/ckd/src/types.rs",
    "crates/kdf/src/lib.rs",
    "crates/kdf/src/ed25519.rs",
    "crates/kdf/src/secp256k1.rs",
    "crates/kdf/src/signer.rs",
    "crates/kdf/src/schema/mod.rs",
    "crates/kdf/src/schema/borsh.rs",
    "crates/kdf/src/schema/digest.rs",
    "crates/kdf/src/schema/hex.rs",
    "crates/kdf/src/schema/additive.rs",
    "crates/kdf/src/schema/reduce.rs",

    # -- Token issuers and deployers whose output the Verifier treats as a real asset -------
    "contracts/poa/factory/src/contract.rs",
    "contracts/poa/factory/src/lib.rs",
    "contracts/poa/token/src/contract.rs",
    "contracts/poa/token/src/lib.rs",
    "contracts/global-deployer/src/contract.rs",
    "contracts/global-deployer/src/state.rs",
    "contracts/global-deployer/src/client.rs",
    "contracts/global-deployer/src/events.rs",
    "contracts/global-deployer/src/error.rs",
    "contracts/global-deployer/src/lib.rs",
    "contracts/outlayer/app/src/contract.rs",
    "contracts/outlayer/app/src/state.rs",
    "contracts/outlayer/app/src/client.rs",
    "contracts/outlayer/app/src/events.rs",
    "contracts/outlayer/app/src/error.rs",
    "contracts/outlayer/app/src/lib.rs",
    "contracts/treasury-logger/src/lib.rs",
    "contracts/treasury-logger/src/state.rs",
    "contracts/treasury-logger/src/event.rs",

    # =================================================================================
    # NOT IN THIS VARIANT:
    # * `contracts/escrow-swap/**` - explicitly out of scope in the NEAR Intents
    #   Smart Contracts bounty program.
    # * `tests/**`, `**/tests/**`, `**/tests.rs`, `crates/testing/**`, `**/mock.rs`,
    #   `**/arbitrary.rs`, `**/fuzz/**`, `**/examples/**` - tests, fixtures and mocks.
    # * `**/build.rs`, `contracts/defuse/src/contract/abi.rs`, `**/near-gds/src/main.rs`,
    #   `**/near-oa/src/main.rs`, `crates/cli-utils/**`, `crates/rand-compat/**`,
    #   `crates/wallet/sdk/**` - generated artefacts, CLIs and off-chain SDK code with no
    #   on-chain decision.
    # * `*.toml`, `*.md`, `LICENSE`, `Makefile`, `scripts/**`, `releases/**`,
    #   `rust-toolchain` - configuration, documentation and tooling.
    # =================================================================================
]


target_scopes = [
    "Critical. A `TokenDiff` THAT DOES NOT NET TO ZERO. `TokenDiff::execute_intent` calls `Deltas::internal_apply_deltas` per `(token_id, delta)` and takes fees only on negative deltas via `TokenDiff::token_fee(...).fee_ceil(amount)`; the ONLY thing forcing the batch to conserve value is `TransferMatcher::finalize` at the very end of `Engine::finalize`, where `TokenTransferMatcher::finalize_into` pairs sorted deposits against withdrawals and `deltas.apply_delta` must leave `TokenDeltas` empty. Show an unprivileged signer who crafts a `MultiPayload` batch - self-cancelling deltas inside one `TokenDiff`, `i128::MIN` / `unsigned_abs` edges, an `Amounts::add` / `sub` path returning `None` late, a `saturating_sub` in `sub_add`, or an `unmatched == 0` overflow branch treated as success - so `execute_intents` commits balance changes whose sum is non-zero. Binding: sum of every `token_balances` change for token T across one `execute_intents` call == 0, and every `Transfers` entry has a signer who authorised it.",

    "Critical. THE SIGNATURE SAYS ONE THING, `signer_id` SAYS ANOTHER. `Engine::execute_signed_intent` takes the `PublicKey` returned by `MultiPayload::verify()`, then trusts `DefusePayload::signer_id` from `extract_defuse_payload()` and only asks `StateView::has_public_key(&signer_id, &public_key)` - which, for an account with no entry in `self.accounts`, falls back to `account_id == public_key.to_implicit_account_id()`. Seven standards (`Nep413`, `Erc191`, `Tip191`, `RawEd25519`, `WebAuthn`, `TonConnect`, `Sep53`) feed that one check, each with its own envelope and `Payload::hash()`. Show an unprivileged party who gets `execute_signed_intent` to accept a payload as signed by a victim: an envelope byte-string one standard's `verify()` accepts that decodes to a different `DefusePayload` under another, `SignedWebAuthnPayload::extract_defuse_payload` reading `self.payload` while `hash()` digests it separately, a malleable or recoverable signature yielding an attacker-chosen `PublicKey`, a `serde(flatten)` field-shadowing in `Nep413DefuseMessage` / `DefusePayload`, or an implicit-account derivation the victim never registered. Binding: the `(signer_id, public_key)` pair the engine authorises with == the pair the holder of the private key actually signed for.",

    "Critical. ONE SIGNATURE, TWO SETTLEMENTS. Replay protection is `verify_intent_nonce` plus `State::commit_nonce`. `VersionedNonce::maybe_from` returns `None` for any nonce lacking `VERSIONED_MAGIC_PREFIX`, and `verify_intent_nonce` then returns `Ok(())` with NO salt, NO nonce deadline and NO expiry check; for `V1(SaltedNonce { salt, nonce: ExpirableNonce { deadline, .. } })` the checks are `is_valid_salt`, `intent_deadline > deadline` and expiry. Commitment goes through `MaybeLegacyNonces::commit`, which rejects legacy-map hits but writes only to `self.nonces`, a `BitMap256` keyed by the top 248 bits; `Nonces::cleanup_by_prefix` clears a whole 256-bit word. Show an unprivileged party who executes one signed `DefuseIntents` twice - a borsh re-encoding of the same `VersionedNonce` producing a different 32-byte `Nonce`, a `Timestamp::now()` / `deadline` boundary, a nonce whose word was cleaned while the signature is still live, or `commit_nonce` succeeding on an account path that never persisted. Binding: the number of times a given signed `MultiPayload` moves funds == 1, for all time.",

    "Critical. THE BALANCE IS GONE BEFORE THE PROMISE RESOLVES. `internal_ft_withdraw` / `internal_nft_withdraw` / `internal_mt_withdraw` call `Contract::withdraw` to debit `token_balances` immediately, then schedule `do_*_withdraw` and a `#[private]` resolver - `ft_resolve_withdraw`, `mt_resolve_transfer`, `resolve_deposit_internal` - that re-credits from `promise_result_checked_json*`, a value the attacker's own token or receiver contract chooses. `ft_resolve_withdraw` credits `amount - used` where `used = amount` on ANY promise error for `is_call`, and `mt_resolve_transfer` clamps `refund.0` to `receiver_balance` and mutates `amounts` in place. Show an unprivileged party - the withdrawal target, a contract they deployed and named in `msg`, or a receiver they lock/drain between the call and the callback - who makes the refunded amount differ from the amount that failed to settle: a double credit, a refund routed to `previous_owner_ids.first()` rather than the real owner, a `token_ids.parse()` mismatch, or a settled transfer that is refunded anyway. Binding: (balance debited) == (assets that actually left the contract) + (amount re-credited by the resolver), per token, per receipt.",

    "Critical. RE-ENTERING THE VERIFIER FROM A CONTRACT THE ATTACKER WROTE. `AuthCall::execute_intent`, `Transfer`'s `NotifyOnTransfer` and `DepositMessage`'s `DepositAction::Notify` / `Execute` all hand control to an attacker-chosen `contract_id` / `receiver_id` via `on_auth()`, `mt_on_transfer()` or a re-entrant `execute_intents`, optionally deploying it first with `p.state_init(state_init, NearToken::ZERO)` (NEP-616) and `Contract::auth_call_callback_gas`. `Engine::finalize` has already run and the intents in one `DefuseIntents` fire concurrently. Show an unprivileged signer whose callee re-enters `execute_intents`, `ft_withdraw`, `mt_resolve_transfer` or `ft_resolve_deposit` and observes or mutates state between the debit and the settlement - spending a balance twice, taking a refund plus the goods, or having `do_auth_call`'s `promise_result_checked_void(0)` pass while the wNEAR unwrap did not fund it. Binding: the set of balance changes an `execute_intents` receipt commits == the set the signed intents authorise, regardless of what any callee does while it is in flight.",

    "Critical. FEES AND ROUNDING THAT DO NOT CLOSE. `Pips::fee_ceil`, `Pips::invert`, `TokenDiff::token_fee` (which returns `Pips::ZERO` for `Nep171` and for `Nep245`/`Imt` when `amount <= 1`), `TokenDiff::supply_delta` / `closure_supply_delta` with `checked_mul_div_ceil` and `checked_mul_div_euclid`, and `UD128` arithmetic in `defuse_decimal` decide how much the `fee_collector` receives and how much a counterparty must supply. Show an unprivileged signer who splits or shapes deltas - many `|delta| == 1` NEP-245 legs, a token id whose `TokenIdType` classification changes the fee, a `fee_ceil` / `mul_div_euclid` rounding direction, or a `closure` that a solver signs against - so the protocol fee actually collected is less than the fee the executed deltas owed, or so the counterparty settles at a price the closure never implied. Binding: fees credited to `fee_collector` for token T == `Pips::fee_ceil` over every negative delta of T in the batch, and the value each party gives up == the value the signed deltas say.",

    "Critical. TWO NAMES FOR ONE ASSET, ONE NAME FOR TWO. Every balance is keyed by a `TokenId` whose `Display` / `FromStr` round-trip in `defuse_token_id` (`nep141`, `nep171`, `nep245`, `imt`) is the only thing tying it to a real contract and token: `mt_resolve_transfer` re-parses `token_ids` from strings, `ft_on_transfer` builds a key from `env::predecessor_account_id()`, `MtTransferEvent` and `TokenDiff` carry `token_id.to_string()`, and `Nep245TokenId` / `Nep171TokenId` embed an arbitrary user-chosen sub-token string. Show an unprivileged party who mints a token, NFT or MT whose id makes two distinct `TokenId` values collide on `to_string()` - or one `TokenId` parse back to a different asset - so a deposit of a worthless asset credits a valuable balance, a withdrawal drains a different token than the one debited, or a resolver refunds the wrong key. Binding: `TokenId::from_str(&t.to_string()) == t` for every constructible `t`, and each `TokenId` maps to exactly one `(contract, token)` on chain.",

    "Critical. wNEAR SPENT, NEAR NEVER DELIVERED. `native_withdraw`, `storage_deposit` and a deposit-bearing `auth_call` all debit the signer's NEP-141 wNEAR balance through `Contract::withdraw`, then chain `ext_wnear::near_withdraw` into `do_native_withdraw` / `do_storage_deposit` / `do_auth_call`, and `FtWithdraw::storage_deposit` does the same inside `internal_ft_withdraw`. Each documents that the wNEAR is NOT refunded on failure, and the only guards are the `min_gas()` floors (`FT_TRANSFER_CALL_GAS_MIN`, `MT_BATCH_TRANSFER_GAS_MIN`, `AuthCall::MIN_GAS_DEFAULT`, `STATE_INIT_GAS`) plus `with_unused_gas_weight(0)` and `auth_call_callback_gas`'s `checked_add`. Show an unprivileged party who makes another user's wNEAR leave without the corresponding NEAR, storage deposit or `on_auth` ever happening - an attacker-chosen `receiver_id` or `contract_id` that makes the callback abort after the debit, a `min_gas` value that starves the callback but passes the floor, or a `state_init` that consumes the gas the settlement needed. Binding: wNEAR debited from an account == NEAR that actually reached the named receiver, or was returned to that account.",

    "High. A WALLET THAT EXECUTES A REQUEST NOBODY SIGNED FOR IT. `Wallet::w_execute_signed(msg, proof)` must reject a `RequestMessage` whose `chain_id` is another network, whose `signer_id` is not `env::current_account_id()`, or whose `nonce` is used, expired or from the future; `Nonces` is a dual-window `BitMap<BTreeMap<u32, u32>>` rotated by `timeout` and `last_cleaned_at`, and `w_execute_extension` trusts `env::predecessor_account_id()` against the enabled-extension set while `WalletOp::SetSignatureMode` / `AddExtension` mutate who may act. `SignatureSchema::verify_request_msg` (ed25519, webauthn-p256, webauthn-ed25519, no-sign) and the NEP-641 `AuthResolver` / `AuthorizationResolution` are the only authority. Show an unprivileged party who gets a wallet holding Verifier balances to execute a `Request` - a nonce replayed across the `old` / `current` window rotation, a `RequestMessage` re-encoded so `proof` still verifies, a `no-sign` or extension path reachable without authorisation, or an `AuthorizationResolution` accepted for the wrong `signer_id`. Binding: every `NearAction` a wallet executes == one its owner signed, for this chain and this account, exactly once.",

    "Critical. THE MISSING BINDING - what nobody built. Nothing in this repository re-derives, after `execute_intents` returns, that the assets the Verifier still custodies equal the sum of all `token_balances` it owes; nothing ties a `TokenId` back to a live on-chain asset at withdrawal time; nothing bounds what an attacker-controlled callee does to state between a debit and its `*_resolve_*` callback; and nothing checks that a legacy (non-versioned) nonce was ever bounded by a salt or an expiry. Identify the FIRST point at which a byte an unprivileged party chose - a `MultiPayload` handed to `execute_intents`, a deposit `msg`, a `ft_withdraw`-family call under `auth_by_predecessor_id`, a value returned by a contract they deployed, or a `RequestMessage` sent to a wallet - becomes a credited balance, a released asset or a committed nonce with no independent party ever re-deriving it. Prove it with one `cargo test` asserting both the value used and the value that should have authorised it, and show that once they diverge nothing in the protocol reconciles them.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate custody and authorization audit questions for one NEAR Intents target.

    ```
    target_file format:
    "'File Name: contracts/defuse/core/src/engine/state/deltas.rs -> Scope: Critical. ...'"
    """

    prompt = f"""
    ```

    Generate custody and authorization security audit questions for this exact
    NEAR Intents target:

    {target_file}

    Project focus:
    NEAR Intents ("the Verifier", `intents.near`) is a custodial multi-token ledger on
    NEAR. Users deposit NEP-141 / NEP-171 / NEP-245 assets, and every later move is
    authorised by an off-chain signature over a `DefusePayload` carried in a
    `MultiPayload`. Untrusted bytes enter through doors any unprivileged party controls:
    a `MultiPayload` batch handed to `execute_intents` / `simulate_intents` by ANY caller
    (the signature is the only authority - the predecessor is irrelevant), a
    `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` deposit `msg`, a direct
    `ft_withdraw`-family call from an account that enabled `auth_by_predecessor_id`, a
    `RequestMessage` sent to a wallet contract, and the return value of any contract the
    attacker deploys and names in `msg`, `AuthCall::contract_id` or `NotifyOnTransfer` -
    which flows back into the `*_resolve_*` callbacks. Those bytes end in one place: the
    `token_balances` of accounts inside the Verifier and the real assets those balances
    are a claim on. Anything that moves value the signer did not authorise, replays one
    signature, breaks conservation across a batch, or leaves the ledger owing more than
    it custodies is the bug.

    Rules:
    * Treat `File Name:` as the exact file.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Rust symbols (module, struct, enum, fn, const, field) as they appear in the file.
    * EVERY question must close on a binding that must hold across a call. State it explicitly
      as an equality between two named values. Narrative questions are rejected.
    * Attacker is unprivileged only: anyone who can send a NEAR transaction, call
      `execute_intents` / `simulate_intents` with any `MultiPayload` batch, deposit tokens with
      an arbitrary `msg`, deploy and control their own FT/NFT/MT and receiver contracts, create
      accounts inside the Verifier and hold their own balances, and sign with their own keys
      under any supported standard.
    * Attacker is NOT the DAO or any `Role` holder (`UnrestrictedWithdrawer`, `SaltManager`,
      `GarbageCollector`, `RelayerKeysManager`, `Pauser`, `UnrestrictedAccountUnlocker`), not a
      relayer key holder, not a contract upgrader, and not the fee collector. They hold no
      victim private key and no access-control role. No malicious validator or node, no key
      compromise, no RPC or TLS interception, no local or physical access, no compromised
      dependency, no social engineering.
    * PROGRAM EXCLUSIONS - a question landing in any of these wastes the whole batch:
      - `contracts/escrow-swap/**` is OUT OF SCOPE for this program.
      - Tests, fixtures and mocks (`tests/**`, `**/tests/**`, `**/tests.rs`,
        `crates/testing/**`, `**/mock.rs`, `**/arbitrary.rs`, `**/fuzz/**`, `**/examples/**`),
        generated and tooling files (`**/build.rs`, `contract/abi.rs`, `**/near-gds/**`,
        `**/near-oa/**`, `crates/cli-utils/**`, `crates/wallet/sdk/**`), `*.toml`, `*.md`,
        `scripts/**`, `releases/**` are OUT OF SCOPE.
      - Unbounded gas or storage consumption, denial of service, rate limiting, retry
        behaviour, queue depth, resource exhaustion, unbounded collections, memory hygiene
        and log volume are OUT OF SCOPE.
      - Griefing with no attacker profit, anything that only costs the attacker their own
        funds, and anything requiring a DAO/role holder, relayer key or an upgrade are OUT OF
        SCOPE.
      - Defects in third-party crates (near-sdk, near-contract-standards, near-plugins, serde,
        borsh, k256, p256, ed25519-dalek, bs58) with no exploit path through this repository's
        own code are OUT OF SCOPE.
      - Also excluded: leaked keys, best-practice notes, feature requests, and theoretical
        findings with no demonstration.
      - A weakness in this repository that manipulates a third-party crate into unsafe
        behaviour remains fully in scope.
    * IN-SCOPE IMPACTS - every question must land on one and name it:
      Critical: tokens moved, credited or withdrawn without the owner's valid signature or
      authorisation; one signed payload settling more than once; a batch whose balance changes
      do not net to zero, so the Verifier owes more than it custodies; a refund or resolver
      credit that does not match what failed to settle; a `TokenId` collision that lets a
      worthless asset claim a valuable balance; protocol fees bypassed or over-collected; user
      funds permanently frozen or unrecoverable.
      High: an intent executed against a locked account, or a lock/unlock state that
      contradicts what the contract enforces; a wallet contract executing a `Request` its owner
      did not authorise for this chain and account; `simulate_intents` reporting an outcome
      that `execute_intents` does not produce, when a party settles on that report.
    * Every question must be a concrete real-world scenario an unprivileged attacker can
      execute against the deployed Verifier - a `MultiPayload` they sign and submit, a deposit
      `msg` they craft, a contract they deploy and name, a direct contract call they make. No
      speculative resource-hygiene or memory questions.
    * A panic or error is a finding only when it freezes funds, lets an unauthorised move
      through, or leaves the ledger unbalanced - say which.
    * Generate 40 to 80 high-signal questions.
    * At least 70% must land on a Critical impact rather than a High one.
    * Every question must be testable by a `cargo test` in this workspace (unit test, or the
      `near-workspaces` sandbox harness), with no mainnet.
    * Avoid generic checklist questions and repeated root causes.
    * Prefer questions that name TWO values that must be equal and ask whether they are: the
      value debited and the value delivered, the signer the signature proves and the
      `signer_id` the engine uses, the times a nonce settles and one, the sum of deltas and
      zero, the fee owed and the fee collected, the asset a `TokenId` names and the asset moved.

    Known dead ends - do NOT generate questions about these:
    * Anything needing a DAO or `Role` holder, a relayer key, a contract upgrade, or a victim's
      private key.
    * A bug in a dependency with no reachable path through this repository.
    * Gas, storage growth, event log size, or an attacker burning only their own funds with no
      protocol value moved and no other party harmed.
    * Findings only reproducible in tests, mocks, fixtures or generated files.
    * `contracts/escrow-swap/**`.

    Core bindings (each question must close on one):
    * AUTHORISATION: every balance change == one a valid signature or `auth_by_predecessor_id`
      caller authorised, for that exact account and amount.
    * REPLAY: the number of times a signed `MultiPayload` settles == 1, forever.
    * CONSERVATION: the sum of all `token_balances` changes for a token across one call == 0,
      and total balances owed == assets actually custodied.
    * SETTLEMENT: value debited == value delivered plus value re-credited by the resolver.
    * IDENTITY: the `(contract, token)` a `TokenId` names == the asset actually moved; the
      `(signer_id, public_key)` authorised == the pair actually signed.
    * FEES: fees credited to `fee_collector` == fees the executed deltas owed.

    Each question must include:
    1. target struct/fn;
    2. attacker action (a concrete signed `MultiPayload`, deposit `msg`, contract call, or a
       contract they deploy, with its fields);
    3. preconditions (existing accounts, balances, lock state, salt, deposited tokens);
    4. call sequence through the code;
    5. the binding that breaks, written as an equality;
    6. scoped impact and whose funds are affected;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Method: struct_or_fn] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, breaking the binding BINDING_EQUALITY, causing scoped impact: SCOPE_IMPACT against PARTY? Proof idea: cargo test PARAMETERS asserting AUTHORISATION, REPLAY, CONSERVATION, SETTLEMENT, IDENTITY, or FEES.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a custody and authorization exploit-validation prompt for NEAR Intents.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: anyone who can send a NEAR transaction, call `execute_intents` / `simulate_intents` with any `MultiPayload` batch, deposit tokens with an arbitrary `msg`, deploy and control their own FT/NFT/MT and receiver contracts, hold their own Verifier balances, and sign with their own keys. They are not the DAO or any `Role` holder (`UnrestrictedWithdrawer`, `SaltManager`, `GarbageCollector`, `RelayerKeysManager`, `Pauser`, `UnrestrictedAccountUnlocker`), not a relayer key holder, not an upgrader, not the fee collector, and hold no victim private key.
- Reject malicious validators or nodes, key compromise, RPC or TLS interception, local or physical access, compromised dependencies and social engineering.
- OUT OF SCOPE, reject on sight: `contracts/escrow-swap/**`; tests, fixtures and mocks (`tests/**`, `**/tests/**`, `**/tests.rs`, `crates/testing/**`, `**/mock.rs`, `**/arbitrary.rs`, `**/fuzz/**`, `**/examples/**`); generated and tooling files (`**/build.rs`, `contract/abi.rs`, `**/near-gds/**`, `**/near-oa/**`, `crates/cli-utils/**`, `crates/wallet/sdk/**`), `*.toml`, `*.md`, `scripts/**`, `releases/**`; unbounded gas or storage consumption, denial of service, rate limiting, retry behaviour and resource exhaustion; griefing with no attacker profit; anything requiring a DAO/role holder, relayer key or upgrade; third-party crate defects with no path through this repository; best-practice notes; feature requests; theoretical findings with no demonstration.
- The impact must be one of: Critical - tokens moved, credited or withdrawn without the owner's valid signature or authorisation, one signed payload settling more than once, a batch whose balance changes do not net to zero so the Verifier owes more than it custodies, a refund or resolver credit that does not match what failed to settle, a `TokenId` collision letting a worthless asset claim a valuable balance, protocol fees bypassed or over-collected, or user funds permanently frozen; High - an intent executed against a locked account or a lock state contradicting what is enforced, a wallet contract executing a `Request` its owner did not authorise for this chain and account, or `simulate_intents` reporting an outcome `execute_intents` does not produce when a party settles on that report.
- Focus on real impact: value leaving the Verifier that the signer never authorised.

## Validate
- Write the binding the question claims is broken as an explicit equality between two named values BEFORE tracing any code.
- Trace the exact reachable path from the attacker's `MultiPayload`, deposit `msg`, contract call or deployed callee, and record every read and write of: the verified `PublicKey` and `DefusePayload::signer_id`, the `Nonce` and its `VersionedNonce` / `Salt` / deadline, each `TokenId` and its string form, every `internal_add_balance` / `internal_sub_balance` / `Amounts::add` / `sub`, the `TransferMatcher` deltas and `Transfers` produced by `finalize`, the fee taken by `Pips::fee_ceil`, every Promise scheduled and every `*_resolve_*` callback's re-credit.
- Evaluate both sides of the equality before and after. If they still match, output no vulnerability.
- Check whether `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, `MaybeLegacyNonces::commit`, `SaltRegistry::is_valid`, `Lock::get_mut`, `TransferMatcher::finalize`, `assert_one_yocto`, `#[private]`, `#[pause]`, the `access_control_any` guards, or a `checked_*` arithmetic path already prevents the divergence.
- State what the attacker gains or destroys per attempt and whether it is repeatable across accounts, tokens or batches.
- Require exact file/fn support and a reproducible `cargo test` proof (unit test or `near-workspaces` sandbox), with no mainnet.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[The broken binding as an equality, the code path, root cause, the attacker's exact payload, msg or call, exploit flow, and why existing guards fail]

### Impact Explanation
[What is moved, credited, replayed, frozen or under-collected, whose funds, repeatability, blast radius, matching severity category]

### Likelihood Explanation
[Preconditions, required balances and accounts, attacker cost, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[cargo test plan with the exact assertions on both sides of the binding]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for NEAR Intents claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- A binding claim is only valid if the report states the broken equality between two named values and shows both sides concretely. Reject prose-only claims.
- Reject anything requiring the DAO or a `Role` holder (`UnrestrictedWithdrawer`, `SaltManager`, `GarbageCollector`, `RelayerKeysManager`, `Pauser`, `UnrestrictedAccountUnlocker`), a relayer key, a contract upgrade, a victim's private key, a malicious validator or node, RPC or TLS interception, local or physical access, a compromised dependency, or social engineering.
- OUT OF SCOPE, reject on sight: `contracts/escrow-swap/**`; tests, fixtures and mocks (`tests/**`, `**/tests/**`, `**/tests.rs`, `crates/testing/**`, `**/mock.rs`, `**/arbitrary.rs`, `**/fuzz/**`, `**/examples/**`); generated and tooling files (`**/build.rs`, `contract/abi.rs`, `**/near-gds/**`, `**/near-oa/**`, `crates/cli-utils/**`, `crates/wallet/sdk/**`), `*.toml`, `*.md`, `scripts/**`, `releases/**`; unbounded gas or storage consumption, denial of service, rate limiting, retry behaviour and resource exhaustion; griefing with no attacker profit; third-party crate defects with no path through this repository; best-practice notes; feature requests; theoretical findings with no demonstration.
- The impact must be one of: Critical - tokens moved, credited or withdrawn without the owner's valid signature or authorisation, one signed payload settling more than once, a batch whose balance changes do not net to zero, a refund or resolver credit that does not match what failed to settle, a `TokenId` collision letting a worthless asset claim a valuable balance, protocol fees bypassed or over-collected, or user funds permanently frozen; High - an intent executed against a locked account or a contradictory lock state, a wallet contract executing an unauthorised `Request`, or `simulate_intents` diverging from `execute_intents` where a party settles on the report.
- Reject claims that depend on a deployment ignoring the documented configuration, or that only harm the attacker's own funds.
- Reject if the bug was already fixed, publicly disclosed, or is covered by an existing advisory or CHANGELOG entry for a supported version.
- Reject a divergence with no authorisation, replay, conservation, settlement, identity or fee boundary crossed.
- A valid report must be triggerable by an unprivileged attacker against the deployed Verifier running the current release.
- A PoC is mandatory. Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, struct/fn, and line references.
2. The binding written explicitly as an equality, with both sides shown before and after.
3. Clear root cause: which unverified signer field, which missing nonce or salt check, which unchecked arithmetic, which attacker-controlled callback return, which `TokenId` encoding causes the divergence.
4. Reachable exploit path: preconditions -> attacker `MultiPayload`, deposit `msg`, contract call or deployed callee -> call sequence -> observed divergence.
5. `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, `MaybeLegacyNonces::commit`, `SaltRegistry::is_valid`, `Lock::get_mut`, `TransferMatcher::finalize`, `assert_one_yocto`, `#[private]`, `#[pause]`, `access_control_any` and the `checked_*` arithmetic reviewed and shown insufficient.
6. Impact stated concretely: how much of which token moves, whose, and whether it is repeatable.
7. Reproducible proof: `cargo test` (unit or `near-workspaces` sandbox) with the asserted values, no mainnet.

## Silent Triage Questions
Before output, internally answer:
- What exactly is the equality, and does it actually fail?
- Can an ordinary depositor, intent signer, token deployer or internet caller trigger it with no role and no victim key?
- Is the flaw in this repository's in-scope code, not in a dependency, in escrow-swap, or in a careless deployment?
- What value moves, or whose funds freeze, and is it repeatable?
- Would a NEAR Intents triager accept the exploit path?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the broken binding and impact]

## Finding Description
[Exact code path, the equality, root cause, exploit flow, and why existing guards fail]

## Impact Explanation
[What is moved, replayed, unbalanced or frozen, affected party, repeatability, severity category]

## Likelihood Explanation
[Attacker capability, preconditions, configuration, cost, feasibility]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or cargo test plan with concrete assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for NEAR Intents.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope repository context only (`contracts/defuse/**`, `contracts/wallet/**`, `contracts/poa/**`, `contracts/global-deployer/src/**`, `contracts/outlayer/app/src/**`, `contracts/treasury-logger/src/**`, `crates/**`), excluding tests, mocks, generated and tooling files. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only unprivileged-attacker analogs that break a custody binding: a balance change versus the signature that authorised it, the times one signed `MultiPayload` settles versus one, the sum of a batch's deltas versus zero, value debited versus value delivered plus refunded, the asset a `TokenId` names versus the asset moved, fees owed versus fees collected.
- OUT OF SCOPE, reject on sight: `contracts/escrow-swap/**`; tests, fixtures and mocks; generated and tooling files (`**/build.rs`, `contract/abi.rs`, `**/near-gds/**`, `**/near-oa/**`, `crates/cli-utils/**`, `crates/wallet/sdk/**`), `*.toml`, `*.md`, `scripts/**`, `releases/**`; unbounded gas or storage consumption, denial of service, rate limiting, retry behaviour and resource exhaustion; griefing with no attacker profit; anything requiring the DAO, a `Role` holder, a relayer key, an upgrade, a victim key, a malicious node, RPC interception, local access or social engineering; third-party crate defects with no path through this repository; best-practice notes; feature requests; theoretical findings.
- The impact must be one of: Critical - tokens moved, credited or withdrawn without valid authorisation, one signed payload settling more than once, a batch that does not net to zero, a mismatched resolver refund, a `TokenId` collision, fees bypassed or over-collected, or funds permanently frozen; High - an intent executed against a locked account, a wallet executing an unauthorised `Request`, or `simulate_intents` diverging from `execute_intents`.
- Reject analogs that depend on a deployment ignoring the documented configuration, and analogs with no authorisation, replay, conservation, settlement, identity or fee boundary crossed.

## Validate
- Map the bug class to the strongest reachable path in this repository and state the binding it would break as an equality.
- Evaluate both sides before and after the attacker's payload, deposit or call sequence.
- Prove root cause with exact file/fn support.
- Accept only concrete value loss, an unauthorised move, a replayed settlement, an unbalanced ledger, or frozen funds.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
