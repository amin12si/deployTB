import asyncio
import functools
import json
import logging
import secrets

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ConversationHandler,
    MessageHandler, ContextTypes, filters,
)

import config
import db
import panel_client
from railway_client import RailwayClient, RailwayAPIError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

WAITING_TOKEN, WAITING_WORKSPACE, WAITING_LABEL, WAITING_IMPORT_FILE, WAITING_WORKSPACE_UPDATE = range(5)


def owner_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != config.OWNER_TELEGRAM_ID:
            return
        return await fn(update, context)
    return wrapper


# ── menus ───────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy Panel", callback_data="deploy_menu")],
        [InlineKeyboardButton("👤 Accounts", callback_data="accounts_menu")],
        [InlineKeyboardButton("📋 Manage Panels", callback_data="panels_menu")],
        [InlineKeyboardButton("💾 Backup / Import", callback_data="backup_menu")],
    ])


async def show_main_menu(update_or_query, edit=False):
    text = "What do you want to do?"
    if edit:
        await update_or_query.edit_message_text(text, reply_markup=main_menu_kb())
    else:
        await update_or_query.message.reply_text(text, reply_markup=main_menu_kb())


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update)


@owner_only
async def on_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_main_menu(query, edit=True)


# ── deploy flow ─────────────────────────────────────────────────────────

@owner_only
async def deploy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = db.list_accounts()
    if not accounts:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Account", callback_data="add_account")],
            [InlineKeyboardButton("⬅ Back", callback_data="main")],
        ])
        await query.edit_message_text("No accounts added yet.", reply_markup=kb)
        return

    rows = []
    for acc in accounts:
        n = db.count_panels_for_account(acc["id"])
        label = f"{acc['label']} ({n}/{config.MAX_PANELS_PER_ACCOUNT})"
        if n >= config.MAX_PANELS_PER_ACCOUNT:
            label = "🔒 " + label
        rows.append([InlineKeyboardButton(label, callback_data=f"deploy_to:{acc['id']}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main")])
    await query.edit_message_text("Deploy on which account?", reply_markup=InlineKeyboardMarkup(rows))


@owner_only
async def deploy_to_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":")[1])
    account = db.get_account(account_id)
    if account is None:
        await query.edit_message_text("That account no longer exists.")
        return

    if db.count_panels_for_account(account_id) >= config.MAX_PANELS_PER_ACCOUNT:
        await query.edit_message_text(
            f"{account['label']} already has the max of {config.MAX_PANELS_PER_ACCOUNT} panels.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="deploy_menu")]]),
        )
        return

    await query.edit_message_text(f"Deploying on {account['label']}…\n\n⏳ Creating project…")
    asyncio.create_task(run_deploy(account_id, query, context))


async def _edit(query, text):
    try:
        await query.edit_message_text(text)
    except Exception:
        pass


async def run_deploy(account_id: int, query, context: ContextTypes.DEFAULT_TYPE):
    account = db.get_account(account_id)
    token = db.get_account_token(account_id)
    client = RailwayClient(token)
    project_id = None
    try:
        n = db.count_panels_for_account(account_id) + 1
        project_name = f"vpn-panel-{secrets.token_hex(3)}"
        label = f"panel-{n}"

        workspace_id = account["workspace_id"]
        if not workspace_id:
            workspace_id = await client.resolve_workspace_id()  # best-effort, usually None
        try:
            project_id = await client.create_project(project_name, workspace_id=workspace_id)
        except RailwayAPIError as e:
            if "workspaceId" in str(e) or "workspace" in str(e).lower():
                raise RailwayAPIError(
                    f"{e} — set a Workspace ID for {account['label']} from its account detail view "
                    "(Ctrl/Cmd+K → \"Copy Active Workspace ID\" in the Railway dashboard), then try again."
                )
            raise

        await _edit(query, f"Deploying on {account['label']}…\n\n✅ Project created\n⏳ Creating service…")
        project = await client.get_project(project_id)
        environments = project["environments"]["edges"]
        if not environments:
            raise RailwayAPIError("project has no default environment")
        environment_id = environments[0]["node"]["id"]

        service_id = await client.create_service_from_repo(project_id, "panel", config.TARGET_REPO, config.TARGET_BRANCH)

        await _edit(query, f"Deploying on {account['label']}…\n\n✅ Service created\n⏳ Setting password…")
        admin_password = secrets.token_urlsafe(9)
        await client.set_variable(project_id, environment_id, service_id, "ADMIN_PASSWORD", admin_password)

        await _edit(query, f"Deploying on {account['label']}…\n\n✅ Password set\n⏳ Generating domain…")
        domain = await client.create_domain(service_id, environment_id)

        await _edit(query, f"Deploying on {account['label']}…\n\n✅ Domain ready\n⏳ Building & deploying…")
        deployment_id = await client.deploy(service_id, environment_id)

        status = "QUEUED"
        for _ in range(40):  # up to ~3.5 minutes
            await asyncio.sleep(5)
            status = await client.get_deployment_status(deployment_id)
            if status in ("SUCCESS", "FAILED", "CRASHED", "REMOVED"):
                break
            await _edit(query, f"Deploying on {account['label']}…\n\n✅ Domain ready\n⏳ Status: {status}")

        if status != "SUCCESS":
            raise RailwayAPIError(f"deployment ended with status {status}")

        # give the app a few seconds to actually start answering after a successful build
        healthy = False
        for _ in range(6):
            await asyncio.sleep(5)
            if await panel_client.check_health(domain):
                healthy = True
                break

        db.add_panel(account_id, label, project_id, service_id, environment_id, domain, admin_password)

        note = "" if healthy else "\n\n⚠️ Deployed, but /health isn't responding yet — give it a minute and check again."
        await _edit(
            query,
            f"✅ Deployed: {label}\n\n🔗 https://{domain}\n🔑 {admin_password}{note}",
        )
    except RailwayAPIError as e:
        kb = None
        if project_id:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Clean up partial deploy", callback_data=f"cleanup:{project_id}:{account_id}")]])
        try:
            await query.edit_message_text(f"❌ Deploy failed: {e}", reply_markup=kb)
        except Exception:
            pass
    except Exception as e:
        log.exception("deploy failed")
        try:
            await query.edit_message_text(f"❌ Deploy failed (unexpected error): {e}")
        except Exception:
            pass


@owner_only
async def cleanup_partial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, project_id, account_id = query.data.split(":")
    token = db.get_account_token(int(account_id))
    client = RailwayClient(token)
    try:
        await client.delete_project(project_id)
        await query.edit_message_text("Partial deploy cleaned up.", reply_markup=main_menu_kb())
    except RailwayAPIError as e:
        await query.edit_message_text(f"Couldn't clean up automatically: {e}\nYou may need to delete it manually in the Railway dashboard.")


# ── accounts ────────────────────────────────────────────────────────────

@owner_only
async def accounts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accounts = db.list_accounts()
    rows = []
    for acc in accounts:
        n = db.count_panels_for_account(acc["id"])
        flag = "⚠️ " if not acc["last_valid"] else ""
        rows.append([InlineKeyboardButton(f"{flag}{acc['label']} ({n}/{config.MAX_PANELS_PER_ACCOUNT})", callback_data=f"account:{acc['id']}")])
    rows.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main")])
    text = "Your Railway accounts:" if accounts else "No accounts added yet."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


@owner_only
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Paste the Railway account API token (from railway.com/account/tokens).\n\n/cancel to abort."
    )
    return WAITING_TOKEN


@owner_only
async def add_account_got_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    msg = await update.message.reply_text("Validating…")
    client = RailwayClient(token)
    try:
        me = await client.validate()
    except RailwayAPIError as e:
        await msg.edit_text(f"❌ Token didn't validate: {e}\n\nSend another token, or /cancel.")
        return WAITING_TOKEN

    context.user_data["pending_token"] = token
    suggested = me.get("name") or me.get("email") or "account"
    context.user_data["pending_suggested_label"] = suggested
    await msg.edit_text(
        f"✅ Valid — signed in as {suggested}.\n\n"
        "Railway now requires a Workspace ID to create projects on most accounts. "
        "In the Railway dashboard, press Ctrl/Cmd+K, search \"Copy Active Workspace ID\", "
        "and paste it here.\n\nSend \"skip\" if you're not sure — we'll try without one "
        "and you can add it later if deploys fail."
    )
    return WAITING_WORKSPACE


@owner_only
async def add_account_got_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    context.user_data["pending_workspace_id"] = None if raw.lower() == "skip" else raw
    await update.message.reply_text("What should I label this account as?")
    return WAITING_LABEL


@owner_only
async def add_account_got_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    label = update.message.text.strip()[:64] or context.user_data.get("pending_suggested_label", "account")
    token = context.user_data.pop("pending_token", None)
    workspace_id = context.user_data.pop("pending_workspace_id", None)
    context.user_data.pop("pending_suggested_label", None)
    if not token:
        await update.message.reply_text("Something went wrong, start over with /start.")
        return ConversationHandler.END
    db.add_account(label, token, workspace_id)
    note = "" if workspace_id else "\n\n⚠️ No Workspace ID set — deploys will fail if your account requires one. Add it from the account's detail view."
    await update.message.reply_text(f"✅ Added account: {label}{note}", reply_markup=main_menu_kb())
    return ConversationHandler.END


@owner_only
async def account_set_workspace_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":")[1])
    context.user_data["editing_workspace_account_id"] = account_id
    await query.edit_message_text(
        "Paste the Workspace ID (Ctrl/Cmd+K → \"Copy Active Workspace ID\" in the Railway dashboard), or /cancel."
    )
    return WAITING_WORKSPACE_UPDATE


@owner_only
async def account_set_workspace_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account_id = context.user_data.pop("editing_workspace_account_id", None)
    if account_id is None:
        await update.message.reply_text("Something went wrong, start over with /start.")
        return ConversationHandler.END
    workspace_id = update.message.text.strip()
    db.set_account_workspace_id(account_id, workspace_id)
    await update.message.reply_text("✅ Workspace ID updated.", reply_markup=main_menu_kb())
    return ConversationHandler.END


@owner_only
async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


@owner_only
async def account_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":")[1])
    account = db.get_account(account_id)
    if account is None:
        await query.edit_message_text("That account no longer exists.", reply_markup=main_menu_kb())
        return
    n = db.count_panels_for_account(account_id)
    status = "✅ valid" if account["last_valid"] else "⚠️ invalid/revoked (last check)"
    ws = account["workspace_id"] or "not set"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Panels", callback_data=f"acct_panels:{account_id}")],
        [InlineKeyboardButton("❤️ Check Health", callback_data=f"acct_health:{account_id}")],
        [InlineKeyboardButton("🏷 Set Workspace ID", callback_data=f"acct_set_ws:{account_id}")],
        [InlineKeyboardButton("🗑 Delete Account", callback_data=f"acct_delete:{account_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data="accounts_menu")],
    ])
    await query.edit_message_text(
        f"👤 {account['label']}\nStatus: {status}\nWorkspace ID: {ws}\n"
        f"Panels: {n}/{config.MAX_PANELS_PER_ACCOUNT}\nAdded: {account['created_at'][:10]}",
        reply_markup=kb,
    )


@owner_only
async def account_health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Checking…")
    account_id = int(query.data.split(":")[1])
    account = db.get_account(account_id)
    token = db.get_account_token(account_id)
    client = RailwayClient(token)
    try:
        me = await client.validate()
        db.set_account_validity(account_id, True)
        text = f"✅ {account['label']} is valid (signed in as {me.get('name') or me.get('email')})."
    except RailwayAPIError as e:
        db.set_account_validity(account_id, False)
        text = f"⚠️ {account['label']} failed validation: {e}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"account:{account_id}")]])
    await query.edit_message_text(text, reply_markup=kb)


@owner_only
async def account_panels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":")[1])
    panels = db.list_panels_for_account(account_id)
    rows = [[InlineKeyboardButton(p["label"], callback_data=f"panel:{p['id']}")] for p in panels]
    rows.append([InlineKeyboardButton("⬅ Back", callback_data=f"account:{account_id}")])
    text = "Panels on this account:" if panels else "No panels on this account yet."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


@owner_only
async def account_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":")[1])
    n = db.count_panels_for_account(account_id)
    if n == 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"acct_delete_do:{account_id}:keep")],
            [InlineKeyboardButton("Cancel", callback_data=f"account:{account_id}")],
        ])
        await query.edit_message_text("Delete this account from the bot?", reply_markup=kb)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 Delete account + {n} panel(s) on Railway", callback_data=f"acct_delete_do:{account_id}:wipe")],
        [InlineKeyboardButton("📤 Remove from bot only (leave panels running)", callback_data=f"acct_delete_do:{account_id}:keep")],
        [InlineKeyboardButton("Cancel", callback_data=f"account:{account_id}")],
    ])
    await query.edit_message_text(
        f"This account has {n} panel(s). Delete the panels on Railway too, or just remove the account from the bot?",
        reply_markup=kb,
    )


@owner_only
async def account_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, account_id, mode = query.data.split(":")
    account_id = int(account_id)
    account = db.get_account(account_id)

    if mode == "wipe":
        token = db.get_account_token(account_id)
        client = RailwayClient(token)
        errors = []
        for p in db.list_panels_for_account(account_id):
            try:
                await client.delete_project(p["railway_project_id"])
            except RailwayAPIError as e:
                errors.append(f"{p['label']}: {e}")
        if errors:
            await query.edit_message_text("Some panels failed to delete on Railway:\n" + "\n".join(errors) +
                                           "\n\nRemoving the account from the bot anyway.")
    db.delete_account(account_id)
    await query.edit_message_text(f"Deleted account: {account['label']}", reply_markup=main_menu_kb())


# ── panels ──────────────────────────────────────────────────────────────

@owner_only
async def panels_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    panels = db.list_panels()
    rows = []
    for p in panels:
        acc = db.get_account(p["account_id"])
        flag = "🔕 " if not p["alerts_enabled"] else ("⚠️ " if not p["last_health_ok"] else "")
        rows.append([InlineKeyboardButton(f"{flag}{p['label']} ({acc['label'] if acc else '?'})", callback_data=f"panel:{p['id']}")])
    if panels:
        rows.append([InlineKeyboardButton("❤️ Check All", callback_data="check_all")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="main")])
    text = "All panels:" if panels else "No panels deployed yet."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def render_panel_detail(query, panel_id: int):
    panel = db.get_panel(panel_id)
    if panel is None:
        await query.edit_message_text("That panel no longer exists.", reply_markup=main_menu_kb())
        return
    account = db.get_account(panel["account_id"])
    alerts = "🔔 on" if panel["alerts_enabled"] else "🔕 off"
    health = "✅ ok (last check)" if panel["last_health_ok"] else "⚠️ down (last check)"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open Panel", url=f"https://{panel['domain']}")],
        [InlineKeyboardButton("🔑 Rotate Password", callback_data=f"panel_rotate:{panel_id}")],
        [InlineKeyboardButton("🔄 Redeploy", callback_data=f"panel_redeploy:{panel_id}")],
        [InlineKeyboardButton(f"Alerts: {alerts} (tap to toggle)", callback_data=f"panel_toggle:{panel_id}")],
        [InlineKeyboardButton("🗑 Delete Panel", callback_data=f"panel_delete:{panel_id}")],
        [InlineKeyboardButton("⬅ Back", callback_data="panels_menu")],
    ])
    await query.edit_message_text(
        f"📦 {panel['label']}\nAccount: {account['label'] if account else '?'}\n\n"
        f"🔗 https://{panel['domain']}\n🔑 {panel['admin_password']}\n\n"
        f"Health: {health}\nCreated: {panel['created_at'][:10]}",
        reply_markup=kb,
    )


@owner_only
async def panel_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":")[1])
    await render_panel_detail(query, panel_id)


@owner_only
async def panel_toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    panel_id = int(query.data.split(":")[1])
    panel = db.get_panel(panel_id)
    db.set_panel_alerts(panel_id, not panel["alerts_enabled"])
    await query.answer("Alerts toggled")
    await render_panel_detail(query, panel_id)


@owner_only
async def panel_rotate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Rotating…")
    panel_id = int(query.data.split(":")[1])
    panel = db.get_panel(panel_id)
    new_password = secrets.token_urlsafe(9)
    ok = await panel_client.rotate_password(panel["domain"], panel["admin_password"], new_password)
    if not ok:
        await query.edit_message_text(
            "❌ Couldn't rotate the password — the panel may be down or the stored password is out of sync.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"panel:{panel_id}")]]),
        )
        return
    db.update_panel_password(panel_id, new_password)
    await query.edit_message_text(
        f"✅ Password rotated for {panel['label']}\n\n🔑 {new_password}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"panel:{panel_id}")]]),
    )


@owner_only
async def panel_redeploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Redeploying…")
    panel_id = int(query.data.split(":")[1])
    panel = db.get_panel(panel_id)
    account_id = panel["account_id"]
    token = db.get_account_token(account_id)
    client = RailwayClient(token)
    await query.edit_message_text(f"Redeploying {panel['label']}…")
    try:
        deployment_id = await client.deploy(panel["railway_service_id"], panel["railway_environment_id"])
        status = "QUEUED"
        for _ in range(40):
            await asyncio.sleep(5)
            status = await client.get_deployment_status(deployment_id)
            if status in ("SUCCESS", "FAILED", "CRASHED", "REMOVED"):
                break
            await _edit(query, f"Redeploying {panel['label']}…\n\nStatus: {status}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"panel:{panel_id}")]])
        if status == "SUCCESS":
            await query.edit_message_text(f"✅ Redeployed {panel['label']}", reply_markup=kb)
        else:
            await query.edit_message_text(f"❌ Redeploy ended with status {status}", reply_markup=kb)
    except RailwayAPIError as e:
        await query.edit_message_text(f"❌ Redeploy failed: {e}",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"panel:{panel_id}")]]))


@owner_only
async def panel_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":")[1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, delete", callback_data=f"panel_delete_do:{panel_id}")],
        [InlineKeyboardButton("Cancel", callback_data=f"panel:{panel_id}")],
    ])
    await query.edit_message_text("Delete this panel and its Railway project? This can't be undone.", reply_markup=kb)


@owner_only
async def panel_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    panel_id = int(query.data.split(":")[1])
    panel = db.get_panel(panel_id)
    token = db.get_account_token(panel["account_id"])
    client = RailwayClient(token)
    try:
        await client.delete_project(panel["railway_project_id"])
    except RailwayAPIError as e:
        await query.edit_message_text(f"Railway deletion failed ({e}) — removing from the bot anyway.")
    db.delete_panel(panel_id)
    await query.edit_message_text(f"Deleted panel: {panel['label']}", reply_markup=main_menu_kb())


@owner_only
async def check_all_panels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Checking all panels…")
    panels = db.list_panels()
    lines = []
    for p in panels:
        ok = await panel_client.check_health(p["domain"])
        db.set_panel_health(p["id"], ok)
        lines.append(f"{'✅' if ok else '⚠️'} {p['label']}")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="panels_menu")]])
    await query.edit_message_text("Health check results:\n\n" + ("\n".join(lines) if lines else "No panels."), reply_markup=kb)


# ── backup / import ─────────────────────────────────────────────────────

@owner_only
async def backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Export Backup", callback_data="do_backup")],
        [InlineKeyboardButton("⬆️ Import Backup", callback_data="do_import")],
        [InlineKeyboardButton("⬅ Back", callback_data="main")],
    ])
    await query.edit_message_text(
        "Backup includes all accounts (tokens stay encrypted) and panels.\n"
        "Import only works on a deploy using the same ENCRYPTION_KEY.",
        reply_markup=kb,
    )


@owner_only
async def do_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Exporting…")
    blob = db.export_all()
    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=json.dumps(json.loads(blob), indent=2).encode(),
        filename="bot-backup.json",
        caption="Backup exported. Keep this file private — tokens are encrypted but this is still sensitive.",
    )


@owner_only
async def do_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Send the backup .json file now, or /cancel.")
    return WAITING_IMPORT_FILE


@owner_only
async def do_import_got_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc is None:
        await update.message.reply_text("That's not a file. Send the backup .json, or /cancel.")
        return WAITING_IMPORT_FILE
    f = await doc.get_file()
    raw = await f.download_as_bytearray()
    try:
        n_accounts, n_panels = db.import_all(raw.decode())
    except ValueError as e:
        await update.message.reply_text(f"❌ Import failed: {e}")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed (possibly wrong ENCRYPTION_KEY): {e}")
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Imported {n_accounts} account(s) and {n_panels} panel(s).", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── health sweep ────────────────────────────────────────────────────────

async def health_sweep(context: ContextTypes.DEFAULT_TYPE):
    for account in db.list_accounts():
        token = db.get_account_token(account["id"])
        client = RailwayClient(token)
        try:
            await client.validate()
            valid = True
        except RailwayAPIError:
            valid = False
        was_valid = bool(account["last_valid"])
        if valid != was_valid:
            db.set_account_validity(account["id"], valid)
            msg = f"✅ Account {account['label']} is valid again." if valid else f"⚠️ Account {account['label']} failed validation — may be revoked or suspended."
            await context.bot.send_message(chat_id=config.OWNER_TELEGRAM_ID, text=msg)

    for panel in db.list_panels():
        if not panel["alerts_enabled"]:
            continue
        ok = await panel_client.check_health(panel["domain"])
        was_ok = bool(panel["last_health_ok"])
        if ok != was_ok:
            db.set_panel_health(panel["id"], ok)
            msg = f"✅ Panel {panel['label']} is back up." if ok else f"⚠️ Panel {panel['label']} is not responding at /health."
            await context.bot.send_message(chat_id=config.OWNER_TELEGRAM_ID, text=msg)


# ── wiring ──────────────────────────────────────────────────────────────

def main():
    db.init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))

    add_account_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_account_start, pattern="^add_account$")],
        states={
            WAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_got_token)],
            WAITING_WORKSPACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_got_workspace)],
            WAITING_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_got_label)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    app.add_handler(add_account_conv)

    set_workspace_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(account_set_workspace_start, pattern="^acct_set_ws:")],
        states={
            WAITING_WORKSPACE_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, account_set_workspace_got)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    app.add_handler(set_workspace_conv)

    import_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(do_import_start, pattern="^do_import$")],
        states={
            WAITING_IMPORT_FILE: [MessageHandler(filters.Document.ALL, do_import_got_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    app.add_handler(import_conv)

    app.add_handler(CallbackQueryHandler(on_main_callback, pattern="^main$"))
    app.add_handler(CallbackQueryHandler(deploy_menu, pattern="^deploy_menu$"))
    app.add_handler(CallbackQueryHandler(deploy_to_account, pattern="^deploy_to:"))
    app.add_handler(CallbackQueryHandler(cleanup_partial, pattern="^cleanup:"))

    app.add_handler(CallbackQueryHandler(accounts_menu, pattern="^accounts_menu$"))
    app.add_handler(CallbackQueryHandler(account_detail, pattern="^account:"))
    app.add_handler(CallbackQueryHandler(account_health_check, pattern="^acct_health:"))
    app.add_handler(CallbackQueryHandler(account_panels, pattern="^acct_panels:"))
    app.add_handler(CallbackQueryHandler(account_delete_confirm, pattern="^acct_delete:"))
    app.add_handler(CallbackQueryHandler(account_delete_do, pattern="^acct_delete_do:"))

    app.add_handler(CallbackQueryHandler(panels_menu, pattern="^panels_menu$"))
    app.add_handler(CallbackQueryHandler(panel_detail, pattern="^panel:"))
    app.add_handler(CallbackQueryHandler(panel_toggle_alerts, pattern="^panel_toggle:"))
    app.add_handler(CallbackQueryHandler(panel_rotate, pattern="^panel_rotate:"))
    app.add_handler(CallbackQueryHandler(panel_redeploy, pattern="^panel_redeploy:"))
    app.add_handler(CallbackQueryHandler(panel_delete_confirm, pattern="^panel_delete:"))
    app.add_handler(CallbackQueryHandler(panel_delete_do, pattern="^panel_delete_do:"))
    app.add_handler(CallbackQueryHandler(check_all_panels, pattern="^check_all$"))

    app.add_handler(CallbackQueryHandler(backup_menu, pattern="^backup_menu$"))
    app.add_handler(CallbackQueryHandler(do_backup, pattern="^do_backup$"))

    app.job_queue.run_repeating(health_sweep, interval=config.HEALTH_SWEEP_INTERVAL_HOURS * 3600, first=120)

    log.info("bot starting (polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
