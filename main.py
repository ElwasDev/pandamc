import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
import secrets
import urllib.parse
import urllib.request
try:
    import requests as _requests
except ImportError:
    _requests = None
from datetime import datetime, timedelta
from flask import Flask, send_from_directory, jsonify, request, redirect, session
import threading

# ─────────────────────────────────────────
#  EMOJI MAPPING
# ─────────────────────────────────────────
EMOJI_MAPPING = {
    'nightbox':       'nightbox',
    'minecraft':      'minecraft',
    'cohete_nightbox': 'cohete_nightbox',
}

def get_emoji(guild, name):
    """Busca un emoji por nombre en el servidor y lo devuelve como string."""
    if not name or not guild:
        return ''
    for emoji in guild.emojis:
        if emoji.name == name:
            return str(emoji)
    return ''

# ─────────────────────────────────────────
#  SERVIDOR WEB (Flask)
# ─────────────────────────────────────────
app_web = Flask(__name__, static_folder='web')
app_web.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

DISCORD_CLIENT_ID     = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
WEB_URL               = os.environ.get("WEB_URL", "http://localhost:5000").rstrip("/")
print(f"DEBUG CLIENT_ID={DISCORD_CLIENT_ID!r}")
print(f"DEBUG CLIENT_SECRET={DISCORD_CLIENT_SECRET[:4] if DISCORD_CLIENT_SECRET else 'VACIO'}...")

DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL  = "https://discord.com/api/users/@me"

# URL de imagen de estado pendiente
IMG_PENDIENTE = "https://media.discordapp.net/attachments/1145130881124667422/1498136147115638944/content.png?ex=69f00f83&is=69eebe03&hm=acc054030e4c0a24546045d4b8308de96abd1ffebedf7589b722518c13062619&=&format=webp&quality=lossless&width=661&height=562"

def get_redirect_uri():
    return f"{WEB_URL}/callback"

# ─────────────────────────────────────────
#  ESTADO GLOBAL
# ─────────────────────────────────────────
# ID del único usuario autorizado para aceptar/rechazar postulaciones
STAFF_AUTORIZADO_ID = 1476355922883510302

# ID del servidor principal de NightBox
GUILD_ID = 1476355922883510293

postulaciones_web_pendientes = []
postulaciones_enviadas = set()   # discord_ids que ya enviaron formulario web
estado_postulaciones = {"abierto": True}

# Guarda message_id del DM enviado al postulante para poder editarlo después
# { discord_id (str) : dm_message_id (int) }
dm_mensajes_postulacion = {}

# ──────────────────────────────────────────
#  RUTAS WEB
# ──────────────────────────────────────────

@app_web.route('/')
def index():
    if not session.get("discord_user"):
        return send_from_directory('web', 'login.html')
    if not estado_postulaciones["abierto"]:
        return send_from_directory('web', 'cerrado.html')
    return send_from_directory('web', 'index.html')

@app_web.route('/login')
def login():
    params = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "redirect_uri":  get_redirect_uri(),
        "response_type": "code",
        "scope":         "identify",
    })
    return redirect(f"{DISCORD_AUTH_URL}?{params}")

@app_web.route('/callback')
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?error=no_code")

    try:
        data = urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  get_redirect_uri(),
        }).encode()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "DiscordBot (NightBox, 1.0)"
        }
        if _requests:
            r = _requests.post(DISCORD_TOKEN_URL, data=data, headers=headers)
            token_data = r.json()
        else:
            req = urllib.request.Request(DISCORD_TOKEN_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req) as resp:
                token_data = json.loads(resp.read())

        access_token = token_data.get("access_token")
        if not access_token:
            print(f"No access token, response: {token_data}")
            return redirect("/?error=no_token")

        if _requests:
            r2 = _requests.get(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}", "User-Agent": "DiscordBot (NightBox, 1.0)"})
            user_data = r2.json()
        else:
            req2 = urllib.request.Request(DISCORD_USER_URL, headers={"Authorization": f"Bearer {access_token}"})
            with urllib.request.urlopen(req2) as resp2:
                user_data = json.loads(resp2.read())

        session["discord_user"] = {
            "id":          user_data.get("id"),
            "username":    user_data.get("username"),
            "global_name": user_data.get("global_name") or user_data.get("username"),
            "avatar":      user_data.get("avatar"),
        }
        return redirect("/")

    except Exception as e:
        import traceback
        print(f"OAuth error: {e}")
        print(f"OAuth error detail: {traceback.format_exc()}")
        return redirect("/?error=oauth_failed")

@app_web.route('/logout')
def logout():
    session.clear()
    return redirect("/")

@app_web.route('/me')
def me():
    user = session.get("discord_user")
    if user:
        return jsonify({"ok": True, "user": user})
    return jsonify({"ok": False}), 401

@app_web.route('/ya_postulo')
def ya_postulo():
    """Devuelve si el usuario ya envió una postulación web."""
    user = session.get("discord_user")
    if not user:
        return jsonify({"enviado": False})
    enviado = user.get("id") in postulaciones_enviadas
    return jsonify({"enviado": enviado})

@app_web.route('/enviar', methods=['POST'])
def recibir_postulacion():
    user = session.get("discord_user")
    if not user:
        return jsonify({"ok": False, "error": "No autenticado"}), 401

    # ── Anti-duplicado ──
    if user.get("id") in postulaciones_enviadas:
        return jsonify({"ok": False, "error": "ya_postulo"}), 409

    data = None
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        pass
    if not data:
        try:
            data = json.loads(request.data.decode('utf-8'))
        except Exception:
            pass
    if not data:
        return jsonify({"ok": False, "error": "Sin datos"}), 400

    data["discord"]      = user.get("username")
    data["discord_id"]   = user.get("id")
    data["discord_name"] = user.get("global_name")

    # Marcar como enviado ANTES de procesar para evitar doble clic
    postulaciones_enviadas.add(user.get("id"))
    postulaciones_web_pendientes.append(data)
    return jsonify({"ok": True})

def iniciar_servidor_web():
    port = int(os.environ.get('PORT', 5000))
    app_web.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ─────────────────────────────────────────
#  BOT DE DISCORD
# ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

TOKEN = os.environ.get("TOKEN", "")
config = {
    "token": TOKEN,
    "categoria_postulaciones_id": int(os.environ.get("CATEGORIA_POSTULACIONES_ID", 0)) or None,
    "canal_revision_id":          int(os.environ.get("CANAL_REVISION_ID", 0)) or None,
    "canal_resultados_id":        int(os.environ.get("CANAL_RESULTADOS_ID", 0)) or None,
}

with open('preguntas.json', 'r', encoding='utf-8') as f:
    preguntas_data = json.load(f)

try:
    with open('imagenes.json', 'r', encoding='utf-8') as f:
        imagenes_config = json.load(f)
except:
    imagenes_config = {"imagen_aceptado": "", "imagen_rechazado": ""}

postulaciones_activas = {}

def guardar_config():
    pass

# ─────────────────────────────────────────
#  GENERADOR DE HTML DE POSTULACIÓN
# ─────────────────────────────────────────
def generar_html_postulacion(discord_tag, discord_name, discord_id, preguntas, respuestas_dict):
    """Genera un archivo HTML con todas las preguntas y respuestas del postulante."""
    filas = ""
    for i, pregunta in enumerate(preguntas):
        respuesta = respuestas_dict.get(i, respuestas_dict.get(f"p{i+1}", "Sin respuesta"))
        if not respuesta:
            respuesta = "Sin respuesta"
        respuesta_html = str(respuesta).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        fila_class = "par" if i % 2 == 0 else "impar"
        filas += f"""
        <div class="pregunta {fila_class}">
            <div class="num">P{i+1}</div>
            <div class="contenido">
                <div class="texto-pregunta">{pregunta}</div>
                <div class="texto-respuesta">{respuesta_html}</div>
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Postulación de {discord_name} — NightBox Staff</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', Arial, sans-serif;
    background: #0d0d0d;
    color: #e0e0e0;
    min-height: 100vh;
  }}
  header {{
    background: linear-gradient(135deg, #c0392b 0%, #8e0000 100%);
    padding: 28px 32px;
    display: flex;
    align-items: center;
    gap: 20px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
  }}
  header .logo {{ font-size: 2.2rem; }}
  header .info h1 {{ font-size: 1.5rem; font-weight: 700; color: #fff; }}
  header .info p {{ font-size: 0.9rem; color: rgba(255,255,255,0.75); margin-top: 4px; }}
  .badge {{
    display: inline-block;
    background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 20px;
    padding: 3px 12px;
    font-size: 0.78rem;
    color: #fff;
    margin-top: 6px;
  }}
  .meta {{
    background: #1a1a1a;
    border-bottom: 1px solid #2a2a2a;
    padding: 16px 32px;
    display: flex;
    gap: 32px;
    font-size: 0.88rem;
    color: #aaa;
  }}
  .meta span b {{ color: #e0e0e0; }}
  .container {{ max-width: 860px; margin: 32px auto; padding: 0 20px 48px; }}
  .titulo-seccion {{
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #c0392b;
    font-weight: 700;
    margin-bottom: 16px;
    padding-left: 4px;
  }}
  .pregunta {{
    display: flex;
    gap: 16px;
    padding: 18px 20px;
    border-radius: 10px;
    margin-bottom: 10px;
    border-left: 3px solid #c0392b;
    transition: transform 0.15s;
  }}
  .pregunta:hover {{ transform: translateX(3px); }}
  .par {{ background: #1c1c1c; }}
  .impar {{ background: #181818; }}
  .num {{
    min-width: 38px;
    height: 38px;
    background: #c0392b;
    color: #fff;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.78rem;
    font-weight: 700;
    flex-shrink: 0;
    margin-top: 2px;
  }}
  .contenido {{ flex: 1; }}
  .texto-pregunta {{ font-size: 0.85rem; color: #aaa; margin-bottom: 6px; font-weight: 500; }}
  .texto-respuesta {{ font-size: 1rem; color: #e8e8e8; line-height: 1.5; }}
  footer {{
    text-align: center;
    padding: 24px;
    font-size: 0.78rem;
    color: #444;
    border-top: 1px solid #1e1e1e;
  }}
</style>
</head>
<body>
<header>
  <div class="logo">🌙</div>
  <div class="info">
    <h1>Postulación de {discord_name}</h1>
    <p>Staff Team — NightBox</p>
    <span class="badge">📋 {len(preguntas)} preguntas respondidas</span>
  </div>
</header>
<div class="meta">
  <span>🎮 <b>{discord_tag}</b></span>
  <span>🆔 <b>{discord_id}</b></span>
</div>
<div class="container">
  <div class="titulo-seccion">Respuestas del postulante</div>
  {filas}
</div>
<footer>NightBox Staff · Sistema de Postulaciones · Documento generado automáticamente</footer>
</body>
</html>"""
    return html


# ─────────────────────────────────────────
#  TAREA: procesar postulaciones web
# ─────────────────────────────────────────
async def procesar_postulaciones_web():
    await bot.wait_until_ready()
    while not bot.is_closed():
        if postulaciones_web_pendientes:
            data = postulaciones_web_pendientes.pop(0)
            try:
                print(f"📬 Procesando postulación de {data.get('discord', '?')}")
                await enviar_al_canal_revision_web(data)
                print(f"✅ Postulación enviada al canal correctamente")
            except Exception as e:
                import traceback
                print(f"❌ Error procesando postulación web: {e}")
                print(traceback.format_exc())
        await asyncio.sleep(3)

async def enviar_al_canal_revision_web(data):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("❌ No se encontró el servidor con ID", GUILD_ID)
        return

    canal_revision = None
    if config.get("canal_revision_id"):
        canal_revision = guild.get_channel(config["canal_revision_id"])
        if not canal_revision:
            try:
                canal_revision = await bot.fetch_channel(config["canal_revision_id"])
                print(f"✅ Canal encontrado via fetch: {canal_revision.id}")
            except Exception as e:
                print(f"❌ fetch_channel falló: {e}")
        else:
            print(f"✅ Canal encontrado en caché: {canal_revision.id}")
    if not canal_revision:
        canal_revision = discord.utils.get(guild.text_channels, name="postulaciones-staff")
        print(f"🔍 Buscando canal por nombre: {'encontrado' if canal_revision else 'NO encontrado'}")
    if not canal_revision:
        try:
            canal_revision = await guild.create_text_channel(name="postulaciones-staff")
            config["canal_revision_id"] = canal_revision.id
            print(f"✅ Canal creado: {canal_revision.id}")
        except Exception as e:
            print(f"❌ No se pudo crear el canal: {e}")
            return

    discord_tag  = data.get('discord', 'No especificado')
    discord_name = data.get('discord_name', discord_tag)
    discord_id   = data.get('discord_id', '')

    # Emojis
    nightbox_e = get_emoji(guild, EMOJI_MAPPING['nightbox']) or '🌙'
    arrow_e    = get_emoji(guild, '1383arrowright') or '➡️'

    preguntas = preguntas_data.get("preguntas", [])

    # ── Embed principal con info del postulante ──
    embed_main = discord.Embed(
        description=(
            f"{nightbox_e} **Postulacion De {discord_name}**\n"
            f"{arrow_e} **Discord:** {discord_tag}\n"
            f"{arrow_e} **ID:** `{discord_id}`"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    embed_main.set_footer(text="Enviado desde la página web · Verificado con Discord OAuth2")

    # ── Embeds de preguntas (máx 12 preguntas por embed para no saturar) ──
    CHUNK = 12
    embeds_preguntas = []
    for chunk_start in range(0, len(preguntas), CHUNK):
        chunk = preguntas[chunk_start:chunk_start + CHUNK]
        e = discord.Embed(color=discord.Color.red())
        for i, pregunta in enumerate(chunk):
            idx = chunk_start + i
            respuesta = data.get(f"p{idx+1}", "").strip() or "Sin respuesta"
            e.add_field(
                name=f"{arrow_e} P{idx+1}: {pregunta[:100]}",
                value=f"> {respuesta[:1000]}",
                inline=False
            )
        embeds_preguntas.append(e)

    view = BotonesRevision(int(discord_id) if discord_id else 0, discord_tag)

    # Primer mensaje: embed principal + botones
    await canal_revision.send(embed=embed_main, view=view)
    # Mensajes adicionales: un embed por grupo de preguntas
    for e in embeds_preguntas:
        await canal_revision.send(embed=e)

    # ── Enviar DM al usuario con estado PENDIENTE ──
    if discord_id:
        try:
            miembro = guild.get_member(int(discord_id))
            if not miembro:
                miembro = await guild.fetch_member(int(discord_id))
            if miembro:
                dm_embed = discord.Embed(
                    title="📬 HEMOS RECIBIDO TU POSTULACION",
                    description=(
                        "Esta notificación aclara que la recibimos correctamente.\n\n"
                        "Hemos recibido tu `postulación para formar parte del equipo staff de NightBox` "
                        "y se encuentra pendiente de revisión.\n"
                        "Desde ahora, hasta la resolución de la postulación, pueden pasar días. "
                        "Por favor, ten paciencia.\n\n"
                        "> Te notificaremos por este medio en cuanto el equipo tome una decisión.\n\n"
                        "📋 **Actualización del estado**\n"
                        "> Estado actual: `Pendiente`"
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now()
                )
                dm_embed.set_image(url=IMG_PENDIENTE)
                dm_embed.set_footer(text="NightBox Staff · Sistema de postulaciones")

                dm_msg = await miembro.send(embed=dm_embed)
                # Guardar el message_id del DM para editarlo después
                dm_mensajes_postulacion[str(discord_id)] = dm_msg.id
        except Exception as e:
            print(f"No se pudo enviar DM al postulante: {e}")

# ─────────────────────────────────────────
#  VISTAS / BOTONES
# ─────────────────────────────────────────
class BotonPostular(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Postularse (Web)",
            style=discord.ButtonStyle.link,
            url=os.environ.get("WEB_URL", "http://localhost:5000"),
            emoji="🌐"
        ))

    @discord.ui.button(label="Postularse (Chat)", style=discord.ButtonStyle.primary, custom_id="postular_button", emoji="⛏️")
    async def postular_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in postulaciones_activas:
            await interaction.response.send_message("❌ Ya tienes una postulación en proceso.", ephemeral=True)
            return

        guild = interaction.guild
        categoria = None
        if config.get("categoria_postulaciones_id"):
            categoria = discord.utils.get(guild.categories, id=config["categoria_postulaciones_id"])
        if not categoria:
            categoria = discord.utils.get(guild.categories, name="📝 Postulaciones")
            if not categoria:
                try:
                    categoria = await guild.create_category("📝 Postulaciones")
                    config["categoria_postulaciones_id"] = categoria.id
                except Exception as e:
                    await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
                    return

        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            canal = await categoria.create_text_channel(
                name=f"🔨・postulacion-{interaction.user.name}",
                overwrites=overwrites
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Error al crear canal: {e}", ephemeral=True)
            return

        postulaciones_activas[interaction.user.id] = {
            "canal_id": canal.id,
            "respuestas": {},
            "pregunta_actual": 0,
            "inicio": datetime.now().isoformat(),
            "tiempo_limite": datetime.now() + timedelta(minutes=34)
        }

        await interaction.response.send_message(
            f"> <:si_mineback:1454893106179735642> Canal creado: {canal.mention}", ephemeral=True
        )
        await iniciar_postulacion(canal, interaction.user)
        asyncio.create_task(temporizador_postulacion(canal, interaction.user.id, 34))


async def temporizador_postulacion(canal, user_id, minutos):
    await asyncio.sleep(minutos * 60)
    if user_id in postulaciones_activas:
        postulacion = postulaciones_activas[user_id]
        if postulacion["canal_id"] == canal.id:
            try:
                await canal.send("⏰ **Tiempo agotado.** El canal se cerrará en 10 segundos.")
                await asyncio.sleep(10)
                await canal.delete()
                del postulaciones_activas[user_id]
            except:
                pass


async def iniciar_postulacion(canal, usuario):
    guild = canal.guild
    nightbox_e  = get_emoji(guild, EMOJI_MAPPING['nightbox'])  or '🌙'
    minecraft_e = get_emoji(guild, EMOJI_MAPPING['minecraft']) or '⛏️'
    embed = discord.Embed(
        title=f"{nightbox_e} Proceso de Postulación — Staff NightBox",
        description=f"¡Hola {usuario.mention}! Bienvenido a tu canal privado de postulación.",
        color=discord.Color.red()
    )
    embed.add_field(name=f"{minecraft_e} Instrucciones", value=(
        "**1.** Responde cada pregunta de forma clara y detallada.\n"
        "**2.** Revisa tus respuestas antes de enviar.\n"
        "**3.** Tienes **34 minutos** para completar el proceso."
    ), inline=False)
    await canal.send(embed=embed)
    await enviar_pregunta(canal, usuario.id, 0)


async def enviar_pregunta(canal, user_id, indice):
    preguntas = preguntas_data["preguntas"]
    if indice >= len(preguntas):
        await finalizar_postulacion(canal, user_id)
        return
    await canal.send(f"**💬 Pregunta {indice + 1} de {len(preguntas)}:** {preguntas[indice]}")


async def finalizar_postulacion(canal, user_id):
    postulacion = postulaciones_activas.get(user_id)
    if not postulacion:
        return
    embed = discord.Embed(title="📋 Resumen de tu postulación", color=discord.Color.red())
    for i, pregunta in enumerate(preguntas_data["preguntas"]):
        embed.add_field(name=f"P{i+1}: {pregunta}", value=postulacion["respuestas"].get(i, "Sin respuesta")[:1024], inline=False)
    await canal.send(embed=embed, view=ConfirmarPostulacion(user_id))


class BotonesRevision(discord.ui.View):
    def __init__(self, user_id, username):
        super().__init__(timeout=None)
        self.user_id  = user_id
        self.username = username

    async def _get_canal_resultados(self, guild):
        canal = guild.get_channel(config.get("canal_resultados_id")) if config.get("canal_resultados_id") else None
        if not canal:
            canal = discord.utils.get(guild.text_channels, name="resultados-postulaciones")
        return canal

    async def _editar_dm_estado(self, guild, nuevo_estado: str, color: discord.Color, emoji_estado: str):
        """Edita el DM original del postulante para cambiar el estado."""
        usuario = guild.get_member(self.user_id)
        if not usuario:
            return
        dm_msg_id = dm_mensajes_postulacion.get(str(self.user_id))
        if not dm_msg_id:
            return
        try:
            dm_channel = await usuario.create_dm()
            dm_msg = await dm_channel.fetch_message(dm_msg_id)
            embed = dm_msg.embeds[0] if dm_msg.embeds else None
            if embed:
                embed_dict = embed.to_dict()
                desc = embed_dict.get("description", "")
                import re
                desc = re.sub(
                    r"> Estado actual: `[^`]+`",
                    f"> Estado actual: `{nuevo_estado}` {emoji_estado}",
                    desc
                )
                embed_dict["description"] = desc
                embed_dict["color"] = color.value
                embed_dict.pop("image", None)
                new_embed = discord.Embed.from_dict(embed_dict)
                await dm_msg.edit(embed=new_embed)
        except Exception as e:
            print(f"No se pudo editar el DM: {e}")

    @discord.ui.button(label="Aceptar", style=discord.ButtonStyle.success, custom_id="aceptar_postulacion", emoji="✅")
    async def aceptar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != STAFF_AUTORIZADO_ID:
            await interaction.response.send_message("❌ No tienes permiso para realizar esta acción.", ephemeral=True)
            return
        guild     = interaction.guild
        canal_res = await self._get_canal_resultados(guild)
        usuario   = guild.get_member(self.user_id)

        if canal_res:
            nombre = usuario.mention if usuario else f"**{self.username}**"
            e = discord.Embed(
                title=f"[INGRESO] El postulante {self.username} fue admitido en el Staff de NightBox",
                description=(
                    f"{nombre} fue admitido en el Staff de NightBox\n\n"
                    "Al igual que los demás postulantes y staff, esperamos que logre alcanzar sus metas, "
                    "y demostrar lo mucho que vale dentro de NightBox.\n\n"
                    "> ➡ Recuerda que entrar al staff es solo el comienzo. Hay muchas etapas que aprobar una vez logres entrar.\n"
                    "> ¡Mantenerse y crecer es lo difícil!\n\n"
                    'Un día un sabio dijo... "*Las pequeñas cosas son las responsables de los **grandes cambios**"'
                ),
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            e.set_image(url="https://media.discordapp.net/attachments/1145130881124667422/1498136175339245588/content.png?ex=69f00f8a&is=69eebe0a&hm=6df4bd118fc527956c31c0977939d3d1abdd854ba3708fcedeeab851a1495b86&=&format=webp&quality=lossless&width=393&height=315")
            await canal_res.send(embed=e)

        if usuario:
            try:
                e_dm = discord.Embed(
                    title="✅ ACTUALIZACION DE TU POSTULACION",
                    description=(
                        "¡Tu postulación fue **aceptada**! ¡Bienvenido al equipo! 🎊\n\n"
                        "📋 **Actualización del estado**\n"
                        "> Estado actual: `Aceptado` ✅"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                e_dm.set_footer(text="NightBox Staff · Sistema de postulaciones")
                await usuario.send(embed=e_dm)
            except:
                pass

        await self._editar_dm_estado(guild, "Aceptado", discord.Color.green(), "✅")

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(color=discord.Color.green())
        embed.title = "✅ POSTULACIÓN ACEPTADA"
        embed.color = discord.Color.green()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"> ✅ Aceptada por {interaction.user.mention}")

    @discord.ui.button(label="Rechazar", style=discord.ButtonStyle.danger, custom_id="rechazar_postulacion", emoji="❌")
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != STAFF_AUTORIZADO_ID:
            await interaction.response.send_message("❌ No tienes permiso para realizar esta acción.", ephemeral=True)
            return
        guild     = interaction.guild
        canal_res = await self._get_canal_resultados(guild)
        usuario   = guild.get_member(self.user_id)

        if canal_res:
            nombre = usuario.mention if usuario else f"**{self.username}**"
            e = discord.Embed(
                title=f"[RESULTADO] La postulación de {self.username} fue rechazada en el Staff de NightBox",
                description=(
                    f"{nombre} tu postulación para formar parte del Staff de NightBox ha sido revisada, "
                    "y en esta ocasión no ha sido aprobada.\n\n"
                    "Agradecemos el tiempo, esfuerzo e interés que mostraste al querer formar parte del equipo de NightBox.\n\n"
                    "> ➡ Recuerda: un rechazo no define tu capacidad. Siempre puedes mejorar, aprender y volver a intentarlo en el futuro.\n"
                    "> Cada experiencia es una oportunidad para crecer.\n\n"
                    'Un día un sabio dijo... "Los grandes logros nacen después de muchos intentos."'
                ),
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            e.set_image(url="https://media.discordapp.net/attachments/1145130881124667422/1498136189763588106/content.png?ex=69f00f8d&is=69eebe0d&hm=426f24866be5b9ded8ee44c20590a3a5cf6e939cdcc71fb7c947142b3935eb73&=&format=webp&quality=lossless&width=393&height=315")
            await canal_res.send(embed=e)

        if usuario:
            try:
                e_dm = discord.Embed(
                    title="❌ ACTUALIZACION DE TU POSTULACION",
                    description=(
                        "Tu postulación fue **rechazada**. Puedes reintentar en 14 días. 💪\n\n"
                        "📋 **Actualización del estado**\n"
                        "> Estado actual: `Rechazado` ❌"
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now()
                )
                e_dm.set_footer(text="NightBox Staff · Sistema de postulaciones")
                await usuario.send(embed=e_dm)
            except:
                pass

        await self._editar_dm_estado(guild, "Rechazado", discord.Color.red(), "❌")

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(color=discord.Color.red())
        embed.title = "❌ POSTULACIÓN RECHAZADA"
        embed.color = discord.Color.red()
        for item in self.children: item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"> ❌ Rechazada por {interaction.user.mention}")


class ConfirmarPostulacion(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="Enviar postulación", style=discord.ButtonStyle.success, emoji="✅")
    async def enviar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Esta no es tu postulación.", ephemeral=True)
            return

        postulacion = postulaciones_activas.get(self.user_id)
        if not postulacion:
            await interaction.response.send_message("❌ Error al encontrar tu postulación.", ephemeral=True)
            return

        guild = interaction.guild
        canal_revision = guild.get_channel(config.get("canal_revision_id")) if config.get("canal_revision_id") else None
        if not canal_revision:
            canal_revision = discord.utils.get(guild.text_channels, name="postulaciones-staff")
            if not canal_revision:
                try:
                    canal_revision = await guild.create_text_channel(name="postulaciones-staff")
                    config["canal_revision_id"] = canal_revision.id
                except: pass

        if canal_revision:
            # Emojis
            nightbox_e = get_emoji(interaction.guild, EMOJI_MAPPING['nightbox']) or '🌙'
            arrow_e    = get_emoji(interaction.guild, '1383arrowright') or '➡️'

            preguntas_lista = preguntas_data["preguntas"]

            # ── Embed principal ──
            embed_main = discord.Embed(
                description=(
                    f"{nightbox_e} **Postulacion De {interaction.user.display_name}**\n"
                    f"{arrow_e} **Discord:** {interaction.user}\n"
                    f"{arrow_e} **ID:** `{interaction.user.id}`"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed_main.set_thumbnail(url=interaction.user.display_avatar.url)
            embed_main.set_footer(text=f"Postulación de {interaction.user.name}")

            # ── Embeds de preguntas (máx 12 por embed) ──
            CHUNK = 12
            embeds_preguntas = []
            for chunk_start in range(0, len(preguntas_lista), CHUNK):
                chunk = preguntas_lista[chunk_start:chunk_start + CHUNK]
                e = discord.Embed(color=discord.Color.red())
                for i, pregunta in enumerate(chunk):
                    idx = chunk_start + i
                    respuesta = postulacion["respuestas"].get(idx, "Sin respuesta")
                    e.add_field(
                        name=f"{arrow_e} P{idx+1}: {pregunta[:100]}",
                        value=f"> {str(respuesta)[:1000]}",
                        inline=False
                    )
                embeds_preguntas.append(e)

            view = BotonesRevision(interaction.user.id, interaction.user.name)
            await canal_revision.send(embed=embed_main, view=view)
            for e in embeds_preguntas:
                await canal_revision.send(embed=e)

        await interaction.response.send_message("✅ **¡Postulación enviada!** Este canal se cerrará en 5 segundos.")

        try:
            dm_embed = discord.Embed(
                title="📬 HEMOS RECIBIDO TU POSTULACION",
                description=(
                    "Esta notificación aclara que la recibimos correctamente.\n\n"
                    "Hemos recibido tu `postulación para formar parte del equipo staff de NightBox` "
                    "y se encuentra pendiente de revisión.\n"
                    "Desde ahora, hasta la resolución de la postulación, pueden pasar días. "
                    "Por favor, ten paciencia.\n\n"
                    "> Te notificaremos por este medio en cuanto el equipo tome una decisión.\n\n"
                    "📋 **Actualización del estado**\n"
                    "> Estado actual: `Pendiente`"
                ),
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            dm_embed.set_image(url=IMG_PENDIENTE)
            dm_embed.set_footer(text="NightBox Staff · Sistema de postulaciones")
            dm_msg = await interaction.user.send(embed=dm_embed)
            dm_mensajes_postulacion[str(interaction.user.id)] = dm_msg.id
        except Exception as e:
            print(f"No se pudo enviar DM (chat): {e}")

        del postulaciones_activas[self.user_id]
        await asyncio.sleep(5)
        try: await interaction.channel.delete()
        except: pass

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Esta no es tu postulación.", ephemeral=True)
            return
        await interaction.response.send_message("❌ Postulación cancelada. Cerrando en 5 segundos.")
        if self.user_id in postulaciones_activas:
            del postulaciones_activas[self.user_id]
        await asyncio.sleep(5)
        try: await interaction.channel.delete()
        except: pass


# ─────────────────────────────────────────
#  EVENTOS Y COMANDOS
# ─────────────────────────────────────────

@bot.tree.command(name="abrir_postulaciones", description="Abre las postulaciones de staff")
@app_commands.checks.has_permissions(administrator=True)
async def abrir_postulaciones(interaction: discord.Interaction):
    estado_postulaciones["abierto"] = True
    postulaciones_enviadas.clear()
    dm_mensajes_postulacion.clear()
    embed = discord.Embed(
        title="✅ Postulaciones abiertas",
        description=(
            "Las postulaciones de staff están ahora **abiertas**.\n\n"
            "🔄 El historial de postulaciones fue reiniciado — todos pueden volver a postularse."
        ),
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="limpiar_postulacion", description="Permite a un usuario volver a postularse (resetea su postulación)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(usuario="El usuario al que quieres resetear la postulación")
async def limpiar_postulacion(interaction: discord.Interaction, usuario: discord.Member):
    uid = str(usuario.id)
    eliminado = uid in postulaciones_enviadas
    postulaciones_enviadas.discard(uid)
    dm_mensajes_postulacion.pop(uid, None)

    if eliminado:
        embed = discord.Embed(
            title="🔄 Postulación reseteada",
            description=f"{usuario.mention} puede volver a postularse.",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="⚠️ Sin postulación registrada",
            description=f"{usuario.mention} no tenía ninguna postulación enviada.",
            color=discord.Color.orange()
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="cerrar_postulaciones", description="Cierra las postulaciones de staff")
@app_commands.checks.has_permissions(administrator=True)
async def cerrar_postulaciones(interaction: discord.Interaction):
    estado_postulaciones["abierto"] = False
    embed = discord.Embed(title="🔒 Postulaciones cerradas", description="Las postulaciones de staff están ahora **cerradas**.", color=discord.Color.red())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def rotar_status():
    """Rota el status del bot entre dos actividades."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        total = len(postulaciones_enviadas)
        actividades = [
            discord.Activity(type=discord.ActivityType.watching, name="Revisando postulaciones"),
            discord.Activity(type=discord.ActivityType.watching, name=f"Postulaciones: {total} enviadas"),
        ]
        for actividad in actividades:
            await bot.change_presence(status=discord.Status.online, activity=actividad)
            await asyncio.sleep(10)


@bot.event
async def on_ready():
    print(f'✅ Bot conectado como {bot.user}')
    print(f'🌐 Página web activa con OAuth2 Discord')
    try:
        synced = await bot.tree.sync()
        print(f'✅ {len(synced)} comandos sincronizados')
    except Exception as e:
        print(f'❌ Error: {e}')
    bot.add_view(BotonPostular())
    bot.add_view(BotonesRevision(0, ""))
    bot.loop.create_task(procesar_postulaciones_web())
    bot.loop.create_task(rotar_status())
    print("✅ Sistema listo")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.author.id in postulaciones_activas:
        postulacion = postulaciones_activas[message.author.id]
        if message.channel.id == postulacion["canal_id"]:
            pregunta_actual = postulacion["pregunta_actual"]
            if pregunta_actual < len(preguntas_data["preguntas"]):
                postulacion["respuestas"][pregunta_actual] = message.content
                postulacion["pregunta_actual"] += 1
                try: await message.add_reaction("✅")
                except: pass
                try: await enviar_pregunta(message.channel, message.author.id, postulacion["pregunta_actual"])
                except Exception as e: print(f"Error: {e}")
    await bot.process_commands(message)


@bot.tree.command(name="setup_postulaciones", description="Configura el sistema de postulaciones (Solo administradores)")
@app_commands.checks.has_permissions(administrator=True)
async def setup_postulaciones(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    nightbox_e  = get_emoji(guild, EMOJI_MAPPING['nightbox'])        or '🌙'
    minecraft_e = get_emoji(guild, EMOJI_MAPPING['minecraft'])       or '⛏️'
    cohete_e    = get_emoji(guild, EMOJI_MAPPING['cohete_nightbox']) or '🚀'

    embed = discord.Embed(
        description=(
            f"# {nightbox_e} - ¡POSTULACIONES ABIERTAS!\n"
            "¿Estás interesado en ser parte del Staff-Team?\n"
            "Si es así, no esperes más. Esta es tu oportunidad. Postúlate dando clic en el botón de abajo.\n\n"
            "# Requisitos a cumplir:\n"
            f"{minecraft_e}: Tener mínimo 14 Años.\n"
            f"{minecraft_e}: Ser premium.\n"
            f"{minecraft_e}: Historial limpio en el servidor.\n"
            f"{minecraft_e}: No ser staff en otro servidor.\n"
            f"{minecraft_e}: Buena ortografía y madurez.\n\n"
            f"{cohete_e} - **¡Postúlate dando clic en el botón de abajo!**\n\n"
            f"{nightbox_e} | NightBox"
        ),
        color=discord.Color.red()
    )

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Postularse",
        style=discord.ButtonStyle.link,
        url=WEB_URL or "https://nighboxpostulaciones.up.railway.app/",
        emoji="🌐"
    ))

    await interaction.channel.send(embed=embed, view=view)
    await interaction.followup.send("✅ Configurado!", ephemeral=True)


@bot.tree.command(name="ayuda_postulaciones", description="Ayuda sobre el sistema")
async def ayuda_postulaciones(interaction: discord.Interaction):
    embed = discord.Embed(title="ℹ️ Ayuda - Postulaciones", color=discord.Color.red())
    embed.add_field(name="🌐 Web", value="Haz clic en el botón → inicia sesión con Discord → completa el formulario.", inline=False)
    embed.add_field(name="🔐 Seguridad", value="El sistema verifica tu identidad con Discord OAuth2.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────
#  ARRANQUE
# ─────────────────────────────────────────
if __name__ == "__main__":
    TOKEN = os.environ.get("TOKEN") or os.environ.get("token") or ""
    TOKEN = TOKEN.strip()
    print(f"DEBUG: TOKEN existe={bool(TOKEN)}, largo={len(TOKEN)}")
    if not TOKEN:
        print("❌ ERROR: Variable de entorno TOKEN no configurada.")
    else:
        hilo_web = threading.Thread(target=iniciar_servidor_web, daemon=True)
        hilo_web.start()
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("❌ Token inválido.")
        except Exception as e:
            print(f"❌ ERROR: {e}")
