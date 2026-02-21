"""
Telegram Grocery Voucher Agent
===============================
Polls the local Outlook desktop app for Cibus/Pluxee voucher emails,
saves codes + barcode images, and serves them via a Telegram bot.

Run:
    python main.py

Stop with Ctrl+C.
"""

import os
import re
import asyncio
import sqlite3
import logging
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
import pythoncom
import win32com.client
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIGURATION — edit this section before running
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN: str = "8348563448:AAEmK8EaCXPfO1cShm8ttkeIHuzJcrW-YEw"

# Telegram user IDs allowed to interact with the bot.
ALLOWED_USER_IDS: list[int] = [
    1820130828,  # my ID
    # girlfriend's ID — add here once you have it
]

# Email addresses of the Outlook accounts to monitor.
# Both must be signed into your local Outlook desktop app.
# Leave EMPTY [] to scan ALL accounts found in Outlook automatically.
OUTLOOK_ACCOUNT_EMAILS: list[str] = []

# Subject keyword used to filter inbound emails (Hebrew: "voucher").
EMAIL_SUBJECT_KEYWORD: str = "שובר"

# Local SQLite database file path.
DB_PATH: str = "vouchers.db"

# Folder where barcode images will be saved.
BARCODES_DIR: str = "barcodes"

# How often (in seconds) the ingestion job polls all mailboxes.
POLL_INTERVAL_SECONDS: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PER-USER CONVERSATION STATE
# Stores the pending target amount while user is choosing a store.
# { user_id: target_amount }
# ---------------------------------------------------------------------------
pending_amount: dict[int, int] = {}

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> None:
    """Create / auto-migrate the vouchers table."""
    Path(BARCODES_DIR).mkdir(exist_ok=True)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")  # allows concurrent readers + 1 writer
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS vouchers (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                code               TEXT    NOT NULL UNIQUE,
                amount             INTEGER NOT NULL,
                store              TEXT,
                status             TEXT    NOT NULL DEFAULT 'available',
                source_email       TEXT,
                assigned_to        INTEGER,
                barcode_image_path TEXT,
                date_added         DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        existing = {row[1] for row in con.execute("PRAGMA table_info(vouchers)")}
        for col, definition in [
            ("store",              "TEXT"),
            ("barcode_image_path", "TEXT"),
            ("assigned_to",        "INTEGER"),
        ]:
            if col not in existing:
                con.execute(f"ALTER TABLE vouchers ADD COLUMN {col} {definition}")
                logger.info("DB migration: added column '%s'.", col)
        con.commit()
        logger.info("Database ready: %s", db_path)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# EMAIL PARSING
# ---------------------------------------------------------------------------


def parse_subject(subject: str) -> tuple[int | None, str | None]:
    """
    Parse Cibus/Pluxee subject → (amount, store).

    Format: "שובר על סך ₪200.00 - שופרסל שלי נווה הדרים - ראשון לציון"
    May be prefixed with "Fw: " / "Fwd: ".
    """
    subject = re.sub(r"^(?:Fw|Fwd|Re)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()

    amount_match = re.search(r"₪\s*(\d+(?:\.\d+)?)", subject)
    amount = int(float(amount_match.group(1))) if amount_match else None

    store: str | None = None
    parts = subject.split(" - ")
    if len(parts) >= 2:
        store = parts[1].strip()

    return amount, store


def parse_email_body(body_text: str) -> str | None:
    """
    Extract barcode code from body — standalone 15-25 digit number on its own line.
    e.g. 91098085941400300563

    ╔══════════════════════════════════════════════╗
    ║  ADJUST REGEX HERE if format ever changes.   ║
    ╚══════════════════════════════════════════════╝
    """
    match = re.search(r"(?m)^\s*(\d{15,25})\s*$", body_text)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# COMBINATION ALGORITHM  (0/1 knapsack — best sum ≤ target)
# ---------------------------------------------------------------------------


def best_combination(
    vouchers: list[tuple[int, int]], target: int
) -> tuple[list[int], int]:
    """
    Find subset of (id, amount) vouchers summing as close to target as possible
    without exceeding it. Returns (chosen_ids, total).
    """
    dp: dict[int, list[int]] = {0: []}
    for vid, amt in vouchers:
        for current_sum, ids in list(dp.items()):
            new_sum = current_sum + amt
            if new_sum <= target and new_sum not in dp:
                dp[new_sum] = ids + [vid]
    best_sum = max(dp.keys())
    return dp[best_sum], best_sum


# ---------------------------------------------------------------------------
# INGESTION JOB
# ---------------------------------------------------------------------------


def _ingest_emails_sync(db_path: str) -> None:
    """Read from local Outlook via COM, parse voucher emails, store to DB."""
    pythoncom.CoInitialize()
    # Single connection for all inserts in this job run (B-1 fix).
    con = sqlite3.connect(db_path, timeout=15)
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")

        allowed_lower = [e.lower() for e in OUTLOOK_ACCOUNT_EMAILS]

        for account in ns.Accounts:
            email_addr: str = account.SmtpAddress or account.DisplayName

            if allowed_lower and email_addr.lower() not in allowed_lower:
                continue

            try:
                inbox = ns.Folders(account.DisplayName).Folders("Inbox")
            except Exception as exc:
                logger.warning("[%s] Could not open Inbox: %s", email_addr, exc)
                continue

            unread = inbox.Items.Restrict("[Unread] = True")
            matching = [
                msg for msg in unread
                if EMAIL_SUBJECT_KEYWORD.lower()
                in (getattr(msg, "Subject", "") or "").lower()
            ]

            if not matching:
                logger.info("[%s] No new voucher emails.", email_addr)
                continue

            logger.info("[%s] Found %d new email(s).", email_addr, len(matching))

            for msg in matching:
                subject: str = getattr(msg, "Subject", "") or ""
                body: str    = getattr(msg, "Body", "") or ""

                amount, store = parse_subject(subject)
                code          = parse_email_body(body)

                if not code or not amount:
                    logger.warning(
                        "[%s] Could not parse from '%s'. amount=%s code=%s — marking read to avoid re-fetch.",
                        email_addr, subject, amount, code,
                    )
                    try:
                        msg.UnRead = False
                        msg.Save()
                    except Exception as exc:
                        logger.warning("[%s] Could not mark unparseable email as read: %s", email_addr, exc)
                    continue

                # Save barcode GIF as relative path (N-3 fix — portable across moves).
                barcode_image_path: str | None = None
                try:
                    for i in range(1, msg.Attachments.Count + 1):
                        att = msg.Attachments.Item(i)
                        fname: str = att.FileName or ""
                        if fname.lower().endswith(".gif") and fname.lower().startswith("img"):
                            dest_rel = os.path.join(BARCODES_DIR, f"{code}.gif")
                            att.SaveAsFile(os.path.abspath(dest_rel))
                            barcode_image_path = dest_rel  # store relative path
                            logger.info("Saved barcode image: %s", dest_rel)
                            break
                except Exception as exc:
                    logger.warning("[%s] Could not save barcode image: %s", email_addr, exc)

                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO vouchers
                        (code, amount, store, source_email, barcode_image_path)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (code, amount, store, email_addr, barcode_image_path),
                )
                con.commit()
                inserted = cur.rowcount > 0

                # Always mark read — prevents infinite re-fetch of both new and
                # duplicate emails on every subsequent poll (N-2 fix).
                try:
                    msg.UnRead = False
                    msg.Save()
                except Exception as exc:
                    logger.warning("[%s] Could not mark email as read: %s", email_addr, exc)

                if inserted:
                    logger.info(
                        "[%s] Stored voucher: code=%s amount=%d store=%s",
                        email_addr, code, amount, store,
                    )
                else:
                    logger.info("[%s] Duplicate code=%s — marked read and skipped.", email_addr, code)

    finally:
        con.close()
        pythoncom.CoUninitialize()


async def ingest_emails_job(context: CallbackContext) -> None:
    """Scheduled job — offloads COM work to a thread."""
    db_path: str = context.bot_data["db_path"]
    logger.info("Running ingestion job (reading from local Outlook app)…")
    try:
        await asyncio.to_thread(_ingest_emails_sync, db_path)
    except Exception:
        # Catch anything (Outlook not open, COM error, etc.) so the
        # repeating job stays alive and retries on the next interval.
        logger.exception("Ingestion job failed — will retry in %d s.", POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------------


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


# ---------------------------------------------------------------------------
# VOUCHER DELIVERY HELPER
# ---------------------------------------------------------------------------


async def _deliver_vouchers(
    update: Update, user_id: int, db_path: str, target: int, store_substr: str
) -> None:
    """Claim and send vouchers for a confirmed (target, store) request."""
    # Single connection with BEGIN IMMEDIATE so the SELECT→compute→UPDATE
    # sequence is fully atomic — no other writer can claim the same rows
    # between the read and the update (CVE-2 fix).
    con = sqlite3.connect(db_path, timeout=15)
    con.isolation_level = None  # manual transaction control
    chosen_rows: list = []
    image_map: dict = {}
    total = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            """
            SELECT id, amount, barcode_image_path FROM vouchers
            WHERE status='available' AND store LIKE ?
            ORDER BY amount DESC
            """,
            (f"%{store_substr}%",),
        ).fetchall()

        if not rows:
            con.execute("ROLLBACK")
            await update.message.reply_text("No available vouchers for that store.")
            return

        voucher_list = [(r[0], r[1]) for r in rows]
        image_map    = {r[0]: r[2] for r in rows}

        chosen_ids, total = best_combination(voucher_list, target)

        if not chosen_ids:
            con.execute("ROLLBACK")
            await update.message.reply_text(f"Could not build any combination ≤ {target} ₪.")
            return

        chosen_rows = con.execute(
            f"""
            SELECT id, code, amount, store FROM vouchers
            WHERE id IN ({','.join('?' * len(chosen_ids))})
            """,
            chosen_ids,
        ).fetchall()
        for vid in chosen_ids:
            con.execute(
                "UPDATE vouchers SET status='pending', assigned_to=? WHERE id=?",
                (user_id, vid),
            )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()

    logger.info("User %d claimed ids=%s total=%d ₪", user_id, [r[0] for r in chosen_rows], total)

    note = "_✓ Exact match!_" if total == target else \
           f"_(Best possible: {total} ₪ — {target - total} ₪ short of {target} ₪)_"

    lines = [f"Here are your voucher(s) — *{total} ₪* total:\n{note}\n"]
    for vid, code, amount, store in chosen_rows:
        lines.append(f"• `{code}` — {amount} ₪ | {store or 'Unknown'}")
    lines.append("\nSend *used* when done.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    for vid, code, amount, store in chosen_rows:
        img_path = image_map.get(vid)
        if img_path and os.path.isfile(img_path):
            try:
                with open(img_path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=f"`{code}` — {amount} ₪",
                        parse_mode="Markdown",
                    )
            except Exception as exc:
                logger.warning("Could not send barcode image for %s: %s", code, exc)


MAX_VOUCHER_AMOUNT = 10_000  # ₪ — sanity cap on user input (B-6 fix)


# ---------------------------------------------------------------------------
# EXCEL IMPORT
# ---------------------------------------------------------------------------


def _fetch_barcode_from_pluxee_link(url: str, code: str) -> str | None:
    """
    Fetch the barcode image from a Pluxee voucher link.

    Pluxee page HTML:
      <title>שובר 91077380723491502920</title>
      <img src="bar.ashx?ekqmKn6G63Kjj_s6aE" />
    """
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        img_tag = soup.find("img")
        if not img_tag or not img_tag.get("src"):
            logger.warning("No <img> found at %s", url)
            return None

        # The barcode image src is relative to the page URL.
        bar_src = img_tag["src"]
        # Build the full image URL from the base of the page URL.
        base = url.rsplit("/", 1)[0]
        bar_url = f"{base}/{bar_src}"

        img_resp = requests.get(bar_url, timeout=15)
        img_resp.raise_for_status()

        dest = os.path.join(BARCODES_DIR, f"{code}.png")
        with open(dest, "wb") as f:
            f.write(img_resp.content)
        logger.info("Saved barcode from Pluxee link: %s", dest)
        return dest
    except Exception as exc:
        logger.warning("Could not fetch barcode from %s: %s", url, exc)
        return None


def import_excel(file_path: str, db_path: str) -> tuple[int, int, int]:
    """
    Import vouchers from an Excel file.

    Expected columns (flexible matching — first match wins):
      - Link / URL column containing https://myconsumers.pluxee.co.il/... links
      - Amount / price column (e.g. "שווי", "סכום", "amount", or ₪ in header)
      - Status column (e.g. "סטטוס", "status") — "used"/"נוצל" → skip

    The barcode CODE is extracted from the Pluxee page <title>.
    The barcode IMAGE is fetched from the page's <img src="bar.ashx?...">.

    Returns (imported, skipped_used, skipped_error).
    """
    Path(BARCODES_DIR).mkdir(exist_ok=True)
    df = pd.read_excel(file_path, engine="openpyxl")

    # --- Detect columns by scanning headers ---
    link_col = amount_col = status_col = store_col = None

    for col in df.columns:
        header = str(col).lower().strip()
        if link_col is None and ("link" in header or "url" in header or "קישור" in header):
            link_col = col
        elif amount_col is None and any(kw in header for kw in ("שווי", "סכום", "amount", "price", "₪")):
            amount_col = col
        elif status_col is None and any(kw in header for kw in ("סטטוס", "status", "מצב")):
            status_col = col
        elif store_col is None and any(kw in header for kw in ("חנות", "store", "סניף", "רשת")):
            store_col = col

    # If no link column found by header, scan all columns for Pluxee URLs.
    if link_col is None:
        for col in df.columns:
            sample = df[col].dropna().astype(str).head(5)
            if sample.str.contains("pluxee", case=False).any():
                link_col = col
                break

    # If no amount column found by header, look for a numeric column.
    if amount_col is None:
        for col in df.columns:
            if col == link_col or col == status_col:
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                amount_col = col
                break

    if link_col is None:
        raise ValueError(
            "Could not find a link/URL column in the Excel file. "
            "Make sure one column contains Pluxee voucher links."
        )

    logger.info(
        "Excel columns detected — link: %s, amount: %s, status: %s, store: %s",
        link_col, amount_col, status_col, store_col,
    )

    imported = skipped_used = skipped_error = 0
    con = sqlite3.connect(db_path, timeout=15)

    try:
        for _, row in df.iterrows():
            # --- Status: skip used vouchers ---
            if status_col is not None:
                raw_status = str(row.get(status_col, "")).strip().lower()
                if any(kw in raw_status for kw in ("used", "נוצל", "נוצלה", "מומש")):
                    skipped_used += 1
                    continue

            link = str(row.get(link_col, "")).strip()
            if not link or "pluxee" not in link.lower():
                skipped_error += 1
                continue

            # --- Amount ---
            amount: int | None = None
            if amount_col is not None:
                try:
                    raw_amt = row[amount_col]
                    # Handle "₪200" or "200.00" strings
                    if isinstance(raw_amt, str):
                        raw_amt = re.sub(r"[₪,\s]", "", raw_amt)
                    amount = int(float(raw_amt))
                except (ValueError, TypeError):
                    pass

            # --- Store ---
            store: str | None = None
            if store_col is not None:
                store = str(row.get(store_col, "")).strip() or None

            # --- Fetch the voucher page to get the barcode code ---
            try:
                resp = requests.get(link, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                # Extract code from <title>שובר 91077380723491502920</title>
                title_tag = soup.find("title")
                title_text = title_tag.get_text() if title_tag else ""
                code_match = re.search(r"(\d{15,25})", title_text)
                if not code_match:
                    # Fallback — try the <img alt="...">
                    img_tag = soup.find("img", alt=True)
                    if img_tag:
                        code_match = re.search(r"(\d{15,25})", img_tag["alt"])

                if not code_match:
                    logger.warning("Could not extract barcode code from %s", link)
                    skipped_error += 1
                    continue

                code = code_match.group(1)

                # If no amount from Excel, try to guess from page (unlikely)
                if amount is None:
                    logger.warning("No amount found for code %s — skipping.", code)
                    skipped_error += 1
                    continue

                # --- Download barcode image ---
                barcode_image_path = _fetch_barcode_from_pluxee_link(link, code)

                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO vouchers
                        (code, amount, store, source_email, barcode_image_path)
                    VALUES (?, ?, ?, 'excel-import', ?)
                    """,
                    (code, amount, store, barcode_image_path),
                )
                con.commit()

                if cur.rowcount > 0:
                    imported += 1
                    logger.info("Excel import: code=%s amount=%d store=%s", code, amount, store)
                else:
                    logger.info("Excel import: duplicate code=%s — skipped.", code)
                    skipped_error += 1

            except Exception as exc:
                logger.warning("Error processing row with link %s: %s", link, exc)
                skipped_error += 1
                continue
    finally:
        con.close()

    return imported, skipped_used, skipped_error


# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🛒 *Voucher Bot Commands*\n\n"
    "`?`  — show this help\n"
    "`inv`  — list all vouchers\n"
    "`grp inv`  — inventory grouped by store\n"
    "`status`  — summary count by amount\n"
    "`used`  — mark your pending voucher(s) as used\n"
    "`cancel`  — cancel current store selection\n"
    "`<amount>`  — e.g. `200`, `350` — find best combo → choose store from menu\n"
    "📎 Send an Excel file (.xlsx) to bulk-import vouchers\n"
)


async def start(update: Update, context: CallbackContext) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def handle_excel_upload(update: Update, context: CallbackContext) -> None:
    """Handle .xlsx file sent to the bot — bulk import vouchers."""
    if not is_authorized(update):
        return

    doc = update.message.document
    if not doc or not doc.file_name:
        return

    fname = doc.file_name.lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls")):
        await update.message.reply_text(
            "Please send an Excel file (.xlsx). Other file types are not supported."
        )
        return

    await update.message.reply_text("📥 Downloading and processing your Excel file…")

    try:
        tg_file = await doc.get_file()
        local_path = os.path.join(BARCODES_DIR, f"_import_{doc.file_name}")
        await tg_file.download_to_drive(local_path)

        db_path = context.bot_data["db_path"]
        imported, skipped_used, skipped_error = await asyncio.to_thread(
            import_excel, local_path, db_path
        )

        lines = [
            "✅ *Excel Import Complete*\n",
            f"• Imported: *{imported}* voucher(s)",
            f"• Skipped (used): {skipped_used}",
            f"• Skipped (error/duplicate): {skipped_error}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    except ValueError as exc:
        await update.message.reply_text(f"❌ Import failed: {exc}")
    except Exception as exc:
        logger.exception("Excel import failed")
        await update.message.reply_text(f"❌ Import failed: {exc}")
    finally:
        # Clean up the downloaded file
        try:
            os.remove(local_path)
        except Exception:
            pass


async def handle_message(update: Update, context: CallbackContext) -> None:
    logger.info(
        "Message from user_id=%s: %r",
        update.effective_user.id if update.effective_user else "??",
        (update.message.text or "")[:80] if update.message else "",
    )
    if not is_authorized(update):
        logger.warning(
            "Blocked unauthorized user_id=%s",
            update.effective_user.id if update.effective_user else "??",
        )
        return

    text: str = update.message.text.strip()
    db_path   = context.bot_data["db_path"]
    user_id   = update.effective_user.id
    lower     = text.lower()

    # ── Help ─────────────────────────────────────────────────────────────────
    if lower == "?":
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

    # ── Full inventory ────────────────────────────────────────────────────────
    elif lower == "inv":
        con = sqlite3.connect(db_path, timeout=10)
        try:
            rows = con.execute(
                "SELECT code, amount, store, status FROM vouchers ORDER BY store, amount, date_added"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            await update.message.reply_text("No vouchers in the database.")
            return

        lines = ["*Full Inventory*\n"]
        for code, amount, store, status in rows:
            emoji = {"available": "🟢", "pending": "🟡", "used": "🔴"}.get(status, "⚪")
            lines.append(f"{emoji} `{code}` — {amount} ₪ | {store or 'Unknown'} | _{status}_")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── Grouped inventory ─────────────────────────────────────────────────────
    elif lower == "grp inv":
        con = sqlite3.connect(db_path, timeout=10)
        try:
            rows = con.execute(
                """
                SELECT store, amount, status, COUNT(*) FROM vouchers
                GROUP BY store, amount, status ORDER BY store, amount
                """
            ).fetchall()
        finally:
            con.close()

        if not rows:
            await update.message.reply_text("No vouchers in the database.")
            return

        grouped: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        for store, amount, status, count in rows:
            grouped[store or "Unknown"][amount][status] = count

        lines = ["*Inventory by Store*\n"]
        for store in sorted(grouped):
            lines.append(f"🏪 *{store}*")
            for amount in sorted(grouped[store]):
                parts = [
                    f"{grouped[store][amount][s]} {s}"
                    for s in ("available", "pending", "used")
                    if grouped[store][amount].get(s)
                ]
                lines.append(f"   {amount} ₪ — {', '.join(parts)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── Status summary ────────────────────────────────────────────────────────
    elif lower == "status":
        con = sqlite3.connect(db_path, timeout=10)
        try:
            rows = con.execute(
                "SELECT amount, status, COUNT(*) FROM vouchers GROUP BY amount, status ORDER BY amount DESC"
            ).fetchall()
        finally:
            con.close()

        if not rows:
            await update.message.reply_text("No vouchers in the database.")
            return

        summary: dict = defaultdict(lambda: defaultdict(int))
        for amount, status, count in rows:
            summary[amount][status] = count

        lines = ["*Status Summary*\n"]
        for amount in sorted(summary.keys(), reverse=True):
            parts = [
                f"{summary[amount][s]} {s}"
                for s in ("available", "pending", "used")
                if summary[amount].get(s)
            ]
            lines.append(f"{amount} ₪: {', '.join(parts)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── Cancel store selection ────────────────────────────────────────────────
    elif lower == "cancel":
        if user_id in pending_amount:
            pending_amount.pop(user_id)
            await update.message.reply_text("Cancelled. Send an amount when you're ready.")
        else:
            await update.message.reply_text("Nothing to cancel.")

    # ── Mark used ─────────────────────────────────────────────────────────────
    elif lower == "used":
        pending_amount.pop(user_id, None)
        con = sqlite3.connect(db_path, timeout=10)
        try:
            rows = con.execute(
                "SELECT id FROM vouchers WHERE status='pending' AND assigned_to=? ORDER BY date_added DESC",
                (user_id,),
            ).fetchall()
            if rows:
                ids = [r[0] for r in rows]
                con.execute(
                    f"UPDATE vouchers SET status='used' WHERE id IN ({','.join('?'*len(ids))})",
                    ids,
                )
                con.commit()
                logger.info("User %d marked %d voucher(s) as used.", user_id, len(ids))
                await update.message.reply_text(f"Marked {len(ids)} voucher(s) as used ✓")
            else:
                await update.message.reply_text("No pending vouchers found for you.")
        finally:
            con.close()

    # ── STEP 2: user is choosing a store number from the menu ─────────────────
    elif user_id in pending_amount and re.fullmatch(r"\d+", text):
        choice = int(text)

        con = sqlite3.connect(db_path, timeout=10)
        try:
            store_rows = con.execute(
                "SELECT DISTINCT store FROM vouchers WHERE status='available' AND store IS NOT NULL ORDER BY store"
            ).fetchall()
        finally:
            con.close()

        stores = [r[0] for r in store_rows]

        # If the number is out of range for the store list, treat it as a NEW
        # amount request (user changed their mind and typed a different amount).
        if choice < 1 or choice > len(stores):
            # Fall through to Step 1 logic below by clearing state and re-routing.
            pending_amount.pop(user_id, None)
            target = choice

            if not stores:
                await update.message.reply_text("No available vouchers in the database.")
                return

            pending_amount[user_id] = target
            lines = [f"For *{target} ₪*, choose a store:\n"]
            for i, store in enumerate(stores, 1):
                lines.append(f"{i}. {store}")
            lines.append("\nReply with the store number.")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        target = pending_amount.pop(user_id)
        chosen_store = stores[choice - 1]

        await update.message.reply_text(
            f"Finding best combo for *{target} ₪* at _{chosen_store}_…",
            parse_mode="Markdown",
        )
        await _deliver_vouchers(update, user_id, db_path, target, chosen_store)

    # ── STEP 1: user sends an amount → show store selection menu ──────────────
    elif re.fullmatch(r"\d+", text):
        target = int(text)

        # Bounds check — reject 0 and unreasonably large amounts (B-6 fix).
        if target <= 0 or target > MAX_VOUCHER_AMOUNT:
            await update.message.reply_text(
                f"Please enter an amount between 1 and {MAX_VOUCHER_AMOUNT:,} ₪."
            )
            return

        con = sqlite3.connect(db_path, timeout=10)
        try:
            store_rows = con.execute(
                "SELECT DISTINCT store FROM vouchers WHERE status='available' AND store IS NOT NULL ORDER BY store"
            ).fetchall()
        finally:
            con.close()

        stores = [r[0] for r in store_rows]

        if not stores:
            await update.message.reply_text("No available vouchers in the database.")
            return

        # Save state — waiting for store selection reply.
        pending_amount[user_id] = target

        lines = [f"For *{target} ₪*, choose a store:\n"]
        for i, store in enumerate(stores, 1):
            lines.append(f"{i}. {store}")
        lines.append("\nReply with the store number.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ── Unknown ───────────────────────────────────────────────────────────────
    else:
        pending_amount.pop(user_id, None)
        await update.message.reply_text(
            "I didn't understand that.\n\n" + HELP_TEXT, parse_mode="Markdown"
        )


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main() -> None:
    init_db(DB_PATH)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.bot_data["db_path"] = DB_PATH

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(
        ingest_emails_job,
        interval=POLL_INTERVAL_SECONDS,
        first=10,
    )

    logger.info(
        "Bot started. Email check every %d s. Press Ctrl+C to stop.",
        POLL_INTERVAL_SECONDS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
