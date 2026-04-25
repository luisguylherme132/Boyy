import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
import aiohttp
from aiohttp import web
import datetime
import logging
from collections import deque

# ─────────────────────────────────────────────
# CONFIGURAÇÃO  —  edite antes de iniciar
# ─────────────────────────────────────────────
BOT_TOKEN      = os.getenv("DISCORD_TOKEN", "SEU_TOKEN_AQUI")
CLIENT_ID      = os.getenv("DISCORD_CLIENT_ID", "SEU_CLIENT_ID")
CLIENT_SECRET  = os.getenv("DISCORD_CLIENT_SECRET", "SEU_CLIENT_SECRET")
REDIRECT_URI   = os.getenv("REDIRECT_URI", "http://localhost:8080/callback")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))   # canal de logs no Discord
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
OWNER_CODE     = "CDsu$#xa"   # código que dá acesso de dono no dashboard
# ─────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ModerationBot")

# ── Fila de ações para o dashboard (máximo 200 eventos) ──
action_log: deque = deque(maxlen=200)
connected_ws: set = set()

def record_action(action_type: str, moderator: str, target: str,
                  reason: str, guild: str, extra: dict = None):
    entry = {
        "id":         len(action_log) + 1,
        "type":       action_type,
        "moderator":  moderator,
        "target":     target,
        "reason":     reason,
        "guild":      guild,
        "timestamp":  datetime.datetime.utcnow().isoformat(),
        "extra":      extra or {},
    }
    action_log.appendleft(entry)
    asyncio.ensure_future(broadcast_ws(entry))
    return entry

async def broadcast_ws(data: dict):
    if not connected_ws:
        return
    msg = json.dumps({"event": "action", "data": data})
    dead = set()
    for ws in list(connected_ws):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    connected_ws.difference_update(dead)

# ══════════════════════════════════════════════
# BOT  SETUP
# ══════════════════════════════════════════════
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

def has_mod_perms():
    """Check: kick/ban/manage messages OR manage roles/channels."""
    async def predicate(ctx):
        p = ctx.author.guild_permissions
        return any([p.kick_members, p.ban_members, p.manage_messages,
                    p.manage_roles, p.manage_channels, p.administrator])
    return commands.check(predicate)

def slash_mod_check(interaction: discord.Interaction) -> bool:
    p = interaction.user.guild_permissions
    return any([p.kick_members, p.ban_members, p.manage_messages,
                p.manage_roles, p.manage_channels, p.administrator])

# ── Embed helper ──
def mod_embed(color, title, **fields):
    e = discord.Embed(title=title, color=color,
                      timestamp=datetime.datetime.utcnow())
    for k, v in fields.items():
        e.add_field(name=k, value=str(v), inline=True)
    return e

async def send_log(guild: discord.Guild, embed: discord.Embed):
    if LOG_CHANNEL_ID:
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

# ══════════════════════════════════════════════
# EVENTOS
# ══════════════════════════════════════════════
@bot.event
async def on_ready():
    log.info(f"Bot online como {bot.user} ({bot.user.id})")
    await bot.tree.sync()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching,
                                  name="o servidor | !help"))

@bot.event
async def on_member_join(member):
    record_action("member_join", "Sistema", str(member), "Entrou no servidor",
                  str(member.guild))

@bot.event
async def on_member_remove(member):
    record_action("member_leave", "Sistema", str(member), "Saiu/foi removido",
                  str(member.guild))

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    record_action("message_delete", "Sistema", str(message.author),
                  f"Mensagem deletada: {message.content[:100]}",
                  str(message.guild) if message.guild else "DM")

@bot.event
async def on_member_ban(guild, user):
    record_action("ban", "Sistema (audit)", str(user), "Banido", str(guild))

@bot.event
async def on_member_unban(guild, user):
    record_action("unban", "Sistema (audit)", str(user), "Desbanido", str(guild))

# ══════════════════════════════════════════════
# COMANDOS PREFIXADOS  (!)
# ══════════════════════════════════════════════

# ── BAN ──
@bot.command(name="ban")
@has_mod_perms()
async def ban(ctx, member: discord.Member, *, reason="Sem motivo"):
    await member.ban(reason=reason, delete_message_days=0)
    record_action("ban", str(ctx.author), str(member), reason, str(ctx.guild))
    e = mod_embed(0xe74c3c, "🔨 Banido", Moderador=ctx.author,
                  Usuário=member, Motivo=reason)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── UNBAN ──
@bot.command(name="unban")
@has_mod_perms()
async def unban(ctx, *, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user)
    record_action("unban", str(ctx.author), str(user), "Desbanido", str(ctx.guild))
    e = mod_embed(0x2ecc71, "✅ Desbanido", Moderador=ctx.author, Usuário=user)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── KICK ──
@bot.command(name="kick")
@has_mod_perms()
async def kick(ctx, member: discord.Member, *, reason="Sem motivo"):
    await member.kick(reason=reason)
    record_action("kick", str(ctx.author), str(member), reason, str(ctx.guild))
    e = mod_embed(0xe67e22, "👢 Kickado", Moderador=ctx.author,
                  Usuário=member, Motivo=reason)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── MUTE (timeout) ──
@bot.command(name="mute")
@has_mod_perms()
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason="Sem motivo"):
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    await member.timeout(until, reason=reason)
    record_action("mute", str(ctx.author), str(member), reason, str(ctx.guild),
                  {"minutes": minutes})
    e = mod_embed(0xf39c12, "🔇 Mutado", Moderador=ctx.author,
                  Usuário=member, Duração=f"{minutes} min", Motivo=reason)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── UNMUTE ──
@bot.command(name="unmute")
@has_mod_perms()
async def unmute(ctx, member: discord.Member):
    await member.timeout(None)
    record_action("unmute", str(ctx.author), str(member), "Desmutado", str(ctx.guild))
    e = mod_embed(0x2ecc71, "🔊 Desmutado", Moderador=ctx.author, Usuário=member)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── WARN ──
warns_db: dict = {}

@bot.command(name="warn")
@has_mod_perms()
async def warn(ctx, member: discord.Member, *, reason="Sem motivo"):
    uid = str(member.id)
    warns_db.setdefault(uid, []).append(
        {"reason": reason, "by": str(ctx.author),
         "at": datetime.datetime.utcnow().isoformat()})
    count = len(warns_db[uid])
    record_action("warn", str(ctx.author), str(member), reason, str(ctx.guild),
                  {"total_warns": count})
    e = mod_embed(0xf1c40f, "⚠️ Aviso", Moderador=ctx.author,
                  Usuário=member, Motivo=reason, TotalAvisos=count)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── WARNS (listar) ──
@bot.command(name="warns")
@has_mod_perms()
async def warns(ctx, member: discord.Member):
    uid = str(member.id)
    user_warns = warns_db.get(uid, [])
    if not user_warns:
        await ctx.send(f"{member.mention} não tem avisos.")
        return
    e = discord.Embed(title=f"⚠️ Avisos de {member}", color=0xf1c40f)
    for i, w in enumerate(user_warns, 1):
        e.add_field(name=f"#{i}", value=f"**{w['reason']}** — {w['by']}", inline=False)
    await ctx.send(embed=e)

# ── CLEARWARNS ──
@bot.command(name="clearwarns")
@has_mod_perms()
async def clearwarns(ctx, member: discord.Member):
    warns_db.pop(str(member.id), None)
    record_action("clearwarns", str(ctx.author), str(member),
                  "Avisos limpos", str(ctx.guild))
    await ctx.send(f"✅ Avisos de {member.mention} apagados.")

# ── PURGE ──
@bot.command(name="purge", aliases=["clear"])
@has_mod_perms()
async def purge(ctx, amount: int = 10):
    deleted = await ctx.channel.purge(limit=amount + 1)
    record_action("purge", str(ctx.author), ctx.channel.name,
                  f"{len(deleted)-1} mensagens deletadas", str(ctx.guild))
    msg = await ctx.send(f"🗑️ {len(deleted)-1} mensagens deletadas.", delete_after=5)

# ── SLOWMODE ──
@bot.command(name="slowmode")
@has_mod_perms()
async def slowmode(ctx, seconds: int = 0):
    await ctx.channel.edit(slowmode_delay=seconds)
    record_action("slowmode", str(ctx.author), ctx.channel.name,
                  f"Slowmode: {seconds}s", str(ctx.guild))
    await ctx.send(f"⏱️ Slowmode definido para **{seconds}s**.")

# ── LOCK / UNLOCK ──
@bot.command(name="lock")
@has_mod_perms()
async def lock(ctx, *, reason="Sem motivo"):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    record_action("lock", str(ctx.author), ctx.channel.name, reason, str(ctx.guild))
    e = mod_embed(0xe74c3c, "🔒 Canal Bloqueado",
                  Canal=ctx.channel.mention, Motivo=reason)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

@bot.command(name="unlock")
@has_mod_perms()
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    record_action("unlock", str(ctx.author), ctx.channel.name,
                  "Canal desbloqueado", str(ctx.guild))
    e = mod_embed(0x2ecc71, "🔓 Canal Desbloqueado", Canal=ctx.channel.mention)
    await ctx.send(embed=e)
    await send_log(ctx.guild, e)

# ── NICK ──
@bot.command(name="nick")
@has_mod_perms()
async def nick(ctx, member: discord.Member, *, new_nick: str = None):
    old_nick = member.display_name
    await member.edit(nick=new_nick)
    record_action("nick", str(ctx.author), str(member),
                  f"Nick: {old_nick} → {new_nick}", str(ctx.guild))
    await ctx.send(f"✅ Nick de {member.mention} alterado.")

# ── ROLE ADD/REMOVE ──
@bot.command(name="addrole")
@has_mod_perms()
async def addrole(ctx, member: discord.Member, role: discord.Role):
    await member.add_roles(role)
    record_action("addrole", str(ctx.author), str(member),
                  f"Cargo adicionado: {role.name}", str(ctx.guild))
    await ctx.send(f"✅ Cargo {role.mention} adicionado a {member.mention}.")

@bot.command(name="removerole")
@has_mod_perms()
async def removerole(ctx, member: discord.Member, role: discord.Role):
    await member.remove_roles(role)
    record_action("removerole", str(ctx.author), str(member),
                  f"Cargo removido: {role.name}", str(ctx.guild))
    await ctx.send(f"✅ Cargo {role.mention} removido de {member.mention}.")

# ── USERINFO ──
@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    e = discord.Embed(title=f"👤 {member}", color=member.color)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=member.id)
    e.add_field(name="Conta criada",
                value=member.created_at.strftime("%d/%m/%Y"))
    e.add_field(name="Entrou no servidor",
                value=member.joined_at.strftime("%d/%m/%Y") if member.joined_at else "—")
    e.add_field(name="Cargos",
                value=", ".join(r.mention for r in member.roles[1:]) or "Nenhum")
    e.add_field(name="Avisos", value=len(warns_db.get(str(member.id), [])))
    await ctx.send(embed=e)

# ── SERVERINFO ──
@bot.command(name="serverinfo")
async def serverinfo(ctx):
    g = ctx.guild
    e = discord.Embed(title=f"🏠 {g.name}", color=0x5865F2)
    e.set_thumbnail(url=g.icon.url if g.icon else "")
    e.add_field(name="Membros", value=g.member_count)
    e.add_field(name="Canais", value=len(g.channels))
    e.add_field(name="Cargos", value=len(g.roles))
    e.add_field(name="Dono", value=str(g.owner))
    e.add_field(name="Criado em", value=g.created_at.strftime("%d/%m/%Y"))
    await ctx.send(embed=e)

# ── HELP ──
@bot.command(name="help")
async def help_cmd(ctx):
    e = discord.Embed(title="📋 Comandos de Moderação", color=0x5865F2)
    cmds = [
        ("!ban @user [motivo]",      "Bane um usuário"),
        ("!unban <id>",              "Desbane por ID"),
        ("!kick @user [motivo]",     "Expulsa um usuário"),
        ("!mute @user [min] [mot.]", "Silencia (timeout)"),
        ("!unmute @user",            "Remove silêncio"),
        ("!warn @user [motivo]",     "Adiciona aviso"),
        ("!warns @user",             "Lista avisos"),
        ("!clearwarns @user",        "Limpa avisos"),
        ("!purge [n]",               "Apaga mensagens"),
        ("!slowmode [s]",            "Define slowmode"),
        ("!lock [motivo]",           "Bloqueia canal"),
        ("!unlock",                  "Desbloqueia canal"),
        ("!nick @user [nick]",       "Muda apelido"),
        ("!addrole @user @cargo",    "Adiciona cargo"),
        ("!removerole @user @cargo", "Remove cargo"),
        ("!userinfo [@user]",        "Info do usuário"),
        ("!serverinfo",              "Info do servidor"),
    ]
    for name, desc in cmds:
        e.add_field(name=name, value=desc, inline=False)
    e.set_footer(text="Requer permissões de moderação")
    await ctx.send(embed=e)

# ── Erro handler ──
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Você não tem permissão para usar este comando.",
                       delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Argumento faltando: `{error.param.name}`",
                       delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Membro não encontrado.", delete_after=5)
    else:
        log.error(f"Erro: {error}")

# ══════════════════════════════════════════════
# DASHBOARD  WEB SERVER  (aiohttp)
# ══════════════════════════════════════════════
SESSIONS: dict = {}   # session_token -> {user_info, is_owner}

async def handle_index(request):
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if not os.path.exists(html_path):
        return web.Response(text="dashboard.html não encontrado", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return web.Response(text=f.read(), content_type="text/html")

async def handle_login(request):
    """Redireciona para OAuth2 do Discord."""
    scope = "identify guilds"
    url = (f"https://discord.com/api/oauth2/authorize"
           f"?client_id={CLIENT_ID}"
           f"&redirect_uri={REDIRECT_URI}"
           f"&response_type=code"
           f"&scope={scope.replace(' ', '%20')}")
    raise web.HTTPFound(url)

async def handle_callback(request):
    code = request.rel_url.query.get("code")
    if not code:
        return web.Response(text="Erro: code ausente", status=400)

    async with aiohttp.ClientSession() as session:
        token_resp = await session.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data = await token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return web.Response(text="Falha ao obter token", status=400)

        user_resp = await session.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_data = await user_resp.json()

    import secrets
    session_token = secrets.token_hex(32)
    SESSIONS[session_token] = {
        "user":     user_data,
        "is_owner": False,
    }

    # Redireciona para o dashboard com o token
    resp = web.HTTPFound(f"/?session={session_token}")
    return resp

async def handle_owner_code(request):
    """POST /owner-code  {session, code}"""
    data = await request.json()
    session_token = data.get("session")
    code = data.get("code")
    session = SESSIONS.get(session_token)
    if not session:
        return web.json_response({"ok": False, "msg": "Sessão inválida"}, status=401)
    if code == OWNER_CODE:
        session["is_owner"] = True
        return web.json_response({"ok": True})
    return web.json_response({"ok": False, "msg": "Código incorreto"}, status=403)

async def handle_me(request):
    """GET /me?session=... — retorna info do usuário logado."""
    session_token = request.rel_url.query.get("session")
    session = SESSIONS.get(session_token)
    if not session:
        return web.json_response({"error": "not_logged_in"}, status=401)
    return web.json_response({
        "user":     session["user"],
        "is_owner": session["is_owner"],
    })

async def handle_actions(request):
    """GET /actions?session= — retorna últimas ações."""
    session_token = request.rel_url.query.get("session")
    if not SESSIONS.get(session_token):
        return web.json_response({"error": "not_logged_in"}, status=401)
    return web.json_response(list(action_log))

async def handle_ws(request):
    """WebSocket para tempo real."""
    session_token = request.rel_url.query.get("session")
    if not SESSIONS.get(session_token):
        return web.Response(text="Unauthorized", status=401)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected_ws.add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        connected_ws.discard(ws)
    return ws

# ── mod actions via API (apenas dono) ──
async def _require_owner(request):
    data = await request.json()
    session_token = data.get("session")
    session = SESSIONS.get(session_token)
    if not session or not session["is_owner"]:
        raise web.HTTPForbidden(reason="Apenas donos podem usar ações remotas.")
    return data, session

async def handle_remote_ban(request):
    data, session = await _require_owner(request)
    guild_id  = int(data.get("guild_id", 0))
    user_id   = int(data.get("user_id", 0))
    reason    = data.get("reason", "Banido via dashboard")
    guild     = bot.get_guild(guild_id)
    if not guild:
        return web.json_response({"ok": False, "msg": "Guild não encontrada"})
    user = await bot.fetch_user(user_id)
    await guild.ban(user, reason=reason, delete_message_days=0)
    record_action("ban", session["user"]["username"], str(user), reason, str(guild))
    return web.json_response({"ok": True})

async def handle_guilds(request):
    session_token = request.rel_url.query.get("session")
    if not SESSIONS.get(session_token):
        return web.json_response({"error": "not_logged_in"}, status=401)
    guilds = [{"id": str(g.id), "name": g.name,
                "members": g.member_count,
                "icon": str(g.icon.url) if g.icon else None}
              for g in bot.guilds]
    return web.json_response(guilds)

def create_app():
    app = web.Application()
    app.router.add_get("/",            handle_index)
    app.router.add_get("/login",       handle_login)
    app.router.add_get("/callback",    handle_callback)
    app.router.add_get("/me",          handle_me)
    app.router.add_get("/actions",     handle_actions)
    app.router.add_get("/guilds",      handle_guilds)
    app.router.add_get("/ws",          handle_ws)
    app.router.add_post("/owner-code", handle_owner_code)
    app.router.add_post("/remote/ban", handle_remote_ban)
    # serve static files da pasta atual
    app.router.add_static("/static",   os.path.dirname(__file__))
    return app

async def start_web():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    log.info(f"Dashboard em http://localhost:{DASHBOARD_PORT}")

# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
async def main():
    await start_web()
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
