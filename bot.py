import asyncio
from solana.rpc.websocket_api import connect
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature
import aiohttp
import logging
from datetime import datetime
import time

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger('websockets').setLevel(logging.WARNING)

# ===== CONFIGURATION =====
WALLET_ADDRESS = "WalletAddressHere"
TOKEN_ADDRESS = "TokenAddressHere"
TELEGRAM_BOT_TOKEN = "TelegramBotTokenHere"
TELEGRAM_CHAT_ID = "telegramChatIDHere"

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
SOLANA_WS_URL = "wss://api.mainnet-beta.solana.com"

# Polling interval in seconds (lower = faster detection, higher = less load)
POLL_INTERVAL = 2

class TokenMonitorPolling:
    def __init__(self, wallet_address, token_address, telegram_token, chat_id):
        self.wallet_address = wallet_address
        self.token_address = token_address
        self.telegram_token = telegram_token
        self.chat_id = chat_id
        self.wallet_pubkey = Pubkey.from_string(wallet_address)
        self.token_pubkey = Pubkey.from_string(token_address)
        self.http_client = None
        self.last_signatures = set()
        
    async def send_telegram_message(self, message):
        """Send notification via Telegram"""
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        logger.info("✅ Telegram notification sent")
                    else:
                        logger.error(f"Failed to send Telegram: {await response.text()}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
    
    async def get_token_account(self):
        """Get the token account address for our wallet + token"""
        try:
            if not self.http_client:
                self.http_client = AsyncClient(SOLANA_RPC_URL)
            
            from solders.rpc.config import RpcTokenAccountsFilterMint
            
            # Get token accounts owned by our wallet for our specific token
            # Pass strings to RPC to avoid type-mismatch between client versions
            # owner can be passed as a string, but RpcTokenAccountsFilterMint expects a Pubkey
            # Use TokenAccountOpts with mint Pubkey (matches solana.rpc.types.TokenAccountOpts)
            from solana.rpc.types import TokenAccountOpts

            opts = TokenAccountOpts(mint=self.token_pubkey)
            response = await self.http_client.get_token_accounts_by_owner(
                self.wallet_pubkey,
                opts
            )

            # response may be object-like or dict-like depending on client versions
            vals = getattr(response, 'value', None) or (response.get('value') if isinstance(response, dict) else None)
            if vals:
                first = vals[0]
                token_account = getattr(first, 'pubkey', None) or (first.get('pubkey') if isinstance(first, dict) else None)
                logger.info(f"📍 Token account: {token_account}")
                return token_account
            else:
                logger.warning("No token account found for this token")
                return None
                
        except Exception as e:
            logger.error(f"Error getting token account: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def get_recent_signatures(self, token_account, limit=10):
        """Get recent transaction signatures for the token account"""
        try:
            if not self.http_client:
                self.http_client = AsyncClient(SOLANA_RPC_URL)
            # ensure token_account is a string
            # The client expects a Pubkey object for address; accept either Pubkey or str
            addr = token_account
            try:
                # if token_account is a string, try to convert to Pubkey
                if isinstance(token_account, str):
                    addr = Pubkey.from_string(token_account)
            except Exception:
                addr = token_account

            resp = await self.http_client.get_signatures_for_address(
                addr,
                limit=limit
            )

            vals = getattr(resp, 'value', None) or (resp.get('value') if isinstance(resp, dict) else None)
            if not vals:
                return []

            sigs = []
            for item in vals:
                s = getattr(item, 'signature', None) or (item.get('signature') if isinstance(item, dict) else None)
                if s:
                    sigs.append(str(s))
            return sigs
            
        except Exception as e:
            import traceback
            logger.debug(f"Error getting signatures: {e}")
            logger.debug(traceback.format_exc())
            return []
    
    async def get_transaction_details(self, signature):
        """Fetch transaction details"""
        try:
            if not self.http_client:
                self.http_client = AsyncClient(SOLANA_RPC_URL)
            # Accept either Signature object or raw string; RPC can accept either depending on client
            sig_str = str(signature)
            try:
                sig_obj = Signature.from_string(sig_str)
            except Exception:
                sig_obj = None

            response = await self.http_client.get_transaction(
                sig_obj or sig_str,
                encoding="jsonParsed",
                max_supported_transaction_version=0
            )

            vals = getattr(response, 'value', None) or (response.get('value') if isinstance(response, dict) else None)
            if not vals:
                return None

            tx_value = response.value if hasattr(response, 'value') else response['value']
            # meta can be at tx_value.transaction.meta or as tx_value.get('meta')
            meta = getattr(getattr(tx_value, 'transaction', None), 'meta', None) or (tx_value.get('meta') if isinstance(tx_value, dict) else None)
            
            token_info = {
                "signature": str(signature),
                "slot": response.value.slot,
                "block_time": response.value.block_time,
                "transfers": []
            }
            
            # Parse token balance changes (robust to dict or object shapes)
            if meta and (hasattr(meta, 'post_token_balances') or (isinstance(meta, dict) and meta.get('post_token_balances'))):
                pre_list = getattr(meta, 'pre_token_balances', None) or (meta.get('pre_token_balances') if isinstance(meta, dict) else None)
                post_list = getattr(meta, 'post_token_balances', None) or (meta.get('post_token_balances') if isinstance(meta, dict) else None)

                pre_balances = {getattr(b, 'account_index', None) or b.get('account_index'): b for b in (pre_list or [])}
                post_balances = {getattr(b, 'account_index', None) or b.get('account_index'): b for b in (post_list or [])}

                for idx, post_bal in post_balances.items():
                    mint = getattr(post_bal, 'mint', None) or (post_bal.get('mint') if isinstance(post_bal, dict) else None)
                    if str(mint) == self.token_address:
                        pre_bal = pre_balances.get(idx)

                        pre_amount = float(getattr(getattr(pre_bal, 'ui_token_amount', None), 'ui_amount', None) or (pre_bal.get('ui_token_amount', {}).get('ui_amount') if isinstance(pre_bal, dict) else 0)) if pre_bal else 0
                        post_amount = float(getattr(getattr(post_bal, 'ui_token_amount', None), 'ui_amount', None) or (post_bal.get('ui_token_amount', {}).get('ui_amount') if isinstance(post_bal, dict) else 0))

                        change = post_amount - pre_amount

                        if change != 0:
                            owner = getattr(post_bal, 'owner', None) or (post_bal.get('owner') if isinstance(post_bal, dict) else None) or "Unknown"

                            token_info["transfers"].append({
                                "amount": abs(change),
                                "decimals": getattr(getattr(post_bal, 'ui_token_amount', None), 'decimals', None) or (post_bal.get('ui_token_amount', {}).get('decimals') if isinstance(post_bal, dict) else None),
                                "is_incoming": change > 0,
                                "owner": str(owner)
                            })
            
            return token_info
            
        except Exception as e:
            import traceback
            logger.debug(f"Error fetching transaction details for {signature}: {e}")
            logger.debug(traceback.format_exc())
            return None
    
    async def process_transaction(self, signature):
        """Process and notify about token transaction"""
        try:
            tx_details = await self.get_transaction_details(signature)
            
            if not tx_details or not tx_details["transfers"]:
                return
            
            for transfer in tx_details["transfers"]:
                direction = "received" if transfer["is_incoming"] else "sent"
                emoji = "📥" if transfer["is_incoming"] else "📤"
                
                amount = transfer["amount"]
                
                if tx_details["block_time"]:
                    time_str = datetime.fromtimestamp(tx_details["block_time"]).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                message = (
                    f"{emoji} <b>TOKEN TRANSFER DETECTED!</b>\n\n"
                    f"{'🟢 INCOMING' if transfer['is_incoming'] else '🔴 OUTGOING'}\n"
                    f"💰 Amount: <b>{amount:,.6f}</b>\n"
                    f"🪙 Token: <code>{self.token_address[:8]}...{self.token_address[-8:]}</code>\n"
                    f"💼 Wallet: <code>{self.wallet_address[:8]}...{self.wallet_address[-8:]}</code>\n"
                    f"📝 Signature: <code>{str(signature)[:12]}...</code>\n"
                    f"🕐 Time: {time_str}\n\n"
                    f"🔗 <a href='https://solscan.io/tx/{signature}'>View on Solscan</a>\n"
                    f"🔗 <a href='https://solana.fm/tx/{signature}'>View on Solana FM</a>"
                )
                
                await self.send_telegram_message(message)
                logger.info(f"🎯 Notified: {direction} {amount} tokens")
            
        except Exception as e:
            logger.error(f"Error processing transaction: {e}")
    
    async def monitor_token_account(self):
        """Main monitoring loop using polling"""
        logger.info(f"🚀 Starting monitor for wallet: {self.wallet_address}")
        logger.info(f"🎯 Monitoring token: {self.token_address}")
        
        # Get token account
        token_account = await self.get_token_account()
        if not token_account:
            logger.error("❌ Could not find token account. Make sure you own this token!")
            return
        
        await self.send_telegram_message(
            f"✅ <b>Token Monitor Started</b>\n\n"
            f"🎯 Token: <code>{self.token_address}</code>\n"
            f"💼 Wallet: <code>{self.wallet_address}</code>\n"
            f"🕐 Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"⚡ Polling every {POLL_INTERVAL}s for new transactions"
        )
        
        # Get initial signatures as a baseline (don't notify on these)
        logger.info("📥 Getting initial transaction history (baseline only)...")
        initial_sigs = await self.get_recent_signatures(token_account, limit=10)
        self.last_signatures = set(str(s) for s in initial_sigs)
        logger.info(f"✅ Baseline: tracking {len(self.last_signatures)} recent transactions (will only notify on NEW ones)")
        
        logger.info(f"🔥 Now monitoring for new transactions (polling every {POLL_INTERVAL}s)...")
        
        check_count = 0
        consecutive_errors = 0
        backoff_delay = POLL_INTERVAL
        while True:
            try:
                await asyncio.sleep(backoff_delay)
                
                check_count += 1
                if check_count % 30 == 0:  # Log every minute
                    logger.info(f"💓 Heartbeat: Checked {check_count} times, monitoring active...")
                
                # Get recent signatures with error tracking
                recent_sigs = await self.get_recent_signatures(token_account, limit=10)
                
                if not recent_sigs:
                    # If we got an empty list, it might be an RPC error or no txs
                    consecutive_errors += 1
                    # Implement exponential backoff: increase delay with consecutive errors
                    backoff_delay = min(POLL_INTERVAL * (2 ** min(consecutive_errors - 1, 3)), 30)
                    if consecutive_errors > 10:
                        # After many failures, log a warning
                        logger.warning(f"⚠️  No signatures returned for {consecutive_errors} consecutive checks (backing off to {backoff_delay}s)")
                    continue
                else:
                    consecutive_errors = 0  # Reset on success
                    backoff_delay = POLL_INTERVAL  # Reset backoff delay
                
                recent_sig_strs = set(str(s) for s in recent_sigs)
                
                # Find new signatures
                new_sigs = recent_sig_strs - self.last_signatures
                
                if new_sigs:
                    logger.info(f"🆕 Found {len(new_sigs)} new transaction(s)!")
                    for sig in new_sigs:
                        logger.info(f"  Processing: {sig[:16]}...")
                        await self.process_transaction(sig)
                    
                    # Update tracked signatures
                    self.last_signatures = recent_sig_strs
                    
            except asyncio.CancelledError:
                logger.info("Monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                await asyncio.sleep(5)

async def main():
    if WALLET_ADDRESS == "YOUR_WALLET_ADDRESS_HERE":
        logger.error("❌ Please configure WALLET_ADDRESS")
        return
    
    if TOKEN_ADDRESS == "YOUR_TOKEN_ADDRESS_HERE":
        logger.error("❌ Please configure TOKEN_ADDRESS")
        return
    
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Please configure TELEGRAM_BOT_TOKEN")
        return
        
    if TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        logger.error("❌ Please configure TELEGRAM_CHAT_ID")
        return
    
    monitor = TokenMonitorPolling(
        wallet_address=WALLET_ADDRESS,
        token_address=TOKEN_ADDRESS,
        telegram_token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID
    )
    
    try:
        await monitor.monitor_token_account()
    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")

if __name__ == "__main__":
    asyncio.run(main())