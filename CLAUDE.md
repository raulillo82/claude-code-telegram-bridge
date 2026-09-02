# Notas del proyecto para Claude Code

## Credenciales

- El `bot_token` de Telegram se resuelve en `bridge/bot.py::_load_bot_token`:
  si `config.json` trae `bot_token`, gana ese valor; si no, se descifra desde
  `~/.credentials/telegram-bot-token.gpg` (esquema centralizado de
  credenciales del `CLAUDE.md` global). El fichero cifrado debe quedar en
  modo `600` — `gpg --encrypt -o` lo recrea con el umask por defecto en cada
  sobrescritura, así que tras rotar el token hay que volver a aplicar
  `chmod 600` a mano.
- Descifrarlo en el arranque requiere que la clave GPG del usuario esté
  desbloqueada en `gpg-agent` en ese momento (ver sección "Firma de
  commits"/`gpg-unlock` del `CLAUDE.md` global). El servicio systemd usa
  `Restart=always`: si el proceso muere después de que expire la caché de la
  clave (8h tras `gpg-unlock`), se quedará reintentando sin arrancar hasta
  que alguien la desbloquee de nuevo.
- El logger de `httpx` se baja a `WARNING` deliberadamente en `bridge/bot.py`
  — a nivel `INFO` registra la URL completa de cada request a la API de
  Telegram, que incluye el token en claro (`.../bot<TOKEN>/...`). No subir
  ese nivel sin sustituir antes el logging por algo que no vuelque la URL.
- Si se rota el token (BotFather → "Revoke current token"), el nuevo valor
  nunca debería pasar por una sesión de Claude en texto plano — cifrarlo
  directamente desde `systemd-ask-password` sin mostrárselo al asistente, y
  verificar el arranque del servicio en vez de pedirle que confirme el
  valor.
