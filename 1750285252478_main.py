
from bip_utils import Bip32Slip10Ed25519, Bip39SeedGenerator, Bip39MnemonicValidator
from stellar_sdk import Keypair, StrKey, Server, TransactionBuilder, Asset
from stellar_sdk.exceptions import BadRequestError
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import os, time

load_dotenv()

# === Config ===
HORIZON_URL = "https://api.mainnet.minepi.com"
NETWORK_PASSPHRASE = "Pi Network"
BASE_FEE = 100000
REMAIN_BALANCE = 1
STOP_AFTER_UNLOCK = 10
TIMEZONE = timezone(timedelta(hours=1))

TX_PAYER = "SDW5JLWTWFEZFUSZMR7D4E3XRJ6CUQGWHVLYXOUHUYSXPVNLAIN5HRYW"
KP_TX_PAYER = Keypair.from_secret(TX_PAYER)
TX_PAYER_AD = KP_TX_PAYER.public_key
DESTINATION_ADDRESS = "GBYNZZ2VEQGQNDFFDDVZHF2CV7CRIKHHQBQ3LM6ILBRQ723XV4H7HQ4G"

server = Server(HORIZON_URL)

def load_account_with_retry():
    while True:
        try:
            return server.load_account(TX_PAYER_AD)
        except BadRequestError as e:
            if "429" in str(e):
                print("[⚠️] Rate limit hit. Retrying in 2s...")
                time.sleep(2)
            else:
                raise

mnemonic = os.getenv("MNEMONIC") or input("Enter Passphrase: ")
if not Bip39MnemonicValidator().IsValid(mnemonic):
    raise ValueError("Invalid mnemonic")

seed = Bip39SeedGenerator(mnemonic).Generate()
derived = Bip32Slip10Ed25519.FromSeed(seed).DerivePath("m/44'/314159'/0'")
priv_key = derived.PrivateKey().Raw().ToBytes()
SECRET_KEY = StrKey.encode_ed25519_secret_seed(priv_key)
KP = Keypair.from_secret(SECRET_KEY)
ACCOUNT_ID = KP.public_key

def get_claimables():
    return server.claimable_balances().for_claimant(ACCOUNT_ID).limit(5).call()

def find_next_unlock(claimables):
    now = datetime.now(timezone.utc)
    unlock_info = None
    for c in claimables["_embedded"]["records"]:
        for claimant in c["claimants"]:
            if claimant["destination"] != ACCOUNT_ID:
                continue
            predicate = claimant["predicate"]
            unlock_utc = None
            if "not" in predicate and "abs_before" in predicate["not"]:
                unlock_utc = datetime.fromisoformat(predicate["not"]["abs_before"].replace("Z", "+00:00"))
            elif predicate == {"unconditional": True}:
                unlock_utc = now
            if unlock_utc and (not unlock_info or unlock_utc < unlock_info["time"]):
                unlock_info = {
                    "id": c["id"],
                    "amount": float(c["amount"]) - REMAIN_BALANCE,
                    "time": unlock_utc
                }
    return unlock_info

def wait_until(unlock_time):
    while True:
        now = datetime.now(timezone.utc)
        diff = (unlock_time - now).total_seconds()
        if diff <= 0:
            break
        elif diff <= 5:
            print(f"⏳ Unlocks in {diff:.2f}s")
        time.sleep(min(diff, 0.5))

def submit_tx(seq, unlock_id, unlock_amt, account):
    local = account
    local.sequence = seq
    tx = (
        TransactionBuilder(source_account=local, network_passphrase=NETWORK_PASSPHRASE, base_fee=BASE_FEE)
        .append_claim_claimable_balance_op(balance_id=unlock_id, source=ACCOUNT_ID)
        .append_payment_op(destination=DESTINATION_ADDRESS, amount=f"{unlock_amt:.6f}", asset=Asset.native(), source=ACCOUNT_ID)
        .add_text_memo("OK")
        .set_timeout(30)
        .build()
    )
    tx.sign(KP)
    tx.sign(KP_TX_PAYER)

    try:
        server.submit_transaction(tx)
        print(f"[✅] Success at seq {seq}")
    except Exception as e:
        if "429" in str(e):
            print(f"[⚠️] 429 Rate limit at seq {seq}. Retrying...")
            time.sleep(0.5)
            submit_tx(seq, unlock_id, unlock_amt, account)
        else:
            print(f"[❌] Failed at seq {seq} → {str(e)[:80]}")

def claim_and_send(unlock_id, unlock_amt):
    account = load_account_with_retry()
    base_seq = int(account.sequence)
    max_threads = 5  # Reduced from 100 to avoid rate-limiting

    for i in range(max_threads):
        time.sleep(0.1)  # Slight stagger to reduce burst
        submit_tx(base_seq + i, unlock_id, unlock_amt, account)

# === Start ===
print("[INFO] Checking balances...")
claimables = get_claimables()
print(f"[INFO] Found {len(claimables['_embedded']['records'])} claimables.")

unlock = find_next_unlock(claimables)
if unlock:
    print(f"[INFO] Next unlock at {unlock['time'].astimezone(TIMEZONE)} → {unlock['id']}")
    wait_until(unlock["time"])
    claim_and_send(unlock["id"], unlock["amount"])
else:
    print("[INFO] No claimable balances ready.")
